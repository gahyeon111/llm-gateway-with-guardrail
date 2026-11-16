"""FastAPI app factory and lifespan (shared httpx clients, verdict cache)."""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import errors, metrics
from app.config import Settings, get_settings
from app.guard.aim_client import HttpAimClient
from app.guard.cache import build_cache
from app.guard.policy import GuardPolicy
from app.routes import chat, models, ops, responses
from app.state import GatewayState
from app.upstream.auth import build_auth
from app.upstream.client import UpstreamClient

OPS_PATHS = {"/healthz", "/readyz", "/metrics"}
KNOWN_PATHS = {"/v1/responses", "/v1/chat/completions", "/v1/models", *OPS_PATHS}


class GatewayMiddleware:
    """Pure ASGI (no response buffering, SSE-safe): shared-secret check + request metrics."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._secret = settings.internal_shared_secret.encode()
        self._header = settings.internal_shared_secret_header.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        label = path if path in KNOWN_PATHS else "other"
        method = scope["method"]
        if self._secret and path not in OPS_PATHS:
            supplied = next((v for k, v in scope["headers"] if k == self._header), b"")
            if not hmac.compare_digest(supplied, self._secret):
                metrics.REQUESTS.labels(label, method, "403").inc()
                resp = errors.GatewayError(403, "forbidden", "Direct access is not allowed.").to_response()
                await resp(scope, receive, send)
                return

        status = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.REQUESTS.labels(label, method, str(status["code"])).inc()


def create_app(
    settings: Settings | None = None,
    *,
    aim_transport: httpx.AsyncBaseTransport | None = None,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """`*_transport` lets tests plug in fake AIM / fake upstream ASGI apps."""
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # per-request URL logs are noise here
    _ = settings.model_map  # fail fast on malformed MODEL_MAP_JSON

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        upstream_http = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_s,
                read=settings.upstream_read_timeout_s,
                write=60.0,
                pool=settings.upstream_connect_timeout_s,
            ),
            trust_env=True,
            transport=upstream_transport,
        )
        aim_http = httpx.AsyncClient(
            timeout=settings.aim_timeout_ms / 1000,
            trust_env=True,
            transport=aim_transport,
            limits=httpx.Limits(max_connections=max(settings.aim_concurrency * 2, 10)),
        )
        aim = HttpAimClient(
            aim_http,
            base_url=settings.aim_base_url,
            check_path=settings.aim_check_path,
            api_key=settings.aim_api_key,
            api_key_header=settings.aim_api_key_header,
            timeout_s=settings.aim_timeout_ms / 1000,
        )
        cache = build_cache(settings.cache_backend, settings.cache_max_items, settings.cache_ttl_s, settings.redis_url)
        policy = GuardPolicy(
            aim,
            cache,
            policy_version=settings.guard_policy_version,
            max_chars=settings.aim_max_chars,
            chunk_overlap=settings.aim_chunk_overlap,
            concurrency=settings.aim_concurrency,
            fail_mode=settings.aim_fail_mode,
        )
        auth = build_auth(
            settings.upstream_mode,
            xenon_api_key=settings.xenon_upstream_api_key,
            bedrock_auth_mode=settings.bedrock_auth_mode,
            bedrock_token=settings.aws_bearer_token_bedrock,
        )
        app.state.gw = GatewayState(
            settings=settings,
            aim=aim,
            cache=cache,
            policy=policy,
            upstream=UpstreamClient(upstream_http, settings, auth),
            clients=[upstream_http, aim_http],
        )
        try:
            yield
        finally:
            await cache.close()
            for c in app.state.gw.clients:
                await c.aclose()

    app = FastAPI(title="AIM Guardrail Gateway", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    errors.install_handlers(app)

    app.add_middleware(GatewayMiddleware, settings=settings)
    app.include_router(ops.router)
    app.include_router(models.router)
    app.include_router(responses.router)
    app.include_router(chat.router)
    return app


app = create_app()
