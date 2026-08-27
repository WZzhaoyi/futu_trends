"""Shared, redacted delivery-result logging for notification channels."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)
_MAX_VALUE_LENGTH = 160


def _compact(value: Any) -> str:
    return " ".join(str(value).split())[:_MAX_VALUE_LENGTH]


def log_delivery(
    channel: str,
    operation: str,
    *,
    ok: bool,
    **details: Any,
) -> None:
    """Emit one concise result line; callers must pass only non-secret details."""
    fields = [
        f"channel={_compact(channel)}",
        f"operation={_compact(operation)}",
        f"ok={str(ok).lower()}",
    ]
    fields.extend(
        f"{key}={_compact(value)}"
        for key, value in details.items()
        if value is not None
    )
    message = "Notification: " + " ".join(fields)
    if ok:
        print(message, flush=True)
    else:
        logger.error(message)


def log_delivery_exception(channel: str, operation: str, exc: Exception) -> None:
    """Log an exception without its potentially sensitive message or request URL."""
    response = getattr(exc, "response", None)
    if response is None:
        response = getattr(exc, "resp", None)
    status = (
        getattr(response, "status_code", None)
        or getattr(response, "status", None)
    )
    log_delivery(
        channel,
        operation,
        ok=False,
        status=status,
        exception=type(exc).__name__,
    )
