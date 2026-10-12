from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import KalshiConnection, Order, PaperTrade, Position, Prediction, Signal
from kalshi_ai.domain.enums import (
    KalshiConnectionStatus,
    KalshiEnvironment,
    OrderStatus,
    Plan,
    RiskMode,
    Side,
    TradingMode,
)
from kalshi_ai.kalshi.errors import KalshiTimeoutError
from kalshi_ai.notifications.messages import NullNotifier
from kalshi_ai.services.kill_switch import activate_user_kill_switch, set_global_kill_switch
from kalshi_ai.services.trading_controls import (
    confirm_live_trading,
    enable_paper_trading,
    review_live_trading,
    set_auto_trading,
    set_risk_mode,
)
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai.trading.emergency import cancel_open_orders
from kalshi_ai.trading.paper import settle_paper_market, simulate_ioc_fill
from kalshi_ai.trading.pipeline import ExecutionPipeline, Outcome
from tests.conftest import give_subscription, make_user
from tests.factories import TICKER, FailingNotifier, FakeBroker, FakeMarketData, book, market, signal


async def persisted_signal(session, sig):
    pred = Prediction(
        market_ticker=sig.market_ticker,
        ts=utcnow(),
        model_version=sig.model_version,
        data_versions={},
        probability_yes=sig.estimated_probability,
        confidence=0.8,
    )
    session.add(pred)
    await session.flush()
    row = Signal(
        prediction_id=pred.id,
        market_ticker=sig.market_ticker,
        ts=sig.as_of,
        side=sig.side,
        action=sig.action,
        estimated_probability=sig.estimated_probability,
        market_probability=sig.market_probability,
        entry_price=sig.entry_price,
        edge=sig.edge,
        confidence=float(sig.confidence),
        confidence_level=sig.confidence_level,
        risk_level="low",
        model_version=sig.model_version,
        expires_at=sig.expires_at,
        idempotency_key=sig.idempotency_key,
    )
    session.add(row)
    await session.flush()
    sig.signal_id = row.id
    return sig


async def paper_user(session, tid: int = 2001, mode: RiskMode = RiskMode.RISKY):
    user = await make_user(session, tid)
    await give_subscription(session, user, Plan.AUTO)
    assert await enable_paper_trading(session, user) == []
    await set_risk_mode(session, user, mode)
    return user


async def live_user(session, tid: int = 3001):
    user = await make_user(session, tid)
    await give_subscription(session, user, Plan.AUTO)
    user.disclosures_accepted_at = utcnow()
    session.add(
        KalshiConnection(
            user_id=user.id,
            environment=KalshiEnvironment.DEMO,
            encrypted_api_key_id=b"x",
            encrypted_private_key=b"y",
            api_key_id_hint="abcd",
            public_key_fingerprint="f",
            key_type="rsa",
            scopes=["read", "write::trade"],
            status=KalshiConnectionStatus.VERIFIED,
        )
    )
    await set_risk_mode(session, user, RiskMode.RISKY)
    assert await set_auto_trading(session, user, True) == []
    review = await review_live_trading(session, user)
    assert review.blockers == [], review.blockers
    assert await confirm_live_trading(session, user, review.fingerprint) == []
    await session.flush()
    return user


def pipeline(market_data=None, broker=None, notifier=None):
    async def factory(_session, _user):
        return broker

    return ExecutionPipeline(
        market_data or FakeMarketData(), broker_factory=factory if broker else None, notifier=notifier or NullNotifier()
    )


# ------------------------------------------------------------------ paper


async def test_paper_trade_happy_path_and_notification(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, signal())
    notifier = NullNotifier()
    res = await pipeline(notifier=notifier).execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.EXECUTED, res.reason
    o = res.order
    assert o.status == OrderStatus.FILLED and o.price == D("0.55") and o.quantity >= 1
    trade = (await session.execute(select(PaperTrade))).scalar_one()
    assert trade.entry_price == D("0.55") and trade.fees > 0
    pos = (await session.execute(select(Position))).scalar_one()
    assert pos.mode == TradingMode.PAPER and pos.quantity == D(o.quantity)
    assert notifier.sent and "PAPER TRADE EXECUTED" in notifier.sent[0][1]
    assert "estimates, not guarantees" in notifier.sent[0][1]
    assert "guaranteed" not in notifier.sent[0][1].lower().replace("not guarantees", "")
    # Settlement updates P&L
    await settle_paper_market(session, TICKER, result_yes=True)
    await session.refresh(trade)
    assert trade.pnl == (D(1) - D("0.55")) * trade.quantity - trade.fees


async def test_duplicate_signal_never_creates_second_order(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, signal())
    p = pipeline()
    first = await p.execute(session, user, sig, TradingMode.PAPER)
    second = await p.execute(session, user, sig, TradingMode.PAPER)
    assert first.outcome == Outcome.EXECUTED and second.outcome == Outcome.DUPLICATE
    assert (await session.execute(select(func.count(Order.id)))).scalar_one() == 1


@pytest.mark.parametrize(
    ("md", "sig_kw", "failed"),
    [
        (FakeMarketData(mkt=market(status="closed")), {}, "market_open"),
        (FakeMarketData(mkt=market(close_in=timedelta(seconds=30))), {}, "market_not_closing"),
        (FakeMarketData(ob=book(at=utcnow() - timedelta(minutes=10))), {}, "orderbook_fresh"),
        (FakeMarketData(ob=book(no_bids=(("0.40", "300"),))), {}, "price_current"),  # ask jumped to 0.60
        (FakeMarketData(ob=book(no_bids=())), {}, "price_available"),
        (FakeMarketData(error=KalshiTimeoutError("t")), {}, "market_data"),
        (FakeMarketData(), {"expires_in": timedelta(seconds=-1)}, "signal_not_expired"),
        (FakeMarketData(), {"prob": "0.57"}, "min_edge"),
        (FakeMarketData(), {"confidence": "0.40"}, "min_confidence"),
        (FakeMarketData(ob=book(yes_bids=(("0.30", "300"),))), {}, "spread"),
        (FakeMarketData(ob=book(no_bids=(("0.45", "2"),))), {}, "top_of_book_depth"),
    ],
)
async def test_any_failed_check_blocks_paper_trade(session, md, sig_kw, failed) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, signal(**sig_kw))
    res = await pipeline(md).execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.REJECTED
    assert failed in res.failed_checks, res.reason
    assert (await session.execute(select(func.count(Order.id)))).scalar_one() == 0


async def test_daily_loss_and_position_limits(session) -> None:
    user = await paper_user(session, mode=RiskMode.LOW)
    session.add(
        PaperTrade(
            user_id=user.id,
            order_id=(await _dummy_order(session, user)).id,
            market_ticker="X-1",
            side=Side.YES,
            quantity=20,
            entry_price=D("0.5"),
            pnl=D("-10"),
            closed_at=utcnow(),
            status="settled",
        )
    )
    await session.flush()
    sig = await persisted_signal(session, signal(prob="0.70"))
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.REJECTED and "daily_loss_limit" in res.failed_checks

    user2 = await paper_user(session, 2002, mode=RiskMode.LOW)
    session.add(
        Position(
            user_id=user2.id,
            mode=TradingMode.PAPER,
            market_ticker=TICKER,
            side=Side.YES,
            quantity=D(10),
            avg_price=D("0.5"),
            cost_basis=D("5"),
        )
    )
    await session.flush()
    sig2 = await persisted_signal(session, signal(prob="0.70"))
    res2 = await pipeline().execute(session, user2, sig2, TradingMode.PAPER)
    assert res2.outcome == Outcome.REJECTED and "position_limit" in res2.failed_checks


async def _dummy_order(session, user) -> Order:
    o = Order(
        user_id=user.id,
        mode=TradingMode.PAPER,
        market_ticker="X-1",
        side=Side.YES,
        quantity=20,
        price=D("0.5"),
        yes_price=D("0.5"),
        status=OrderStatus.FILLED,
        client_order_id=f"dummy-{user.id}",
    )
    session.add(o)
    await session.flush()
    return o


async def test_insufficient_paper_balance(session) -> None:
    user = await paper_user(session)
    from kalshi_ai.services.trading_state import ensure_paper_account

    (await ensure_paper_account(session, user.id)).cash_balance = D("0")
    sig = await persisted_signal(session, signal())
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.REJECTED and "balance_positive" in res.failed_checks


async def test_kill_switches_block_trades(session, monkeypatch) -> None:
    user = await paper_user(session)
    await activate_user_kill_switch(session, user)
    sig = await persisted_signal(session, signal())
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.INELIGIBLE and "user_kill_switch" in res.failed_checks

    admin = await make_user(session, 999, admin=True)
    user2 = await paper_user(session, 2003)
    await set_global_kill_switch(session, admin, True, "drill")
    sig2 = await persisted_signal(session, signal())
    res2 = await pipeline().execute(session, user2, sig2, TradingMode.PAPER)
    assert res2.outcome == Outcome.INELIGIBLE and "global_kill_switch" in res2.failed_checks


async def test_kill_switch_activated_mid_pipeline_is_caught_by_final_validation(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, signal())

    class KillDuringFetch(FakeMarketData):
        async def get_orderbook(self, ticker: str):
            await activate_user_kill_switch(session, user)  # user hits /stop while the pipeline runs
            await session.flush()
            return await super().get_orderbook(ticker)

    res = await pipeline(KillDuringFetch()).execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.REJECTED and "final_user_kill" in res.failed_checks


async def test_expired_subscription_blocks(session) -> None:
    user = await paper_user(session)
    from kalshi_ai.db.models import Subscription

    for sub in (await session.execute(select(Subscription))).scalars():
        sub.expires_at = utcnow() - timedelta(seconds=1)
    await session.flush()
    sig = await persisted_signal(session, signal())
    res = await pipeline().execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.INELIGIBLE and "subscription" in res.failed_checks


async def test_watch_signal_never_trades(session) -> None:
    from kalshi_ai.domain.enums import SignalAction

    user = await paper_user(session)
    sig = await persisted_signal(session, signal(action=SignalAction.WATCH))
    assert (await pipeline().execute(session, user, sig, TradingMode.PAPER)).outcome == Outcome.REJECTED


async def test_telegram_outage_does_not_affect_trade(session) -> None:
    user = await paper_user(session)
    sig = await persisted_signal(session, signal())
    res = await pipeline(notifier=FailingNotifier()).execute(session, user, sig, TradingMode.PAPER)
    assert res.outcome == Outcome.EXECUTED


def test_ioc_fill_walks_book_and_respects_limit() -> None:
    b = book(no_bids=(("0.45", "5"), ("0.44", "5"), ("0.40", "100")))
    fill = simulate_ioc_fill(b, Side.YES, 20, D("0.56"))
    assert fill.filled == 10 and fill.avg_price == D("0.555")
    assert simulate_ioc_fill(b, Side.YES, 20, D("0.50")).filled == 0


# ------------------------------------------------------------------ live


async def test_live_requires_every_precondition(session) -> None:
    user = await make_user(session, 3100)
    await give_subscription(session, user, Plan.PRO)  # plan without live trading
    sig = await persisted_signal(session, signal())
    res = await pipeline(broker=FakeBroker()).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.INELIGIBLE
    assert {
        "subscription",
        "auto_trading_enabled",
        "live_trading_enabled",
        "risk_confirmed",
        "kalshi_connected",
    } <= set(res.failed_checks)


async def test_live_order_happy_path(session) -> None:
    user = await live_user(session)
    sig = await persisted_signal(session, signal())
    broker = FakeBroker()
    res = await pipeline(broker=broker).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.EXECUTED, res.reason
    assert len(broker.placed) == 1 and broker.closed == 1
    intent = broker.placed[0]
    assert intent.client_order_id == res.order.client_order_id and intent.limit_price == D("0.55")
    assert res.order.status == OrderStatus.FILLED and res.order.kalshi_order_id == "ord-1"
    pos = (await session.execute(select(Position).where(Position.mode == TradingMode.LIVE.value))).scalar_one()
    assert pos.quantity == D(res.order.quantity)
    # Replay of the same signal is a duplicate and never reaches the exchange.
    again = await pipeline(broker=broker).execute(session, user, sig, TradingMode.LIVE)
    assert again.outcome == Outcome.DUPLICATE and len(broker.placed) == 1


async def test_live_insufficient_balance(session) -> None:
    user = await live_user(session)
    sig = await persisted_signal(session, signal())
    broker = FakeBroker(balance="0")
    res = await pipeline(broker=broker).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.REJECTED and "balance_positive" in res.failed_checks and broker.placed == []


async def test_live_timeout_reconciles_without_resubmitting(session) -> None:
    user = await live_user(session)
    sig = await persisted_signal(session, signal())
    broker = FakeBroker(mode="timeout_but_placed")
    res = await pipeline(broker=broker).execute(session, user, sig, TradingMode.LIVE)
    assert len(broker.placed) == 1
    assert res.order.status == OrderStatus.FILLED and res.order.kalshi_order_id == "ord-1"

    user2 = await live_user(session, 3002)
    sig2 = await persisted_signal(session, signal())
    broker2 = FakeBroker(mode="timeout_not_placed")
    res2 = await pipeline(broker=broker2).execute(session, user2, sig2, TradingMode.LIVE)
    assert len(broker2.placed) == 1 and res2.order.status == OrderStatus.CANCELED


async def test_live_exchange_rejection(session) -> None:
    user = await live_user(session)
    sig = await persisted_signal(session, signal())
    res = await pipeline(broker=FakeBroker(mode="reject")).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.REJECTED and res.order.status == OrderStatus.REJECTED


async def test_changing_risk_after_confirmation_blocks_live(session) -> None:
    user = await live_user(session)
    await set_risk_mode(session, user, RiskMode.PASSIVE)
    sig = await persisted_signal(session, signal())
    broker = FakeBroker()
    res = await pipeline(broker=broker).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.INELIGIBLE and broker.placed == []


async def test_disconnected_kalshi_blocks_live(session) -> None:
    user = await live_user(session)
    conn = (await session.execute(select(KalshiConnection))).scalar_one()
    await session.delete(conn)
    await session.flush()
    sig = await persisted_signal(session, signal())
    res = await pipeline(broker=FakeBroker()).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.INELIGIBLE and "kalshi_connected" in res.failed_checks


async def test_platform_live_switch_off_blocks(session, monkeypatch) -> None:
    user = await live_user(session)
    monkeypatch.setenv("LIVE_TRADING", "false")
    from kalshi_ai.config import get_settings

    get_settings.cache_clear()
    sig = await persisted_signal(session, signal())
    res = await ExecutionPipeline(FakeMarketData(), settings=get_settings()).execute(
        session, user, sig, TradingMode.LIVE
    )
    assert res.outcome == Outcome.INELIGIBLE and "platform_live_enabled" in res.failed_checks


async def test_emergency_stop_cancels_only_our_open_orders(session) -> None:
    user = await live_user(session)
    broker = FakeBroker()
    from kalshi_ai.kalshi.models import KalshiOrder

    ours = Order(
        user_id=user.id,
        mode=TradingMode.LIVE,
        market_ticker=TICKER,
        side=Side.YES,
        quantity=5,
        price=D("0.5"),
        yes_price=D("0.5"),
        status=OrderStatus.RESTING,
        client_order_id="c-ours",
        kalshi_order_id="k-ours",
    )
    session.add(ours)
    await session.flush()
    broker.remote = {
        "c-ours": KalshiOrder(order_id="k-ours", ticker=TICKER, status="resting"),
        "c-manual": KalshiOrder(
            order_id="k-manual", ticker=TICKER, status="resting"
        ),  # placed by the user on kalshi.com
    }

    async def factory(_s, _u):
        return broker

    await activate_user_kill_switch(session, user)
    stats = await cancel_open_orders(session, user, factory)
    assert broker.canceled == ["k-ours"] and stats["live_canceled"] == 1
    assert ours.status == OrderStatus.CANCELED
    rs = await get_risk_settings(session, user.id)
    assert rs.live_trading_enabled is False and rs.auto_trading_enabled is False
    sig = await persisted_signal(session, signal())
    res = await pipeline(broker=FakeBroker()).execute(session, user, sig, TradingMode.LIVE)
    assert res.outcome == Outcome.INELIGIBLE


async def test_concurrent_duplicate_processing(sessionmaker) -> None:
    """Two workers racing on the same signal: exactly one order exists afterwards."""
    async with sessionmaker() as s:
        user = await paper_user(s, 2500)
        sig = await persisted_signal(s, signal())
        await s.commit()

    async def run():
        async with sessionmaker() as s:
            from kalshi_ai.db.models import User

            u = await s.get(User, user.id)
            r = await pipeline().execute(s, u, sig, TradingMode.PAPER)
            await s.commit()
            return r.outcome

    if sessionmaker.kw["bind"].dialect.name == "postgresql":
        outcomes = list(await asyncio.gather(run(), run()))  # real race; unique constraint must hold
    else:
        outcomes = [await run(), await run()]  # SQLite's single shared connection serializes
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(Order.id)))).scalar_one() == 1
    assert sorted(outcomes) == sorted([Outcome.EXECUTED, Outcome.DUPLICATE])
