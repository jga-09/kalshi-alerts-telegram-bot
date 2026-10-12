# Strategies: Hammer, Drift, Fade, Surge

Each strategy is a different rule for turning the model's probability estimate into a trade. They fire
independently and each suits different market conditions. **No strategy wins every time.** An edge
only shows up over many trades, and none of these rules is a guarantee.

Code: `shared/kalshi_ai/strategies/` (rules in `engine.py`, user settings in `config.py`).

| Strategy | Idea | How it enters | What to expect |
|---|---|---|---|
| 🔨 **Hammer** | Trend-following. The model sees edge at the ask **and** recent movement (underlying momentum, EMA spread, trend regime, order-book lean, recent price change) points the same way. | Buys at the ask (immediate-or-cancel). | Fills reliably. Works best in trending markets. |
| 🌀 **Drift** | Value. Rests a limit order one tick inside the spread when the model says the side is worth at least the edge minimum more than that price **and** at least the ask. | Post-only limit order that expires after ~5 minutes (or 2 minutes before close). | Highest hit-rate when filled. Many orders won't fill on busy or thin markets, which is the strategy being disciplined, not a malfunction. |
| 🎯 **Fade** | Contrarian. Buys a cheap side (≤ 35¢) right after the crowd pushed it down: its price fell ≥ 8¢ in the recent trade window, the underlying is ≥ 2 sd stretched against it, or RSI is beyond 75/25 against it. Needs model edge too. | Buys at the ask. | Wins less often, but each win pays more. Suits choppy, whipsawing markets. |
| ⚡ **Surge** | Only Drift's strongest setups: edge at the **ask** ≥ 1.5× the minimum, confidence ≥ 0.70, liquid book. Sits out **weekdays 12:00–18:00 America/New_York**. | Buys at the live ask. Your max-price cap still applies. | About 1 trade in 10 of Drift's. Quiet, high conviction. |

Why Drift requires value even at the ask: resting buy orders get filled most often when the price is
moving against you (adverse selection). Requiring the model to like the side even at the ask filters
those out.

**Conflicts.** If strategies disagree on the side of one market (e.g. Hammer says YES, Fade says NO),
none of them trade it that minute.

**Surge vs Drift.** When both are on and a setup qualifies for Surge, it is traded once, as Surge. If
you only run Drift, the same setup is traded as a Drift limit order.

## Presets and settings (Telegram: `/strategies`)

| Risk level | Preset strategies |
|---|---|
| LOW | Drift + Surge |
| PASSIVE | Hammer + Drift + Surge |
| RISKY | Hammer + Drift + Fade + Surge |

Until you change anything, your strategies follow your risk level. After you edit them they are
"custom" until you tap **Use preset for my risk level**.

* **On/off** per strategy.
* **Size (0.5x / 1x / 1.5x / 2x)**, relative between strategies. The largest enabled size trades at
  100% of what your risk level allows; the others scale down proportionally (e.g. Surge 2x and
  Hammer 1x means Surge gets full size and Hammer half). Sizing can only *shrink* the risk engine's
  size, never loosen a limit.
* **Max price (50–95¢)**. No strategy buys contracts above this price.

The same settings are available at `GET/PUT /api/me/strategies`.

## Safety

Strategies only produce signals. Every one passes through the same `ExecutionPipeline` →
`RiskEngine` → final validation as before: kill switches, daily loss, exposure, spread, depth,
minimum edge and confidence all still apply, per user. Paper trading remains the default. Live
Drift orders are sent `post_only` with an exchange-side `expiration_time`, and the reconciler also
cancels any live resting order that is still open 30 seconds after it expires.

Paper resting orders fill only when the opposite side is visibly offered at or below the limit. They
fill at the limit price with an estimated maker fee and never assume queue priority. The worker checks
them every 30 seconds.

## Finding out which strategies make money

1. Run in paper mode for a few weeks. Results are tracked per strategy.
2. Look at `/strategies` in Telegram, or **Admin → Performance → by strategy**: trades, win rate,
   **average P&L per trade**, total P&L and fill rate.
3. Backtest with `scripts/backtest.py --strategies hammer,drift,fade,surge`, which reports the same
   metrics per strategy.
4. Turn off the strategies whose average P&L per trade is negative after fees. Win rate alone misleads:
   Fade can be profitable with a lower win rate, and a high-win-rate strategy that buys at 90¢ can
   lose money.

The thresholds above (trend 0.25, Fade ≤ 35¢ / 8¢ move / 2 sd, Surge 1.5× edge / 0.70 confidence,
the 12–6pm blackout) are starting points that have **not yet been validated on Kalshi data**. Tune
them in `StrategyParams` only after your own paper results or backtests show a reason to.
