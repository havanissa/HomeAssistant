"""Ambient light levels (0.6.7, Phase 4.7): which ambient category a room is in now.

Pure logic. The runtime (``brightness_runtime.py``) feeds it the room's
daylight evidence from ``daylight_monitor.py``; nothing here subscribes to a
sensor, estimates daylight on its own or sends anything.

Categories
----------
A room has ordered categories, darkest first. Each has a **stable id**, a name
and the lux value it starts at (0.6.8, Phase 4.8): the first starts at 0 lx,
the last has no upper bound, and the ranges are contiguous. A value **equal to
a threshold belongs to the higher category**. The defaults are the 0.6.7
categories (``LEVELS``: No light from 0, Low light from 5, Bright from 20 lx).
Categories are global definitions with optional per-room definitions (a room
may have a completely different structure); ``AmbientSettings.thresholds`` is
the derived list of starts after the first. Renaming a category never changes
its id, so brightness matrices keyed by id keep their values. Adding,
removing and reassigning categories is planned and reviewed by
``ambient_levels.py``.

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
import re
from typing import Any

from .daylight import HIGH, MEDIUM, Estimate, fmt_lux

# The default categories, darkest first: (key, name). Thresholds: one between each pair.
LEVELS: tuple[tuple[str, str], ...] = (("none", "No light"), ("low", "Low light"), ("bright", "Bright"))
LEVEL_KEYS = tuple(k for k, _ in LEVELS)
LEVEL_NAMES = dict(LEVELS)
DEFAULT_THRESHOLDS = (5, 20)
THRESHOLD_MAX = 2000
# Custom categories (0.6.8): at least two; at most ``max_levels`` (global setting, default 12, limit 24).
MIN_LEVELS = 2
DEFAULT_MAX_LEVELS = 12
MAX_LEVELS_LIMIT = 24
LEVEL_NAME_MAX = 40
_ID = re.compile(r"^[a-z0-9_]{1,40}$")
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


def valid_thresholds(value: Any, count: int = len(LEVELS)) -> tuple[int, ...] | None:
    """Thresholds for every boundary of ``count`` levels, strictly increasing and positive; None when invalid."""
    if not isinstance(value, (list, tuple)) or len(value) != count - 1:
        return None
    out: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float, str)):
            return None
        try:
            raw = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(raw) or raw <= 0 or raw > THRESHOLD_MAX:
            return None
        number = int(round(raw))
        if number <= 0 or (out and number <= out[-1]):
            return None
        out.append(number)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class AmbientLevel:
    """One ambient category: a stable id (never changed by a rename), its name and where it starts (lx)."""

    id: str
    name: str
    start: int

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "start": self.start}


DEFAULT_LEVELS: tuple[AmbientLevel, ...] = tuple(
    AmbientLevel(k, n, 0 if i == 0 else DEFAULT_THRESHOLDS[i - 1]) for i, (k, n) in enumerate(LEVELS))


def levels_from_thresholds(thresholds: tuple[int, ...] | None) -> tuple[AmbientLevel, ...] | None:
    """The default categories (0.6.7 ids and names) with these boundaries."""
    if thresholds is None:
        return None
    return tuple(AmbientLevel(k, n, 0 if i == 0 else thresholds[i - 1]) for i, (k, n) in enumerate(LEVELS))


def level_error(value: Any, *, max_levels: int = DEFAULT_MAX_LEVELS) -> str | None:
    """Why a list of category definitions is invalid (None when it is valid).

    Each item: ``{"id", "name", "start"}``. The first starts at 0 lx; the starts increase strictly; ids are
    unique (lowercase letters, digits, ``_``); names are not empty and unique (case-insensitive).
    """
    if not isinstance(value, (list, tuple)):
        return "The categories must be a list"
    if len(value) < MIN_LEVELS:
        return f"At least {MIN_LEVELS} categories are needed"
    if len(value) > max_levels:
        return f"At most {max_levels} categories"
    ids: set[str] = set()
    names: set[str] = set()
    previous: float | None = None
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return f"Category {i + 1} is invalid"
        level_id = item.get("id")
        if not isinstance(level_id, str) or not _ID.match(level_id):
            return f"Category {i + 1} has an invalid id"
        if level_id in ids:
            return f"Duplicate category id {level_id}"
        ids.add(level_id)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return f"Category {i + 1} needs a name"
        if len(name.strip()) > LEVEL_NAME_MAX:
            return f"The name of category {i + 1} is too long"
        if name.strip().casefold() in names:
            return f"Duplicate category name {name.strip()}"
        names.add(name.strip().casefold())
        start = item.get("start")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            return f"{name.strip()}: the start must be a number"
        if not math.isfinite(float(start)):
            return f"{name.strip()}: the start must be a number"
        if start < 0:
            return f"{name.strip()}: the start cannot be negative"
        if float(start) != int(start):
            return f"{name.strip()}: the start must be a whole number of lux"
        if start > THRESHOLD_MAX:
            return f"{name.strip()}: at most {THRESHOLD_MAX} lx"
        if i == 0 and start != 0:
            return f"The first category ({name.strip()}) must start at 0 lx"
        if previous is not None and start <= previous:
            return f"{name.strip()} must start above {int(previous)} lx (each category starts higher than the one before)"
        previous = float(start)
    return None


def valid_levels(value: Any, *, max_levels: int = MAX_LEVELS_LIMIT) -> tuple[AmbientLevel, ...] | None:
    """Category definitions, or None when invalid (see ``level_error``)."""
    if level_error(value, max_levels=max_levels) is not None:
        return None
    return tuple(AmbientLevel(item["id"], item["name"].strip(), int(item["start"])) for item in value)


def _level(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.match(value) else None


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
    """Ambient levels and brightness tuning. Globally every value is set; in a room ``None`` inherits it.

    ``levels`` (0.6.8): the category definitions. A 0.6.7 ``thresholds`` list (three categories) is read as the
    default categories with those boundaries. ``max_levels`` is global only.
    """

    levels: tuple[AmbientLevel, ...] | None = None
    hysteresis: float | None = None
    stable_seconds: int | None = None
    fallback: AmbientFallback | None = None
    fallback_level: str | None = None
    # Brightness rules: fade of a brightness change (s), smallest change sent (%), and the shortest time between
    # two automatic brightness changes caused by the ambient level (s; 0 = no limit). Zones are never delayed.
    transition: float | None = None
    dead_band: int | None = None
    min_interval: int | None = None
    max_levels: int | None = None

    @property
    def thresholds(self) -> tuple[int, ...] | None:
        """The boundaries: where each category after the first starts."""
        return tuple(level.start for level in self.levels[1:]) if self.levels else None

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(level.id for level in self.levels or DEFAULT_LEVELS)

    @property
    def names(self) -> dict[str, str]:
        return {level.id: level.name for level in self.levels or DEFAULT_LEVELS}

    def name_of(self, key: str | None) -> str:
        return self.names.get(key or "", key or "")

    def fallback_index(self) -> int:
        """The fallback category; when it no longer exists, the second category (or the only one)."""
        keys = self.keys
        key = self.fallback_level or DEFAULT_FALLBACK_LEVEL
        return keys.index(key) if key in keys else min(1, len(keys) - 1)

    @classmethod
    def from_dict(cls, data: Any, *, defaults: bool = False) -> AmbientSettings:
        data = data if isinstance(data, dict) else {}
        levels = valid_levels(data.get("levels")) if data.get("levels") is not None else None
        if levels is None:
            levels = levels_from_thresholds(valid_thresholds(data.get("thresholds")))
        out = cls(
            levels=levels,
            hysteresis=_num(data.get("hysteresis"), 0, HYSTERESIS_MAX),
            stable_seconds=_int(data.get("stable_seconds"), 0, STABLE_MAX),
            fallback=_fallback(data.get("fallback")),
            fallback_level=_level(data.get("fallback_level")),
            transition=_num(data.get("transition"), 0, TRANSITION_LIMIT),
            dead_band=_int(data.get("dead_band"), 0, DEAD_BAND_MAX),
            min_interval=_int(data.get("min_interval"), 0, MIN_INTERVAL_MAX),
            max_levels=_int(data.get("max_levels"), MIN_LEVELS, MAX_LEVELS_LIMIT) if defaults else None,
        )
        return GLOBAL_AMBIENT.merged(out) if defaults else out

    def as_dict(self) -> dict[str, Any]:
        out = {
            "levels": [level.as_dict() for level in self.levels] if self.levels else None,
            # (Also written for 0.6.7, which reads three categories from it.)
            "thresholds": list(self.thresholds) if self.thresholds else None,
            "hysteresis": self.hysteresis, "stable_seconds": self.stable_seconds,
            "fallback": self.fallback.value if self.fallback else None, "fallback_level": self.fallback_level,
            "transition": self.transition, "dead_band": self.dead_band, "min_interval": self.min_interval,
        }
        if self.max_levels is not None:
            out["max_levels"] = self.max_levels
        return out

    def merged(self, room: AmbientSettings) -> AmbientSettings:
        """These (global) settings with a room's own values applied."""
        return replace(self, **{k: v for k in ("levels", *TUNING_KEYS) if (v := getattr(room, k)) is not None})

    def sources(self, room: AmbientSettings) -> dict[str, str]:
        out = {k: "room" if getattr(room, k) is not None else "global" for k in ("levels", *TUNING_KEYS)}
        out["thresholds"] = out["levels"]
        return out


GLOBAL_AMBIENT = AmbientSettings(
    levels=DEFAULT_LEVELS, hysteresis=DEFAULT_HYSTERESIS, stable_seconds=DEFAULT_STABLE_SECONDS,
    fallback=AmbientFallback.HOLD, fallback_level=DEFAULT_FALLBACK_LEVEL, transition=DEFAULT_TRANSITION,
    dead_band=DEFAULT_DEAD_BAND, min_interval=DEFAULT_MIN_INTERVAL, max_levels=DEFAULT_MAX_LEVELS,
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

    def persist(self, keys: tuple[str, ...] = LEVEL_KEYS) -> dict[str, Any]:
        """Kept by category id, so a restart with other categories never maps to a wrong one."""
        committed = keys[self.committed] if self.committed is not None and self.committed < len(keys) else None
        return {"committed": committed, "since": _iso(self.since), "reliable_at": _iso(self.reliable_at)}

    @classmethod
    def restore(cls, data: Any, keys: tuple[str, ...] = LEVEL_KEYS) -> AmbientTracker:
        data = data if isinstance(data, dict) else {}
        key = data.get("committed")
        committed = keys.index(key) if key in keys else None
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
        return self.settings.keys[self.level]

    @property
    def name(self) -> str:
        return self.settings.name_of(self.key)

    def key_of(self, index: int | None) -> str | None:
        keys = self.settings.keys
        return keys[index] if index is not None and 0 <= index < len(keys) else None

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
            return f"{self.name} ({lux}; {self.settings.name_of(self.key_of(self.candidate))} pending)"
        return f"{self.name} ({lux})"

    def as_dict(self) -> dict[str, Any]:
        s = self.settings
        levels = s.levels or DEFAULT_LEVELS
        return {
            "enabled": self.enabled, "level": self.key, "name": self.name, "index": self.level,
            "label": self.label(), "basis": self.basis, "evidence": self.evidence.kind,
            "evidence_level": self.key_of(self.evidence.level),
            "lux": None if self.evidence.lux is None else round(self.evidence.lux, 1), "lux_text": self.lux_text(),
            "estimated": self.evidence.kind == ESTIMATED, "upper_bound": self.evidence.kind == BOUND,
            "text": self.evidence.text, "since": _iso(self.since),
            "candidate": self.key_of(self.candidate),
            "candidate_due": _iso(self.candidate_due), "note": self.note,
            "measured_lux": None if self.measured_lux is None else round(self.measured_lux, 1),
            "sensor": self.sensor, "sensor_name": self.sensor_name, "contaminated": self.contaminated,
            "contamination": self.contamination, "confidence": self.confidence, "reliable_at": _iso(self.reliable_at),
            "thresholds": [lv.start for lv in levels[1:]], "hysteresis": s.hysteresis,
            "stable_seconds": s.stable_seconds, "fallback": s.fallback.value if s.fallback else None,
            "fallback_level": s.keys[s.fallback_index()],
            "levels": [{"key": lv.id, "name": lv.name, "from": lv.start,
                        "below": levels[i + 1].start if i < len(levels) - 1 else None}
                       for i, lv in enumerate(levels)],
        }


def classify(tracker: AmbientTracker, estimate: Estimate, settings: AmbientSettings, now: datetime,
             ) -> tuple[Evidence, int | None]:
    """One evaluation: the evidence and the previous committed level when the category changed."""
    thresholds = settings.thresholds or DEFAULT_THRESHOLDS
    if tracker.committed is not None and tracker.committed > len(thresholds):
        tracker.committed = None  # (fewer categories than when it was committed: classified again)
    ev = evidence(estimate, thresholds, settings.hysteresis or 0, tracker.committed)
    instant = ev.level
    reliable = instant is not None and ev.held is None
    if instant is None and settings.fallback == AmbientFallback.LEVEL:
        instant = settings.fallback_index()
        reliable = False
    old = tracker.update(instant, now, settings.stable_seconds or 0, reliable=reliable)
    return ev, old


def status(tracker: AmbientTracker, ev: Evidence, settings: AmbientSettings, *, enabled: bool) -> AmbientStatus:
    """The category the brightness rules use now (the committed one, or the fallback level)."""
    fallback = settings.fallback_index()
    keys = settings.keys

    def name(index: int) -> str:
        return settings.name_of(keys[index])

    note = None
    if tracker.committed is None:
        level, basis = fallback, "fallback"
        note = (f"Not enough trustworthy light evidence yet: using the fallback {name(fallback)}"
                + (f" ({ev.held})" if ev.held else ""))
    else:
        level = tracker.committed
        if ev.held:
            basis = "held"
            note = (f"Ambient sensor affected by room lighting. Holding {name(level)} "
                    "classification until trustworthy evidence is available.")
        elif ev.level is None:
            if settings.fallback == AmbientFallback.LEVEL and level == fallback:
                basis = "fallback"
                note = f"No trustworthy light evidence: using the fallback {name(level)}"
            else:
                basis = "held"
                note = (f"No trustworthy light evidence ({ev.text.split('—', 1)[-1].strip()}). Holding "
                        f"{name(level)} until trustworthy evidence is available.")
        else:
            basis = ev.kind
    return AmbientStatus(
        enabled=enabled, level=level, basis=basis, evidence=ev, settings=settings, since=tracker.since,
        candidate=tracker.candidate, candidate_due=tracker.due(settings.stable_seconds or 0), note=note,
        reliable_at=tracker.reliable_at,
    )
