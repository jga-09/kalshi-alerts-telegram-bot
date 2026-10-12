from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from kalshi_ai.config import get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.domain.enums import AssetClass, Side, SignalAction
from kalshi_ai.features.engine import FeatureEngine, MarketContext, classify_market
from kalshi_ai.kalshi.models import KalshiTrade
from kalshi_ai.modeling.base import PROB_CEIL, PROB_FLOOR
from kalshi_ai.modeling.llm import MAX_LLM_TILT, LLMQualitativeAnalyzer, parse_analysis
from kalshi_ai.modeling.ml import LogisticModel, train_logistic
from kalshi_ai.modeling.prediction import PredictionEngine
from kalshi_ai.modeling.registry import ModelRegistry
from kalshi_ai.modeling.rule_based import RuleBasedModel
from kalshi_ai.news.engine import NewsEngine
from kalshi_ai.signals.engine import SignalEngine, compute_edges
from tests.factories import book, candles, market


def ctx(**kw) -> MarketContext:
    now = utcnow()
    base = MarketContext(
        as_of=now, market=market(), orderbook=book(at=now - timedelta(seconds=2)), candles={"BTC-USD": candles(120)}
    )
    return replace(base, **kw)


def test_classify_market() -> None:
    spec = classify_market(market(strike_type="greater", floor_strike=61000))
    assert spec.asset_class == AssetClass.CRYPTO and spec.direction == "above" and spec.strike == 61000
    assert (
        classify_market(market(ticker="KXGOLDD-26OCT02-T2400", title="Gold above 2400?")).asset_class == AssetClass.GOLD
    )
    assert classify_market(market()).direction == "up"


def test_feature_snapshot_is_point_in_time() -> None:
    """Injecting data from the future must not change the features at all."""
    c = ctx()
    base = FeatureEngine().build(c)
    future = c.as_of + timedelta(minutes=5)
    future_article = NewsEngine().process(
        [
            {
                "title": "Bitcoin crashes after massive hack",
                "url": "https://n.com/1",
                "source": "Reuters",
                "published_at": (c.as_of - timedelta(minutes=1)).isoformat(),
            }
        ],
        c.as_of,
    )[0]
    future_article = replace(future_article, published_at=future)
    future_candles = candles(130, end=c.as_of + timedelta(minutes=10))
    future_trade = KalshiTrade(
        trade_id="t",
        ticker=c.market.ticker,
        count=D(500),
        yes_price=D("0.9"),
        taker_outcome_side="yes",
        created_time=future,
    )
    polluted = replace(
        c,
        articles=[future_article],
        trades=[future_trade],
        candles={"BTC-USD": candles(120) + [x for x in future_candles if x.ts > c.as_of]},
    )
    after = FeatureEngine().build(polluted)
    assert after.features == base.features
    assert after.feature_hash == base.feature_hash


def test_feature_hash_reproducible_and_fields_present() -> None:
    c = ctx()
    a, b = FeatureEngine().build(c), FeatureEngine().build(c)
    assert a.feature_hash == b.feature_hash
    for key in (
        "market_prob_yes",
        "yes_ask",
        "no_ask",
        "ta_rsi",
        "baseline_prob_yes",
        "data_completeness",
        "ob_imbalance",
        "minutes_to_close",
        "strike",
    ):
        assert key in a.features
    assert a.features["yes_ask"] == pytest.approx(0.55)
    assert 0 < a.completeness <= 1


def test_rule_model_shrinks_to_market_without_data() -> None:
    fv = FeatureEngine().build(ctx(candles={}))
    out = RuleBasedModel().predict(fv)
    assert out is not None
    market_p = fv.get("market_prob_yes")
    assert abs(out.probability_yes - market_p) <= abs(0.5 - market_p) + 0.01
    assert out.confidence < 0.8
    assert RuleBasedModel().predict(FeatureEngine().build(ctx(orderbook=None, candles={}))) is None


def test_probabilities_never_claim_certainty() -> None:
    fv = FeatureEngine().build(ctx())
    extreme = replace(fv, features={**fv.features, "baseline_prob_yes": 0.9999, "market_prob_yes": 0.9999})
    out = RuleBasedModel().predict(extreme)
    assert PROB_FLOOR <= out.probability_yes <= PROB_CEIL


async def test_prediction_engine_and_signal() -> None:
    fv = FeatureEngine().build(ctx())
    pred = await PredictionEngine().predict(fv)
    assert pred is not None and 0 < pred.probability_yes < 1
    assert pred.model_version.startswith("ensemble[rule_based")
    sig = SignalEngine().decide(pred)
    assert sig is not None
    assert sig.idempotency_key == SignalEngine().decide(pred).idempotency_key  # deterministic
    assert sig.expires_at <= fv.as_of + timedelta(minutes=3)


def test_edge_computation_both_sides() -> None:
    edges = {e.side: e for e in compute_edges(0.621, 0.54, 0.47)}
    assert edges[Side.YES].edge == D("0.0810")  # matches the spec example: +8.1pp
    assert edges[Side.NO].edge == D("0.3790") - D("0.47")


def _pred_with(prob: float, conf: float, liquidity_ok: bool = True):
    fv = FeatureEngine().build(ctx())
    fv = replace(fv, features={**fv.features, "liquidity_ok": 1 if liquidity_ok else 0})
    return SimpleNamespace(
        features=fv,
        probability_yes=prob,
        confidence=conf,
        confidence_level=__import__("kalshi_ai.modeling.prediction", fromlist=["x"]).confidence_level(conf),
        model_version="m",
        supporting=[],
        conflicting=[],
        data_age_seconds=1.0,
    )


def test_signal_actions_require_edge_confidence_and_liquidity() -> None:
    eng = SignalEngine()
    assert eng.decide(_pred_with(0.70, 0.8)).action == SignalAction.TRADE_IF_RISK_PASSES
    assert eng.decide(_pred_with(0.56, 0.8)).action == SignalAction.WATCH  # small edge
    assert eng.decide(_pred_with(0.70, 0.3)).action == SignalAction.WATCH  # low confidence
    assert eng.decide(_pred_with(0.70, 0.8, liquidity_ok=False)).action == SignalAction.WATCH
    no_side = eng.decide(_pred_with(0.30, 0.8))
    assert no_side.side == Side.NO and no_side.action == SignalAction.TRADE_IF_RISK_PASSES
    assert eng.decide(_pred_with(0.535, 0.8)).action == SignalAction.NO_TRADE


def test_logistic_training_and_inference() -> None:
    fv = FeatureEngine().build(ctx())
    rows, ys = [], []
    for i in range(300):
        p = 0.2 + 0.6 * (i % 10) / 9
        rows.append({**fv.features, "market_prob_yes": p})
        ys.append(1 if (i * 7919) % 100 < p * 100 else 0)
    artifact = train_logistic(rows, ys)
    model = LogisticModel(artifact, "t1")
    lo = model.predict(replace(fv, features={**fv.features, "market_prob_yes": 0.2}))
    hi = model.predict(replace(fv, features={**fv.features, "market_prob_yes": 0.8}))
    assert lo.probability_yes < hi.probability_yes
    assert json.loads(json.dumps(artifact.to_json()))["feature_names"][0] == "market_prob_yes"


def test_llm_tilt_is_bounded_and_uncertainty_damped() -> None:
    a = parse_analysis(
        {
            "supporting_factors": ["x"] * 20,
            "conflicting_factors": [],
            "qualitative_tilt": "lean_yes",
            "uncertainty": "low",
            "summary": "s" * 2000,
        },
        "m",
    )
    assert a.tilt == MAX_LLM_TILT and len(a.supporting) == 5 and len(a.summary) == 500
    assert (
        parse_analysis(
            {
                "supporting_factors": [],
                "conflicting_factors": [],
                "qualitative_tilt": "lean_no",
                "uncertainty": "high",
                "summary": "",
            },
            "m",
        ).tilt
        == 0
    )
    assert parse_analysis({"qualitative_tilt": "BUY NOW 100%"}, "m") is None


class _FakeLLMClient:
    def __init__(self, response):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))
        self.response = response
        self.calls = []

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


async def test_llm_analyzer_handles_refusal_errors_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_ENABLED", "true")
    get_settings.cache_clear()
    fv = FeatureEngine().build(ctx())
    ok_text = json.dumps(
        {
            "supporting_factors": ["momentum"],
            "conflicting_factors": [],
            "qualitative_tilt": "lean_yes",
            "uncertainty": "medium",
            "summary": "ok",
        }
    )
    ok = SimpleNamespace(
        stop_reason="end_turn", model="claude-opus-5-5", content=[SimpleNamespace(type="text", text=ok_text)]
    )
    client = _FakeLLMClient(ok)
    result = await LLMQualitativeAnalyzer(client=client).analyze(fv, ["Bitcoin rallies"])
    assert result is not None and result.tilt == MAX_LLM_TILT / 2
    sent = client.calls[0]
    assert sent["model"] == "claude-opus-5-5"
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert "tools" not in sent  # the LLM has no tools - it cannot act
    refused = SimpleNamespace(stop_reason="refusal", content=[])
    assert await LLMQualitativeAnalyzer(client=_FakeLLMClient(refused)).analyze(fv, []) is None
    assert await LLMQualitativeAnalyzer(client=_FakeLLMClient(TimeoutError())).analyze(fv, []) is None
    # Enabled LLM shifts the ensemble by at most MAX_LLM_TILT.
    base = await PredictionEngine(ModelRegistry.default()).predict(fv)
    with_llm = await PredictionEngine(ModelRegistry.default(), LLMQualitativeAnalyzer(client=client)).predict(fv)
    assert abs(with_llm.probability_yes - base.probability_yes) <= MAX_LLM_TILT + 1e-9
