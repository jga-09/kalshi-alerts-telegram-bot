"""Users, subscriptions, plans, Stripe records and activation codes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kalshi_ai.db.base import Base, StrEnumType, TimestampMixin, UUIDPKMixin, utcnow
from kalshi_ai.domain.enums import (
    ActivationCodeStatus,
    BillingInterval,
    KalshiConnectionStatus,
    KalshiEnvironment,
    PaymentStatus,
    Plan,
    SubscriptionSource,
    SubscriptionStatus,
    UserRole,
    UserStatus,
)


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[UserRole] = mapped_column(StrEnumType(UserRole), default=UserRole.CUSTOMER, nullable=False)
    status: Mapped[UserStatus] = mapped_column(StrEnumType(UserStatus), default=UserStatus.ACTIVE, nullable=False)
    suspended_reason: Mapped[str | None] = mapped_column(String(500))
    disclosures_accepted_at: Mapped[datetime | None] = mapped_column()
    disclosures_version: Mapped[str | None] = mapped_column(String(32))
    last_seen_at: Mapped[datetime | None] = mapped_column()

    subscriptions: Mapped[list[Subscription]] = relationship(back_populates="user", lazy="raise")


class SubscriptionPlan(UUIDPKMixin, TimestampMixin, Base):
    """Database-configurable price list. Seeded from env (STRIPE_PRICE_*)."""

    __tablename__ = "subscription_plans"
    __table_args__ = (UniqueConstraint("plan", "interval", name="plan_interval"),)

    plan: Mapped[Plan] = mapped_column(StrEnumType(Plan), nullable=False)
    interval: Mapped[BillingInterval] = mapped_column(StrEnumType(BillingInterval), nullable=False)
    stripe_price_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="usd")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    display_name: Mapped[str | None] = mapped_column(String(64))


class Subscription(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_user_status", "user_id", "status"),
        Index("ix_subscriptions_expires_at", "expires_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan: Mapped[Plan] = mapped_column(StrEnumType(Plan), nullable=False)
    interval: Mapped[BillingInterval] = mapped_column(StrEnumType(BillingInterval), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(StrEnumType(SubscriptionStatus), nullable=False)
    source: Mapped[SubscriptionSource] = mapped_column(StrEnumType(SubscriptionSource), nullable=False)
    started_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    payment_status: Mapped[PaymentStatus] = mapped_column(
        StrEnumType(PaymentStatus), default=PaymentStatus.NONE, nullable=False
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column()
    activation_code_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("activation_codes.id"))

    user: Mapped[User] = relationship(back_populates="subscriptions", lazy="raise")


class StripeCustomer(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "stripe_customers"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    stripe_customer_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)


class StripeEvent(Base):
    """Processed Stripe webhook events - guarantees idempotent handling."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    type: Mapped[str] = mapped_column(String(128), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class ActivationCode(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "activation_codes"
    __table_args__ = (Index("ix_activation_codes_status", "status"),)

    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # First characters only, for admins to identify a code without storing it.
    code_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    plan: Mapped[Plan] = mapped_column(StrEnumType(Plan), nullable=False)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column()
    status: Mapped[ActivationCodeStatus] = mapped_column(
        StrEnumType(ActivationCodeStatus), default=ActivationCodeStatus.ACTIVE, nullable=False
    )
    max_uses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_uses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    revoked_at: Mapped[datetime | None] = mapped_column()
    note: Mapped[str | None] = mapped_column(String(255))


class ActivationCodeRedemption(UUIDPKMixin, Base):
    __tablename__ = "activation_code_redemptions"
    __table_args__ = (UniqueConstraint("code_id", "user_id", name="code_user"),)

    code_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activation_codes.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("subscriptions.id"))
    redeemed_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class KalshiConnection(UUIDPKMixin, TimestampMixin, Base):
    """One isolated Kalshi API credential per customer, encrypted at rest."""

    __tablename__ = "kalshi_connections"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    environment: Mapped[KalshiEnvironment] = mapped_column(StrEnumType(KalshiEnvironment), nullable=False)
    encrypted_api_key_id: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    encrypted_private_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_id_hint: Mapped[str] = mapped_column(String(16), nullable=False)
    public_key_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    key_type: Mapped[str] = mapped_column(String(16), nullable=False)
    scopes: Mapped[list[Any] | None] = mapped_column()
    status: Mapped[KalshiConnectionStatus] = mapped_column(
        StrEnumType(KalshiConnectionStatus), default=KalshiConnectionStatus.PENDING, nullable=False
    )
    last_verified_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String(255))


class ConnectToken(UUIDPKMixin, Base):
    """Single-use, short-lived token that lets a Telegram user open the secure web
    form to submit Kalshi credentials (so secrets never travel through Telegram)."""

    __tablename__ = "connect_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, default="kalshi_connect")
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    used_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_actor", "actor_user_id"),
        Index("ix_audit_logs_action", "action"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)  # user | admin | system
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(64))
    target_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow, nullable=False)


class Disclosure(UUIDPKMixin, TimestampMixin, Base):
    """Configurable legal pages (terms, privacy, risk, subscription terms)."""

    __tablename__ = "disclosures"
    __table_args__ = (UniqueConstraint("slug", "version", name="slug_version"),)

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
