"""Learned baselines: logistic regression (coefficients stored as JSON) and gradient boosting.

Training uses only resolved predictions' stored feature snapshots (point-in-time),
with a time-ordered split so the evaluation never sees the future.
"""

from __future__ import annotations

import hashlib
import json
import math
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from kalshi_ai.features.engine import FeatureVector
from kalshi_ai.modeling.base import ModelOutput, PredictionModel, clamp_prob, logit

DEFAULT_FEATURES = (
    "market_prob_yes",
    "baseline_prob_yes",
    "ta_momentum",
    "ta_momentum_15",
    "ta_ema_spread_norm",
    "ta_rsi",
    "ta_bb_pct_b",
    "ta_trend_regime",
    "ta_atr_pct",
    "ob_imbalance",
    "ob_trade_flow",
    "ob_signal",
    "news_score",
    "social_weighted_signal",
    "minutes_to_close",
    "data_completeness",
)


def _row(features: dict[str, Any], names: tuple[str, ...]) -> list[float]:
    """Vectorize; logit-transform probabilities; missing -> 0 plus an indicator-free neutral value."""
    out = []
    for n in names:
        v = features.get(n)
        if v is None or (isinstance(v, float) and math.isnan(v)):
            v = 0.5 if n.endswith("prob_yes") else 0.0
        v = float(v)
        if n.endswith("prob_yes"):
            v = logit(v)
        elif n == "ta_rsi":
            v = (v - 50) / 50
        elif n == "minutes_to_close":
            v = math.log1p(max(0.0, v))
        out.append(v)
    return out


@dataclass
class LogisticArtifact:
    feature_names: tuple[str, ...]
    coef: list[float]
    intercept: float
    mean: list[float]
    scale: list[float]

    def to_json(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "coef": self.coef,
            "intercept": self.intercept,
            "mean": self.mean,
            "scale": self.scale,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> LogisticArtifact:
        return cls(tuple(d["feature_names"]), list(d["coef"]), float(d["intercept"]), list(d["mean"]), list(d["scale"]))


class LogisticModel(PredictionModel):
    name = "logistic"

    def __init__(self, artifact: LogisticArtifact | None = None, version: str = "untrained"):
        self.artifact = artifact
        self.version = version

    def is_ready(self) -> bool:
        return self.artifact is not None

    def predict(self, fv: FeatureVector) -> ModelOutput | None:
        if self.artifact is None or fv.get("market_prob_yes") is None:
            return None
        a = self.artifact
        x = np.array(_row(fv.features, a.feature_names))
        z = (x - np.array(a.mean)) / np.where(np.array(a.scale) == 0, 1, np.array(a.scale))
        score = float(np.dot(z, np.array(a.coef)) + a.intercept)
        p = 1 / (1 + math.exp(-max(-30, min(30, score))))
        return ModelOutput(self.id, clamp_prob(p), 0.5 + 0.3 * fv.completeness)


def train_logistic(
    rows: list[dict[str, Any]], outcomes: list[int], names: tuple[str, ...] = DEFAULT_FEATURES, c: float = 0.5
) -> LogisticArtifact:
    from sklearn.linear_model import LogisticRegression

    x = np.array([_row(r, names) for r in rows])
    mean, scale = x.mean(axis=0), x.std(axis=0)
    scale[scale == 0] = 1
    clf = LogisticRegression(C=c, max_iter=2000)
    clf.fit((x - mean) / scale, np.array(outcomes))
    return LogisticArtifact(names, clf.coef_[0].tolist(), float(clf.intercept_[0]), mean.tolist(), scale.tolist())


class GradientBoostingModel(PredictionModel):
    """sklearn HistGradientBoosting. Artifact is a local file whose SHA-256 is pinned in the model registry."""

    name = "gbm"

    def __init__(
        self,
        path: str | None = None,
        sha256: str | None = None,
        version: str = "untrained",
        names: tuple[str, ...] = DEFAULT_FEATURES,
    ):
        self.version = version
        self.names = names
        self._clf: Any = None
        if path and sha256:
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise ValueError("GBM artifact checksum mismatch - refusing to load")
            self._clf = pickle.loads(data)  # noqa: S301 - integrity verified above

    def is_ready(self) -> bool:
        return self._clf is not None

    def predict(self, fv: FeatureVector) -> ModelOutput | None:
        if self._clf is None or fv.get("market_prob_yes") is None:
            return None
        p = float(self._clf.predict_proba(np.array([_row(fv.features, self.names)]))[0][1])
        return ModelOutput(self.id, clamp_prob(p), 0.5 + 0.3 * fv.completeness)


def train_gbm(
    rows: list[dict[str, Any]],
    outcomes: list[int],
    out_dir: str,
    version: str,
    names: tuple[str, ...] = DEFAULT_FEATURES,
) -> tuple[str, str]:
    from sklearn.ensemble import HistGradientBoostingClassifier

    clf = HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=200, l2_regularization=1.0)
    clf.fit(np.array([_row(r, names) for r in rows]), np.array(outcomes))
    data = pickle.dumps(clf)
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    path = str(Path(out_dir) / f"gbm-{version}.pkl")
    Path(path).write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def artifact_json(artifact: LogisticArtifact) -> str:
    return json.dumps(artifact.to_json())
