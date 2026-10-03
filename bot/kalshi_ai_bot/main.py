"""Bot entrypoint. Long polling by default; webhook mode when TELEGRAM_WEBHOOK_URL is set.

python -m kalshi_ai_bot.main
"""

from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand
from redis.asyncio import Redis

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.session import init_engine
from kalshi_ai.logging import configure_logging, get_logger
from kalshi_ai_bot.container import BotContainer
from kalshi_ai_bot.handlers import account, admin, guard, markets, onboarding, trading
from kalshi_ai_bot.middlewares import ContainerMiddleware, SessionMiddleware, ThrottleMiddleware

log = get_logger(__name__)

COMMANDS = [
    ("start", "Main menu"),
    ("help", "Help"),
    ("profile", "Your profile"),
    ("subscribe", "Plans & billing"),
    ("subscription", "Subscription status"),
    ("connect", "Connect Kalshi (secure link)"),
    ("disconnect", "Disconnect Kalshi"),
    ("account", "Account"),
    ("balance", "Balance"),
    ("markets", "Markets"),
    ("signal", "AI market analysis"),
    ("positions", "Positions"),
    ("orders", "Orders"),
    ("settings", "Settings"),
    ("risk", "Risk level"),
    ("autotrade", "Auto trading"),
    ("paper", "Paper trading"),
    ("stop", "EMERGENCY STOP"),
    ("resume", "Clear emergency stop"),
    ("status", "System status"),
]


def build_dispatcher(container: BotContainer, storage: BaseStorage | None = None) -> Dispatcher:
    dp = Dispatcher(storage=storage or MemoryStorage())
    dp.update.outer_middleware(ContainerMiddleware(container))
    dp.update.outer_middleware(ThrottleMiddleware(container))
    session_mw = SessionMiddleware()
    dp.message.middleware(session_mw)
    dp.callback_query.middleware(session_mw)
    # Order matters: guard (catch-all) must be last.
    routers = [onboarding.router, account.router, markets.router, trading.router, admin.router, guard.router]
    for r in routers:
        r._parent_router = None  # allow rebuilding a dispatcher (tests, restarts) with the module-level routers
    dp.include_routers(*routers)
    return dp


def build_bot(settings: Settings) -> Bot:
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and set it in .env")
    return Bot(token=token, default=DefaultBotProperties(parse_mode="HTML"))


async def connect_telegram(bot: Bot, max_delay: float = 60.0) -> None:
    """Check the token and register commands, retrying network failures instead of crash-looping."""
    delay = 2.0
    while True:
        try:
            me = await bot.get_me()
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
            log.info("telegram_connected", bot_username=me.username)
            print(f"Connected to Telegram as @{me.username}. Send /start to your bot.", flush=True)
            return
        except TelegramUnauthorizedError:
            raise SystemExit(
                "Telegram rejected TELEGRAM_BOT_TOKEN (Unauthorized). The token in .env is wrong or was revoked. "
                "Get the current token from @BotFather, then re-run: bash scripts/setup-linux.sh"
            ) from None
        except TelegramNetworkError as exc:
            print(
                f"Cannot reach api.telegram.org ({exc.message}). Check the internet connection; "
                f"retrying in {delay:.0f}s.",
                flush=True,
            )
            log.warning("telegram_unreachable", retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env.value != "development")
    init_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    container = BotContainer.build(settings, redis)
    bot = build_bot(settings)
    try:
        await connect_telegram(bot)
        storage = RedisStorage(Redis.from_url(settings.redis_url))
        dp = build_dispatcher(container, storage)
        webhook_url = settings.telegram_webhook_url.strip()
        log.info("bot_starting", mode="webhook" if webhook_url else "polling")
        if webhook_url:
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
            from aiohttp import web

            secret = settings.telegram_webhook_secret.get_secret_value() or None
            await bot.set_webhook(webhook_url, secret_token=secret, drop_pending_updates=False)
            app = web.Application()
            SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path="/telegram/webhook")
            setup_application(app, dp, bot=bot)
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("BOT_PORT", "8081"))).start()  # noqa: S104
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as exc:
        detail = str(exc).replace(settings.telegram_bot_token.get_secret_value(), "<token>")[:300]
        print(f"Bot stopped with an error: {type(exc).__name__}: {detail}", flush=True)
        log.exception("bot_crashed")
        raise
    finally:
        await bot.session.close()
        await redis.aclose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
