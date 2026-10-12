"""Read/update a user's strategy settings (which strategies run, relative size, max entry price)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import User
from kalshi_ai.services.audit import audit
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai.strategies.config import StrategyConfig, load_strategy_config


async def get_strategy_config(session: AsyncSession, user: User) -> StrategyConfig:
    rs = await get_risk_settings(session, user.id)
    return load_strategy_config(rs.strategy_config, rs.risk_mode)


async def save_strategy_config(session: AsyncSession, user: User, cfg: StrategyConfig) -> StrategyConfig:
    """Strategy choices only narrow what trades; risk limits (and live confirmation) are unaffected."""
    rs = await get_risk_settings(session, user.id)
    rs.strategy_config = cfg.to_json()
    await audit(session, "strategies.updated", actor_user_id=user.id, details=cfg.to_json())
    return load_strategy_config(rs.strategy_config, rs.risk_mode)


async def reset_strategy_config(session: AsyncSession, user: User) -> StrategyConfig:
    rs = await get_risk_settings(session, user.id)
    rs.strategy_config = None
    await audit(session, "strategies.reset_to_preset", actor_user_id=user.id, details={"mode": rs.risk_mode.value})
    return load_strategy_config(None, rs.risk_mode)
