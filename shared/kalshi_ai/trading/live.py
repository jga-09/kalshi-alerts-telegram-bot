"""LiveTradingEngine - sends ONE already-validated order to Kalshi for ONE customer.

It is only invoked by ExecutionPipeline after every safety check passed and the
PENDING order row was committed. It never retries an order blindly: on timeout
the order is marked UNKNOWN and reconciled by client_order_id.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import KalshiConnection, Order
from kalshi_ai.domain.enums import OrderStatus, TradingMode
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.errors import (
    KalshiAuthError,
    KalshiError,
    KalshiTimeoutError,
    KalshiValidationError,
)
from kalshi_ai.kalshi.models import CreateOrderResult, KalshiOrder
from kalshi_ai.kalshi.services import KalshiOrderService, KalshiPortfolioService, OrderIntent
from kalshi_ai.logging import get_logger
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.services.kalshi_connections import build_user_client
from kalshi_ai.services.trading_state import apply_fill_to_position

log = get_logger(__name__)


class LiveBroker(Protocol):
    async def available_balance(self) -> Decimal: ...
    async def place(self, intent: OrderIntent) -> CreateOrderResult: ...
    async def find(self, ticker: str, client_order_id: str) -> KalshiOrder | None: ...
    async def cancel(self, order_id: str, ticker: str) -> None: ...
    async def resting_orders(self) -> list[KalshiOrder]: ...
    async def aclose(self) -> None: ...


class KalshiBroker:
    """Per-customer broker. Built from that customer's own encrypted credential only."""

    def __init__(self, client: KalshiClient):
        self.client = client
        self.orders = KalshiOrderService(client)
        self.portfolio = KalshiPortfolioService(client)

    @classmethod
    def for_connection(cls, conn: KalshiConnection, cipher: SecretCipher) -> KalshiBroker:
        return cls(build_user_client(conn, cipher))

    async def available_balance(self) -> Decimal:
        return (await self.portfolio.get_balance()).balance

    async def place(self, intent: OrderIntent) -> CreateOrderResult:
        return await self.orders.create_order(intent)

    async def find(self, ticker: str, client_order_id: str) -> KalshiOrder | None:
        return await self.orders.find_by_client_order_id(ticker, client_order_id)

    async def cancel(self, order_id: str, ticker: str) -> None:
        await self.orders.cancel_order(order_id, ticker=ticker)

    async def resting_orders(self) -> list[KalshiOrder]:
        return await self.portfolio.get_orders(status="resting")

    async def aclose(self) -> None:
        await self.client.aclose()


class LiveTradingEngine:
    async def execute(self, session: AsyncSession, order: Order, broker: LiveBroker) -> Order:
        intent = OrderIntent(order.market_ticker, order.side, order.quantity, order.price, order.client_order_id)
        order.submitted_at = utcnow()
        order.status = OrderStatus.SUBMITTED
        try:
            result = await broker.place(intent)
        except KalshiTimeoutError:
            order.status = OrderStatus.UNKNOWN
            order.reason = "timeout - outcome unknown, reconciling"
            await session.flush()
            await self.reconcile(session, order, broker)
            return order
        except KalshiValidationError as exc:
            order.status = OrderStatus.REJECTED
            order.reason = f"exchange rejected order ({exc.code or exc.status_code})"
            return order
        except KalshiAuthError:
            order.status = OrderStatus.FAILED
            order.reason = "Kalshi authentication failed - reconnect your account"
            return order
        except KalshiError as exc:
            order.status = OrderStatus.FAILED
            order.reason = f"Kalshi error: {type(exc).__name__}"
            return order
        await self._apply_result(
            session,
            order,
            result.order_id,
            result.fill_count,
            result.remaining_count,
            result.average_fill_price,
            result.average_fee_paid,
        )
        return order

    async def reconcile(self, session: AsyncSession, order: Order, broker: LiveBroker) -> Order:
        """Find out what really happened to an UNKNOWN order. Never re-submits."""
        try:
            remote = await broker.find(order.market_ticker, order.client_order_id)
        except KalshiError:
            log.warning("reconcile_failed", order_id=str(order.id))
            return order  # stays UNKNOWN; counted as exposure; retried by the worker
        if remote is None:
            order.status = OrderStatus.CANCELED
            order.reason = "timeout - order not found at exchange (not placed)"
            return order
        remaining = remote.remaining_count if remote.status == "resting" else Decimal(0)
        await self._apply_result(
            session,
            order,
            remote.order_id,
            remote.fill_count,
            remaining,
            remote.yes_price if order.side.value == "yes" else remote.no_price,
            None,
        )
        if remote.status == "resting":
            order.status = OrderStatus.RESTING
        return order

    async def _apply_result(
        self,
        session: AsyncSession,
        order: Order,
        kalshi_order_id: str,
        filled: Decimal,
        remaining: Decimal,
        avg_price: Decimal | None,
        avg_fee: Decimal | None,
    ) -> None:
        order.kalshi_order_id = kalshi_order_id
        newly_filled = filled - order.filled_quantity
        order.filled_quantity = filled
        if avg_price is not None:
            order.avg_fill_price = avg_price
        if avg_fee is not None:
            order.fees = (avg_fee * filled).quantize(Decimal("0.0001"))
        if filled >= order.quantity:
            order.status = OrderStatus.FILLED
        elif filled > 0:
            order.status = OrderStatus.PARTIALLY_FILLED if remaining == 0 else OrderStatus.RESTING
        else:
            order.status = OrderStatus.RESTING if remaining > 0 else OrderStatus.CANCELED
            if order.status == OrderStatus.CANCELED:
                order.reason = "IOC order not filled (no liquidity at limit)"
        if newly_filled > 0:
            await apply_fill_to_position(
                session,
                user_id=order.user_id,
                mode=TradingMode.LIVE,
                ticker=order.market_ticker,
                side=order.side,
                quantity=newly_filled,
                price=order.avg_fill_price or order.price,
                fees=(avg_fee or Decimal(0)) * newly_filled,
            )
        await session.flush()
