"""Execution pipeline - the ONLY path from a signal to an order.

Signal -> Eligibility -> Idempotency -> Market check -> Risk engine -> Order construction
       -> Final validation -> Persist PENDING -> Paper engine / Kalshi API -> Confirmation
       -> Database -> Telegram notification

If ANY check fails: DO NOT TRADE. Every decision is persisted with its reasons.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, RiskSettings, User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import OrderStatus, SignalAction, TradingMode, UserStatus
from kalshi_ai.kalshi.errors import KalshiError
from kalshi_ai.kalshi.models import KalshiMarket, KalshiOrderBook
from kalshi_ai.logging import bind_context, get_logger
from kalshi_ai.notifications.messages import Notifier, TradeNotification
from kalshi_ai.risk.engine import CheckResult, LiquidityView, RiskDecision, RiskEngine, SignalView
from kalshi_ai.services.audit import audit
from kalshi_ai.services.kalshi_connections import connection_can_trade, get_connection
from kalshi_ai.services.kill_switch import LIVE_TRADING_SUSPENDED, get_flag, is_global_kill_active
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai.services.trading_controls import limits_for
from kalshi_ai.services.trading_state import account_state, ensure_paper_account
from kalshi_ai.signals.engine import SignalDecision
from kalshi_ai.trading.live import LiveBroker, LiveTradingEngine
from kalshi_ai.trading.paper import PaperTradingEngine

log = get_logger(__name__)
CLIENT_ORDER_NAMESPACE = uuid.UUID("6f1c2f2e-6a1e-4c1b-9d33-7b1c8a3e5f10")
MAX_PRICE_DRIFT = Decimal("0.02")  # current ask may be at most 2c worse than the signal's entry price
CLOSE_BUFFER = timedelta(minutes=1)


class Outcome(StrEnum):
    EXECUTED = "executed"
    NOT_FILLED = "not_filled"
    REJECTED = "rejected"  # failed a safety/risk check - nothing sent
    INELIGIBLE = "ineligible"  # user not set up for this mode
    DUPLICATE = "duplicate"
    ERROR = "error"  # exchange/API error after sending (order state persisted)


@dataclass
class ExecutionResult:
    outcome: Outcome
    mode: TradingMode
    order: Order | None = None
    checks: list[CheckResult] = field(default_factory=list)
    reason: str = ""

    @property
    def failed_checks(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]


class MarketDataProvider(Protocol):
    async def get_market(self, ticker: str) -> KalshiMarket: ...
    async def get_orderbook(self, ticker: str) -> KalshiOrderBook: ...


BrokerFactory = Callable[[AsyncSession, User], Awaitable[LiveBroker]]


def client_order_id(user_id: uuid.UUID, signal_id: Any, mode: TradingMode) -> str:
    """Deterministic: the same (user, signal, mode) always maps to the same exchange client_order_id."""
    return str(uuid.uuid5(CLIENT_ORDER_NAMESPACE, f"{user_id}:{signal_id}:{mode.value}"))


class ExecutionPipeline:
    def __init__(
        self,
        market_data: MarketDataProvider,
        *,
        broker_factory: BrokerFactory | None = None,
        notifier: Notifier | None = None,
        settings: Settings | None = None,
    ):
        self.market_data = market_data
        self.broker_factory = broker_factory
        self.notifier = notifier
        self.settings = settings or get_settings()
        self.paper = PaperTradingEngine()
        self.live = LiveTradingEngine()

    # ------------------------------------------------------------------ eligibility
    async def eligibility(
        self, session: AsyncSession, user: User, rs: RiskSettings, mode: TradingMode
    ) -> list[CheckResult]:
        checks: list[CheckResult] = []

        def check(name: str, ok: bool, detail: str) -> None:
            checks.append(CheckResult(name, bool(ok), "" if ok else detail))

        access = await get_access(session, user)
        check("user_active", user.status == UserStatus.ACTIVE, "account suspended")
        check("global_kill_switch", not await is_global_kill_active(session), "global kill switch active")
        check("user_kill_switch", not rs.kill_switch_active, "user emergency stop active")
        check("admin_allowed", not rs.admin_auto_trading_disabled, "automated trading disabled by admin")
        if mode == TradingMode.PAPER:
            check("subscription", access.has(Feature.PAPER_TRADING), "plan does not include paper trading")
            check("paper_enabled", rs.paper_trading_enabled, "paper trading not started")
        else:
            check("platform_live_enabled", self.settings.live_trading, "LIVE_TRADING disabled on platform")
            check(
                "monitor_suspension",
                not (await get_flag(session, LIVE_TRADING_SUSPENDED)).active,
                "live trading suspended by safety monitor",
            )
            check("subscription", access.has(Feature.LIVE_TRADING), "plan does not include live trading")
            check("auto_trading_enabled", rs.auto_trading_enabled, "auto trading not enabled")
            check("live_trading_enabled", rs.live_trading_enabled, "live trading not enabled")
            check(
                "risk_confirmed",
                rs.live_trading_confirmed_at is not None
                and rs.confirmed_risk_hash == limits_for(rs, self.settings).fingerprint(),
                "risk settings not confirmed (or changed since confirmation)",
            )
            conn = await get_connection(session, user.id)
            check("kalshi_connected", connection_can_trade(conn), "Kalshi not connected with a trade-scoped key")
        return checks

    # ------------------------------------------------------------------ main entry
    async def execute(
        self, session: AsyncSession, user: User, signal: SignalDecision, mode: TradingMode
    ) -> ExecutionResult:
        bind_context(customer_id=str(user.id), signal_id=str(signal.signal_id), mode=mode.value)
        rs = (await session.execute(select(RiskSettings).where(RiskSettings.user_id == user.id))).scalar_one()
        checks = await self.eligibility(session, user, rs, mode)
        if not all(c.passed for c in checks):
            return ExecutionResult(
                Outcome.INELIGIBLE,
                mode,
                checks=checks,
                reason="; ".join(f"{c.name}: {c.detail}" for c in checks if not c.passed),
            )
        if signal.action != SignalAction.TRADE_IF_RISK_PASSES or signal.signal_id is None:
            return ExecutionResult(Outcome.REJECTED, mode, reason=f"signal action is {signal.action.value}")

        existing = (
            await session.execute(
                select(Order).where(
                    Order.user_id == user.id, Order.signal_id == signal.signal_id, Order.mode == mode.value
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return ExecutionResult(Outcome.DUPLICATE, mode, order=existing, reason="signal already processed")

        now = utcnow()
        # ---- Market check (fresh data, never the signal's cached view) ----
        try:
            market = await self.market_data.get_market(signal.market_ticker)
            book = await self.market_data.get_orderbook(signal.market_ticker)
        except KalshiError as exc:
            return await self._reject(
                session,
                user,
                signal,
                mode,
                rs,
                [CheckResult("market_data", False, f"market data unavailable: {type(exc).__name__}")],
            )
        current_ask = book.ask_for(signal.side)
        market_checks = [
            CheckResult("market_open", market.is_open, "" if market.is_open else f"market status {market.status}"),
            CheckResult(
                "market_not_closing",
                market.close_time is None or market.close_time - now > CLOSE_BUFFER,
                "market closes within 1 minute",
            ),
            CheckResult("price_available", current_ask is not None, "no ask available"),
        ]
        if current_ask is not None:
            drift_ok = current_ask <= signal.entry_price + MAX_PRICE_DRIFT
            market_checks.append(
                CheckResult(
                    "price_current", drift_ok, "" if drift_ok else f"ask moved {signal.entry_price}->{current_ask}"
                )
            )
        book_age = (now - book.fetched_at).total_seconds()
        market_checks.append(
            CheckResult(
                "orderbook_fresh", book_age <= self.settings.max_data_age_seconds, f"order book {book_age:.0f}s old"
            )
        )
        if not all(c.passed for c in market_checks):
            return await self._reject(session, user, signal, mode, rs, market_checks)
        assert current_ask is not None

        # ---- Balance ----
        broker: LiveBroker | None = None
        try:
            if mode == TradingMode.PAPER:
                balance = (await ensure_paper_account(session, user.id)).cash_balance
            else:
                if self.broker_factory is None:
                    return await self._reject(
                        session, user, signal, mode, rs, [CheckResult("broker", False, "live broker not configured")]
                    )
                broker = await self.broker_factory(session, user)
                balance = await broker.available_balance()
        except KalshiError as exc:
            if broker is not None:
                await broker.aclose()
            return await self._reject(
                session,
                user,
                signal,
                mode,
                rs,
                [CheckResult("balance", False, f"balance unavailable: {type(exc).__name__}")],
            )

        try:
            # ---- Risk engine (re-priced at the CURRENT ask) ----
            limits = limits_for(rs, self.settings)
            engine = RiskEngine(
                limits,
                rs.risk_mode,
                min_price=self.settings.hard_min_price,
                max_price=self.settings.hard_max_price,
                max_data_age_seconds=self.settings.max_data_age_seconds,
            )
            view = SignalView(
                market_ticker=signal.market_ticker,
                correlation_group=signal.correlation_group,
                side=signal.side,
                estimated_probability=signal.estimated_probability,
                market_probability=current_ask,
                entry_price=current_ask,
                edge=signal.estimated_probability - current_ask,
                confidence=signal.confidence,
                expires_at=signal.expires_at,
                data_age_seconds=max(signal.data_age_seconds, book_age),
            )
            acct = await account_state(session, user.id, mode, signal.market_ticker, balance, now)
            spread = book.spread
            liquidity = LiquidityView(
                spread=spread,
                depth_at_limit=book.ask_depth(signal.side, current_ask),
                top_of_book_depth=book.top_ask_size(signal.side),
            )
            decision = engine.evaluate(view, acct, liquidity, now)
            if not decision.approved:
                return await self._reject(session, user, signal, mode, rs, market_checks + decision.checks)

            # ---- Order construction + final validation ----
            order = Order(
                user_id=user.id,
                signal_id=signal.signal_id,
                mode=mode,
                market_ticker=signal.market_ticker,
                side=signal.side,
                action="buy",
                quantity=decision.quantity,
                price=current_ask,
                yes_price=current_ask if signal.side.value == "yes" else Decimal(1) - current_ask,
                book_side="bid" if signal.side.value == "yes" else "ask",
                status=OrderStatus.PENDING,
                client_order_id=client_order_id(user.id, signal.signal_id, mode),
                risk_mode=rs.risk_mode,
                model_version=signal.model_version,
                estimated_probability=signal.estimated_probability,
                market_probability=current_ask,
                edge=view.edge,
                validation=self._validation_record(market_checks, decision),
            )
            final = await self.final_validation(session, user, order, mode)
            if not all(c.passed for c in final):
                return await self._reject(session, user, signal, mode, rs, market_checks + decision.checks + final)

            try:
                async with session.begin_nested():
                    session.add(order)
                    await session.flush()
            except IntegrityError:
                return ExecutionResult(Outcome.DUPLICATE, mode, reason="concurrent duplicate order blocked")
            bind_context(trade_id=str(order.id))
            await audit(
                session,
                f"order.{mode.value}.created",
                actor_user_id=user.id,
                actor_type="system",
                target_type="order",
                target_id=order.id,
                details={
                    "ticker": order.market_ticker,
                    "side": order.side.value,
                    "qty": order.quantity,
                    "price": str(order.price),
                },
            )

            # ---- Execute ----
            if mode == TradingMode.PAPER:
                await self.paper.execute(session, order, book)
            else:
                assert broker is not None
                await session.commit()  # durably record PENDING before anything reaches the exchange
                await self.live.execute(session, order, broker)
        finally:
            if broker is not None:
                await broker.aclose()

        outcome = {
            OrderStatus.FILLED: Outcome.EXECUTED,
            OrderStatus.PARTIALLY_FILLED: Outcome.EXECUTED,
            OrderStatus.RESTING: Outcome.EXECUTED,
            OrderStatus.CANCELED: Outcome.NOT_FILLED,
            OrderStatus.REJECTED: Outcome.REJECTED,
        }.get(order.status, Outcome.ERROR)
        await audit(
            session,
            f"order.{mode.value}.{order.status.value}",
            actor_user_id=user.id,
            actor_type="system",
            target_type="order",
            target_id=order.id,
            details={"filled": str(order.filled_quantity)},
        )
        await session.flush()
        await self._notify(user, order, market)
        return ExecutionResult(
            outcome, mode, order=order, checks=market_checks + decision.checks + final, reason=order.reason or ""
        )

    async def final_validation(
        self, session: AsyncSession, user: User, order: Order, mode: TradingMode
    ) -> list[CheckResult]:
        """Last gate before sending. Re-reads kill switches straight from the database."""
        s = self.settings
        rs = (
            await session.execute(
                select(RiskSettings).where(RiskSettings.user_id == user.id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        notional = order.price * order.quantity
        checks = [
            CheckResult("final_global_kill", not await is_global_kill_active(session), "global kill switch active"),
            CheckResult("final_user_kill", not rs.kill_switch_active, "user emergency stop active"),
            CheckResult(
                "final_quantity",
                isinstance(order.quantity, int) and 1 <= order.quantity <= s.hard_max_order_contracts,
                f"invalid quantity {order.quantity}",
            ),
            CheckResult(
                "final_price", s.hard_min_price <= order.price <= s.hard_max_price, f"invalid price {order.price}"
            ),
            CheckResult(
                "final_notional", notional <= s.hard_max_order_notional_usd, f"notional {notional} exceeds hard cap"
            ),
        ]
        if mode == TradingMode.LIVE:
            checks.append(
                CheckResult("final_live_enabled", rs.live_trading_enabled and s.live_trading, "live trading disabled")
            )
        return checks

    async def _reject(
        self,
        session: AsyncSession,
        user: User,
        signal: SignalDecision,
        mode: TradingMode,
        rs: RiskSettings,
        checks: list[CheckResult],
    ) -> ExecutionResult:
        reason = "; ".join(f"{c.name}: {c.detail}" for c in checks if not c.passed)[:500]
        await audit(
            session,
            f"order.{mode.value}.blocked",
            actor_user_id=user.id,
            actor_type="system",
            target_type="signal",
            target_id=str(signal.signal_id),
            details={"failed": [c.name for c in checks if not c.passed], "reason": reason},
        )
        log.info("trade_blocked", reason=reason, mode=mode.value)
        return ExecutionResult(Outcome.REJECTED, mode, checks=checks, reason=reason)

    @staticmethod
    def _validation_record(checks: list[CheckResult], decision: RiskDecision) -> dict[str, Any]:
        return {"market": [c.as_dict() for c in checks], "risk": decision.as_dict()}

    async def _notify(self, user: User, order: Order, market: KalshiMarket) -> None:
        if self.notifier is None or user.telegram_chat_id is None:
            return
        note = TradeNotification(
            mode=order.mode.value,
            status=order.status.value,
            market_ticker=order.market_ticker,
            market_title=market.title,
            side=order.side.value,
            entry_price=order.avg_fill_price or order.price,
            quantity=order.quantity,
            filled_quantity=order.filled_quantity,
            model_probability=order.estimated_probability,
            market_probability=order.market_probability,
            edge=order.edge,
            risk_mode=order.risk_mode.value if order.risk_mode else None,
            reason=order.reason,
        )
        try:
            await self.notifier.send(user.telegram_chat_id, note.render())
        except Exception as exc:  # Telegram outage must never affect trade state
            log.warning("notification_failed", error=type(exc).__name__)
