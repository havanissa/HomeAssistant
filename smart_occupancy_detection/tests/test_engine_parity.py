"""Detection parity: this App's engine copy decides exactly like Smart Comfort's ``occupancy_core``.

* the golden scenarios (recorded with the 0.6.10 integration) are byte-identical
  copies and pass on this engine;
* a differential test drives both engines through hundreds of randomised houses
  and event sequences (shared sensors, adjacency, every kind and role,
  inversion, exclusion, unknown and unavailable states, late events, activity
  evidence, restarts, live reconfiguration) and compares every field of every
  evaluation and every next deadline.

Smart Comfort's engine is imported here, in the test only: the App never imports
it (``test_package.py`` checks that).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path
import random
import sys

import pytest

from sod_engine import golden as new_golden, house as new_house, types as new_types

REPO = Path(__file__).resolve().parents[2]
OLD_BACKEND = REPO / "smart_lighting" / "backend"


@pytest.fixture(scope="module")
def old():
    if not (OLD_BACKEND / "occupancy_core").is_dir():
        pytest.skip("Smart Comfort's engine is not in this checkout")
    sys.path.insert(0, str(OLD_BACKEND))
    try:
        modules = {name: importlib.import_module(f"occupancy_core.{name}") for name in ("house", "types", "golden")}
    finally:
        sys.path.remove(str(OLD_BACKEND))
    return modules


def test_self_test_passes():
    result = new_golden.self_test()
    assert result["passed"], result
    assert result["scenarios"] == 18


def test_golden_fixtures_are_byte_identical_copies():
    ours = sorted(new_golden.GOLDEN_DIR.glob("*.json"))
    assert len(ours) == 18
    for path in ours:
        assert path.read_bytes() == (OLD_BACKEND / "occupancy_core" / "golden" / path.name).read_bytes(), path.name
    theirs = {p.name for p in (OLD_BACKEND / "occupancy_core" / "golden").glob("*.json")}
    # Only the two lighting-only scenarios are left out.
    assert theirs - {p.name for p in ours} == {"10_preliminary_lighting.json", "12_zone_holds.json"}


def test_golden_scenarios_agree_with_smart_comfort(old):
    for scenario in new_golden.scenarios():
        assert new_golden.run(scenario) == old["golden"].run(scenario) == scenario["expect"], scenario["name"]


SPEC_SCENARIOS = {
    # The detection scenarios this phase must cover, and the golden scenario that covers each.
    "PIR-only rooms, brief visits, motion confirmation": "pir_only",
    "PIR + radar": "pir_radar_confirmation",
    "Radar dropouts": "radar_dropouts",
    "Door movement and exit evidence, adjacent-room movement": "door_transitions",
    "PIR + PC activity (inverted)": "pc_activity_inverted",
    "Bed occupancy": "bed_occupancy",
    "Unavailable radar, sensor reconnect": "unavailable_and_missing",
    "All sensors offline": "all_unavailable",
    "Stuck PIR": "stuck_sensors",
    "Multiple rooms sharing one input": "shared_space",
    "Normal vacancy": "long_inactivity",
    "Startup with a previously occupied room, restart while occupied": "restarts",
    "Live configuration edits": "config_edits",
    "Activity entity evidence": "wall_switch_activity",
    "Fast vacancy (Confirm vacancy role)": "confirm_vacancy_role",
    "No usable sensors": "no_usable_sensors",
    "Out-of-order events": "event_order",
}


def test_every_required_scenario_has_a_golden_recording():
    names = {s["name"] for s in new_golden.scenarios()}
    missing = {k: v for k, v in SPEC_SCENARIOS.items() if v not in names}
    assert not missing


# --------------------------------------------------------------------------- differential
T0 = datetime(2026, 11, 21, 8, 0, tzinfo=UTC)
ENTITIES = [f"binary_sensor.e{i}" for i in range(6)]
VALUES = ["on", "off", "on", "off", "unavailable", "unknown", None, "open", "closed", "42", "home", "not_home"]


def random_house(rng: random.Random) -> dict:
    rooms = {}
    names = [f"r{i}" for i in range(rng.randint(1, 3))]
    for area in names:
        sensors = []
        for entity in rng.sample(ENTITIES, rng.randint(1, 4)):
            kind = rng.choice(["pir", "radar", "door", "pc_activity", "bed", "other"])
            role = rng.choice(["trigger", "maintain", "confirm_vacancy", "ignore", "trigger", "maintain"])
            sensors.append({"entity_id": entity, "kind": kind, "role": role, "inverted": rng.random() < 0.2,
                            "enabled": rng.random() < 0.9})
        timing = {
            "correlation_window": rng.randint(1, 20), "exit_confirmation": rng.choice([0, 15, 40, 60]),
            "inactivity_timeout": rng.choice([30, 90, 200]), "dropout_grace": rng.choice([0, 10, 30]),
            "trigger_stuck_after": rng.choice([0, 150, 400]), "presence_stuck_after": rng.choice([0, 0, 200]),
            "startup_hold": rng.choice([0, 0, 30, 60]),
        }
        adjacent = [a for a in names if a != area and rng.random() < 0.5]
        rooms[area] = {"sensors": sensors, "timing": timing, "adjacent": adjacent}
    return rooms


def random_events(rng: random.Random, rooms: dict) -> list[dict]:
    events, t = [], 0.0
    for _ in range(rng.randint(30, 70)):
        t += rng.choice([0, 0.5, 1, 3, 5, 9, 12, 25, 40, 90, 250])
        roll = rng.random()
        if roll < 0.80:
            event = {"t": t, "entity": rng.choice(ENTITIES), "state": rng.choice(VALUES)}
            if rng.random() < 0.15:
                event["changed"] = max(0.0, t - rng.choice([1, 4, 15]))
            events.append(event)
        elif roll < 0.88:
            events.append({"t": t, "activity": {"area": rng.choice(list(rooms)), "source": "switch"}})
        elif roll < 0.94:
            events.append({"t": t, "restart": True})
        else:
            change = {}
            for area, room in rooms.items():
                if rng.random() < 0.6:
                    change[area] = {
                        "timing": {"inactivity_timeout": rng.choice([30, 120]), "exit_confirmation": rng.choice([0, 20])},
                        "sensors": [{**s, "role": rng.choice(["trigger", "maintain", "confirm_vacancy", "ignore"]),
                                     "inverted": rng.random() < 0.2, "enabled": rng.random() < 0.9}
                                    for s in room["sensors"]],
                    }
            events.append({"t": t, "reconfigure": change})
    return events


def _iso(value):
    return value.isoformat() if value else None


def run(house_mod, types_mod, rooms: dict, initial: dict, events: list[dict], *, rebuild: bool = False) -> list[tuple]:
    """Both runtimes' steps (golden.run), recording every evaluation in full."""
    def spec_of(raw_rooms):
        return house_mod.HouseSpec(tuple(
            house_mod.RoomSpec(a, tuple(types_mod.SensorAssignment.from_dict(s) for s in r["sensors"]),
                               types_mod.OccupancyTiming.from_dict(r["timing"]), tuple(r["adjacent"]))
            for a, r in raw_rooms.items()))

    def at(seconds):
        return T0 + timedelta(seconds=seconds)

    states = {e: house_mod.Observation(s, at(-3600), e) for e, s in initial.items() if s is not None}
    spec = spec_of(rooms)
    house = house_mod.House(spec)
    out: list[tuple] = []
    timers: dict[str, datetime | None] = {}
    order = {a: i for i, a in enumerate(spec.area_ids)}

    def evaluate(area, now):
        r = house.evaluate(area, now).result
        nd = house.next_deadline(area, now)
        timers[area] = nd
        out.append((area, _iso(now), r.state.value, r.occupied, r.reason, r.reason_code, r.confidence.score,
                    r.confidence.basis, r.usable, _iso(r.last_activity), r.last_activity_source, _iso(r.quiet_since),
                    _iso(r.vacancy_at), r.required_quiet, r.vacancy_evidence, r.starting_up, _iso(r.state_since),
                    tuple((v.track.entity_id, v.status.value, v.track.value.value) for v in r.signals), _iso(nd)))

    def advance(until):
        while due := [(d, order[a], a) for a, d in timers.items() if d is not None and d <= until]:
            when, _, area = min(due)
            evaluate(area, when)

    now = at(0)
    house.load(None, now, states.get, lambda e: e)
    house.resync(now, states.get)
    for a in spec.area_ids:
        evaluate(a, now)
    for event in events:
        when = at(event["t"])
        advance(when)
        now = when
        if "entity" in event:
            if event["state"] is None:
                states.pop(event["entity"], None)
                obs = None
            else:
                obs = states[event["entity"]] = house_mod.Observation(
                    event["state"], at(event.get("changed", event["t"])), event["entity"])
            for a in house.observe(event["entity"], obs, now):
                evaluate(a, now)
        elif "activity" in event:
            if house.note_activity(event["activity"]["area"], now, event["activity"]["source"]):
                evaluate(event["activity"]["area"], now)
        elif "restart" in event:
            stored = house.persist()
            house = house_mod.House(spec)
            timers.clear()
            house.load(stored, now, states.get, lambda e: e)
            house.resync(now, states.get)
            for a in spec.area_ids:
                evaluate(a, now)
        elif "reconfigure" in event:
            new_rooms = {}
            for a, r in rooms.items():
                change = event["reconfigure"].get(a) or {}
                new_rooms[a] = {**r, "sensors": change.get("sensors", r["sensors"]),
                                "timing": {**r["timing"], **change.get("timing", {})}}
            rooms = new_rooms
            spec = spec_of(rooms)
            if rebuild:  # this App's live path: same signals, so every engine is kept
                kept = dict(house.engines)
                house = house.rebuild(spec, now, states.get, lambda e: e)
                assert all(house.engines[a] is kept[a] for a in kept)
            else:
                house.reconfigure(spec)
            house.resync(now, states.get)
            for a in spec.area_ids:
                evaluate(a, now)
    advance(at(events[-1]["t"] + 2000 if events else 2000))
    return out


@pytest.mark.parametrize("seed", range(400))
def test_differential_parity_with_smart_comfort(old, seed):
    rng = random.Random(seed)
    rooms = random_house(rng)
    initial = {e: rng.choice(["off", "off", "on", "unavailable", None]) for e in ENTITIES}
    events = random_events(rng, rooms)
    ours = run(new_house, new_types, rooms, initial, events)
    theirs = run(old["house"], old["types"], rooms, initial, events)
    assert len(ours) == len(theirs)
    for i, (a, b) in enumerate(zip(ours, theirs, strict=True)):
        assert a == b, f"seed {seed}, evaluation {i}"


@pytest.mark.parametrize("seed", range(150))
def test_rebuild_with_the_same_signals_equals_reconfigure(old, seed):
    rng = random.Random(10_000 + seed)
    rooms = random_house(rng)
    initial = {e: rng.choice(["off", "on", "unavailable", None]) for e in ENTITIES}
    events = random_events(rng, rooms)
    assert run(new_house, new_types, rooms, initial, events, rebuild=True) == \
        run(old["house"], old["types"], rooms, initial, events)


# --------------------------------------------------------------------------- additions (no rule changed)
def _spec(sensors, timing=None, adjacent=()):
    return new_house.HouseSpec((new_house.RoomSpec(
        "kamar", tuple(new_types.SensorAssignment.from_dict(s) for s in sensors),
        new_types.OccupancyTiming.from_dict({"startup_hold": 120, **(timing or {})}), tuple(adjacent)),))


PIR = {"entity_id": "binary_sensor.pir", "kind": "pir", "role": "trigger"}
RADAR = {"entity_id": "binary_sensor.radar", "kind": "radar", "role": "maintain"}


def test_hold_all_keeps_an_occupied_room_and_restarts_its_quiet_period():
    states = {"binary_sensor.pir": new_house.Observation("off", T0, "PIR")}
    house = new_house.House(_spec([PIR], {"startup_hold": 0}))
    house.load(None, T0, states.get, lambda e: e)
    house.evaluate("kamar", T0)
    states["binary_sensor.pir"] = new_house.Observation("on", T0 + timedelta(seconds=5), "PIR")
    house.observe("binary_sensor.pir", states["binary_sensor.pir"], T0 + timedelta(seconds=5))
    house.evaluate("kamar", T0 + timedelta(seconds=20))  # confirmed (sustained)
    states["binary_sensor.pir"] = new_house.Observation("off", T0 + timedelta(seconds=30), "PIR")
    house.observe("binary_sensor.pir", states["binary_sensor.pir"], T0 + timedelta(seconds=30))
    before = house.evaluate("kamar", T0 + timedelta(seconds=30)).result
    assert before.state.value == "possible_vacancy"
    assert before.vacancy_at == T0 + timedelta(seconds=630)
    later = T0 + timedelta(seconds=500)
    house.engines["kamar"].timing = replace(house.engines["kamar"].timing, startup_hold=120)
    house.hold_all(later)
    house.resync(later, states.get)
    after = house.evaluate("kamar", later).result
    assert after.occupied is True and after.starting_up
    assert after.vacancy_at == later + timedelta(seconds=600)  # the quiet period starts again


def test_rebuild_keeps_engines_of_unchanged_rooms_and_restarts_changed_ones():
    states = {"binary_sensor.pir": new_house.Observation("on", T0, "PIR"),
              "binary_sensor.radar": new_house.Observation("on", T0, "Radar")}
    spec = _spec([PIR, RADAR], {"startup_hold": 0})
    house = new_house.House(spec)
    house.load(None, T0, states.get, lambda e: e)
    now = T0 + timedelta(seconds=60)
    assert house.evaluate("kamar", now).result.state.value == "confirmed_occupancy"
    engine = house.engines["kamar"]
    # Only the timing changes: the same engine object, nothing lost.
    same = house.rebuild(_spec([PIR, RADAR], {"startup_hold": 0, "inactivity_timeout": 900}), now, states.get,
                         lambda e: e)
    assert same.engines["kamar"] is engine and engine.timing.inactivity_timeout == 900
    # A sensor removed: a new engine that keeps the decision (occupied) with the start-up hold.
    changed = same.rebuild(_spec([RADAR], {"startup_hold": 120}), now, states.get, lambda e: e)
    assert changed.engines["kamar"] is not engine
    result = changed.evaluate("kamar", now).result
    assert result.occupied is True and result.starting_up


def test_lighting_only_inputs_are_gone():
    engine = new_house.House(_spec([PIR])).engines["kamar"]
    for name in ("preliminary", "external_hold", "set_hold", "export_state", "import_state", "adopt"):
        assert not hasattr(engine, name), name
    assert not hasattr(new_house.House, "set_preliminary") and not hasattr(new_house.House, "set_holds")
