"""Shared per-process state, created in the app lifespan."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import Request

from app import errors
from app.config import Settings
from app.guard.aim_client import AimClient
from app.guard.cache import VerdictCache
from app.guard.policy import GuardPolicy
from app.upstream.client import UpstreamClient


@dataclass
class GatewayState:
    settings: Settings
    aim: AimClient
    cache: VerdictCache
    policy: GuardPolicy
    upstream: UpstreamClient
    clients: list[httpx.AsyncClient] = field(default_factory=list)
    ready_cache: dict[str, Any] = field(default_factory=dict)


def get_state(request: Request) -> GatewayState:
    return request.app.state.gw


async def read_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise errors.invalid_request("Request body must be valid JSON.") from None
    if not isinstance(body, dict):
        raise errors.invalid_request("Request body must be a JSON object.")
    return body


def require_model(body: dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise errors.invalid_request("'model' is required.", param="model")
    return model
