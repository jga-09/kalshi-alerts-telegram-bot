"""FeatureEngine: MarketContext (as of time T) -> reproducible feature snapshot.

Look-ahead protection: every input is filtered to ``ts <= as_of`` here, in one
place, so live trading and backtests share the exact same code path.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_ai.data.base import Candle, DataPoint
from kalshi_ai.domain.enums import AssetClass
from kalshi_ai.features.assets import crypto_features, event_proximity, gold_features
from kalshi_ai.kalshi.models import KalshiMarket, KalshiOrderBook, KalshiTrade
from kalshi_ai.news.engine import Article, NewsEngine
from kalshi_ai.orderbook.analysis import analyze as analyze_orderbook
from kalshi_ai.social.engine import Post, SocialAnalyzer
from kalshi_ai.ta.engine import compute_features, probability_above_strike

FEATURE_SET_VERSION = "fs-2026.10.1"


@dataclass(frozen=True)
class MarketSpec:
    asset_class: AssetClass
    underlying: str | None  # e.g. BTC-USD
    strike: float | None
    direction: str  # "above" | "below" | "up" | "unknown"


def classify_market(market: KalshiMarket) -> MarketSpec:
    t = market.ticker.upper()
    title = (market.title or "").lower()
    if re.search(r"BTC|BITCOIN", t) or "bitcoin" in title:
        asset, underlying = AssetClass.CRYPTO, "BTC-USD"
    elif re.search(r"ETH", t) or "ethereum" in title:
        asset, underlying = AssetClass.CRYPTO, "ETH-USD"
    elif re.search(r"GOLD|XAU", t) or "gold" in title:
        asset, underlying = AssetClass.GOLD, "XAUUSD"
    elif re.search(r"FED|CPI|INFL|GDP|PAYROLL|RATE", t):
        asset, underlying = AssetClass.MACRO, None
    else:
        asset, underlying = AssetClass.OTHER, None
    strike_type = str(market.raw.get("strike_type") or "").lower()
    floor_strike, cap_strike = market.raw.get("floor_strike"), market.raw.get("cap_strike")
    if strike_type in ("greater", "greater_or_equal") and floor_strike is not None:
        return MarketSpec(asset, underlying, float(floor_strike), "above")
    if strike_type in ("less", "less_or_equal") and cap_strike is not None:
        return MarketSpec(asset, underlying, float(cap_strike), "below")
    if "up" in title and ("down" in title or "15" in t):
        return MarketSpec(asset, underlying, None, "up")
    return MarketSpec(asset, underlying, None, "unknown")


@dataclass
class MarketContext:
    as_of: datetime
    market: KalshiMarket
    orderbook: KalshiOrderBook | None
    trades: list[KalshiTrade] = field(default_factory=list)
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    points: list[DataPoint] = field(default_factory=list)
    articles: list[Article] = field(default_factory=list)
    posts: list[Post] = field(default_factory=list)
    calendar: list[dict[str, Any]] = field(default_factory=list)
    source_health: dict[str, str] = field(default_factory=dict)
    max_data_age_seconds: int = 120

    def point_in_time(self) -> MarketContext:
        """Return a copy containing only information available at ``as_of``."""
        t = self.as_of
        return MarketContext(
            as_of=t,
            market=self.market,
            orderbook=self.orderbook if self.orderbook is None or self.orderbook.fetched_at <= t else None,
            trades=[x for x in self.trades if x.created_time <= t],
            # A candle is only complete once its period has ended; with 1m candles require open+60s <= t.
            candles={k: [c for c in v if c.ts.timestamp() + 60 <= t.timestamp()] for k, v in self.candles.items()},
            points=[p for p in self.points if p.ts <= t],
            articles=[a for a in self.articles if a.published_at <= t],
            posts=[p for p in self.posts if p.posted_at <= t],
            calendar=self.calendar,
            source_health=self.source_health,
            max_data_age_seconds=self.max_data_age_seconds,
        )


@dataclass(frozen=True)
class FeatureVector:
    market_ticker: str
    as_of: datetime
    spec: MarketSpec
    features: dict[str, Any]
    data_versions: dict[str, Any]
    feature_hash: str
    data_age_seconds: float
    completeness: float  # 0..1 share of key inputs available

    def get(self, key: str, default: float | None = None) -> float | None:
        v = self.features.get(key, default)
        return default if v is None else v


def _clean(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


class FeatureEngine:
    version = FEATURE_SET_VERSION

    def __init__(self) -> None:
        self.news = NewsEngine()
        self.social = SocialAnalyzer()

    def build(self, raw_ctx: MarketContext) -> FeatureVector:
        ctx = raw_ctx.point_in_time()
        now = ctx.as_of
        m = ctx.market
        spec = classify_market(m)
        f: dict[str, Any] = {"asset_class": spec.asset_class.value, "market_direction": spec.direction}
        versions: dict[str, Any] = {"feature_set": self.version, "source_health": dict(ctx.source_health)}
        ages: list[float] = []

        # --- Kalshi market & order book -----------------------------------
        if m.close_time:
            f["minutes_to_close"] = max(0.0, (m.close_time - now).total_seconds() / 60)
        if ctx.orderbook is not None:
            ob = analyze_orderbook(ctx.orderbook, ctx.trades, now)
            f.update(ob.as_features())
            f["market_prob_yes"] = float(ob.mid) if ob.mid is not None else None
            f["yes_ask"] = float(ob.best_yes_ask) if ob.best_yes_ask is not None else None
            f["no_ask"] = float(ctx.orderbook.best_no_ask) if ctx.orderbook.best_no_ask is not None else None
            f["liquidity_ok"] = 1 if ob.liquidity_ok else 0
            age = (now - ctx.orderbook.fetched_at).total_seconds()
            ages.append(age)
            versions["orderbook"] = {"fetched_at": ctx.orderbook.fetched_at.isoformat(), "age_s": age}
        else:
            f["market_prob_yes"] = None
            f["liquidity_ok"] = 0

        # --- Underlying technicals ----------------------------------------
        candles = ctx.candles.get(spec.underlying or "", [])
        ta = compute_features(candles)
        f.update({f"ta_{k}" if not k.startswith("ta_") else k: v for k, v in ta.items()})
        if candles:
            age = now.timestamp() - (candles[-1].ts.timestamp() + 60)
            ages.append(age)
            versions["candles"] = {"symbol": spec.underlying, "last": candles[-1].ts.isoformat(), "n": len(candles)}

        # Baseline probability from price vs strike and realized volatility.
        price, vol = ta.get("price"), ta.get("realized_vol")
        minutes = f.get("minutes_to_close")
        strike = spec.strike
        if spec.direction == "up" and m.open_time and candles:
            opens = [c for c in candles if c.ts <= m.open_time]
            strike = opens[-1].close if opens else None
        f["strike"] = strike
        baseline = None
        if price and vol and minutes and strike:
            p_above = probability_above_strike(float(price), float(strike), float(vol), float(minutes))
            if p_above is not None:
                baseline = p_above if spec.direction in ("above", "up") else 1 - p_above
        f["baseline_prob_yes"] = baseline

        # --- News & social --------------------------------------------------
        news = (
            self.news.summarize(ctx.articles, spec.asset_class, now) if spec.asset_class != AssetClass.OTHER else None
        )
        if news:
            f.update(news.as_features())
            versions["news"] = {"unique_stories": news.unique_stories, "newest_age_min": news.newest_age_minutes}
        if ctx.posts and spec.asset_class in (AssetClass.CRYPTO, AssetClass.GOLD):
            f.update(self.social.summarize(ctx.posts, spec.asset_class, now))
        else:
            f["social_available"] = 0.0

        # --- Asset-specific -------------------------------------------------
        if spec.asset_class == AssetClass.CRYPTO:
            f.update(crypto_features(ctx.points, now))
            f.update(event_proximity(ctx.calendar, now, "crypto"))
        elif spec.asset_class == AssetClass.GOLD:
            f.update(gold_features(ctx.points, now, ctx.calendar))

        # --- Data quality ---------------------------------------------------
        key_inputs = [f.get("market_prob_yes"), f.get("ta_available") or None, f.get("baseline_prob_yes")]
        if spec.asset_class in (AssetClass.CRYPTO, AssetClass.GOLD):
            key_inputs.append(f.get("news_score") if f.get("news_unique_stories") else None)
        completeness = sum(1 for k in key_inputs if k is not None) / len(key_inputs)
        data_age = max(ages) if ages else float("inf")
        degraded = sum(1 for s in ctx.source_health.values() if s in ("stale", "down"))
        f["data_completeness"] = completeness
        f["data_age_seconds"] = data_age if math.isfinite(data_age) else None
        f["sources_degraded"] = degraded

        clean = {k: _clean(v) for k, v in sorted(f.items())}
        digest = hashlib.sha256(
            json.dumps({"t": now.isoformat(), "m": m.ticker, "f": clean}, sort_keys=True, default=str).encode()
        ).hexdigest()
        return FeatureVector(m.ticker, now, spec, clean, versions, digest, data_age, completeness)
