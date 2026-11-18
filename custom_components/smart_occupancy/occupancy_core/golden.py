"""Golden regression scenarios: recorded event sequences with the transitions the engine must produce.

The scenarios (``golden/*.json``) were recorded with the 0.6.10 engine of the
integration, before it moved into this package; the integration's tests prove
again on every run that the frozen 0.6.10 engine and this package agree, and
the App runs them at start-up as a self-test (a readiness check for handover).

A scenario runs through the same steps as both runtimes: every event is
observed and the rooms it affects are evaluated at once, and every room is
evaluated again at its ``next_deadline`` (the timers), in time order. Times are
seconds after ``T0``; the initial states changed one hour before ``T0`` and the
house starts at ``T0`` (so the start-up hold, when the timing has one, runs
from ``T0``).

Event forms (one key besides ``t``)::

    {"t": 5, "entity": "binary_sensor.pir", "state": "on"}       # a state change ("state": null: removed)
    {"t": 9, "entity": "binary_sensor.pir", "state": "off", "changed": 7}  # arrives late (last_changed 7)
    {"t": 5, "preliminary": {"<area>": ["binary_sensor.pir"]}}   # preliminary signals (every room)
    {"t": 5, "hold": {"area": "<area>", "zones": ["Desk"]}}      # zones holding a room
    {"t": 5, "activity": {"area": "<area>", "source": "wall switch"}}
    {"t": 5, "restart": true}                                    # persist, start again (start-up hold)
    {"t": 5, "reconfigure": {"<area>": {"timing": {...}, "sensors": [...]}}}  # live change, same signals
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from .house import House, HouseSpec, Observation, RoomSpec
from .types import OccupancyTiming, SensorAssignment

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def spec_of(rooms: dict[str, Any]) -> HouseSpec:
    out = []
    for area_id, raw in rooms.items():
        sensors = tuple(SensorAssignment.from_dict(s) for s in raw.get("sensors", []))
        timing = OccupancyTiming.from_dict({"startup_hold": 0, **raw.get("timing", {})})
        out.append(RoomSpec(area_id, sensors, timing, tuple(raw.get("adjacent", []))))
    return HouseSpec(tuple(out))


class _World:
    """The entity states of a scenario (what Home Assistant would hold)."""

    def __init__(self, initial: dict[str, str | None]) -> None:
        self.states: dict[str, Observation] = {
            e: Observation(s, at(-3600), e) for e, s in initial.items() if s is not None
        }

    def lookup(self, entity_id: str) -> Observation | None:
        return self.states.get(entity_id)

    def name(self, entity_id: str) -> str | None:
        obs = self.states.get(entity_id)
        return obs.name if obs else None


def run(scenario: dict[str, Any], house_factory: Callable[[HouseSpec], Any] = House) -> list[dict[str, Any]]:
    """Run one scenario; returns every transition (and the first evaluation of each start) in order."""
    spec = spec_of(scenario["rooms"])
    world = _World(scenario.get("initial", {}))
    out: list[dict[str, Any]] = []
    house = house_factory(spec)
    now = at(0)
    house.load(None, now, world.lookup, world.name)
    # One timer per room, set after each of its evaluations to its next deadline (as both runtimes do).
    timers: dict[str, datetime | None] = {}

    def evaluate(area_id: str, now: datetime, start: bool = False) -> None:
        evaluation = house.evaluate(area_id, now)
        timers[area_id] = house.next_deadline(area_id, now)
        if evaluation.transition:
            r = evaluation.result
            item = {"t": round((now - T0).total_seconds(), 3), "area": area_id, "state": r.state.value,
                    "occupied": r.occupied, "reason_code": r.reason_code}
            if start:
                item["start"] = True
            out.append(item)

    def start_all(now: datetime) -> None:
        house.resync(now, world.lookup)
        for area_id in spec.area_ids:
            evaluate(area_id, now, start=True)

    order = {a: i for i, a in enumerate(spec.area_ids)}

    def advance(until: datetime) -> None:
        """Fire every timer due up to ``until``, earliest first (ties in configuration order)."""
        while due := [(d, order[a], a) for a, d in timers.items() if d is not None and d <= until]:
            when, _, area_id = min(due)
            evaluate(area_id, when)

    start_all(now)
    for event in sorted(scenario.get("events", []), key=lambda e: e["t"]):
        when = at(event["t"])
        advance(when)
        now = when
        if "entity" in event:
            entity_id, value = event["entity"], event.get("state")
            if value is None:
                world.states.pop(entity_id, None)
                obs = None
            else:
                name = event.get("name") or entity_id
                # "changed": Home Assistant's last_changed when it differs from the arrival time (a delayed event).
                obs = world.states[entity_id] = Observation(value, at(event.get("changed", event["t"])), name)
            for area_id in house.observe(entity_id, obs, now):
                evaluate(area_id, now)
        elif "preliminary" in event:
            wanted = {a: frozenset(v) for a, v in event["preliminary"].items()}
            for area_id in house.set_preliminary(wanted):
                evaluate(area_id, now)
        elif "hold" in event:
            hold = event["hold"]
            if house.set_holds(hold["area"], frozenset(hold["zones"]), now):
                evaluate(hold["area"], now)
        elif "activity" in event:
            act = event["activity"]
            if house.note_activity(act["area"], now, act["source"]):
                evaluate(act["area"], now)
        elif "restart" in event:
            stored = house.persist()
            preliminary = {a: e.preliminary for a, e in house.engines.items()}
            holds = {a: e.external_hold for a, e in house.engines.items()}
            house = house_factory(spec)
            timers.clear()
            house.load(stored, now, world.lookup, world.name)
            # The lighting runtime sets its preliminary signals again before the first evaluation, and the
            # zones report their holds again when they start.
            house.set_preliminary(preliminary)
            for area_id, names in holds.items():
                house.set_holds(area_id, names, now)
            start_all(now)
        elif "reconfigure" in event:
            rooms = []
            for room in spec.rooms:
                change = event["reconfigure"].get(room.area_id) or {}
                sensors = room.sensors
                if "sensors" in change:
                    sensors = tuple(SensorAssignment.from_dict(s) for s in change["sensors"])
                timing = room.timing.with_overrides(change.get("timing") or {})
                rooms.append(replace(room, sensors=sensors, timing=timing))
            spec = HouseSpec(tuple(rooms))
            house.reconfigure(spec)
            house.resync(now, world.lookup)
            for area_id in spec.area_ids:
                evaluate(area_id, now)
        else:
            raise ValueError(f"unknown event {event}")
    advance(at(scenario.get("until", (scenario.get("events") or [{"t": 0}])[-1]["t"] + 1)))
    return out


def scenarios() -> list[dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(GOLDEN_DIR.glob("*.json"))]


def self_test() -> dict[str, Any]:
    """Run every golden scenario on this engine and compare with the recorded transitions."""
    failed = []
    items = scenarios()
    for scenario in items:
        got = run(scenario)
        if got != scenario["expect"]:
            first = next((i for i, (a, b) in enumerate(zip(got, scenario["expect"], strict=False)) if a != b),
                         min(len(got), len(scenario["expect"])))
            failed.append({"name": scenario["name"], "at_transition": first})
    return {"scenarios": len(items), "transitions": sum(len(s["expect"]) for s in items), "passed": not failed,
            "failed": failed}
