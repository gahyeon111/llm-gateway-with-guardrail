"""Upstream HTTP calls: header filtering, auth, error mapping (DESIGN 7.3, 9장)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from starlette.datastructures import Headers

from app import errors, metrics
from app.config import Settings
from app.upstream.auth import UpstreamAuth
from app.upstream.routing import Target

log = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "host",
    "content-length",
}


class UpstreamClient:
    def __init__(self, http: httpx.AsyncClient, settings: Settings, auth: UpstreamAuth) -> None:
        self._http = http
        self._settings = settings
        self._auth = auth
        self._exact, self._prefixes = settings.forward_header_rules

    async def build_headers(self, incoming: Headers) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, value in incoming.items():
            n = name.lower()
            if n in HOP_BY_HOP or n in ("authorization", "accept-encoding", "content-type"):
                continue
            if n in self._exact or any(n.startswith(p) for p in self._prefixes):
                out[n] = value
        out["content-type"] = "application/json"
        out["accept-encoding"] = "identity"  # no compression → no stream buffering
        out.update(await self._auth.get_auth_headers())
        return out

    def prepare_body(self, body: dict[str, Any], target: Target) -> dict[str, Any]:
        out = dict(body)
        out["model"] = target.model
        for f in self._settings.strip_fields:
            out.pop(f, None)
        return out

    async def post_json(self, target: Target, path: str, body: dict, incoming: Headers) -> dict:
        req = self._http.build_request(
            "POST", target.base_url + path, content=_dumps(body), headers=await self.build_headers(incoming)
        )
        resp = await self._send(req, target, stream=False)
        try:
            data = resp.json()
        except ValueError:
            raise errors.upstream_error("Upstream returned a non-JSON response.") from None
        if not isinstance(data, dict):
            raise errors.upstream_error("Upstream returned an unexpected response.")
        return data

    async def open_stream(self, target: Target, path: str, body: dict, incoming: Headers) -> httpx.Response:
        """Returns an open streaming response (2xx). Caller must aclose() it."""
        req = self._http.build_request(
            "POST", target.base_url + path, content=_dumps(body), headers=await self.build_headers(incoming)
        )
        return await self._send(req, target, stream=True)

    async def _send(self, req: httpx.Request, target: Target, *, stream: bool) -> httpx.Response:
        mode = self._settings.upstream_mode
        try:
            resp = await self._http.send(req, stream=stream)
        except httpx.TimeoutException:
            metrics.UPSTREAM_REQUESTS.labels(mode, target.client_model, "timeout").inc()
            raise errors.upstream_timeout() from None
        except httpx.HTTPError as e:
            metrics.UPSTREAM_REQUESTS.labels(mode, target.client_model, "connect_error").inc()
            log.warning("upstream transport error: %s", type(e).__name__)
            raise errors.upstream_error() from None
        metrics.UPSTREAM_REQUESTS.labels(mode, target.client_model, str(resp.status_code)).inc()
        if resp.is_success:
            return resp
        try:
            content = await resp.aread()
        finally:
            await resp.aclose()
        raise map_upstream_error(resp.status_code, content, resp.headers)

    async def health(self, base_urls: list[str]) -> bool:
        """Reachability only: any HTTP response below 500 from every base URL."""
        if not base_urls:
            return False
        try:
            headers = await self._auth.get_auth_headers()
        except Exception:
            return False
        for url in base_urls:
            try:
                resp = await self._http.get(url + "/models", headers=headers, timeout=2.0)
            except httpx.HTTPError:
                return False
            if resp.status_code >= 500:
                return False
        return True


def map_upstream_error(status: int, content: bytes, headers: httpx.Headers) -> Exception:
    if status in (401, 403):
        log.error("upstream auth failed: status=%s", status)
        return errors.upstream_auth_error()
    if status == 429:
        fwd = {k: headers[k] for k in ("retry-after", "retry-after-ms") if k in headers}
        return errors.UpstreamPassthroughError(429, content, _ctype(headers), fwd)
    if 400 <= status < 500:
        return errors.UpstreamPassthroughError(status, content, _ctype(headers), {})
    return errors.upstream_error(f"Upstream returned status {status}.")


def _ctype(headers: httpx.Headers) -> str:
    return headers.get("content-type", "application/json")


def _dumps(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
