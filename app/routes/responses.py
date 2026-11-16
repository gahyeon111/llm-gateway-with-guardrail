"""POST /v1/responses (DESIGN 4–6장)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app import errors
from app.guard.extract_responses import input_refs, output_refs
from app.guard.policy import GuardUnavailable
from app.guard.refs import guard_refs
from app.guard.stream_responses import ResponsesStreamGuard
from app.guard.text_buffer import GuardBlocked
from app.sse import SSE_HEADERS, SSE_MEDIA_TYPE, encode_event, parse_sse
from app.state import get_state, read_json_body, require_model
from app.upstream.routing import resolve

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/v1/responses")
async def create_response(request: Request):
    gw = get_state(request)
    s = gw.settings
    body = await read_json_body(request)
    model = require_model(body)
    if body.get("previous_response_id") is not None:
        raise errors.unsupported_parameter("previous_response_id")
    if body.get("background") is True:
        raise errors.unsupported_parameter("background")
    target = resolve(s, model)
    meta = {"model": model, "endpoint": "responses"}

    if s.guard_input_enabled:
        await guard_refs(
            gw.policy,
            input_refs(body),
            "input",
            endpoint="responses",
            tool_mask_policy=s.tool_mask_policy,
            meta=meta,
        )

    upstream_body = gw.upstream.prepare_body(body, target)

    if body.get("stream") is True:
        resp = await gw.upstream.open_stream(target, "/responses", upstream_body, request.headers)
        guard = None
        if s.guard_output_enabled:
            guard = ResponsesStreamGuard(
                gw.policy,
                window_chars=s.output_window_chars,
                overlap_chars=s.output_overlap_chars,
                tool_mask_policy=s.tool_mask_policy,
                guard_reasoning=s.guard_reasoning == "check",
                meta=meta,
            )
        return StreamingResponse(_stream(resp, guard), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)

    data = await gw.upstream.post_json(target, "/responses", upstream_body, request.headers)
    if s.guard_output_enabled:
        await guard_refs(
            gw.policy,
            output_refs(data, s.guard_reasoning == "check"),
            "output",
            endpoint="responses",
            tool_mask_policy=s.tool_mask_policy,
            meta=meta,
        )
    return JSONResponse(data)


async def _stream(resp: httpx.Response, guard: ResponsesStreamGuard | None) -> AsyncIterator[bytes]:
    try:
        async for sse in parse_sse(resp.aiter_bytes()):
            if guard is None:
                yield encode_event(sse.data, sse.event)
                continue
            try:
                ev = json.loads(sse.data)
            except ValueError:
                ev = None
            if not isinstance(ev, dict):
                yield encode_event(sse.data, sse.event)
                continue
            try:
                out = await guard.process(ev)
            except GuardBlocked:
                yield _failed(guard, "guardrail_blocked", errors.GUARDRAIL_BLOCKED_STREAM_MESSAGE)
                return
            except GuardUnavailable:
                yield _failed(guard, "guardrail_unavailable", errors.GUARDRAIL_UNAVAILABLE_MESSAGE)
                return
            for e in out:
                guard.number(e)
                yield encode_event(e, e.get("type") or sse.event)
    except httpx.HTTPError as e:
        log.warning("upstream stream interrupted: %s", type(e).__name__)
        if guard is not None:
            yield _failed(guard, "upstream_error", "Upstream stream was interrupted.")
    finally:
        await resp.aclose()


def _failed(guard: ResponsesStreamGuard, code: str, message: str) -> bytes:
    ev = guard.number(guard.failed_event(code, message))
    return encode_event(ev, "response.failed")


@router.get("/v1/responses/{response_id}")
@router.delete("/v1/responses/{response_id}")
async def stored_response_unsupported(response_id: str):
    raise errors.GatewayError(404, "not_found", "Stored responses are not supported by this gateway (stateless).")
