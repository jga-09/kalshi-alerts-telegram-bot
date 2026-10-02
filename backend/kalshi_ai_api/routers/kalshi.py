"""Kalshi account connection endpoints.

Kalshi offers no OAuth for third-party apps (verified against the official
OpenAPI spec), so ``/api/kalshi/callback`` does not exist. Customers submit an
API key ID + private key over HTTPS to POST /api/kalshi/connect using a
single-use connect token issued by the Telegram bot.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from kalshi_ai.config import get_settings
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import KalshiEnvironment
from kalshi_ai.kalshi.auth import KalshiAuthService
from kalshi_ai.kalshi.errors import KalshiCredentialFormatError
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.services.kalshi_connections import (
    ConnectTokenError,
    connection_can_trade,
    disconnect,
    get_connection,
    kalshi_base_url,
    resolve_connect_token,
    save_connection,
    verify_credentials,
)
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai_api.deps import CurrentUser, RedisDep, SessionDep, SettingsDep, client_ip

router = APIRouter(prefix="/api/kalshi", tags=["kalshi"])

INSTRUCTIONS = [
    "Log in to kalshi.com and open Account -> API Keys.",
    "Create a new API key. Grant ONLY the 'read' and 'write::trade' scopes. Do NOT grant transfer/withdraw scopes.",
    "Download the private key file (.pem) - Kalshi shows it only once.",
    "Paste the API Key ID and the private key below. They are sent over HTTPS and encrypted at rest.",
    "You can revoke the key on kalshi.com at any time; Kalshi AI never sees your Kalshi password.",
]


@router.get("/connect")
async def connect_info(session: SessionDep, token: str = Query(min_length=20, max_length=128)) -> dict[str, Any]:
    """Validate a connect link and return the instructions/requirements for the secure form."""
    try:
        user = await resolve_connect_token(session, token, consume=False)
    except ConnectTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    conn = await get_connection(session, user.id)
    return {
        "valid": True,
        "instructions": INSTRUCTIONS,
        "environments": [e.value for e in KalshiEnvironment],
        "default_environment": get_settings().kalshi_env.value,
        "already_connected": conn is not None,
        "accepted_key_types": ["RSA (>= 2048-bit)", "Ed25519"],
    }


class ConnectRequest(BaseModel):
    token: str = Field(min_length=20, max_length=128)
    api_key_id: str = Field(min_length=8, max_length=128)
    private_key_pem: str = Field(min_length=64, max_length=16_384, repr=False)
    environment: KalshiEnvironment = KalshiEnvironment.DEMO

    @field_validator("api_key_id")
    @classmethod
    def _key_id(cls, v: str) -> str:
        v = v.strip()
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("API key ID has invalid characters")
        return v


@router.post("/connect")
async def connect(
    body: ConnectRequest, request: Request, session: SessionDep, settings: SettingsDep, redis: RedisDep
) -> dict[str, Any]:
    limit = await RateLimiter(redis, fail_closed=True).hit(f"kalshi_connect:{client_ip(request)}", 10, 600)
    if not limit.allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many attempts; try again later")
    try:
        user = await resolve_connect_token(session, body.token, consume=False)
    except ConnectTokenError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    if not (await get_access(session, user)).has(Feature.KALSHI_CONNECT):
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "Your plan does not include Kalshi account connection.")
    try:
        auth = KalshiAuthService(body.api_key_id, body.private_key_pem)
    except KalshiCredentialFormatError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None

    verification = await verify_credentials(auth, kalshi_base_url(body.environment, settings), settings)
    if not verification.ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, verification.error or "Verification failed")
    if verification.transfer_capable and not settings.kalshi_allow_transfer_scope:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This API key can transfer funds (scope 'write' or 'write::transfer'). For your safety, create a key "
            "with only 'read' and 'write::trade' scopes.",
        )
    await resolve_connect_token(session, body.token, consume=True)  # single use, burned only on success
    conn = await save_connection(
        session,
        user,
        api_key_id=body.api_key_id,
        private_key_pem=body.private_key_pem,
        environment=body.environment,
        verification=verification,
        auth=auth,
        cipher=SecretCipher.from_settings(settings),
        ip_address=client_ip(request),
    )
    return {
        "status": conn.status.value,
        "environment": conn.environment.value,
        "key_type": conn.key_type,
        "api_key_id_hint": f"***{conn.api_key_id_hint}",
        "scopes": conn.scopes,
        "can_trade": connection_can_trade(conn),
    }


@router.post("/disconnect")
async def disconnect_endpoint(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    removed = await disconnect(session, user)
    return {"disconnected": removed}


@router.get("/status")
async def connection_status(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    conn = await get_connection(session, user.id)
    if conn is None:
        return {"connected": False}
    return {
        "connected": True,
        "status": conn.status.value,
        "environment": conn.environment.value,
        "key_type": conn.key_type,
        "api_key_id_hint": f"***{conn.api_key_id_hint}",
        "scopes": conn.scopes,
        "can_trade": connection_can_trade(conn),
        "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
    }
