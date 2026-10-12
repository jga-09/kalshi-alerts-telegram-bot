"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from kalshi_ai.config import Settings, get_settings
from kalshi_ai.db.session import get_engine, init_engine
from kalshi_ai.logging import configure_logging, get_logger
from kalshi_ai_api.middleware import RateLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from kalshi_ai_api.routers import account, admin, auth, billing, disclosures, health, kalshi

log = get_logger(__name__)


def create_app(settings: Settings | None = None, *, manage_resources: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json_logs=settings.app_env.value != "development")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if manage_resources:
            init_engine(settings.database_url)
            app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
        log.info(
            "api_started",
            env=settings.app_env.value,
            kalshi_env=settings.kalshi_env.value,
            live_trading=settings.live_trading,
        )
        yield
        if manage_resources:
            await app.state.redis.aclose()
            await get_engine().dispose()

    app = FastAPI(
        title="Kalshi AI API",
        version="0.1.0",
        description=(
            "Kalshi AI backend. AI probabilities are ESTIMATES, not guarantees. Trading involves risk of loss."
        ),
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.add_middleware(RateLimitMiddleware, per_minute=settings.api_rate_limit_per_minute)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token", "X-Request-ID"],
    )
    app.add_middleware(RequestContextMiddleware)

    for module in (health, auth, billing, kalshi, account, admin, disclosures):
        app.include_router(module.router)
    return app


app = create_app()
