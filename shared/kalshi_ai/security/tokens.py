"""JWT access tokens for the admin dashboard / customer web sessions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from kalshi_ai.config import Settings, get_settings


class TokenError(Exception):
    pass


def create_access_token(
    subject: uuid.UUID | str,
    role: str,
    settings: Settings | None = None,
    ttl_minutes: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ttl_minutes or settings.jwt_access_ttl_minutes)).timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": "kalshi-ai",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        return jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            issuer="kalshi-ai",
            options={"require": ["exp", "sub", "role", "iat"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError("Invalid or expired token") from exc
