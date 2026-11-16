"""Format-independent hold-back buffer for streamed text (DESIGN 6.2).

Deltas are held until a window fills or a sentence boundary arrives, then the unreleased text is checked:

* PASS  → release the held deltas (original payloads) except a tail of >= `overlap` chars, which stays
          held so a value split across windows (card number, RRN, ...) is checked whole next time.
* MASK  → release one synthesized delta with the masked text (see `_release_masked` for the tail rule).
* BLOCK → raise GuardBlocked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.guard.aim_client import AimResult, Verdict

SENTENCE_BOUNDARIES = (".", "!", "?", "\n", "。")


class GuardBlocked(Exception):
    def __init__(self, reason: str | None = None) -> None:
        super().__init__("guardrail_blocked")
        self.reason = reason


@dataclass
class Release:
    """A piece of text to send. `payload` is the original event (forward as-is) or None (synthesize).

    `template` is an original event to copy metadata (item_id, indexes, ...) from when synthesizing.
    """

    text: str
    payload: Any | None
    template: Any


CheckFn = Callable[[str], Awaitable[AimResult]]


class TextGuardBuffer:
    def __init__(self, check: CheckFn, window_chars: int, overlap_chars: int) -> None:
        self._check = check
        self._window = window_chars
        self._overlap = overlap_chars
        self._entries: list[tuple[str, Any]] = []  # unreleased (text, payload)
        self._unchecked = 0  # chars received since the last check
        self.final_text = ""  # everything released so far (MASK applied)

    @property
    def pending_text(self) -> str:
        return "".join(t for t, _ in self._entries)

    async def feed(self, text: str, payload: Any) -> list[Release]:
        if not text:
            return []
        self._entries.append((text, payload))
        self._unchecked += len(text)
        pending_len = sum(len(t) for t, _ in self._entries)
        if pending_len >= self._window or any(b in text for b in SENTENCE_BOUNDARIES):
            return await self._check_and_release(final=False)
        return []

    async def finish(self) -> list[Release]:
        """Force-check whatever is left and release all of it."""
        if not self._entries:
            return []
        if self._unchecked == 0:  # tail was already checked as part of the previous window
            return self._release_originals(len(self._entries))
        return await self._check_and_release(final=True)

    # --- internals ---------------------------------------------------------------------------

    def _tail_start(self) -> int:
        """Index of the first entry of the minimal suffix holding >= overlap chars."""
        if self._overlap <= 0:
            return len(self._entries)
        acc = 0
        for i in range(len(self._entries) - 1, -1, -1):
            acc += len(self._entries[i][0])
            if acc >= self._overlap:
                return i
        return 0

    def _release_originals(self, upto: int) -> list[Release]:
        released = self._entries[:upto]
        self._entries = self._entries[upto:]
        out = [Release(t, p, p) for t, p in released]
        self.final_text += "".join(t for t, _ in released)
        return out

    async def _check_and_release(self, final: bool) -> list[Release]:
        pending = self.pending_text
        result = await self._check(pending)
        self._unchecked = 0
        if result.verdict is Verdict.BLOCK:
            raise GuardBlocked(result.reason)
        if result.verdict is Verdict.PASS or result.masked_text is None:
            return self._release_originals(len(self._entries) if final else self._tail_start())
        return self._release_masked(pending, result.masked_text, final)

    def _release_masked(self, pending: str, masked: str, final: bool) -> list[Release]:
        """Keep the raw tail held only if masking left it untouched and lengths line up; otherwise
        release the whole masked text (never hold back a partially masked raw tail)."""
        template = self._entries[0][1]
        keep_from = len(self._entries) if final else self._tail_start()
        tail_len = sum(len(t) for t, _ in self._entries[keep_from:])
        cut = len(pending) - tail_len
        if 0 < tail_len < len(pending) and len(masked) == len(pending) and masked[cut:] == pending[cut:]:
            out_text = masked[:cut]
            self._entries = self._entries[keep_from:]
        else:
            out_text = masked
            self._entries = []
        self.final_text += out_text
        return [Release(out_text, None, template)] if out_text else []
