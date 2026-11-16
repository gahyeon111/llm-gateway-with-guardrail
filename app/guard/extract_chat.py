"""Text extraction for Chat Completions (DESIGN 5.1)."""

from __future__ import annotations

from typing import Any

from app.guard.refs import TextRef, str_ref

INPUT_ROLES = {"system", "developer", "user", "tool"}


def input_refs(body: dict[str, Any]) -> list[TextRef]:
    refs: list[TextRef] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") not in INPUT_ROLES:
            continue
        content = msg.get("content")
        if isinstance(content, str):
            refs += str_ref(msg, "content")
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    refs += str_ref(part, "text")
    return refs


def output_refs(response: dict[str, Any]) -> list[TextRef]:
    refs: list[TextRef] = []
    for choice in response.get("choices") or []:
        msg = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(msg, dict):
            continue
        refs += str_ref(msg, "content")
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                refs += str_ref(fn, "arguments", "tool")
        legacy = msg.get("function_call")
        if isinstance(legacy, dict):
            refs += str_ref(legacy, "arguments", "tool")
    return refs
