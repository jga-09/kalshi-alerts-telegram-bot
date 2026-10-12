"""Alembic environment - async engine, URL from DATABASE_URL env var."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection

import kalshi_ai.db.models  # noqa: F401  (registers all tables)
from kalshi_ai.config import get_settings
from kalshi_ai.db.base import Base, StrEnumType, UTCDateTime
from kalshi_ai.db.session import create_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return config.attributes.get("database_url") or get_settings().database_url


def render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """Render app-level TypeDecorators as plain SQL types so migrations are self-contained."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.impl.length})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=render_item,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_engine(_url())
    async with engine.connect() as connection:
        await connection.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
