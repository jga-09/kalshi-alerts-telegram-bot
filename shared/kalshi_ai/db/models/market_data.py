"""Market data, news, social and generic time-series data points."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kalshi_ai.db.base import Base, BigIntPK, Probability, StrEnumType, TimestampMixin, utcnow
from kalshi_ai.domain.enums import AssetClass, NewsCategory


class Market(TimestampMixin, Base):
    __tablename__ = "markets"
    __table_args__ = (
        Index("ix_markets_status_close", "status", "close_time"),
        Index("ix_markets_series", "series_ticker"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    event_ticker: Mapped[str | None] = mapped_column(String(128), index=True)
    series_ticker: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(500))
    yes_sub_title: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_class: Mapped[AssetClass] = mapped_column(StrEnumType(AssetClass), default=AssetClass.OTHER, nullable=False)
    open_time: Mapped[datetime | None] = mapped_column()
    close_time: Mapped[datetime | None] = mapped_column()
    result: Mapped[str | None] = mapped_column(String(16))
    tick_size: Mapped[Decimal | None] = mapped_column(Probability)
    raw: Mapped[dict[str, Any] | None] = mapped_column()


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    __table_args__ = (Index("ix_market_snapshots_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    yes_bid: Mapped[Decimal | None] = mapped_column(Probability)
    yes_ask: Mapped[Decimal | None] = mapped_column(Probability)
    no_bid: Mapped[Decimal | None] = mapped_column(Probability)
    no_ask: Mapped[Decimal | None] = mapped_column(Probability)
    last_price: Mapped[Decimal | None] = mapped_column(Probability)
    volume: Mapped[Decimal | None] = mapped_column()
    open_interest: Mapped[Decimal | None] = mapped_column()
    status: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), default="kalshi", nullable=False)


class OrderBookSnapshot(Base):
    __tablename__ = "order_books"
    __table_args__ = (Index("ix_order_books_ticker_ts", "ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    yes_levels: Mapped[list[Any]] = mapped_column(nullable=False)  # [[price, size], ...] bids for YES
    no_levels: Mapped[list[Any]] = mapped_column(nullable=False)  # [[price, size], ...] bids for NO
    best_yes_bid: Mapped[Decimal | None] = mapped_column(Probability)
    best_yes_ask: Mapped[Decimal | None] = mapped_column(Probability)
    spread: Mapped[Decimal | None] = mapped_column(Probability)
    yes_depth: Mapped[Decimal | None] = mapped_column()
    no_depth: Mapped[Decimal | None] = mapped_column()
    imbalance: Mapped[float | None] = mapped_column(Float)


class DataPoint(Base):
    """Generic external time-series point (BTC spot, DXY, yields, funding...)."""

    __tablename__ = "data_points"
    __table_args__ = (Index("ix_data_points_symbol_ts", "symbol", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False, default="price")
    ts: Mapped[datetime] = mapped_column(nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    reliability: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    collected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    extra: Mapped[dict[str, Any] | None] = mapped_column()


class SourceHealth(TimestampMixin, Base):
    __tablename__ = "source_health"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | degraded | stale | down | disabled
    last_success_at: Mapped[datetime | None] = mapped_column()
    last_error_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(500))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class NewsArticle(Base):
    __tablename__ = "news"
    __table_args__ = (
        Index("ix_news_published_at", "published_at"),
        Index("ix_news_cluster", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(nullable=False)
    collected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    cluster_id: Mapped[str] = mapped_column(String(64), nullable=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    category: Mapped[NewsCategory] = mapped_column(StrEnumType(NewsCategory), nullable=False)
    sentiment: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    relevance: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    expected_direction: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # -1, 0, +1
    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    topics: Mapped[list[Any] | None] = mapped_column()
    reliability: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="platform_external"),
        Index("ix_social_posts_posted_at", "posted_at"),
    )

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    author: Mapped[str | None] = mapped_column(String(128))
    author_followers: Mapped[int | None] = mapped_column(Integer)
    author_age_days: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    posted_at: Mapped[datetime] = mapped_column(nullable=False)
    collected_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    engagement: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sentiment: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    spam_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bot_score: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    topics: Mapped[list[Any] | None] = mapped_column()


class SystemEvent(Base):
    __tablename__ = "system_events"
    __table_args__ = (Index("ix_system_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column()


class SystemFlag(Base):
    """Durable global switches (e.g. global kill switch). Cached in Redis."""

    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
