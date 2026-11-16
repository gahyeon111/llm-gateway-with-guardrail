from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fake_aim import create_fake_aim
from tests.fake_upstream import create_fake_upstream

MODEL_MAP = {
    "gpt-5.6-sol": {"xenon_model": "xenon-sol-rev1", "bedrock_model": "global.openai.gpt-5.6-sol"},
    "gpt-5.6-terra": {"xenon_model": "xenon-terra-rev1", "bedrock_model": "global.openai.gpt-5.6-terra"},
    "gpt-5.6-luna": {
        "xenon_model": "xenon-luna-rev1",
        "bedrock_model": "global.openai.gpt-5.6-luna",
        "base_url": "http://luna-router/v1",
    },
}


def make_settings(**overrides) -> Settings:
    base = dict(
        model_map_json=json.dumps(MODEL_MAP),
        xenon_upstream_base_url="http://upstream/v1",
        aim_base_url="http://aim",
        output_window_chars=40,
        output_overlap_chars=24,
        aim_max_chars=8000,
        aim_chunk_overlap=200,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


class Harness:
    def __init__(self, **overrides) -> None:
        self.aim = create_fake_aim()
        self.upstream = create_fake_upstream()
        self.settings = make_settings(**overrides)
        self.app = create_app(
            self.settings,
            aim_transport=httpx.ASGITransport(app=self.aim),
            upstream_transport=httpx.ASGITransport(app=self.upstream),
        )
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def __enter__(self) -> Harness:
        self.client.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self.client.__exit__(*exc)

    @property
    def aim_calls(self) -> list:
        return self.aim.state.calls

    @property
    def upstream_requests(self) -> list:
        return self.upstream.state.requests

    def script(self, fn) -> None:
        self.upstream.state.script = fn

    def stream_events(self, path: str, body: dict, headers: dict | None = None) -> list:
        """POST a streaming request and return parsed events (dicts, or raw strings such as [DONE])."""
        with self.client.stream("POST", path, json=body, headers=headers or {}) as r:
            assert r.status_code == 200, r.read()
            raw = r.read().decode()
        events = []
        for block in raw.split("\n\n"):
            data = [ln[6:] for ln in block.split("\n") if ln.startswith("data: ")]
            if not data:
                continue
            payload = "\n".join(data)
            try:
                events.append(json.loads(payload))
            except ValueError:
                events.append(payload)
        return events


@pytest.fixture
def harness() -> Iterator[Harness]:
    with Harness() as h:
        yield h


@pytest.fixture
def make_harness():
    created: list[Harness] = []

    def _make(**overrides) -> Harness:
        h = Harness(**overrides).__enter__()
        created.append(h)
        return h

    yield _make
    for h in created:
        h.__exit__(None, None, None)
