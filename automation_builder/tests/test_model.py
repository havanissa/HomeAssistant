"""The rule model: validation and canonical form."""

from __future__ import annotations

import pytest

from ab import model
import builders as B

LAMP = "light.lamp"


def problems(fn):
    with pytest.raises(model.ConfigError) as err:
        fn()
    return " | ".join(p["message"] for p in err.value.problems)


def test_canonical_form_is_stable():
    device = B.device(1, " Lamp ", [LAMP, LAMP], [B.rule(1, "A", [B.state("binary_sensor.m", to="on")],
                                                         [B.call("light.turn_on", transition=1, brightness_pct=50)])])
    assert device["name"] == "Lamp" and device["targets"] == [LAMP]
    assert list(device["rules"][0]["actions"][0]["data"]) == ["brightness_pct", "transition"]  # sorted
    config = model.validate_config({"devices": {device["id"]: device}})
    assert model.validate_config(config) == config
    assert config["settings"]["occupancy"] == model.OCCUPANCY_DEFAULTS


def test_a_rule_needs_a_trigger_and_an_action():
    text = problems(lambda: B.device(1, "Lamp", [LAMP], [B.rule(1, "A", [], [])]))
    assert "at least one trigger" in text and "at least one action" in text


def test_device_needs_explicit_targets():
    assert "Select at least one entity" in problems(lambda: B.device(1, "Lamp", [], []))
    assert "Choose an entity" in problems(lambda: B.device(1, "Lamp", ["Light.Lamp"], []))


@pytest.mark.parametrize(("trigger", "message"), [
    (B.state("nope"), "Choose an entity"),
    (B.state("light.x", to="on", frm="on"), "never happens"),
    (B.numeric("sensor.x"), "Give a threshold"),
    (B.numeric("sensor.x", above=10, below=5), "lower than"),
    (B.at("25:00"), "time of day"),
    (B.at("07:00", ["someday"]), "weekdays"),
    (B.sun("noon"), "sunrise or sunset"),
    (B.occupancy("sensor.x"), "binary sensor"),
    (B.occupancy("binary_sensor.x", "maybe"), "becomes occupied"),
    ({"type": "webhook"}, "Unsupported trigger type"),
])
def test_invalid_triggers(trigger, message):
    assert message in problems(lambda: B.device(1, "L", [LAMP], [B.rule(1, "A", [trigger], [B.call("light.turn_on")])]))


@pytest.mark.parametrize(("condition", "message"), [
    (B.c_state("light.x", None), "Choose a state"),
    (B.c_numeric("sensor.x"), "Give a threshold"),
    (B.c_time(), "time range"),
    ({"type": "zone", "value": "home"}, "Unsupported condition type"),
    (B.any_of(), "at least one condition"),
])
def test_invalid_conditions(condition, message):
    rule = B.rule(1, "A", [B.state("binary_sensor.m")], [B.call("light.turn_on")], B.all_of(condition))
    assert message in problems(lambda: B.device(1, "L", [LAMP], [rule]))


@pytest.mark.parametrize(("action", "message"), [
    (B.call("turn_on"), "Choose an action"),
    (B.call("light.turn_on", target=["light.other"]), "not one of this device's selected entities"),
    (B.call("switch.turn_on"), "cannot control light.lamp"),
    (B.call("light.turn_on", entity_id="light.all"), "Leave entity_id out"),
    (B.call("light.turn_on", rgb_color=[1, 2, 3], color_temp_kelvin=2700), "one way to set the colour"),
    (B.delay(0), "whole number of seconds"),
    ({"type": "stop"}, "Unsupported action type"),
])
def test_invalid_actions(action, message):
    assert message in problems(lambda: B.device(1, "L", [LAMP], [B.rule(1, "A", [B.state("binary_sensor.m")], [action])]))


def test_a_trailing_delay_is_refused():
    rule = B.rule(1, "A", [B.state("binary_sensor.m")], [B.call("light.turn_on"), B.delay(5)])
    assert "delay at the end" in problems(lambda: B.device(1, "L", [LAMP], [rule]))


def test_an_action_can_address_part_of_the_device():
    device = B.device(1, "Relay", ["switch.a", "switch.b", "light.c"], [
        B.rule(1, "A", [B.state("binary_sensor.m")], [B.call("switch.turn_on", target=["switch.a", "switch.b"]),
                                                       B.call("light.turn_on", target=["light.c"]),
                                                       B.call("notify.send", target="none", message="hi"),
                                                       B.call("homeassistant.turn_off")])])
    targets = [a["target"] for a in device["rules"][0]["actions"]]
    assert targets == [["switch.a", "switch.b"], ["light.c"], "none", "all"]


def test_state_values_are_kept_exactly():
    device = B.device(1, "L", [LAMP], [B.rule(1, "A", [B.state("input_select.mood", to="Góða Nátt ")],
                                              [B.call("light.turn_on")])])
    assert device["rules"][0]["triggers"][0]["to"] == "Góða Nátt "


def test_recheck_settings_and_overrides():
    settings = B.settings()
    rule = B.device(1, "L", [LAMP], [B.rule(1, "A", [B.occupancy("binary_sensor.o")], [B.call("light.turn_on")],
                                            recheck={"first_seconds": 4, "periodic_minutes": 0})])["rules"][0]
    timing = model.rule_recheck(rule, settings)
    assert (timing["first_seconds"], timing["second_seconds"], timing["periodic_minutes"]) == (4, 15, 0)
    assert "Choose one of" in problems(lambda: B.device(1, "L", [LAMP], [B.rule(
        1, "A", [B.occupancy("binary_sensor.o")], [B.call("light.turn_on")], recheck={"periodic_minutes": 7})]))
    p = model.Problems()
    model.validate_settings({"occupancy": {"first_seconds": 30, "second_seconds": 10}}, p)
    assert "after the first" in p.items[0]["message"]


def test_config_rejects_bad_ids_and_duplicates():
    device = B.device(1, "L", [LAMP], [B.rule(1, "A", [B.state("binary_sensor.m")], [B.call("light.turn_on")])])
    with pytest.raises(model.ConfigError) as err:
        model.validate_config({"devices": {"lamp": device}})
    assert "Invalid device id" in err.value.problems[0]["message"]
    with pytest.raises(model.ConfigError) as err:
        model.validate_config({"devices": {"d_00000001": device, "d_00000002": {**device, "id": "d_00000002"}}})
    assert "used twice" in err.value.problems[0]["message"]


def test_referenced_entities():
    device = B.device(1, "L", [LAMP], [
        B.rule(1, "A", [B.occupancy("binary_sensor.o")], [B.call("light.turn_on")], B.all_of(B.c_numeric("sensor.lux", below=5)),
               recheck={"override_entity": "input_boolean.manual"}),
        B.rule(2, "B", [B.state("binary_sensor.unused")], [B.call("light.turn_off")], enabled=False)])
    assert model.referenced_entities(device) == {
        LAMP: {"controlled"}, "binary_sensor.o": {"trigger"}, "sensor.lux": {"condition"},
        "input_boolean.manual": {"manual override signal"}}
