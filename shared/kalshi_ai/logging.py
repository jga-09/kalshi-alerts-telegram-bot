"""Structured JSON logging with mandatory secret redaction.

Context such as request_id / trade_id / customer_id is bound with
``structlog.contextvars`` and automatically added to every log line.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Mapping
from typing import Any

import structlog

SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass(word)?|secret|token|private[_-]?key|api[_-]?key|authorization|signature|cookie|"
    r"pem|credential|encryption|pepper|code_plain|access[_-]?code)",
    re.IGNORECASE,
)
# Values that look like secrets even when the key name is innocent.
SENSITIVE_VALUE_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"\bsk_(live|test)_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bwhsec_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{10,}", re.IGNORECASE),
]
REDACTED = "***REDACTED***"


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        for pattern in SENSITIVE_VALUE_PATTERNS:
            value = pattern.sub(REDACTED, value)
        return value
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    return value


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and SENSITIVE_KEY_PATTERN.search(key):
            out[key] = REDACTED
        else:
            out[key] = redact_value(value)
    return out


def _redaction_processor(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact_mapping(event_dict)


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _redaction_processor,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # Third-party HTTP clients can log full URLs/headers at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "aiogram.event", "stripe"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


bind_context = structlog.contextvars.bind_contextvars
clear_context = structlog.contextvars.clear_contextvars
