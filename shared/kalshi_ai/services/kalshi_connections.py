"""Customer Kalshi account connections.

Flow (Kalshi has no OAuth for third-party apps):
 1. Customer sends /connect in Telegram -> we issue a single-use, 15-minute connect token
    and reply with an HTTPS link to our web form. Secrets never pass through Telegram.
 2. Customer creates an API key at kalshi.com (recommended scopes: ``read`` + ``write::trade``,
    NEVER ``write::transfer``) and submits key ID + private key PEM to POST /api/kalshi/connect.
 3. We verify the credential live (balance call + key scope lookup), encrypt both values
    with the platform key (Fernet/MultiFernet) and store them in ``kalshi_connections``.
 4. Clients are built per-request, per-user; decrypted key material only lives in memory.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import ConnectToken, KalshiConnection, User
from kalshi_ai.domain.enums import KalshiConnectionStatus, KalshiEnvironment
from kalshi_ai.kalshi.auth import KalshiAuthService
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.errors import KalshiAuthError, KalshiError
from kalshi_ai.kalshi.services import KalshiPortfolioService
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.security.hashing import sha256_hex
from kalshi_ai.services.audit import audit
from kalshi_ai.services.users import get_risk_settings

CONNECT_TOKEN_TTL = timedelta(minutes=15)
TRADE_SCOPES = {"write", "write::trade"}
TRANSFER_CAPABLE_SCOPES = {"write", "write::transfer"}


class ConnectTokenError(Exception):
    pass


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    scopes: list[str] | None
    can_trade: bool
    transfer_capable: bool
    error: str | None = None


async def issue_connect_token(session: AsyncSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(ConnectToken(token_hash=sha256_hex(token), user_id=user.id, expires_at=utcnow() + CONNECT_TOKEN_TTL))
    await session.flush()
    await audit(session, "kalshi.connect_token.issued", actor_user_id=user.id)
    return token


async def resolve_connect_token(session: AsyncSession, token: str, *, consume: bool) -> User:
    row = (
        await session.execute(
            select(ConnectToken).where(ConnectToken.token_hash == sha256_hex(token)).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= utcnow():
        raise ConnectTokenError("This connect link is invalid or expired. Send /connect in Telegram for a new one.")
    user = await session.get(User, row.user_id)
    if user is None:
        raise ConnectTokenError("Account not found.")
    if consume:
        row.used_at = utcnow()
    return user


def connect_url(token: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.frontend_base_url.rstrip('/')}/connect?token={token}"


def kalshi_base_url(env: KalshiEnvironment, settings: Settings | None = None) -> str:
    from kalshi_ai.config import KALSHI_HOSTS, KalshiEnv

    settings = settings or get_settings()
    if settings.kalshi_rest_base_url and env.value == settings.kalshi_env.value:
        return settings.kalshi_rest_base_url
    return KALSHI_HOSTS[KalshiEnv(env.value)]["rest"]


async def verify_credentials(
    auth: KalshiAuthService, base_url: str, settings: Settings | None = None
) -> VerificationResult:
    settings = settings or get_settings()
    async with KalshiClient(base_url, auth, timeout=settings.kalshi_timeout_seconds) as client:
        portfolio = KalshiPortfolioService(client)
        try:
            await portfolio.get_balance()
        except KalshiAuthError:
            return VerificationResult(False, None, False, False, "Kalshi rejected these credentials.")
        except KalshiError as exc:
            return VerificationResult(False, None, False, False, f"Could not reach Kalshi: {type(exc).__name__}")
        scopes: list[str] | None = None
        try:
            for key in await portfolio.get_api_keys():
                if key.api_key_id == auth.api_key_id:
                    scopes = key.scopes
                    break
        except KalshiError:
            scopes = None  # scope lookup unavailable -> live trading will not be allowed
    scope_set = set(scopes or [])
    return VerificationResult(
        ok=True,
        scopes=scopes,
        can_trade=bool(scope_set & TRADE_SCOPES),
        transfer_capable=bool(scope_set & TRANSFER_CAPABLE_SCOPES),
    )


async def save_connection(
    session: AsyncSession,
    user: User,
    *,
    api_key_id: str,
    private_key_pem: str,
    environment: KalshiEnvironment,
    verification: VerificationResult,
    auth: KalshiAuthService,
    cipher: SecretCipher,
    ip_address: str | None = None,
) -> KalshiConnection:
    conn = await get_connection(session, user.id)
    if conn is None:
        conn = KalshiConnection(
            user_id=user.id,
            environment=environment,
            encrypted_api_key_id=b"",
            encrypted_private_key=b"",
            api_key_id_hint="",
            public_key_fingerprint="",
            key_type="",
        )
        session.add(conn)
    conn.environment = environment
    conn.encrypted_api_key_id = cipher.encrypt(api_key_id)
    conn.encrypted_private_key = cipher.encrypt(private_key_pem)
    conn.api_key_id_hint = api_key_id[-4:]
    conn.public_key_fingerprint = auth.public_fingerprint
    conn.key_type = auth.key_type
    conn.scopes = verification.scopes
    conn.status = KalshiConnectionStatus.VERIFIED if verification.ok else KalshiConnectionStatus.INVALID
    conn.last_verified_at = utcnow()
    conn.last_error = verification.error
    # Any credential change revokes live-trading confirmation.
    rs = await get_risk_settings(session, user.id)
    rs.live_trading_enabled = False
    rs.live_trading_confirmed_at = None
    await session.flush()
    await audit(
        session,
        "kalshi.connected",
        actor_user_id=user.id,
        target_type="kalshi_connection",
        target_id=conn.id,
        ip_address=ip_address,
        details={
            "environment": environment.value,
            "key_type": auth.key_type,
            "scopes": verification.scopes,
            "fingerprint": auth.public_fingerprint[:16],
        },
    )
    return conn


async def get_connection(session: AsyncSession, user_id: uuid.UUID) -> KalshiConnection | None:
    return (
        await session.execute(select(KalshiConnection).where(KalshiConnection.user_id == user_id))
    ).scalar_one_or_none()


async def disconnect(session: AsyncSession, user: User) -> bool:
    conn = await get_connection(session, user.id)
    rs = await get_risk_settings(session, user.id)
    rs.live_trading_enabled = False
    rs.live_trading_confirmed_at = None
    if conn is None:
        return False
    # Crypto-shred: delete the encrypted material entirely.
    await session.delete(conn)
    await audit(session, "kalshi.disconnected", actor_user_id=user.id, target_type="user", target_id=user.id)
    return True


def build_user_auth(conn: KalshiConnection, cipher: SecretCipher) -> KalshiAuthService:
    """Decrypt ONE user's credential into an in-memory signer. Never log the result."""
    api_key_id = cipher.decrypt(conn.encrypted_api_key_id).decode()
    pem = cipher.decrypt(conn.encrypted_private_key)
    return KalshiAuthService(api_key_id, pem)


def build_user_client(conn: KalshiConnection, cipher: SecretCipher, settings: Settings | None = None) -> KalshiClient:
    settings = settings or get_settings()
    if conn.status != KalshiConnectionStatus.VERIFIED:
        raise KalshiAuthError("Kalshi connection is not verified.")
    return KalshiClient(
        kalshi_base_url(conn.environment, settings),
        build_user_auth(conn, cipher),
        timeout=settings.kalshi_timeout_seconds,
        max_retries=settings.kalshi_max_retries,
    )


def connection_can_trade(conn: KalshiConnection | None) -> bool:
    return (
        conn is not None
        and conn.status == KalshiConnectionStatus.VERIFIED
        and bool(set(conn.scopes or []) & TRADE_SCOPES)
    )
