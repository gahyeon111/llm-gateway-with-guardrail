"""Client model name → upstream target (DESIGN 7.1)."""

from __future__ import annotations

from dataclasses import dataclass

from app import errors
from app.config import Settings


@dataclass(frozen=True)
class Target:
    client_model: str
    model: str  # model id sent upstream
    base_url: str


def resolve(settings: Settings, model: str) -> Target:
    entry = settings.model_map.get(model)
    if entry is None:
        raise errors.model_not_found(model)
    if settings.upstream_mode == "xenon":
        upstream_model = entry.xenon_model
        base_url = entry.base_url or settings.xenon_upstream_base_url
    else:
        upstream_model = entry.bedrock_model
        base_url = settings.bedrock_base_url
    if not upstream_model or not base_url:
        # Misconfiguration for this model in the current mode; don't leak config details.
        raise errors.model_not_found(model)
    return Target(client_model=model, model=upstream_model, base_url=base_url.rstrip("/"))


def all_base_urls(settings: Settings) -> list[str]:
    urls: set[str] = set()
    for entry in settings.model_map.values():
        if settings.upstream_mode == "xenon":
            url = entry.base_url or settings.xenon_upstream_base_url
        else:
            url = settings.bedrock_base_url
        if url:
            urls.add(url.rstrip("/"))
    return sorted(urls)
