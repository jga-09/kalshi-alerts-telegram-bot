"""Emergency stop: block new automated orders immediately, then cancel eligible open orders.

Blocking is synchronous (a DB flag the execution validator re-reads on every order).
Cancellation is best-effort and only touches orders Kalshi AI placed (tracked in our DB).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import Order, User
from kalshi_ai.domain.enums import OrderStatus, TradingMode
from kalshi_ai.kalshi.errors import KalshiError
from kalshi_ai.logging import get_logger
from kalshi_ai.services.audit import audit
from kalshi_ai.trading.live import LiveBroker

log = get_logger(__name__)
CANCELLABLE = [
    OrderStatus.RESTING.value,
    OrderStatus.SUBMITTED.value,
    OrderStatus.PENDING.value,
    OrderStatus.UNKNOWN.value,
]


async def cancel_open_orders(
    session: AsyncSession, user: User, broker_factory: Callable[[AsyncSession, User], Awaitable[LiveBroker]] | None
) -> dict[str, int]:
    orders = (
        (await session.execute(select(Order).where(Order.user_id == user.id, Order.status.in_(CANCELLABLE))))
        .scalars()
        .all()
    )
    stats = {"paper_canceled": 0, "live_canceled": 0, "live_failed": 0}
    live = [o for o in orders if o.mode == TradingMode.LIVE]
    for o in orders:
        if o.mode == TradingMode.PAPER:
            o.status = OrderStatus.CANCELED
            o.reason = "emergency stop"
            stats["paper_canceled"] += 1
    if live and broker_factory is not None:
        broker = await broker_factory(session, user)
        try:
            ours = {o.kalshi_order_id: o for o in live if o.kalshi_order_id}
            for remote in await broker.resting_orders():
                local = ours.get(remote.order_id)
                if local is None:
                    continue  # never touch orders the customer placed themselves
                try:
                    await broker.cancel(remote.order_id, remote.ticker)
                    local.status = OrderStatus.CANCELED
                    local.reason = "emergency stop"
                    stats["live_canceled"] += 1
                except KalshiError:
                    stats["live_failed"] += 1
        except KalshiError as exc:
            log.error("emergency_cancel_failed", error=type(exc).__name__)
            stats["live_failed"] += len(live)
        finally:
            await broker.aclose()
    await audit(session, "user.kill_switch.orders_canceled", actor_user_id=user.id, actor_type="system", details=stats)
    return stats
