"""Import and diagnostics against a real Home Assistant.

* An imported automation and its original are loaded side by side: under the same events they must make exactly
  the same service calls.
* The traces Home Assistant records of a generated automation are interpreted per rule (triggered, blocked by
  which condition, executed, failed), and the reported entity state is compared with what a rule commanded.
"""

from __future__ import annotations

import json

from homeassistant.setup import async_setup_component

from ab import compiler, diagnostics, importer, model
import builders as B

LAMP = "light.lamp"
LAMP2 = "light.lamp_2"
MOTION = "binary_sensor.motion"
DOOR = "binary_sensor.door"
LUX = "sensor.lux"
MOOD = "input_select.mood"

ORIGINALS = [
    {"id": "1001", "alias": "Motion light", "mode": "single",
     "triggers": [{"trigger": "state", "entity_id": [MOTION, DOOR], "to": "on", "id": "in"},
                  {"trigger": "state", "entity_id": MOTION, "to": "off", "for": "00:00:10", "id": "out"}],
     "conditions": [{"condition": "or", "conditions": [
         {"condition": "numeric_state", "entity_id": LUX, "below": 100},
         {"condition": "state", "entity_id": MOOD, "state": ["Hygge", "Nátt"]}]}],
     "actions": [{"choose": [
         {"conditions": [{"condition": "trigger", "id": "in"}],
          "sequence": [{"action": "light.turn_on", "target": {"entity_id": [LAMP, LAMP2]}, "data": {"brightness_pct": 60}}]}],
         "default": [{"service": "light.turn_off", "entity_id": LAMP}]},
         {"condition": "state", "entity_id": MOOD, "state": "Nátt"},
         {"action": "light.turn_off", "target": {"entity_id": LAMP2}, "data": {"transition": 2}}]},
    {"id": "1002", "alias": "Stairs", "mode": "restart",
     "trigger": [{"platform": "state", "entity_id": "binary_sensor.stairs", "to": "on"}],
     "action": [{"service": "light.turn_on", "data": {"entity_id": "light.stairs"}},
                {"wait_for_trigger": [{"platform": "state", "entity_id": "binary_sensor.stairs", "to": "off"}],
                 "timeout": {"seconds": 30}, "continue_on_timeout": False},
                {"delay": "00:00:05"},
                {"service": "light.turn_off", "target": {"entity_id": "light.stairs"}}]},
]


def convert(config: dict, number: int) -> dict:
    result = importer.analyse(config, entity_id=f"automation.x{number}", devices={}, settings=B.settings(),
                              state={"state": "on", "attributes": {"id": config["id"], "friendly_name": config["alias"]}})
    assert result["can_convert"], result["unsupported"]
    rule = {**result["rule"], "id": f"r_{number:08x}"}
    return B.device(number, config["alias"], [c["entity_id"] for c in result["controlled"]], [rule])


async def test_imported_copy_behaves_like_the_original(house, hass):
    devices = [convert(config, i + 1) for i, config in enumerate(ORIGINALS)]
    assert [model.waits(d["rules"][0]) for d in devices] == [False, True]
    copies = [a for d in devices for a in compiler.compile_device(d, B.settings())["automations"]]
    assert [(a["role"], a["mode"]) for a in copies] == [("shared", "queued"), ("separate", "restart")]
    for entity, value in ((LAMP, "off"), (LAMP2, "off"), (MOTION, "off"), (DOOR, "off"), (LUX, "50"), (MOOD, "Dagur"),
                          ("binary_sensor.stairs", "off"), ("light.stairs", "off")):
        house.set(entity, value)
    for domain, services in house.SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, house._record)  # noqa: SLF001
    assert await async_setup_component(hass, "automation", {"automation": [*ORIGINALS, *(compiler.with_id(a) for a in copies)]})
    await hass.async_block_till_done()
    assert len(hass.states.async_all("automation")) == 4
    house.apply = False  # the calls are compared; the test sets every state itself

    async def step(entity: str, value: str, wait: float = 0) -> list:
        house.set(entity, value)
        await house.settle()
        if wait:
            await house.advance(wait)
        calls = sorted(json.dumps(c, sort_keys=True) for c in house.taken())
        assert calls[0::2] == calls[1::2], f"{entity}={value}: original and copy differ: {calls}"
        return [json.loads(c) for c in calls[0::2]]

    assert await step(MOTION, "on") == [["light.turn_on", [LAMP, LAMP2], {"brightness_pct": 60}]]
    assert await step(DOOR, "on") == [["light.turn_on", [LAMP, LAMP2], {"brightness_pct": 60}]]
    assert await step(LUX, "500") == []
    assert await step(MOTION, "off", wait=11) == []           # too bright and the mood is Dagur: the OR fails
    assert await step(MOOD, "Nátt") == []
    assert await step(MOTION, "on") == [["light.turn_off", [LAMP2], {"transition": 2}],
                                         ["light.turn_on", [LAMP, LAMP2], {"brightness_pct": 60}]]
    assert await step(MOTION, "off", wait=11) == [["light.turn_off", [LAMP], {}], ["light.turn_off", [LAMP2], {"transition": 2}]]
    assert await step(MOOD, "Hygge") == []
    assert await step(MOTION, "on") == [["light.turn_on", [LAMP, LAMP2], {"brightness_pct": 60}]]  # the guard stops the rest
    # the waiting automation: wait, timeout that aborts, restart
    assert await step("binary_sensor.stairs", "on") == [["light.turn_on", ["light.stairs"], {}]]
    assert await step("binary_sensor.stairs", "off", wait=6) == [["light.turn_off", ["light.stairs"], {}]]
    assert await step("binary_sensor.stairs", "on", wait=40) == [["light.turn_on", ["light.stairs"], {}]]  # timeout: stop
    assert await step("binary_sensor.stairs", "off", wait=10) == []
    assert await step("binary_sensor.stairs", "on", wait=2) == [["light.turn_on", ["light.stairs"], {}]]
    assert await step("binary_sensor.stairs", "off", wait=2) == []
    assert await step("binary_sensor.stairs", "on", wait=1) == [["light.turn_on", ["light.stairs"], {}]]  # restarted
    assert await step("binary_sensor.stairs", "off", wait=6) == [["light.turn_off", ["light.stairs"], {}]]


async def traces(hass, hass_ws_client, automation_id: str) -> list[dict]:
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "trace/list", "domain": "automation", "item_id": automation_id})
    listed = (await ws.receive_json())["result"]
    out = []
    for i, item in enumerate(sorted(listed, key=lambda t: t["timestamp"]["start"])):
        await ws.send_json({"id": 10 + i, "type": "trace/get", "domain": "automation", "item_id": automation_id,
                            "run_id": item["run_id"]})
        out.append((await ws.receive_json())["result"])
    return json.loads(json.dumps(out, default=str))


async def test_rule_level_interpretation_of_real_traces(house, hass, hass_ws_client):
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Motion", [B.state(MOTION, to="on")], [B.call("light.turn_on", brightness_pct=70)],
               B.all_of(B.c_numeric(LUX, below=100), B.c_state(MOOD, "Hygge", negate=True))),
        B.rule(2, "Door", [B.state(DOOR, to="on")], [B.call("light.turn_off")]),
        B.rule(3, "Broken", [B.state("binary_sensor.x", to="on")], [B.call("light.set_nothing", target="none")]),
    ])
    for entity, value in ((LAMP, "off"), (MOTION, "off"), (DOOR, "off"), (LUX, "50"), (MOOD, "Hygge"), ("binary_sensor.x", "off")):
        house.set(entity, value)
    automation = (await house.load(device))[0]
    rules = {r["id"]: r for r in device["rules"]}

    async def run(entity: str) -> dict:
        house.set(entity, "off")
        await house.settle()
        house.freezer.tick(1)
        house.set(entity, "on")
        await house.settle()
        trace = (await traces(hass, hass_ws_client, automation["id"]))[-1]
        return {e["rule_id"]: e for e in diagnostics.interpret(trace, automation["blocks"], rules)}

    found = await run(MOTION)                     # triggered, blocked by the second condition
    assert set(found) == {"r_00000001"}
    assert found["r_00000001"]["executed"] is False and found["r_00000001"]["outcome"] == "blocked"
    assert found["r_00000001"]["blocked_by"] == "input_select.mood is not “Hygge”"
    house.set(MOOD, "Dagur")
    house.set(LUX, "400")
    found = await run(MOTION)                     # blocked by the first
    assert found["r_00000001"]["blocked_by"] == "sensor.lux is below 100"
    house.set(LUX, "40")
    found = await run(MOTION)                     # executed
    entry = found["r_00000001"]
    assert entry["executed"] and entry["outcome"] == "finished" and entry["blocked_by"] is None
    assert entry["calls"] == [{"at": entry["calls"][0]["at"], "action": "light.turn_on", "entities": [LAMP],
                               "data": {"brightness_pct": 70}}]
    # the lamp reports "on" with the run's own context: confirmed (the house sets no brightness: not reported)
    expected = diagnostics.expectations(entry["calls"])
    check = diagnostics.verify(LAMP, expected[LAMP], hass.states.get(LAMP).as_dict(), entry["context_id"])
    assert check["verdict"] == "mismatch" and "brightness" in check["detail"]
    hass.states.async_set(LAMP, "on", {"brightness": 178}, context=hass.states.get(LAMP).context)
    check = diagnostics.verify(LAMP, expected[LAMP], json.loads(json.dumps(hass.states.get(LAMP).as_dict(), default=str)),
                               entry["context_id"])
    assert check["verdict"] == "confirmed", check

    found = await run(DOOR)                       # another rule of the same automation
    assert set(found) == {"r_00000002"} and found["r_00000002"]["calls"][0]["action"] == "light.turn_off"
    house.apply = False                           # a command that is lost: the lamp keeps reporting "on"
    hass.states.async_set(LAMP, "on")
    await house.settle()
    found = await run(DOOR)
    entry = found["r_00000002"]
    state = json.loads(json.dumps(hass.states.get(LAMP).as_dict(), default=str))
    check = diagnostics.verify(LAMP, diagnostics.expectations(entry["calls"])[LAMP], state, entry["context_id"])
    assert check["verdict"] == "mismatch" and check["expected"] == "“off”" and check["reported"] == "“on”"
    house.freezer.tick(2)
    hass.states.async_set(LAMP, "on", {"brightness": 10})   # somebody else touched it afterwards
    await house.settle()
    state = json.loads(json.dumps(hass.states.get(LAMP).as_dict(), default=str))
    check = diagnostics.verify(LAMP, diagnostics.expectations(entry["calls"])[LAMP], state, entry["context_id"])
    assert check["verdict"] == "changed_since"


async def test_failed_and_waiting_runs_and_rechecks_in_traces(house, hass, hass_ws_client, caplog):
    OCC = "binary_sensor.sod_kamar_occupied"
    device = B.device(1, "Lamp", [LAMP], [
        B.rule(1, "Occupied", [B.occupancy(OCC)], [B.call("light.turn_on")]),
        B.rule(2, "Stairs", [B.state(MOTION, to="on")], [B.call("light.turn_on"), B.delay(30), B.call("light.turn_off")]),
    ])
    house.set(LAMP, "off")
    house.set(MOTION, "off")
    house.set(OCC, "off", fresh=True)
    shared, own = await house.load(device)
    rules = {r["id"]: r for r in device["rules"]}
    await house.advance(301)
    house.apply = False
    house.set(OCC, "on", fresh=True)
    await house.advance(3)                        # the first run and the 2-second recheck (the command was lost)
    house.apply = True
    await house.advance(14)                       # the 15-second recheck repeats it once more, then the lamp is on
    await house.advance(300)                      # periodic checks: nothing to do
    entries = [e for t in await traces(hass, hass_ws_client, shared["id"])
               for e in diagnostics.interpret(t, shared["blocks"], rules)]
    kinds = [(e["kind"], e["executed"], e["blocked_by"]) for e in entries]
    assert kinds[0] == ("rule", True, None)
    assert kinds[1] == ("recheck", True, None) and kinds[2] == ("recheck", True, None)
    assert kinds[3:] and all(k == ("recheck", False, diagnostics.RECHECK_LAST) for k in kinds[3:])

    house.set(MOTION, "on")
    await house.advance(5)
    running = diagnostics.interpret((await traces(hass, hass_ws_client, own["id"]))[-1], own["blocks"], rules)[0]
    assert running["outcome"] == "running" and [c["action"] for c in running["calls"]] == ["light.turn_on"]
    await house.advance(30)
    done = diagnostics.interpret((await traces(hass, hass_ws_client, own["id"]))[-1], own["blocks"], rules)[0]
    assert done["outcome"] == "finished" and [c["action"] for c in done["calls"]] == ["light.turn_on", "light.turn_off"]
