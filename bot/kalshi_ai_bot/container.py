"""Dependency container shared by bot handlers (injected via aiogram workflow data)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import User
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.models import KalshiMarket
from kalshi_ai.kalshi.services import KalshiMarketService
from kalshi_ai.security.crypto import SecretCipher
from kalshi_ai.services.analysis import AnalysisService
from kalshi_ai.services.ingestion import load_candles
from kalshi_ai.services.kalshi_connections import get_connection
from kalshi_ai.trading.live import KalshiBroker, LiveBroker


@dataclass
class BotContainer:
    settings: Settings
    redis: Redis
    market_service: KalshiMarketService
    analysis: AnalysisService
    cipher: SecretCipher | None
    _market_cache: tuple[Any, list[KalshiMarket]] | None = field(default=None, repr=False)

    @classmethod
    def build(cls, settings: Settings, redis: Redis) -> BotContainer:
        client = KalshiClient(settings.kalshi_rest_url, timeout=settings.kalshi_timeout_seconds)
        market_service = KalshiMarketService(client)

        async def candles(symbol: str):
            return await load_candles(redis, symbol)

        try:
            cipher: SecretCipher | None = SecretCipher.from_settings(settings)
        except Exception:
            cipher = None
        return cls(settings, redis, market_service, AnalysisService(market_service, candles, settings=settings), cipher)

    async def featured_markets(self, limit: int = 10) -> list[KalshiMarket]:
        """Open markets for supported asset classes, cached for 60s to respect Kalshi rate limits."""
        if self._market_cache and utcnow() - self._market_cache[0] < timedelta(seconds=60):
            return self._market_cache[1][:limit]
        markets: list[KalshiMarket] = []
        for series in self.settings.featured_series:
            try:
                markets.extend(await self.market_service.list_markets(series_ticker=series, limit=5, max_pages=1))
            except Exception:  # noqa: S112 - a missing series must not break the menu
                continue
        markets = [m for m in markets if m.is_open]
        self._market_cache = (utcnow(), markets)
        return markets[:limit]

    async def broker_for(self, session: AsyncSession, user: User) -> LiveBroker:
        """Build a broker from THIS user's own encrypted credential only."""
        if self.cipher is None:
            raise RuntimeError("Encryption keys not configured")
        conn = await get_connection(session, user.id)
        if conn is None:
            raise RuntimeError("Kalshi not connected")
        return KalshiBroker.for_connection(conn, self.cipher)
