from __future__ import annotations

import math

import numpy as np
import pytest

from kalshi_ai.ta import indicators as ind
from kalshi_ai.ta.engine import compute_features, probability_above_strike
from tests.factories import candles

PRICES = np.array(
    [
        44.34,
        44.09,
        44.15,
        43.61,
        44.33,
        44.83,
        45.10,
        45.42,
        45.84,
        46.08,
        45.89,
        46.03,
        45.61,
        46.28,
        46.28,
        46.00,
        46.03,
        46.41,
        46.22,
        45.64,
        46.21,
        46.25,
        45.71,
        46.45,
        45.78,
        45.35,
        44.03,
        44.18,
        44.22,
        44.57,
        43.42,
        42.66,
        43.13,
    ]
)


def test_sma_ema_basic() -> None:
    assert np.allclose(ind.sma([1, 2, 3, 4, 5], 3)[2:], [2, 3, 4])
    assert math.isnan(ind.sma([1, 2], 3)[0])
    e = ind.ema([1, 2, 3, 4, 5, 6], 3)
    assert e[2] == pytest.approx(2.0) and e[5] == pytest.approx(5.0)


def test_rsi_matches_wilder_reference() -> None:
    r = ind.rsi(PRICES, 14)
    # Classic Wilder example: RSI(14) on bar 15 ~ 70.53
    assert r[14] == pytest.approx(70.53, abs=0.1)
    assert np.nanmin(r) >= 0 and np.nanmax(r) <= 100


@pytest.mark.parametrize(
    "fn",
    [
        lambda c: ind.rsi(c, 14),
        lambda c: ind.ema(c, 10),
        lambda c: ind.macd(c)[2],
        lambda c: ind.bollinger(c, 20)[3],
        lambda c: ind.momentum(c, 5),
        lambda c: ind.realized_volatility(c, 10),
        lambda c: ind.zscore(c, 10),
    ],
)
def test_no_look_ahead(fn) -> None:
    """Value at i computed on the full series equals value computed on series[:i+1]."""
    full = fn(PRICES)
    for i in range(20, len(PRICES)):
        prefix = fn(PRICES[: i + 1])
        a, b = full[i], prefix[-1]
        assert (math.isnan(a) and math.isnan(b)) or a == pytest.approx(b)


def test_atr_bollinger_vwap_breakout() -> None:
    cs = candles(80)
    h = [c.high for c in cs]
    lo = [c.low for c in cs]
    cl = [c.close for c in cs]
    v = [c.volume for c in cs]
    assert np.all(ind.atr(h, lo, cl, 14)[13:] > 0)
    mid, up, low, _pct_b = ind.bollinger(cl, 20)
    assert np.all(up[19:] >= mid[19:]) and np.all(low[19:] <= mid[19:])
    vw = ind.vwap(h, lo, cl, v)
    assert min(lo) <= vw[-1] <= max(h)
    assert ind.breakout([1, 1, 1, 1, 5], [1, 1, 1, 1, 5], [1, 1, 1, 1, 1], lookback=3) == 1
    strong = [c.close for c in candles(80, drift=40)]
    assert ind.trend_regime(strong) == 1 and ind.trend_regime(strong[::-1]) == -1


def test_resample_ohlcv() -> None:
    ts = np.arange(0, 600, 60)
    vals = {
        "open": np.arange(10),
        "high": np.arange(10) + 1,
        "low": np.arange(10) - 1,
        "close": np.arange(10) + 0.5,
        "volume": np.ones(10),
    }
    r = ind.resample(ts, vals, 300)
    assert list(r["open"]) == [0, 5] and list(r["close"]) == [4.5, 9.5] and list(r["volume"]) == [5, 5]
    assert list(r["high"]) == [5, 10] and list(r["low"]) == [-1, 4]


def test_compute_features_and_insufficient_data() -> None:
    assert compute_features(candles(10))["ta_available"] == 0
    f = compute_features(candles(120))
    assert f["ta_available"] == 1 and 0 <= f["rsi"] <= 100 and f["atr"] > 0
    assert all(not (isinstance(v, float) and math.isnan(v)) for v in f.values())


def test_probability_above_strike() -> None:
    assert probability_above_strike(100, 100, 0.001, 10) == pytest.approx(0.5)
    assert probability_above_strike(101, 100, 0.001, 10) > 0.9
    assert probability_above_strike(100, 100, 0, 10) is None
