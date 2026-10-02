"""KalshiMarketService, KalshiOrderService, KalshiPortfolioService.

All Kalshi functionality used by the application goes through these classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Any

from kalshi_ai.db.base import utcnow
from kalshi_ai.domain.enums import Side
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.errors import KalshiValidationError
from kalshi_ai.kalshi.models import (
    CreateOrderResult,
    ExchangeStatus,
    KalshiApiKeyInfo,
    KalshiBalance,
    KalshiMarket,
    KalshiOrder,
    KalshiOrderBook,
    KalshiPosition,
    KalshiTrade,
)

ONE = Decimal("1")
CENT = Decimal("0.01")


class KalshiMarketService:
    """Public market data (no customer credentials required)."""

    def __init__(self, client: KalshiClient):
        self.client = client

    async def exchange_status(self) -> ExchangeStatus:
        data = await self.client.get("/exchange/status")
        return ExchangeStatus(
            exchange_active=bool(data.get("exchange_active")), trading_active=bool(data.get("trading_active"))
        )

    async def list_markets(
        self,
        *,
        status: str | None = "open",
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        limit: int = 200,
        max_pages: int = 5,
    ) -> list[KalshiMarket]:
        markets: list[KalshiMarket] = []
        cursor: str | None = None
        for _ in range(max_pages):
            data = await self.client.get(
                "/markets",
                params={
                    "status": status,
                    "series_ticker": series_ticker,
                    "event_ticker": event_ticker,
                    "limit": min(limit, 1000),
                    "cursor": cursor,
                },
            )
            markets.extend(KalshiMarket.from_api(m) for m in data.get("markets", []))
            cursor = data.get("cursor") or None
            if not cursor or len(markets) >= limit:
                break
        return markets[:limit]

    async def get_market(self, ticker: str) -> KalshiMarket:
        data = await self.client.get(f"/markets/{ticker}")
        return KalshiMarket.from_api(data["market"])

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> KalshiOrderBook:
        data = await self.client.get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        return KalshiOrderBook.from_api(ticker, data, utcnow())

    async def get_trades(self, ticker: str, *, limit: int = 100, min_ts: int | None = None) -> list[KalshiTrade]:
        data = await self.client.get("/markets/trades", params={"ticker": ticker, "limit": limit, "min_ts": min_ts})
        return [KalshiTrade.from_api(t) for t in data.get("trades", [])]

    async def get_candlesticks(
        self, series_ticker: str, ticker: str, *, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> list[dict[str, Any]]:
        data = await self.client.get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return list(data.get("candlesticks", []))


class KalshiPortfolioService:
    """Customer-scoped portfolio reads. Requires an authenticated client for ONE customer."""

    def __init__(self, client: KalshiClient):
        if not client.authenticated:
            raise ValueError("KalshiPortfolioService requires an authenticated client")
        self.client = client

    async def get_balance(self) -> KalshiBalance:
        return KalshiBalance.from_api(await self.client.get("/portfolio/balance", auth_required=True))

    async def get_positions(self, *, limit: int = 200) -> list[KalshiPosition]:
        data = await self.client.get(
            "/portfolio/positions", params={"limit": limit, "count_filter": "position"}, auth_required=True
        )
        return [KalshiPosition.from_api(p) for p in data.get("market_positions", [])]

    async def get_orders(
        self, *, ticker: str | None = None, status: str | None = None, limit: int = 100
    ) -> list[KalshiOrder]:
        data = await self.client.get(
            "/portfolio/orders", params={"ticker": ticker, "status": status, "limit": limit}, auth_required=True
        )
        return [KalshiOrder.from_api(o) for o in data.get("orders", [])]

    async def get_fills(self, *, ticker: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        data = await self.client.get("/portfolio/fills", params={"ticker": ticker, "limit": limit}, auth_required=True)
        return list(data.get("fills", []))

    async def get_api_keys(self) -> list[KalshiApiKeyInfo]:
        data = await self.client.get("/api_keys", auth_required=True)
        return [
            KalshiApiKeyInfo(
                api_key_id=k.get("api_key_id", ""),
                name=k.get("name"),
                scopes=list(k.get("scopes") or []),
                subaccount=k.get("subaccount"),
            )
            for k in data.get("api_keys", [])
        ]


@dataclass(frozen=True)
class OrderIntent:
    """Our internal order intent: BUY `quantity` contracts of `side` paying at most `limit_price` each."""

    ticker: str
    side: Side
    quantity: int
    limit_price: Decimal  # price per contract of `side`, in dollars (0.01..0.99)
    client_order_id: str


def build_v2_order_payload(intent: OrderIntent, *, time_in_force: str = "immediate_or_cancel") -> dict[str, Any]:
    """Translate a BUY intent to Kalshi's V2 single-book order.

    The V2 book is quoted in YES prices:
      * BUY YES at p  -> book side ``bid`` at p
      * BUY NO  at q  -> book side ``ask`` at (1 - q)   (selling YES == buying NO)
    """
    if intent.quantity < 1:
        raise KalshiValidationError("quantity must be >= 1")
    price = intent.limit_price.quantize(Decimal("0.0001"))
    if not (CENT <= price <= ONE - CENT):
        raise KalshiValidationError("limit price must be within 0.01..0.99")
    if intent.side == Side.YES:
        book_side, yes_price = "bid", price
    else:
        book_side, yes_price = "ask", (ONE - price)
    return {
        "ticker": intent.ticker,
        "client_order_id": intent.client_order_id,
        "side": book_side,
        "count": str(intent.quantity),
        "price": f"{yes_price:.4f}",
        "time_in_force": time_in_force,
        "self_trade_prevention_type": "taker_at_cross",
        "cancel_order_on_pause": True,
    }


class KalshiOrderService:
    """Order placement/cancellation for ONE customer.

    Should only be invoked by the LiveTradingEngine after every safety check passed.
    """

    def __init__(self, client: KalshiClient):
        if not client.authenticated:
            raise ValueError("KalshiOrderService requires an authenticated client")
        self.client = client

    async def create_order(
        self, intent: OrderIntent, *, time_in_force: str = "immediate_or_cancel"
    ) -> CreateOrderResult:
        payload = build_v2_order_payload(intent, time_in_force=time_in_force)
        data = await self.client.request(
            "POST", "/portfolio/events/orders", json=payload, auth_required=True, retry=False
        )
        return CreateOrderResult.from_api(data.get("order", data))

    async def cancel_order(self, order_id: str, *, ticker: str | None = None) -> dict[str, Any]:
        return await self.client.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": ticker},
            auth_required=True,
            retry=False,
        )

    async def get_order(self, order_id: str) -> KalshiOrder:
        data = await self.client.get(f"/portfolio/orders/{order_id}", auth_required=True)
        return KalshiOrder.from_api(data.get("order", data))

    async def find_by_client_order_id(self, ticker: str, client_order_id: str) -> KalshiOrder | None:
        """Reconciliation after a timeout: did our order reach the exchange?"""
        data = await self.client.get("/portfolio/orders", params={"ticker": ticker, "limit": 200}, auth_required=True)
        for raw in data.get("orders", []):
            if raw.get("client_order_id") == client_order_id:
                return KalshiOrder.from_api(raw)
        return None


def contracts_affordable(balance: Decimal, price: Decimal, fee_buffer: Decimal = Decimal("0.02")) -> int:
    """Whole contracts affordable at `price` with a per-contract fee buffer."""
    per = price + fee_buffer
    if per <= 0:
        return 0
    return int((balance / per).to_integral_value(rounding=ROUND_DOWN))
