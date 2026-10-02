from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest

from kalshi_ai.backtest.engine import BacktestEngine, HistoricalDataset, HistoricalFrame, LookAheadError
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import Order, Prediction, SourceHealth
from kalshi_ai.domain.enums import ModelStatus, OrderStatus, RiskMode, Severity, Side, TradingMode
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.modeling.registry import ModelRegistry, RegisteredModel
from kalshi_ai.monitoring.drift import apply_safety_actions, run_checks
from kalshi_ai.services.kill_switch import LIVE_TRADING_SUSPENDED, get_flag
from tests.acceptance.test_full_flow import FixedModel
from tests.conftest import make_user
from tests.factories import book, candles, market

# ------------------------------------------------------------------ monitoring


async def test_model_worse_than_market_suspends_live_trading(session) -> None:
    now = utcnow()
    for i in range(60):
        outcome = i % 2 == 0
        session.add(
            Prediction(
                market_ticker=f"M-{i}",
                ts=now - timedelta(hours=1),
                model_version="m",
                data_versions={},
                confidence=0.7,
                probability_yes=D("0.2") if outcome else D("0.8"),  # confidently wrong
                market_probability_yes=D("0.6") if outcome else D("0.4"),
                outcome_yes=outcome,
            )
        )
    await session.flush()
    alerts = await run_checks(session)
    assert any(a.type == "model_worse_than_market" and a.severity == Severity.CRITICAL for a in alerts)
    assert await apply_safety_actions(session, alerts) is True
    assert (await get_flag(session, LIVE_TRADING_SUSPENDED)).active
    assert await apply_safety_actions(session, alerts) is False  # already suspended; never auto-cleared


async def test_data_quality_and_trading_error_alerts(session) -> None:
    user = await make_user(session, 4001)
    for name, status in (("a", "down"), ("b", "stale"), ("c", "ok"), ("d", "disabled")):
        session.add(SourceHealth(source=name, status=status, consecutive_failures=3, latency_ms=9000))
    for i in range(6):
        session.add(
            Order(
                user_id=user.id,
                mode=TradingMode.LIVE,
                market_ticker="T",
                side=Side.YES,
                quantity=1,
                price=D("0.5"),
                yes_price=D("0.5"),
                client_order_id=f"e{i}",
                status=OrderStatus.FAILED if i < 3 else OrderStatus.FILLED,
            )
        )
    await session.flush()
    types = {a.type: a.severity for a in await run_checks(session)}
    assert types["data_quality"] == Severity.CRITICAL  # 2 of 3 configured sources unhealthy
    assert types["trading_errors"] == Severity.CRITICAL
    assert types["source_latency"] == Severity.WARNING


async def test_healthy_system_produces_no_critical_alerts(session) -> None:
    session.add(SourceHealth(source="ok", status="ok", consecutive_failures=0, latency_ms=50))
    await session.flush()
    alerts = await run_checks(session)
    assert not [a for a in alerts if a.severity == Severity.CRITICAL]
    assert await apply_safety_actions(session, alerts) is False


# ------------------------------------------------------------------ backtesting


def dataset(result_yes: bool = True, n_frames: int = 3) -> HistoricalDataset:
    start = utcnow() - timedelta(hours=2)
    frames = []
    for i in range(n_frames):
        t = start + timedelta(minutes=i)
        m = market(close_in=timedelta(minutes=30))
        m = m.model_copy(update={"close_time": t + timedelta(minutes=30), "open_time": t - timedelta(minutes=5)})
        frames.append(HistoricalFrame(as_of=t, market=m, orderbook=book(at=t - timedelta(seconds=1))))
    return HistoricalDataset(
        frames={frames[0].market.ticker: frames},
        results={frames[0].market.ticker: result_yes},
        candles={"BTC-USD": candles(200, end=start + timedelta(hours=1))},
    )


def fixed_engine(p: float = 0.70) -> PredictionEngine:
    return PredictionEngine(ModelRegistry([RegisteredModel(FixedModel(p), 1.0, ModelStatus.ACTIVE)]))


async def test_backtest_report_and_settlement() -> None:
    report = await BacktestEngine(dataset(True), risk_mode=RiskMode.PASSIVE, prediction_engine=fixed_engine()).run()
    d = report.as_dict()
    assert d["trade_count"] >= 1 and all(t.won for t in report.trades)
    assert D(d["simulated_pnl"]) > 0
    for key in ("win_rate", "max_drawdown", "expected_value_per_trade", "brier_score", "prediction_calibration"):
        assert key in d
    assert "Past performance" in d["disclaimer"]
    losing = await BacktestEngine(dataset(False), prediction_engine=fixed_engine()).run()
    assert D(losing.as_dict()["simulated_pnl"]) < 0


async def test_backtest_respects_risk_limits() -> None:
    report = await BacktestEngine(
        dataset(True, n_frames=30), risk_mode=RiskMode.LOW, prediction_engine=fixed_engine()
    ).run()
    total_contracts = sum(t.quantity for t in report.trades)
    assert total_contracts <= 10  # LOW: max 10 contracts per market
    assert report.blocked_by_risk  # later frames were blocked by position/exposure limits


async def test_backtest_rejects_future_orderbook() -> None:
    ds = dataset()
    frames = next(iter(ds.frames.values()))
    f = frames[0]
    frames[0] = HistoricalFrame(as_of=f.as_of, market=f.market, orderbook=book(at=f.as_of + timedelta(seconds=5)))
    with pytest.raises(LookAheadError):
        await BacktestEngine(ds, prediction_engine=fixed_engine()).run()


async def test_backtest_candles_after_step_are_ignored() -> None:
    """Appending future candles must not change any decision."""
    base = await BacktestEngine(dataset(True), prediction_engine=PredictionEngine()).run()
    ds = dataset(True)
    ds.candles["BTC-USD"] = ds.candles["BTC-USD"] + candles(60, start=10.0, end=utcnow() + timedelta(hours=1))
    polluted = await BacktestEngine(ds, prediction_engine=PredictionEngine()).run()
    assert [(t.side, t.quantity, t.price) for t in base.trades] == [
        (t.side, t.quantity, t.price) for t in polluted.trades
    ]
