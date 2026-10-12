from __future__ import annotations

from aiogram.types import CallbackQuery, InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import User
from kalshi_ai.domain.entitlements import Feature
from kalshi_ai.services.subscriptions import AccessState, get_access
from kalshi_ai_bot import texts
from kalshi_ai_bot.keyboards import inactive_menu

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
Target = Message | CallbackQuery


async def reply(target: Target, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    message = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        await target.answer()
    if message is None:
        return
    await message.answer(text, reply_markup=markup, link_preview_options=NO_PREVIEW)


async def require(target: Target, session: AsyncSession, user: User, feature: Feature) -> AccessState | None:
    """Return access if the user's plan includes `feature`; otherwise explain and return None."""
    access = await get_access(session, user)
    if not access.active:
        await reply(target, texts.INACTIVE, inactive_menu())
        return None
    if not access.has(feature):
        await reply(
            target,
            f"Your {access.plan.value.upper() if access.plan else ''} plan does not include this "
            "feature. Use /subscribe to upgrade.",
        )
        return None
    return access
