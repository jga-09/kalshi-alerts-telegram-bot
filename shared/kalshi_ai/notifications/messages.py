"""User-facing message formatting (Telegram HTML). Never includes secrets; never claims certainty."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from html import escape
from typing import Protocol

from kalshi_ai.domain.disclosures import SHORT_RISK_NOTICE

ESTIMATE_NOTE = "<i>Probabilities are model estimates, not guarantees.</i>"


def pct(x: Decimal | float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.{digits}f}%"


def cents(x: Decimal | float | None) -> str:
    return "n/a" if x is None else f"{float(x) * 100:.0f}¢"


def usd(x: Decimal | float | None) -> str:
    return "n/a" if x is None else f"${float(x):,.2f}"


@dataclass(frozen=True)
class TradeNotification:
    mode: str  # paper | live
    status: str
    market_ticker: str
    market_title: str | None
    side: str
    entry_price: Decimal
    quantity: int
    filled_quantity: Decimal
    model_probability: Decimal | None
    market_probability: Decimal | None
    edge: Decimal | None
    risk_mode: str | None
    reason: str | None = None

    def render(self) -> str:
        title = "TRADE EXECUTED" if self.status in ("filled", "partially_filled") else f"ORDER {self.status.upper()}"
        if self.mode == "paper":
            title = "📝 PAPER " + title
        else:
            title = "⚡ LIVE " + title
        exposure = self.entry_price * self.filled_quantity
        lines = [
            f"<b>{title}</b>",
            "",
            f"<b>Market:</b> {escape(self.market_title or self.market_ticker)}",
            f"<b>Ticker:</b> <code>{escape(self.market_ticker)}</code>",
            f"<b>Direction:</b> {escape(self.side.upper())}",
            f"<b>Entry:</b> {cents(self.entry_price)}",
            f"<b>Quantity:</b> {self.filled_quantity:g} of {self.quantity}",
            f"<b>Total exposure:</b> {usd(exposure)}",
            f"<b>Model probability (estimate):</b> {pct(self.model_probability)}",
            f"<b>Market probability:</b> {pct(self.market_probability, 0)}",
            f"<b>Estimated edge:</b> {pct(self.edge)}",
            f"<b>Risk:</b> {escape((self.risk_mode or 'n/a').upper())}",
            f"<b>Status:</b> {escape(self.status.upper())}",
        ]
        if self.reason and self.status not in ("filled",):
            lines.append(f"<b>Note:</b> {escape(self.reason[:300])}")
        lines += ["", ESTIMATE_NOTE]
        return "\n".join(lines)


def render_signal(
    *,
    market: str,
    ticker: str,
    prob_yes: float,
    side: str,
    model_prob_side: Decimal,
    market_prob_side: Decimal,
    edge: Decimal,
    confidence: str,
    action: str,
    supporting: list[str],
    conflicting: list[str],
    data_age_seconds: float | None,
) -> str:
    action_text = {
        "trade_if_risk_passes": "TRADE ONLY IF RISK RULES PASS",
        "watch": "WATCH - edge below threshold",
        "no_trade": "NO TRADE - no positive edge",
    }.get(action, action.upper())
    freshness = "n/a" if data_age_seconds is None else f"{data_age_seconds:.0f}s old"
    lines = [
        f"<b>MARKET:</b> {escape(market)}",
        f"<code>{escape(ticker)}</code>",
        "",
        "<b>Model probability (estimate):</b>",
        f"YES {pct(prob_yes)}  |  NO {pct(1 - prob_yes)}",
        f"<b>Kalshi price ({escape(side.upper())}):</b> {cents(market_prob_side)}",
        f"<b>Estimated edge:</b> {float(edge) * 100:+.1f} percentage points",
        f"<b>Confidence:</b> {escape(confidence.upper())}",
        f"<b>Data freshness:</b> {freshness}",
        f"<b>Action:</b> {escape(action_text)}",
    ]
    if supporting:
        lines += ["", "<b>Supporting signals:</b>"] + [f"• {escape(s)}" for s in supporting[:5]]
    if conflicting:
        lines += ["", "<b>Conflicting signals:</b>"] + [f"• {escape(s)}" for s in conflicting[:5]]
    lines += ["", ESTIMATE_NOTE]
    return "\n".join(lines)


def risk_footer() -> str:
    return f"\n\n<i>{escape(SHORT_RISK_NOTICE)}</i>"


class Notifier(Protocol):
    async def send(self, chat_id: int, text: str) -> bool: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, chat_id: int, text: str) -> bool:
        self.sent.append((chat_id, text))
        return True
