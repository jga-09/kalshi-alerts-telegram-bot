"""PredictionEngine: features -> ensemble probability ESTIMATE with evidence and confidence."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import FeatureSnapshot, Prediction
from kalshi_ai.domain.enums import ConfidenceLevel
from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.modeling.base import ModelOutput, clamp_prob, logit, sigmoid
from kalshi_ai.modeling.llm import LLMAnalysis, LLMQualitativeAnalyzer
from kalshi_ai.modeling.registry import ModelRegistry


def confidence_level(conf: float) -> ConfidenceLevel:
    if conf >= 0.75:
        return ConfidenceLevel.HIGH
    if conf >= 0.55:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


@dataclass
class PredictionResult:
    market_ticker: str
    probability_yes: float
    confidence: float
    confidence_level: ConfidenceLevel
    model_version: str
    outputs: list[ModelOutput]
    shadow_outputs: list[ModelOutput]
    supporting: list[str]
    conflicting: list[str]
    data_age_seconds: float
    llm: LLMAnalysis | None = None
    features: FeatureVector | None = None
    prediction_id: uuid.UUID | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def market_probability_yes(self) -> float | None:
        return self.features.get("market_prob_yes") if self.features else None


class PredictionEngine:
    def __init__(self, registry: ModelRegistry | None = None, llm: LLMQualitativeAnalyzer | None = None):
        self.registry = registry or ModelRegistry.default()
        self.llm = llm

    async def predict(self, fv: FeatureVector, headlines: list[str] | None = None) -> PredictionResult | None:
        outputs: list[tuple[ModelOutput, float]] = []
        for entry in self.registry.active:
            out = entry.model.predict(fv)
            if out is not None:
                outputs.append((out, entry.weight))
        if not outputs:
            return None
        shadow = [o for o in (e.model.predict(fv) for e in self.registry.shadow) if o is not None]

        total_w = sum(w * max(0.05, o.confidence) for o, w in outputs)
        z = sum(w * max(0.05, o.confidence) * logit(o.probability_yes) for o, w in outputs) / total_w
        prob = sigmoid(z)
        probs = [o.probability_yes for o, _ in outputs]
        disagreement = (max(probs) - min(probs)) if len(probs) > 1 else 0.0
        conf = sum(o.confidence for o, _ in outputs) / len(outputs)
        conf *= max(0.3, 1.0 - 2.0 * disagreement)

        llm_result = None
        if self.llm is not None and self.llm.enabled():
            llm_result = await self.llm.analyze(fv, headlines or [])
            if llm_result is not None:
                prob += llm_result.tilt  # already bounded to +/- MAX_LLM_TILT
        prob = clamp_prob(prob)

        supporting = list(dict.fromkeys(s for o, _ in outputs for s in o.supporting))
        conflicting = list(dict.fromkeys(s for o, _ in outputs for s in o.conflicting))
        if disagreement > 0.1:
            conflicting.append(f"models disagree by {disagreement * 100:.0f} percentage points")
        if (fv.get("sources_degraded") or 0) > 0:
            conflicting.append("one or more data sources are stale or unavailable")
        version = "ensemble[" + "+".join(sorted(o.model for o, _ in outputs)) + "]"
        return PredictionResult(
            market_ticker=fv.market_ticker,
            probability_yes=prob,
            confidence=max(0.0, min(1.0, conf)),
            confidence_level=confidence_level(conf),
            model_version=version,
            outputs=[o for o, _ in outputs],
            shadow_outputs=shadow,
            supporting=supporting[:8],
            conflicting=conflicting[:8],
            data_age_seconds=fv.data_age_seconds,
            llm=llm_result,
            features=fv,
        )


async def persist_prediction(session: AsyncSession, result: PredictionResult) -> Prediction:
    fv = result.features
    assert fv is not None
    snap = FeatureSnapshot(
        market_ticker=fv.market_ticker,
        ts=fv.as_of,
        feature_set_version=fv.data_versions.get("feature_set", "unknown"),
        features=fv.features,
        data_versions=fv.data_versions,
        feature_hash=fv.feature_hash,
    )
    session.add(snap)
    await session.flush()

    def d(x: float | None) -> Decimal | None:
        return None if x is None else Decimal(str(round(x, 6)))

    market_p = fv.get("market_prob_yes")
    pred = Prediction(
        market_ticker=fv.market_ticker,
        ts=fv.as_of,
        feature_id=snap.id,
        model_version=result.model_version,
        data_versions=fv.data_versions,
        probability_yes=d(result.probability_yes),
        confidence=result.confidence,
        market_probability_yes=d(market_p),
        market_price_yes_ask=d(fv.get("yes_ask")),
        market_price_no_ask=d(fv.get("no_ask")),
        edge=d(result.probability_yes - market_p) if market_p is not None else None,
        data_freshness_seconds=fv.data_age_seconds if fv.data_age_seconds != float("inf") else None,
        analysis={
            "supporting": result.supporting,
            "conflicting": result.conflicting,
            "models": [{"model": o.model, "p": o.probability_yes, "conf": o.confidence} for o in result.outputs],
            "shadow": [{"model": o.model, "p": o.probability_yes} for o in result.shadow_outputs],
            "llm": None
            if result.llm is None
            else {"summary": result.llm.summary, "tilt": result.llm.tilt, "model": result.llm.model},
            "disclaimer": "Model probabilities are estimates, not guarantees.",
        },
    )
    session.add(pred)
    await session.flush()
    result.prediction_id = pred.id
    return pred
