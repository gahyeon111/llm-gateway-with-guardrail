"""★ AIM adapter — the ONLY module that knows AIM's HTTP wire format (DESIGN 8장).

The HTTP details below are an ASSUMED spec (DESIGN 8.2). When the internal AIM guide is available,
change only this file. Everything else depends solely on `AimClient.check()`, `AimResult` and `Verdict`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Literal, Protocol

import httpx

log = logging.getLogger(__name__)

Direction = Literal["input", "output"]


class Verdict(str, Enum):
    PASS = "PASS"
    MASK = "MASK"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class AimResult:
    verdict: Verdict
    masked_text: str | None = None  # set when verdict == MASK
    reason: str | None = None  # user-displayable reason (optional)


class AimUnavailable(Exception):
    """AIM timed out, returned 5xx/unexpected data, or could not be reached."""


class AimClient(Protocol):
    async def check(self, text: str, direction: Direction, meta: dict | None = None) -> AimResult: ...

    async def health(self) -> bool: ...


class HttpAimClient:
    """Assumed spec:

    POST {base_url}{check_path}
    {api_key_header}: {api_key}
    {"text": "...", "direction": "input"|"output", "metadata": {...}}
    → 200 {"verdict": "PASS"|"MASK"|"BLOCK", "masked_text": "...", "reason": "..."}
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        check_path: str,
        api_key: str,
        api_key_header: str,
        timeout_s: float,
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + check_path
        self._base_url = base_url
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers[api_key_header] = api_key
        self._timeout = timeout_s

    async def check(self, text: str, direction: Direction, meta: dict | None = None) -> AimResult:
        payload = {"text": text, "direction": direction, "metadata": meta or {}}
        try:
            resp = await self._http.post(self._url, json=payload, headers=self._headers, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise AimUnavailable(type(e).__name__) from e
        if resp.status_code != 200:
            raise AimUnavailable(f"status={resp.status_code}")
        try:
            data = resp.json()
            verdict = Verdict(str(data["verdict"]).upper())
        except Exception as e:  # malformed response
            raise AimUnavailable("malformed response") from e
        masked = data.get("masked_text")
        reason = data.get("reason")
        if verdict is Verdict.MASK and not isinstance(masked, str):
            # Without masked text we cannot apply MASK safely → treat as BLOCK (DESIGN 17장).
            log.warning("AIM returned MASK without masked_text; treating as BLOCK")
            return AimResult(Verdict.BLOCK, reason=reason)
        return AimResult(verdict, masked_text=masked if verdict is Verdict.MASK else None, reason=reason)

    async def health(self) -> bool:
        """Reachability only: any HTTP response below 500 counts as reachable."""
        try:
            resp = await self._http.get(self._base_url, headers=self._headers, timeout=2.0)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500
