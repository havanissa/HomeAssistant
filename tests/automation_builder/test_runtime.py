"""The generated automations in Home Assistant's own parser and automation engine (Home Assistant 2026.11.1)."""

from __future__ import annotations

from datetime import datetime

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.util import dt as dt_util

import builders as B

LAMP = "light.lamp"
LAMP2 = "light.lamp_2"
MOTION = "binary_sensor.motion"
SWITCH = "event.wall_switch"
LUX = "sensor.lux"
TEMP = "sensor.temperature"
MOOD = "input_select.mood"
OCC = "binary_sensor.sod_kamar_occupied"
ON = ("light.turn_on", (LAMP,), {})
OFF = ("light.turn_off", (LAMP,), {})


async def test_basic_if_then(house):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Motion", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=70)],
               B.all_of(B.c_numeric(LUX, below=100)))])
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    house.set(LUX, "250")
    automations = await house.load(device)
    assert len(automations) == 1
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == []  # too bright
    house.set(MOTION, "off")
    house.set(LUX, "40")
    await house.settle()
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 70})]


async def test_five_rules_in_one_automation(house, hass):
    """A wall switch, a motion sensor, a schedule, a temperature threshold and a mood: one automation, and each
    trigger runs exactly its own rule."""
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Wall switch", [B.state(SWITCH)], [B.call("light.toggle")]),
        B.rule(2, "Motion", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=70)]),
        B.rule(3, "Bedtime", [B.at("23:00")], [B.call("light.turn_off")]),
        B.rule(4, "Too warm", [B.numeric(TEMP, above=28)], [B.call("light.turn_off")]),
        B.rule(5, "Hygge", [B.state(MOOD, to="Hygge")],
               [B.call("light.turn_on", brightness_pct=30, color_temp_kelvin=2400)]),
    ])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (SWITCH, "2026-11-21T10:00:00+00:00"), (TEMP, "21"),
                          (MOOD, "Dagur")):
        house.set(entity, value)
    automations = await house.load(device)
    assert len(automations) == 1 and automations[0]["mode"] == "queued"
    assert len(automations[0]["config"]["triggers"]) == 5
    assert len(hass.states.async_all("automation")) == 1

    house.set(SWITCH, "2026-11-21T10:00:05+00:00")
    await house.settle()
    assert house.taken() == [("light.toggle", (LAMP,), {})]
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 70})]
    house.set(TEMP, "29")
    await house.settle()
    assert house.taken() == [OFF]
    house.set(TEMP, "30")  # still above: a threshold is crossed once
    await house.settle()
    assert house.taken() == []
    house.set(MOOD, "Hygge")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 30, "color_temp_kelvin": 2400})]
    house.freezer.move_to(dt_util.now().replace(hour=23, minute=0, second=0, microsecond=0))
    async_fire_time_changed(hass, dt_util.utcnow())
    await house.settle()
    assert house.taken() == [OFF]


async def test_shared_trigger_is_listed_once_and_runs_each_rule_once(house):
    device = B.device(1, "Lamps", [LAMP, LAMP2], [
        B.rule(1, "Lamp one", [B.state(MOTION, to="on")], [B.call("light.turn_on", target=[LAMP])]),
        B.rule(2, "Lamp two", [B.state(MOTION, to="on")], [B.call("light.turn_on", target=[LAMP2], brightness_pct=20)],
               B.all_of(B.c_state(MOOD, "Hygge"))),
        B.rule(3, "Off", [B.state(MOTION, to="off", for_seconds=60)], [B.call("light.turn_off")]),
    ])
    for entity, value in ((LAMP, "off"), (LAMP2, "off"), (MOTION, "off"), (MOOD, "Hygge")):
        house.set(entity, value)
    automations = await house.load(device)
    assert [t["trigger"] for t in automations[0]["config"]["triggers"]] == ["state", "state"]
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {}), ("light.turn_on", (LAMP2,), {"brightness_pct": 20})]
    house.set(MOTION, "off")
    await house.advance(59)
    assert house.taken() == []
    await house.advance(2)
    assert house.taken() == [("light.turn_off", (LAMP, LAMP2), {})]


async def test_disabled_rule_and_disabled_device(house, hass):
    rules = [B.rule(1, "On", [B.state(MOTION, to="on")], [B.call("light.turn_on")]),
             B.rule(2, "Off", [B.state(MOTION, to="off")], [B.call("light.turn_off")], enabled=False)]
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    await house.load(B.device(1, "Lamp", [LAMP], rules), B.device(2, "Other", [LAMP2], rules, enabled=False))
    assert len(hass.states.async_all("automation")) == 1  # the disabled device has no automation at all
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [ON]
    house.set(MOTION, "off")
    await house.settle()
    assert house.taken() == []


async def test_or_conditions_and_weekdays(house, hass):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Morning", [B.at("07:00", ["mon", "tue", "wed", "thu", "fri"]), B.state(MOTION, to="on")],
               [B.call("light.turn_on")],
               B.any_of(B.c_state(MOOD, "Dagur"), B.all_of(B.c_numeric(LUX, below=50), B.c_state(MOOD, "Auto"))))])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (MOOD, "Nátt"), (LUX, "10")):
        house.set(entity, value)
    await house.load(device)
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == []  # neither alternative holds
    house.set(MOOD, "Auto")
    house.set(MOTION, "off")
    await house.settle()
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [ON]  # dark and Auto
    tz = dt_util.get_default_time_zone()
    house.freezer.move_to(datetime(2026, 11, 21, 7, 0, 0, tzinfo=tz))  # a Saturday
    async_fire_time_changed(hass, dt_util.utcnow())
    await house.settle()
    assert house.taken() == []  # the weekday restriction belongs to the time trigger...
    house.set(MOTION, "off")
    await house.settle()
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [ON]  # ...and not to the motion trigger of the same rule
    house.freezer.move_to(datetime(2026, 11, 23, 7, 0, 0, tzinfo=tz))  # a Monday
    async_fire_time_changed(hass, dt_util.utcnow())
    await house.settle()
    assert house.taken() == [ON]


async def test_other_domains(house):
    cover = B.device(1, "Blind", ["cover.blind"], [
        B.rule(1, "Close at dusk", [B.numeric(LUX, below=20)], [B.call("cover.close_cover")])])
    climate = B.device(2, "Heater", ["climate.heater", "switch.pump"], [
        B.rule(2, "Window", [B.state("binary_sensor.window", to="on", for_seconds=30)],
               [B.call("climate.set_temperature", target=["climate.heater"], temperature=16.5),
                B.call("switch.turn_off", target=["switch.pump"])])])
    house.set(LUX, "100")
    house.set("binary_sensor.window", "off")
    automations = await house.load(cover, climate)
    assert len(automations) == 2
    house.set(LUX, "10")
    await house.settle()
    assert house.taken() == [("cover.close_cover", ("cover.blind",), {})]
    house.set("binary_sensor.window", "on")
    await house.advance(31)
    assert house.taken() == [("climate.set_temperature", ("climate.heater",), {"temperature": 16.5}),
                             ("switch.turn_off", ("switch.pump",), {})]


async def test_delay_rule_runs_separately_and_blocks_nothing(house, hass):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Stairs", [B.state(MOTION, to="on")], [B.call("light.turn_on"), B.delay(120), B.call("light.turn_off")]),
        B.rule(2, "Hygge", [B.state(MOOD, to="Hygge")], [B.call("light.turn_on", brightness_pct=30)]),
    ])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (MOOD, "Dagur")):
        house.set(entity, value)
    automations = await house.load(device)
    assert [(a["role"], a["mode"]) for a in automations] == [("shared", "queued"), ("separate", "restart")]
    house.set(MOTION, "on")
    await house.settle()
    assert house.taken() == [ON]
    await house.advance(60)
    house.set(MOOD, "Hygge")  # the other rule runs while the first one waits
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {"brightness_pct": 30})]
    house.set(MOTION, "off")
    await house.settle()
    house.set(MOTION, "on")  # retriggered after 60 s: the wait starts over
    await house.settle()
    assert house.taken() == [ON]
    await house.advance(100)
    assert house.taken() == []  # 160 s after the first trigger: the first wait was cancelled
    await house.advance(25)
    assert house.taken() == [OFF]


async def test_parallel_delay_rule_gets_its_own_parallel_automation(house):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Pulse", [B.state(MOTION, to="on")], [B.call("light.turn_on"), B.delay(30), B.call("light.turn_off")],
               retrigger="parallel"),
        B.rule(2, "Hygge", [B.state(MOOD, to="Hygge")], [B.call("light.turn_on", brightness_pct=30)]),
    ])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (MOOD, "Dagur")):
        house.set(entity, value)
    automations = await house.load(device)
    assert [(a["role"], a["mode"]) for a in automations] == [("shared", "queued"), ("separate", "parallel")]
    house.set(MOTION, "on")
    await house.settle()
    house.set(MOOD, "Hygge")
    await house.settle()
    assert house.taken() == [ON, ("light.turn_on", (LAMP,), {"brightness_pct": 30})]
    await house.advance(31)
    assert house.taken() == [OFF]


# --------------------------------------------------------------- occupancy
def occupancy_device(*extra_rules, conditions=None, actions=None, **recheck):
    return B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Occupied", [B.occupancy(OCC)], actions or [B.call("light.turn_on")], conditions,
               recheck=recheck or {}), *extra_rules])


async def start_occupancy(house, device, *, grace=True, settings=None, lamp="off"):
    house.set(LAMP, lamp)
    house.set(OCC, "off", fresh=True)
    automations = await house.load(device, settings=settings)
    if grace:
        await house.advance(330, step=30)  # past the start-up grace
    house.taken()
    return automations


async def test_occupancy_rule_has_generated_rechecks(house):
    automations = await start_occupancy(house, occupancy_device())
    config = automations[0]["config"]
    kinds = [(t["trigger"], t.get("for")) for t in config["triggers"]]
    assert kinds == [("state", None), ("state", {"hours": 0, "minutes": 0, "seconds": 2}),
                     ("state", {"hours": 0, "minutes": 0, "seconds": 15}), ("time_pattern", None)]
    assert all("delay" not in step and "wait_for_trigger" not in step for step in config["actions"])  # no waiting

    house.set(OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [ON]
    await house.advance(20)
    await house.advance(600, step=30)
    assert house.taken() == []  # the lamp is on: no recheck repeats the command


async def test_recheck_corrects_a_lost_command_once(house):
    await start_occupancy(house, occupancy_device())
    house.apply = False  # the first command is lost
    house.set(OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [ON]
    await house.advance(1)
    assert house.taken() == []
    house.apply = True
    await house.advance(2)
    assert house.taken() == [ON]  # the 2 s recheck sees the lamp still off
    await house.advance(20)
    await house.advance(600, step=30)
    assert house.taken() == []


async def test_recheck_leaves_a_manual_change_alone(house):
    await start_occupancy(house, occupancy_device())
    house.set(OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [ON]
    await house.advance(5)
    house.set(LAMP, "off")  # somebody switches the lamp off
    await house.advance(20)
    await house.advance(900, step=30)
    assert house.taken() == []  # neither the 15 s recheck nor the periodic one forces it back on


async def test_recheck_evaluates_current_conditions(house):
    device = occupancy_device(conditions=B.all_of(B.c_numeric(LUX, below=100)))
    house.set(LUX, "400")
    await start_occupancy(house, device)
    house.set(OCC, "on", fresh=True)
    await house.advance(20)
    assert house.taken() == []  # too bright: neither the rule nor its rechecks act
    house.set(LUX, "30")
    await house.advance(330, step=30)
    assert house.taken() == [ON]  # the periodic check sees the room occupied, dark and the lamp untouched
    await house.advance(600, step=30)
    assert house.taken() == []


@pytest.mark.parametrize("problem", ["unavailable", "unknown", "stale", "vacant"])
async def test_no_correction_without_trustworthy_occupancy(house, problem):
    await start_occupancy(house, occupancy_device())
    house.apply = False
    house.set(OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [ON]
    if problem == "stale":
        house.set(OCC, "on", fresh=False)
    else:
        house.set(OCC, "off" if problem == "vacant" else problem, fresh=True)
    await house.advance(20)
    await house.advance(600, step=30)
    assert house.taken() == []  # the lamp is off, but the occupancy is not known to be current: nothing is forced


async def test_an_on_rule_never_turns_anything_off(house):
    await start_occupancy(house, occupancy_device())
    house.set(OCC, "on", fresh=True)
    await house.advance(20)
    assert house.taken() == [ON]
    house.set(OCC, "off", fresh=True)
    await house.advance(20)
    await house.advance(900, step=30)
    assert house.taken() == []  # no OFF is inferred from an ON rule


async def test_vacancy_rule_and_occupied_rule_together(house):
    device = occupancy_device(B.rule(2, "Vacant", [B.occupancy(OCC, "vacant")], [B.call("light.turn_off")]))
    automations = await start_occupancy(house, device)
    assert len(automations) == 1
    house.set(OCC, "on", fresh=True)
    await house.advance(20)
    assert house.taken() == [ON]
    house.apply = False
    house.set(OCC, "off", fresh=True)
    await house.advance(1)
    assert house.taken() == [OFF]
    house.apply = True
    await house.advance(2)
    assert house.taken() == [OFF]  # the lost OFF is repeated by the vacancy rule's own recheck
    await house.advance(600, step=30)
    assert house.taken() == []


async def test_toggle_is_never_repeated(house):
    device = occupancy_device(actions=[B.call("light.toggle")])
    automations = await start_occupancy(house, device)
    assert [t["trigger"] for t in automations[0]["config"]["triggers"]] == ["state"]  # no recheck triggers at all
    house.apply = False
    house.set(OCC, "on", fresh=True)
    await house.advance(20)
    await house.advance(600, step=30)
    assert house.taken() == [("light.toggle", (LAMP,), {})]


async def test_manual_override_signal(house):
    device = occupancy_device(override_entity="input_boolean.manual")
    house.set("input_boolean.manual", "on")
    await start_occupancy(house, device)
    house.apply = False
    house.set(OCC, "on", fresh=True)
    await house.advance(20)
    assert house.taken() == [ON]  # the rule itself runs; the rechecks respect the override signal
    house.set("input_boolean.manual", "off")
    house.apply = True
    await house.advance(330, step=30)
    assert house.taken() == [ON]


async def test_no_correction_during_the_start_up_grace(house):
    """Right after Home Assistant starts (or the automation is loaded) "last changed" times say nothing: an
    occupied room with a lamp somebody switched off must stay as it is."""
    house.set(LAMP, "off")
    house.set(OCC, "unavailable")
    await house.load(occupancy_device())
    await house.advance(30)
    house.set(OCC, "on", fresh=True)  # the occupancy entity comes back after the restart: still occupied
    await house.advance(30)
    await house.advance(1200, step=30)
    assert house.taken() == []


async def test_recheck_timing_is_configurable(house):
    device = occupancy_device(first_seconds=5, second_seconds=40, periodic_minutes=0)
    automations = await start_occupancy(house, device, settings=B.settings(startup_grace_seconds=0), grace=False)
    triggers = automations[0]["config"]["triggers"]
    assert [t.get("for", {}).get("seconds") for t in triggers] == [None, 5, 40]
    assert automations[0]["config"]["conditions"] == [] and "trace" not in automations[0]["config"]
    house.apply = False
    await house.advance(1)
    house.set(OCC, "on", fresh=True)
    await house.advance(4)
    assert house.taken() == [ON]
    await house.advance(2)
    assert house.taken() == [ON]
    await house.advance(40)
    assert house.taken() == [ON]
    await house.advance(900, step=30)
    assert house.taken() == []  # no periodic check


async def test_ordinary_rules_work_without_occupancy_or_mood(house, hass):
    device = B.device(1, "Fan", ["switch.fan"], [
        B.rule(1, "Humid", [B.numeric("sensor.humidity", above=70, for_seconds=10)], [B.call("switch.turn_on")]),
        B.rule(2, "Dry", [B.numeric("sensor.humidity", below=55)], [B.call("switch.turn_off")])])
    house.set("sensor.humidity", "50")
    house.set("switch.fan", "off")
    automations = await house.load(device)
    assert all(t["trigger"] == "numeric_state" for t in automations[0]["config"]["triggers"])
    assert not [s for s in hass.states.async_all() if "sod_" in s.entity_id or s.domain == "input_select"]
    house.set("sensor.humidity", "80")
    await house.advance(11)
    assert house.taken() == [("switch.turn_on", ("switch.fan",), {})]
    house.set("sensor.humidity", "50")
    await house.settle()
    assert house.taken() == [("switch.turn_off", ("switch.fan",), {})]


async def test_recheck_with_entity_ids_that_start_with_a_digit(house):
    """``states.light.3d_lamp`` is not valid in a template; the generated ones use ``states['light.3d_lamp']``."""
    lamp, occ = "light.3d_lamp", "binary_sensor.2nd_floor_occupied"
    device = B.device(1, "Lamp", [lamp], [B.rule(1, "Occupied", [B.occupancy(occ)], [B.call("light.turn_on")])])
    house.set(lamp, "off")
    house.set(occ, "off")
    await house.load(device)
    await house.advance(330, step=30)
    house.apply = False
    house.set(occ, "on")
    await house.advance(1)
    house.apply = True
    await house.advance(2)
    assert house.taken() == [("light.turn_on", (lamp,), {}), ("light.turn_on", (lamp,), {})]
