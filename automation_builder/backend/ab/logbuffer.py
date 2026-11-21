"""Logging set-up with a bounded in-memory buffer (shown in Settings -> Diagnostics); secrets are redacted."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import logging
from typing import Any

LOG_BUFFER_SIZE = 300


class BufferHandler(logging.Handler):
    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=LOG_BUFFER_SIZE)
        self._secrets = [s for s in secrets if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.redact(record.getMessage())
        except Exception:  # noqa: BLE001 - never fail on logging
            message = str(record.msg)
        self.records.append({
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
            "level": record.levelname, "logger": record.name, "message": message[:2000],
        })

    def entries(self, level: str | None = None) -> list[dict[str, Any]]:
        wanted = logging.getLevelName(level.upper()) if level else 0
        return [r for r in self.records if logging.getLevelName(r["level"]) >= wanted]


class _Redacting(logging.Formatter):
    def __init__(self, handler: BufferHandler) -> None:
        super().__init__("%(asctime)s %(levelname)s (%(name)s) %(message)s", "%Y-%m-%d %H:%M:%S")
        self._handler = handler

    def format(self, record: logging.LogRecord) -> str:
        return self._handler.redact(super().format(record))


def setup_logging(level: str, secrets: list[str]) -> BufferHandler:
    buffer = BufferHandler(secrets)
    root = logging.getLogger()
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(_Redacting(buffer))
    root.addHandler(console)
    root.addHandler(buffer)
    root.setLevel(logging.getLevelName(level.upper()))
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return buffer
