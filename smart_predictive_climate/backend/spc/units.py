"""Temperatures and small conversions shared by the observation and learning modules."""

from __future__ import annotations

from typing import Any

#: hvac_action values that mean "this device is heating now".
HEATING_ACTIONS = {"heating", "preheating"}
#: Plausible readings (°C). Outside them a reading is kept but marked implausible.
PLAUSIBLE = {"indoor": (-20.0, 50.0), "outdoor": (-60.0, 60.0), "target": (0.0, 40.0)}


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def to_celsius(value: Any, unit: str | None) -> float | None:
    """A temperature in °C, or None when it is not a number or the unit is not a temperature unit."""
    number = as_float(value)
    if number is None:
        return None
    unit = (unit or "").strip()
    if unit in ("°C", "C", "℃"):
        return number
    if unit in ("°F", "F", "℉"):
        return (number - 32.0) * 5.0 / 9.0
    if unit == "K":
        return number - 273.15
    return None


def climate_unit(attrs: dict[str, Any], ha_unit: str) -> str:
    """The unit of a climate entity's temperatures: its ``temperature_unit`` attribute, else the system unit."""
    unit = attrs.get("temperature_unit")
    return unit if unit in ("°C", "°F") else ha_unit
