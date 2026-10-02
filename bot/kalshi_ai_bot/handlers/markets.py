"""/markets and /signal - AI analysis presented as structured evidence, never certainty."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.kalshi.errors import KalshiError, KalshiNotFoundError
from kalshi_ai.notifications.messages import cents, render_signal
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers.common import Target, reply, require
from kalshi_ai_bot.keyboards import Menu, SignalPick, signals_menu

router = Router(name="markets")
TICKER_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


async def show_markets(target: Target, session: AsyncSession, user: User, container: BotContainer) -> None:
    if await require(target, session, user, Feature.MARKETS) is None:
        return
    markets = await container.featured_markets()
    if not markets:
        await reply(target, "No supported markets are open right now (or Kalshi is unreachable).")
        return
    lines = ["<b>Open markets</b>"]
    for m in markets:
        lines.append(
            f"• {escape(m.title or m.ticker)}\n  <code>{escape(m.ticker)}</code> YES {cents(m.yes_bid)}/"
            f"{cents(m.yes_ask)}"
        )
    await reply(target, "\n".join(lines), signals_menu([(m.ticker, f"Analyze {m.ticker}") for m in markets]))


@router.message(Command("markets"))
async def cmd_markets(message: Message, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_markets(message, session, user, container)


@router.callback_query(Menu.filter(F.action.in_({"markets", "signals"})))
async def cb_markets(cb: CallbackQuery, session: AsyncSession, user: User, container: BotContainer) -> None:
    await show_markets(cb, session, user, container)


async def run_signal(target: Target, session: AsyncSession, user: User, container: BotContainer, ticker: str) -> None:
    if await require(target, session, user, Feature.SIGNALS) is None:
        return
    ticker = ticker.strip().upper()
    if not ticker or len(ticker) > 60 or not set(ticker) <= TICKER_OK:
        await reply(target, "Please provide a valid Kalshi market ticker, e.g. /signal KXBTC15M-...")
        return
    limit = await RateLimiter(container.redis, fail_closed=False).hit(f"signal:{user.id}", 6, 60)
    if not limit.allowed:
        await reply(target, "⏳ Analysis limit reached - please wait a minute.")
        return
    try:
        outcome = await container.analysis.analyze(session, ticker)
    except KalshiNotFoundError:
        await reply(target, "Market not found.")
        return
    except KalshiError:
        await reply(target, "Kalshi market data is unavailable right now. No analysis was produced.")
        return
    if outcome is None or outcome.signal is None:
        await reply(target, "Not enough reliable data to estimate this market right now.")
        return
    sig, pred = outcome.signal, outcome.prediction
    await reply(
        target,
        render_signal(
            market=outcome.market.title or ticker,
            ticker=ticker,
            prob_yes=pred.probability_yes,
            side=sig.side.value,
            model_prob_side=sig.estimated_probability,
            market_prob_side=sig.market_probability,
            edge=sig.edge,
            confidence=sig.confidence_level.value,
            action=sig.action.value,
            supporting=pred.supporting,
            conflicting=pred.conflicting,
            data_age_seconds=None if pred.data_age_seconds == float("inf") else pred.data_age_seconds,
        ),
    )


@router.message(Command("signal"))
async def cmd_signal(
    message: Message, command: CommandObject, session: AsyncSession, user: User, container: BotContainer
) -> None:
    if command.args:
        await run_signal(message, session, user, container, command.args.split()[0])
    else:
        await show_markets(message, session, user, container)


@router.callback_query(SignalPick.filter())
async def cb_signal(
    cb: CallbackQuery, callback_data: SignalPick, session: AsyncSession, user: User, container: BotContainer
) -> None:
    await run_signal(cb, session, user, container, callback_data.ticker)
