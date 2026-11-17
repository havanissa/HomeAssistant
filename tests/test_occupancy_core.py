"""Phase A3.2: the shared engine package reproduces the 0.6.10 engine exactly.

* golden scenarios: the recorded transitions (made with the frozen 0.6.10
  engine) are reproduced by the shared engine, and by the frozen engine itself;
* randomised parity: hundreds of random homes and event streams run in
  lockstep on the shared ``House`` and the frozen ``LegacyHouse``; every
  evaluation's complete result, every persisted state and every timer deadline
  must be identical;
* exact state transfer (``export_state`` / ``import_state``): a copy evaluates
  exactly like the original from then on;
* ``adopt``: an ownership change never causes vacancy by itself;
* wire formats: round trips and strict refusal of malformed data.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import random

import pytest

from custom_components.smart_occupancy.occupancy_core import CORE_VERSION, fingerprint, golden, source_files
from custom_components.smart_occupancy.occupancy_core.engine import StateError
from custom_components.smart_occupancy.occupancy_core.house import House, HouseSpec, Observation, RoomSpec
from custom_components.smart_occupancy.occupancy_core.types import (
    OccupancyState,
    OccupancyTiming,
    SensorAssignment,
    SensorKind,
    SensorRole,
)
from custom_components.smart_occupancy.occupancy_core.wire import (
    WireError,
    result_from_wire,
    result_to_wire,
    spec_from_wire,
    spec_to_wire,
)

from .legacy_engine_0610 import LegacyHouse

REPO = Path(__file__).resolve().parents[1]
at = golden.at


# ------------------------------------------------------------------ golden
@pytest.mark.parametrize("scenario", golden.scenarios(), ids=lambda s: s["name"])
def test_golden_scenario_shared_engine(scenario) -> None:
    assert scenario["expect"], "a scenario without recorded transitions"
    assert golden.run(scenario) == scenario["expect"]


@pytest.mark.parametrize("scenario", golden.scenarios(), ids=lambda s: s["name"])
def test_golden_scenario_frozen_engine(scenario) -> None:
    assert golden.run(scenario, LegacyHouse) == scenario["expect"]


def test_golden_coverage() -> None:
    """The fixtures cover every state, every reason code family and every input kind."""
    items = golden.scenarios()
    assert len(items) >= 20
    seen_states = {e["state"] for s in items for e in s["expect"]}
    assert seen_states == {s.value for s in OccupancyState}
    codes = {e["reason_code"] for s in items for e in s["expect"]}
    assert {"possible_occupancy", "occupied", "confirming_vacancy", "vacated", "vacant", "door_only",
            "all_signals_unknown", "no_signals"} <= codes
    kinds = {k for s in items for e in s["events"] for k in e if k != "t"}
    assert {"entity", "preliminary", "hold", "activity", "restart", "reconfigure"} <= kinds
    assert golden.self_test()["passed"]


# ------------------------------------------------------ randomised parity
ENTITIES = [f"binary_sensor.s{i}" for i in range(10)] + ["media_player.pc", "sensor.odd"]
VALUES = ["on", "off", "on", "off", "unavailable", "unknown", "playing", "idle", "42", None]


def random_spec(rng: random.Random) -> HouseSpec:
    areas = [f"room{i}" for i in range(rng.randint(1, 4))]
    rooms = []
    for area in areas:
        sensors = []
        for entity_id in rng.sample(ENTITIES, rng.randint(0, 4)):
            kind = rng.choice(list(SensorKind))
            sensors.append(SensorAssignment(entity_id, kind, rng.choice(list(SensorRole)),
                                            inverted=rng.random() < 0.15, enabled=rng.random() < 0.9))
        timing = OccupancyTiming(
            correlation_window=rng.choice([1, 5, 10, 30]), exit_confirmation=rng.choice([0, 30, 120, 300]),
            inactivity_timeout=rng.choice([30, 120, 600]), dropout_grace=rng.choice([0, 10, 30]),
            trigger_stuck_after=rng.choice([0, 300, 1800]), presence_stuck_after=rng.choice([0, 600]),
            startup_hold=rng.choice([0, 60, 120]),
        )
        adjacent = tuple(a for a in areas if a != area and rng.random() < 0.5)
        rooms.append(RoomSpec(area, tuple(sensors), timing, adjacent))
    return HouseSpec(tuple(rooms))


def random_events(rng: random.Random, spec: HouseSpec, count: int) -> list[dict]:
    t = 0.0
    events = []
    entities = sorted({s.entity_id for r in spec.rooms for s in r.sensors}) or ["binary_sensor.none"]
    for _ in range(count):
        t += rng.choice([0, 0.5, 1, 3, 8, 15, 40, 90, 400, 1200])
        roll = rng.random()
        if roll < 0.75:
            event = {"t": t, "entity": rng.choice(entities), "state": rng.choice(VALUES)}
            if rng.random() < 0.1:
                event["changed"] = max(0.0, t - rng.choice([1, 5, 60]))  # delayed / out of order
            if rng.random() < 0.05:
                event["name"] = "Renamed"
        elif roll < 0.82:
            area = rng.choice(spec.rooms).area_id
            event = {"t": t, "hold": {"area": area, "zones": rng.choice([[], ["Desk"], ["Desk", "Sofa"]])}}
        elif roll < 0.88:
            area = rng.choice(spec.rooms).area_id
            room = spec.room(area)
            sig = [s.entity_id for s in room.sensors]
            event = {"t": t, "preliminary": {area: rng.sample(sig, min(len(sig), rng.randint(0, 2)))}}
        elif roll < 0.93:
            event = {"t": t, "activity": {"area": rng.choice(spec.rooms).area_id, "source": "wall switch"}}
        elif roll < 0.97:
            area = rng.choice(spec.rooms).area_id
            room = spec.room(area)
            sensors = [{**s.as_dict(), "role": rng.choice(list(SensorRole)).value,
                        "enabled": rng.random() < 0.8, "inverted": s.inverted} for s in room.sensors]
            event = {"t": t, "reconfigure": {area: {"sensors": sensors,
                                                    "timing": {"exit_confirmation": rng.choice([0, 60, 300])}}}}
        else:
            event = {"t": t, "restart": True}
        events.append(event)
    return events


def canonical(house, area_id: str) -> dict:
    engine = house.engines[area_id]
    return {
        "result": result_to_wire(engine.result, engine.visit_confirmed) if engine.result else None,
        "persist": engine.persist(),
    }


class Lockstep:
    """Drives several houses with identical calls (like golden.run) and compares them after every step."""

    def __init__(self, spec: HouseSpec, initial: dict[str, str]) -> None:
        self.spec = spec
        self.states = {e: Observation(s, at(-3600), e) for e, s in initial.items()}
        self.houses = [House(spec), LegacyHouse(spec)]
        self.timers: dict[str, object] = {}
        self.steps = 0
        for h in self.houses:
            h.load(None, at(0), self.states.get, self._name)
        self._start(at(0))

    def _name(self, entity_id):
        obs = self.states.get(entity_id)
        return obs.name if obs else None

    def _evaluate(self, area_id, now) -> None:
        for h in self.houses:
            h.evaluate(area_id, now)
        deadlines = [h.next_deadline(area_id, now) for h in self.houses]
        assert all(x == deadlines[0] for x in deadlines), (area_id, now)
        self.timers[area_id] = deadlines[0]
        expected = canonical(self.houses[1], area_id)
        assert canonical(self.houses[0], area_id) == expected, (area_id, now)
        self.steps += 1

    def _start(self, now) -> None:
        for h in self.houses:
            h.resync(now, self.states.get)
        for area_id in self.spec.area_ids:
            self._evaluate(area_id, now)

    def advance(self, until) -> None:
        order = {a: i for i, a in enumerate(self.spec.area_ids)}
        while due := [(d, order[a], a) for a, d in self.timers.items() if d is not None and d <= until]:
            when, _, area_id = min(due)
            self._evaluate(area_id, when)

    def apply(self, event: dict) -> None:
        now = at(event["t"])
        self.advance(now)
        if "entity" in event:
            if event["state"] is None:
                self.states.pop(event["entity"], None)
                obs = None
            else:
                obs = self.states[event["entity"]] = Observation(event["state"], at(event.get("changed", event["t"])),
                                                                 event.get("name") or event["entity"])
            affected = [h.observe(event["entity"], obs, now) for h in self.houses]
            assert all(x == affected[0] for x in affected)
            for area_id in affected[0]:
                self._evaluate(area_id, now)
        elif "hold" in event:
            changed = [h.set_holds(event["hold"]["area"], frozenset(event["hold"]["zones"]), now) for h in self.houses]
            assert all(x == changed[0] for x in changed)
            if changed[0]:
                self._evaluate(event["hold"]["area"], now)
        elif "preliminary" in event:
            wanted = {a: frozenset(v) for a, v in event["preliminary"].items()}
            changed = [h.set_preliminary(wanted) for h in self.houses]
            assert all(x == changed[0] for x in changed)
            for area_id in changed[0]:
                self._evaluate(area_id, now)
        elif "activity" in event:
            changed = [h.note_activity(event["activity"]["area"], now, "wall switch") for h in self.houses]
            assert all(x == changed[0] for x in changed)
            if changed[0]:
                self._evaluate(event["activity"]["area"], now)
        elif "reconfigure" in event:
            rooms = []
            for room in self.spec.rooms:
                change = event["reconfigure"].get(room.area_id)
                if change:
                    room = replace(room, sensors=tuple(SensorAssignment.from_dict(s) for s in change["sensors"]),
                                   timing=room.timing.with_overrides(change["timing"]))
                rooms.append(room)
            self.spec = HouseSpec(tuple(rooms))
            for h in self.houses:
                h.reconfigure(self.spec)
            self._start(now)
        elif "restart" in event:
            stored = [h.persist() for h in self.houses]
            assert all(x == stored[0] for x in stored)
            extra = [({a: e.preliminary for a, e in h.engines.items()}, {a: e.external_hold for a, e in h.engines.items()})
                     for h in self.houses]
            self.houses = [type(h)(self.spec) for h in self.houses]
            self.timers.clear()
            for h, (prelim, holds) in zip(self.houses, extra, strict=True):
                h.load(stored[0], now, self.states.get, self._name)
                h.set_preliminary(prelim)
                for area_id, names in holds.items():
                    h.set_holds(area_id, names, now)
            self._start(now)


@pytest.mark.parametrize("seed", range(300))
def test_randomised_parity_with_frozen_engine(seed: int) -> None:
    rng = random.Random(seed)
    spec = random_spec(rng)
    entities = {s.entity_id for r in spec.rooms for s in r.sensors}
    initial = {e: rng.choice(["on", "off", "unavailable"]) for e in entities if rng.random() < 0.9}
    run = Lockstep(spec, initial)
    for event in random_events(rng, spec, 80):
        run.apply(event)
    run.advance(at(100_000))
    assert run.steps > 0


# ------------------------------------------------------- state transfer
def test_exported_state_copy_evaluates_identically() -> None:
    for seed in range(60):
        rng = random.Random(1000 + seed)
        spec = random_spec(rng)
        events = [e for e in random_events(rng, spec, 120) if "restart" not in e and "reconfigure" not in e]
        cut = rng.randint(5, len(events) - 5)
        original = Lockstep(spec, {})
        original.houses = original.houses[:1]  # the shared engine only
        for event in events[:cut]:
            _apply_single(original, event)
        now = at(events[cut - 1]["t"])
        state = json.loads(json.dumps(original.houses[0].export_state()))  # through JSON, as on the bridge
        copy = House(spec)
        for area_id, engine in copy.engines.items():
            engine.import_state(state[area_id])
            engine.evaluate(now)
            original.houses[0].engines[area_id].evaluate(now)
        original.houses.append(copy)
        for event in events[cut:]:
            _apply_single(original, event)
        original.advance(at(100_000))


def _apply_single(run: Lockstep, event: dict) -> None:
    """Lockstep.apply for houses of the shared engine (compares every house with the last one)."""
    def check(area_id, now):
        houses = run.houses  # a restart replaces them
        for h in houses:
            h.evaluate(area_id, now)
        run.timers[area_id] = houses[0].next_deadline(area_id, now)
        first = canonical(houses[0], area_id)
        assert all(canonical(h, area_id) == first for h in houses[1:]), (area_id, now)

    run._evaluate = check  # noqa: SLF001 - the test's own helper
    run.apply(event)


def test_import_refuses_invalid_state() -> None:
    spec = golden.spec_of({"r": {"sensors": [{"entity_id": "binary_sensor.a", "kind": "pir", "role": "trigger"}]}})
    house = House(spec)
    house.load(None, at(0), lambda e: None, lambda e: None)
    engine = house.engines["r"]
    good = engine.export_state()
    engine.import_state(good)
    for bad in (None, {**good, "schema": 2}, {**good, "area_id": "other"}, {**good, "state": "party"},
                {**good, "occupied": "yes"}, {**good, "tracks": {}}, {**good, "state_since": "yesterday"},
                {**good, "state_since": "2026-11-17T12:00:00"}, {**good, "neighbour_motion": {"far": None}},
                {**good, "preliminary": "binary_sensor.a"},
                {**good, "tracks": {"binary_sensor.a": {**good["tracks"]["binary_sensor.a"], "value": "maybe"}}}):
        before = engine.export_state()
        with pytest.raises(StateError):
            engine.import_state(bad)
        assert engine.export_state() == before  # nothing changed


# ------------------------------------------------------------------ adopt
def _room(**timing) -> House:
    spec = golden.spec_of({"r": {"sensors": [
        {"entity_id": "binary_sensor.pir", "kind": "pir", "role": "trigger"},
        {"entity_id": "binary_sensor.radar", "kind": "radar", "role": "maintain"}], "timing": timing}})
    house = House(spec)
    house.load(None, at(0), lambda e: Observation("off", at(-3600)), lambda e: None)
    house.evaluate("r", at(0))
    return house


def test_adopt_when_decisions_agree_changes_nothing() -> None:
    house = _room()
    house.observe("binary_sensor.radar", Observation("on", at(10)), at(10))
    house.evaluate("r", at(10))
    house.observe("binary_sensor.radar", Observation("off", at(100)), at(100))
    result = house.evaluate("r", at(200)).result
    assert result.state == OccupancyState.POSSIBLE_VACANCY
    engine = house.engines["r"]
    before = engine.export_state()
    assert not engine.adopt({"state": "possible_vacancy", "occupied": True, "visit_confirmed": False}, at(300))
    assert engine.export_state() == before
    assert house.evaluate("r", at(300)).result.vacancy_at == result.vacancy_at  # the countdown keeps its deadline


def test_adopted_occupancy_never_vacates_early() -> None:
    house = _room(exit_confirmation=300, inactivity_timeout=600)
    engine = house.engines["r"]
    assert house.result("r").state == OccupancyState.VACANT
    # The App (owner) had the room occupied; this engine saw nothing (e.g. it missed an event): it adopts.
    assert engine.adopt({"state": "confirmed_occupancy", "occupied": True, "visit_confirmed": True,
                         "state_since": at(50).isoformat()}, at(1000))
    result = house.evaluate("r", at(1000)).result
    assert result.occupied is True and result.state == OccupancyState.POSSIBLE_VACANCY
    assert result.vacancy_at >= at(1000) + timedelta(seconds=600)  # confirmed visit: the full timeout from now


def test_adopted_vacancy_yields_to_current_evidence() -> None:
    house = _room()
    house.observe("binary_sensor.radar", Observation("on", at(10)), at(10))
    house.evaluate("r", at(10))
    engine = house.engines["r"]
    assert engine.adopt({"state": "vacant", "occupied": False, "visit_confirmed": False}, at(20))
    result = house.evaluate("r", at(20)).result
    assert result.occupied is True  # the radar is active: the room is occupied again (evidence, not ownership)


def test_adopt_refuses_invalid_decision() -> None:
    engine = _room().engines["r"]
    for bad in (None, {"state": "x"}, {"state": "vacant", "occupied": 1}, {"state": "vacant", "visit_confirmed": 2},
                {"state": "vacant", "state_since": "now"}):
        with pytest.raises(StateError):
            engine.adopt(bad, at(5))


# ------------------------------------------------------------------- wire
def test_spec_round_trip_and_revision() -> None:
    for seed in range(30):
        spec = random_spec(random.Random(seed))
        data = json.loads(json.dumps(spec_to_wire(spec)))
        again = spec_from_wire(data)
        assert again == spec and again.revision() == spec.revision()
    data = spec_to_wire(random_spec(random.Random(1)))
    with pytest.raises(WireError):
        spec_from_wire({**data, "revision": "0" * 16})


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(rooms=[]),
    lambda d: next(iter(d["rooms"].values())).update(timing={"correlation_window": 10}),
    lambda d: next(iter(d["rooms"].values()))["timing"].update(correlation_window=0),
    lambda d: next(iter(d["rooms"].values()))["timing"].update(exit_confirmation="300"),
    lambda d: next(iter(d["rooms"].values())).update(sensors=[{"entity_id": "Not An Id", "kind": "pir",
                                                              "role": "trigger", "inverted": False, "enabled": True}]),
    lambda d: next(iter(d["rooms"].values())).update(sensors=[{"entity_id": "binary_sensor.a", "kind": "laser",
                                                              "role": "trigger", "inverted": False, "enabled": True}]),
    lambda d: next(iter(d["rooms"].values())).update(adjacent=[next(iter(d["rooms"]))]),
])
def test_spec_refuses_malformed(mutate) -> None:
    data = json.loads(json.dumps(spec_to_wire(golden.spec_of({"r": {"sensors": [
        {"entity_id": "binary_sensor.a", "kind": "pir", "role": "trigger"}]}}))))
    data.pop("revision")
    mutate(data)
    with pytest.raises(WireError):
        spec_from_wire(data)


def test_result_round_trip_and_refusal() -> None:
    for seed in range(40):
        rng = random.Random(500 + seed)
        spec = random_spec(rng)
        run = Lockstep(spec, {})
        run.houses = run.houses[:1]
        for event in random_events(rng, spec, 40):
            if "reconfigure" not in event:
                _apply_single(run, event)
        for room in run.spec.rooms:
            engine = run.houses[0].engines[room.area_id]
            data = json.loads(json.dumps(result_to_wire(engine.result, engine.visit_confirmed)))
            rebuilt = result_from_wire(data, room)
            assert result_to_wire(rebuilt, engine.visit_confirmed) == data
            assert rebuilt.state == engine.result.state and rebuilt.vacancy_at == engine.result.vacancy_at
    room = spec.rooms[0]
    engine = run.houses[0].engines[room.area_id]
    good = result_to_wire(engine.result, engine.visit_confirmed)
    for bad in ({**good, "state": "dancing"}, {**good, "occupied": "true"}, {**good, "signals": good["signals"] + [{}]},
                {**good, "reason": "x" * 300}, {**good, "confidence": {**good["confidence"], "score": 101}},
                {**good, "vacancy_at": "2026-11-17 12:00"}, {**good, "required_quiet": -1}, "nonsense"):
        with pytest.raises(WireError):
            result_from_wire(bad, room)


# ---------------------------------------------------------------- package
def test_core_has_no_dependencies_and_a_stable_fingerprint() -> None:
    core = REPO / "custom_components/smart_occupancy/occupancy_core"
    for path in source_files():
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module != "__future__":
                modules = [node.module.split(".")[0]]
            else:
                continue
            for module in modules:
                assert module in {"collections", "dataclasses", "datetime", "enum", "functools", "hashlib",
                                  "json", "pathlib", "re", "typing"}, (path.name, module)
    assert len(fingerprint()) == 64 and fingerprint() == fingerprint()
    assert {p.name for p in source_files() if p.suffix == ".py"} == {
        "__init__.py", "engine.py", "golden.py", "house.py", "protocol.py", "types.py", "wire.py"}
    assert all(p.is_relative_to(core) for p in source_files())
    assert CORE_VERSION == "1.0.0"


def test_app_copy_is_byte_identical() -> None:
    """The App runs a vendored copy (its build context is its own folder): it must be the very same files."""
    core = REPO / "custom_components/smart_occupancy/occupancy_core"
    copy = REPO / "smart_lighting/backend/occupancy_core"
    ours = {p.relative_to(core).as_posix(): p.read_bytes() for p in source_files()}
    theirs = {p.relative_to(copy).as_posix(): p.read_bytes() for p in copy.rglob("*")
              if p.is_file() and p.suffix in (".py", ".json") and "__pycache__" not in p.parts}
    assert theirs == ours, "run scripts/sync_occupancy_core.py"
