"""Central configuration loaded from environment variables.

Every secret is a ``SecretStr`` so it is never rendered by ``repr()`` / logs.
Hard global risk limits live here and cannot be overridden by users.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class KalshiEnv(StrEnum):
    DEMO = "demo"
    PROD = "prod"


# Official hosts as published in Kalshi's OpenAPI spec (kalshi_python SDK 3.31.0).
KALSHI_HOSTS: dict[KalshiEnv, dict[str, str]] = {
    KalshiEnv.PROD: {
        "rest": "https://external-api.kalshi.com/trade-api/v2",
        "ws": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    },
    KalshiEnv.DEMO: {
        "rest": "https://external-api.demo.kalshi.co/trade-api/v2",
        "ws": "wss://demo-api.kalshi.co/trade-api/ws/v2",
    },
}


def _split_csv(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Application -------------------------------------------------------
    app_name: str = "Kalshi AI"
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"
    public_base_url: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:3000"
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:3000"])

    # --- Security ----------------------------------------------------------
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    jwt_access_ttl_minutes: int = 30
    # Comma-separated Fernet keys. First key encrypts; all keys decrypt (rotation).
    encryption_keys: SecretStr = SecretStr("")
    # Server-side pepper for HMAC hashing of activation codes.
    code_hash_pepper: SecretStr = SecretStr("dev-only-pepper-change-me")
    admin_telegram_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    activation_max_attempts: int = 5
    activation_window_seconds: int = 900
    api_rate_limit_per_minute: int = 120

    # --- Infrastructure ----------------------------------------------------
    database_url: str = "postgresql+asyncpg://kalshi:kalshi@localhost:5432/kalshi_ai"
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_bot_username: str = "KalshiAIBot"
    # Shared secret used by the bot when calling the internal API and for webhook mode.
    internal_api_token: SecretStr = SecretStr("dev-only-internal-token")
    telegram_webhook_secret: SecretStr = SecretStr("")

    # --- Stripe ------------------------------------------------------------
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")
    stripe_trial_days: int = 0
    stripe_price_signals_monthly: str = ""
    stripe_price_signals_yearly: str = ""
    stripe_price_pro_monthly: str = ""
    stripe_price_pro_yearly: str = ""
    stripe_price_auto_monthly: str = ""
    stripe_price_auto_yearly: str = ""
    stripe_price_premium_monthly: str = ""
    stripe_price_premium_yearly: str = ""

    # --- Kalshi ------------------------------------------------------------
    kalshi_env: KalshiEnv = KalshiEnv.DEMO
    kalshi_rest_base_url: str | None = None
    kalshi_ws_url: str | None = None
    kalshi_timeout_seconds: float = 10.0
    kalshi_max_retries: int = 2
    # Keys whose scopes allow moving money out (write / write::transfer) are rejected unless enabled.
    kalshi_allow_transfer_scope: bool = False

    # --- Data sources (all optional) --------------------------------------
    news_api_key: SecretStr = SecretStr("")
    fred_api_key: SecretStr = SecretStr("")
    reddit_client_id: SecretStr = SecretStr("")
    reddit_client_secret: SecretStr = SecretStr("")
    reddit_user_agent: str = "kalshi-ai/0.1 (contact: admin@example.com)"
    x_bearer_token: SecretStr = SecretStr("")
    rss_feeds: Annotated[list[str], NoDecode] = Field(default_factory=list)
    anthropic_api_key: SecretStr = SecretStr("")
    llm_model: str = "claude-sonnet-5-5"
    llm_enabled: bool = False

    # --- Trading defaults --------------------------------------------------
    paper_trading: bool = True
    # Global platform switch: when False NO live order can be placed by anyone.
    live_trading: bool = False
    min_edge: Decimal = Decimal("0.05")
    max_position_size: int = 50
    max_daily_loss: Decimal = Decimal("50")
    max_data_age_seconds: int = 120
    paper_starting_balance: Decimal = Decimal("1000")

    # --- Hard global limits (users can never exceed) -----------------------
    hard_max_order_contracts: int = 500
    hard_max_order_notional_usd: Decimal = Decimal("250")
    hard_max_daily_loss_usd: Decimal = Decimal("500")
    hard_max_trades_per_day: int = 100
    hard_min_edge: Decimal = Decimal("0.02")
    hard_min_confidence: Decimal = Decimal("0.50")
    hard_max_spread: Decimal = Decimal("0.10")
    hard_min_top_of_book_depth: int = 10
    hard_max_market_exposure_usd: Decimal = Decimal("1000")
    hard_min_price: Decimal = Decimal("0.02")
    hard_max_price: Decimal = Decimal("0.98")

    @field_validator("cors_origins", "rss_feeds", mode="before")
    @classmethod
    def _csv_list(cls, v: object) -> list[str]:
        return _split_csv(v)

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _csv_ints(cls, v: object) -> list[int]:
        return [int(x) for x in _split_csv(v)]

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.app_env == AppEnv.PRODUCTION:
            weak = []
            if "dev-only" in self.jwt_secret.get_secret_value() or len(self.jwt_secret.get_secret_value()) < 32:
                weak.append("JWT_SECRET")
            if not self.encryption_keys.get_secret_value():
                weak.append("ENCRYPTION_KEYS")
            if "dev-only" in self.code_hash_pepper.get_secret_value():
                weak.append("CODE_HASH_PEPPER")
            if "dev-only" in self.internal_api_token.get_secret_value():
                weak.append("INTERNAL_API_TOKEN")
            if weak:
                raise ValueError(f"Insecure production configuration; set strong values for: {', '.join(weak)}")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == AppEnv.PRODUCTION

    @property
    def kalshi_rest_url(self) -> str:
        return self.kalshi_rest_base_url or KALSHI_HOSTS[self.kalshi_env]["rest"]

    @property
    def kalshi_websocket_url(self) -> str:
        return self.kalshi_ws_url or KALSHI_HOSTS[self.kalshi_env]["ws"]

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    def stripe_price_id(self, plan: str, interval: str) -> str:
        return str(getattr(self, f"stripe_price_{plan.lower()}_{interval.lower()}", "") or "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
