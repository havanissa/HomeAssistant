"""End-to-end checks of Phase 0.2: the real App image against a real Home Assistant.

    python e2e_02_checks.py <tokens.json> [--stop-app "<cmd>"] [--start-app "<cmd>"] [--kill-app "<cmd>"]
                            [--restart-ha "<cmd>"] [--out results.json]

Same targets and environment variables as ``e2e_checks.py`` (the local stack, or a Home Assistant OS through
``haos.py``). It can run on a fresh installation or after ``e2e_checks.py``; it uses its own devices.

What it does in the real Home Assistant, with the test's own administrator (never the App): changes helper
states, presses the demo button, fires events, turns the *original* of an imported automation off (the App cannot
and does not), and creates an automation from the exported blueprint.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

from e2e_checks import (HA, INGRESS, READ_COMMANDS, STACK, TARGET, App, Home, brightness_pct, call, check, request,
                        results, skip, summary, t_state)

KITCHEN = "light.kitchen_lights"
CEILING = "light.ceiling_lights"
OFFICE = "light.office_rgbw_lights"
FAN = "fan.living_room_fan"
DECOR = "switch.decorative_lights"
COVER = "cover.kitchen_window"
BED = "light.bed_light"
FLAKY = "light.flaky_lamp"
MOTION = "binary_sensor.test_motion"
DOOR = "binary_sensor.test_door"
LUX = "sensor.test_lux"
MOOD = "input_select.mood"
BUTTON = "event.push_button_press"
NOTIFIER = "notify.notifier"
IMPORTABLE, NOT_IMPORTABLE, EVERY_CHANGE = "1700000000002", "1700000000003", "1700000000001"


def numeric(entity_id: str, above: Any = None, below: Any = None) -> dict[str, Any]:
    return {"type": "numeric", "entity_id": entity_id, "above": above, "below": below}


def c_state(entity_id: str, state: str, negate: bool = False) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "state": state, "negate": negate, "for_seconds": 0}


def group(kind: str, *items: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "items": list(items)}


def press(home: Home, kind: str = "pressed") -> None:
    """The demo button reports an event (an event entity's state is the time of its last event)."""
    stamp = datetime.now(UTC).isoformat(timespec="milliseconds")
    status, _ = request(f"{HA}/api/states/{BUTTON}", "POST", {"state": stamp, "attributes": {
        "event_types": ["pressed"], "event_type": kind, "friendly_name": "Push Button press"}}, home.headers)
    assert status in (200, 201), status


def fire(home: Home, event_type: str, **data: Any) -> None:
    status, _ = request(f"{HA}/api/events/{event_type}", "POST", data, home.headers)
    assert status == 200, status


def select(home: Home, option: str) -> None:
    home.call("input_select.select_option", MOOD, option=option)


def diag(app: App, device_id: str, rule_id: str) -> dict[str, Any]:
    return app.device(device_id)["rule_diagnostics"][rule_id]


def wait_diag(app: App, device_id: str, rule_id: str, accept: Any, timeout: float = 20) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        found = diag(app, device_id, rule_id)
        if accept(found) or time.monotonic() > deadline:
            return found
        time.sleep(1)


def run(command: str) -> None:
    subprocess.run(command, shell=True, check=True, capture_output=True)  # noqa: S602 - the test's own command


def wait_app(app: App, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, overview = app.get("api/overview")
            if status == 200 and overview["connection"]["home_assistant"]["connected"]:
                return True
        except OSError:
            pass
        time.sleep(1)
    return False


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--stop-app", default="docker stop -t 10 ab-test-app")
    parser.add_argument("--start-app", default="docker start ab-test-app")
    parser.add_argument("--kill-app", default="docker kill ab-test-app")
    parser.add_argument("--restart-ha", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    tokens = json.loads(Path(args.tokens).read_text())
    home, app = Home(tokens["llat"]), App()
    originals_before = {i: home.automation(i)[1] for i in (EVERY_CHANGE, IMPORTABLE, NOT_IMPORTABLE)}
    for state in home.automations():  # an earlier run may have left an original turned off (the test's own step)
        if str(state["attributes"].get("id")) in originals_before and state["state"] != "on":
            home.call("automation.turn_on", state["entity_id"])
    status, overview = app.get("api/overview")
    check("20 the App answers and is connected", status == 200 and overview["connection"]["home_assistant"]["connected"], overview.get("connection"))
    check("20 this is version 0.2.0", overview["app"]["version"] == "0.2.0", overview["app"])
    for helper in ("test_motion", "ab_door", "guest_mode", "drop_commands"):
        home.set(helper, False)
    home.lux(400)
    select(home, "Dagur")
    for light in (KITCHEN, CEILING, OFFICE, FLAKY):
        home.call("light.turn_off", light)
    home.call("fan.turn_off", FAN)
    home.call("switch.turn_off", DECOR)
    time.sleep(1)

    # ------------------------------------------------- 21. buttons, events and device triggers
    _, found = app.get("api/entities?purpose=button&q=")
    check("21 event entities are offered as buttons and remotes", BUTTON in [e["entity_id"] for e in found["results"]], found)
    _, detail = app.get(f"api/entity?ids={BUTTON}")
    check("21 the presses a button reports are read from Home Assistant", detail["entities"][BUTTON]["attributes"].get("event_types") == ["pressed"], detail)
    _, devices = app.get("api/all-devices?q=bed%20light")
    bed_device = next((d["device_id"] for d in devices["devices"] if d["name"] == "Bed Light"), None)
    status, offered = app.get(f"api/device-automations?kind=trigger&device_id={bed_device}")
    turned_on = next((i for i in offered.get("items", []) if i["config"].get("type") == "turned_on"), None)
    check("21 device triggers are discovered from Home Assistant", status == 200 and turned_on is not None, offered)
    status, none = app.get("api/device-automations?kind=trigger&device_id=does-not-exist")
    check("21 an unknown device gives a clear error, nothing invented", status == 502 and "could not list" in none["message"], none)

    decor = app.add_device("Decor", [DECOR])
    r_press = app.add_rule(decor, "Button", [{"type": "button", "entity_id": BUTTON, "event_types": ["pressed"]}], [call("switch.toggle")])
    r_event = app.add_rule(decor, "Event", [{"type": "event", "event_type": "ab_test_event", "event_data": {"button": "left"}}], [call("switch.turn_on")])
    r_device = app.add_rule(decor, "Bed light turned on", [{"type": "device", "config": turned_on["config"], "label": turned_on["label"]}],
                            [call("switch.turn_off")])
    view = app.device(decor)
    check("21 readable summaries of the new triggers", view["summaries"][r_press]["if"] == "Push Button press reports “pressed”"
          and view["summaries"][r_event]["if"] == "the event ab_test_event happens (button: left)", view["summaries"])
    status, deployed = app.deploy(decor)
    check("21 one automation with a button, an event and a device trigger deploys", status == 200 and deployed["deployed"], deployed)
    press(home)
    check("21 a real state change of the event entity runs the button rule", home.wait(DECOR, "on", 5))
    press(home)
    check("21 ...every press counts", home.wait(DECOR, "off", 5))
    press(home, "something_else")
    check("21 ...another kind of press does not", home.stays(DECOR, "off", 2))
    fire(home, "ab_test_event", button="right")
    check("21 an event with other data does nothing", home.stays(DECOR, "off", 2))
    fire(home, "ab_test_event", button="left", extra=1)
    check("21 a real Home Assistant event runs the event rule", home.wait(DECOR, "on", 5))
    home.call("light.turn_off", BED)
    time.sleep(1)
    home.call("light.turn_on", BED)
    check("21 a native device trigger runs its rule", home.wait(DECOR, "off", 5))

    # ------------------------------------------------- 22. nested conditions
    fan = app.add_device("Fan", [FAN])
    r_nested = app.add_rule(fan, "Motion", [t_state(MOTION, to="on")], [call("fan.turn_on")], [
        group("or", numeric(LUX, below=100), c_state(MOOD, "Nátt")),
        group("not", c_state("input_boolean.guest_mode", "on")),
        {"type": "template", "value_template": "{{ states('input_number.script_level') | float(0) < 50 }}"}])
    r_wait = app.add_rule(fan, "Off when bright", [t_state(MOTION, to="off")], [
        {"type": "wait_condition", "conditions": group("and", numeric(LUX, above=800)), "timeout_seconds": 6, "on_timeout": "stop"},
        call("fan.turn_off")], retrigger="restart")
    view = app.device(fan)
    check("22 nested conditions read naturally", view["summaries"][r_nested]["and"] ==
          "(Test lux is below 100 OR Mood is “Nátt”) AND none of (Guest mode is “on”) AND the template "
          "{{ states('input_number.script_level') | float(0) < 50 }} is true", view["summaries"][r_nested]["and"])
    automations = {a["role"]: a for a in view["live"]["automations"]}
    check("22 the waiting rule is separated, with the reason; the shared automation stays queued",
          automations["shared"]["mode"] == "queued" and automations["separate"]["mode"] == "restart"
          and "its own automation because it waits" in automations["separate"]["reason"], view["live"]["automations"])
    status, _ = app.deploy(fan)
    check("22 both automations deploy", status == 200 and len([m for m in home.managed() if fan in m]) == 2, home.managed())

    def motion() -> None:
        home.set("test_motion", False)
        time.sleep(1.2)
        home.set("test_motion", True)

    motion()
    check("22 OR group false (bright, not Nátt): nothing", home.stays(FAN, "off", 2))
    select(home, "Nátt")
    motion()
    check("22 OR group true through the mood: the fan starts", home.wait(FAN, "on", 5))
    home.call("fan.turn_off", FAN)
    home.set("guest_mode", True)
    motion()
    check("22 NOT group: guest mode blocks it", home.stays(FAN, "off", 2))
    home.set("guest_mode", False)
    select(home, "Dagur")
    home.lux(40)
    home.call("input_number.set_value", "input_number.script_level", value=80)
    motion()
    check("22 the template condition blocks it", home.stays(FAN, "off", 2))
    home.call("input_number.set_value", "input_number.script_level", value=0)
    motion()
    check("22 everything holds: the fan starts", home.wait(FAN, "on", 5))
    # wait for a condition, with a timeout that stops
    home.set("test_motion", False)
    check("22 wait for condition: still waiting, nothing happens", home.stays(FAN, "on", 3))
    home.lux(900)
    check("22 ...the condition comes true: the rule continues", home.wait(FAN, "off", 5))
    home.call("fan.turn_on", FAN)
    home.lux(40)
    home.set("test_motion", True)
    time.sleep(1.2)
    home.set("test_motion", False)
    check("22 ...a timeout that stops: the action after the wait does not run", home.stays(FAN, "on", 9))
    home.call("fan.turn_off", FAN)

    # ------------------------------------------------- 23. branches, wait for trigger, repeat
    kitchen = app.add_device("Kitchen", [KITCHEN])
    r_door = app.add_rule(kitchen, "Door", [t_state(DOOR, to="on")], [
        {"type": "choose", "branches": [{"conditions": group("and", numeric(LUX, below=100)), "actions": [call("light.turn_on", brightness_pct=30)]}],
         "default": [call("light.turn_on", brightness_pct=80)]},
        {"type": "wait_trigger", "triggers": [t_state(DOOR, to="off")], "timeout_seconds": 8, "on_timeout": "continue"},
        {"type": "delay", "seconds": 2},
        call("light.turn_off")], retrigger="restart")
    r_guest = app.add_rule(kitchen, "Guest", [t_state("input_boolean.guest_mode", to="on")], [
        {"type": "if", "conditions": group("and", c_state(KITCHEN, "on")), "then": [call("light.turn_off")],
         "else": [{"type": "repeat", "mode": "count", "count": 3, "conditions": group("and"), "actions": [call("light.toggle")]}]}])
    view = app.device(kitchen)
    codes = {c["code"]: c["level"] for c in view["conflicts"]}
    check("23 a later action of a waiting rule on the same lamp is explained, not blocked", codes.get("late_action") == "warning" and view["blocking"] == 0, view["conflicts"])
    status, _ = app.deploy(kitchen)
    check("23 branches, waits and repeats deploy (Home Assistant validated them)", status == 200, _)
    home.lux(40)
    home.set("ab_door", True)
    check("23 choose: the dark branch", home.wait(KITCHEN, "on", 5) and brightness_pct(home, KITCHEN) == 30, brightness_pct(home, KITCHEN))
    home.set("ab_door", False)
    check("23 wait for trigger, then the delay, then off", home.stays(KITCHEN, "on", 1) and home.wait(KITCHEN, "off", 6))
    home.lux(500)
    time.sleep(1)
    home.set("ab_door", True)
    check("23 choose: the default branch", home.wait(KITCHEN, "on", 5) and brightness_pct(home, KITCHEN) == 80, brightness_pct(home, KITCHEN))
    check("23 the wait's timeout continues with the next action", home.stays(KITCHEN, "on", 6) and home.wait(KITCHEN, "off", 8))
    home.set("ab_door", False)
    home.set("guest_mode", True)
    check("23 if/else with a repeat: three toggles leave the lamp on", home.wait(KITCHEN, "on", 5))
    home.set("guest_mode", False)
    time.sleep(1)
    home.set("guest_mode", True)
    check("23 if: the lamp is on, so it is turned off", home.wait(KITCHEN, "off", 5))
    home.set("guest_mode", False)

    # ------------------------------------------------- 24. variables, templates, notification, script
    _, targets = app.get("api/notify-targets")
    check("24 notification targets come from Home Assistant", "persistent_notification.create" in targets["actions"]
          and NOTIFIER in [e["entity_id"] for e in targets["entities"]], targets)
    ceiling = app.add_device("Ceiling", [CEILING])
    r_vars = app.add_rule(ceiling, "Hygge", [t_state(MOOD, to="Hygge")], [
        {"type": "variables", "variables": {"level": "{{ [states('sensor.test_lux') | int(0) // 10, 15] | max }}"}},
        {"type": "service", "action": "light.turn_on", "target": "all", "data": {"brightness_pct": "{{ level }}"}},
        {"type": "notify", "action": "notify.send_message", "entity_id": NOTIFIER, "title": "Ceiling", "message": "Level {{ level }}"},
        {"type": "script", "entity_id": "script.ab_test_script", "wait": False, "variables": {"level": "{{ level }}"}}])
    r_slow = app.add_rule(ceiling, "Slow", [t_state(MOOD, to="Nátt")], [
        {"type": "script", "entity_id": "script.ab_slow_script", "wait": True, "variables": {}}, call("light.turn_off")], retrigger="single")
    status, _ = app.deploy(ceiling)
    check("24 variables, templates, a notification and scripts deploy", status == 200, _)
    home.lux(600)
    notified_before = home.value(NOTIFIER)
    home.call("input_number.set_value", "input_number.script_level", value=0)
    home.set("slow_done", False)
    select(home, "Hygge")
    check("24 a variable computed from a template sets the brightness", home.wait(CEILING, "on", 5) and brightness_pct(home, CEILING) == 60, brightness_pct(home, CEILING))
    check("24 the notification was sent through Home Assistant", home.wait(NOTIFIER, lambda s: s["state"] != notified_before, 5))
    check("24 the script ran with the variable", home.wait("input_number.script_level", lambda s: float(s["state"]) == 60, 5), home.value("input_number.script_level"))
    select(home, "Nátt")
    check("24 a script that is waited for: the next action follows only after it", home.stays(CEILING, "on", 2)
          and home.wait("input_boolean.slow_done", "on", 6) and home.wait(CEILING, "off", 4))
    select(home, "Dagur")

    # ------------------------------------------------- 25. rule-level diagnostics and action verification
    d = wait_diag(app, fan, r_nested, lambda x: x["last_blocked"] and x["last_executed"])
    check("25 last triggered, executed and blocked per rule, from Home Assistant's traces",
          d["runs"] >= 5 and d["executions"] >= 2 and d["blocks"] >= 3 and d["last_executed"]["run_id"], {k: d[k] for k in ("runs", "executions", "blocks")})
    check("25 the blocking condition is named", d["last_blocked"]["by"].startswith(("the template", "none of", "(Test lux")), d["last_blocked"])
    check("25 deployment status and entity availability per rule", d["sync"]["status"] == "in_sync" and d["entity_problems"] == 0
          and {e["entity_id"] for e in d["entities"]} >= {FAN, MOTION, LUX, MOOD}, d["sync"])
    d = wait_diag(app, kitchen, r_guest, lambda x: (x["verification"] or {}).get("state") in ("confirmed", "mismatch"))
    check("25 a branch's action is verified against the reported state", d["verification"]["state"] == "confirmed"
          and d["verification"]["checks"][0]["entity_id"] == KITCHEN, d["verification"])
    d = diag(app, decor, r_press)
    check("25 a toggle is reported as not verifiable, not guessed", (d["verification"] or {}).get("state") == "not_verifiable", d["verification"])
    flaky = app.add_device("Flaky", [FLAKY])
    r_flaky = app.add_rule(flaky, "Mood", [t_state(MOOD, to="Hygge")], [call("light.turn_on")])
    app.deploy(flaky)
    home.set("drop_commands", True)
    time.sleep(1)
    select(home, "Hygge")
    d = wait_diag(app, flaky, r_flaky, lambda x: (x["verification"] or {}).get("state") == "mismatch", 25)
    check("25 a lost command: executed, but the reported state does not match - shown as a mismatch",
          d["last_outcome"]["outcome"] == "finished" and d["verification"]["state"] == "mismatch"
          and d["verification"]["checks"][0]["reported"] == "“off”", d["verification"])
    check("25 ...and nothing was commanded from here: the lamp stays as it is", home.stays(FLAKY, "off", 3))
    home.set("drop_commands", False)
    select(home, "Dagur")
    time.sleep(1)
    select(home, "Hygge")
    d = wait_diag(app, flaky, r_flaky, lambda x: (x["verification"] or {}).get("state") == "confirmed", 25)
    check("25 the next run is confirmed", d["verification"]["state"] == "confirmed" and home.value(FLAKY) == "on", d["verification"])
    select(home, "Dagur")
    home.set("occ_available", True)
    app.edit_rule(flaky, r_flaky, name="Mood (renamed)", actions=[call("light.turn_off")])
    check("25 a changed rule shows that Home Assistant still runs the old version", diag(app, flaky, r_flaky)["sync"]["status"] == "pending")

    # ------------------------------------------------- 26. blueprint export
    office = app.add_device("Office light", [OFFICE])
    app.add_rule(office, "Motion when dark", [t_state(MOTION, to="on", for_seconds=2)], [call("light.turn_on", brightness_pct=45)],
                 [numeric(LUX, below=200)])
    app.add_rule(office, "No motion", [t_state(MOTION, to="off", for_seconds=3)], [call("light.turn_off")])
    status, _ = app.deploy(office)
    office_auto = f"automation_builder_{office}"
    deployed_before = home.automation(office_auto)[1]
    status, view = app.get(f"api/blueprint/{office}")
    keys = {i["key"]: i for i in view["inputs"]}
    check("26 the export screen lists what can become an input, with stable identifiers",
          status == 200 and {"controlled_office_rgbw_lights", "binary_sensor_test_motion", "sensor_test_lux"} <= set(keys)
          and any(k.endswith("_a1_brightness_pct") for k in keys) and any(k.endswith("_c1_below") for k in keys), sorted(keys))
    check("26 readable names and suitable selectors", keys["controlled_office_rgbw_lights"]["name"] == "Office RGBW Lights"
          and keys["controlled_office_rgbw_lights"]["selector"] == {"entity": {"domain": "light"}}, keys["controlled_office_rgbw_lights"])
    brightness = next(k for k in keys if k.endswith("_a1_brightness_pct"))
    below = next(k for k in keys if k.endswith("_c1_below"))
    hold = next(k for k in keys if k.endswith("_t1_for") and keys[k]["value"] == 2)
    chosen = {"name": "Motion light", "description": "Turns a light on at motion when it is dark.", "inputs": {
        "controlled_office_rgbw_lights": {"name": "Light", "description": "The light to control"},
        "binary_sensor_test_motion": {"name": "Motion sensor"}, "sensor_test_lux": {}, brightness: {}, below: {}, hold: {}}}
    status, _ = app.change("api/devices/update", {"device_id": office, "patch": {"blueprint": chosen}})
    status, view = app.get(f"api/blueprint/{office}")
    check("26 the choices are saved and the blueprint validates in Home Assistant", view["valid"] and sum(i["exposed"] for i in view["inputs"]) == 6
          and any(v["level"] == "ok" and "Home Assistant accepts" in v["message"] for v in view["validation"]), view["validation"])
    status, text = request(f"{INGRESS}/api/blueprint/{office}/download")
    check("26 download: native blueprint YAML", status == 200 and text.startswith("blueprint:\n  name: Motion light") and "domain: automation" in text
          and "!input 'controlled_office_rgbw_lights'" in text and "automation_builder" not in text.split("triggers:")[1], str(text)[:300])
    status, installed = app.post("api/blueprint/install", {"device_id": office, "overwrite": False})
    path = f"automation_builder/{office}.yaml"
    check("26 installed through Home Assistant's blueprint command", status == 200 and installed["path"] == f"blueprints/automation/{path}", installed)
    status, again = app.post("api/blueprint/install", {"device_id": office, "overwrite": False})
    check("26 installing again replaces a file only with a yes", status == 409 and again["error"] == "exists" and again["installed_by_app"] is True, again)
    status, again = app.post("api/blueprint/install", {"device_id": office, "overwrite": True})
    check("26 ...then it is replaced", status == 200 and again["replaced"] is True, again)
    check("26 exporting changed nothing of the managed automation", home.automation(office_auto)[1] == deployed_before and app.status(office) == "in_sync")
    # an automation made from the blueprint by the user (here: the test), with other entities and values
    status, made = request(f"{HA}/api/config/automation/config/ab_e2e_from_blueprint", "POST", {
        "alias": "From the exported blueprint", "use_blueprint": {"path": path, "input": {
            "controlled_office_rgbw_lights": CEILING, "binary_sensor_test_motion": DOOR, brightness: 20, below: 2000,
            hold: {"hours": 0, "minutes": 0, "seconds": 1}}}}, home.headers)
    check("26 Home Assistant creates an automation from the blueprint", status == 200, made)
    loaded = home.wait("automation.from_the_exported_blueprint", "on", 10)
    check("26 ...and loads it", loaded, home.value("automation.from_the_exported_blueprint"))
    run(args.stop_app)  # the blueprint must work without Automation Builder
    try:
        home.call("light.turn_off", CEILING)
        home.call("light.turn_off", OFFICE)
        home.lux(500)
        time.sleep(1)
        home.set("ab_door", True)
        check("26 the blueprint automation runs with its own inputs while the App is stopped",
              home.wait(CEILING, "on", 6) and brightness_pct(home, CEILING) == 20, brightness_pct(home, CEILING))
        check("26 ...and does not touch the original's light", home.value(OFFICE) == "off")
        home.set("ab_door", False)
        check("26 ...the second rule works too", home.wait(CEILING, "off", 8))
    finally:
        run(args.start_app)
    check("26 the App is back", wait_app(app))
    request(f"{HA}/api/config/automation/config/ab_e2e_from_blueprint", "DELETE", None, home.headers)

    # ------------------------------------------------- 27. import of existing automations
    status, listed = app.get("api/automations")
    names = {a["id"]: a for a in listed["automations"]}
    check("27 the unmanaged automations are listed, the managed ones are not", status == 200 and {EVERY_CHANGE, IMPORTABLE, NOT_IMPORTABLE} <= set(names)
          and not any(str(a["id"]).startswith("automation_builder_") for a in listed["automations"]), sorted(names))
    status, bad = app.post("api/import/analyse", {"entity_id": names[NOT_IMPORTABLE]["entity_id"]})
    reasons = " | ".join(f"{u['what']}: {u['why']}" for u in bad["unsupported"])
    check("27 unsupported constructs are named, and no conversion is offered", status == 200 and not bad["can_convert"]
          and "never a whole area" in reasons and "stop" in reasons, reasons)
    status, refused = app.change("api/import/convert", {"entity_id": names[NOT_IMPORTABLE]["entity_id"], "destination": "new"})
    check("27 ...converting it anyway is refused", status == 409 and refused["error"] == "blocked", refused)
    status, every = app.post("api/import/analyse", {"entity_id": names[EVERY_CHANGE]["entity_id"]})
    check("27 a trigger that cannot be represented exactly is not approximated", not every["can_convert"] and "every change" in every["unsupported"][0]["why"], every["unsupported"])
    status, good = app.post("api/import/analyse", {"entity_id": names[IMPORTABLE]["entity_id"]})
    check("27 a simple automation is analysed: triggers, conditions, actions, controlled entities",
          status == 200 and good["can_convert"] and good["controlled"] == [{"entity_id": COVER, "name": "Kitchen Window"}]
          and good["counts"] == {"triggers": 2, "conditions": 1, "actions": 1}, good.get("unsupported"))
    check("27 ...with a readable summary of the copy", good["summary"]["if"] == "Mood changes to “Nátt” OR Test door changes to “on” and stays so for 2 s"
          and "when started by: Mood changes to “Nátt”: close Kitchen Window; otherwise: open Kitchen Window" in good["summary"]["then"], good["summary"])
    managed_before = home.managed()
    status, copied = app.change("api/import/convert", {"entity_id": names[IMPORTABLE]["entity_id"], "destination": "new"})
    imported = copied.get("device_id")
    check("27 the copy is a new device; nothing is deployed and the original is untouched", status == 200 and imported
          and home.managed() == managed_before and home.automation(IMPORTABLE)[1] == originals_before[IMPORTABLE]
          and home.value(names[IMPORTABLE]["entity_id"]) == "on", copied)
    status, twice = app.change("api/import/convert", {"entity_id": names[IMPORTABLE]["entity_id"], "destination": "new"})
    check("27 a second copy of the same automation is refused", status == 409, twice)
    plan = app.plan(imported)
    check("27 deploying the copy while the original runs is refused, with the reason",
          not plan["can_deploy"] and len(plan["duplicates"]) == 1 and "still turned on" in plan["reasons"][0], plan["reasons"])
    status, refused = app.deploy(imported)
    check("27 ...also when asked directly", status == 409 and home.managed() == managed_before, refused)
    home.call("automation.turn_off", names[IMPORTABLE]["entity_id"])  # the user's own step, in Home Assistant
    home.wait(names[IMPORTABLE]["entity_id"], "off", 5)
    time.sleep(1)
    status, _ = app.deploy(imported)
    check("27 with the original turned off, the copy deploys", status == 200 and f"automation_builder_{imported}" in home.managed(), _)
    home.call("cover.open_cover", COVER)
    home.wait(COVER, "open", 15)
    home.lux(300)
    select(home, "Nátt")
    check("27 the copy behaves like the original: Nátt closes the window", home.wait(COVER, ("closed", "closing"), 10), home.value(COVER))
    home.wait(COVER, "closed", 15)
    home.set("ab_door", True)
    check("27 ...the door (held 2 s) opens it", home.stays(COVER, "closed", 1) and home.wait(COVER, ("open", "opening"), 8), home.value(COVER))
    home.set("ab_door", False)
    select(home, "Dagur")
    check("27 every original is byte-for-byte what it was", all(home.automation(i)[1] == originals_before[i] for i in originals_before))

    # ------------------------------------------------- 28. an interrupted deployment
    if TARGET == "stack":
        app.add_rule(office, "Extra", [t_state(MOOD, to="Nátt")], [call("light.turn_off")])
        plan = app.plan(office)
        request(f"{STACK}/_test/fail_api?mode=slow", "POST")
        thread = threading.Thread(target=lambda: app.change("api/devices/deploy", {"device_id": office, "plan_hash": plan["plan_hash"]}), daemon=True)
        thread.start()
        time.sleep(3)   # Home Assistant has the new automation, the App still waits for the answer
        written = len(home.automation(office_auto)[1]["actions"]) == 3
        run(args.kill_app)
        request(f"{STACK}/_test/fail_api", "POST")
        run(args.start_app)
        check("28 the App was killed in the middle of a deployment and starts again", written and wait_app(app), written)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and app.status(office) != "in_sync":
            time.sleep(1)
        view = app.device(office)
        check("28 it finds that the automation had reached Home Assistant and records it (no false “changed outside”)",
              view["live"]["status"] == "in_sync" and "interrupted" in (view["last_result"] or {}).get("message", ""), (view["live"]["status"], view["last_result"]))
        thread.join(20)
    else:
        skip("28 an interrupted deployment is recognised after a restart", "needs the stand-in Supervisor's slow answer; on Home Assistant OS see haos_checks.py")

    # ------------------------------------------------- what the App sent
    if TARGET == "stack":
        _, sent = request(f"{STACK}/_test/commands")
        kinds = {c["type"] for c in sent["commands"]}
        allowed = READ_COMMANDS | {"device_automation/trigger/list", "device_automation/condition/list",
                                   "device_automation/action/list", "blueprint/list", "blueprint/save"}
        check("29 the App sent only reads, validation and its own blueprint saves", kinds <= allowed, kinds - allowed)
        saves = [c for c in sent["commands"] if c["type"] == "blueprint/save"]
        check("29 blueprints went into the App's own folder only", saves and all(c["path"].startswith("automation_builder/") and c.get("domain") == "automation" for c in saves), saves)
        traces = {c.get("item_id") for c in sent["commands"] if c["type"] in ("trace/list", "trace/get")}
        check("29 traces were read of the App's own automations only", traces and all(str(t).startswith("automation_builder_") for t in traces), traces)
        paths = {h["path"] for h in sent["http"]}
        check("29 over HTTP only its own automations", all(p.startswith("config/automation/config/automation_builder_") for p in paths), paths)
    else:
        skip("29 what the App sent, recorded by the Supervisor", "needs the stand-in Supervisor's recording; covered on the stack")
    _, mechanism = app.get("api/settings")
    check("29 the App's own count: no service call", "call_service" not in mechanism["mechanism"]["commands"], mechanism["mechanism"]["commands"])

    # ------------------------------------------------- Home Assistant restart
    if args.restart_ha:
        run(args.restart_ha)
        check("30 Home Assistant restarted", home.wait_up())
        ready = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not ready:
            try:
                _, overview = app.get("api/overview")
                mine = [d for d in overview["devices"] if d["id"] in (decor, fan, kitchen, ceiling, office, imported)]
                ready = overview["connection"]["home_assistant"]["connected"] and {d["status"] for d in mine} == {"in_sync"}
            except (OSError, KeyError):
                pass
            time.sleep(2)
        check("30 every Phase 0.2 automation is loaded again and in sync", ready, [(d["name"], d["status"]) for d in overview.get("devices", [])])
        d = diag(app, fan, r_nested)
        check("30 the rule diagnostics survive the restart (Home Assistant forgets its traces)", d["runs"] >= 5 and d["last_executed"], d)
        home.call("switch.turn_off", DECOR)
        time.sleep(1)
        press(home)
        check("30 the button rule still works; becoming available again was not a press", home.wait(DECOR, "on", 5))
        fire(home, "ab_test_event", button="left")
        home.call("switch.turn_off", DECOR)

    return summary(args.out)


if __name__ == "__main__":
    _ = results
    sys.exit(main())
