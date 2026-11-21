"""Configuration: strict validation, revisions, atomic saves with backups, recovery, export/import, Smart Comfort import."""

from __future__ import annotations

import copy
import json

import pytest

from sod import config as C, smart_comfort_import as SCI
from tests.conftest import KAMAR, STOVA, room


def config(**rooms):
    return {"timing": C.empty_config()["timing"], "rooms": rooms}


def problems(raw) -> list[str]:
    with pytest.raises(C.ConfigError) as err:
        C.validate_config(raw)
    return [f"{p['path']}: {p['message']}" for p in err.value.problems]


def test_defaults_match_the_engine():
    assert C.empty_config()["timing"] == {"correlation_window": 10, "exit_confirmation": 300, "inactivity_timeout": 600,
                                          "dropout_grace": 30, "trigger_stuck_after": 1800, "presence_stuck_after": 0,
                                          "startup_hold": 120}


def test_valid_config_is_canonical():
    raw = config(**{KAMAR: {"sensors": [{"entity_id": "binary_sensor.pir", "kind": "pir"}]}})
    clean = C.validate_config(raw)
    assert clean["rooms"][KAMAR] == {"name": KAMAR, "enabled": True, "sensors": [
        {"entity_id": "binary_sensor.pir", "kind": "pir", "role": "trigger", "inverted": False, "enabled": True}],
        "activity": [], "timing": {}, "adjacent": []}


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda r: r["sensors"].append(dict(r["sensors"][0])), "is assigned twice"),
    (lambda r: r["sensors"][0].update(kind="laser"), "must be one of"),
    (lambda r: r["sensors"][0].update(role="always"), "must be one of"),
    (lambda r: r["sensors"][0].update(entity_id="not an id"), "not a valid entity id"),
    (lambda r: r["sensors"][0].update(entity_id="light.lamp"), "must be one of: binary_sensor"),
    (lambda r: r["sensors"][0].update(inverted="yes"), "must be true or false"),
    (lambda r: r.update(timing={"inactivity_timeout": 5}), "must be between 30 and 14400"),
    (lambda r: r.update(timing={"inactivity_timeout": "600"}), "whole number"),
    (lambda r: r.update(timing={"sunset": 5}), "unknown timing parameter"),
    (lambda r: r.update(adjacent=["nowhere"]), "is not a configured room"),
    (lambda r: r.update(adjacent=[KAMAR]), "not another room"),
    (lambda r: r.update(activity=["binary_sensor.kamar_pir"]), "already a sensor"),
    (lambda r: r.update(lights=["light.x"]), "unknown fields"),
    (lambda r: r.update(sensors=[{"entity_id": "binary_sensor.door", "kind": "door", "role": "trigger"}]),
     "at least one enabled sensor that can detect presence"),
    (lambda r: r["sensors"].__setitem__(slice(None), [{**s, "enabled": False} for s in r["sensors"]]),
     "at least one enabled sensor"),
])
def test_invalid_rooms_are_refused_with_the_reason(mutate, expected):
    r = room()
    mutate(r)
    found = problems(config(**{KAMAR: r}))
    assert any(expected in p for p in found), found


def test_disabled_room_needs_no_usable_sensor():
    C.validate_config(config(**{KAMAR: room([("binary_sensor.door", "door", "trigger")], enabled=False)}))


def test_warnings_do_not_block():
    clean = C.validate_config(config(**{KAMAR: room([("binary_sensor.pir", "pir", "trigger")],
                                                    timing={"exit_confirmation": 900})}))
    messages = [w["message"] for w in C.warnings_of(clean)]
    assert any("fast exit timeout is longer" in m for m in messages)
    assert any("Only motion sensors" in m for m in messages)


def test_house_spec_uses_inherited_timing_and_only_enabled_neighbours():
    clean = C.validate_config(config(**{
        KAMAR: room(timing={"inactivity_timeout": 900}, adjacent=[STOVA]),
        STOVA: room([("binary_sensor.stova_pir", "pir", "trigger")], name="Stova", enabled=False),
    }))
    spec = C.house_spec(clean)
    assert spec.area_ids == (KAMAR,)
    kamar = spec.room(KAMAR)
    assert kamar.timing.inactivity_timeout == 900 and kamar.timing.exit_confirmation == 300
    assert kamar.adjacent == ()


def test_save_checks_the_revision_backs_up_and_writes_atomically(tmp_path):
    store = C.ConfigStore(tmp_path)
    store.load()
    first = store.revision
    store.save(config(**{KAMAR: room()}), expected_revision=first, reason="room added", by="admin")
    assert store.path.exists() and not list(store.dir.glob("*.tmp"))
    with pytest.raises(C.ConfigError) as err:
        store.save(config(), expected_revision=first, reason="stale editor")
    assert err.value.code == "stale" and err.value.status == 409
    second = store.revision
    store.save(config(**{KAMAR: room(timing={"inactivity_timeout": 900})}), expected_revision=second, reason="x")
    backups = store.backups()
    assert len(backups) == 1 and backups[0]["rooms"] == 1
    assert (store.dir / "config.json.bak").exists()
    document = json.loads(store.path.read_text())
    assert document["revision"] == store.revision and document["by"] is None
    # Reloading gives the same configuration and revision.
    again = C.ConfigStore(tmp_path)
    again.load()
    assert again.config == store.config and again.revision == store.revision and again.recovery is None


def test_an_invalid_save_changes_nothing(tmp_path):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save(config(**{KAMAR: room()}), expected_revision=store.revision, reason="a")
    before = store.path.read_bytes()
    bad = config(**{KAMAR: room([("binary_sensor.door", "door", "trigger")])})
    with pytest.raises(C.ConfigError):
        store.save(bad, expected_revision=store.revision, reason="b")
    assert store.path.read_bytes() == before


def test_backups_are_bounded(tmp_path):
    store = C.ConfigStore(tmp_path)
    store.load()
    for i in range(C.BACKUPS + 5):
        store.save(config(**{KAMAR: room(timing={"inactivity_timeout": 600 + i})}), expected_revision=store.revision,
                   reason="change")
    assert len(store.backups()) == C.BACKUPS


def test_a_damaged_file_is_never_used_and_never_overwritten(tmp_path):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save(config(**{KAMAR: room()}), expected_revision=store.revision, reason="a")
    store.save(config(**{KAMAR: room(timing={"inactivity_timeout": 900})}), expected_revision=store.revision,
               reason="b")
    good = copy.deepcopy(store.config)
    store.path.write_text('{"schema": 1, "config": {"rooms": ')  # truncated write
    recovered = C.ConfigStore(tmp_path)
    recovered.load()
    assert recovered.recovery["used"] == "config.json.bak"
    assert recovered.config["rooms"][KAMAR]["timing"] == {}  # the previous document
    assert list(recovered.dir.glob("config.damaged-*.json"))  # set aside, not deleted
    # Tampered content (revision mismatch) is refused as well.
    store2 = C.ConfigStore(tmp_path / "other")
    store2.load()
    store2.save(config(**{KAMAR: room()}), expected_revision=store2.revision, reason="a")
    data = json.loads(store2.path.read_text())
    data["config"]["rooms"][KAMAR]["enabled"] = False
    store2.path.write_text(json.dumps(data))
    third = C.ConfigStore(tmp_path / "other")
    third.load()
    assert third.recovery is not None and third.config["rooms"] == {}
    assert good  # unchanged original


def test_export_and_reviewed_import_round_trip(tmp_path):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save(config(**{KAMAR: room()}), expected_revision=store.revision, reason="a")
    exported = store.export()
    assert exported["format"] == C.EXPORT_FORMAT
    assert store.parse_import(json.loads(json.dumps(exported))) == store.config
    with pytest.raises(C.ConfigError):
        store.parse_import({"format": "smart_lighting.core_config", "options": {}})
    backup_name = None
    store.save(config(), expected_revision=store.revision, reason="cleared")
    backup_name = store.backups()[0]["name"]
    assert store.parse_import(store.backup_document(backup_name))["rooms"][KAMAR]
    with pytest.raises(C.ConfigError):
        store.backup_document("../config.json")


def test_diff_lists_what_changes():
    before = C.validate_config(config(**{KAMAR: room()}))
    after = C.validate_config(config(**{KAMAR: room(timing={"inactivity_timeout": 900}),
                                        STOVA: room([("binary_sensor.s", "pir", "trigger")], name="Stova")}))
    changes = C.diff(before, after)
    assert {"type": "room_changed", "area_id": KAMAR, "name": "Kamar Suður", "fields": ["timing"]} in changes
    assert any(c["type"] == "room_added" and c["area_id"] == STOVA for c in changes)


# --------------------------------------------------------------------------- Smart Comfort import
def smart_comfort_options():
    return {
        "config_version": 2, "operating_mode": "observe", "mood": {"main_mood_entity": "select.mood"},
        "categories": [{"key": "lamps", "name": "Lamps"}],
        "rooms": {
            "kamar_nordur": {"enabled": True, "sensors": [
                {"entity_id": "binary_sensor.kamar_radar", "kind": "radar", "role": "maintain", "inverted": False},
                {"entity_id": "binary_sensor.kamar_door", "kind": "door", "role": "trigger", "inverted": False},
                {"entity_id": "input_boolean.nobara_pc", "kind": "pc_activity", "role": "maintain", "inverted": True,
                 "enabled": False}],
                "lights": ["light.bed_light"], "reference_occupancy_entity": "input_boolean.tilstadar_kamar",
                "mood_overrides": {"Dagur": {}}, "light_overrides": {}, "timing": {"exit_confirmation": 120},
                "adjacent_rooms": ["gongd", "stova"]},
            "stova": {"enabled": True, "sensors": [
                {"entity_id": "binary_sensor.stova_door", "kind": "door", "role": "trigger", "inverted": False},
                {"entity_id": "binary_sensor.stova_bed", "kind": "bed"}],
                "lights": [], "timing": {}, "adjacent_rooms": []},
            "lights_only": {"enabled": False, "sensors": [], "lights": ["light.x"]},
        },
        "global_profiles": {}, "occupancy": {"correlation_window": 10, "exit_confirmation": 240,
                                             "inactivity_timeout": 900, "dropout_grace": 30,
                                             "trigger_stuck_after": 1800, "presence_stuck_after": 0,
                                             "startup_hold": 120},
    }


FORMATS = {
    "core": lambda o: {"format": "smart_lighting.core_config", "version": 1, "options": o},
    "rooms_backup": lambda o: {"format": "smart_occupancy.rooms_backup", "version": 1, "rooms": o["rooms"],
                               "occupancy": o["occupancy"]},
    "migration": lambda o: {"format": "smart_lighting.migration_backup", "version": 1,
                            "core_config": {"format": "smart_lighting.core_config", "options": o}},
    "migration_legacy": lambda o: {"format": "smart_lighting.migration_backup", "version": 1, "core_config": None,
                                   "legacy_options": o},
}


@pytest.mark.parametrize("fmt", FORMATS)
def test_smart_comfort_review_takes_only_occupancy_settings(fmt):
    document = FORMATS[fmt](smart_comfort_options())
    original = json.dumps(document, sort_keys=True)
    review = SCI.review(document, C.empty_config(), area_name=lambda a: {"kamar_nordur": "Kamar Norður"}.get(a),
                        area_exists=lambda a: a != "gongd", entity_exists=lambda e: e != "input_boolean.nobara_pc")
    assert json.dumps(document, sort_keys=True) == original  # never changed
    rooms = {r["area_id"]: r for r in review["rooms"]}
    assert set(rooms) == {"kamar_nordur", "stova"}  # the lighting-only room is skipped
    kamar = rooms["kamar_nordur"]
    assert kamar["name"] == "Kamar Norður" and kamar["room"]["timing"] == {"exit_confirmation": 120}
    assert kamar["room"]["sensors"][2] == {"entity_id": "input_boolean.nobara_pc", "kind": "pc_activity",
                                           "role": "maintain", "inverted": True, "enabled": False}
    assert kamar["missing_entities"] == ["input_boolean.nobara_pc"]
    assert any("Ignored (not occupancy)" in n for n in kamar["notes"])
    assert rooms["stova"]["room"]["sensors"][1]["role"] == "maintain"  # Smart Comfort's default for the kind
    assert rooms["stova"]["room"]["enabled"] is True
    assert review["timing"]["inactivity_timeout"] == 900 and review["timing_differs"]


def test_smart_comfort_apply_merges_selected_rooms_and_drops_unknown_neighbours():
    document = FORMATS["core"](smart_comfort_options())
    current = C.validate_config(config(**{KAMAR: room()}))
    new, notes = SCI.apply(document, current, ["kamar_nordur", "stova"], False, area_name=lambda a: None)
    assert set(new["rooms"]) == {KAMAR, "kamar_nordur", "stova"}
    assert new["rooms"]["kamar_nordur"]["adjacent"] == ["stova"]  # gongd is not configured here
    assert any("gongd" in n for n in notes)
    assert new["timing"] == current["timing"]
    with_timing, _ = SCI.apply(document, current, ["kamar_nordur"], True, area_name=lambda a: None)
    assert with_timing["timing"]["inactivity_timeout"] == 900
    with pytest.raises(C.ConfigError):
        SCI.apply(document, current, ["nowhere"], False, area_name=lambda a: None)
    with pytest.raises(C.ConfigError):
        SCI.review({"format": "something_else"}, current, area_name=lambda a: None, area_exists=lambda a: True,
                   entity_exists=lambda e: True)
