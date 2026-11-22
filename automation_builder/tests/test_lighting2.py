"""Lighting Designer 2: Truth/Brightness Matrices, Colour & Mood, switches, Advanced Rules, priority, helpers."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from ab import compiler, designers, model, simulate
from ab.designers import lighting2 as L2, switches as SW
from ab.ha import NotAllowed, check_params
from tests.conftest import ADMIN, state

L1, L2_, L3 = "light.l1", "light.l2", "light.l3"
A1, A2 = "z_000000a1", "z_000000a2"
DEV = "d_0000abcd"


def sources() -> dict[str, Any]:
    return {"sources": {A1: {"name": "Area1", "kind": "zone", "entity_id": "binary_sensor.a1"},
                        A2: {"name": "Area2", "kind": "zone", "entity_id": "binary_sensor.a2"}}}


def col(cid: str, kind: str = "presence", **kw: Any) -> dict[str, Any]:
    return {"id": cid, "kind": kind, **kw}


def presence_truth(cells: dict[str, Any], policy: str | None = None, ranks: list[str] | None = None) -> dict[str, Any]:
    return {"id": "m_0001", "name": "Presence", "columns": [
        col("c_0001", source=A1, state="occupied"), col("c_0002", source=A1, state="vacant"),
        col("c_0003", source=A2, state="occupied"), col("c_0004", source=A2, state="vacant")],
        "cells": cells, "policy": policy, "ranks": ranks or []}


def designer(**parts: Any) -> dict[str, Any]:
    d = {"id": "g_00000001", "kind": "lighting", "version": 2, "name": "Lighting", "lights": [L1, L2_]}
    d.update(parts)
    return d


def device(d: dict[str, Any], rules: list[dict[str, Any]] | None = None, targets: list[str] | None = None) -> dict[str, Any]:
    return {"name": "Living", "targets": targets or [L1, L2_, L3], "rules": rules or [], "designers": [d]}


def expand(d: dict[str, Any], **kw: Any) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    cfg = model.validate_config({"devices": {DEV: device(d, **kw)}, "presence": sources()})
    dev = cfg["devices"][DEV]
    x = designers.expand(dev, cfg.get("presence"))
    return dev, x, cfg


def problems(x: Any, level: str = "blocking") -> list[str]:
    return [p["code"] for p in x.problems if p["level"] == level]


def evaluate(x: Any, light: str, states: dict[str, Any]) -> dict[str, Any]:
    gen = next(iter(x.generators.values()))
    ev = simulate.Evaluator(states, {"time": "12:00:00", "weekday": "mon"}, x.refs.evaluators, lambda e: e)
    return L2.evaluate_plan(gen.plans[light], ev, states.get(light))


def on(entity: str, value: str = "on", **attributes: Any) -> dict[str, Any]:
    return {"state": value, "attributes": attributes}


SPEC_TRUTH = {L1: {"c_0001": {"kind": "off"}, "c_0002": {"kind": "on"}, "c_0003": {"kind": "on"}, "c_0004": {"kind": "on"}},
              L2_: {"c_0001": {"kind": "off"}, "c_0002": {"kind": "on"}, "c_0003": {"kind": "on"}, "c_0004": {"kind": "on"}}}


# ------------------------------------------------------------------ the document
def test_new_designers_are_version_2_with_nothing_set():
    blank = designers.blank("lighting", "g_00000002", [L1])
    assert blank["version"] == 2 and blank["lights"] == [] and blank["truth"]["matrices"] == []
    assert blank["priority"] == ["advanced", "override", "mood", "brightness", "truth"]  # the proposed default, shown
    assert blank["override"]["policy"] is None and blank["switches"] == [] and blank["brightness"]["zero"] is None


def test_version_1_documents_are_kept_and_compile_as_before():
    v1 = {"id": "g_00000001", "kind": "lighting", "version": 1, "name": "Old", "lights": [L1], "primary": "mood",
          "mood": {"enabled": True, "entity_id": "input_select.mood", "values": ["Hygge"],
                   "cells": {L1: {"Hygge": {"kind": "on", "brightness_pct": 30}}}}}
    cfg = model.validate_config({"devices": {DEV: {"name": "Living", "targets": [L1], "rules": [], "designers": [v1]}}})
    d = cfg["devices"][DEV]["designers"][0]
    assert d["version"] == 1 and "primary" in d and designers.supported(d)
    x = designers.expand(cfg["devices"][DEV], None)
    out = compiler.compile_device(designers.effective(cfg["devices"][DEV], x), cfg["settings"])
    assert [a["role"] for a in out["automations"]] == ["shared"]          # version 1: one shared automation, as in 0.3
    assert "brightness_pct: 30" in compiler.to_yaml(compiler.with_id(out["automations"][0]))


def test_truth_cells_have_no_percentages_and_brightness_cells_no_on_off():
    p = model.Problems()
    L2.validate(designer(truth={"matrices": [presence_truth({L1: {"c_0001": {"kind": "set", "pct": 40}}})]}),
                "g_00000001", [L1, L2_], "d", p)
    assert any("Truth Matrix cell" in i["message"] for i in p.items)
    p = model.Problems()
    L2.validate(designer(brightness={"matrices": [{**presence_truth({L1: {"c_0001": {"kind": "on"}}}), "id": "m_0002"}]}),
                "g_00000001", [L1, L2_], "d", p)
    assert any("Brightness Matrix cell" in i["message"] for i in p.items)


# ------------------------------------------------------------------ truth, brightness, gating
def test_truth_on_off_and_brightness_with_truth_gating():
    d = designer(truth={"matrices": [presence_truth(SPEC_TRUTH, "priority", ["c_0001", "c_0003", "c_0004"])]},
                 brightness={"matrices": [{"id": "m_0002", "name": "B", "columns": [
                     col("c_0005", source=A1, state="occupied"), col("c_0006", source=A1, state="vacant")],
                     "cells": {L1: {"c_0006": {"kind": "set", "pct": 50}, "c_0005": {"kind": "set", "pct": 25}}}}]})
    _dev, x, _cfg = expand(d)
    assert problems(x) == []
    off = on(L1, "off")
    # Area1 vacant, Area2 vacant: L1 ON at 50 %
    r = evaluate(x, L1, {"binary_sensor.a1": on("", "off"), "binary_sensor.a2": on("", "off"), L1: off})
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 50}}
    assert r["resolved"]["on"][1].startswith("Truth") and r["resolved"]["brightness"][2] == "brightness"
    # Area1 occupied (rank 1, OFF) wins over Area2 occupied (ON): the 25 % never switches it on
    r = evaluate(x, L1, {"binary_sensor.a1": on(""), "binary_sensor.a2": on(""), L1: on(L1, "on", brightness=128)})
    assert r["command"]["service"] == "turn_off"
    r = evaluate(x, L1, {"binary_sensor.a1": on(""), "binary_sensor.a2": on(""), L1: off})
    assert r["command"] == {"service": None, "why": "already off"}


def test_brightness_alone_never_switches_a_light_on():
    d = designer(brightness={"matrices": [{"id": "m_0002", "name": "B", "columns": [col("c_0005", source=A1, state="occupied")],
                                           "cells": {L1: {"c_0005": {"kind": "set", "pct": 70}}}}]})
    _dev, x, _cfg = expand(d)
    r = evaluate(x, L1, {"binary_sensor.a1": on(""), L1: on(L1, "off")})
    assert r["command"]["service"] is None
    r = evaluate(x, L1, {"binary_sensor.a1": on(""), L1: on(L1, "on", brightness=255)})
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 70}}
    r = evaluate(x, L1, {"binary_sensor.a1": on(""), L1: on(L1, "on", brightness=178)})   # 70 % = 178.5: already so
    assert r["command"]["service"] is None


def test_zero_brightness_needs_an_explicit_meaning():
    cells = {L1: {"c_0005": {"kind": "set", "pct": 0}}}
    base = {"matrices": [{"id": "m_0002", "name": "B", "columns": [col("c_0005", source=A1, state="occupied")], "cells": cells}]}
    truth = {"matrices": [{"id": "m_0001", "name": "T", "columns": [col("c_0001", source=A1, state="occupied")],
                           "cells": {L1: {"c_0001": {"kind": "on"}}}}]}
    _dev, x, _cfg = expand(designer(truth=truth, brightness=base))
    assert "zero_brightness" in problems(x)
    states = {"binary_sensor.a1": on(""), L1: on(L1, "on", brightness=100)}
    for zero, expected in (("off", {"service": "turn_off", "data": {}}), ("minimum", {"service": "turn_on", "data": {"brightness_pct": 1}}),
                           ("unchanged", {"service": None, "why": "already so"})):
        _dev, x, _cfg = expand(designer(truth=truth, brightness={**base, "zero": zero}))
        assert problems(x) == [] and evaluate(x, L1, states)["command"] == expected, zero


def test_presence_and_lux_both_as_truth_and_brightness_sources():
    lux_cols = [col("c_0011", "numeric", entity_id="sensor.lux", below=50, for_seconds=30),
                col("c_0012", "numeric", entity_id="sensor.lux", **{"from": 50})]
    d = designer(truth={"matrices": [{"id": "m_0001", "name": "Lux", "columns": lux_cols,
                                      "cells": {L1: {"c_0011": {"kind": "on"}, "c_0012": {"kind": "off"}}}}]},
                 brightness={"matrices": [{"id": "m_0002", "name": "Zone", "columns": [col("c_0013", source=A1, state="occupied"),
                                                                                          col("c_0014", source=A1, state="vacant")],
                                           "cells": {L1: {"c_0013": {"kind": "set", "pct": 80}, "c_0014": {"kind": "set", "pct": 20}}}}]})
    dev, x, cfg = expand(d)
    assert problems(x) == []
    r = evaluate(x, L1, {"sensor.lux": on("", "12"), "binary_sensor.a1": on(""), L1: on(L1, "off")})
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 80}}
    r = evaluate(x, L1, {"sensor.lux": on("", "400"), "binary_sensor.a1": on(""), L1: on(L1, "on", brightness=200)})
    assert r["command"]["service"] == "turn_off"
    r = evaluate(x, L1, {"sensor.lux": on("", "unavailable"), "binary_sensor.a1": on(""), L1: on(L1, "off")})
    assert r["command"]["service"] is None        # an unavailable sensor is no light level: nothing is decided
    yaml_text = "".join(compiler.to_yaml(compiler.with_id(a)) for a in compiler.compile_device(
        designers.effective(dev, x), cfg["settings"])["automations"])
    assert "for:\n      hours: 0\n      minutes: 0\n      seconds: 30" in yaml_text  # the delay is on the crossing triggers


def test_simultaneous_zones_need_an_explicit_policy_never_the_column_order():
    _dev, x, _cfg = expand(designer(truth={"matrices": [presence_truth(SPEC_TRUTH)]}))
    assert "no_policy" in problems(x)
    _dev, x, _cfg = expand(designer(truth={"matrices": [presence_truth(SPEC_TRUTH, "priority")]}))
    assert "unranked" in problems(x)
    both = {"binary_sensor.a1": on(""), "binary_sensor.a2": on(""), L1: on(L1, "off")}
    for policy, ranks, expected in (("priority", ["c_0003", "c_0004", "c_0001"], "turn_on"),
                                    ("priority", ["c_0001", "c_0003", "c_0004"], None),
                                    ("on_wins", [], "turn_on"), ("off_wins", [], None), ("exclusive", [], None)):
        _dev, x, _cfg = expand(designer(truth={"matrices": [presence_truth(SPEC_TRUTH, policy, ranks)]}))
        assert problems(x) == [], (policy, x.problems)
        assert evaluate(x, L1, both)["command"]["service"] == expected, policy
    # exclusive: when they agree it acts; when they disagree nothing is decided
    _dev, x, _cfg = expand(designer(truth={"matrices": [presence_truth(SPEC_TRUTH, "exclusive")]}))
    r = evaluate(x, L1, {"binary_sensor.a1": on("", "off"), "binary_sensor.a2": on(""), L1: on(L1, "off")})
    assert r["command"]["service"] == "turn_on"
    assert evaluate(x, L1, both)["truth"] is None


def test_several_matrices_combine_explicitly():
    second = {"id": "m_0003", "name": "Switch", "columns": [col("c_0021", "state", entity_id="switch.wall", state="on"),
                                                              col("c_0022", "state", entity_id="switch.wall", state="off")],
              "cells": {L1: {"c_0021": {"kind": "on"}, "c_0022": {"kind": "off"}}}}
    first = {"id": "m_0001", "name": "Zone", "columns": [col("c_0001", source=A1, state="occupied")],
             "cells": {L1: {"c_0001": {"kind": "on"}}}}
    _dev, x, _cfg = expand(designer(truth={"matrices": [first, second]}))
    assert "no_combine" in problems(x)
    states = {"binary_sensor.a1": on(""), "switch.wall": on("", "off"), L1: on(L1, "on", brightness=100)}
    for combine, expected in (("priority", None), ("on_wins", None), ("off_wins", "turn_off")):
        _dev, x, _cfg = expand(designer(truth={"matrices": [first, second], "combine": combine}))
        assert problems(x) == [] and evaluate(x, L1, states)["command"]["service"] == expected, combine


def test_unset_no_action_and_defaults_are_strict():
    m = {"id": "m_0001", "name": "T", "columns": [col("c_0001", source=A1, state="occupied"), col("c_0002", source=A1, state="vacant")],
         "cells": {L1: {"c_0001": {"kind": "default"}}}}
    _dev, x, _cfg = expand(designer(truth={"matrices": [m]}))
    assert "missing_default" in problems(x)
    _dev, x, _cfg = expand(designer(truth={"matrices": [{**m, "default": {"kind": "on"}}]}))
    assert problems(x) == []
    assert evaluate(x, L1, {"binary_sensor.a1": on("", "off"), L1: on(L1, "off")})["command"]["service"] == "turn_on"  # default fills unset
    # NO ACTION keeps the light as it is and stops the default
    _dev, x, _cfg = expand(designer(truth={"matrices": [{**m, "cells": {L1: {"c_0002": {"kind": "none"}}}, "default": {"kind": "on"}}]}))
    r = evaluate(x, L1, {"binary_sensor.a1": on("", "off"), L1: on(L1, "off")})
    assert r["truth"][0] == "keep" and r["command"]["service"] is None


# ------------------------------------------------------------------ mood and priority
def mood(**cells: Any) -> dict[str, Any]:
    return {"enabled": True, "entity_id": "input_select.mood", "values": ["Dagur", "Hygge"],
            "cells": {L1: {"Hygge": {"kind": "set", "color_temp_kelvin": 2700, **cells}}}}


TRUTH_ON = {"matrices": [{"id": "m_0001", "name": "T", "columns": [col("c_0001", "always")], "cells": {L1: {"c_0001": {"kind": "on"}}}}]}
BRIGHT_40 = {"matrices": [{"id": "m_0002", "name": "B", "columns": [col("c_0002", "always")],
                           "cells": {L1: {"c_0002": {"kind": "set", "pct": 40}}}}]}


def test_property_specific_resolution_matches_the_spec_example():
    _dev, x, _cfg = expand(designer(truth=TRUTH_ON, brightness=BRIGHT_40, mood=mood()))
    states = {"input_select.mood": on("", "Hygge"), L1: on(L1, "off")}
    r = evaluate(x, L1, states)
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 40, "color_temp_kelvin": 2700}}
    assert [r["resolved"][p][2] for p in ("on", "brightness", "color")] == ["truth", "brightness", "mood"]
    # Hygge with its own brightness: the mood ranks above the Brightness Matrix by default
    _dev, x, _cfg = expand(designer(truth=TRUTH_ON, brightness=BRIGHT_40, mood=mood(brightness_pct=25)))
    assert evaluate(x, L1, states)["command"]["data"]["brightness_pct"] == 25
    # ... unless the user puts the Brightness Matrix higher
    order = ["advanced", "override", "brightness", "mood", "truth"]
    _dev, x, _cfg = expand(designer(truth=TRUTH_ON, brightness=BRIGHT_40, mood=mood(brightness_pct=25), priority=order))
    assert evaluate(x, L1, states)["command"]["data"]["brightness_pct"] == 40


def test_advanced_rule_overrides_and_the_baseline_returns_by_itself():
    tv = {"id": "a_0001", "name": "TV", "kind": "while", "lights": [L1],
          "conditions": {"type": "and", "items": [{"type": "state", "entity_id": "media_player.tv", "state": "playing"}]},
          "set": {"truth": "off"}}
    dev, x, cfg = expand(designer(truth=TRUTH_ON, brightness=BRIGHT_40, mood=mood(), advanced=[tv]))
    assert problems(x) == []
    lit = on(L1, "on", brightness=102, color_mode="color_temp", color_temp_kelvin=2700)
    r = evaluate(x, L1, {"media_player.tv": on("", "playing"), "input_select.mood": on("", "Hygge"), L1: lit})
    assert r["command"]["service"] == "turn_off" and r["resolved"]["on"][1] == "Advanced: TV"
    r = evaluate(x, L1, {"media_player.tv": on("", "idle"), "input_select.mood": on("", "Hygge"), L1: on(L1, "off")})
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 40, "color_temp_kelvin": 2700}}
    # the evaluation runs when the TV stops: its conditions' entity is a trigger of the light's automation
    auto = next(a for a in compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"] if a["role"] == "group")
    assert {"trigger": "state", "entity_id": "media_player.tv", "from": "playing"} in [
        {k: v for k, v in t.items() if k != "id"} for t in auto["config"]["triggers"]]


def test_one_automation_per_light_and_the_command_is_compared_first():
    dev, x, cfg = expand(designer(truth={"matrices": [presence_truth(SPEC_TRUTH, "on_wins")]}))
    out = compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"]
    assert sorted(a["role"] for a in out) == ["group", "group"] and {a["group"] for a in out} == {L1, L2_}
    assert all(a["mode"] == "queued" for a in out)
    text = compiler.to_yaml(compiler.with_id(out[0]))
    assert "data: '{{ ab_cmd.data }}'" in text and "states[''light.l" in text


# ------------------------------------------------------------------ switches and overrides
def wall(*mappings: dict[str, Any], kind: str = "momentary") -> dict[str, Any]:
    return {"id": "w_0001", "name": "Wall", "kind": kind, "interactions": [
        {"id": "i_0001", "kind": "single", "trigger": {"type": "event", "event_type": "zha_event",
                                                        "event_data": {"device_ieee": "00:11", "command": "on"}}},
        {"id": "i_0002", "kind": "double", "trigger": {"type": "mqtt", "topic": "zigbee2mqtt/Wall/action", "payload": "double"}},
        {"id": "i_0003", "kind": "long", "trigger": {"type": "button", "entity_id": "event.wall", "event_types": ["hold"]}},
        {"id": "i_0004", "kind": "release", "trigger": {"type": "button", "entity_id": "event.wall", "event_types": ["release"]}}],
        "mappings": list(mappings)}


def mapping(mid: str, interaction: str, action: dict[str, Any], lights: list[str] | None = None, override: bool = False) -> dict[str, Any]:
    return {"id": mid, "interaction": interaction, "lights": lights or [L1], "action": action, "override": override}


def test_switch_actions_compile_to_native_triggers_and_toggle_once():
    d = designer(truth=TRUTH_ON, mood=mood(), switches=[wall(
        mapping("p_0001", "i_0001", {"kind": "toggle"}),
        mapping("p_0002", "i_0002", {"kind": "mood", "value": "Hygge"}),
        mapping("p_0003", "i_0003", {"kind": "dim", "direction": "up", "step": 10, "interval": 1, "release": "i_0004"}))])
    dev, x, cfg = expand(d)
    assert problems(x) == []
    out = compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"]
    shared = next(a for a in out if a["role"] == "shared")
    text = compiler.to_yaml(compiler.with_id(shared))
    assert text.count("action: light.toggle") == 1 and "event_type: zha_event" in text and "topic: zigbee2mqtt/Wall/action" in text
    assert "color_temp_kelvin: 2700" in text                                          # "apply Hygge"
    dim = next(a for a in out if a["role"] == "separate")                              # it waits: an automation of its own
    assert dim["mode"] == "restart" and "brightness_step_pct: 10" in compiler.to_yaml(compiler.with_id(dim))
    assert "wait_for_trigger" in compiler.to_yaml(compiler.with_id(dim))


def test_two_mappings_toggling_one_light_on_one_press_are_a_conflict():
    from ab import conflicts
    d = designer(truth=TRUTH_ON, switches=[wall(mapping("p_0001", "i_0001", {"kind": "toggle"}),
                                                mapping("p_0002", "i_0001", {"kind": "toggle"}))])
    dev, x, _cfg = expand(d)
    found = conflicts.analyse(designers.effective(dev, x))
    assert any(c["code"] in ("double_toggle", "duplicate_rule") and c["level"] == "blocking" for c in found)


def test_override_needs_a_policy_and_brings_its_helpers_and_release():
    d = designer(truth=TRUTH_ON, switches=[wall(mapping("p_0001", "i_0001", {"kind": "toggle"}, override=True),
                                                mapping("p_0002", "i_0002", {"kind": "resume"}))])
    _dev, x, _cfg = expand(d)
    assert "no_override_policy" in problems(x)
    h = L2.helper_ids(DEV, L1)
    assert h["flag"] == "input_boolean.ab_0000abcd_l1_override" and h["timer"] == "timer.ab_0000abcd_l1_override"
    for policy, extra, needed in (("duration", {"minutes": 30}, {"input_boolean", "timer"}),
                                  ("until_resumed", {}, {"input_boolean"}),
                                  ("until_vacant", {"source": A1}, {"input_boolean"}),
                                  ("until_event", {"release": [{"type": "time", "at": "06:00"}]}, {"input_boolean"})):
        dev, x, cfg = expand({**d, "override": {"policy": policy, **extra}})
        assert problems(x) == [], (policy, x.problems)
        view = x.views["g_00000001"]
        assert {hh["domain"] for hh in view["helpers"]} == needed, policy
        texts = "".join(compiler.to_yaml(compiler.with_id(a)) for a in
                        compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"])
        assert "input_boolean.turn_on" in texts and "input_boolean.turn_off" in texts   # started by the press, ended by resume
        if policy == "duration":
            assert "timer.start" in texts and "duration: 00:30:00" in texts and "timer.finished" in texts
        if policy == "until_vacant":
            assert "binary_sensor.a1\n    to: 'off'" in texts


def test_override_holds_the_light_and_its_end_restores_the_current_baseline():
    d = designer(truth=TRUTH_ON, brightness=BRIGHT_40, override={"policy": "until_resumed"},
                 switches=[wall(mapping("p_0001", "i_0001", {"kind": "off"}, override=True))])
    _dev, x, _cfg = expand(d)
    flag = L2.helper_ids(DEV, L1)["flag"]
    r = evaluate(x, L1, {flag: on("", "on"), L1: on(L1, "off")})
    assert r["override"] is True and r["command"]["service"] is None and r["resolved"]["on"][1] == "Manual override"
    r = evaluate(x, L1, {flag: on("", "off"), L1: on(L1, "off")})
    assert r["command"] == {"service": "turn_on", "data": {"brightness_pct": 40}}   # back to what the matrices say now


def test_an_ordinary_switch_event_gets_no_priority_of_its_own():
    """Without an override a press is one-shot: the next evaluation applies the baseline again."""
    d = designer(truth=TRUTH_ON, switches=[wall(mapping("p_0001", "i_0001", {"kind": "off"}))])
    _dev, x, _cfg = expand(d)
    assert x.views["g_00000001"]["helpers"] == []
    gen = x.generators["g_00000001"]
    assert gen.plans[L1]["override"] is None
    assert evaluate(x, L1, {L1: on(L1, "off")})["command"]["service"] == "turn_on"


def test_manual_change_detection_uses_the_context_not_every_state_change():
    d = designer(truth=TRUTH_ON, override={"policy": "duration", "minutes": 20, "manual": True})
    dev, x, cfg = expand(d)
    text = "".join(compiler.canonical(a["config"]) for a in compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"])
    assert "trigger.to_state.context.parent_id is none" in text and "from_state.state in ['on', 'off']" in text


# ------------------------------------------------------------------ the twin of the command template
def test_command_twin_compares_before_sending():
    resolved = lambda **kw: {k: [v, "x", "y"] for k, v in kw.items()}  # noqa: E731
    assert L2.command(None, resolved(on="on")) == {"service": None, "why": "unavailable"}
    assert L2.command({"state": "unavailable"}, resolved(on="on"))["service"] is None
    lit = {"state": "on", "attributes": {"brightness": 102, "color_mode": "color_temp", "color_temp_kelvin": 2702,
                                         "min_color_temp_kelvin": 2000, "max_color_temp_kelvin": 6500}}
    assert L2.command(lit, resolved(on="on", brightness=40, color={"ct": 2700}))["service"] is None
    low = {"state": "on", "attributes": {**lit["attributes"], "color_temp_kelvin": 2000}}
    assert L2.command(low, resolved(color={"ct": 1500}))["service"] is None       # clamped to what the lamp can do
    assert L2.command(lit, resolved(color={"rgb": [255, 0, 0], "hs": [0.0, 100.0]}))["data"] == {"rgb_color": [255, 0, 0]}
    assert L2.command(lit, resolved(on="off", transition=2)) == {"service": "turn_off", "data": {"transition": 2}}
    assert L2.command({"state": "off", "attributes": {}}, resolved(brightness=50))["service"] is None
    assert L2.hs_of([255, 128, 0]) == [30.1, 100.0]


# ------------------------------------------------------------------ switch discovery
def test_interactions_are_classified_from_home_assistants_own_words():
    assert SW.classify("remote_button_short_press", "turn_on") == "single"
    assert SW.classify("remote_button_double_press", "button_1") == "double"
    assert SW.classify("remote_button_long_release") == "release"
    assert SW.classify("action", "brightness_move_up") == "long"
    assert SW.classify("action", "brightness_step_down") == "down"
    assert SW.classify("turned_on") == "on"
    items = SW.from_device_triggers([{"config": {"device_id": "x", "domain": "mqtt", "type": "action", "subtype": "single",
                                                 "discovery_id": "0x1 action_single"}, "label": "Wall: action single"}])
    assert items[0]["kind"] == "single" and items[0]["trigger"]["type"] == "device"


def test_helper_commands_are_limited_to_the_apps_own_helpers():
    check_params("input_boolean/create", {"name": "AB 0000abcd l1 override"})
    check_params("timer/delete", {"timer_id": "ab_0000abcd_l1_override"})
    for kind, params in (("input_boolean/create", {"name": "Kitchen"}), ("input_boolean/delete", {"input_boolean_id": "kitchen"}),
                         ("timer/delete", {"timer_id": "ab_0000abcd_l1"})):
        with pytest.raises(NotAllowed):
            check_params(kind, params)


# ------------------------------------------------------------------ through the App
def seed(core: Any, d: dict[str, Any]) -> None:
    core.store.save(model.validate_config({"devices": {DEV: device(d)}, "presence": sources()}),
                    expected_revision=core.store.revision, reason="t")


async def test_deployment_creates_owns_and_removes_the_helpers(core, home):
    for light in (L1, L2_, L3):
        home.states[light] = state(light, "off", supported_color_modes=["brightness"])
    d = designer(truth=TRUTH_ON, override={"policy": "duration", "minutes": 15},
                 switches=[wall(mapping("p_0001", "i_0001", {"kind": "toggle"}, override=True))])
    seed(core, d)
    plan = await core.plan(DEV)
    assert plan["can_deploy"], plan["reasons"]
    assert sorted((h["entity_id"], h["operation"]) for h in plan["helpers"]) == [
        ("input_boolean.ab_0000abcd_l1_override", "create"), ("timer.ab_0000abcd_l1_override", "create")]
    await core.deploy(DEV, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert set(core.deploys.helpers) == {"input_boolean.ab_0000abcd_l1_override", "timer.ab_0000abcd_l1_override"}
    assert home.collections["timer"]["ab_0000abcd_l1_override"]["restore"] is True
    plan = await core.plan(DEV)
    assert not plan["changes"] and {h["operation"] for h in plan["helpers"]} == {"keep"}
    # the override no longer needs a timer: it is deleted after the automations no longer use it
    core.save_designer(DEV, {**copy.deepcopy(core.store.config["devices"][DEV]["designers"][0]),
                             "override": {"policy": "until_resumed"}}, core.store.revision, ADMIN)
    plan = await core.plan(DEV)
    assert ("timer.ab_0000abcd_l1_override", "delete") in [(h["entity_id"], h["operation"]) for h in plan["helpers"]]
    await core.deploy(DEV, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert "ab_0000abcd_l1_override" not in home.collections["timer"] and "timer.ab_0000abcd_l1_override" not in core.deploys.helpers
    await core.delete_device(DEV, core.store.revision, False, ADMIN)
    assert home.collections["input_boolean"] == {} and core.deploys.helpers == {}


async def test_a_helper_the_app_did_not_create_blocks_and_is_never_touched(core, home):
    home.states[L1] = state(L1, "off", supported_color_modes=["brightness"])
    await home.command("input_boolean/create", name="AB 0000abcd l1 override")
    d = designer(truth=TRUTH_ON, override={"policy": "until_resumed"},
                 switches=[wall(mapping("p_0001", "i_0001", {"kind": "toggle"}, override=True))])
    seed(core, d)
    plan = await core.plan(DEV)
    assert not plan["can_deploy"] and any("did not create it" in r for r in plan["reasons"])
    await core.delete_device(DEV, core.store.revision, False, ADMIN)
    assert "ab_0000abcd_l1_override" in home.collections["input_boolean"]


async def test_designer_page_simulation_and_diagnostics_show_who_controls(core, home):
    home.states[L1] = state(L1, "on", supported_color_modes=["color_temp"], brightness=255, color_mode="color_temp",
                            color_temp_kelvin=4000, min_color_temp_kelvin=2000, max_color_temp_kelvin=6500)
    home.states["input_select.mood"]["state"] = "Hygge"
    d = designer(truth=TRUTH_ON, brightness=BRIGHT_40, mood=mood(), override={"policy": "until_resumed"},
                 switches=[wall(mapping("p_0001", "i_0001", {"kind": "toggle"}, override=True))])
    seed(core, d)
    page = core.designer_page(DEV, "g_00000001")
    light = next(item for item in page["live"]["lights"] if item["entity_id"] == L1)
    assert light["command"] == {"service": "turn_on", "data": {"brightness_pct": 40, "color_temp_kelvin": 2700}}
    assert light["resolved"]["color"][1] == "Mood: Hygge" and page["helpers"][0]["exists"] is False
    inputs = core.simulation_inputs(DEV)
    assert any(e["id"] == "i_0001" for e in inputs["events"])
    sim = core.simulation(DEV, {"event": "i_0001", "states": {L1: {"state": "off", "attributes": {}}}})
    result = sim["lighting"]["g_00000001"]
    assert result["event"]["happened"][0]["text"].startswith("toggle") and "manual override starts" in result["event"]["happened"][0]["text"]
    assert next(item for item in result["lights"] if item["entity_id"] == L1)["override"] is True
    diag = await core.diagnostics()
    summary = diag["devices"][0]["lighting"]["g_00000001"]
    assert summary[0]["controlled_by"]["color"] == "Mood: Hygge"


# ------------------------------------------------------------------ conversion from version 1
def v1_presence(**extra: Any) -> dict[str, Any]:
    d = {"id": "g_00000001", "kind": "lighting", "version": 1, "name": "Old", "lights": [L1], "primary": "presence",
         "presence": {"enabled": True, "policy": "priority", "columns": [{"source": A1, "state": "occupied"},
                                                                          {"source": A2, "state": "occupied"}],
                      "cells": {L1: {f"{A1}:occupied": {"kind": "on", "brightness_pct": 60},
                                     f"{A2}:occupied": {"kind": "off"}}}},
         "override": {"manual_minutes": 10}}
    d.update(extra)
    return d


def test_conversion_is_explicit_lossless_or_refused(core):
    cfg = model.validate_config({"devices": {DEV: {"name": "Living", "targets": [L1], "rules": [], "designers": [v1_presence()]}},
                                 "presence": sources()})
    core.store.save(cfg, expected_revision=core.store.revision, reason="t")
    report = core.convert_designer(DEV, "g_00000001", False, None, ADMIN)
    assert report["refused"] == [] and any("continuously" in c for c in report["changes"])
    assert core.store.config["devices"][DEV]["designers"][0]["version"] == 1            # a report changes nothing
    core.convert_designer(DEV, "g_00000001", True, core.store.revision, ADMIN)
    d = core.store.config["devices"][DEV]["designers"][0]
    assert d["version"] == 2 and d["truth"]["matrices"][0]["policy"] == "priority" and len(d["truth"]["matrices"][0]["ranks"]) == 2
    assert d["brightness"]["matrices"][0]["cells"][L1] and d["override"] == {"policy": "duration", "minutes": 10, "source": None,
                                                                             "release": [], "manual": True}
    analysis = core.analysis(core.store.config["devices"][DEV])
    assert analysis["designer_problems"] == []
    # ambiguous parts are refused, never converted silently
    bad = v1_presence()
    bad["presence"]["policy"] = "compatible"
    bad["lux"] = {"enabled": True, "entity_id": "sensor.lux", "bands": [{"id": "b_0001", "name": "Dark", "below": 50},
                                                                         {"id": "b_0002", "name": "Bright"}], "gate": {L1: {"on": ["b_0001"]}}}
    clean = model.validate_config({"devices": {DEV: {"name": "Living", "targets": [L1], "rules": [], "designers": [bad]}},
                                   "presence": sources()})["devices"][DEV]["designers"][0]
    result = L2.convert(clean, sources()["sources"])
    assert result["designer"] is None and len(result["refused"]) == 2


# ------------------------------------------------------------------ UNSET vs NO ACTION, selecting a mood, moving rules here
def test_no_action_mapping_is_explicit_and_compiles_to_nothing():
    d = designer(truth=TRUTH_ON, switches=[wall(mapping("p_0001", "i_0001", {"kind": "none"}, lights=[]))])
    dev, x, cfg = expand(d)
    assert problems(x) == [] and "no_action" in [p["code"] for p in x.problems if p["level"] == "info"]
    out = compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"]
    assert all("zha_event" not in compiler.canonical(a["config"]) for a in out)


def test_a_switch_can_select_a_mood():
    d = designer(truth=TRUTH_ON, mood=mood(), switches=[wall(mapping("p_0001", "i_0002", {"kind": "select_mood", "value": "Hygge"}, lights=[]))])
    dev, x, cfg = expand(d)
    assert problems(x) == []
    text = "".join(compiler.canonical(a["config"]) for a in compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"])
    assert '"action":"input_select.select_option"' in text and '"option":"Hygge"' in text and '"entity_id":["input_select.mood"]' in text


async def test_ordinary_button_rules_move_into_switches_without_duplicates(core, home):
    for light in (L1, L2_, L3):
        home.states[light] = state(light, "off", supported_color_modes=["brightness"])
    rules = [{"id": "r_0000b001", "name": "Remote on", "enabled": True,
              "triggers": [{"type": "event", "event_type": "zha_event", "event_data": {"command": "on"}}],
              "conditions": {"type": "and", "items": []},
              "actions": [{"type": "service", "action": "light.turn_on", "target": [L1], "data": {"brightness_pct": 60}}]},
             {"id": "r_0000b002", "name": "Not simple", "enabled": True,
              "triggers": [{"type": "event", "event_type": "zha_event", "event_data": {"command": "off"}}],
              "conditions": {"type": "and", "items": [{"type": "state", "entity_id": "input_boolean.manual", "state": "on"}]},
              "actions": [{"type": "service", "action": "light.turn_off", "target": [L1], "data": {}}]}]
    core.store.save(model.validate_config({"devices": {DEV: {**device(designer(truth=TRUTH_ON)), "rules": rules}}, "presence": sources()}),
                    expected_revision=core.store.revision, reason="t")
    page = core.designer_page(DEV, "g_00000001")
    assert [a["rule_id"] for a in page["adoptable"]] == ["r_0000b001"]       # the one with a condition stays a rule
    result = core.adopt_rule(DEV, "g_00000001", "r_0000b001", core.store.revision, ADMIN)
    dev = core.store.config["devices"][DEV]
    assert [r["id"] for r in dev["rules"]] == ["r_0000b002"]
    switch = dev["designers"][0]["switches"][0]
    assert switch["adopted"] and switch["mappings"][0]["action"] == {"kind": "brightness", "pct": 60}
    assert switch["interactions"][0]["trigger"]["event_data"] == {"command": "on"} and result["designer"]["config"]["switches"]


async def test_blueprint_export_of_a_version_2_designer_keeps_values_fixed(core, home):
    for light in (L1, L2_, L3):
        home.states[light] = state(light, "off", supported_color_modes=["color_temp"], min_color_temp_kelvin=2000, max_color_temp_kelvin=6500)
    seed(core, designer(truth={"matrices": [presence_truth(SPEC_TRUTH, "on_wins")]}, brightness=BRIGHT_40, mood=mood()))
    view = await core.blueprint_view(DEV)
    assert view["valid"] and view["automation"]["role"] == "group"          # one blueprint per light's automation
    assert any("exactly the automation this device deploys" in v["message"] for v in view["validation"])


def test_user_labels_never_become_templates():
    m = {"id": "m_0001", "name": "T {{ 1/0 }}", "columns": [col("c_0001", "always", label="{% raise %}")], "cells": {L1: {"c_0001": {"kind": "on"}}}}
    dev, x, cfg = expand(designer(truth={"matrices": [m]}))
    text = "".join(compiler.canonical(a["config"]) for a in compiler.compile_device(designers.effective(dev, x), cfg["settings"])["automations"])
    assert "1/0" in text and "{{ 1/0" not in text and "{% raise" not in text
