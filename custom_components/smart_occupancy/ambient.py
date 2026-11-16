"""Ambient light levels (0.6.7, Phase 4.7): which ambient category a room is in now.

Pure logic. The runtime (``brightness_runtime.py``) feeds it the room's
daylight evidence from ``daylight_monitor.py``; nothing here subscribes to a
sensor, estimates daylight on its own or sends anything.

Categories
----------
A room has ordered categories (``LEVELS``: No light, Low light, Bright) and
one threshold between each pair: a value **equal to a threshold belongs to the
higher category** (defaults 5 and 20 lx: below 5 No light, 5 to below 20 Low
light, 20 and above Bright). Thresholds are global defaults with optional room
overrides. Only the number of thresholds depends on the number of levels, so
more categories can be added later without changing the classifier.

Evidence
--------
The value classified is the room's **ambient (natural) light** from the
daylight estimator, never a raw lamp-lit reading:

* ``measured`` - a trusted indoor reading (lights off and settled);
* ``estimated`` - outdoor light x the room's factor, a recent trusted reading
  scaled by the outdoor change, or the sun / outdoor darkness;
* ``bound`` - a reading taken while lamps are on (or settling): natural light
  is *at most* that value. A bound can only keep or lower the category, never
  raise it: when a lamp switched on by these rules lifts its own sensor to
  23 lx, that is not proof that the room became Bright (the feedback loop
  ``4 lx -> lamp on -> 23 lx -> Bright -> lamp off -> 4 lx`` cannot happen);
* ``uncertain`` - no usable evidence (unavailable, stale or invalid sensors and
  no estimate): the fallback policy applies.

Stability
---------
* Hysteresis (lx): moving **up** happens at the threshold itself; moving
  **down** needs the value to fall below ``threshold - hysteresis``.
* A new category is committed only after it held for ``stable_seconds``
  (uncertain evidence in between restarts the wait). The first evaluation
  commits at once (nothing to compare with).
* Fallback: ``hold`` (default) keeps the last committed category while the
  evidence is uncertain; before any category was known the fallback level is
  used. ``level`` uses the fallback level whenever the evidence is uncertain
  (after the same stability period).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
import math
from typing import Any

from .daylight import HIGH, MEDIUM, Estimate, fmt_lux

# The categories, darkest first: (key, name). Thresholds: one between each pair.
LEVELS: tuple[tuple[str, str], ...] = (("none", "No light"), ("low", "Low light"), ("bright", "Bright"))
LEVEL_KEYS = tuple(k for k, _ in LEVELS)
LEVEL_NAMES = dict(LEVELS)
DEFAULT_THRESHOLDS = (5, 20)
THRESHOLD_MAX = 2000
DEFAULT_HYSTERESIS = 2.0
HYSTERESIS_MAX = 200.0
DEFAULT_STABLE_SECONDS = 120
STABLE_MAX = 3600
DEFAULT_FALLBACK_LEVEL = "low"
# Brightness tuning (used by the brightness rules, configured with the levels).
DEFAULT_TRANSITION = 2.0
TRANSITION_LIMIT = 60.0
DEFAULT_DEAD_BAND = 3
DEAD_BAND_MAX = 25
DEFAULT_MIN_INTERVAL = 0
MIN_INTERVAL_MAX = 3600

MEASURED = "measured"
ESTIMATED = "estimated"
BOUND = "bound"
UNCERTAIN = "uncertain"


class AmbientFallback(StrEnum):
    HOLD = "hold"      # keep the last reliable category
    LEVEL = "level"    # use the fallback level while the evidence is uncertain


def _num(value: Any, low: float, high: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    number = min(max(number, low), high)
    return int(number) if number.is_integer() else number


def fmt(lux: float) -> str:
    """Whole lux, cut (not rounded) below 100: 19.6 lx is shown as 19 lx, like its category (below 20)."""
    if lux < 100:
        return str(int(math.floor(lux)))
    return fmt_lux(lux)


def _int(value: Any, low: int, high: int) -> int | None:
    number = _num(value, low, high)
    return None if number is None else int(round(number))


def valid_thresholds(value: Any) -> tuple[int, ...] | None:
    """Thresholds for every level boundary, strictly increasing and positive; None when invalid."""
    if not isinstance(value, (list, tuple)) or len(value) != len(LEVELS) - 1:
        return None
    out: list[int] = []
    for item in value:
        number = _int(item, 0, THRESHOLD_MAX)
        if number is None or number <= 0 or (out and number <= out[-1]):
            return None
        out.append(number)
    return tuple(out)


def _level(value: Any) -> str | None:
    return value if value in LEVEL_KEYS else None


def _fallback(value: Any) -> AmbientFallback | None:
    try:
        return AmbientFallback(value)
    except ValueError:
        return None


# ------------------------------------------------------------ configuration
TUNING_KEYS = ("hysteresis", "stable_seconds", "fallback", "fallback_level", "transition", "dead_band",
               "min_interval")


@dataclass(frozen=True, slots=True)
class AmbientSettings:
    """Ambient levels and brightness tuning. Globally every value is set; in a room ``None`` inherits it."""

    thresholds: tuple[int, ...] | None = None
    hysteresis: float | None = None
    stable_seconds: int | None = None
    fallback: AmbientFallback | None = None
    fallback_level: str | None = None
    # Brightness rules: fade of a brightness change (s), smallest change sent (%), and the shortest time between
    # two automatic brightness changes caused by the ambient level (s; 0 = no limit). Zones are never delayed.
    transition: float | None = None
    dead_band: int | None = None
    min_interval: int | None = None

    @classmethod
    def from_dict(cls, data: Any, *, defaults: bool = False) -> AmbientSettings:
        data = data if isinstance(data, dict) else {}
        out = cls(
            thresholds=valid_thresholds(data.get("thresholds")),
            hysteresis=_num(data.get("hysteresis"), 0, HYSTERESIS_MAX),
            stable_seconds=_int(data.get("stable_seconds"), 0, STABLE_MAX),
            fallback=_fallback(data.get("fallback")),
            fallback_level=_level(data.get("fallback_level")),
            transition=_num(data.get("transition"), 0, TRANSITION_LIMIT),
            dead_band=_int(data.get("dead_band"), 0, DEAD_BAND_MAX),
            min_interval=_int(data.get("min_interval"), 0, MIN_INTERVAL_MAX),
        )
        return GLOBAL_AMBIENT.merged(out) if defaults else out

    def as_dict(self) -> dict[str, Any]:
        return {
            "thresholds": list(self.thresholds) if self.thresholds else None,
            "hysteresis": self.hysteresis, "stable_seconds": self.stable_seconds,
            "fallback": self.fallback.value if self.fallback else None, "fallback_level": self.fallback_level,
            "transition": self.transition, "dead_band": self.dead_band, "min_interval": self.min_interval,
        }

    def merged(self, room: AmbientSettings) -> AmbientSettings:
        """These (global) settings with a room's own values applied."""
        return replace(self, **{k: v for k in ("thresholds", *TUNING_KEYS) if (v := getattr(room, k)) is not None})

    def sources(self, room: AmbientSettings) -> dict[str, str]:
        return {k: "room" if getattr(room, k) is not None else "global" for k in ("thresholds", *TUNING_KEYS)}


GLOBAL_AMBIENT = AmbientSettings(
    thresholds=DEFAULT_THRESHOLDS, hysteresis=DEFAULT_HYSTERESIS, stable_seconds=DEFAULT_STABLE_SECONDS,
    fallback=AmbientFallback.HOLD, fallback_level=DEFAULT_FALLBACK_LEVEL, transition=DEFAULT_TRANSITION,
    dead_band=DEFAULT_DEAD_BAND, min_interval=DEFAULT_MIN_INTERVAL,
)


@dataclass(frozen=True, slots=True)
class RoomAmbient:
    """A room's ambient brightness control: on or off, and its own values (``None``: global default).

    The indoor sensors are the room's daylight sensors (``RoomDaylight.sensors``): one sensor list per room.
    """

    enabled: bool = False
    settings: AmbientSettings = field(default_factory=AmbientSettings)

    @classmethod
    def from_dict(cls, data: Any) -> RoomAmbient:
        data = data if isinstance(data, dict) else {}
        return cls(enabled=data.get("enabled") is True, settings=AmbientSettings.from_dict(data))

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, **self.settings.as_dict()}


# ------------------------------------------------------------- classification
def raw_level(lux: float, thresholds: tuple[int, ...]) -> int:
    """The category of a value without history: a value equal to a threshold is in the higher category."""
    return sum(1 for t in thresholds if lux >= t)


def hysteretic_level(lux: float, thresholds: tuple[int, ...], hysteresis: float, previous: int | None) -> int:
    """Up at the threshold itself; down only below ``threshold - hysteresis``."""
    raw = raw_level(lux, thresholds)
    if previous is None or raw >= previous:
        return raw
    down = sum(1 for t in thresholds if lux >= t - hysteresis)
    return min(previous, max(raw, down))


@dataclass(slots=True, frozen=True)
class Evidence:
    """What the daylight evidence says about the category now."""

    level: int | None          # None: uncertain
    kind: str                  # measured | estimated | bound | uncertain
    lux: float | None
    text: str
    held: str | None = None    # why the category is held instead of following the evidence


def evidence(estimate: Estimate, thresholds: tuple[int, ...], hysteresis: float, committed: int | None) -> Evidence:
    """The category the room's daylight estimate supports (see the module docstring)."""
    lux = estimate.lux
    if lux is None or estimate.confidence not in (HIGH, MEDIUM):
        return Evidence(None, UNCERTAIN, None, estimate.text)
    level = hysteretic_level(lux, thresholds, hysteresis, committed)
    if estimate.upper_bound:
        # Natural light is at most ``lux``: a lower category is proven, a higher one is not.
        if committed is None:
            if raw_level(lux, thresholds) == 0:
                return Evidence(0, BOUND, lux, estimate.text)
            return Evidence(None, BOUND, lux, estimate.text,
                            held="the indoor sensor is affected by room lighting, so a higher category is not proven")
        if level > committed:
            return Evidence(committed, BOUND, lux, estimate.text,
                            held="the indoor sensor is affected by room lighting")
        return Evidence(level, BOUND, lux, estimate.text)
    return Evidence(level, MEASURED if estimate.measured else ESTIMATED, lux, estimate.text)


@dataclass(slots=True)
class AmbientTracker:
    """The committed category changes only after the new one held for the stability period."""

    committed: int | None = None
    since: datetime | None = None
    candidate: int | None = None
    candidate_since: datetime | None = None
    # When the committed category was last supported by usable evidence.
    reliable_at: datetime | None = None

    def update(self, instant: int | None, now: datetime, stable_seconds: int, *, reliable: bool) -> int | None:
        """Feed one evaluation; returns the previous committed level when it changed (else None).

        ``instant`` None (uncertain, hold policy): nothing changes and a pending candidate is dropped.
        """
        if reliable and instant is not None and instant == self.committed:
            self.reliable_at = now
        if instant is None:
            self.candidate = self.candidate_since = None
            return None
        if self.committed is None:
            self.committed, self.since = instant, now
            if reliable:
                self.reliable_at = now
            self.candidate = self.candidate_since = None
            return None
        if instant == self.committed:
            self.candidate = self.candidate_since = None
            return None
        if instant != self.candidate:
            self.candidate, self.candidate_since = instant, now
        if self.candidate_since is not None and now - self.candidate_since >= timedelta(seconds=stable_seconds):
            old = self.committed
            self.committed, self.since = instant, now
            if reliable:
                self.reliable_at = now
            self.candidate = self.candidate_since = None
            return old
        return None

    def due(self, stable_seconds: int) -> datetime | None:
        if self.candidate is None or self.candidate_since is None:
            return None
        return self.candidate_since + timedelta(seconds=stable_seconds)

    def persist(self) -> dict[str, Any]:
        return {"committed": LEVEL_KEYS[self.committed] if self.committed is not None else None,
                "since": _iso(self.since), "reliable_at": _iso(self.reliable_at)}

    @classmethod
    def restore(cls, data: Any) -> AmbientTracker:
        data = data if isinstance(data, dict) else {}
        key = data.get("committed")
        committed = LEVEL_KEYS.index(key) if key in LEVEL_KEYS else None
        return cls(committed=committed, since=_parse(data.get("since")), reliable_at=_parse(data.get("reliable_at")))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ------------------------------------------------------------------- status
@dataclass(slots=True)
class AmbientStatus:
    """The ambient category of one room now, how it is known, and what is uncertain."""

    enabled: bool
    level: int                         # the category used by the brightness rules
    basis: str                         # measured | estimated | bound | held | fallback
    evidence: Evidence
    settings: AmbientSettings
    since: datetime | None = None
    candidate: int | None = None
    candidate_due: datetime | None = None
    note: str | None = None            # shown and logged: why the category is held
    measured_lux: float | None = None  # the raw reading of the chosen indoor sensor (may include lamplight)
    sensor: str | None = None
    sensor_name: str | None = None
    contaminated: bool = False
    contamination: str | None = None
    confidence: str | None = None
    reliable_at: datetime | None = None

    @property
    def key(self) -> str:
        return LEVEL_KEYS[self.level]

    @property
    def name(self) -> str:
        return LEVEL_NAMES[self.key]

    def lux_text(self) -> str:
        """"12 lx", "≈ 24 lx", "at most 23 lx" or "" (no value)."""
        lux = self.evidence.lux
        if lux is None:
            return ""
        if self.evidence.kind == BOUND:
            return f"at most {fmt(lux)} lx"
        return f"{'≈ ' if self.evidence.kind == ESTIMATED else ''}{fmt(lux)} lx"

    def label(self) -> str:
        """"Low light (12 lx)" - the category with the value it is based on."""
        lux = self.lux_text()
        if self.basis in ("held", "fallback") or not lux:
            word = {"held": "held", "fallback": "fallback"}.get(self.basis)
            return f"{self.name} ({word})" if word else self.name
        if self.candidate is not None and self.candidate != self.level:
            return f"{self.name} ({lux}; {LEVEL_NAMES[LEVEL_KEYS[self.candidate]]} pending)"
        return f"{self.name} ({lux})"

    def as_dict(self) -> dict[str, Any]:
        s = self.settings
        return {
            "enabled": self.enabled, "level": self.key, "name": self.name, "index": self.level,
            "label": self.label(), "basis": self.basis, "evidence": self.evidence.kind,
            "evidence_level": LEVEL_KEYS[self.evidence.level] if self.evidence.level is not None else None,
            "lux": None if self.evidence.lux is None else round(self.evidence.lux, 1), "lux_text": self.lux_text(),
            "estimated": self.evidence.kind == ESTIMATED, "upper_bound": self.evidence.kind == BOUND,
            "text": self.evidence.text, "since": _iso(self.since),
            "candidate": LEVEL_KEYS[self.candidate] if self.candidate is not None else None,
            "candidate_due": _iso(self.candidate_due), "note": self.note,
            "measured_lux": None if self.measured_lux is None else round(self.measured_lux, 1),
            "sensor": self.sensor, "sensor_name": self.sensor_name, "contaminated": self.contaminated,
            "contamination": self.contamination, "confidence": self.confidence, "reliable_at": _iso(self.reliable_at),
            "thresholds": list(s.thresholds or DEFAULT_THRESHOLDS), "hysteresis": s.hysteresis,
            "stable_seconds": s.stable_seconds, "fallback": s.fallback.value if s.fallback else None,
            "fallback_level": s.fallback_level,
            "levels": [{"key": k, "name": n, "from": 0 if i == 0 else (s.thresholds or DEFAULT_THRESHOLDS)[i - 1],
                        "below": (s.thresholds or DEFAULT_THRESHOLDS)[i] if i < len(LEVELS) - 1 else None}
                       for i, (k, n) in enumerate(LEVELS)],
        }


def classify(tracker: AmbientTracker, estimate: Estimate, settings: AmbientSettings, now: datetime,
             ) -> tuple[Evidence, int | None]:
    """One evaluation: the evidence and the previous committed level when the category changed."""
    thresholds = settings.thresholds or DEFAULT_THRESHOLDS
    ev = evidence(estimate, thresholds, settings.hysteresis or 0, tracker.committed)
    instant = ev.level
    reliable = instant is not None and ev.held is None
    if instant is None and settings.fallback == AmbientFallback.LEVEL:
        instant = LEVEL_KEYS.index(settings.fallback_level or DEFAULT_FALLBACK_LEVEL)
        reliable = False
    old = tracker.update(instant, now, settings.stable_seconds or 0, reliable=reliable)
    return ev, old


def status(tracker: AmbientTracker, ev: Evidence, settings: AmbientSettings, *, enabled: bool) -> AmbientStatus:
    """The category the brightness rules use now (the committed one, or the fallback level)."""
    fallback = LEVEL_KEYS.index(settings.fallback_level or DEFAULT_FALLBACK_LEVEL)
    note = None
    if tracker.committed is None:
        level, basis = fallback, "fallback"
        note = (f"Not enough trustworthy light evidence yet: using the fallback {LEVEL_NAMES[LEVEL_KEYS[fallback]]}"
                + (f" ({ev.held})" if ev.held else ""))
    else:
        level = tracker.committed
        if ev.held:
            basis = "held"
            note = (f"Ambient sensor affected by room lighting. Holding {LEVEL_NAMES[LEVEL_KEYS[level]]} "
                    "classification until trustworthy evidence is available.")
        elif ev.level is None:
            if settings.fallback == AmbientFallback.LEVEL and level == fallback:
                basis = "fallback"
                note = f"No trustworthy light evidence: using the fallback {LEVEL_NAMES[LEVEL_KEYS[level]]}"
            else:
                basis = "held"
                note = (f"No trustworthy light evidence ({ev.text.split('—', 1)[-1].strip()}). Holding "
                        f"{LEVEL_NAMES[LEVEL_KEYS[level]]} until trustworthy evidence is available.")
        else:
            basis = ev.kind
    return AmbientStatus(
        enabled=enabled, level=level, basis=basis, evidence=ev, settings=settings, since=tracker.since,
        candidate=tracker.candidate, candidate_due=tracker.due(settings.stable_seconds or 0), note=note,
        reliable_at=tracker.reliable_at,
    )
