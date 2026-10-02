"""ModelEvaluator: scores models on resolved, stored feature snapshots and trains learned baselines.

A candidate is only promoted if, on a time-ordered holdout, it beats BOTH the
currently active ensemble and the market's own implied probability (Brier score).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.analytics.metrics import brier_score, log_loss
from kalshi_ai.db.models import FeatureSnapshot, ModelVersion, Prediction
from kalshi_ai.domain.enums import AssetClass, ModelStatus
from kalshi_ai.features.engine import FeatureVector, MarketSpec
from kalshi_ai.modeling.base import PredictionModel
from kalshi_ai.modeling.ml import LogisticModel, train_logistic
from kalshi_ai.services.audit import audit


@dataclass(frozen=True)
class LabeledExample:
    ts: datetime
    features: dict[str, Any]
    outcome: int
    market_prob: float | None


def to_feature_vector(ex: LabeledExample, ticker: str = "HIST") -> FeatureVector:
    spec = MarketSpec(
        AssetClass(ex.features.get("asset_class", "other")),
        None,
        ex.features.get("strike"),
        ex.features.get("market_direction", "unknown"),
    )
    return FeatureVector(
        ticker,
        ex.ts,
        spec,
        ex.features,
        {},
        "",
        float(ex.features.get("data_age_seconds") or 0),
        float(ex.features.get("data_completeness") or 0),
    )


async def load_examples(session: AsyncSession, limit: int = 50_000) -> list[LabeledExample]:
    rows = (
        await session.execute(
            select(Prediction, FeatureSnapshot)
            .join(FeatureSnapshot, FeatureSnapshot.id == Prediction.feature_id)
            .where(Prediction.outcome_yes.is_not(None))
            .order_by(Prediction.ts)
            .limit(limit)
        )
    ).all()
    seen: set[int] = set()
    out = []
    for pred, snap in rows:
        if snap.id in seen:
            continue
        seen.add(snap.id)
        out.append(
            LabeledExample(
                pred.ts,
                snap.features,
                1 if pred.outcome_yes else 0,
                float(pred.market_probability_yes) if pred.market_probability_yes else None,
            )
        )
    return out


def evaluate_model(model: PredictionModel, examples: list[LabeledExample]) -> dict[str, Any]:
    probs, outcomes, market, market_out = [], [], [], []
    for ex in examples:
        out = model.predict(to_feature_vector(ex))
        if out is None:
            continue
        probs.append(out.probability_yes)
        outcomes.append(ex.outcome)
        if ex.market_prob is not None:
            market.append(ex.market_prob)
            market_out.append(ex.outcome)
    return {
        "n": len(probs),
        "brier": brier_score(probs, outcomes),
        "log_loss": log_loss(probs, outcomes),
        "market_brier": brier_score(market, market_out),
    }


def time_split(examples: list[LabeledExample], holdout_frac: float = 0.3) -> tuple[list, list]:
    ordered = sorted(examples, key=lambda e: e.ts)
    cut = int(len(ordered) * (1 - holdout_frac))
    return ordered[:cut], ordered[cut:]


async def train_and_register_logistic(
    session: AsyncSession, version: str, min_examples: int = 500
) -> ModelVersion | None:
    examples = await load_examples(session)
    if len(examples) < min_examples:
        return None
    train, holdout = time_split(examples)
    if len({e.outcome for e in train}) < 2:
        return None
    artifact = train_logistic([e.features for e in train], [e.outcome for e in train])
    metrics = evaluate_model(LogisticModel(artifact, version), holdout)
    row = ModelVersion(
        name="logistic",
        version=version,
        kind="logistic",
        status=ModelStatus.SHADOW,
        params={"weight": 1.0, "train_n": len(train), "holdout_n": len(holdout)},
        metrics=metrics,
        artifact=artifact.to_json(),
    )
    session.add(row)
    await session.flush()
    await audit(
        session,
        "model.trained",
        actor_type="system",
        target_type="model_version",
        target_id=row.id,
        details={"name": "logistic", "version": version, "metrics": metrics},
    )
    return row


def should_promote(candidate: dict[str, Any], incumbent: dict[str, Any], min_n: int = 200) -> bool:
    if (candidate.get("n") or 0) < min_n or candidate.get("brier") is None:
        return False
    market = candidate.get("market_brier")
    beats_market = market is None or candidate["brier"] < market
    beats_incumbent = incumbent.get("brier") is None or candidate["brier"] < incumbent["brier"]
    return beats_market and beats_incumbent
