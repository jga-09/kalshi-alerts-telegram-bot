from __future__ import annotations

from datetime import timedelta

from kalshi_ai.db.base import utcnow
from kalshi_ai.domain.enums import AssetClass, NewsCategory
from kalshi_ai.news.engine import NewsEngine
from kalshi_ai.social.engine import SocialAnalyzer, parse_post, spam_score

NOW = utcnow()


def item(title: str, minutes_ago: float, url: str, source: str = "Reuters", summary: str = "") -> dict:
    return {
        "title": title,
        "url": url,
        "source": source,
        "summary": summary,
        "published_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "_reliability": 0.7,
    }


def test_syndicated_duplicates_count_once() -> None:
    engine = NewsEngine()
    title = "Bitcoin surges to record high as ETF inflows jump"
    items = [item(title, 10 + i, f"https://site{i}.com/a") for i in range(8)]
    items.append(item("Gold slumps as dollar strengthens", 30, "https://x.com/gold"))
    articles = engine.process(items, NOW)
    btc = engine.summarize(articles, AssetClass.CRYPTO, NOW)
    assert btc.article_count == 8 and btc.unique_stories == 1
    assert sum(1 for a in articles if a.is_duplicate) == 7
    one = engine.summarize(engine.process(items[:1], NOW), AssetClass.CRYPTO, NOW)
    assert abs(btc.score - one.score) < 1e-9  # eight copies carry the same weight as one
    assert btc.score > 0


def test_recency_weighting_and_categories() -> None:
    engine = NewsEngine()
    items = [
        item("Bitcoin plunges after exchange hack", 2000, "https://a.com/1"),
        item("Bitcoin rallies strongly on adoption news", 5, "https://b.com/2"),
    ]
    articles = engine.process(items, NOW)
    cats = {a.title: a.category for a in articles}
    assert cats["Bitcoin rallies strongly on adoption news"] == NewsCategory.BREAKING
    assert cats["Bitcoin plunges after exchange hack"] == NewsCategory.BACKGROUND
    assert engine.summarize(articles, AssetClass.CRYPTO, NOW).score > 0  # newer positive story dominates


def test_rejects_future_and_undated_items() -> None:
    engine = NewsEngine()
    future = item("Bitcoin rallies", -60, "https://f.com/1")
    undated = {"title": "Bitcoin rallies", "url": "https://u.com/1", "source": "x"}
    assert engine.process([future, undated], NOW) == []


def test_spam_bot_and_coordination_detection() -> None:
    assert spam_score("🚀🚀 100x MOON GIVEAWAY!!! join my telegram group http://scam.io http://x.io") > 0.6
    assert spam_score("Bitcoin funding rates turned negative overnight while open interest fell.") < 0.2
    posts = [
        parse_post(
            {
                "platform": "x",
                "external_id": str(i),
                "author": f"bot{i}",
                "author_age_days": 3,
                "author_followers": 2,
                "text": "Bitcoin will moon tonight, buy now before the pump",
                "posted_at": (NOW - timedelta(minutes=i)).isoformat(),
                "engagement": 5,
            }
        )
        for i in range(5)
    ]
    posts.append(
        parse_post(
            {
                "platform": "reddit",
                "external_id": "r1",
                "author": "analyst",
                "author_age_days": 2000,
                "author_followers": 50000,
                "text": "Bitcoin ETF outflows continued; sentiment weak",
                "posted_at": (NOW - timedelta(minutes=3)).isoformat(),
                "engagement": 300,
            }
        )
    )
    enriched = SocialAnalyzer().enrich([p for p in posts if p])
    coordinated = [p for p in enriched if p.coordinated]
    assert len(coordinated) == 5 and all(p.quality < 0.05 for p in coordinated)
    summary = SocialAnalyzer().summarize(enriched, AssetClass.CRYPTO, NOW)
    # Five coordinated bullish bot posts must not outweigh one credible bearish post.
    assert summary["social_sentiment"] < 0
    assert summary["social_coordinated_posts"] == 5.0
    assert abs(summary["social_weighted_signal"]) <= 0.5


def test_social_influence_is_capped() -> None:
    posts = [
        parse_post(
            {
                "platform": "reddit",
                "external_id": str(i),
                "author": f"user{i}",
                "author_age_days": 900,
                "author_followers": 1000,
                "text": f"Bitcoin rally strong gains surge record {i} analysis",
                "posted_at": (NOW - timedelta(minutes=i % 50)).isoformat(),
                "engagement": 100,
            }
        )
        for i in range(60)
    ]
    s = SocialAnalyzer(max_influence=0.5).summarize(
        SocialAnalyzer().enrich([p for p in posts if p]), AssetClass.CRYPTO, NOW
    )
    assert 0 < s["social_weighted_signal"] <= 0.5
