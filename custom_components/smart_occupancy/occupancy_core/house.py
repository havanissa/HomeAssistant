"""One occupancy engine per room, and everything between them (shared by the integration and the App).

The Home Assistant independent part of the integration's 0.6.10
``OccupancyManager``, moved here so both runtimes apply exactly the same rules:

* which rooms an entity belongs to, and which rooms see its movement as
  adjacent-room evidence (a capable signal that starts in room A notifies every
  room that lists A as adjacent);
* how a state is read: ``interpret`` with the signal's inversion, at
  ``min(last_changed, now)``; a re-read (``resync``) of a value that differs
  from what the engine holds uses ``now`` when the state's timestamp is older
  than the signal's (the current state is newer, whatever its timestamp);
* restoring at start, live reconfiguration (same rooms and signals only),
  preliminary signals, zone holds and wall-switch activity;
* evaluation and transition detection (state or occupied changed).

The adapters only connect it to their event source, timers and storage:
``occupancy_manager.py`` in the integration, ``occupancy_runtime.py`` in the
App. Nothing here reads a clock: every call gets ``now``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from typing import Any

from .engine import RoomEngine, RoomResult, interpret
from .types import OccupancyTiming, SensorAssignment


@dataclass(frozen=True, slots=True)
class RoomSpec:
    """What the engine of one room needs from the configuration."""

    area_id: str
    sensors: tuple[SensorAssignment, ...]
    timing: OccupancyTiming
    adjacent: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensors": [
                {"entity_id": s.entity_id, "kind": s.kind.value, "role": s.role.value, "inverted": s.inverted,
                 "enabled": s.enabled}
                for s in self.sensors
            ],
            "timing": self.timing.as_dict(),
            "adjacent": list(self.adjacent),
        }


@dataclass(frozen=True, slots=True)
class HouseSpec:
    """The occupancy configuration of every enabled room, in configuration order."""

    rooms: tuple[RoomSpec, ...] = ()

    def room(self, area_id: str) -> RoomSpec | None:
        return next((r for r in self.rooms if r.area_id == area_id), None)

    @property
    def area_ids(self) -> tuple[str, ...]:
        return tuple(r.area_id for r in self.rooms)

    def as_dict(self) -> dict[str, Any]:
        return {"rooms": {r.area_id: r.as_dict() for r in self.rooms}}

    def revision(self) -> str:
        """Short digest of everything the engines use (equal on both sides when they run the same configuration)."""
        text = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def same_signals(self, other: HouseSpec) -> bool:
        """The same rooms with the same signals (entity ids, order and kinds): changes can be applied live."""
        if set(self.area_ids) != set(other.area_ids):
            return False
        for room in self.rooms:
            new = other.room(room.area_id)
            assert new is not None
            if [(s.entity_id, s.kind) for s in room.sensors] != [(s.entity_id, s.kind) for s in new.sensors]:
                return False
        return True


@dataclass(frozen=True, slots=True)
class Observation:
    """An entity's state as the runtime sees it (Home Assistant's ``state``, ``last_changed`` and name)."""

    state: str | None
    last_changed: datetime | None
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Evaluation:
    area_id: str
    previous: RoomResult | None
    result: RoomResult

    @property
    def transition(self) -> bool:
        """The state or the occupied output changed (or this is the first evaluation)."""
        p = self.previous
        return p is None or (p.state, p.occupied) != (self.result.state, self.result.occupied)


Lookup = Callable[[str], Observation | None]


class House:
    """The room engines and the routing between them."""

    def __init__(self, spec: HouseSpec) -> None:
        self.spec = spec
        self.engines: dict[str, RoomEngine] = {}
        self._entity_rooms: dict[str, list[str]] = {}
        self._neighbours_of: dict[str, list[str]] = {}  # area -> rooms that list it as adjacent
        for room in spec.rooms:
            self.engines[room.area_id] = RoomEngine(room.area_id, room.sensors, room.timing, adjacent=room.adjacent)
            for s in room.sensors:
                self._entity_rooms.setdefault(s.entity_id, []).append(room.area_id)
            for neighbour in room.adjacent:
                self._neighbours_of.setdefault(neighbour, []).append(room.area_id)

    @property
    def signal_entities(self) -> list[str]:
        return sorted(self._entity_rooms)

    def rooms_of(self, entity_id: str) -> list[str]:
        return list(self._entity_rooms.get(entity_id, ()))

    # ------------------------------------------------------------ lifecycle
    def load(self, stored: dict[str, Any] | None, now: datetime, lookup: Lookup,
             name_of: Callable[[str], str | None]) -> None:
        """Restore the previous decisions (conservatively, with the start-up hold) and read every signal."""
        stored = stored if isinstance(stored, dict) else {}
        for area_id, engine in self.engines.items():
            data = stored.get(area_id)
            if isinstance(data, dict):
                engine.restore(data, now)
            else:
                engine.start(now)
            for entity_id, track in engine.tracks.items():
                obs = lookup(entity_id)
                track.name = name_of(entity_id)
                at = min(obs.last_changed, now) if obs and obs.last_changed else now
                engine.observe(entity_id, interpret(obs.state if obs else None, inverted=track.assignment.inverted),
                               at)

    def resync(self, now: datetime, lookup: Lookup) -> None:
        """Read the current state of every signal again (observations that are not newer are ignored)."""
        for engine in self.engines.values():
            for entity_id, track in engine.tracks.items():
                obs = lookup(entity_id)
                value = interpret(obs.state if obs else None, inverted=track.assignment.inverted)
                if value == track.value:
                    continue
                at = min(obs.last_changed, now) if obs and obs.last_changed else now
                if track.since is not None and at < track.since:
                    at = now  # the current state is newer than what the engine holds, whatever its timestamp
                engine.observe(entity_id, value, at, obs.name if obs else None)

    def reconfigure(self, spec: HouseSpec) -> None:
        """Apply changed signal settings and timing (same rooms and signals) without losing what is known."""
        if not self.spec.same_signals(spec):
            raise ValueError("the rooms or signals changed: build a new House")
        self.spec = spec
        for room in spec.rooms:
            self.engines[room.area_id].reconfigure(room.sensors, room.timing)

    # --------------------------------------------------------------- inputs
    def observe(self, entity_id: str, obs: Observation | None, now: datetime) -> list[str]:
        """A state change of ``entity_id``. Returns the rooms to evaluate (in a stable order)."""
        at = min(obs.last_changed, now) if obs and obs.last_changed else now
        name = obs.name if obs else None
        affected: dict[str, None] = {}
        for area_id in self._entity_rooms.get(entity_id, ()):
            engine = self.engines[area_id]
            track = engine.tracks[entity_id]
            before = track.active_start
            value = interpret(obs.state if obs else None, inverted=track.assignment.inverted)
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
        return list(affected)

    def set_preliminary(self, signals: dict[str, frozenset[str]]) -> list[str]:
        """Signals that start possible occupancy lighting, per room. Returns the rooms whose set changed."""
        changed = []
        for area_id, engine in self.engines.items():
            wanted = frozenset(signals.get(area_id, frozenset()))
            if wanted != engine.preliminary:
                engine.preliminary = wanted
                changed.append(area_id)
        return changed

    def set_holds(self, area_id: str, names: frozenset[str], now: datetime) -> bool:
        engine = self.engines.get(area_id)
        return engine is not None and engine.set_hold(frozenset(names), now)

    def note_activity(self, area_id: str, at: datetime, source: str) -> bool:
        engine = self.engines.get(area_id)
        return engine is not None and engine.note_activity_evidence(at, source)

    # ----------------------------------------------------------- evaluation
    def evaluate(self, area_id: str, now: datetime) -> Evaluation:
        engine = self.engines[area_id]
        previous = engine.result
        return Evaluation(area_id, previous, engine.evaluate(now))

    def next_deadline(self, area_id: str, now: datetime) -> datetime | None:
        return self.engines[area_id].next_deadline(now)

    def result(self, area_id: str) -> RoomResult | None:
        engine = self.engines.get(area_id)
        return engine.result if engine else None

    # ---------------------------------------------------------- persistence
    def persist(self) -> dict[str, dict[str, Any]]:
        return {area_id: engine.persist() for area_id, engine in self.engines.items()}

    def export_state(self) -> dict[str, dict[str, Any]]:
        return {area_id: engine.export_state() for area_id, engine in self.engines.items()}
