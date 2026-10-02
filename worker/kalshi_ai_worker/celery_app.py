"""Celery application and beat schedule.

celery -A kalshi_ai_worker.celery_app worker -l info
celery -A kalshi_ai_worker.celery_app beat -l info
"""

from __future__ import annotations

from celery import Celery

from kalshi_ai.config import get_settings
from kalshi_ai.logging import configure_logging

settings = get_settings()
configure_logging(settings.log_level, json_logs=settings.app_env.value != "development")

app = Celery("kalshi_ai", broker=settings.broker_url, backend=None, include=["kalshi_ai_worker.tasks"])
app.conf.update(
    task_acks_late=True,  # a crashed worker's task is redelivered (tasks are idempotent)
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=240,
    task_default_queue="default",
    task_routes={
        "kalshi_ai_worker.tasks.cancel_user_open_orders": {"queue": "urgent"},
        "kalshi_ai_worker.tasks.cancel_all_open_orders": {"queue": "urgent"},
    },
    broker_connection_retry_on_startup=True,
    timezone="UTC",
    beat_schedule={
        "ingest-data": {"task": "kalshi_ai_worker.tasks.ingest_data", "schedule": 60.0},
        "scan-markets": {"task": "kalshi_ai_worker.tasks.scan_markets", "schedule": 60.0},
        "mark-and-settle": {"task": "kalshi_ai_worker.tasks.mark_and_settle", "schedule": 120.0},
        "reconcile-live": {"task": "kalshi_ai_worker.tasks.reconcile_live_orders", "schedule": 120.0},
        "expire-subscriptions": {"task": "kalshi_ai_worker.tasks.expire_subscriptions", "schedule": 300.0},
        "monitor-models": {"task": "kalshi_ai_worker.tasks.monitor", "schedule": 900.0},
        "train-models": {"task": "kalshi_ai_worker.tasks.train_models", "schedule": 86_400.0},
    },
)
