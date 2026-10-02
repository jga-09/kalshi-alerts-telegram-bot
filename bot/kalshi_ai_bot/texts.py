"""User-facing copy. Plain language for beginners; never promises profits."""

from __future__ import annotations

from kalshi_ai.domain.disclosures import SHORT_RISK_NOTICE

WELCOME = (
    "<b>Welcome to Kalshi AI.</b>\n\n"
    "1. Activate subscription\n"
    "2. Connect Kalshi\n"
    "3. Choose risk level\n"
    "4. Start paper trading\n"
    "5. Review performance\n"
    "6. Enable live trading if desired\n\n"
    f"<i>{SHORT_RISK_NOTICE}</i>"
)

INACTIVE = "Your Kalshi AI subscription is inactive."
ACTIVATED = "✅ Subscription activated."
ENTER_CODE = "🔑 Send your access code (e.g. <code>KAI-XXXX-XXXX-XXXX-XXXX</code>)."
CODE_INVALID = "❌ That code is invalid, expired, or no longer available."
CODE_ALREADY = "You have already used this code."
CODE_RATE_LIMITED = "⏳ Too many attempts. Please try again in {minutes} minutes."
CODE_UNAVAILABLE = "⚠️ Code activation is temporarily unavailable. Please try again shortly."

HELP = (
    "<b>Kalshi AI commands</b>\n"
    "/start - main menu\n"
    "/subscribe - plans &amp; billing\n"
    "/subscription - subscription status\n"
    "/connect - connect your Kalshi account (secure web form)\n"
    "/account /profile - your account\n"
    "/balance /positions /orders - portfolio\n"
    "/markets - supported markets\n"
    "/signal - AI analysis for a market\n"
    "/paper - paper trading\n"
    "/autotrade - automated trading\n"
    "/risk - risk level\n"
    "/settings - your settings\n"
    "/status - system status\n"
    "/stop - EMERGENCY STOP (blocks all automated orders)\n"
    "/resume - clear emergency stop\n\n"
    f"<i>{SHORT_RISK_NOTICE}</i>"
)

CONNECT = (
    "<b>Connect your Kalshi account</b>\n\n"
    "Kalshi AI never asks for your Kalshi password, and you must <b>never</b> paste API keys or private "
    "keys into Telegram.\n\n"
    "1. On kalshi.com open Account → API Keys and create a key with only the <b>read</b> and "
    "<b>write::trade</b> scopes (never transfer/withdraw).\n"
    "2. Open the secure link below (valid 15 minutes, single use) and submit the key ID and private key.\n\n"
    "{url}\n\n"
    "You can revoke the key on kalshi.com at any time."
)

SECRET_WARNING = (
    "⚠️ That message looked like a private key or API secret, so it was deleted.\n"
    "Never send credentials in Telegram. Use /connect to get a secure link."
)

LIVE_REVIEW = (
    "<b>LIVE TRADING</b>\n\n"
    "You are about to allow Kalshi AI to place orders on your connected Kalshi account.\n\n"
    "<b>Review:</b>\n"
    "Risk level: <b>{risk_mode}</b>\n"
    "Maximum trade: <b>${max_trade}</b>\n"
    "Maximum daily loss: <b>${max_daily_loss}</b>\n"
    "Maximum trades/day: <b>{max_trades}</b>\n"
    "Minimum estimated edge: <b>{min_edge}</b>\n\n"
    "• AI probabilities are estimates and can be wrong.\n"
    "• Automated trading can result in losses, including on LOW risk.\n"
    "• You can stop at any time with /stop or the EMERGENCY STOP button.\n"
)

STOPPED = (
    "🛑 <b>EMERGENCY STOP ACTIVATED</b>\n\n"
    "• New automated orders are blocked immediately.\n"
    "• Open orders placed by Kalshi AI are being cancelled where supported.\n"
    "• Auto and live trading have been switched off.\n\n"
    "Use /resume to clear the stop. Live trading will need to be re-confirmed."
)
