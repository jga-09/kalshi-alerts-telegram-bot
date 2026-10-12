"""/strategies - choose which strategies trade for you, their relative size, and a max entry price."""

from __future__ import annotations

from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.analytics.performance import strategy_performance
from kalshi_ai.db.models import User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import StrategyId
from kalshi_ai.notifications.messages import usd
from kalshi_ai.services.strategy_settings import get_strategy_config, reset_strategy_config, save_strategy_config
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai.strategies.config import (
    LABELS,
    MAX_PRICE_CHOICES,
    PRESETS,
    toggled,
    with_max_price,
    with_next_weight,
)
from kalshi_ai_bot.handlers.common import Target, reply, require
from kalshi_ai_bot.keyboards import Menu, StrategyCb, strategies_menu

router = Router(name="strategies")

DESCRIPTIONS = {
    StrategyId.HAMMER: "trend-following; buys at the ask so it fills reliably. Best when the market is trending.",
    StrategyId.DRIFT: "patient value entries; rests a limit order inside the spread. Some orders won't fill.",
    StrategyId.FADE: "contrarian; buys a cheap side after an overreaction. Wins less often, bigger wins.",
    StrategyId.SURGE: "only Drift's strongest setups, bought at the live price. Skips weekdays 12-6pm ET.",
}


def _stats_line(stats: dict[str, Any] | None) -> str:
    if not stats or not stats.get("trades"):
        return "no paper trades yet"
    parts = [f"{stats['trades']} trades"]
    if stats.get("resolved"):
        parts.append(f"win {stats['win_rate'] * 100:.0f}%")
        parts.append(f"P&amp;L {usd(stats['realized_pnl'])}")
        parts.append(f"avg {usd(stats['average_trade_pnl'])}/trade")
    if stats.get("fill_rate") is not None:
        parts.append(f"fill {stats['fill_rate'] * 100:.0f}%")
    return " · ".join(parts)


async def show_strategies(target: Target, session: AsyncSession, user: User) -> None:
    if await require(target, session, user, Feature.PAPER_TRADING) is None:
        return
    rs = await get_risk_settings(session, user.id)
    cfg = await get_strategy_config(session, user)
    perf = await strategy_performance(session, user.id)
    lines = ["🧩 <b>Strategies</b>", ""]
    for s in StrategyId:
        state = f"ON · size {cfg.weight(s):g}x" if s in cfg.enabled else "OFF"
        lines += [
            f"<b>{LABELS[s]}</b> - {state}",
            f"<i>{escape(DESCRIPTIONS[s])}</i>",
            f"Paper results: {_stats_line(perf.get(s.value))}",
            "",
        ]
    preset = " + ".join(LABELS[s].split(" ", 1)[1] for s in PRESETS[rs.risk_mode])
    lines += [
        f"💵 <b>Max price:</b> {cfg.max_price_cents}¢ per contract (applies to every strategy)",
        f"<b>Settings:</b> {'custom' if cfg.customized else f'{rs.risk_mode.value.upper()} preset ({preset})'}",
    ]
    if not cfg.enabled:
        lines.append("⚠️ All strategies are off - auto trading will not place any trades.")
    lines += [
        "",
        "Size is relative: the biggest size gets the full amount your risk level allows; the others scale down. "
        "Every trade still passes the risk engine.",
        "<i>No strategy wins every time. Judge them by P&amp;L per trade over many trades, not by win rate.</i>",
    ]
    await reply(target, "\n".join(lines), strategies_menu(cfg))


@router.message(Command("strategies"))
async def cmd_strategies(message: Message, session: AsyncSession, user: User) -> None:
    await show_strategies(message, session, user)


@router.callback_query(Menu.filter(F.action == "strategies"))
async def cb_strategies(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_strategies(cb, session, user)


@router.callback_query(StrategyCb.filter())
async def cb_strategy_change(cb: CallbackQuery, callback_data: StrategyCb, session: AsyncSession, user: User) -> None:
    if await require(cb, session, user, Feature.PAPER_TRADING) is None:
        return
    cfg = await get_strategy_config(session, user)
    action = callback_data.action
    try:
        if action == "preset":
            await reset_strategy_config(session, user)
        elif action == "maxp":
            idx = MAX_PRICE_CHOICES.index(cfg.max_price_cents) if cfg.max_price_cents in MAX_PRICE_CHOICES else -1
            await save_strategy_config(
                session, user, with_max_price(cfg, MAX_PRICE_CHOICES[(idx + 1) % len(MAX_PRICE_CHOICES)])
            )
        elif action in ("toggle", "weight"):
            strategy = StrategyId(callback_data.s)
            new = toggled(cfg, strategy) if action == "toggle" else with_next_weight(cfg, strategy)
            await save_strategy_config(session, user, new)
        else:
            raise ValueError(action)
    except ValueError:
        await cb.answer("Invalid option")
        return
    await show_strategies(cb, session, user)
