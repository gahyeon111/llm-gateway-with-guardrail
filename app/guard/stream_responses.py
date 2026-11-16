"""Output guard for Responses API SSE streams (DESIGN 6.1–6.4).

`process()` takes one upstream event and returns the events to send to the client (possibly none,
when they are held back). Guard failures raise GuardBlocked / GuardUnavailable; the caller then emits
`failed_event()` and closes the stream.
"""

from __future__ import annotations

from typing import Any

from app import metrics
from app.guard.aim_client import AimResult, Verdict
from app.guard.policy import GuardPolicy
from app.guard.text_buffer import GuardBlocked, Release, TextGuardBuffer

# delta type → (done type, key kind, index field)
_TEXT_STREAMS = {
    "response.output_text.delta": ("response.output_text.done", "out", "content_index"),
}
_REASONING_STREAMS = {
    "response.reasoning_summary_text.delta": ("response.reasoning_summary_text.done", "sum", "summary_index"),
    "response.reasoning_text.delta": ("response.reasoning_text.done", "rsn", "content_index"),
}
# tool delta type → (done type, field holding the full value in .done / in the item)
_TOOL_STREAMS = {
    "response.function_call_arguments.delta": ("response.function_call_arguments.done", "arguments"),
    "response.custom_tool_call_input.delta": ("response.custom_tool_call_input.done", "input"),
}
_SYNTH_FIELDS = ("type", "item_id", "output_index", "content_index", "summary_index")

TextKey = tuple[str, Any, Any]


class ResponsesStreamGuard:
    def __init__(
        self,
        policy: GuardPolicy,
        *,
        window_chars: int,
        overlap_chars: int,
        tool_mask_policy: str,
        guard_reasoning: bool,
        meta: dict | None = None,
        endpoint: str = "responses",
    ) -> None:
        self._policy = policy
        self._window = window_chars
        self._overlap = overlap_chars
        self._tool_mask_policy = tool_mask_policy
        self._meta = meta
        self._endpoint = endpoint
        self._seq = 0
        self.response_id: str | None = None

        self._text_streams = dict(_TEXT_STREAMS)
        if guard_reasoning:
            self._text_streams.update(_REASONING_STREAMS)
        self._text_done = {done: (kind, idx) for done, kind, idx in self._text_streams.values()}
        self._tool_done = {done: field for done, field in _TOOL_STREAMS.values()}

        self._text_bufs: dict[TextKey, TextGuardBuffer] = {}
        self._text_final: dict[TextKey, str] = {}
        self._tool_held: dict[Any, list[dict]] = {}
        self._tool_final: dict[Any, str] = {}
        self._check_reasoning = guard_reasoning

    # --- public ------------------------------------------------------------------------------

    def number(self, event: dict) -> dict:
        """Renumber sequence_number from 0 for every event sent to the client (DESIGN 6.1)."""
        event["sequence_number"] = self._seq
        self._seq += 1
        return event

    def failed_event(self, code: str, message: str) -> dict:
        return {
            "type": "response.failed",
            "response": {
                "id": self.response_id,
                "object": "response",
                "status": "failed",
                "error": {"code": code, "message": message},
            },
        }

    async def process(self, ev: dict) -> list[dict]:
        t = ev.get("type")
        if t in ("response.created", "response.in_progress") and isinstance(ev.get("response"), dict):
            self.response_id = ev["response"].get("id") or self.response_id

        if t in self._text_streams:
            _, kind, idx_field = self._text_streams[t]
            key = (kind, ev.get("item_id"), ev.get(idx_field))
            buf = self._text_bufs.get(key)
            if buf is None:
                buf = self._text_bufs[key] = TextGuardBuffer(self._check_window, self._window, self._overlap)
            delta = ev.get("delta")
            if not isinstance(delta, str):
                return [ev]
            return [self._release_event(r) for r in await buf.feed(delta, ev)]

        if t in self._text_done:
            kind, idx_field = self._text_done[t]
            key = (kind, ev.get("item_id"), ev.get(idx_field))
            out = await self._close_text(key)
            if isinstance(ev.get("text"), str):
                ev["text"] = await self._final_text(key, ev["text"])
            out.append(ev)
            return out

        if t in _TOOL_STREAMS:
            self._tool_held.setdefault(ev.get("item_id"), []).append(ev)
            return []

        if t in self._tool_done:
            return await self._close_tool(ev, self._tool_done[t])

        if t == "response.content_part.done":
            part = ev.get("part")
            if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                key = ("out", ev.get("item_id"), ev.get("content_index"))
                part["text"] = await self._final_text(key, part["text"])
            return [ev]

        if t == "response.reasoning_summary_part.done" and self._check_reasoning:
            part = ev.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                key = ("sum", ev.get("item_id"), ev.get("summary_index"))
                part["text"] = await self._final_text(key, part["text"])
            return [ev]

        if t == "response.output_item.done":
            if isinstance(ev.get("item"), dict):
                await self._rewrite_item(ev["item"])
            return [ev]

        if t in ("response.completed", "response.incomplete"):
            out = await self._flush_open_texts()
            resp = ev.get("response")
            if isinstance(resp, dict):
                for item in resp.get("output") or []:
                    if isinstance(item, dict):
                        await self._rewrite_item(item)
            out.append(ev)
            return out

        # response.failed, error, and anything unknown: forward unchanged.
        return [ev]

    # --- text --------------------------------------------------------------------------------

    async def _check_window(self, text: str) -> AimResult:
        # Window texts are unique per stream position; caching them would only churn the cache.
        return await self._policy.check(text, "output", self._meta, use_cache=False)

    def _release_event(self, r: Release) -> dict:
        if r.payload is not None:
            return r.payload
        synth = {k: r.template[k] for k in _SYNTH_FIELDS if k in r.template}
        synth["delta"] = r.text
        if "logprobs" in r.template:
            synth["logprobs"] = []
        return synth

    async def _close_text(self, key: TextKey) -> list[dict]:
        buf = self._text_bufs.pop(key, None)
        if buf is None:
            return []
        out = [self._release_event(r) for r in await buf.finish()]
        self._text_final[key] = buf.final_text
        return out

    async def _flush_open_texts(self) -> list[dict]:
        out: list[dict] = []
        for key in list(self._text_bufs):
            out += await self._close_text(key)
        return out

    async def _final_text(self, key: TextKey, upstream_text: str) -> str:
        """The guarded final text for a text part. If it never streamed through a buffer, check it now
        so no unchecked text reaches the client inside a *.done / completed payload."""
        if key in self._text_final:
            return self._text_final[key]
        res = await self._policy.check(upstream_text, "output", self._meta)
        self._count(res.verdict)
        if res.verdict is Verdict.BLOCK:
            raise GuardBlocked(res.reason)
        final = res.masked_text if res.verdict is Verdict.MASK and res.masked_text is not None else upstream_text
        self._text_final[key] = final
        return final

    # --- tools -------------------------------------------------------------------------------

    async def _guard_tool_value(self, value: str) -> str:
        res = await self._policy.check(value, "output", self._meta)
        verdict = res.verdict
        if verdict is Verdict.MASK and (self._tool_mask_policy == "block" or res.masked_text is None):
            verdict = Verdict.BLOCK
        self._count(verdict)
        if verdict is Verdict.BLOCK:
            raise GuardBlocked(res.reason)
        return res.masked_text if verdict is Verdict.MASK else value

    async def _close_tool(self, done_ev: dict, field: str) -> list[dict]:
        item_id = done_ev.get("item_id")
        held = self._tool_held.pop(item_id, [])
        value = done_ev.get(field)
        if not isinstance(value, str):
            value = "".join(e.get("delta") or "" for e in held)
        final = await self._guard_tool_value(value)
        self._tool_final[item_id] = final
        if final == value:
            return [*held, done_ev]
        template = held[0] if held else {"type": done_ev["type"].replace(".done", ".delta")}
        synth = {k: template[k] for k in _SYNTH_FIELDS if k in template}
        synth.setdefault("item_id", item_id)
        if "output_index" in done_ev:
            synth.setdefault("output_index", done_ev["output_index"])
        synth["delta"] = final
        done_ev[field] = final
        return [synth, done_ev]

    async def _final_tool(self, item_id: Any, value: str) -> str:
        if item_id in self._tool_final:
            return self._tool_final[item_id]
        final = await self._guard_tool_value(value)
        self._tool_final[item_id] = final
        return final

    # --- items -------------------------------------------------------------------------------

    async def _rewrite_item(self, item: dict) -> None:
        item_id = item.get("id")
        t = item.get("type")
        if t == "message":
            for ci, part in enumerate(item.get("content") or []):
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    part["text"] = await self._final_text(("out", item_id, ci), part["text"])
        elif t == "function_call" and isinstance(item.get("arguments"), str):
            item["arguments"] = await self._final_tool(item_id, item["arguments"])
        elif t == "custom_tool_call" and isinstance(item.get("input"), str):
            item["input"] = await self._final_tool(item_id, item["input"])
        elif t == "reasoning" and self._check_reasoning:
            for kind, field in (("sum", "summary"), ("rsn", "content")):
                for i, part in enumerate(item.get(field) or []):
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        part["text"] = await self._final_text((kind, item_id, i), part["text"])

    def _count(self, verdict: Verdict) -> None:
        metrics.GUARD_VERDICTS.labels(self._endpoint, "output", verdict.value).inc()
