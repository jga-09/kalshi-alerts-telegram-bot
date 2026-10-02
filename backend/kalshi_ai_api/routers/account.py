"""Customer self-service endpoints. Every query is scoped to the authenticated user."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from kalshi_ai.db.models import Order, PaperTrade
from kalshi_ai.domain.enums import RiskMode, TradingMode
from kalshi_ai.services.kalshi_connections import get_connection
from kalshi_ai.services.kill_switch import activate_user_kill_switch
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai.services.trading_controls import (
    confirm_live_trading,
    disable_live_trading,
    limits_for,
    review_live_trading,
    set_auto_trading,
    set_risk_mode,
)
from kalshi_ai.services.trading_state import open_positions
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai_api.deps import CurrentUser, SessionDep

router = APIRouter(prefix="/api/me", tags=["account"])


@router.get("/profile")
async def profile(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    access = await get_access(session, user)
    conn = await get_connection(session, user.id)
    return {
        "id": str(user.id),
        "telegram_username": user.telegram_username,
        "first_name": user.first_name,
        "created_at": user.created_at.isoformat(),
        "subscription": {
            "active": access.active,
            "plan": access.plan.value if access.plan else None,
            "expires_at": access.expires_at.isoformat() if access.expires_at else None,
        },
        "kalshi_connected": conn is not None,
        "disclosures_accepted": user.disclosures_accepted_at is not None,
    }


@router.get("/settings")
async def get_settings_endpoint(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    rs = await get_risk_settings(session, user.id)
    return {
        "risk_mode": rs.risk_mode.value,
        "overrides": rs.overrides,
        "effective_limits": limits_for(rs).as_public_dict(),
        "paper_trading_enabled": rs.paper_trading_enabled,
        "auto_trading_enabled": rs.auto_trading_enabled,
        "live_trading_enabled": rs.live_trading_enabled,
        "kill_switch_active": rs.kill_switch_active,
    }


class RiskUpdate(BaseModel):
    risk_mode: RiskMode
    overrides: dict[str, Any] | None = Field(default=None, description="Only stricter values take effect")


@router.put("/risk")
async def update_risk(body: RiskUpdate, user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    try:
        limits = await set_risk_mode(session, user, body.risk_mode, body.overrides)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    return {"effective_limits": limits.as_public_dict(), "live_trading_requires_reconfirmation": True}


class Toggle(BaseModel):
    enabled: bool


@router.post("/auto-trading")
async def auto_trading(body: Toggle, user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    blockers = await set_auto_trading(session, user, body.enabled)
    return {"enabled": body.enabled and not blockers, "blockers": [b.value for b in blockers]}


@router.get("/live-trading/review")
async def live_review(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    return (await review_live_trading(session, user)).summary()


class LiveConfirm(BaseModel):
    fingerprint: str = Field(min_length=64, max_length=64)
    acknowledge_risk: bool


@router.post("/live-trading/confirm")
async def live_confirm(body: LiveConfirm, user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    if not body.acknowledge_risk:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You must acknowledge the trading risks")
    blockers = await confirm_live_trading(session, user, body.fingerprint)
    return {"live_trading_enabled": not blockers, "blockers": [b.value for b in blockers]}


@router.post("/live-trading/disable")
async def live_disable(user: CurrentUser, session: SessionDep) -> dict[str, bool]:
    await disable_live_trading(session, user)
    return {"live_trading_enabled": False}


@router.post("/emergency-stop")
async def emergency_stop(user: CurrentUser, session: SessionDep) -> dict[str, Any]:
    """Blocks new automated orders immediately. Open-order cancellation runs in the worker."""
    await activate_user_kill_switch(session, user)
    from kalshi_ai_api.tasks_bridge import enqueue_cancel_open_orders

    enqueue_cancel_open_orders(user.id)
    return {"kill_switch_active": True}


@router.get("/positions")
async def positions(user: CurrentUser, session: SessionDep, mode: TradingMode = TradingMode.PAPER) -> list[dict]:
    return [
        {
            "market_ticker": p.market_ticker,
            "side": p.side.value,
            "quantity": str(p.quantity),
            "avg_price": str(p.avg_price),
            "mark_price": str(p.mark_price) if p.mark_price is not None else None,
            "unrealized_pnl": str(p.unrealized_pnl),
            "realized_pnl": str(p.realized_pnl),
        }
        for p in await open_positions(session, user.id, mode)
    ]


@router.get("/orders")
async def orders(user: CurrentUser, session: SessionDep, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(min(limit, 200))
        )
    ).scalars()
    return [
        {
            "id": str(o.id),
            "mode": o.mode.value,
            "market_ticker": o.market_ticker,
            "side": o.side.value,
            "quantity": o.quantity,
            "price": str(o.price),
            "status": o.status.value,
            "reason": o.reason,
            "created_at": o.created_at.isoformat(),
        }
        for o in rows
    ]


@router.get("/paper-trades")
async def paper_trades(user: CurrentUser, session: SessionDep, limit: int = 50) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(PaperTrade)
            .where(PaperTrade.user_id == user.id)
            .order_by(PaperTrade.opened_at.desc())
            .limit(min(limit, 200))
        )
    ).scalars()
    return [
        {
            "market_ticker": t.market_ticker,
            "side": t.side.value,
            "quantity": t.quantity,
            "entry_price": str(t.entry_price),
            "exit_price": str(t.exit_price) if t.exit_price is not None else None,
            "pnl": str(t.pnl) if t.pnl is not None else None,
            "status": t.status.value,
            "opened_at": t.opened_at.isoformat(),
        }
        for t in rows
    ]
