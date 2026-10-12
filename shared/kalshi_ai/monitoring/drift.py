"""Model & system monitoring with automatic safety actions.

Critical conditions automatically SUSPEND live trading platform-wide (paper continues).
Suspension is never lifted automatically - an admin must review and clear it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.analytics.metrics import brier_score
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, PaperTrade, Prediction, SourceHealth
from kalshi_ai.domain.enums import OrderStatus, Severity, TradingMode
from kalshi_ai.services.audit import system_event
from kalshi_ai.services.kill_switch import LIVE_TRADING_SUSPENDED, get_flag, set_flag


@dataclass(frozen=True)
class MonitorThresholds:
    min_resolved: int = 50
    calibration_degradation: float = 0.03  # Brier worse than 30d baseline by this much
    market_margin: float = 0.01  # model Brier worse than market Brier by this much => critical
    max_down_share: float = 0.5
    max_latency_ms: float = 5_000
    max_order_error_rate: float = 0.2
    min_orders_for_error_rate: int = 5
    ev_shift: float = 0.05  # change in mean paper P&L per $ risked


@dataclass
class Alert:
    type: str
    severity: Severity
    message: str
    details: dict[str, Any] = field(default_factory=dict)


async def _brier(session: AsyncSession, since: Any, until: Any | None = None) -> tuple[float | None, float | None, int]:
    q = select(Prediction).where(Prediction.outcome_yes.is_not(None), Prediction.ts >= since)
    if until is not None:
        q = q.where(Prediction.ts < until)
    preds = list((await session.execute(q)).scalars())
    probs = [float(p.probability_yes) for p in preds]
    outs = [1 if p.outcome_yes else 0 for p in preds]
    mk = [
        (float(p.market_probability_yes), o)
        for p, o in zip(preds, outs, strict=True)
        if p.market_probability_yes is not None
    ]
    return brier_score(probs, outs), (brier_score([m for m, _ in mk], [o for _, o in mk]) if mk else None), len(preds)


async def run_checks(session: AsyncSession, th: MonitorThresholds | None = None) -> list[Alert]:
    th = th or MonitorThresholds()
    now = utcnow()
    alerts: list[Alert] = []

    # 1. Calibration deterioration and edge vs market.
    recent, recent_market, n_recent = await _brier(session, now - timedelta(days=1))
    baseline, _, n_base = await _brier(session, now - timedelta(days=30), now - timedelta(days=1))
    if n_recent >= th.min_resolved and recent is not None:
        if baseline is not None and n_base >= th.min_resolved and recent - baseline > th.calibration_degradation:
            alerts.append(
                Alert(
                    "calibration_degraded",
                    Severity.WARNING,
                    f"Brier {recent:.3f} vs 30d baseline {baseline:.3f}",
                    {"recent": recent, "baseline": baseline, "n": n_recent},
                )
            )
        if recent_market is not None and recent - recent_market > th.market_margin:
            alerts.append(
                Alert(
                    "model_worse_than_market",
                    Severity.CRITICAL,
                    f"Model Brier {recent:.3f} worse than market {recent_market:.3f}",
                    {"model": recent, "market": recent_market, "n": n_recent},
                )
            )

    # 2. Data quality and latency.
    rows = (await session.execute(select(SourceHealth).where(SourceHealth.status != "disabled"))).scalars().all()
    if rows:
        down = [r.source for r in rows if r.status in ("down", "stale")]
        share = len(down) / len(rows)
        if share > th.max_down_share:
            alerts.append(
                Alert(
                    "data_quality",
                    Severity.CRITICAL,
                    f"{len(down)}/{len(rows)} sources down or stale",
                    {"sources": down},
                )
            )
        elif down:
            alerts.append(
                Alert("data_quality", Severity.WARNING, f"Sources degraded: {', '.join(down)}", {"sources": down})
            )
        slow = [r.source for r in rows if r.latency_ms and r.latency_ms > th.max_latency_ms]
        if slow:
            alerts.append(
                Alert("source_latency", Severity.WARNING, f"High latency: {', '.join(slow)}", {"sources": slow})
            )

    # 3. Trading errors.
    statuses = (
        (
            await session.execute(
                select(Order.status).where(
                    Order.mode == TradingMode.LIVE.value, Order.created_at >= now - timedelta(hours=1)
                )
            )
        )
        .scalars()
        .all()
    )
    if len(statuses) >= th.min_orders_for_error_rate:
        errors = sum(1 for s in statuses if s in (OrderStatus.FAILED, OrderStatus.UNKNOWN))
        rate = errors / len(statuses)
        if rate > th.max_order_error_rate:
            alerts.append(
                Alert(
                    "trading_errors",
                    Severity.CRITICAL,
                    f"Live order error rate {rate:.0%}",
                    {"orders": len(statuses), "errors": errors},
                )
            )

    # 4. Significant performance change (paper P&L per dollar risked, recent vs prior).
    trades = (
        (
            await session.execute(
                select(PaperTrade)
                .where(PaperTrade.closed_at.is_not(None))
                .order_by(PaperTrade.closed_at.desc())
                .limit(250)
            )
        )
        .scalars()
        .all()
    )
    if len(trades) >= 100:

        def ev(ts: list[PaperTrade]) -> float:
            risked = sum(float(t.entry_price) * t.quantity for t in ts) or 1.0
            return sum(float(t.pnl or 0) for t in ts) / risked

        recent_ev, prior_ev = ev(list(trades[:50])), ev(list(trades[50:]))
        if abs(recent_ev - prior_ev) > th.ev_shift:
            alerts.append(
                Alert(
                    "performance_shift",
                    Severity.WARNING,
                    f"Paper EV/$ moved from {prior_ev:+.3f} to {recent_ev:+.3f}",
                    {"recent": recent_ev, "prior": prior_ev},
                )
            )
    return alerts


async def apply_safety_actions(session: AsyncSession, alerts: list[Alert]) -> bool:
    """Record alerts; suspend live trading on any CRITICAL alert. Returns True if suspended now."""
    for a in alerts:
        await system_event(session, f"monitor.{a.type}", a.message, a.severity, a.details)
    critical = [a for a in alerts if a.severity == Severity.CRITICAL]
    if critical and not (await get_flag(session, LIVE_TRADING_SUSPENDED)).active:
        reason = "; ".join(a.message for a in critical)[:400]
        await set_flag(session, LIVE_TRADING_SUSPENDED, True, reason=f"auto: {reason}", actor_user_id=None)
        await system_event(
            session, "monitor.live_trading_suspended", f"Live trading suspended: {reason}", Severity.CRITICAL
        )
        return True
    return False
