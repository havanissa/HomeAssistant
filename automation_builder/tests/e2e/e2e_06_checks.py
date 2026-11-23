"""End-to-end checks of App 0.5: the real App image against a real Home Assistant 2026.11.1.

* Switches & Remotes: one remote (ZHA events) turns the same light ON with one press and OFF with another, toggles
  it with a third - deployed, run by Home Assistant, also while the App is stopped.
* Light Level Manager: a template, default matrices, a room's light level with its own sensor and a room override,
  deployed with its helper; a light follows the room's level through Light Level matrices; the helper's lifecycle.
* Heating Designer: a heating template and a room profile, a demo heat pump driven by a mode helper, a window
  (frost protection), a manual change by hand (override) and the resume button, running without the App, and its
  blueprint installed through Home Assistant.

    python e2e_06_checks.py <tokens.json> [--out results.json]

Needs the test package (sensor.test_lux, input_select.heat_mode, input_boolean.e2e_window, input_button.heat_resume)
and the demo integration (lights, climate.heatpump).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import time

from e2e_checks import HA, App, Home, check, request, summary

OFFICE, KITCHEN, PUMP = "light.office_rgbw_lights", "light.kitchen_lights", "climate.heatpump"
HERE = Path(__file__).parent


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    home, app = Home(json.loads(Path(args.tokens).read_text())["llat"]), App()
    _, overview = app.get("api/overview")
    check("100 this is version 0.5", overview["app"]["version"].startswith("0.5"), overview["app"])
    for entity_id in [a["entity_id"] for a in home.automations() if a["state"] == "on"]:
        home.call("automation.turn_off", entity_id)     # the earlier scenarios' automations act on the same lights
    for light in (OFFICE, KITCHEN):
        home.call("light.turn_off", light)

    def zha(command: str) -> None:
        status, _ = request(f"{HA}/api/events/zha_event", "POST", {"device_ieee": "e2e:06:01", "command": command}, home.headers)
        assert status == 200

    def pct(entity: str) -> int | None:
        value = home.state(entity)["attributes"].get("brightness")
        return round(value * 100 / 255) if isinstance(value, (int, float)) else None

    def wait_for(predicate, timeout: float = 15) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.5)
        return predicate()

    # ================================================= Part 2: the same remote, the same light, ON and OFF
    device = app.add_device("Office light", [OFFICE])
    _, made = app.change("api/designers/create", {"device_id": device, "kind": "lighting"})
    designer_id = made["designer_id"]
    _, page = app.get(f"api/designers/{device}/{designer_id}")
    d = page["designer"]["config"]
    ev = lambda cmd: {"type": "event", "event_type": "zha_event", "event_data": {"device_ieee": "e2e:06:01", "command": cmd}}  # noqa: E731
    d.update(lights=[OFFICE], switches=[{
        "id": "w_0601", "name": "Office remote", "kind": "momentary", "entity_id": None, "ha_device_id": None, "manual": True,
        "interactions": [{"id": "i_0601", "kind": "single", "label": "Single press", "trigger": ev("single")},
                         {"id": "i_0602", "kind": "double", "label": "Double press", "trigger": ev("double")},
                         {"id": "i_0603", "kind": "triple", "label": "Triple press", "trigger": ev("triple")}],
        "mappings": [{"id": "p_0601", "interaction": "i_0601", "lights": [OFFICE], "action": {"kind": "on"}},
                     {"id": "p_0602", "interaction": "i_0602", "lights": [OFFICE], "action": {"kind": "off"}},
                     {"id": "p_0603", "interaction": "i_0603", "lights": [OFFICE], "action": {"kind": "toggle"}},
                     {"id": "p_0604", "interaction": "i_0601", "lights": [OFFICE], "action": {"kind": "brightness", "pct": 60},
                      "enabled": False}]}])
    status, saved = app.change("api/designers/save", {"device_id": device, "designer": d})
    mappings = saved["designer"]["config"]["switches"][0]["mappings"] if status == 200 else []
    check("101 one switch keeps four interactions on the same light (three enabled)", len(mappings) == 4
          and [m["enabled"] for m in mappings] == [True, True, True, False], saved)
    check("101 ON on one press and OFF on another is no conflict", status == 200 and saved["blocking"] == 0
          and not [c for c in saved.get("conflicts", []) if c["level"] == "blocking"], saved.get("conflicts"))
    status, result = app.deploy(device)
    check("102 deployed", status == 200, result)
    zha("single")
    check("103 single press: the light turns ON (real Home Assistant)", home.wait(OFFICE, "on"), home.state(OFFICE))
    zha("double")
    check("103 double press: the same light turns OFF", home.wait(OFFICE, "off"), home.state(OFFICE))
    zha("triple")
    check("103 triple press toggles it on", home.wait(OFFICE, "on"), home.state(OFFICE))
    zha("triple")
    check("103 and off again (one toggle per press)", home.wait(OFFICE, "off") and home.stays(OFFICE, "off", 2), home.state(OFFICE))
    zha("single")
    check("103 the disabled interaction does nothing (no 60 %)", home.wait(OFFICE, "on") and pct(OFFICE) != 60, home.state(OFFICE))
    subprocess.run([str(HERE / "stack.sh"), "stop-app"], check=True)
    try:
        zha("double")
        off = home.wait(OFFICE, "off")
        zha("single")
        check("104 with the App stopped, ON and OFF still work (native automation)", off and home.wait(OFFICE, "on"), home.state(OFFICE))
    finally:
        subprocess.run([str(HERE / "stack.sh"), "start-app"], check=True)
    app.get("api/overview")

    # ================================================= Part 1: the Light Level Manager
    home.lux(60)
    status, t = app.change("api/light-levels/template/preset", {})
    template_id = t.get("template_id")
    _, view = app.get("api/light-levels")
    template = next(x for x in view["templates"] if x["id"] == template_id)
    fast = {k: template[k] for k in ("id", "name", "bands")} | {"hysteresis": 10, "darker_seconds": 2, "brighter_seconds": 2, "unavailable_seconds": 60}
    status, _ = app.change("api/light-levels/template/save", {"template": fast})
    _, view = app.get("api/light-levels")
    check("110 a template: saving a change raises its version", status == 200 and next(x for x in view["templates"] if x["id"] == template_id)["version"] == 2, view["templates"])
    cells_t = {"very_dark": {"kind": "on"}, "dark": {"kind": "on"}, "dim": {"kind": "on"}, "bright": {"kind": "off"}}
    cells_b = {"very_dark": {"kind": "set", "pct": 20}, "dark": {"kind": "set", "pct": 40}, "dim": {"kind": "set", "pct": 75}, "bright": {"kind": "set", "pct": 100}}
    _, mt = app.change("api/light-levels/matrix/save", {"matrix": {"name": "Default truth", "layer": "truth", "template": template_id, "cells": cells_t}})
    _, mb = app.change("api/light-levels/matrix/save", {"matrix": {"name": "Default brightness", "layer": "brightness", "template": template_id, "cells": cells_b}})
    status, _ = app.change("api/light-levels/defaults", {"defaults": {"default_template": template_id,
                                                                      "default_matrices": {"truth": mt["matrix_id"], "brightness": mb["matrix_id"]}}})
    status, room = app.change("api/light-levels/room/save", {"room": {"area_id": "kitchen", "create": True}})
    check("111 a room: its light level device is created, with the standard template and matrices", status == 200 and room.get("device_id"), room)
    ll_device = room["device_id"]
    _, sensors = app.get("api/light-levels/sensors?area=kitchen&all=0")
    check("111 the room's own light sensor is offered (filtered by its area)", [s["entity_id"] for s in sensors["sensors"]] == ["sensor.test_lux"], sensors)
    status, _ = app.change("api/light-levels/room/save", {"room": {"area_id": "kitchen", "source": {"entity_id": "sensor.test_lux", "attribute": None},
                                                                   "overrides": {"bands": {"dark": {"below": 95}}}}})
    _, view = app.get("api/light-levels")
    kitchen = next(r for r in view["rooms"] if r["area_id"] == "kitchen")
    dark = next(b for b in kitchen["effective"]["bands"] if b["id"] == "dark")
    check("112 inheritance is shown: 95 lx room override over the global 80 lx", (dark["below"], dark["global"], dark["source"]) == (95, 80, "room"), dark)
    check("112 the room reads its sensor (measured) and knows what the reading calls for", kitchen["reading"]["value"] == 60
          and not kitchen["reading"]["estimated"] and kitchen["computed"] == "dark", kitchen["reading"])
    plan = app.plan(ll_device)
    helper = kitchen["helpers"]["band"]
    check("113 the room's deployment creates its helper", [(h["entity_id"], h["operation"]) for h in plan["helpers"]] == [(helper, "create")]
          and plan["can_deploy"], plan.get("reasons"))
    status, result = app.deploy(ll_device)
    check("113 deployed; the helper exists and the level is set", status == 200 and home.wait(helper, "dark", 20), [result, home.state(helper)])

    light = app.add_device("Kitchen light", [KITCHEN])
    _, made = app.change("api/designers/create", {"device_id": light, "kind": "lighting"})
    lid = made["designer_id"]
    _, page = app.get(f"api/designers/{light}/{lid}")
    ld = page["designer"]["config"]
    ld.update(lights=[KITCHEN],
              truth={"combine": None, "matrices": [{"id": "m_0611", "name": "Light level", "enabled": True, "level": {"room": "kitchen"}, "cells": {}}]},
              brightness={"combine": None, "zero": None, "matrices": [{"id": "m_0612", "name": "Light level", "enabled": True, "level": {"room": "kitchen"}, "cells": {}}]})
    status, saved = app.change("api/designers/save", {"device_id": light, "designer": ld})
    resolved = saved["designer"]["view"]["resolved"]["m_0612"][KITCHEN] if status == 200 else {}
    check("114 the light's Light Level matrices inherit the room's default matrices", status == 200 and saved["blocking"] == 0
          and resolved.get("dim", {}).get("label") == "75%" and resolved["dim"].get("source") == "global default matrix", resolved)
    status, result = app.deploy(light)
    check("114 the light deployed", status == 200, result)
    home.lux(150)
    check("115 brighter (150 lx): after the stabilisation the level is dim and the light 75 %", home.wait(helper, "dim", 15)
          and wait_for(lambda: home.value(KITCHEN) == "on" and abs((pct(KITCHEN) or 0) - 75) <= 1), [home.value(helper), home.state(KITCHEN)])
    home.lux(205)
    check("115 within the hysteresis (205 < 200 + 10) nothing changes", home.stays(helper, "dim", 5), home.value(helper))
    home.lux(400)
    check("115 bright (400 lx): the light turns OFF", home.wait(helper, "bright", 15) and home.wait(KITCHEN, "off", 15), [home.value(helper), home.state(KITCHEN)])
    home.lux(60)
    check("115 dark (60 lx, the room's own 95 lx limit): ON at 40 %", home.wait(helper, "dark", 15)
          and wait_for(lambda: home.value(KITCHEN) == "on" and abs((pct(KITCHEN) or 0) - 40) <= 1), home.state(KITCHEN))
    status, refused = app.change("api/light-levels/room/delete", {"area_id": "kitchen", "overwrite": False})
    check("116 a room's light level that a light uses cannot be removed", status == 409, refused)
    status, refused = app.change("api/devices/delete", {"device_id": ll_device, "overwrite": False})
    check("116 its device can only be removed on the Light Level page", status == 409, refused)

    # ================================================= Part 3: the Heating Designer
    home.call("input_select.select_option", "input_select.heat_mode", option="Day")
    home.set("e2e_window", False)
    status, h = app.change("api/heating/template/save", {"template": "preset"})
    status, _ = app.change("api/heating/room/save", {"room": {"area_id": "bedroom", "template": h["template_id"], "overrides": {"comfort": {"kind": "set", "temp": 22}}}})
    _, hv = app.get("api/heating")
    bedroom = next(r for r in hv["rooms"] if r["area_id"] == "bedroom")
    check("120 room profile: comfort 22 (room), setback 18 (global)", (bedroom["cells"]["comfort"]["source"], bedroom["cells"]["setback"]["source"])
          == ("room", "global"), bedroom["cells"])
    pump = app.add_device("Heat pump", [PUMP])
    status, made = app.change("api/designers/create", {"device_id": pump, "kind": "heating"})
    check("120 a thermostat offers the Heating Designer", status == 200, made)
    hid = made["designer_id"]
    _, page = app.get(f"api/designers/{pump}/{hid}")
    hd = page["designer"]["config"]
    caps = page["capabilities"][PUMP]
    check("120 its capabilities are discovered (heating only, no cooling offered)", caps["heating_modes"] == ["heat"] and not caps["can_cool"], caps)
    hd.update(thermostats=[PUMP], profile={"room": "bedroom"},
              modes={"source": "entity", "entity": {"entity_id": "input_select.heat_mode", "map": {"Day": "comfort", "Night": "night", "Away": "away"}},
                     "schedule": {"split": "all", "all": [], "weekdays": [], "weekend": []}},
              window={"enabled": True, "sensors": ["input_boolean.e2e_window"], "open_seconds": 2, "close_seconds": 2, "action": "frost", "unavailable": "ignore"},
              override={"manual": True, "policy": "until_resumed", "resume": [{"type": "state", "entity_id": "input_button.heat_resume"}], "release": []})
    status, saved = app.change("api/designers/save", {"device_id": pump, "designer": hd})
    check("121 the heating designer is complete", status == 200 and saved["blocking"] == 0,
          [p["message"] for p in saved.get("designer", {}).get("problems", []) if p["level"] == "blocking"] if status == 200 else saved)
    status, result = app.deploy(pump)
    flag = next((h["entity_id"] for h in app.plan(pump)["helpers"] if h["entity_id"].startswith("input_boolean.")), None)
    check("121 deployed with its override helper", status == 200 and flag and home.wait(flag, "off"), result)
    target = lambda: home.state(PUMP)["attributes"].get("temperature")  # noqa: E731
    home.call("input_select.select_option", "input_select.heat_mode", option="Night")
    check("122 Night → 17° (global)", wait_for(lambda: target() == 17), home.state(PUMP)["attributes"])
    home.call("input_select.select_option", "input_select.heat_mode", option="Day")
    check("122 Day → 22° (the room's comfort)", wait_for(lambda: target() == 22), home.state(PUMP)["attributes"])
    home.set("e2e_window", True)
    check("123 a window open for 2 s → frost protection 7°", wait_for(lambda: target() == 7, 10), home.state(PUMP)["attributes"])
    home.set("e2e_window", False)
    check("123 closed → back to what applies now (22°)", wait_for(lambda: target() == 22, 10), home.state(PUMP)["attributes"])
    home.call("climate.set_temperature", PUMP, temperature=24)       # by hand (the test's own user, no automation)
    check("124 a change by hand starts a manual override and is not undone", home.wait(flag, "on", 10) and target() == 24, home.state(flag))
    home.call("input_select.select_option", "input_select.heat_mode", option="Night")
    check("124 during the override the mode changes nothing", wait_for(lambda: target() == 24, 3) and time.sleep(2) is None and target() == 24, target())
    home.call("input_button.press", "input_button.heat_resume")
    check("125 resume: the override ends and the current mode applies (Night 17°)", home.wait(flag, "off", 10) and wait_for(lambda: target() == 17), target())
    subprocess.run([str(HERE / "stack.sh"), "stop-app"], check=True)
    try:
        home.call("input_select.select_option", "input_select.heat_mode", option="Away")
        check("126 with the App stopped, Away → 16°", wait_for(lambda: target() == 16), target())
    finally:
        subprocess.run([str(HERE / "stack.sh"), "start-app"], check=True)
    app.get("api/overview")
    _, bp = app.get(f"api/blueprint/{pump}")
    keys = [i["key"] for i in bp["inputs"]]
    chosen = {"automation": None, "name": "AB heating", "description": "", "inputs": {k: {"name": "", "description": ""} for k in keys}}
    status, _ = app.change("api/devices/update", {"device_id": pump, "patch": {"blueprint": chosen}})
    _, bp = app.get(f"api/blueprint/{pump}")
    check("127 the heating blueprint exposes the thermostat, temperatures and the mode helper, and Home Assistant accepts it",
          bp["valid"] and {"controlled_heatpump", "heatpump_comfort", "input_select_heat_mode"} <= set(keys), [bp.get("validation"), keys])
    status, installed = app.post("api/blueprint/install", {"device_id": pump, "overwrite": True})
    check("127 installed through Home Assistant's blueprint command", status == 200, installed)

    # ================================================= cleanup: helpers go with their devices
    for device_id in (pump, light, device):
        status, _ = app.change("api/devices/delete", {"device_id": device_id, "overwrite": False})
    status, removed = app.change("api/light-levels/room/delete", {"area_id": "kitchen", "overwrite": False})
    check("128 the room's light level is removed with its automation and helper", status == 200 and home.wait(helper, None, 10), [removed, home.state(helper)])
    check("128 and the heating override helper went with its device", home.wait(flag, None, 10), home.state(flag))
    return summary(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
