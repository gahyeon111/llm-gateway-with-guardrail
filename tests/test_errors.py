"""Error mapping (DESIGN 9장), ops endpoints and access control."""

import httpx
import pytest

from tests.fake_upstream import Reply


def body(**kw):
    b = {"model": "gpt-5.6-terra", "input": "hi", "stream": False}
    b.update(kw)
    return b


def test_models_list(harness):
    r = harness.client.get("/v1/models")
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"]
    assert r.json()["data"][0]["owned_by"] == "samsungcard-aim-gateway"


def test_healthz_and_metrics(harness):
    assert harness.client.get("/healthz").json() == {"status": "ok"}
    harness.client.post("/v1/responses", json=body())
    m = harness.client.get("/metrics").text
    assert "gateway_requests_total" in m
    assert "aim_checks_total" in m


def test_readyz(harness):
    r = harness.client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"aim": True, "upstream": True}


def test_model_not_found(harness):
    r = harness.client.post("/v1/responses", json=body(model="gpt-4"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize(
    "extra,param",
    [({"previous_response_id": "resp_1"}, "previous_response_id"), ({"background": True}, "background")],
)
def test_unsupported_parameters(harness, extra, param):
    r = harness.client.post("/v1/responses", json=body(**extra))
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "unsupported_parameter"
    assert r.json()["error"]["param"] == param


def test_stored_response_endpoints_404(harness):
    assert harness.client.get("/v1/responses/resp_1").status_code == 404
    assert harness.client.delete("/v1/responses/resp_1").status_code == 404


def test_invalid_json(harness):
    r = harness.client.post("/v1/responses", content=b"{nope", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"


def test_missing_model(harness):
    r = harness.client.post("/v1/responses", json={"input": "hi"})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "model"


@pytest.mark.parametrize("status", [401, 403])
def test_upstream_auth_error_hidden(harness, status):
    harness.script(lambda p, b: Reply(status=status, json_body={"error": {"message": "bad key sk-xxx"}}))
    r = harness.client.post("/v1/responses", json=body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_auth_error"
    assert "sk-xxx" not in r.text


def test_upstream_429_passthrough_with_retry_after(harness):
    err = {"error": {"message": "slow down", "code": "rate_limit_exceeded"}}
    harness.script(lambda p, b: Reply(status=429, json_body=err, headers={"retry-after": "7"}))
    r = harness.client.post("/v1/responses", json=body(stream=True))
    assert r.status_code == 429
    assert r.headers["retry-after"] == "7"
    assert r.json() == err


def test_upstream_other_4xx_passthrough(harness):
    err = {"error": {"message": "bad param", "code": "invalid_value"}}
    harness.script(lambda p, b: Reply(status=422, json_body=err))
    r = harness.client.post("/v1/responses", json=body())
    assert r.status_code == 422
    assert r.json() == err


def test_upstream_5xx(harness):
    harness.script(lambda p, b: Reply(status=500, json_body={"error": "boom"}))
    r = harness.client.post("/v1/responses", json=body())
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"


class _RaisingTransport(httpx.AsyncBaseTransport):
    def __init__(self, exc):
        self.exc = exc

    async def handle_async_request(self, request):
        raise self.exc


@pytest.mark.parametrize(
    "exc,status,code",
    [
        (httpx.ReadTimeout("t"), 504, "upstream_timeout"),
        (httpx.ConnectError("c"), 502, "upstream_error"),
    ],
)
def test_upstream_transport_errors(exc, status, code):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import make_settings
    from tests.fake_aim import create_fake_aim

    app = create_app(
        make_settings(),
        aim_transport=httpx.ASGITransport(app=create_fake_aim()),
        upstream_transport=_RaisingTransport(exc),
    )
    with TestClient(app) as client:
        r = client.post("/v1/responses", json=body())
    assert r.status_code == status
    assert r.json()["error"]["code"] == code


def test_internal_shared_secret(make_harness):
    h = make_harness(internal_shared_secret="s3cret")
    r = h.client.post("/v1/responses", json=body())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    r = h.client.post("/v1/responses", json=body(), headers={"X-Internal-Secret": "s3cret"})
    assert r.status_code == 200
    assert h.client.get("/healthz").status_code == 200
    # the shared secret header is never forwarded upstream
    assert "x-internal-secret" not in h.upstream_requests[0][1]
