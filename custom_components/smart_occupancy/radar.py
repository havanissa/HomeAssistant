"""Radar distance zones (0.6.8, Phase 4.8): a zone active while a radar sees someone within a distance band.

Pure logic: configuration, distance units and the band tracker. The zone
runtime (``zone_runtime.py``) feeds it the radar's presence state and its
distance measurement; everything after that - the zone's active state, its
priority, ``holds_room``, room-vacancy gating, the brightness matrix column
and the lighting arbitration - is the existing zone machinery.

A **radial band**, not an area: a radar that reports one target distance tells
how far the (nearest or strongest) target is, not where it is or how many
people there are. ``1.0-3.0 m`` is every point between 1 and 3 m from the
radar, in every direction it can see. Several people at once cannot be told
apart from one distance value; nothing here claims to. A radar with documented
multi-target outputs could feed one zone per target distance entity later
(``RadarConfig`` names one distance source on purpose).

Activation
----------
A distance zone is *eligible* only when **all** of these hold:

1. the radar's presence entity reports detection (distance alone is never
   presence; a stale distance while presence is clear never activates it),
2. the distance is a valid measurement (a number, finite, not negative, in a
   supported unit; optionally not older than ``max_age``),
3. it is inside ``[min, max]`` (both boundaries inside),
4. that held for ``entry_delay`` seconds,

and the zone runtime still requires the room not to be vacant.

Release and hysteresis
----------------------
An active zone stays active while presence is detected and the distance is
within ``[min - hysteresis, max + hysteresis]``. Leaving that (or presence
clearing) starts the ``exit_delay``; coming back cancels it. So a person
standing at 3.0 m does not switch the zone on and off.

Unavailable distance (``unavailable``, the zone's fail-safe): ``hold`` keeps
the zone as it is while presence is detected; ``vacant`` releases it after
the exit delay; ``occupied`` keeps it active while presence is detected. An
unavailable *presence* entity: ``hold`` keeps the state, ``vacant`` releases,
``occupied`` keeps it active (while the room is occupied). A sustained,
explicit absence (presence off) always releases the zone after the exit
delay, whatever the fail-safe. An unavailable value is never read as 0 m.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Any

DEFAULT_ENTRY_DELAY = 2.0
DEFAULT_EXIT_DELAY = 15.0
DEFAULT_HYSTERESIS = 0.2
DELAY_MAX = 600.0
HYSTERESIS_MAX = 2.0
DISTANCE_MAX = 50.0
MAX_AGE_MAX = 3600

# Supported units -> factor to metres. Anything else is refused (never guessed).
UNITS: dict[str, float] = {"m": 1.0, "cm": 0.01, "mm": 0.001}
UNIT_NAMES = {"m": "metres", "cm": "centimetres", "mm": "millimetres"}
_ON = frozenset({"on", "detected", "occupied", "home", "true"})
_OFF = frozenset({"off", "clear", "not_occupied", "not_home", "false"})
_GONE = frozenset({"unavailable", "unknown", "none", ""})

INACTIVE = "inactive"
ENTERING = "entering"
ACTIVE = "active"
LEAVING = "leaving"


def _float(value: Any, low: float, high: float, default: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return round(min(max(number, low), high), 3)


@dataclass(frozen=True, slots=True)
class RadarConfig:
    """The radar inputs and the band of one distance zone."""

    presence: str                  # binary_sensor (or another on/off entity) of the radar
    distance: str                  # the distance sensor (or the entity whose attribute holds the distance)
    min_m: float = 1.0
    max_m: float = 3.0
    attribute: str | None = None   # a numeric attribute of ``distance`` instead of its state
    unit: str | None = None        # None: the entity's unit_of_measurement (an attribute needs a unit here)
    entry_delay: float = DEFAULT_ENTRY_DELAY
    exit_delay: float = DEFAULT_EXIT_DELAY
    hysteresis: float = DEFAULT_HYSTERESIS
    max_age: int | None = None     # seconds; a distance not reported for longer is invalid (None: no limit)

    @classmethod
    def from_dict(cls, data: Any) -> RadarConfig | None:
        if not isinstance(data, dict):
            return None
        presence, distance = data.get("presence"), data.get("distance")
        if not isinstance(presence, str) or "." not in presence or not isinstance(distance, str) \
                or "." not in distance:
            return None
        low = _float(data.get("min_m"), 0, DISTANCE_MAX, None)
        high = _float(data.get("max_m"), 0, DISTANCE_MAX, None)
        if low is None or high is None or high <= low:
            return None
        unit = data.get("unit")
        attribute = data.get("attribute")
        max_age = _float(data.get("max_age"), 1, MAX_AGE_MAX, None)
        return cls(
            presence=presence, distance=distance, min_m=low, max_m=high,
            attribute=attribute.strip() or None if isinstance(attribute, str) else None,
            unit=unit if unit in UNITS else None,
            entry_delay=_float(data.get("entry_delay"), 0, DELAY_MAX, DEFAULT_ENTRY_DELAY) or 0.0,
            exit_delay=_float(data.get("exit_delay"), 0, DELAY_MAX, DEFAULT_EXIT_DELAY) or 0.0,
            hysteresis=_float(data.get("hysteresis"), 0, HYSTERESIS_MAX, DEFAULT_HYSTERESIS) or 0.0,
            max_age=int(max_age) if max_age else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"presence": self.presence, "distance": self.distance, "min_m": self.min_m, "max_m": self.max_m,
                "attribute": self.attribute, "unit": self.unit, "entry_delay": self.entry_delay,
                "exit_delay": self.exit_delay, "hysteresis": self.hysteresis, "max_age": self.max_age}

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.presence, self.distance)))

    def band(self) -> str:
        return f"{fmt_m(self.min_m)}–{fmt_m(self.max_m)} m"

    def inside(self, metres: float) -> bool:
        return self.min_m <= metres <= self.max_m

    def within_margin(self, metres: float) -> bool:
        return self.min_m - self.hysteresis <= metres <= self.max_m + self.hysteresis


def fmt_m(metres: float | None) -> str:
    """Metres with one or two decimals: 1.0, 2.15."""
    if metres is None:
        return "—"
    text = f"{metres:.2f}"
    return text[:-1] if text.endswith("0") else text


# ------------------------------------------------------------------ readings
@dataclass(frozen=True, slots=True)
class Distance:
    """One distance reading: metres, or why it is not usable."""

    metres: float | None
    problem: str | None = None     # None: valid
    raw: Any = None
    unit: str | None = None

    @property
    def valid(self) -> bool:
        return self.metres is not None and self.problem is None


def to_metres(value: Any, unit: str | None) -> Distance:
    """A raw value in a unit -> metres. Refuses unknown units, non-numbers, non-finite and negative values."""
    if value is None or (isinstance(value, str) and value.strip().casefold() in _GONE):
        return Distance(None, "unavailable", value, unit)
    if unit not in UNITS:
        return Distance(None, f"unsupported unit {unit!r}" if unit else "no unit (choose metres, centimetres "
                        "or millimetres)", value, unit)
    if isinstance(value, bool):
        return Distance(None, "not a number", value, unit)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return Distance(None, "not a number", value, unit)
    if not math.isfinite(number):
        return Distance(None, "not a finite number", value, unit)
    if number < 0:
        return Distance(None, "negative distance", value, unit)
    return Distance(round(number * UNITS[unit], 4), None, value, unit)


def read_distance(cfg: RadarConfig, state: Any, now: datetime | None = None) -> Distance:
    """The configured distance from a Home Assistant state object (or None)."""
    if state is None:
        return Distance(None, "missing")
    attrs = getattr(state, "attributes", {}) or {}
    if cfg.attribute:
        if state.state in ("unavailable", "unknown"):
            return Distance(None, "unavailable", state.state)
        value, unit = attrs.get(cfg.attribute), cfg.unit
        if value is None:
            return Distance(None, f"no attribute {cfg.attribute}")
    else:
        value = state.state
        reported = attrs.get("unit_of_measurement")
        if reported is not None and cfg.unit is not None and reported != cfg.unit:
            return Distance(None, f"the entity reports {reported}, not {cfg.unit}", value, reported)
        unit = reported if reported is not None else cfg.unit
    reading = to_metres(value, unit)
    if reading.valid and cfg.max_age and now is not None:
        at = getattr(state, "last_reported", None) or getattr(state, "last_updated", None)
        if at is not None and now - at > timedelta(seconds=cfg.max_age):
            return Distance(None, f"stale (not reported for over {cfg.max_age} s)", value, unit)
    return reading


def presence_of(state: Any) -> bool | None:
    """True: detection; False: explicitly clear; None: unavailable or not understood."""
    if state is None:
        return None
    value = str(state.state).casefold()
    if value in _ON:
        return True
    if value in _OFF:
        return False
    return None


# ------------------------------------------------------------------ tracker
@dataclass(slots=True)
class Evaluation:
    active: bool
    phase: str                    # inactive | entering | active | leaving
    reason: str
    due: datetime | None = None   # when the phase may change without a new reading
    failsafe: str | None = None   # the fail-safe in force (distance or presence unavailable)
    known: bool = True            # presence known (an unknown radar never holds the room)


@dataclass(slots=True)
class RadarTracker:
    """The band state of one distance zone (see the module docstring)."""

    cfg: RadarConfig
    phase: str = INACTIVE
    since: datetime | None = None   # when the current phase started

    def restore(self, active: bool, since: datetime | None) -> None:
        self.phase, self.since = (ACTIVE, since) if active else (INACTIVE, None)

    def _to(self, phase: str, now: datetime) -> None:
        if phase != self.phase:
            self.phase, self.since = phase, now

    def evaluate(self, presence: bool | None, reading: Distance, now: datetime, *, failsafe: str = "hold",
                 room_occupied: bool = True) -> Evaluation:
        cfg = self.cfg
        d = reading.metres if reading.valid else None
        where = f"{fmt_m(d)} m" if d is not None else (reading.problem or "no distance")
        active_like = self.phase in (ACTIVE, LEAVING)
        # 1. Presence unknown: the zone's fail-safe.
        if presence is None:
            if failsafe == "vacant":
                return self._leave(now, "radar presence unavailable (fail-safe: release)", failsafe, known=False)
            if failsafe == "occupied" and room_occupied:
                self._to(ACTIVE, now)
                return Evaluation(True, ACTIVE, "radar presence unavailable (fail-safe: occupied while the room "
                                  "is occupied)", failsafe=failsafe, known=False)
            if active_like:
                self._to(ACTIVE, now)
            elif self.phase == ENTERING:
                self._to(INACTIVE, now)
            return Evaluation(self.phase == ACTIVE, self.phase, "radar presence unavailable (fail-safe: hold)",
                              failsafe=failsafe, known=False)
        # 2. Explicit absence: always released after the exit delay.
        if presence is False:
            if active_like:
                return self._leave(now, "radar presence clear")
            self._to(INACTIVE, now)
            return Evaluation(False, INACTIVE, "radar presence clear" + (
                f" (last distance {where} is not used)" if d is not None else ""))
        # 3. Presence detected, distance unusable: the fail-safe (never read as 0 m).
        if d is None:
            if failsafe == "vacant":
                if active_like:
                    return self._leave(now, f"distance {where} (fail-safe: release)", failsafe)
                self._to(INACTIVE, now)
                return Evaluation(False, INACTIVE, f"distance {where}", failsafe=failsafe)
            if failsafe == "occupied":
                self._to(ACTIVE, now)
                return Evaluation(True, ACTIVE, f"distance {where} (fail-safe: occupied while presence is "
                                  "detected)", failsafe=failsafe)
            if active_like:
                self._to(ACTIVE, now)
                return Evaluation(True, ACTIVE, f"distance {where} (fail-safe: hold)", failsafe=failsafe)
            self._to(INACTIVE, now)
            return Evaluation(False, INACTIVE, f"distance {where} (fail-safe: hold; not activated)",
                              failsafe=failsafe)
        # 4. A valid distance while presence is detected.
        if active_like:
            if cfg.within_margin(d):
                self._to(ACTIVE, now)
                margin = "" if cfg.inside(d) else f", within the {fmt_m(cfg.hysteresis)} m hysteresis margin"
                return Evaluation(True, ACTIVE, f"distance {where} within {cfg.band()}{margin}")
            return self._leave(now, f"distance {where} outside {cfg.band()}")
        if not cfg.inside(d):
            self._to(INACTIVE, now)
            return Evaluation(False, INACTIVE, f"distance {where} outside {cfg.band()}")
        if self.phase != ENTERING:
            self._to(ENTERING, now)
        due = (self.since or now) + timedelta(seconds=cfg.entry_delay)
        if now >= due:
            self._to(ACTIVE, now)
            return Evaluation(True, ACTIVE, f"distance {where} within {cfg.band()}")
        return Evaluation(False, ENTERING, f"distance {where} within {cfg.band()}; entering after "
                          f"{cfg.entry_delay:g} s", due=due)

    def _leave(self, now: datetime, why: str, failsafe: str | None = None, *, known: bool = True) -> Evaluation:
        if self.phase == ACTIVE:
            self._to(LEAVING, now)
        if self.phase != LEAVING:
            self._to(INACTIVE, now)
            return Evaluation(False, INACTIVE, why, failsafe=failsafe, known=known)
        due = (self.since or now) + timedelta(seconds=self.cfg.exit_delay)
        if now >= due:
            self._to(INACTIVE, now)
            return Evaluation(False, INACTIVE, f"{why}; released after {self.cfg.exit_delay:g} s",
                              failsafe=failsafe, known=known)
        return Evaluation(True, LEAVING, f"{why}; releasing after {self.cfg.exit_delay:g} s", due=due,
                          failsafe=failsafe, known=known)


def simulate(cfg: RadarConfig, presence: bool | None, metres: float | None, *, active: bool,
             failsafe: str = "hold") -> dict[str, Any]:
    """What the zone does with a test reading (no delays: the state the reading leads to). Never sends."""
    tracker = RadarTracker(cfg)
    now = datetime(2000, 1, 1)
    tracker.restore(active, now if active else None)
    reading = Distance(metres, None) if metres is not None else Distance(None, "unavailable")
    first = tracker.evaluate(presence, reading, now, failsafe=failsafe)
    later = tracker.evaluate(presence, reading, now + timedelta(seconds=cfg.entry_delay + cfg.exit_delay + 1),
                             failsafe=failsafe)
    zone = "inside the band" if metres is not None and cfg.inside(metres) else (
        "within the hysteresis margin" if metres is not None and cfg.within_margin(metres) else
        "outside the band" if metres is not None else "no distance")
    return {"now": {"active": first.active, "phase": first.phase, "reason": first.reason},
            "settled": {"active": later.active, "phase": later.phase, "reason": later.reason},
            "where": zone, "band": cfg.band(),
            "margin": [round(cfg.min_m - cfg.hysteresis, 3), round(cfg.max_m + cfg.hysteresis, 3)]}
