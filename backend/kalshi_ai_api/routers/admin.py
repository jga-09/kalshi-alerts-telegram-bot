"""Admin API. EVERY route depends on AdminUser (backend role ADMIN + Telegram ID in ADMIN_TELEGRAM_IDS).

No route returns customer secrets: Kalshi credentials are never selected here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from kalshi_ai.analytics.performance import model_calibration, platform_performance, recent_order_error_rate
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import (
    ActivationCode,
    AuditLog,
    KalshiConnection,
    Order,
    RiskSettings,
    SourceHealth,
    Subscription,
    SubscriptionPlan,
    SystemEvent,
    User,
)
from kalshi_ai.domain.enums import (
    OrderStatus,
    Plan,
    SubscriptionSource,
    SubscriptionStatus,
    UserStatus,
)
from kalshi_ai.services.access_codes import generate_codes, revoke_code
from kalshi_ai.services.audit import audit
from kalshi_ai.services.kill_switch import (
    GLOBAL_KILL_SWITCH,
    LIVE_TRADING_SUSPENDED,
    get_flag,
    set_flag,
    set_global_kill_switch,
)
from kalshi_ai.services.subscriptions import extend_subscription, grant_time
from kalshi_ai.services.users import get_risk_settings, set_user_status
from kalshi_ai_api.deps import AdminUser, SessionDep
from kalshi_ai_api.tasks_bridge import enqueue_cancel_all_open_orders

router = APIRouter(prefix="/api/admin", tags=["admin"])

ACTIVE = [SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIALING.value, SubscriptionStatus.PAST_DUE.value]


async def _count(session: SessionDep, stmt: Any) -> int:
    return int((await session.execute(stmt)).scalar_one())


@router.get("/stats")
async def stats(admin: AdminUser, session: SessionDep) -> dict[str, Any]:
    now = utcnow()
    active_subs = (
        (
            await session.execute(
                select(Subscription).where(Subscription.status.in_(ACTIVE), Subscription.expires_at > now)
            )
        )
        .scalars()
        .all()
    )
    plan_prices = {
        (p.plan, p.interval): p.amount_cents for p in (await session.execute(select(SubscriptionPlan))).scalars()
    }
    mrr_cents = 0
    for s in active_subs:
        if s.source == SubscriptionSource.STRIPE:
            cents = plan_prices.get((s.plan, s.interval), 0)
            mrr_cents += cents // 12 if s.interval.value == "yearly" else cents
    by_plan: dict[str, int] = {}
    for s in active_subs:
        by_plan[s.plan.value] = by_plan.get(s.plan.value, 0) + 1
    return {
        "total_users": await _count(session, select(func.count(User.id))),
        "suspended_users": await _count(
            session, select(func.count(User.id)).where(User.status == UserStatus.SUSPENDED.value)
        ),
        "active_subscriptions": len({s.user_id for s in active_subs}),
        "active_subscriptions_by_plan": by_plan,
        "expired_subscriptions": await _count(
            session, select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.EXPIRED.value)
        ),
        "estimated_mrr_usd": mrr_cents / 100,
        "kalshi_connections": await _count(session, select(func.count(KalshiConnection.id))),
        "active_auto_traders": await _count(
            session, select(func.count(RiskSettings.id)).where(RiskSettings.auto_trading_enabled.is_(True))
        ),
        "live_traders": await _count(
            session, select(func.count(RiskSettings.id)).where(RiskSettings.live_trading_enabled.is_(True))
        ),
        "paper_traders": await _count(
            session, select(func.count(RiskSettings.id)).where(RiskSettings.paper_trading_enabled.is_(True))
        ),
        "open_orders": await _count(
            session,
            select(func.count(Order.id)).where(
                Order.status.in_([OrderStatus.RESTING.value, OrderStatus.SUBMITTED.value, OrderStatus.UNKNOWN.value])
            ),
        ),
        "global_kill_switch": (await get_flag(session, GLOBAL_KILL_SWITCH)).active,
        "live_trading_suspended_by_monitor": (await get_flag(session, LIVE_TRADING_SUSPENDED)).active,
        "order_errors_24h": await recent_order_error_rate(session),
    }


@router.get("/health/sources")
async def source_health(admin: AdminUser, session: SessionDep) -> list[dict[str, Any]]:
    return [
        {
            "source": r.source,
            "status": r.status,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
            "last_error": r.last_error,
            "latency_ms": r.latency_ms,
            "consecutive_failures": r.consecutive_failures,
        }
        for r in (await session.execute(select(SourceHealth).order_by(SourceHealth.source))).scalars()
    ]


@router.get("/performance")
async def performance(admin: AdminUser, session: SessionDep, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    return await platform_performance(session, days)


@router.get("/models")
async def models(admin: AdminUser, session: SessionDep, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    return await model_calibration(session, days)


@router.get("/trades/recent")
async def recent_trades(admin: AdminUser, session: SessionDep, limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Order).order_by(Order.created_at.desc()).limit(limit))).scalars()
    return [
        {
            "id": str(o.id),
            "user_id": str(o.user_id),
            "mode": o.mode.value,
            "market_ticker": o.market_ticker,
            "side": o.side.value,
            "quantity": o.quantity,
            "price": str(o.price),
            "status": o.status.value,
            "reason": o.reason,
            "model_version": o.model_version,
            "created_at": o.created_at.isoformat(),
        }
        for o in rows
    ]


@router.get("/events")
async def events(
    admin: AdminUser, session: SessionDep, severity: str | None = None, limit: int = Query(100, le=1000)
) -> list[dict[str, Any]]:
    q = select(SystemEvent).order_by(SystemEvent.created_at.desc()).limit(limit)
    if severity:
        q = q.where(SystemEvent.severity == severity)
    return [
        {
            "id": e.id,
            "type": e.type,
            "severity": e.severity,
            "message": e.message,
            "details": e.details,
            "created_at": e.created_at.isoformat(),
        }
        for e in (await session.execute(q)).scalars()
    ]


@router.get("/audit-logs")
async def audit_logs(
    admin: AdminUser, session: SessionDep, action: str | None = None, limit: int = Query(100, le=1000)
) -> list[dict[str, Any]]:
    q = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action.startswith(action))
    return [
        {
            "id": str(a.id),
            "action": a.action,
            "actor_type": a.actor_type,
            "actor_user_id": str(a.actor_user_id) if a.actor_user_id else None,
            "target_type": a.target_type,
            "target_id": a.target_id,
            "details": a.details,
            "created_at": a.created_at.isoformat(),
        }
        for a in (await session.execute(q)).scalars()
    ]


# ------------------------------------------------------------------ users


@router.get("/users")
async def list_users(
    admin: AdminUser, session: SessionDep, limit: int = Query(100, le=1000), offset: int = 0
) -> list[dict[str, Any]]:
    rows = (await session.execute(select(User).order_by(User.created_at.desc()).limit(limit).offset(offset))).scalars()
    return [
        {
            "id": str(u.id),
            "telegram_user_id": u.telegram_user_id,
            "telegram_username": u.telegram_username,
            "role": u.role.value,
            "status": u.status.value,
            "created_at": u.created_at.isoformat(),
        }
        for u in rows
    ]


async def _user(session: SessionDep, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return user


class SuspendBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


@router.post("/users/{user_id}/suspend")
async def suspend(user_id: uuid.UUID, body: SuspendBody, admin: AdminUser, session: SessionDep) -> dict[str, str]:
    user = await _user(session, user_id)
    if user.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Admins cannot suspend themselves")
    await set_user_status(session, user, UserStatus.SUSPENDED, actor=admin, reason=body.reason)
    return {"status": "suspended"}


@router.post("/users/{user_id}/reactivate")
async def reactivate(user_id: uuid.UUID, admin: AdminUser, session: SessionDep) -> dict[str, str]:
    await set_user_status(session, await _user(session, user_id), UserStatus.ACTIVE, actor=admin)
    return {"status": "active"}


class AutoTradingBody(BaseModel):
    allowed: bool


@router.post("/users/{user_id}/auto-trading")
async def admin_auto_trading(
    user_id: uuid.UUID, body: AutoTradingBody, admin: AdminUser, session: SessionDep
) -> dict[str, bool]:
    user = await _user(session, user_id)
    rs = await get_risk_settings(session, user.id)
    rs.admin_auto_trading_disabled = not body.allowed
    if not body.allowed:
        rs.auto_trading_enabled = False
        rs.live_trading_enabled = False
    await audit(
        session,
        "admin.user.auto_trading",
        actor_user_id=admin.id,
        actor_type="admin",
        target_type="user",
        target_id=user.id,
        details={"allowed": body.allowed},
    )
    return {"auto_trading_allowed": body.allowed}


class ExtendBody(BaseModel):
    days: int = Field(ge=1, le=3660)
    plan: Plan | None = None


@router.post("/users/{user_id}/extend")
async def extend(user_id: uuid.UUID, body: ExtendBody, admin: AdminUser, session: SessionDep) -> dict[str, str]:
    user = await _user(session, user_id)
    latest = (
        await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.expires_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None and latest.source != SubscriptionSource.STRIPE and (body.plan in (None, latest.plan)):
        sub = await extend_subscription(session, latest, body.days, actor_user_id=admin.id)
    else:
        sub = await grant_time(
            session,
            user,
            body.plan or (latest.plan if latest else Plan.SIGNALS),
            body.days,
            source=SubscriptionSource.ADMIN,
            actor_user_id=admin.id,
        )
    return {"subscription_id": str(sub.id), "expires_at": sub.expires_at.isoformat()}


# ------------------------------------------------------------------ access codes


class GenerateCodesBody(BaseModel):
    plan: Plan
    duration_days: int = Field(ge=1, le=3660, description="7, 30, 90, 365 or any custom duration")
    count: int = Field(1, ge=1, le=500)
    max_uses: int = Field(1, ge=1, le=10_000)
    code_valid_days: int | None = Field(None, ge=1, le=3660, description="Days until the code itself expires")
    note: str | None = Field(None, max_length=255)


@router.post("/codes")
async def create_codes(body: GenerateCodesBody, admin: AdminUser, session: SessionDep) -> dict[str, Any]:
    expires: datetime | None = utcnow() + timedelta(days=body.code_valid_days) if body.code_valid_days else None
    generated = await generate_codes(
        session,
        plan=body.plan,
        duration_days=body.duration_days,
        count=body.count,
        max_uses=body.max_uses,
        code_expires_at=expires,
        note=body.note,
        admin=admin,
    )
    return {
        "warning": "Codes are shown ONCE and are not stored in plaintext. Distribute them securely.",
        "codes": [{"id": str(g.record.id), "code": g.code} for g in generated],
    }


@router.get("/codes")
async def list_codes(admin: AdminUser, session: SessionDep, limit: int = Query(100, le=1000)) -> list[dict[str, Any]]:
    rows = (
        await session.execute(select(ActivationCode).order_by(ActivationCode.created_at.desc()).limit(limit))
    ).scalars()
    return [
        {
            "id": str(c.id),
            "hint": c.code_hint + "…",
            "plan": c.plan.value,
            "duration_days": c.duration_days,
            "status": c.status.value,
            "max_uses": c.max_uses,
            "current_uses": c.current_uses,
            "expires_at": c.expires_at.isoformat() if c.expires_at else None,
            "created_at": c.created_at.isoformat(),
            "note": c.note,
        }
        for c in rows
    ]


@router.post("/codes/{code_id}/revoke")
async def revoke(code_id: uuid.UUID, admin: AdminUser, session: SessionDep) -> dict[str, str]:
    if await revoke_code(session, code_id, admin=admin) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Code not found")
    return {"status": "revoked"}


# ------------------------------------------------------------------ kill switch & platform flags


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = Field(min_length=3, max_length=500)


@router.post("/kill-switch")
async def kill_switch(body: KillSwitchBody, admin: AdminUser, session: SessionDep) -> dict[str, bool]:
    await set_global_kill_switch(session, admin, body.active, body.reason)
    if body.active:
        enqueue_cancel_all_open_orders()
    return {"global_kill_switch": body.active}


@router.post("/live-trading-suspension")
async def live_suspension(body: KillSwitchBody, admin: AdminUser, session: SessionDep) -> dict[str, bool]:
    await set_flag(session, LIVE_TRADING_SUSPENDED, body.active, reason=body.reason, actor_user_id=admin.id)
    await audit(
        session,
        "admin.live_trading_suspension",
        actor_user_id=admin.id,
        actor_type="admin",
        details={"active": body.active, "reason": body.reason},
    )
    return {"live_trading_suspended": body.active}


class PlanPriceBody(BaseModel):
    plan: Plan
    interval: str = Field(pattern="^(monthly|yearly)$")
    stripe_price_id: str = Field(min_length=3, max_length=128)
    amount_cents: int = Field(ge=0, le=10_000_000)
    currency: str = Field("usd", min_length=3, max_length=3)
    active: bool = True


@router.put("/plans")
async def upsert_plan(body: PlanPriceBody, admin: AdminUser, session: SessionDep) -> dict[str, str]:
    from kalshi_ai.domain.enums import BillingInterval

    interval = BillingInterval(body.interval)
    row = (
        await session.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.plan == body.plan.value, SubscriptionPlan.interval == interval.value
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = SubscriptionPlan(plan=body.plan, interval=interval)
        session.add(row)
    row.stripe_price_id, row.amount_cents = body.stripe_price_id, body.amount_cents
    row.currency, row.active = body.currency.lower(), body.active
    await audit(
        session,
        "admin.plan.updated",
        actor_user_id=admin.id,
        actor_type="admin",
        details={"plan": body.plan.value, "interval": body.interval, "amount_cents": body.amount_cents},
    )
    return {"status": "ok", "amount": str(Decimal(body.amount_cents) / 100)}
