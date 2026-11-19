"""Multi-sensor sunlight for one window (App 0.8.0, Phase A6.1). Pure: no Home Assistant here.

A window may name several illuminance sensors as its own sunlight evidence. The rules are explicit, with no
average, median or majority vote:

* **Activate** - as soon as ANY enabled sensor with a healthy reading reports **more than** the activation level.
  Agreement of the other sensors is not required.
* **Release** - only when ALL enabled sensors report healthy readings **strictly below** the release level, and
  have done so continuously for the release delay. Any sensor at or above the release level restarts the
  countdown; a sensor above the activation level keeps protection active and restarts it too.
* **Invalid readings** (missing, unavailable, unknown, non-numeric, stale, stuck, implausible - judged by
  ``environment.judge_lux``) never count as sunshine. While protection is active they never count as "below the
  release level" either: release cannot be confirmed, the countdown restarts, protection is kept and the reason
  says which sensor prevents it. Only a reviewed configuration change (disabling or removing the sensor) ends that.

The release level must be lower than the activation level (hysteresis); ``check_levels`` refuses anything else.

Persistence: ``state`` (active, which sensor triggered it, since when) survives a restart; the countdown does
not. After a restart the release delay starts again from the first healthy all-below evaluation, so a restart,
a missed update or a stale timestamp can never shorten it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

MAX_SENSORS = 8
DEFAULT_ON_LUX = 30000
DEFAULT_OFF_LUX = 15000
DEFAULT_RELEASE_MINUTES = 15


@dataclass(frozen=True, slots=True)
class SunSensor:
    """One illuminance sensor of a window's sunlight evidence."""

    entity_id: str
    enabled: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: Any) -> SunSensor | None:
        if isinstance(data, str):
            data = {"entity_id": data}
        if not isinstance(data, Mapping):
            return None
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("sensor.") or len(entity_id) > 255:
            return None
        return cls(entity_id, data.get("enabled") is not False)


def sensors_from(raw: Any) -> tuple[SunSensor, ...]:
    out: list[SunSensor] = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        sensor = SunSensor.from_dict(item)
        if sensor is not None and all(s.entity_id != sensor.entity_id for s in out):
            out.append(sensor)
    return tuple(out[:MAX_SENSORS])


def check_levels(on_lux: Any, off_lux: Any) -> str | None:
    """A problem with the thresholds, or None. The release level must be lower than the activation level."""
    for value in (on_lux, off_lux):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            return "The sunlight levels must be numbers of 0 lx or more"
    if off_lux >= on_lux:
        return "“Release below” must be lower than “Activate above”"
    return None


@dataclass(slots=True)
class SunState:
    """The window's multi-sensor sunlight state. ``active``/``triggered_by``/``since`` are stored; ``countdown``
    (the start of the all-below period) is deliberately not."""

    active: bool = False
    triggered_by: str | None = None
    since: datetime | None = None
    countdown: datetime | None = None

    def export(self) -> dict[str, Any]:
        return {"active": self.active, "triggered_by": self.triggered_by,
                "since": self.since.isoformat() if self.since else None}

    @classmethod
    def restore(cls, data: Any) -> SunState:
        if not isinstance(data, Mapping) or data.get("active") is not True:
            return cls()
        since = data.get("since")
        try:
            when = datetime.fromisoformat(since) if isinstance(since, str) else None
        except ValueError:
            when = None
        trigger = data.get("triggered_by")
        # Restored conservatively: still active, and the release countdown starts again from zero.
        return cls(True, trigger if isinstance(trigger, str) else None, when, None)


@dataclass(frozen=True, slots=True)
class SensorReading:
    """One sensor as the rules see it: ``value`` is only set for a healthy reading."""

    entity_id: str
    enabled: bool
    ok: bool
    value: float | None
    status: str          # ok / missing / unavailable / stale / stuck / implausible / disabled
    detail: str = ""


@dataclass(slots=True)
class SunResult:
    active: bool
    changed: str | None                     # "activated" / "released" / None
    triggered_by: str | None
    since: datetime | None
    above_on: list[str] = field(default_factory=list)       # healthy sensors above the activation level
    blocking: list[str] = field(default_factory=list)       # healthy sensors at or above the release level
    invalid: list[str] = field(default_factory=list)        # enabled sensors without a trustworthy reading
    countdown_started: datetime | None = None
    release_at: datetime | None = None
    remaining_seconds: int | None = None
    reason: str = ""
    release_confirmable: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"active": self.active, "changed": self.changed, "triggered_by": self.triggered_by,
                "since": self.since.isoformat() if self.since else None, "above_on": list(self.above_on),
                "blocking": list(self.blocking), "invalid": list(self.invalid),
                "countdown_started": self.countdown_started.isoformat() if self.countdown_started else None,
                "release_at": self.release_at.isoformat() if self.release_at else None,
                "remaining_seconds": self.remaining_seconds, "reason": self.reason,
                "release_confirmable": self.release_confirmable}


def _lux(value: float | None) -> str:
    return f"{value:.0f} lx" if value is not None else "—"


def evaluate(state: SunState, readings: Iterable[SensorReading], *, on_lux: float, off_lux: float,
             release_minutes: float, now: datetime, names: Mapping[str, str] | None = None) -> SunResult:
    """Apply the ANY-activate / ALL-release rules once and update ``state`` in place."""
    names = names or {}

    def name(entity_id: str) -> str:
        return names.get(entity_id) or entity_id

    enabled = [r for r in readings if r.enabled]
    healthy = [r for r in enabled if r.ok and r.value is not None]
    invalid = [r.entity_id for r in enabled if not (r.ok and r.value is not None)]
    above_on = [r.entity_id for r in healthy if r.value > on_lux]  # type: ignore[operator]
    blocking = [r.entity_id for r in healthy if r.value >= off_lux]  # type: ignore[operator]
    value_of = {r.entity_id: r.value for r in healthy}
    result = SunResult(state.active, None, state.triggered_by, state.since, above_on, blocking, invalid)

    if not state.active:
        state.countdown = None
        if above_on:
            first = max(above_on, key=lambda e: value_of[e] or 0)
            state.active, state.triggered_by, state.since = True, first, now
            result.active, result.changed, result.triggered_by, result.since = True, "activated", first, now
            result.reason = (f"Direct sunlight: {name(first)} reads {_lux(value_of[first])}, above "
                             f"{on_lux:.0f} lx")
        elif not enabled:
            result.reason = "No sensor is enabled"
        elif not healthy:
            result.reason = ("No trustworthy reading (" + ", ".join(name(e) for e in invalid)
                             + "): an invalid sensor is never evidence of sunshine")
        else:
            peak = max(healthy, key=lambda r: r.value or 0)
            result.reason = f"No direct sunlight: the brightest sensor, {name(peak.entity_id)}, reads " \
                            f"{_lux(peak.value)} (activates above {on_lux:.0f} lx)"
        return result

    # Active: release needs every enabled sensor healthy and strictly below the release level, continuously.
    if above_on:
        state.countdown = None
        result.reason = ("Still direct sunlight: " + ", ".join(f"{name(e)} {_lux(value_of[e])}" for e in above_on)
                         + f" above {on_lux:.0f} lx")
        return result
    if invalid:
        state.countdown = None
        result.release_confirmable = False
        result.reason = ("Release cannot be confirmed: " + ", ".join(name(e) for e in invalid)
                         + " has no trustworthy reading. Protection is kept; disable or remove a failed sensor to "
                           "release without it")
        return result
    if blocking:
        state.countdown = None
        result.reason = ("Release waits: " + ", ".join(f"{name(e)} {_lux(value_of[e])}" for e in blocking)
                         + f" at or above {off_lux:.0f} lx")
        return result
    if not enabled:
        # Every sensor was disabled (a reviewed change): nothing keeps the multi-sensor condition any more.
        state.active, state.triggered_by, state.since, state.countdown = False, None, None, None
        result.active, result.changed, result.triggered_by, result.since = False, "released", None, None
        result.reason = "Released: no sensor is enabled any more"
        return result
    if state.countdown is None:
        state.countdown = now
    release_at = state.countdown + timedelta(minutes=release_minutes)
    result.countdown_started = state.countdown
    if now >= release_at:
        state.active, state.triggered_by, state.since, state.countdown = False, None, None, None
        result.active, result.changed, result.triggered_by, result.since = False, "released", None, None
        result.reason = (f"Released: every sensor below {off_lux:.0f} lx for {release_minutes:g} min")
        return result
    result.release_at = release_at
    result.remaining_seconds = max(0, int((release_at - now).total_seconds() + 0.999))
    result.reason = (f"Every sensor below {off_lux:.0f} lx: released in {result.remaining_seconds // 60} min "
                     f"{result.remaining_seconds % 60:02d} s unless one rises again")
    return result


def simulate(state: SunState, steps: Iterable[tuple[float, Mapping[str, Any]]], sensors: Iterable[SunSensor], *,
             on_lux: float, off_lux: float, release_minutes: float, start: datetime,
             names: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """A read-only run of the rules: ``steps`` are (minutes after ``start``, {entity: lux, None (invalid) or
    "unavailable"}). Works on a copy of ``state``; returns one result per step."""
    work = SunState(state.active, state.triggered_by, state.since, state.countdown)
    sensors = list(sensors)
    out = []
    for minutes, values in steps:
        readings = []
        for sensor in sensors:
            raw = values.get(sensor.entity_id)
            ok = isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0
            readings.append(SensorReading(sensor.entity_id, sensor.enabled, ok, float(raw) if ok else None,
                                          "ok" if ok else ("disabled" if not sensor.enabled else "unavailable")))
        result = evaluate(work, readings, on_lux=on_lux, off_lux=off_lux, release_minutes=release_minutes,
                          now=start + timedelta(minutes=minutes), names=names)
        out.append({"minutes": minutes, **result.as_dict()})
    return out
