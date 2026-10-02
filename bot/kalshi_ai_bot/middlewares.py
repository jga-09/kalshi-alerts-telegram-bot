"""aiogram middlewares: DB session per update, user resolution, per-user throttling, error isolation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from kalshi_ai.db.session import get_sessionmaker
from kalshi_ai.logging import bind_context, clear_context, get_logger
from kalshi_ai.services.rate_limit import RateLimiter
from kalshi_ai.services.users import get_or_create_telegram_user
from kalshi_ai_bot.container import BotContainer

log = get_logger(__name__)
Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]


class SessionMiddleware(BaseMiddleware):
    """Opens one transactional DB session per update and resolves the internal user."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        clear_context()
        if tg_user is None or tg_user.is_bot:
            return None
        bind_context(telegram_user_id=tg_user.id)
        session = get_sessionmaker()()
        try:
            chat = data.get("event_chat")
            user, created = await get_or_create_telegram_user(
                session,
                tg_user.id,
                chat_id=chat.id if chat else None,
                username=tg_user.username,
                first_name=tg_user.first_name,
            )
            bind_context(customer_id=str(user.id))
            data["session"] = session
            data["user"] = user
            data["user_created"] = created
            result = await handler(event, data)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            log.exception("bot_handler_error")
            await _safe_error_reply(event)
            return None
        finally:
            await session.close()


async def _safe_error_reply(event: TelegramObject) -> None:
    text = "⚠️ Something went wrong. No trading action was taken. Please try again shortly."
    try:
        if isinstance(event, Message):
            await event.answer(text)
        elif isinstance(event, CallbackQuery) and event.message:
            await event.answer("Error - no action taken", show_alert=False)
    except Exception:  # noqa: S110 - Telegram itself may be down
        pass


class ThrottleMiddleware(BaseMiddleware):
    """30 updates/minute per user. Fails open (availability) - sensitive flows have their own limits."""

    def __init__(self, container: BotContainer, per_minute: int = 30):
        self.container = container
        self.per_minute = per_minute

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is not None:
            result = await RateLimiter(self.container.redis, fail_closed=False).hit(
                f"tg:{tg_user.id}", self.per_minute, 60
            )
            if not result.allowed:
                if isinstance(event, CallbackQuery):
                    await event.answer("Slow down a little 🙂")
                return None
        return await handler(event, data)


class ContainerMiddleware(BaseMiddleware):
    def __init__(self, container: BotContainer):
        self.container = container

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        data["container"] = self.container
        return await handler(event, data)
