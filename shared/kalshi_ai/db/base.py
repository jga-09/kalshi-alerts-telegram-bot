from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, MetaData, Numeric, String, TypeDecorator, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that is always stored/returned in UTC.

    PostgreSQL stores ``timestamptz``; SQLite (tests) drops tzinfo, so we
    re-attach UTC on the way out.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed; use timezone-aware UTC datetimes.")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class StrEnumType(TypeDecorator[str]):
    """Store StrEnum values as VARCHAR (portable, migration-friendly, no PG enum DDL churn)."""

    impl = String(32)
    cache_ok = True

    def __init__(self, enum_cls: type[Enum], length: int = 32):
        super().__init__(length)
        self.enum_cls = enum_cls

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        return self.enum_cls(value).value

    def process_result_value(self, value: str | None, dialect: Any) -> Any:
        if value is None:
            return None
        return self.enum_cls(value)


JSONType = JSON().with_variant(JSONB(), "postgresql")
BigIntPK = BigInteger().with_variant(Integer(), "sqlite")
Money = Numeric(18, 6, asdecimal=True)
Probability = Numeric(9, 6, asdecimal=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: UTCDateTime(),
        Decimal: Money,
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
        uuid.UUID: Uuid(),
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)


class UUIDPKMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
