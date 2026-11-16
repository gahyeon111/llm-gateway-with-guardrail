"""Output guard for Chat Completions SSE streams (DESIGN 6.6).

* `choices[].delta.content`  → TextGuardBuffer per choice
* `choices[].delta.tool_calls` → held per (choice, tool index) until the choice's finish_reason
* chunks without choices (usage) → forwarded unchanged

Multi-part chunks (e.g. content together with role/finish_reason) are split into per-part chunks that
reuse the original chunk's metadata (id, created, model, ...).
"""

from __future__ import annotations

from app import metrics
from app.guard.aim_client import AimResult, Verdict
from app.guard.policy import GuardPolicy
from app.guard.text_buffer import GuardBlocked, Release, TextGuardBuffer


def _clone_with_choice(chunk: dict, choice: dict) -> dict:
    out = {k: v for k, v in chunk.items() if k != "choices"}
    out["choices"] = [choice]
    return out


class _ToolState:
    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self.args = ""
        self.id: str | None = None
        self.name: str | None = None
        self.template: dict | None = None


class ChatStreamGuard:
    def __init__(
        self,
        policy: GuardPolicy,
        *,
        window_chars: int,
        overlap_chars: int,
        tool_mask_policy: str,
        meta: dict | None = None,
    ) -> None:
        self._policy = policy
        self._window = window_chars
        self._overlap = overlap_chars
        self._tool_mask_policy = tool_mask_policy
        self._meta = meta
        self._text: dict[int, TextGuardBuffer] = {}
        self._tools: dict[int, dict[int, _ToolState]] = {}

    async def process(self, chunk: dict) -> list[dict]:
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return [chunk]
        out: list[dict] = []
        single = len(choices) == 1
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            idx = choice.get("index", 0)
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            finish = choice.get("finish_reason")
            content = delta.get("content")
            tool_calls = delta.get("tool_calls")
            has_content = isinstance(content, str) and content != ""
            has_tools = isinstance(tool_calls, list) and bool(tool_calls)

            if not has_content and not has_tools:
                if finish is not None:
                    out += await self._finish_choice(idx)
                out.append(chunk if single else _clone_with_choice(chunk, choice))
                continue

            rest_delta = {k: v for k, v in delta.items() if k not in ("content", "tool_calls")}
            content_only = single and not has_tools and finish is None and not rest_delta
            needs_rest = not content_only and (bool(rest_delta) or finish is not None)
            if needs_rest and finish is None:
                out.append(_clone_with_choice(chunk, {**choice, "delta": rest_delta}))

            if has_content:
                payload = (
                    chunk
                    if content_only
                    else _clone_with_choice(chunk, {"index": idx, "delta": {"content": content}, "finish_reason": None})
                )
                buf = self._text.get(idx)
                if buf is None:
                    buf = self._text[idx] = TextGuardBuffer(self._check_window, self._window, self._overlap)
                out += [self._release_chunk(r, idx) for r in await buf.feed(content, payload)]

            if has_tools:
                for tc in tool_calls:
                    if not isinstance(tc, dict):
                        continue
                    st = self._tools.setdefault(idx, {}).setdefault(tc.get("index", 0), _ToolState())
                    piece = _clone_with_choice(
                        chunk, {"index": idx, "delta": {"tool_calls": [tc]}, "finish_reason": None}
                    )
                    st.chunks.append(piece)
                    st.template = st.template or piece
                    st.id = st.id or tc.get("id")
                    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                    st.name = st.name or fn.get("name")
                    if isinstance(fn.get("arguments"), str):
                        st.args += fn["arguments"]

            if finish is not None:
                out += await self._finish_choice(idx)
                out.append(_clone_with_choice(chunk, {**choice, "delta": rest_delta}))
        return out

    async def flush_all(self) -> list[dict]:
        """Release everything still held (upstream ended without finish_reason)."""
        out: list[dict] = []
        for idx in sorted(set(self._text) | set(self._tools)):
            out += await self._finish_choice(idx)
        return out

    # --- internals ---------------------------------------------------------------------------

    async def _check_window(self, text: str) -> AimResult:
        return await self._policy.check(text, "output", self._meta, use_cache=False)

    def _release_chunk(self, r: Release, idx: int) -> dict:
        if r.payload is not None:
            return r.payload
        return _clone_with_choice(r.template, {"index": idx, "delta": {"content": r.text}, "finish_reason": None})

    async def _finish_choice(self, idx: int) -> list[dict]:
        out: list[dict] = []
        buf = self._text.pop(idx, None)
        if buf is not None:
            out += [self._release_chunk(r, idx) for r in await buf.finish()]
        tools = self._tools.pop(idx, {})
        finals: dict[int, str] = {}
        for ti, st in tools.items():  # check every tool call before releasing any of them
            res = await self._policy.check(st.args, "output", self._meta)
            verdict = res.verdict
            if verdict is Verdict.MASK and (self._tool_mask_policy == "block" or res.masked_text is None):
                verdict = Verdict.BLOCK
            metrics.GUARD_VERDICTS.labels("chat", "output", verdict.value).inc()
            if verdict is Verdict.BLOCK:
                raise GuardBlocked(res.reason)
            finals[ti] = res.masked_text if verdict is Verdict.MASK else st.args
        for ti in sorted(tools):
            st = tools[ti]
            if finals[ti] == st.args:
                out += st.chunks
            else:
                tc = {
                    "index": ti,
                    "id": st.id,
                    "type": "function",
                    "function": {"name": st.name, "arguments": finals[ti]},
                }
                out.append(
                    _clone_with_choice(
                        st.template or {}, {"index": idx, "delta": {"tool_calls": [tc]}, "finish_reason": None}
                    )
                )
        return out
