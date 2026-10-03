"""Drive the real aiogram dispatcher with synthetic updates against a recording fake Telegram session."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from sqlalchemy import select

from kalshi_ai.config import get_settings
from kalshi_ai.db.models import ConnectToken, RiskSettings
from kalshi_ai.domain.enums import Plan
from kalshi_ai.services.access_codes import generate_codes
from kalshi_ai.services.trading_controls import limits_for
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.main import build_dispatcher
from tests.conftest import give_subscription, make_user
from tests.factories import FakeMarketData

_ids = itertools.count(1)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.requests.append(method)
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(_ids),
                date=datetime.now(UTC),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args: Any, **kwargs: Any):
        raise NotImplementedError

    @property
    def texts(self) -> list[str]:
        return [r.text for r in self.requests if isinstance(r, SendMessage)]

    def last_markup_buttons(self) -> list[tuple[str, str | None]]:
        for r in reversed(self.requests):
            if isinstance(r, SendMessage) and r.reply_markup is not None:
                return [(b.text, b.callback_data) for row in r.reply_markup.inline_keyboard for b in row]
        return []


@pytest.fixture
async def tg(sessionmaker, redis):
    settings = get_settings()
    rec = RecordingSession()
    bot = Bot("123456:TEST-telegram-token-abcdefghijklmnopqrstuvwxyz", session=rec)
    container = BotContainer(
        settings=settings,
        redis=redis,
        market_service=None,
        analysis=None,  # type: ignore[arg-type]
        cipher=None,
    )
    dp = build_dispatcher(container)

    async def send(user_id: int, text: str) -> None:
        msg = Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=TgUser(id=user_id, is_bot=False, first_name="Tess"),
            text=text,
        )
        await dp.feed_update(bot, Update(update_id=next(_ids), message=msg))

    async def press(user_id: int, data: str) -> None:
        msg = Message(message_id=next(_ids), date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), text="x")
        cb = CallbackQuery(
            id=str(next(_ids)),
            from_user=TgUser(id=user_id, is_bot=False, first_name="Tess"),
            chat_instance="ci",
            data=data,
            message=msg,
        )
        await dp.feed_update(bot, Update(update_id=next(_ids), callback_query=cb))

    return rec, send, press, container


async def test_start_inactive_shows_buy_and_code(tg) -> None:
    rec, send, _, _ = tg
    await send(5001, "/start")
    assert "Welcome to Kalshi AI" in rec.texts[-1]
    assert "Your Kalshi AI subscription is inactive." in rec.texts[-1]
    labels = [b[0] for b in rec.last_markup_buttons()]
    assert "💳 BUY SUBSCRIPTION" in labels and "🔑 ENTER ACCESS CODE" in labels


async def test_access_code_flow(tg, sessionmaker) -> None:
    rec, send, press, _ = tg
    async with sessionmaker() as s:
        admin = await make_user(s, 999, admin=True)
        [code] = await generate_codes(s, plan=Plan.PRO, duration_days=30, admin=admin)
        await s.commit()
    await press(5002, "m:code")
    assert "Send your access code" in rec.texts[-1]
    await send(5002, "KAI-NOPE-NOPE-NOPE-NOPE")
    assert "invalid" in rec.texts[-1]
    await send(5002, code.code)
    assert "Subscription activated." in rec.texts[-1]
    assert any(isinstance(r, DeleteMessage) for r in rec.requests)  # code removed from chat
    assert code.code not in "".join(rec.texts)  # never echoed back


async def test_premium_commands_gated(tg) -> None:
    rec, send, _, _ = tg
    for cmd in ("/paper", "/autotrade", "/connect", "/balance"):
        await send(5003, cmd)
        assert rec.texts[-1] == "Your Kalshi AI subscription is inactive."


async def test_connect_issues_link_not_credentials(tg, sessionmaker) -> None:
    rec, send, _, _ = tg
    async with sessionmaker() as s:
        u = await make_user(s, 5004)
        await give_subscription(s, u, Plan.PRO)
        await s.commit()
    await send(5004, "/connect")
    text = rec.texts[-1]
    assert "/connect?token=" in text and "never" in text.lower()
    async with sessionmaker() as s:
        tok = (await s.execute(select(ConnectToken))).scalar_one()
        assert tok.token_hash not in text  # only the hash is stored; the link carries the raw token


async def test_secret_message_is_deleted(tg) -> None:
    rec, send, _, _ = tg
    await send(5005, "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----")
    assert any(isinstance(r, DeleteMessage) for r in rec.requests)
    assert "deleted" in rec.texts[-1]


async def test_live_confirmation_requires_explicit_confirm(tg, sessionmaker) -> None:
    from kalshi_ai.db.base import utcnow
    from kalshi_ai.db.models import KalshiConnection
    from kalshi_ai.domain.enums import KalshiConnectionStatus, KalshiEnvironment

    rec, send, press, _ = tg
    async with sessionmaker() as s:
        u = await make_user(s, 5006)
        await give_subscription(s, u, Plan.AUTO)
        u.disclosures_accepted_at = utcnow()
        s.add(
            KalshiConnection(
                user_id=u.id,
                environment=KalshiEnvironment.DEMO,
                encrypted_api_key_id=b"x",
                encrypted_private_key=b"y",
                api_key_id_hint="abcd",
                public_key_fingerprint="f",
                key_type="rsa",
                scopes=["read", "write::trade"],
                status=KalshiConnectionStatus.VERIFIED,
            )
        )
        await s.commit()
    await press(5006, "m:auto_on")
    await press(5006, "m:live_review")
    review = rec.texts[-1]
    assert "LIVE TRADING" in review and "Maximum daily loss" in review
    buttons = rec.last_markup_buttons()
    assert [b[0] for b in buttons] == ["✅ CONFIRM & ENABLE", "❌ CANCEL"]
    await press(5006, buttons[1][1])  # CANCEL
    async with sessionmaker() as s:
        rs = (await s.execute(select(RiskSettings).where(RiskSettings.user_id == u.id))).scalar_one()
        assert rs.live_trading_enabled is False
    await press(5006, "live:confirm:0000000000000000")  # forged/stale fingerprint
    assert "changed" in rec.texts[-1]
    await press(5006, buttons[0][1])  # CONFIRM
    assert "Live trading enabled" in rec.texts[-1]
    async with sessionmaker() as s:
        rs = (await s.execute(select(RiskSettings).where(RiskSettings.user_id == u.id))).scalar_one()
        assert rs.live_trading_enabled is True and rs.confirmed_risk_hash == limits_for(rs).fingerprint()
    # Emergency stop disables everything.
    await send(5006, "/stop")
    assert "EMERGENCY STOP ACTIVATED" in rec.texts[-1]
    async with sessionmaker() as s:
        rs = (await s.execute(select(RiskSettings).where(RiskSettings.user_id == u.id))).scalar_one()
        assert rs.kill_switch_active and not rs.live_trading_enabled and not rs.auto_trading_enabled


async def test_admin_commands_require_role_and_id(tg, sessionmaker) -> None:
    rec, send, _, _ = tg
    async with sessionmaker() as s:
        await make_user(s, 5007, admin=True)  # admin role but NOT in ADMIN_TELEGRAM_IDS
        await make_user(s, 999, admin=True)
        await s.commit()
    await send(5007, "/killswitch on test")
    assert rec.texts[-1].startswith("Unknown command")
    await send(5008, "/gencode pro 30")  # random user
    assert rec.texts[-1].startswith("Unknown command")
    await send(999, "/gencode pro 30 2")
    assert rec.texts[-1].count("KAI-") == 2
    await send(999, "/killswitch on drill")
    assert "ACTIVATED" in rec.texts[-1]


async def test_callback_answered(tg) -> None:
    rec, _, press, _ = tg
    await press(5009, "m:subscribe")
    assert any(isinstance(r, AnswerCallbackQuery) for r in rec.requests)
    assert "plans" in rec.texts[-1].lower()


def test_fake_market_data_is_importable() -> None:
    assert FakeMarketData() is not None


async def test_disconnect_command(tg, sessionmaker) -> None:
    from kalshi_ai.db.models import KalshiConnection
    from kalshi_ai.domain.enums import KalshiConnectionStatus, KalshiEnvironment

    rec, send, _, _ = tg
    async with sessionmaker() as s:
        u = await make_user(s, 5010)
        s.add(
            KalshiConnection(
                user_id=u.id,
                environment=KalshiEnvironment.DEMO,
                encrypted_api_key_id=b"x",
                encrypted_private_key=b"y",
                api_key_id_hint="abcd",
                public_key_fingerprint="f",
                key_type="rsa",
                scopes=["read"],
                status=KalshiConnectionStatus.VERIFIED,
            )
        )
        await s.commit()
    await send(5010, "/disconnect")
    assert "credentials were deleted" in rec.texts[-1]
    async with sessionmaker() as s:
        assert (await s.execute(select(KalshiConnection))).scalars().all() == []
    await send(5010, "/disconnect")
    assert "No Kalshi account" in rec.texts[-1]


class _FailingSession(RecordingSession):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = errors

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        from aiogram.methods import GetMe
        from aiogram.types import User as TgBotUser

        if self.errors:
            raise self.errors.pop(0)
        self.requests.append(method)
        if isinstance(method, GetMe):
            return TgBotUser(id=1, is_bot=True, first_name="B", username="test_bot")
        return True


async def test_connect_telegram_bad_token_exits_with_clear_message() -> None:
    from aiogram.exceptions import TelegramUnauthorizedError
    from aiogram.methods import GetMe

    from kalshi_ai_bot.main import connect_telegram

    session = _FailingSession([TelegramUnauthorizedError(GetMe(), "Unauthorized")])
    with pytest.raises(SystemExit, match="rejected TELEGRAM_BOT_TOKEN"):
        await connect_telegram(Bot("123456:TEST-telegram-token-abcdefghijklmnopqrstuvwxyz", session=session))


async def test_connect_telegram_retries_network_errors(monkeypatch) -> None:
    import asyncio

    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe

    from kalshi_ai_bot.main import connect_telegram

    sleeps: list[float] = []

    async def fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = _FailingSession([TelegramNetworkError(GetMe(), "down"), TelegramNetworkError(GetMe(), "down")])
    await connect_telegram(Bot("123456:TEST-telegram-token-abcdefghijklmnopqrstuvwxyz", session=session))
    assert sleeps == [2.0, 4.0]
    assert any(type(r).__name__ == "SetMyCommands" for r in session.requests)
