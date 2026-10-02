"""TechnicalAnalysisEngine - turns candles into a compact, named feature set.

For 15-minute Kalshi markets we emphasise short lookbacks on 1-minute candles,
but smooth with EMAs/ATR normalisation to avoid reacting to single-tick noise.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from kalshi_ai.data.base import Candle
from kalshi_ai.ta import indicators as ind


@dataclass(frozen=True)
class TAConfig:
    rsi_period: int = 14
    ema_fast: int = 9
    ema_slow: int = 21
    trend_fast: int = 20
    trend_slow: int = 50
    bb_period: int = 20
    atr_period: int = 14
    momentum_period: int = 5
    vol_period: int = 20
    breakout_lookback: int = 20
    min_candles: int = 30


def _last(a: np.ndarray) -> float | None:
    if len(a) == 0 or np.isnan(a[-1]):
        return None
    return float(a[-1])


def compute_features(candles: list[Candle], cfg: TAConfig | None = None) -> dict[str, float | int | None]:
    cfg = cfg or TAConfig()
    if len(candles) < cfg.min_candles:
        return {"ta_available": 0, "ta_candles": len(candles)}
    o = np.array([c.open for c in candles])
    h = np.array([c.high for c in candles])
    lo = np.array([c.low for c in candles])
    c = np.array([c.close for c in candles])
    v = np.array([cd.volume for cd in candles])
    price = float(c[-1])
    atr_v = _last(ind.atr(h, lo, c, cfg.atr_period))
    _, macd_sig, macd_hist = ind.macd(c)
    _, _, _, pct_b = ind.bollinger(c, cfg.bb_period)
    vwap_v = _last(ind.vwap(h, lo, c, v, window=min(len(c), 60)))
    support, resistance = ind.support_resistance(h, lo, 30)
    rv = _last(ind.realized_volatility(c, cfg.vol_period))
    zs = _last(ind.zscore(c, cfg.bb_period))
    ema_f, ema_s = _last(ind.ema(c, cfg.ema_fast)), _last(ind.ema(c, cfg.ema_slow))
    features: dict[str, float | int | None] = {
        "ta_available": 1,
        "ta_candles": len(candles),
        "price": price,
        "rsi": _last(ind.rsi(c, cfg.rsi_period)),
        "macd_hist_norm": (_last(macd_hist) / atr_v) if atr_v and _last(macd_hist) is not None else None,
        "macd_signal": _last(macd_sig),
        "ema_spread_norm": ((ema_f - ema_s) / atr_v) if atr_v and ema_f and ema_s else None,
        "sma20_dist": (price / _last(ind.sma(c, 20)) - 1.0) if _last(ind.sma(c, 20)) else None,
        "vwap_dist": (price / vwap_v - 1.0) if vwap_v else None,
        "bb_pct_b": _last(pct_b),
        "atr": atr_v,
        "atr_pct": (atr_v / price) if atr_v else None,
        "momentum": _last(ind.momentum(c, cfg.momentum_period)),
        "momentum_15": _last(ind.momentum(c, 15)),
        "acceleration": _last(ind.acceleration(c, cfg.momentum_period)),
        "volume_momentum": _last(ind.volume_momentum(v)),
        "realized_vol": rv,
        "zscore": zs,
        "dist_to_support_atr": ((price - support) / atr_v) if atr_v else None,
        "dist_to_resistance_atr": ((resistance - price) / atr_v) if atr_v else None,
        "breakout": ind.breakout(c, h, lo, cfg.breakout_lookback),
        "trend_regime": ind.trend_regime(c, cfg.trend_fast, cfg.trend_slow),
        "mean_reversion_signal": (-1 if zs is not None and zs > 2 else 1 if zs is not None and zs < -2 else 0),
        "last_open": float(o[-1]),
    }
    return {
        k: (None if isinstance(val, float) and (math.isnan(val) or math.isinf(val)) else val)
        for k, val in features.items()
    }


def probability_above_strike(
    price: float, strike: float, sigma_per_min: float, minutes: float, drift_per_min: float = 0.0
) -> float | None:
    """P(price_T > strike) under a driftless-ish lognormal model. A baseline, not a forecast."""
    if price <= 0 or strike <= 0 or minutes <= 0 or sigma_per_min <= 0:
        return None
    s = sigma_per_min * math.sqrt(minutes)
    d = (math.log(price / strike) + drift_per_min * minutes) / s
    return 0.5 * (1.0 + math.erf(d / math.sqrt(2.0)))
