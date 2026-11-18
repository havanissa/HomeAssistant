"""Sun events (``homeassistant.helpers.sun`` subset), computed with astral exactly as Home Assistant does."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..core import HomeAssistant
from ..util import dt as dt_util

ELEVATION_AGNOSTIC_EVENTS = ("noon", "midnight")


def get_astral_location(hass: HomeAssistant) -> tuple[Any, float]:
    from astral import LocationInfo  # noqa: PLC0415
    from astral.location import Location  # noqa: PLC0415

    info = LocationInfo("", "", str(hass.config.time_zone), hass.config.latitude, hass.config.longitude)
    return Location(info), hass.config.elevation


def get_astral_observer(hass: HomeAssistant) -> Any:
    from astral import Observer  # noqa: PLC0415

    return Observer(hass.config.latitude, hass.config.longitude, hass.config.elevation)


def get_astral_event_next(hass: HomeAssistant, event: str, utc_point_in_time: datetime | None = None,
                          offset: timedelta | None = None) -> datetime:
    location, elevation = get_astral_location(hass)
    return get_location_astral_event_next(location, elevation, event, utc_point_in_time, offset)


def get_location_astral_event_next(location: Any, elevation: float, event: str,
                                   utc_point_in_time: datetime | None = None,
                                   offset: timedelta | None = None) -> datetime:
    offset = offset or timedelta()
    utc_point_in_time = utc_point_in_time or dt_util.utcnow()
    kwargs: dict[str, Any] = {"local": False}
    if event not in ELEVATION_AGNOSTIC_EVENTS:
        kwargs["observer_elevation"] = elevation
    mod = -1
    first_err: ValueError | None = None
    while mod < 367:
        try:
            next_dt = getattr(location, event)(
                dt_util.as_local(utc_point_in_time).date() + timedelta(days=mod), **kwargs) + offset
            if next_dt > utc_point_in_time:
                return next_dt
        except ValueError as err:
            first_err = first_err or err
        mod += 1
    raise ValueError(f"Unable to find event after one year, initial ValueError: {first_err}") from first_err
