"""Text extraction for the Responses API (DESIGN 5.1, 6.5)."""

from __future__ import annotations

from typing import Any

from app.guard.refs import TextRef, str_ref

INPUT_ROLES = {"user", "developer", "system"}
INPUT_TEXT_PART_TYPES = {"input_text", "text"}
TOOL_OUTPUT_TYPES = {"function_call_output", "custom_tool_call_output"}
TOOL_OUTPUT_PART_TYPES = {"input_text", "text", "output_text"}


def _item_type(item: dict) -> str | None:
    t = item.get("type")
    if t is None and "role" in item:
        return "message"
    return t


def _content_refs(holder: dict, key: str, part_types: set[str]) -> list[TextRef]:
    content = holder.get(key)
    if isinstance(content, str):
        return [TextRef(holder, key)]
    refs: list[TextRef] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in part_types:
                refs += str_ref(part, "text")
    return refs


def input_refs(body: dict[str, Any]) -> list[TextRef]:
    """User/developer/system text, tool outputs and instructions. Model-generated items are skipped."""
    refs = str_ref(body, "instructions")
    inp = body.get("input")
    if isinstance(inp, str):
        refs += str_ref(body, "input")
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            t = _item_type(item)
            if t == "message" and item.get("role") in INPUT_ROLES:
                refs += _content_refs(item, "content", INPUT_TEXT_PART_TYPES)
            elif t in TOOL_OUTPUT_TYPES:
                refs += _content_refs(item, "output", TOOL_OUTPUT_PART_TYPES)
    return refs


def output_item_refs(item: dict[str, Any], check_reasoning: bool) -> list[TextRef]:
    t = item.get("type")
    if t == "message":
        refs = []
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                refs += str_ref(part, "text")
        return refs
    if t == "function_call":
        return str_ref(item, "arguments", "tool")
    if t == "custom_tool_call":
        return str_ref(item, "input", "tool")
    if t == "reasoning" and check_reasoning:
        refs = []
        for key in ("summary", "content"):
            for part in item.get(key) or []:
                if isinstance(part, dict):
                    refs += str_ref(part, "text")
        return refs
    return []


def output_refs(response: dict[str, Any], check_reasoning: bool) -> list[TextRef]:
    refs: list[TextRef] = []
    for item in response.get("output") or []:
        if isinstance(item, dict):
            refs += output_item_refs(item, check_reasoning)
    return refs
