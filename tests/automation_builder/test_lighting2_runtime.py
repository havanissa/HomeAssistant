"""Lighting Designer 2 in Home Assistant's own engine: Truth and Brightness Matrices, Colour & Mood, Advanced Rules,
switches and remotes with manual overrides (real input_boolean and timer helpers), and the Python twin compared with
what Home Assistant does."""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Context, ServiceCall
from homeassistant.setup import async_setup_component

from ab import compiler, designers, model, simulate
from ab.designers import lighting2 as L2

from .conftest import House

DEV = "d_0000abcd"
A1, A2 = "z_000000a1", "z_000000a2"
LIGHTS = ("light.l1", "light.l2", "light.l3", "light.l4")
L1, LL2, L3, L4 = LIGHTS
MOOD, TV, LUX = "input_select.mood", "media_player.tv", "sensor.lux"
FLAG, TIMER = L2.helper_ids(DEV, L1)["flag"], L2.helper_ids(DEV, L1)["timer"]


class LightHouse(House):
    """Lights that behave like lights: brightness, colour temperature, colour and effect follow the commands, and a
    light switched on without a brightness comes back at its last one."""

    def __init__(self, hass, freezer) -> None:
        super().__init__(hass, freezer)
        self.memory: dict[str, dict[str, Any]] = {}

    async def _record(self, call: ServiceCall) -> None:
        if call.domain != "light":
            await super()._record(call)
            return
        self.calls.append(call)
        if not self.apply:
            return
        ids = call.data.get("entity_id") or []
        for entity_id in [ids] if isinstance(ids, str) else ids:
            current = self.hass.states.get(entity_id)
            if current is None or current.state == "unavailable":
                continue
            attrs = dict(current.attributes)
            lit = self.memory.setdefault(entity_id, {"brightness": 255, "color_mode": "color_temp", "color_temp_kelvin": 4000})
            service = call.service
            if service == "toggle":
                service = "turn_off" if current.state == "on" else "turn_on"
            if service == "turn_off":
                for key in ("brightness", "color_mode", "color_temp_kelvin", "hs_color", "rgb_color", "effect"):
                    attrs[key] = None
                self.hass.states.async_set(entity_id, "off", attrs, context=call.context)
                continue
            data = call.data
            if "brightness_pct" in data:
                lit["brightness"] = round(data["brightness_pct"] * 255 / 100)
            if "brightness_step_pct" in data:
                lit["brightness"] = max(3, min(255, lit["brightness"] + round(data["brightness_step_pct"] * 255 / 100)))
            if "color_temp_kelvin" in data:
                lit.update(color_mode="color_temp", color_temp_kelvin=data["color_temp_kelvin"], hs_color=None, rgb_color=None)
            if "rgb_color" in data:
                lit.update(color_mode="hs", hs_color=tuple(L2.hs_of(list(data["rgb_color"]))), rgb_color=tuple(data["rgb_color"]),
                           color_temp_kelvin=None)
            if "hs_color" in data:
                lit.update(color_mode="hs", hs_color=tuple(data["hs_color"]), color_temp_kelvin=None)
            if "effect" in data:
                lit["effect"] = data["effect"]
            attrs.update(lit)
            self.hass.states.async_set(entity_id, "on", attrs, context=call.context)

    def light(self, entity_id: str) -> tuple[str, Any, Any]:
        state = self.hass.states.get(entity_id)
        return state.state, state.attributes.get("brightness"), state.attributes.get("color_temp_kelvin")

    def light_calls(self) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
        return [c for c in self.taken() if c[0].startswith("light.")]


@pytest.fixture
async def lights(hass, freezer):
    house = LightHouse(hass, freezer)
    yield house
    entities = [s.entity_id for s in hass.states.async_all("automation")]
    if entities:
        await hass.services.async_call("automation", "turn_off", {"entity_id": entities}, blocking=True)
        await hass.async_block_till_done()


def sources() -> dict[str, Any]:
    return {"sources": {A1: {"name": "Area1", "kind": "zone", "entity_id": "binary_sensor.area1"},
                        A2: {"name": "Area2", "kind": "zone", "entity_id": "binary_sensor.area2"}}}


def cols(prefix: str) -> list[dict[str, Any]]:
    return [{"id": f"c_{prefix}001", "kind": "presence", "source": A1, "state": "occupied"},
            {"id": f"c_{prefix}002", "kind": "presence", "source": A1, "state": "vacant"},
            {"id": f"c_{prefix}003", "kind": "presence", "source": A2, "state": "occupied"},
            {"id": f"c_{prefix}004", "kind": "presence", "source": A2, "state": "vacant"}]


def row(prefix: str, values: list[Any], kind: str) -> dict[str, Any]:
    out = {}
    for i, value in enumerate(values, start=1):
        out[f"c_{prefix}00{i}"] = {"kind": value} if kind == "truth" else {"kind": "set", "pct": value}
    return out


def spec_designer(**extra: Any) -> dict[str, Any]:
    """The example of the specification: four lights, two areas, a Truth and a Brightness Matrix."""
    truth = {L1: row("a", ["off", "on", "on", "on"], "truth"), LL2: row("a", ["off", "on", "on", "on"], "truth"),
             L3: row("a", ["on"] * 4, "truth"), L4: row("a", ["on"] * 4, "truth")}
    bright = {L1: row("b", [0, 50, 25, 50], "b"), LL2: row("b", [0, 50, 25, 50], "b"),
              L3: row("b", [25, 50, 50, 100], "b"), L4: row("b", [25, 50, 50, 100], "b")}
    d = {"id": "g_00000001", "kind": "lighting", "version": 2, "name": "Lighting", "lights": list(LIGHTS),
         # Area1 occupied outranks Area2 (both can hold at once: never decided by the column order)
         "truth": {"matrices": [{"id": "m_0001", "name": "Presence", "columns": cols("a"), "cells": truth,
                                 "policy": "priority", "ranks": ["c_a001", "c_a003", "c_a004", "c_a002"]}]},
         "brightness": {"zero": "unchanged", "matrices": [{"id": "m_0002", "name": "Presence", "columns": cols("b"),
                                                           "cells": bright, "policy": "priority",
                                                           "ranks": ["c_b001", "c_b003", "c_b002", "c_b004"]}]}}
    d.update(extra)
    return d


def build(d: dict[str, Any], targets: tuple[str, ...] = LIGHTS) -> tuple[dict[str, Any], Any]:
    config = model.validate_config({"settings": {}, "presence": sources(), "devices": {DEV: {
        "name": "Stova", "targets": list(targets), "rules": [], "designers": [d]}}})
    device = config["devices"][DEV]
    x = designers.expand(device, config["presence"])
    blocking = [p for p in x.problems if p["level"] == "blocking"]
    assert not blocking, blocking
    return designers.effective(device, x), x


async def setup(hass, house: LightHouse, d: dict[str, Any], *, helpers: bool = False, mood: str | None = None,
                off: tuple[str, ...] = LIGHTS, before: Any = None) -> tuple[list[dict[str, Any]], Any]:
    for light in off:
        hass.states.async_set(light, "off", {"supported_color_modes": ["color_temp", "hs"], "min_color_temp_kelvin": 2000,
                                             "max_color_temp_kelvin": 6500})
    hass.states.async_set("binary_sensor.area1", "off")
    hass.states.async_set("binary_sensor.area2", "off")
    hass.states.async_set(TV, "idle")
    if mood:
        assert await async_setup_component(hass, "input_select", {"input_select": {"mood": {
            "options": ["Dagur", "Hygge", "Nátt"], "initial": mood}}})
    if helpers:
        assert await async_setup_component(hass, "input_boolean", {"input_boolean": {FLAG.split(".")[1]: {"name": "o"}}})
        assert await async_setup_component(hass, "timer", {"timer": {TIMER.split(".")[1]: {"restore": True}}})
    if before is not None:
        await before()
    effective, x = build(d)
    automations = await house.load(effective)
    await hass.async_block_till_done()
    return automations, x


def zones(hass, a1: bool, a2: bool) -> None:
    hass.states.async_set("binary_sensor.area1", "on" if a1 else "off")
    hass.states.async_set("binary_sensor.area2", "on" if a2 else "off")


# ------------------------------------------------------------------ the specification's example
async def test_truth_and_brightness_matrices_of_the_example(lights, hass):
    automations, _x = await setup(hass, lights, spec_designer())
    assert sorted(a["role"] for a in automations) == ["group"] * 4          # one native automation per light
    zones(hass, False, True)     # Area1 vacant, Area2 occupied
    await lights.settle()
    assert [lights.light(light)[:2] for light in LIGHTS] == [("on", 64), ("on", 64), ("on", 128), ("on", 128)]
    zones(hass, True, True)      # both occupied: Area1 (rank 1) decides - L1/L2 OFF, L3/L4 25 %
    await lights.settle()
    assert [lights.light(light)[:2] for light in LIGHTS] == [("off", None), ("off", None), ("on", 64), ("on", 64)]
    lights.taken()
    zones(hass, True, True)      # nothing changed: nothing is sent
    hass.states.async_set("binary_sensor.area2", "on", {"x": 1})
    await lights.settle()
    assert lights.light_calls() == []
    zones(hass, False, False)    # both vacant: 50 % everywhere (Area1 vacant ranks above Area2 vacant for brightness)
    await lights.settle()
    assert [lights.light(light)[:2] for light in LIGHTS] == [("on", 128)] * 4


async def test_zero_brightness_is_never_an_off_command(lights, hass):
    await setup(hass, lights, spec_designer())
    zones(hass, False, True)     # (an input must change for an evaluation: the sensors start "off")
    await lights.settle()
    zones(hass, False, False)
    await lights.settle()
    lights.taken()
    zones(hass, True, False)     # Area1 occupied: L3 25 %; L1 OFF by its Truth Matrix (not by the 0 %)
    await lights.settle()
    calls = lights.light_calls()
    assert ("light.turn_off", (L1,), {}) in calls and ("light.turn_on", (L3,), {"brightness_pct": 25}) in calls


# ------------------------------------------------------------------ mood, Advanced Rules, the baseline
TV_RULE = {"id": "a_0001", "name": "TV", "kind": "while", "lights": [L3],
           "conditions": {"type": "and", "items": [{"type": "state", "entity_id": TV, "state": "playing"}]},
           "set": {"truth": "off"}}
MOODS = {"enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge"],
         "cells": {L3: {"Hygge": {"kind": "set", "color_temp_kelvin": 2700}, "Dagur": {"kind": "set", "color_temp_kelvin": 5000}}}}


async def test_advanced_rule_ends_and_the_current_baseline_returns(lights, hass):
    await setup(hass, lights, spec_designer(advanced=[TV_RULE], mood=MOODS), mood="Hygge")
    zones(hass, False, True)
    await lights.settle()
    assert lights.light(L3) == ("on", 128, 2700)                  # ON 50 % 2700 K: truth, brightness and mood together
    hass.states.async_set(TV, "playing")
    await lights.settle()
    assert lights.light(L3)[0] == "off" and lights.light(L4)[0] == "on"
    zones(hass, True, True)                                       # the inputs change while the rule holds
    await hass.services.async_call("input_select", "select_option", {"entity_id": MOOD, "option": "Dagur"}, blocking=True)
    await lights.settle()
    assert lights.light(L3)[0] == "off"
    lights.taken()
    hass.states.async_set(TV, "idle")                             # no restoring rule: the evaluation does it
    await lights.settle()
    assert lights.light(L3) == ("on", 64, 5000)                   # the CURRENT baseline: Area1 25 %, Dagur 5000 K
    assert lights.light_calls() == [("light.turn_on", (L3,), {"brightness_pct": 25, "color_temp_kelvin": 5000})]


async def test_mood_changes_colour_only_while_the_light_is_on(lights, hass):
    d = spec_designer(mood=MOODS)
    await setup(hass, lights, d, mood="Hygge")
    zones(hass, True, True)
    await lights.settle()
    assert lights.light(L3) == ("on", 64, 2700)
    await hass.services.async_call("input_select", "select_option", {"entity_id": MOOD, "option": "Dagur"}, blocking=True)
    await lights.settle()
    assert lights.light(L3) == ("on", 64, 5000)


# ------------------------------------------------------------------ switches, remotes and overrides
def switch(*mappings: dict[str, Any]) -> dict[str, Any]:
    return {"id": "w_0001", "name": "Wall", "kind": "momentary", "interactions": [
        {"id": "i_0001", "kind": "single", "trigger": {"type": "event", "event_type": "zha_event",
                                                        "event_data": {"device_ieee": "00:11", "command": "toggle"}}},
        {"id": "i_0002", "kind": "double", "trigger": {"type": "button", "entity_id": "event.wall", "event_types": ["double"]}},
        {"id": "i_0003", "kind": "long", "trigger": {"type": "button", "entity_id": "event.wall", "event_types": ["hold"]}},
        {"id": "i_0004", "kind": "release", "trigger": {"type": "button", "entity_id": "event.wall", "event_types": ["release"]}}],
        "mappings": list(mappings)}


def mapping(mid: str, interaction: str, action: dict[str, Any], override: bool = False, light: str = L1) -> dict[str, Any]:
    return {"id": mid, "interaction": interaction, "lights": [light], "action": action, "override": override}


def press(hass, kind: str) -> None:
    hass.states.async_set("event.wall", f"2026-11-22T10:{len(kind):02d}:00.000+00:00", {"event_type": kind,
                                                                                          "event_types": ["double", "hold", "release"]})


async def test_switch_override_lasts_its_time_and_returns_to_the_current_baseline(lights, hass):
    d = spec_designer(override={"policy": "duration", "minutes": 1},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "toggle"}, override=True),
                                       mapping("p_0002", "i_0002", {"kind": "resume"}))])
    hass.states.async_set("event.wall", "unknown", {"event_types": ["double", "hold", "release"]})
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    assert lights.light(L1)[0] == "on"
    lights.taken()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})
    await lights.settle()
    assert lights.light(L1)[0] == "off" and hass.states.get(FLAG).state == "on" and hass.states.get(TIMER).state == "active"
    assert [c for c in lights.light_calls()] == [("light.toggle", (L1,), {})]       # exactly once
    zones(hass, False, False)                     # the matrices change during the override: the light is left alone
    await lights.settle()
    assert lights.light(L1)[0] == "off" and lights.light(LL2) == ("on", 128, 4000)
    lights.taken()
    await lights.advance(62)                      # the override ends: back to the baseline as it is NOW (50 %)
    assert hass.states.get(FLAG).state == "off" and lights.light(L1)[:2] == ("on", 128)
    assert lights.light_calls() == [("light.turn_on", (L1,), {"brightness_pct": 50})]


async def test_resume_ends_the_override_at_once_and_reapplies_the_baseline(lights, hass):
    d = spec_designer(override={"policy": "until_resumed"},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "off"}, override=True),
                                       mapping("p_0002", "i_0002", {"kind": "resume"}))])
    hass.states.async_set("event.wall", "unknown", {"event_types": ["double", "hold", "release"]})
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})
    await lights.settle()
    assert lights.light(L1)[0] == "off" and hass.states.get(FLAG).state == "on"
    await lights.advance(3600, step=60)           # "until resumed": time does not end it
    assert hass.states.get(FLAG).state == "on" and lights.light(L1)[0] == "off"
    press(hass, "double")
    await lights.settle()
    assert hass.states.get(FLAG).state == "off" and lights.light(L1)[:2] == ("on", 64)
    lights.taken()
    press(hass, "double")                         # resume without an override: the baseline again, nothing to change
    await lights.settle()
    assert lights.light_calls() == []


async def test_one_shot_press_gets_no_lasting_priority(lights, hass):
    d = spec_designer(switches=[switch(mapping("p_0001", "i_0001", {"kind": "off"}))])
    await setup(hass, lights, d)
    zones(hass, False, True)
    await lights.settle()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})
    await lights.settle()
    assert lights.light(L1)[0] == "off"
    zones(hass, False, False)                     # the next evaluation applies the baseline again
    await lights.settle()
    assert lights.light(L1)[:2] == ("on", 128)


async def test_hold_to_dim_until_release(lights, hass):
    d = spec_designer(switches=[switch(mapping("p_0001", "i_0003", {"kind": "dim", "direction": "down", "step": 10,
                                                                    "interval": 1, "release": "i_0004"}))])
    hass.states.async_set("event.wall", "unknown", {"event_types": ["double", "hold", "release"]})
    automations, _x = await setup(hass, lights, d)
    assert any(a["role"] == "separate" and a["mode"] == "restart" for a in automations)
    zones(hass, False, True)
    await lights.settle()
    zones(hass, False, False)
    await lights.settle()
    assert lights.light(L1)[1] == 128
    press(hass, "hold")
    await lights.settle()
    await lights.advance(2.5, step=0.5)
    dimmed = lights.light(L1)[1]
    assert dimmed < 128
    press(hass, "release")
    await lights.settle()
    await lights.advance(3, step=0.5)
    assert lights.light(L1)[1] == dimmed           # the release ends it


async def test_override_until_vacant_and_after_a_restart(lights, hass):
    d = spec_designer(override={"policy": "until_vacant", "source": A2},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "off"}, override=True))])
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})
    await lights.settle()
    assert hass.states.get(FLAG).state == "on" and lights.light(L1)[0] == "off"
    zones(hass, False, False)                     # Area2 vacant: the override ends, the baseline returns
    await lights.settle()
    assert hass.states.get(FLAG).state == "off" and lights.light(L1)[:2] == ("on", 128)


async def test_timed_override_that_expired_during_a_restart_ends_at_the_start(lights, hass):
    d = spec_designer(override={"policy": "duration", "minutes": 5},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "off"}, override=True))])
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    await hass.services.async_call("input_boolean", "turn_on", {"entity_id": FLAG}, blocking=True)   # as restored
    await hass.services.async_call("light", "turn_off", {"entity_id": L1}, blocking=True)
    await lights.settle()
    assert hass.states.get(TIMER).state == "idle" and lights.light(L1)[0] == "off"
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)       # the start trigger fires when Home Assistant has started
    await lights.settle()
    assert hass.states.get(FLAG).state == "off" and lights.light(L1)[0] == "on"


async def test_manual_change_only_counts_without_an_automation_context(lights, hass):
    d = spec_designer(override={"policy": "duration", "minutes": 10, "manual": True})
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    assert hass.states.get(FLAG).state == "off"                    # the designer's own commands are not "manual"
    state = hass.states.get(L1)
    hass.states.async_set(L1, "on", {**state.attributes, "brightness": 30})   # an attribute report: not a change by hand
    await lights.settle()
    assert hass.states.get(FLAG).state == "off"
    hass.states.async_set(L1, "off", state.attributes, context=Context(user_id="someone"))   # switched off by a person
    await lights.settle()
    assert hass.states.get(FLAG).state == "on" and lights.light(L1)[0] == "off"
    zones(hass, False, False)
    await lights.settle()
    assert lights.light(L1)[0] == "off"                            # held while the override lasts
    await hass.services.async_call("timer", "cancel", {"entity_id": TIMER}, blocking=True)


async def test_unavailable_light_gets_no_commands(lights, hass):
    await setup(hass, lights, spec_designer())
    hass.states.async_set(L1, "unavailable")                       # a wall switch cut its power
    zones(hass, False, True)
    await lights.settle()
    assert not [c for c in lights.light_calls() if L1 in c[1]]
    await lights.advance(120, step=10)
    assert hass.states.get(L1).state == "unavailable" and not [c for c in lights.light_calls() if L1 in c[1]]


async def test_simultaneous_switch_and_occupancy_events(lights, hass):
    d = spec_designer(override={"policy": "until_resumed"},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "brightness", "pct": 90}, override=True))])
    await setup(hass, lights, d, helpers=True)
    zones(hass, False, True)
    await lights.settle()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})
    zones(hass, True, True)                                        # in the same moment
    await lights.settle()
    assert lights.light(L1)[:2] == ("on", 230)                     # the switch override holds; L2 follows the matrices
    assert lights.light(LL2)[0] == "off"


# ------------------------------------------------------------------ the twin against the engine
@pytest.mark.parametrize("a1,a2,tv,mood_value,override,lit", list(itertools.product(
    (False, True), (False, True), ("idle", "playing"), ("Dagur", "Hygge"), (False, True), (False, True))))
async def test_simulation_predicts_what_home_assistant_does(lights, hass, a1, a2, tv, mood_value, override, lit):
    """Every combination: the Python twin predicts the command of each light's automation, and Home Assistant sends
    exactly that (one evaluation per light, from the same states)."""
    tv_rule = {**TV_RULE, "lights": [L3, L1]}
    d = spec_designer(advanced=[tv_rule], mood=MOODS, override={"policy": "until_resumed"},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "off"}, override=True))])

    async def inputs() -> None:
        if override:
            await hass.services.async_call("input_boolean", "turn_on", {"entity_id": FLAG}, blocking=True)
        hass.states.async_set(TV, tv)
        zones(hass, a1, a2)
        if lit:
            for light in LIGHTS:
                hass.states.async_set(light, "on", {**hass.states.get(light).attributes, "brightness": 128,
                                                    "color_mode": "color_temp", "color_temp_kelvin": 2700})

    _automations, x = await setup(hass, lights, d, helpers=True, mood=mood_value, before=inputs)
    await lights.settle()
    lights.taken()
    gen = x.generators["g_00000001"]
    states = {s.entity_id: {"state": s.state, "attributes": dict(s.attributes), "held": 10, "age": 10}
              for s in hass.states.async_all()}
    ev = simulate.Evaluator(states, {"time": "12:00:00", "weekday": "mon"}, x.refs.evaluators, lambda e: e)
    expected = []
    for light in LIGHTS:
        predicted = L2.evaluate_plan(gen.plans[light], ev, states.get(light))["command"]
        if predicted["service"]:
            expected.append((f"light.{predicted['service']}", (light,), predicted["data"]))
        hass.bus.async_fire(L2.EVENT, {"light": light})
        await lights.settle()
    assert lights.light_calls() == expected
    for light in LIGHTS:  # and once it is applied, the next evaluation sends nothing
        hass.bus.async_fire(L2.EVENT, {"light": light})
    await lights.settle()
    assert lights.light_calls() == []


# ------------------------------------------------------------------ more switch sources and priorities
async def test_latched_switch_as_action_and_as_matrix_source(lights, hass):
    """A wall switch's state is a Truth Matrix source for L1, and its switching on is an action for L2."""
    hass.states.async_set("switch.wall", "off")
    d = spec_designer(truth={"matrices": [{"id": "m_0001", "name": "Wall", "policy": None, "ranks": [], "columns": [
        {"id": "c_e001", "kind": "state", "entity_id": "switch.wall", "state": "on"},
        {"id": "c_e002", "kind": "state", "entity_id": "switch.wall", "state": "off"}],
        "cells": {L1: {"c_e001": {"kind": "on"}, "c_e002": {"kind": "off"}}}}]}, brightness={"matrices": []},
        switches=[{"id": "w_0002", "name": "Wall", "kind": "latched", "entity_id": "switch.wall", "interactions": [
            {"id": "i_0011", "kind": "on", "trigger": {"type": "state", "entity_id": "switch.wall", "to": "on", "from": "off"}}],
            "mappings": [mapping("p_0011", "i_0011", {"kind": "brightness", "pct": 40}, light=LL2)]}])
    await setup(hass, lights, d)
    hass.states.async_set("switch.wall", "on")
    await lights.settle()
    assert lights.light(L1)[0] == "on" and lights.light(LL2)[:2] == ("on", 102)
    lights.taken()
    hass.states.async_set("switch.wall", "unavailable")             # a switch that drops off the network
    hass.states.async_set("switch.wall", "on")
    await lights.settle()
    assert not [c for c in lights.light_calls() if LL2 in c[1]]       # coming back is no "switched on"
    hass.states.async_set("switch.wall", "off")
    await lights.settle()
    assert lights.light(L1)[0] == "off" and lights.light(LL2)[0] == "on"


@pytest.mark.parametrize("expected_lingering_timers", [True])   # MQTT's own periodic housekeeping
async def test_zigbee2mqtt_action_over_mqtt(lights, hass, mqtt_mock):
    from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
    d = spec_designer(switches=[{"id": "w_0003", "name": "Z2M", "kind": "momentary", "interactions": [
        {"id": "i_0021", "kind": "double", "trigger": {"type": "mqtt", "topic": "zigbee2mqtt/Wall/action", "payload": "double"}}],
        "mappings": [mapping("p_0021", "i_0021", {"kind": "off"})]}])
    await setup(hass, lights, d)
    zones(hass, False, True)
    await lights.settle()
    lights.taken()
    async_fire_mqtt_message(hass, "zigbee2mqtt/Wall/action", "single")
    await lights.settle()
    assert lights.light_calls() == []
    async_fire_mqtt_message(hass, "zigbee2mqtt/Wall/action", "double")
    await lights.settle()
    assert lights.light_calls() == [("light.turn_off", (L1,), {})]


async def test_switch_selects_a_mood_and_the_lights_follow(lights, hass):
    d = spec_designer(mood=MOODS, switches=[switch(mapping("p_0031", "i_0002", {"kind": "select_mood", "value": "Dagur"}, light=L3))])
    d["switches"][0]["mappings"][0]["lights"] = []
    hass.states.async_set("event.wall", "unknown", {"event_types": ["double", "hold", "release"]})
    await setup(hass, lights, d, mood="Hygge")
    zones(hass, False, True)
    await lights.settle()
    assert lights.light(L3)[2] == 2700
    press(hass, "double")
    await lights.settle()
    assert hass.states.get(MOOD).state == "Dagur" and lights.light(L3)[2] == 5000


@pytest.mark.parametrize("order,expected", [(None, "off"), (["override", "advanced", "mood", "brightness", "truth"], "on")])
async def test_advanced_rule_above_a_switch_override_by_default_and_below_on_request(lights, hass, order, expected):
    """A switch override vs. an Advanced Rule: the configured priority decides, not the fact that a switch is physical."""
    d = spec_designer(advanced=[{**TV_RULE, "lights": [L1]}], override={"policy": "until_resumed"},
                      switches=[switch(mapping("p_0001", "i_0001", {"kind": "on"}, override=True))])
    if order:
        d["priority"] = order
    await setup(hass, lights, d, helpers=True)
    zones(hass, True, True)                       # the matrices say OFF for L1
    await lights.settle()
    hass.bus.async_fire("zha_event", {"device_ieee": "00:11", "command": "toggle"})   # override: ON
    await lights.settle()
    assert lights.light(L1)[0] == "on"
    hass.states.async_set(TV, "playing")
    await lights.settle()
    assert lights.light(L1)[0] == expected
