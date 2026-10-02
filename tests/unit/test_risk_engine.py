from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from kalshi_ai.config import get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.domain.enums import RiskMode, Side
from kalshi_ai.risk.engine import AccountState, LiquidityView, RiskEngine, SignalView, kelly_fraction_for_binary
from kalshi_ai.risk.profiles import DEFAULT_PROFILES, effective_limits, validate_overrides

NOW = utcnow()


def signal(**kw) -> SignalView:
    base = SignalView(
        market_ticker="KXBTC15M-A-T1",
        correlation_group="KXBTC15M",
        side=Side.YES,
        estimated_probability=D("0.65"),
        market_probability=D("0.55"),
        entry_price=D("0.55"),
        edge=D("0.10"),
        confidence=D("0.80"),
        expires_at=NOW + timedelta(minutes=5),
        data_age_seconds=5,
    )
    return replace(base, **kw)


def account(**kw) -> AccountState:
    base = AccountState(
        available_balance=D("1000"),
        realized_pnl_today=D(0),
        unrealized_pnl_today=D(0),
        trades_today=0,
        position_contracts_in_market=D(0),
        exposure_in_market=D(0),
        exposure_in_group=D(0),
    )
    return replace(base, **kw)


LIQ = LiquidityView(spread=D("0.02"), depth_at_limit=D("500"), top_of_book_depth=D("200"))


def engine(mode: RiskMode = RiskMode.PASSIVE) -> RiskEngine:
    s = get_settings()
    return RiskEngine(
        effective_limits(mode, None, s),
        mode,
        min_price=s.hard_min_price,
        max_price=s.hard_max_price,
        max_data_age_seconds=s.max_data_age_seconds,
    )


def test_approves_and_sizes_within_all_caps() -> None:
    decision = engine().evaluate(signal(), account(), LIQ, NOW)
    assert decision.approved, decision.reason
    lim = engine().limits
    assert 1 <= decision.quantity <= lim.max_position_contracts
    assert decision.quantity * D("0.55") <= lim.max_order_notional


@pytest.mark.parametrize(
    ("sig_kw", "acct_kw", "liq", "failed"),
    [
        ({"edge": D("0.01"), "estimated_probability": D("0.56")}, {}, LIQ, "min_edge"),
        ({"confidence": D("0.3")}, {}, LIQ, "min_confidence"),
        ({"data_age_seconds": 10_000}, {}, LIQ, "data_fresh"),
        ({"expires_at": NOW - timedelta(seconds=1)}, {}, LIQ, "signal_not_expired"),
        (
            {"entry_price": D("0.99"), "estimated_probability": D("0.999"), "edge": D("0.009")},
            {},
            LIQ,
            "price_in_bounds",
        ),
        ({"edge": D("0.30")}, {}, LIQ, "edge_consistent"),  # model claims an edge the prices don't support
        ({}, {"realized_pnl_today": D("-30")}, LIQ, "daily_loss_limit"),
        ({}, {"trades_today": 10}, LIQ, "max_trades_per_day"),
        ({}, {"position_contracts_in_market": D("25")}, LIQ, "position_limit"),
        ({}, {"exposure_in_market": D("30")}, LIQ, "market_exposure"),
        ({}, {"exposure_in_group": D("60")}, LIQ, "correlated_exposure"),
        ({}, {"available_balance": D("0")}, LIQ, "balance_positive"),
        ({}, {}, LiquidityView(spread=D("0.20"), depth_at_limit=D(500), top_of_book_depth=D(200)), "spread"),
        ({}, {}, LiquidityView(spread=None, depth_at_limit=D(500), top_of_book_depth=D(200)), "spread"),
        ({}, {}, LiquidityView(spread=D("0.02"), depth_at_limit=D(500), top_of_book_depth=D(1)), "top_of_book_depth"),
        ({}, {}, LiquidityView(spread=D("0.02"), depth_at_limit=D(1), top_of_book_depth=D(200)), "order_size"),
    ],
)
def test_any_failed_check_blocks_trade(sig_kw, acct_kw, liq, failed) -> None:
    decision = engine().evaluate(signal(**sig_kw), account(**acct_kw), liq, NOW)
    assert not decision.approved
    assert decision.quantity == 0
    assert failed in {c.name for c in decision.failed}


def test_unrealized_losses_count_toward_daily_limit() -> None:
    d = engine().evaluate(signal(), account(unrealized_pnl_today=D("-29.99"), realized_pnl_today=D("-0.02")), LIQ, NOW)
    assert "daily_loss_limit" in {c.name for c in d.failed}


def test_overrides_can_only_tighten() -> None:
    loose = effective_limits(RiskMode.LOW, {"max_daily_loss": "100000", "min_edge": "0.0"})
    base = effective_limits(RiskMode.LOW)
    assert loose == base
    tight = effective_limits(RiskMode.LOW, {"max_daily_loss": "2", "min_edge": "0.2"})
    assert tight.max_daily_loss == D("2") and tight.min_edge == D("0.2")


def test_hard_limits_cap_every_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARD_MAX_ORDER_NOTIONAL_USD", "3")
    monkeypatch.setenv("HARD_MIN_EDGE", "0.09")
    get_settings.cache_clear()
    for mode in RiskMode:
        lim = effective_limits(mode)
        assert lim.max_order_notional <= D("3")
        assert lim.min_edge >= D("0.09")


def test_low_is_strictest_profile() -> None:
    low, risky = DEFAULT_PROFILES[RiskMode.LOW], DEFAULT_PROFILES[RiskMode.RISKY]
    assert low.max_daily_loss < risky.max_daily_loss and low.min_edge > risky.min_edge


def test_validate_overrides() -> None:
    assert validate_overrides({"max_daily_loss": 5}) == {"max_daily_loss": "5"}
    with pytest.raises(ValueError):
        validate_overrides({"bogus": 1})
    with pytest.raises(ValueError):
        validate_overrides({"max_daily_loss": -1})


def test_kelly() -> None:
    assert kelly_fraction_for_binary(D("0.6"), D("0.5")) == D("0.2")
    assert kelly_fraction_for_binary(D("0.4"), D("0.5")) == D(0)


def test_risk_fingerprint_changes_with_limits() -> None:
    assert effective_limits(RiskMode.LOW).fingerprint() != effective_limits(RiskMode.RISKY).fingerprint()
