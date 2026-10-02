from __future__ import annotations

from enum import StrEnum


class UserRole(StrEnum):
    CUSTOMER = "customer"
    SUPPORT = "support"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class Plan(StrEnum):
    SIGNALS = "signals"
    PRO = "pro"
    AUTO = "auto"
    PREMIUM = "premium"


class BillingInterval(StrEnum):
    MONTHLY = "monthly"
    YEARLY = "yearly"
    CUSTOM = "custom"


class SubscriptionStatus(StrEnum):
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    INCOMPLETE = "incomplete"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class SubscriptionSource(StrEnum):
    STRIPE = "stripe"
    ACCESS_CODE = "access_code"
    ADMIN = "admin"


class PaymentStatus(StrEnum):
    NONE = "none"
    PAID = "paid"
    PENDING = "pending"
    FAILED = "failed"
    REFUNDED = "refunded"


class ActivationCodeStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXHAUSTED = "exhausted"
    EXPIRED = "expired"


class KalshiEnvironment(StrEnum):
    DEMO = "demo"
    PROD = "prod"


class KalshiConnectionStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    INVALID = "invalid"
    DISCONNECTED = "disconnected"


class RiskMode(StrEnum):
    LOW = "low"
    PASSIVE = "passive"
    RISKY = "risky"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    YES = "yes"
    NO = "no"


class SignalAction(StrEnum):
    TRADE_IF_RISK_PASSES = "trade_if_risk_passes"
    WATCH = "watch"
    NO_TRADE = "no_trade"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class OrderStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"  # blocked by our own validation, never sent
    SUBMITTED = "submitted"
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    FAILED = "failed"  # exchange/API error
    UNKNOWN = "unknown"  # timeout - must reconcile before any retry


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"


class NewsCategory(StrEnum):
    BREAKING = "breaking"
    RECENT = "recent"
    BACKGROUND = "background"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ModelStatus(StrEnum):
    ACTIVE = "active"
    SHADOW = "shadow"
    RETIRED = "retired"


class AssetClass(StrEnum):
    CRYPTO = "crypto"
    GOLD = "gold"
    MACRO = "macro"
    OTHER = "other"
