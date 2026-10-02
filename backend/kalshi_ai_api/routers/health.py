from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from kalshi_ai.db.models import SourceHealth
from kalshi_ai.db.session import check_database, get_sessionmaker
from kalshi_ai.services.kill_switch import is_global_kill_active

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up. Does not touch dependencies."""
    return {"status": "ok"}


async def _check(coro: Any, limit_seconds: float = 3.0) -> dict[str, Any]:
    try:
        await asyncio.wait_for(coro, limit_seconds)
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "down", "error": type(exc).__name__}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: database + Redis must be reachable. Data sources / Kalshi are reported, not required."""
    checks: dict[str, Any] = {
        "database": await _check(check_database()),
        "redis": await _check(request.app.state.redis.ping()),
    }
    details: dict[str, Any] = {}
    if checks["database"]["status"] == "ok":
        try:
            async with get_sessionmaker()() as session:
                details["global_kill_switch"] = await is_global_kill_active(session)
                rows = (await session.execute(select(SourceHealth))).scalars().all()
                details["data_sources"] = {r.source: r.status for r in rows}
        except Exception as exc:
            details["error"] = type(exc).__name__
    kalshi = getattr(request.app.state, "kalshi_health", None)
    if kalshi is not None:
        details["kalshi_api"] = kalshi
    ok = all(c["status"] == "ok" for c in checks.values())
    return JSONResponse(
        {"status": "ready" if ok else "not_ready", "checks": checks, "details": details},
        status_code=200 if ok else 503,
    )
