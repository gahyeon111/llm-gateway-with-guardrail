"""Guard policy: caching, chunking, concurrency limit, retry and fail mode (DESIGN 5.2–5.4, 8.4)."""

from __future__ import annotations

import asyncio
import difflib
import logging
import time
from collections.abc import Sequence

from app import metrics
from app.guard.aim_client import AimClient, AimResult, AimUnavailable, Direction, Verdict
from app.guard.cache import VerdictCache, cache_key

log = logging.getLogger(__name__)

PASS_RESULT = AimResult(Verdict.PASS)


class GuardUnavailable(Exception):
    """AIM is unavailable and AIM_FAIL_MODE=closed."""


def split_chunks(text: str, max_chars: int, overlap: int) -> list[tuple[int, str]]:
    """Split into overlapping chunks. Returns (start_offset, chunk) pairs."""
    if len(text) <= max_chars:
        return [(0, text)]
    step = max_chars - overlap
    chunks = []
    start = 0
    while True:
        chunks.append((start, text[start : start + max_chars]))
        if start + max_chars >= len(text):
            break
        start += step
    return chunks


Edit = tuple[int, int, str]  # (start, end, replacement) in original-text coordinates


def mask_edits(original: str, masked: str) -> list[Edit]:
    """Regions where `masked` differs from `original`, as edits on the original text."""
    if len(original) == len(masked):  # fast path: character-for-character masking
        edits: list[Edit] = []
        i, n = 0, len(original)
        while i < n:
            if original[i] != masked[i]:
                j = i
                while j < n and original[j] != masked[j]:
                    j += 1
                edits.append((i, j, masked[i:j]))
                i = j
            else:
                i += 1
        return edits
    sm = difflib.SequenceMatcher(None, original, masked, autojunk=False)
    return [(i1, i2, masked[j1:j2]) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def apply_edits(text: str, edits: list[Edit]) -> str:
    """Apply edits; where edits overlap (the same value masked by two chunks) keep the wider one."""
    chosen: list[Edit] = []
    for e in sorted(edits, key=lambda e: (e[0], -(e[1] - e[0]))):
        if chosen and e[0] < chosen[-1][1]:
            if e[1] - e[0] > chosen[-1][1] - chosen[-1][0]:
                chosen[-1] = e
            continue
        if chosen and e == chosen[-1]:
            continue
        chosen.append(e)
    out, pos = [], 0
    for a, b, rep in chosen:
        out.append(text[pos:a])
        out.append(rep)
        pos = b
    out.append(text[pos:])
    return "".join(out)


def merge_chunk_results(text: str, chunks: list[tuple[int, str]], results: Sequence[AimResult]) -> AimResult:
    """Combine per-chunk verdicts. Any BLOCK → BLOCK; any MASK → union of every chunk's masking.

    Taking the union (instead of splitting overlap regions by ownership) guarantees that a value no
    longer than the overlap is masked as long as one chunk saw it whole (DESIGN 17장).
    """
    for r in results:
        if r.verdict is Verdict.BLOCK:
            return r
    if all(r.verdict is Verdict.PASS for r in results):
        return PASS_RESULT
    edits: list[Edit] = []
    for (start, chunk), r in zip(chunks, results, strict=True):
        if r.verdict is Verdict.MASK and r.masked_text is not None:
            edits += [(start + a, start + b, rep) for a, b, rep in mask_edits(chunk, r.masked_text)]
    reason = next((r.reason for r in results if r.verdict is Verdict.MASK and r.reason), None)
    return AimResult(Verdict.MASK, masked_text=apply_edits(text, edits), reason=reason)


class GuardPolicy:
    def __init__(
        self,
        aim: AimClient,
        cache: VerdictCache,
        *,
        policy_version: str,
        max_chars: int,
        chunk_overlap: int,
        concurrency: int,
        fail_mode: str,
        retry_backoff_s: float = 0.2,
    ) -> None:
        self._aim = aim
        self._cache = cache
        self._policy_version = policy_version
        self._max_chars = max_chars
        self._overlap = chunk_overlap
        self._sem = asyncio.Semaphore(concurrency)
        self._fail_open = fail_mode == "open"
        self._backoff = retry_backoff_s

    async def check(
        self, text: str, direction: Direction, meta: dict | None = None, *, use_cache: bool = True
    ) -> AimResult:
        """Check one text. Raises GuardUnavailable when AIM is down and fail mode is closed."""
        if not text or not text.strip():
            return PASS_RESULT
        key = cache_key(self._policy_version, direction, text) if use_cache else None
        if key is not None:
            cached = await self._cache.get(key)
            metrics.CACHE_LOOKUPS.labels("hit" if cached else "miss").inc()
            if cached is not None:
                return cached

        chunks = split_chunks(text, self._max_chars, self._overlap)
        outcomes = await asyncio.gather(*(self._call(c, direction, meta) for _, c in chunks))
        results = [r for r, _ in outcomes]
        failed_open = any(f for _, f in outcomes)
        merged = merge_chunk_results(text, chunks, results)
        if key is not None and not failed_open:
            await self._cache.set(key, merged)
        return merged

    async def check_many(self, texts: Sequence[str], direction: Direction, meta: dict | None = None) -> list[AimResult]:
        """Check several texts in parallel (bounded by AIM_CONCURRENCY), deduplicating identical ones."""
        unique = list(dict.fromkeys(texts))
        results = await asyncio.gather(*(self.check(t, direction, meta) for t in unique))
        by_text = dict(zip(unique, results, strict=True))
        return [by_text[t] for t in texts]

    async def _call(self, text: str, direction: Direction, meta: dict | None) -> tuple[AimResult, bool]:
        """Returns (result, failed_open)."""
        last_exc: Exception | None = None
        for attempt in range(2):  # one retry
            if attempt:
                await asyncio.sleep(self._backoff)
            async with self._sem:
                t0 = time.perf_counter()
                try:
                    result = await self._aim.check(text, direction, meta)
                except AimUnavailable as e:
                    last_exc = e
                    continue
                finally:
                    metrics.AIM_LATENCY.labels(direction).observe(time.perf_counter() - t0)
            metrics.AIM_CHECKS.labels(direction, result.verdict.value).inc()
            return result, False

        metrics.AIM_ERRORS.labels(direction).inc()
        if self._fail_open:
            metrics.AIM_FAIL_OPEN.labels(direction).inc()
            log.warning("AIM unavailable (%s); fail-open → PASS", last_exc)
            return PASS_RESULT, True
        log.error("AIM unavailable (%s); fail-closed", last_exc)
        raise GuardUnavailable(str(last_exc)) from last_exc
