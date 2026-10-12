"""KalshiClient - low-level HTTP transport for the Kalshi Trade API v2.

* Public market-data endpoints can be called without credentials.
* Portfolio/order endpoints require a per-customer ``KalshiAuthService``.
* Only idempotent GETs are retried. Order creation is NEVER retried automatically;
  a timeout surfaces as ``KalshiTimeoutError`` so the caller can reconcile by
  ``client_order_id`` before taking any further action.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from kalshi_ai.kalshi.auth import KalshiAuthService
from kalshi_ai.kalshi.errors import (
    KalshiAuthError,
    KalshiError,
    KalshiNotFoundError,
    KalshiRateLimitError,
    KalshiTimeoutError,
    KalshiUnavailableError,
    KalshiValidationError,
)
from kalshi_ai.logging import get_logger

log = get_logger(__name__)


class KalshiClient:
    def __init__(
        self,
        base_url: str,
        auth: KalshiAuthService | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval_seconds: float = 0.05,
    ):
        self.base_url = base_url.rstrip("/")
        self._base_path = urlparse(self.base_url).path  # e.g. /trade-api/v2 (part of the signed path)
        self._auth = auth
        self._max_retries = max_retries
        self._min_interval = min_interval_seconds
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "kalshi-ai/0.1"},
        )

    @property
    def authenticated(self) -> bool:
        return self._auth is not None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()

    def _headers(self, method: str, path: str, auth_required: bool) -> dict[str, str]:
        if not auth_required:
            return {}
        if self._auth is None:
            raise KalshiAuthError("This endpoint requires a connected Kalshi account.")
        return self._auth.headers(method, f"{self._base_path}{path}")

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        auth_required: bool = False,
        retry: bool | None = None,
    ) -> dict[str, Any]:
        method = method.upper()
        retry = (method == "GET") if retry is None else retry
        attempts = 1 + (self._max_retries if retry else 0)
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(attempts):
            await self._throttle()
            started = time.monotonic()
            try:
                # Sign per attempt: the timestamp is part of the signature.
                headers = self._headers(method, path, auth_required)
                resp = await self._http.request(method, path, params=clean_params, json=json, headers=headers)
            except httpx.TimeoutException as exc:
                last_exc = KalshiTimeoutError(f"Kalshi request timed out ({method} {path})")
                log.warning("kalshi_timeout", method=method, path=path, attempt=attempt)
                if not retry:
                    raise last_exc from exc
            except httpx.TransportError as exc:
                last_exc = KalshiUnavailableError(f"Kalshi unreachable ({type(exc).__name__})")
                log.warning("kalshi_transport_error", method=method, path=path, attempt=attempt)
                if not retry:
                    # A transport error before a response could still mean the order reached Kalshi.
                    raise KalshiTimeoutError(f"Kalshi request outcome unknown ({method} {path})") from exc
            else:
                latency_ms = (time.monotonic() - started) * 1000
                log.debug("kalshi_response", method=method, path=path, status=resp.status_code, latency_ms=latency_ms)
                if resp.status_code < 400:
                    return resp.json() if resp.content else {}
                err = self._to_error(resp, method, path)
                if isinstance(err, KalshiRateLimitError | KalshiUnavailableError) and retry:
                    last_exc = err
                else:
                    raise err
            if attempt < attempts - 1:
                await asyncio.sleep(min(4.0, (0.25 * 2**attempt) + random.uniform(0, 0.1)))
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _to_error(resp: httpx.Response, method: str, path: str) -> KalshiError:
        code: str | None = None
        message = ""
        try:
            body = resp.json()
            err = body.get("error", body) if isinstance(body, dict) else {}
            code = err.get("code") if isinstance(err, dict) else None
            message = str(err.get("message", ""))[:200] if isinstance(err, dict) else ""
        except ValueError:
            pass
        summary = f"Kalshi {method} {path} failed: HTTP {resp.status_code}" + (f" ({code})" if code else "")
        if message:
            summary += f": {message}"
        status = resp.status_code
        if status in (401, 403):
            return KalshiAuthError(summary, status_code=status, code=code)
        if status == 404:
            return KalshiNotFoundError(summary, status_code=status, code=code)
        if status == 429:
            return KalshiRateLimitError(summary, status_code=status, code=code)
        if status >= 500:
            return KalshiUnavailableError(summary, status_code=status, code=code)
        return KalshiValidationError(summary, status_code=status, code=code)

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, auth_required: bool = False
    ) -> dict[str, Any]:
        return await self.request("GET", path, params=params, auth_required=auth_required)
