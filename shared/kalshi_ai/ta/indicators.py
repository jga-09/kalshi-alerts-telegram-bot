"""Technical indicators (numpy). Every output at index i uses only inputs[0..i] - no look-ahead.

Arrays are aligned with the input; warm-up positions are NaN.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

Array = NDArray[np.float64]


def _arr(x: object) -> Array:
    return np.asarray(x, dtype=np.float64)


def sma(values: object, period: int) -> Array:
    v = _arr(values)
    out = np.full_like(v, np.nan)
    if period <= 0 or len(v) < period:
        return out
    csum = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def ema(values: object, period: int) -> Array:
    v = _arr(values)
    out = np.full_like(v, np.nan)
    if period <= 0 or len(v) < period:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = v[:period].mean()  # seed with SMA
    for i in range(period, len(v)):
        out[i] = alpha * v[i] + (1 - alpha) * out[i - 1]
    return out


def rsi(close: object, period: int = 14) -> Array:
    c = _arr(close)
    out = np.full_like(c, np.nan)
    if len(c) <= period:
        return out
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = gain[:period].mean()
    avg_loss = loss[:period].mean()

    def value(g: float, lo: float) -> float:
        if lo == 0:
            return 100.0 if g > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + g / lo)

    out[period] = value(avg_gain, avg_loss)
    for i in range(period + 1, len(c)):  # Wilder smoothing
        avg_gain = (avg_gain * (period - 1) + gain[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + loss[i - 1]) / period
        out[i] = value(avg_gain, avg_loss)
    return out


def macd(close: object, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Array, Array, Array]:
    c = _arr(close)
    line = ema(c, fast) - ema(c, slow)
    sig = np.full_like(c, np.nan)
    valid = ~np.isnan(line)
    if valid.sum() >= signal:
        idx = np.where(valid)[0]
        sig_vals = ema(line[idx], signal)
        sig[idx] = sig_vals
    return line, sig, line - sig


def true_range(high: object, low: object, close: object) -> Array:
    h, lo, c = _arr(high), _arr(low), _arr(close)
    prev = np.concatenate(([np.nan], c[:-1]))
    tr = np.nanmax(np.vstack([h - lo, np.abs(h - prev), np.abs(lo - prev)]), axis=0)
    tr[0] = h[0] - lo[0]
    return tr


def atr(high: object, low: object, close: object, period: int = 14) -> Array:
    tr = true_range(high, low, close)
    out = np.full_like(tr, np.nan)
    if len(tr) < period:
        return out
    out[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def bollinger(close: object, period: int = 20, num_std: float = 2.0) -> tuple[Array, Array, Array, Array]:
    """Returns (middle, upper, lower, %b)."""
    c = _arr(close)
    mid = sma(c, period)
    std = np.full_like(c, np.nan)
    for i in range(period - 1, len(c)):
        std[i] = c[i - period + 1 : i + 1].std(ddof=0)
    upper, lower = mid + num_std * std, mid - num_std * std
    width = upper - lower
    pct_b = np.where(width > 0, (c - lower) / np.where(width == 0, 1, width), 0.5)
    pct_b[np.isnan(mid)] = np.nan
    return mid, upper, lower, pct_b


def vwap(high: object, low: object, close: object, volume: object, window: int | None = None) -> Array:
    typical = (_arr(high) + _arr(low) + _arr(close)) / 3.0
    vol = _arr(volume)
    pv = typical * vol
    if window is None:
        cum_v = np.cumsum(vol)
        return np.where(cum_v > 0, np.cumsum(pv) / np.where(cum_v == 0, 1, cum_v), np.nan)
    return np.where(sma(vol, window) > 0, sma(pv, window) / sma(vol, window), np.nan)


def momentum(close: object, period: int = 10) -> Array:
    c = _arr(close)
    out = np.full_like(c, np.nan)
    if len(c) > period:
        out[period:] = c[period:] / c[:-period] - 1.0
    return out


def log_returns(close: object) -> Array:
    c = _arr(close)
    out = np.full_like(c, np.nan)
    out[1:] = np.log(c[1:] / c[:-1])
    return out


def realized_volatility(close: object, period: int = 20) -> Array:
    r = log_returns(close)
    out = np.full_like(r, np.nan)
    for i in range(period, len(r)):
        out[i] = np.nanstd(r[i - period + 1 : i + 1], ddof=1)
    return out


def volume_momentum(volume: object, short: int = 5, long: int = 20) -> Array:
    v = _arr(volume)
    long_avg = sma(v, long)
    return np.where(long_avg > 0, sma(v, short) / np.where(long_avg == 0, 1, long_avg) - 1.0, np.nan)


def acceleration(close: object, period: int = 5) -> Array:
    """Change in momentum: second difference of price over `period` bars, normalized by price."""
    mom = momentum(close, period)
    out = np.full_like(mom, np.nan)
    out[period:] = mom[period:] - mom[:-period]
    return out


def support_resistance(high: object, low: object, lookback: int = 30) -> tuple[float, float]:
    h, lo = _arr(high), _arr(low)
    if len(h) == 0:
        return float("nan"), float("nan")
    return float(np.min(lo[-lookback:])), float(np.max(h[-lookback:]))


def breakout(close: object, high: object, low: object, lookback: int = 20) -> int:
    """+1 if the latest close breaks the prior `lookback` high, -1 for the low, else 0."""
    c, h, lo = _arr(close), _arr(high), _arr(low)
    if len(c) <= lookback:
        return 0
    prior_high, prior_low = h[-lookback - 1 : -1].max(), lo[-lookback - 1 : -1].min()
    if c[-1] > prior_high:
        return 1
    if c[-1] < prior_low:
        return -1
    return 0


def zscore(close: object, period: int = 20) -> Array:
    c = _arr(close)
    mid = sma(c, period)
    out = np.full_like(c, np.nan)
    for i in range(period - 1, len(c)):
        s = c[i - period + 1 : i + 1].std(ddof=0)
        out[i] = (c[i] - mid[i]) / s if s > 0 else 0.0
    return out


def trend_regime(close: object, fast: int = 20, slow: int = 50, threshold: float = 0.001) -> int:
    """+1 uptrend, -1 downtrend, 0 range - based on EMA spread relative to price."""
    c = _arr(close)
    if len(c) < slow:
        return 0
    spread = (ema(c, fast)[-1] - ema(c, slow)[-1]) / c[-1]
    if spread > threshold:
        return 1
    if spread < -threshold:
        return -1
    return 0


def resample(ts_seconds: object, values: dict[str, object], bucket_seconds: int) -> dict[str, Array]:
    """OHLCV resampling for configurable timeframes. `values` needs open/high/low/close/volume."""
    t = np.asarray(ts_seconds, dtype=np.int64)
    buckets = t // bucket_seconds
    keys, idx = np.unique(buckets, return_index=True)
    o, h, lo, c, v = (_arr(values[k]) for k in ("open", "high", "low", "close", "volume"))
    ends = np.append(idx[1:], len(t))
    return {
        "ts": keys * bucket_seconds,
        "open": np.array([o[s] for s in idx]),
        "high": np.array([h[s:e].max() for s, e in zip(idx, ends, strict=True)]),
        "low": np.array([lo[s:e].min() for s, e in zip(idx, ends, strict=True)]),
        "close": np.array([c[e - 1] for e in ends]),
        "volume": np.array([v[s:e].sum() for s, e in zip(idx, ends, strict=True)]),
    }
