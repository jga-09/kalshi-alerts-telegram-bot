"""Catch-all guard: delete anything that looks like a credential and warn the user.

Registered LAST so it only sees messages no other handler consumed.
"""

from __future__ import annotations

import contextlib
import re

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import User
from kalshi_ai.services.audit import audit
from kalshi_ai_bot import texts
from kalshi_ai_bot.handlers.common import reply

router = Router(name="guard")
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bMII[A-Za-z0-9+/]{60,}"),  # base64 DER key material
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),  # API key IDs
    re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{8,}"),
]


def looks_like_secret(text: str) -> bool:
    return any(p.search(text) for p in SECRET_PATTERNS)


@router.message(F.text | F.document)
async def guard(message: Message, session: AsyncSession, user: User) -> None:
    text = message.text or message.caption or ""
    is_key_file = bool(message.document and (message.document.file_name or "").lower().endswith((".pem", ".key")))
    if looks_like_secret(text) or is_key_file:
        with contextlib.suppress(Exception):
            await message.delete()
        await audit(session, "telegram.secret_message_deleted", actor_user_id=user.id)
        await reply(message, texts.SECRET_WARNING)
        return
    await reply(message, "I didn't understand that. Send /help for commands.")
