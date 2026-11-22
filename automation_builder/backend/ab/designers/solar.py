"""Sun geometry of the Cover Designer: the orientation convention, sectors, angles, and their native templates.

Orientation convention (what the user sees)
    0° = South, measured clockwise seen from above: +90° = West, -90° = East, ±180° = North.

Home Assistant's convention (``sun.sun`` attribute ``azimuth``)
    0° = North, clockwise: 90° = East, 180° = South, 270° = West.

Both run clockwise, so the conversion is a shift by 180°::

    ha   = (user + 180) mod 360
    user = ((ha - 180) mod 360), mapped into (-180, 180]

A window's *sector* is the range of sun directions in which sunlight reaches it: from ``left`` degrees
counter-clockwise of the window's direction to ``right`` degrees clockwise of it. In the northern hemisphere the
sun moves clockwise (east, south, west), so it enters a sector at its counter-clockwise edge.

The *profile angle* (vertical shadow angle) is the elevation of the sun projected onto the plane at right angles
to the window: ``atan(tan(elevation) / cos(relative azimuth))``. It tells how deep direct sunlight reaches into a
room through a vertical window better than the elevation alone when the sun shines in at a slant.

Everything here exists twice: as Python (for the previews and the simulation) and as the Jinja template that
the generated automation evaluates in Home Assistant. Tests render the templates in a real Home Assistant and
compare them with the Python results.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from typing import Any

SUN = "sun.sun"


def normalize(angle: float) -> float:
    """An angle in (-180, 180]."""
    value = math.fmod(float(angle), 360.0)
    if value <= -180:
        value += 360
    elif value > 180:
        value -= 360
    return round(value, 6) + 0.0


def to_ha(user: float) -> float:
    """User orientation (0° = South) -> Home Assistant azimuth (0° = North), in [0, 360)."""
    return round(math.fmod(math.fmod(float(user) + 180.0, 360.0) + 360.0, 360.0), 6) + 0.0


def from_ha(azimuth: float) -> float:
    """Home Assistant azimuth (0° = North) -> user orientation (0° = South), in (-180, 180]."""
    return normalize(float(azimuth) - 180.0)


def relative(azimuth_ha: float, center_user: float) -> float:
    """How far the sun is from the window's direction, in (-180, 180] (negative: counter-clockwise of it)."""
    return normalize(from_ha(azimuth_ha) - float(center_user))


def sector(center_user: float, left: float, right: float) -> tuple[float, float]:
    """The sector as Home Assistant azimuths ``(start, end)``; ``start > end`` when it contains North (0°)."""
    return to_ha(center_user - left), to_ha(center_user + right)


def in_sector(azimuth_ha: float, center_user: float, left: float, right: float) -> bool:
    """Strictly inside, like Home Assistant's ``above``/``below``."""
    start, end = sector(center_user, left, right)
    if start < end:
        return start < azimuth_ha < end
    return azimuth_ha > start or azimuth_ha < end


def profile_angle(elevation: float, azimuth_ha: float, center_user: float) -> float | None:
    """The profile angle in degrees, or None when the sun is behind the window plane."""
    rel = math.radians(relative(azimuth_ha, center_user))
    if math.cos(rel) <= 1e-9:
        return None
    return math.degrees(math.atan2(math.tan(math.radians(elevation)), math.cos(rel)))


def interpolate(angle: float, points: list[list[float]]) -> int:
    """The position for an angle: linear between two points ``[[angle, position], [angle, position]]``, clamped
    to the nearer point outside them, rounded like Home Assistant's ``round`` filter."""
    (a1, p1), (a2, p2) = sorted(points, key=lambda pt: pt[0])
    if angle <= a1:
        value = p1
    elif angle >= a2:
        value = p2
    else:
        value = p1 + (angle - a1) * (p2 - p1) / (a2 - a1)
    return int(round(max(0.0, min(100.0, float(value)))))


def response(basis: str, points: list[list[float]], elevation: float, azimuth_ha: float, center_user: float) -> int | None:
    """What the generated template computes for these sun values (None: not computable, the rule does not act)."""
    angle = elevation if basis == "elevation" else profile_angle(elevation, azimuth_ha, center_user)
    return None if angle is None else interpolate(angle, points)


def _num(value: Any) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


def response_template(basis: str, points: list[list[float]], center_user: float, sun_ref: str = f"'{SUN}'") -> str:
    """The native template of :func:`response` (one line, rendered by Home Assistant into the position)."""
    (a1, p1), (a2, p2) = sorted(points, key=lambda pt: pt[0])
    el = f"(state_attr({sun_ref}, 'elevation') | float(0))"
    if basis == "elevation":
        angle = el
    else:
        az = f"(state_attr({sun_ref}, 'azimuth') | float(0))"
        rel = f"(((({az} - {_num(to_ha(center_user))}) % 360) + 540) % 360 - 180)"
        angle = f"(atan2(tan({el} * pi / 180), cos({rel} * pi / 180)) * 180 / pi)"
    slope = (p2 - p1) / (a2 - a1)
    return ("{% set a = " + angle + " %}"
            "{% set v = " + _num(p1) + " if a <= " + _num(a1) + " else (" + _num(p2) + " if a >= " + _num(a2)
            + " else " + _num(p1) + " + (a - " + _num(a1) + ") * " + _num(slope) + ") %}"
            "{{ ([0, [100, v] | min] | max) | round(0) | int }}")


# --------------------------------------------------------------- the sun's path, for the previews only
def sun_position(when: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """``(elevation, azimuth)`` in degrees (Home Assistant's convention), NOAA's approximation.

    Only for the interface (today's sun path on the compass, the shading preview). The generated automation
    always uses Home Assistant's own ``sun.sun``.
    """
    when = when.astimezone(UTC)
    day = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
                       - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
    decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma) - 0.006758 * math.cos(2 * gamma)
            + 0.000907 * math.sin(2 * gamma) - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
    true_solar = hour * 60 + eqtime + 4 * longitude
    hour_angle = math.radians(true_solar / 4 - 180)
    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(hour_angle)
    zenith = math.acos(max(-1.0, min(1.0, cos_zenith)))
    elevation = 90 - math.degrees(zenith)
    azimuth = math.degrees(math.atan2(math.sin(hour_angle),
                                      math.cos(hour_angle) * math.sin(lat) - math.tan(decl) * math.cos(lat))) + 180
    return round(elevation, 2), round(azimuth % 360, 2)


def day_path(day: datetime, latitude: float, longitude: float, step_minutes: int = 20) -> list[dict[str, Any]]:
    """The sun's positions over one (UTC) day while it is above the horizon."""
    start = day.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    for i in range(0, 24 * 60, step_minutes):
        when = start + timedelta(minutes=i)
        elevation, azimuth = sun_position(when, latitude, longitude)
        if elevation > -1:
            out.append({"at": when.isoformat(timespec="minutes"), "elevation": elevation, "azimuth": azimuth})
    return out
