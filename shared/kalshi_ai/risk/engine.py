"""Deterministic RiskEngine.

The AI/model only produces a Signal. This engine - pure, deterministic, with no
LLM involvement - decides whether a trade is permitted and how large it may be.
Every check is recorded; ANY failure means DO NOT TRADE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any

from kalshi_ai.domain.enums import RiskMode, Side
from kalshi_ai.risk.profiles import RiskLimits

ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class SignalView:
    market_ticker: str
    correlation_group: str
    side: Side
    estimated_probability: Decimal  # model P(side wins)
    market_probability: Decimal  # implied by entry price
    entry_price: Decimal  # dollars per contract of `side` (best ask)
    edge: Decimal
    confidence: Decimal
    expires_at: datetime
    data_age_seconds: float


@dataclass(frozen=True)
class AccountState:
    available_balance: Decimal
    realized_pnl_today: Decimal  # negative = loss
    unrealized_pnl_today: Decimal
    trades_today: int
    position_contracts_in_market: Decimal
    exposure_in_market: Decimal
    exposure_in_group: Decimal


@dataclass(frozen=True)
class LiquidityView:
    spread: Decimal | None
    depth_at_limit: Decimal  # contracts available at or better than entry price
    top_of_book_depth: Decimal


@dataclass
class RiskDecision:
    approved: bool
    quantity: int
    checks: list[CheckResult] = field(default_factory=list)
    risk_mode: RiskMode | None = None

    @property
    def failed(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    @property
    def reason(self) -> str:
        if self.approved:
            return "all risk checks passed"
        return "; ".join(f"{c.name}: {c.detail}" for c in self.failed) or "rejected"

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "quantity": self.quantity,
            "risk_mode": self.risk_mode.value if self.risk_mode else None,
            "checks": [c.as_dict() for c in self.checks],
        }


def kelly_fraction_for_binary(prob: Decimal, price: Decimal) -> Decimal:
    """Full-Kelly bankroll fraction for buying a $1 binary at `price` with win prob `prob`."""
    if price <= ZERO or price >= ONE:
        return ZERO
    f = (prob - price) / (ONE - price)
    return max(ZERO, f)


class RiskEngine:
    def __init__(
        self, limits: RiskLimits, mode: RiskMode, *, min_price: Decimal, max_price: Decimal, max_data_age_seconds: int
    ):
        self.limits = limits
        self.mode = mode
        self.min_price = min_price
        self.max_price = max_price
        self.max_data_age_seconds = max_data_age_seconds

    def evaluate(
        self, signal: SignalView, account: AccountState, liquidity: LiquidityView, now: datetime
    ) -> RiskDecision:
        lim = self.limits
        checks: list[CheckResult] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append(CheckResult(name, bool(ok), "" if ok else detail))

        check("signal_not_expired", now < signal.expires_at, "signal expired")
        check(
            "data_fresh",
            signal.data_age_seconds <= self.max_data_age_seconds,
            f"data age {signal.data_age_seconds:.0f}s > {self.max_data_age_seconds}s",
        )
        check(
            "price_in_bounds",
            self.min_price <= signal.entry_price <= self.max_price,
            f"price {signal.entry_price} outside [{self.min_price}, {self.max_price}]",
        )
        check("probability_valid", ZERO < signal.estimated_probability < ONE, "invalid probability")
        recomputed_edge = signal.estimated_probability - signal.entry_price
        check(
            "edge_consistent",
            abs(recomputed_edge - signal.edge) <= Decimal("0.005"),
            "edge does not match probability - price",
        )
        check("min_edge", recomputed_edge >= lim.min_edge, f"edge {recomputed_edge:.4f} < {lim.min_edge}")
        check(
            "min_confidence",
            signal.confidence >= lim.min_confidence,
            f"confidence {signal.confidence} < {lim.min_confidence}",
        )
        check(
            "spread",
            liquidity.spread is not None and liquidity.spread <= lim.max_spread,
            f"spread {liquidity.spread} > {lim.max_spread}",
        )
        check(
            "top_of_book_depth",
            liquidity.top_of_book_depth >= lim.min_top_of_book_depth,
            f"depth {liquidity.top_of_book_depth} < {lim.min_top_of_book_depth}",
        )
        daily_pnl = account.realized_pnl_today + min(ZERO, account.unrealized_pnl_today)
        check(
            "daily_loss_limit",
            -daily_pnl < lim.max_daily_loss,
            f"daily loss {-daily_pnl} reached limit {lim.max_daily_loss}",
        )
        check(
            "max_trades_per_day",
            account.trades_today < lim.max_trades_per_day,
            f"{account.trades_today} trades today >= {lim.max_trades_per_day}",
        )
        check(
            "position_limit",
            account.position_contracts_in_market < lim.max_position_contracts,
            f"position {account.position_contracts_in_market} >= {lim.max_position_contracts}",
        )
        check(
            "market_exposure",
            account.exposure_in_market < lim.max_market_exposure,
            f"market exposure {account.exposure_in_market} >= {lim.max_market_exposure}",
        )
        check(
            "correlated_exposure",
            account.exposure_in_group < lim.max_correlated_exposure,
            f"correlated exposure {account.exposure_in_group} >= {lim.max_correlated_exposure}",
        )
        check("balance_positive", account.available_balance > ZERO, "no available balance")

        quantity = 0
        if all(c.passed for c in checks):
            quantity = self.size(signal, account, liquidity)
            check("order_size", quantity >= 1, "computed size < 1 contract after all caps")
        approved = all(c.passed for c in checks)
        return RiskDecision(approved=approved, quantity=quantity if approved else 0, checks=checks, risk_mode=self.mode)

    def size(self, signal: SignalView, account: AccountState, liquidity: LiquidityView) -> int:
        lim = self.limits
        price = signal.entry_price

        def contracts(dollars: Decimal) -> int:
            if dollars <= ZERO:
                return 0
            return int((dollars / price).to_integral_value(rounding=ROUND_DOWN))

        remaining_loss_budget = lim.max_daily_loss + min(
            ZERO, account.realized_pnl_today + min(ZERO, account.unrealized_pnl_today)
        )
        kelly_dollars = (
            kelly_fraction_for_binary(signal.estimated_probability, price)
            * lim.kelly_fraction
            * (account.available_balance)
        )
        caps = [
            int(lim.max_position_contracts - account.position_contracts_in_market),
            contracts(lim.max_order_notional),
            contracts(account.available_balance * Decimal("0.98")),  # leave room for fees
            contracts(lim.max_market_exposure - account.exposure_in_market),
            contracts(lim.max_correlated_exposure - account.exposure_in_group),
            contracts(remaining_loss_budget),  # worst case of a binary buy is the full premium
            contracts(kelly_dollars),
            int((liquidity.depth_at_limit * Decimal("0.5")).to_integral_value(rounding=ROUND_DOWN)),
        ]
        return max(0, min(caps))
