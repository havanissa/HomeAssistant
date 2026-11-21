"""The runtime: Home Assistant events, timers, outages, restarts, configuration changes, activity entities."""

from __future__ import annotations

import json

import pytest

from sod import config as C
from sod.runtime import OccupancyRuntime
from tests.conftest import KAMAR, STOVA, room, settle

PIR, RADAR, DOOR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar", "binary_sensor.kamar_door"


async def make(tmp_path, ha, clock, rooms=None, timing=None):
    store = C.ConfigStore(tmp_path)
    store.load()
    cfg = {"timing": {**C.empty_config()["timing"], "startup_hold": 0, **(timing or {})},
           "rooms": rooms if rooms is not None else {KAMAR: room()}}
    store.save(cfg, expected_revision=store.revision, reason="test")
    runtime = OccupancyRuntime(ha, store, tmp_path)
    await runtime.start()
    return runtime


def fire_due(runtime, clock):
    """Fire every room timer due by the clock's time, each at its own deadline (as asyncio would)."""
    target = clock.now
    for _ in range(50):
        due = sorted((d, a) for a, d in runtime.deadlines.items() if d <= target)
        if not due:
            break
        clock.now = due[0][0]
        runtime._fire(due[0][1])
    clock.now = target


def status(runtime, area_id=KAMAR):
    view = runtime.room_view(area_id)
    return view["status"], view["state"]


async def test_events_drive_the_decision(tmp_path, ha, clock):
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock)
    assert runtime.house is None  # nothing is decided before Home Assistant's states are known
    ha.connect()
    assert status(runtime) == ("vacant", "vacant")
    clock.advance(10)
    ha.set(PIR, "on", clock.now)
    assert status(runtime) == ("occupied", "possible_occupancy")
    clock.advance(2)
    ha.set(RADAR, "on", clock.now)
    assert status(runtime) == ("occupied", "confirmed_occupancy")
    view = runtime.room_view(KAMAR, detail=True)
    assert view["contributing"] == [PIR, RADAR] and view["confidence"] == 100
    assert [e["state"] for e in view["timeline"] if e["kind"] == "state"][:2] == ["confirmed_occupancy",
                                                                                 "possible_occupancy"]
    rooms, disabled = runtime.published_rooms()
    assert rooms[KAMAR]["state"] == "confirmed_occupancy" and rooms[KAMAR]["occupied"] is True and not disabled
    clock.advance(60)
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    clock.advance(30)
    fire_due(runtime, clock)  # the dropout grace ends
    assert status(runtime) == ("occupied", "possible_vacancy")
    clock.advance(600)
    fire_due(runtime, clock)
    assert status(runtime) == ("vacant", "vacant")
    await runtime.stop()


async def test_a_home_assistant_outage_never_makes_a_room_vacant(tmp_path, ha, clock):
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock, timing={"startup_hold": 120})
    ha.connect()
    clock.advance(30)
    ha.set(PIR, "off", clock.now)
    assert status(runtime)[1] == "possible_vacancy"
    vacancy_at = runtime.result(KAMAR).vacancy_at
    ha.disconnect()
    assert runtime.suspended and not runtime._timers  # frozen
    assert runtime.room_view(KAMAR)["frozen"]
    clock.advance(3600)  # far past the old deadline
    runtime._fire(KAMAR)  # a stray timer does nothing while frozen
    assert status(runtime)[0] == "occupied"
    ha.connect()
    result = runtime.result(KAMAR)
    assert result.occupied is True and result.starting_up
    assert result.vacancy_at > vacancy_at and result.vacancy_at >= clock.now
    assert any(e.get("event") == "reconnected" for e in runtime.timeline.recent(KAMAR))
    await runtime.stop()


async def test_restart_keeps_an_occupied_room_occupied(tmp_path, ha, clock):
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "on", clock.now)
    runtime = await make(tmp_path, ha, clock, timing={"startup_hold": 120})
    ha.connect()
    clock.advance(20)
    runtime._fire(KAMAR)
    assert status(runtime) == ("occupied", "confirmed_occupancy")
    await runtime.stop()  # the App stops (update, restart)
    stored = json.loads((tmp_path / "state" / "occupancy.json").read_text())
    assert stored["rooms"][KAMAR]["occupied"] is True
    # Meanwhile the person left and the sensors cleared; the new App process knows nothing about the gap.
    clock.advance(300)
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    ha2 = type(ha)()
    ha2.states = ha.states
    again = OccupancyRuntime(ha2, runtime.store, tmp_path)
    await again.start()
    ha2.connect()
    result = again.result(KAMAR)
    assert result.occupied is True and result.starting_up  # restored, held, a new quiet period
    assert result.vacancy_at == clock.now + __import__("datetime").timedelta(seconds=600)
    await again.stop()


async def test_a_damaged_state_file_is_reported_and_not_used(tmp_path, ha, clock):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "occupancy.json").write_text("{broken")
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    assert "not usable" in runtime.state_notice
    assert status(runtime) == ("vacant", "vacant")
    await runtime.stop()


async def test_all_sensors_offline_keeps_the_last_state_as_uncertain(tmp_path, ha, clock):
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "on", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    clock.advance(5)
    ha.set(PIR, "unavailable", clock.now)
    ha.set(RADAR, "unavailable", clock.now)
    view = runtime.room_view(KAMAR)
    assert (view["status"], view["occupied"]) == ("uncertain", True)
    assert {p["code"] for p in view["problems"]} >= {"all_offline", "sensor_unavailable"}
    assert view["warnings"] == 3
    rooms, _ = runtime.published_rooms()
    assert rooms[KAMAR]["occupied"] is True and rooms[KAMAR]["state"] == "uncertain"
    assert any(e["kind"] == "sensor" and "unavailable" in e["text"] for e in runtime.timeline.recent(KAMAR))
    clock.advance(5)
    ha.set(RADAR, "on", clock.now)  # reconnects
    assert status(runtime)[0] == "occupied"
    assert any(e["kind"] == "sensor" and "usable again" in e["text"] for e in runtime.timeline.recent(KAMAR))
    await runtime.stop()


async def test_activity_entities_refresh_but_never_occupy(tmp_path, ha, clock):
    switch = "event.kamar_switch"
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    ha.set(switch, "2026-11-21T11:00:00+00:00", clock.now)
    runtime = await make(tmp_path, ha, clock, rooms={KAMAR: room(activity=[switch])})
    ha.connect()
    clock.advance(10)
    ha.set(switch, clock.now.isoformat(), clock.now)
    assert status(runtime) == ("vacant", "vacant")  # never occupies
    ha.set(PIR, "on", clock.now)
    clock.advance(20)
    runtime._fire(KAMAR)
    ha.set(PIR, "off", clock.now)
    clock.advance(30)
    runtime._fire(KAMAR)
    first = runtime.result(KAMAR).vacancy_at
    clock.advance(200)
    ha.set(switch, clock.now.isoformat(), clock.now)  # someone pressed the switch
    result = runtime.result(KAMAR)
    assert result.vacancy_at > first
    assert runtime.room_view(KAMAR)["last_activity_source"].startswith("activity: ")
    # An unavailable state is not activity.
    ha.set(switch, "unavailable", clock.now)
    assert runtime.result(KAMAR).vacancy_at == result.vacancy_at
    await runtime.stop()


async def test_configuration_changes_apply_live(tmp_path, ha, clock):
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "on", clock.now)
    runtime = await make(tmp_path, ha, clock, timing={"startup_hold": 120})
    ha.connect()
    clock.advance(130)
    runtime._fire(KAMAR)
    engine = runtime.house.engines[KAMAR]
    store = runtime.store
    # Timing only: the same engine, no start-up hold.
    new = json.loads(json.dumps(store.config))
    new["rooms"][KAMAR]["timing"] = {"inactivity_timeout": 1200}
    prediction = runtime.predict(C.validate_config(new))
    assert prediction[KAMAR]["changes_now"] is False and prediction[KAMAR]["restarts"] is False
    assert runtime.house.engines[KAMAR] is engine and engine.timing.inactivity_timeout == 600  # unchanged by predict
    store.save(new, expected_revision=store.revision, reason="timing")
    assert runtime.house.engines[KAMAR] is engine and engine.timing.inactivity_timeout == 1200
    assert not runtime.result(KAMAR).starting_up
    # A sensor removed: the decision is kept, the start-up hold applies.
    new = json.loads(json.dumps(store.config))
    new["rooms"][KAMAR]["sensors"] = new["rooms"][KAMAR]["sensors"][1:]
    prediction = runtime.predict(C.validate_config(new))
    assert prediction[KAMAR]["restarts"] is True and prediction[KAMAR]["after"]["occupied"] is True
    store.save(new, expected_revision=store.revision, reason="sensor removed")
    assert runtime.house.engines[KAMAR] is not engine and runtime.result(KAMAR).starting_up
    # A room added, then disabled, then removed.
    new = json.loads(json.dumps(store.config))
    new["rooms"][STOVA] = room([("binary_sensor.stova_pir", "pir", "trigger")], name="Stova", adjacent=[KAMAR])
    ha.set("binary_sensor.stova_pir", "on", clock.now)
    assert runtime.predict(C.validate_config(new))[STOVA]["after"]["status"] == "occupied"
    store.save(new, expected_revision=store.revision, reason="room added")
    assert status(runtime, STOVA)[0] == "occupied"
    new["rooms"][STOVA]["enabled"] = False
    store.save(new, expected_revision=store.revision, reason="disabled")
    rooms, disabled = runtime.published_rooms()
    assert STOVA not in rooms and disabled == {STOVA: "Stova"}
    assert runtime.room_view(STOVA)["status"] == "off"
    del new["rooms"][STOVA]
    store.save(new, expected_revision=store.revision, reason="removed")
    assert runtime.published_rooms() == ({KAMAR: runtime.published_rooms()[0][KAMAR]}, {})
    await runtime.stop()


async def test_adjacent_motion_and_shared_sensor(tmp_path, ha, clock):
    shared = "binary_sensor.open_pir"
    for e in (shared, DOOR, "binary_sensor.stova_radar"):
        ha.set(e, "off", clock.now)
    rooms = {
        KAMAR: room([(shared, "pir", "trigger"), (DOOR, "door", "trigger")], adjacent=[STOVA]),
        STOVA: room([(shared, "pir", "trigger"), ("binary_sensor.stova_radar", "radar", "maintain")], name="Stova"),
    }
    runtime = await make(tmp_path, ha, clock, rooms=rooms)
    ha.connect()
    clock.advance(5)
    ha.set(shared, "on", clock.now)
    assert status(runtime, KAMAR)[0] == status(runtime, STOVA)[0] == "occupied"
    clock.advance(3)
    ha.set("binary_sensor.stova_radar", "on", clock.now)
    notes = {p["code"] for p in runtime.problems(KAMAR)}
    assert "adjacent_motion" in notes
    await runtime.stop()


async def test_timeline_and_state_are_persisted(tmp_path, ha, clock):
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    ha.set(PIR, "on", clock.now)
    await settle()
    await runtime.stop()
    saved = json.loads((tmp_path / "state" / "timeline.json").read_text())
    assert saved["schema"] == 1 and any(e["kind"] == "state" for e in saved["entries"])
    assert (tmp_path / "state" / "occupancy.json").exists()


@pytest.mark.parametrize("missing", [True, False])
async def test_missing_sensor_is_explained(tmp_path, ha, clock, missing):
    ha.set(PIR, "off", clock.now)
    if not missing:
        ha.set(RADAR, "unavailable", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    texts = [p["text"] for p in runtime.problems(KAMAR)]
    if missing:
        assert any("does not exist in Home Assistant" in t for t in texts)
    else:
        assert any("(radar) stopped reporting" in t for t in texts)
    await runtime.stop()


async def test_a_configuration_change_while_disconnected_never_vacates(tmp_path, ha, clock):
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    clock.advance(20)
    runtime._fire(KAMAR)
    ha.set(PIR, "off", clock.now)
    assert status(runtime)[1] == "possible_vacancy"
    ha.disconnect()
    clock.advance(3600)
    store = runtime.store
    new = json.loads(json.dumps(store.config))
    new["timing"]["inactivity_timeout"] = 300
    store.save(new, expected_revision=store.revision, reason="edited while away")
    assert status(runtime)[0] == "occupied"  # not evaluated on old knowledge
    ha.connect()
    result = runtime.result(KAMAR)
    assert result.occupied is True and result.vacancy_at >= clock.now
    await runtime.stop()


async def test_unusable_stored_records_are_dropped(tmp_path, ha, clock):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "occupancy.json").write_text(json.dumps({"schema": 1, "rooms": {
        KAMAR: {"state": "confirmed_occupancy", "occupied": True, "last_activity": "2026-11-21T10:00:00"},
        STOVA: {"state": "nonsense"}}}))
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = await make(tmp_path, ha, clock)
    ha.connect()
    assert runtime.stored_state == {} and "not usable" in runtime.state_notice
    assert status(runtime) == ("vacant", "vacant")
    await runtime.stop()


async def test_coming_back_from_unavailable_is_not_activity(tmp_path, ha, clock):
    switch = "event.kamar_switch"
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "off", clock.now)
    ha.set(switch, "unavailable", clock.now)
    runtime = await make(tmp_path, ha, clock, rooms={KAMAR: room(activity=[switch])})
    ha.connect()
    clock.advance(20)
    runtime._fire(KAMAR)
    ha.set(PIR, "off", clock.now)
    before = runtime.result(KAMAR).vacancy_at
    clock.advance(60)
    ha.set(switch, "2026-11-21T11:00:00+00:00", clock.now)  # restored after a restart: not a person
    assert runtime.result(KAMAR).vacancy_at == before
    clock.advance(10)
    ha.set(switch, clock.now.isoformat(), clock.now)  # a real press
    assert runtime.result(KAMAR).vacancy_at > before
    await runtime.stop()
