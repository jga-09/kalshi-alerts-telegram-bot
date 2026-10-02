"""News pipeline: NewsCollector -> NewsNormalizer -> NewsDeduplicator -> NewsSentimentAnalyzer -> NewsImpactAnalyzer.

Duplicates (syndicated copies, re-posts) are clustered so a story counts ONCE no
matter how many outlets carry it. Newer information receives exponentially more weight.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kalshi_ai.data.base import DataConnector
from kalshi_ai.domain.enums import AssetClass, NewsCategory
from kalshi_ai.text.sentiment import fingerprint, jaccard, sentiment_score, shingles

TOPIC_KEYWORDS: dict[AssetClass, tuple[str, ...]] = {
    AssetClass.CRYPTO: (
        "bitcoin",
        "btc",
        "crypto",
        "ethereum",
        "eth",
        "etf",
        "stablecoin",
        "coinbase",
        "binance",
        "blockchain",
        "satoshi",
        "halving",
        "miners",
        "tether",
        "usdc",
    ),
    AssetClass.GOLD: (
        "gold",
        "bullion",
        "xau",
        "precious metal",
        "comex",
        "lbma",
        "central bank buying",
        "safe haven",
        "silver",
    ),
    AssetClass.MACRO: (
        "federal reserve",
        "fed",
        "fomc",
        "powell",
        "inflation",
        "cpi",
        "pce",
        "payrolls",
        "jobs report",
        "treasury",
        "yields",
        "rate cut",
        "rate hike",
        "recession",
        "gdp",
        "dollar",
        "dxy",
    ),
}
SOURCE_RELIABILITY = {
    "reuters": 0.95,
    "associated press": 0.95,
    "bloomberg": 0.9,
    "financial times": 0.9,
    "wall street journal": 0.9,
    "cnbc": 0.8,
    "coindesk": 0.75,
    "the block": 0.75,
}
BREAKING_WINDOW = timedelta(minutes=60)
RECENT_WINDOW = timedelta(hours=24)


@dataclass
class Article:
    source: str
    url: str
    title: str
    summary: str
    published_at: datetime
    url_hash: str
    fingerprint: str
    reliability: float
    cluster_id: str = ""
    is_duplicate: bool = False
    sentiment: float = 0.0
    relevance: dict[str, float] = field(default_factory=dict)
    expected_direction: int = 0
    confidence: float = 0.0
    category: NewsCategory = NewsCategory.BACKGROUND

    @property
    def text(self) -> str:
        return f"{self.title}. {self.summary}"


class NewsCollector:
    def __init__(self, connectors: list[DataConnector]):
        self.connectors = connectors

    async def collect(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        items: list[dict[str, Any]] = []
        results: dict[str, Any] = {}
        for c in self.connectors:
            res = await c.fetch()
            results[c.name] = res
            for item in res.items:
                items.append({**item, "_reliability": c.reliability})
        return items, results


class NewsNormalizer:
    def normalize(self, raw: dict[str, Any], now: datetime) -> Article | None:
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not url.startswith(("http://", "https://")):
            return None
        published = raw.get("published_at")
        try:
            ts = datetime.fromisoformat(str(published).replace("Z", "+00:00")) if published else None
        except ValueError:
            ts = None
        if ts is None or ts.tzinfo is None or ts > now + timedelta(minutes=5):
            return None  # undated or future-dated items are unusable (and a look-ahead risk)
        source = str(raw.get("source") or "unknown")[:128]
        base_rel = float(raw.get("_reliability", 0.5))
        rel = max(base_rel, next((v for k, v in SOURCE_RELIABILITY.items() if k in source.lower()), 0.0))
        summary = (raw.get("summary") or "").strip()
        return Article(
            source=source,
            url=url,
            title=title[:500],
            summary=summary[:2000],
            published_at=ts,
            url_hash=hashlib.sha256(url.split("?")[0].encode()).hexdigest(),
            fingerprint=fingerprint(f"{title} {summary[:300]}"),
            reliability=min(1.0, rel),
        )


class NewsDeduplicator:
    def __init__(self, similarity_threshold: float = 0.6):
        self.threshold = similarity_threshold

    def deduplicate(self, articles: list[Article]) -> list[Article]:
        """Assign cluster ids; the earliest article in a cluster is canonical, others are duplicates."""
        seen_urls: set[str] = set()
        clusters: list[tuple[str, set[str], str]] = []  # (cluster_id, shingles, fingerprint)
        out: list[Article] = []
        for a in sorted(articles, key=lambda x: x.published_at):
            if a.url_hash in seen_urls:
                continue
            seen_urls.add(a.url_hash)
            sh = shingles(a.title)
            match = next(
                (cid for cid, csh, fp in clusters if fp == a.fingerprint or jaccard(sh, csh) >= self.threshold), None
            )
            if match is None:
                cid = a.fingerprint[:16]
                clusters.append((cid, sh, a.fingerprint))
                out.append(replace(a, cluster_id=cid, is_duplicate=False))
            else:
                out.append(replace(a, cluster_id=match, is_duplicate=True))
        return out


class NewsSentimentAnalyzer:
    def analyze(self, article: Article) -> Article:
        return replace(article, sentiment=sentiment_score(article.text))


class NewsImpactAnalyzer:
    def __init__(self, half_life_minutes: float = 90.0):
        self.half_life = half_life_minutes

    def relevance(self, article: Article) -> dict[str, float]:
        text = article.text.lower()
        scores = {}
        for asset, kws in TOPIC_KEYWORDS.items():
            hits = sum(1 for kw in kws if kw in text)
            scores[asset.value] = min(1.0, hits / 3.0)
        return scores

    def categorize(self, article: Article, now: datetime) -> NewsCategory:
        age = now - article.published_at
        if age <= BREAKING_WINDOW:
            return NewsCategory.BREAKING
        if age <= RECENT_WINDOW:
            return NewsCategory.RECENT
        return NewsCategory.BACKGROUND

    def analyze(self, article: Article, now: datetime) -> Article:
        rel = self.relevance(article)
        direction = 1 if article.sentiment > 0.15 else -1 if article.sentiment < -0.15 else 0
        confidence = min(1.0, abs(article.sentiment)) * article.reliability * max(rel.values() or [0])
        return replace(
            article,
            relevance=rel,
            expected_direction=direction,
            confidence=confidence,
            category=self.categorize(article, now),
        )

    def decay(self, article: Article, now: datetime) -> float:
        minutes = max(0.0, (now - article.published_at).total_seconds() / 60)
        return math.pow(0.5, minutes / self.half_life)


@dataclass(frozen=True)
class NewsSummary:
    asset: str
    score: float  # -1..1 recency/reliability weighted, cluster-deduplicated
    article_count: int
    unique_stories: int
    breaking: int
    recent: int
    background: int
    newest_age_minutes: float | None
    top_headlines: list[str]

    def as_features(self, prefix: str = "news") -> dict[str, float | None]:
        return {
            f"{prefix}_score": self.score,
            f"{prefix}_unique_stories": float(self.unique_stories),
            f"{prefix}_breaking": float(self.breaking),
            f"{prefix}_newest_age_min": self.newest_age_minutes,
        }


class NewsEngine:
    def __init__(self, half_life_minutes: float = 90.0):
        self.normalizer = NewsNormalizer()
        self.dedup = NewsDeduplicator()
        self.sentiment = NewsSentimentAnalyzer()
        self.impact = NewsImpactAnalyzer(half_life_minutes)

    def process(self, raw_items: list[dict[str, Any]], now: datetime) -> list[Article]:
        normalized = [a for a in (self.normalizer.normalize(r, now) for r in raw_items) if a is not None]
        return [self.impact.analyze(self.sentiment.analyze(a), now) for a in self.dedup.deduplicate(normalized)]

    def summarize(
        self, articles: list[Article], asset: AssetClass, now: datetime, min_relevance: float = 0.3
    ) -> NewsSummary:
        relevant = [a for a in articles if a.relevance.get(asset.value, 0) >= min_relevance and a.published_at <= now]
        canonical: dict[str, Article] = {}
        for a in sorted(relevant, key=lambda x: x.published_at):
            canonical.setdefault(a.cluster_id, a)  # one vote per story
        num = den = 0.0
        for a in canonical.values():
            w = self.impact.decay(a, now) * a.reliability * a.relevance.get(asset.value, 0)
            num += w * a.sentiment
            den += w
        newest = max((a.published_at for a in relevant), default=None)
        cats = [a.category for a in canonical.values()]
        top = sorted(canonical.values(), key=lambda a: a.published_at, reverse=True)[:3]
        return NewsSummary(
            asset=asset.value,
            score=(num / den) if den > 0 else 0.0,
            article_count=len(relevant),
            unique_stories=len(canonical),
            breaking=cats.count(NewsCategory.BREAKING),
            recent=cats.count(NewsCategory.RECENT),
            background=cats.count(NewsCategory.BACKGROUND),
            newest_age_minutes=((now - newest).total_seconds() / 60) if newest else None,
            top_headlines=[a.title for a in top],
        )
