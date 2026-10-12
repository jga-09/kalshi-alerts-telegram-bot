"""StrategyEngine: turns one prediction (model probability + features) into strategy signals.

Each strategy is a different, independent rule for when and how to act on the model:

* Hammer - trend-following. Model edge at the ask AND the underlying/market trend points the same
  way. Takes the ask, so it fills reliably.
* Drift  - value. Rests a limit order one tick inside the spread when the model says the side is
  worth more than that price (and at least the ask). Some orders never fill; that is the point.
* Fade   - contrarian. Buys a cheap side (<= 35c) right after the crowd pushed it down hard, when
  the model still sees edge. Wins less often, but each win pays more.
* Surge  - only Drift's strongest setups (large edge even at the ask, high confidence), taken at
  the live ask. Sits out weekday afternoons 12-6pm ET.

No strategy can trade by itself: every signal goes through ExecutionPipeline -> RiskEngine.
When strategies disagree on the side of one market, none of them trade it (conflict = no clarity).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.domain.enums import ConfidenceLevel, EntryStyle, Side, SignalAction, StrategyId
from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.modeling.prediction import PredictionResult
from kalshi_ai.services.trading_state import correlation_group
from kalshi_ai.signals.engine import SignalDecision, compute_edges, d

NEW_YORK = ZoneInfo("America/New_York")
TICK = Decimal("0.01")
ONE = Decimal(1)


@dataclass(frozen=True)
class StrategyParams:
    hammer_min_trend: float = 0.25  # -1..1 trend score that must point toward the side bought
    fade_max_price: Decimal = Decimal("0.35")
    fade_min_price_drop: float = 0.08  # the side's price fell >= 8c over the recent trade window...
    fade_zscore: float = 2.0  # ...or the underlying is stretched >= 2 sd against the side...
    fade_rsi: float = 75.0  # ...or RSI is beyond 75/25 against the side
    surge_edge_multiple: Decimal = Decimal("1.5")  # edge at the ask >= 1.5 x min edge
    surge_min_confidence: Decimal = Decimal("0.70")
    surge_blackout_hours: tuple[int, int] = (12, 18)  # weekdays, America/New_York
    take_ttl: timedelta = timedelta(minutes=3)
    make_ttl: timedelta = timedelta(minutes=5)  # how long a Drift order may rest


@dataclass
class StrategyResult:
    signals: list[SignalDecision] = field(default_factory=list)
    notes: dict[str, str] = field(default_factory=dict)  # strategy -> why it did / didn't fire
    conflict: bool = False


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def trend_toward_yes(fv: FeatureVector) -> float | None:
    """-1..1: how strongly recent movement points toward YES winning."""
    parts: list[float] = []
    direction = {"above": 1.0, "up": 1.0, "below": -1.0}.get(fv.spec.direction)
    if direction is not None:  # underlying trend only means something when we know the strike direction
        if (mom := fv.get("ta_momentum")) is not None:
            parts.append(_clamp(mom / 0.002) * direction)
        if (ema := fv.get("ta_ema_spread_norm")) is not None:
            parts.append(_clamp(ema) * direction)
        if (regime := fv.get("ta_trend_regime")) is not None:
            parts.append(float(regime) * direction)
    if (ob := fv.get("ob_signal")) is not None:
        parts.append(_clamp(ob))
    if (pc := fv.get("ob_price_change")) is not None:
        parts.append(_clamp(pc / 0.05))
    return sum(parts) / len(parts) if parts else None


def stretched_against(fv: FeatureVector, side: Side, p: StrategyParams) -> str | None:
    """Reason text if the crowd has pushed hard AGAINST `side` (i.e. `side` got cheap fast)."""
    sign = 1.0 if side == Side.YES else -1.0  # +1: moves toward YES are "for" the side
    if (pc := fv.get("ob_price_change")) is not None and pc * sign <= -p.fade_min_price_drop:
        return f"price moved {pc * 100:+.0f}c against {side.value.upper()}"
    direction = {"above": 1.0, "up": 1.0, "below": -1.0}.get(fv.spec.direction)
    if direction is not None:
        if (z := fv.get("ta_zscore")) is not None and z * direction * sign <= -p.fade_zscore:
            return f"underlying stretched {z:+.1f} sd"
        if (rsi := fv.get("ta_rsi")) is not None:
            if direction * sign > 0 and rsi <= 100 - p.fade_rsi:
                return f"underlying oversold (RSI {rsi:.0f})"
            if direction * sign < 0 and rsi >= p.fade_rsi:
                return f"underlying overbought (RSI {rsi:.0f})"
    return None


def in_surge_blackout(now: datetime, hours: tuple[int, int]) -> bool:
    local = now.astimezone(NEW_YORK)
    return local.weekday() < 5 and hours[0] <= local.hour < hours[1]


class StrategyEngine:
    def __init__(self, settings: Settings | None = None, params: StrategyParams | None = None):
        self.settings = settings or get_settings()
        self.params = params or StrategyParams()

    @property
    def min_edge(self) -> Decimal:
        return max(self.settings.min_edge, self.settings.hard_min_edge)

    def evaluate(self, pred: PredictionResult) -> StrategyResult:
        res = StrategyResult()
        fv = pred.features
        if fv is None:
            return res
        p, min_edge, conf = self.params, self.min_edge, d(pred.confidence)
        if conf < self.settings.hard_min_confidence:
            res.notes = {s.value: f"confidence {conf} below platform minimum" for s in StrategyId}
            return res
        asks = {e.side: e for e in compute_edges(pred.probability_yes, fv.get("yes_ask"), fv.get("no_ask"))}
        bids = {Side.YES: fv.get("yes_bid"), Side.NO: fv.get("no_bid")}
        prob = {Side.YES: d(pred.probability_yes), Side.NO: d(1 - pred.probability_yes)}
        liquid = bool(fv.get("liquidity_ok"))
        trend_yes = trend_toward_yes(fv)
        candidates: list[tuple[StrategyId, Side, EntryStyle, Decimal, dict[str, object]]] = []

        # --- Hammer: trend + edge at the ask -------------------------------------------
        best = None
        for side, e in asks.items():
            trend = None if trend_yes is None else (trend_yes if side == Side.YES else -trend_yes)
            if e.edge >= min_edge and liquid and trend is not None and trend >= p.hammer_min_trend:
                if best is None or e.edge > best[1].edge:
                    best = (side, e, trend)
        if best:
            side, e, trend = best
            candidates.append((StrategyId.HAMMER, side, EntryStyle.TAKE, e.market_probability, {"trend": trend}))
        else:
            res.notes["hammer"] = "no edge at the ask with a confirming trend"

        # --- Drift: limit one tick inside the spread -------------------------------------
        drift = None
        for side in (Side.YES, Side.NO):
            bid, ask = bids[side], asks.get(side)
            if bid is None or ask is None:
                continue
            limit = min(d(bid) + TICK, ask.market_probability - TICK)
            if not (TICK <= limit < ONE):
                continue
            edge_at_limit = prob[side] - limit
            # Value must exist even at the ask: resting orders get filled most when we are wrong.
            if edge_at_limit >= min_edge and ask.edge >= 0 and (drift is None or edge_at_limit > drift[2]):
                drift = (side, limit, edge_at_limit, ask)
        if drift:
            side, limit, edge_at_limit, ask = drift
            candidates.append(
                (
                    StrategyId.DRIFT,
                    side,
                    EntryStyle.MAKE,
                    limit,
                    {"limit": str(limit), "ask": str(ask.market_probability)},
                )
            )
            # --- Surge: Drift's strongest setups, at the live ask ------------------------
            strong = ask.edge >= min_edge * p.surge_edge_multiple and conf >= p.surge_min_confidence and liquid
            if strong and not in_surge_blackout(fv.as_of, p.surge_blackout_hours):
                candidates.append((StrategyId.SURGE, side, EntryStyle.TAKE, ask.market_probability, {}))
            else:
                res.notes["surge"] = "weekday 12-6pm ET blackout" if strong else "setup not strong enough for Surge"
        else:
            res.notes["drift"] = "no value inside the spread"
            res.notes["surge"] = "no Drift setup"

        # --- Fade: buy the cheap side after an overreaction ------------------------------
        fade = None
        for side, e in asks.items():
            if e.market_probability > p.fade_max_price or e.edge < min_edge:
                continue
            why = stretched_against(fv, side, p)
            if why and (fade is None or e.edge > fade[1].edge):
                fade = (side, e, why)
        if fade:
            side, e, why = fade
            candidates.append((StrategyId.FADE, side, EntryStyle.TAKE, e.market_probability, {"overreaction": why}))
        else:
            res.notes["fade"] = "no cheap side after an overreaction"

        sides = {c[1] for c in candidates}
        if len(sides) > 1:
            res.conflict = True
            for c in candidates:
                res.notes[c[0].value] = "strategies disagree on direction - standing aside"
            return res
        for strategy, side, style, entry, extra in candidates:
            res.signals.append(self._decision(pred, fv, strategy, side, style, entry, prob[side], conf, extra))
            res.notes[strategy.value] = f"fired: {side.value.upper()} at {entry * 100:.0f}c"
        return res

    def _decision(
        self,
        pred: PredictionResult,
        fv: FeatureVector,
        strategy: StrategyId,
        side: Side,
        style: EntryStyle,
        entry: Decimal,
        est: Decimal,
        conf: Decimal,
        extra: dict[str, object],
    ) -> SignalDecision:
        ttl = self.params.make_ttl if style == EntryStyle.MAKE else self.params.take_ttl
        expires = fv.as_of + ttl
        if (minutes_left := fv.get("minutes_to_close")) is not None:
            expires = min(expires, fv.as_of + timedelta(minutes=max(0.0, minutes_left - 2)))
        liquid = bool(fv.get("liquidity_ok"))
        risk_level = (
            "high"
            if pred.confidence_level == ConfidenceLevel.LOW or not liquid
            else ("medium" if pred.confidence_level == ConfidenceLevel.MEDIUM else "low")
        )
        bucket = fv.as_of.replace(second=0, microsecond=0).strftime("%Y%m%dT%H%M")
        model_tag = hashlib.sha256(pred.model_version.encode()).hexdigest()[:10]
        key = f"{fv.market_ticker}:{strategy.value}:{bucket}:{model_tag}"
        if len(key) > 128:
            key = hashlib.sha256(key.encode()).hexdigest()
        return SignalDecision(
            market_ticker=fv.market_ticker,
            correlation_group=correlation_group(fv.market_ticker),
            action=SignalAction.TRADE_IF_RISK_PASSES,
            side=side,
            estimated_probability=est,
            market_probability=entry,
            entry_price=entry,
            edge=est - entry,
            confidence=conf,
            confidence_level=pred.confidence_level,
            risk_level=risk_level,
            model_version=pred.model_version,
            as_of=fv.as_of,
            expires_at=expires,
            idempotency_key=key,
            data_age_seconds=pred.data_age_seconds,
            reasons={
                "strategy": strategy.value,
                "entry_style": style.value,
                "supporting": pred.supporting,
                "conflicting": pred.conflicting,
                "min_edge": str(self.min_edge),
                **{k: (str(v) if isinstance(v, Decimal) else v) for k, v in extra.items()},
            },
            strategy=strategy,
            entry_style=style,
        )
