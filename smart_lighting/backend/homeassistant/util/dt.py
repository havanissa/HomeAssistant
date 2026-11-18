"""Date and time helpers (Home Assistant's ``homeassistant.util.dt`` subset), in Home Assistant's time zone."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIME_ZONE: tzinfo = UTC


def get_time_zone(name: str) -> tzinfo | None:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def set_default_time_zone(zone: tzinfo) -> None:
    global DEFAULT_TIME_ZONE  # noqa: PLW0603 - Home Assistant's own pattern
    DEFAULT_TIME_ZONE = zone


def get_default_time_zone() -> tzinfo:
    return DEFAULT_TIME_ZONE


def utcnow() -> datetime:
    return datetime.now(UTC)


def now(time_zone: tzinfo | None = None) -> datetime:
    return datetime.now(time_zone or DEFAULT_TIME_ZONE)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=DEFAULT_TIME_ZONE)
    return value.astimezone(UTC)


def as_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(DEFAULT_TIME_ZONE)


def utc_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, UTC)


def as_timestamp(value: datetime | str) -> float:
    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        raise ValueError("not a valid date/time")
    return as_utc(parsed).timestamp()


def parse_datetime(value: str, *, raise_on_error: bool = False) -> datetime | None:
    """An ISO 8601 date/time (aware when it carries an offset), or None."""
    if not isinstance(value, str):
        if raise_on_error:
            raise ValueError("not a string")
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        if raise_on_error:
            raise
        return None
