"""POST /v1/chat/completions (DESIGN 4장, 6.6)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import errors
from app.guard.extract_chat import input_refs, output_refs
from app.guard.policy import GuardUnavailable
from app.guard.refs import guard_refs
from app.guard.stream_chat import ChatStreamGuard
from app.guard.text_buffer import GuardBlocked
from app.sse import SSE_HEADERS, SSE_MEDIA_TYPE, encode_event, parse_sse
from app.state import get_state, read_json_body, require_model
from app.upstream.routing import resolve

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/chat/completions")
async def create_chat_completion(request: Request):
    gw = get_state(request)
    s = gw.settings
    body = await read_json_body(request)
    model = require_model(body)
    if not isinstance(body.get("messages"), list):
        raise errors.invalid_request("'messages' must be an array.", param="messages")
    target = resolve(s, model)
    meta = {"model": model, "endpoint": "chat"}

    if s.guard_input_enabled:
        await guard_refs(
            gw.policy, input_refs(body), "input", endpoint="chat", tool_mask_policy=s.tool_mask_policy, meta=meta
        )

    upstream_body = gw.upstream.prepare_body(body, target)

    if body.get("stream") is True:
        resp = await gw.upstream.open_stream(target, "/chat/completions", upstream_body, request.headers)
        guard = None
        if s.guard_output_enabled:
            guard = ChatStreamGuard(
                gw.policy,
                window_chars=s.output_window_chars,
                overlap_chars=s.output_overlap_chars,
                tool_mask_policy=s.tool_mask_policy,
                meta=meta,
            )
        return StreamingResponse(_stream(resp, guard), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    data = await gw.upstream.post_json(target, "/chat/completions", upstream_body, request.headers)
    if s.guard_output_enabled:
        await guard_refs(
            gw.policy, output_refs(data), "output", endpoint="chat", tool_mask_policy=s.tool_mask_policy, meta=meta
        )
    return JSONResponse(data)


def _error_chunk(code: str, message: str) -> bytes:
    return encode_event(errors.error_body(message, "guardrail_error", code)) + encode_event("[DONE]")


async def _stream(resp: httpx.Response, guard: ChatStreamGuard | None) -> AsyncIterator[bytes]:
    try:
        async for sse in parse_sse(resp.aiter_bytes()):
            if guard is None:
                yield encode_event(sse.data, sse.event)
                continue
            try:
                if sse.data.strip() == "[DONE]":
                    for c in await guard.flush_all():
                        yield encode_event(c)
                    yield encode_event("[DONE]")
                    continue
                try:
                    chunk = json.loads(sse.data)
                except ValueError:
                    chunk = None
                if not isinstance(chunk, dict):
                    yield encode_event(sse.data, sse.event)
                    continue
                for c in await guard.process(chunk):
                    yield encode_event(c)
            except GuardBlocked:
                yield _error_chunk("guardrail_blocked", errors.GUARDRAIL_BLOCKED_STREAM_MESSAGE)
                return
            except GuardUnavailable:
                yield _error_chunk("guardrail_unavailable", errors.GUARDRAIL_UNAVAILABLE_MESSAGE)
                return
    except httpx.HTTPError as e:
        log.warning("upstream stream interrupted: %s", type(e).__name__)
        yield encode_event(errors.error_body("Upstream stream was interrupted.", "upstream_error", "upstream_error"))
    finally:
        await resp.aclose()
