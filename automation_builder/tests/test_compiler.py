"""The compiler and the conflict check (Home Assistant itself runs the output in ../../tests/automation_builder)."""

from __future__ import annotations

import copy
import json

import yaml

from ab import blueprint, catalog, compiler, conflicts, describe
import builders as B

LAMP = "light.lamp"
MOTION = "binary_sensor.motion"
OCC = "binary_sensor.sod_kamar_occupied"
S = B.settings()


def five_rules():
    return B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Wall switch", [B.state("event.wall_switch")], [B.call("light.toggle")]),
        B.rule(2, "Motion", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=70)],
               B.all_of(B.c_numeric("sensor.lux", below=100))),
        B.rule(3, "Bedtime", [B.at("23:00")], [B.call("light.turn_off")]),
        B.rule(4, "Too warm", [B.numeric("sensor.temp", above=28)], [B.call("light.turn_off")]),
        B.rule(5, "Hygge", [B.state("input_select.mood", to="Hygge")],
               [B.call("light.turn_on", brightness_pct=30, color_temp_kelvin=2400)]),
    ])


def test_one_device_one_automation():
    result = compiler.compile_device(five_rules(), S)
    assert len(result["automations"]) == 1
    automation = result["automations"][0]
    assert automation["id"] == "automation_builder_d_00000001" and automation["mode"] == "queued"
    config = automation["config"]
    assert config["alias"] == "Lamp (Automation Builder)" and "Managed by the Automation Builder App" in config["description"]
    assert len(config["triggers"]) == 5 and len({t["id"] for t in config["triggers"]}) == 5
    assert [a["alias"] for a in config["actions"]] == ["Rule: Wall switch", "Rule: Motion", "Rule: Bedtime",
                                                       "Rule: Too warm", "Rule: Hygge"]
    # each rule is its own "if": started by its own trigger, and its own conditions
    motion = config["actions"][1]
    assert motion["if"][0] == {"condition": "trigger", "id": [config["triggers"][1]["id"]]}
    assert motion["if"][1] == {"condition": "numeric_state", "entity_id": "sensor.lux", "below": 100}
    assert motion["then"] == [{"action": "light.turn_on", "target": {"entity_id": [LAMP]}, "data": {"brightness_pct": 70}}]
    assert "choose" not in json.dumps(config)


def test_compilation_is_deterministic_and_idempotent():
    device = five_rules()
    first = compiler.compile_device(device, S)
    second = compiler.compile_device(copy.deepcopy(device), copy.deepcopy(S))
    assert first == second and json.dumps(first) == json.dumps(second)
    # the YAML round-trips to the same configuration (what Home Assistant returns after saving it)
    automation = first["automations"][0]
    loaded = yaml.safe_load(compiler.to_yaml(compiler.with_id(automation)))[0]
    assert compiler.config_hash(compiler.strip_id(loaded)) == automation["hash"]


def test_ids_are_stable_when_other_rules_change():
    device = five_rules()
    before = {t["id"] for t in compiler.compile_device(device, S)["automations"][0]["config"]["triggers"]}
    device["rules"] = [device["rules"][4], *device["rules"][:3]]  # reordered, one removed
    after = {t["id"] for t in compiler.compile_device(device, S)["automations"][0]["config"]["triggers"]}
    assert after < before


def test_identical_triggers_are_listed_once():
    device = B.device(1, "Lamps", [LAMP, "light.two"], [
        B.rule(1, "One", [B.state(MOTION, to="on")], [B.call("light.turn_on", target=[LAMP])]),
        B.rule(2, "Two", [B.state(MOTION, to="on"), B.at("07:00")], [B.call("light.turn_on", target=["light.two"])])])
    config = compiler.compile_device(device, S)["automations"][0]["config"]
    assert [t["trigger"] for t in config["triggers"]] == ["state", "time"]
    shared = config["triggers"][0]["id"]
    assert config["actions"][0]["if"][0]["id"] == [shared]
    assert config["actions"][1]["if"][0]["id"] == [shared, config["triggers"][1]["id"]]


def test_disabled_rules_and_devices_are_left_out():
    device = five_rules()
    device["rules"][0]["enabled"] = False
    config = compiler.compile_device(device, S)["automations"][0]["config"]
    assert len(config["triggers"]) == 4 and "Wall switch" not in config["description"]
    device["enabled"] = False
    assert compiler.compile_device(device, S) == {"automations": [], "notes": []}


def test_delay_rules_and_execution_modes():
    def build(mode):
        return B.device(1, "Lamp", [LAMP], [
            B.rule(1, "Stairs", [B.state(MOTION, to="on")], [B.call("light.turn_on"), B.delay(90), B.call("light.turn_off")],
                   retrigger=mode),
            B.rule(2, "Hygge", [B.state("input_select.mood", to="Hygge")], [B.call("light.turn_on")])])

    for mode in ("restart", "single", "queued"):
        result = compiler.compile_device(build(mode), S)
        shared, own = result["automations"]
        assert (shared["role"], shared["mode"], shared["rule_ids"]) == ("shared", "queued", ["r_00000002"])
        assert (own["role"], own["mode"], own["id"]) == ("separate", mode, "automation_builder_d_00000001_r_00000001")
        assert "its own automation because" in own["reason"]
        assert own["config"]["actions"][0]["then"][1] == {"delay": {"hours": 0, "minutes": 1, "seconds": 30}}
        assert {n["code"] for n in result["notes"]} == {"delay_restart", "separate_automation"}
    result = compiler.compile_device(build("parallel"), S)
    assert [(a["role"], a["mode"]) for a in result["automations"]] == [("shared", "parallel")]


def test_occupancy_rechecks_are_generated_only_for_occupancy_rules():
    plain = compiler.compile_device(five_rules(), S)["automations"][0]["config"]
    assert all(t["trigger"] != "time_pattern" and "for" not in t for t in plain["triggers"])
    assert plain["conditions"] == [] and "trace" not in plain

    device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Occupied", [B.occupancy(OCC)], [B.call("light.turn_on")],
                                                 B.all_of(B.c_numeric("sensor.lux", below=100)))])
    config = compiler.compile_device(device, S)["automations"][0]["config"]
    kinds = [t["id"].split("_")[0] for t in config["triggers"]]
    assert kinds == ["occupancy", "recheck", "recheck", "periodic"]
    assert config["triggers"][0] == {"trigger": "state", "entity_id": OCC, "from": "off", "to": "on",
                                     "id": config["triggers"][0]["id"]}
    assert config["triggers"][3]["minutes"] == "/5"
    rule, recheck = config["actions"]
    assert recheck["alias"] == "Recheck: Occupied" and recheck["then"] == rule["then"]
    checks = recheck["if"]
    assert checks[0]["id"] == [t["id"] for t in config["triggers"][1:]]
    assert checks[1] == {"condition": "state", "entity_id": OCC, "state": "on"}      # current occupancy
    assert "fresh" in checks[2]["value_template"]                                   # not stale
    assert "timedelta(seconds=300)" in checks[3]["value_template"]                   # start-up grace
    assert checks[4] == {"condition": "numeric_state", "entity_id": "sensor.lux", "below": 100}  # current conditions
    wrong = checks[5]
    assert wrong["conditions"][0] == {"condition": "not", "conditions": [
        {"condition": "state", "entity_id": LAMP, "state": ["on", "unavailable", "unknown"]}]}
    assert f"states['{LAMP}'].last_changed <= states['{OCC}'].last_changed" in wrong["conditions"][1]["value_template"]
    assert config["trace"] == {"stored_traces": 20}
    assert config["conditions"][0]["conditions"][1] == {"condition": "state", "entity_id": OCC, "state": "on"}
    text = json.dumps(config)
    assert "turn_off" not in text and "delay" not in text and "wait" not in text  # nothing inferred, nothing waits


def test_no_rechecks_for_actions_that_cannot_be_repeated_safely():
    cases = {
        "non_idempotent": [B.call("light.toggle")],
        "delay": [B.call("light.turn_on"), B.delay(5), B.call("light.turn_off")],
        "unknown": [B.call("script.do_things", target="none")],
        "unverifiable": [B.call("light.turn_on", target=[LAMP]), B.call("light.turn_off", target=[LAMP])],
    }
    for why, actions in cases.items():
        device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Occ", [B.occupancy(OCC)], actions, retrigger="parallel")])
        result = compiler.compile_device(device, S)
        config = result["automations"][0]["config"]
        assert [t["id"].split("_")[0] for t in config["triggers"]] == ["occupancy"], why
        codes = {n["code"] for n in result["notes"]}
        assert any(c.startswith("recheck_off_") for c in codes), (why, codes)
    off = B.device(1, "Lamp", [LAMP], [B.rule(1, "Occ", [B.occupancy(OCC)], [B.call("light.turn_on")],
                                              recheck={"enabled": False})])
    result = compiler.compile_device(off, S)
    assert len(result["automations"][0]["config"]["triggers"]) == 1 and result["notes"][0]["code"] == "recheck_disabled"


def test_effects():
    assert catalog.effect("light.turn_on")["states"] == ["on"]
    assert catalog.effect("cover.close_cover")["states"] == ["closed", "closing"]
    assert catalog.effect("climate.set_hvac_mode", {"hvac_mode": "heat"})["states"] == ["heat"]
    assert catalog.effect("input_select.select_option", {"option": "Hygge"})["states"] == ["Hygge"]
    assert catalog.effect("climate.set_temperature", {"temperature": 20})["kind"] == "idempotent"
    for action in ("light.toggle", "button.press", "cover.toggle", "vacuum.start"):
        assert catalog.effect(action)["kind"] == "non_idempotent", action
    assert catalog.effect("notify.send")["kind"] == "unknown"
    assert set(catalog.CONTROLLABLE_DOMAINS) >= {"light", "switch", "fan", "cover", "climate", "lock", "valve"}


# ---------------------------------------------------------------- conflicts
def codes(device):
    return [(c["code"], c["level"], c["acknowledged"]) for c in conflicts.analyse(device)]


def test_contradictory_rules_are_blocking_until_confirmed():
    rules = [B.rule(1, "On", [B.state(MOTION, to="on")], [B.call("light.turn_on")]),
             B.rule(2, "Off", [B.state(MOTION, to="on")], [B.call("light.turn_off")])]
    device = B.device(1, "Lamp", [LAMP], rules)
    found = conflicts.analyse(device)
    assert codes(device) == [("contradiction", "blocking", False)]
    assert "“Off” has the last word" in found[0]["message"] and conflicts.blocking(found)
    confirmed = B.device(1, "Lamp", [LAMP], rules, acknowledged=[found[0]["key"]])
    assert codes(confirmed) == [("contradiction", "blocking", True)] and not conflicts.blocking(conflicts.analyse(confirmed))
    swapped = B.device(1, "Lamp", [LAMP], rules[::-1], acknowledged=[found[0]["key"]])
    assert codes(swapped) == [("contradiction", "blocking", False)]  # the confirmation was for the other order


def test_rules_that_cannot_run_together_do_not_conflict():
    on = [B.call("light.turn_on")]
    off = [B.call("light.turn_off")]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "On", [B.state(MOTION, to="on")], on),
                                           B.rule(2, "Off", [B.state(MOTION, to="off")], off)])) == []
    assert codes(B.device(1, "L", [LAMP], [
        B.rule(1, "Day", [B.state(MOTION, to="on")], on, B.all_of(B.c_state("input_select.mood", "Dagur"))),
        B.rule(2, "Night", [B.state(MOTION, to="on")], off, B.all_of(B.c_state("input_select.mood", "Nátt")))])) == []
    assert codes(B.device(1, "L", [LAMP], [
        B.rule(1, "Dark", [B.state(MOTION, to="on")], on, B.all_of(B.c_numeric("sensor.lux", below=50))),
        B.rule(2, "Bright", [B.state(MOTION, to="on")], off, B.all_of(B.c_numeric("sensor.lux", above=50)))])) == []
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "Occ", [B.occupancy(OCC)], on),
                                           B.rule(2, "Vac", [B.occupancy(OCC, "vacant")], off)])) == []
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", [B.at("07:00", ["mon"])], on),
                                           B.rule(2, "B", [B.at("07:00", ["sat"])], off)])) == []


def test_other_conflicts():
    trigger = [B.state(MOTION, to="on")]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.toggle")]),
                                           B.rule(2, "B", trigger, [B.call("light.toggle")],
                                                  B.all_of(B.c_numeric("sensor.lux", below=5)))])) == [("double_toggle", "blocking", False)]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.toggle")]),
                                           B.rule(2, "B", [B.state(MOTION)], [B.call("light.turn_on")])])) == [("toggle_overlap", "blocking", False)]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.turn_on", brightness_pct=70)]),
                                           B.rule(2, "B", trigger, [B.call("light.turn_on", brightness_pct=20)])])) == [("competing", "blocking", False)]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.turn_on")]),
                                           B.rule(2, "B", trigger, [B.call("light.turn_on")])])) == [("duplicate_rule", "blocking", False)]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.turn_on")]),
                                           B.rule(2, "B", trigger, [B.call("light.turn_on")],
                                                  B.all_of(B.c_numeric("sensor.lux", below=5)))])) == [("repeated_action", "warning", False)]
    assert codes(B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.turn_on"), B.call("light.turn_off")])])) == [("self_contradiction", "warning", False)]
    disabled = B.device(1, "L", [LAMP], [B.rule(1, "A", trigger, [B.call("light.turn_on")]),
                                         B.rule(2, "B", trigger, [B.call("light.turn_off")], enabled=False)])
    assert codes(disabled) == []


# ------------------------------------------------------- summaries, inputs
def test_readable_summary():
    names = {LAMP: "Bed lamp", MOTION: "Motion", "sensor.lux": "Lux", OCC: "Kamar"}.get
    name = lambda e: names(e) or e  # noqa: E731
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "A", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=70)],
               B.all_of(B.c_numeric("sensor.lux", below=100))),
        B.rule(2, "B", [B.at("07:00", ["mon", "tue", "wed", "thu", "fri"]), B.sun("sunset", -30)],
               [B.call("light.turn_on"), B.delay(90), B.call("light.turn_off")],
               B.any_of(B.c_state("input_select.mood", "Hygge"), B.c_time("22:00", "06:00"))),
        B.rule(3, "C", [B.occupancy(OCC)], [B.call("light.toggle")])])
    texts = [describe.rule(r, name, device["targets"])["text"] for r in device["rules"]]
    assert texts[0] == "IF Motion changes to “on” AND Lux is below 100 THEN turn Bed lamp on (brightness 70 %)"
    assert texts[1] == ("IF the time is 07:00 on weekdays OR it is 30 min before sunset AND input_select.mood is “Hygge” "
                        "OR the time is between 22:00 and 06:00 THEN turn Bed lamp on, then wait 1 min 30 s, then turn Bed lamp off")
    assert texts[2] == "IF Kamar becomes occupied THEN toggle Bed lamp"


def test_blueprint_inputs_cover_the_reusable_values():
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "A", [B.numeric("sensor.lux", below=100, for_seconds=30)], [B.call("light.turn_on", brightness_pct=70),
                                                                              B.delay(60), B.call("light.turn_off")],
               B.all_of(B.c_state("input_select.mood", "Hygge")))])
    kinds = {(i["kind"], i["value"]) for i in blueprint.inputs_of(device)}
    assert kinds == {("entity", LAMP), ("entity", "sensor.lux"), ("number", 100), ("duration", 30), ("action_data", 70),
                     ("duration", 60), ("entity", "input_select.mood"), ("state", "Hygge")}
