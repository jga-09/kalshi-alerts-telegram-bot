"""Concrete data connectors - only official, publicly documented or licensed-by-key APIs.

| Connector            | API                                         | Auth          | Default |
|----------------------|---------------------------------------------|---------------|---------|
| coinbase_spot        | Coinbase Exchange public candles            | none          | on      |
| deribit_derivatives  | Deribit public book summary (funding, OI)   | none          | off     |
| fred_macro           | FRED (St. Louis Fed) series observations    | FRED_API_KEY  | off*    |
| gold_licensed        | ADAPTER - requires a licensed gold/GC feed  | provider key  | off     |
| rss_news             | Publisher RSS feeds configured by operator  | none          | off*    |
| newsapi              | newsapi.org /v2/everything                  | NEWS_API_KEY  | off*    |
| reddit               | Reddit OAuth API (client credentials)       | app creds     | off*    |
| x_recent_search      | X API v2 recent search                      | bearer token  | off*    |
| economic_calendar    | Operator-maintained JSON calendar file      | none          | on      |

(*) enabled automatically once the corresponding credential/config is present.
Operators are responsible for complying with each provider's terms in their jurisdiction.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import feedparser
import httpx

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.data.base import Candle, DataConnector, DataPoint, FetchResult
from kalshi_ai.logging import get_logger

log = get_logger(__name__)
TIMEOUT = httpx.Timeout(8.0)


async def _timed(source: str, fn: Callable[[], Awaitable[FetchResult]]) -> FetchResult:
    started = time.monotonic()
    try:
        result = await fn()
    except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
        log.warning("data_source_error", source=source, error=type(exc).__name__)
        result = FetchResult(source=source, ok=False, error=type(exc).__name__)
    result.latency_ms = (time.monotonic() - started) * 1000
    return result


class CoinbaseSpotConnector(DataConnector):
    name = "coinbase_spot"
    reliability = 0.9
    stale_after_seconds = 180
    URL = "https://api.exchange.coinbase.com/products/{product}/candles"

    def __init__(
        self,
        products: tuple[str, ...] = ("BTC-USD", "ETH-USD"),
        granularity: int = 60,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.products = products
        self.granularity = granularity
        self.transport = transport

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> FetchResult:
        async def run() -> FetchResult:
            result = FetchResult(source=self.name, ok=True)
            async with httpx.AsyncClient(
                timeout=TIMEOUT, transport=self.transport, headers={"User-Agent": "kalshi-ai/0.1"}
            ) as http:
                for product in self.products:
                    resp = await http.get(self.URL.format(product=product), params={"granularity": self.granularity})
                    resp.raise_for_status()
                    rows = resp.json()
                    candles = sorted(
                        (
                            Candle(
                                datetime.fromtimestamp(r[0], UTC),
                                float(r[3]),
                                float(r[2]),
                                float(r[1]),
                                float(r[4]),
                                float(r[5]),
                            )
                            for r in rows
                        ),
                        key=lambda c: c.ts,
                    )
                    result.candles[product] = candles
                    if candles:
                        last = candles[-1]
                        result.points.append(
                            DataPoint(self.name, product, "price", last.ts, last.close, self.reliability)
                        )
            return result

        return await _timed(self.name, run)


class DeribitDerivativesConnector(DataConnector):
    """Public perpetual-futures data (funding rate, open interest, mark). Off unless DERIBIT_ENABLED=true."""

    name = "deribit_derivatives"
    reliability = 0.8
    stale_after_seconds = 300
    URL = "https://www.deribit.com/api/v2/public/get_book_summary_by_instrument"

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return self.settings.deribit_enabled

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("disabled (DERIBIT_ENABLED=false)")

        async def run() -> FetchResult:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as http:
                resp = await http.get(self.URL, params={"instrument_name": "BTC-PERPETUAL"})
                resp.raise_for_status()
                row = resp.json()["result"][0]
            ts = datetime.fromtimestamp(int(row.get("creation_timestamp", time.time() * 1000)) / 1000, UTC)
            pts = [
                DataPoint(self.name, "BTC-PERP", "funding_8h", ts, float(row.get("funding_8h") or 0), self.reliability),
                DataPoint(
                    self.name, "BTC-PERP", "open_interest", ts, float(row.get("open_interest") or 0), self.reliability
                ),
                DataPoint(self.name, "BTC-PERP", "mark_price", ts, float(row.get("mark_price") or 0), self.reliability),
            ]
            return FetchResult(source=self.name, ok=True, points=pts)

        return await _timed(self.name, run)


class FredMacroConnector(DataConnector):
    """Treasury yields, real yields and the trade-weighted dollar (DXY proxy; ICE DXY itself is licensed)."""

    name = "fred_macro"
    reliability = 0.95
    stale_after_seconds = 4 * 86_400  # daily series, published with a lag
    URL = "https://api.stlouisfed.org/fred/series/observations"
    SERIES = {
        "DGS10": "us10y_yield",
        "DGS2": "us2y_yield",
        "DFII10": "us10y_real_yield",
        "DTWEXBGS": "usd_broad_index",
    }

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return bool(self.settings.fred_api_key.get_secret_value())

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("FRED_API_KEY not set")

        async def run() -> FetchResult:
            result = FetchResult(source=self.name, ok=True)
            key = self.settings.fred_api_key.get_secret_value()
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as http:
                for series, metric in self.SERIES.items():
                    resp = await http.get(
                        self.URL,
                        params={
                            "series_id": series,
                            "api_key": key,
                            "file_type": "json",
                            "sort_order": "desc",
                            "limit": 10,
                        },
                    )
                    resp.raise_for_status()
                    for obs in resp.json().get("observations", []):
                        if obs.get("value") in (None, ".", ""):
                            continue
                        ts = datetime.fromisoformat(obs["date"]).replace(tzinfo=UTC)
                        result.points.append(
                            DataPoint(self.name, series, metric, ts, float(obs["value"]), self.reliability)
                        )
                        break
            return result

        return await _timed(self.name, run)


class LicensedGoldConnector(DataConnector):
    """ADAPTER ONLY. Spot gold (LBMA/COMEX) and GC futures data are licensed products.

    To enable, implement ``_fetch_provider`` for your licensed vendor (e.g. CME DataMine,
    a broker market-data API) and set GOLD_DATA_PROVIDER. Until then this source reports
    DISABLED and gold signals run with reduced confidence.
    """

    name = "gold_licensed"
    reliability = 0.9
    stale_after_seconds = 300

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.gold_data_provider)

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("no licensed gold data provider configured (GOLD_DATA_PROVIDER)")
        return FetchResult(
            source=self.name, ok=False, error=f"provider '{self.settings.gold_data_provider}' adapter not implemented"
        )


class RssNewsConnector(DataConnector):
    name = "rss_news"
    reliability = 0.6
    stale_after_seconds = 3600

    def __init__(self, settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return bool(self.settings.rss_feeds)

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("RSS_FEEDS not set")

        async def run() -> FetchResult:
            items: list[dict[str, Any]] = []
            ok_any = False
            async with httpx.AsyncClient(
                timeout=TIMEOUT,
                transport=self.transport,
                follow_redirects=True,
                headers={"User-Agent": "kalshi-ai/0.1 (+rss)"},
            ) as http:
                for url in self.settings.rss_feeds:
                    try:
                        resp = await http.get(url)
                        resp.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    ok_any = True
                    feed = feedparser.parse(resp.content)
                    source = feed.feed.get("title", url)[:120]
                    for e in feed.entries[:50]:
                        published = e.get("published_parsed") or e.get("updated_parsed")
                        items.append(
                            {
                                "source": source,
                                "url": e.get("link", ""),
                                "title": e.get("title", ""),
                                "summary": e.get("summary", "")[:2000],
                                "published_at": datetime(*published[:6], tzinfo=UTC).isoformat() if published else None,
                            }
                        )
            return FetchResult(source=self.name, ok=ok_any, items=items, error=None if ok_any else "all feeds failed")

        return await _timed(self.name, run)


class NewsApiConnector(DataConnector):
    name = "newsapi"
    reliability = 0.7
    stale_after_seconds = 3600
    URL = "https://newsapi.org/v2/everything"

    def __init__(
        self,
        query: str = 'bitcoin OR gold OR "federal reserve" OR inflation',
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.query = query
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return bool(self.settings.news_api_key.get_secret_value())

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("NEWS_API_KEY not set")

        async def run() -> FetchResult:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as http:
                resp = await http.get(
                    self.URL,
                    params={"q": self.query, "language": "en", "sortBy": "publishedAt", "pageSize": 50},
                    headers={"X-Api-Key": self.settings.news_api_key.get_secret_value()},
                )
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
            items = [
                {
                    "source": (a.get("source") or {}).get("name") or "newsapi",
                    "url": a.get("url") or "",
                    "title": a.get("title") or "",
                    "summary": (a.get("description") or "")[:2000],
                    "published_at": a.get("publishedAt"),
                }
                for a in articles
            ]
            return FetchResult(source=self.name, ok=True, items=items)

        return await _timed(self.name, run)


class RedditConnector(DataConnector):
    """Reddit official OAuth API (application-only). Requires a registered Reddit app."""

    name = "reddit"
    reliability = 0.3
    stale_after_seconds = 1800
    TOKEN_URL = "https://www.reddit.com/api/v1/access_token"  # noqa: S105 - URL, not a secret
    API = "https://oauth.reddit.com"

    def __init__(
        self,
        subreddits: tuple[str, ...] = ("Bitcoin", "CryptoCurrency", "Gold", "economics"),
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.subreddits = subreddits
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return bool(
            self.settings.reddit_client_id.get_secret_value() and self.settings.reddit_client_secret.get_secret_value()
        )

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("REDDIT_CLIENT_ID/SECRET not set")

        async def run() -> FetchResult:
            ua = {"User-Agent": self.settings.reddit_user_agent}
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport, headers=ua) as http:
                tok = await http.post(
                    self.TOKEN_URL,
                    data={"grant_type": "client_credentials"},
                    auth=(
                        self.settings.reddit_client_id.get_secret_value(),
                        self.settings.reddit_client_secret.get_secret_value(),
                    ),
                )
                tok.raise_for_status()
                bearer = {"Authorization": f"Bearer {tok.json()['access_token']}"}
                items: list[dict[str, Any]] = []
                for sub in self.subreddits:
                    resp = await http.get(f"{self.API}/r/{sub}/new", params={"limit": 50}, headers=bearer)
                    resp.raise_for_status()
                    for child in resp.json().get("data", {}).get("children", []):
                        d = child.get("data", {})
                        items.append(
                            {
                                "platform": "reddit",
                                "external_id": d.get("name") or d.get("id"),
                                "author": d.get("author"),
                                "text": f"{d.get('title', '')}\n{d.get('selftext', '')}"[:4000],
                                "posted_at": datetime.fromtimestamp(float(d.get("created_utc", 0)), UTC).isoformat(),
                                "engagement": int(d.get("score", 0)) + int(d.get("num_comments", 0)),
                                "topic": sub,
                            }
                        )
            return FetchResult(source=self.name, ok=True, items=items)

        return await _timed(self.name, run)


class XRecentSearchConnector(DataConnector):
    """X API v2 recent search. Requires a paid/approved X developer bearer token."""

    name = "x_recent_search"
    reliability = 0.3
    stale_after_seconds = 1800
    URL = "https://api.x.com/2/tweets/search/recent"

    def __init__(
        self,
        query: str = "(bitcoin OR #BTC OR gold OR #XAU) lang:en -is:retweet",
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.query = query
        self.settings = settings or get_settings()
        self.transport = transport

    def is_configured(self) -> bool:
        return bool(self.settings.x_bearer_token.get_secret_value())

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("X_BEARER_TOKEN not set")

        async def run() -> FetchResult:
            async with httpx.AsyncClient(timeout=TIMEOUT, transport=self.transport) as http:
                resp = await http.get(
                    self.URL,
                    params={
                        "query": self.query,
                        "max_results": 50,
                        "tweet.fields": "created_at,public_metrics,author_id",
                        "expansions": "author_id",
                        "user.fields": "public_metrics,created_at",
                    },
                    headers={"Authorization": f"Bearer {self.settings.x_bearer_token.get_secret_value()}"},
                )
                resp.raise_for_status()
                body = resp.json()
            users = {u["id"]: u for u in (body.get("includes") or {}).get("users", [])}
            items = []
            for t in body.get("data", []):
                author = users.get(t.get("author_id"), {})
                metrics = t.get("public_metrics") or {}
                created = author.get("created_at")
                age_days = None
                if created:
                    age_days = (datetime.now(UTC) - datetime.fromisoformat(created.replace("Z", "+00:00"))).days
                items.append(
                    {
                        "platform": "x",
                        "external_id": t["id"],
                        "author": author.get("username"),
                        "author_followers": (author.get("public_metrics") or {}).get("followers_count"),
                        "author_age_days": age_days,
                        "text": t.get("text", ""),
                        "posted_at": t.get("created_at"),
                        "engagement": sum(
                            int(metrics.get(k, 0)) for k in ("like_count", "retweet_count", "reply_count")
                        ),
                    }
                )
            return FetchResult(source=self.name, ok=True, items=items)

        return await _timed(self.name, run)


class EconomicCalendarConnector(DataConnector):
    """Reads an operator-maintained JSON file of scheduled releases (FOMC, CPI, NFP...).

    Format: [{"name": "FOMC Rate Decision", "time": "2026-10-28T18:00:00Z", "importance": "high",
              "assets": ["gold", "crypto", "macro"]}, ...]
    """

    name = "economic_calendar"
    reliability = 0.95
    stale_after_seconds = 30 * 86_400

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def is_configured(self) -> bool:
        return bool(self.settings.economic_calendar_path) and Path(self.settings.economic_calendar_path).exists()

    async def fetch(self) -> FetchResult:
        if not self.is_configured():
            return self.not_configured("ECONOMIC_CALENDAR_PATH missing")
        try:
            events = json.loads(await asyncio.to_thread(Path(self.settings.economic_calendar_path).read_text))
        except (OSError, ValueError) as exc:
            return FetchResult(source=self.name, ok=False, error=type(exc).__name__)
        return FetchResult(source=self.name, ok=True, items=[e for e in events if "time" in e and "name" in e])


def default_connectors(settings: Settings | None = None) -> list[DataConnector]:
    settings = settings or get_settings()
    return [
        CoinbaseSpotConnector(),
        DeribitDerivativesConnector(settings),
        FredMacroConnector(settings),
        LicensedGoldConnector(settings),
        RssNewsConnector(settings),
        NewsApiConnector(settings=settings),
        RedditConnector(settings=settings),
        XRecentSearchConnector(settings=settings),
        EconomicCalendarConnector(settings),
    ]
