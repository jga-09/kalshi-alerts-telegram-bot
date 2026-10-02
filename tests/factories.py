"""Test data builders and fakes (market data provider, live broker, notifier)."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from decimal import Decimal as D

from kalshi_ai.data.base import Candle
from kalshi_ai.db.base import utcnow
from kalshi_ai.domain.enums import ConfidenceLevel, Side, SignalAction
from kalshi_ai.kalshi.errors import KalshiTimeoutError, KalshiValidationError
from kalshi_ai.kalshi.models import CreateOrderResult, KalshiMarket, KalshiOrder, KalshiOrderBook, OrderBookLevel
from kalshi_ai.kalshi.services import OrderIntent
from kalshi_ai.signals.engine import SignalDecision

TICKER = "KXBTC15M-26OCT021500-T"


def book(
    yes_bids=(("0.53", "300"),), no_bids=(("0.45", "300"),), at: datetime | None = None, ticker: str = TICKER
) -> KalshiOrderBook:
    return KalshiOrderBook(
        ticker=ticker,
        yes_bids=sorted([OrderBookLevel(price=D(p), size=D(s)) for p, s in yes_bids], key=lambda x: -x.price),
        no_bids=sorted([OrderBookLevel(price=D(p), size=D(s)) for p, s in no_bids], key=lambda x: -x.price),
        fetched_at=at or utcnow(),
    )


def market(
    status: str = "active",
    close_in: timedelta = timedelta(minutes=10),
    ticker: str = TICKER,
    title: str = "Bitcoin price up or down in 15 minutes",
    open_ago: timedelta = timedelta(minutes=5),
    **raw,
) -> KalshiMarket:
    now = utcnow()
    return KalshiMarket(
        ticker=ticker,
        event_ticker="KXBTC15M-26OCT021500",
        title=title,
        status=status,
        open_time=now - open_ago,
        close_time=now + close_in,
        yes_bid=D("0.53"),
        yes_ask=D("0.55"),
        no_bid=D("0.45"),
        no_ask=D("0.47"),
        raw=raw,
    )


def candles(n: int = 120, start: float = 60_000.0, drift: float = 2.0, end: datetime | None = None) -> list[Candle]:
    end = end or utcnow().replace(second=0, microsecond=0) - timedelta(minutes=1)
    out = []
    price = start
    for i in range(n):
        ts = end - timedelta(minutes=n - 1 - i)
        o = price
        price = price + drift + 15 * math.sin(i / 3)
        out.append(Candle(ts, o, max(o, price) + 5, min(o, price) - 5, price, 10 + (i % 7)))
    return out


def signal(
    signal_id: uuid.UUID | None = None,
    *,
    side: Side = Side.YES,
    prob: str = "0.66",
    price: str = "0.55",
    confidence: str = "0.85",
    action: SignalAction = SignalAction.TRADE_IF_RISK_PASSES,
    expires_in: timedelta = timedelta(minutes=3),
    ticker: str = TICKER,
) -> SignalDecision:
    now = utcnow()
    return SignalDecision(
        market_ticker=ticker,
        correlation_group=ticker.split("-")[0],
        action=action,
        side=side,
        estimated_probability=D(prob),
        market_probability=D(price),
        entry_price=D(price),
        edge=D(prob) - D(price),
        confidence=D(confidence),
        confidence_level=ConfidenceLevel.HIGH,
        risk_level="low",
        model_version="ensemble[rule_based:1.0.0]",
        as_of=now,
        expires_at=now + expires_in,
        idempotency_key=f"{ticker}:test:{uuid.uuid4().hex}",
        data_age_seconds=3,
        signal_id=signal_id,
    )


class FakeMarketData:
    def __init__(
        self, mkt: KalshiMarket | None = None, ob: KalshiOrderBook | None = None, error: Exception | None = None
    ):
        self.mkt = mkt or market()
        self.ob = ob or book()
        self.error = error

    async def get_market(self, ticker: str) -> KalshiMarket:
        if self.error:
            raise self.error
        return self.mkt

    async def get_orderbook(self, ticker: str) -> KalshiOrderBook:
        if self.error:
            raise self.error
        return self.ob


class FakeBroker:
    def __init__(self, balance: str = "500", mode: str = "fill", fill_ratio: float = 1.0):
        self.balance = D(balance)
        self.mode = mode
        self.fill_ratio = fill_ratio
        self.placed: list[OrderIntent] = []
        self.canceled: list[str] = []
        self.remote: dict[str, KalshiOrder] = {}
        self.closed = 0

    async def available_balance(self) -> D:
        return self.balance

    async def place(self, intent: OrderIntent) -> CreateOrderResult:
        self.placed.append(intent)
        filled = D(int(intent.quantity * self.fill_ratio))
        oid = f"ord-{len(self.placed)}"
        self.remote[intent.client_order_id] = KalshiOrder(
            order_id=oid,
            client_order_id=intent.client_order_id,
            ticker=intent.ticker,
            status="executed" if filled else "canceled",
            fill_count=filled,
            yes_price=intent.limit_price if intent.side == Side.YES else D(1) - intent.limit_price,
            no_price=intent.limit_price if intent.side == Side.NO else D(1) - intent.limit_price,
        )
        if self.mode == "timeout_but_placed":
            raise KalshiTimeoutError("timeout")
        if self.mode == "timeout_not_placed":
            del self.remote[intent.client_order_id]
            raise KalshiTimeoutError("timeout")
        if self.mode == "reject":
            raise KalshiValidationError("insufficient", status_code=400, code="insufficient_balance")
        return CreateOrderResult(
            order_id=oid,
            client_order_id=intent.client_order_id,
            fill_count=filled,
            remaining_count=D(0),
            average_fill_price=intent.limit_price,
            average_fee_paid=D("0.01"),
            ts_ms=1,
        )

    async def find(self, ticker: str, client_order_id: str) -> KalshiOrder | None:
        return self.remote.get(client_order_id)

    async def cancel(self, order_id: str, ticker: str) -> None:
        self.canceled.append(order_id)

    async def resting_orders(self) -> list[KalshiOrder]:
        return [o for o in self.remote.values() if o.status == "resting"]

    async def aclose(self) -> None:
        self.closed += 1


class FailingNotifier:
    async def send(self, chat_id: int, text: str) -> bool:
        raise ConnectionError("telegram down")
