"""Phase 0.3: the specialised designers, presence sources, simulation and their place in the App.

The designers' rules in Home Assistant's own engine are tested in ``tests/automation_builder/test_designers_runtime.py``.
"""

from __future__ import annotations

import copy
import json

import pytest

from ab import blueprint, compiler, conflicts, designers, model, simulate
from ab.designers import common as C, cover as CV, gallery, lighting as L, presence as P, solar
from ab.model import ConfigError
import builders as B
from conftest import ADMIN, state

SOFA, KITCHEN, ROOM = "z_00000001", "z_00000002", "z_00000003"
LAMP, SPOTS = "light.sofa_lamp", "light.kitchen_spots"
MOOD, LUX = "input_select.mood", "sensor.lux"


def sources():
    return {"sources": {
        SOFA: {"name": "Sofa", "kind": "zone", "entity_id": "binary_sensor.sofa_zone", "entry_seconds": 5},
        KITCHEN: {"name": "Kitchen", "kind": "zone", "entity_id": "binary_sensor.kitchen_zone"},
        ROOM: {"name": "Stova", "kind": "room", "entity_id": "binary_sensor.stova", "exit_seconds": 60},
    }}


def lighting(**over):
    d = {"id": "g_00000001", "kind": "lighting", "version": 1, "lights": [LAMP, SPOTS], "primary": "presence",
         "mood": {"enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge"],
                  "cells": {LAMP: {"Hygge": {"kind": "on", "brightness_pct": 40, "color_temp_kelvin": 2700}}}},
         "presence": {"enabled": True, "policy": "priority",
                      "columns": [{"source": SOFA, "state": "occupied"}, {"source": KITCHEN, "state": "occupied"},
                                  {"source": ROOM, "state": "vacant"}],
                      "cells": {LAMP: {f"{SOFA}:occupied": {"kind": "mood"}, f"{ROOM}:vacant": {"kind": "off"}},
                                SPOTS: {f"{SOFA}:occupied": {"kind": "on", "brightness_pct": 20},
                                        f"{KITCHEN}:occupied": {"kind": "on", "brightness_pct": 100}}}}}
    d.update(over)
    return d


def config(*designer_list, targets=(LAMP, SPOTS), rules=(), presence=None):
    return model.validate_config({"settings": {}, "presence": sources() if presence is None else presence,
                                  "devices": {"d_00000001": {"name": "Stova", "targets": list(targets), "rules": list(rules),
                                                             "designers": list(designer_list)}}})


def expand(cfg, index=None):
    device = cfg["devices"]["d_00000001"]
    return device, designers.expand(device, cfg.get("presence"), index=index)


def blocking(x):
    return [p["code"] for p in x.problems if p["level"] == "blocking"]


# ------------------------------------------------------------------ the document
def test_unset_is_absent_and_round_trips():
    cfg = config(lighting())
    d = cfg["devices"]["d_00000001"]["designers"][0]
    assert SPOTS not in d["mood"]["cells"]                       # nothing configured: nothing stored
    assert d["mood"]["default"] is None and d["mood"]["row_defaults"] == {}
    again = model.validate_config(json.loads(json.dumps(cfg)))
    assert again == cfg                                          # the canonical form is stable
    assert d["mood"]["cells"][LAMP]["Hygge"] == {"kind": "on", "brightness_pct": 40, "color_temp_kelvin": 2700,
                                                 "color": None, "effect": None, "transition": None}


def test_devices_without_designers_keep_their_document():
    raw = {"settings": {}, "devices": {"d_00000001": {"name": "Lamp", "targets": [LAMP], "rules": []}}}
    cfg = model.validate_config(raw)
    assert "presence" not in cfg and "designers" not in cfg["devices"]["d_00000001"]


def test_invalid_designers_are_refused_with_paths():
    bad = lighting(lights=[LAMP, "light.elsewhere"])
    bad["mood"]["cells"][LAMP]["Dagur"] = {"kind": "on", "brightness_pct": 140}
    bad["presence"]["cells"][LAMP][f"{SOFA}:occupied"] = {"kind": "sometimes"}
    with pytest.raises(ConfigError) as err:
        config(bad)
    paths = [p["path"] for p in err.value.problems]
    assert any("lights[1]" in p for p in paths) and any("brightness_pct" in p for p in paths)
    assert any("presence.cells" in p for p in paths)
    with pytest.raises(ConfigError):  # a default cannot be "use default"
        config(lighting(mood={**lighting()["mood"], "default": {"kind": "default"}}))
    with pytest.raises(ConfigError):  # the mood matrix cannot refer to itself
        config(lighting(mood={**lighting()["mood"], "cells": {LAMP: {"Dagur": {"kind": "mood"}}}}))


def test_unknown_and_newer_designers_are_kept_unchanged_and_block():
    future = {"id": "g_00000009", "kind": "climate", "version": 1, "anything": {"deep": [1, 2, {"x": None}]}}
    newer = {**lighting(), "id": "g_00000008", "version": 7, "new_field": True}
    cfg = config(future, newer)
    kept = cfg["devices"]["d_00000001"]["designers"]
    assert kept == [future, newer]
    _device, x = expand(cfg)
    assert blocking(x) == ["unsupported", "unsupported"] and not x.rules


def test_presence_sources_must_exist_and_cannot_be_deleted_in_use(core):
    with pytest.raises(ConfigError):
        config(lighting(), presence={"sources": {SOFA: sources()["sources"][SOFA]}})
    core.store.save(config(lighting()), expected_revision=core.store.revision, reason="t")
    with pytest.raises(ConfigError) as err:
        core.delete_source(SOFA, core.store.revision, ADMIN)
    assert err.value.code == "in_use"


# ------------------------------------------------------------------ defaults
@pytest.mark.parametrize(("cell", "row", "matrix", "state_", "source"), [
    (None, None, None, "unset", None),
    (None, {"kind": "on"}, {"kind": "off"}, "default", "row default"),
    (None, None, {"kind": "off"}, "default", "matrix default"),
    ({"kind": "default"}, None, None, "missing_default", None),
    ({"kind": "default"}, None, {"kind": "off"}, "default", "matrix default"),
    ({"kind": "none"}, {"kind": "on"}, None, "none", "cell"),
    ({"kind": "off"}, {"kind": "on"}, None, "explicit", "cell"),
    (None, {"kind": "none"}, {"kind": "off"}, "none", "row default"),
])
def test_default_precedence(cell, row, matrix, state_, source):
    res = C.resolve(cell, row, matrix)
    assert (res["state"], res["source"]) == (state_, source)
    assert (C.acting(res) is not None) == (state_ in ("explicit", "default"))


def test_labels_show_the_source_of_a_default():
    assert L.resolved_label(C.resolve(None, {"kind": "on", "brightness_pct": 30, "color_temp_kelvin": None, "color": None,
                                             "effect": None, "transition": None}, None)) == "DEFAULT: 30%"
    assert L.resolved_label(C.resolve(None, None, None)) == "UNSET"
    assert L.resolved_label(C.resolve({"kind": "none"}, None, None)) == "NO ACTION"
    assert L.label({"kind": "on", "brightness_pct": 40, "color_temp_kelvin": 2700, "color": None, "effect": None,
                    "transition": None}) == "40% · 2700K"
    assert L.label({"kind": "off", "transition": None}) == "OFF"
    assert CV.label({"kind": "position", "position": 20, "tilt": 40}) == "20% · tilt 40%"


def test_use_default_without_a_default_blocks():
    d = lighting(primary="mood", presence={"enabled": False},
                 mood={"enabled": True, "entity_id": MOOD, "values": ["Dagur"], "cells": {LAMP: {"Dagur": {"kind": "default"}}}})
    _device, x = expand(config(d))
    assert "missing_default" in blocking(x) and not x.rules


# ------------------------------------------------------------------ expansion
def test_combined_designer_generates_stable_ordinary_rules():
    cfg = config(lighting())
    device, x = expand(cfg)
    assert blocking(x) == []
    names = sorted(r["name"] for r in x.rules)
    assert names == ["Lighting · light.kitchen_spots · Kitchen occupied", "Lighting · light.kitchen_spots · Sofa occupied",
                     "Lighting · light.sofa_lamp · Sofa occupied · Hygge", "Lighting · light.sofa_lamp · Stova vacant"]
    again = designers.expand(copy.deepcopy(device), cfg["presence"])
    assert [r["id"] for r in again.rules] == [r["id"] for r in x.rules]          # stable ids
    first = compiler.compile_device(designers.effective(device, x), cfg["settings"])
    second = compiler.compile_device(designers.effective(device, again), cfg["settings"])
    assert [a["hash"] for a in first["automations"]] == [a["hash"] for a in second["automations"]]   # deterministic
    assert len(first["automations"]) == 1 and first["automations"][0]["mode"] == "queued"           # one automation
    for rule in x.rules:  # every generated rule is an ordinary rule of the model
        p = model.Problems()
        assert model.validate_rule({k: v for k, v in rule.items() if k != "generated"}, "r", p, device["targets"], generated=True) \
            == {k: v for k, v in rule.items() if k != "generated"} and not p.items


def test_priority_policy_makes_situations_exclusive_without_conflicts():
    cfg = config(lighting())
    device, x = expand(cfg)
    found = conflicts.analyse(designers.effective(device, x)) + x.conflicts
    assert [c for c in found if c["level"] == "blocking"] == []
    kitchen = next(r for r in x.rules if r["name"].endswith("light.kitchen_spots · Kitchen occupied"))
    negated = [c for c in kitchen["conditions"]["items"] if c.get("negate")]
    assert negated and negated[0]["entity_id"] == "binary_sensor.sofa_zone"     # the sofa (left) dominates
    assert {t["entity_id"] for t in kitchen["triggers"]} == {"binary_sensor.kitchen_zone", "binary_sensor.sofa_zone"}


def test_overlapping_situations_need_a_policy_and_compatible_reports_differences():
    d = lighting()
    d["presence"]["policy"] = None
    _device, x = expand(config(d))
    assert "no_policy" in blocking(x)
    d["presence"]["policy"] = "compatible"
    device, x = expand(config(d))
    # nothing assumes where the zones are: the sofa can be occupied while the room is vacant, too
    assert sorted(c["entity_id"] for c in x.conflicts) == sorted([LAMP, SPOTS]) and {c["code"] for c in x.conflicts} == {"zone_overlap"}
    d["presence"]["cells"][SPOTS][f"{KITCHEN}:occupied"] = {"kind": "on", "brightness_pct": 20}   # now the same command
    _device, x = expand(config(d))
    assert [c["entity_id"] for c in x.conflicts] == [LAMP]


def test_no_action_dominates_under_priority_but_unset_does_not():
    d = lighting()
    d["presence"]["cells"][SPOTS][f"{SOFA}:occupied"] = {"kind": "none"}
    _device, x = expand(config(d))
    kitchen = next(r for r in x.rules if r["name"].endswith("Kitchen occupied"))
    assert any(c.get("negate") and c["entity_id"] == "binary_sensor.sofa_zone" for c in kitchen["conditions"]["items"])
    del d["presence"]["cells"][SPOTS][f"{SOFA}:occupied"]
    _device, x = expand(config(d))
    kitchen = next(r for r in x.rules if r["name"].endswith("Kitchen occupied"))
    assert not any(c.get("negate") for c in kitchen["conditions"]["items"] if c["type"] == "state")


def test_missing_sources_block_and_are_never_guessed():
    d = lighting()
    d["mood"]["entity_id"] = None
    _device, x = expand(config(d))
    assert "missing_source" in blocking(x) and not x.rules
    unset = {"sources": {**sources()["sources"], SOFA: {"name": "Sofa", "kind": "zone", "entity_id": None}}}
    _device, x = expand(config(lighting(), presence=unset))
    assert any("Sofa" in p["message"] and p["code"] == "missing_source" for p in x.problems)


def test_several_matrices_need_an_explicit_decider():
    _device, x = expand(config(lighting(primary=None)))
    assert "no_primary" in blocking(x)


def test_mood_auto_mode_and_exclusive_mood_values():
    d = lighting(primary="mood", presence={"enabled": False}, mood={
        "enabled": True, "entity_id": "input_select.main_mood", "values": ["Dagur", "Hygge"],
        "auto": {"value": "Auto", "entity_id": "sensor.effective_mood"},
        "cells": {LAMP: {"Dagur": {"kind": "on", "brightness_pct": 80}, "Hygge": {"kind": "on", "brightness_pct": 30}}}})
    device, x = expand(config(d))
    assert blocking(x) == [] and len(x.rules) == 2
    dagur = next(r for r in x.rules if r["generated"]["mood"] == "Dagur")
    assert dagur["conditions"]["items"][0]["type"] == "or"
    assert {(t["entity_id"], t["to"]) for t in dagur["triggers"]} == {
        ("input_select.main_mood", "Dagur"), ("input_select.main_mood", "Auto"), ("sensor.effective_mood", "Dagur")}
    assert conflicts.analyse(designers.effective(device, x)) == []   # the two moods exclude each other by construction
    with pytest.raises(ConfigError):  # the automatic value cannot also be a column
        config(lighting(mood={**d["mood"], "values": ["Auto", "Dagur"]}))


def test_light_level_permission_never_acts_by_itself():
    d = lighting(lux={"enabled": True, "entity_id": LUX, "hysteresis": 20, "delay_seconds": 30,
                      "bands": [{"id": "b_0001", "name": "Dark", "below": 100}, {"id": "b_0002", "name": "Dim", "below": 300},
                                {"id": "b_0003", "name": "Bright"}],
                      "gate": {LAMP: {"on": ["b_0001", "b_0002"], "off": None}}})
    _device, x = expand(config(d))
    lamp = next(r for r in x.rules if r["generated"]["mood"] == "Hygge")
    assert not any(t.get("entity_id") == LUX for t in lamp["triggers"])                          # only permits
    assert {"type": "numeric", "entity_id": LUX, "above": None, "below": 300, "attribute": None} in lamp["conditions"]["items"]
    vacant = next(r for r in x.rules if r["name"].endswith("Stova vacant"))
    assert not any(c.get("entity_id") == LUX for c in vacant["conditions"]["items"])             # OFF is not restricted
    d["lux"]["apply_on_change"] = True
    _device, x = expand(config(d))
    lamp = next(r for r in x.rules if r["generated"]["mood"] == "Hygge")
    assert {"type": "numeric", "entity_id": LUX, "above": None, "below": 300, "for_seconds": 30, "attribute": None} in lamp["triggers"]


def test_light_level_bands_with_hysteresis_decide():
    d = lighting(primary="lux", presence={"enabled": False}, mood={"enabled": False}, lux={
        "enabled": True, "entity_id": LUX, "hysteresis": 20,
        "bands": [{"id": "b_0001", "name": "Dark", "below": 100}, {"id": "b_0002", "name": "Normal", "below": 1000},
                  {"id": "b_0003", "name": "Bright"}],
        "cells": {LAMP: {"b_0001": {"kind": "on", "brightness_pct": 80}, "b_0003": {"kind": "off"}}}})
    device, x = expand(config(d))
    dark = next(r for r in x.rules if r["generated"]["column"] == "b_0001")
    bright = next(r for r in x.rules if r["generated"]["column"] == "b_0003")
    assert [(t["above"], t["below"]) for t in dark["triggers"]] == [(None, 100)]
    assert [(t["above"], t["below"]) for t in bright["triggers"]] == [(1020, None)]            # 1000 + hysteresis 20
    assert [c["type"] for c in bright["conditions"]["items"]] == ["not", "state", "state"]       # not unavailable/unknown
    assert conflicts.analyse(designers.effective(device, x)) == []
    missing = copy.deepcopy(d)
    missing["lux"]["bands"][1]["below"] = None
    _device, x = expand(config(missing))
    assert "missing_threshold" in blocking(x)


def test_capabilities_are_checked_against_home_assistant(home, core):
    home.states[LAMP] = state(LAMP, "off", supported_color_modes=["onoff"])
    home.states[SPOTS] = state(SPOTS, "off", supported_color_modes=["color_temp"], min_color_temp_kelvin=2200, max_color_temp_kelvin=6500)
    index = core.index()
    _device, x = expand(config(lighting()), index)
    assert any(p["code"] == "capability" and LAMP in p["message"] and "dim" in p["message"] for p in x.problems)
    home.states[LAMP] = state(LAMP, "off", supported_color_modes=["color_temp", "hs"])
    _device, x = expand(config(lighting()), core.index())
    assert not any(p["code"] == "capability" for p in x.problems)


def test_rechecks_are_offered_only_for_binary_sensors_without_delays():
    srcs = {"sources": {**sources()["sources"], SOFA: {"name": "Sofa", "kind": "zone", "entity_id": "binary_sensor.sofa_zone"}}}
    d = lighting(rechecks=True)
    device, x = expand(config(d, presence=srcs))
    sofa = next(r for r in x.rules if r["name"].endswith("light.kitchen_spots · Sofa occupied"))
    assert sofa["triggers"][0]["type"] == "occupancy" and sofa["recheck"]["enabled"] is True
    compiled = compiler.compile_device(designers.effective(device, x), B.settings())
    blocks = compiled["automations"][0]["blocks"]
    assert any(b["kind"] == "recheck" and b["rule_id"] == sofa["id"] for b in blocks)
    vacant = next(r for r in x.rules if r["name"].endswith("Stova vacant"))   # 60 s exit delay: no occupancy trigger
    assert vacant["triggers"][0]["type"] == "state"


def test_designer_and_ordinary_rules_on_one_light_are_named():
    rule = B.rule(1, "Motion", [B.state("binary_sensor.motion", to="on")], [B.call("light.turn_on", target=[LAMP])])
    _device, x = expand(config(lighting(), rules=[rule]))
    assert any(p["code"] == "designer_overlap" for p in x.problems)


# ------------------------------------------------------------------ position zones and persons
def test_position_and_person_sources():
    p = model.Problems()
    zone = P.validate_source("z_00000004", {"name": "Desk", "kind": "position", "position": {
        "targets": [{"x": {"entity_id": "sensor.x"}, "y": {"entity_id": "sensor.y"}}], "units": "mm",
        "shape": {"type": "rect", "points": [[0, 0], [100, 100]]}}}, "s", p)
    assert not p.items and P.missing(zone, "occupied") is None
    states = {"sensor.x": {"state": "50"}, "sensor.y": {"state": "50"}}
    assert P.evaluate_position(zone, states, "inside") and not P.evaluate_position(zone, states, "outside")
    assert not P.evaluate_position(zone, {"sensor.x": {"state": "unavailable"}}, "inside")
    assert not P.evaluate_position(zone, {"sensor.x": {"state": "unavailable"}}, "outside")      # no readings: neither
    half = P.validate_source("z_00000007", {"name": "Half", "kind": "position", "position": {
        "targets": [{"x": {"entity_id": "sensor.x"}, "y": {"entity_id": "sensor.y"}}], "shape": {"type": "polygon", "points": [[0, 0], [1, 1]]}}}, "s", p)
    assert not p.items and "not complete" in P.missing(half, "occupied")   # a shape being drawn is stored, and blocks
    person = P.validate_source("z_00000005", {"name": "Anna", "kind": "person", "entity_id": "person.anna"}, "s", p)
    assert "no zone chosen" in P.missing(person, "occupied")                                     # "home" is not assumed
    with pytest.raises(AssertionError):
        assert not P.validate_source("z_00000006", {"name": "PIR", "kind": "zone", "entity_id": "sensor.pir"}, "s", model.Problems())
    bad = model.Problems()
    P.validate_source("z_00000006", {"name": "PIR", "kind": "person", "entity_id": "binary_sensor.pir"}, "s", bad)
    assert bad.items  # a person source needs a person or tracker, never a motion sensor


# ------------------------------------------------------------------ covers
def blinds(**sun):
    base = {"enabled": True, "hysteresis": 3, "min_change": 5,
            "windows": {"cover.living": {"orientation": 0, "left": 60, "right": 60, "elevation_min": 5, "elevation_max": 60,
                                         "position": {"kind": "curve", "basis": "profile", "points": [[10, 10], [50, 80]]},
                                         "leave": {"kind": "open"}}}}
    base.update(sun)
    return {"id": "g_00000002", "kind": "cover", "version": 1, "covers": ["cover.living"], "sun": base,
            "modes": {"enabled": True, "names": ["Daylight", "Privacy"], "driver": "selector", "entity_id": "input_select.cover_mode",
                      "cells": {"cover.living": {"Daylight": {"kind": "position", "position": 100}, "Privacy": {"kind": "position", "position": 20}}},
                      "sun_policy": {"Daylight": "sun_wins", "Privacy": "mode_wins"}}}


def test_cover_rules_and_policies_without_conflicts():
    cfg = config(blinds(), targets=["cover.living"])
    device, x = expand(cfg)
    assert blocking(x) == []
    layers = sorted((r["generated"]["layer"], r["generated"]["column"]) for r in x.rules)
    assert layers == [("mode", "Daylight"), ("mode", "Privacy"), ("sun", "leave"), ("sun", "shade")]
    assert conflicts.analyse(designers.effective(device, x)) == []
    shade = next(r for r in x.rules if r["generated"]["column"] == "shade")
    az = next(c for c in shade["conditions"]["items"] if c.get("attribute") == "azimuth")
    assert (az["above"], az["below"]) == (120, 240)
    assert {"type": "state", "entity_id": "input_select.cover_mode", "state": "Privacy", "negate": True, "for_seconds": 0,
            "attribute": None} in shade["conditions"]["items"]                                    # Privacy wins
    daylight = next(r for r in x.rules if r["generated"]["column"] == "Daylight")
    assert daylight["conditions"]["items"][1]["type"] == "not"                                    # not while shading
    leave = next(r for r in x.rules if r["generated"]["column"] == "leave")
    assert any(c["type"] == "state" and c.get("negate") and c["state"] == "Daylight" for c in leave["conditions"]["items"])


def test_cover_requires_explicit_policy_inputs_and_safety():
    d = blinds()
    d["modes"]["sun_policy"] = {}
    _device, x = expand(config(d, targets=["cover.living"]))
    assert "no_sun_policy" in blocking(x)
    d = blinds()
    d["sun"]["windows"]["cover.living"]["elevation_min"] = None
    _device, x = expand(config(d, targets=["cover.living"]))
    assert "missing_input" in blocking(x) and not [r for r in x.rules if r["generated"]["layer"] == "sun"]
    d = blinds(min_change=None)
    _device, x = expand(config(d, targets=["cover.living"]))
    assert any("minimum change" in p["message"] for p in x.problems)
    d = blinds()
    d["sun"]["windows"]["cover.living"]["weather_sensitive"] = True
    _device, x = expand(config(d, targets=["cover.living"]))
    assert "weather_safety" in blocking(x)


def test_sector_crossing_north_and_cover_capabilities(home, core):
    d = blinds()
    d["sun"]["windows"]["cover.living"].update(orientation=180, left=30, right=30)     # a north window (southern hemisphere)
    home.states["cover.living"] = state("cover.living", "open", supported_features=3)  # open/close only
    _device, x = expand(config(d, targets=["cover.living"]), core.index())
    shade = next(r for r in x.rules if r["generated"]["column"] == "shade")
    az = shade["conditions"]["items"][0]
    assert az["type"] == "or" and [(i["above"], i["below"]) for i in az["items"]] == [(330, None), (None, 30)]
    assert any(p["code"] == "capability" and "move to a position" in p["message"] for p in x.problems)
    home.states["cover.living"] = state("cover.living", "open", supported_features=15, device_class="awning")
    _device, x = expand(config(blinds(), targets=["cover.living"]), core.index())
    assert any(p["code"] == "weather_safety" for p in x.problems)


def test_normal_mood_mapping_is_optional():
    d = blinds()
    d["modes"].update(driver="mood", mood_entity=MOOD, mood_map={"Dagur": "Daylight"})   # Hygge stays unmapped
    device, x = expand(config(d, targets=["cover.living"]))
    assert "mode_unmapped" in [p["code"] for p in x.problems]
    daylight = next(r for r in x.rules if r["generated"]["column"] == "Daylight")
    assert any(t.get("to") == "Dagur" for t in daylight["triggers"])
    assert not any(r["generated"]["column"] == "Privacy" for r in x.rules)
    assert conflicts.analyse(designers.effective(device, x)) == []


def test_orientation_conversion():
    assert [solar.to_ha(u) for u in (0, 90, -90, 180, 45)] == [180, 270, 90, 0, 225]
    assert [solar.from_ha(a) for a in (180, 270, 90, 0, 359)] == [0, 90, -90, 180, 179]
    assert solar.relative(200, 0) == 20 and solar.relative(160, 0) == -20
    assert solar.profile_angle(30, 180, 0) == pytest.approx(30)
    assert solar.profile_angle(30, 240, 0) == pytest.approx(49.1066, abs=1e-3)   # oblique sun: deeper profile
    assert solar.profile_angle(30, 280, 0) is None                               # behind the window plane


# ------------------------------------------------------------------ simulation
def test_simulation_shows_matching_and_failing_rules_and_missing_sources():
    cfg = config(lighting())
    device, x = expand(cfg)
    effective = designers.effective(device, x)
    key = next(t["key"] for t in simulate.triggers_of(effective, str) if t["text"].startswith("binary_sensor.sofa_zone changes to"))
    states = {"binary_sensor.sofa_zone": {"state": "on", "held": 10}, MOOD: {"state": "Hygge", "origin": "test"},
              "binary_sensor.kitchen_zone": {"state": "off"}}
    result = simulate.run(effective, {"states": states, "trigger": key}, x.refs.evaluators, str)
    final = {o["entity_id"]: o["final"] for o in result["outcome"]}
    assert final[LAMP]["data"] == {"brightness_pct": 40, "color_temp_kelvin": 2700}
    assert final[SPOTS]["data"] == {"brightness_pct": 20}
    assert result["origins"][MOOD] == "test"
    states["binary_sensor.sofa_zone"]["held"] = 2                                 # not confirmed yet (5 s)
    result = simulate.run(effective, {"states": states, "trigger": key}, x.refs.evaluators, str)
    assert all(o["final"] is None for o in result["outcome"])
    assert any(r["result"] == "blocked" for r in result["rules"])
    result = simulate.run(effective, {"states": {}, "trigger": key}, x.refs.evaluators, str)
    assert "binary_sensor.sofa_zone" in result["missing"]


# ------------------------------------------------------------------ blueprints
def test_designer_blueprint_inputs_are_shared_and_restore_exactly():
    d = lighting(lux={"enabled": True, "entity_id": LUX, "bands": [{"id": "b_0001", "name": "Dark", "below": 100},
                                                                   {"id": "b_0002", "name": "Bright"}], "gate": {LAMP: {"on": ["b_0001"]}}})
    cfg = config(d)
    device = cfg["devices"]["d_00000001"]
    found = blueprint.candidates(device, cfg["settings"], presence=cfg["presence"])
    keys = [i["key"] for i in found["inputs"]]
    assert keys.count("g_00000001_b_0001_below") == 1 and "sensor_lux" in keys and "binary_sensor_sofa_zone" in keys
    chosen = {k: {"name": "", "description": ""} for k in keys}
    built = blueprint.build({**device, "blueprint": {"name": "B", "description": "", "automation": None, "inputs": chosen}},
                            cfg["settings"], presence=cfg["presence"])
    assert built["problems"] == [], built["problems"]
    assert "!input g_00000001_b_0001_below" in built["yaml"] or "!input 'g_00000001_b_0001_below'" in built["yaml"], built["yaml"]
    assert all(len(k) <= 64 for k in keys) and len(set(keys)) == len(keys)


def test_gallery_templates_bind_nothing():
    for template in gallery.TEMPLATES:
        d = designers.blank(template["designer"], "g_00000007", ["light.a", "cover.b"])
        applied = gallery.apply(template["id"], d)
        p = model.Problems()
        module = designers.REGISTRY[template["designer"]]
        clean = module.validate(applied, "g_00000007", ["light.a", "cover.b"], "d", p)
        assert not p.items
        assert clean[module.ROWS] == []                                          # rows are chosen by the user
        text = json.dumps(clean)
        assert "entity_id\": \"" not in text.replace("entity_id\": null", "")    # no entity is bound


# ------------------------------------------------------------------ the App
async def test_designer_workflow_through_the_core(core, home):
    core.store.save(model.validate_config({"settings": {}, "presence": sources(),
                                           "devices": {"d_00000001": {"name": "Stova", "targets": [LAMP, SPOTS], "rules": []}}}),
                    expected_revision=core.store.revision, reason="t")
    for entity in (LAMP, SPOTS):
        home.states[entity] = state(entity, "off", supported_color_modes=["color_temp"], min_color_temp_kelvin=2000, max_color_temp_kelvin=6500)
    # A version 1 designer (Phase 0.3) as an earlier App saved it: kept, compiled and deployed exactly as before.
    v1 = {"id": "g_0000000a", "kind": "lighting", "version": 1, "name": "Lighting", "lights": [], "primary": "presence",
          "presence": {"enabled": True}, "mood": {"enabled": True}, "lux": {"enabled": True}}
    cfg = copy.deepcopy(core.store.config)
    cfg["devices"]["d_00000001"]["designers"] = [v1]
    core.store.save(model.validate_config(cfg), expected_revision=core.store.revision, reason="t")
    created = {"designer_id": v1["id"]}
    page = core.designer_page("d_00000001", created["designer_id"])
    assert page["designer"]["config"]["primary"] == "presence" and page["designer"]["config"]["lights"] == []
    assert any(p["code"] == "no_lights" for p in page["designer"]["problems"])
    plan = await core.plan("d_00000001")
    assert not plan["can_deploy"] and any("lights" in r for r in plan["reasons"])
    draft = {**lighting(), "id": created["designer_id"], "lux": {"enabled": False}}
    saved = core.save_designer("d_00000001", draft, core.store.revision, ADMIN)
    assert saved["blocking"] == 0 and len(saved["designer"]["rules"]) == 4
    plan = await core.plan("d_00000001")
    assert plan["can_deploy"], plan["reasons"]
    await core.deploy("d_00000001", core.store.revision, plan["plan_hash"], False, ADMIN)
    record = core.deploys.records["automation_builder_d_00000001"]
    assert len(record["generated"]) == 4 and "designers" in record["device"]
    view = await core.device_view("d_00000001")
    assert view["live"]["status"] == "in_sync" and view["designers"]["items"][0]["rules"]
    sim = core.simulation("d_00000001", {"trigger": None})
    assert sim["triggers"] and sim["note"].startswith("A prediction")
    core.delete_designer("d_00000001", created["designer_id"], core.store.revision, ADMIN)
    plan = await core.plan("d_00000001")
    assert [i["operation"] for i in plan["items"]] == ["delete"]


async def test_acknowledged_designer_conflicts_survive_saves(core):
    d = lighting()
    d["presence"]["policy"] = "compatible"
    core.store.save(config(d), expected_revision=core.store.revision, reason="t")
    analysis = core.analysis(core.store.config["devices"]["d_00000001"])
    keys = [c["key"] for c in analysis["conflicts"] if c["code"] == "zone_overlap"]
    assert len(keys) == 2
    for key in keys:
        core.update_device("d_00000001", {"acknowledge": key}, core.store.revision, ADMIN)
    core.update_device("d_00000001", {"name": "Renamed"}, core.store.revision, ADMIN)
    assert set(keys) <= set(core.store.config["devices"]["d_00000001"]["acknowledged"])
    assert core.analysis(core.store.config["devices"]["d_00000001"])["blocking"] == 0


def test_presence_sources_through_the_core(core):
    saved = core.save_source({"name": "Desk", "kind": "zone", "entity_id": None}, core.store.revision, ADMIN)
    source_id = saved["source_id"]
    view = core.presence_view()
    assert view["sources"][0]["entity_id"] is None and view["sources"][0]["live"]["state"] is None   # unset, not "off"
    core.delete_source(source_id, core.store.revision, ADMIN)
    assert "presence" not in core.store.config


def test_export_and_import_keep_designers_and_presence(core):
    core.store.save(config(lighting()), expected_revision=core.store.revision, reason="t")
    exported = core.store.export()
    review = core.review_import(exported)
    assert review["changes"] == []
    other = copy.deepcopy(exported)
    other["config"]["presence"]["sources"][SOFA]["entry_seconds"] = 9
    assert any(c["name"] == "Presence sources" for c in core.review_import(other)["changes"])
    core.apply_import(other, core.store.revision, ADMIN)
    assert core.store.config["presence"]["sources"][SOFA]["entry_seconds"] == 9
    assert core.store.config["devices"]["d_00000001"]["designers"][0]["presence"]["policy"] == "priority"
