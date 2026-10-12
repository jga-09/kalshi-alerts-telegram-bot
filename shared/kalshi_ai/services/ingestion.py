"""Ingestion: run connectors, record source health, persist normalized data."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.data.base import Candle, DataConnector, FetchResult
from kalshi_ai.data.health import record_fetch
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import DataPoint as DataPointRow
from kalshi_ai.db.models import NewsArticle, SocialPost
from kalshi_ai.logging import get_logger
from kalshi_ai.news.engine import NewsEngine
from kalshi_ai.social.engine import SocialAnalyzer, parse_post

log = get_logger(__name__)
CANDLE_KEY = "candles:{symbol}"
CANDLE_TTL = 900


async def store_candles(redis: Redis, symbol: str, candles: list[Candle]) -> None:
    payload = json.dumps([[c.ts.timestamp(), c.open, c.high, c.low, c.close, c.volume] for c in candles[-500:]])
    try:
        await redis.set(CANDLE_KEY.format(symbol=symbol), payload, ex=CANDLE_TTL)
    except (RedisError, OSError) as exc:
        log.warning("candle_cache_write_failed", symbol=symbol, error=type(exc).__name__)


async def load_candles(redis: Redis, symbol: str) -> list[Candle]:
    try:
        raw = await redis.get(CANDLE_KEY.format(symbol=symbol))
    except (RedisError, OSError):
        return []  # Redis outage => no candles => lower confidence (never invented data)
    if not raw:
        return []
    return [Candle(datetime.fromtimestamp(r[0], UTC), *r[1:]) for r in json.loads(raw)]


async def persist_news(session: AsyncSession, items: list[dict[str, Any]], now: datetime) -> int:
    articles = NewsEngine().process(items, now)
    if not articles:
        return 0
    hashes = [a.url_hash for a in articles]
    existing = set(
        (await session.execute(select(NewsArticle.url_hash).where(NewsArticle.url_hash.in_(hashes)))).scalars()
    )
    added = 0
    for a in articles:
        if a.url_hash in existing:
            continue
        session.add(
            NewsArticle(
                source=a.source,
                url=a.url,
                url_hash=a.url_hash,
                title=a.title,
                summary=a.summary,
                published_at=a.published_at,
                content_fingerprint=a.fingerprint,
                cluster_id=a.cluster_id,
                is_duplicate=a.is_duplicate,
                category=a.category,
                sentiment=a.sentiment,
                relevance=max(a.relevance.values() or [0.0]),
                expected_direction=a.expected_direction,
                confidence=a.confidence,
                topics=[k for k, v in a.relevance.items() if v >= 0.3],
                reliability=a.reliability,
            )
        )
        added += 1
    await session.flush()
    return added


async def persist_social(session: AsyncSession, items: list[dict[str, Any]]) -> int:
    posts = [p for p in (parse_post(i) for i in items) if p is not None]
    posts = SocialAnalyzer().enrich(posts)
    added = 0
    for p in posts:
        exists = (
            await session.execute(
                select(SocialPost.id).where(SocialPost.platform == p.platform, SocialPost.external_id == p.external_id)
            )
        ).scalar_one_or_none()
        if exists:
            continue
        session.add(
            SocialPost(
                platform=p.platform,
                external_id=p.external_id,
                author=p.author,
                author_followers=p.author_followers,
                author_age_days=p.author_age_days,
                text=p.text,
                text_fingerprint=p.fingerprint,
                posted_at=p.posted_at,
                engagement=p.engagement,
                sentiment=p.sentiment,
                spam_score=p.spam_score,
                bot_score=p.bot_score,
                is_duplicate=p.is_duplicate or p.coordinated,
            )
        )
        added += 1
    await session.flush()
    return added


async def run_connector(session: AsyncSession, redis: Redis | None, connector: DataConnector) -> FetchResult:
    result = await connector.fetch()
    await record_fetch(session, result, connector.stale_after_seconds)
    if not result.ok:
        return result
    now = utcnow()
    for p in result.points:
        session.add(
            DataPointRow(
                source=p.source,
                symbol=p.symbol,
                metric=p.metric,
                ts=p.ts,
                value=p.value,
                reliability=p.reliability,
                extra=p.extra or None,
            )
        )
    if redis is not None:
        for symbol, candles in result.candles.items():
            await store_candles(redis, symbol, candles)
    if result.items:
        if connector.name in ("reddit", "x_recent_search"):
            await persist_social(session, result.items)
        elif connector.name != "economic_calendar":
            await persist_news(session, result.items, now)
    await session.flush()
    return result
