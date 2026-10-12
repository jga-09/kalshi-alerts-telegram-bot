"""PaperTradingEngine - simulated execution against the REAL current order book.

Fills walk the visible book at or better than the limit price (IOC semantics),
charge estimated taker fees and never assume more liquidity than displayed.
Paper results are hypothetical and are always labelled as such.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, PaperTrade, Position
from kalshi_ai.domain.enums import OrderStatus, PositionStatus, Side, TradingMode
from kalshi_ai.kalshi.models import KalshiOrderBook
from kalshi_ai.services.trading_state import apply_fill_to_position, ensure_paper_account, mark_to_market
from kalshi_ai.trading.fees import estimate_maker_fee, estimate_taker_fee

ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class SimulatedFill:
    filled: int
    avg_price: Decimal | None
    fees: Decimal


def simulate_ioc_fill(book: KalshiOrderBook, side: Side, quantity: int, limit_price: Decimal) -> SimulatedFill:
    """Walk opposite-side bids (a NO bid at q is a YES ask at 1-q) from best price."""
    opposite = book.no_bids if side == Side.YES else book.yes_bids
    remaining, cost, filled = quantity, ZERO, 0
    for lvl in opposite:  # sorted best (highest bid => lowest ask) first
        ask = ONE - lvl.price
        if ask > limit_price or remaining <= 0:
            break
        take = min(remaining, int(lvl.size))
        cost += ask * take
        filled += take
        remaining -= take
    if filled == 0:
        return SimulatedFill(0, None, ZERO)
    avg = (cost / filled).quantize(Decimal("0.0001"))
    return SimulatedFill(filled, avg, estimate_taker_fee(filled, avg))


class PaperTradingEngine:
    async def execute(self, session: AsyncSession, order: Order, book: KalshiOrderBook) -> Order:
        """Taker orders: IOC against the book. Maker orders: fill what crosses now, rest the remainder."""
        fill = simulate_ioc_fill(book, order.side, order.quantity, order.price)
        if fill.filled and fill.avg_price is not None:
            if not await record_paper_fill(session, order, fill.filled, fill.avg_price, fill.fees):
                order.status = OrderStatus.REJECTED
                order.reason = "insufficient paper balance"
                return order
        order.submitted_at = utcnow()
        if order.filled_quantity >= order.quantity:
            order.status = OrderStatus.FILLED
        elif order.entry_style == "make":
            order.status = OrderStatus.RESTING
            order.reason = "resting limit order (waits for the price to come to it)"
        elif order.filled_quantity > 0:
            order.status = OrderStatus.PARTIALLY_FILLED
        else:
            order.status = OrderStatus.CANCELED
            order.reason = "paper IOC: no liquidity at or better than limit price"
        await session.flush()
        return order


async def record_paper_fill(session: AsyncSession, order: Order, filled: int, price: Decimal, fees: Decimal) -> bool:
    """Apply one (possibly partial) paper fill. Returns False if the paper balance can't cover it."""
    acct = await ensure_paper_account(session, order.user_id)
    total = price * filled + fees
    if total > acct.cash_balance:
        return False
    acct.cash_balance -= total
    prev = order.filled_quantity
    order.filled_quantity = prev + Decimal(filled)
    order.avg_fill_price = ((order.avg_fill_price or ZERO) * prev + price * filled) / order.filled_quantity
    order.avg_fill_price = order.avg_fill_price.quantize(Decimal("0.0001"))
    order.fees += fees
    await apply_fill_to_position(
        session,
        user_id=order.user_id,
        mode=TradingMode.PAPER,
        ticker=order.market_ticker,
        side=order.side,
        quantity=Decimal(filled),
        price=price,
        fees=fees,
    )
    trade = (await session.execute(select(PaperTrade).where(PaperTrade.order_id == order.id))).scalar_one_or_none()
    if trade is None:
        session.add(
            PaperTrade(
                user_id=order.user_id,
                order_id=order.id,
                market_ticker=order.market_ticker,
                side=order.side,
                quantity=filled,
                entry_price=price,
                fees=fees,
                estimated_probability=order.estimated_probability,
                edge=order.edge,
                risk_mode=order.risk_mode,
                strategy=order.strategy,
            )
        )
    else:
        trade.entry_price = order.avg_fill_price
        trade.quantity = int(order.filled_quantity)
        trade.fees += fees
    await session.flush()
    return True


async def process_resting_paper_orders(
    session: AsyncSession, ticker: str, book: KalshiOrderBook | None, now: datetime | None = None
) -> dict[str, int]:
    """Fill resting paper limit orders the market has traded down to; cancel expired ones.

    Conservative: a resting buy fills only when the opposite side is offered AT OR BELOW our limit
    (someone is visibly willing to sell at our price), never assuming queue priority.
    """
    now = now or utcnow()
    stats = {"filled": 0, "expired": 0}
    orders = (
        (
            await session.execute(
                select(Order).where(
                    Order.market_ticker == ticker,
                    Order.mode == TradingMode.PAPER.value,
                    Order.status == OrderStatus.RESTING.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for o in orders:
        remaining = o.quantity - int(o.filled_quantity)
        if book is not None and remaining > 0:
            fill = simulate_ioc_fill(book, o.side, remaining, o.price)
            if fill.filled:
                # A maker is paid its own limit price, with the (lower) maker fee estimate.
                if await record_paper_fill(session, o, fill.filled, o.price, estimate_maker_fee(fill.filled, o.price)):
                    stats["filled"] += 1
                else:
                    o.status = OrderStatus.CANCELED if o.filled_quantity == 0 else OrderStatus.PARTIALLY_FILLED
                    o.reason = "insufficient paper balance when the limit order filled"
                    continue
        if o.filled_quantity >= o.quantity:
            o.status = OrderStatus.FILLED
        elif o.expires_at is None or now >= o.expires_at:
            o.status = OrderStatus.CANCELED if o.filled_quantity == 0 else OrderStatus.PARTIALLY_FILLED
            o.reason = "limit order expired " + ("unfilled" if o.filled_quantity == 0 else "partly filled")
            stats["expired"] += 1
    await session.flush()
    return stats


async def settle_paper_market(session: AsyncSession, ticker: str, result_yes: bool) -> int:
    """Close all open paper trades/positions in a settled market. Returns trades settled."""
    await process_resting_paper_orders(session, ticker, None, now=datetime.max.replace(tzinfo=UTC))
    trades = (
        (
            await session.execute(
                select(PaperTrade).where(
                    PaperTrade.market_ticker == ticker, PaperTrade.status == PositionStatus.OPEN.value
                )
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    by_user: dict[uuid.UUID, Decimal] = {}
    for t in trades:
        won = (t.side == Side.YES) == result_yes
        exit_price = ONE if won else ZERO
        t.exit_price = exit_price
        t.pnl = (exit_price - t.entry_price) * t.quantity - t.fees
        t.status = PositionStatus.SETTLED
        t.closed_at = now
        by_user[t.user_id] = by_user.get(t.user_id, ZERO) + exit_price * t.quantity
    for user_id, payout in by_user.items():
        acct = await ensure_paper_account(session, user_id)
        acct.cash_balance += payout
    positions = (
        (
            await session.execute(
                select(Position).where(
                    Position.market_ticker == ticker,
                    Position.mode == TradingMode.PAPER.value,
                    Position.status == PositionStatus.OPEN.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for p in positions:
        won = (p.side == Side.YES) == result_yes
        p.realized_pnl += (ONE if won else ZERO) * p.quantity - p.cost_basis
        p.unrealized_pnl = ZERO
        p.mark_price = ONE if won else ZERO
        p.status = PositionStatus.SETTLED
        p.closed_at = now
    await session.flush()
    return len(trades)


async def mark_paper_positions(session: AsyncSession, ticker: str, book: KalshiOrderBook) -> None:
    """Mark open paper positions at the price we could SELL at now (best bid of that side)."""
    positions = (
        (
            await session.execute(
                select(Position).where(
                    Position.market_ticker == ticker,
                    Position.mode == TradingMode.PAPER.value,
                    Position.status == PositionStatus.OPEN.value,
                )
            )
        )
        .scalars()
        .all()
    )
    for p in positions:
        bid = book.best_yes_bid if p.side == Side.YES else book.best_no_bid
        if bid is not None:
            mark_to_market(p, bid)
