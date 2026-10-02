"""Celery task wrappers. Each run builds its own engine/clients inside a fresh event loop."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from kalshi_ai.config import get_settings
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.services import KalshiMarketService
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.modeling.registry import ModelRegistry
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.services.analysis import AnalysisService
from kalshi_ai.services.ingestion import load_candles
from kalshi_ai.services.kalshi_connections import get_connection
from kalshi_ai.trading.live import KalshiBroker
from kalshi_ai_worker import jobs
from kalshi_ai_worker.celery_app import app


@asynccontextmanager
async def job_context():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    client = KalshiClient(settings.kalshi_rest_url, timeout=settings.kalshi_timeout_seconds)
    market_service = KalshiMarketService(client)
    cipher = SecretCipher.from_settings(settings)

    async def candles(symbol: str):
        return await load_candles(redis, symbol)

    async def broker_factory(session, user):
        conn = await get_connection(session, user.id)
        if conn is None:
            raise RuntimeError("Kalshi not connected")
        return KalshiBroker.for_connection(conn, cipher)

    notifier = None
    token = settings.telegram_bot_token.get_secret_value()
    bot = None
    if token:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties

        from kalshi_ai_bot.notifier import TelegramNotifier

        bot = Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))
        notifier = TelegramNotifier(bot)
    async with maker() as s:
        registry = await ModelRegistry.load(s)
    ctx = jobs.JobContext(
        sessionmaker=maker,
        redis=redis,
        market_service=market_service,
        analysis=AnalysisService(market_service, candles, PredictionEngine(registry), settings),
        broker_factory=broker_factory,
        notifier=notifier,
        settings=settings,
    )
    try:
        yield ctx
    finally:
        await client.aclose()
        await redis.aclose()
        if bot is not None:
            await bot.session.close()
        await engine.dispose()


def _run[T](fn: Callable[[jobs.JobContext], Awaitable[T]]) -> T:
    async def runner() -> T:
        async with job_context() as ctx:
            return await fn(ctx)

    return asyncio.run(runner())


@app.task(name="kalshi_ai_worker.tasks.ingest_data")
def ingest_data() -> dict[str, bool]:
    return _run(jobs.ingest_data)


@app.task(name="kalshi_ai_worker.tasks.scan_markets")
def scan_markets() -> dict[str, Any]:
    return _run(jobs.scan_markets)


@app.task(name="kalshi_ai_worker.tasks.mark_and_settle")
def mark_and_settle() -> dict[str, int]:
    return _run(jobs.mark_and_settle)


@app.task(name="kalshi_ai_worker.tasks.reconcile_live_orders")
def reconcile_live_orders() -> int:
    return _run(jobs.reconcile_live_orders)


@app.task(name="kalshi_ai_worker.tasks.expire_subscriptions")
def expire_subscriptions() -> int:
    return _run(jobs.expire)


@app.task(name="kalshi_ai_worker.tasks.monitor")
def monitor() -> dict[str, Any]:
    return _run(jobs.monitor)


@app.task(name="kalshi_ai_worker.tasks.train_models")
def train_models() -> str | None:
    return _run(jobs.train)


@app.task(name="kalshi_ai_worker.tasks.cancel_user_open_orders", acks_late=True, max_retries=5)
def cancel_user_open_orders(user_id: str) -> dict[str, int]:
    return _run(lambda ctx: jobs.cancel_user_orders(ctx, uuid.UUID(user_id)))


@app.task(name="kalshi_ai_worker.tasks.cancel_all_open_orders")
def cancel_all_open_orders() -> int:
    return _run(jobs.cancel_all)
