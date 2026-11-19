"""Zone occupancy at runtime (0.6.0): one occupancy engine per enabled zone.

Each zone of an enabled room gets its own ``RoomEngine`` (the Phase 0.2
engine, unchanged) over its subset of the room's signals, with the room's
timing and the zone's own overrides. The engines are fed from one state
subscription and have at most one timer each (their next deadline), exactly
like the rooms' engines in ``occupancy_manager.py``. Nothing here polls.

A zone is **active** (its lighting rule applies) when:

* its engine reports it occupied (``activation: possible``), or occupied
  after a confirmed visit (``activation: confirmed``),
* or, while every zone signal is unavailable, by its fail-safe: ``hold`` keeps
  the last state, ``vacant`` releases it, ``occupied`` keeps it active while
  the room is occupied,
* and never while the room itself is vacant.

Zones with ``holds_room`` keep their (occupied) room occupied while they are
active and have a known state (an unavailable zone never holds a room).

Radar Distance Zones (0.6.8) have no engine: a ``radar.RadarTracker`` turns
the radar's presence state and distance into the zone's active state (entry
delay, exit delay, hysteresis, fail-safe), with the same room gating, holds,
history and timer handling as every other zone.

Only sensor states make a zone active: no prediction or forecast is used.
Every transition is kept in a small bounded history per room (zone-aware data
for later learning; observation only) and in the diagnostics.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import ZONE_HISTORY_SIZE, OccupancyState
from .engine import RoomEngine, RoomResult, SignalStatus, interpret
from .occupancy import friendly_name
from .radar import Distance, Evaluation, RadarTracker, fmt_m, presence_of, read_distance
from .zones import ZoneActivation, ZoneConfig, ZoneUnavailable

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass(slots=True)
class ZoneRuntime:
    area_id: str
    zone: ZoneConfig
    engine: RoomEngine | None
    active: bool = False
    active_since: datetime | None = None
    confirmed: bool = False
    failsafe: str | None = None  # the fail-safe in force while every zone signal is unavailable
    result: RoomResult | None = None
    # Radar Distance Zone (0.6.8): the band tracker, its last evaluation and inputs.
    radar: RadarTracker | None = None
    radar_eval: Evaluation | None = None
    reading: Distance | None = None
    presence: bool | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.area_id, self.zone.id

    @property
    def known(self) -> bool:
        if self.zone.is_radar:
            return self.radar_eval is not None and self.radar_eval.known
        return self.result is not None and self.result.state != OccupancyState.UNCERTAIN

    def radar_status(self, hass: HomeAssistant) -> dict[str, Any] | None:
        cfg = self.zone.radar
        if not self.zone.is_radar:
            return None
        if cfg is None:
            return {"configured": False, "state": "unavailable", "reason": "radar entities not chosen"}
        ev, reading = self.radar_eval, self.reading
        state = "unavailable" if ev is None or not ev.known or (reading is not None and not reading.valid
                                                                and self.presence) else (
            "active" if self.active else "inactive")
        return {
            "configured": True, "presence_entity": cfg.presence, "distance_entity": cfg.distance,
            "presence_name": friendly_name(hass, cfg.presence) or cfg.presence,
            "distance_name": friendly_name(hass, cfg.distance) or cfg.distance,
            "presence": self.presence, "distance_m": reading.metres if reading else None,
            "distance_text": f"{fmt_m(reading.metres)} m" if reading and reading.valid else
            (reading.problem if reading else None),
            "distance_valid": bool(reading and reading.valid), "raw": reading.raw if reading else None,
            "unit": reading.unit if reading else None,
            "phase": ev.phase if ev else None, "reason": ev.reason if ev else None,
            "due": _iso(ev.due) if ev else None, "state": state, "band": cfg.band(),
            "min_m": cfg.min_m, "max_m": cfg.max_m, "hysteresis": cfg.hysteresis,
        }

    def status(self, hass: HomeAssistant) -> dict[str, Any]:
        if self.zone.is_radar:
            radar = self.radar_status(hass) or {}
            return {
                "id": self.zone.id, "name": self.zone.name, "active": self.active, "since": _iso(self.active_since),
                "state": radar.get("phase"), "occupied": self.active, "reason": radar.get("reason"),
                "failsafe": self.failsafe, "sensors": [], "available": self.known, "vacancy_at": radar.get("due"),
                "radar": radar,
            }
        r = self.result
        sensors = []
        for v in (r.signals if r else ()):
            sensors.append({"entity_id": v.track.entity_id, "name": v.track.name or friendly_name(hass, v.track.entity_id)
                            or v.track.entity_id, "status": v.status.value, "value": v.track.value.value,
                            "available": v.status != SignalStatus.UNKNOWN, "used": v.track.used})
        return {
            "id": self.zone.id, "name": self.zone.name, "active": self.active, "since": _iso(self.active_since),
            "state": r.state.value if r else None, "occupied": r.occupied if r else None,
            "reason": r.reason if r else None, "failsafe": self.failsafe, "sensors": sensors,
            "available": self.known, "vacancy_at": _iso(r.vacancy_at) if r else None,
        }


@dataclass(slots=True)
class _RoomZones:
    zones: dict[str, ZoneRuntime] = field(default_factory=dict)
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=ZONE_HISTORY_SIZE))


class ZoneManager:
    """Owns the zone engines, their timers and their state; tells the lighting controller about changes."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController,
                 on_change: Callable[[str, set[str]], None]) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self._on_change = on_change
        self.rooms: dict[str, _RoomZones] = {}
        self._entity_zones: dict[str, list[tuple[str, str]]] = {}
        self._timers: dict[tuple[str, str], CALLBACK_TYPE] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._room_unsubs: list[CALLBACK_TYPE] = []
        self._restored: dict[str, Any] = {}
        self._running = False
        # App 0.9.0: True while zones are rebuilt for an edit that removed none (detection or lighting settings
        # saved, a zone added): the controller takes the resulting zone states as its baseline, sending nothing.
        self.quiet = False

    # ------------------------------------------------------------ lifecycle
    def restore(self, data: Any) -> None:
        self._restored = data if isinstance(data, dict) else {}

    def persist(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for area_id, room in self.rooms.items():
            zones = {z.zone.id: {"engine": z.engine.persist() if z.engine else None, "active": z.active,
                                 "since": _iso(z.active_since), "confirmed": z.confirmed}
                     for z in room.zones.values()}
            out[area_id] = {"zones": zones, "history": list(room.history)}
        return out

    @callback
    def async_start(self) -> None:
        self._running = True
        self._build()
        for area_id in self.ctrl.config.enabled_rooms:
            self._room_unsubs.append(
                self.ctrl.coordinator.occupancy.async_add_listener(area_id, self._room_listener(area_id)))

    @callback
    def async_stop(self) -> None:
        self._running = False
        self._clear_subscriptions()
        for unsub in self._room_unsubs:
            unsub()
        self._room_unsubs.clear()

    def _clear_subscriptions(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()

    @callback
    def async_config_changed(self, *, quiet: bool = False) -> None:
        """Zones were added, removed or changed: engines of unchanged signals keep what they know.

        ``quiet`` (App 0.9.0): no zone was removed - a state the edit itself changes is the new baseline.
        """
        if self._running:
            self.quiet = quiet
            try:
                self._build()
            finally:
                self.quiet = False

    # ------------------------------------------------------------- building
    def _build(self) -> None:
        ctrl = self.ctrl
        now = dt_util.utcnow()
        old_rooms, self.rooms = self.rooms, {}
        self._clear_subscriptions()
        self._entity_zones = {}
        changed: dict[str, set[str]] = {}
        for area_id, room_cfg in ctrl.config.enabled_rooms.items():
            zones = [z for z in ctrl.lighting.room(area_id).zones if z.enabled]
            old = old_rooms.get(area_id)
            room = _RoomZones(history=old.history if old else deque(
                (h for h in (self._restored.get(area_id) or {}).get("history") or [] if isinstance(h, dict)),
                maxlen=ZONE_HISTORY_SIZE))
            assigned = {s.entity_id: s for s in room_cfg.sensors}
            for zone in zones:
                previous = old.zones.get(zone.id) if old else None
                if zone.is_radar:
                    room.zones[zone.id] = self._build_radar(area_id, zone, previous)
                    continue
                sensors = tuple(assigned[e] for e in zone.sensors if e in assigned)
                timing = ctrl.config.timing_for(area_id).with_overrides(zone.timing)
                if previous is not None and previous.engine is not None:
                    engine = previous.engine
                    engine.reconfigure(sensors, timing)
                    zr = ZoneRuntime(area_id, zone, engine, previous.active, previous.active_since, previous.confirmed,
                                     previous.failsafe, previous.result)
                else:
                    engine = RoomEngine(f"{area_id}#{zone.id}", sensors, timing)
                    saved = ((self._restored.get(area_id) or {}).get("zones") or {}).get(zone.id) or {}
                    zr = ZoneRuntime(area_id, zone, engine)
                    if isinstance(saved.get("engine"), dict):
                        engine.restore(saved["engine"], now)
                        zr.active = bool(saved.get("active"))
                        zr.confirmed = bool(saved.get("confirmed"))
                        zr.active_since = dt_util.parse_datetime(saved.get("since") or "") if zr.active else None
                    else:
                        engine.start(now)
                for entity_id, track in engine.tracks.items():
                    state = self.hass.states.get(entity_id)
                    track.name = friendly_name(self.hass, entity_id)
                    at = min(state.last_changed, now) if state else now
                    value = interpret(state.state if state else None, inverted=track.assignment.inverted)
                    if value != track.value:
                        if track.since is not None and at < track.since:
                            at = now
                        engine.observe(entity_id, value, at)
                    self._entity_zones.setdefault(entity_id, []).append((area_id, zone.id))
                room.zones[zone.id] = zr
            if old:
                gone = set(old.zones) - set(room.zones)
                if any(old.zones[z].active for z in gone):
                    changed.setdefault(area_id, set()).update(gone)
            self.rooms[area_id] = room
        self._restored = {}
        if self._entity_zones:
            self._unsub = async_track_state_change_event(self.hass, list(self._entity_zones), self._on_state)
        for area_id in self.rooms:
            changed.setdefault(area_id, set()).update(self._evaluate_room(area_id, now, notify=False))
        self._sync_holds()
        for area_id, zones in changed.items():
            if zones:
                self._on_change(area_id, zones)

    def _build_radar(self, area_id: str, zone: ZoneConfig, previous: ZoneRuntime | None) -> ZoneRuntime:
        """A Radar Distance Zone: keeps its state over a reconfiguration; restored after a restart."""
        zr = ZoneRuntime(area_id, zone, None)
        if zone.radar is not None:
            zr.radar = RadarTracker(zone.radar)
            if previous is not None and previous.zone.is_radar:
                zr.active, zr.active_since, zr.failsafe = previous.active, previous.active_since, previous.failsafe
                if previous.radar is not None:
                    zr.radar.phase, zr.radar.since = previous.radar.phase, previous.radar.since
            else:
                saved = ((self._restored.get(area_id) or {}).get("zones") or {}).get(zone.id) or {}
                if saved.get("active"):
                    # (Restored as active; the current readings decide at once, with the exit delay.)
                    zr.active = True
                    zr.active_since = dt_util.parse_datetime(saved.get("since") or "")
                    zr.radar.restore(True, zr.active_since)
            for entity_id in zone.radar.entities:
                self._entity_zones.setdefault(entity_id, []).append((area_id, zone.id))
        return zr

    # --------------------------------------------------------------- events
    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new = event.data["new_state"]
        now = dt_util.utcnow()
        at = min(new.last_changed, now) if new else now
        name = new.attributes.get("friendly_name") if new else None
        rooms: dict[str, set[str]] = {}
        for area_id, zone_id in self._entity_zones.get(entity_id, ()):
            zr = self.rooms.get(area_id, _RoomZones()).zones.get(zone_id)
            if zr is None:
                continue
            if zr.engine is None:
                rooms.setdefault(area_id, set()).add(zone_id)  # (a radar zone reads its entities when evaluated)
                continue
            track = zr.engine.tracks[entity_id]
            zr.engine.observe(entity_id, interpret(new.state if new else None, inverted=track.assignment.inverted),
                              at, name)
            rooms.setdefault(area_id, set()).add(zone_id)
        for area_id, zone_ids in rooms.items():
            self._update(area_id, now, zone_ids)

    def _room_listener(self, area_id: str) -> Callable[[], None]:
        @callback
        def update() -> None:
            # The room's occupancy changed (e.g. vacant): its zones are evaluated again.
            if self._running and area_id in self.rooms:
                self._update(area_id, dt_util.utcnow(), None)
        return update

    def _timer(self, key: tuple[str, str]) -> Callable[[datetime], None]:
        @callback
        def fire(_now: datetime) -> None:
            self._timers.pop(key, None)
            if self._running:
                self._update(key[0], dt_util.utcnow(), {key[1]})
        return fire

    def _update(self, area_id: str, now: datetime, zone_ids: set[str] | None) -> None:
        changed = self._evaluate_room(area_id, now, only=zone_ids)
        self._sync_holds()
        if changed:
            self._on_change(area_id, changed)

    # ------------------------------------------------------------ evaluation
    def _evaluate_room(self, area_id: str, now: datetime, *, only: set[str] | None = None,
                       notify: bool = True) -> set[str]:
        room = self.rooms.get(area_id)
        if room is None:
            return set()
        result = self.ctrl.coordinator.occupancy.result(area_id)
        room_vacant = result is not None and result.state == OccupancyState.VACANT and result.occupied is False
        room_occupied = bool(result and result.occupied)
        changed: set[str] = set()
        for zone_id, zr in room.zones.items():
            if zr.zone.is_radar:
                if self._evaluate_radar(room, zr, now, room_vacant=room_vacant, room_occupied=room_occupied):
                    changed.add(zone_id)
                continue
            if only is None or zone_id in only:
                zr.result = zr.engine.evaluate(now)
                self._schedule(zr, now)
            r = zr.result
            if r is None:
                continue
            if r.state == OccupancyState.CONFIRMED_OCCUPANCY:
                zr.confirmed = True
            elif r.occupied is False:
                zr.confirmed = False
            failsafe = None
            if r.state == OccupancyState.UNCERTAIN:
                failsafe = zr.zone.unavailable.value
                if zr.zone.unavailable == ZoneUnavailable.VACANT:
                    active = False
                elif zr.zone.unavailable == ZoneUnavailable.OCCUPIED:
                    active = room_occupied
                else:
                    active = zr.active
            else:
                active = bool(r.occupied) and (zr.zone.activation == ZoneActivation.POSSIBLE or zr.confirmed)
            if room_vacant:
                active = False
            if failsafe != zr.failsafe:
                self._history(room, zr, "unavailable" if failsafe else "available", now, failsafe=failsafe)
                zr.failsafe = failsafe
            if active != zr.active:
                zr.active = active
                zr.active_since = now if active else None
                changed.add(zone_id)
                self._history(room, zr, "occupied" if active else "vacant", now, state=r.state.value,
                              reason="room vacant" if room_vacant and not active else None)
                _LOGGER.debug("zone_transition area=%s zone=%s active=%s state=%s", area_id, zone_id, active, r.state)
        if changed and notify:
            self.ctrl.mark_dirty()
        return changed

    def _evaluate_radar(self, room: _RoomZones, zr: ZoneRuntime, now: datetime, *, room_vacant: bool,
                        room_occupied: bool) -> bool:
        """One evaluation of a Radar Distance Zone; True when its active state changed."""
        cfg = zr.zone.radar
        if cfg is None or zr.radar is None:
            active, zr.radar_eval = False, Evaluation(False, "inactive", "radar entities not chosen", known=False)
        else:
            zr.presence = presence_of(self.hass.states.get(cfg.presence))
            zr.reading = read_distance(cfg, self.hass.states.get(cfg.distance), now)
            zr.radar_eval = zr.radar.evaluate(zr.presence, zr.reading, now, failsafe=zr.zone.unavailable.value,
                                              room_occupied=room_occupied)
            active = zr.radar_eval.active
            if cancel := self._timers.pop(zr.key, None):
                cancel()
            due = zr.radar_eval.due
            if due is None and cfg.max_age and zr.reading.valid:
                due = now + timedelta(seconds=cfg.max_age)  # (the reading goes stale then)
            if due is not None and due > now and self._running:
                self._timers[zr.key] = async_track_point_in_utc_time(self.hass, self._timer(zr.key), due)
        if room_vacant:
            active = False
            if zr.radar is not None and zr.radar.phase != "inactive":
                zr.radar.phase, zr.radar.since = "inactive", now
        failsafe = zr.radar_eval.failsafe
        if failsafe != zr.failsafe:
            self._history(room, zr, "unavailable" if failsafe else "available", now, failsafe=failsafe)
            zr.failsafe = failsafe
        if active == zr.active:
            return False
        zr.active = active
        zr.active_since = now if active else None
        reading = zr.reading
        self._history(room, zr, "occupied" if active else "vacant", now, state=zr.radar_eval.phase,
                      reason="room vacant" if room_vacant and not active else zr.radar_eval.reason,
                      distance=reading.metres if reading and reading.valid else None)
        _LOGGER.debug("radar_zone_transition area=%s zone=%s active=%s reason=%s", zr.area_id, zr.zone.id, active,
                      zr.radar_eval.reason)
        return True

    def _schedule(self, zr: ZoneRuntime, now: datetime) -> None:
        if cancel := self._timers.pop(zr.key, None):
            cancel()
        when = zr.engine.next_deadline(now)
        if when is not None and when > now and self._running:
            self._timers[zr.key] = async_track_point_in_utc_time(self.hass, self._timer(zr.key), when)

    def _sync_holds(self) -> None:
        occupancy = self.ctrl.coordinator.occupancy
        for area_id, room in self.rooms.items():
            names = frozenset(z.zone.name for z in room.zones.values() if z.active and z.zone.holds_room and z.known)
            occupancy.async_set_holds(area_id, names)

    @staticmethod
    def _history(room: _RoomZones, zr: ZoneRuntime, event: str, now: datetime, **data: Any) -> None:
        room.history.append({"at": _iso(now), "zone": zr.zone.id, "name": zr.zone.name, "event": event,
                             **{k: v for k, v in data.items() if v is not None}})

    # ------------------------------------------------------------------ read
    def active(self, area_id: str) -> list[ZoneRuntime]:
        """Active zones of a room, the winning one first (higher priority; then configuration order)."""
        room = self.rooms.get(area_id)
        if room is None:
            return []
        order = {z.id: i for i, z in enumerate(self.ctrl.lighting.room(area_id).zones)}
        return sorted((z for z in room.zones.values() if z.active),
                      key=lambda z: (-z.zone.priority, order.get(z.zone.id, 99)))

    def zone(self, area_id: str, zone_id: str) -> ZoneRuntime | None:
        return self.rooms.get(area_id, _RoomZones()).zones.get(zone_id)

    def status(self, area_id: str) -> list[dict[str, Any]]:
        room = self.rooms.get(area_id)
        return [z.status(self.hass) for z in room.zones.values()] if room else []

    def history(self, area_id: str) -> list[dict[str, Any]]:
        """Zone transitions of a room, oldest first (observed from sensor states only; never inferred)."""
        room = self.rooms.get(area_id)
        return list(room.history) if room else []

    def diagnostics(self) -> dict[str, Any]:
        return {a: {"zones": self.status(a), "history": self.history(a)[-10:],
                    "holds": sorted(z.zone.name for z in r.zones.values() if z.active and z.zone.holds_room and z.known)}
                for a, r in self.rooms.items()}
