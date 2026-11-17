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

Published result (Phase A3.2): ``result()`` is the one occupancy-result
interface of every consumer. It returns the integration engine's result,
unless the Smart Lighting App owns occupancy (``engine_bridge``) and has
published the room: then it is the App's accepted result. Transitions, the
history, the presence log, the helper comparison and the listeners follow the
published result. The integration's engine keeps running in every case (hot
standby), so a fallback is continuous.
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
from .engine import RoomEngine, RoomResult, StateError, Value, interpret
from .engine_bridge import EngineBridge
from .models import IntegrationConfig
from .occupancy_core.house import House, HouseSpec, Observation, RoomSpec
from .occupancy_core.wire import result_to_wire
from .occupancy import friendly_name
from .prediction import NullHistory, OccupancyHistory

_LOGGER = logging.getLogger(__name__)


def _reference_value(state: State | None) -> bool | None:
    value = interpret(state.state if state else None)
    return None if value == Value.UNKNOWN else value == Value.ACTIVE


def house_spec(config: IntegrationConfig) -> HouseSpec:
    """The engines' configuration: every enabled room's signals, effective timing and adjacent rooms."""
    return HouseSpec(tuple(
        RoomSpec(area_id, room.sensors, config.timing_for(area_id), room.adjacent_rooms)
        for area_id, room in config.enabled_rooms.items()
    ))


class OccupancyManager:
    """Owns the room engines, their timers and their persisted state."""

    def __init__(
        self, hass: HomeAssistant, config: IntegrationConfig, history: OccupancyHistory | None = None
    ) -> None:
        self.hass = hass
        self.config = config
        self.history: OccupancyHistory = history or NullHistory()
        # The engines and the routing between them: the shared engine package (Phase A3.2).
        self.house = House(house_spec(config))
        self.engines: dict[str, RoomEngine] = self.house.engines
        self.comparisons: dict[str, ComparisonTracker] = {}
        self._reference_rooms: dict[str, list[str]] = {}
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._store: Store[dict[str, Any]] = Store(hass, OCCUPANCY_STORE_VERSION, OCCUPANCY_STORE_KEY)
        self._dirty = False
        self._running = False
        # Called with (area_id, previous, result, now) on every occupancy transition (0.6.10: presence log).
        self.on_transition: Callable[[str, RoomResult | None, RoomResult, datetime], None] | None = None
        # The last published result of each room (transitions are detected on it).
        self._published: dict[str, RoomResult] = {}
        self.spec_revision = self.house.spec.revision()
        # Who publishes occupancy, and the App's bridge sessions (Phase A3.2).
        self.bridge = EngineBridge(hass, self)

        for area_id, room in config.enabled_rooms.items():
            if room.reference_occupancy_entity:
                engine = self.engines[area_id]
                self.comparisons[area_id] = ComparisonTracker(grace_seconds=engine.timing.correlation_window)
                self._reference_rooms.setdefault(room.reference_occupancy_entity, []).append(area_id)

    # ------------------------------------------------------------ lifecycle
    @property
    def tracked_entities(self) -> list[str]:
        return sorted(set(self.house.signal_entities) | set(self._reference_rooms))

    async def async_load(self) -> None:
        """Restore the previous decisions and read the current signal states."""
        data = await self._store.async_load() or {}
        stored = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        now = dt_util.utcnow()
        self.house.load(
            {a: d["engine"] for a, d in stored.items() if isinstance(d, dict) and isinstance(d.get("engine"), dict)},
            now, self._lookup, lambda entity_id: friendly_name(self.hass, entity_id),
        )
        for area_id, engine in self.engines.items():
            if area_id in self.comparisons:
                self.comparisons[area_id] = ComparisonTracker.restore(
                    (stored.get(area_id) or {}).get("comparison"), engine.timing.correlation_window
                )
        await self.bridge.async_load()
        self.bridge.restore_decisions(now)

    def _lookup(self, entity_id: str) -> Observation | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return Observation(state.state, state.last_changed, state.attributes.get("friendly_name"))

    @callback
    def async_start(self) -> None:
        self._running = True
        self.bridge.async_start()
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
        self.house.resync(dt_util.utcnow(), self._lookup)

    @callback
    def async_reconfigure(self, config: IntegrationConfig) -> None:
        """Apply changed presence settings (signals enabled or excluded, timing) without a reload (0.3.8).

        Only the settings of signals already assigned change here: the set of
        rooms and of assigned entities is the same (anything else reloads the
        integration). Every room is evaluated again at once.
        """
        self.config = config
        self.house.reconfigure(house_spec(config))
        self.spec_revision = self.house.spec.revision()
        self.bridge.spec_changed()
        for area_id, engine in self.engines.items():
            if (tracker := self.comparisons.get(area_id)) is not None:
                tracker.grace_seconds = engine.timing.correlation_window
        self._resync()
        if self._running:
            now = dt_util.utcnow()
            for area_id in self.engines:
                self._evaluate(area_id, now)

    @callback
    def async_set_preliminary(self, signals: dict[str, frozenset[str]]) -> None:
        """Signals that start possible occupancy lighting, per room: they cannot confirm occupancy alone (0.3.11).

        Rooms without active preliminary lighting get an empty set (the
        engine's rules unchanged). A change is evaluated at once.
        """
        now = dt_util.utcnow()
        self.bridge.input_preliminary(signals, now)
        for area_id in self.house.set_preliminary(signals):
            _LOGGER.debug("occupancy_preliminary_signals area=%s signals=%s", area_id,
                          ",".join(sorted(self.engines[area_id].preliminary)))
            if self._running:
                self._evaluate(area_id, now)

    @callback
    def async_set_holds(self, area_id: str, names: frozenset[str]) -> None:
        """Occupied zones that keep a room occupied (0.6.0). A change is evaluated at once."""
        now = dt_util.utcnow()
        if area_id in self.engines:
            self.bridge.input_hold(area_id, frozenset(names), now)
        if self.house.set_holds(area_id, names, now):
            _LOGGER.debug("occupancy_zone_hold area=%s zones=%s", area_id, ",".join(sorted(names)))
            if self._running:
                self._evaluate(area_id, now)

    def same_signals(self, config: IntegrationConfig) -> bool:
        """The rooms and assigned signals of ``config`` are the ones this manager runs."""
        return self.house.spec.same_signals(house_spec(config))

    @callback
    def async_stop(self) -> None:
        self._running = False
        self.bridge.async_stop()
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
        await self.bridge.async_flush()

    async def async_remove(self) -> None:
        await self._store.async_remove()
        await self.bridge.async_remove()

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
        if area_id not in self.engines or not self._running:
            return False
        self.bridge.input_activity(area_id, source, dt_util.utcnow())
        if not self.house.note_activity(area_id, dt_util.utcnow(), source):
            return False
        self._evaluate(area_id, dt_util.utcnow())
        return True

    def result(self, area_id: str) -> RoomResult | None:
        """The published occupancy of a room (see the module docstring)."""
        if (accepted := self.bridge.published(area_id)) is not None:
            return accepted.result
        engine = self.engines.get(area_id)
        return engine.result if engine else None

    def published_by(self, area_id: str) -> str:
        """Which engine decided the published result: "integration" or "app"."""
        return self.bridge.published_by(area_id)

    def local_wires(self) -> dict[str, dict[str, Any]]:
        """The integration engine's current results in their JSON form (for the bridge)."""
        return {a: result_to_wire(e.result, e.visit_confirmed) for a, e in self.engines.items() if e.result}

    def published_wire(self, area_id: str) -> dict[str, Any] | None:
        if (accepted := self.bridge.published(area_id)) is not None:
            return accepted.wire
        engine = self.engines.get(area_id)
        return result_to_wire(engine.result, engine.visit_confirmed) if engine and engine.result else None

    def published_key(self, area_id: str) -> tuple[str, dict[str, Any] | None]:
        """What the listeners see: the publisher and the published result (a change of either is notified)."""
        return self.published_by(area_id), self.published_wire(area_id)

    def comparison(self, area_id: str) -> ComparisonTracker | None:
        return self.comparisons.get(area_id)

    # --------------------------------------------------------------- events
    @callback
    def _on_state_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        now = dt_util.utcnow()
        obs = (Observation(new_state.state, new_state.last_changed, new_state.attributes.get("friendly_name"))
               if new_state else None)
        affected = dict.fromkeys(self.house.observe(entity_id, obs, now))
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
        evaluation = self.house.evaluate(area_id, now)
        result = evaluation.result
        if evaluation.transition:
            _LOGGER.debug(
                "occupancy_transition area=%s state=%s occupied=%s reason=%s",
                area_id, result.state, result.occupied, result.reason_code,
            )
            self._mark_dirty()
        self.bridge.local_evaluated(area_id, now)
        app_published = self.bridge.published(area_id) is not None
        if app_published:
            self._compare(area_id, now, self.result(area_id))
        else:
            self._on_published(area_id, now)
        self._reschedule(area_id, now)
        if notify and not app_published:
            self._notify(area_id)

    @callback
    def _on_published(self, area_id: str, now: datetime) -> None:
        """The published result may have changed: transitions, history, presence log, helper comparison."""
        result = self.result(area_id)
        if result is None:
            return
        previous = self._published.get(area_id)
        self._published[area_id] = result
        if previous is None or (previous.state, previous.occupied) != (result.state, result.occupied):
            self.history.record(area_id, result.state, result.occupied, now)
            if self.on_transition is not None:
                try:
                    self.on_transition(area_id, previous, result, now)
                except Exception:  # noqa: BLE001 - the log must never break occupancy
                    _LOGGER.exception("Presence log failed")
        self._compare(area_id, now, result)

    @callback
    def _compare(self, area_id: str, now: datetime, result: RoomResult | None) -> None:
        if (tracker := self.comparisons.get(area_id)) is None or result is None:
            return
        room = self.config.rooms[area_id]
        helper = _reference_value(self.hass.states.get(room.reference_occupancy_entity or ""))
        if tracker.update(now, result.occupied, helper, result.last_activity):
            self._mark_dirty()

    @callback
    def _reschedule(self, area_id: str, now: datetime) -> None:
        tracker = self.comparisons.get(area_id)
        deadlines = [d for d in (self.engines[area_id].next_deadline(now), tracker.next_deadline() if tracker else None)
                     if d is not None and d > now]
        self._schedule(area_id, min(deadlines) if deadlines else None)

    @callback
    def _notify(self, area_id: str) -> None:
        for update in list(self._listeners.get(area_id, ())):
            update()

    # ------------------------------------------------------- bridge (A3.2)
    @callback
    def async_published_changed(self, area_id: str, now: datetime, before: tuple[str, Any]) -> None:
        """The App's accepted result of a room changed (the bridge calls this)."""
        self._on_published(area_id, now)
        self._reschedule(area_id, now)
        if self.published_key(area_id) != before:
            self._notify(area_id)

    @callback
    def async_take_over(self, decisions: dict[str, dict[str, Any]], before: dict[str, tuple[str, Any]],
                        now: datetime) -> None:
        """The integration's engine publishes again (fallback): reconcile, evaluate, notify changed rooms only."""
        for area_id, decision in decisions.items():
            engine = self.engines.get(area_id)
            if engine is None:
                continue
            try:
                if engine.adopt(decision, now):
                    _LOGGER.info("occupancy_adopted area=%s state=%s occupied=%s (the App's last decision)",
                                 area_id, decision.get("state"), decision.get("occupied"))
                    self._mark_dirty()
            except StateError as err:
                _LOGGER.warning("occupancy_adopt_refused area=%s error=%s", area_id, err)
        if not self._running:
            return
        for area_id in self.engines:
            self._evaluate(area_id, now, notify=False)
            if self.published_key(area_id) != before.get(area_id):
                self._notify(area_id)

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
                "preliminary_signals": sorted(engine.preliminary),
                "zone_holds": sorted(engine.external_hold),
                "adjacent_rooms": list(engine.adjacent),
                "state": result.state.value if result else None,
                "occupied": result.occupied if result else None,
                "reason": result.reason if result else None,
                "signals": [v.as_dict() for v in result.signals] if result else [],
                "next_deadline": (d.isoformat() if (d := engine.next_deadline(now)) else None),
                "timer_scheduled": area_id in self._timers,
                "comparison": c.as_dict() if (c := self.comparisons.get(area_id)) else None,
                "published_by": self.published_by(area_id),
            }
        return out
