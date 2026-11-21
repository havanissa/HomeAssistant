"""Phase 0.2: the new rule parts, consolidation, blueprint export, import, recovery and rule diagnostics."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from ab import blueprint, compiler, conflicts, describe, diagnostics, ha, importer, model
from ab.model import ConfigError

import builders as B
from conftest import ADMIN, make_core, state

LAMP = "light.bed_lamp"
MOTION = "binary_sensor.motion"
OCC = "binary_sensor.sod_kamar_occupied"
S = B.settings()


def problems(build) -> str:
    with pytest.raises(ConfigError) as err:
        build()
    return " | ".join(p["message"] for p in err.value.problems)


def rule(*actions, triggers=None, conditions=None, number=1, **extra):
    return B.rule(number, f"R{number}", triggers or [B.state(MOTION, to="on")], list(actions), conditions, **extra)


def lamp(*rules, **extra):
    return B.device(1, "Lamp", [LAMP], list(rules), **extra)


# ------------------------------------------------------------------- model
def test_rules_of_phase_01_keep_their_document_and_their_automation():
    """An upgrade must not make a deployed device look changed: the same rules compile to the same automation."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Motion", [B.state(MOTION, to="on", for_seconds=5)], [B.call("light.turn_on", brightness_pct=70)],
               B.all_of(B.c_numeric("sensor.lux", below=100), B.any_of(B.c_state("input_select.mood", "Hygge", negate=True),
                                                                       B.c_time("22:00", "06:00", ["mon"])))),
        B.rule(2, "Occupied", [B.occupancy(OCC)], [B.call("light.turn_on")], recheck={"first_seconds": 3}),
        B.rule(3, "Bedtime", [B.at("23:00", ["mon", "tue"]), B.sun("sunset", -30)], [B.call("light.turn_off")]),
        B.rule(4, "Stairs", [B.state(MOTION, to="on")], [B.call("light.turn_on"), B.delay(90), B.call("light.turn_off")],
               retrigger="single"),
    ])
    assert "imported_from" not in device["rules"][0] and "blueprint" not in device
    shared, own = compiler.compile_device(device, S)["automations"]
    # hashes of the automations Automation Builder 0.1.0 generates for these rules (compared with git 31cad38)
    assert (shared["hash"], own["hash"]) == ("6458b80e01d6f561ec06", "d1d4143113482168bce9")


@pytest.mark.parametrize(("trigger", "message"), [
    (B.button("sensor.remote", "single"), "event entity"),
    (B.button("event.remote", 5), "event types"),
    ({"type": "device", "config": {"device_id": "abc"}}, "Home Assistant offers for the device"),
    (B.event("zha event"), "event type"),
    (B.mqtt(""), "MQTT topic"),
    (B.template("no template"), "A template contains"),
    (B.pattern(), "at least one of hours"),
    (B.pattern(minutes="every 5"), "Use a number"),
    ({"type": "homeassistant", "event": "restart"}, "starts"),
    (B.state(MOTION, to="on") | {"attribute": "bad{attr}"}, "name of an attribute"),
])
def test_invalid_new_triggers(trigger, message):
    assert message in problems(lambda: lamp(rule(B.call("light.turn_on"), triggers=[trigger])))


@pytest.mark.parametrize(("action", "message"), [
    (B.if_(B.all_of(), [B.call("light.turn_on")]), "at least one condition"),
    (B.if_(B.all_of(B.c_state(LAMP, "on")), []), "at least one action"),
    (B.choose(), "at least one branch"),
    (B.wait_trigger(), "what the rule waits for"),
    (B.wait_trigger(B.occupancy(OCC)), "Unsupported trigger type"),
    (B.wait_trigger(B.button("event.remote", "single")), "any event of the button"),
    (B.wait_condition(B.all_of(B.c_time("10:00"))), "A wait can watch"),
    (B.wait_condition(B.all_of(B.c_state(LAMP, "on", for_seconds=5))), "how long a state has lasted"),
    (B.wait_condition(B.all_of(B.c_template("{% if 1 %}{{ true }}{% endif %}"), B.c_state(LAMP, "on"))), "one {{ … }}"),
    (B.repeat([B.call("light.toggle")], count=0), "between 1 and"),
    (B.repeat([B.call("light.toggle")], until=B.all_of(B.c_state(LAMP, "on"))), "spins without pause"),
    (B.variables(), "at least one variable"),
    (B.variables(trigger=1), "set by Home Assistant itself"),
    (B.variables(**{"Bad Name": 1}), "not a variable name"),
    (B.notify("light.turn_on", "x"), "a notify action"),
    (B.notify("notify.send_message", "x"), "Choose an entity"),
    (B.notify("notify.phone", ""), "Enter the message"),
    (B.script("light.x"), "a script"),
    (B.scene("script.x"), "a scene"),
    (B.if_(B.all_of(B.c_trigger(3)), [B.call("light.turn_on")]), "which of the rule's triggers"),
])
def test_invalid_new_actions(action, message):
    assert message in problems(lambda: lamp(rule(action, B.call("light.turn_off"))))


def test_nesting_and_size_limits():
    deep = B.call("light.turn_on")
    for _ in range(model.MAX_ACTION_DEPTH):
        deep = B.if_(B.all_of(B.c_state(LAMP, "on")), [deep])
    assert "nested too deeply" in problems(lambda: lamp(rule(deep)))
    many = [B.repeat([B.call("light.toggle")] * 20, count=2)] * 4
    assert f"At most {model.MAX_ACTIONS_TOTAL} actions per rule" in problems(lambda: lamp(rule(*many)))


def test_summaries_of_advanced_rules():
    r = rule(B.choose((B.all_of(B.c_trigger(0)), [B.call("light.turn_on", brightness_pct=40)]),
                      default=[B.wait_trigger(B.state(MOTION, to="off"), timeout=60, on_timeout="stop"), B.call("light.turn_off")]),
             B.notify("notify.phone", "Done"), B.repeat([B.call("light.toggle")], count=3),
             triggers=[B.button("event.remote", "single"), B.event("zha_event", command="on")],
             conditions=B.all_of(B.none_of(B.c_state("input_boolean.manual", "on")), B.c_sun(after="sunset", after_offset=-30)))
    text = describe.rule(lamp(r)["rules"][0], lambda e: e, [LAMP])
    assert text["if"] == "event.remote reports “single” OR the event zha_event happens (command: on)"
    assert text["and"] == "none of (input_boolean.manual is “on”) AND it is after sunset - 30 min"
    assert text["then"] == ("(when started by: event.remote reports “single”: turn light.bed_lamp on (brightness 40 %); "
                            "otherwise: wait until binary_sensor.motion changes to “off” (at most 1 min, then stop), then "
                            "turn light.bed_lamp off), then notify notify.phone: “Done”, then repeat 3 times (toggle light.bed_lamp)")


# ---------------------------------------------------------------- compiler
def test_waiting_rules_are_separated_and_the_shared_mode_never_changes():
    waiting = {
        "delay": [B.call("light.turn_on"), B.delay(5), B.call("light.turn_off")],
        "wait for trigger": [B.wait_trigger(B.state(MOTION, to="off")), B.call("light.turn_off")],
        "wait for condition": [B.wait_condition(B.all_of(B.c_state(MOTION, "off"))), B.call("light.turn_off")],
        "open repeat": [B.repeat([B.call("light.toggle"), B.delay(1), B.call("light.toggle")], while_=B.all_of(B.c_state(MOTION, "on")))],
        "waited script": [B.script("script.long", wait=True)],
        "wait in a branch": [B.if_(B.all_of(B.c_state(LAMP, "on")), [B.delay(3), B.call("light.turn_off")])],
    }
    for name, actions in waiting.items():
        for mode in model.RETRIGGER_MODES:
            device = lamp(rule(B.call("light.turn_on"), number=1), rule(*actions, number=2, retrigger=mode))
            result = compiler.compile_device(device, S)
            assert [(a["role"], a["mode"]) for a in result["automations"]] == [("shared", "queued"), ("separate", mode)], name
            assert f"its own automation because it waits" in result["automations"][1]["reason"]
    instant = [B.repeat([B.call("light.toggle")], count=4), B.script("script.x"), B.notify("notify.phone", "hi"),
               B.choose((B.all_of(B.c_state(LAMP, "on")), [B.call("light.turn_off")])), B.variables(a=1), B.scene("scene.x")]
    result = compiler.compile_device(lamp(rule(*instant, retrigger="parallel"), rule(B.call("light.turn_on"), number=2)), S)
    assert [(a["role"], a["mode"], len(a["rule_ids"])) for a in result["automations"]] == [("shared", "queued", 2)]
    assert [b["kind"] for b in result["automations"][0]["blocks"]] == ["rule", "rule"]


def test_native_output_of_the_new_parts():
    r = rule(
        B.variables(level=40),
        B.wait_condition(B.all_of(B.c_template("{{ is_state('sun.sun', 'below_horizon') }}")), timeout=90, on_timeout="continue"),
        B.script("script.scene_prep", wait=True, room="kamar"), B.script("script.other"), B.scene("scene.evening"),
        B.notify("notify.send_message", "Hi", entity_id="notify.tv"),
        {"type": "device", "label": "x", "config": {"device_id": "d1", "domain": "light", "type": "turn_on", "entity_id": "e1"}},
        B.call("light.turn_on"),
        triggers=[B.mqtt("zigbee2mqtt/remote/action", "single"), B.pattern(minutes="/15"), {"type": "homeassistant", "event": "start"},
                  B.at("input_datetime.wake"), B.device_trigger("Press", device_id="d9", domain="zha", type="remote_button_short_press", subtype="turn_on")],
        conditions=B.all_of(B.c_sun(after="sunset", before="sunrise", after_offset=-30), B.c_state("sun.sun", "5") | {"attribute": "elevation"}))
    config = compiler.compile_device(lamp(r), S)["automations"][0]["config"]
    assert [{k: v for k, v in t.items() if k != "id"} for t in config["triggers"]] == [
        {"trigger": "mqtt", "topic": "zigbee2mqtt/remote/action", "payload": "single"},
        {"trigger": "time_pattern", "minutes": "/15"}, {"trigger": "homeassistant", "event": "start"},
        {"trigger": "time", "at": "input_datetime.wake"},
        {"trigger": "device", "device_id": "d9", "domain": "zha", "subtype": "turn_on", "type": "remote_button_short_press"}]
    assert config["conditions"] == [] and "trace" not in config  # a user's time pattern is not an occupancy check
    block = config["actions"][0]
    assert block["if"][1:] == [
        {"condition": "sun", "after": "sunset", "after_offset": "-00:30:00", "before": "sunrise"},
        {"condition": "state", "entity_id": "sun.sun", "attribute": "elevation", "state": "5"}]
    assert block["then"] == [
        {"variables": {"level": 40}},
        {"wait_template": "{{ is_state('sun.sun', 'below_horizon') }}", "timeout": {"hours": 0, "minutes": 1, "seconds": 30},
         "continue_on_timeout": True},
        {"action": "script.scene_prep", "data": {"room": "kamar"}},
        {"action": "script.turn_on", "target": {"entity_id": ["script.other"]}},
        {"action": "scene.turn_on", "target": {"entity_id": ["scene.evening"]}},
        {"action": "notify.send_message", "target": {"entity_id": ["notify.tv"]}, "data": {"message": "Hi"}},
        {"device_id": "d1", "domain": "light", "entity_id": "e1", "type": "turn_on"},
        {"action": "light.turn_on", "target": {"entity_id": [LAMP]}}]


def test_advanced_occupancy_rule_gets_no_rechecks_and_says_so():
    device = lamp(rule(B.if_(B.all_of(B.c_numeric("sensor.lux", below=50)), [B.call("light.turn_on")]), triggers=[B.occupancy(OCC)]))
    result = compiler.compile_device(device, S)
    assert [n["code"] for n in result["notes"]] == ["recheck_off_advanced"]
    assert all(not t["id"].startswith(("recheck", "periodic")) for t in result["automations"][0]["config"]["triggers"])


def test_conflicts_with_branches_and_late_actions():
    branch = rule(B.if_(B.all_of(B.c_state("input_boolean.manual", "on")), [B.call("light.turn_off")]), number=1)
    plain = rule(B.call("light.turn_on"), number=2)
    found = conflicts.analyse(lamp(branch, plain))
    assert [(c["code"], c["level"]) for c in found] == [("possible_overlap", "warning")]
    waiting = rule(B.call("light.turn_on"), B.delay(60), B.call("light.turn_off"), number=1)
    other = rule(B.call("light.turn_on", brightness_pct=5), number=2, triggers=[B.state("binary_sensor.door", to="on")])
    found = conflicts.analyse(lamp(waiting, other))
    assert [(c["code"], c["level"]) for c in found] == [("late_action", "warning")]
    assert conflicts.blocking(found) == []


# ---------------------------------------------------------------- blueprint
def blueprint_device(**changes):
    r1 = B.rule(1, changes.get("name", "Motion"), [B.state(MOTION, to="on", for_seconds=5)],
                [B.call("light.turn_on", brightness_pct=changes.get("brightness", 70), color_temp_kelvin=2700)],
                B.all_of(B.c_numeric("sensor.lux", below=100)))
    r2 = B.rule(2, "Later", [B.at("23:00")], [B.call("light.turn_on", brightness_pct="{{ 10 if is_state('light.bed_lamp', 'on') else 20 }}"),
                                               B.delay(30), B.call("light.turn_off")])
    return lamp(r1, r2)


def test_blueprint_inputs_are_stable_and_complete():
    first = blueprint.candidates(blueprint_device(), S)
    keys = {i["key"]: i for i in first["inputs"]}
    assert set(keys) == {"controlled_bed_lamp", "binary_sensor_motion", "sensor_lux", "r_00000001_c1_below",
                         "r_00000001_t1_for", "r_00000001_a1_brightness_pct", "r_00000001_a1_color_temp_kelvin",
                         "r_00000001_t1_to"}
    assert keys["controlled_bed_lamp"]["selector"] == {"entity": {"domain": "light"}}
    assert keys["r_00000001_t1_for"]["default"] == {"hours": 0, "minutes": 0, "seconds": 5}
    assert keys["r_00000001_a1_color_temp_kelvin"]["selector"] == {"color_temp": {"unit": "kelvin", "min": 1500, "max": 9000}}
    assert [i["group"] for i in first["inputs"]][:3] == ["Controlled entities", "Trigger entities", "Sensor entities"]
    # renaming a rule and changing a value keeps every identifier
    again = blueprint.candidates(blueprint_device(name="Movement", brightness=35), S)
    assert [i["key"] for i in again["inputs"]] == [i["key"] for i in first["inputs"]]
    # the waiting rule has its own automation and its own candidates
    own = blueprint.candidates(blueprint_device(), S, "automation_builder_d_00000001_r_00000002")
    assert {i["key"] for i in own["inputs"]} == {"controlled_bed_lamp", "r_00000002_t1_at", "r_00000002_a2_delay"}


def test_blueprint_document_and_what_stays_fixed():
    device = {**blueprint_device(), "blueprint": {"name": "Hall light", "description": "For halls", "automation": None, "inputs": {
        "controlled_bed_lamp": {"name": "Lamp", "description": "The lamp to switch"}, "r_00000001_c1_below": {},
        "gone_input": {}}}}
    device = model.validate_device(device["id"], device, "d", model.Problems())
    built = blueprint.build(device, S)
    assert built["problems"] == []
    document = yaml.load(built["yaml"], Loader=yaml.BaseLoader)  # noqa: S506 - !input needs no constructor here
    assert document["blueprint"]["name"] == "Hall light" and document["blueprint"]["domain"] == "automation"
    assert document["blueprint"]["input"]["controlled_bed_lamp"] == {
        "name": "Lamp", "description": "The lamp to switch", "default": LAMP, "selector": {"entity": {"domain": "light"}}}
    assert "alias" not in document and "Managed by" not in built["yaml"] and "id:" not in built["yaml"].split("triggers:")[0]
    assert "- !input 'controlled_bed_lamp'" in built["yaml"] and "below: !input 'r_00000001_c1_below'" in built["yaml"]
    text = " ".join(built["warnings"])
    assert "“gone_input” no longer exists" in text and "holds one automation" in text
    # the original device and its automation are untouched by an export
    assert compiler.compile_device(device, S)["automations"][0]["config"] == built["config"]
    own = blueprint.build({**device, "blueprint": {**device["blueprint"], "automation": "automation_builder_d_00000001_r_00000002",
                                                   "inputs": {"controlled_bed_lamp": {}}}}, S)
    assert any("names light.bed_lamp directly" in w for w in own["warnings"])


async def test_blueprint_screen_install_and_overwrite_rules(core, home):
    core.create_device({"name": "Lamp", "targets": [LAMP]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    core.save_rule(device_id, {"name": "Motion", "triggers": [B.state(MOTION, to="on")],
                               "actions": [B.call("light.turn_on", brightness_pct=70)]}, core.store.revision, ADMIN)
    view = await core.blueprint_view(device_id)
    assert view["valid"] and [i["key"] for i in view["inputs"]][:2] == ["controlled_bed_lamp", "binary_sensor_motion"]
    assert not any(i["exposed"] for i in view["inputs"]) and view["installed"] is None
    assert view["inputs"][0]["name"] == "Bed lamp"  # readable names from Home Assistant
    settings = {"name": "Motion lamp", "description": "", "inputs": {"controlled_bed_lamp": {}, "binary_sensor_motion": {}}}
    preview = await core.blueprint_view(device_id, settings)
    assert sum(i["exposed"] for i in preview["inputs"]) == 2 and "!input 'binary_sensor_motion'" in preview["yaml"]
    assert core.store.config["devices"][device_id].get("blueprint") is None  # a preview saves nothing
    before = dict(core.deploys.records)
    core.update_device(device_id, {"blueprint": settings}, core.store.revision, ADMIN)
    home.invalid = {"actions": "extra keys not allowed"}
    assert not (await core.blueprint_view(device_id))["valid"]
    with pytest.raises(ConfigError) as err:
        await core.blueprint_install(device_id, False, ADMIN)
    assert err.value.code == "blocked" and home.blueprints == {}
    home.invalid = {}
    result = await core.blueprint_install(device_id, False, ADMIN)
    path = f"automation_builder/{device_id}.yaml"
    assert result["path"] == f"blueprints/automation/{path}" and "blueprint:" in home.blueprints[path]
    assert (await core.blueprint_view(device_id))["installed"]["current"] is True
    with pytest.raises(ConfigError) as err:  # installing again replaces a file: only with a yes
        await core.blueprint_install(device_id, False, ADMIN)
    assert err.value.code == "exists" and err.value.extra["installed_by_app"] is True
    assert (await core.blueprint_install(device_id, True, ADMIN))["replaced"] is True
    assert core.deploys.records == before and home.file == {}  # nothing was deployed or changed by exporting
    assert home.commands_sent["blueprint/save"] == 2


async def test_blueprint_save_is_limited_to_the_apps_own_folder():
    for path in ("motion.yaml", "../automations.yaml", "automation_builder/../x.yaml", "automation_builder/x.yml",
                 "homeassistant/motion_light.yaml", "automation_builder/sub/x.yaml"):
        with pytest.raises(ha.NotAllowed):
            ha.check_params("blueprint/save", {"domain": "automation", "path": path, "yaml": ""})
    with pytest.raises(ha.NotAllowed):
        ha.check_params("blueprint/save", {"domain": "script", "path": "automation_builder/x.yaml", "yaml": ""})
    with pytest.raises(ha.NotAllowed):
        ha.check_params("trace/get", {"domain": "automation", "item_id": "1700000000001", "run_id": "x"})
    with pytest.raises(ha.NotAllowed):
        ha.check_params("trace/list", {"domain": "script", "item_id": "automation_builder_d_1"})
    ha.check_params("blueprint/save", {"domain": "automation", "path": "automation_builder/d_0a1b2c3d_r_00000001.yaml", "yaml": ""})
    assert not {"blueprint/delete", "blueprint/import", "call_service", "trace/debug/step"} & ha.ALLOWED_COMMANDS


# ------------------------------------------------------------------ import
SIMPLE = {"id": "1700000000009", "alias": "Bed lamp at motion", "mode": "single",
          "triggers": [{"trigger": "state", "entity_id": MOTION, "to": "on"}],
          "conditions": [{"condition": "numeric_state", "entity_id": "sensor.lux", "below": 80}],
          "actions": [{"action": "light.turn_on", "target": {"entity_id": LAMP}, "data": {"brightness_pct": 55}}]}


@pytest.mark.parametrize(("change", "why"), [
    ({"actions": [{"action": "light.turn_on", "target": {"area_id": "kamar"}}]}, "never a whole area"),
    ({"actions": [*SIMPLE["actions"], {"stop": "enough"}]}, "“stop” is not available"),
    ({"actions": [{"action": "light.turn_on", "target": {"entity_id": "{{ lamp }}"}}]}, "not a single entity"),
    ({"actions": [{"action": "light.turn_on", "target": {"entity_id": LAMP}, "continue_on_error": True}]}, "continue_on_error"),
    ({"actions": [{**SIMPLE["actions"][0], "enabled": False}]}, "disabled in the automation"),
    ({"actions": [{"action": "notify.phone", "data": {"message": "x"}}]}, "none of its actions names an entity"),
    ({"triggers": [{"trigger": "zone", "entity_id": "person.a", "zone": "zone.home"}]}, "trigger type “zone”"),
    ({"triggers": [{"trigger": "state", "entity_id": MOTION}]}, "every change, attribute changes included"),
    ({"triggers": [{"trigger": "state", "entity_id": MOTION, "to": "on", "variables": {"a": 1}}]}, "sets variables"),
    ({"triggers": [{"trigger": "numeric_state", "entity_id": "sensor.lux", "below": "input_number.limit"}]}, "another entity"),
    ({"conditions": [{"condition": "zone", "entity_id": "person.a", "zone": "zone.home"}]}, "condition type “zone”"),
    ({"conditions": [{"condition": "trigger", "id": "nope"}]}, "no trigger has"),
    ({"variables": {"x": 1}}, "automation-level setting"),
    ({"use_blueprint": {"path": "x.yaml"}}, "made from a blueprint"),
    ({"id": None}, "has no id"),
    ({"actions": [{"delay": "{{ 5 }}"}, SIMPLE["actions"][0]]}, "duration is a template"),
    ({"actions": [{"repeat": {"for_each": [1, 2], "sequence": SIMPLE["actions"]}}]}, "for_each"),
    ({"actions": [{"action": "switch.turn_on", "target": {"entity_id": LAMP}}]}, "not a switch entity"),
])
def test_unsupported_constructs_are_reported_never_dropped(change, why):
    config = {**SIMPLE, **change}
    result = importer.analyse(config, entity_id="automation.x", devices={}, settings=S,
                              state={"state": "on", "attributes": {"id": config.get("id")}})
    assert result["can_convert"] is False and result["rule"] is None
    assert any(why in u["why"] or why in u["what"] for u in result["unsupported"]), result["unsupported"]


async def test_import_is_a_copy_that_cannot_run_twice_by_accident(core, home):
    home.add_automation("automation.bed_lamp_at_motion", SIMPLE)
    home.add_automation("automation.complex", {**SIMPLE, "id": "2", "alias": "Complex", "actions": [{"stop": "x"}]})
    core.create_device({"name": "Lamp", "targets": [LAMP]}, core.store.revision, ADMIN)
    existing = next(iter(core.store.config["devices"]))
    core.save_rule(existing, {"name": "Same trigger", "triggers": [B.state(MOTION, to="on")],
                              "actions": [B.call("light.turn_off")]}, core.store.revision, ADMIN)
    listed = core.import_list()["automations"]
    assert [(a["entity_id"], a["imported"]) for a in listed] == [("automation.bed_lamp_at_motion", None), ("automation.complex", None)]

    analysis = await core.import_analyse("automation.bed_lamp_at_motion")
    assert analysis["can_convert"] and analysis["unsupported"] == []
    assert analysis["controlled"] == [{"entity_id": LAMP, "name": "Bed lamp"}]
    assert analysis["summary"]["text"] == ("IF Motion changes to “on” AND Lux is below 80 THEN turn Bed lamp on (brightness 55 %)")
    assert analysis["destinations"] == [{"device_id": existing, "name": "Lamp"}]
    assert [o["level"] for o in analysis["overlaps"]] == ["warning"] and "same trigger" in analysis["overlaps"][0]["message"]
    assert "triggers:" in analysis["original_yaml"] and "automation_builder_" in analysis["yaml"]
    refused = await core.import_analyse("automation.complex")
    assert not refused["can_convert"]
    with pytest.raises(ConfigError) as err:
        await core.import_convert("automation.complex", "new", core.store.revision, ADMIN)
    assert err.value.code == "blocked"

    revision = core.store.revision
    result = await core.import_convert("automation.bed_lamp_at_motion", "new", revision, ADMIN)
    device = core.store.config["devices"][result["device_id"]]
    assert result["device_id"] != existing and device["targets"] == [LAMP] and len(device["rules"]) == 1
    assert device["rules"][0]["imported_from"] == {"automation_id": SIMPLE["id"], "alias": "Bed lamp at motion", "allow_both": False}
    # nothing happened in Home Assistant: the original is as it was, nothing was deployed
    assert home.file == {} and home.configs["automation.bed_lamp_at_motion"] == SIMPLE
    assert home.states["automation.bed_lamp_at_motion"]["state"] == "on"
    assert core.import_list()["automations"][0]["imported"]["device_id"] == result["device_id"]
    with pytest.raises(ConfigError):  # a second copy is refused
        await core.import_convert("automation.bed_lamp_at_motion", "new", core.store.revision, ADMIN)

    # deploying the copy while the original still runs is refused, with the reason
    plan = await core.plan(result["device_id"])
    assert plan["can_deploy"] is False and "still turned on" in plan["reasons"][0] and len(plan["duplicates"]) == 1
    with pytest.raises(ConfigError) as err:
        await core.deploy(result["device_id"], core.store.revision, plan["plan_hash"], False, ADMIN)
    assert err.value.code == "blocked" and home.file == {}
    # ... the user turns the original off in Home Assistant: now it deploys
    home.states["automation.bed_lamp_at_motion"]["state"] = "off"
    plan = await core.plan(result["device_id"])
    assert plan["can_deploy"] is True and plan["duplicates"] == []
    # ... or allows both explicitly
    home.states["automation.bed_lamp_at_motion"]["state"] = "on"
    rule = {**device["rules"][0], "imported_from": {**device["rules"][0]["imported_from"], "allow_both": True}}
    core.save_rule(result["device_id"], rule, core.store.revision, ADMIN)
    plan = await core.plan(result["device_id"])
    assert plan["can_deploy"] is True
    await core.deploy(result["device_id"], core.store.revision, plan["plan_hash"], False, ADMIN)
    assert list(home.file) == [f"automation_builder_{result['device_id']}"]
    view = await core.device_view(result["device_id"])
    assert view["originals"][0]["running"] is True and view["originals"][0]["allow_both"] is True


async def test_import_into_an_existing_device_only_when_chosen(core, home):
    home.add_automation("automation.bed_lamp_at_motion", SIMPLE)
    core.create_device({"name": "Relay", "targets": ["switch.relay_1"]}, core.store.revision, ADMIN)
    core.create_device({"name": "Lamp and relay", "targets": [LAMP, "switch.relay_2"]}, core.store.revision, ADMIN)
    relay, both = sorted(core.store.config["devices"], key=lambda d: core.store.config["devices"][d]["name"] != "Relay")
    with pytest.raises(ConfigError):
        await core.import_convert("automation.bed_lamp_at_motion", relay, core.store.revision, ADMIN)
    result = await core.import_convert("automation.bed_lamp_at_motion", both, core.store.revision, ADMIN)
    assert result["device_id"] == both and len(core.store.config["devices"]) == 2
    added = core.store.config["devices"][both]["rules"][0]
    assert added["actions"][0]["target"] == [LAMP]  # only the entity the original controlled, not the whole device


# ---------------------------------------------------------------- recovery
async def deployed_device(core, home, *actions):
    core.create_device({"name": "Lamp", "targets": [LAMP]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    core.save_rule(device_id, {"name": "Motion", "triggers": [B.state(MOTION, to="on")],
                               "actions": list(actions) or [B.call("light.turn_on", brightness_pct=70)],
                               "conditions": B.all_of(B.c_numeric("sensor.lux", below=100))}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    return device_id, f"automation_builder_{device_id}"


async def test_interrupted_deployment_is_recognised_after_a_restart(tmp_path, home):
    core = make_core(tmp_path, home)
    device_id, automation_id = await deployed_device(core, home)
    assert core.deploys.pending is None and core.deploys.records[automation_id]["blocks"] == [
        {"rule_id": core.store.config["devices"][device_id]["rules"][0]["id"], "kind": "rule", "index": 0}]
    rule = core.store.config["devices"][device_id]["rules"][0]
    core.save_rule(device_id, {**rule, "actions": [B.call("light.turn_off")]}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)

    class Killed(BaseException):
        pass

    async def die(*_args):  # the App is stopped right after Home Assistant took the new automation
        raise Killed

    core._confirm_loaded = die  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    old_hash = core.deploys.records[automation_id]["hash"]
    restarted = make_core(tmp_path, home)
    assert restarted.deploys.pending is not None and restarted.deploys.records[automation_id]["hash"] == old_hash
    assert (await restarted.device_view(device_id))["live"]["status"] == "modified"  # before the recovery: unknown content
    result = await restarted.recover()
    assert result["adopted"] == [automation_id] and result["left"] == [] and restarted.deploys.pending is None
    view = await restarted.device_view(device_id)
    assert view["live"]["status"] == "in_sync" and "interrupted" in view["last_result"]["message"]
    assert home.requests["POST"] == 2  # the recovery itself wrote nothing

    # interrupted *before* the write reached Home Assistant: nothing is adopted, the change stays pending
    rule = restarted.store.config["devices"][device_id]["rules"][0]
    restarted.save_rule(device_id, {**rule, "actions": [B.call("light.toggle")]}, restarted.store.revision, ADMIN)
    plan = await restarted.plan(device_id)

    async def die_before(*_args):
        raise Killed

    home.put = die_before  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await restarted.deploy(device_id, restarted.store.revision, plan["plan_hash"], False, ADMIN)
    del home.put
    again = make_core(tmp_path, home)
    result = await again.recover()
    assert result["adopted"] == [] and result["left"] == [automation_id]
    assert (await again.device_view(device_id))["live"]["status"] == "changes_pending"


async def test_failed_deployment_leaves_no_pending_note(core, home):
    device_id, automation_id = await deployed_device(core, home)
    rule = core.store.config["devices"][device_id]["rules"][0]
    core.save_rule(device_id, {**rule, "actions": [B.call("light.turn_off")]}, core.store.revision, ADMIN)
    plan = await core.plan(device_id)
    home.reject = "Message malformed: extra keys not allowed"
    with pytest.raises(ConfigError) as err:
        await core.deploy(device_id, core.store.revision, plan["plan_hash"], False, ADMIN)
    assert err.value.code == "deploy_failed" and "Nothing was changed" in str(err.value) and core.deploys.pending is None
    home.reject, home.unreachable = None, True   # the endpoint cannot be reached at all
    with pytest.raises(ConfigError) as err:
        await core.plan(device_id)
    assert err.value.status == 502 and "could not be reached" in str(err.value)


# -------------------------------------------------------------- diagnostics
def trace(run_id: str, start: datetime, *, gate=True, conditions=(True,), calls=(), error=None, state="stopped",
          context="ctx-1", index=0) -> dict:
    base = f"action/{index}"
    stamp = start.isoformat()
    steps = {"trigger/0": [{"timestamp": stamp}], f"{base}/if/condition/0": [{"result": {"result": gate}, "timestamp": stamp}]}
    ok = gate
    for i, value in enumerate(conditions if gate else (), start=1):
        steps[f"{base}/if/condition/{i}"] = [{"result": {"result": value}, "timestamp": stamp}]
        ok = ok and value
        if not value:
            break
    if ok:
        for i, (action, entity, data) in enumerate(calls):
            domain, service = action.split(".")
            step = {"timestamp": stamp, "result": {"params": {"domain": domain, "service": service, "service_data": data,
                                                              "target": {"entity_id": [entity]}}, "running_script": False}}
            if error and i == len(calls) - 1:
                step["error"] = error
            steps[f"{base}/then/{i}"] = [step]
    return {"run_id": run_id, "state": state, "script_execution": "error" if error else "finished", "error": error,
            "timestamp": {"start": stamp, "finish": None if state == "running" else stamp}, "trigger": "state of binary_sensor.motion",
            "last_step": f"{base}/then/{len(calls) - 1}" if ok and calls else f"{base}/if/condition/0",
            "context": {"id": context}, "trace": steps}


async def test_rule_diagnostics_from_traces_and_action_verification(core, home):
    device_id, automation_id = await deployed_device(core, home)
    rule_id = core.store.config["devices"][device_id]["rules"][0]["id"]
    core.observer.settle = 0.0
    now = datetime.now(UTC)
    on = ("light.turn_on", LAMP, {"brightness_pct": 70})
    home.traces = {automation_id: [trace("run-1", now - timedelta(seconds=50), conditions=(False,))]}
    view = await core.device_view(device_id)
    d = view["rule_diagnostics"][rule_id]
    assert d["runs"] == 1 and d["executions"] == 0 and d["last_executed"] is None
    assert d["last_blocked"]["by"] == "Lux is below 100" and d["last_triggered"]["run_id"] == "run-1"
    assert d["sync"] == {"status": "in_sync", "text": "Deployed"} and d["entity_problems"] == 0
    assert {e["entity_id"]: e["uses"] for e in d["entities"]} == {
        MOTION: ["trigger"], LAMP: ["controlled"], "sensor.lux": ["condition"]}

    # executed: Home Assistant accepted the call, and the lamp reports on at 70 % with the run's context
    home.traces[automation_id].append(trace("run-2", now - timedelta(seconds=20), calls=[on], context="ctx-2"))
    home.states[LAMP] = {**state(LAMP, "on", brightness=178), "context": {"id": "ctx-2"}}
    await core.observer.refresh(automation_id)
    await asyncio.sleep(0.05)
    d = core.observer.view(rule_id)
    assert (d["runs"], d["executions"], d["blocks"]) == (2, 1, 1)
    assert d["last_outcome"]["outcome"] == "finished" and d["last_blocked"]["run_id"] == "run-1"
    assert d["verification"]["state"] == "confirmed" and d["verification"]["checks"][0]["reported"] == "“on”, brightness 178"

    # executed, but the lamp still reports off (a lost command): a mismatch is shown, nothing is commanded
    home.traces[automation_id].append(trace("run-3", now - timedelta(seconds=8), calls=[on], context="ctx-3"))
    home.states[LAMP] = {**state(LAMP, "off"), "context": {"id": "ctx-0"}}
    await core.observer.refresh(automation_id)
    await asyncio.sleep(0.05)
    check = core.observer.view(rule_id)["verification"]
    assert check["state"] == "mismatch" and check["checks"][0]["expected"] == "“on”, brightness 178"
    assert "accepted the action" in check["checks"][0]["detail"]
    # a failed action and an unverifiable one
    home.traces[automation_id].append(trace("run-4", now - timedelta(seconds=4), calls=[("light.toggle", LAMP, {})],
                                            error="Service light.toggle not found", context="ctx-4"))
    await core.observer.refresh(automation_id)
    await asyncio.sleep(0.05)
    d = core.observer.view(rule_id)
    assert d["last_outcome"]["outcome"] == "failed" and "not found" in d["last_outcome"]["error"]
    assert d["verification"]["state"] == "not_verifiable"
    # a run seen long after it happened is not judged; nothing is counted twice
    home.traces[automation_id].append(trace("run-5", now - timedelta(seconds=1), calls=[on], context="ctx-5"))
    home.traces[automation_id][-1]["timestamp"]["start"] = (now + timedelta(seconds=1)).isoformat()
    home.traces[automation_id][-1]["timestamp"]["finish"] = (now - timedelta(hours=1)).isoformat()
    for _ in range(3):
        await core.observer.refresh(automation_id)
    d = core.observer.view(rule_id)
    assert d["verification"]["state"] == "too_late" and (d["runs"], d["executions"]) == (5, 4)
    assert home.commands_sent["call_service"] == 0 and home.requests["POST"] == 1
    # the observations survive a restart of the App
    assert diagnostics.RuleStats(core.settings.data_dir).rules[rule_id]["runs"] == 5
    # the rule changes: the diagnostics say that Home Assistant still runs the old version
    rule = core.store.config["devices"][device_id]["rules"][0]
    core.save_rule(device_id, {**rule, "actions": [B.call("light.turn_off")]}, core.store.revision, ADMIN)
    view = await core.device_view(device_id)
    assert view["rule_diagnostics"][rule_id]["sync"]["status"] == "pending"
    home.states["sensor.lux"]["state"] = "unavailable"
    view = await core.device_view(device_id)
    assert view["rule_diagnostics"][rule_id]["entity_problems"] == 1


def test_verification_only_judges_what_can_be_judged():
    at = "2026-11-21T10:00:00+00:00"

    def check(action, data, entity_state, attributes=None, context="run", **extra):
        calls = [{"at": at, "action": action, "entities": ["x.y"], "data": data}]
        s = {"state": entity_state, "attributes": attributes or {}, "context": {"id": context},
             "last_updated": extra.get("updated", at)}
        return diagnostics.verify("x.y", diagnostics.expectations(calls)["x.y"], s, "run")["verdict"]

    assert check("light.turn_on", {}, "on") == "confirmed"
    assert check("light.turn_on", {}, "off") == "mismatch"
    assert check("light.turn_on", {}, "unavailable") == "unavailable"
    assert check("light.toggle", {}, "on") == "not_verifiable"
    assert check("notify.phone", {"message": "x"}, "unknown") == "not_verifiable"
    assert check("light.turn_on", {"brightness_pct": 30}, "on", {"brightness": 77}) == "confirmed"
    assert check("light.turn_on", {"brightness_pct": 30}, "on", {"brightness": 120}) == "mismatch"
    assert check("light.turn_on", {"color_temp_kelvin": 2000}, "on",
                 {"color_temp_kelvin": 2202, "min_color_temp_kelvin": 2202, "max_color_temp_kelvin": 6500}) == "confirmed"
    assert check("cover.set_cover_position", {"position": 40}, "open", {"current_position": 41}) == "confirmed"
    assert check("cover.set_cover_position", {"position": 40}, "open", {"current_position": 80}) == "mismatch"
    assert check("climate.set_temperature", {"temperature": 21.5}, "heat", {"temperature": 21.5}) == "confirmed"
    assert check("climate.set_hvac_mode", {"hvac_mode": "heat"}, "off") == "mismatch"
    assert check("input_number.set_value", {"value": 4}, "4.0") == "confirmed"
    assert check("scene.turn_on", {}, "2026-11-21T09:00:00+00:00") == "not_verifiable"
    # somebody else changed the entity after the action: not judged
    assert check("light.turn_on", {}, "off", context="someone", updated="2026-11-21T10:00:03+00:00") == "changed_since"
    assert check("light.turn_on", {}, "off", context="someone", updated="2026-11-21T09:59:00+00:00") == "mismatch"


async def test_device_triggers_come_from_home_assistant_only(core, home):
    home.entity_registry.append({"entity_id": "event.remote_action", "device_id": "dev-remote", "id": "reg-1"})
    home.devices.append({"id": "dev-remote", "name": "Hall remote", "area_id": "kamar", "manufacturer": "IKEA", "model": "E1743"})
    home.states["event.remote_action"] = state("event.remote_action", "unknown", friendly_name="Hall remote action",
                                               event_types=["on", "off", "brightness_move_up"])
    home.device_automations["trigger:dev-remote"] = [
        {"platform": "device", "domain": "zha", "device_id": "dev-remote", "type": "remote_button_short_press", "subtype": "turn_on",
         "metadata": {}},
        {"platform": "device", "domain": "zha", "device_id": "dev-remote", "type": "remote_button_long_press", "subtype": "dim_up",
         "metadata": {}},
        {"platform": "device", "domain": "event", "device_id": "dev-remote", "entity_id": "reg-1", "type": "changed_states",
         "metadata": {"secondary": True}}]
    found = await core.device_automations("dev-remote", "trigger")
    assert [(i["label"], i["secondary"]) for i in found["items"]] == [
        ("remote button long press dim up", False), ("remote button short press turn on", False),
        ("Hall remote action: changed states", True)]
    assert found["items"][1]["config"] == {"domain": "zha", "device_id": "dev-remote", "type": "remote_button_short_press",
                                           "subtype": "turn_on"}
    assert (await core.device_automations("dev-lamp", "trigger"))["items"] == []  # nothing is invented
    with pytest.raises(ConfigError):
        await core.device_automations("dev-remote", "service")
    index = core.index()
    assert [d["name"] for d in index.all_devices("remote")] == ["Hall remote"]
    assert [e["entity_id"] for e in index.search("", "button")] == ["event.remote_action"]
    assert index.view("event.remote_action", detail=True)["attributes"]["event_types"] == ["on", "off", "brightness_move_up"]
    assert core.notify_targets()["actions"] == ["notify.phone", "persistent_notification.create"]
    # a rule made with a discovered trigger is stored as Home Assistant gave it, and described readably
    core.create_device({"name": "Lamp", "targets": [LAMP]}, core.store.revision, ADMIN)
    device_id = next(iter(core.store.config["devices"]))
    preview = core.preview_rule(device_id, {"name": "Press", "triggers": [{"type": "device", **found["items"][1]}],
                                            "actions": [B.call("light.toggle")]})
    assert preview["valid"] and preview["summary"]["if"] == "remote button short press turn on"
    assert "trigger: device" in preview["yaml"] and "subtype: turn_on" in preview["yaml"]
