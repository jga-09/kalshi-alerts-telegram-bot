"""Async job implementations (unit-testable without Celery)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.data.base import DataConnector
from kalshi_ai.data.connectors import default_connectors
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, Position, Prediction, RiskSettings, Signal, User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import (
    OrderStatus,
    PositionStatus,
    SignalAction,
    TradingMode,
    UserStatus,
)
from kalshi_ai.kalshi.errors import KalshiError
from kalshi_ai.kalshi.services import KalshiMarketService
from kalshi_ai.logging import get_logger
from kalshi_ai.modeling.evaluator import train_and_register_logistic
from kalshi_ai.monitoring.drift import apply_safety_actions, run_checks
from kalshi_ai.notifications.messages import Notifier, render_signal
from kalshi_ai.services.analysis import AnalysisService
from kalshi_ai.services.ingestion import run_connector
from kalshi_ai.services.subscriptions import expire_due_subscriptions, get_access
from kalshi_ai.trading.emergency import cancel_open_orders
from kalshi_ai.trading.live import LiveBroker, LiveTradingEngine
from kalshi_ai.trading.paper import mark_paper_positions, settle_paper_market
from kalshi_ai.trading.pipeline import ExecutionPipeline, MarketDataProvider

log = get_logger(__name__)
BrokerFactory = Callable[[AsyncSession, User], Awaitable[LiveBroker]]


@dataclass
class JobContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis | None
    market_service: KalshiMarketService
    analysis: AnalysisService
    broker_factory: BrokerFactory | None
    notifier: Notifier | None
    settings: Settings = field(default_factory=get_settings)


async def ingest_data(ctx: JobContext, connectors: list[DataConnector] | None = None) -> dict[str, bool]:
    results = {}
    for connector in connectors or default_connectors(ctx.settings):
        async with ctx.sessionmaker() as session:
            try:
                res = await run_connector(session, ctx.redis, connector)
                await session.commit()
                results[connector.name] = res.ok
            except Exception as exc:  # one bad source never stops the others
                await session.rollback()
                log.error("ingest_failed", source=connector.name, error=type(exc).__name__)
                results[connector.name] = False
    return results


async def _eligible_users(session: AsyncSession) -> list[tuple[User, RiskSettings]]:
    rows = (
        await session.execute(
            select(User, RiskSettings)
            .join(RiskSettings, RiskSettings.user_id == User.id)
            .where(
                User.status == UserStatus.ACTIVE.value,
                RiskSettings.kill_switch_active.is_(False),
                (RiskSettings.paper_trading_enabled.is_(True)) | (RiskSettings.live_trading_enabled.is_(True)),
            )
        )
    ).all()
    return [(u, rs) for u, rs in rows]


async def scan_markets(ctx: JobContext, tickers: list[str] | None = None) -> dict[str, Any]:
    """Analyze markets; for actionable signals, fan out to each eligible user's own execution."""
    stats: dict[str, Any] = {"analyzed": 0, "signals": 0, "executions": {}}
    if tickers is None:
        tickers = []
        for series in ctx.settings.featured_series:
            try:
                tickers += [
                    m.ticker
                    for m in await ctx.market_service.list_markets(series_ticker=series, limit=5, max_pages=1)
                    if m.is_open
                ]
            except KalshiError:
                continue
    pipeline = ExecutionPipeline(
        _MarketData(ctx.market_service), broker_factory=ctx.broker_factory, notifier=ctx.notifier, settings=ctx.settings
    )
    for ticker in tickers:
        async with ctx.sessionmaker() as session:
            try:
                outcome = await ctx.analysis.analyze(session, ticker)
                await session.commit()
            except KalshiError as exc:
                await session.rollback()
                log.warning("analysis_failed", ticker=ticker, error=type(exc).__name__)
                continue
        stats["analyzed"] += 1
        if outcome is None or outcome.signal is None or outcome.signal.action != SignalAction.TRADE_IF_RISK_PASSES:
            continue
        stats["signals"] += 1
        async with ctx.sessionmaker() as session:
            users = await _eligible_users(session)
        for user, rs in users:
            modes = ([TradingMode.PAPER] if rs.paper_trading_enabled else []) + (
                [TradingMode.LIVE] if rs.live_trading_enabled else []
            )
            for mode in modes:
                async with ctx.sessionmaker() as session:  # isolated transaction per user per mode
                    try:
                        fresh_user = await session.get(User, user.id)
                        if fresh_user is None:
                            continue
                        res = await pipeline.execute(session, fresh_user, outcome.signal, mode)
                        await session.commit()
                        key = f"{mode.value}:{res.outcome.value}"
                        stats["executions"][key] = stats["executions"].get(key, 0) + 1
                    except Exception as exc:
                        await session.rollback()
                        log.error("execution_failed", user_id=str(user.id), mode=mode.value, error=type(exc).__name__)
        if ctx.notifier is not None:
            await _broadcast_signal(ctx, outcome)
    return stats


async def _broadcast_signal(ctx: JobContext, outcome: Any) -> None:
    """Send a TRADE signal once per user (dedup by signal key in Redis) to subscribers with alerts on."""
    sig, pred = outcome.signal, outcome.prediction
    text = render_signal(
        market=outcome.market.title or sig.market_ticker,
        ticker=sig.market_ticker,
        prob_yes=pred.probability_yes,
        side=sig.side.value,
        model_prob_side=sig.estimated_probability,
        market_prob_side=sig.market_probability,
        edge=sig.edge,
        confidence=sig.confidence_level.value,
        action=sig.action.value,
        supporting=pred.supporting,
        conflicting=pred.conflicting,
        data_age_seconds=pred.data_age_seconds if pred.data_age_seconds != float("inf") else None,
    )
    async with ctx.sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(User)
                    .join(RiskSettings, RiskSettings.user_id == User.id)
                    .where(
                        RiskSettings.notify_signals.is_(True),
                        User.status == UserStatus.ACTIVE.value,
                        User.telegram_chat_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for user in rows:
            if not (await get_access(session, user)).has(Feature.SIGNALS):
                continue
            if ctx.redis is not None:
                try:
                    if not await ctx.redis.set(f"sigsent:{sig.idempotency_key}:{user.id}", 1, ex=3600, nx=True):
                        continue
                except Exception:  # noqa: S112 - without dedup we skip rather than spam
                    continue
            assert ctx.notifier is not None
            await ctx.notifier.send(user.telegram_chat_id, text)  # type: ignore[arg-type]


class _MarketData(MarketDataProvider):
    def __init__(self, svc: KalshiMarketService):
        self.svc = svc

    async def get_market(self, ticker: str):
        return await self.svc.get_market(ticker)

    async def get_orderbook(self, ticker: str):
        return await self.svc.get_orderbook(ticker)


async def mark_and_settle(ctx: JobContext) -> dict[str, int]:
    """Mark open paper positions; settle paper trades and resolve predictions for finalized markets."""
    stats = {"marked": 0, "settled_markets": 0, "resolved_predictions": 0}
    async with ctx.sessionmaker() as session:
        tickers = set(
            (
                await session.execute(
                    select(Position.market_ticker).where(Position.status == PositionStatus.OPEN.value)
                )
            ).scalars()
        )
        tickers |= set(
            (
                await session.execute(
                    select(Prediction.market_ticker).where(
                        Prediction.outcome_yes.is_(None), Prediction.ts >= utcnow() - timedelta(days=7)
                    )
                )
            ).scalars()
        )
    for ticker in tickers:
        async with ctx.sessionmaker() as session:
            try:
                market = await ctx.market_service.get_market(ticker)
                if market.result in ("yes", "no"):
                    stats["settled_markets"] += 1
                    await settle_paper_market(session, ticker, market.result == "yes")
                    preds = (
                        (
                            await session.execute(
                                select(Prediction).where(
                                    Prediction.market_ticker == ticker, Prediction.outcome_yes.is_(None)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    for p in preds:
                        p.outcome_yes = market.result == "yes"
                        p.resolved_at = utcnow()
                    stats["resolved_predictions"] += len(preds)
                elif market.is_open:
                    await mark_paper_positions(session, ticker, await ctx.market_service.get_orderbook(ticker))
                    stats["marked"] += 1
                await session.commit()
            except KalshiError as exc:
                await session.rollback()
                log.warning("mark_settle_failed", ticker=ticker, error=type(exc).__name__)
    return stats


async def reconcile_live_orders(ctx: JobContext) -> int:
    """Resolve UNKNOWN/SUBMITTED live orders against the exchange (never resubmits)."""
    if ctx.broker_factory is None:
        return 0
    engine = LiveTradingEngine()
    fixed = 0
    async with ctx.sessionmaker() as session:
        orders = (
            (
                await session.execute(
                    select(Order).where(
                        Order.mode == TradingMode.LIVE.value,
                        Order.status.in_(
                            [OrderStatus.UNKNOWN.value, OrderStatus.SUBMITTED.value, OrderStatus.RESTING.value]
                        ),
                        Order.created_at >= utcnow() - timedelta(days=2),
                    )
                )
            )
            .scalars()
            .all()
        )
        for order in orders:
            user = await session.get(User, order.user_id)
            if user is None:
                continue
            broker = await ctx.broker_factory(session, user)
            try:
                before = order.status
                await engine.reconcile(session, order, broker)
                fixed += int(order.status != before)
            finally:
                await broker.aclose()
        await session.commit()
    return fixed


async def cancel_user_orders(ctx: JobContext, user_id: uuid.UUID) -> dict[str, int]:
    async with ctx.sessionmaker() as session:
        user = await session.get(User, user_id)
        if user is None:
            return {}
        stats = await cancel_open_orders(session, user, ctx.broker_factory)
        await session.commit()
    return stats


async def cancel_all(ctx: JobContext) -> int:
    async with ctx.sessionmaker() as session:
        user_ids = set(
            (
                await session.execute(
                    select(Order.user_id).where(
                        Order.status.in_(
                            [OrderStatus.RESTING.value, OrderStatus.SUBMITTED.value, OrderStatus.UNKNOWN.value]
                        )
                    )
                )
            ).scalars()
        )
    for uid in user_ids:
        await cancel_user_orders(ctx, uid)
    return len(user_ids)


async def expire(ctx: JobContext) -> int:
    async with ctx.sessionmaker() as session:
        n = await expire_due_subscriptions(session)
        await session.commit()
    return n


async def monitor(ctx: JobContext) -> dict[str, Any]:
    async with ctx.sessionmaker() as session:
        alerts = await run_checks(session)
        suspended = await apply_safety_actions(session, alerts)
        await session.commit()
    return {"alerts": [a.type for a in alerts], "suspended": suspended}


async def train(ctx: JobContext) -> str | None:
    async with ctx.sessionmaker() as session:
        row = await train_and_register_logistic(session, version=utcnow().strftime("%Y%m%d%H%M"))
        await session.commit()
        return row.version if row else None


async def signals_since(session: AsyncSession, minutes: int = 60) -> list[Signal]:
    return list(
        (await session.execute(select(Signal).where(Signal.ts >= utcnow() - timedelta(minutes=minutes)))).scalars()
    )
