"""Transparent rule-based baseline.

Starts from the volatility baseline (price vs strike) when available, otherwise
from the market's own price, then applies small, bounded tilts from momentum,
order flow, news and social. Finally it shrinks toward the market price in
proportion to missing/stale data - so poor data means "no edge", not a guess.
"""

from __future__ import annotations

from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.modeling.base import ModelOutput, PredictionModel, clamp_prob, logit, sigmoid

MAX_TILT_LOGIT = 0.6  # total bounded adjustment (~ +/-14pp around 50%)


class RuleBasedModel(PredictionModel):
    name = "rule_based"
    version = "1.0.0"

    def predict(self, fv: FeatureVector) -> ModelOutput | None:
        market = fv.get("market_prob_yes")
        baseline = fv.get("baseline_prob_yes")
        if market is None and baseline is None:
            return None
        anchor = baseline if baseline is not None else market
        supporting: list[str] = []
        conflicting: list[str] = []
        tilt = 0.0
        direction = 1.0 if fv.spec.direction in ("above", "up", "unknown") else -1.0

        def add(
            name: str,
            value: float | None,
            weight: float,
            label_pos: str,
            label_neg: str,
            underlying_signal: bool = True,
        ) -> None:
            """Underlying-price signals flip for 'below' markets; YES-book signals don't."""
            nonlocal tilt
            if value is None:
                return
            contrib = max(-1.0, min(1.0, value)) * weight * (direction if underlying_signal else 1.0)
            tilt += contrib
            if abs(contrib) >= 0.02:
                label = label_pos if value > 0 else label_neg  # describes the raw signal
                (supporting if contrib > 0 else conflicting).append(label)

        mom = fv.get("ta_momentum")
        add(
            "momentum",
            (mom / 0.002) if mom is not None else None,
            0.15,
            "short-term momentum up",
            "short-term momentum down",
        )
        add("ema", fv.get("ta_ema_spread_norm"), 0.10, "fast EMA above slow EMA", "fast EMA below slow EMA")
        regime = fv.get("ta_trend_regime")
        add("trend", float(regime) if regime is not None else None, 0.08, "uptrend regime", "downtrend regime")
        rsi = fv.get("ta_rsi")
        if rsi is not None and (rsi > 75 or rsi < 25):
            # Stretched RSI favours mean reversion against the move.
            add("rsi", (50 - rsi) / 25, 0.06, "RSI oversold (reversion)", "RSI overbought (reversion)")
        add(
            "orderbook",
            fv.get("ob_signal"),
            0.12,
            "order book leans YES",
            "order book leans NO",
            underlying_signal=False,
        )
        add("news", fv.get("news_score"), 0.10, "recent news positive", "recent news negative")
        add(
            "social",
            fv.get("social_weighted_signal"),
            0.05,
            "quality-weighted social positive",
            "quality-weighted social negative",
        )
        if fv.get("event_high_importance_within_60m"):
            conflicting.append("high-importance economic release within 60 minutes (elevated uncertainty)")
            tilt *= 0.5

        tilt = max(-MAX_TILT_LOGIT, min(MAX_TILT_LOGIT, tilt))
        raw = sigmoid(logit(anchor) + tilt)
        # Shrink toward market price when data is incomplete or sources are degraded.
        quality = fv.completeness * (0.7 if (fv.get("sources_degraded") or 0) > 0 else 1.0)
        if market is not None:
            raw = market + quality * (raw - market)
        confidence = max(
            0.0,
            min(
                1.0,
                0.35
                + 0.45 * quality
                + (0.1 if baseline is not None else 0.0)
                - (0.15 if not fv.get("liquidity_ok") else 0.0),
            ),
        )
        if baseline is not None and market is not None and abs(baseline - market) > 0.25:
            conflicting.append("volatility baseline disagrees strongly with market price")
            confidence *= 0.8
        return ModelOutput(
            self.id,
            clamp_prob(raw),
            confidence,
            supporting,
            conflicting,
            {"anchor": anchor, "tilt_logit": tilt, "quality": quality},
        )
