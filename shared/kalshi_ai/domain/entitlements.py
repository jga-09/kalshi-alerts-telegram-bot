"""Plan -> feature entitlements. The single place that decides what a tier can do."""

from __future__ import annotations

from enum import StrEnum

from kalshi_ai.domain.enums import Plan


class Feature(StrEnum):
    SIGNALS = "signals"
    MARKETS = "markets"
    KALSHI_CONNECT = "kalshi_connect"
    PORTFOLIO = "portfolio"
    PAPER_TRADING = "paper_trading"
    ANALYTICS = "analytics"
    AUTO_TRADING = "auto_trading"
    LIVE_TRADING = "live_trading"
    ADVANCED_ANALYSIS = "advanced_analysis"
    PRIORITY_SIGNALS = "priority_signals"


_SIGNALS = {Feature.SIGNALS, Feature.MARKETS}
_PRO = _SIGNALS | {Feature.KALSHI_CONNECT, Feature.PORTFOLIO, Feature.PAPER_TRADING, Feature.ANALYTICS}
_AUTO = _PRO | {Feature.AUTO_TRADING, Feature.LIVE_TRADING}
_PREMIUM = _AUTO | {Feature.ADVANCED_ANALYSIS, Feature.PRIORITY_SIGNALS}

PLAN_FEATURES: dict[Plan, frozenset[Feature]] = {
    Plan.SIGNALS: frozenset(_SIGNALS),
    Plan.PRO: frozenset(_PRO),
    Plan.AUTO: frozenset(_AUTO),
    Plan.PREMIUM: frozenset(_PREMIUM),
}

PLAN_RANK: dict[Plan, int] = {Plan.SIGNALS: 1, Plan.PRO: 2, Plan.AUTO: 3, Plan.PREMIUM: 4}

PLAN_DESCRIPTIONS: dict[Plan, str] = {
    Plan.SIGNALS: "AI market signals and market scanner",
    Plan.PRO: "Signals + Kalshi portfolio view, paper trading and analytics",
    Plan.AUTO: "Pro + opt-in automated trading (paper first, live with confirmation)",
    Plan.PREMIUM: "Auto + advanced qualitative analysis and priority signals",
}


def has_feature(plan: Plan | None, feature: Feature) -> bool:
    return plan is not None and feature in PLAN_FEATURES[plan]
