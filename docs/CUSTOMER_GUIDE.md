# Customer guide

Welcome to Kalshi AI. This guide explains, in plain language, how to use the bot safely.

> **Important:** Kalshi AI's probabilities are *estimates*. They can be wrong. Trading event contracts involves
> risk and you can lose the money you put in. Automated trading can lose money too. Past results — including
> paper trading — do not guarantee future results.

## 1. Activate your subscription

Open the bot and send `/start`. If you are not subscribed you will see **BUY SUBSCRIPTION** and
**ENTER ACCESS CODE**.

* **Buy**: choose a plan and pay on Stripe's secure page. Your plan activates automatically once Stripe confirms.
* **Access code**: tap ENTER ACCESS CODE and send the code. The message is deleted for your privacy.

| Plan | Includes |
|---|---|
| SIGNALS | AI market analysis and market list |
| PRO | + Kalshi connection, balance/positions, paper trading, performance |
| AUTO | + automated trading (paper first, live only after you confirm) |
| PREMIUM | + advanced qualitative analysis and priority signals |

## 2. Connect Kalshi (optional, PRO and above)

Send `/connect`. You get a private link (valid 15 minutes). On kalshi.com create an API key with **only**
"read" and "write::trade" permissions — never transfer/withdraw — and enter it on that page.

* Kalshi AI never asks for your Kalshi password.
* **Never paste keys into Telegram.** The bot deletes messages that look like keys.
* You stay in control: send `/disconnect` to delete the stored credentials, and revoke the key on kalshi.com.

## 3. Choose a risk level

`/risk` → LOW, PASSIVE or RISKY. LOW uses the smallest sizes and strictest filters. **No level is safe.**

## 4. Paper trade first

`/paper` starts simulated trading with virtual money against real market prices. Check results with `/paper`
and `/positions`. Look at profit/loss and calibration, not just how often trades win.

## 5. Read a signal

`/signal` (or the Signals button) shows, for a market:

* **Model probability (estimate)** for YES and NO
* **Kalshi price** — what you would pay
* **Estimated edge** = estimate − price, in percentage points
* **Confidence**, **data freshness**, **supporting** and **conflicting** signals
* **Action** — usually "TRADE ONLY IF RISK RULES PASS", "WATCH" or "NO TRADE"

## 6. Live trading (optional, AUTO and above)

`/autotrade` → accept the risk disclosure → enable auto trading (starts in paper) → *Review LIVE trading*.
You will see your risk level, maximum trade, maximum daily loss and maximum trades per day.
Tap **CONFIRM & ENABLE** only if you understand and accept them. Changing your risk level later turns live
trading off until you confirm again. Every order still has to pass strict safety checks.

## 7. Emergency stop

Send `/stop` or tap **EMERGENCY STOP** at any time. New automated orders are blocked immediately and open orders
placed by Kalshi AI are cancelled where possible. `/resume` clears the stop; live trading stays off until you
turn it on again.

## Useful commands

`/help` · `/profile` · `/subscription` · `/balance` · `/positions` · `/orders` · `/settings` · `/status`
