"""FastAPI dependencies: DB session, Redis, authentication, RBAC, CSRF."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.models import User
from kalshi_ai.db.session import get_sessionmaker
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import UserStatus
from kalshi_ai.security.tokens import TokenError, decode_access_token
from kalshi_ai.services.subscriptions import AccessState, get_access
from kalshi_ai.services.users import is_admin

ACCESS_COOKIE = "kai_session"
CSRF_COOKIE = "kai_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


async def get_session() -> AsyncIterator[AsyncSession]:
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def get_redis(request: Request) -> Redis:
    return request.app.state.redis


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis)]


def _extract_token(request: Request) -> tuple[str | None, bool]:
    """Returns (token, from_cookie)."""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip(), False
    cookie = request.cookies.get(ACCESS_COOKIE)
    return cookie, cookie is not None


async def current_user(request: Request, session: SessionDep) -> User:
    token, from_cookie = _extract_token(request)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        claims = decode_access_token(token)
    except TokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session") from None
    if from_cookie and request.method not in SAFE_METHODS:
        # Double-submit CSRF check for cookie-authenticated state changes.
        header = request.headers.get(CSRF_HEADER, "")
        cookie = request.cookies.get(CSRF_COOKIE, "")
        if not header or not cookie or not secrets.compare_digest(header, cookie):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF validation failed")
    try:
        user_id = uuid.UUID(claims["sub"])
    except (ValueError, KeyError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session") from None
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid session")
    if user.status != UserStatus.ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account suspended")
    request.state.user_id = str(user.id)
    return user


CurrentUser = Annotated[User, Depends(current_user)]


async def require_admin(user: CurrentUser, settings: SettingsDep) -> User:
    if not is_admin(user, settings):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


async def current_access(user: CurrentUser, session: SessionDep) -> AccessState:
    return await get_access(session, user)


AccessDep = Annotated[AccessState, Depends(current_access)]


def require_feature(feature: Feature):
    async def _dep(access: AccessDep) -> AccessState:
        if not access.active:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Your Kalshi AI subscription is inactive.")
        if not access.has(feature):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Your plan does not include {feature.value}.")
        return access

    return _dep


async def require_internal_token(
    settings: SettingsDep, x_internal_token: Annotated[str | None, Header()] = None
) -> None:
    expected = settings.internal_api_token.get_secret_value()
    if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid internal token")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None
