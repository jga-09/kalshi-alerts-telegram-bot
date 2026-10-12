from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from kalshi_ai.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def create_engine(url: str | None = None) -> AsyncEngine:
    url = url or get_settings().database_url
    kwargs: dict[str, object] = {"pool_pre_ping": True}
    if not url.startswith("sqlite"):
        kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
    return create_async_engine(url, **kwargs)


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_engine(url)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def set_sessionmaker(maker: async_sessionmaker[AsyncSession], engine: AsyncEngine | None = None) -> None:
    """Used by tests / workers to inject a configured sessionmaker."""
    global _sessionmaker, _engine
    _sessionmaker = maker
    if engine is not None:
        _engine = engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    session = get_sessionmaker()()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_database() -> bool:
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True
