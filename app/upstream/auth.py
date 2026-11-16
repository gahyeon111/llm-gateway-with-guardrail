"""Upstream authentication (DESIGN 7.2)."""

from __future__ import annotations

from typing import Protocol


class UpstreamAuth(Protocol):
    async def get_auth_headers(self) -> dict[str, str]: ...


class NoAuth:
    async def get_auth_headers(self) -> dict[str, str]:
        return {}


class BearerAuth:
    """Static bearer token: Xenon external-model key, or Bedrock long-term API key (stage 1)."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


class IamBedrockAuth:
    """Stage 2 (M7): short-lived Bedrock token from IAM credentials via aws-bedrock-token-generator,
    refreshed before expiry. Interface only for now."""

    async def get_auth_headers(self) -> dict[str, str]:
        raise NotImplementedError("BEDROCK_AUTH_MODE=iam is not implemented yet (M7)")


def build_auth(mode: str, *, xenon_api_key: str, bedrock_auth_mode: str, bedrock_token: str) -> UpstreamAuth:
    if mode == "xenon":
        return BearerAuth(xenon_api_key) if xenon_api_key else NoAuth()
    if bedrock_auth_mode == "iam":
        return IamBedrockAuth()
    if not bedrock_token:
        raise ValueError("UPSTREAM_MODE=bedrock with BEDROCK_AUTH_MODE=api_key requires AWS_BEARER_TOKEN_BEDROCK")
    return BearerAuth(bedrock_token)
