"""Daylight: is artificial light needed in a room? (Phase 0.3.3)

Pure logic. The runtime (``daylight_monitor.py``) feeds it Home Assistant
states; nothing here subscribes, schedules or sends anything.

The daylight decision only says **whether** occupancy may switch lights on.
The mood profile still says what the lights look like. No brightness or
colour is derived from lux.

Evidence, strongest first
-------------------------
1. **The sun is well below the horizon** (after civil dusk, < -6 deg): there is
   no natural light, whatever a sensor reads (a bright reading then comes from
   a lamp).
2. **Indoor sensor, lights off**: a reading of the preferred usable sensor
   that was *taken* (see ``ReportMode``) at least ``settle_seconds`` after the
   last influencing light turned off, while none is on. This is the only
   reading accepted as a measurement of natural light ("trusted sample").
3. **Indoor sensor, lights on, reading below the turn-on threshold**: lamps
   only add light, so natural light is at most the reading: it is dark.
   A bright reading with lights on is never used (it may be the lamp itself).
4. **Outdoor darkness** from the optional global outdoor sensor (smoothed).
5. **The most recent indoor evidence**, if recent: the last trusted sample
   (scaled by the change of the outdoor light since then when both are known),
   or a later "at most" reading from (3) - lamps switched on afterwards must not
   bring back an older, brighter sample.
6. **Outdoor light x the room's daylight factor** (learned from trusted
   indoor/outdoor pairs, or set by the user). Outdoor brightness alone never
   makes a room bright: rooms receive very different shares of it.
7. **The sun has set** (twilight): low natural light, medium confidence.

Otherwise daylight is uncertain and the room's fallback applies.

``unknown`` / ``unavailable`` / non-numeric readings are never 0 lx; they are
simply not evidence. Estimates are labelled as estimates; only (2) and (3) are
measured values.

Extension point: room covers (blinds, curtains) can later scale estimates
5-7 (``estimate_daylight(..., shading=...)``); they are not read in 0.3.3.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import math
from statistics import median
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from .const import (
    BOUND_MAX_AGE_MINUTES,
    CALIBRATION_MAX_SAMPLES,
    CALIBRATION_MIN_INTERVAL_SECONDS,
    CALIBRATION_MIN_OUTDOOR_LUX,
    CALIBRATION_MIN_SAMPLES,
    DAYLIGHT_FACTOR_MAX,
    DAYLIGHT_FACTOR_MIN,
    DEFAULT_DAYLIGHT_STABLE_SECONDS,
    DEFAULT_OFF_ABOVE_LUX,
    DEFAULT_ON_BELOW_LUX,
    DEFAULT_OUTDOOR_DARK_LUX,
    DEFAULT_OUTDOOR_WINDOW_SECONDS,
    DEFAULT_SAMPLE_MAX_AGE_MINUTES,
    DEFAULT_SETTLE_SECONDS,
    LUX_MAX_THRESHOLD,
    LUX_MIN,
    LUX_VALID_MAX,
    OUTDOOR_WINDOW_MAX_SECONDS,
    SAMPLE_MAX_AGE_MAX_MINUTES,
    SENSOR_MAX_AGE_MAX_MINUTES,
    SETTLE_SECONDS_MAX,
    STABLE_SECONDS_MAX,
    SUN_DARK_ELEVATION,
    SUN_HORIZON_ELEVATION,
    AutoOn,
    Darkness,
    Reliability,
    ReportMode,
    UncertainPolicy,
)

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

DARK = Darkness.DARK
BRIGHT = Darkness.BRIGHT
UNCERTAIN = Darkness.UNCERTAIN

# Plan reason codes of the daylight gate.
GATE_NOT_NEEDED = "daylight"
GATE_UNCERTAIN = "daylight_uncertain"
GATE_NEVER = "auto_on_never"
GATE_CODES = frozenset({GATE_NOT_NEEDED, GATE_UNCERTAIN, GATE_NEVER})


# ------------------------------------------------------------------ helpers
def _enum(enum_cls: type, value: Any, default: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        return default


def _int(value: Any, default: int, low: int, high: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(min(max(float(value), low), high))
    except (TypeError, ValueError):
        return default


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(v.strip() for v in value if isinstance(v, str) and "." in v))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def round_lux(value: float | None) -> int | None:
    """Coarse lux (1 lx below 10, 5 lx below 100, then 2 significant digits).

    Shown and published values are rounded: illuminance sensors are not more
    precise than that, and small fluctuations then do not rewrite entities or
    refresh dashboards.
    """
    if value is None:
        return None
    if value < 10:
        return int(round(value))
    if value < 100:
        return int(round(value / 5) * 5)
    if value < 1000:
        return int(round(value, -1))
    return int(round(value, -2))


def fmt_lux(value: float) -> str:
    return str(round_lux(value))


# ------------------------------------------------------------ configuration
@dataclass(frozen=True, slots=True)
class IndoorSensor:
    """An indoor illuminance sensor of a room, with its freshness and reliability preferences."""

    entity_id: str
    # Readings without a report for longer than this are not used (minutes, 0 = no limit).
    max_age: int = 0
    report_mode: ReportMode = ReportMode.CHANGES
    reliability: Reliability = Reliability.NORMAL

    @classmethod
    def from_dict(cls, data: Any) -> IndoorSensor | None:
        if isinstance(data, str):
            data = {"entity_id": data}
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str) or "." not in data["entity_id"]:
            return None
        return cls(
            entity_id=data["entity_id"].strip(),
            max_age=_int(data.get("max_age"), 0, 0, SENSOR_MAX_AGE_MAX_MINUTES),
            report_mode=_enum(ReportMode, data.get("report_mode"), ReportMode.CHANGES),
            reliability=_enum(Reliability, data.get("reliability"), Reliability.NORMAL),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "max_age": self.max_age, "report_mode": self.report_mode.value,
                "reliability": self.reliability.value}


@dataclass(frozen=True, slots=True)
class RoomDaylight:
    """Automatic switch-on settings of one room."""

    auto_on: AutoOn = AutoOn.DARK
    # A trigger sensor (PIR) becoming active switches on at once, without waiting for confirmation.
    on_first_motion: bool = True
    # Preferred sensor first, then fallbacks.
    sensors: tuple[IndoorSensor, ...] = ()
    on_below: int = DEFAULT_ON_BELOW_LUX
    off_above: int = DEFAULT_OFF_ABOVE_LUX
    stable_seconds: int = DEFAULT_DAYLIGHT_STABLE_SECONDS
    settle_seconds: int = DEFAULT_SETTLE_SECONDS
    uncertain: UncertainPolicy = UncertainPolicy.TURN_ON
    # Extra lights/switches that light the sensor (window lamps, adjacent open-plan lights).
    influencing: tuple[str, ...] = ()
    # Every light in the room's area (and every managed light) counts as influencing.
    area_lights: bool = True
    # Indoor/outdoor ratio set by the user; None = learned.
    factor: float | None = None
    # How long the last trusted indoor sample stands in for a measurement (minutes).
    sample_max_age: int = DEFAULT_SAMPLE_MAX_AGE_MINUTES

    @classmethod
    def from_dict(cls, data: Any) -> RoomDaylight:
        data = data if isinstance(data, dict) else {}
        sensors = tuple(s for s in (IndoorSensor.from_dict(x) for x in data.get("sensors") or []) if s)
        unique = tuple({s.entity_id: s for s in reversed(sensors)}.values())[::-1]
        on_below = _int(data.get("on_below"), DEFAULT_ON_BELOW_LUX, LUX_MIN, LUX_MAX_THRESHOLD)
        off_above = _int(data.get("off_above"), DEFAULT_OFF_ABOVE_LUX, LUX_MIN, LUX_MAX_THRESHOLD * 2)
        if off_above <= on_below:
            off_above = on_below * 2
        factor = _float(data.get("factor"))
        return cls(
            auto_on=_enum(AutoOn, data.get("auto_on"), AutoOn.DARK),
            on_first_motion=bool(data.get("on_first_motion", True)),
            sensors=unique,
            on_below=on_below,
            off_above=off_above,
            stable_seconds=_int(data.get("stable_seconds"), DEFAULT_DAYLIGHT_STABLE_SECONDS, 0, STABLE_SECONDS_MAX),
            settle_seconds=_int(data.get("settle_seconds"), DEFAULT_SETTLE_SECONDS, 0, SETTLE_SECONDS_MAX),
            uncertain=_enum(UncertainPolicy, data.get("uncertain"), UncertainPolicy.TURN_ON),
            influencing=_ids(data.get("influencing")),
            area_lights=bool(data.get("area_lights", True)),
            factor=None if factor is None else min(max(factor, DAYLIGHT_FACTOR_MIN), DAYLIGHT_FACTOR_MAX),
            sample_max_age=_int(data.get("sample_max_age"), DEFAULT_SAMPLE_MAX_AGE_MINUTES, 1,
                                SAMPLE_MAX_AGE_MAX_MINUTES),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_on": self.auto_on.value,
            "on_first_motion": self.on_first_motion,
            "sensors": [s.as_dict() for s in self.sensors],
            "on_below": self.on_below,
            "off_above": self.off_above,
            "stable_seconds": self.stable_seconds,
            "settle_seconds": self.settle_seconds,
            "uncertain": self.uncertain.value,
            "influencing": list(self.influencing),
            "area_lights": self.area_lights,
            "factor": self.factor,
            "sample_max_age": self.sample_max_age,
        }

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(s.entity_id for s in self.sensors)


@dataclass(frozen=True, slots=True)
class DaylightConfig:
    """Global daylight settings (shared by every room)."""

    outdoor_entity: str | None = None
    # Smoothed outdoor illuminance at or below this means: dark outside.
    outdoor_dark_lux: int = DEFAULT_OUTDOOR_DARK_LUX
    # Outdoor readings are smoothed over this window (time-weighted median), so spikes are ignored.
    outdoor_window: int = DEFAULT_OUTDOOR_WINDOW_SECONDS

    @classmethod
    def from_dict(cls, data: Any) -> DaylightConfig:
        data = data if isinstance(data, dict) else {}
        entity = data.get("outdoor_entity")
        return cls(
            outdoor_entity=entity.strip() if isinstance(entity, str) and "." in entity else None,
            outdoor_dark_lux=_int(data.get("outdoor_dark_lux"), DEFAULT_OUTDOOR_DARK_LUX, 0, LUX_MAX_THRESHOLD),
            outdoor_window=_int(data.get("outdoor_window"), DEFAULT_OUTDOOR_WINDOW_SECONDS, 0,
                                OUTDOOR_WINDOW_MAX_SECONDS),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"outdoor_entity": self.outdoor_entity, "outdoor_dark_lux": self.outdoor_dark_lux,
                "outdoor_window": self.outdoor_window}


# ----------------------------------------------------------------- readings
OK = "ok"


@dataclass(slots=True, frozen=True)
class SensorReading:
    """The current reading of one indoor sensor."""

    config: IndoorSensor
    preference: int              # 0 = preferred, 1 = first fallback, ...
    name: str | None
    value: float | None          # lux; None when there is no usable number
    status: str                  # ok | missing | unavailable | unknown | invalid | stale
    detail: str
    sample_at: datetime | None   # when this value was measured (per report mode)
    reported_at: datetime | None  # last report (also with an unchanged value)

    @property
    def entity_id(self) -> str:
        return self.config.entity_id

    @property
    def usable(self) -> bool:
        return self.status == OK and self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id, "name": self.name, "preference": self.preference,
            "value": None if self.value is None else round(self.value, 1), "status": self.status,
            "detail": self.detail, "sample_at": _iso(self.sample_at), "reported_at": _iso(self.reported_at),
            "report_mode": self.config.report_mode.value, "reliability": self.config.reliability.value,
            "max_age": self.config.max_age,
        }


def parse_lux(state: State | None) -> tuple[float | None, str, str]:
    """(lux, status, detail). ``unknown`` / ``unavailable`` are never 0 lx."""
    if state is None:
        return None, "missing", "does not exist"
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None, state.state, f"is {state.state}"
    value = _float(state.state)
    if value is None:
        return None, "invalid", f"'{state.state}' is not a number"
    if value < 0 or value > LUX_VALID_MAX:
        return None, "invalid", f"{value:g} lx is not a valid illuminance"
    return value, OK, f"{fmt_lux(value)} lx"


def read_sensor(state: State | None, config: IndoorSensor, preference: int, now: datetime) -> SensorReading:
    name = state.attributes.get("friendly_name") if state else None
    value, status, detail = parse_lux(state)
    if state is None:
        return SensorReading(config, preference, name, None, status, detail, None, None)
    reported = getattr(state, "last_reported", None) or state.last_updated
    sample_at = reported if config.report_mode == ReportMode.REPORTS else state.last_changed
    if status == OK and config.max_age and now - reported > timedelta(minutes=config.max_age):
        minutes = int((now - reported).total_seconds() // 60)
        status, detail = "stale", f"no report for {minutes} min (limit {config.max_age} min)"
    return SensorReading(config, preference, name, value, status, detail, sample_at, reported)


@dataclass(slots=True, frozen=True)
class LightEvidence:
    """Influencing lights now: which are on, which are unknown, and when the last one turned off."""

    on: tuple[str, ...] = ()
    unknown: tuple[str, ...] = ()
    off_since: datetime | None = None
    names: Mapping[str, str] = field(default_factory=dict)

    @property
    def lit(self) -> bool:
        return bool(self.on)


def light_evidence(states: Iterable[tuple[str, State | None]]) -> LightEvidence:
    on: list[str] = []
    unknown: list[str] = []
    names: dict[str, str] = {}
    off_since: datetime | None = None
    for entity_id, state in states:
        if state is None:
            continue  # not an entity (any more): no light
        names[entity_id] = state.attributes.get("friendly_name") or entity_id
        if state.state == "on":
            on.append(entity_id)
        elif state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            unknown.append(entity_id)
        elif off_since is None or state.last_changed > off_since:
            # last_changed of an off light is when it turned off (or came back as off):
            # after a restart that is the start time, which is the safe assumption.
            off_since = state.last_changed
    return LightEvidence(tuple(on), tuple(unknown), off_since, names)


@dataclass(slots=True, frozen=True)
class Contamination:
    """Can indoor readings be taken as natural light right now?"""

    state: str                  # clear | lit | settling | possible
    text: str | None = None
    until: datetime | None = None  # end of the settling period

    @property
    def contaminated(self) -> bool:
        return self.state in ("lit", "settling")

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "contaminated": self.contaminated, "text": self.text, "until": _iso(self.until)}


def _names(ids: Iterable[str], names: Mapping[str, str], limit: int = 3) -> str:
    items = [names.get(e, e) for e in ids]
    return ", ".join(items[:limit]) + (f" +{len(items) - limit}" if len(items) > limit else "")


def contamination(lights: LightEvidence, settle_seconds: int, now: datetime) -> Contamination:
    if lights.lit:
        return Contamination("lit", f"Lights on: {_names(lights.on, lights.names)}")
    if lights.off_since is not None and settle_seconds:
        until = lights.off_since + timedelta(seconds=settle_seconds)
        if now < until:
            return Contamination("settling", "Settling after the lights turned off", until)
    if lights.unknown:
        return Contamination("possible", f"{_names(lights.unknown, lights.names)} unavailable: "
                             "its light cannot be ruled out")
    return Contamination("clear")


# ------------------------------------------------------------------ outdoor
class OutdoorFilter:
    """Time-weighted median of the outdoor illuminance over a window: a short spike has no effect."""

    def __init__(self, max_samples: int = 240) -> None:
        self._samples: deque[tuple[datetime, float]] = deque(maxlen=max_samples)

    def clear(self) -> None:
        self._samples.clear()

    def add(self, at: datetime, value: float | None) -> None:
        """Record a value (None = no valid value from ``at`` on)."""
        if self._samples and at < self._samples[-1][0]:
            return  # out of order
        self._samples.append((at, math.nan if value is None else value))

    def prune(self, now: datetime, window: int) -> None:
        start = now - timedelta(seconds=window)
        # Keep the last sample before the window: it is the value at the window's start.
        while len(self._samples) >= 2 and self._samples[1][0] <= start:
            self._samples.popleft()

    def value(self, now: datetime, window: int) -> float | None:
        if not self._samples:
            return None
        last = self._samples[-1][1]
        if math.isnan(last):
            return None
        if window <= 0:
            return last
        self.prune(now, window)
        start = now - timedelta(seconds=window)
        weights: dict[float, float] = {}
        items = list(self._samples)
        for i, (at, value) in enumerate(items):
            begin = max(at, start)
            end = items[i + 1][0] if i + 1 < len(items) else now
            seconds = (end - begin).total_seconds()
            if seconds <= 0 or math.isnan(value):
                continue
            weights[value] = weights.get(value, 0.0) + seconds
        if not weights:
            return last
        half = sum(weights.values()) / 2
        total = 0.0
        for value in sorted(weights):
            total += weights[value]
            if total >= half:
                return value
        return last

    def __len__(self) -> int:
        return len(self._samples)


@dataclass(slots=True, frozen=True)
class OutdoorReading:
    entity_id: str | None
    value: float | None        # smoothed
    raw: float | None
    status: str                # ok | not_configured | missing | unavailable | unknown | invalid
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "value": None if self.value is None else round(self.value),
                "raw": None if self.raw is None else round(self.raw), "status": self.status, "detail": self.detail}


NO_OUTDOOR = OutdoorReading(None, None, None, "not_configured", "No outdoor light sensor")


@dataclass(slots=True, frozen=True)
class SunInfo:
    elevation: float | None = None
    next_sunrise: datetime | None = None
    next_sunset: datetime | None = None

    @property
    def dark(self) -> bool:
        return self.elevation is not None and self.elevation <= SUN_DARK_ELEVATION

    @property
    def down(self) -> bool:
        return self.elevation is not None and self.elevation < SUN_HORIZON_ELEVATION

    def as_dict(self) -> dict[str, Any]:
        return {"elevation": None if self.elevation is None else round(self.elevation, 1), "dark": self.dark,
                "down": self.down, "next_sunrise": _iso(self.next_sunrise), "next_sunset": _iso(self.next_sunset)}


# -------------------------------------------------------------- calibration
@dataclass(slots=True, frozen=True)
class TrustedSample:
    """An indoor reading accepted as natural light (lights off, settled)."""

    entity_id: str
    lux: float
    at: datetime
    outdoor: float | None = None

    def persist(self) -> list[Any]:
        return [self.entity_id, self.lux, self.at.isoformat(), self.outdoor]

    @classmethod
    def restore(cls, data: Any) -> TrustedSample | None:
        if not isinstance(data, list) or len(data) != 4 or not isinstance(data[0], str):
            return None
        lux, outdoor = _float(data[1]), _float(data[3])
        try:
            at = datetime.fromisoformat(str(data[2]))
        except ValueError:
            return None
        return cls(data[0], lux, at, outdoor) if lux is not None else None

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "lux": round(self.lux, 1), "at": _iso(self.at),
                "outdoor": None if self.outdoor is None else round(self.outdoor)}


@dataclass(slots=True)
class Calibration:
    """Indoor/outdoor ratios of trusted samples; the median is the learned daylight factor."""

    ratios: list[tuple[datetime, float]] = field(default_factory=list)

    def add(self, sample: TrustedSample) -> bool:
        if sample.outdoor is None or sample.outdoor < CALIBRATION_MIN_OUTDOOR_LUX:
            return False
        if self.ratios and sample.at - self.ratios[-1][0] < timedelta(seconds=CALIBRATION_MIN_INTERVAL_SECONDS):
            return False
        ratio = min(max(sample.lux / sample.outdoor, 0.0), DAYLIGHT_FACTOR_MAX)
        self.ratios.append((sample.at, ratio))
        del self.ratios[:-CALIBRATION_MAX_SAMPLES]
        return True

    @property
    def factor(self) -> float | None:
        if len(self.ratios) < CALIBRATION_MIN_SAMPLES:
            return None
        return max(median(r for _, r in self.ratios), DAYLIGHT_FACTOR_MIN)

    def persist(self) -> list[list[Any]]:
        return [[at.isoformat(), r] for at, r in self.ratios]

    @classmethod
    def restore(cls, data: Any) -> Calibration:
        out = cls()
        for item in data if isinstance(data, list) else []:
            try:
                out.ratios.append((datetime.fromisoformat(str(item[0])), float(item[1])))
            except (TypeError, ValueError, IndexError):
                continue
        del out.ratios[:-CALIBRATION_MAX_SAMPLES]
        return out


# ---------------------------------------------------------------- estimate
@dataclass(slots=True, frozen=True)
class Estimate:
    """Natural light in a room, and how it is known."""

    lux: float | None
    confidence: str | None     # high | medium | low | None (unknown)
    basis: str
    text: str                  # "Natural light: 35 lx — measured indoors, lights off"
    measured: bool = False     # a direct indoor measurement (not an estimate)
    upper_bound: bool = False  # natural light is at most ``lux``
    sensor: str | None = None
    notes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {"lux": None if self.lux is None else round(self.lux, 1), "confidence": self.confidence,
                "basis": self.basis, "text": self.text, "measured": self.measured, "upper_bound": self.upper_bound,
                "estimated": not self.measured and self.lux is not None, "sensor": self.sensor,
                "notes": list(self.notes)}


@dataclass(slots=True, frozen=True)
class EstimateResult:
    estimate: Estimate
    chosen: SensorReading | None
    contamination: Contamination
    accepted: TrustedSample | None  # a new trusted sample (lights off, settled)
    bound: TrustedSample | None = None  # a new "at most" reading (lights on or settling, still dark)


def _minutes(delta: timedelta) -> int:
    return max(0, int(delta.total_seconds() // 60))


def estimate_daylight(
    *,
    room: RoomDaylight,
    glob: DaylightConfig,
    readings: list[SensorReading],
    lights: LightEvidence,
    outdoor: OutdoorReading,
    sun: SunInfo,
    trusted: TrustedSample | None,
    learned_factor: float | None,
    now: datetime,
    bound: TrustedSample | None = None,
    shading: float | None = None,  # extension point (blinds/curtains), unused in 0.3.3
) -> EstimateResult:
    cont = contamination(lights, room.settle_seconds, now)
    chosen = next((r for r in readings if r.usable), None)
    settled_from = lights.off_since + timedelta(seconds=room.settle_seconds) if lights.off_since else None
    trusted_now = bool(
        chosen and chosen.value is not None and cont.state in ("clear", "possible")
        and chosen.sample_at is not None and (settled_from is None or chosen.sample_at >= settled_from)
    )
    accepted: TrustedSample | None = None
    notes: list[str] = []
    if chosen and chosen.preference:
        notes.append(f"Preferred sensor not usable; using fallback {chosen.name or chosen.entity_id}")

    new_bound: TrustedSample | None = None

    def done(est: Estimate) -> EstimateResult:
        return EstimateResult(replace(est, notes=tuple(notes) + est.notes), chosen, cont, accepted, new_bound)

    # 1. After dusk there is no natural light, whatever a sensor reads.
    if sun.dark:
        if trusted_now and chosen and chosen.value is not None and chosen.value >= room.off_above:
            notes.append(f"{chosen.name or chosen.entity_id} reads {fmt_lux(chosen.value)} lx after dusk with the "
                         "managed lights off: a light that is not listed as influencing probably reaches it")
        return done(Estimate(0.0, HIGH, "sun_dark", "Natural light: Low — estimated from the sun (after dusk)"))

    # 2. A trusted indoor measurement: lights off and settled.
    if trusted_now and chosen and chosen.value is not None:
        value = chosen.value
        accepted = TrustedSample(chosen.entity_id, value, chosen.sample_at or now, outdoor.value)
        confidence = HIGH if cont.state == "clear" and chosen.config.reliability == Reliability.NORMAL else MEDIUM
        if cont.state == "possible":
            notes.append(cont.text or "")
        if outdoor.value is not None and outdoor.value <= glob.outdoor_dark_lux and value >= room.off_above:
            confidence = MEDIUM
            notes.append("The outdoor sensor reads dark, but the room is bright: another light may reach the sensor")
        return done(Estimate(value, confidence, "indoor",
                             f"Natural light: {fmt_lux(value)} lx — measured indoors, lights off",
                             measured=True, sensor=chosen.entity_id))

    # 3. Lights on (or settling) and still dark at the sensor: natural light is at most the reading.
    if (chosen and chosen.value is not None and chosen.value < room.on_below and chosen.sample_at is not None
            and now - chosen.sample_at <= timedelta(minutes=BOUND_MAX_AGE_MINUTES)):
        how = {"lit": "with lights on", "settling": "while the lights settle"}.get(
            cont.state, "before the lights had been off long enough")
        new_bound = TrustedSample(chosen.entity_id, chosen.value, chosen.sample_at, outdoor.value)
        return done(Estimate(chosen.value, HIGH, "indoor_bound",
                             f"Natural light: at most {fmt_lux(chosen.value)} lx — measured indoors {how}",
                             measured=True, upper_bound=True, sensor=chosen.entity_id))
    if chosen and cont.contaminated:
        notes.append(f"Indoor reading not used as daylight: {cont.text}")
    elif chosen and not trusted_now:
        notes.append("Indoor reading is from before the lights had been off long enough; waiting for a new "
                     "measurement")

    shade = 1.0 if shading is None else min(max(shading, 0.0), 1.0)
    # 4. Dark outside.
    if outdoor.value is not None and outdoor.value <= glob.outdoor_dark_lux:
        confidence = HIGH
        if sun.elevation is not None and sun.elevation > 10:
            confidence = MEDIUM
            notes.append("The outdoor sensor reads dark although the sun is up (covered or shaded?)")
        return done(Estimate(0.0, confidence, "outdoor_dark", "Natural light: Low — estimated from outdoor darkness"))

    # 5. The most recent indoor evidence, if recent: a trusted sample (scaled by the outdoor change),
    #    or a later "at most" reading taken with lights on.
    recent = [x for x in (trusted, bound) if x is not None and now - x.at <= timedelta(minutes=room.sample_max_age)]
    if recent:
        latest = max(recent, key=lambda x: x.at)
        age = _minutes(now - latest.at)
        if latest is bound:
            return done(Estimate(latest.lux, MEDIUM, "recent_bound",
                                 f"Natural light: at most {fmt_lux(latest.lux)} lx — measured indoors {age} min ago "
                                 "with lights on", measured=True, upper_bound=True, sensor=latest.entity_id))
        lux = latest.lux
        how = f"measured indoors {age} min ago, lights off"
        if outdoor.value is not None and latest.outdoor and latest.outdoor >= CALIBRATION_MIN_OUTDOOR_LUX:
            lux = latest.lux * outdoor.value / latest.outdoor * shade
            how = f"from the indoor reading {age} min ago and outdoor light now"
        return done(Estimate(lux, MEDIUM, "recent_indoor", f"Natural light: ≈ {fmt_lux(lux)} lx — {how}",
                             sensor=latest.entity_id))

    # 6. Outdoor light x the room's daylight factor.
    factor = room.factor or learned_factor
    if outdoor.value is not None and factor:
        lux = outdoor.value * factor * shade
        source = "set" if room.factor else "learned"
        return done(Estimate(lux, MEDIUM, "outdoor_factor",
                             f"Natural light: ≈ {fmt_lux(lux)} lx — estimated from outdoor light "
                             f"({fmt_lux(outdoor.value)} lx × {factor:.3g}, {source} factor)"))

    # 7. The sun has set: little natural light.
    if sun.down:
        return done(Estimate(0.0, MEDIUM, "sun_down", "Natural light: Low — estimated from the sun (after sunset)"))

    # Uncertain.
    if not room.sensors:
        why = "no indoor light sensor"
    elif chosen is None:
        bad = next(iter(readings), None)
        why = "no trustworthy reading" + (f" ({bad.name or bad.entity_id} {bad.detail})" if bad else "")
    elif cont.contaminated:
        why = "the lights affect the indoor sensor and there is no other reference"
    else:
        why = "no trustworthy reading"
    if outdoor.value is not None:
        why += f"; outside is bright ({fmt_lux(outdoor.value)} lx) but this room's share is not known yet"
    return done(Estimate(None, None, "none", f"Natural light: Uncertain — {why}"))


# ----------------------------------------------------------------- decision
def classify(estimate: Estimate, room: RoomDaylight, previous: str | None) -> Darkness:
    """Dark / bright / uncertain, with hysteresis between the two thresholds."""
    if estimate.lux is None or estimate.confidence not in (HIGH, MEDIUM):
        return UNCERTAIN
    if estimate.lux < room.on_below:
        return DARK
    if estimate.lux >= room.off_above:
        return BRIGHT
    return Darkness(previous) if previous in (DARK, BRIGHT) else BRIGHT


@dataclass(slots=True)
class DarknessTracker:
    """The committed decision changes only after the new one held for ``stable_seconds``."""

    committed: Darkness | None = None
    since: datetime | None = None
    candidate: Darkness | None = None
    candidate_since: datetime | None = None

    def update(self, instant: Darkness, now: datetime, stable_seconds: int) -> Darkness | None:
        """Returns the previous committed decision when it changed (None otherwise)."""
        if self.committed is None:
            self.committed, self.since = instant, now
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
            self.candidate = self.candidate_since = None
            return old
        return None

    def due(self, stable_seconds: int) -> datetime | None:
        if self.candidate is None or self.candidate_since is None:
            return None
        return self.candidate_since + timedelta(seconds=stable_seconds)


# --------------------------------------------------------------------- gate
@dataclass(slots=True, frozen=True)
class DaylightGate:
    """What the planner needs: may occupancy switch an off light on?"""

    policy: AutoOn
    decision: Darkness
    uncertain: UncertainPolicy
    text: str
    on_below: int = DEFAULT_ON_BELOW_LUX

    def blocks(self) -> tuple[str, str] | None:
        """(reason code, reason) when switching on is not wanted; None when it may proceed."""
        if self.policy == AutoOn.NEVER:
            return GATE_NEVER, "Automatic switch-on is off for this room (lights already on still follow the mood)"
        if self.policy != AutoOn.DARK:
            return None
        if self.decision == BRIGHT:
            return GATE_NOT_NEEDED, f"Not switched on: daylight is enough ({self.text}; on below {self.on_below} lx)"
        if self.decision == UNCERTAIN and self.uncertain == UncertainPolicy.STAY_OFF:
            return GATE_UNCERTAIN, ("Not switched on: daylight is uncertain and this room then leaves lights off "
                                    f"({self.text})")
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"policy": self.policy.value, "decision": self.decision.value, "uncertain": self.uncertain.value,
                "text": self.text, "on_below": self.on_below, "blocks": (b := self.blocks()) and b[0]}
