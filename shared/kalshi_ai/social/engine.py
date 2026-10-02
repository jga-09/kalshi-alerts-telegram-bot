"""Social sentiment engine (Reddit/X where API access is permitted).

Social data is noisy and manipulable. This engine down-weights spam, bot-like
accounts, duplicate and coordinated posts, and caps its own influence; it is one
input among many and never a sole trading trigger.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from kalshi_ai.domain.enums import AssetClass
from kalshi_ai.news.engine import TOPIC_KEYWORDS
from kalshi_ai.text.sentiment import fingerprint, sentiment_score

URL_RE = re.compile(r"https?://\S+")
SHILL_TERMS = (
    "100x",
    "1000x",
    "moon",
    "pump",
    "giveaway",
    "airdrop",
    "dm me",
    "guaranteed",
    "free crypto",
    "join my",
    "telegram group",
    "signals group",
    "presale",
)


@dataclass
class Post:
    platform: str
    external_id: str
    author: str | None
    text: str
    posted_at: datetime
    engagement: int = 0
    author_followers: int | None = None
    author_age_days: int | None = None
    fingerprint: str = ""
    sentiment: float = 0.0
    spam_score: float = 0.0
    bot_score: float = 0.0
    is_duplicate: bool = False
    coordinated: bool = False

    @property
    def quality(self) -> float:
        q = (1 - self.spam_score) * (1 - self.bot_score)
        if self.is_duplicate:
            q *= 0.1
        if self.coordinated:
            q *= 0.05
        return max(0.0, q)


def parse_post(raw: dict[str, Any]) -> Post | None:
    try:
        ts = datetime.fromisoformat(str(raw["posted_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    text = str(raw.get("text") or "").strip()
    if not text or ts.tzinfo is None:
        return None
    return Post(
        platform=str(raw.get("platform", "unknown")),
        external_id=str(raw.get("external_id")),
        author=raw.get("author"),
        text=text[:4000],
        posted_at=ts,
        engagement=int(raw.get("engagement") or 0),
        author_followers=raw.get("author_followers"),
        author_age_days=raw.get("author_age_days"),
    )


def spam_score(text: str) -> float:
    lower = text.lower()
    score = 0.0
    score += min(0.4, 0.15 * len(URL_RE.findall(text)))
    score += min(0.5, 0.25 * sum(1 for t in SHILL_TERMS if t in lower))
    letters = [c for c in text if c.isalpha()]
    if len(letters) > 20 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        score += 0.2
    if re.search(r"(.)\1{5,}", text):
        score += 0.1
    if len(text) < 12:
        score += 0.2
    if text.count("$") >= 4 or text.count("#") >= 6:
        score += 0.2
    return min(1.0, score)


class SocialAnalyzer:
    def __init__(
        self,
        coordination_window: timedelta = timedelta(minutes=30),
        coordination_authors: int = 3,
        max_influence: float = 0.5,
    ):
        self.coordination_window = coordination_window
        self.coordination_authors = coordination_authors
        self.max_influence = max_influence

    def enrich(self, posts: list[Post]) -> list[Post]:
        per_author = Counter(p.author for p in posts if p.author)
        enriched = []
        for p in posts:
            bot = 0.0
            if p.author_age_days is not None and p.author_age_days < 30:
                bot += 0.4
            if p.author_followers is not None and p.author_followers < 10:
                bot += 0.2
            if p.author and per_author[p.author] > 10:  # very high posting rate in one batch
                bot += 0.3
            if p.author_followers is not None and p.author_followers < 50 and p.engagement > 1000:
                bot += 0.2  # engagement inconsistent with reach
            enriched.append(
                replace(
                    p,
                    fingerprint=fingerprint(p.text),
                    sentiment=sentiment_score(p.text),
                    spam_score=spam_score(p.text),
                    bot_score=min(1.0, bot),
                )
            )
        # Duplicates & coordination: identical content from several distinct authors in a short window.
        groups: dict[str, list[int]] = defaultdict(list)
        for i, p in enumerate(enriched):
            groups[p.fingerprint].append(i)
        for idxs in groups.values():
            if len(idxs) < 2:
                continue
            idxs.sort(key=lambda i: enriched[i].posted_at)
            first = enriched[idxs[0]]
            authors = {enriched[i].author for i in idxs}
            span = enriched[idxs[-1]].posted_at - first.posted_at
            coordinated = len(authors) >= self.coordination_authors and span <= self.coordination_window
            for i in idxs[1:]:
                enriched[i] = replace(enriched[i], is_duplicate=True, coordinated=coordinated)
            if coordinated:
                enriched[idxs[0]] = replace(enriched[idxs[0]], coordinated=True)
        return enriched

    def summarize(self, posts: list[Post], asset: AssetClass, now: datetime) -> dict[str, float | None]:
        kws = TOPIC_KEYWORDS[asset]
        relevant = [p for p in posts if p.posted_at <= now and any(k in p.text.lower() for k in kws)]
        last_hour = [p for p in relevant if now - p.posted_at <= timedelta(hours=1)]
        prior = [p for p in relevant if timedelta(hours=1) < now - p.posted_at <= timedelta(hours=4)]
        prior_rate = len(prior) / 3.0
        velocity = (len(last_hour) - prior_rate) / max(1.0, prior_rate)
        weights = [p.quality * math.log1p(max(0, p.engagement)) + p.quality for p in last_hour]
        total_w = sum(weights)
        sentiment = sum(w * p.sentiment for w, p in zip(weights, last_hour, strict=True)) / total_w if total_w else 0.0
        low_quality = sum(1 for p in last_hour if p.quality < 0.3)
        coordinated = sum(1 for p in last_hour if p.coordinated)
        quality_ratio = 1 - (low_quality / len(last_hour)) if last_hour else 0.0
        influence = self.max_influence * quality_ratio * min(1.0, len(last_hour) / 20)
        return {
            "social_available": 1.0 if relevant else 0.0,
            "social_sentiment": sentiment,
            "social_volume_1h": float(len(last_hour)),
            "social_velocity": velocity,
            "social_engagement": float(sum(p.engagement for p in last_hour)),
            "social_unusual": 1.0 if velocity > 3 and len(last_hour) >= 10 else 0.0,
            "social_low_quality_share": (low_quality / len(last_hour)) if last_hour else None,
            "social_coordinated_posts": float(coordinated),
            "social_weighted_signal": max(-influence, min(influence, sentiment * influence)),
        }
