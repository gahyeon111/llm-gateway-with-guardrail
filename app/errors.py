"""OpenAI-format errors and exception → response mapping (DESIGN 9장).

Error messages must never contain request/response bodies, keys or internal upstream URLs.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger(__name__)

GUARDRAIL_BLOCKED_MESSAGE = "AIM 가드레일에 의해 차단되었습니다."
GUARDRAIL_BLOCKED_STREAM_MESSAGE = "AIM 가드레일에 의해 응답이 차단되었습니다."
GUARDRAIL_UNAVAILABLE_MESSAGE = "AIM 가드레일을 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."


def error_body(message: str, type_: str, code: str | None, param: str | None = None) -> dict:
    return {"error": {"message": message, "type": type_, "code": code, "param": param}}


class GatewayError(Exception):
    """An error rendered as an OpenAI-format JSON error response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        type_: str = "invalid_request_error",
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code
        self.message = message
        self.type = type_
        self.param = param
        self.headers = headers or {}

    def body(self) -> dict:
        return error_body(self.message, self.type, self.code, self.param)

    def to_response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.headers)


class UpstreamPassthroughError(Exception):
    """Upstream 4xx (other than auth) or 429: forward status and body as-is."""

    def __init__(self, status: int, content: bytes, content_type: str, headers: dict[str, str]):
        super().__init__(f"upstream {status}")
        self.status = status
        self.content = content
        self.content_type = content_type
        self.headers = headers

    def to_response(self) -> Response:
        return Response(self.content, status_code=self.status, media_type=self.content_type, headers=self.headers)


# --- Constructors for the codes in DESIGN 9장 ---------------------------------------------


def guardrail_blocked(stage: str, reason: str | None = None) -> GatewayError:
    msg = f"{GUARDRAIL_BLOCKED_MESSAGE} (stage={stage})"
    if reason:
        msg += f" 사유: {reason}"
    return GatewayError(400, "guardrail_blocked", msg, type_="guardrail_error")


def guardrail_unavailable() -> GatewayError:
    return GatewayError(503, "guardrail_unavailable", GUARDRAIL_UNAVAILABLE_MESSAGE, type_="guardrail_error")


def unsupported_parameter(param: str) -> GatewayError:
    return GatewayError(400, "unsupported_parameter", f"'{param}' is not supported by this gateway.", param=param)


def invalid_request(message: str, param: str | None = None) -> GatewayError:
    return GatewayError(400, "invalid_request", message, param=param)


def model_not_found(model: str) -> GatewayError:
    return GatewayError(404, "model_not_found", f"The model '{model}' does not exist.", param="model")


def upstream_auth_error() -> GatewayError:
    return GatewayError(
        502, "upstream_auth_error", "Upstream authentication failed (gateway configuration).", "upstream_error"
    )


def upstream_error(message: str = "Upstream request failed.") -> GatewayError:
    return GatewayError(502, "upstream_error", message, "upstream_error")


def upstream_timeout() -> GatewayError:
    return GatewayError(504, "upstream_timeout", "Upstream request timed out.", "upstream_error")


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _gateway_error(_: Request, exc: GatewayError):
        return exc.to_response()

    @app.exception_handler(UpstreamPassthroughError)
    async def _passthrough(_: Request, exc: UpstreamPassthroughError):
        return exc.to_response()

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled gateway error: %s", type(exc).__name__)
        return JSONResponse(error_body("Internal gateway error.", "gateway_error", "gateway_error"), status_code=500)
