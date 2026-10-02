"""Subscription lifecycle. The database is the single source of truth for access."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import RiskSettings, Subscription, User
from kalshi_ai.domain.entitlements import PLAN_FEATURES, PLAN_RANK, Feature
from kalshi_ai.domain.enums import (
    BillingInterval,
    PaymentStatus,
    Plan,
    SubscriptionSource,
    SubscriptionStatus,
    UserStatus,
)
from kalshi_ai.services.audit import audit

ACCESS_GRANTING_STATUSES = (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING, SubscriptionStatus.PAST_DUE)


@dataclass(frozen=True)
class AccessState:
    active: bool
    plan: Plan | None
    expires_at: datetime | None
    subscription_id: uuid.UUID | None
    status: SubscriptionStatus | None

    def has(self, feature: Feature) -> bool:
        return self.active and self.plan is not None and feature in PLAN_FEATURES[self.plan]


INACTIVE = AccessState(False, None, None, None, None)


async def list_subscriptions(session: AsyncSession, user_id: uuid.UUID) -> list[Subscription]:
    res = await session.execute(
        select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.expires_at.desc())
    )
    return list(res.scalars())


async def get_access(session: AsyncSession, user: User, now: datetime | None = None) -> AccessState:
    """Effective access = highest-ranked non-expired access-granting subscription."""
    if user.status != UserStatus.ACTIVE:
        return INACTIVE
    now = now or utcnow()
    res = await session.execute(
        select(Subscription).where(
            Subscription.user_id == user.id,
            Subscription.status.in_([s.value for s in ACCESS_GRANTING_STATUSES]),
            Subscription.expires_at > now,
        )
    )
    subs = list(res.scalars())
    if not subs:
        return INACTIVE
    best = max(subs, key=lambda s: (PLAN_RANK[s.plan], s.expires_at))
    return AccessState(True, best.plan, best.expires_at, best.id, best.status)


async def grant_time(
    session: AsyncSession,
    user: User,
    plan: Plan,
    days: int,
    *,
    source: SubscriptionSource,
    activation_code_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """Grant/extend non-Stripe access (access codes, admin extensions).

    Extends an existing active grant of the same plan+source instead of stacking rows.
    """
    if days <= 0 or days > 3660:
        raise ValueError("days must be between 1 and 3660")
    now = utcnow()
    existing = (
        await session.execute(
            select(Subscription)
            .where(
                Subscription.user_id == user.id,
                Subscription.plan == plan.value,
                Subscription.source == source.value,
                Subscription.status == SubscriptionStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.expires_at = existing.expires_at + timedelta(days=days)
        if activation_code_id:
            existing.activation_code_id = activation_code_id
        sub = existing
    else:
        sub = Subscription(
            user_id=user.id,
            plan=plan,
            interval=BillingInterval.CUSTOM,
            status=SubscriptionStatus.ACTIVE,
            source=source,
            started_at=now,
            expires_at=now + timedelta(days=days),
            payment_status=PaymentStatus.NONE,
            activation_code_id=activation_code_id,
        )
        session.add(sub)
    await session.flush()
    await audit(
        session,
        "subscription.granted",
        actor_user_id=actor_user_id or user.id,
        actor_type="admin" if source == SubscriptionSource.ADMIN else "user",
        target_type="subscription",
        target_id=sub.id,
        details={"plan": plan.value, "days": days, "source": source.value},
    )
    return sub


async def extend_subscription(
    session: AsyncSession, sub: Subscription, days: int, *, actor_user_id: uuid.UUID
) -> Subscription:
    if days <= 0 or days > 3660:
        raise ValueError("days must be between 1 and 3660")
    base = max(sub.expires_at, utcnow())
    sub.expires_at = base + timedelta(days=days)
    if (
        sub.status in (SubscriptionStatus.EXPIRED, SubscriptionStatus.CANCELED)
        and sub.source != SubscriptionSource.STRIPE
    ):
        sub.status = SubscriptionStatus.ACTIVE
    await audit(
        session,
        "admin.subscription.extended",
        actor_user_id=actor_user_id,
        actor_type="admin",
        target_type="subscription",
        target_id=sub.id,
        details={"days": days},
    )
    return sub


async def expire_due_subscriptions(session: AsyncSession, now: datetime | None = None) -> int:
    """Mark lapsed subscriptions expired and revoke trading permissions for users left without access.

    Run periodically by the worker. Returns number of subscriptions expired.
    """
    now = now or utcnow()
    res = await session.execute(
        select(Subscription).where(
            Subscription.status.in_([s.value for s in ACCESS_GRANTING_STATUSES]),
            Subscription.expires_at <= now,
        )
    )
    expired = list(res.scalars())
    affected_users: set[uuid.UUID] = set()
    for sub in expired:
        sub.status = SubscriptionStatus.EXPIRED
        affected_users.add(sub.user_id)
        await audit(session, "subscription.expired", actor_type="system", target_type="subscription", target_id=sub.id)
    await session.flush()
    for user_id in affected_users:
        await enforce_entitlements(session, user_id, now)
    return len(expired)


async def enforce_entitlements(session: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> None:
    """Turn off auto/live trading for a user whose access no longer includes it."""
    user = await session.get(User, user_id)
    if user is None:
        return
    access = await get_access(session, user, now)
    if not access.has(Feature.AUTO_TRADING):
        await session.execute(
            update(RiskSettings)
            .where(RiskSettings.user_id == user_id)
            .values(auto_trading_enabled=False, live_trading_enabled=False)
        )
