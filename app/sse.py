"""Minimal Server-Sent Events parser / serializer."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass
class SseEvent:
    event: str | None
    data: str


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseEvent]:
    """Parse an SSE byte stream into events. Handles CRLF, multi-line data and comments."""
    buf = b""
    event_name: str | None = None
    data_lines: list[str] = []

    def flush() -> SseEvent | None:
        nonlocal event_name, data_lines
        ev = None
        if data_lines:
            ev = SseEvent(event_name, "\n".join(data_lines))
        event_name, data_lines = None, []
        return ev

    async for chunk in chunks:
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            raw, buf = buf[:idx], buf[idx + 1 :]
            line = raw.rstrip(b"\r").decode("utf-8")
            if line == "":
                ev = flush()
                if ev is not None:
                    yield ev
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_name = value
            elif field == "data":
                data_lines.append(value)
            # id / retry are ignored
    if buf.strip():
        line = buf.rstrip(b"\r").decode("utf-8")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    ev = flush()
    if ev is not None:
        yield ev


def encode_event(data: dict | str, event: str | None = None) -> bytes:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    lines = []
    if event:
        lines.append(f"event: {event}")
    for part in payload.split("\n"):
        lines.append(f"data: {part}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
SSE_MEDIA_TYPE = "text/event-stream; charset=utf-8"
