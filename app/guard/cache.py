"""Verdict cache (DESIGN 5.2). Key = sha256(policy_version + direction + text)."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Protocol

from app.guard.aim_client import AimResult, Verdict


def cache_key(policy_version: str, direction: str, text: str) -> str:
    h = hashlib.sha256()
    for part in (policy_version, direction, text):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _encode(r: AimResult) -> str:
    return json.dumps({"v": r.verdict.value, "m": r.masked_text, "r": r.reason}, ensure_ascii=False)


def _decode(s: str | bytes) -> AimResult:
    d = json.loads(s)
    return AimResult(Verdict(d["v"]), masked_text=d.get("m"), reason=d.get("r"))


class VerdictCache(Protocol):
    async def get(self, key: str) -> AimResult | None: ...

    async def set(self, key: str, value: AimResult) -> None: ...

    async def close(self) -> None: ...


class MemoryCache:
    """In-process LRU with TTL. Single pod only."""

    def __init__(self, max_items: int, ttl_s: int) -> None:
        self._data: OrderedDict[str, tuple[float, AimResult]] = OrderedDict()
        self._max = max_items
        self._ttl = ttl_s

    async def get(self, key: str) -> AimResult | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    async def set(self, key: str, value: AimResult) -> None:
        self._data[key] = (time.monotonic() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    async def close(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class RedisCache:
    """Shared cache for multi-replica deployments. Requires the `redis` extra."""

    def __init__(self, url: str, ttl_s: int) -> None:
        import redis.asyncio as redis  # optional dependency

        self._r = redis.from_url(url)
        self._ttl = ttl_s

    async def get(self, key: str) -> AimResult | None:
        try:
            raw = await self._r.get("aimgw:" + key)
        except Exception:
            return None  # cache failures must never break the request path
        return _decode(raw) if raw else None

    async def set(self, key: str, value: AimResult) -> None:
        try:
            await self._r.set("aimgw:" + key, _encode(value), ex=self._ttl)
        except Exception:
            pass

    async def close(self) -> None:
        await self._r.aclose()


def build_cache(backend: str, max_items: int, ttl_s: int, redis_url: str) -> VerdictCache:
    if backend == "redis":
        if not redis_url:
            raise ValueError("CACHE_BACKEND=redis requires REDIS_URL")
        return RedisCache(redis_url, ttl_s)
    return MemoryCache(max_items, ttl_s)
