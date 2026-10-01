"""Structured JSON logging with context variables and secret redaction."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# A key is redacted if it equals one of these, or if any of its "_"/"-" separated parts does.
_SENSITIVE_PARTS = {
    "token", "secret", "password", "pin", "totp", "authorization", "jwt", "checksum",
    "credentials", "jwttoken",
}
_SENSITIVE_KEYS = {"api_key", "x-api-key", "code", "request_token", "apikey", "privatekey"}
REDACTED = "***"


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    if k in _SENSITIVE_KEYS:
        return True
    parts = k.replace("-", "_").split("_")
    return any(p in _SENSITIVE_PARTS for p in parts)


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and _is_sensitive(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


def _redact_processor(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return redact(event_dict)


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    # httpx logs full request lines at INFO; keep it quiet so URLs/headers never reach the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_processor,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
