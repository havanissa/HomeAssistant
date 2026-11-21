"""Exported blueprints in a real Home Assistant: saved through its own blueprint command, instantiated with other
inputs than the original, and run by its automation engine - without anything of Automation Builder at run time."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeassistant.setup import async_setup_component

from ab import blueprint, compiler
import builders as B

LAMP = "light.lamp"
OTHER = "light.other"
MOTION = "binary_sensor.motion"
OTHER_MOTION = "binary_sensor.hall_motion"
LUX = "sensor.lux"
OCC = "binary_sensor.sod_kamar_occupied"
OTHER_OCC = "binary_sensor.sod_stova_occupied"


@pytest.fixture
def config_dir(hass, tmp_path):
    """Home Assistant's configuration folder for this test (blueprints are files)."""
    hass.config.config_dir = str(tmp_path)
    return tmp_path


def export(device, expose="all", **settings):
    found = blueprint.candidates(device, B.settings(), settings.get("automation"))
    keys = [i["key"] for i in found["inputs"]] if expose == "all" else list(expose)
    device = {**device, "blueprint": {"name": "Test blueprint", "description": "", "automation": settings.get("automation"),
                                      "inputs": {k: {"name": "", "description": ""} for k in keys}}}
    built = blueprint.build(device, B.settings())
    assert built["problems"] == [], built["problems"]
    return built


async def instantiate(house, hass, config_dir: Path, built, *instances: dict, ws=None):
    """Write the blueprint where Home Assistant expects it and create automations from it."""
    path = config_dir / "blueprints" / "automation" / built["path"]
    path.parent.mkdir(parents=True)
    path.write_text(built["yaml"], encoding="utf-8")
    for domain, services in house.SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, house._record)  # noqa: SLF001
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": f"from_blueprint_{i}", "alias": f"From blueprint {i}", "use_blueprint": {"path": built["path"], "input": inputs}}
        for i, inputs in enumerate(instances)]})
    await hass.async_block_till_done()
    states = hass.states.async_all("automation")
    assert len(states) == len(instances) and all(s.state == "on" for s in states), states


async def test_blueprint_is_accepted_by_home_assistant_and_runs_with_other_inputs(house, hass, config_dir, hass_ws_client):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Motion", [B.state(MOTION, to="on", for_seconds=5)],
               [B.call("light.turn_on", brightness_pct=70, color_temp_kelvin=2700)], B.all_of(B.c_numeric(LUX, below=100))),
        B.rule(2, "Late", [B.at("23:00")], [B.call("light.turn_off")]),
    ])
    built = export(device)
    keys = set(built["document"]["blueprint"]["input"])
    assert {"controlled_lamp", "binary_sensor_motion", "sensor_lux", "r_00000001_t1_for", "r_00000001_c1_below",
            "r_00000001_a1_brightness_pct", "r_00000001_a1_color_temp_kelvin", "r_00000002_t1_at"} <= keys
    for entity, value in ((LAMP, "off"), (OTHER, "off"), (MOTION, "off"), (OTHER_MOTION, "off"), (LUX, "250")):
        house.set(entity, value)
    # instance 0: every default (the original device); instance 1: another lamp, sensor, threshold, time, brightness
    await instantiate(house, hass, config_dir, built, {}, {
        "controlled_lamp": OTHER, "binary_sensor_motion": OTHER_MOTION, "r_00000001_c1_below": 300,
        "r_00000001_t1_for": {"seconds": 1}, "r_00000001_a1_brightness_pct": 25})

    house.set(MOTION, "on")
    await house.advance(6)
    assert house.taken() == []                      # the original threshold: 250 lx is too bright
    house.set(OTHER_MOTION, "on")
    await house.advance(2)
    assert house.taken() == [("light.turn_on", (OTHER,), {"brightness_pct": 25, "color_temp_kelvin": 2700})]
    house.set(LUX, "40")
    house.set(MOTION, "off")
    await house.settle()
    house.set(MOTION, "on")
    await house.advance(4)
    assert house.taken() == []                      # the original needs 5 s
    await house.advance(2)
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 70, "color_temp_kelvin": 2700})]

    # Home Assistant's own commands: the list shows it without an error, a save of the same text is accepted,
    # and a broken blueprint is refused.
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "blueprint/list", "domain": "automation"})
    listed = (await ws.receive_json())["result"]
    assert listed[built["path"]]["metadata"]["name"] == "Test blueprint"
    assert set(listed[built["path"]]["metadata"]["input"]) == keys
    await ws.send_json({"id": 2, "type": "blueprint/save", "domain": "automation", "path": "automation_builder/copy.yaml",
                        "yaml": built["yaml"]})
    assert (await ws.receive_json())["success"] is True
    await ws.send_json({"id": 3, "type": "blueprint/save", "domain": "automation", "path": "automation_builder/bad.yaml",
                        "yaml": built["yaml"].replace("domain: automation", "domain: script")})
    assert (await ws.receive_json())["success"] is False
    await ws.send_json({"id": 4, "type": "blueprint/substitute", "domain": "automation", "path": built["path"], "input": {}})
    substituted = (await ws.receive_json())["result"]["substituted_config"]
    original = compiler.compile_device(device, B.settings())["automations"][0]["config"]
    assert blueprint._by_content(substituted) == blueprint._by_content(  # noqa: SLF001
        {k: v for k, v in original.items() if k not in ("alias", "description")})


async def test_occupancy_blueprint_keeps_its_rechecks_for_other_entities(house, hass, config_dir):
    """The rechecks refer to the occupancy sensor and the lamp in templates: both follow the inputs."""
    device = B.device(1, "Lamp", [LAMP], [B.rule(1, "Occupied", [B.occupancy(OCC)], [B.call("light.turn_on")])])
    built = export(device)
    assert "ab_controlled_lamp" in built["document"]["variables"]
    for entity in (LAMP, OTHER):
        house.set(entity, "off")
    house.set(OCC, "off", fresh=True)
    house.set(OTHER_OCC, "off", fresh=True)
    await instantiate(house, hass, config_dir, built, {
        "controlled_lamp": OTHER, "binary_sensor_sod_kamar_occupied": OTHER_OCC})
    await house.advance(301)                        # past the start-up grace
    house.apply = False                             # the first command is lost
    house.set(OTHER_OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [("light.turn_on", (OTHER,), {})]
    house.apply = True
    await house.advance(2.5)                        # the 2-second recheck repeats it for the *other* lamp
    assert house.taken() == [("light.turn_on", (OTHER,), {})]
    await house.advance(20)
    assert house.taken() == []                      # the lamp is on: nothing more
    house.set(OCC, "on", fresh=True)                # the original sensor means nothing to this instance
    await house.advance(20)
    assert house.taken() == []


async def test_advanced_rule_as_blueprint(house, hass, config_dir):
    """A waiting rule with a branch: its own automation, exported with the wait timeout and the delay as inputs."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Plain", [B.state("binary_sensor.door", to="on")], [B.call("light.turn_on")]),
        B.rule(2, "Stairs", [B.state(MOTION, to="on")], [
            B.call("light.turn_on"),
            B.wait_condition(B.all_of(B.c_state(MOTION, "off")), timeout=60, on_timeout="stop"),
            B.delay(10),
            B.if_(B.all_of(B.c_numeric(LUX, above=5)), [B.call("light.turn_off")])], retrigger="restart"),
    ])
    shared = export(device)
    assert any("holds one automation" in w for w in shared["warnings"])
    own = export(device, automation="automation_builder_d_00000001_r_00000002")
    assert own["automation"]["mode"] == "restart" and own["path"] == "automation_builder/d_00000001_r_00000002.yaml"
    assert {"r_00000002_a2_timeout", "r_00000002_a3_delay", "r_00000002_a4_c2_above"} <= set(own["document"]["blueprint"]["input"])
    for entity, value in ((LAMP, "off"), (OTHER, "off"), (MOTION, "off"), (OTHER_MOTION, "off"), (LUX, "50")):
        house.set(entity, value)
    await instantiate(house, hass, config_dir, own, {
        "controlled_lamp": OTHER, "binary_sensor_motion": OTHER_MOTION, "r_00000002_a3_delay": {"seconds": 3},
        "r_00000002_a2_timeout": {"seconds": 20}})
    house.set(OTHER_MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (OTHER,), {})]
    house.set(OTHER_MOTION, "off")                  # the wait refers to the motion input through a variable
    await house.advance(2)
    assert house.taken() == []
    await house.advance(2)
    assert house.taken() == [("light.turn_off", (OTHER,), {})]
    house.set(OTHER_MOTION, "on")
    await house.advance(25)                         # nobody leaves within 20 s: the rule stops
    assert house.taken() == [("light.turn_on", (OTHER,), {})]
    await house.advance(20)
    assert house.taken() == []
