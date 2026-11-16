"""Pointers to text fields inside a JSON document, plus applying guard verdicts to them."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app import errors, metrics
from app.guard.aim_client import Direction, Verdict
from app.guard.policy import GuardPolicy, GuardUnavailable

RefKind = Literal["text", "tool"]


@dataclass
class TextRef:
    """A (container, key) pointer to a string field, so MASK can rewrite it in place."""

    container: Any
    key: str | int
    kind: RefKind = "text"

    def get(self) -> str:
        return self.container[self.key]

    def set(self, value: str) -> None:
        self.container[self.key] = value


def str_ref(container: Any, key: str | int, kind: RefKind = "text") -> list[TextRef]:
    """A ref if container[key] is a string, else nothing."""
    try:
        value = container[key]
    except (KeyError, IndexError, TypeError):
        return []
    return [TextRef(container, key, kind)] if isinstance(value, str) else []


async def guard_refs(
    policy: GuardPolicy,
    refs: list[TextRef],
    direction: Direction,
    *,
    endpoint: str,
    tool_mask_policy: str,
    meta: dict | None = None,
) -> None:
    """Check every ref; apply MASK in place; raise a 4xx/5xx GatewayError on BLOCK/unavailable.

    Tool-call arguments that come back MASKed are treated as BLOCK when TOOL_MASK_POLICY=block,
    because masked JSON arguments would break tool execution (DESIGN 6.3).
    """
    if not refs:
        return
    try:
        results = await policy.check_many([r.get() for r in refs], direction, meta)
    except GuardUnavailable:
        raise errors.guardrail_unavailable() from None
    blocked = None
    for ref, res in zip(refs, results, strict=True):
        verdict = res.verdict
        if verdict is Verdict.MASK and ref.kind == "tool" and tool_mask_policy == "block":
            verdict = Verdict.BLOCK
        metrics.GUARD_VERDICTS.labels(endpoint, direction, verdict.value).inc()
        if verdict is Verdict.BLOCK and blocked is None:
            blocked = res
    if blocked is not None:
        raise errors.guardrail_blocked(direction, blocked.reason)
    for ref, res in zip(refs, results, strict=True):
        if res.verdict is Verdict.MASK and res.masked_text is not None:
            ref.set(res.masked_text)
