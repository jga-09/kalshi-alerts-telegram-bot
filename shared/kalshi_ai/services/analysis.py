"""AnalysisService: one market -> context -> features -> prediction -> signal (all persisted)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.data.base import Candle
from kalshi_ai.data.base import DataPoint as DataPointDTO
from kalshi_ai.data.connectors import EconomicCalendarConnector
from kalshi_ai.data.health import health_snapshot
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import DataPoint, Market, MarketSnapshot, NewsArticle, OrderBookSnapshot, SocialPost
from kalshi_ai.features.engine import FeatureEngine, MarketContext, classify_market
from kalshi_ai.kalshi.errors import KalshiError
from kalshi_ai.kalshi.models import KalshiMarket, KalshiOrderBook
from kalshi_ai.kalshi.services import KalshiMarketService
from kalshi_ai.logging import get_logger
from kalshi_ai.modeling.prediction import PredictionEngine, PredictionResult, persist_prediction
from kalshi_ai.news.engine import Article
from kalshi_ai.signals.engine import SignalDecision, SignalEngine, persist_signal
from kalshi_ai.social.engine import Post

log = get_logger(__name__)
CandleSource = Callable[[str], Awaitable[list[Candle]]]


@dataclass
class AnalysisOutcome:
    prediction: PredictionResult
    signal: SignalDecision | None
    market: KalshiMarket


class AnalysisService:
    def __init__(
        self,
        market_service: KalshiMarketService,
        candle_source: CandleSource,
        prediction_engine: PredictionEngine | None = None,
        settings: Settings | None = None,
    ):
        self.markets = market_service
        self.candles = candle_source
        self.settings = settings or get_settings()
        self.features = FeatureEngine()
        self.predictor = prediction_engine or PredictionEngine()
        self.signals = SignalEngine(self.settings)

    async def build_context(
        self, session: AsyncSession, ticker: str, now: datetime | None = None
    ) -> tuple[MarketContext, KalshiOrderBook]:
        market = await self.markets.get_market(ticker)
        book = await self.markets.get_orderbook(ticker)
        try:
            trades = await self.markets.get_trades(
                ticker, limit=200, min_ts=int((utcnow() - timedelta(minutes=30)).timestamp())
            )
        except KalshiError:
            trades = []
        spec = classify_market(market)
        candles = {spec.underlying: await self.candles(spec.underlying)} if spec.underlying else {}
        # "As of" is the moment all live inputs are in hand; anything fetched later would be excluded.
        now = now or utcnow()
        point_rows = (
            (
                await session.execute(
                    select(DataPoint).where(DataPoint.ts >= now - timedelta(days=7), DataPoint.ts <= now)
                )
            )
            .scalars()
            .all()
        )
        news_rows = (
            (
                await session.execute(
                    select(NewsArticle).where(
                        NewsArticle.published_at >= now - timedelta(hours=48), NewsArticle.published_at <= now
                    )
                )
            )
            .scalars()
            .all()
        )
        social_rows = (
            (
                await session.execute(
                    select(SocialPost).where(
                        SocialPost.posted_at >= now - timedelta(hours=4), SocialPost.posted_at <= now
                    )
                )
            )
            .scalars()
            .all()
        )
        calendar = (await EconomicCalendarConnector(self.settings).fetch()).items
        ctx = MarketContext(
            as_of=now,
            market=market,
            orderbook=book,
            trades=trades,
            candles=candles,
            points=[DataPointDTO(r.source, r.symbol, r.metric, r.ts, r.value, r.reliability) for r in point_rows],
            articles=[_article(r) for r in news_rows],
            posts=[
                Post(
                    platform=r.platform,
                    external_id=r.external_id,
                    author=r.author,
                    text=r.text,
                    posted_at=r.posted_at,
                    engagement=r.engagement,
                    author_followers=r.author_followers,
                    author_age_days=r.author_age_days,
                    fingerprint=r.text_fingerprint,
                    sentiment=r.sentiment,
                    spam_score=r.spam_score,
                    bot_score=r.bot_score,
                    is_duplicate=r.is_duplicate,
                )
                for r in social_rows
            ],
            calendar=calendar,
            source_health=await health_snapshot(session),
            max_data_age_seconds=self.settings.max_data_age_seconds,
        )
        await self._record_market(session, market, book, spec.asset_class.value)
        return ctx, book

    async def analyze(self, session: AsyncSession, ticker: str, now: datetime | None = None) -> AnalysisOutcome | None:
        ctx, _ = await self.build_context(session, ticker, now)
        fv = self.features.build(ctx)
        headlines = [a.title for a in sorted(ctx.articles, key=lambda a: a.published_at, reverse=True)[:5]]
        pred = await self.predictor.predict(fv, headlines)
        if pred is None:
            return None
        row = await persist_prediction(session, pred)
        decision = self.signals.decide(pred)
        if decision is not None:
            await persist_signal(session, decision, row.id)
        return AnalysisOutcome(pred, decision, ctx.market)

    async def _record_market(self, session: AsyncSession, m: KalshiMarket, book: KalshiOrderBook, asset: str) -> None:
        row = (await session.execute(select(Market).where(Market.ticker == m.ticker))).scalar_one_or_none()
        if row is None:
            row = Market(ticker=m.ticker, status=m.status)
            session.add(row)
        row.event_ticker, row.title, row.yes_sub_title, row.status = m.event_ticker, m.title, m.yes_sub_title, m.status
        row.open_time, row.close_time, row.result, row.asset_class = m.open_time, m.close_time, m.result, asset
        row.series_ticker = m.raw.get("series_ticker") or m.ticker.split("-", 1)[0]
        session.add(
            MarketSnapshot(
                ticker=m.ticker,
                ts=book.fetched_at,
                yes_bid=m.yes_bid,
                yes_ask=m.yes_ask,
                no_bid=m.no_bid,
                no_ask=m.no_ask,
                last_price=m.last_price,
                volume=m.volume,
                open_interest=m.open_interest,
                status=m.status,
            )
        )
        session.add(
            OrderBookSnapshot(
                ticker=m.ticker,
                ts=book.fetched_at,
                yes_levels=[[str(lvl.price), str(lvl.size)] for lvl in book.yes_bids[:25]],
                no_levels=[[str(lvl.price), str(lvl.size)] for lvl in book.no_bids[:25]],
                best_yes_bid=book.best_yes_bid,
                best_yes_ask=book.best_yes_ask,
                spread=book.spread,
                yes_depth=sum((lvl.size for lvl in book.yes_bids), Decimal(0)),
                no_depth=sum((lvl.size for lvl in book.no_bids), Decimal(0)),
            )
        )
        await session.flush()


def _article(r: NewsArticle) -> Article:
    topics = r.topics or []
    return Article(
        source=r.source,
        url=r.url,
        title=r.title,
        summary=r.summary or "",
        published_at=r.published_at,
        url_hash=r.url_hash,
        fingerprint=r.content_fingerprint,
        reliability=r.reliability,
        cluster_id=r.cluster_id,
        is_duplicate=r.is_duplicate,
        sentiment=r.sentiment,
        relevance={t: r.relevance for t in topics},
        expected_direction=r.expected_direction,
        confidence=r.confidence,
        category=r.category,
    )
