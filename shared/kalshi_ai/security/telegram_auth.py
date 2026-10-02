"""Verification of Telegram Login Widget payloads.

Per Telegram's spec (https://core.telegram.org/widgets/login#checking-authorization):
data_check_string = sorted "key=value" lines (excluding ``hash``) joined by "\\n";
secret_key = SHA256(bot_token); expected hash = HMAC_SHA256(secret_key, data_check_string).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

MAX_AUTH_AGE_SECONDS = 86_400


class TelegramAuthError(Exception):
    pass


def verify_login_widget(data: dict[str, Any], bot_token: str, *, now: float | None = None) -> dict[str, Any]:
    if not bot_token:
        raise TelegramAuthError("Telegram bot token not configured")
    received = str(data.get("hash", ""))
    if not received:
        raise TelegramAuthError("Missing hash")
    fields = {k: v for k, v in data.items() if k != "hash" and v is not None}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise TelegramAuthError("Invalid Telegram signature")
    auth_date = int(fields.get("auth_date", 0))
    if (now or time.time()) - auth_date > MAX_AUTH_AGE_SECONDS:
        raise TelegramAuthError("Telegram login expired")
    return fields
