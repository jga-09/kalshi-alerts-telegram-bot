"""Kalshi errors. Messages never include credentials, signatures or raw headers."""

from __future__ import annotations


class KalshiError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class KalshiAuthError(KalshiError):
    """401/403 - invalid key, revoked key, insufficient scope, or clock skew."""


class KalshiRateLimitError(KalshiError):
    pass


class KalshiTimeoutError(KalshiError):
    """Request outcome is UNKNOWN. For orders: reconcile before any retry."""


class KalshiUnavailableError(KalshiError):
    pass


class KalshiValidationError(KalshiError):
    """400/409/422 - request rejected by the exchange."""


class KalshiNotFoundError(KalshiError):
    pass


class KalshiCredentialFormatError(KalshiError):
    pass
