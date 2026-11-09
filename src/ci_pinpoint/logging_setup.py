"""Structured logging to stderr: human-readable by default, JSON with
``CI_PINPOINT_LOG_FORMAT=json``. Stdout is reserved for the brief."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in vars(record).items() if k not in _RESERVED}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            **_extras(record),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(f"{k}={v}" for k, v in _extras(record).items())
        return f"{record.levelname.lower():7} {record.getMessage()}" + (
            f"  {extras}" if extras else ""
        )


def configure(verbosity: int, env: Mapping[str, str]) -> None:
    level = logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if env.get("CI_PINPOINT_LOG_FORMAT", "").lower() == "json"
        else TextFormatter()
    )
    root = logging.getLogger("ci_pinpoint")
    root.handlers[:] = [handler]
    root.setLevel(level)
    root.propagate = False
