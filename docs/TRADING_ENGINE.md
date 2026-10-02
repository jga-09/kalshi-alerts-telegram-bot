# Trading engine

**The AI is never the final authority.** Models produce estimates; a deterministic risk engine and execution
validator decide, and any failed check means *do not trade*.

## 1. Data → features (`shared/kalshi_ai/features/engine.py`)

* `MarketContext` bundles everything known about one market at time `as_of`.
* `point_in_time()` is the **single** look-ahead filter: trades, candles (completed only), data points, news and
  social posts must be timestamped ≤ `as_of`; an order book fetched after `as_of` is dropped.
* Features: market mid/asks, minutes to close, order-book imbalance/flow/microprice, TA (RSI, MACD, EMA/SMA,
  VWAP, Bollinger, ATR, momentum, acceleration, volatility, z-score, S/R distances, breakout, trend regime),
  volatility-baseline probability (lognormal price-vs-strike), deduplicated recency-weighted news score,
  quality-weighted capped social signal, crypto (funding, OI) and gold (sessions, yields, real yields, USD index,
  GC front contract & roll window, event proximity) features, data completeness, data age, degraded-source count.
* Every prediction stores a `features` row: feature set version, all values, data versions/source health and a
  SHA-256 hash — reproducible for backtests and audits.

## 2. Models (`shared/kalshi_ai/modeling`)

| Model | Notes |
|---|---|
| `RuleBasedModel` | Anchors on the volatility baseline (or market price), applies bounded tilts (≤ ±0.6 logit), then **shrinks toward market price** in proportion to missing/stale data |
| `LogisticModel` | Coefficients stored as JSON in `model_versions.artifact`; trained on resolved snapshots with a time-ordered split |
| `GradientBoostingModel` | sklearn HistGB; artifact file pinned by SHA-256 (refuses to load on mismatch) |
| `LLMQualitativeAnalyzer` (optional) | Claude via structured JSON output; no tools; contributes at most ±3pp; failures/refusals ignored |

`ModelRegistry` loads ACTIVE (weighted) and SHADOW (logged only) models. `PredictionEngine` combines active
models in logit space weighted by confidence; disagreement lowers confidence. Probabilities are clamped to
[0.02, 0.98] — no model may claim certainty. `ModelEvaluator` promotes a candidate only if it beats both the
incumbent **and the market's own implied probability** (Brier score) on a held-out later period.

## 3. Edge and signals (`shared/kalshi_ai/signals/engine.py`)

```
market_probability(side) = best ask of that side (what we would pay)
edge(side) = estimated P(side) − market_probability(side)
```

Both YES and NO are evaluated; the better side is chosen. Actions:

* `TRADE_IF_RISK_PASSES` — edge ≥ max(`MIN_EDGE`, `HARD_MIN_EDGE`), confidence ≥ `HARD_MIN_CONFIDENCE`, liquidity OK
* `WATCH` — positive but insufficient edge/confidence/liquidity
* `NO_TRADE` — no positive edge

Signals expire after 3 minutes (or 1 minute before close). Idempotency key = `ticker:model_version:minute`.

## 4. Risk engine (`shared/kalshi_ai/risk`)

Effective limits = strictest of (profile, user overrides, hard global caps, platform env caps).

| Parameter | LOW | PASSIVE | RISKY |
|---|---|---|---|
| Max contracts per market | 10 | 25 | 100 |
| Max order notional | $5 | $15 | $50 |
| Max daily loss | $10 | $30 | $100 |
| Max trades/day | 5 | 10 | 25 |
| Min confidence | 0.70 | 0.60 | 0.55 |
| Min edge | 8pp | 6pp | 4pp |
| Max market exposure | $10 | $30 | $100 |
| Max correlated (series) exposure | $20 | $60 | $200 |
| Max spread | 4¢ | 6¢ | 8¢ |
| Min top-of-book depth | 50 | 25 | 10 |
| Kelly fraction cap | 0.10 | 0.20 | 0.25 |

Override profiles with `RISK_PROFILES_JSON`. **No level is "safe".** Checks: signal not expired, data fresh,
price within [0.02, 0.98], probability valid, edge consistent with prices, min edge, min confidence, spread,
depth, daily loss (realized + unrealized losses), trades/day, position, market and correlated exposure, balance.
Size = min(position headroom, notional cap, 98% of balance, exposure headroom, remaining daily-loss budget,
fractional Kelly, 50% of visible depth at the limit). Size < 1 ⇒ rejected.

## 5. Execution pipeline (`shared/kalshi_ai/trading/pipeline.py`)

```
Signal → Eligibility → Idempotency → Market check → Risk engine → Order construction
       → Final validation → persist PENDING → Paper fill | Kalshi API → Confirmation → DB → Telegram
```

* **Eligibility**: active user; no global/user kill switch; no admin block; plan entitlement; paper enabled, or
  for live: platform `LIVE_TRADING`, no monitor suspension, auto+live enabled, risk confirmation whose fingerprint
  equals the *current* limits, verified trade-scoped Kalshi connection.
* **Market check (fresh fetch)**: market open, > 1 min to close, ask available, ask ≤ signal price + 2¢, book age
  ≤ `MAX_DATA_AGE_SECONDS`.
* **Final validation**: re-reads kill switches **from the database**, quantity integer within hard cap, price
  bounds, notional ≤ hard cap, live still enabled.
* **Idempotency**: unique `(user_id, signal_id, mode)`; deterministic `client_order_id`; concurrent duplicates
  blocked by the DB constraint (tested with real concurrency on PostgreSQL).
* **Live**: PENDING row committed before sending; IOC limit orders; timeouts ⇒ `UNKNOWN` ⇒ reconciliation by
  `client_order_id` (never resubmitted); exchange rejections recorded; positions updated from fills.
* **Paper**: fills walk the *real* current book at ≤ limit, estimated fees, paper cash; settlement at 0/1.
* Notifications are best-effort: a Telegram outage never changes trade state.

## 6. Emergency stops

* **User** (`/stop`, EMERGENCY STOP button, `POST /api/me/emergency-stop`): sets the DB flag immediately, disables
  auto + live, enqueues cancellation of Kalshi-AI-placed open orders (inline fallback if the queue is down).
  `/resume` clears the flag; live trading must be re-confirmed.
* **Global** (admins only: role + configured Telegram ID): blocks every account; cancels open orders.
* **Monitor suspension**: critical monitoring alerts (model worse than market, data outage, live error spike)
  automatically suspend live trading platform-wide until an admin clears it.

## 7. Monitoring (`shared/kalshi_ai/monitoring/drift.py`)

Every 15 min: calibration vs 30-day baseline, model vs market Brier, share of degraded sources, source latency,
live order error rate, paper EV shift. Alerts are stored as `system_events`.
