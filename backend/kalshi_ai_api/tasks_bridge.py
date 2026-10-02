"""Thin bridge so the API can enqueue worker jobs without importing Celery at module import time."""

from __future__ import annotations

import uuid

from kalshi_ai.logging import get_logger

log = get_logger(__name__)


def enqueue_cancel_open_orders(user_id: uuid.UUID) -> None:
    try:
        from kalshi_ai_worker.tasks import cancel_user_open_orders

        cancel_user_open_orders.delay(str(user_id))
    except Exception as exc:  # broker down: the kill switch flag still blocks all NEW orders
        log.error("enqueue_cancel_failed", user_id=str(user_id), error=type(exc).__name__)


def enqueue_cancel_all_open_orders() -> None:
    try:
        from kalshi_ai_worker.tasks import cancel_all_open_orders

        cancel_all_open_orders.delay()
    except Exception as exc:
        log.error("enqueue_cancel_all_failed", error=type(exc).__name__)
