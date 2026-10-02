"""Features, models, predictions, signals, orders, positions, risk settings."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kalshi_ai.db.base import (
    Base,
    BigIntPK,
    Probability,
    StrEnumType,
    TimestampMixin,
    UUIDPKMixin,
    utcnow,
)
from kalshi_ai.domain.enums import (
    ConfidenceLevel,
    ModelStatus,
    OrderStatus,
    PositionStatus,
    RiskMode,
    Side,
    SignalAction,
    TradingMode,
)


class FeatureSnapshot(Base):
    """Reproducible, immutable feature vector used for one prediction."""

    __tablename__ = "features"
    __table_args__ = (Index("ix_features_ticker_ts", "market_ticker", "ts"),)

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)  # "as of" time - no data after this is allowed
    feature_set_version: Mapped[str] = mapped_column(String(32), nullable=False)
    features: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    data_versions: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    feature_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class ModelVersion(UUIDPKMixin, Base):
    __tablename__ = "model_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="name_version"),)

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[ModelStatus] = mapped_column(StrEnumType(ModelStatus), default=ModelStatus.SHADOW, nullable=False)
    params: Mapped[dict[str, Any] | None] = mapped_column()
    metrics: Mapped[dict[str, Any] | None] = mapped_column()
    artifact: Mapped[dict[str, Any] | None] = mapped_column()  # e.g. logistic coefficients
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Prediction(UUIDPKMixin, Base):
    __tablename__ = "predictions"
    __table_args__ = (
        Index("ix_predictions_ticker_ts", "market_ticker", "ts"),
        Index("ix_predictions_resolved", "resolved_at"),
    )

    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    feature_id: Mapped[int | None] = mapped_column(ForeignKey("features.id"))
    model_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("model_versions.id"))
    model_version: Mapped[str] = mapped_column(String(96), nullable=False)
    data_versions: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    probability_yes: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    market_probability_yes: Mapped[Decimal | None] = mapped_column(Probability)
    market_price_yes_ask: Mapped[Decimal | None] = mapped_column(Probability)
    market_price_no_ask: Mapped[Decimal | None] = mapped_column(Probability)
    edge: Mapped[Decimal | None] = mapped_column(Probability)
    data_freshness_seconds: Mapped[float | None] = mapped_column(Float)
    analysis: Mapped[dict[str, Any] | None] = mapped_column()
    outcome_yes: Mapped[bool | None] = mapped_column(Boolean)
    resolved_at: Mapped[datetime | None] = mapped_column()


class Signal(UUIDPKMixin, Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signals_ticker_ts", "market_ticker", "ts"),)

    prediction_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("predictions.id"), nullable=False)
    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    ts: Mapped[datetime] = mapped_column(nullable=False)
    side: Mapped[Side] = mapped_column(StrEnumType(Side), nullable=False)
    action: Mapped[SignalAction] = mapped_column(StrEnumType(SignalAction), nullable=False)
    estimated_probability: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    market_probability: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    edge: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_level: Mapped[ConfidenceLevel] = mapped_column(StrEnumType(ConfidenceLevel), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    model_version: Mapped[str] = mapped_column(String(96), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    reasons: Mapped[dict[str, Any] | None] = mapped_column()


class RiskSettings(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "risk_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    risk_mode: Mapped[RiskMode] = mapped_column(StrEnumType(RiskMode), default=RiskMode.LOW, nullable=False)
    # User may only make limits *stricter* than the risk-mode profile.
    overrides: Mapped[dict[str, Any] | None] = mapped_column()
    paper_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    live_trading_confirmed_at: Mapped[datetime | None] = mapped_column()
    risk_settings_confirmed_at: Mapped[datetime | None] = mapped_column()
    # Hash of the risk parameters the user confirmed; changes require re-confirmation.
    confirmed_risk_hash: Mapped[str | None] = mapped_column(String(64))
    kill_switch_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    kill_switch_activated_at: Mapped[datetime | None] = mapped_column()
    admin_auto_trading_disabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    market_allowlist: Mapped[list[Any] | None] = mapped_column()
    notify_signals: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PaperAccount(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "paper_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    starting_balance: Mapped[Decimal] = mapped_column(nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(nullable=False)


class Order(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        # Idempotency: one order per (user, signal, mode). A replayed signal cannot create a second trade.
        UniqueConstraint("user_id", "signal_id", "mode", name="user_signal_mode"),
        Index("ix_orders_user_created", "user_id", "created_at"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_user_market", "user_id", "market_ticker"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signals.id"))
    mode: Mapped[TradingMode] = mapped_column(StrEnumType(TradingMode), nullable=False)
    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[Side] = mapped_column(StrEnumType(Side), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False, default="buy")
    book_side: Mapped[str | None] = mapped_column(String(8))  # Kalshi V2 bid/ask
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal] = mapped_column(Probability, nullable=False)  # price paid per contract of `side`
    yes_price: Mapped[Decimal] = mapped_column(Probability, nullable=False)  # price on the YES book
    status: Mapped[OrderStatus] = mapped_column(StrEnumType(OrderStatus), nullable=False)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    kalshi_order_id: Mapped[str | None] = mapped_column(String(64), index=True)
    filled_quantity: Mapped[Decimal] = mapped_column(default=Decimal(0), nullable=False)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Probability)
    fees: Mapped[Decimal] = mapped_column(default=Decimal(0), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    risk_mode: Mapped[RiskMode | None] = mapped_column(StrEnumType(RiskMode))
    model_version: Mapped[str | None] = mapped_column(String(96))
    estimated_probability: Mapped[Decimal | None] = mapped_column(Probability)
    market_probability: Mapped[Decimal | None] = mapped_column(Probability)
    edge: Mapped[Decimal | None] = mapped_column(Probability)
    submitted_at: Mapped[datetime | None] = mapped_column()
    validation: Mapped[dict[str, Any] | None] = mapped_column()


class Position(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("user_id", "mode", "market_ticker", "side", name="user_mode_market_side"),
        Index("ix_positions_user_status", "user_id", "status"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    mode: Mapped[TradingMode] = mapped_column(StrEnumType(TradingMode), nullable=False)
    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[Side] = mapped_column(StrEnumType(Side), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    avg_price: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    cost_basis: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    mark_price: Mapped[Decimal | None] = mapped_column()
    realized_pnl: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    unrealized_pnl: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    status: Mapped[PositionStatus] = mapped_column(
        StrEnumType(PositionStatus), default=PositionStatus.OPEN, nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column()


class PaperTrade(UUIDPKMixin, Base):
    __tablename__ = "paper_trades"
    __table_args__ = (Index("ix_paper_trades_user_opened", "user_id", "opened_at"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"), unique=True, nullable=False)
    market_ticker: Mapped[str] = mapped_column(String(128), nullable=False)
    side: Mapped[Side] = mapped_column(StrEnumType(Side), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Probability, nullable=False)
    fees: Mapped[Decimal] = mapped_column(nullable=False, default=Decimal(0))
    exit_price: Mapped[Decimal | None] = mapped_column(Probability)
    pnl: Mapped[Decimal | None] = mapped_column()
    status: Mapped[PositionStatus] = mapped_column(
        StrEnumType(PositionStatus), default=PositionStatus.OPEN, nullable=False
    )
    estimated_probability: Mapped[Decimal | None] = mapped_column(Probability)
    edge: Mapped[Decimal | None] = mapped_column(Probability)
    risk_mode: Mapped[RiskMode | None] = mapped_column(StrEnumType(RiskMode))
    opened_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column()
