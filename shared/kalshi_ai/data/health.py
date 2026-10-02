"""Source-health tracking. The model receives these statuses as features."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.data.base import FetchResult
from kalshi_ai.db.base import utcnow
from kalshi_ai.db.models import SourceHealth


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # recent failures but last success still fresh
    STALE = "stale"  # last success older than stale threshold
    DOWN = "down"  # repeated failures and no fresh data
    DISABLED = "disabled"  # not configured / no license


def classify(
    last_success_at: datetime | None,
    consecutive_failures: int,
    stale_after_seconds: int,
    configured: bool = True,
    now: datetime | None = None,
) -> HealthStatus:
    if not configured:
        return HealthStatus.DISABLED
    now = now or utcnow()
    fresh = last_success_at is not None and now - last_success_at <= timedelta(seconds=stale_after_seconds)
    if fresh and consecutive_failures == 0:
        return HealthStatus.OK
    if fresh:
        return HealthStatus.DEGRADED
    if consecutive_failures >= 3:
        return HealthStatus.DOWN
    return HealthStatus.STALE


async def record_fetch(session: AsyncSession, result: FetchResult, stale_after_seconds: int) -> SourceHealth:
    row = (await session.execute(select(SourceHealth).where(SourceHealth.source == result.source))).scalar_one_or_none()
    now = utcnow()
    if row is None:
        row = SourceHealth(source=result.source, status=HealthStatus.DOWN.value, consecutive_failures=0)
        session.add(row)
    if result.ok:
        row.last_success_at = now
        row.consecutive_failures = 0
    else:
        row.last_error_at = now
        row.last_error = (result.error or "unknown")[:500]
        row.consecutive_failures = (row.consecutive_failures or 0) + (1 if result.configured else 0)
    row.latency_ms = result.latency_ms
    row.status = classify(row.last_success_at, row.consecutive_failures, stale_after_seconds, result.configured).value
    await session.flush()
    return row


async def health_snapshot(session: AsyncSession) -> dict[str, str]:
    rows = (await session.execute(select(SourceHealth))).scalars().all()
    return {r.source: r.status for r in rows}
