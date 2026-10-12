"""Per-user trading state queries. Always filtered by user_id AND mode - never mixed."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, PaperAccount, PaperTrade, Position
from kalshi_ai.domain.enums import OrderStatus, PositionStatus, Side, TradingMode
from kalshi_ai.risk.engine import AccountState

ZERO = Decimal(0)
COUNTED_ORDER_STATUSES = [
    s.value
    for s in (
        OrderStatus.PENDING,
        OrderStatus.SUBMITTED,
        OrderStatus.RESTING,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.UNKNOWN,
    )
]
OPEN_ORDER_STATUSES = [
    s.value for s in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.RESTING, OrderStatus.UNKNOWN)
]


def correlation_group(ticker: str) -> str:
    """Kalshi tickers are SERIES-EVENT-MARKET; markets in one series are treated as correlated."""
    return ticker.split("-", 1)[0].upper()


def day_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def ensure_paper_account(session: AsyncSession, user_id: uuid.UUID) -> PaperAccount:
    acct = (await session.execute(select(PaperAccount).where(PaperAccount.user_id == user_id))).scalar_one_or_none()
    if acct is None:
        start = get_settings().paper_starting_balance
        acct = PaperAccount(user_id=user_id, starting_balance=start, cash_balance=start)
        session.add(acct)
        await session.flush()
    return acct


async def open_positions(session: AsyncSession, user_id: uuid.UUID, mode: TradingMode) -> list[Position]:
    res = await session.execute(
        select(Position)
        .where(Position.user_id == user_id, Position.mode == mode.value, Position.status == PositionStatus.OPEN.value)
        .order_by(Position.updated_at.desc())
    )
    return list(res.scalars())


async def trades_today(
    session: AsyncSession, user_id: uuid.UUID, mode: TradingMode, now: datetime | None = None
) -> int:
    res = await session.execute(
        select(func.count(Order.id)).where(
            Order.user_id == user_id,
            Order.mode == mode.value,
            Order.created_at >= day_start(now),
            Order.status.in_(COUNTED_ORDER_STATUSES),
        )
    )
    return int(res.scalar_one())


async def realized_pnl_today(
    session: AsyncSession, user_id: uuid.UUID, mode: TradingMode, now: datetime | None = None
) -> Decimal:
    start = day_start(now)
    if mode == TradingMode.PAPER:
        res = await session.execute(
            select(func.coalesce(func.sum(PaperTrade.pnl), 0)).where(
                PaperTrade.user_id == user_id, PaperTrade.closed_at >= start
            )
        )
        return Decimal(str(res.scalar_one()))
    res = await session.execute(
        select(func.coalesce(func.sum(Position.realized_pnl), 0)).where(
            Position.user_id == user_id, Position.mode == mode.value, Position.updated_at >= start
        )
    )
    return Decimal(str(res.scalar_one()))


async def account_state(
    session: AsyncSession,
    user_id: uuid.UUID,
    mode: TradingMode,
    ticker: str,
    available_balance: Decimal,
    now: datetime | None = None,
) -> AccountState:
    group = correlation_group(ticker)
    positions = await open_positions(session, user_id, mode)
    pending = (
        (
            await session.execute(
                select(Order).where(
                    Order.user_id == user_id, Order.mode == mode.value, Order.status.in_(OPEN_ORDER_STATUSES)
                )
            )
        )
        .scalars()
        .all()
    )
    exposure_market = sum((p.cost_basis for p in positions if p.market_ticker == ticker), ZERO)
    exposure_group = sum((p.cost_basis for p in positions if correlation_group(p.market_ticker) == group), ZERO)
    for o in pending:
        notional = o.price * (Decimal(o.quantity) - o.filled_quantity)
        if o.market_ticker == ticker:
            exposure_market += notional
        if correlation_group(o.market_ticker) == group:
            exposure_group += notional
    contracts_market = sum((p.quantity for p in positions if p.market_ticker == ticker), ZERO)
    return AccountState(
        available_balance=available_balance,
        realized_pnl_today=await realized_pnl_today(session, user_id, mode, now),
        unrealized_pnl_today=sum((p.unrealized_pnl for p in positions), ZERO),
        trades_today=await trades_today(session, user_id, mode, now),
        position_contracts_in_market=contracts_market,
        exposure_in_market=exposure_market,
        exposure_in_group=exposure_group,
    )


async def apply_fill_to_position(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    mode: TradingMode,
    ticker: str,
    side: Side,
    quantity: Decimal,
    price: Decimal,
    fees: Decimal = ZERO,
) -> Position:
    pos = (
        await session.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.mode == mode.value,
                Position.market_ticker == ticker,
                Position.side == side.value,
            )
        )
    ).scalar_one_or_none()
    if pos is None:
        pos = Position(
            user_id=user_id,
            mode=mode,
            market_ticker=ticker,
            side=side,
            quantity=ZERO,
            avg_price=ZERO,
            cost_basis=ZERO,
            realized_pnl=ZERO,
            unrealized_pnl=ZERO,
        )
        session.add(pos)
    if pos.status != PositionStatus.OPEN:
        pos.status = PositionStatus.OPEN
        pos.quantity, pos.cost_basis, pos.avg_price, pos.closed_at = ZERO, ZERO, ZERO, None
    new_qty = pos.quantity + quantity
    pos.cost_basis = pos.cost_basis + quantity * price + fees
    pos.quantity = new_qty
    pos.avg_price = (pos.cost_basis / new_qty) if new_qty > 0 else ZERO
    pos.mark_price = price
    pos.updated_at = utcnow()
    await session.flush()
    return pos


def mark_to_market(pos: Position, mark: Decimal) -> None:
    pos.mark_price = mark
    pos.unrealized_pnl = pos.quantity * mark - pos.cost_basis


def expiry(ts: datetime, minutes: int) -> datetime:
    return ts + timedelta(minutes=minutes)
