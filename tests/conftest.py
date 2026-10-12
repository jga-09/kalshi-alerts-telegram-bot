"""Shared fixtures.

Unit/API tests run against SQLite (aiosqlite) + fakeredis so they need no services.
Integration tests use real PostgreSQL/Redis when TEST_DATABASE_URL / TEST_REDIS_URL are set.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import timedelta

from cryptography.fernet import Fernet

# Must be set before kalshi_ai.config.get_settings() is first called.
os.environ.update(
    {
        "APP_ENV": "test",
        "ENCRYPTION_KEYS": Fernet.generate_key().decode(),
        "JWT_SECRET": "test-jwt-secret-test-jwt-secret-0123456789",
        "CODE_HASH_PEPPER": "test-pepper",
        "INTERNAL_API_TOKEN": "test-internal-token",
        "TELEGRAM_BOT_TOKEN": "123456:TEST-telegram-token-abcdefghijklmnopqrstuvwxyz",
        "STRIPE_SECRET_KEY": "sk_test_dummy",
        "STRIPE_WEBHOOK_SECRET": "whsec_test_secret",
        "STRIPE_PRICE_PRO_MONTHLY": "price_pro_monthly",
        "STRIPE_PRICE_AUTO_MONTHLY": "price_auto_monthly",
        "STRIPE_PRICE_AUTO_YEARLY": "price_auto_yearly",
        "ADMIN_TELEGRAM_IDS": "999",
        "KALSHI_ENV": "demo",
        "KALSHI_REST_BASE_URL": "https://kalshi.test/trade-api/v2",
        "LIVE_TRADING": "true",
        "MIN_EDGE": "0.03",
        "MAX_POSITION_SIZE": "100",
        "MAX_DAILY_LOSS": "100",
        "LOG_LEVEL": "WARNING",
    }
)

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import kalshi_ai.db.models  # noqa: F401
from kalshi_ai.config import get_settings
from kalshi_ai.db.base import Base, utcnow
from kalshi_ai.db.models import Subscription, User
from kalshi_ai.db.session import set_sessionmaker
from kalshi_ai.domain.enums import (
    BillingInterval,
    PaymentStatus,
    Plan,
    SubscriptionSource,
    SubscriptionStatus,
    UserRole,
)
from kalshi_ai.security.tokens import create_access_token
from kalshi_ai.services.users import get_or_create_telegram_user


@pytest.fixture(autouse=True)
def _fresh_settings() -> None:
    get_settings.cache_clear()


def _sqlite_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection, _record) -> None:
        dbapi_connection.isolation_level = None  # let SQLAlchemy emit BEGIN/SAVEPOINT itself
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def _on_begin(conn) -> None:
        conn.exec_driver_sql("BEGIN")

    return engine


@pytest.fixture
async def engine() -> AsyncIterator:
    url = os.environ.get("TEST_DATABASE_URL")
    eng = create_async_engine(url) if url else _sqlite_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine) -> async_sessionmaker[AsyncSession]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(maker, engine)
    return maker


@pytest.fixture
async def session(sessionmaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.flushall()
    await r.aclose()


async def make_user(session: AsyncSession, telegram_id: int = 1001, *, admin: bool = False) -> User:
    user, _ = await get_or_create_telegram_user(session, telegram_id, chat_id=telegram_id, username=f"u{telegram_id}")
    if admin:
        user.role = UserRole.ADMIN
    await session.flush()
    return user


async def give_subscription(session: AsyncSession, user: User, plan: Plan = Plan.AUTO, days: int = 30) -> Subscription:
    sub = Subscription(
        user_id=user.id,
        plan=plan,
        interval=BillingInterval.MONTHLY,
        status=SubscriptionStatus.ACTIVE,
        source=SubscriptionSource.ADMIN,
        started_at=utcnow(),
        expires_at=utcnow() + timedelta(days=days),
        payment_status=PaymentStatus.PAID,
    )
    session.add(sub)
    await session.flush()
    return sub


@pytest.fixture
async def user(session: AsyncSession) -> User:
    u = await make_user(session, 1001)
    await session.commit()
    return u


@pytest.fixture
async def admin_user(session: AsyncSession) -> User:
    u = await make_user(session, 999, admin=True)
    await session.commit()
    return u


def auth_headers(user: User, role: str = "customer") -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role)}"}


@pytest.fixture
async def app(sessionmaker, redis):
    from kalshi_ai_api.main import create_app

    application = create_app(get_settings(), manage_resources=False)
    application.state.redis = redis
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c
