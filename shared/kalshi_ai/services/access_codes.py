"""Activation (access) codes: generation, redemption with brute-force protection, revocation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import ActivationCode, ActivationCodeRedemption, Subscription, User
from kalshi_ai.domain.enums import ActivationCodeStatus, Plan, SubscriptionSource
from kalshi_ai.security.hashing import generate_activation_code, hash_code, normalize_code
from kalshi_ai.services.audit import audit
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.subscriptions import grant_time

STANDARD_DURATIONS = (7, 30, 90, 365)


class RedeemOutcome(StrEnum):
    ACTIVATED = "activated"
    INVALID = "invalid"  # unknown/expired/revoked/exhausted are indistinguishable to the user
    ALREADY_USED = "already_used"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RedeemResult:
    outcome: RedeemOutcome
    subscription: Subscription | None = None
    retry_after_seconds: int = 0


@dataclass(frozen=True)
class GeneratedCode:
    code: str  # plaintext - returned ONCE to the admin, never stored
    record: ActivationCode


async def generate_codes(
    session: AsyncSession,
    *,
    plan: Plan,
    duration_days: int,
    count: int = 1,
    max_uses: int = 1,
    code_expires_at: datetime | None = None,
    note: str | None = None,
    admin: User,
) -> list[GeneratedCode]:
    if not 1 <= duration_days <= 3660:
        raise ValueError("duration_days must be 1..3660")
    if not 1 <= count <= 500:
        raise ValueError("count must be 1..500")
    if not 1 <= max_uses <= 10_000:
        raise ValueError("max_uses must be 1..10000")
    if code_expires_at is not None and code_expires_at <= utcnow():
        raise ValueError("code expiry must be in the future")
    out: list[GeneratedCode] = []
    for _ in range(count):
        code = generate_activation_code()
        record = ActivationCode(
            code_hash=hash_code(code),
            code_hint=code[:8],
            plan=plan,
            duration_days=duration_days,
            expires_at=code_expires_at,
            max_uses=max_uses,
            note=note,
            created_by_user_id=admin.id,
        )
        session.add(record)
        out.append(GeneratedCode(code=code, record=record))
    await session.flush()
    await audit(
        session,
        "admin.codes.generated",
        actor_user_id=admin.id,
        actor_type="admin",
        target_type="activation_code",
        details={"count": count, "plan": plan.value, "duration_days": duration_days, "max_uses": max_uses},
    )
    return out


async def revoke_code(session: AsyncSession, code_id: uuid.UUID, *, admin: User) -> ActivationCode | None:
    record = await session.get(ActivationCode, code_id)
    if record is None:
        return None
    record.status = ActivationCodeStatus.REVOKED
    record.revoked_at = utcnow()
    await audit(
        session,
        "admin.codes.revoked",
        actor_user_id=admin.id,
        actor_type="admin",
        target_type="activation_code",
        target_id=record.id,
    )
    return record


def _looks_like_code(raw: str) -> bool:
    norm = normalize_code(raw)
    return 8 <= len(norm) <= 40


async def redeem_code(
    session: AsyncSession,
    user: User,
    raw_code: str,
    limiter: RateLimiter,
    settings: Settings | None = None,
) -> RedeemResult:
    settings = settings or get_settings()
    # Rate limit per user AND globally per hour to blunt distributed guessing.
    per_user = await limiter.hit(
        f"activate:user:{user.id}", settings.activation_max_attempts, settings.activation_window_seconds
    )
    if not per_user.backend_available:
        return RedeemResult(RedeemOutcome.UNAVAILABLE)
    if not per_user.allowed:
        await audit(session, "code.rate_limited", actor_user_id=user.id, target_type="user", target_id=user.id)
        return RedeemResult(RedeemOutcome.RATE_LIMITED, retry_after_seconds=per_user.retry_after_seconds)
    global_limit = await limiter.hit("activate:global", 2000, 3600)
    if not global_limit.allowed:
        return RedeemResult(RedeemOutcome.RATE_LIMITED, retry_after_seconds=global_limit.retry_after_seconds)

    if not _looks_like_code(raw_code):
        await audit(session, "code.invalid_attempt", actor_user_id=user.id)
        return RedeemResult(RedeemOutcome.INVALID)

    now = utcnow()
    # Row lock (Postgres) so concurrent redemptions cannot exceed max_uses.
    record = (
        await session.execute(
            select(ActivationCode).where(ActivationCode.code_hash == hash_code(raw_code)).with_for_update()
        )
    ).scalar_one_or_none()

    if record is None:
        await audit(session, "code.invalid_attempt", actor_user_id=user.id)
        return RedeemResult(RedeemOutcome.INVALID)
    if record.expires_at is not None and record.expires_at <= now:
        if record.status == ActivationCodeStatus.ACTIVE:
            record.status = ActivationCodeStatus.EXPIRED
        await audit(session, "code.expired_attempt", actor_user_id=user.id, target_id=record.id)
        return RedeemResult(RedeemOutcome.INVALID)
    if record.status != ActivationCodeStatus.ACTIVE or record.current_uses >= record.max_uses:
        await audit(
            session,
            "code.unusable_attempt",
            actor_user_id=user.id,
            target_id=record.id,
            details={"status": record.status.value},
        )
        return RedeemResult(RedeemOutcome.INVALID)

    prior = (
        await session.execute(
            select(ActivationCodeRedemption.id).where(
                ActivationCodeRedemption.code_id == record.id, ActivationCodeRedemption.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if prior is not None:
        return RedeemResult(RedeemOutcome.ALREADY_USED)

    # Redemption row + conditional use increment + grant happen in ONE savepoint:
    # either all succeed or none do. The unique (code_id, user_id) constraint and the
    # conditional UPDATE are race-safe guards in addition to the row lock.
    try:
        async with session.begin_nested():
            redemption = ActivationCodeRedemption(code_id=record.id, user_id=user.id)
            session.add(redemption)
            await session.flush()
            result = await session.execute(
                update(ActivationCode)
                .where(
                    ActivationCode.id == record.id,
                    ActivationCode.current_uses < ActivationCode.max_uses,
                    ActivationCode.status == ActivationCodeStatus.ACTIVE.value,
                )
                .values(current_uses=ActivationCode.current_uses + 1)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise _CodeExhaustedError
            await session.refresh(record)
            if record.current_uses >= record.max_uses:
                record.status = ActivationCodeStatus.EXHAUSTED
            sub = await grant_time(
                session,
                user,
                record.plan,
                record.duration_days,
                source=SubscriptionSource.ACCESS_CODE,
                activation_code_id=record.id,
            )
            redemption.subscription_id = sub.id
    except IntegrityError:
        return RedeemResult(RedeemOutcome.ALREADY_USED)
    except _CodeExhaustedError:
        return RedeemResult(RedeemOutcome.INVALID)
    await audit(session, "code.redeemed", actor_user_id=user.id, target_type="activation_code", target_id=record.id)
    return RedeemResult(RedeemOutcome.ACTIVATED, subscription=sub)


class _CodeExhaustedError(Exception):
    pass


def default_code_expiry(days: int = 90) -> datetime:
    return utcnow() + timedelta(days=days)
