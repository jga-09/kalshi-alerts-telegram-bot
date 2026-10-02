"""/profile /account /subscription /settings /status /connect /balance /positions /orders."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.data.health import health_snapshot
from kalshi_ai.db.models import Order, User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import TradingMode
from kalshi_ai.kalshi.errors import KalshiError
from kalshi_ai.notifications.messages import cents, usd
from kalshi_ai.services.kalshi_connections import (
    connect_url,
    connection_can_trade,
    disconnect,
    get_connection,
    issue_connect_token,
)
from kalshi_ai.services.kill_switch import LIVE_TRADING_SUSPENDED, get_flag, is_global_kill_active
from kalshi_ai.services.subscriptions import get_access, list_subscriptions
from kalshi_ai.services.trading_controls import limits_for
from kalshi_ai.services.trading_state import ensure_paper_account, open_positions
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai_bot import texts
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers.common import Target, reply, require
from kalshi_ai_bot.keyboards import Menu

router = Router(name="account")


@router.message(Command("profile", "account"))
async def cmd_profile(message: Message, session: AsyncSession, user: User) -> None:
    access = await get_access(session, user)
    conn = await get_connection(session, user.id)
    rs = await get_risk_settings(session, user.id)
    kalshi_status = f"connected ({conn.environment.value}, key ***{conn.api_key_id_hint})" if conn else "not connected"
    lines = [
        "<b>Your account</b>",
        f"Name: {escape(user.first_name or '-')}",
        f"Telegram: @{escape(user.telegram_username or '-')}",
        f"Member since: {user.created_at:%Y-%m-%d}",
        f"Plan: {access.plan.value.upper() if access.active and access.plan else 'inactive'}",
        f"Kalshi: {kalshi_status}",
        f"Risk level: {rs.risk_mode.value.upper()}",
        f"Paper trading: {'on' if rs.paper_trading_enabled else 'off'}",
        f"Auto trading: {'on' if rs.auto_trading_enabled else 'off'}",
        f"Live trading: {'ON' if rs.live_trading_enabled else 'off'}",
        f"Emergency stop: {'ACTIVE' if rs.kill_switch_active else 'off'}",
    ]
    await reply(message, "\n".join(lines))


async def show_subscription(target: Target, session: AsyncSession, user: User) -> None:
    access = await get_access(session, user)
    subs = await list_subscriptions(session, user.id)
    lines = ["<b>Subscription</b>"]
    if access.active:
        lines.append(f"Active plan: <b>{access.plan.value.upper()}</b> until {access.expires_at:%Y-%m-%d %H:%M} UTC")
    else:
        lines.append(texts.INACTIVE)
    for s in subs[:5]:
        flag = " (cancels at period end)" if s.cancel_at_period_end else ""
        lines.append(
            f"• {s.plan.value.upper()} {s.interval.value} - {s.status.value}, "
            f"payment {s.payment_status.value}, until {s.expires_at:%Y-%m-%d}{flag}"
        )
    lines.append("\nManage billing: /subscribe")
    await reply(target, "\n".join(lines))


@router.message(Command("subscription"))
async def cmd_subscription(message: Message, session: AsyncSession, user: User) -> None:
    await show_subscription(message, session, user)


@router.callback_query(Menu.filter(F.action == "subscription"))
async def cb_subscription(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_subscription(cb, session, user)


@router.message(Command("settings"))
async def cmd_settings(message: Message, session: AsyncSession, user: User) -> None:
    rs = await get_risk_settings(session, user.id)
    lim = limits_for(rs)
    await reply(
        message,
        "\n".join(
            [
                "<b>Settings</b>",
                f"Risk level: <b>{rs.risk_mode.value.upper()}</b> (change with /risk)",
                f"Max trade: {usd(lim.max_order_notional)} | Max daily loss: {usd(lim.max_daily_loss)}",
                f"Max trades/day: {lim.max_trades_per_day} | Max contracts/market: {lim.max_position_contracts}",
                f"Min estimated edge: {float(lim.min_edge) * 100:.1f}pp | Min confidence: {float(lim.min_confidence):.2f}",
                f"Signal alerts: {'on' if rs.notify_signals else 'off'}",
                "",
                "<i>LOW risk does not mean safe. Limits reduce but do not eliminate losses.</i>",
            ]
        ),
    )


@router.message(Command("status"))
async def cmd_status(message: Message, session: AsyncSession, container: BotContainer) -> None:
    health = await health_snapshot(session)
    kill = await is_global_kill_active(session)
    suspended = (await get_flag(session, LIVE_TRADING_SUSPENDED)).active
    lines = [
        "<b>System status</b>",
        f"Trading: {'⛔ PAUSED (global kill switch)' if kill else '✅ normal'}",
        f"Live trading platform-wide: {'enabled' if container.settings.live_trading and not suspended else 'disabled'}",
        f"Kalshi environment: {container.settings.kalshi_env.value}",
        "",
        "<b>Data sources</b>",
    ]
    icon = {"ok": "🟢", "degraded": "🟡", "stale": "🟠", "down": "🔴", "disabled": "⚪"}
    lines += [f"{icon.get(st, '⚪')} {escape(src)}: {st}" for src, st in sorted(health.items())] or ["no data yet"]
    await reply(message, "\n".join(lines))


async def show_connect(target: Target, session: AsyncSession, user: User, container: BotContainer) -> None:
    if await require(target, session, user, Feature.KALSHI_CONNECT) is None:
        return
    token = await issue_connect_token(session, user)
    url = connect_url(token, container.settings)
    conn = await get_connection(session, user.id)
    prefix = f"Currently connected: key ***{conn.api_key_id_hint} ({conn.environment.value}).\n\n" if conn else ""
    await reply(target, prefix + texts.CONNECT.format(url=escape(url)))


@router.message(Command("connect"))
async def cmd_connect(message: Message, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_connect(message, session, user, container)


@router.callback_query(Menu.filter(F.action == "connect"))
async def cb_connect(cb: CallbackQuery, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_connect(cb, session, user, container)


@router.message(Command("disconnect"))
async def cmd_disconnect(message: Message, session: AsyncSession, user: User) -> None:
    removed = await disconnect(session, user)
    await reply(
        message,
        "Kalshi disconnected. Your encrypted credentials were deleted and live trading is off.\n"
        "Also revoke the API key on kalshi.com if you no longer use it."
        if removed
        else "No Kalshi account is connected.",
    )


async def show_balance(target: Target, session: AsyncSession, user: User, container: BotContainer) -> None:
    if await require(target, session, user, Feature.PORTFOLIO) is None:
        return
    lines = ["<b>Balance</b>"]
    paper = await ensure_paper_account(session, user.id)
    lines.append(f"📝 Paper: {usd(paper.cash_balance)} (started {usd(paper.starting_balance)})")
    conn = await get_connection(session, user.id)
    if conn is None:
        lines.append("Kalshi: not connected (/connect)")
    else:
        broker = None
        try:
            broker = await container.broker_for(session, user)
            lines.append(f"⚡ Kalshi ({conn.environment.value}): {usd(await broker.available_balance())} available")
        except (KalshiError, RuntimeError):
            lines.append("Kalshi: unable to fetch balance right now")
        finally:
            if broker is not None:
                await broker.aclose()
    await reply(target, "\n".join(lines))


@router.message(Command("balance"))
async def cmd_balance(message: Message, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_balance(message, session, user, container)


@router.callback_query(Menu.filter(F.action == "balance"))
async def cb_balance(cb: CallbackQuery, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_balance(cb, session, user, container)


async def show_positions(target: Target, session: AsyncSession, user: User) -> None:
    if await require(target, session, user, Feature.PORTFOLIO) is None:
        return
    lines = ["<b>Open positions</b>"]
    for mode in (TradingMode.PAPER, TradingMode.LIVE):
        positions = await open_positions(session, user.id, mode)
        lines.append(f"\n<b>{mode.value.upper()}</b>")
        if not positions:
            lines.append("none")
        for p in positions[:15]:
            lines.append(
                f"• <code>{escape(p.market_ticker)}</code> {p.side.value.upper()} x{p.quantity:g} @ "
                f"{cents(p.avg_price)} | mark {cents(p.mark_price)} | uPnL {usd(p.unrealized_pnl)}"
            )
    await reply(target, "\n".join(lines))


@router.message(Command("positions"))
async def cmd_positions(message: Message, session: AsyncSession, user: User) -> None:
    await show_positions(message, session, user)


@router.callback_query(Menu.filter(F.action == "positions"))
async def cb_positions(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_positions(cb, session, user)


@router.message(Command("orders"))
async def cmd_orders(message: Message, session: AsyncSession, user: User) -> None:
    if await require(message, session, user, Feature.PORTFOLIO) is None:
        return
    rows = (
        (
            await session.execute(
                select(Order).where(Order.user_id == user.id).order_by(Order.created_at.desc()).limit(10)
            )
        )
        .scalars()
        .all()
    )
    lines = ["<b>Recent orders</b>"] + [
        f"• {o.created_at:%m-%d %H:%M} {o.mode.value} <code>{escape(o.market_ticker)}</code> "
        f"{o.side.value.upper()} x{o.quantity} @ {cents(o.price)} - {o.status.value}"
        for o in rows
    ]
    if not rows:
        lines.append("No orders yet.")
    conn = await get_connection(session, user.id)
    if conn is not None and not connection_can_trade(conn):
        lines.append("\nYour Kalshi key is read-only; live orders are not possible.")
    await reply(message, "\n".join(lines))
