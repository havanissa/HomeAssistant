"""Phase 4.8 (0.6.8), pure logic: custom ambient categories, their reviewed changes, fixture capabilities,
the radar distance band and Priority Input configuration. No Home Assistant instance needed."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from custom_components.smart_occupancy import ambient as amb, ambient_levels as al, brightness as br
from custom_components.smart_occupancy.ambient import AmbientLevel, AmbientSettings, RoomAmbient
from custom_components.smart_occupancy.brightness import LightMatrix
from custom_components.smart_occupancy.fixtures import Fixture, FixtureKind, fixtures_from, restrict
from custom_components.smart_occupancy.lighting import Capabilities
from custom_components.smart_occupancy.lighting_config import LightingConfig, RoomLighting, migrate_store, rename_entity
from custom_components.smart_occupancy.planner import build_command
from custom_components.smart_occupancy.priority import PriorityInput, claims_for, inputs_from
from custom_components.smart_occupancy.profiles import ResolvedSetting
from custom_components.smart_occupancy.const import LightAction
from custom_components.smart_occupancy.radar import Distance, RadarConfig, RadarTracker, read_distance, simulate, to_metres
from custom_components.smart_occupancy.zones import ZoneConfig, ZoneDetection, zones_from

SIX = [{"id": "dark", "name": "Darkness", "start": 0}, {"id": "dim", "name": "Very dim", "start": 3},
       {"id": "low", "name": "Low light", "start": 10}, {"id": "normal", "name": "Normal light", "start": 40},
       {"id": "bright", "name": "Bright", "start": 120}, {"id": "vbright", "name": "Very bright", "start": 300}]


def levels(items) -> tuple[AmbientLevel, ...]:
    return amb.valid_levels(items)


# ======================================================================= categories
def test_custom_categories_validation_and_exact_boundaries() -> None:
    six = levels(SIX)
    settings = AmbientSettings(levels=six)
    assert settings.thresholds == (3, 10, 40, 120, 300) and settings.keys[0] == "dark"
    # A reading exactly at a threshold belongs to the higher category; the last one has no upper bound.
    t = settings.thresholds
    assert [settings.keys[amb.raw_level(v, t)] for v in (0, 2.99, 3, 9.9, 10, 40, 119, 120, 300, 99999)] == [
        "dark", "dark", "dim", "dim", "low", "normal", "normal", "bright", "vbright", "vbright"]
    bad = {
        "duplicate id": [*SIX[:2], {"id": "dim", "name": "Other", "start": 20}],
        "duplicate name": [*SIX[:2], {"id": "x", "name": "very DIM", "start": 20}],
        "negative": [SIX[0], {"id": "x", "name": "X", "start": -1}],
        "not increasing": [SIX[0], {"id": "a", "name": "A", "start": 10}, {"id": "b", "name": "B", "start": 10}],
        "empty name": [SIX[0], {"id": "a", "name": "  ", "start": 10}],
        "first not 0": [{"id": "a", "name": "A", "start": 1}, {"id": "b", "name": "B", "start": 10}],
        "not a number": [SIX[0], {"id": "a", "name": "A", "start": "ten"}],
        "infinite": [SIX[0], {"id": "a", "name": "A", "start": float("inf")}],
        "bad id": [SIX[0], {"id": "Bad Id", "name": "A", "start": 10}],
        "one only": [SIX[0]],
    }
    for why, items in bad.items():
        assert amb.level_error(items) is not None, why
        assert amb.valid_levels(items) is None, why
    twelve = [{"id": f"l{i}", "name": f"L{i}", "start": i * 10} for i in range(12)]
    assert amb.level_error(twelve) is None and amb.level_error([*twelve, {"id": "x", "name": "X", "start": 500}])
    assert amb.level_error([*twelve, {"id": "x", "name": "X", "start": 500}], max_levels=13) is None
    # A room's own structure; another room keeps the global one.
    config = LightingConfig.from_dict({"rooms": {"kno": {"ambient": {"enabled": True, "levels": SIX}}, "wc": {}}})
    assert config.ambient_for("kno").keys == tuple(x["id"] for x in SIX)
    assert config.ambient_for("wc").keys == ("none", "low", "bright")
    # The fallback category falls back to the second one when it does not exist in a structure.
    assert AmbientSettings(levels=six, fallback_level="none").fallback_index() == 1


def test_classification_status_and_tracker_use_ids() -> None:
    settings = AmbientSettings.from_dict({"levels": SIX, "stable_seconds": 0}, defaults=True)
    tracker = amb.AmbientTracker()
    now = datetime(2026, 11, 16, 12)
    est = amb.Estimate(55, "high", "x", "Natural light: 55 lx", measured=True)
    ev, _ = amb.classify(tracker, est, settings, now)
    st = amb.status(tracker, ev, settings, enabled=True)
    assert st.key == "normal" and st.name == "Normal light" and st.as_dict()["levels"][3]["below"] == 120
    keys = settings.keys
    assert amb.AmbientTracker.restore(tracker.persist(keys), keys).committed == 3
    assert amb.AmbientTracker.restore(tracker.persist(keys), ("a", "b")).committed is None  # gone: classified again
    # Lamp-lit readings still never raise the category (the feedback loop).
    bound = amb.Estimate(400, "high", "x", "at most 400", measured=True, upper_bound=True)
    for i in range(5):
        amb.classify(tracker, bound, settings, now + timedelta(minutes=i + 1))
    assert tracker.committed == 3


def _config_with(matrix: LightMatrix, room_levels=None) -> LightingConfig:
    room = RoomLighting(ambient=RoomAmbient(enabled=True, settings=AmbientSettings(levels=room_levels)),
                        brightness={matrix.entity_id: matrix},
                        zones=(ZoneConfig(id="desk", name="Desk"),))
    return LightingConfig(rooms={"kno": room, "wc": RoomLighting(), "own": RoomLighting(
        ambient=RoomAmbient(settings=AmbientSettings(levels=levels(SIX))))})


FLOOR = LightMatrix("light.floor", room={"none": 40, "low": 25, "bright": 0}, zones={"desk": {"none": 70, "low": 50}})


def test_rename_add_remove_and_reviewed_reassignment() -> None:
    config = _config_with(FLOOR)
    base = config.ambient.levels
    # Rename: the id stays, every value stays.
    renamed = (base[0], replace(base[1], name="Dim"), base[2])
    change = al.plan(config, ["kno"], None, renamed)
    assert change.ready and change.rooms[0].renamed == [("low", "Low light", "Dim")]
    after = al.apply(config, change)
    assert after.room("kno").brightness["light.floor"] == FLOOR
    assert after.ambient_for("kno").name_of("low") == "Dim" and after.ambient_for("own").keys[0] == "dark"
    assert "own" in change.unaffected  # a room with its own categories is never overwritten
    # Add: the new category is Not configured everywhere (nothing invented).
    added = (*base[:2], AmbientLevel("normal", "Normal", 12), base[2])
    change = al.plan(config, ["kno"], None, added)
    assert change.ready and change.rooms[0].added == ["normal"]
    m = al.apply(config, change).room("kno").brightness["light.floor"]
    assert m.value("room", "normal") is None and m.value("desk", "normal") is None and m.room["low"] == 25
    # Remove a category that has values: needs a review; moving them replaces the target's value.
    removed = (base[0], base[2])
    change = al.plan(config, ["kno"], None, removed)
    assert not change.ready and change.unresolved == ["low"]
    cells = change.as_dict()["rooms"][0]["cells"]
    assert {(c["column"], c["value"]) for c in cells} == {("room", 25), ("desk", 50)}
    assert change.rooms[0].fallback == {"from": "Low light", "to": "Bright", "id": "bright"}  # (the fallback)
    try:
        al.apply(config, change)
        raise AssertionError("an unresolved removal must not apply")
    except ValueError:
        pass
    change = al.plan(config, ["kno"], None, removed, {"low": "none"})
    assert change.ready
    m = al.apply(config, change).room("kno").brightness["light.floor"]
    assert m.room == {"none": 25, "bright": 0} and m.zones["desk"] == {"none": 50}
    assert al.apply(config, change).ambient.fallback_level == "none"
    # Delete instead.
    change = al.plan(config, ["kno"], None, removed, {"low": None})
    m = al.apply(config, change).room("kno").brightness["light.floor"]
    assert m.room == {"none": 40, "bright": 0} and m.zones["desk"] == {"none": 70}
    # A move to a category that does not exist afterwards is refused.
    assert al.plan(config, ["kno"], None, removed, {"low": "low"}).errors


def test_room_levels_and_use_global_levels() -> None:
    config = _config_with(FLOOR)
    six = levels(SIX)
    # The room gets its own structure: none/low/bright values must be reviewed (low still exists).
    change = al.plan(config, ["kno"], "kno", six)
    assert set(change.unresolved) == {"none"} and change.rooms[0].added == ["dark", "dim", "normal", "vbright"]
    change = al.plan(config, ["kno"], "kno", six, {"none": "dark"})
    after = al.apply(config, change)
    m = after.room("kno").brightness["light.floor"]
    assert after.ambient_for("kno").keys == tuple(x["id"] for x in SIX) and after.ambient_for("wc").keys[0] == "none"
    assert m.room["dark"] == 40 and m.room["low"] == 25 and m.room.get("dim") is None and m.room["bright"] == 0
    # Use Global Levels: back to the global structure, with the same review.
    back = al.plan(after, ["kno"], "kno", None)
    assert back.unresolved == ["dark"]
    restored = al.apply(after, al.plan(after, ["kno"], "kno", None, {"dark": "none"}))
    assert restored.room("kno").ambient.settings.levels is None
    assert restored.room("kno").brightness["light.floor"].room == {"none": 40, "low": 25, "bright": 0}


def test_templates_map_onto_custom_categories() -> None:
    general = br.find_template((), "general")
    six = levels(SIX)
    mapped = general.for_levels(six)
    # Same id: its own value; otherwise the value of the default category its start lies in.
    assert mapped.room == {"dark": 40, "dim": 40, "low": 25, "normal": 0, "bright": 0, "vbright": 0}
    matrix = general.matrix("light.x", ["desk"], levels=six)
    assert matrix.zones["desk"]["low"] == 50 and set(matrix.room) == {x["id"] for x in SIX}
    saved = br.Template.from_dict({"id": "mine", "name": "Mine", "levels": SIX, "room": {"dark": 1, "vbright": 99}})
    assert saved.levels == six and saved.room["vbright"] == 99 and saved.for_levels(amb.DEFAULT_LEVELS).room == {
        "none": 1, "low": None, "bright": None}


def test_store_migration_preserves_the_three_categories_and_values() -> None:
    old = {
        "ambient": {"thresholds": [5, 20], "hysteresis": 2.0, "stable_seconds": 120, "fallback": "hold",
                    "fallback_level": "low", "transition": 2.0, "dead_band": 3, "min_interval": 0},
        "brightness_templates": [],
        "rooms": {"kno": {"ambient": {"enabled": True, "thresholds": [8, 30]}, "relays": ["switch.relay"],
                          "zones": [{"id": "desk", "name": "Desk", "sensors": ["binary_sensor.desk"]}],
                          "brightness": {"light.floor": {"room": {"none": 15, "low": 15, "bright": 0},
                                                         "zones": {"desk": {"none": 65, "low": 45, "bright": 80}}}}},
                  "wc": {"ambient": {"enabled": False}}},
    }
    migrated = migrate_store(1, 8, old)
    assert [lv["id"] for lv in migrated["ambient"]["levels"]] == ["none", "low", "bright"]
    assert migrated["ambient"]["max_levels"] == 12 and migrated["priority_lock"] is False
    room = migrated["rooms"]["kno"]
    assert [lv["start"] for lv in room["ambient"]["levels"]] == [0, 8, 30]
    assert room["fixtures"] == {} and room["priority_inputs"] == []
    assert room["zones"][0]["detection"] == "sensors" and room["zones"][0]["radar"] is None
    config = LightingConfig.from_dict(migrated)
    assert config.ambient_for("kno").thresholds == (8, 30) and config.ambient_for("wc").thresholds == (5, 20)
    assert config.room("kno").brightness["light.floor"].room == {"none": 15, "low": 15, "bright": 0}
    assert config.room("kno").brightness["light.floor"].zones["desk"] == {"none": 65, "low": 45, "bright": 80}
    assert config.room("kno").zones[0].detection == ZoneDetection.SENSORS and config.room("kno").relays == (
        "switch.relay",)
    assert config.room("kno").fixtures == {} and config.room("kno").priority_inputs == ()
    # Written back, a 0.6.7 install still reads its three categories.
    assert config.as_dict()["ambient"]["thresholds"] == [5, 20]
    assert LightingConfig.from_dict(config.as_dict()) == config


# ======================================================================= fixtures
def test_dumb_light_narrows_capabilities_and_never_sends_attributes() -> None:
    full = Capabilities(known=True, brightness=True, color_temp=True, color=True, transition=True, effect=True)
    dumb = Fixture("light.lamp", FixtureKind.DUMB, name="Cupboard Light")
    assert restrict(full, dumb) == Capabilities(known=True)
    assert restrict(full, Fixture("light.lamp", FixtureKind.COLOR)) == full  # descriptive only
    assert restrict(Capabilities(known=True), Fixture("light.x", FixtureKind.COLOR)) == Capabilities(known=True)
    mood = ResolvedSetting(action=LightAction.ON, brightness_pct=40, color_temp_kelvin=2700, transition=2,
                           rgb_color=(255, 0, 0), effect="colorloop")
    service, data, _req, _notes = build_command(mood, restrict(full, dumb))
    assert (service, data) == ("turn_on", {})
    service, data, _req, _notes = build_command(ResolvedSetting(action=LightAction.OFF, transition=2),
                                                restrict(full, dumb))
    assert (service, data) == ("turn_off", {})
    assert fixtures_from({"light.a": {"kind": "custom", "label": "Cupboard"}, "light.b": {"kind": "nope"},
                          "bad": {"kind": "dumb"}}) == {"light.a": Fixture("light.a", FixtureKind.CUSTOM, "Cupboard")}
    assert Fixture("light.a", FixtureKind.CUSTOM, "Cupboard").describe() == "Cupboard"
    assert dumb.describe() == "Dumb Light — On/Off only"
    assert br.describe_value(100, onoff=True) == "On" and br.describe_value(0, onoff=True) == "Off"


# ======================================================================= radar
CFG = RadarConfig(presence="binary_sensor.radar", distance="sensor.radar_distance", min_m=1.0, max_m=3.0)
T0 = datetime(2026, 11, 16, 12)


def d(m: float | None) -> Distance:
    return Distance(m, None) if m is not None else Distance(None, "unavailable")


def test_distance_units_and_invalid_measurements() -> None:
    assert to_metres("2100", "mm").metres == 2.1 and to_metres(210, "cm").metres == 2.1
    assert to_metres("2.1", "m").metres == 2.1
    for value, unit, problem in (("2", "ft", "unsupported unit 'ft'"), ("2", None, "no unit"),
                                 ("-1", "m", "negative distance"), ("nan", "m", "not a finite number"),
                                 ("inf", "m", "not a finite number"), ("x", "m", "not a number"),
                                 ("unavailable", "m", "unavailable"), (None, "m", "unavailable"),
                                 (True, "m", "not a number")):
        reading = to_metres(value, unit)
        assert not reading.valid and reading.problem.startswith(problem), (value, unit, reading)

    class S:
        def __init__(self, state, attrs, at=T0):
            self.state, self.attributes, self.last_reported, self.last_updated = state, attrs, at, at

    assert read_distance(CFG, S("1500", {"unit_of_measurement": "mm"})).metres == 1.5
    attr = replace(CFG, attribute="target_distance", unit="cm")
    assert read_distance(attr, S("on", {"target_distance": 250})).metres == 2.5
    assert not read_distance(replace(CFG, unit="cm"), S("150", {"unit_of_measurement": "mm"})).valid
    stale = replace(CFG, max_age=60)
    assert read_distance(stale, S("2", {"unit_of_measurement": "m"}, T0), T0 + timedelta(seconds=30)).valid
    assert "stale" in read_distance(stale, S("2", {"unit_of_measurement": "m"}, T0), T0 + timedelta(seconds=61)).problem


def test_band_boundaries_entry_exit_delays_and_hysteresis() -> None:
    tr = RadarTracker(CFG)
    # Exact boundaries are inside; the entry delay must hold.
    for metres in (1.0, 3.0):
        tr = RadarTracker(CFG)
        assert tr.evaluate(True, d(metres), T0).phase == "entering"
        assert tr.evaluate(True, d(metres), T0 + timedelta(seconds=1.9)).active is False
        assert tr.evaluate(True, d(metres), T0 + timedelta(seconds=2)).active is True
    tr = RadarTracker(CFG)
    assert not tr.evaluate(True, d(0.99), T0).active and tr.phase == "inactive"
    assert not tr.evaluate(True, d(3.01), T0).active
    # Inside the hysteresis margin an active zone stays; a new one does not enter.
    tr.evaluate(True, d(2.9), T0)
    assert tr.evaluate(True, d(2.9), T0 + timedelta(seconds=2)).active
    assert tr.evaluate(True, d(3.15), T0 + timedelta(seconds=3)).phase == "active"   # max + 0.2 margin
    assert tr.evaluate(True, d(0.85), T0 + timedelta(seconds=4)).phase == "active"   # min - 0.2 margin
    # Outside the margin: released only after the exit delay; coming back cancels it.
    ev = tr.evaluate(True, d(3.25), T0 + timedelta(seconds=5))
    assert ev.active and ev.phase == "leaving" and ev.due == T0 + timedelta(seconds=20)
    assert tr.evaluate(True, d(3.1), T0 + timedelta(seconds=10)).phase == "active"   # (a person at 3.0 m)
    tr.evaluate(True, d(3.25), T0 + timedelta(seconds=11))
    assert tr.evaluate(True, d(3.25), T0 + timedelta(seconds=25.9)).active
    assert not tr.evaluate(True, d(3.25), T0 + timedelta(seconds=26)).active
    # Never active from a fluctuation that does not hold the entry delay.
    tr = RadarTracker(CFG)
    for i in range(10):
        tr.evaluate(True, d(2.95 if i % 2 else 3.05), T0 + timedelta(seconds=i * 1.5))
    assert not tr.evaluate(True, d(3.05), T0 + timedelta(seconds=16)).active


def test_presence_clear_and_unavailable_fail_safes() -> None:
    tr = RadarTracker(CFG)
    # Distance alone is never presence: a stale distance in range while presence is clear.
    assert not tr.evaluate(False, d(2.0), T0).active
    assert not tr.evaluate(False, d(2.0), T0 + timedelta(seconds=10)).active
    tr.restore(True, T0)
    ev = tr.evaluate(False, d(2.0), T0)
    assert ev.phase == "leaving" and "presence clear" in ev.reason
    assert not tr.evaluate(False, d(2.0), T0 + timedelta(seconds=15)).active
    # Distance unavailable while presence is detected: never read as 0 m.
    for failsafe, active_after in (("hold", True), ("vacant", False), ("occupied", True)):
        tr = RadarTracker(CFG)
        tr.restore(True, T0)
        tr.evaluate(True, d(None), T0, failsafe=failsafe)
        assert tr.evaluate(True, d(None), T0 + timedelta(seconds=16), failsafe=failsafe).active is active_after
        fresh = RadarTracker(CFG)
        assert fresh.evaluate(True, d(None), T0, failsafe=failsafe).active is (failsafe == "occupied")
    # A sustained explicit absence releases whatever the fail-safe.
    tr = RadarTracker(CFG)
    tr.restore(True, T0)
    tr.evaluate(False, d(None), T0, failsafe="occupied")
    assert not tr.evaluate(False, d(None), T0 + timedelta(seconds=15), failsafe="occupied").active
    # Presence unknown.
    tr = RadarTracker(CFG)
    tr.restore(True, T0)
    ev = tr.evaluate(None, d(2.0), T0)
    assert ev.active and not ev.known
    # Test distances (simulation; no delays, nothing sent).
    sim = simulate(CFG, True, 3.1, active=False)
    assert sim["settled"]["active"] is False and sim["where"] == "within the hysteresis margin"
    assert simulate(CFG, True, 3.1, active=True)["settled"]["active"] is True
    assert simulate(CFG, True, 2.0, active=False)["settled"]["active"] is True
    assert simulate(CFG, False, 2.0, active=True)["settled"]["active"] is False


def test_radar_zone_config_round_trip_and_rename() -> None:
    zone = ZoneConfig.from_dict({"id": "desk", "name": "Desk", "detection": "radar",
                                 "radar": {**CFG.as_dict(), "min_m": "1", "max_m": 3}})
    assert zone.is_radar and zone.radar == CFG and ZoneConfig.from_dict(zone.as_dict()) == zone
    assert RadarConfig.from_dict({**CFG.as_dict(), "min_m": 3, "max_m": 1}) is None
    assert zones_from([{"id": "a", "name": "A"}])[0].detection == ZoneDetection.SENSORS
    config = LightingConfig(rooms={"kno": RoomLighting(zones=(zone,))})
    renamed = rename_entity(config, "sensor.radar_distance", "sensor.desk_radar_distance")
    assert renamed.room("kno").zones[0].radar.distance == "sensor.desk_radar_distance"


# ======================================================================= priority inputs
def test_priority_input_polarity_claims_and_defaults() -> None:
    pi = PriorityInput.from_dict({"id": "door", "name": "Cupboard Door", "entity_id": "binary_sensor.door",
                                  "targets": ["light.cupboard"]})
    assert pi.enabled is False and pi.brightness_pct == 100 and pi.release_delay == 2
    assert (pi.reading("on"), pi.reading("off"), pi.reading("unknown"), pi.reading("unavailable")) == (
        True, False, None, None)
    nc = replace(pi, active_when="off")  # a normally closed contact
    assert (nc.reading("on"), nc.reading("off")) == (False, True)
    # Never enabled without an input and a target.
    assert PriorityInput.from_dict({"id": "x", "enabled": True, "targets": ["light.a"]}).enabled is False
    assert PriorityInput.from_dict({"id": "x", "enabled": True, "entity_id": "binary_sensor.a"}).enabled is False
    a = replace(pi, id="a", name="A", brightness_pct=60, targets=("light.cupboard", "light.shelf"))
    b = replace(pi, id="b", name="B", brightness_pct=80)
    c = replace(pi, id="c", name="C", brightness_pct=80)
    claims = claims_for([a, b, c], {})
    assert claims["light.cupboard"].winner.id == "b" and claims["light.shelf"].winner.id == "a"
    assert claims_for([replace(a, brightness_pct=None), b], {})["light.cupboard"].winner.id == "b"
    assert inputs_from([pi.as_dict(), pi.as_dict(), {"bad": 1}]) == (pi,)
    config = LightingConfig(rooms={"kno": RoomLighting(priority_inputs=(pi,))})
    renamed = rename_entity(config, "light.cupboard", "light.cupboard_2")
    assert renamed.room("kno").priority_inputs[0].targets == ("light.cupboard_2",)
