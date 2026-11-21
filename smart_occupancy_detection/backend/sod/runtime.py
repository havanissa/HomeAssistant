"""The occupancy runtime: the engine (``sod_engine``) connected to Home Assistant, event driven.

* **Events** - Home Assistant's ``state_changed`` events at their
  ``last_changed``, one asyncio timer per room at its engine's next deadline.
  Nothing is polled.
* **Start** - the previous decisions (``/data/state/occupancy.json``) are
  restored with the engine's restart semantics once Home Assistant's states are
  known: an occupied room stays occupied, its quiet period starts again and no
  room is vacated during the start-up hold.
* **Connection loss** - while the connection to Home Assistant is down, the
  timers are frozen (no room is vacated on old knowledge). When it is back,
  every room gets the restart semantics again (``House.hold_all``) and every
  sensor is read again: nothing is known about the gap, so it can never cause
  vacancy.
* **Configuration changes** - applied live (``House.rebuild``): a room whose
  sensors stayed the same keeps everything its engine knows; a room with
  changed sensors keeps its decision and starts like after a restart.
* **Activity entities** - a change of a room's activity entity (a wall switch,
  a remote) refreshes an occupied room's quiet period; it never occupies a room.

The runtime reads Home Assistant through the allowlisted client (``ha.py``),
which cannot call a service: it decides occupancy, it controls nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import copy
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from sod_engine import ENGINE_VERSION, golden
from sod_engine.engine import RoomResult, SignalStatus
from sod_engine.house import House, HouseSpec, Observation
from sod_engine.types import OccupancyState

from . import config as C
from .ha import HomeAssistantClient
from .timeline import SAVE_DELAY as TIMELINE_SAVE_DELAY, Timeline

_LOGGER = logging.getLogger(__name__)

STATE_SCHEMA = 1
SAVE_DELAY_TRANSITION = 2.0
SAVE_DELAY_ACTIVITY = 60.0
NEIGHBOUR_NOTICE_SECONDS = 120
UNKNOWN_STATES = frozenset({"unknown", "unavailable", "none", ""})


def utcnow() -> datetime:
    return datetime.now(UTC)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="seconds") if value else None


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        os.replace(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)


def status_of(result: RoomResult | None) -> str:
    """The simple status the interface shows: occupied, vacant or uncertain."""
    if result is None or result.state == OccupancyState.UNCERTAIN:
        return "uncertain"
    return "occupied" if result.occupied else "vacant"


DETAIL = {
    "vacant": "Vacant",
    "possible_occupancy": "Possible occupancy",
    "confirmed_occupancy": "Occupied",
    "possible_vacancy": "Vacancy pending",
    "uncertain": "Uncertain",
}
SIGNAL_LABELS = {
    "active": "Active", "grace": "Just cleared (dropout grace)", "cleared": "Clear", "recent": "Moved just now",
    "idle": "Idle", "stuck": "Stuck (ignored)", "unknown": "Unavailable", "ignored": "Not used",
}


class OccupancyRuntime:
    def __init__(self, ha: HomeAssistantClient, store: C.ConfigStore, data_dir: Path | None) -> None:
        self.ha = ha
        self.store = store
        self.dir = data_dir / "state" if data_dir else None
        self.house: House | None = None
        self.timeline = Timeline(self.dir / "timeline.json" if self.dir else None)
        self.self_test = golden.self_test()
        self.started_at = utcnow()
        self.connected_at: datetime | None = None
        self.suspended = True  # until Home Assistant's states are known
        self.stored_state: dict[str, Any] = {}
        self.state_notice: str | None = None
        self.counters = {"events": 0, "evaluations": 0, "transitions": 0, "state_saves": 0}
        self._activity: dict[str, list[str]] = {}
        self._activity_seen: dict[str, str | None] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self.deadlines: dict[str, datetime] = {}  # when each room is evaluated again by its timer
        #: Why each room is held (no vacancy confirmed): "start", "reconnect" or "sensors_changed".
        self.hold_reason: dict[str, str] = {}
        self._signal_status: dict[str, dict[str, str]] = {}
        self._listeners: list[Callable[[list[str]], None]] = []
        self._save_handle: asyncio.TimerHandle | None = None
        self._save_at: float | None = None
        self._timeline_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._write_lock = asyncio.Lock()
        self._stopping = False
        if not self.self_test["passed"]:
            _LOGGER.error("The engine self-test failed: %s (nothing will be published)", self.self_test["failed"])

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._load_files)
        self.ha.add_listener(self._on_ha)
        self.store.add_listener(self._config_changed)
        self._activity = C.activity_entities(self.store.config)
        if self.ha.connected:
            self._connected()

    async def stop(self) -> None:
        self._stopping = True
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        for handle in (self._save_handle, self._timeline_handle):
            if handle:
                handle.cancel()
        try:
            await self._save_state()
            await asyncio.to_thread(self.timeline.write, self.timeline.snapshot())
        except OSError as err:
            _LOGGER.warning("Could not store the occupancy state at shutdown: %s", err)

    def add_listener(self, listener: Callable[[list[str]], None]) -> None:
        """``listener(area_ids)`` after the view of these rooms may have changed."""
        self._listeners.append(listener)

    def _changed(self, area_ids: list[str]) -> None:
        for listener in list(self._listeners):
            try:
                listener(area_ids)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("View listener failed")

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -------------------------------------------------------------- storage
    def _load_files(self) -> None:
        self.timeline.load()
        if self.dir is None:
            return
        for name in ("occupancy.json", "occupancy.json.bak"):
            path = self.dir / name
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
                    raise ValueError(f"schema {data.get('schema') if isinstance(data, dict) else None!r}")
                rooms = data.get("rooms")
                if not isinstance(rooms, dict):
                    raise ValueError("rooms missing")
            except (OSError, ValueError) as err:
                self.state_notice = f"The stored occupancy state ({name}) was not usable: {err}"
                _LOGGER.warning("%s", self.state_notice)
                continue
            self.stored_state = {a: r for a, r in rooms.items() if _valid_record(r)}
            if len(self.stored_state) != len(rooms):
                self.state_notice = f"{len(rooms) - len(self.stored_state)} stored room decision(s) were not usable"
            if name != "occupancy.json":
                self.state_notice = (self.state_notice or "") + " The previous copy was used."
            return

    def _state_document(self) -> dict[str, Any] | None:
        if self.house is None:
            return None
        rooms = {**self.stored_state, **self.house.persist()}
        rooms = {a: r for a, r in rooms.items() if a in (self.store.config.get("rooms") or {})}
        return {"schema": STATE_SCHEMA, "engine_version": ENGINE_VERSION, "saved_at": _iso(utcnow()),
                "config_revision": self.store.revision, "rooms": rooms}

    def _write_state(self, document: dict[str, Any] | None) -> None:
        if document is None or self.dir is None:
            return
        _atomic_write(self.dir / "occupancy.json", document)
        self.counters["state_saves"] += 1

    def _save_soon(self, delay: float) -> None:
        if self.dir is None or self._stopping:
            return
        loop = asyncio.get_running_loop()
        due = loop.time() + delay
        if self._save_handle is not None and self._save_at is not None and self._save_at <= due:
            return
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._save_at = due
        self._save_handle = loop.call_later(delay, lambda: self._spawn(self._save_state()))

    async def _save_state(self) -> None:
        """One write at a time; the document is built on the event loop (never while the engines change)."""
        if self._save_handle is not None:
            self._save_handle.cancel()
        self._save_handle = self._save_at = None
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._write_state, self._state_document())
            except OSError as err:
                _LOGGER.warning("Could not store the occupancy state: %s", err)

    def _timeline_soon(self) -> None:
        if self._timeline_handle is None and not self._stopping and self.timeline.path is not None:
            async def save() -> None:
                self._timeline_handle = None
                try:
                    await asyncio.to_thread(self.timeline.write, self.timeline.snapshot())
                except OSError as err:
                    _LOGGER.warning("Could not store the timeline: %s", err)

            self._timeline_handle = asyncio.get_running_loop().call_later(
                TIMELINE_SAVE_DELAY, lambda: self._spawn(save()))

    def _activity_key(self, entity_id: str) -> tuple[str | None, str]:
        state = self.ha.states.get(entity_id) or {}
        return state.get("last_changed"), str(state.get("state") or "").strip().casefold()

    # ------------------------------------------------------------ HA events
    def _lookup(self, entity_id: str) -> Observation | None:
        state = self.ha.states.get(entity_id)
        if not state:
            return None
        return Observation(state.get("state"), _time(state.get("last_changed")),
                           (state.get("attributes") or {}).get("friendly_name"))

    def _name_of(self, entity_id: str) -> str | None:
        obs = self._lookup(entity_id)
        if obs and obs.name:
            return obs.name
        reg = next((e for e in self.ha.entity_registry if e.get("entity_id") == entity_id), None)
        return (reg or {}).get("name") or (reg or {}).get("original_name")

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            self._on_state(*payload)
        elif kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._disconnected()
        elif kind == "registries":
            self._changed(list(self.house.spec.area_ids) if self.house else [])

    def _connected(self) -> None:
        now = utcnow()
        self.suspended = False
        self.connected_at = now
        spec = C.house_spec(self.store.config)
        if self.house is None:
            house = House(spec)
            stored = {a: r for a, r in self.stored_state.items() if a in spec.area_ids}
            house.load(stored, now, self._lookup, self._name_of)
            self.house = house
            self.hold_reason = {a: "start" for a in spec.area_ids}
            self.timeline.add("system", now, event="started", text=(
                f"Started; {len(stored)} room(s) restored from the previous run" if stored else "Started"))
        else:
            self.house.hold_all(now)
            self.house.resync(now, self._lookup)
            self.hold_reason = {a: "reconnect" for a in spec.area_ids}
            self.timeline.add("system", now, event="reconnected", text=(
                "Connected to Home Assistant again: every sensor was read again and the start-up hold applies "
                "(nothing is known about the interruption)"))
        for entity_id in self._activity:
            self._activity_seen[entity_id] = self._activity_key(entity_id)
        for area_id in spec.area_ids:
            self._evaluate(area_id, now)
        self._timeline_soon()
        self._changed(list(spec.area_ids))

    def _disconnected(self) -> None:
        if self.suspended:
            return
        self.suspended = True
        for handle in self._timers.values():
            handle.cancel()
        self._timers.clear()
        self.deadlines.clear()
        if self.house is not None:
            self.timeline.add("system", utcnow(), event="disconnected", text=(
                "Connection to Home Assistant lost: decisions are frozen until it is back (no room is made vacant "
                "meanwhile)"))
            self._timeline_soon()
            self._spawn(self._save_state())
        self._changed(list(self.house.spec.area_ids) if self.house else [])

    def _on_state(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        house = self.house
        if house is None or self.suspended:
            return
        rooms = house.rooms_of(entity_id)
        activity_rooms = self._activity.get(entity_id, [])
        if not rooms and not activity_rooms:
            return
        now = utcnow()
        self.counters["events"] += 1
        obs = None
        if new_state is not None:
            obs = Observation(new_state.get("state"), _time(new_state.get("last_changed")),
                              (new_state.get("attributes") or {}).get("friendly_name"))
        affected = house.observe(entity_id, obs, now) if rooms else []
        if activity_rooms:
            changed_at = (new_state or {}).get("last_changed")
            previous = self._activity_seen.get(entity_id)
            self._activity_seen[entity_id] = self._activity_key(entity_id)
            state = str((new_state or {}).get("state") or "").strip().casefold()
            # A change between two known states is activity (coming back from unavailable is not).
            if new_state is not None and previous is not None and changed_at != previous[0] and \
                    state not in UNKNOWN_STATES and previous[1] not in UNKNOWN_STATES:
                at = min(_time(changed_at) or now, now)
                source = (obs.name if obs else None) or entity_id
                for area_id in activity_rooms:
                    if house.note_activity(area_id, at, f"activity: {source}") and area_id not in affected:
                        affected.append(area_id)
        for area_id in affected:
            self._evaluate(area_id, now)

    # ---------------------------------------------------------- evaluation
    def _evaluate(self, area_id: str, now: datetime) -> None:
        house = self.house
        if house is None or area_id not in house.engines:
            return
        evaluation = house.evaluate(area_id, now)
        result = evaluation.result
        self.counters["evaluations"] += 1
        self._schedule(area_id, house.next_deadline(area_id, now), now)
        self._note_signals(area_id, result, now)
        if evaluation.transition:
            self.counters["transitions"] += 1
            previous = evaluation.previous
            source = result.last_activity_source
            self.timeline.add(
                "state", now, area_id, state=result.state.value, occupied=result.occupied,
                previous=previous.state.value if previous else None, reason=result.reason,
                reason_code=result.reason_code, source=self.source_name(area_id, source) if source else None)
            self._timeline_soon()
            self._save_soon(SAVE_DELAY_TRANSITION)
        elif evaluation.previous is not None and evaluation.previous.last_activity != result.last_activity:
            self._save_soon(SAVE_DELAY_ACTIVITY)
        self._changed([area_id])

    def _note_signals(self, area_id: str, result: RoomResult, now: datetime) -> None:
        """Sensor health changes (unavailable, stuck, usable again) go to the timeline."""
        before = self._signal_status.get(area_id)
        current: dict[str, str] = {}
        for view in result.signals:
            status = view.status.value
            current[view.track.entity_id] = status
            if before is None:
                continue
            old = before.get(view.track.entity_id)
            bad = (SignalStatus.UNKNOWN.value, SignalStatus.STUCK.value)
            if old == status or (old not in bad and status not in bad):
                continue
            if status == SignalStatus.UNKNOWN.value:
                text = f"{view.track.label()} became unavailable"
            elif status == SignalStatus.STUCK.value:
                text = f"{view.track.label()} is ignored as stuck (active since {_iso(view.track.since)})"
            elif old is not None:
                text = f"{view.track.label()} is usable again"
            else:
                continue
            self.timeline.add("sensor", now, area_id, entity_id=view.track.entity_id, status=status, text=text)
            self._timeline_soon()
        self._signal_status[area_id] = current

    def _schedule(self, area_id: str, when: datetime | None, now: datetime) -> None:
        if handle := self._timers.pop(area_id, None):
            handle.cancel()
        self.deadlines.pop(area_id, None)
        if when is None or self._stopping or self.suspended:
            return
        self.deadlines[area_id] = when
        delay = max(0.0, (when - now).total_seconds())
        self._timers[area_id] = asyncio.get_running_loop().call_later(delay, self._fire, area_id)

    def _fire(self, area_id: str) -> None:
        self._timers.pop(area_id, None)
        self.deadlines.pop(area_id, None)
        if not self.suspended:
            self._evaluate(area_id, utcnow())

    # ------------------------------------------------------- configuration
    def _config_changed(self, reason: str) -> None:
        config = self.store.config
        self._activity = C.activity_entities(config)
        for entity_id in self._activity:
            self._activity_seen.setdefault(entity_id, self._activity_key(entity_id))
        now = utcnow()
        self.timeline.add("config", now, text=f"Configuration changed ({reason})")
        self._timeline_soon()
        if self.house is None:
            return
        spec = C.house_spec(config)
        removed = [a for a in self.house.spec.area_ids if a not in spec.area_ids]
        for area_id in removed:
            if handle := self._timers.pop(area_id, None):
                handle.cancel()
            self.deadlines.pop(area_id, None)
            self._signal_status.pop(area_id, None)
            # The decision is kept (a disabled room that is enabled again starts from it, conservatively).
            if engine := self.house.engines.get(area_id):
                self.stored_state[area_id] = engine.persist()
        old_house = self.house
        stored_for_new = {a: self.stored_state[a] for a in spec.area_ids
                          if a not in old_house.engines and a in self.stored_state}
        house = old_house.rebuild(spec, now, self._lookup, self._name_of)
        for area_id, engine in house.engines.items():
            if engine is not old_house.engines.get(area_id):
                self.hold_reason[area_id] = "sensors_changed" if area_id in old_house.engines else "start"
        for area_id, data in stored_for_new.items():
            house.engines[area_id].restore(data, now)
        self.house = house
        if self.suspended:
            # Nothing is evaluated on old knowledge: the reconnect applies the start-up hold and evaluates.
            self._changed(list(dict.fromkeys([*spec.area_ids, *removed])))
            return
        house.resync(now, self._lookup)
        for area_id in spec.area_ids:
            self._evaluate(area_id, now)
        self._save_soon(SAVE_DELAY_TRANSITION)
        self._changed(list(dict.fromkeys([*spec.area_ids, *removed])))

    def predict(self, new_config: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """What every room would show right after ``new_config`` is applied (a copy is evaluated; nothing changes)."""
        if self.house is None or self.suspended:
            return {}
        now = utcnow()
        spec = C.house_spec(new_config)
        candidate = copy.deepcopy(self.house).rebuild(spec, now, self._lookup, self._name_of)
        for area_id in spec.area_ids:
            if area_id not in self.house.engines and area_id in self.stored_state:
                candidate.engines[area_id].restore(self.stored_state[area_id], now)
        candidate.resync(now, self._lookup)
        out: dict[str, dict[str, Any]] = {}
        for area_id in spec.area_ids:
            after = candidate.evaluate(area_id, now).result
            before = self.house.result(area_id)
            same = self.house.spec.room(area_id)
            new = spec.room(area_id)
            sensors_changed = same is None or new is None or \
                [(s.entity_id, s.kind) for s in same.sensors] != [(s.entity_id, s.kind) for s in new.sensors]
            out[area_id] = {
                "before": _brief(before), "after": _brief(after),
                "changes_now": before is None or (before.state, before.occupied) != (after.state, after.occupied),
                "restarts": sensors_changed and before is not None,
                "vacancy_at": _iso(after.vacancy_at),
            }
        return out

    # --------------------------------------------------------------- views
    def source_name(self, area_id: str, entity_id: str | None) -> str | None:
        if not entity_id:
            return None
        if entity_id.startswith("activity: "):
            return entity_id
        engine = self.house.engines.get(area_id) if self.house else None
        track = engine.tracks.get(entity_id) if engine else None
        return (track.label() if track else None) or self._name_of(entity_id) or entity_id

    def room_name(self, area_id: str) -> str:
        area = next((a for a in self.ha.areas if a.get("area_id") == area_id), None)
        if area and area.get("name"):
            return str(area["name"])[:C.MAX_NAME]
        room = (self.store.config.get("rooms") or {}).get(area_id) or {}
        return str(room.get("name") or area_id)[:C.MAX_NAME]

    def result(self, area_id: str) -> RoomResult | None:
        return self.house.result(area_id) if self.house else None

    def problems(self, area_id: str, result: RoomResult | None = None, now: datetime | None = None) -> list[dict]:
        """Plain-language diagnostics of one room (most important first)."""
        now = now or utcnow()
        result = result or self.result(area_id)
        room = (self.store.config.get("rooms") or {}).get(area_id) or {}
        out: list[dict[str, Any]] = []
        if not room.get("enabled", True):
            return [{"level": "info", "code": "disabled", "text": "Detection is turned off for this room."}]
        if not self.ha.connected:
            out.append({"level": "warning", "code": "ha_disconnected", "text": (
                "Not connected to Home Assistant: the last decision is kept and nothing is made vacant until the "
                "connection is back.")})
        if result is None:
            out.append({"level": "info", "code": "waiting", "text": "Waiting for Home Assistant's sensor states."})
            return out
        if result.reason_code == "no_signals":
            out.append({"level": "error", "code": "no_signals", "sensor": True,
                        "text": "This room has no usable occupancy sensor (a door contact alone cannot detect "
                                "presence)."})
        elif result.reason_code == "all_signals_unknown":
            out.append({"level": "warning", "code": "all_offline", "sensor": True, "text": (
                "Occupancy is uncertain because every sensor is offline; the last state is kept.")})
        for view in result.signals:
            name = view.track.label()
            if view.status == SignalStatus.UNKNOWN:
                if view.track.entity_id not in self.ha.states:
                    text = f"{name} does not exist in Home Assistant (removed or renamed?)."
                elif view.track.kind.value == "radar":
                    text = f"{name} (radar) stopped reporting: it is unavailable."
                else:
                    text = f"{name} is unavailable."
                if view.track.lost_while_active:
                    text += " It was reporting presence, so the room is held occupied longer."
                out.append({"level": "warning", "code": "sensor_unavailable", "sensor": True,
                            "entity_id": view.track.entity_id, "text": text})
            elif view.status == SignalStatus.STUCK:
                active = _words(now - view.track.since) if view.track.since else "too long"
                out.append({"level": "warning", "code": "sensor_stuck", "sensor": True,
                            "entity_id": view.track.entity_id, "at": _iso(view.track.since),
                            "text": f"{name} has been active for {active} and is ignored as stuck."})
        for entity_id in room.get("activity", []):
            if entity_id not in self.ha.states:
                out.append({"level": "warning", "code": "activity_missing", "entity_id": entity_id,
                            "text": f"Activity entity {entity_id} does not exist in Home Assistant."})
        if result.starting_up and self.house is not None:
            until = self.house.engines[area_id]._startup_until
            left = _words(until - now) if until else "a moment"
            reason = self.hold_reason.get(area_id, "start")
            why = {"start": "Start-up hold (the App started)",
                   "reconnect": "Reconnect hold (the connection to Home Assistant was interrupted)",
                   "sensors_changed": "Hold after a change of this room's sensors"}.get(reason, "Hold")
            out.append({"level": "info", "code": "startup_hold", "reason": reason, "until": _iso(until), "text": (
                f"{why}: the previous state is kept and no vacancy is confirmed for {left}.")})
        if result.vacancy_at is not None and result.state == OccupancyState.POSSIBLE_VACANCY:
            out.append({"level": "info", "code": "vacancy_pending", "until": _iso(result.vacancy_at), "text": (
                f"Vacancy is pending: the room becomes vacant in {_words(result.vacancy_at - now)} unless there is "
                "new activity.")})
        if self.house is not None:
            engine = self.house.engines[area_id]
            for other, at in sorted(engine._neighbour_motion.items()):
                if now - at < timedelta(seconds=NEIGHBOUR_NOTICE_SECONDS):
                    out.append({"level": "info", "code": "adjacent_motion", "area_id": other, "at": _iso(at),
                                "text": f"Recent movement in an adjacent room ({self.room_name(other)})."})
        return out

    def room_view(self, area_id: str, *, detail: bool = False) -> dict[str, Any]:
        room = (self.store.config.get("rooms") or {}).get(area_id) or {}
        result = self.result(area_id)
        now = utcnow()
        problems = self.problems(area_id, result, now)
        view: dict[str, Any] = {
            "area_id": area_id, "name": self.room_name(area_id), "enabled": room.get("enabled", True),
            "status": status_of(result) if room.get("enabled", True) else "off",
            "state": result.state.value if result else None,
            "label": DETAIL[result.state.value] if result else "Waiting",
            "occupied": result.occupied if result else None,
            "since": _iso(result.state_since) if result else None,
            "reason": result.reason if result else None,
            "reason_code": result.reason_code if result else None,
            "confidence": result.confidence.score if result else None,
            "last_activity": _iso(result.last_activity) if result else None,
            "last_activity_source": self.source_name(area_id, result.last_activity_source) if result else None,
            "vacancy_at": _iso(result.vacancy_at) if result else None,
            "starting_up": result.starting_up if result else False,
            "hold": ({"reason": self.hold_reason.get(area_id, "start"),
                      "until": _iso(self.house.engines[area_id]._startup_until)}
                     if result and result.starting_up and self.house else None),
            "contributing": [v.track.label() for v in result.signals
                             if v.status in (SignalStatus.ACTIVE, SignalStatus.GRACE, SignalStatus.RECENT)]
            if result else [],
            "sensor_count": len(room.get("sensors", [])),
            "problems": problems,
            "warnings": sum(1 for p in problems if p.get("sensor")),
            "frozen": self.suspended,
        }
        if detail:
            states = self.ha.states
            signals = []
            views = {v.track.entity_id: v for v in result.signals} if result else {}
            for sensor in room.get("sensors", []):
                entity_id = sensor["entity_id"]
                v = views.get(entity_id)
                state = states.get(entity_id) or {}
                signals.append({
                    **sensor, "name": (v.track.label() if v else None) or self._name_of(entity_id) or entity_id,
                    "ha_state": state.get("state"), "exists": entity_id in states,
                    "value": v.track.value.value if v else None, "status": v.status.value if v else None,
                    "status_label": SIGNAL_LABELS.get(v.status.value) if v else "Not used",
                    "since": _iso(v.track.since) if v else None,
                    # Data age: Home Assistant's last change and last update of the sensor's state.
                    "last_changed": state.get("last_changed"), "last_updated": state.get("last_updated"),
                })
            activity = [{"entity_id": e, "name": self._name_of(e) or e, "ha_state": (states.get(e) or {}).get("state"),
                         "exists": e in states, "last_changed": (states.get(e) or {}).get("last_changed")}
                        for e in room.get("activity", [])]
            view["signals"] = signals
            view["activity"] = activity
            view["timeline"] = self.timeline.recent(area_id, 40)
            if result:
                view["engine"] = {
                    "state": result.state.value, "reason_code": result.reason_code,
                    "confidence": {"score": result.confidence.score, **result.confidence.as_dict()},
                    "quiet_since": _iso(result.quiet_since), "required_quiet": result.required_quiet,
                    "vacancy_evidence": list(result.vacancy_evidence), "usable": result.usable,
                    "visit_confirmed": self.house.engines[area_id].visit_confirmed if self.house else None,
                    "timing": self.house.engines[area_id].timing.as_dict() if self.house else None,
                    "adjacent_motion": {a: _iso(t) for a, t in self.house.engines[area_id]._neighbour_motion.items()}
                    if self.house else {},
                    "next_evaluation": _iso(self.deadlines.get(area_id)),
                }
        return view

    def overview(self) -> list[dict[str, Any]]:
        rooms = self.store.config.get("rooms") or {}
        views = [self.room_view(a) for a in rooms]
        return sorted(views, key=lambda v: str(v["name"]).casefold())

    def published_rooms(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """What the companion shows: every enabled room's decision, and the disabled rooms (entities unavailable)."""
        rooms: dict[str, dict[str, Any]] = {}
        disabled: dict[str, str] = {}
        if self.house is None:
            return rooms, disabled
        for area_id, room in (self.store.config.get("rooms") or {}).items():
            name = self.room_name(area_id) or area_id
            if not room.get("enabled", True):
                disabled[area_id] = name
                continue
            result = self.house.result(area_id)
            if result is None:
                continue
            problems = self.problems(area_id, result)
            rooms[area_id] = {
                "name": name, "state": result.state.value, "occupied": result.occupied,
                "confidence": int(result.confidence.score), "reason": result.reason[:255],
                "reason_code": result.reason_code, "last_activity": _iso(result.last_activity),
                "state_since": _iso(result.state_since), "vacancy_at": _iso(result.vacancy_at),
                "starting_up": result.starting_up,
                "sensor_problems": min(100, sum(1 for p in problems if p.get("sensor"))),
            }
        return rooms, disabled

    def status(self) -> dict[str, Any]:
        return {
            "engine_version": ENGINE_VERSION, "self_test": self.self_test, "started_at": _iso(self.started_at),
            "connected_at": _iso(self.connected_at), "frozen": self.suspended,
            "rooms": len(self.house.engines) if self.house else 0, "counters": dict(self.counters),
            "state_notice": self.state_notice, "timers": len(self._timers),
        }


def _words(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60} min"


def _valid_record(record: Any) -> bool:
    """A stored room decision the engine can restore (anything else is dropped, never half-used)."""
    if not isinstance(record, dict) or record.get("state") not in {s.value for s in OccupancyState}:
        return False
    if record.get("occupied") is not None and not isinstance(record.get("occupied"), bool):
        return False
    if not isinstance(record.get("visit_confirmed", False), bool):
        return False
    source = record.get("last_activity_source")
    if source is not None and not isinstance(source, str):
        return False
    last = record.get("last_activity")
    if last is not None:
        if not isinstance(last, str):
            return False
        try:
            if datetime.fromisoformat(last).tzinfo is None:
                return False
        except ValueError:
            return False
    return True


def _brief(result: RoomResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {"status": status_of(result), "state": result.state.value, "label": DETAIL[result.state.value],
            "occupied": result.occupied, "reason": result.reason}


__all__ = ["HouseSpec", "OccupancyRuntime", "status_of"]
