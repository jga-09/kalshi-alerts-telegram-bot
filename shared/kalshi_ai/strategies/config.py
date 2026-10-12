"""Per-user strategy settings: which strategies run, their relative size, and a max entry price.

Stored as JSON on RiskSettings.strategy_config; ``None`` means "use the preset for my risk level".
Size weights are RELATIVE: the largest enabled weight sizes at 100% of what the risk engine allows
and the others scale down from there, so per-strategy sizing can never loosen a risk limit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from kalshi_ai.domain.enums import RiskMode, StrategyId

LABELS: dict[StrategyId, str] = {
    StrategyId.HAMMER: "🔨 Hammer",
    StrategyId.DRIFT: "🌀 Drift",
    StrategyId.FADE: "🎯 Fade",
    StrategyId.SURGE: "⚡ Surge",
}
PRESETS: dict[RiskMode, tuple[StrategyId, ...]] = {
    RiskMode.LOW: (StrategyId.DRIFT, StrategyId.SURGE),
    RiskMode.PASSIVE: (StrategyId.HAMMER, StrategyId.DRIFT, StrategyId.SURGE),
    RiskMode.RISKY: (StrategyId.HAMMER, StrategyId.DRIFT, StrategyId.FADE, StrategyId.SURGE),
}
WEIGHT_CHOICES = (Decimal("0.5"), Decimal("1"), Decimal("1.5"), Decimal("2"))
MAX_PRICE_CHOICES = (50, 60, 75, 90, 95)  # cents
DEFAULT_MAX_PRICE_CENTS = 90
ONE = Decimal(1)


@dataclass(frozen=True)
class StrategyConfig:
    enabled: frozenset[StrategyId]
    weights: dict[StrategyId, Decimal] = field(default_factory=dict)
    max_price_cents: int = DEFAULT_MAX_PRICE_CENTS
    customized: bool = False

    @property
    def max_price(self) -> Decimal:
        return Decimal(self.max_price_cents) / 100

    def weight(self, strategy: StrategyId) -> Decimal:
        return self.weights.get(strategy, ONE)

    def size_factor(self, strategy: StrategyId) -> Decimal:
        """0 < factor <= 1, applied to the risk engine's Kelly sizing (all hard caps still apply)."""
        if strategy not in self.enabled:
            return Decimal(0)
        top = max(self.weight(s) for s in self.enabled)
        return (self.weight(strategy) / top).quantize(Decimal("0.0001"))

    def select(self, available: set[StrategyId]) -> list[StrategyId]:
        """Strategies to trade this user, given the signals that fired for one market.

        With both Drift and Surge on, a setup strong enough for Surge is traded once, as Surge.
        """
        chosen = available & self.enabled
        if StrategyId.SURGE in chosen:
            chosen.discard(StrategyId.DRIFT)
        return sorted(chosen, key=lambda s: list(StrategyId).index(s))

    def to_json(self) -> dict[str, Any]:
        return {
            "enabled": sorted(s.value for s in self.enabled),
            "weights": {s.value: str(w) for s, w in self.weights.items()},
            "max_price_cents": self.max_price_cents,
        }


def preset(mode: RiskMode) -> StrategyConfig:
    return StrategyConfig(enabled=frozenset(PRESETS[mode]))


def load_strategy_config(raw: dict[str, Any] | None, mode: RiskMode) -> StrategyConfig:
    if not raw:
        return preset(mode)
    enabled = frozenset(StrategyId(s) for s in raw.get("enabled", []) if s in StrategyId._value2member_map_)
    weights = {}
    for k, v in (raw.get("weights") or {}).items():
        if k in StrategyId._value2member_map_:
            w = Decimal(str(v))
            weights[StrategyId(k)] = w if w in WEIGHT_CHOICES else ONE
    cents = int(raw.get("max_price_cents", DEFAULT_MAX_PRICE_CENTS))
    cents = cents if 1 <= cents <= 99 else DEFAULT_MAX_PRICE_CENTS
    return StrategyConfig(enabled=enabled, weights=weights, max_price_cents=cents, customized=True)


def toggled(cfg: StrategyConfig, strategy: StrategyId) -> StrategyConfig:
    enabled = set(cfg.enabled)
    enabled.symmetric_difference_update({strategy})
    return StrategyConfig(frozenset(enabled), dict(cfg.weights), cfg.max_price_cents, True)


def with_next_weight(cfg: StrategyConfig, strategy: StrategyId) -> StrategyConfig:
    current = cfg.weight(strategy)
    idx = WEIGHT_CHOICES.index(current) if current in WEIGHT_CHOICES else 1
    weights = dict(cfg.weights)
    weights[strategy] = WEIGHT_CHOICES[(idx + 1) % len(WEIGHT_CHOICES)]
    return StrategyConfig(cfg.enabled, weights, cfg.max_price_cents, True)


def with_max_price(cfg: StrategyConfig, cents: int) -> StrategyConfig:
    if cents not in MAX_PRICE_CHOICES:
        raise ValueError(f"max price must be one of {MAX_PRICE_CHOICES}")
    return StrategyConfig(cfg.enabled, dict(cfg.weights), cents, True)
