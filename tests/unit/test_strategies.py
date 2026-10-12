"""Hammer / Drift / Fade / Surge: signal rules, user settings, sizing, resting orders, live payloads."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from kalshi_ai.db.models import PaperTrade
from kalshi_ai.domain.enums import (
    AssetClass,
    ConfidenceLevel,
    EntryStyle,
    OrderStatus,
    RiskMode,
    Side,
    StrategyId,
    TradingMode,
)
from kalshi_ai.features.engine import FeatureVector, MarketSpec
from kalshi_ai.kalshi.errors import KalshiValidationError
from kalshi_ai.kalshi.services import OrderIntent, build_v2_order_payload
from kalshi_ai.modeling.prediction import PredictionResult
from kalshi_ai.services.strategy_settings import get_strategy_config, save_strategy_config
from kalshi_ai.strategies.config import (
    PRESETS,
    StrategyConfig,
    load_strategy_config,
    toggled,
    with_max_price,
    with_next_weight,
)
from kalshi_ai.strategies.engine import StrategyEngine, in_surge_blackout
from kalshi_ai.trading.paper import process_resting_paper_orders
from kalshi_ai.trading.pipeline import Outcome
from tests.factories import TICKER, FakeBroker, FakeMarketData, book, signal
from tests.unit.test_execution_pipeline import live_user, paper_user, persisted_signal, pipeline

SATURDAY = datetime(2026, 10, 10, 15, 0, tzinfo=UTC)
WEEKDAY_2PM_ET = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)  # Wednesday 14:00 New York (EDT)
WEEKDAY_10AM_ET = datetime(2026, 10, 14, 14, 0, tzinfo=UTC)

BOOK = {"yes_ask": 0.55, "no_ask": 0.47, "yes_bid": 0.53, "no_bid": 0.45, "liquidity_ok": 1, "minutes_to_close": 10}
UPTREND = {"ta_momentum": 0.004, "ta_ema_spread_norm": 0.5, "ta_trend_regime": 1, "ob_signal": 0.3}


def pred(p_yes: float, *, conf: float = 0.8, at: datetime = SATURDAY, **features) -> PredictionResult:
    fv = FeatureVector(
        TICKER,
        at,
        MarketSpec(AssetClass.CRYPTO, "BTC-USD", None, "up"),
        {**BOOK, **features},
        {},
        "hash",
        3.0,
        1.0,
    )
    return PredictionResult(TICKER, p_yes, conf, ConfidenceLevel.HIGH, "ensemble:1", [], [], [], [], 3.0, features=fv)


def fired(result) -> dict[StrategyId, object]:
    return {s.strategy: s for s in result.signals}


# ------------------------------------------------------------------ signal rules


def test_strong_trending_setup_fires_hammer_drift_and_surge() -> None:
    res = StrategyEngine().evaluate(pred(0.66, **UPTREND))
    sigs = fired(res)
    assert set(sigs) == {StrategyId.HAMMER, StrategyId.DRIFT, StrategyId.SURGE}
    assert all(s.side == Side.YES for s in res.signals)
    hammer, drift, surge = sigs[StrategyId.HAMMER], sigs[StrategyId.DRIFT], sigs[StrategyId.SURGE]
    assert hammer.entry_style == EntryStyle.TAKE and hammer.entry_price == D("0.55")
    assert drift.entry_style == EntryStyle.MAKE and drift.entry_price == D("0.54")  # one tick inside the spread
    assert drift.edge == D("0.12") and drift.expires_at > hammer.expires_at  # resting orders get longer
    assert surge.entry_style == EntryStyle.TAKE and surge.entry_price == D("0.55")
    assert len({s.idempotency_key for s in res.signals}) == 3


def test_hammer_needs_a_confirming_trend() -> None:
    res = StrategyEngine().evaluate(pred(0.66, ta_momentum=-0.004, ta_trend_regime=-1, ob_signal=-0.3))
    assert StrategyId.HAMMER not in fired(res) and "trend" in res.notes["hammer"]


def test_drift_requires_value_even_at_the_ask() -> None:
    # Wide spread: 0.50 is 9c above the 0.41 limit, but below the 0.55 ask. Resting orders get filled most
    # often exactly when the model is wrong, so Drift (and therefore Surge) stays out.
    wide = {"yes_bid": 0.40, "yes_ask": 0.55, "no_bid": 0.45, "no_ask": 0.60}
    res = StrategyEngine().evaluate(pred(0.50, **wide))
    assert StrategyId.DRIFT not in fired(res) and StrategyId.SURGE not in fired(res)
    assert res.notes["drift"] == "no value inside the spread"


def test_surge_sits_out_weekday_afternoons_eastern() -> None:
    assert in_surge_blackout(WEEKDAY_2PM_ET, (12, 18))
    assert not in_surge_blackout(WEEKDAY_10AM_ET, (12, 18)) and not in_surge_blackout(SATURDAY, (12, 18))
    res = StrategyEngine().evaluate(pred(0.66, at=WEEKDAY_2PM_ET, **UPTREND))
    assert StrategyId.SURGE not in fired(res) and "blackout" in res.notes["surge"]
    assert StrategyId.DRIFT in fired(res)  # Drift still works in the afternoon


def test_surge_needs_high_confidence() -> None:
    res = StrategyEngine().evaluate(pred(0.66, conf=0.65, **UPTREND))
    assert StrategyId.SURGE not in fired(res) and StrategyId.DRIFT in fired(res)


def test_fade_buys_the_cheap_side_after_an_overreaction() -> None:
    cheap_no = {"yes_ask": 0.75, "no_ask": 0.27, "yes_bid": 0.72, "no_bid": 0.24, "ob_price_change": 0.10}
    res = StrategyEngine().evaluate(pred(0.60, **cheap_no))
    fade = fired(res)[StrategyId.FADE]
    assert fade.side == Side.NO and fade.entry_price == D("0.27") and fade.entry_style == EntryStyle.TAKE
    assert "against NO" in fade.reasons["overreaction"]
    # Without the overreaction there is nothing to fade.
    calm = StrategyEngine().evaluate(pred(0.60, **{**cheap_no, "ob_price_change": 0.0}))
    assert StrategyId.FADE not in fired(calm)


def test_strategies_that_disagree_stand_aside() -> None:
    crossed = {"yes_ask": 0.50, "no_ask": 0.30, "yes_bid": 0.45, "no_bid": 0.25, "ob_price_change": 0.10}
    res = StrategyEngine().evaluate(pred(0.60, **UPTREND, **crossed))
    assert res.conflict and res.signals == []


def test_low_confidence_fires_nothing() -> None:
    res = StrategyEngine().evaluate(pred(0.66, conf=0.3, **UPTREND))
    assert res.signals == []


# ------------------------------------------------------------------ settings


def test_presets_and_surge_supersedes_drift() -> None:
    assert PRESETS[RiskMode.PASSIVE] == (StrategyId.HAMMER, StrategyId.DRIFT, StrategyId.SURGE)
    assert all(StrategyId.SURGE in p for p in PRESETS.values())
    cfg = load_strategy_config(None, RiskMode.PASSIVE)
    assert not cfg.customized
    assert cfg.select({StrategyId.HAMMER, StrategyId.DRIFT, StrategyId.SURGE}) == [StrategyId.HAMMER, StrategyId.SURGE]
    drift_only = StrategyConfig(frozenset({StrategyId.DRIFT}))
    assert drift_only.select({StrategyId.DRIFT, StrategyId.SURGE}) == [StrategyId.DRIFT]


def test_size_weights_are_relative_and_never_exceed_full_size() -> None:
    cfg = StrategyConfig(frozenset(StrategyId), {StrategyId.SURGE: D("2")})
    assert cfg.size_factor(StrategyId.SURGE) == D("1")
    assert cfg.size_factor(StrategyId.HAMMER) == D("0.5")
    assert cfg.size_factor(StrategyId.FADE) == D("0.5")
    assert all(cfg.size_factor(s) <= 1 for s in StrategyId)
    assert StrategyConfig(frozenset({StrategyId.HAMMER})).size_factor(StrategyId.DRIFT) == 0


def test_config_edits_and_sanitizing() -> None:
    cfg = load_strategy_config(None, RiskMode.LOW)
    cfg = toggled(cfg, StrategyId.FADE)
    assert StrategyId.FADE in cfg.enabled and cfg.customized
    cfg = with_next_weight(cfg, StrategyId.FADE)
    assert cfg.weight(StrategyId.FADE) == D("1.5")
    cfg = with_max_price(cfg, 60)
    assert cfg.max_price == D("0.60")
    with pytest.raises(ValueError):
        with_max_price(cfg, 100)
    raw = {"enabled": ["hammer", "bogus"], "weights": {"hammer": "7"}, "max_price_cents": 500}
    clean = load_strategy_config(raw, RiskMode.LOW)
    assert clean.enabled == {StrategyId.HAMMER} and clean.weight(StrategyId.HAMMER) == 1
    assert clean.max_price_cents == 90


# ------------------------------------------------------------------ pipeline


def strat(strategy: StrategyId, style: EntryStyle = EntryStyle.TAKE, price: str = "0.55", **kw):
    return replace(signal(price=price, **kw), strategy=strategy, entry_style=style)


async def test_turned_off_strategy_never_trades(session) -> None:
    user = await paper_user(session)
    await save_strategy_config(session, user, StrategyConfig(frozenset({StrategyId.DRIFT})))
    sig = await persisted_signal(session, strat(StrategyId.HAMMER))
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.INELIGIBLE and "turned off" in res.reason


async def test_max_price_cap_blocks_expensive_entries(session) -> None:
    user = await paper_user(session)
    cfg = await get_strategy_config(session, user)
    await save_strategy_config(session, user, with_max_price(cfg, 50))
    sig = await persisted_signal(session, strat(StrategyId.SURGE))
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.REJECTED and "max_price_cap" in res.failed_checks


async def test_size_weight_shrinks_quantity(session) -> None:
    full_user = await paper_user(session, 2101)
    small_user = await paper_user(session, 2102)
    cfg = await get_strategy_config(session, small_user)
    await save_strategy_config(session, small_user, replace(cfg, weights={StrategyId.SURGE: D("2")}))
    full = await pipeline().execute(
        session, full_user, await persisted_signal(session, strat(StrategyId.HAMMER)), TradingMode.PAPER
    )
    small = await pipeline().execute(
        session, small_user, await persisted_signal(session, strat(StrategyId.HAMMER)), TradingMode.PAPER
    )
    assert full.outcome == small.outcome == Outcome.EXECUTED
    assert 1 <= small.order.quantity < full.order.quantity
    assert full.order.strategy == "hammer"


async def test_drift_order_rests_then_fills_at_its_limit(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, strat(StrategyId.DRIFT, EntryStyle.MAKE, price="0.54", prob="0.66"))
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)  # YES ask is 0.55 > 0.54 limit
    order = res.order
    assert res.outcome == Outcome.EXECUTED and order.status == OrderStatus.RESTING
    assert order.entry_style == "make" and order.expires_at == sig.expires_at and order.filled_quantity == 0
    assert (await session.execute(select(PaperTrade))).scalar_one_or_none() is None
    # Market unchanged -> still resting.
    assert await process_resting_paper_orders(session, TICKER, book()) == {"filled": 0, "expired": 0}
    # A seller comes down to 0.53 (NO bid 0.47): we're filled at OUR price, 0.54, with the maker fee.
    stats = await process_resting_paper_orders(session, TICKER, book(no_bids=(("0.47", "500"),)))
    assert stats["filled"] == 1 and order.status == OrderStatus.FILLED
    trade = (await session.execute(select(PaperTrade))).scalar_one()
    assert trade.entry_price == D("0.54") and trade.strategy == "drift" and trade.quantity == order.quantity


async def test_unfilled_drift_order_expires(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, strat(StrategyId.DRIFT, EntryStyle.MAKE, price="0.54", prob="0.66"))
    order = (await pipeline().execute(session, user, sig, TradingMode.PAPER)).order
    later = sig.expires_at + timedelta(seconds=1)
    assert (await process_resting_paper_orders(session, TICKER, book(), now=later))["expired"] == 1
    assert order.status == OrderStatus.CANCELED and "expired unfilled" in order.reason


async def test_marketable_drift_order_fills_immediately(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, strat(StrategyId.DRIFT, EntryStyle.MAKE, price="0.56", prob="0.68"))
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)  # ask 0.55 <= limit 0.56
    assert res.order.status == OrderStatus.FILLED and res.order.entry_style == "take"


async def test_live_drift_order_is_post_only_and_expires_at_the_exchange(session) -> None:
    user = await live_user(session)
    broker = FakeBroker()
    sig = await persisted_signal(session, strat(StrategyId.DRIFT, EntryStyle.MAKE, price="0.54", prob="0.66"))
    await pipeline(market_data=FakeMarketData(), broker=broker).execute(session, user, sig, TradingMode.LIVE)
    [intent] = broker.placed
    assert intent.time_in_force == "good_till_canceled" and intent.post_only
    assert intent.expiration_ts == int(sig.expires_at.timestamp())


def test_resting_order_payload() -> None:
    intent = OrderIntent(TICKER, Side.NO, 3, D("0.40"), "cid", "good_till_canceled", 1_800_000_000, True)
    payload = build_v2_order_payload(intent)
    assert payload["time_in_force"] == "good_till_canceled" and payload["expiration_time"] == 1_800_000_000
    assert payload["post_only"] is True and payload["side"] == "ask" and payload["price"] == "0.6000"
    with pytest.raises(KalshiValidationError):
        build_v2_order_payload(replace(intent, time_in_force="immediate_or_cancel"))
    ioc = build_v2_order_payload(OrderIntent(TICKER, Side.YES, 1, D("0.5"), "c2"))
    assert ioc["time_in_force"] == "immediate_or_cancel" and "post_only" not in ioc
