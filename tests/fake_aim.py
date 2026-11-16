"""Fake AIM for local dev / CI (DESIGN 8.5). Implements the ASSUMED spec in app/guard/aim_client.py.

* text contains "BLOCKME"                     → BLOCK
* 16-digit card number (dashes optional)      → MASK (****-****-****-1234)
* otherwise                                   → PASS
* FAKE_AIM_LATENCY_MS                         → injected latency

Run standalone:  uvicorn tests.fake_aim:app --port 9001
"""

from __future__ import annotations

import asyncio
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

CARD_RE = re.compile(r"\d{4}-?\d{4}-?\d{4}-?(\d{4})")


def judge(text: str) -> dict:
    if "BLOCKME" in text:
        return {"verdict": "BLOCK", "reason": "test block keyword"}
    if CARD_RE.search(text):
        return {"verdict": "MASK", "masked_text": CARD_RE.sub(lambda m: f"****-****-****-{m.group(1)}", text)}
    return {"verdict": "PASS"}


def create_fake_aim() -> FastAPI:
    app = FastAPI()
    app.state.calls = []  # list of (direction, text)
    app.state.fail = False  # True → respond 500
    app.state.latency_ms = int(os.environ.get("FAKE_AIM_LATENCY_MS", "0"))

    @app.get("/")
    async def root():
        return {"status": "ok"}

    @app.post("/v1/check")
    async def check(request: Request):
        body = await request.json()
        app.state.calls.append((body.get("direction"), body.get("text", "")))
        if app.state.latency_ms:
            await asyncio.sleep(app.state.latency_ms / 1000)
        if app.state.fail:
            return JSONResponse({"error": "down"}, status_code=500)
        return judge(body.get("text", ""))

    return app


app = create_fake_aim()
