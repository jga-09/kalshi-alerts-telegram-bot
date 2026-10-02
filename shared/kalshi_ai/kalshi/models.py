"""Typed DTOs for Kalshi responses.

Kalshi now returns prices as fixed-point dollar strings (``*_dollars``, e.g. "0.5600")
and contract counts as fixed-point strings (``*_fp``, e.g. "10.00"). We parse them
into ``Decimal`` and never use floats for money.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict

from kalshi_ai.domain.enums import Side

ONE = Decimal("1")


def dec(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def cents_to_dollars(value: Any) -> Decimal | None:
    d = dec(value)
    return None if d is None else d / Decimal(100)


class KalshiMarket(BaseModel):
    model_config = ConfigDict(frozen=True)

    ticker: str
    event_ticker: str | None = None
    title: str | None = None
    yes_sub_title: str | None = None
    status: str
    open_time: datetime | None = None
    close_time: datetime | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    last_price: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    result: str | None = None
    raw: dict[str, Any] = {}

    @property
    def is_open(self) -> bool:
        return self.status in {"open", "active"}

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> KalshiMarket:
        def price(field: str) -> Decimal | None:
            # Prefer fixed-point dollars; fall back to legacy integer cents.
            return (
                dec(data.get(f"{field}_dollars"))
                if data.get(f"{field}_dollars") is not None
                else cents_to_dollars(data.get(field))
            )

        return cls(
            ticker=data["ticker"],
            event_ticker=data.get("event_ticker"),
            title=data.get("title"),
            yes_sub_title=data.get("yes_sub_title"),
            status=str(data.get("status", "unknown")),
            open_time=data.get("open_time"),
            close_time=data.get("close_time"),
            yes_bid=price("yes_bid"),
            yes_ask=price("yes_ask"),
            no_bid=price("no_bid"),
            no_ask=price("no_ask"),
            last_price=price("last_price"),
            volume=dec(data.get("volume_fp"), dec(data.get("volume"))),
            open_interest=dec(data.get("open_interest_fp"), dec(data.get("open_interest"))),
            result=data.get("result") or None,
            raw={k: data.get(k) for k in ("series_ticker", "market_type", "strike_type", "floor_strike", "cap_strike")},
        )


class OrderBookLevel(BaseModel):
    model_config = ConfigDict(frozen=True)
    price: Decimal
    size: Decimal


class KalshiOrderBook(BaseModel):
    """Kalshi books contain BIDS only: YES bids and NO bids.

    A NO bid at price q is equivalent to a YES ask at (1 - q).
    """

    model_config = ConfigDict(frozen=True)

    ticker: str
    yes_bids: list[OrderBookLevel]  # sorted best (highest) first
    no_bids: list[OrderBookLevel]  # sorted best (highest) first
    fetched_at: datetime

    @classmethod
    def from_api(cls, ticker: str, data: dict[str, Any], fetched_at: datetime) -> KalshiOrderBook:
        book = data.get("orderbook_fp") or data.get("orderbook") or {}

        def levels(key_fp: str, key_legacy: str) -> list[OrderBookLevel]:
            raw = book.get(key_fp)
            out: list[OrderBookLevel] = []
            if raw is not None:
                for p, s in raw:
                    out.append(OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s))))
            else:
                for p, s in book.get(key_legacy) or []:
                    out.append(OrderBookLevel(price=Decimal(p) / 100, size=Decimal(s)))
            return sorted(out, key=lambda lvl: lvl.price, reverse=True)

        return cls(
            ticker=ticker,
            yes_bids=levels("yes_dollars", "yes"),
            no_bids=levels("no_dollars", "no"),
            fetched_at=fetched_at,
        )

    @property
    def best_yes_bid(self) -> Decimal | None:
        return self.yes_bids[0].price if self.yes_bids else None

    @property
    def best_no_bid(self) -> Decimal | None:
        return self.no_bids[0].price if self.no_bids else None

    @property
    def best_yes_ask(self) -> Decimal | None:
        return ONE - self.no_bids[0].price if self.no_bids else None

    @property
    def best_no_ask(self) -> Decimal | None:
        return ONE - self.yes_bids[0].price if self.yes_bids else None

    def ask_for(self, side: Side) -> Decimal | None:
        return self.best_yes_ask if side == Side.YES else self.best_no_ask

    def ask_depth(self, side: Side, limit_price: Decimal) -> Decimal:
        """Contracts available to BUY `side` at or below limit_price."""
        opposite = self.no_bids if side == Side.YES else self.yes_bids
        return sum((lvl.size for lvl in opposite if ONE - lvl.price <= limit_price), Decimal(0))

    @property
    def spread(self) -> Decimal | None:
        if self.best_yes_bid is None or self.best_yes_ask is None:
            return None
        return self.best_yes_ask - self.best_yes_bid


class KalshiBalance(BaseModel):
    balance: Decimal  # available cash, dollars
    portfolio_value: Decimal | None = None
    updated_ts: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> KalshiBalance:
        bal = dec(data.get("balance_dollars")) if data.get("balance_dollars") else cents_to_dollars(data.get("balance"))
        return cls(
            balance=bal or Decimal(0),
            portfolio_value=cents_to_dollars(data.get("portfolio_value")),
            updated_ts=data.get("updated_ts"),
        )


class KalshiPosition(BaseModel):
    ticker: str
    position: Decimal  # >0 YES contracts, <0 NO contracts
    market_exposure: Decimal
    realized_pnl: Decimal
    fees_paid: Decimal

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> KalshiPosition:
        return cls(
            ticker=data["ticker"],
            position=dec(data.get("position_fp"), dec(data.get("position"), Decimal(0))) or Decimal(0),
            market_exposure=dec(data.get("market_exposure_dollars"), Decimal(0)) or Decimal(0),
            realized_pnl=dec(data.get("realized_pnl_dollars"), Decimal(0)) or Decimal(0),
            fees_paid=dec(data.get("fees_paid_dollars"), Decimal(0)) or Decimal(0),
        )


class KalshiOrder(BaseModel):
    order_id: str
    client_order_id: str | None = None
    ticker: str
    status: str
    outcome_side: str | None = None
    book_side: str | None = None
    yes_price: Decimal | None = None
    no_price: Decimal | None = None
    fill_count: Decimal = Decimal(0)
    remaining_count: Decimal = Decimal(0)
    initial_count: Decimal = Decimal(0)
    created_time: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> KalshiOrder:
        return cls(
            order_id=data["order_id"],
            client_order_id=data.get("client_order_id"),
            ticker=data.get("ticker", ""),
            status=data.get("status", "unknown"),
            outcome_side=data.get("outcome_side") or data.get("side"),
            book_side=data.get("book_side"),
            yes_price=dec(data.get("yes_price_dollars")),
            no_price=dec(data.get("no_price_dollars")),
            fill_count=dec(data.get("fill_count_fp"), Decimal(0)) or Decimal(0),
            remaining_count=dec(data.get("remaining_count_fp"), Decimal(0)) or Decimal(0),
            initial_count=dec(data.get("initial_count_fp"), Decimal(0)) or Decimal(0),
            created_time=data.get("created_time"),
        )


class CreateOrderResult(BaseModel):
    order_id: str
    client_order_id: str | None
    fill_count: Decimal
    remaining_count: Decimal
    average_fill_price: Decimal | None
    average_fee_paid: Decimal | None
    ts_ms: int | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CreateOrderResult:
        return cls(
            order_id=data["order_id"],
            client_order_id=data.get("client_order_id"),
            fill_count=dec(data.get("fill_count"), Decimal(0)) or Decimal(0),
            remaining_count=dec(data.get("remaining_count"), Decimal(0)) or Decimal(0),
            average_fill_price=dec(data.get("average_fill_price")),
            average_fee_paid=dec(data.get("average_fee_paid")),
            ts_ms=data.get("ts_ms"),
        )


class KalshiTrade(BaseModel):
    trade_id: str
    ticker: str
    count: Decimal
    yes_price: Decimal
    taker_outcome_side: str | None
    created_time: datetime

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> KalshiTrade:
        return cls(
            trade_id=data["trade_id"],
            ticker=data["ticker"],
            count=dec(data.get("count_fp"), dec(data.get("count"), Decimal(0))) or Decimal(0),
            yes_price=dec(data.get("yes_price_dollars"), cents_to_dollars(data.get("yes_price"))) or Decimal(0),
            taker_outcome_side=data.get("taker_outcome_side") or data.get("taker_side"),
            created_time=data["created_time"],
        )


class KalshiApiKeyInfo(BaseModel):
    api_key_id: str
    name: str | None = None
    scopes: list[str] = []
    subaccount: int | None = None


class ExchangeStatus(BaseModel):
    exchange_active: bool
    trading_active: bool
