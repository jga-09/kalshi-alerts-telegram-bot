"""Model interface. Models output P(YES) ESTIMATES - never certainties - and never place orders."""

from __future__ import annotations

import abc
import math
from dataclasses import dataclass, field
from typing import Any

from kalshi_ai.features.engine import FeatureVector

PROB_FLOOR, PROB_CEIL = 0.02, 0.98  # a model may never claim (near-)certainty


def clamp_prob(p: float) -> float:
    if math.isnan(p):
        return 0.5
    return min(PROB_CEIL, max(PROB_FLOOR, p))


def logit(p: float) -> float:
    p = clamp_prob(p)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1 / (1 + math.exp(-x))
    e = math.exp(x)
    return e / (1 + e)


@dataclass(frozen=True)
class ModelOutput:
    model: str  # name:version
    probability_yes: float
    confidence: float  # 0..1, model's own view of reliability for this input
    supporting: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


class PredictionModel(abc.ABC):
    name: str
    version: str

    @property
    def id(self) -> str:
        return f"{self.name}:{self.version}"

    def is_ready(self) -> bool:
        return True

    @abc.abstractmethod
    def predict(self, fv: FeatureVector) -> ModelOutput | None:
        """Return None when the model cannot make a responsible estimate for this input."""
