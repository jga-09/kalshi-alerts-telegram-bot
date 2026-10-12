"""ModelRegistry: which models are active (weighted) vs shadow (logged only)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import ModelVersion
from kalshi_ai.domain.enums import ModelStatus
from kalshi_ai.logging import get_logger
from kalshi_ai.modeling.base import PredictionModel
from kalshi_ai.modeling.ml import GradientBoostingModel, LogisticArtifact, LogisticModel
from kalshi_ai.modeling.rule_based import RuleBasedModel

log = get_logger(__name__)


@dataclass
class RegisteredModel:
    model: PredictionModel
    weight: float
    status: ModelStatus


@dataclass
class ModelRegistry:
    entries: list[RegisteredModel] = field(default_factory=list)

    @classmethod
    def default(cls) -> ModelRegistry:
        return cls([RegisteredModel(RuleBasedModel(), 1.0, ModelStatus.ACTIVE)])

    @property
    def active(self) -> list[RegisteredModel]:
        return [e for e in self.entries if e.status == ModelStatus.ACTIVE and e.model.is_ready()]

    @property
    def shadow(self) -> list[RegisteredModel]:
        return [e for e in self.entries if e.status == ModelStatus.SHADOW and e.model.is_ready()]

    def register(self, model: PredictionModel, weight: float = 1.0, status: ModelStatus = ModelStatus.SHADOW) -> None:
        self.entries = [e for e in self.entries if e.model.name != model.name or e.status != status]
        self.entries.append(RegisteredModel(model, weight, status))

    @classmethod
    async def load(cls, session: AsyncSession) -> ModelRegistry:
        registry = cls.default()
        rows = (
            await session.execute(
                select(ModelVersion)
                .where(ModelVersion.status.in_([ModelStatus.ACTIVE.value, ModelStatus.SHADOW.value]))
                .order_by(ModelVersion.created_at)
            )
        ).scalars()
        for row in rows:
            try:
                model: PredictionModel | None = None
                if row.kind == "logistic" and row.artifact:
                    model = LogisticModel(LogisticArtifact.from_json(row.artifact), version=row.version)
                elif row.kind == "gbm" and row.artifact:
                    model = GradientBoostingModel(
                        row.artifact.get("path"), row.artifact.get("sha256"), version=row.version
                    )
                if model is not None:
                    weight = float((row.params or {}).get("weight", 1.0))
                    registry.register(model, weight, row.status)
            except (ValueError, OSError, KeyError) as exc:
                log.error("model_load_failed", name=row.name, version=row.version, error=type(exc).__name__)
        return registry
