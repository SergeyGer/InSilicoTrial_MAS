"""Structured logging helpers.

Spark executors and Databricks notebooks have very different logging setups, so
the platform never relies on the root logger configuration: every module obtains
a named logger through :func:`get_logger`, and the CLI configures a single
handler for the driver process.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_JSON_FORMAT = True  # Spark/executor-friendly default; flipped by configure_logging


class _JsonFormatter(logging.Formatter):
    """Minimal JSON formatter - keeps worker logs machine-parseable in Delta."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        return json.dumps(payload, default=str)


_configured = False


def configure_logging(level: str | int | None = None, *, json_logs: bool | None = None) -> None:
    """Configure the process-wide logging handler (idempotent).

    Parameters
    ----------
    level:
        Log level name or numeric level. Defaults to ``$INSILICO_LOG_LEVEL`` or ``INFO``.
    json_logs:
        Emit one JSON object per line (default) or human-readable lines.
    """
    global _configured, _JSON_FORMAT
    level = level or os.environ.get("INSILICO_LOG_LEVEL", "INFO")
    if json_logs is not None:
        _JSON_FORMAT = json_logs
    if _configured:
        logging.getLogger("insilico_trial_mas").setLevel(level)
        return

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(_JsonFormatter() if _JSON_FORMAT else logging.Formatter(_LOG_FORMAT))
    root = logging.getLogger("insilico_trial_mas")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    _configured = True

    # Third-party noise control: Spark and botocore are extremely chatty.
    for noisy in ("py4j", "botocore", "urllib3", "mlflow", "langchain"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


#: Fixed error vocabulary. Log lines and the ``llm_error`` column must be
#: machine-parsable and, more importantly, must never contain text produced by a
#: provider SDK: those messages are not careful about what they echo back and can
#: contain the request payload, the prompt or the API key
#: (CodeQL ``py/clear-text-logging-sensitive-data``).
ERROR_KINDS: tuple[tuple[type[BaseException], str], ...] = (
    (TimeoutError, "timeout"),
    (ConnectionError, "connection"),
    (PermissionError, "permission"),
    (FileNotFoundError, "not-found"),
    (OSError, "io-error"),
    (KeyError, "missing-field"),
    (ValueError, "invalid-value"),
    (TypeError, "invalid-type"),
    (ArithmeticError, "arithmetic-error"),
)


def error_kind(exc: BaseException) -> str:
    """Classify an exception into a fixed label for logs and Silver columns.

    Returns a constant from :data:`ERROR_KINDS`, never a string built from the
    exception, so no provider text can leak into a log, a Delta table or an
    attached issue. Use :func:`debug_error_text` when a human-readable message is
    genuinely required - and only in an exception, never in a log call.
    """
    for exception_type, label in ERROR_KINDS:
        if isinstance(exc, exception_type):
            return label
    return "unexpected-error"


def debug_error_text(exc: BaseException) -> str:
    """Full ``Type: message`` text for the developer, for exception chaining only.

    Safe in ``raise ... from``/``raise X(f"... {debug_error_text(exc)}")`` because
    exception objects are not persisted, but it must never be passed to a logger or
    written to a store: the message is outside our control.
    """
    return f"{type(exc).__name__}: {exc}"


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger, configuring logging on first use."""
    if not _configured:
        configure_logging()
    return logging.getLogger(f"insilico_trial_mas.{name}")


def log_extra(**fields: Any) -> dict[str, Any]:
    """Build the ``extra`` payload understood by :class:`_JsonFormatter`."""
    return {"extra_fields": fields}
