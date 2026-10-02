"""Bot entrypoint. Long polling by default; webhook mode when TELEGRAM_WEBHOOK_URL is set.

python -m kalshi_ai_bot.main
"""

from __future__ import annotations

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
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


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env.value != "development")
    init_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    container = BotContainer.build(settings, redis)
    bot = build_bot(settings)
    storage = RedisStorage(Redis.from_url(settings.redis_url))
    dp = build_dispatcher(container, storage)
    await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in COMMANDS])
    webhook_url = os.environ.get("TELEGRAM_WEBHOOK_URL")
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


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
