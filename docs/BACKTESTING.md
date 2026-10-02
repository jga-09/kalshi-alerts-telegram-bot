# Backtesting

`shared/kalshi_ai/backtest/engine.py` replays history through the **same** FeatureEngine → PredictionEngine →
SignalEngine → RiskEngine → fill simulator used in production, so a backtest cannot quietly use different logic.

## Look-ahead protection

* Each step builds a `MarketContext` with `as_of` = the frame time; `FeatureEngine.point_in_time()` drops every
  trade, candle (only *completed* candles), data point, article and post timestamped after `as_of`.
* An order book newer than its step raises `LookAheadError`.
* Market results are read only at settlement, after all decisions are made.
* Tests (`tests/unit/test_monitoring_backtest.py`, `test_features_models_signals.py`) assert that injecting
  future candles/news/trades changes **nothing**.

## Data

The live system records what a backtest needs: `markets`, `market_snapshots`, `order_books`, `news`,
`social_posts`, `data_points`, `features`, `predictions`. Candles are cached only briefly in Redis, so the runner
can fetch historical 1-minute candles from Coinbase's public API.

```bash
python scripts/backtest.py --start 2026-09-01 --end 2026-09-30 --series KXBTC15M --risk passive \
  --candles-from-coinbase BTC-USD --out report.json
```

## Report

`simulated_pnl`, `trade_count`, `win_rate`, `average_trade_pnl` (= expected value per trade), `roi`,
`max_drawdown`, `brier_score`, `log_loss`, `expected_calibration_error`, per-bin calibration of *all* predictions
(not just trades), an equity curve, and counts of which risk checks blocked trades.

## Limitations (results are hypothetical)

* Fills use recorded book snapshots at the frame time; queue position, latency and book changes between
  snapshots are not modelled. Fees use an estimate of Kalshi's published formula.
* Snapshot cadence limits fidelity (default analysis cadence is 60 s).
* Settlement uses Kalshi's recorded result; underlying candles are a proxy for the official settlement index.
* Optimise for expected value and calibration — a high win rate on expensive favourites can still lose money.
