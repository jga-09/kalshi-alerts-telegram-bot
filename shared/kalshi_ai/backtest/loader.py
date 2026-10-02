"""Build a HistoricalDataset from data recorded by the live system (market_snapshots, order_books, news...)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.backtest.engine import HistoricalDataset, HistoricalFrame
from kalshi_ai.data.base import DataPoint as DataPointDTO
from kalshi_ai.db.models import DataPoint, Market, MarketSnapshot, NewsArticle, OrderBookSnapshot
from kalshi_ai.kalshi.models import KalshiMarket, KalshiOrderBook, OrderBookLevel
from kalshi_ai.services.analysis import _article


async def load_dataset(
    session: AsyncSession, start: datetime, end: datetime, series: str | None = None
) -> HistoricalDataset:
    mq = select(Market).where(Market.close_time >= start, Market.close_time <= end, Market.result.in_(["yes", "no"]))
    if series:
        mq = mq.where(Market.series_ticker == series)
    markets = {m.ticker: m for m in (await session.execute(mq)).scalars()}
    frames: dict[str, list[HistoricalFrame]] = {}
    for ticker, m in markets.items():
        books = (
            (
                await session.execute(
                    select(OrderBookSnapshot).where(OrderBookSnapshot.ticker == ticker).order_by(OrderBookSnapshot.ts)
                )
            )
            .scalars()
            .all()
        )
        snaps = {
            s.ts: s
            for s in (await session.execute(select(MarketSnapshot).where(MarketSnapshot.ticker == ticker))).scalars()
        }
        out = []
        for b in books:
            snap = snaps.get(b.ts)
            km = KalshiMarket(
                ticker=ticker,
                event_ticker=m.event_ticker,
                title=m.title,
                status=snap.status if snap and snap.status else "active",
                open_time=m.open_time,
                close_time=m.close_time,
                raw=m.raw or {},
            )
            book = KalshiOrderBook(
                ticker=ticker,
                yes_bids=[OrderBookLevel(price=Decimal(p), size=Decimal(s)) for p, s in b.yes_levels],
                no_bids=[OrderBookLevel(price=Decimal(p), size=Decimal(s)) for p, s in b.no_levels],
                fetched_at=b.ts,
            )
            out.append(HistoricalFrame(as_of=b.ts, market=km, orderbook=book))
        frames[ticker] = out
    points = (await session.execute(select(DataPoint).where(DataPoint.ts >= start, DataPoint.ts <= end))).scalars()
    news = (await session.execute(select(NewsArticle).where(NewsArticle.published_at <= end))).scalars()
    return HistoricalDataset(
        frames=frames,
        results={t: m.result == "yes" for t, m in markets.items()},
        points=[DataPointDTO(p.source, p.symbol, p.metric, p.ts, p.value, p.reliability) for p in points],
        articles=[_article(a) for a in news],
    )
