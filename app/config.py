"""Gateway settings, loaded from environment variables (DESIGN 10장)."""

from __future__ import annotations

import json
from functools import cached_property
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ModelEntry(BaseModel):
    xenon_model: str | None = None
    bedrock_model: str | None = None
    base_url: str | None = None  # optional per-model override (e.g. a dedicated service router URL)


DEFAULT_FORWARD_HEADERS = (
    "accept,user-agent,openai-beta,session_id,conversation_id,originator,version,"
    "x-request-id,x-client-request-id,x-codex-*,x-stainless-*"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- App ---
    port: int = 8080
    log_level: str = "INFO"
    internal_shared_secret: str = ""
    internal_shared_secret_header: str = "X-Internal-Secret"
    models_owned_by: str = "samsungcard-aim-gateway"

    # --- Upstream ---
    upstream_mode: Literal["xenon", "bedrock"] = "xenon"
    model_map_json: str = "{}"
    xenon_upstream_base_url: str = ""
    xenon_upstream_api_key: str = ""
    bedrock_base_url: str = "https://bedrock-runtime.ap-northeast-2.amazonaws.com/openai/v1"
    bedrock_auth_mode: Literal["api_key", "iam"] = "api_key"
    aws_bearer_token_bedrock: str = ""
    upstream_connect_timeout_s: float = 10.0
    upstream_read_timeout_s: float = 600.0
    strip_request_fields: str = ""
    forward_headers: str = DEFAULT_FORWARD_HEADERS

    # --- AIM ---
    aim_base_url: str = ""
    aim_check_path: str = "/v1/check"
    aim_api_key: str = ""
    aim_api_key_header: str = "X-Starfort-Guard-Api-Key"
    aim_timeout_ms: int = 3000
    aim_max_chars: int = Field(default=8000, gt=0)
    aim_chunk_overlap: int = Field(default=200, ge=0)
    aim_concurrency: int = Field(default=8, gt=0)
    aim_fail_mode: Literal["closed", "open"] = "closed"

    # --- Guard policy ---
    guard_input_enabled: bool = True
    guard_output_enabled: bool = True
    guard_reasoning: Literal["skip", "check"] = "skip"
    tool_mask_policy: Literal["block", "mask"] = "block"
    output_window_chars: int = Field(default=400, gt=0)
    output_overlap_chars: int = Field(default=64, ge=0)
    guard_policy_version: str = "1"

    # --- Cache ---
    cache_backend: Literal["memory", "redis"] = "memory"
    cache_max_items: int = 50000
    cache_ttl_s: int = 86400
    redis_url: str = ""

    @field_validator("aim_chunk_overlap")
    @classmethod
    def _overlap_lt_max(cls, v: int, info) -> int:
        max_chars = info.data.get("aim_max_chars", 8000)
        if v >= max_chars:
            raise ValueError("AIM_CHUNK_OVERLAP must be smaller than AIM_MAX_CHARS")
        return v

    @cached_property
    def model_map(self) -> dict[str, ModelEntry]:
        raw = json.loads(self.model_map_json or "{}")
        if not isinstance(raw, dict):
            raise ValueError("MODEL_MAP_JSON must be a JSON object")
        return {name: ModelEntry.model_validate(entry) for name, entry in raw.items()}

    @cached_property
    def strip_fields(self) -> list[str]:
        return [f.strip() for f in self.strip_request_fields.split(",") if f.strip()]

    @cached_property
    def forward_header_rules(self) -> tuple[frozenset[str], tuple[str, ...]]:
        exact: set[str] = set()
        prefixes: list[str] = []
        for h in self.forward_headers.split(","):
            h = h.strip().lower()
            if not h:
                continue
            if h.endswith("*"):
                prefixes.append(h[:-1])
            else:
                exact.add(h)
        return frozenset(exact), tuple(prefixes)


def get_settings() -> Settings:
    return Settings()
