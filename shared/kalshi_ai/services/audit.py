from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from kalshi_ai.db.models import AuditLog, SystemEvent
from kalshi_ai.domain.enums import Severity
from kalshi_ai.logging import get_logger, redact_mapping

log = get_logger(__name__)


async def audit(
    session: AsyncSession,
    action: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_type: str = "user",
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    ip_address: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append an audit entry. Details are always redacted before persisting."""
    ctx = structlog.contextvars.get_contextvars()
    entry = AuditLog(
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        ip_address=ip_address,
        request_id=ctx.get("request_id"),
        details=redact_mapping(details or {}),
    )
    session.add(entry)
    log.info("audit", action=action, actor_type=actor_type, target_type=target_type, target_id=entry.target_id)
    return entry


async def system_event(
    session: AsyncSession,
    type_: str,
    message: str,
    severity: Severity = Severity.INFO,
    details: dict[str, Any] | None = None,
) -> SystemEvent:
    event = SystemEvent(type=type_, severity=severity.value, message=message, details=redact_mapping(details or {}))
    session.add(event)
    log.log(30 if severity != Severity.INFO else 20, "system_event", type=type_, severity=severity.value)
    return event
