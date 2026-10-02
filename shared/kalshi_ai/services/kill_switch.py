"""Global and per-user emergency stop.

The database row is authoritative. The execution validator reads it directly
on every order so a cache/Redis outage can never let an order through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import SystemFlag, User
from kalshi_ai.domain.enums import Severity
from kalshi_ai.services.audit import audit, system_event
from kalshi_ai.services.users import get_risk_settings, is_admin

GLOBAL_KILL_SWITCH = "global_kill_switch"
LIVE_TRADING_SUSPENDED = "live_trading_suspended"  # set automatically by model monitoring


class NotAuthorizedError(Exception):
    pass


@dataclass(frozen=True)
class FlagState:
    active: bool
    reason: str | None
    updated_at: Any


async def get_flag(session: AsyncSession, key: str) -> FlagState:
    row = (await session.execute(select(SystemFlag).where(SystemFlag.key == key))).scalar_one_or_none()
    if row is None:
        return FlagState(False, None, None)
    return FlagState(bool(row.value.get("active")), row.value.get("reason"), row.updated_at)


async def set_flag(
    session: AsyncSession, key: str, active: bool, *, reason: str | None, actor_user_id: uuid.UUID | None
) -> None:
    row = (await session.execute(select(SystemFlag).where(SystemFlag.key == key))).scalar_one_or_none()
    value = {"active": active, "reason": reason, "at": utcnow().isoformat()}
    if row is None:
        session.add(SystemFlag(key=key, value=value, updated_by_user_id=actor_user_id))
    else:
        row.value = value
        row.updated_by_user_id = actor_user_id
        row.updated_at = utcnow()
    await session.flush()


async def is_global_kill_active(session: AsyncSession) -> bool:
    return (await get_flag(session, GLOBAL_KILL_SWITCH)).active


async def set_global_kill_switch(session: AsyncSession, admin: User, active: bool, reason: str | None = None) -> None:
    if not is_admin(admin):
        await audit(session, "global_kill_switch.denied", actor_user_id=admin.id, details={"requested": active})
        raise NotAuthorizedError("Only authorized admins can change the global kill switch.")
    await set_flag(session, GLOBAL_KILL_SWITCH, active, reason=reason, actor_user_id=admin.id)
    await audit(
        session,
        "admin.global_kill_switch." + ("activated" if active else "deactivated"),
        actor_user_id=admin.id,
        actor_type="admin",
        details={"reason": reason},
    )
    await system_event(
        session,
        "global_kill_switch",
        f"Global kill switch {'ACTIVATED' if active else 'deactivated'}",
        Severity.CRITICAL if active else Severity.WARNING,
        {"reason": reason},
    )


async def activate_user_kill_switch(session: AsyncSession, user: User, *, actor: str = "user") -> None:
    rs = await get_risk_settings(session, user.id)
    rs.kill_switch_active = True
    rs.kill_switch_activated_at = utcnow()
    rs.auto_trading_enabled = False
    rs.live_trading_enabled = False
    await audit(
        session,
        "user.kill_switch.activated",
        actor_user_id=user.id,
        actor_type=actor,
        target_type="user",
        target_id=user.id,
    )


async def deactivate_user_kill_switch(session: AsyncSession, user: User) -> None:
    """Clears the stop. Auto/live trading stay OFF and must be re-enabled + re-confirmed explicitly."""
    rs = await get_risk_settings(session, user.id)
    rs.kill_switch_active = False
    rs.live_trading_confirmed_at = None
    rs.confirmed_risk_hash = None
    await audit(session, "user.kill_switch.deactivated", actor_user_id=user.id, target_type="user", target_id=user.id)
