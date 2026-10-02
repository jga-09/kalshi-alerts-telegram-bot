"""SignalEngine + edge calculation.

For a binary market bought at its ask:
    market_probability(side) = ask price of that side (what we would actually pay)
    edge(side)               = estimated P(side) - market_probability(side)

Predicting UP is never enough: a signal is TRADE_IF_RISK_PASSES only when the
edge clears the minimum edge AND confidence/liquidity are adequate. Even then,
the risk engine and execution validator decide independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.models import Signal
from kalshi_ai.domain.enums import ConfidenceLevel, Side, SignalAction
from kalshi_ai.modeling.prediction import PredictionResult
from kalshi_ai.services.trading_state import correlation_group

Q = Decimal("0.0001")


def d(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Q, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class EdgeView:
    side: Side
    estimated_probability: Decimal
    market_probability: Decimal
    edge: Decimal


def compute_edges(prob_yes: float, yes_ask: float | None, no_ask: float | None) -> list[EdgeView]:
    out = []
    if yes_ask is not None and 0 < yes_ask < 1:
        out.append(EdgeView(Side.YES, d(prob_yes), d(yes_ask), d(prob_yes) - d(yes_ask)))
    if no_ask is not None and 0 < no_ask < 1:
        out.append(EdgeView(Side.NO, d(1 - prob_yes), d(no_ask), d(1 - prob_yes) - d(no_ask)))
    return out


@dataclass
class SignalDecision:
    market_ticker: str
    correlation_group: str
    action: SignalAction
    side: Side
    estimated_probability: Decimal
    market_probability: Decimal
    entry_price: Decimal
    edge: Decimal
    confidence: Decimal
    confidence_level: ConfidenceLevel
    risk_level: str
    model_version: str
    as_of: datetime
    expires_at: datetime
    idempotency_key: str
    data_age_seconds: float
    reasons: dict[str, Any] = field(default_factory=dict)
    signal_id: Any = None


class SignalEngine:
    def __init__(self, settings: Settings | None = None, ttl: timedelta = timedelta(minutes=3)):
        self.settings = settings or get_settings()
        self.ttl = ttl

    def decide(self, pred: PredictionResult) -> SignalDecision | None:
        fv = pred.features
        if fv is None:
            return None
        edges = compute_edges(pred.probability_yes, fv.get("yes_ask"), fv.get("no_ask"))
        if not edges:
            return None
        best = max(edges, key=lambda e: e.edge)
        min_edge = max(self.settings.min_edge, self.settings.hard_min_edge)
        liquidity_ok = bool(fv.get("liquidity_ok"))
        conf = d(pred.confidence)
        if best.edge >= min_edge and conf >= self.settings.hard_min_confidence and liquidity_ok:
            action = SignalAction.TRADE_IF_RISK_PASSES
        elif best.edge > 0:
            action = SignalAction.WATCH
        else:
            action = SignalAction.NO_TRADE
        minutes_left = fv.get("minutes_to_close")
        expires = fv.as_of + self.ttl
        if minutes_left is not None:
            expires = min(expires, fv.as_of + timedelta(minutes=max(0.0, minutes_left - 1)))
        risk_level = (
            "high"
            if pred.confidence_level == ConfidenceLevel.LOW or not liquidity_ok
            else ("medium" if pred.confidence_level == ConfidenceLevel.MEDIUM else "low")
        )
        bucket = fv.as_of.replace(second=0, microsecond=0).strftime("%Y%m%dT%H%M")
        return SignalDecision(
            market_ticker=fv.market_ticker,
            correlation_group=correlation_group(fv.market_ticker),
            action=action,
            side=best.side,
            estimated_probability=best.estimated_probability,
            market_probability=best.market_probability,
            entry_price=best.market_probability,
            edge=best.edge,
            confidence=conf,
            confidence_level=pred.confidence_level,
            risk_level=risk_level,
            model_version=pred.model_version,
            as_of=fv.as_of,
            expires_at=expires,
            # Same market + model + minute => same key => a replay can never create a second signal.
            idempotency_key=f"{fv.market_ticker}:{pred.model_version}:{bucket}",
            data_age_seconds=pred.data_age_seconds,
            reasons={
                "supporting": pred.supporting,
                "conflicting": pred.conflicting,
                "all_edges": [{"side": e.side.value, "edge": str(e.edge)} for e in edges],
                "min_edge": str(min_edge),
                "liquidity_ok": liquidity_ok,
            },
        )


async def persist_signal(session: AsyncSession, decision: SignalDecision, prediction_id: Any) -> Signal:
    """Idempotent: returns the existing row if this signal key was already stored."""
    existing = (
        await session.execute(select(Signal).where(Signal.idempotency_key == decision.idempotency_key))
    ).scalar_one_or_none()
    if existing is not None:
        decision.signal_id = existing.id
        return existing
    row = Signal(
        prediction_id=prediction_id,
        market_ticker=decision.market_ticker,
        ts=decision.as_of,
        side=decision.side,
        action=decision.action,
        estimated_probability=decision.estimated_probability,
        market_probability=decision.market_probability,
        entry_price=decision.entry_price,
        edge=decision.edge,
        confidence=float(decision.confidence),
        confidence_level=decision.confidence_level,
        risk_level=decision.risk_level,
        model_version=decision.model_version,
        expires_at=decision.expires_at,
        idempotency_key=decision.idempotency_key,
        reasons=decision.reasons,
    )
    session.add(row)
    await session.flush()
    decision.signal_id = row.id
    return row
