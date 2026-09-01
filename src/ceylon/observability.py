"""Structured logging.

Logs are JSON with fixed fields so they are queryable rather than readable-only
(practice 45). Every record carries the ``run_id`` (practice 44), so one id
reconstructs an entire run across extraction, landing and — from Stage 3 — the
``run_metadata`` table.

Nothing here prints. ``print`` is banned by lint for exactly this reason: a print
has no level, no timestamp, no run id, and cannot be filtered.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

__all__ = ["JsonFormatter", "configure_logging", "get_logger"]

# Attributes LogRecord always carries. Anything outside this set was passed by the
# caller via `extra=` and is promoted to a top-level field.
_STANDARD_RECORD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)  # fmt: skip

# Anything whose key looks like a credential is redacted before it can reach a log
# sink, rather than relying on callers to remember (practice 18).
_REDACTED_KEY_HINTS = ("password", "secret", "token", "key", "credential", "auth")
_REDACTED = "***redacted***"


def _redact(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(hint in lowered for hint in _REDACTED_KEY_HINTS):
        return _REDACTED
    return value


class JsonFormatter(logging.Formatter):
    """Render a log record as a single-line JSON object.

    Args:
        run_id: Correlation id stamped onto every record.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "run_id": self._run_id,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = _redact(key, value)
        if record.exc_info:
            payload["error_type"] = (
                record.exc_info[0].__name__ if record.exc_info[0] else "unknown"
            )
            payload["traceback"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(run_id: str, level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent: calling twice replaces the handler rather than doubling output.

    Args:
        run_id: Correlation id for this invocation, from ``new_run_id``.
        level: Minimum level to emit.
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(run_id))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Return a module logger.

    Args:
        name: Usually ``__name__``.
    """
    return logging.getLogger(name)
