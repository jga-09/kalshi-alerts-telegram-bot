# Telegram bot

Built with **aiogram 3** (`bot/kalshi_ai_bot`). HTML parse mode, inline keyboards, FSM state in Redis.

## Commands

| Command | Purpose | Plan |
|---|---|---|
| `/start` | Onboarding + main menu (or BUY SUBSCRIPTION / ENTER ACCESS CODE when inactive) | any |
| `/help` | Command list | any |
| `/profile`, `/account` | Account summary | any |
| `/subscribe` | Plans → Stripe Checkout link | any |
| `/subscription` | Status, renewal, payment state | any |
| `/code` | Enter an access code | any |
| `/connect` | Single-use HTTPS link to connect Kalshi (no credentials in Telegram) | PRO+ |
| `/disconnect` | Delete stored Kalshi credentials, disable live trading | any |
| `/balance` | Paper cash + Kalshi available balance | PRO+ |
| `/positions`, `/orders` | Paper and live positions / recent orders | PRO+ |
| `/markets` | Open markets in supported series | SIGNALS+ |
| `/signal [ticker]` | Structured AI analysis (estimate, edge, confidence, evidence) | SIGNALS+ |
| `/paper` (`/paper off`) | Start paper trading, show paper P&L and calibration | PRO+ |
| `/risk` | Choose LOW / PASSIVE / RISKY | any |
| `/autotrade` | Auto trading (paper first) and the LIVE review entry point | AUTO+ |
| `/settings` | Effective limits | any |
| `/status` | Platform status and data-source health | any |
| `/stop` | **Emergency stop** | any |
| `/resume` | Clear emergency stop (live stays off until re-confirmed) | any |
| `/admin`, `/gencode`, `/killswitch` | Admin only (role + configured ID) | admin |

Main menu buttons: Signals · Markets · Balance · Positions · Auto Trade · Paper Trading · Risk Settings ·
Connect Kalshi · Subscription · EMERGENCY STOP.

## Key flows

**Access code** — ENTER ACCESS CODE → user sends the code → the message is deleted → redemption (per-user 5 tries /
15 min + global limit, fail-closed if Redis is down) → "Subscription activated."

**Live trading** — `/autotrade` → accept risk disclosure (once) → enable auto (paper) → *Review LIVE trading* →
review card (risk level, max trade, max daily loss, max trades/day, min edge) → **[CONFIRM & ENABLE] / [CANCEL]**.
The confirm button carries a prefix of the reviewed limit fingerprint; if limits changed since the review the
confirmation is refused.

**Credential guard** — any message that looks like a private key, API key ID or Stripe key (or a `.pem` file) is
deleted and the user is told to use `/connect`.

## Polling vs webhook

* Default: long polling (`python -m kalshi_ai_bot.main`). Run a single instance.
* Webhook: set `TELEGRAM_WEBHOOK_URL=https://<domain>/telegram/webhook` and `TELEGRAM_WEBHOOK_SECRET`; the bot
  listens on `:8081` and Caddy routes `/telegram/webhook` to it. Telegram's secret-token header is verified.

## Notifications

`TelegramNotifier` sends trade notifications and signal alerts, honours `retry_after` flood limits and treats
blocked users gracefully. Failures never affect trade state.
