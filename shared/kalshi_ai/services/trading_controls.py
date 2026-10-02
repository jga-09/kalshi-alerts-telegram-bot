"""User-facing trading switches: paper trading, auto trading, live trading (opt-in with confirmation).

Live trading requires, in this order:
 1. Active subscription with the LIVE_TRADING entitlement
 2. Verified Kalshi connection whose key has a trade scope
 3. Risk disclosures accepted
 4. Auto trading explicitly enabled by the user
 5. The user reviewing the exact effective risk limits and confirming them
    (we store a fingerprint; any later limit change invalidates the confirmation)
 6. Platform LIVE_TRADING=true, no global/user kill switch, no admin block
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import RiskSettings, User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import RiskMode
from kalshi_ai.risk.profiles import RiskLimits, effective_limits, validate_overrides
from kalshi_ai.services.audit import audit
from kalshi_ai.services.kalshi_connections import connection_can_trade, get_connection
from kalshi_ai.services.kill_switch import LIVE_TRADING_SUSPENDED, get_flag, is_global_kill_active
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai.services.trading_state import ensure_paper_account
from kalshi_ai.services.users import get_risk_settings


class Blocker(StrEnum):
    NO_SUBSCRIPTION = "Your Kalshi AI subscription is inactive."
    PLAN_NO_PAPER = "Your plan does not include paper trading."
    PLAN_NO_AUTO = "Your plan does not include automated trading (AUTO or PREMIUM required)."
    NO_KALSHI = "Connect your Kalshi account first (/connect)."
    KALSHI_NO_TRADE_SCOPE = "Your Kalshi API key does not have the 'write::trade' scope."
    DISCLOSURES = "Please review and accept the risk disclosure first."
    AUTO_DISABLED = "Enable auto trading first."
    PLATFORM_LIVE_OFF = "Live trading is currently disabled on this platform."
    GLOBAL_KILL = "Trading is paused platform-wide by an administrator."
    USER_KILL = "Your emergency stop is active. Clear it first with /resume."
    ADMIN_BLOCK = "Automated trading has been disabled on your account by an administrator."
    MONITOR_SUSPENDED = "Live trading is temporarily suspended by automated safety monitoring."
    NOT_CONFIRMED = "Risk settings not confirmed."
    CONFIRMATION_STALE = "Your risk settings changed since you reviewed them. Please review again."


@dataclass(frozen=True)
class LiveReview:
    risk_mode: RiskMode
    limits: RiskLimits
    fingerprint: str
    blockers: list[Blocker]

    def summary(self) -> dict[str, Any]:
        return {
            "risk_mode": self.risk_mode.value,
            "max_trade_usd": str(self.limits.max_order_notional),
            "max_daily_loss_usd": str(self.limits.max_daily_loss),
            "max_trades_per_day": self.limits.max_trades_per_day,
            "max_position_contracts": self.limits.max_position_contracts,
            "min_edge": str(self.limits.min_edge),
            "min_confidence": str(self.limits.min_confidence),
            "fingerprint": self.fingerprint,
            "blockers": [b.value for b in self.blockers],
        }


def limits_for(rs: RiskSettings, settings: Settings | None = None) -> RiskLimits:
    return effective_limits(rs.risk_mode, rs.overrides, settings)


async def enable_paper_trading(session: AsyncSession, user: User) -> list[Blocker]:
    access = await get_access(session, user)
    if not access.active:
        return [Blocker.NO_SUBSCRIPTION]
    if not access.has(Feature.PAPER_TRADING):
        return [Blocker.PLAN_NO_PAPER]
    rs = await get_risk_settings(session, user.id)
    rs.paper_trading_enabled = True
    await ensure_paper_account(session, user.id)
    await audit(session, "trading.paper.enabled", actor_user_id=user.id)
    return []


async def disable_paper_trading(session: AsyncSession, user: User) -> None:
    rs = await get_risk_settings(session, user.id)
    rs.paper_trading_enabled = False
    await audit(session, "trading.paper.disabled", actor_user_id=user.id)


async def set_auto_trading(session: AsyncSession, user: User, enabled: bool) -> list[Blocker]:
    rs = await get_risk_settings(session, user.id)
    if not enabled:
        rs.auto_trading_enabled = False
        rs.live_trading_enabled = False
        await audit(session, "trading.auto.disabled", actor_user_id=user.id)
        return []
    access = await get_access(session, user)
    blockers: list[Blocker] = []
    if not access.active:
        blockers.append(Blocker.NO_SUBSCRIPTION)
    elif not access.has(Feature.AUTO_TRADING):
        blockers.append(Blocker.PLAN_NO_AUTO)
    if rs.kill_switch_active:
        blockers.append(Blocker.USER_KILL)
    if rs.admin_auto_trading_disabled:
        blockers.append(Blocker.ADMIN_BLOCK)
    if blockers:
        return blockers
    rs.auto_trading_enabled = True
    # Auto trading starts in PAPER mode; live needs its own confirmation.
    rs.paper_trading_enabled = True
    await ensure_paper_account(session, user.id)
    await audit(session, "trading.auto.enabled", actor_user_id=user.id)
    return []


async def set_risk_mode(
    session: AsyncSession, user: User, mode: RiskMode, overrides: dict[str, Any] | None = None
) -> RiskLimits:
    rs = await get_risk_settings(session, user.id)
    rs.risk_mode = mode
    if overrides is not None:
        rs.overrides = validate_overrides(overrides) or None
    new_limits = limits_for(rs)
    if rs.confirmed_risk_hash != new_limits.fingerprint():
        # Changing limits always requires a fresh live confirmation.
        rs.live_trading_enabled = False
        rs.live_trading_confirmed_at = None
    await audit(session, "risk.updated", actor_user_id=user.id, details={"mode": mode.value, "overrides": rs.overrides})
    return new_limits


async def live_trading_blockers(
    session: AsyncSession, user: User, rs: RiskSettings, settings: Settings | None = None
) -> list[Blocker]:
    settings = settings or get_settings()
    blockers: list[Blocker] = []
    access = await get_access(session, user)
    if not access.active:
        blockers.append(Blocker.NO_SUBSCRIPTION)
    elif not access.has(Feature.LIVE_TRADING):
        blockers.append(Blocker.PLAN_NO_AUTO)
    conn = await get_connection(session, user.id)
    if conn is None:
        blockers.append(Blocker.NO_KALSHI)
    elif not connection_can_trade(conn):
        blockers.append(Blocker.KALSHI_NO_TRADE_SCOPE)
    if user.disclosures_accepted_at is None:
        blockers.append(Blocker.DISCLOSURES)
    if not rs.auto_trading_enabled:
        blockers.append(Blocker.AUTO_DISABLED)
    if not settings.live_trading:
        blockers.append(Blocker.PLATFORM_LIVE_OFF)
    if await is_global_kill_active(session):
        blockers.append(Blocker.GLOBAL_KILL)
    if (await get_flag(session, LIVE_TRADING_SUSPENDED)).active:
        blockers.append(Blocker.MONITOR_SUSPENDED)
    if rs.kill_switch_active:
        blockers.append(Blocker.USER_KILL)
    if rs.admin_auto_trading_disabled:
        blockers.append(Blocker.ADMIN_BLOCK)
    return blockers


async def review_live_trading(session: AsyncSession, user: User, settings: Settings | None = None) -> LiveReview:
    rs = await get_risk_settings(session, user.id)
    limits = limits_for(rs, settings)
    blockers = await live_trading_blockers(session, user, rs, settings)
    await audit(session, "trading.live.reviewed", actor_user_id=user.id, details={"fingerprint": limits.fingerprint()})
    return LiveReview(rs.risk_mode, limits, limits.fingerprint(), blockers)


async def confirm_live_trading(
    session: AsyncSession, user: User, confirmed_fingerprint: str, settings: Settings | None = None
) -> list[Blocker]:
    """Second, explicit step. The fingerprint proves the user saw the exact limits being enabled."""
    rs = await get_risk_settings(session, user.id)
    blockers = await live_trading_blockers(session, user, rs, settings)
    limits = limits_for(rs, settings)
    if confirmed_fingerprint != limits.fingerprint():
        blockers.append(Blocker.CONFIRMATION_STALE)
    if blockers:
        await audit(
            session,
            "trading.live.confirm_rejected",
            actor_user_id=user.id,
            details={"blockers": [b.name for b in blockers]},
        )
        return blockers
    now = utcnow()
    rs.live_trading_enabled = True
    rs.live_trading_confirmed_at = now
    rs.risk_settings_confirmed_at = now
    rs.confirmed_risk_hash = limits.fingerprint()
    await audit(
        session,
        "trading.live.enabled",
        actor_user_id=user.id,
        details={"limits": limits.as_public_dict(), "mode": rs.risk_mode.value},
    )
    return []


async def disable_live_trading(session: AsyncSession, user: User) -> None:
    rs = await get_risk_settings(session, user.id)
    rs.live_trading_enabled = False
    rs.live_trading_confirmed_at = None
    await audit(session, "trading.live.disabled", actor_user_id=user.id)
