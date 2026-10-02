"""Web authentication via Telegram Login Widget -> JWT in httpOnly cookie (+ CSRF cookie)."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel

from kalshi_ai.security.telegram_auth import TelegramAuthError, verify_login_widget
from kalshi_ai.security.tokens import create_access_token
from kalshi_ai.services.audit import audit
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai.services.users import get_or_create_telegram_user, is_admin
from kalshi_ai_api.deps import (
    ACCESS_COOKIE,
    CSRF_COOKIE,
    CurrentUser,
    RedisDep,
    SessionDep,
    SettingsDep,
    client_ip,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class TelegramLoginPayload(BaseModel):
    id: int
    auth_date: int
    hash: str
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo_url: str | None = None


@router.post("/telegram")
async def telegram_login(
    payload: TelegramLoginPayload,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    redis: RedisDep,
) -> dict[str, Any]:
    limit = await RateLimiter(redis, fail_closed=True).hit(f"login:{client_ip(request)}", 20, 300)
    if not limit.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts")
    try:
        fields = verify_login_widget(
            payload.model_dump(exclude_none=True), settings.telegram_bot_token.get_secret_value()
        )
    except TelegramAuthError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Telegram authentication failed") from None
    user, _ = await get_or_create_telegram_user(
        session, int(fields["id"]), username=fields.get("username"), first_name=fields.get("first_name")
    )
    role = "admin" if is_admin(user, settings) else "customer"
    token = create_access_token(user.id, role, settings)
    csrf = secrets.token_urlsafe(32)
    secure = settings.is_production or settings.public_base_url.startswith("https")
    max_age = settings.jwt_access_ttl_minutes * 60
    response.set_cookie(ACCESS_COOKIE, token, httponly=True, secure=secure, samesite="strict", max_age=max_age)
    response.set_cookie(CSRF_COOKIE, csrf, httponly=False, secure=secure, samesite="strict", max_age=max_age)
    await audit(session, "auth.login", actor_user_id=user.id, ip_address=client_ip(request))
    return {"user_id": str(user.id), "role": role, "csrf_token": csrf}


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(ACCESS_COOKIE)
    response.delete_cookie(CSRF_COOKIE)
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: CurrentUser, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    access = await get_access(session, user)
    return {
        "id": str(user.id),
        "telegram_username": user.telegram_username,
        "first_name": user.first_name,
        "role": "admin" if is_admin(user, settings) else "customer",
        "subscription": {
            "active": access.active,
            "plan": access.plan.value if access.plan else None,
            "expires_at": access.expires_at.isoformat() if access.expires_at else None,
        },
    }
