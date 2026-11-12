"""Runs one occupancy engine per enabled room inside Home Assistant.

Event driven:

* one state-change subscription covers every assigned signal and every
  reference helper of every room (no polling, no per-sensor loop);
* each room has at most one scheduled callback, set to the next moment its
  evaluation can change by itself (dropout grace, correlation window, stuck
  detection, vacancy confirmation, start-up hold);
* entities are notified after each evaluation and write their state only if
  it actually changed.

Engine state (occupied, state, last activity) and comparison counters are
kept in ``.storage/smart_occupancy.occupancy_state``, written only on
occupancy transitions and counter changes, coalesced by a delay. Nothing is
written to the Recorder directly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .comparison import ComparisonTracker
from .const import (
    OCCUPANCY_SAVE_DELAY_SECONDS,
    OCCUPANCY_STORE_KEY,
    OCCUPANCY_STORE_VERSION,
)
from .engine import RoomEngine, RoomResult, Value, interpret
from .models import IntegrationConfig
from .occupancy import friendly_name
from .prediction import NullHistory, OccupancyHistory

_LOGGER = logging.getLogger(__name__)


def _reference_value(state: State | None) -> bool | None:
    value = interpret(state.state if state else None)
    return None if value == Value.UNKNOWN else value == Value.ACTIVE


class OccupancyManager:
    """Owns the room engines, their timers and their persisted state."""

    def __init__(
        self, hass: HomeAssistant, config: IntegrationConfig, history: OccupancyHistory | None = None
    ) -> None:
        self.hass = hass
        self.config = config
        self.history: OccupancyHistory = history or NullHistory()
        self.engines: dict[str, RoomEngine] = {}
        self.comparisons: dict[str, ComparisonTracker] = {}
        self._entity_rooms: dict[str, list[str]] = {}
        self._reference_rooms: dict[str, list[str]] = {}
        self._neighbours_of: dict[str, list[str]] = {}  # area -> rooms that list it as adjacent
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._store: Store[dict[str, Any]] = Store(hass, OCCUPANCY_STORE_VERSION, OCCUPANCY_STORE_KEY)
        self._dirty = False
        self._running = False

        for area_id, room in config.enabled_rooms.items():
            sensors = room.sensors
            engine = RoomEngine(area_id, sensors, config.timing_for(area_id), adjacent=room.adjacent_rooms)
            self.engines[area_id] = engine
            for s in sensors:
                self._entity_rooms.setdefault(s.entity_id, []).append(area_id)
            for neighbour in room.adjacent_rooms:
                self._neighbours_of.setdefault(neighbour, []).append(area_id)
            if room.reference_occupancy_entity:
                self.comparisons[area_id] = ComparisonTracker(grace_seconds=engine.timing.correlation_window)
                self._reference_rooms.setdefault(room.reference_occupancy_entity, []).append(area_id)

    # ------------------------------------------------------------ lifecycle
    @property
    def tracked_entities(self) -> list[str]:
        return sorted(set(self._entity_rooms) | set(self._reference_rooms))

    async def async_load(self) -> None:
        """Restore the previous decisions and read the current signal states."""
        data = await self._store.async_load() or {}
        stored = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        now = dt_util.utcnow()
        for area_id, engine in self.engines.items():
            room_data = stored.get(area_id) or {}
            if isinstance(room_data.get("engine"), dict):
                engine.restore(room_data["engine"], now)
            else:
                engine.start(now)
            if area_id in self.comparisons:
                self.comparisons[area_id] = ComparisonTracker.restore(
                    room_data.get("comparison"), engine.timing.correlation_window
                )
            for entity_id, track in engine.tracks.items():
                state = self.hass.states.get(entity_id)
                track.name = friendly_name(self.hass, entity_id)
                at = min(state.last_changed, now) if state else now
                engine.observe(
                    entity_id, interpret(state.state if state else None, inverted=track.assignment.inverted), at
                )

    @callback
    def async_start(self) -> None:
        self._running = True
        if tracked := self.tracked_entities:
            self._unsub = async_track_state_change_event(self.hass, tracked, self._on_state_event)
        # The states were read in async_load, before this subscription existed: a signal that changed in
        # between (common while Home Assistant is starting, e.g. a PC bridge connecting) would otherwise stay
        # at its old value - "unknown" for a sensor that was still unavailable - until it changed again.
        self._resync()
        now = dt_util.utcnow()
        for area_id in self.engines:
            self._evaluate(area_id, now, notify=False)
        _LOGGER.debug("occupancy_start rooms=%d tracked=%d", len(self.engines), len(self.tracked_entities))

    @callback
    def _resync(self) -> None:
        """Read the current state of every signal again (observations that are not newer are ignored)."""
        now = dt_util.utcnow()
        for engine in self.engines.values():
            for entity_id, track in engine.tracks.items():
                state = self.hass.states.get(entity_id)
                value = interpret(state.state if state else None, inverted=track.assignment.inverted)
                if value == track.value:
                    continue
                at = min(state.last_changed, now) if state else now
                if track.since is not None and at < track.since:
                    at = now  # the current state is newer than what the engine holds, whatever its timestamp
                name = state.attributes.get("friendly_name") if state else None
                engine.observe(entity_id, value, at, name)

    @callback
    def async_reconfigure(self, config: IntegrationConfig) -> None:
        """Apply changed presence settings (signals enabled or excluded, timing) without a reload (0.3.8).

        Only the settings of signals already assigned change here: the set of
        rooms and of assigned entities is the same (anything else reloads the
        integration). Every room is evaluated again at once.
        """
        self.config = config
        for area_id, engine in self.engines.items():
            room = config.room(area_id)
            engine.reconfigure(room.sensors, config.timing_for(area_id))
            if (tracker := self.comparisons.get(area_id)) is not None:
                tracker.grace_seconds = engine.timing.correlation_window
        self._resync()
        if self._running:
            now = dt_util.utcnow()
            for area_id in self.engines:
                self._evaluate(area_id, now)

    def same_signals(self, config: IntegrationConfig) -> bool:
        """The rooms and assigned signals of ``config`` are the ones this manager runs."""
        if set(config.enabled_rooms) != set(self.engines):
            return False
        return all(
            [s.entity_id for s in config.room(a).sensors] == list(engine.tracks)
            and [(s.entity_id, s.kind) for s in config.room(a).sensors]
            == [(t.entity_id, t.kind) for t in engine.tracks.values()]
            for a, engine in self.engines.items()
        )

    @callback
    def async_stop(self) -> None:
        self._running = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        self._listeners.clear()

    async def async_flush(self) -> None:
        if self._dirty:
            await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    # ------------------------------------------------------------ listeners
    @callback
    def async_add_listener(self, area_id: str, update: Callable[[], None]) -> CALLBACK_TYPE:
        listeners = self._listeners.setdefault(area_id, [])
        listeners.append(update)

        @callback
        def remove() -> None:
            if update in listeners:
                listeners.remove(update)

        return remove

    @callback
    def async_note_activity(self, area_id: str, source: str) -> bool:
        """Refresh the activity of an occupied room (optional wall switch evidence). Never occupies a room."""
        engine = self.engines.get(area_id)
        if engine is None or not self._running:
            return False
        if not engine.note_activity_evidence(dt_util.utcnow(), source):
            return False
        self._evaluate(area_id, dt_util.utcnow())
        return True

    def result(self, area_id: str) -> RoomResult | None:
        engine = self.engines.get(area_id)
        return engine.result if engine else None

    def comparison(self, area_id: str) -> ComparisonTracker | None:
        return self.comparisons.get(area_id)

    # --------------------------------------------------------------- events
    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        now = dt_util.utcnow()
        at = min(new_state.last_changed, now) if new_state else now
        name = new_state.attributes.get("friendly_name") if new_state else None
        affected: dict[str, None] = {}
        for area_id in self._entity_rooms.get(entity_id, ()):
            engine = self.engines[area_id]
            track = engine.tracks[entity_id]
            before = track.active_start
            value = interpret(new_state.state if new_state else None, inverted=track.assignment.inverted)
            if not engine.observe(entity_id, value, at, name):
                if name and track.name != name:
                    track.name = name
                    affected[area_id] = None
                continue
            affected[area_id] = None
            if track.capable and track.active_start is not None and track.active_start != before:
                for other in self._neighbours_of.get(area_id, ()):
                    if other in self.engines:
                        self.engines[other].note_neighbour_motion(area_id, at)
                        affected[other] = None
        for area_id in self._reference_rooms.get(entity_id, ()):
            affected[area_id] = None
        for area_id in affected:
            self._evaluate(area_id, now)

    @callback
    def _on_timer(self, area_id: str) -> Callable[[datetime], None]:
        @callback
        def fire(_now: datetime) -> None:
            self._timers.pop(area_id, None)
            if self._running:
                self._evaluate(area_id, dt_util.utcnow())

        return fire

    # ------------------------------------------------------------- evaluate
    @callback
    def _evaluate(self, area_id: str, now: datetime, *, notify: bool = True) -> None:
        engine = self.engines[area_id]
        previous = engine.result
        result = engine.evaluate(now)
        if previous is None or (previous.state, previous.occupied) != (result.state, result.occupied):
            _LOGGER.debug(
                "occupancy_transition area=%s state=%s occupied=%s reason=%s",
                area_id, result.state, result.occupied, result.reason_code,
            )
            self.history.record(area_id, result.state, result.occupied, now)
            self._mark_dirty()
        if (tracker := self.comparisons.get(area_id)) is not None:
            room = self.config.rooms[area_id]
            helper = _reference_value(self.hass.states.get(room.reference_occupancy_entity or ""))
            if tracker.update(now, result.occupied, helper, result.last_activity):
                self._mark_dirty()
        deadlines = [d for d in (engine.next_deadline(now), tracker.next_deadline() if tracker else None)
                     if d is not None and d > now]
        self._schedule(area_id, min(deadlines) if deadlines else None)
        if notify:
            for update in list(self._listeners.get(area_id, ())):
                update()

    @callback
    def _schedule(self, area_id: str, when: datetime | None) -> None:
        if cancel := self._timers.pop(area_id, None):
            cancel()
        if when is not None and self._running:
            self._timers[area_id] = async_track_point_in_utc_time(self.hass, self._on_timer(area_id), when)

    # ---------------------------------------------------------- persistence
    @callback
    def _mark_dirty(self) -> None:
        self._dirty = True
        self._store.async_delay_save(self._data, OCCUPANCY_SAVE_DELAY_SECONDS)

    def _data(self) -> dict[str, Any]:
        self._dirty = False
        return {
            "rooms": {
                area_id: {
                    "engine": engine.persist(),
                    "comparison": c.persist() if (c := self.comparisons.get(area_id)) else None,
                }
                for area_id, engine in self.engines.items()
            }
        }

    def diagnostics(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        out: dict[str, Any] = {}
        for area_id, engine in self.engines.items():
            result = engine.result
            out[area_id] = {
                "timing": engine.timing.as_dict(),
                "adjacent_rooms": list(engine.adjacent),
                "state": result.state.value if result else None,
                "occupied": result.occupied if result else None,
                "reason": result.reason if result else None,
                "signals": [v.as_dict() for v in result.signals] if result else [],
                "next_deadline": (d.isoformat() if (d := engine.next_deadline(now)) else None),
                "timer_scheduled": area_id in self._timers,
                "comparison": c.as_dict() if (c := self.comparisons.get(area_id)) else None,
            }
        return out
