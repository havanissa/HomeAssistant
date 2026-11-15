"""Sun position against each window (0.6.1): display and configuration only.

Exposure uses the **standard** solar azimuth (0 = north, 90 = east) and
elevation, from ``sun.sun`` or two sensors. A window is *potentially exposed*
when the sun is above the window's minimum elevation, within ± the exposure
width of the window's bearing and not behind a configured obstruction (a hill,
a building). This says nothing about clouds or brightness; it never moves a
cover in this phase.

``sensor.sun_angle`` is the house's compensated angle (0 = the sun faces the
south-facing windows). Its formula and sign are **not assumed**: the
calibration below compares it with the standard azimuth over the day and
reports the relationship only once the samples fit a straight line
(``sun_angle = ±azimuth + c``) closely. Until then it is shown as a value.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise
import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .config import SolarSettings

CALIBRATION_MAX_SAMPLES = 240
CALIBRATION_MIN_SAMPLES = 12
CALIBRATION_MIN_SPAN = 30.0      # degrees of azimuth the samples must cover
CALIBRATION_MAX_RESIDUAL = 2.0   # degrees
CALIBRATION_MIN_STEP = 1.0       # a new sample needs the azimuth to have moved this much


def angle_diff(a: float, b: float) -> float:
    """Signed difference a - b in (-180, 180]."""
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def in_arc(value: float, start: float, end: float) -> bool:
    """Is a bearing inside the clockwise arc from start to end (wrapping through north)?"""
    value, start, end = value % 360, start % 360, end % 360
    return start <= value <= end if start <= end else value >= start or value <= end


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def read_source(hass: HomeAssistant, entity_id: str | None, attribute: str) -> float | None:
    """A sun value: an attribute of sun.sun (azimuth / elevation), or a sensor's state."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    if entity_id.startswith("sun."):
        return _float(state.attributes.get(attribute))
    return _float(state.state)


@dataclass(frozen=True, slots=True)
class SunNow:
    azimuth: float | None
    elevation: float | None
    sun_angle: float | None

    def as_dict(self) -> dict[str, Any]:
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 1)

        return {"azimuth": r(self.azimuth), "elevation": r(self.elevation), "sun_angle": r(self.sun_angle)}


EXPOSURE_TEXT = {
    "exposed": "Potentially exposed to direct sunshine",
    "outside_sector": "The sun is not in front of this window",
    "too_low": "The sun is too low for this window",
    "below_horizon": "The sun is below the horizon",
    "obstructed": "The sun is behind an obstruction",
    "no_direction": "Set the window's direction to see its sun exposure",
    "unknown": "Sun position unavailable",
}


def exposure(bearing: float | None, sun: SunNow, settings: SolarSettings) -> dict[str, Any]:
    """Whether direct sun can reach a window now (geometry only)."""
    out: dict[str, Any] = {"status": "unknown", "offset": None}
    if bearing is None:
        out["status"] = "no_direction"
    elif sun.azimuth is not None and sun.elevation is not None:
        offset = angle_diff(sun.azimuth, bearing)
        out["offset"] = round(offset, 1)
        if sun.elevation <= 0 and sun.elevation < settings.min_elevation:
            out["status"] = "below_horizon"
        elif abs(offset) > settings.exposure_width:
            out["status"] = "outside_sector"
        elif sun.elevation < settings.min_elevation:
            out["status"] = "too_low"
        elif any(in_arc(sun.azimuth, o.from_azimuth, o.to_azimuth) and sun.elevation < o.min_elevation
                 for o in settings.obstructions):
            out["status"] = "obstructed"
        else:
            out["status"] = "exposed"
    out["exposed"] = out["status"] == "exposed"
    out["text"] = EXPOSURE_TEXT[out["status"]]
    return out


# ------------------------------------------------------------ calibration
def _circular_mean(values: Iterable[float]) -> float:
    values = list(values)
    s = sum(math.sin(math.radians(v)) for v in values)
    c = sum(math.cos(math.radians(v)) for v in values)
    return math.degrees(math.atan2(s, c)) % 360.0


def _span(azimuths: Sequence[float]) -> float:
    """Smallest arc covering every azimuth."""
    if len(azimuths) < 2:
        return 0.0
    ordered = sorted(a % 360 for a in azimuths)
    gaps = [b - a for a, b in pairwise(ordered)] + [ordered[0] + 360 - ordered[-1]]
    return 360.0 - max(gaps)


def calibrate(samples: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Fit sun_angle = sign * azimuth + offset over (azimuth, sun_angle) samples; report how well it fits.

    ``status``: ``insufficient`` (too few samples or too little sun movement yet), ``validated`` (both
    the sign and the offset fit every sample within 2°) or ``inconsistent`` (no straight relation:
    the sensor is not a shifted azimuth, so it can be used for its own façade only).
    """
    azimuths = [a for a, _ in samples]
    out: dict[str, Any] = {"samples": len(samples), "span": round(_span(azimuths), 1), "status": "insufficient",
                           "sign": None, "offset": None, "residual": None, "zero_azimuth": None}
    if len(samples) < CALIBRATION_MIN_SAMPLES or out["span"] < CALIBRATION_MIN_SPAN:
        return out
    best: tuple[float, int, float] | None = None
    for sign in (1, -1):
        offsets = [(angle - sign * az) % 360.0 for az, angle in samples]
        mean = _circular_mean(offsets)
        residual = max(abs(angle_diff(o, mean)) for o in offsets)
        if best is None or residual < best[0]:
            best = (residual, sign, mean)
    residual, sign, offset = best
    offset = angle_diff(offset, 0.0)  # show as -180..180
    out.update(residual=round(residual, 1), sign=sign, offset=round(offset, 1))
    if residual <= CALIBRATION_MAX_RESIDUAL:
        out["status"] = "validated"
        # The standard azimuth at which sun_angle reads 0 (the direction its reference windows face).
        out["zero_azimuth"] = round((-offset if sign == 1 else offset) % 360.0, 1)
    else:
        out["status"] = "inconsistent"
    return out


def relation_text(cal: dict[str, Any]) -> str:
    if cal["status"] == "insufficient":
        return (f"Not validated yet: {cal['samples']} samples over {cal['span']:.0f}° of sun movement "
                f"(needs {CALIBRATION_MIN_SAMPLES} over {CALIBRATION_MIN_SPAN:.0f}°).")
    if cal["status"] == "inconsistent":
        return (f"Not a shifted azimuth: the best straight fit is off by up to {cal['residual']}°. "
                "It is shown as a value only.")
    sign = "azimuth" if cal["sign"] == 1 else "−azimuth"
    off = cal["offset"]
    rel = f"sun_angle ≈ {sign} {'+' if off >= 0 else '−'} {abs(off):.1f}°"
    return f"{rel} (within {cal['residual']}° over {cal['samples']} samples); 0° = azimuth {cal['zero_azimuth']:.0f}°."


def add_sample(samples: list[tuple[float, float]], azimuth: float, sun_angle: float) -> bool:
    """Record a reading. A reading at (almost) the same azimuth replaces the last one: the house's
    sun_angle template updates just after the azimuth, so the newest pair is the consistent one."""
    sample = (round(azimuth, 2), round(sun_angle, 2))
    if samples and abs(angle_diff(azimuth, samples[-1][0])) < CALIBRATION_MIN_STEP:
        if samples[-1] == sample:
            return False
        samples[-1] = sample
        return True
    samples.append(sample)
    del samples[:-CALIBRATION_MAX_SAMPLES]
    return True
