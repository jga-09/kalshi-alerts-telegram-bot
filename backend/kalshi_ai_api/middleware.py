"""Request-ID, structured access logging, security headers and per-IP rate limiting."""

from __future__ import annotations

import re
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kalshi_ai.logging import bind_context, clear_context, get_logger
from kalshi_ai.services.rate_limit import RateLimiter

log = get_logger("kalshi_ai_api.access")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Cache-Control": "no-store",
}


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        request.state.request_id = request_id
        clear_context()
        bind_context(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error", method=request.method, path=request.url.path)
            response = JSONResponse({"detail": "Internal server error", "request_id": request_id}, status_code=500)
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        log.info(
            "http_request",
            method=request.method,
            path=request.url.path,  # path only - query strings may contain tokens
            status=response.status_code,
            duration_ms=duration_ms,
            customer_id=getattr(request.state, "user_id", None),
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, hsts: bool) -> None:
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        for key, value in SECURITY_HEADERS.items():
            if key == "Content-Security-Policy" and request.url.path.startswith(("/docs", "/redoc")):
                continue
            response.headers.setdefault(key, value)
        if self.hsts:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """General API throttling. Fails OPEN for availability; sensitive flows have their own fail-closed limits."""

    EXEMPT = ("/health", "/ready", "/api/stripe/webhook")

    def __init__(self, app, per_minute: int) -> None:
        super().__init__(app)
        self.per_minute = per_minute

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path.startswith(self.EXEMPT):
            return await call_next(request)
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            ip = request.client.host if request.client else "unknown"
            result = await RateLimiter(redis, fail_closed=False).hit(f"api:{ip}", self.per_minute, 60)
            if not result.allowed:
                return JSONResponse(
                    {"detail": "Too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(result.retry_after_seconds or 60)},
                )
        return await call_next(request)
