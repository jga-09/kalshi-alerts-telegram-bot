"""Performance & calibration metrics.

Win rate alone is misleading for binary contracts (buying 90c favourites wins
often and can still lose money). We report expected value, realized P&L,
drawdown and calibration (Brier score, log loss, reliability bins).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class TradeRecord:
    pnl: Decimal
    cost: Decimal
    estimated_probability: Decimal  # P(our side wins) at entry
    entry_price: Decimal
    won: bool | None  # None = unresolved
    tags: dict[str, str] = field(default_factory=dict)  # market, hour, risk_mode, mode...


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float


def brier_score(probs: Sequence[float], outcomes: Sequence[int]) -> float | None:
    if not probs:
        return None
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes, strict=True)) / len(probs)


def log_loss(probs: Sequence[float], outcomes: Sequence[int], eps: float = 1e-6) -> float | None:
    if not probs:
        return None
    total = 0.0
    for p, o in zip(probs, outcomes, strict=True):
        p = min(max(p, eps), 1 - eps)
        total += -(o * math.log(p) + (1 - o) * math.log(1 - p))
    return total / len(probs)


def calibration_bins(probs: Sequence[float], outcomes: Sequence[int], n_bins: int = 10) -> list[CalibrationBin]:
    buckets: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for p, o in zip(probs, outcomes, strict=True):
        idx = min(int(p * n_bins), n_bins - 1)
        buckets[idx].append((p, o))
    out = []
    for idx in sorted(buckets):
        items = buckets[idx]
        out.append(
            CalibrationBin(
                lower=idx / n_bins,
                upper=(idx + 1) / n_bins,
                count=len(items),
                mean_predicted=sum(p for p, _ in items) / len(items),
                observed_rate=sum(o for _, o in items) / len(items),
            )
        )
    return out


def expected_calibration_error(bins: Iterable[CalibrationBin]) -> float | None:
    bins = list(bins)
    total = sum(b.count for b in bins)
    if total == 0:
        return None
    return sum(b.count * abs(b.mean_predicted - b.observed_rate) for b in bins) / total


def max_drawdown(pnls: Sequence[Decimal]) -> Decimal:
    peak = Decimal(0)
    equity = Decimal(0)
    worst = Decimal(0)
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def summarize(trades: Sequence[TradeRecord]) -> dict[str, Any]:
    resolved = [t for t in trades if t.won is not None]
    pnls = [t.pnl for t in resolved]
    wins = [t for t in resolved if t.won]
    total_pnl = sum(pnls, Decimal(0))
    total_cost = sum((t.cost for t in resolved), Decimal(0))
    # Ex-ante EV per contract-dollar = sum(q - p) weighted by cost.
    ex_ante_edge = (
        sum(((t.estimated_probability - t.entry_price) for t in trades), Decimal(0)) / len(trades) if trades else None
    )
    probs = [float(t.estimated_probability) for t in resolved]
    outcomes = [1 if t.won else 0 for t in resolved]
    bins = calibration_bins(probs, outcomes)
    return {
        "trades": len(trades),
        "resolved": len(resolved),
        "win_rate": (len(wins) / len(resolved)) if resolved else None,
        "realized_pnl": str(total_pnl),
        "average_trade_pnl": str(total_pnl / len(resolved)) if resolved else None,
        "roi": float(total_pnl / total_cost) if total_cost else None,
        "average_estimated_edge": str(ex_ante_edge) if ex_ante_edge is not None else None,
        "max_drawdown": str(max_drawdown(pnls)),
        "brier_score": brier_score(probs, outcomes),
        "log_loss": log_loss(probs, outcomes),
        "expected_calibration_error": expected_calibration_error(bins),
        "calibration": [b.__dict__ for b in bins],
    }


def summarize_by(trades: Sequence[TradeRecord], key: str | Callable[[TradeRecord], str]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        k = key(t) if callable(key) else t.tags.get(key, "unknown")
        groups[k].append(t)
    return {k: summarize(v) for k, v in sorted(groups.items())}
