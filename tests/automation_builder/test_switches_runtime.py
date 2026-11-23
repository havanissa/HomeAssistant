"""Switches & Remotes (0.5) in Home Assistant's own engine: one switch, many interactions against the same light.

The 0.4 interface could hold one action per interaction (a single press could turn Light1 ON *or* OFF, and adding the
same switch again made a second card). The rule model and compiler always allowed several mappings; these tests run
exactly the documents the new interaction list writes: the same switch and the same light in any number of mappings,
each a rule of its own, compiled into the device's one shared automation without losing a trigger or an action.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from homeassistant.core import Context

from ab import compiler, conflicts, designers, model

from .test_lighting2_runtime import DEV, FLAG, L1, L3, LL2, LightHouse, lights, setup, spec_designer, zones  # noqa: F401

WALL = "event.wall"
_STAMP = itertools.count(1)


def press(hass, kind: str, entity: str = WALL) -> None:
    """An event entity reports a press: its state is a new timestamp, its attribute the event type."""
    n = next(_STAMP)
    hass.states.async_set(entity, f"2026-11-23T10:{n // 60:02d}:{n % 60:02d}.000+00:00",
                          {"event_type": kind, "event_types": ["single", "double", "triple", "long", "release"]})


def zha(hass, command: str, ieee: str = "00:11") -> None:
    hass.bus.async_fire("zha_event", {"device_ieee": ieee, "command": command})


def remote(*mappings: dict[str, Any], interactions: list[dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
    events = interactions or [
        {"id": "i_0001", "kind": "single", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["single"]}},
        {"id": "i_0002", "kind": "double", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["double"]}},
        {"id": "i_0003", "kind": "triple", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["triple"]}},
        {"id": "i_0004", "kind": "long", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["long"]}},
        {"id": "i_0005", "kind": "release", "trigger": {"type": "button", "entity_id": WALL, "event_types": ["release"]}}]
    return {"id": "w_0001", "name": "Living Room Wall Switch", "kind": "momentary", "entity_id": WALL,
            "interactions": events, "mappings": list(mappings), **extra}


def m(mid: str, interaction: str, action: dict[str, Any], *lights_: str, override: bool = False, **extra: Any) -> dict[str, Any]:
    return {"id": mid, "interaction": interaction, "lights": list(lights_ or (L1,)), "action": action, "override": override, **extra}


def plain(**extra: Any) -> dict[str, Any]:
    """A designer with only switches (no matrices): every light starts off and only the switch acts."""
    d = spec_designer(**extra)
    d["truth"] = {"matrices": []}
    d["brightness"] = {"matrices": []}
    return d


async def start(hass, house, d, **kw):
    hass.states.async_set(WALL, "unknown", {"event_types": ["single", "double", "triple", "long", "release"]})
    automations, x = await setup(hass, house, d, **kw)
    await house.settle()
    house.taken()
    return automations, x


# ------------------------------------------------------------------ 1-3: the same switch, the same light, several actions
async def test_same_switch_turns_the_same_light_on_and_off(lights, hass):
    d = plain(switches=[remote(m("p_0001", "i_0001", {"kind": "on"}), m("p_0002", "i_0002", {"kind": "off"}))])
    automations, _x = await start(hass, lights, d)
    shared = [a for a in automations if a["role"] == "shared"]
    assert len(shared) == 1 and len(shared[0]["rule_ids"]) == 2              # both mappings, one automation
    press(hass, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})] and lights.light(L1)[0] == "on"
    press(hass, "double")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_off", (L1,), {})] and lights.light(L1)[0] == "off"
    press(hass, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})] and lights.light(L1)[0] == "on"


async def test_on_off_and_toggle_on_the_same_light(lights, hass):
    d = plain(switches=[remote(m("p_0001", "i_0001", {"kind": "on"}), m("p_0002", "i_0002", {"kind": "off"}),
                               m("p_0003", "i_0003", {"kind": "toggle"}))])
    await start(hass, lights, d)
    for kind, expected, state in (("triple", "light.toggle", "on"), ("triple", "light.toggle", "off"),
                                  ("single", "light.turn_on", "on"), ("double", "light.turn_off", "off")):
        press(hass, kind)
        await lights.settle()
        assert lights.light_calls() == [(expected, (L1,), {})], kind      # exactly one call: no duplicate toggle
        assert lights.light(L1)[0] == state


async def test_five_interactions_of_one_switch_on_one_light(lights, hass):
    """Single ON, double OFF, long = dim while held, release ends it, triple = resume automatic."""
    d = plain(override={"policy": "until_resumed"}, switches=[remote(
        m("p_0001", "i_0001", {"kind": "on"}, override=True), m("p_0002", "i_0002", {"kind": "off"}, override=True),
        m("p_0003", "i_0004", {"kind": "dim", "direction": "down", "step": 10, "interval": 1, "release": "i_0005"}),
        m("p_0004", "i_0003", {"kind": "resume"}))])
    automations, _x = await start(hass, lights, d, helpers=True)
    assert sorted(a["role"] for a in automations) == ["separate", "shared"]   # hold-to-dim waits: its own automation
    press(hass, "single")
    await lights.settle()
    assert lights.light(L1)[0] == "on" and hass.states.get(FLAG).state == "on"
    lights.taken()
    press(hass, "long")
    await lights.settle()
    await lights.advance(2.5, step=0.5)
    dimmed = lights.light(L1)[1]
    assert dimmed < 255
    press(hass, "release")
    await lights.settle()
    await lights.advance(3, step=0.5)
    assert lights.light(L1)[1] == dimmed                                     # the release ends the dimming
    press(hass, "double")
    await lights.settle()
    assert lights.light(L1)[0] == "off"
    press(hass, "triple")
    await lights.settle()
    assert hass.states.get(FLAG).state == "off"                              # resume ends the override


# ------------------------------------------------------------------ 4-8: several targets, switches, buttons, press kinds
async def test_one_event_several_compatible_actions_and_one_switch_many_lights(lights, hass):
    d = plain(switches=[remote(m("p_0001", "i_0001", {"kind": "on"}, L1, LL2), m("p_0002", "i_0001", {"kind": "on"}, L3),
                               m("p_0003", "i_0002", {"kind": "off"}, L1, LL2, L3))])
    await start(hass, lights, d)
    press(hass, "single")
    await lights.settle()
    assert sorted(lights.light_calls()) == sorted([("light.turn_on", (L1, LL2), {}), ("light.turn_on", (L3,), {})])
    press(hass, "double")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_off", (L1, LL2, L3), {})]


async def test_several_switches_control_one_light(lights, hass):
    second = {"id": "w_0002", "name": "Bedside remote", "kind": "momentary", "interactions": [
        {"id": "i_0011", "kind": "on", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:22", "command": "on"}}},
        {"id": "i_0012", "kind": "off", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:22", "command": "off"}}}],
        "mappings": [m("p_0011", "i_0011", {"kind": "on"}), m("p_0012", "i_0012", {"kind": "off"})]}
    d = plain(switches=[remote(m("p_0001", "i_0001", {"kind": "toggle"})), second])
    await start(hass, lights, d)
    zha(hass, "on", "00:22")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})]
    press(hass, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.toggle", (L1,), {})] and lights.light(L1)[0] == "off"
    zha(hass, "off", "00:11")                                                # another remote's button: nothing
    await lights.settle()
    assert lights.light_calls() == []


async def test_separate_buttons_and_short_and_long_presses_of_one_remote(lights, hass):
    buttons = [{"id": "i_0021", "kind": "single", "button": "button_1", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:11", "command": "on"}}},
               {"id": "i_0022", "kind": "single", "button": "button_2", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:11", "command": "off"}}},
               {"id": "i_0023", "kind": "long", "button": "button_1", "trigger": {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "00:11", "command": "move_with_on_off"}}}]
    d = plain(switches=[remote(m("p_0021", "i_0021", {"kind": "on"}), m("p_0022", "i_0022", {"kind": "off"}),
                               m("p_0023", "i_0023", {"kind": "brightness", "pct": 100}), interactions=buttons)])
    await start(hass, lights, d)
    zha(hass, "on")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})]
    zha(hass, "move_with_on_off")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {"brightness_pct": 100})]
    zha(hass, "off")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_off", (L1,), {})]


@pytest.mark.parametrize("expected_lingering_timers", [True])   # MQTT's own periodic housekeeping
async def test_zigbee2mqtt_single_and_double_on_the_same_light(lights, hass, mqtt_mock):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
    topic = "zigbee2mqtt/Wall/action"
    events = [{"id": "i_0031", "kind": "single", "trigger": {"type": "mqtt", "topic": topic, "payload": "single"}},
              {"id": "i_0032", "kind": "double", "trigger": {"type": "mqtt", "topic": topic, "payload": "double"}}]
    d = plain(switches=[remote(m("p_0031", "i_0031", {"kind": "on"}), m("p_0032", "i_0032", {"kind": "off"}), interactions=events)])
    await start(hass, lights, d)
    async_fire_mqtt_message(hass, topic, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})]
    async_fire_mqtt_message(hass, topic, "double")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_off", (L1,), {})]


# ------------------------------------------------------------------ 17-18: overrides and resume with a matrix
async def test_override_then_resume_restores_the_matrix_baseline(lights, hass):
    d = spec_designer(override={"policy": "until_resumed"}, switches=[remote(
        m("p_0001", "i_0001", {"kind": "off"}, override=True), m("p_0002", "i_0002", {"kind": "on"}, override=True),
        m("p_0003", "i_0003", {"kind": "resume"}))])
    await start(hass, lights, d, helpers=True)
    zones(hass, False, True)                   # the matrices: L1 on at 25 %
    await lights.settle()
    assert lights.light(L1)[:2] == ("on", 64)
    lights.taken()
    press(hass, "single")
    await lights.settle()
    assert lights.light(L1)[0] == "off" and hass.states.get(FLAG).state == "on"
    zones(hass, False, False)                  # the matrices change during the override: L1 is left alone
    await lights.settle()
    assert lights.light(L1)[0] == "off"
    press(hass, "double")                      # the same switch, the same light: ON, the override continues
    await lights.settle()
    assert lights.light(L1)[0] == "on" and hass.states.get(FLAG).state == "on"
    lights.taken()
    press(hass, "triple")                      # resume: back to what the matrices say now (both vacant: on, 50 %)
    await lights.settle()
    assert hass.states.get(FLAG).state == "off" and lights.light(L1)[:2] == ("on", 128)


# ------------------------------------------------------------------ conditions, latched switches, feedback
async def test_only_when_conditions_of_one_mapping(lights, hass):
    night = {"type": "and", "items": [{"type": "state", "entity_id": "input_boolean.night", "state": "on", "negate": False,
                                       "for_seconds": 0, "attribute": None}]}
    d = plain(switches=[remote(m("p_0001", "i_0001", {"kind": "brightness", "pct": 10}, conditions=night),
                               m("p_0002", "i_0001", {"kind": "brightness", "pct": 100},
                                 conditions={"type": "and", "items": [{**night["items"][0], "negate": True}]}))])
    hass.states.async_set("input_boolean.night", "off")
    await start(hass, lights, d)
    press(hass, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {"brightness_pct": 100})]
    hass.states.async_set("input_boolean.night", "on")
    press(hass, "single")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {"brightness_pct": 10})]


async def test_latched_switch_on_and_off_and_changes_by_automations_ignored(lights, hass):
    latched = {"id": "w_0005", "name": "Wall rocker", "kind": "latched", "entity_id": "switch.wall", "ignore_automations": True,
               "interactions": [
                   {"id": "i_0051", "kind": "on", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "on", "from": "off", "for_seconds": 0}},
                   {"id": "i_0052", "kind": "off", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "off", "from": "on", "for_seconds": 0}}],
               "mappings": [m("p_0051", "i_0051", {"kind": "on"}), m("p_0052", "i_0052", {"kind": "off"})]}
    hass.states.async_set("switch.wall", "off")
    await start(hass, lights, plain(switches=[latched]))
    hass.states.async_set("switch.wall", "on")                               # by hand
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {})]
    hass.states.async_set("switch.wall", "off", context=Context(parent_id="01AUTOMATION"))   # set by an automation
    await lights.settle()
    assert lights.light_calls() == []
    hass.states.async_set("switch.wall", "on")
    hass.states.async_set("switch.wall", "off")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_on", (L1,), {}), ("light.turn_off", (L1,), {})]


# ------------------------------------------------------------------ conflicts stay with the conflict check
def _device(d: dict[str, Any]) -> dict[str, Any]:
    config = model.validate_config({"settings": {}, "devices": {DEV: {"name": "Stova", "targets": [L1, LL2, L3, "light.l4"],
                                                                      "rules": [], "designers": [d]}}})
    device = config["devices"][DEV]
    return designers.effective(device, designers.expand(device, None))


def test_on_and_off_on_the_same_press_is_a_blocking_conflict_but_different_presses_are_not():
    fine = _device(plain(switches=[remote(m("p_0001", "i_0001", {"kind": "on"}), m("p_0002", "i_0002", {"kind": "off"}))]))
    assert not [c for c in conflicts.analyse(fine) if c["level"] == "blocking"]
    clash = _device(plain(switches=[remote(m("p_0001", "i_0001", {"kind": "on"}), m("p_0002", "i_0001", {"kind": "off"}))]))
    assert [c["code"] for c in conflicts.analyse(clash) if c["level"] == "blocking"] == ["contradiction"]
    twice = _device(plain(switches=[remote(m("p_0001", "i_0001", {"kind": "toggle"}), m("p_0002", "i_0001", {"kind": "toggle"}))]))
    assert [c["code"] for c in conflicts.analyse(twice) if c["level"] == "blocking"] == ["duplicate_rule"]


def test_every_trigger_and_action_survives_consolidation():
    d = plain(switches=[remote(*[m(f"p_000{i}", f"i_000{i}", {"kind": k}) for i, k in
                                 zip(range(1, 6), ("on", "off", "toggle", "on", "off"))])])
    device = _device(d)
    shared = next(a for a in compiler.compile_device(device, model.validate_settings({}, model.Problems()))["automations"]
                  if a["role"] == "shared")
    assert len(shared["rule_ids"]) == 5
    assert len(shared["config"]["actions"]) == 5                             # one block per mapping
    assert len(shared["config"]["triggers"]) == 1                            # the event entity: one native trigger,
    gates = [b["if"][0] for b in shared["config"]["actions"]]                # told apart by the event type
    assert len({str(g) for g in gates}) == 5
