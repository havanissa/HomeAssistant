"""Logging setup: keeps recent records for Diagnostics and hides the token."""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import logging


class RedactFilter(logging.Filter):
    """Replace secrets in a record's final message, whatever logged it."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 8]

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            message = record.getMessage()
            redacted = message
            for secret in self._secrets:
                redacted = redacted.replace(secret, "**redacted**")
            if redacted != message:
                record.msg, record.args = redacted, None
        return True


class RingBufferHandler(logging.Handler):
    """Keep the most recent log records in memory (for Diagnostics)."""

    def __init__(self, capacity: int = 200) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: deque[dict[str, str]] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                {
                    "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds"),
                    "level": record.levelname.lower(),
                    "logger": record.name,
                    "message": record.getMessage(),
                }
            )
        except Exception:  # noqa: BLE001 - logging must never raise
            self.handleError(record)

    def entries(self) -> list[dict[str, str]]:
        return list(self.records)


def setup_logging(level: str, secrets: list[str]) -> RingBufferHandler:
    """Configure root logging once and return the in-memory buffer."""
    redact = RedactFilter(secrets)
    buffer = RingBufferHandler()
    buffer.addFilter(redact)
    console = logging.StreamHandler()
    console.addFilter(redact)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s (%(name)s) %(message)s", "%Y-%m-%d %H:%M:%S"))

    root = logging.getLogger()
    root.handlers[:] = [console, buffer]
    root.setLevel(level.upper())
    # aiohttp's access log is noisy at info level (every asset request).
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    return buffer
