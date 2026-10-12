"""Telegram implementation of the Notifier protocol used by the execution pipeline and worker."""

from __future__ import annotations

import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from kalshi_ai.logging import get_logger

log = get_logger(__name__)


class TelegramNotifier:
    def __init__(self, bot: Bot, max_retry_after: float = 10.0):
        self.bot = bot
        self.max_retry_after = max_retry_after

    async def send(self, chat_id: int, text: str) -> bool:
        for _ in range(2):
            try:
                await self.bot.send_message(
                    chat_id, text, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                return True
            except TelegramRetryAfter as exc:
                if exc.retry_after > self.max_retry_after:
                    break
                await asyncio.sleep(exc.retry_after)
            except TelegramForbiddenError:
                log.info("telegram_user_blocked_bot", chat_id=chat_id)
                return False
            except (TelegramAPIError, OSError) as exc:
                log.warning("telegram_send_failed", error=type(exc).__name__)
                return False
        return False
