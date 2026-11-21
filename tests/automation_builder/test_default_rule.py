"""The optional Default Rule in Home Assistant's own engine: it runs when one of the device's triggers fired and no
rule matched - and only then."""

from __future__ import annotations

import json

from ab import compiler, diagnostics
import builders as B

LAMP = "light.lamp"
MOTION = "binary_sensor.motion"
LUX = "sensor.lux"
LATE = "input_boolean.late"
OCC = "binary_sensor.sod_kamar_occupied"
OFF = ("light.turn_off", (LAMP,), {})


def on(pct: int):
    return ("light.turn_on", (LAMP,), {"brightness_pct": pct})


def device(*rules, default=None, **extra):
    return B.device(1, "Lamp", [LAMP], list(rules), default_rule=default, **extra)


def example(default=None, **extra):
    """IF motion AND lux < 100 -> 80 %; IF motion AND late -> 10 %; DEFAULT -> off."""
    return device(
        B.rule(1, "Dark", [B.state(MOTION)], [B.call("light.turn_on", brightness_pct=80)],
               B.all_of(B.c_state(MOTION, "on"), B.c_numeric(LUX, below=100))),
        B.rule(2, "Late", [B.state(MOTION)], [B.call("light.turn_on", brightness_pct=10)],
               B.all_of(B.c_state(MOTION, "on"), B.c_state(LATE, "on"))),
        default=default, **extra)


DEFAULT_OFF = {"enabled": True, "actions": [B.call("light.turn_off")]}


async def flip(house, value: str) -> list:
    house.set(MOTION, value)
    await house.settle()
    return house.taken()


async def test_default_runs_only_when_no_rule_matches(house, hass):
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (LUX, "500"), (LATE, "off")):
        house.set(entity, value)
    automations = await house.load(example(DEFAULT_OFF))
    assert len(automations) == 1 and automations[0]["mode"] == "queued" and automations[0]["default"] is True
    assert len(hass.states.async_all("automation")) == 1

    assert await flip(house, "on") == [OFF]            # bright, not late: no rule matches -> the default
    house.set(LUX, "40")
    assert await flip(house, "off") == [OFF]           # motion ended: no rule matches -> the default
    assert await flip(house, "on") == [on(80)]         # the first rule matches: the default stays quiet
    house.set(LATE, "on")
    await flip(house, "off")
    house.taken()
    assert await flip(house, "on") == [on(80), on(10)]  # both rules match: both run, in order, no default
    house.set(LUX, "400")
    await flip(house, "off")
    house.taken()
    assert await flip(house, "on") == [on(10)]         # only the second
    # the conditions of the rules changing (without any trigger) never calls the default: nothing is enforced
    house.set(LATE, "off")
    house.set(LUX, "900")
    await house.advance(120)
    assert house.taken() == []


async def test_default_decides_before_the_rules_act(house):
    """A rule whose own action changes what its condition looks at must not make the default fire afterwards."""
    dev = device(B.rule(1, "On when off", [B.state(MOTION, to="on")], [B.call("light.turn_on")], B.all_of(B.c_state(LAMP, "off"))),
                 default=DEFAULT_OFF)
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    await house.load(dev)
    assert await flip(house, "on") == [("light.turn_on", (LAMP,), {})]   # the lamp is on now; the default did not undo it
    await flip(house, "off")
    assert await flip(house, "on") == [OFF]                               # lamp on: the rule does not match -> default


async def test_disabled_or_empty_default_changes_nothing(house):
    plain = compiler.compile_device(example(), B.settings())["automations"]
    for default in (None, {"enabled": False, "actions": [B.call("light.turn_off")]}, {"enabled": True, "actions": []}):
        if default and default["enabled"] and not default["actions"]:
            continue  # an enabled default needs an action (refused by the model, see the App tests)
        compiled = compiler.compile_device(example(default), B.settings())["automations"]
        assert [a["config"] for a in compiled] == [a["config"] for a in plain]
    for entity, value in ((LAMP, "on"), (MOTION, "off"), (LUX, "500"), (LATE, "off")):
        house.set(entity, value)
    await house.load(example({"enabled": False, "actions": [B.call("light.turn_off")]}))
    assert await flip(house, "on") == []


async def test_default_respects_a_manual_override_signal(house):
    for entity, value in ((LAMP, "on"), (MOTION, "off"), (LUX, "500"), (LATE, "off"), ("input_boolean.manual", "on")):
        house.set(entity, value)
    await house.load(example({**DEFAULT_OFF, "override_entity": "input_boolean.manual"}))
    assert await flip(house, "on") == []
    house.set("input_boolean.manual", "off")
    assert await flip(house, "off") == [OFF]


async def test_default_with_waiting_rules_and_occupancy_rechecks(house, hass, hass_ws_client):
    """A waiting rule has its own automation, but when it matches the default stays quiet; occupancy rechecks and
    the periodic check never call for the default."""
    dev = device(
        B.rule(1, "Occupied", [B.occupancy(OCC)], [B.call("light.turn_on")], B.all_of(B.c_numeric(LUX, below=100))),
        B.rule(2, "Stairs", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=30), B.delay(20), B.call("light.turn_off")],
               B.all_of(B.c_state(LATE, "on")), retrigger="restart"),
        default={"enabled": True, "actions": [B.call("light.turn_on", brightness_pct=1)]})
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (LUX, "500"), (LATE, "on")):
        house.set(entity, value)
    house.set(OCC, "off", fresh=True)
    shared, own = await house.load(dev)
    assert (shared["role"], own["role"], own["mode"]) == ("shared", "separate", "restart")
    assert len(shared["config"]["actions"]) == 2          # the wrapper and the recheck block
    assert [b.get("path") for b in shared["blocks"]] == ["action/0/then/0", None]
    await house.advance(301)
    assert await flip(house, "on") == [on(30)]             # the waiting rule matches (late): no default
    await house.advance(25)
    assert house.taken() == [OFF]
    house.set(LATE, "off")
    await flip(house, "off")
    assert await flip(house, "on") == [on(1)]              # it does not match now: the default, from the shared automation
    house.set(OCC, "on", fresh=True)                       # occupied while bright: the rule does not match -> default once
    await house.settle()
    assert house.taken() == [on(1)]
    await house.advance(400)                               # 2 s, 15 s and periodic checks: never the default
    assert house.taken() == []
    # the diagnostics still find the rule inside the wrapper
    ws = await hass_ws_client(hass)
    house.set(LUX, "50")
    house.set(OCC, "off", fresh=True)
    await house.settle()
    house.taken()
    house.set(OCC, "on", fresh=True)
    await house.settle()
    assert house.taken() == [("light.turn_on", (LAMP,), {})]
    await ws.send_json({"id": 1, "type": "trace/list", "domain": "automation", "item_id": shared["id"]})
    listed = sorted((await ws.receive_json())["result"], key=lambda t: t["timestamp"]["start"])
    await ws.send_json({"id": 2, "type": "trace/get", "domain": "automation", "item_id": shared["id"], "run_id": listed[-1]["run_id"]})
    trace = json.loads(json.dumps((await ws.receive_json())["result"], default=str))
    entries = diagnostics.interpret(trace, shared["blocks"], {r["id"]: r for r in dev["rules"]})
    assert [(e["rule_id"], e["kind"], e["executed"]) for e in entries] == [("r_00000001", "rule", True)]
    assert entries[0]["calls"][0]["action"] == "light.turn_on"
