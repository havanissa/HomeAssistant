"""End-to-end checks of App 0.4 (Lighting Designer 2): the real App image against a real Home Assistant.

Truth and Brightness Matrices from zones and a light level, Colour & Mood, an Advanced Rule that releases by itself,
a remote (ZHA event) with a manual override held in real helpers that the App creates, a resume button (event
entity), the simulation against what Home Assistant really does, the automations running while the App is stopped,
an override across a restart of Home Assistant, the helpers' lifecycle, and the conversion of a version 1 designer.

    python e2e_05_checks.py <tokens.json> [--restart-ha "<command>"] [--out results.json]

Needs the test package of 0.3 (zones, lux, mood, guest_mode) and the demo integration (lights, event entity).
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import time

from e2e_checks import HA, App, Home, check, request, summary

KITCHEN, BED = "light.kitchen_lights", "light.bed_light"
BUTTON = "event.push_button_press"
HERE = Path(__file__).parent


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--restart-ha", default="")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    home, app = Home(json.loads(Path(args.tokens).read_text())["llat"]), App()
    _, overview = app.get("api/overview")
    check("80 this is version 0.4 or later", tuple(map(int, overview["app"]["version"].split(".")[:2])) >= (0, 4), overview["app"])

    def select(entity: str, option: str) -> None:
        home.call("input_select.select_option", entity, option=option)

    def bright(entity: str) -> int | None:
        value = home.state(entity)["attributes"].get("brightness")
        return round(value * 100 / 255) if isinstance(value, (int, float)) else None

    def kelvin(entity: str) -> int | None:
        return home.state(entity)["attributes"].get("color_temp_kelvin")

    def lit(entity: str, pct: int | None = None, k: int | None = None, timeout: float = 15) -> bool:
        return home.wait(entity, lambda s: s["state"] == "on" and (pct is None or abs(round((s["attributes"].get("brightness") or 0) * 100 / 255) - pct) <= 1)
                         and (k is None or abs((s["attributes"].get("color_temp_kelvin") or 0) - k) <= 60), timeout)

    def zha(command: str = "toggle") -> None:
        status, _ = request(f"{HA}/api/events/zha_event", "POST", {"device_ieee": "e2e:00:01", "command": command}, home.headers)
        assert status == 200

    def press() -> None:
        status, _ = request(f"{HA}/api/states/{BUTTON}", "POST", {"state": datetime.now(UTC).isoformat(timespec="milliseconds"),
                                                                   "attributes": {"event_type": "pressed", "event_types": ["pressed"],
                                                                                  "friendly_name": "Push button"}}, home.headers)
        assert status in (200, 201)

    for helper in ("sofa_zone_drv", "kitchen_zone_drv", "guest_mode"):
        home.set(helper, False)
    home.lux(400)
    select("input_select.mood", "Dagur")
    paused = [a["entity_id"] for a in home.automations() if a["state"] == "on"]
    for entity_id in paused:  # the earlier scenarios' automations would act on the same lights
        home.call("automation.turn_off", entity_id)
    for light in (KITCHEN, BED):
        home.call("light.turn_off", light)

    # ------------------------------------------------- 81. a device, sources and a version 2 designer
    def source(**raw):
        status, data = app.change("api/presence/save", {"source": raw})
        assert status == 200, data
        return data["source_id"]
    sofa = source(name="Sofa 2", kind="zone", entity_id="binary_sensor.sofa_zone")
    counter = source(name="Kitchen 2", kind="zone", entity_id="binary_sensor.kitchen_zone")
    device_id = app.add_device("Living lights", [KITCHEN, BED])
    status, made = app.change("api/designers/create", {"device_id": device_id, "kind": "lighting"})
    check("81 a new Lighting Designer is version 2 with nothing set", status == 200, made)
    designer_id = made["designer_id"]
    _, page = app.get(f"api/designers/{device_id}/{designer_id}")
    blank = page["designer"]["config"]
    check("81 nothing is chosen for it", blank["version"] == 2 and blank["lights"] == [] and blank["truth"]["matrices"] == []
          and blank["override"]["policy"] is None and blank["priority"] == ["advanced", "override", "mood", "brightness", "truth"], blank)
    c = lambda i: f"c_00{i:02d}"  # noqa: E731
    designer = {**blank, "lights": [KITCHEN, BED],
                "truth": {"combine": None, "matrices": [{
                    "id": "m_0001", "name": "Presence", "enabled": True, "policy": "priority", "ranks": [c(3), c(1), c(2)],
                    "columns": [{"id": c(1), "kind": "presence", "source": sofa, "state": "occupied"},
                                {"id": c(2), "kind": "presence", "source": sofa, "state": "vacant"},
                                {"id": c(3), "kind": "presence", "source": counter, "state": "occupied"}],
                    "cells": {KITCHEN: {c(1): {"kind": "off"}, c(2): {"kind": "on"}, c(3): {"kind": "on"}},
                              BED: {c(1): {"kind": "on"}, c(2): {"kind": "off"}}}, "row_defaults": {}, "default": None}]},
                "brightness": {"combine": None, "zero": None, "matrices": [{
                    "id": "m_0002", "name": "Light level", "enabled": True, "policy": None, "ranks": [],
                    "columns": [{"id": c(4), "kind": "numeric", "entity_id": "sensor.test_lux", "below": 100},
                                {"id": c(5), "kind": "numeric", "entity_id": "sensor.test_lux", "from": 100}],
                    "cells": {KITCHEN: {c(4): {"kind": "set", "pct": 30}, c(5): {"kind": "set", "pct": 80}},
                              BED: {c(4): {"kind": "set", "pct": 20}, c(5): {"kind": "set", "pct": 60}}}, "row_defaults": {}, "default": None}]},
                "mood": {"enabled": True, "entity_id": "input_select.mood", "values": ["Dagur", "Hygge"], "auto": None, "row_defaults": {}, "default": None,
                         "cells": {KITCHEN: {"Dagur": {"kind": "set", "color_temp_kelvin": 5000}, "Hygge": {"kind": "set", "color_temp_kelvin": 2700}},
                                   BED: {"Dagur": {"kind": "set", "color_temp_kelvin": 5000}, "Hygge": {"kind": "set", "color_temp_kelvin": 2700}}}},
                "advanced": [{"id": "a_0001", "name": "Guests", "enabled": True, "kind": "while", "lights": [KITCHEN],
                              "conditions": {"type": "and", "items": [{"type": "state", "entity_id": "input_boolean.guest_mode", "state": "on"}]},
                              "set": {"truth": "off"}}],
                "switches": [
                    {"id": "w_0001", "name": "Remote", "kind": "momentary", "entity_id": None, "ha_device_id": None,
                     "interactions": [{"id": "i_0001", "kind": "single", "label": "Single press", "trigger": {
                         "type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "e2e:00:01", "command": "toggle"}}}],
                     "mappings": [{"id": "p_0001", "interaction": "i_0001", "lights": [KITCHEN], "action": {"kind": "toggle"}, "override": True}]},
                    {"id": "w_0002", "name": "Push button", "kind": "momentary", "entity_id": BUTTON, "ha_device_id": None,
                     "interactions": [{"id": "i_0002", "kind": "single", "label": "Pressed", "trigger": {
                         "type": "button", "entity_id": BUTTON, "event_types": ["pressed"]}}],
                     "mappings": [{"id": "p_0002", "interaction": "i_0002", "lights": [KITCHEN], "action": {"kind": "resume"}}]}],
                "override": {"policy": None, "minutes": None, "source": None, "release": [], "manual": False}}
    status, saved = app.change("api/designers/save", {"device_id": device_id, "designer": designer})
    codes = {p["code"] for p in saved["designer"]["problems"] if p["level"] == "blocking"} if status == 200 else set()
    check("82 an override without a policy is refused before deployment", "no_override_policy" in codes, codes)
    designer["override"] = {"policy": "duration", "minutes": 1, "source": None, "release": [], "manual": False}
    status, saved = app.change("api/designers/save", {"device_id": device_id, "designer": designer})
    check("82 with a duration it can be deployed", status == 200 and saved["blocking"] == 0,
          [p["message"] for p in saved.get("designer", {}).get("problems", []) if p["level"] == "blocking"] if status == 200 else saved)
    autos = saved.get("automations") or []
    check("82 one native automation per light, plus the shared one for the switches",
          sorted(a["role"] for a in autos) == ["group", "group", "shared"], autos)

    # ------------------------------------------------- 83. deployment creates the helpers
    plan = app.plan(device_id)
    helpers = {h["entity_id"]: h for h in plan["helpers"]}
    flag = next((e for e in helpers if e.startswith("input_boolean.")), None)
    timer = next((e for e in helpers if e.startswith("timer.")), None)
    check("83 the plan lists the helpers it will create", flag and timer and {h["operation"] for h in helpers.values()} == {"create"}
          and plan["can_deploy"], plan["helpers"])
    status, result = app.deploy(device_id)
    check("83 deployed", status == 200, result)
    check("83 the helpers exist in Home Assistant (timer restores across restarts)", home.wait(flag, "off") and home.wait(timer, "idle")
          and home.state(timer)["attributes"].get("restore") in (True, None), [home.state(flag), home.state(timer)])
    check("83 the automations exist in Home Assistant", sum(1 for a in home.managed() if a.startswith(f"automation_builder_{device_id}")) == 3,
          home.managed())
    plan = app.plan(device_id)
    check("83 deploying again changes nothing", not plan["changes"] and {h["operation"] for h in plan["helpers"]} == {"keep"}, plan["helpers"])

    # ------------------------------------------------- 84. truth, brightness, mood
    home.set("sofa_zone_drv", True)
    check("84 sofa occupied: Kitchen OFF, Bed ON at 60 % and 5000 K", home.wait(KITCHEN, "off") and lit(BED, 60, 5000),
          [home.state(KITCHEN), home.state(BED)])
    home.set("sofa_zone_drv", False)
    check("84 sofa vacant: Kitchen ON at 80 % and 5000 K, Bed OFF", lit(KITCHEN, 80, 5000) and home.wait(BED, "off"),
          [home.state(KITCHEN), home.state(BED)])
    select("input_select.mood", "Hygge")
    check("84 the mood changes the colour of the ON light, not its brightness", lit(KITCHEN, 80, 2700) and home.stays(BED, "off", 2),
          home.state(KITCHEN))
    home.lux(50)
    check("84 dark: the Brightness Matrix gives 30 %", lit(KITCHEN, 30, 2700), home.state(KITCHEN))
    home.set("kitchen_zone_drv", True)
    home.set("sofa_zone_drv", True)
    check("84 both zones occupied: the ranking decides (Kitchen zone first): Kitchen stays ON", home.stays(KITCHEN, "on", 3)
          and lit(BED, 20, 2700), [home.state(KITCHEN), home.state(BED)])
    home.set("kitchen_zone_drv", False)
    check("84 only the sofa: Kitchen OFF", home.wait(KITCHEN, "off"), home.state(KITCHEN))
    home.set("sofa_zone_drv", False)
    check("84 back on", lit(KITCHEN, 30, 2700), home.state(KITCHEN))

    # ------------------------------------------------- 85. an Advanced Rule releases by itself
    home.set("guest_mode", True)
    check("85 while the guests are here: Kitchen OFF", home.wait(KITCHEN, "off"), home.state(KITCHEN))
    home.lux(400)
    select("input_select.mood", "Dagur")
    check("85 the matrices change meanwhile: it stays OFF", home.stays(KITCHEN, "off", 3), home.state(KITCHEN))
    home.set("guest_mode", False)
    check("85 the rule ends: the CURRENT baseline returns (80 %, 5000 K)", lit(KITCHEN, 80, 5000), home.state(KITCHEN))

    # ------------------------------------------------- 86. a remote with a timed manual override
    zha()
    check("86 single press: toggled once (OFF), override on, timer running", home.wait(KITCHEN, "off") and home.wait(flag, "on")
          and home.wait(timer, "active"), [home.state(KITCHEN), home.state(flag), home.state(timer)])
    home.set("sofa_zone_drv", True)
    home.set("sofa_zone_drv", False)
    check("86 during the override the matrices change nothing", home.stays(KITCHEN, "off", 4), home.state(KITCHEN))
    home.lux(50)
    check("86 the override ends after its minute; the light returns to the baseline as it is NOW (30 %)",
          home.wait(flag, "off", 75) and lit(KITCHEN, 30, 5000), [home.state(flag), home.state(KITCHEN)])
    zha()
    home.wait(flag, "on")
    press()
    check("86 the resume button ends it at once", home.wait(flag, "off") and lit(KITCHEN, 30), [home.state(flag), home.state(KITCHEN)])
    zha()
    check("86 one press, one toggle (no double toggle, no loop)", home.wait(KITCHEN, "off") and home.stays(KITCHEN, "off", 3), home.state(KITCHEN))
    press()
    home.wait(KITCHEN, "on")

    # ------------------------------------------------- 87. who controls the light, the simulation
    _, page = app.get(f"api/designers/{device_id}/{designer_id}")
    live = {item["entity_id"]: item for item in page["live"]["lights"]}
    k = live[KITCHEN]
    check("87 the page shows which layer controls each property now", k["resolved"]["on"][1].startswith("Truth")
          and k["resolved"]["brightness"][1].startswith("Brightness") and k["resolved"]["color"][1] == "Mood: Dagur"
          and k["command"]["service"] is None, k)
    _, inputs = app.get(f"api/simulation/{device_id}")
    scenario = {"states": {i["entity_id"]: {"state": i["state"], "attributes": i["attributes"], "origin": "live"} for i in inputs["inputs"]}}
    scenario["states"]["binary_sensor.sofa_zone"] = {"state": "on", "attributes": {}, "origin": "test"}
    scenario["states"]["input_select.mood"] = {"state": "Hygge", "attributes": {}, "origin": "test"}
    status, sim = app.post("api/designers/simulate", {"device_id": device_id, "scenario": scenario})
    predicted = {item["entity_id"]: item["command"] for item in sim["lighting"][designer_id]["lights"]}
    check("87 the simulation predicts: Kitchen off, Bed on at 20 % and 2700 K", predicted[KITCHEN]["service"] == "turn_off"
          and predicted[BED] == {"service": "turn_on", "data": {"brightness_pct": 20, "color_temp_kelvin": 2700}}, predicted)
    select("input_select.mood", "Hygge")
    home.set("sofa_zone_drv", True)
    check("87 and Home Assistant does exactly that", home.wait(KITCHEN, "off") and lit(BED, 20, 2700), [home.state(KITCHEN), home.state(BED)])
    status, sim = app.post("api/designers/simulate", {"device_id": device_id, "scenario": {**scenario, "event": "i_0001"}})
    event = sim["lighting"][designer_id]["event"]
    check("87 a simulated press is an event; it starts the override and sends nothing to a device",
          event and "manual override starts" in event["happened"][0]["text"], event)

    # ------------------------------------------------- 88. without the App
    subprocess.run([str(HERE / "stack.sh"), "stop-app"], check=True)
    home.set("sofa_zone_drv", False)
    check("88 App stopped: the automations still run (Kitchen on, Bed off)", lit(KITCHEN, 30) and home.wait(BED, "off"),
          [home.state(KITCHEN), home.state(BED)])
    zha()
    check("88 App stopped: the remote and its override work", home.wait(KITCHEN, "off") and home.wait(flag, "on"), home.state(flag))
    press()
    home.wait(flag, "off")
    subprocess.run([str(HERE / "stack.sh"), "start-app"], check=True)
    app = App()
    app.get("api/overview")

    # ------------------------------------------------- 89. an override across a restart of Home Assistant
    if args.restart_ha:
        home.lux(400)
        lit(KITCHEN, 80)
        zha()
        home.wait(flag, "on")
        subprocess.run(args.restart_ha, shell=True, check=True)
        home.wait_up()
        time.sleep(5)
        check("89 after a restart the override is still on and its timer still runs", home.value(flag) == "on"
              and home.value(timer) == "active" and home.value(KITCHEN) in ("off", "on"), [home.state(flag), home.state(timer)])
        mood_k = 5000 if home.value("input_select.mood") == "Dagur" else 2700  # the test package's mood has an initial value
        check("89 when its time is up the light returns to the baseline", home.wait(flag, "off", 90) and lit(KITCHEN, 80, mood_k, 20),
              [home.state(flag), home.state(KITCHEN), home.value("input_select.mood")])
        app = App()
        for _ in range(30):
            status, ov = app.get("api/overview")
            if status == 200 and ov.get("connection", {}).get("home_assistant", {}).get("connected"):
                break
            time.sleep(2)

    # ------------------------------------------------- 90. diagnostics and the helpers' lifecycle
    _, diag = app.get("api/diagnostics")
    mine = next(d for d in diag["devices"] if d["id"] == device_id)
    check("90 diagnostics show the source of control and the helpers", mine["lighting"][designer_id]
          and set(mine["helpers"]) == {flag, timer}, {"lighting": mine.get("lighting"), "helpers": mine.get("helpers")})
    designer = app.get(f"api/designers/{device_id}/{designer_id}")[1]["designer"]["config"]
    designer["override"] = {**designer["override"], "policy": "until_resumed", "minutes": None}
    app.change("api/designers/save", {"device_id": device_id, "designer": designer})
    plan = app.plan(device_id)
    check("90 a timer that is no longer needed is deleted at the next deployment", (timer, "delete") in
          [(h["entity_id"], h["operation"]) for h in plan["helpers"]], plan["helpers"])
    app.deploy(device_id)
    check("90 ... and only after the automations stopped using it", home.wait(timer, lambda s: s.get("state") is None) and home.value(flag) == "off",
          home.state(timer))

    # ------------------------------------------------- 91. a version 1 designer is converted only when asked
    _, exported = app.get("api/export")
    doc = json.loads(json.dumps(exported))
    v1 = {"id": "g_0000a001", "kind": "lighting", "version": 1, "name": "Old lighting", "enabled": True, "lights": [BED], "primary": "presence",
          "presence": {"enabled": True, "policy": "priority", "columns": [{"source": sofa, "state": "occupied"}],
                       "cells": {BED: {f"{sofa}:occupied": {"kind": "on", "brightness_pct": 40}}}}}
    doc["config"]["devices"]["d_0000a001"] = {"name": "Old bed", "targets": [BED], "rules": [], "designers": [v1]}
    status, review = app.post("api/import/review", {"document": doc})
    status, applied = app.change("api/import/apply", {"document": doc})
    check("91 a 0.3 configuration with a version 1 designer is kept", status == 200 and
          app.get("api/designers/d_0000a001/g_0000a001")[1]["designer"]["config"]["version"] == 1, applied)
    _, old = app.get("api/designers/d_0000a001/g_0000a001")
    check("91 its page offers the conversion with what changes", old["conversion"]["refused"] == [] and old["conversion"]["changes"], old.get("conversion"))
    status, converted = app.change("api/designers/convert", {"device_id": "d_0000a001", "designer_id": "g_0000a001", "apply": True})
    _, new = app.get("api/designers/d_0000a001/g_0000a001")
    check("91 converted on request, nothing deployed yet", status == 200 and new["designer"]["config"]["version"] == 2
          and app.device("d_0000a001")["live"]["status"] in ("not_deployed", "changes_pending"), converted)
    app.change("api/devices/delete", {"device_id": "d_0000a001", "overwrite": False})

    # ------------------------------------------------- 92. removing the device removes its helpers
    status, removed = app.change("api/devices/delete", {"device_id": device_id, "overwrite": False})
    check("92 removing the device removes its automations and the helpers it created", status == 200
          and home.wait(flag, lambda s: s.get("state") is None) and not [a for a in home.managed() if device_id in a], removed)
    for entity_id in paused:
        home.call("automation.turn_on", entity_id)
    return summary(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
