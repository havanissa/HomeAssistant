"""Automation Builder 0.5: Switches & Remotes as an interaction list, the Light Level Manager and the Heating Designer.

The runtime behaviour (Home Assistant's own engine) is tested in ``tests/automation_builder/test_switches_runtime.py``,
``test_levels_runtime.py`` and ``test_heating_runtime.py`` of the repository; these are the model, compiler, API and
migration checks.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from ab import compiler, conflicts, designers, model
from ab.designers import heating as H, levels as LV, switches as SW
from ab.ha import NotAllowed, check_params
from ab.model import ConfigError
from tests.conftest import ADMIN, state

L1, L2_ = "light.l1", "light.l2"
DEV = "d_0000abcd"
WALL = "event.wall"


def cfg(devices: dict[str, Any], **shared: Any) -> dict[str, Any]:
    return model.validate_config({"settings": {}, "devices": devices, **shared})


def expand(config: dict[str, Any], device_id: str) -> tuple[dict[str, Any], Any]:
    device = config["devices"][device_id]
    x = designers.expand(device, config.get("presence"), shared=config)
    return designers.effective(device, x), x


def blocking(x: Any) -> list[str]:
    return [p["code"] for p in x.problems if p["level"] == "blocking"]


# ================================================================== Part 2: switches and remotes
def remote(*mappings: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"id": "w_0001", "name": "Wall", "kind": "momentary", "entity_id": WALL, "interactions": [
        {"id": f"i_000{i}", "kind": k, "trigger": {"type": "button", "entity_id": WALL, "event_types": [k]}}
        for i, k in enumerate(("single", "double", "triple", "long", "release"), start=1)], "mappings": list(mappings), **extra}


def mp(mid: str, interaction: str, kind: str, *lights: str, **extra: Any) -> dict[str, Any]:
    return {"id": mid, "interaction": interaction, "lights": list(lights or (L1,)), "action": {"kind": kind}, "override": False, **extra}


def lighting(**parts: Any) -> dict[str, Any]:
    return {"id": "g_00000001", "kind": "lighting", "version": 2, "name": "Lighting", "lights": [L1, L2_], **parts}


def light_device(d: dict[str, Any]) -> dict[str, Any]:
    return {DEV: {"name": "Stova", "targets": [L1, L2_], "rules": [], "designers": [d]}}


def test_one_interaction_can_hold_many_mappings_and_every_one_is_kept():
    d = lighting(switches=[remote(mp("p_0001", "i_0001", "on"), mp("p_0002", "i_0001", "brightness", L2_) | {"action": {"kind": "brightness", "pct": 30}},
                                  mp("p_0003", "i_0002", "off"), mp("p_0004", "i_0003", "toggle"), mp("p_0005", "i_0003", "resume", L2_))])
    config = cfg(light_device(d))
    kept = config["devices"][DEV]["designers"][0]["switches"][0]["mappings"]
    assert [m["id"] for m in kept] == ["p_0001", "p_0002", "p_0003", "p_0004", "p_0005"]
    effective, x = expand(config, DEV)
    assert not blocking(x)
    switch_rules = [r for r in effective["rules"] if r["generated"]["layer"] == "switch"]
    assert len(switch_rules) == 5 and len({r["id"] for r in switch_rules}) == 5        # an id per mapping


def test_old_documents_are_unchanged_and_compile_byte_identically_to_0_4():
    """Golden hashes computed with the 0.4.0 code (git 1be713c) for the same document."""
    sw = {"id": "w_0001", "name": "Wall", "kind": "momentary", "entity_id": WALL, "interactions": [
        {"id": "i_0001", "kind": "single", "label": "", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["single"]}},
        {"id": "i_0002", "kind": "double", "label": "", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["double"]}},
        {"id": "i_0003", "kind": "long", "label": "", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["long"]}},
        {"id": "i_0004", "kind": "release", "label": "", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["release"]}},
        {"id": "i_0005", "kind": "single", "label": "zha", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:11", "command": "on"}}}],
        "mappings": [{"id": "p_0001", "interaction": "i_0001", "lights": [L1], "action": {"kind": "toggle"}, "override": True},
                     {"id": "p_0002", "interaction": "i_0002", "lights": [L1, L2_], "action": {"kind": "resume"}, "override": False},
                     {"id": "p_0003", "interaction": "i_0003", "lights": [L2_], "action": {"kind": "dim", "direction": "down", "step": 10, "interval": 1, "release": "i_0004"}, "override": False},
                     {"id": "p_0004", "interaction": "i_0005", "lights": [L2_], "action": {"kind": "brightness", "pct": 40}, "override": False}]}
    lat = {"id": "w_0002", "name": "Rocker", "kind": "latched", "entity_id": "switch.wall", "interactions": [
        {"id": "i_0011", "kind": "on", "label": "", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "on", "from": "off", "for_seconds": 0}},
        {"id": "i_0012", "kind": "off", "label": "", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "off", "from": "on", "for_seconds": 0}}],
        "mappings": [{"id": "p_0011", "interaction": "i_0011", "lights": [L1], "action": {"kind": "on"}, "override": False},
                     {"id": "p_0012", "interaction": "i_0012", "lights": [L1], "action": {"kind": "off"}, "override": False}]}
    d = lighting(switches=[sw, lat], override={"policy": "duration", "minutes": 5},
                 truth={"matrices": [{"id": "m_0001", "name": "T", "columns": [{"id": "c_0001", "kind": "numeric", "entity_id": "sensor.lux", "below": 50, "from": None, "for_seconds": 60}],
                                      "cells": {L1: {"c_0001": {"kind": "on"}}, L2_: {"c_0001": {"kind": "on"}}}}]})
    config = model.validate_config({"settings": {}, "devices": {DEV: {"name": "S", "targets": [L1, L2_], "rules": [], "designers": [d]}}})
    device = config["devices"][DEV]
    assert compiler.config_hash(device["designers"][0]) == "e021f73c1d44257e3bda"   # the saved document: no new keys
    effective, x = expand(config, DEV)
    out = compiler.compile_device(effective, config["settings"])
    assert {a["id"]: a["hash"] for a in out["automations"]} == {
        "automation_builder_d_0000abcd": "a8d1c395e67f98abe717",
        "automation_builder_d_0000abcd_l_9cc24734": "fa2ee0cdf6155a729317",
        "automation_builder_d_0000abcd_l_3126467f": "e12fd1139e55df1df3ce",
        "automation_builder_d_0000abcd_r_e844c941": "19015208fdc9177c9c5f"}


def test_presses_of_one_event_entity_never_happen_together():
    """The 0.4 defect behind "choose ON or OFF": single → ON and double → OFF of one event entity were one native
    trigger for the conflict check, so it reported a blocking contradiction and the device could not be deployed."""
    single = {"type": "button", "entity_id": WALL, "event_types": ["single"]}
    double = {"type": "button", "entity_id": WALL, "event_types": ["double"]}
    assert not conflicts._together(single, double)                                            # noqa: SLF001
    assert conflicts._together(single, dict(single))                                          # noqa: SLF001
    assert conflicts._together(single, {"type": "button", "entity_id": WALL, "event_types": []})  # noqa: SLF001
    for second in ("off", "toggle"):
        effective, _x = expand(cfg(light_device(lighting(switches=[remote(mp("p_0001", "i_0001", "on" if second == "off" else "toggle"),
                                                                           mp("p_0002", "i_0002", second))]))), DEV)
        assert not [c for c in conflicts.analyse(effective) if c["level"] == "blocking"]


def test_editing_deleting_and_reordering_one_mapping_leaves_the_others_alone():
    base = lighting(switches=[remote(mp("p_0001", "i_0001", "on"), mp("p_0002", "i_0002", "off"), mp("p_0003", "i_0003", "toggle"))])

    def rules(d: dict[str, Any]) -> dict[str, Any]:
        effective, _x = expand(cfg(light_device(d)), DEV)
        return {r["generated"]["column"]: r for r in effective["rules"] if r["generated"]["layer"] == "switch"}

    before = rules(base)
    edited = copy.deepcopy(base)
    edited["switches"][0]["mappings"][1]["action"] = {"kind": "brightness", "pct": 10}
    after = rules(edited)
    assert after["w_0001:p_0001"] == before["w_0001:p_0001"] and after["w_0001:p_0003"] == before["w_0001:p_0003"]
    assert after["w_0001:p_0002"]["actions"] != before["w_0001:p_0002"]["actions"]
    gone = copy.deepcopy(base)
    del gone["switches"][0]["mappings"][1]
    gone["switches"][0]["mappings"].append(mp("p_0009", "i_0002", "off"))      # re-added: a new id, the same behaviour
    again = rules(gone)
    assert set(again) == {"w_0001:p_0001", "w_0001:p_0003", "w_0001:p_0009"}
    assert again["w_0001:p_0009"]["actions"] == before["w_0001:p_0002"]["actions"]
    moved = copy.deepcopy(base)
    moved["switches"][0]["mappings"].reverse()
    effective, _x = expand(cfg(light_device(moved)), DEV)
    shared = next(a for a in compiler.compile_device(effective, cfg({})["settings"])["automations"] if a["role"] == "shared")
    order = [effective["rules"][[r["id"] for r in effective["rules"]].index(rid)]["generated"]["column"] for rid in shared["rule_ids"]]
    assert order == ["w_0001:p_0003", "w_0001:p_0002", "w_0001:p_0001"]        # the list order is the run order


def test_conditions_buttons_and_the_automation_guard_are_kept_only_when_set():
    cond = {"type": "and", "items": [{"type": "state", "entity_id": "input_boolean.night", "state": "on"}]}
    latched = {"id": "w_0002", "name": "Rocker", "kind": "latched", "entity_id": "switch.wall", "ignore_automations": True,
               "interactions": [{"id": "i_0011", "kind": "on", "button": "left", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "on", "from": "off"}}],
               "mappings": [mp("p_0011", "i_0011", "on", conditions=cond), mp("p_0012", "i_0011", "on", L2_)]}
    config = cfg(light_device(lighting(switches=[latched])))
    sw = config["devices"][DEV]["designers"][0]["switches"][0]
    assert sw["ignore_automations"] is True and sw["interactions"][0]["button"] == "left"
    assert sw["mappings"][0]["conditions"]["items"][0]["entity_id"] == "input_boolean.night" and "conditions" not in sw["mappings"][1]
    effective, _x = expand(config, DEV)
    by = {r["generated"]["column"]: r for r in effective["rules"]}
    guard = "{{ trigger.to_state is none or trigger.to_state.context.parent_id is none }}"
    assert [c.get("value_template") or c["entity_id"] for c in by["w_0002:p_0011"]["conditions"]["items"]] == ["input_boolean.night", guard]
    assert [c.get("value_template") for c in by["w_0002:p_0012"]["conditions"]["items"]] == [guard]


def test_discovery_names_the_button_of_device_triggers():
    found = SW.from_device_triggers([
        {"label": "\"Turn on\" pressed", "config": {"domain": "zha", "type": "remote_button_short_press", "subtype": "turn_on", "device_id": "x"}},
        {"label": "Button 2 released", "config": {"domain": "hue", "type": "short_release", "subtype": 2, "device_id": "x"}},
        {"label": "on_press", "config": {"domain": "mqtt", "type": "action", "subtype": "on_press", "device_id": "x"}}])
    assert [f.get("button") for f in found] == ["turn_on", "button 2", None]
    assert [f["kind"] for f in found] == ["single", "release", "single"]


async def test_the_saved_interactions_survive_a_restart_of_the_app(core, home, tmp_path):
    from tests.conftest import make_core
    d = lighting(switches=[remote(mp("p_0001", "i_0001", "on"), mp("p_0002", "i_0002", "off"), mp("p_0003", "i_0001", "on", L2_))])
    for light in (L1, L2_):
        home.states[light] = state(light, "off", supported_color_modes=["brightness"])
    core.create_device({"name": "Stova", "targets": [L1, L2_]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    new = copy.deepcopy(core.store.config)
    new["devices"][device_id]["designers"] = [d]
    core._save(new, core.store.revision, "t", ADMIN)  # noqa: SLF001
    again = make_core(tmp_path, home)
    kept = again.store.config["devices"][device_id]["designers"][0]["switches"][0]["mappings"]
    assert [(m["id"], m["interaction"], m["action"]["kind"], m["lights"]) for m in kept] == [
        ("p_0001", "i_0001", "on", [L1]), ("p_0002", "i_0002", "off", [L1]), ("p_0003", "i_0001", "on", [L2_])]


# ================================================================== Part 1: the Light Level Manager
LL = "d_00000011"


def level_config(room: dict[str, Any] | None = None, template: dict[str, Any] | None = None, *, lights: dict[str, Any] | None = None,
                 **levels: Any) -> dict[str, Any]:
    r = LV.blank_room("kamar", LL, None)
    r.update(template="t_00000001", source={"entity_id": "sensor.lux", "attribute": None})
    r.update(room or {})
    raw_levels = {"templates": {"t_00000001": template or LV.preset_template("t_00000001")}, "rooms": {"kamar": r}, **levels}
    targets = LV.device_targets(LV.validate_room("kamar", r, "r", model.Problems()))
    devices = {LL: {"name": "Light Level · Kamar", "targets": targets, "rules": [],
                    "designers": [{"id": "g_00000011", "kind": "light_level", "version": 1, "room": "kamar"}]}}
    if lights is not None:
        devices[DEV] = {"name": "Stova", "targets": [L1, L2_], "rules": [], "designers": [lights]}
    return model.validate_config({"settings": {}, "devices": devices, "light_levels": raw_levels})


def test_bands_are_ordered_without_gaps_and_ids_are_stable():
    p = model.Problems()
    bad = LV.validate_template("t_00000001", {"name": "X", "bands": [
        {"id": "dark", "name": "Dark", "below": 80}, {"id": "dim", "name": "Dim", "below": 50}, {"id": "no_reading", "name": "R", "below": 9},
        {"id": "bright", "name": "Bright", "below": 300}]}, "t", p)
    messages = " ".join(i["message"] for i in p.items)
    assert "must increase" in messages and "reserved" in messages and "no upper limit" in messages
    assert [b["id"] for b in bad["bands"]][:2] == ["dark", "dim"]
    p = model.Problems()
    LV.validate_template("t_00000001", {"name": "X", "bands": [{"id": "dark", "name": "Dark"}]}, "t", p)
    assert p.items                                                                 # at least two levels


def test_room_inherits_overrides_and_keeps_its_overrides_when_the_template_changes():
    room = LV.validate_room("kamar", {"device_id": LL, "template": "t_00000001", "overrides": {"bands": {"dark": {"below": 95}}, "hysteresis": 25}}, "r", model.Problems())
    template = LV.preset_template("t_00000001")
    eff = LV.effective(room, template)
    assert [(b["id"], b["below"], b["source"]) for b in eff["bands"]] == [
        ("very_dark", 20, "global"), ("dark", 95, "room"), ("dim", 200, "global"), ("bright", None, "none")]
    assert eff["settings"]["hysteresis"] == {"value": 25, "global": 10, "source": "room"}
    assert eff["settings"]["darker_seconds"]["source"] == "global"
    changed = {**template, "version": 2, "bands": [{**b, "below": b["below"] + 5 if b["below"] else None} for b in template["bands"]]}
    eff = LV.effective(room, changed)
    assert [b["below"] for b in eff["bands"]] == [25, 95, 205, None]               # global follows, the room keeps 95
    renamed = {**template, "bands": [{**b, "name": b["name"].upper()} for b in template["bands"]]}
    assert [b["id"] for b in LV.effective(room, renamed)["bands"]] == ["very_dark", "dark", "dim", "bright"]
    broken = LV.validate_room("kamar", {"device_id": LL, "overrides": {"bands": {"dark": {"below": 250}}}}, "r", model.Problems())
    assert LV.effective(broken, template)["problems"]                              # 250 > the next limit: refused
    assert LV.effective({**room, "overrides": {"bands": {"gone": {"below": 5}}}}, template)["orphans"] == ["gone"]


@pytest.mark.parametrize("value,current,expected", [
    (30, -1, 1), (5, -1, 0), (250, -1, 3), (None, 2, -1), ("x", 1, -1),
    (85, 1, 1), (100, 1, 1), (105, 1, 2), (204, 2, 2), (210, 2, 3), (195, 3, 3), (189, 3, 2), (11, 1, 1), (9, 1, 0),
    (500, 0, 3), (0, 3, 0)])
def test_band_index_with_hysteresis(value, current, expected):
    assert LV.band_index(value, [20, 95, 200], 10, current) == expected


def test_readings_measured_estimated_and_never_invented():
    room = LV.validate_room("kamar", {"device_id": LL, "source": {"entity_id": "sensor.lux"}, "ambient": {
        "method": "calibrated", "lights": [L1, L2_], "contributions": {L1: 120}}}, "r", model.Problems())
    states = {"sensor.lux": {"state": "300"}, L1: {"state": "on", "attributes": {"brightness": 128}}, L2_: {"state": "on"}}
    read = LV.reading(room, states)
    assert read["estimated"] and abs(read["value"] - (300 - 120 * 128 / 255)) < 1e-9 and "uncalibrated" in read["confidence"]
    sample = LV.validate_room("kamar", {"device_id": LL, "source": {"entity_id": "sensor.lux"}, "ambient": {
        "method": "off_sample", "lights": [L1], "max_age_minutes": 10}}, "r", model.Problems())
    helper = LV.helper_ids(LL, "kamar")["ambient"]
    from datetime import datetime, timedelta, UTC
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    fresh = {helper: {"state": "140.0", "last_reported": (now - timedelta(minutes=2)).isoformat()}, "sensor.lux": {"state": "400"}}
    assert LV.reading(sample, fresh, now)["value"] == 140 and LV.reading(sample, fresh, now)["artificial"] == 260
    stale = {helper: {"state": "140.0", "last_reported": (now - timedelta(minutes=12)).isoformat()}}
    assert LV.reading(sample, stale, now)["value"] is None
    never = {helper: {"state": "-1.0", "last_reported": now.isoformat()}}
    assert LV.reading(sample, never, now)["value"] is None                         # a new helper is not a 0 lx reading
    plain_room = LV.validate_room("kamar", {"device_id": LL, "source": {"entity_id": "sensor.lux"}}, "r", model.Problems())
    assert LV.reading(plain_room, {"sensor.lux": {"state": "unavailable"}})["value"] is None
    assert LV.reading(plain_room, {"sensor.lux": {"state": "42"}}) | {} == {**LV.reading(plain_room, {"sensor.lux": {"state": "42"}}), "estimated": False}


def test_room_light_level_compiles_to_one_automation_with_its_helper():
    config = level_config()
    effective, x = expand(config, LL)
    assert not blocking(x)
    helpers = x.views["g_00000011"]["helpers"]
    assert [(h["entity_id"], h["domain"]) for h in helpers] == [("input_text.ab_00000011_kamar_light_level", "input_text")]
    out = compiler.compile_device(effective, config["settings"])["automations"]
    assert [a["role"] for a in out] == ["shared"] and len(out[0]["rule_ids"]) == 3     # follows, regular check, no reading
    no_template = level_config({"template": None})
    assert blocking(expand(no_template, LL)[1]) == ["no_template"]
    no_source = level_config({"source": None})
    assert blocking(expand(no_source, LL)[1]) == ["missing_source"]
    sampled = level_config({"ambient": {"method": "off_sample", "lights": [L1], "max_age_minutes": 20}})
    effective, x = expand(sampled, LL)
    assert not blocking(x) and [h["domain"] for h in x.views["g_00000011"]["helpers"]] == ["input_text", "input_number"]
    assert x.views["g_00000011"]["helpers"][1]["params"]["initial"] == -1
    assert blocking(expand(level_config({"ambient": {"method": "calibrated", "lights": [L1]}}), LL)[1]) == ["uncalibrated"]


def test_light_level_matrix_inherits_global_room_and_light_values():
    matrices = {"x_00000001": {"name": "T", "layer": "truth", "template": "t_00000001",
                               "cells": {"very_dark": {"kind": "on"}, "dark": {"kind": "on"}, "dim": {"kind": "on"}, "bright": {"kind": "off"}}}}
    d = lighting(truth={"matrices": [{"id": "m_0001", "name": "Light level", "level": {"room": "kamar"},
                                      "cells": {L1: {"bright": {"kind": "on"}}}}]})
    config = level_config({"matrices": {"truth": "x_00000001", "brightness": None}, "matrix_overrides": {"truth": {"dim": {"kind": "off"}}, "brightness": {}}},
                          lights=d, matrices=matrices)
    effective, x = expand(config, DEV)
    assert not blocking(x)
    view = x.views["g_00000001"]["resolved"]["m_0001"]
    assert [c["id"] for c in view["__columns"]] == ["very_dark", "dark", "dim", "bright"]
    assert (view[L1]["dark"]["label"], view[L1]["dark"]["source"]) == ("ON", "global default matrix")
    assert (view[L2_]["dim"]["label"], view[L2_]["dim"]["source"]) == ("OFF", "room light level")
    assert view[L1]["bright"]["state"] == "explicit" and view[L2_]["bright"]["label"] == "OFF"     # the light's own ON wins for L1
    helper = LV.helper_ids(LL, "kamar")["band"]
    plan = x.generators["g_00000001"].plans[L1]
    assert {c["entity_id"] for b in plan["truth"][0]["branches"] for c in b["conditions"]["items"]} == {helper}


def test_a_matrix_of_an_unknown_room_is_refused_and_a_room_without_template_blocks():
    d = lighting(truth={"matrices": [{"id": "m_0001", "name": "LL", "level": {"room": "nowhere"}, "cells": {}}]})
    with pytest.raises(ConfigError):
        level_config(lights=d)
    d = lighting(truth={"matrices": [{"id": "m_0001", "name": "LL", "level": {"room": "kamar"}, "cells": {L1: {"dark": {"kind": "on"}}}}]})
    assert "no_template" in blocking(expand(level_config({"template": None}, lights=d), DEV)[1])


def test_helpers_of_the_light_level_are_allowlisted_by_domain_and_name():
    check_params("input_text/create", {"name": "AB 00000011 kamar light level"})
    check_params("input_number/delete", {"input_number_id": "ab_00000011_kamar_ambient_lux"})
    for command, params in (("input_text/create", {"name": "AB 00000011 kamar override"}), ("input_number/create", {"name": "Something"}),
                            ("input_text/delete", {"input_text_id": "my_text"}), ("input_boolean/create", {"name": "AB 00000011 kamar light level"})):
        with pytest.raises(NotAllowed):
            check_params(command, params)


async def test_light_level_api_room_device_versions_guards_and_deploy(core, home):
    home.states["sensor.kamar_lux"] = state("sensor.kamar_lux", "60", unit_of_measurement="lx", device_class="illuminance")
    home.entity_registry.append({"entity_id": "sensor.kamar_lux", "device_id": None, "area_id": "kamar"})
    tid = core.add_level_preset(core.store.revision, ADMIN)["template_id"]
    saved = core.save_level_room({"area_id": "kamar", "create": True}, core.store.revision, ADMIN)
    device_id = saved["device_id"]
    room = core.store.config["light_levels"]["rooms"]["kamar"]
    assert room["template"] == tid                                                 # the standard template, on purpose
    assert core.store.config["devices"][device_id]["targets"] == [LV.helper_ids(device_id, "kamar")["band"]]
    assert [s["entity_id"] for s in core.level_sensors("kamar", False)["sensors"]] == ["sensor.kamar_lux"]
    assert "sensor.lux" not in [s["entity_id"] for s in core.level_sensors("kamar", False)["sensors"]]   # another room's
    core.save_level_room({"area_id": "kamar", "source": {"entity_id": "sensor.kamar_lux"}}, core.store.revision, ADMIN)
    version = core.store.config["light_levels"]["rooms"]["kamar"]["version"]
    core.save_level_room({"area_id": "kamar", "source": {"entity_id": "sensor.kamar_lux"}}, core.store.revision, ADMIN)
    assert core.store.config["light_levels"]["rooms"]["kamar"]["version"] == version   # nothing changed: same version
    template = copy.deepcopy(core.store.config["light_levels"]["templates"][tid])
    template["hysteresis"] = 15
    core.save_level_template(template, core.store.revision, ADMIN)
    assert core.store.config["light_levels"]["templates"][tid]["version"] == 2
    with pytest.raises(ConfigError, match="used by"):
        core.delete_level_template(tid, core.store.revision, ADMIN)
    with pytest.raises(ConfigError, match="Light Level page"):
        await core.delete_device(device_id, core.store.revision, False, ADMIN)
    view = await core.levels_view()
    r = view["rooms"][0]
    assert r["reading"]["value"] == 60 and r["computed"] == "dark" and r["status"]["status"] == "not_deployed"
    plan = await core.plan(device_id)
    assert plan["can_deploy"], plan["reasons"]
    assert [(h["entity_id"], h["operation"]) for h in plan["helpers"]] == [(LV.helper_ids(device_id, "kamar")["band"], "create")]
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert LV.helper_ids(device_id, "kamar")["band_item"] in home.collections["input_text"]
    assert home.collections["input_text"][LV.helper_ids(device_id, "kamar")["band_item"]]["max"] == 32
    overview = await core.overview()
    assert any(g["area_id"] == "kamar" and device_id in g["device_ids"] for g in overview["rooms"])
    await core.delete_level_room("kamar", core.store.revision, False, ADMIN)
    assert "light_levels" not in core.store.config or "kamar" not in core.store.config["light_levels"]["rooms"]
    assert device_id not in core.store.config["devices"] and home.collections["input_text"] == {}


# ================================================================== Part 3: the Heating Designer
HD = "d_00000033"
TH = "climate.kamar"


def heating_config(**extra: Any) -> dict[str, Any]:
    d = {"id": "g_00000033", "kind": "heating", "version": 1, "name": "Heating", "thermostats": [TH], "profile": {"room": "kamar"},
         "modes": {"source": "schedule", "schedule": {"split": "all", "all": [{"at": "06:00", "mode": "comfort"}, {"at": "22:00", "mode": "night"}]}}}
    d.update(extra)
    return model.validate_config({"settings": {}, "heating": {"templates": {"h_00000001": H.preset_template("h_00000001")},
                                                             "rooms": {"kamar": {"template": "h_00000001", "overrides": {"night": {"kind": "none"}}}}},
                                  "presence": {"sources": {"z_00000001": {"name": "K", "kind": "room", "entity_id": "binary_sensor.k"}}},
                                  "devices": {HD: {"name": "Radiator", "targets": [TH], "rules": [], "designers": [d]}}})


def test_heating_values_inherit_global_room_thermostat_and_unset_stays_unset():
    hp = heating_config()["heating"]
    assert H.resolve(hp, "kamar", None, "comfort") == {"cell": {"kind": "set", "temp": 21}, "source": "global"}
    assert H.resolve(hp, "kamar", None, "night") == {"cell": {"kind": "none"}, "source": "room"}
    assert H.resolve(hp, "kamar", {"kind": "set", "temp": 19.5}, "comfort")["source"] == "thermostat"
    assert H.resolve(hp, "kamar", None, "unknown")["source"] == "unset"
    assert H.resolve(hp, "elsewhere", None, "comfort")["source"] == "unset"     # no profile chosen: nothing inherited


def test_schedule_windows_split_at_midnight_and_carry_the_last_entry_over():
    sched = {"split": "weekdays", "all": [], "weekdays": [{"at": "06:00:00", "mode": "comfort", "temp": None}, {"at": "23:00:00", "mode": "night", "temp": None}],
             "weekend": [{"at": "08:00:00", "mode": "comfort", "temp": None}, {"at": "23:30:00", "mode": None, "temp": 17.5}]}
    found = {(w["after"], w["before"], tuple(w["weekdays"]), w["slot"]["mode"] or w["slot"]["temp"]) for w in H.windows(sched)}
    assert (None, "06:00:00", ("tue", "wed", "thu", "fri"), "night") in found     # the night before a weekday
    assert (None, "06:00:00", ("mon",), 17.5) in found                            # Sunday's last entry carries into Monday
    assert (None, "08:00:00", ("sat",), "night") in found and (None, "08:00:00", ("sun",), 17.5) in found
    assert ("23:30:00", None, ("sat", "sun"), 17.5) in found
    assert H.next_transition(sched, "fri", "23:10:00") == {"weekday": "sat", "at": "08:00:00", "mode": "comfort", "temp": None, "in_days": 1}


@pytest.mark.parametrize("attrs,value,expected", [
    ({"temperature": 18}, 21, {"service": "set_temperature", "data": {"temperature": 21}}),
    ({"temperature": 21}, 21.2, {"service": None}),                                            # within half a step
    ({"temperature": 18, "max_temp": 25}, 30, {"service": "set_temperature", "data": {"temperature": 25}}),
    ({"temperature": 18, "target_temp_step": 1}, 20.4, {"service": "set_temperature", "data": {"temperature": 20}}),
    ({"temperature": 18}, "keep", {"service": None}),
    ({"temperature": 18}, "off", {"service": "set_hvac_mode", "data": {"hvac_mode": "off"}}),
    ({"temperature": 18, "preset_mode": "comfort"}, "preset:eco", {"service": "set_preset_mode", "data": {"preset_mode": "eco"}}),
    ({"supported_features": 2, "target_temp_low": 18, "target_temp_high": 24}, 20, {"service": "set_temperature", "data": {"target_temp_low": 20, "target_temp_high": 24}}),
])
def test_command_twin(attrs, value, expected):
    assert H.command({"state": "heat", "attributes": {"min_temp": 7, "max_temp": 30, "target_temp_step": 0.5, "supported_features": 1, **attrs}},
                     value, heat=False, hvac_mode=None) == expected


def test_an_off_thermostat_is_left_off_unless_chosen():
    off = {"state": "off", "attributes": {"temperature": 18, "supported_features": 1}}
    assert H.command(off, 21, heat=False, hvac_mode="heat") == {"service": None}
    assert H.command(off, 21, heat=True, hvac_mode="heat") == {"service": "set_temperature", "data": {"temperature": 21, "hvac_mode": "heat"}}
    assert H.command({"state": "unavailable"}, 21, heat=True, hvac_mode="heat") == {"service": None}


def test_heating_blocks_until_it_is_complete_and_never_chooses_for_you():
    assert blocking(expand(heating_config(profile={"room": None}), HD)[1]) == ["no_profile"]
    x = expand(heating_config(window={"enabled": True, "sensors": ["binary_sensor.w"], "action": "off", "unavailable": "ignore"}), HD)[1]
    assert "off_needs_heat" in blocking(x)
    x = expand(heating_config(window={"enabled": True, "sensors": ["binary_sensor.w"]}), HD)[1]
    assert {"no_window_action", "no_window_fallback"} <= set(blocking(x))
    x = expand(heating_config(away={"enabled": True}), HD)[1]
    assert "no_away_source" in blocking(x)
    x = expand(heating_config(override={"manual": True, "policy": "until_next"}, modes={"source": None}), HD)[1]
    assert "no_next" in blocking(x)
    x = expand(heating_config(presence={"enabled": True, "source": "z_00000001", "mode": "unknown"}), HD)[1]
    assert "unknown_mode" in blocking(x)
    effective, x = expand(heating_config(), HD)
    assert not blocking(x)
    out = compiler.compile_device(effective, model.validate_settings({}, model.Problems()))["automations"]
    assert [a["role"] for a in out] == ["group"]                                    # one automation per thermostat


def test_capabilities_heating_only_presets_and_setpoints():
    d = heating_config(when_off="heat", hvac_mode="heat_cool", cells={TH: {"comfort": {"kind": "preset", "preset": "boost"}}})["devices"][HD]["designers"][0]
    x = designers.Expansion({"id": HD, "targets": [TH], "rules": [], "enabled": True}, None)
    H.capability_problems(d, {TH: {"unknown": False, "hvac_modes": ["off", "heat"], "target": True, "range": False, "preset_modes": ["eco"]}}, x)
    assert sorted(p["code"] for p in x.problems) == ["unsupported_hvac", "unsupported_preset"]
    from tests.conftest import FakeHome
    from ab.discovery import Index
    home = FakeHome()
    home.states[TH] = state(TH, "heat", hvac_modes=["off", "heat"], supported_features=401, min_temp=7, max_temp=30, preset_modes=["eco"])
    cap = designers.capability(Index(home.states, home.entity_registry, home.devices, home.areas), TH)
    assert cap["designers"] == ["heating"] and cap["heating_modes"] == ["heat"] and not cap["can_cool"] and cap["presets"]


def test_quick_starts_fill_times_and_modes_but_no_sources():
    d = heating_config()["devices"][HD]["designers"][0]
    modes = ["comfort", "setback", "night", "away"]
    for kind in H.QUICK:
        out = H.quick_start(kind, d, modes)
        assert out["away"]["entity_id"] is None and out["presence"]["source"] is None and out["window"]["sensors"] == []
    assert len(H.quick_start("schedule", d, modes)["modes"]["schedule"]["weekdays"]) == 4
    assert H.quick_start("window", d, modes)["window"]["action"] == "frost"


async def test_heating_api_profiles_and_designer_on_a_thermostat(core, home):
    core.create_device({"name": "Heater", "targets": ["climate.heater"]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    tid = core.save_heating_template("preset", core.store.revision, ADMIN)["template_id"]
    core.save_heating_room({"area_id": "kamar", "template": tid, "overrides": {"comfort": {"kind": "set", "temp": 22}}}, core.store.revision, ADMIN)
    view = core.heating_view()
    assert view["rooms"][0]["cells"]["comfort"] == {"cell": {"kind": "set", "temp": 22}, "source": "room"}
    assert view["rooms"][0]["cells"]["setback"]["source"] == "global"
    created = core.create_designer(device_id, "heating", None, core.store.revision, ADMIN)
    designer_id = created["designer_id"]
    page = core.designer_page(device_id, designer_id)
    assert {p["code"] for p in page["designer"]["problems"] if p["level"] == "blocking"} == {"no_thermostats"}
    d = copy.deepcopy(page["designer"]["config"])
    d.update(thermostats=["climate.heater"], profile={"room": "kamar"})
    core.save_designer(device_id, d, core.store.revision, ADMIN)
    page = core.heating_quick(device_id, designer_id, "day_night", core.store.revision, ADMIN)
    assert page["designer"]["config"]["modes"]["source"] == "schedule"
    assert not [p for p in page["designer"]["problems"] if p["level"] == "blocking"]
    with pytest.raises(ConfigError, match="Heating Designers use"):
        core.delete_heating_room("kamar", core.store.revision, ADMIN)
    with pytest.raises(ConfigError, match="Room profiles use"):
        core.delete_heating_template(tid, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    assert plan["can_deploy"], plan["reasons"]


def test_no_dependency_on_smart_predictive_climate():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / "backend"
    names = {node.module or "" for path in root.rglob("*.py") for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
             if isinstance(node, ast.ImportFrom)}
    assert not any("predictive" in n or n.startswith("spc") for n in names)


def test_names_never_reach_a_variable_as_template_syntax():
    config = heating_config()
    raw = copy.deepcopy(config)
    raw["heating"]["templates"]["h_00000001"]["modes"][0]["name"] = "Comfort {{ states('x') }} {% if %}"
    config = model.validate_config(raw)
    effective, x = expand(config, HD)
    labels = [b["value"][1] for b in x.generators["g_00000033"].plans[TH]["branches"]]
    assert labels and not any(ch in label for label in labels for ch in "{}%")


def test_observations_through_the_app_s_state_events(core, home, monkeypatch):
    """0.5 e2e finding: the App passes (entity, new state); the observer keeps the previous state itself."""
    from ab.designers import levels as LVmod
    clock = [1000.0]
    monkeypatch.setattr(LVmod.time, "monotonic", lambda: clock[0])
    room = LV.validate_room("kamar", {"device_id": LL, "source": {"entity_id": "sensor.lux"}, "ambient": {
        "method": "calibrated", "lights": [L1]}}, "r", model.Problems())
    core.store.config["light_levels"] = {"rooms": {"kamar": room}}
    core.ha = home

    def event(entity: str, value: str, **attrs: Any) -> None:
        home.states[entity] = state(entity, value, **attrs)
        core._on_ha("state", (entity, home.states[entity]))  # noqa: SLF001
    for n in range(3):
        event(L1, "off")                       # the first event of a light is only remembered
        event("sensor.lux", "50")
        clock[0] += 60
        event(L1, "on", brightness=255)
        clock[0] += 5
        event("sensor.lux", str(170 + n))      # the reading steps by ~120 lx at full brightness
        clock[0] += 60
    found = core.light_observations.summary("kamar", L1)
    assert found and found["count"] == 3 and 120 <= found["median"] <= 122 and found["confidence"] == "consistent"
