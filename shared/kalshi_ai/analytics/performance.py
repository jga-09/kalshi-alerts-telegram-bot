"""DB-backed performance analytics for users, admins and model monitoring."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.analytics.metrics import TradeRecord, brier_score, calibration_bins, summarize, summarize_by
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, PaperTrade, Position, Prediction
from kalshi_ai.domain.enums import OrderStatus, PositionStatus, TradingMode


def _paper_record(t: PaperTrade) -> TradeRecord:
    won = (t.pnl or Decimal(0)) > 0 if t.status != PositionStatus.OPEN and t.pnl is not None else None
    return TradeRecord(
        pnl=t.pnl or Decimal(0),
        cost=t.entry_price * t.quantity + t.fees,
        estimated_probability=t.estimated_probability or t.entry_price,
        entry_price=t.entry_price,
        won=won,
        tags={
            "market": t.market_ticker.split("-", 1)[0],
            "hour_utc": f"{t.opened_at.hour:02d}",
            "risk_mode": t.risk_mode.value if t.risk_mode else "unknown",
            "mode": "paper",
        },
    )


async def paper_records(
    session: AsyncSession, user_id: uuid.UUID | None = None, since: datetime | None = None
) -> list[TradeRecord]:
    q = select(PaperTrade)
    if user_id is not None:
        q = q.where(PaperTrade.user_id == user_id)
    if since is not None:
        q = q.where(PaperTrade.opened_at >= since)
    return [_paper_record(t) for t in (await session.execute(q)).scalars()]


async def live_records(
    session: AsyncSession, user_id: uuid.UUID | None = None, since: datetime | None = None
) -> list[TradeRecord]:
    q = select(Position).where(Position.mode == TradingMode.LIVE.value)
    if user_id is not None:
        q = q.where(Position.user_id == user_id)
    if since is not None:
        q = q.where(Position.created_at >= since)
    out = []
    for p in (await session.execute(q)).scalars():
        resolved = p.status != PositionStatus.OPEN
        out.append(
            TradeRecord(
                pnl=p.realized_pnl if resolved else p.unrealized_pnl,
                cost=p.cost_basis,
                estimated_probability=p.avg_price,
                entry_price=p.avg_price,
                won=(p.realized_pnl > 0) if resolved else None,
                tags={
                    "market": p.market_ticker.split("-", 1)[0],
                    "mode": "live",
                    "hour_utc": f"{p.created_at.hour:02d}",
                },
            )
        )
    return out


async def user_performance(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    paper = await paper_records(session, user_id)
    live = await live_records(session, user_id)
    return {"paper": summarize(paper), "live": summarize(live)}


async def platform_performance(session: AsyncSession, days: int = 30) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    paper = await paper_records(session, since=since)
    live = await live_records(session, since=since)
    return {
        "window_days": days,
        "paper": summarize(paper),
        "live": summarize(live),
        "paper_by_market": summarize_by(paper, "market"),
        "paper_by_hour": summarize_by(paper, "hour_utc"),
        "paper_by_risk_mode": summarize_by(paper, "risk_mode"),
    }


async def model_calibration(session: AsyncSession, days: int = 30, model_version: str | None = None) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    q = select(Prediction).where(Prediction.outcome_yes.is_not(None), Prediction.ts >= since)
    if model_version:
        q = q.where(Prediction.model_version == model_version)
    preds = list((await session.execute(q)).scalars())
    probs = [float(p.probability_yes) for p in preds]
    outcomes = [1 if p.outcome_yes else 0 for p in preds]
    market = [
        (float(p.market_probability_yes), o)
        for p, o in zip(preds, outcomes, strict=True)
        if p.market_probability_yes is not None
    ]
    by_model: dict[str, list[Prediction]] = {}
    for p in preds:
        by_model.setdefault(p.model_version, []).append(p)
    return {
        "window_days": days,
        "resolved_predictions": len(preds),
        "model_brier": brier_score(probs, outcomes),
        # Baseline: if the model isn't beating the market's own implied probability it has no edge.
        "market_brier": brier_score([m for m, _ in market], [o for _, o in market]) if market else None,
        "calibration": [b.__dict__ for b in calibration_bins(probs, outcomes)],
        "by_model": {
            name: {
                "count": len(items),
                "brier": brier_score(
                    [float(i.probability_yes) for i in items], [1 if i.outcome_yes else 0 for i in items]
                ),
            }
            for name, items in by_model.items()
        },
    }


async def recent_order_error_rate(session: AsyncSession, hours: int = 24) -> dict[str, Any]:
    since = utcnow() - timedelta(hours=hours)
    rows = (
        (
            await session.execute(
                select(Order.status).where(Order.mode == TradingMode.LIVE.value, Order.created_at >= since)
            )
        )
        .scalars()
        .all()
    )
    total = len(rows)
    errors = sum(1 for s in rows if s in (OrderStatus.FAILED, OrderStatus.UNKNOWN))
    return {
        "window_hours": hours,
        "live_orders": total,
        "errors": errors,
        "error_rate": errors / total if total else 0.0,
    }
