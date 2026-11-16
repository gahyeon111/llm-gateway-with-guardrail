"""Operational endpoints: /healthz, /readyz, /metrics."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.state import get_state
from app.upstream.routing import all_base_urls

router = APIRouter()

READY_CACHE_S = 10.0


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    gw = get_state(request)
    cached = gw.ready_cache
    now = time.monotonic()
    if cached and now - cached["at"] < READY_CACHE_S:
        checks = cached["checks"]
    else:
        s = gw.settings
        guard_needed = s.guard_input_enabled or s.guard_output_enabled
        aim_ok, upstream_ok = await asyncio.gather(
            gw.aim.health() if guard_needed else _true(),
            gw.upstream.health(all_base_urls(s)),
        )
        checks = {"aim": aim_ok, "upstream": upstream_ok}
        gw.ready_cache.update(at=now, checks=checks)
    ok = all(checks.values())
    return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503)


async def _true() -> bool:
    return True


@router.get("/metrics")
async def metrics_endpoint():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
