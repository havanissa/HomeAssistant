"""Where the sun is, and how much of it reaches a window.

Home Assistant's ``sun.sun`` entity gives the sun's position *now*. A forecast needs it for every future step,
and learning needs it for every past one (also for days the Recorder no longer has), so the position is computed
here from the location Home Assistant is configured with, with the standard NOAA formulas (accurate to a few
hundredths of a degree, far finer than a window's geometry). ``sun.sun`` is only used to cross-check the
calculation in the diagnostics.

Orientation convention of this App: **0° = South**, counted clockwise as seen from above: 90° = West,
180° = North, 270° = East. Home Assistant (and ``position``) use compass azimuths (0° = North, 90° = East); the
two differ by exactly 180°: ``compass = (orientation + 180) mod 360``.

Clouds. Without a sunlight or irradiance sensor the only evidence of clouds is a weather entity's cloud
coverage (measured now, forecast for later). ``clear_fraction`` turns it into the share of direct sun that is
assumed to come through, with the Kasten-Czeplak relation. That is an *estimate of an estimate*: it is never
stored or shown as an irradiance measurement, and everything computed from it is labelled ``estimate``.
"""

from __future__ import annotations

from datetime import UTC, datetime
import math

COMPASS_NAMES = (("S", 0), ("SW", 45), ("W", 90), ("NW", 135), ("N", 180), ("NE", 225), ("E", 270), ("SE", 315))


def to_compass(orientation: float) -> float:
    """A window orientation of this App (0° = South, clockwise) as a compass bearing (0° = North)."""
    return (float(orientation) + 180.0) % 360.0


def from_compass(bearing: float) -> float:
    return (float(bearing) + 180.0) % 360.0


def orientation_name(orientation: float) -> str:
    index = int(((float(orientation) % 360.0) + 22.5) // 45) % 8
    return COMPASS_NAMES[index][0]


def position(t: float, latitude: float, longitude: float) -> tuple[float, float]:
    """``(elevation, compass azimuth)`` of the sun in degrees at UTC timestamp ``t`` (NOAA solar position)."""
    when = datetime.fromtimestamp(t, UTC)
    day = when.timetuple().tm_yday
    hours = when.hour + when.minute / 60.0 + when.second / 3600.0
    days_in_year = 366.0 if when.year % 4 == 0 and (when.year % 100 != 0 or when.year % 400 == 0) else 365.0
    gamma = 2.0 * math.pi / days_in_year * (day - 1 + (hours - 12.0) / 24.0)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    true_solar_minutes = (hours * 60.0 + eqtime + 4.0 * longitude) % 1440.0
    hour_angle = math.radians(true_solar_minutes / 4.0 - 180.0)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)
    sin_zenith = math.sin(zenith)
    if abs(sin_zenith) < 1e-9:
        azimuth = 180.0
    else:
        cos_az = (math.sin(lat) * cos_zenith - math.sin(decl)) / (math.cos(lat) * sin_zenith)
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cos_az))))
        azimuth = (angle + 180.0) % 360.0 if hour_angle > 0 else (540.0 - angle) % 360.0
    return elevation, azimuth


def clear_fraction(cloud_coverage: float | None) -> float | None:
    """The assumed share of direct sunshine at this cloud coverage (0-100 %): 1 - 0.75 n^3.4 (Kasten-Czeplak).

    An estimate from a coarse number, not a measurement. ``None`` in, ``None`` out: an unknown sky stays unknown.
    """
    if cloud_coverage is None:
        return None
    n = max(0.0, min(100.0, float(cloud_coverage))) / 100.0
    return max(0.0, 1.0 - 0.75 * n ** 3.4)


def daylight(t: float, latitude: float, longitude: float) -> bool:
    return position(t, latitude, longitude)[0] > 0.0
