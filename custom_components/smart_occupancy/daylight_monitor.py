"""Daylight runtime: keeps each room's daylight decision current (Phase 0.3.3).

Event driven, no polling:

* one state subscription for every indoor light sensor, the outdoor sensor and
  every influencing light;
* lux changes are coalesced (``DAYLIGHT_DEBOUNCE_SECONDS``) before a
  background re-evaluation; occupancy triggers evaluate immediately with the
  latest evidence;
* each room has at most one timer, set to the next moment its decision can
  change by itself (stability period, settling after lights turned off, a
  trusted sample or reading getting too old);
* one timer for the next dawn / sunrise / sunset / dusk.

The committed decision of a room changes only after the new one held for the
room's stability period. When it becomes dark while the room is occupied, the
lighting controller is told (``LightingController.async_daylight_dark``);
nothing else here can cause a light command.

Vacant profile (0.4.0): a room can give its Vacant profile its own darkness
threshold. Then a second decision is kept at that threshold, with the same
hysteresis (the room's ratio between its off and on thresholds) and the same
stability period; without one it is the room's decision. Every change of the
committed vacant decision is reported (``async_daylight_vacant``), so a
vacant room can switch decorative lights on when it becomes dark and off
again when daylight is enough.

Trusted samples and calibration ratios are kept in the lighting state store
(``.storage/smart_occupancy.lighting_state``), so a restart does not lose them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import (
    BOUND_MAX_AGE_MINUTES,
    DAYLIGHT_DEBOUNCE_SECONDS,
    AutoOn,
    Darkness,
    UncertainPolicy,
)
from .daylight import (
    BRIGHT,
    DARK,
    NO_OUTDOOR,
    OK,
    UNCERTAIN,
    Calibration,
    Contamination,
    DarknessTracker,
    DaylightGate,
    Estimate,
    LightEvidence,
    OutdoorFilter,
    OutdoorReading,
    RoomDaylight,
    SensorReading,
    SunInfo,
    TrustedSample,
    classify,
    estimate_daylight,
    fmt_lux,
    light_evidence,
    parse_lux,
    read_sensor,
    round_lux,
)

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

_SUN_EVENTS = ("dawn", "sunrise", "sunset", "dusk")
_SAVE_INTERVAL = timedelta(minutes=5)
_WORDS = {DARK: "Dark", BRIGHT: "Bright", UNCERTAIN: "Uncertain"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass(slots=True)
class DaylightStatus:
    """The daylight situation of one room at one evaluation."""

    area_id: str
    config: RoomDaylight
    decision: Darkness          # used for switching on now (hysteresis applied)
    committed: Darkness | None  # stable decision (changes after the stability period)
    estimate: Estimate
    chosen: SensorReading | None
    contamination: Contamination
    readings: list[SensorReading]
    lights: LightEvidence
    outdoor: OutdoorReading
    sun: SunInfo
    trusted: TrustedSample | None
    learned_factor: float | None
    calibration_samples: int
    evaluated_at: datetime
    changing_since: datetime | None = None
    errors: list[str] = field(default_factory=list)
    # The Vacant profile's own threshold (0.4.0); None: the room's decision is used.
    vacant_on_below: int | None = None
    vacant_off_above: int | None = None
    vacant_decision: Darkness | None = None
    vacant_committed: Darkness | None = None

    @property
    def gate(self) -> DaylightGate:
        return DaylightGate(self.config.auto_on, self.decision, self.config.uncertain, self.estimate.text,
                            self.config.on_below)

    @property
    def vacant_gate(self) -> DaylightGate:
        """The daylight decision for the Vacant profile: at its own threshold, if it has one."""
        if self.vacant_on_below is None or self.vacant_decision is None:
            return self.gate
        return DaylightGate(self.config.auto_on, self.vacant_decision, self.config.uncertain, self.estimate.text,
                            self.vacant_on_below)

    @property
    def reason(self) -> str:
        cfg = self.config
        lux = self.estimate.lux
        if cfg.auto_on == AutoOn.NEVER:
            policy = "Automatic switch-on is off"
        elif cfg.auto_on == AutoOn.ALWAYS:
            policy = "Lights turn on when occupied, regardless of daylight"
        else:
            policy = None
        if self.decision == DARK:
            text = "No daylight outside" if self.estimate.basis in ("sun_dark", "outdoor_dark", "sun_down") \
                else f"Below {cfg.on_below} lx"
            text += ": lights are needed"
        elif self.decision == BRIGHT:
            if lux is not None and lux < cfg.off_above:
                text = (f"Between {cfg.on_below} and {cfg.off_above} lx: stays bright until it drops below "
                        f"{cfg.on_below} lx")
            else:
                text = f"At least {cfg.off_above} lx: lights are not needed"
        else:
            text = "Not enough evidence: lights " + (
                "turn on anyway (fallback)" if cfg.uncertain == UncertainPolicy.TURN_ON else "stay off (fallback)")
        if self.committed is not None and self.committed != self.decision:
            text += f" (was {_WORDS[self.committed].lower()}; settles after {cfg.stable_seconds} s)"
        return f"{policy} · {text}" if policy else text

    def summary(self) -> dict[str, Any]:
        """Compact, rounded view (entity state attributes, dashboard header)."""
        e = self.estimate
        return {
            "decision": self.decision.value,
            "natural_light": e.text,
            "lux": round_lux(e.lux),
            "estimated": not e.measured and e.lux is not None,
            "upper_bound": e.upper_bound,
            "confidence": e.confidence,
            "basis": e.basis,
            "reason": self.reason,
            "policy": self.config.auto_on.value,
            "contaminated": self.contamination.contaminated,
            "contamination": self.contamination.text,
            "sensor": self.chosen.entity_id if self.chosen else None,
            "outdoor_lux": round_lux(self.outdoor.value),
            "sun_elevation": None if self.sun.elevation is None else round(self.sun.elevation),
            "errors": list(self.errors),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "committed": self.committed.value if self.committed else None,
            "changing_since": _iso(self.changing_since),
            "estimate": self.estimate.as_dict(),
            "contamination_detail": self.contamination.as_dict(),
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "sensors": [r.as_dict() for r in self.readings],
            "lights_on": list(self.lights.on),
            "lights_unknown": list(self.lights.unknown),
            "lights_off_since": _iso(self.lights.off_since),
            "outdoor": self.outdoor.as_dict(),
            "sun": self.sun.as_dict(),
            "last_trusted": self.trusted.as_dict() if self.trusted else None,
            "factor": {"set": self.config.factor, "learned": None if self.learned_factor is None
                       else round(self.learned_factor, 4), "samples": self.calibration_samples},
            "evaluated_at": _iso(self.evaluated_at),
            "gate": self.gate.as_dict(),
            "vacant": {"on_below": self.vacant_on_below, "off_above": self.vacant_off_above,
                       "decision": self.vacant_decision.value if self.vacant_decision else None,
                       "committed": self.vacant_committed.value if self.vacant_committed else None,
                       "gate": self.vacant_gate.as_dict()},
        }


@dataclass(slots=True)
class _Room:
    tracker: DarknessTracker = field(default_factory=DarknessTracker)
    trusted: dict[str, TrustedSample] = field(default_factory=dict)
    # Latest "at most" reading (lights on, still dark): newer evidence than an older trusted sample.
    bound: TrustedSample | None = None
    calibration: Calibration = field(default_factory=Calibration)
    status: DaylightStatus | None = None
    fingerprint: Any = None
    usable: frozenset[str] = frozenset()
    saved_at: datetime | None = None  # sample time of the last trusted sample that asked for a save
    timer: CALLBACK_TYPE | None = None
    timer_at: datetime | None = None
    # The Vacant profile's decision at its own threshold (0.4.0): the threshold it was made for, and its tracker.
    vacant_on_below: int | None = None
    vacant: DarknessTracker = field(default_factory=DarknessTracker)


def vacant_thresholds(cfg: RoomDaylight, on_below: int) -> RoomDaylight:
    """The room's daylight settings at the Vacant profile's threshold, with the room's hysteresis ratio."""
    ratio = cfg.off_above / cfg.on_below if cfg.on_below else 2.0
    return replace(cfg, on_below=on_below, off_above=max(on_below + 1, round(on_below * ratio)))


class DaylightMonitor:
    """Daylight decisions of every enabled room."""

    def __init__(self, hass: HomeAssistant, controller: LightingController) -> None:
        self.hass = hass
        self.ctrl = controller
        self.rooms: dict[str, _Room] = {}
        self.outdoor = OutdoorFilter()
        self._outdoor_entity: str | None = None
        self._entity_rooms: dict[str, set[str]] = {}
        self._tracked: tuple[str, ...] = ()
        self._unsub: CALLBACK_TYPE | None = None
        self._pending: set[str] = set()
        self._debounce: CALLBACK_TYPE | None = None
        self._sun_timer: CALLBACK_TYPE | None = None
        self._sun_cache: tuple[datetime, SunInfo] | None = None
        self._outdoor_ok: bool | None = None
        self._started = False

    # ------------------------------------------------------------ lifecycle
    def restore(self, data: Any) -> None:
        rooms = data.get("rooms") if isinstance(data, dict) and isinstance(data.get("rooms"), dict) else {}
        for area_id, item in rooms.items():
            if not isinstance(item, dict):
                continue
            room = self.rooms.setdefault(area_id, _Room())
            for entity_id, raw in (item.get("trusted") or {}).items():
                if (sample := TrustedSample.restore(raw)) is not None:
                    room.trusted[entity_id] = sample
            room.calibration = Calibration.restore(item.get("calibration"))

    def persist(self) -> dict[str, Any]:
        return {"rooms": {
            a: {"trusted": {e: s.persist() for e, s in r.trusted.items()}, "calibration": r.calibration.persist()}
            for a, r in self.rooms.items() if r.trusted or r.calibration.ratios
        }}

    @callback
    def async_start(self) -> None:
        self._started = True
        self.resubscribe()
        self._schedule_sun()
        now = dt_util.utcnow()
        for area_id in self._areas:
            self.evaluate(area_id, now, notify=False)

    @callback
    def async_stop(self) -> None:
        self._started = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._tracked = ()
        for attr in ("_debounce", "_sun_timer"):
            if cancel := getattr(self, attr):
                cancel()
                setattr(self, attr, None)
        for room in self.rooms.values():
            if room.timer:
                room.timer()
                room.timer = None
        self._pending.clear()

    # --------------------------------------------------------------- config
    @property
    def _areas(self) -> list[str]:
        return sorted(self.ctrl.config.enabled_rooms)

    def room_config(self, area_id: str) -> RoomDaylight:
        return self.ctrl.lighting.room(area_id).daylight

    def influencing(self, area_id: str) -> list[str]:
        """Lights whose light can reach the room's sensors: managed ones always, plus configured extras."""
        cfg = self.room_config(area_id)
        managed = self.ctrl.managed_room(area_id)
        out = set(managed.targets) | set(cfg.influencing)
        for members in managed.members.values():
            out |= members
        if cfg.area_lights:
            out |= set(self.ctrl.index.by_area.get(area_id, []))
        return sorted(out)

    @callback
    def resubscribe(self) -> None:
        """Track the sensors and lights of every room (call after configuration or registry changes)."""
        if not self._started:
            return
        entity_rooms: dict[str, set[str]] = {}
        for area_id in self._areas:
            for entity_id in (*self.room_config(area_id).entities, *self.influencing(area_id)):
                entity_rooms.setdefault(entity_id, set()).add(area_id)
        outdoor = self.ctrl.lighting.daylight.outdoor_entity
        if outdoor != self._outdoor_entity:
            self._outdoor_entity = outdoor
            self.outdoor.clear()
            self._outdoor_ok = None
            if outdoor and (state := self.hass.states.get(outdoor)) is not None:
                value, status, _ = parse_lux(state)
                self.outdoor.add(min(state.last_changed, dt_util.utcnow()), value if status == OK else None)
        self._entity_rooms = entity_rooms
        for area_id in list(self.rooms):
            if area_id in self.ctrl.config.enabled_rooms:
                continue
            room = self.rooms[area_id]
            if room.timer:
                room.timer()
                room.timer = room.timer_at = None
            if not (room.trusted or room.calibration.ratios):
                del self.rooms[area_id]
        tracked = tuple(sorted({*entity_rooms, *([outdoor] if outdoor else [])}))
        if tracked == self._tracked and (self._unsub or not tracked):
            return
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._tracked = tracked
        if tracked:
            self._unsub = async_track_state_change_event(self.hass, list(tracked), self._on_state)

    @callback
    def async_config_changed(self) -> None:
        """Daylight settings (or managed lights) changed: re-subscribe and re-evaluate every room."""
        self.resubscribe()
        if self._started:
            self._schedule_eval(self._areas, 0)

    @callback
    def reset_calibration(self, area_id: str) -> None:
        room = self.rooms.setdefault(area_id, _Room())
        room.calibration = Calibration()
        room.trusted.clear()
        room.bound = None
        self.ctrl._mark_dirty()
        self._schedule_eval([area_id], 0)

    # --------------------------------------------------------------- events
    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        now = dt_util.utcnow()
        if entity_id == self._outdoor_entity:
            value, status, _ = parse_lux(new)
            self.outdoor.add(min(new.last_changed, now) if new else now, value if status == OK else None)
            ok = status == OK
            if ok != self._outdoor_ok:
                self._outdoor_ok = ok
                self.ctrl.coordinator.async_schedule_refresh()
            self._schedule_eval(self._areas, DAYLIGHT_DEBOUNCE_SECONDS)
        rooms = self._entity_rooms.get(entity_id)
        if rooms:
            is_light = entity_id.split(".", 1)[0] in ("light", "switch")
            # A light switching is evidence about contamination: evaluate quickly.
            self._schedule_eval(rooms, 0.5 if is_light else DAYLIGHT_DEBOUNCE_SECONDS)

    @callback
    def _schedule_eval(self, areas, delay: float) -> None:
        if not self._started:
            return
        self._pending.update(a for a in areas if a in self.ctrl.config.enabled_rooms)
        if self._debounce is not None:
            if delay > 0:
                return
            self._debounce()
            self._debounce = None

        @callback
        def fire(_now: datetime) -> None:
            self._debounce = None
            pending, self._pending = self._pending, set()
            now = dt_util.utcnow()
            for area_id in sorted(pending):
                self.evaluate(area_id, now)

        self._debounce = async_call_later(self.hass, delay, fire)

    def _schedule_sun(self) -> None:
        if self._sun_timer:
            self._sun_timer()
            self._sun_timer = None
        if not self._started:
            return
        from homeassistant.helpers.sun import get_astral_event_next

        now = dt_util.utcnow()
        times = []
        for event in _SUN_EVENTS:
            try:
                times.append(get_astral_event_next(self.hass, event, now))
            except ValueError:
                continue  # polar day/night: this event does not occur
        if not times:
            return

        @callback
        def fire(_now: datetime) -> None:
            self._sun_timer = None
            self._sun_cache = None
            self._schedule_sun()
            self._schedule_eval(self._areas, 0)

        self._sun_timer = async_track_point_in_utc_time(self.hass, fire, min(times) + timedelta(seconds=1))

    def _sun(self, now: datetime) -> SunInfo:
        if self._sun_cache and abs((now - self._sun_cache[0]).total_seconds()) < 30:
            return self._sun_cache[1]
        try:
            import astral.sun

            from homeassistant.helpers.sun import get_astral_event_next, get_astral_observer

            observer = get_astral_observer(self.hass)
            elevation = float(astral.sun.elevation(observer, now))

            def next_event(name: str) -> datetime | None:
                try:
                    return get_astral_event_next(self.hass, name, now)
                except ValueError:
                    return None

            info = SunInfo(elevation, next_event("sunrise"), next_event("sunset"))
        except (ValueError, TypeError, ArithmeticError) as err:
            _LOGGER.debug("sun_position_unavailable error=%s", err)
            info = SunInfo()
        self._sun_cache = (now, info)
        return info

    def _outdoor_reading(self, now: datetime) -> OutdoorReading:
        cfg = self.ctrl.lighting.daylight
        entity = cfg.outdoor_entity
        if not entity:
            return NO_OUTDOOR
        state = self.hass.states.get(entity)
        raw, status, detail = parse_lux(state)
        if status != OK:
            return OutdoorReading(entity, None, None, status, f"{entity} {detail}")
        if not len(self.outdoor):
            self.outdoor.add(min(state.last_changed, now) if state else now, raw)
        value = self.outdoor.value(now, cfg.outdoor_window)
        if value is None:
            value = raw
        smoothed = f"; {fmt_lux(value)} lx over {cfg.outdoor_window // 60 or cfg.outdoor_window} " \
                   f"{'min' if cfg.outdoor_window >= 60 else 's'}" if value != raw and cfg.outdoor_window else ""
        return OutdoorReading(entity, value, raw, OK, f"{detail}{smoothed}")

    # ----------------------------------------------------------- evaluation
    def evaluate(self, area_id: str, now: datetime | None = None, *, notify: bool = True) -> DaylightStatus:
        """Re-evaluate one room with the current states. May tell the controller it became dark."""
        now = now or dt_util.utcnow()
        hass = self.hass
        cfg = self.room_config(area_id)
        room = self.rooms.setdefault(area_id, _Room())
        readings = [read_sensor(hass.states.get(s.entity_id), s, i, now) for i, s in enumerate(cfg.sensors)]
        lights = light_evidence((e, hass.states.get(e)) for e in self.influencing(area_id))
        outdoor = self._outdoor_reading(now)
        sun = self._sun(now)
        configured = set(cfg.entities)
        room.trusted = {e: s for e, s in room.trusted.items() if e in configured}
        chosen = next((r for r in readings if r.usable), None)
        trusted = (room.trusted.get(chosen.entity_id) if chosen else None) or max(
            room.trusted.values(), key=lambda s: s.at, default=None)
        if room.bound is not None and room.bound.entity_id not in configured:
            room.bound = None
        result = estimate_daylight(
            room=cfg, glob=self.ctrl.lighting.daylight, readings=readings, lights=lights, outdoor=outdoor,
            sun=sun, trusted=trusted, learned_factor=room.calibration.factor, now=now, bound=room.bound,
        )
        if result.bound is not None:
            room.bound = result.bound
        if (sample := result.accepted) is not None:
            previous = room.trusted.get(sample.entity_id)
            if previous is None or sample.at > previous.at or abs(sample.lux - previous.lux) > 0.5:
                room.trusted[sample.entity_id] = sample
                calibrated = room.calibration.add(sample)
                if calibrated:
                    _LOGGER.debug("daylight_calibration area=%s factor=%s samples=%d", area_id,
                                  room.calibration.factor, len(room.calibration.ratios))
                # Kept in memory at once; written at most every few minutes (frequent sensors).
                if calibrated or room.saved_at is None or sample.at - room.saved_at >= _SAVE_INTERVAL:
                    room.saved_at = sample.at
                    self.ctrl._mark_dirty()
            trusted = room.trusted[sample.entity_id]
        instant = classify(result.estimate, cfg, room.tracker.committed)
        old = room.tracker.update(instant, now, cfg.stable_seconds)
        # The Vacant profile's decision (0.4.0): at its own threshold, or the room's own decision.
        v_on = self.ctrl.lighting.vacant_on_below(area_id)
        if v_on is not None and v_on == cfg.on_below:
            v_on = None
        if v_on != room.vacant_on_below:
            room.vacant_on_below, room.vacant = v_on, DarknessTracker()
        vcfg = vacant_thresholds(cfg, v_on) if v_on is not None else None
        if vcfg is not None:
            v_instant = classify(result.estimate, vcfg, room.vacant.committed)
            v_old = room.vacant.update(v_instant, now, cfg.stable_seconds)
            v_committed = room.vacant.committed
        else:
            v_instant, v_old, v_committed = instant, old, room.tracker.committed
        errors = [f"{r.name or r.entity_id}: {r.detail}" for r in readings if r.status != OK]
        if outdoor.status not in (OK, "not_configured"):
            errors.append(f"Outdoor: {outdoor.detail}")
        status = DaylightStatus(
            area_id=area_id, config=cfg, decision=instant, committed=room.tracker.committed,
            estimate=result.estimate, chosen=result.chosen, contamination=result.contamination, readings=readings,
            lights=lights, outdoor=outdoor, sun=sun, trusted=trusted, learned_factor=room.calibration.factor,
            calibration_samples=len(room.calibration.ratios), evaluated_at=now,
            changing_since=room.tracker.candidate_since, errors=errors,
            vacant_on_below=v_on, vacant_off_above=vcfg.off_above if vcfg else None,
            vacant_decision=v_instant, vacant_committed=v_committed,
        )
        room.status = status
        if old is not None:
            _LOGGER.debug("daylight_decision area=%s old=%s new=%s basis=%s text=%s", area_id, old,
                          room.tracker.committed, result.estimate.basis, result.estimate.text)
            if room.tracker.committed == DARK and cfg.auto_on == AutoOn.DARK and (
                    old == BRIGHT or (old == UNCERTAIN and cfg.uncertain == UncertainPolicy.STAY_OFF)):
                self.ctrl.async_daylight_dark(area_id, status)
        if v_old is not None and v_committed is not None:
            _LOGGER.debug("daylight_vacant_decision area=%s old=%s new=%s on_below=%s", area_id, v_old,
                          v_committed, v_on if v_on is not None else cfg.on_below)
            self.ctrl.async_daylight_vacant(area_id, v_old, status)
        self._schedule_deadline(area_id, room, status, now)
        usable = frozenset(r.entity_id for r in readings if r.usable)
        if usable != room.usable:
            room.usable = usable
            self.ctrl.coordinator.async_schedule_refresh()
        fingerprint = (status.summary(), status.committed)
        if fingerprint != room.fingerprint:
            room.fingerprint = fingerprint
            if notify:
                self.ctrl._notify()
        return status

    def _schedule_deadline(self, area_id: str, room: _Room, status: DaylightStatus, now: datetime) -> None:
        cfg = status.config
        candidates: list[datetime] = []
        if (due := room.tracker.due(cfg.stable_seconds)) is not None:
            candidates.append(due)
        if room.vacant_on_below is not None and (due := room.vacant.due(cfg.stable_seconds)) is not None:
            candidates.append(due)
        if status.contamination.until is not None:
            candidates.append(status.contamination.until)
        for sample in (status.trusted, room.bound):
            if sample is not None:
                candidates.append(sample.at + timedelta(minutes=cfg.sample_max_age))
        if status.estimate.basis == "indoor_bound" and status.chosen and status.chosen.sample_at:
            candidates.append(status.chosen.sample_at + timedelta(minutes=BOUND_MAX_AGE_MINUTES))
        for r in status.readings:
            if r.usable and r.config.max_age and r.reported_at:
                candidates.append(r.reported_at + timedelta(minutes=r.config.max_age))
        out = status.outdoor
        if out.value is not None and out.raw is not None and out.value != out.raw:
            # The smoothed outdoor value is still moving towards the latest reading: look again
            # (bounded: only until the window has passed the last change).
            window = self.ctrl.lighting.daylight.outdoor_window
            candidates.append(now + timedelta(seconds=max(15.0, window / 10)))
        future = [c for c in candidates if c > now]
        when = min(future) + timedelta(milliseconds=200) if future else None
        if when == room.timer_at and room.timer is not None:
            return
        if room.timer:
            room.timer()
            room.timer = None
        room.timer_at = when
        if when is None or not self._started:
            return

        @callback
        def fire(_now: datetime) -> None:
            room.timer = None
            room.timer_at = None
            self.evaluate(area_id)

        room.timer = async_track_point_in_utc_time(self.hass, fire, when)

    # -------------------------------------------------------------- queries
    def status(self, area_id: str) -> DaylightStatus:
        """The latest evaluation (evaluated now if there is none)."""
        room = self.rooms.get(area_id)
        cfg = self.room_config(area_id)
        v_on = self.ctrl.lighting.vacant_on_below(area_id)
        if room is None or room.status is None or room.status.config != cfg \
                or room.status.vacant_on_below != (None if v_on == cfg.on_below else v_on):
            return self.evaluate(area_id, notify=False)
        return room.status

    def gate(self, area_id: str) -> DaylightGate:
        """Fresh decision for switching on now (uses the latest evidence, no waiting)."""
        return self.evaluate(area_id, notify=True).gate

    def vacant_gate(self, area_id: str, *, fresh: bool = False) -> DaylightGate:
        """The Vacant profile's daylight decision (its own threshold, if it has one)."""
        return (self.evaluate(area_id, notify=True) if fresh else self.status(area_id)).vacant_gate

    def diagnostics(self) -> dict[str, Any]:
        return {
            "outdoor_entity": self._outdoor_entity,
            "outdoor_samples": len(self.outdoor),
            "tracked_entities": list(self._tracked),
            "rooms": {a: {**self.status(a).as_dict(), "influencing": self.influencing(a)} for a in self._areas},
        }
