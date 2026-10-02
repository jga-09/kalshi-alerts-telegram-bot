from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import RiskSettings, User
from kalshi_ai.domain.enums import RiskMode, UserRole, UserStatus
from kalshi_ai.services.audit import audit


class UserSuspendedError(Exception):
    pass


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await session.get(User, user_id)


async def get_user_by_telegram_id(session: AsyncSession, telegram_user_id: int) -> User | None:
    return (await session.execute(select(User).where(User.telegram_user_id == telegram_user_id))).scalar_one_or_none()


async def get_or_create_telegram_user(
    session: AsyncSession,
    telegram_user_id: int,
    *,
    chat_id: int | None = None,
    username: str | None = None,
    first_name: str | None = None,
) -> tuple[User, bool]:
    user = await get_user_by_telegram_id(session, telegram_user_id)
    created = False
    if user is None:
        user = User(
            telegram_user_id=telegram_user_id,
            telegram_chat_id=chat_id,
            telegram_username=username,
            first_name=first_name,
            role=UserRole.CUSTOMER,
        )
        session.add(user)
        await session.flush()
        # Safe defaults: LOW risk, paper off until user starts it, live OFF.
        session.add(RiskSettings(user_id=user.id, risk_mode=RiskMode.LOW))
        await audit(session, "user.created", actor_user_id=user.id, target_type="user", target_id=user.id)
        created = True
    else:
        user.telegram_chat_id = chat_id or user.telegram_chat_id
        user.telegram_username = username or user.telegram_username
        user.first_name = first_name or user.first_name
    user.last_seen_at = utcnow()
    return user, created


def is_admin(user: User | None, settings: Settings | None = None) -> bool:
    """Admin requires BOTH: backend role ADMIN *and* Telegram ID in ADMIN_TELEGRAM_IDS.

    Knowing a command or being listed in env alone is never enough.
    """
    if user is None or user.status != UserStatus.ACTIVE:
        return False
    settings = settings or get_settings()
    return user.role == UserRole.ADMIN and user.telegram_user_id in settings.admin_telegram_ids


def ensure_active(user: User) -> None:
    if user.status != UserStatus.ACTIVE:
        raise UserSuspendedError("Account suspended")


async def get_risk_settings(session: AsyncSession, user_id: uuid.UUID) -> RiskSettings:
    rs = (await session.execute(select(RiskSettings).where(RiskSettings.user_id == user_id))).scalar_one_or_none()
    if rs is None:
        rs = RiskSettings(user_id=user_id, risk_mode=RiskMode.LOW)
        session.add(rs)
        await session.flush()
    return rs


async def set_user_status(
    session: AsyncSession, user: User, status: UserStatus, *, actor: User, reason: str | None = None
) -> None:
    user.status = status
    user.suspended_reason = reason if status == UserStatus.SUSPENDED else None
    if status == UserStatus.SUSPENDED:
        rs = await get_risk_settings(session, user.id)
        rs.auto_trading_enabled = False
        rs.live_trading_enabled = False
    await audit(
        session,
        f"admin.user.{status.value}",
        actor_user_id=actor.id,
        actor_type="admin",
        target_type="user",
        target_id=user.id,
        details={"reason": reason},
    )
