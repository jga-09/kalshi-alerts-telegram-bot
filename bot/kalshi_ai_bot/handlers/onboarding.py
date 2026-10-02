"""/start, /help, subscription purchase and access-code activation."""

from __future__ import annotations

import contextlib
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import User
from kalshi_ai.domain.disclosures import CURRENT_DISCLOSURE_VERSION, DEFAULT_DISCLOSURES
from kalshi_ai.domain.entitlements import PLAN_DESCRIPTIONS
from kalshi_ai.domain.enums import BillingInterval, Plan
from kalshi_ai.payments.stripe_service import StripeNotConfiguredError, create_checkout_session
from kalshi_ai.services.access_codes import RedeemOutcome, redeem_code
from kalshi_ai.services.audit import audit
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.subscriptions import get_access
from kalshi_ai_bot import texts
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers.common import Target, reply
from kalshi_ai_bot.keyboards import Menu, PlanPick, inactive_menu, main_menu, plans_menu

router = Router(name="onboarding")


class CodeEntry(StatesGroup):
    waiting = State()


async def show_home(target: Target, session: AsyncSession, user: User) -> None:
    access = await get_access(session, user)
    if not access.active:
        await reply(target, f"{texts.WELCOME}\n\n{texts.INACTIVE}", inactive_menu())
        return
    expires = access.expires_at.strftime("%Y-%m-%d") if access.expires_at else "n/a"
    await reply(target, f"{texts.WELCOME}\n\n<b>Plan:</b> {access.plan.value.upper()} (until {expires})", main_menu())


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User, state: FSMContext) -> None:
    await state.clear()
    await show_home(message, session, user)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await reply(message, texts.HELP)


async def show_plans(target: Target) -> None:
    lines = ["<b>Kalshi AI plans</b>", ""]
    lines += [f"<b>{p.value.upper()}</b> - {PLAN_DESCRIPTIONS[p]}" for p in Plan]
    lines += ["", "Payment is handled securely by Stripe. Access starts once Stripe confirms payment."]
    await reply(target, "\n".join(lines), plans_menu())


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message) -> None:
    await show_plans(message)


@router.callback_query(Menu.filter(F.action == "subscribe"))
async def cb_subscribe(cb: CallbackQuery) -> None:
    await show_plans(cb)


@router.callback_query(PlanPick.filter())
async def cb_plan(
    cb: CallbackQuery, callback_data: PlanPick, session: AsyncSession, user: User, container: BotContainer
) -> None:
    try:
        url = await create_checkout_session(
            session, user, Plan(callback_data.plan), BillingInterval(callback_data.interval), container.settings
        )
    except StripeNotConfiguredError:
        await reply(
            cb, "Online checkout is not available right now. You can activate with an access code.", inactive_menu()
        )
        return
    except ValueError:
        await cb.answer("Invalid plan")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Pay securely with Stripe", url=url)]])
    await reply(
        cb,
        "Complete payment on Stripe. Your subscription activates automatically once payment is "
        "confirmed - we never activate based on the redirect alone.",
        kb,
    )


@router.callback_query(Menu.filter(F.action == "code"))
async def cb_code(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CodeEntry.waiting)
    await reply(cb, texts.ENTER_CODE)


@router.message(Command("code"))
async def cmd_code(message: Message, state: FSMContext) -> None:
    await state.set_state(CodeEntry.waiting)
    await reply(message, texts.ENTER_CODE)


@router.message(StateFilter(CodeEntry.waiting), F.text)
async def on_code(
    message: Message, state: FSMContext, session: AsyncSession, user: User, container: BotContainer
) -> None:
    raw = (message.text or "").strip()
    if raw.startswith("/"):
        await state.clear()
        return
    # Remove the code from the chat history; it is single/limited-use.
    with contextlib.suppress(Exception):
        await message.delete()
    result = await redeem_code(session, user, raw, RateLimiter(container.redis, fail_closed=True), container.settings)
    if result.outcome == RedeemOutcome.ACTIVATED:
        await state.clear()
        await session.flush()
        await show_activated(message, session, user)
    elif result.outcome == RedeemOutcome.RATE_LIMITED:
        await state.clear()
        await reply(message, texts.CODE_RATE_LIMITED.format(minutes=max(1, result.retry_after_seconds // 60)))
    elif result.outcome == RedeemOutcome.ALREADY_USED:
        await state.clear()
        await reply(message, texts.CODE_ALREADY)
    elif result.outcome == RedeemOutcome.UNAVAILABLE:
        await state.clear()
        await reply(message, texts.CODE_UNAVAILABLE)
    else:
        await reply(message, texts.CODE_INVALID + "\nTry again or send /start to cancel.")


async def show_activated(message: Message, session: AsyncSession, user: User) -> None:
    access = await get_access(session, user)
    expires = access.expires_at.strftime("%Y-%m-%d") if access.expires_at else "n/a"
    plan = access.plan.value.upper() if access.plan else "?"
    await reply(message, f"{texts.ACTIVATED}\n<b>Plan:</b> {plan} until {expires}", main_menu())


@router.callback_query(Menu.filter(F.action == "accept_disclosures"))
async def cb_accept(cb: CallbackQuery, session: AsyncSession, user: User) -> None:
    user.disclosures_accepted_at = utcnow()
    user.disclosures_version = CURRENT_DISCLOSURE_VERSION
    await audit(
        session,
        "disclosures.accepted",
        actor_user_id=user.id,
        details={"version": CURRENT_DISCLOSURE_VERSION, "channel": "telegram"},
    )
    await reply(cb, "Thank you. Risk disclosure accepted.", main_menu())


def disclosure_text() -> str:
    body = DEFAULT_DISCLOSURES["risk"]["body_markdown"]
    plain = body.replace("# Risk Disclosure", "").replace("**", "").strip()
    return f"<b>Risk Disclosure</b>\n\n{escape(plain)}"
