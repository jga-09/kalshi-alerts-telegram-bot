"""/paper /autotrade /risk /stop /resume and the explicit two-step live-trading confirmation."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.analytics.performance import user_performance
from kalshi_ai.db.models import User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.domain.enums import RiskMode
from kalshi_ai.notifications.messages import usd
from kalshi_ai.services.jobs import enqueue_cancel_open_orders
from kalshi_ai.services.kill_switch import activate_user_kill_switch, deactivate_user_kill_switch
from kalshi_ai.services.trading_controls import (
    confirm_live_trading,
    disable_live_trading,
    disable_paper_trading,
    enable_paper_trading,
    limits_for,
    review_live_trading,
    set_auto_trading,
    set_risk_mode,
)
from kalshi_ai.services.trading_state import ensure_paper_account
from kalshi_ai.services.users import get_risk_settings
from kalshi_ai.trading.emergency import cancel_open_orders
from kalshi_ai_bot import texts
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers.common import Target, reply, require
from kalshi_ai_bot.handlers.onboarding import disclosure_text
from kalshi_ai_bot.keyboards import (
    LiveConfirm,
    Menu,
    RiskPick,
    autotrade_menu,
    disclosure_menu,
    live_confirm_menu,
    main_menu,
    risk_menu,
)

router = Router(name="trading")


# ------------------------------------------------------------------ paper


async def show_paper(target: Target, session: AsyncSession, user: User) -> None:
    if await require(target, session, user, Feature.PAPER_TRADING) is None:
        return
    rs = await get_risk_settings(session, user.id)
    if not rs.paper_trading_enabled:
        await enable_paper_trading(session, user)
    acct = await ensure_paper_account(session, user.id)
    perf = (await user_performance(session, user.id))["paper"]
    win = f"{perf['win_rate'] * 100:.0f}%" if perf["win_rate"] is not None else "n/a"
    brier = f"{perf['brier_score']:.3f}" if perf["brier_score"] is not None else "n/a"
    await reply(
        target,
        "\n".join(
            [
                "📝 <b>Paper trading is ON</b> (simulated - no real money)",
                f"Cash: {usd(acct.cash_balance)} of {usd(acct.starting_balance)} starting balance",
                f"Trades: {perf['trades']} | resolved: {perf['resolved']} | realized P&amp;L: {usd(perf['realized_pnl'])}",
                f"Win rate: {win} | Max drawdown: {usd(perf['max_drawdown'])} | Calibration (Brier): {brier}",
                "",
                "<i>Paper results are hypothetical and do not guarantee live results. Win rate alone is not a measure "
                "of skill - watch P&amp;L and calibration.</i>",
            ]
        ),
    )


@router.message(Command("paper"))
async def cmd_paper(message: Message, session: AsyncSession, user: User) -> None:
    if message.text and message.text.strip().endswith("off"):
        await disable_paper_trading(session, user)
        await reply(message, "Paper trading turned off.")
        return
    await show_paper(message, session, user)


@router.callback_query(Menu.filter(F.action == "paper"))
async def cb_paper(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_paper(cb, session, user)


# ------------------------------------------------------------------ risk


async def show_risk(target: Target, session: AsyncSession, user: User) -> None:
    rs = await get_risk_settings(session, user.id)
    lim = limits_for(rs)
    await reply(
        target,
        "\n".join(
            [
                f"<b>Risk level: {rs.risk_mode.value.upper()}</b>",
                f"Max trade {usd(lim.max_order_notional)} | daily loss {usd(lim.max_daily_loss)} | "
                f"{lim.max_trades_per_day} trades/day",
                "",
                "LOW = smallest size, strictest filters. PASSIVE = moderate. RISKY = larger size, looser filters.",
                "<i>No level is safe; all trading can lose money. Changing level turns off live trading until you "
                "re-confirm.</i>",
            ]
        ),
        risk_menu(rs.risk_mode),
    )


@router.message(Command("risk"))
async def cmd_risk(message: Message, session: AsyncSession, user: User) -> None:
    await show_risk(message, session, user)


@router.callback_query(Menu.filter(F.action == "risk"))
async def cb_risk(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_risk(cb, session, user)


@router.callback_query(RiskPick.filter())
async def cb_risk_pick(cb: CallbackQuery, callback_data: RiskPick, session: AsyncSession, user: User) -> None:
    try:
        mode = RiskMode(callback_data.mode)
    except ValueError:
        await cb.answer("Invalid option")
        return
    await set_risk_mode(session, user, mode)
    await show_risk(cb, session, user)


# ------------------------------------------------------------------ auto / live


async def show_autotrade(target: Target, session: AsyncSession, user: User) -> None:
    if await require(target, session, user, Feature.AUTO_TRADING) is None:
        return
    if user.disclosures_accepted_at is None:
        await reply(target, disclosure_text(), disclosure_menu())
        return
    rs = await get_risk_settings(session, user.id)
    status = "LIVE ⚡" if rs.live_trading_enabled else ("PAPER 📝" if rs.auto_trading_enabled else "OFF")
    await reply(
        target,
        f"<b>Auto trading: {status}</b>\nRisk level: {rs.risk_mode.value.upper()}\n\n"
        "Auto trading always starts in paper mode. Live trading requires a separate review and "
        "confirmation.",
        autotrade_menu(rs.auto_trading_enabled, rs.live_trading_enabled),
    )


@router.message(Command("autotrade"))
async def cmd_autotrade(message: Message, session: AsyncSession, user: User) -> None:
    await show_autotrade(message, session, user)


@router.callback_query(Menu.filter(F.action == "autotrade"))
async def cb_autotrade(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await show_autotrade(cb, session, user)


@router.callback_query(Menu.filter(F.action.in_({"auto_on", "auto_off"})))
async def cb_auto_toggle(cb: CallbackQuery, callback_data: Menu, session: AsyncSession, user: User) -> None:
    blockers = await set_auto_trading(session, user, callback_data.action == "auto_on")
    if blockers:
        await reply(cb, "Cannot enable auto trading:\n" + "\n".join(f"• {b.value}" for b in blockers))
        return
    await show_autotrade(cb, session, user)


@router.callback_query(Menu.filter(F.action == "live_review"))
async def cb_live_review(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    review = await review_live_trading(session, user)
    if review.blockers:
        await reply(cb, "Live trading cannot be enabled yet:\n" + "\n".join(f"• {b.value}" for b in review.blockers))
        return
    lim = review.limits
    text = texts.LIVE_REVIEW.format(
        risk_mode=review.risk_mode.value.upper(),
        max_trade=lim.max_order_notional,
        max_daily_loss=lim.max_daily_loss,
        max_trades=lim.max_trades_per_day,
        min_edge=f"{float(lim.min_edge) * 100:.1f}pp",
    )
    await reply(cb, text, live_confirm_menu(review.fingerprint))


@router.callback_query(LiveConfirm.filter())
async def cb_live_confirm(cb: CallbackQuery, callback_data: LiveConfirm, session: AsyncSession, user: User) -> None:
    if callback_data.action != "confirm":
        await reply(cb, "Live trading NOT enabled. You remain in paper mode.")
        return
    rs = await get_risk_settings(session, user.id)
    current = limits_for(rs).fingerprint()
    if not current.startswith(callback_data.fp):
        await reply(cb, "Your risk settings changed since you reviewed them. Please review again.")
        return
    blockers = await confirm_live_trading(session, user, current)
    if blockers:
        await reply(cb, "Live trading NOT enabled:\n" + "\n".join(f"• {b.value}" for b in blockers))
        return
    await reply(
        cb,
        "⚡ <b>Live trading enabled.</b>\nEvery order still passes the risk engine and final safety "
        "checks. Use /stop at any time.",
        main_menu(),
    )


@router.callback_query(Menu.filter(F.action == "live_off"))
async def cb_live_off(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    await disable_live_trading(session, user)
    await reply(cb, "Live trading disabled. Auto trading continues in paper mode.")


# ------------------------------------------------------------------ emergency stop


async def emergency_stop(target: Target, session: AsyncSession, user: User, container: BotContainer) -> None:
    await activate_user_kill_switch(session, user)
    await session.commit()  # the block takes effect before anything else happens
    if not enqueue_cancel_open_orders(user.id):
        # Worker unavailable: cancel inline (best effort).
        try:
            await cancel_open_orders(session, user, container.broker_for)
        except Exception:  # noqa: S110 - the kill switch itself is already durable
            pass
    await reply(target, texts.STOPPED)


@router.message(Command("stop"))
async def cmd_stop(message: Message, session: AsyncSession, user: User, container: BotContainer) -> None:
    await emergency_stop(message, session, user, container)


@router.callback_query(Menu.filter(F.action == "stop"))
async def cb_stop(cb: CallbackQuery, session: AsyncSession, user: User, container: BotContainer) -> None:
    await emergency_stop(cb, session, user, container)


@router.message(Command("resume"))
async def cmd_resume(message: Message, session: AsyncSession, user: User) -> None:
    await deactivate_user_kill_switch(session, user)
    await reply(
        message, "Emergency stop cleared. Auto and live trading remain OFF until you enable them again (/autotrade)."
    )
