"""PaperTradingEngine - simulated execution against the REAL current order book.

Fills walk the visible book at or better than the limit price (IOC semantics),
charge estimated taker fees and never assume more liquidity than displayed.
Paper results are hypothetical and are always labelled as such.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, PaperTrade, Position
from kalshi_ai.domain.enums import OrderStatus, PositionStatus, Side, TradingMode
from kalshi_ai.kalshi.models import KalshiOrderBook
from kalshi_ai.services.trading_state import apply_fill_to_position, ensure_paper_account, mark_to_market
from kalshi_ai.trading.fees import estimate_taker_fee

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
        acct = await ensure_paper_account(session, order.user_id)
        fill = simulate_ioc_fill(book, order.side, order.quantity, order.price)
        if fill.filled == 0 or fill.avg_price is None:
            order.status = OrderStatus.CANCELED
            order.reason = "paper IOC: no liquidity at or better than limit price"
            return order
        total = fill.avg_price * fill.filled + fill.fees
        if total > acct.cash_balance:
            order.status = OrderStatus.REJECTED
            order.reason = "insufficient paper balance"
            return order
        acct.cash_balance -= total
        order.filled_quantity = Decimal(fill.filled)
        order.avg_fill_price = fill.avg_price
        order.fees = fill.fees
        order.status = OrderStatus.FILLED if fill.filled == order.quantity else OrderStatus.PARTIALLY_FILLED
        order.submitted_at = utcnow()
        await apply_fill_to_position(
            session,
            user_id=order.user_id,
            mode=TradingMode.PAPER,
            ticker=order.market_ticker,
            side=order.side,
            quantity=Decimal(fill.filled),
            price=fill.avg_price,
            fees=fill.fees,
        )
        session.add(
            PaperTrade(
                user_id=order.user_id,
                order_id=order.id,
                market_ticker=order.market_ticker,
                side=order.side,
                quantity=fill.filled,
                entry_price=fill.avg_price,
                fees=fill.fees,
                estimated_probability=order.estimated_probability,
                edge=order.edge,
                risk_mode=order.risk_mode,
            )
        )
        await session.flush()
        return order


async def settle_paper_market(session: AsyncSession, ticker: str, result_yes: bool) -> int:
    """Close all open paper trades/positions in a settled market. Returns trades settled."""
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
