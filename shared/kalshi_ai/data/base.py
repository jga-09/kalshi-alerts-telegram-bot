"""Data-source abstractions.

Every data point carries source, timestamp, symbol, value, reliability and
freshness. Connectors never invent values: when a source is missing, disabled
or stale the pipeline sees *absence* and lowers confidence accordingly.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kalshi_ai.db.base import utcnow


@dataclass(frozen=True)
class DataPoint:
    source: str
    symbol: str
    metric: str
    ts: datetime  # time the observation refers to (exchange/publication time)
    value: float
    reliability: float = 1.0  # 0..1 prior trust in this source
    collected_at: datetime = field(default_factory=utcnow)
    extra: dict[str, Any] = field(default_factory=dict)

    def age_seconds(self, now: datetime | None = None) -> float:
        return max(0.0, ((now or utcnow()) - self.ts).total_seconds())

    def freshness(self, max_age_seconds: float, now: datetime | None = None) -> float:
        """1.0 = brand new, 0.0 = at/over max age."""
        if max_age_seconds <= 0:
            return 0.0
        return max(0.0, 1.0 - self.age_seconds(now) / max_age_seconds)


@dataclass(frozen=True)
class Candle:
    ts: datetime  # candle OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FetchResult:
    source: str
    ok: bool
    points: list[DataPoint] = field(default_factory=list)
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)  # news/social raw items
    error: str | None = None
    latency_ms: float | None = None
    configured: bool = True


class DataConnector(abc.ABC):
    """Base connector. Subclasses must only call APIs they are licensed/permitted to use."""

    name: str = "base"
    reliability: float = 0.5
    stale_after_seconds: int = 300

    @abc.abstractmethod
    def is_configured(self) -> bool: ...

    @abc.abstractmethod
    async def fetch(self) -> FetchResult: ...

    def not_configured(self, reason: str) -> FetchResult:
        return FetchResult(source=self.name, ok=False, error=reason, configured=False)
