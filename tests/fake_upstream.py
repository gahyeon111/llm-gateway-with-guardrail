"""Fake OpenAI-compatible upstream (Responses + Chat Completions) that replays scripted SSE.

Tests set `app.state.script = fn(path, body) -> Reply`. Without a script, a default echo behaviour is
used, which is handy for running the gateway locally:

    uvicorn tests.fake_upstream:app --port 9002
    (input containing "CALL_TOOL" makes the Responses endpoint return a shell function call)
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


@dataclass
class Reply:
    status: int = 200
    json_body: dict | None = None
    events: list[dict | str] | None = None  # dicts → JSON (event: <type>), strs → raw data
    headers: dict[str, str] = field(default_factory=dict)
    named_events: bool = True  # Responses API uses `event:` lines, Chat Completions does not


def sse(events: list[dict | str], named: bool) -> bytes:
    out = []
    for ev in events:
        if isinstance(ev, str):
            out.append(f"data: {ev}\n\n")
        else:
            prefix = f"event: {ev['type']}\n" if named and "type" in ev else ""
            out.append(prefix + "data: " + json.dumps(ev, ensure_ascii=False) + "\n\n")
    return "".join(out).encode()


# --- Responses event builders -------------------------------------------------------------------


def _response(rid: str, status: str, output: list) -> dict:
    return {"id": rid, "object": "response", "status": status, "model": "upstream-model", "output": output}


def message_item(item_id: str, text: str) -> dict:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def text_item_events(item_id: str, output_index: int, deltas: list[str]) -> tuple[list[dict], dict]:
    text = "".join(deltas)
    item = message_item(item_id, text)
    evs: list[dict] = [
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": {**item, "status": "in_progress", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    ]
    for d in deltas:
        evs.append(
            {
                "type": "response.output_text.delta",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": 0,
                "delta": d,
                "logprobs": [],
            }
        )
    evs += [
        {
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "text": text,
            "logprobs": [],
        },
        {
            "type": "response.content_part.done",
            "item_id": item_id,
            "output_index": output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": text, "annotations": []},
        },
        {"type": "response.output_item.done", "output_index": output_index, "item": item},
    ]
    return evs, item


def tool_item_events(
    item_id: str, output_index: int, name: str, arg_deltas: list[str], custom: bool = False
) -> tuple[list[dict], dict]:
    value = "".join(arg_deltas)
    if custom:
        item = {"id": item_id, "type": "custom_tool_call", "call_id": "call_" + item_id, "name": name, "input": value}
        delta_t, done_t, field_name = (
            "response.custom_tool_call_input.delta",
            "response.custom_tool_call_input.done",
            "input",
        )
    else:
        item = {
            "id": item_id,
            "type": "function_call",
            "call_id": "call_" + item_id,
            "name": name,
            "arguments": value,
            "status": "completed",
        }
        delta_t, done_t, field_name = (
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "arguments",
        )
    evs: list[dict] = [
        {"type": "response.output_item.added", "output_index": output_index, "item": {**item, field_name: ""}}
    ]
    for d in arg_deltas:
        evs.append({"type": delta_t, "item_id": item_id, "output_index": output_index, "delta": d})
    evs += [
        {"type": done_t, "item_id": item_id, "output_index": output_index, field_name: value},
        {"type": "response.output_item.done", "output_index": output_index, "item": item},
    ]
    return evs, item


def response_stream(rid: str, items: list[tuple[list[dict], dict]], extra: list[dict] | None = None) -> list[dict]:
    """Wrap item event groups with created/in_progress/completed and add upstream sequence numbers."""
    evs: list[dict] = [
        {"type": "response.created", "response": _response(rid, "in_progress", [])},
        {"type": "response.in_progress", "response": _response(rid, "in_progress", [])},
    ]
    outputs = []
    for item_evs, item in items:
        evs += item_evs
        outputs.append(item)
    evs += extra or []
    completed = _response(rid, "completed", outputs)
    completed["usage"] = {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30}
    evs.append({"type": "response.completed", "response": completed})
    for i, e in enumerate(evs):
        e["sequence_number"] = 1000 + i  # deliberately not 0-based: gateway must renumber
    return evs


# --- Chat builders -------------------------------------------------------------------------------


def chat_chunk(delta: dict, finish_reason: str | None = None, index: int = 0) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "upstream-model",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish_reason}],
    }


# --- App -----------------------------------------------------------------------------------------


def _default_script(path: str, body: dict) -> Reply:
    text_in = json.dumps(body.get("input") or body.get("messages"), ensure_ascii=False)
    if path.endswith("/chat/completions"):
        if body.get("stream"):
            return Reply(
                events=[
                    chat_chunk({"role": "assistant", "content": ""}),
                    chat_chunk({"content": "echo: ok"}),
                    chat_chunk({}, "stop"),
                    "[DONE]",
                ],
                named_events=False,
            )
        return Reply(
            json_body={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "echo: ok"}, "finish_reason": "stop"}
                ],
            }
        )
    if "CALL_TOOL" in text_in and "function_call_output" not in text_in:
        items = [tool_item_events("fc_1", 0, "shell", ['{"command":', '["echo","hi"]}'])]
    else:
        items = [text_item_events("msg_1", 0, ["Hello ", "from ", "fake ", "upstream."])]
    events = response_stream("resp_1", items)
    if body.get("stream"):
        return Reply(events=events)
    return Reply(json_body=events[-1]["response"])


async def _paced(events: list[dict | str], named: bool) -> AsyncIterator[bytes]:
    """Emit events one by one; FAKE_UPSTREAM_EVENT_DELAY_MS adds a delay between them (local dev)."""
    delay = int(os.environ.get("FAKE_UPSTREAM_EVENT_DELAY_MS", "0")) / 1000
    for ev in events:
        if delay:
            await asyncio.sleep(delay)
        yield sse([ev], named)


def create_fake_upstream() -> FastAPI:
    app = FastAPI()
    app.state.requests = []  # list of (path, headers, body)
    app.state.script = None

    async def handle(request: Request):
        body = await request.json()
        app.state.requests.append((request.url.path, dict(request.headers), body))
        script: Callable[[str, dict], Reply] = app.state.script or _default_script
        reply = script(request.url.path, body)
        if reply.events is not None:
            return StreamingResponse(
                _paced(reply.events, reply.named_events),
                status_code=reply.status,
                media_type="text/event-stream",
                headers=reply.headers,
            )
        if reply.json_body is not None:
            return JSONResponse(reply.json_body, status_code=reply.status, headers=reply.headers)
        return Response(status_code=reply.status, headers=reply.headers)

    app.add_api_route("/v1/responses", handle, methods=["POST"])
    app.add_api_route("/v1/chat/completions", handle, methods=["POST"])

    @app.get("/v1/models")
    async def models() -> Any:
        return {"object": "list", "data": []}

    return app


app = create_fake_upstream()
