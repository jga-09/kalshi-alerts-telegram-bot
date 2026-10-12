"""Risk profiles and effective-limit resolution.

effective = strictest_of(profile defaults, user overrides, HARD global limits)
Users may tighten limits but can never loosen them past the profile or the hard caps.
No profile - including LOW - means "safe".
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields, replace
from decimal import Decimal
from typing import Any

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.domain.enums import RiskMode


@dataclass(frozen=True)
class RiskLimits:
    max_position_contracts: int  # per market
    max_order_notional: Decimal  # dollars per order
    max_daily_loss: Decimal  # dollars
    max_trades_per_day: int
    min_confidence: Decimal  # 0..1
    min_edge: Decimal  # probability points, e.g. 0.05
    max_market_exposure: Decimal  # dollars at risk in one market
    max_correlated_exposure: Decimal  # dollars at risk across one correlation group (series)
    max_spread: Decimal  # dollars
    min_top_of_book_depth: int  # contracts
    kelly_fraction: Decimal  # fraction of full Kelly used as an additional sizing cap

    def as_public_dict(self) -> dict[str, Any]:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.as_public_dict(), sort_keys=True).encode()).hexdigest()


DEFAULT_PROFILES: dict[RiskMode, RiskLimits] = {
    RiskMode.LOW: RiskLimits(
        max_position_contracts=10,
        max_order_notional=Decimal("5"),
        max_daily_loss=Decimal("10"),
        max_trades_per_day=5,
        min_confidence=Decimal("0.70"),
        min_edge=Decimal("0.08"),
        max_market_exposure=Decimal("10"),
        max_correlated_exposure=Decimal("20"),
        max_spread=Decimal("0.04"),
        min_top_of_book_depth=50,
        kelly_fraction=Decimal("0.10"),
    ),
    RiskMode.PASSIVE: RiskLimits(
        max_position_contracts=25,
        max_order_notional=Decimal("15"),
        max_daily_loss=Decimal("30"),
        max_trades_per_day=10,
        min_confidence=Decimal("0.60"),
        min_edge=Decimal("0.06"),
        max_market_exposure=Decimal("30"),
        max_correlated_exposure=Decimal("60"),
        max_spread=Decimal("0.06"),
        min_top_of_book_depth=25,
        kelly_fraction=Decimal("0.20"),
    ),
    RiskMode.RISKY: RiskLimits(
        max_position_contracts=100,
        max_order_notional=Decimal("50"),
        max_daily_loss=Decimal("100"),
        max_trades_per_day=25,
        min_confidence=Decimal("0.55"),
        min_edge=Decimal("0.04"),
        max_market_exposure=Decimal("100"),
        max_correlated_exposure=Decimal("200"),
        max_spread=Decimal("0.08"),
        min_top_of_book_depth=10,
        kelly_fraction=Decimal("0.25"),
    ),
}

# Fields where a LOWER value is stricter; all others (min_*) are stricter when HIGHER.
_UPPER_BOUND_FIELDS = {
    "max_position_contracts",
    "max_order_notional",
    "max_daily_loss",
    "max_trades_per_day",
    "max_market_exposure",
    "max_correlated_exposure",
    "max_spread",
    "kelly_fraction",
}


def _coerce(name: str, value: Any) -> Any:
    if name in ("max_position_contracts", "max_trades_per_day", "min_top_of_book_depth"):
        return int(value)
    return Decimal(str(value))


def load_profiles(settings: Settings | None = None) -> dict[RiskMode, RiskLimits]:
    """Defaults, optionally overridden by RISK_PROFILES_JSON, e.g. '{"low": {"max_daily_loss": "5"}}'."""
    raw = os.environ.get("RISK_PROFILES_JSON")
    profiles = dict(DEFAULT_PROFILES)
    if raw:
        data = json.loads(raw)
        valid = {f.name for f in fields(RiskLimits)}
        for mode_name, overrides in data.items():
            mode = RiskMode(mode_name)
            clean = {k: _coerce(k, v) for k, v in overrides.items() if k in valid}
            profiles[mode] = replace(profiles[mode], **clean)
    return profiles


def hard_limits(settings: Settings) -> dict[str, Any]:
    return {
        "max_position_contracts": settings.hard_max_order_contracts,
        "max_order_notional": settings.hard_max_order_notional_usd,
        "max_daily_loss": settings.hard_max_daily_loss_usd,
        "max_trades_per_day": settings.hard_max_trades_per_day,
        "min_confidence": settings.hard_min_confidence,
        "min_edge": settings.hard_min_edge,
        "max_market_exposure": settings.hard_max_market_exposure_usd,
        "max_spread": settings.hard_max_spread,
        "min_top_of_book_depth": settings.hard_min_top_of_book_depth,
    }


def _stricter(name: str, a: Any, b: Any) -> Any:
    return min(a, b) if name in _UPPER_BOUND_FIELDS else max(a, b)


def effective_limits(
    mode: RiskMode, overrides: dict[str, Any] | None = None, settings: Settings | None = None
) -> RiskLimits:
    settings = settings or get_settings()
    base = load_profiles(settings)[mode]
    values = asdict(base)
    for name, value in (overrides or {}).items():
        if name in values and value is not None:
            values[name] = _stricter(name, values[name], _coerce(name, value))
    for name, value in hard_limits(settings).items():
        values[name] = _stricter(name, values[name], _coerce(name, value))
    # Platform-wide defaults from env act as additional caps.
    values["min_edge"] = max(values["min_edge"], settings.min_edge)
    values["max_position_contracts"] = min(values["max_position_contracts"], settings.max_position_size)
    values["max_daily_loss"] = min(values["max_daily_loss"], settings.max_daily_loss)
    return RiskLimits(**values)


def validate_overrides(overrides: dict[str, Any]) -> dict[str, Any]:
    valid = {f.name for f in fields(RiskLimits)}
    clean: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid:
            raise ValueError(f"Unknown risk setting: {key}")
        coerced = _coerce(key, value)
        if coerced < 0:
            raise ValueError(f"{key} must be non-negative")
        clean[key] = str(coerced) if isinstance(coerced, Decimal) else coerced
    return clean
