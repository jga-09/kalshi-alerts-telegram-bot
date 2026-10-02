"""FINAL ACCEPTANCE TEST - the 25-step customer journey plus failure cases.

External systems are simulated at their boundaries only:
* Kalshi REST -> respx HTTP mocks (real KalshiClient/services/signing code runs)
* Stripe -> real HMAC-signed webhook payloads (real verification code runs)
* Telegram -> recording session (real aiogram dispatcher/handlers run)
* Exchange order placement -> FakeBroker implementing the LiveBroker protocol
The model is a deterministic stub so the test checks the plumbing, not model skill.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest
import respx
from sqlalchemy import func, select

from kalshi_ai.config import get_settings
from kalshi_ai.data.base import Candle, DataConnector, FetchResult
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import (
    FeatureSnapshot,
    KalshiConnection,
    NewsArticle,
    Order,
    PaperTrade,
    Position,
    Prediction,
    RiskSettings,
    Signal,
    SocialPost,
    SourceHealth,
    User,
)
from kalshi_ai.domain.enums import ModelStatus, OrderStatus, TradingMode
from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.kalshi.client import KalshiClient
from kalshi_ai.kalshi.services import KalshiMarketService
from kalshi_ai.modeling.base import ModelOutput, PredictionModel
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.modeling.registry import ModelRegistry, RegisteredModel
from kalshi_ai.notifications.messages import NullNotifier
from kalshi_ai.services.analysis import AnalysisService
from kalshi_ai.services.ingestion import store_candles
from kalshi_ai.services.kalshi_connections import issue_connect_token
from kalshi_ai_worker import jobs
from tests.api.test_bot import RecordingSession  # noqa: F401 - fixture helper reuse
from tests.conftest import auth_headers
from tests.factories import FakeBroker, candles
from tests.unit.test_kalshi_integration import rsa_pem
from tests.unit.test_stripe_webhooks import sign, sub_event

pytestmark = pytest.mark.acceptance
BASE = "https://kalshi.test/trade-api/v2"
T = "KXBTC15M-26OCT021500-T"


class FixedModel(PredictionModel):
    name, version = "fixed_test_model", "1"

    def __init__(self, p: float = 0.70, conf: float = 0.85):
        self.p, self.conf = p, conf

    def predict(self, fv: FeatureVector) -> ModelOutput | None:
        return ModelOutput(self.id, self.p, self.conf, ["test evidence"], [])


class StaticConnector(DataConnector):
    def __init__(self, name: str, items: list[dict]):
        self.name, self.items = name, items

    def is_configured(self) -> bool:
        return True

    async def fetch(self) -> FetchResult:
        return FetchResult(source=self.name, ok=True, items=self.items)


def mock_kalshi(router: respx.MockRouter, *, status: str = "active", result: str = "", yes_ask: str = "0.55") -> None:
    now = utcnow()
    no_bid = str(D(1) - D(yes_ask))
    market = {
        "ticker": T,
        "event_ticker": "KXBTC15M-26OCT021500",
        "title": "BTC 15m UP/DOWN",
        "status": status,
        "open_time": (now - timedelta(minutes=5)).isoformat(),
        "close_time": (now + timedelta(minutes=10)).isoformat(),
        "yes_bid_dollars": "0.53",
        "yes_ask_dollars": yes_ask,
        "no_bid_dollars": no_bid,
        "no_ask_dollars": "0.47",
        "last_price_dollars": "0.54",
        "volume_fp": "5000.00",
        "result": result,
    }
    router.get(f"{BASE}/markets/{T}").mock(return_value=httpx.Response(200, json={"market": market}))
    router.get(f"{BASE}/markets/{T}/orderbook").mock(
        return_value=httpx.Response(
            200, json={"orderbook_fp": {"yes_dollars": [["0.5300", "300.00"]], "no_dollars": [[no_bid, "300.00"]]}}
        )
    )
    router.get(f"{BASE}/markets/trades").mock(return_value=httpx.Response(200, json={"trades": [], "cursor": ""}))
    router.get(f"{BASE}/markets").mock(return_value=httpx.Response(200, json={"markets": [market], "cursor": ""}))


@respx.mock(assert_all_called=False)
async def test_full_customer_journey(client, sessionmaker, redis, respx_mock, monkeypatch) -> None:
    import itertools
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.types import Chat, Message, Update
    from aiogram.types import User as TgUser

    from kalshi_ai_bot.container import BotContainer
    from kalshi_ai_bot.main import build_dispatcher
    from tests.api.test_bot import RecordingSession as Rec  # local import keeps fixture ordering simple
    from tests.api.test_bot import _ids  # noqa: F401

    settings = get_settings()
    counter = itertools.count(10_000)
    rec = Rec()
    bot = Bot("123456:TEST-telegram-token-abcdefghijklmnopqrstuvwxyz", session=rec)
    dp = build_dispatcher(
        BotContainer(
            settings=settings,
            redis=redis,
            market_service=None,
            analysis=None,  # type: ignore[arg-type]
            cipher=None,
        )
    )
    TG_ID = 77001

    async def tg(text: str) -> str:
        msg = Message(
            message_id=next(counter),
            date=datetime.now(UTC),
            chat=Chat(id=TG_ID, type="private"),
            from_user=TgUser(id=TG_ID, is_bot=False, first_name="Ada"),
            text=text,
        )
        await dp.feed_update(bot, Update(update_id=next(counter), message=msg))
        return rec.texts[-1]

    # 1. New Telegram user
    assert "subscription is inactive" in await tg("/start")
    async with sessionmaker() as s:
        user = (await s.execute(select(User).where(User.telegram_user_id == TG_ID))).scalar_one()
        user_id = user.id

    # 2-3. Subscription purchase -> Stripe webhook (signature-verified)
    payload = json.dumps(sub_event("evt_acc_1", str(user_id))).encode()
    r = await client.post("/api/stripe/webhook", content=payload, headers={"Stripe-Signature": sign(payload)})
    assert r.status_code == 200 and r.json()["status"] == "processed"

    # 4. Subscription activation
    async with sessionmaker() as s:
        user = await s.get(User, user_id)
    me = (await client.get("/api/auth/me", headers=auth_headers(user))).json()
    assert me["subscription"]["active"] and me["subscription"]["plan"] == "auto"

    # 5-6. Connect Kalshi + verification (real signing code against mocked Kalshi)
    async with sessionmaker() as s:
        token = await issue_connect_token(s, await s.get(User, user_id))
        await s.commit()
    pem, _ = rsa_pem()
    respx_mock.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(200, json={"balance": 50000}))
    respx_mock.get(f"{BASE}/api_keys").mock(
        return_value=httpx.Response(
            200, json={"api_keys": [{"api_key_id": "kid-acceptance1", "scopes": ["read", "write::trade"]}]}
        )
    )
    r = await client.post(
        "/api/kalshi/connect",
        json={"token": token, "api_key_id": "kid-acceptance1", "private_key_pem": pem, "environment": "demo"},
    )
    assert r.status_code == 200 and r.json()["status"] == "verified" and r.json()["can_trade"]

    # 7. User starts paper trading
    assert "Paper trading is ON" in await tg("/paper")

    # 8-11. Market data, news, social arrive; technicals computable
    mock_kalshi(respx_mock)
    now = utcnow()
    await store_candles(redis, "BTC-USD", candles(120))
    news = StaticConnector(
        "rss_news",
        [
            {
                "title": "Bitcoin rallies as ETF inflows jump",
                "url": "https://n.example/1",
                "source": "Reuters",
                "published_at": (now - timedelta(minutes=5)).isoformat(),
            }
        ],
    )
    social = StaticConnector(
        "reddit",
        [
            {
                "platform": "reddit",
                "external_id": "p1",
                "author": "analyst",
                "author_age_days": 900,
                "text": "Bitcoin strength continues, buyers active",
                "posted_at": (now - timedelta(minutes=3)).isoformat(),
                "engagement": 40,
            }
        ],
    )
    market_svc = KalshiMarketService(KalshiClient(BASE, min_interval_seconds=0))

    async def candle_source(symbol: str) -> list[Candle]:
        from kalshi_ai.services.ingestion import load_candles

        return await load_candles(redis, symbol)

    registry = ModelRegistry([RegisteredModel(FixedModel(), 1.0, ModelStatus.ACTIVE)])
    notifier = NullNotifier()
    live_broker = FakeBroker(balance="500")

    async def broker_factory(_s, _u):
        return live_broker

    ctx = jobs.JobContext(
        sessionmaker=sessionmaker,
        redis=redis,
        market_service=market_svc,
        analysis=AnalysisService(market_svc, candle_source, PredictionEngine(registry), settings),
        broker_factory=broker_factory,
        notifier=notifier,
        settings=settings,
    )
    ingest = await jobs.ingest_data(ctx, [news, social])
    assert ingest == {"rss_news": True, "reddit": True}
    async with sessionmaker() as s:
        assert (await s.execute(select(func.count(NewsArticle.id)))).scalar_one() == 1
        assert (await s.execute(select(func.count(SocialPost.id)))).scalar_one() == 1
        assert {r.source: r.status for r in (await s.execute(select(SourceHealth))).scalars()}["rss_news"] == "ok"

    # 12-16. AI analysis -> signal -> risk -> paper order -> Telegram notification
    stats = await jobs.scan_markets(ctx, [T])
    assert stats["analyzed"] == 1 and stats["signals"] == 1, stats
    assert stats["executions"] == {"paper:executed": 1}, stats
    async with sessionmaker() as s:
        pred = (await s.execute(select(Prediction))).scalar_one()
        feat = await s.get(FeatureSnapshot, pred.feature_id)
        assert feat.features["ta_available"] == 1 and feat.features["news_unique_stories"] >= 1  # TA + news used
        assert feat.features["social_available"] == 1.0
        assert pred.analysis["disclaimer"].startswith("Model probabilities are estimates")
        sig = (await s.execute(select(Signal))).scalar_one()
        assert sig.edge == D("0.1500")
        order = (await s.execute(select(Order))).scalar_one()
        assert order.mode == TradingMode.PAPER and order.status == OrderStatus.FILLED
        assert order.validation["risk"]["approved"] is True
    trade_msgs = [t for _, t in notifier.sent if "PAPER TRADE EXECUTED" in t]
    assert trade_msgs and "Model probability (estimate)" in trade_msgs[0]

    # 17. P&L updates (mark-to-market, then settlement)
    await jobs.mark_and_settle(ctx)
    async with sessionmaker() as s:
        pos = (await s.execute(select(Position))).scalar_one()
        assert pos.mark_price == D("0.53")  # marked at the YES bid
        assert pos.unrealized_pnl < 0  # crossing the spread costs money - honest accounting

    # 18-19. Enable live trading with explicit confirmation (via the API flow)
    h = auth_headers(user)
    await client.post("/api/disclosures/accept", headers=h)
    assert (await client.post("/api/me/auto-trading", headers=h, json={"enabled": True})).json()["blockers"] == []
    review = (await client.get("/api/me/live-trading/review", headers=h)).json()
    assert review["blockers"] == []
    bad = await client.post(
        "/api/me/live-trading/confirm",
        headers=h,
        json={"fingerprint": review["fingerprint"], "acknowledge_risk": False},
    )
    assert bad.status_code == 400
    ok = await client.post(
        "/api/me/live-trading/confirm", headers=h, json={"fingerprint": review["fingerprint"], "acknowledge_risk": True}
    )
    assert ok.json()["live_trading_enabled"] is True

    # 20-23. Live order passes all checks -> submitted to Kalshi -> status & position update
    stats = await jobs.scan_markets(ctx, [T])  # same minute => same signal => paper is a duplicate
    assert stats["executions"].get("paper:duplicate") == 1
    assert stats["executions"].get("live:executed") == 1, stats
    assert len(live_broker.placed) == 1
    async with sessionmaker() as s:
        live_order = (await s.execute(select(Order).where(Order.mode == TradingMode.LIVE.value))).scalar_one()
        assert live_order.status == OrderStatus.FILLED and live_order.kalshi_order_id == "ord-1"
        live_pos = (await s.execute(select(Position).where(Position.mode == TradingMode.LIVE.value))).scalar_one()
        assert live_pos.quantity == D(live_order.quantity)
        conn = (await s.execute(select(KalshiConnection))).scalar_one()
        assert conn.user_id == user_id  # the order used this customer's own connection
    assert any("LIVE TRADE EXECUTED" in t for _, t in notifier.sent)

    # 24. Emergency stop
    assert "EMERGENCY STOP ACTIVATED" in await tg("/stop")

    # 25. New automated orders are blocked
    async with sessionmaker() as s:
        await s.execute(Signal.__table__.update().values(idempotency_key=Signal.idempotency_key + "-old"))
        await s.commit()
    stats = await jobs.scan_markets(ctx, [T])
    assert stats["executions"] == {}, stats  # user no longer eligible for any mode
    assert len(live_broker.placed) == 1

    # Settlement P&L
    respx_mock.get(f"{BASE}/markets/{T}").mock(
        return_value=httpx.Response(200, json={"market": {"ticker": T, "status": "finalized", "result": "yes"}})
    )
    settled = await jobs.mark_and_settle(ctx)
    assert settled["settled_markets"] == 1 and settled["resolved_predictions"] >= 1
    async with sessionmaker() as s:
        trade = (await s.execute(select(PaperTrade))).scalar_one()
        assert trade.pnl == (D(1) - trade.entry_price) * trade.quantity - trade.fees
        rs = (await s.execute(select(RiskSettings).where(RiskSettings.user_id == user_id))).scalar_one()
        assert rs.kill_switch_active


@respx.mock(assert_all_called=False)
async def test_failure_cases_api_timeout_and_redis_outage(sessionmaker, respx_mock) -> None:
    """Kalshi API timeout and Redis outage: analysis degrades safely; nothing trades."""
    from redis.asyncio import Redis

    respx_mock.get(f"{BASE}/markets/{T}").mock(side_effect=httpx.ConnectTimeout("t"))
    settings = get_settings()
    market_svc = KalshiMarketService(KalshiClient(BASE, max_retries=0, min_interval_seconds=0))
    dead_redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)

    async def candle_source(symbol: str) -> list[Candle]:
        from kalshi_ai.services.ingestion import load_candles

        return await load_candles(dead_redis, symbol)

    ctx = jobs.JobContext(
        sessionmaker=sessionmaker,
        redis=dead_redis,
        market_service=market_svc,
        analysis=AnalysisService(market_svc, candle_source, settings=settings),
        broker_factory=None,
        notifier=None,
        settings=settings,
    )
    stats = await jobs.scan_markets(ctx, [T])
    assert stats == {"analyzed": 0, "signals": 0, "executions": {}}
    # Redis down: candles unavailable -> analysis still possible but without TA (lower confidence).
    mock_kalshi(respx_mock)
    assert await candle_source("BTC-USD") == []
    await dead_redis.aclose()


async def test_database_outage_fails_closed(redis) -> None:
    """With the database unreachable nothing can trade: readiness fails and sessions raise."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from kalshi_ai_api.main import create_app

    engine = create_async_engine("postgresql+asyncpg://nobody:x@127.0.0.1:1/none")
    from kalshi_ai.db.session import set_sessionmaker

    set_sessionmaker(async_sessionmaker(engine), engine)
    app = create_app(get_settings(), manage_resources=False)
    app.state.redis = redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://t") as c:
        r = await c.get("/ready")
        assert r.status_code == 503 and r.json()["checks"]["database"]["status"] == "down"
    await engine.dispose()
