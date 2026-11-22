"""End-to-end checks of Phase 0.3 (the designers): the real App image against a real Home Assistant.

Lighting (mood + zones + light level combined, a position zone), covers (sun shading against Home Assistant's
real sun, cover modes with explicit sun policy, an awning's weather protection), presence sources, templates,
simulation, blueprint export, and the automations running while the App is stopped.

    python e2e_04_checks.py <tokens.json> [--out results.json]

Needs the test package of 0.3 (zones, radar, wind, awning drivers) in the disposable Home Assistant.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from e2e_checks import HA, App, Home, brightness_pct, check, request, summary

CEILING, KITCHEN, RGBWW, OFFICE = "light.ceiling_lights", "light.kitchen_lights", "light.living_room_rgbww_lights", "light.office_rgbw_lights"
WINDOW, AWNING = "cover.living_room_window", "cover.test_awning"
HERE = Path(__file__).parent


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    home, app = Home(json.loads(Path(args.tokens).read_text())["llat"]), App()
    app.get("api/overview")

    def number(name: str, value: float) -> None:
        home.call("input_number.set_value", f"input_number.{name}", value=value)

    def select(entity: str, option: str) -> None:
        home.call("input_select.select_option", entity, option=option)

    for helper in ("sofa_zone_drv", "kitchen_zone_drv", "occ_kamar", "rain_drv"):
        home.set(helper, False)
    home.set("occ_available", True)
    home.set("occ_fresh", True)
    home.lux(500)
    number("radar_x", 0)
    number("radar_y", 0)
    number("wind_drv", 2)
    select("input_select.mood", "Dagur")
    select("input_select.cover_mode", "Sleep")
    for light in (CEILING, KITCHEN, RGBWW, OFFICE):
        home.call("light.turn_off", light)
    # Automations of the earlier scenarios (for example e2e_02's "Ceiling" device, which follows the mood) would act on
    # the same lights: they are paused during this scenario and turned on again at its end.
    paused = [a["entity_id"] for a in home.automations() if a["state"] == "on"]
    for entity_id in paused:
        home.call("automation.turn_off", entity_id)

    # ------------------------------------------------- 50. presence sources
    def source(**raw):
        status, data = app.change("api/presence/save", {"source": raw})
        assert status == 200, data
        return data["source_id"]
    _, rooms = app.get("api/rooms")
    living = next(r["area_id"] for r in rooms["rooms"] if r["name"] == "Living Room")
    sofa = source(name="Sofa", kind="zone", area_id=living, entity_id="binary_sensor.sofa_zone", entry_seconds=2)
    kitchen = source(name="Kitchen counter", kind="zone", area_id=living, entity_id="binary_sensor.kitchen_zone")
    room = source(name="Living room", kind="room", area_id=living, entity_id="binary_sensor.sod_kamar_occupied", exit_seconds=3, respect_fresh=True)
    desk = source(name="Desk", kind="position", entity_id=None, entry_seconds=1, exit_seconds=1, position={
        "targets": [{"x": {"entity_id": "sensor.radar_target_x"}, "y": {"entity_id": "sensor.radar_target_y"}}],
        "units": "mm", "notes": "origin at the radar, y away from it", "ignore_origin": True,
        "shape": {"type": "rect", "points": [[0, 500], [1500, 2000]]}})
    unset = source(name="Not chosen yet", kind="zone", entity_id=None)
    _, view = app.get("api/presence")
    by_id = {s["id"]: s for s in view["sources"]}
    check("50 presence sources are saved with their kind, room and delays", by_id[sofa]["entry_seconds"] == 2 and by_id[sofa]["area"] == "Living Room"
          and by_id[desk]["kind"] == "position" and by_id[room]["respect_fresh"] is True, by_id[sofa])
    check("50 a source without an entity is unset, not off", by_id[unset]["entity_id"] is None and by_id[unset]["live"]["state"] is None, by_id[unset]["live"])
    check("50 live states are shown (a position zone from its coordinates)", by_id[sofa]["live"]["state"] == "off" and by_id[desk]["live"]["state"] == "no readings",
          [by_id[sofa]["live"], by_id[desk]["live"]])

    # ------------------------------------------------- 51. a device, the gallery, an empty start
    device = app.add_device("Living lights", [CEILING, KITCHEN, RGBWW])
    _, gallery = app.get(f"api/gallery/{device}")
    check("51 the gallery offers the lighting templates (and only those) for lights", {t["designer"] for t in gallery["templates"]} == {"lighting"}
          and "combined_lighting" in [t["id"] for t in gallery["templates"]] and gallery["planned"], [t["id"] for t in gallery["templates"]])
    status, made = app.change("api/gallery/apply", {"device_id": device, "template": "combined_lighting"})
    designer_id = made["designer_id"]
    _, page = app.get(f"api/designers/{device}/{designer_id}")
    config = page["designer"]["config"]
    check("51 a template turns controls on and binds nothing", config["primary"] == "presence" and config["mood"]["enabled"] and config["lux"]["enabled"]
          and config["lights"] == [] and config["mood"]["entity_id"] is None and config["lux"]["entity_id"] is None and config["presence"]["columns"] == [], config)
    plan = app.plan(device)
    check("51 an unconfigured designer cannot be deployed, and says what is missing", not plan["can_deploy"]
          and any("lights" in r for r in plan["reasons"]), plan["reasons"])

    # ------------------------------------------------- 52. the combined lighting designer
    col = lambda s, st="occupied": f"{s}:{st}"  # noqa: E731
    on = lambda **v: {"kind": "on", **{k: None for k in ("brightness_pct", "color_temp_kelvin", "color", "effect", "transition")}, **v}  # noqa: E731
    config.update(lights=[CEILING, KITCHEN, RGBWW])
    config["mood"].update(entity_id="input_select.mood", values=["Dagur", "Hygge"], apply_on_change=True,
                          cells={CEILING: {"Hygge": on(brightness_pct=40, color_temp_kelvin=2700), "Dagur": on(brightness_pct=80)}})
    config["presence"].update(columns=[{"source": sofa, "state": "occupied"}, {"source": kitchen, "state": "occupied"},
                                       {"source": room, "state": "vacant"}], policy="priority",
                              cells={CEILING: {col(sofa): {"kind": "mood"}},
                                     KITCHEN: {col(kitchen): on(brightness_pct=100), col(room, "vacant"): {"kind": "off", "transition": None}},
                                     RGBWW: {col(sofa): on(brightness_pct=20), col(kitchen): on(brightness_pct=50),
                                             col(room, "vacant"): {"kind": "off", "transition": None}}},
                              # the lamp's only unset cell is "vacant": its own default covers it (a matrix default would
                              # also fill the spots' unset sofa cell, and so - by priority - keep the kitchen column away)
                              row_defaults={CEILING: {"kind": "off", "transition": None}})
    config["presence"]["cells"][CEILING][col(kitchen)] = {"kind": "none"}   # explicitly nothing (and, by priority, nothing below)
    config["lux"].update(entity_id="sensor.test_lux", hysteresis=20, bands=[{"id": "b_0001", "name": "Dark", "below": 100},
                                                                             {"id": "b_0002", "name": "Bright", "below": None}],
                         gate={CEILING: {"on": ["b_0001"], "off": None}})
    status, saved = app.change("api/designers/save", {"device_id": device, "designer": config})
    check("52 the designer saves; nothing is left to resolve", status == 200 and saved["blocking"] == 0,
          [p["message"] for p in saved.get("designer", {}).get("problems", []) if p["level"] == "blocking"] if status == 200 else saved)
    resolved = saved["designer"]["view"]["resolved"]["presence"]
    check("52 an unset cell shows the default and its source, No Action stays No Action", resolved[CEILING][col(room, "vacant")]["label"] == "DEFAULT: OFF"
          and resolved[CEILING][col(room, "vacant")]["source"] == "row default" and resolved[KITCHEN][col(sofa)]["label"] == "UNSET"
          and resolved[CEILING][col(kitchen)]["label"] == "NO ACTION" and resolved[KITCHEN][col(kitchen)]["label"] == "100%", resolved[CEILING])
    check("52 one automation for the device, made of ordinary rules", saved["automation"]["rules"] == len(saved["designer"]["rules"]) >= 6
          and "Rule: Lighting" in saved["yaml"], saved["automation"])
    plan = app.plan(device)
    check("52 Home Assistant validates the generated automation", plan["can_deploy"] and not any(i["errors"] for i in plan["items"]), plan["reasons"])
    status, result = app.deploy(device)
    check("52 deployed", status == 200, result)
    managed = [m for m in home.managed() if m.startswith(f"automation_builder_{device}")]
    check("52 exactly one native automation for the device", managed == [f"automation_builder_{device}"], managed)

    # ------------------------------------------------- 53. it runs in Home Assistant
    select("input_select.mood", "Hygge")
    home.lux(50)
    home.set("occ_kamar", True)
    time.sleep(1)
    home.set("sofa_zone_drv", True)
    check("53 the sofa is not confirmed before its 2 s", home.stays(CEILING, "off", 1.2))
    ok = home.wait(CEILING, lambda s: s["state"] == "on" and s["attributes"].get("color_temp_kelvin") == 2700, 6)
    check("53 sofa occupied + dark + Hygge: the sofa lamp at 40 %, 2700 K (mood × presence × light level)", ok
          and abs(brightness_pct(home, CEILING) - 40) <= 1, home.state(CEILING)["attributes"])
    check("53 the pendant follows the sofa column (20 %), the kitchen spots stay off (No Action)",
          home.wait(RGBWW, lambda s: s["state"] == "on", 4) and abs(brightness_pct(home, RGBWW) - 20) <= 1 and home.value(KITCHEN) == "off",
          [brightness_pct(home, RGBWW), home.value(KITCHEN)])
    select("input_select.mood", "Dagur")
    check("53 the mood changes while the sofa is used: the lamp follows (80 %)",
          home.wait(CEILING, lambda s: abs(round((s["attributes"].get("brightness") or 0) / 2.55) - 80) <= 1, 5), home.state(CEILING)["attributes"].get("brightness"))
    home.set("kitchen_zone_drv", True)
    check("53 kitchen occupied too: the spots go on (100 %, unset in the sofa column), the pendant keeps the sofa's 20 % (priority)",
          home.wait(KITCHEN, "on", 5) and abs(brightness_pct(home, KITCHEN) - 100) <= 1 and abs(brightness_pct(home, RGBWW) - 20) <= 1,
          [brightness_pct(home, KITCHEN), brightness_pct(home, RGBWW)])
    home.set("sofa_zone_drv", False)
    check("53 the sofa is left: the kitchen column applies to the pendant (50 %); the room stays occupied",
          home.wait(RGBWW, lambda s: abs(round((s["attributes"].get("brightness") or 0) / 2.55) - 50) <= 1, 5) and home.value(CEILING) == "on",
          brightness_pct(home, RGBWW))
    home.call("light.turn_off", CEILING)
    home.lux(800)
    home.set("sofa_zone_drv", True)
    check("53 bright: “allow ON only when dark” keeps the sofa lamp off (a permission, not a command)", home.stays(CEILING, "off", 4))
    home.set("occ_fresh", False)
    home.set("occ_kamar", False)
    home.set("sofa_zone_drv", False)          # everybody leaves the zones first, then the room
    home.set("kitchen_zone_drv", False)
    check("53 the room's information is stale: the vacancy does nothing", home.stays(KITCHEN, "on", 5))
    home.set("occ_fresh", True)
    home.set("occ_kamar", True)
    time.sleep(1)
    home.set("occ_kamar", False)
    check("53 the room becomes vacant (3 s): every light off (explicit OFF and the lamp's row default)",
          home.wait(KITCHEN, "off", 8) and home.wait(RGBWW, "off", 3), [home.value(KITCHEN), home.value(RGBWW)])

    # ------------------------------------------------- 54. simulation matches the real run
    home.set("sofa_zone_drv", False)
    home.set("kitchen_zone_drv", False)
    _, inputs = app.get(f"api/simulation/{device}")
    trigger = next(t["key"] for t in inputs["triggers"] if "Kitchen zone" in t["text"] and "“on”" in t["text"] and "from" not in t["text"])
    status, sim = app.post("api/designers/simulate", {"device_id": device, "scenario": {"trigger": trigger, "states": {
        "binary_sensor.kitchen_zone": {"state": "on", "origin": "test"}, "binary_sensor.sofa_zone": {"state": "off"},
        "input_select.mood": {"state": "Dagur"}, "sensor.test_lux": {"state": "800"}, "binary_sensor.sod_kamar_occupied": {"state": "on"}}}})
    predicted = {o["entity_id"]: o["final"] for o in sim["outcome"]}
    check("54 the simulation predicts: spots 100 %, pendant 50 %, sofa lamp nothing", predicted[KITCHEN]["data"] == {"brightness_pct": 100}
          and predicted[RGBWW]["data"] == {"brightness_pct": 50} and predicted[CEILING] is None and sim["origins"]["binary_sensor.kitchen_zone"] == "test", predicted)
    home.set("occ_kamar", True)
    home.set("kitchen_zone_drv", True)
    check("54 … and Home Assistant does exactly that", home.wait(KITCHEN, "on", 5) and home.wait(RGBWW, "on", 3)
          and abs(brightness_pct(home, RGBWW) - 50) <= 1 and home.value(CEILING) == "off", [home.value(KITCHEN), brightness_pct(home, RGBWW), home.value(CEILING)])

    # ------------------------------------------------- 55. a position zone
    desk_device = app.add_device("Desk lamp", [OFFICE])
    _, made = app.change("api/designers/create", {"device_id": desk_device, "kind": "lighting"})
    _, page = app.get(f"api/designers/{desk_device}/{made['designer_id']}")
    d = page["designer"]["config"]
    d.update(lights=[OFFICE], primary="presence")
    d["presence"].update(enabled=True, columns=[{"source": desk, "state": "occupied"}, {"source": desk, "state": "vacant"}],
                         cells={OFFICE: {col(desk): on(brightness_pct=70), col(desk, "vacant"): {"kind": "off", "transition": None}}})
    status, saved = app.change("api/designers/save", {"device_id": desk_device, "designer": d})
    check("55 a position zone compiles into a native template", status == 200 and saved["blocking"] == 0 and "namespace(inside=false" in saved["yaml"], saved.get("designer", {}).get("problems"))
    status, _ = app.deploy(desk_device)
    number("radar_x", 700)
    number("radar_y", 900)
    check("55 a target inside the drawn rectangle turns the desk lamp on", home.wait(OFFICE, "on", 6) and abs(brightness_pct(home, OFFICE) - 70) <= 1)
    number("radar_x", 3000)
    check("55 the target leaves the shape: off", home.wait(OFFICE, "off", 6))
    _, view = app.get("api/presence")
    check("55 the live view places the target", next(s for s in view["sources"] if s["id"] == desk)["live"]["targets"] == [[3000.0, 900.0]],
          next(s for s in view["sources"] if s["id"] == desk)["live"])

    # ------------------------------------------------- 56. covers and the real sun
    sun = home.state("sun.sun")["attributes"]
    blinds = app.add_device("Blinds", [WINDOW, AWNING])
    _, gallery = app.get(f"api/gallery/{blinds}")
    check("56 covers get the cover templates", {t["designer"] for t in gallery["templates"]} == {"cover"}, [t["id"] for t in gallery["templates"]])
    _, made = app.change("api/gallery/apply", {"device_id": blinds, "template": "sun_shading"})
    _, page = app.get(f"api/designers/{blinds}/{made['designer_id']}")
    caps = page["capabilities"]
    check("56 capabilities come from Home Assistant (the demo window tilts, the awning is an awning)", caps[WINDOW]["tilt"] and caps[WINDOW]["position"]
          and caps[AWNING]["weather_sensitive"] and not caps[AWNING]["tilt"], [caps[WINDOW], caps[AWNING]])
    c = page["designer"]["config"]
    facing = round(((sun["azimuth"] - 180 + 180) % 360) - 180)  # the window faces the sun now (0° = South)
    window = {"orientation": facing, "offset": None, "left": 60, "right": 60, "elevation_min": -10, "elevation_max": None,
              "position": {"kind": "fixed", "value": 30}, "tilt": None, "leave": None, "weather_sensitive": False}
    c.update(covers=[WINDOW, AWNING])
    c["sun"].update(windows={WINDOW: window, AWNING: {**window, "position": {"kind": "fixed", "value": 100}}})
    c["modes"] = {"enabled": True, "names": ["Daylight", "Privacy", "Sleep"], "driver": "selector", "entity_id": "input_select.cover_mode",
                  "cells": {WINDOW: {"Daylight": {"kind": "position", "position": 100, "tilt": None},
                                     "Privacy": {"kind": "position", "position": 20, "tilt": None}, "Sleep": {"kind": "close"}},
                            AWNING: {"Sleep": {"kind": "close"}}},
                  "sun_policy": {"Daylight": "sun_wins", "Privacy": "mode_wins"}, "mood_entity": None, "mood_map": {}, "row_defaults": {}, "default": None}
    status, saved = app.change("api/designers/save", {"device_id": blinds, "designer": c})
    codes = sorted({p["code"] for p in saved["designer"]["problems"] if p["level"] == "blocking"})
    check("56 an awning is not left to sun shading without weather protection; a mode needs its sun policy",
          status == 200 and codes == ["no_sun_policy", "weather_safety"], codes)
    c["sun"]["windows"][AWNING]["weather_sensitive"] = True
    c["sun"]["safety"] = {"wind_entity": "sensor.test_wind", "wind_above": 10, "rain_entity": "binary_sensor.test_rain", "acknowledged": False}
    c["modes"]["sun_policy"]["Sleep"] = "mode_wins"
    status, saved = app.change("api/designers/save", {"device_id": blinds, "designer": c})
    check("56 with protection and policies the cover designer is complete", status == 200 and saved["blocking"] == 0,
          [p["message"] for p in saved["designer"]["problems"] if p["level"] == "blocking"])
    status, _ = app.deploy(blinds)
    check("56 deployed (one automation)", status == 200 and [m for m in home.managed() if blinds in m] == [f"automation_builder_{blinds}"])
    select("input_select.cover_mode", "Privacy")
    check("56 Privacy: the mode wins over the sun (20 %)", home.wait(WINDOW, lambda s: s["attributes"].get("current_position") == 20, 25),
          home.state(WINDOW)["attributes"].get("current_position"))
    select("input_select.cover_mode", "Daylight")
    check("56 Daylight: the sun shines in, so the sun shading wins (30 %); the awning extends (calm)",
          home.wait(WINDOW, lambda s: s["attributes"].get("current_position") == 30, 25) and home.wait(AWNING, lambda s: s["attributes"].get("current_position") == 100, 5),
          [home.state(WINDOW)["attributes"].get("current_position"), home.state(AWNING)["attributes"].get("current_position")])
    number("wind_drv", 15)
    check("56 wind above the limit: the awning is retracted", home.wait(AWNING, "closed", 5), home.state(AWNING))
    select("input_select.cover_mode", "Privacy")
    time.sleep(1)
    select("input_select.cover_mode", "Daylight")
    check("56 while it is windy the awning is not extended again", home.stays(AWNING, "closed", 4))
    number("wind_drv", 2)

    # ------------------------------------------------- 57. without the App
    stack = HERE / "stack.sh"
    subprocess.run([str(stack), "stop-app"], check=True)
    home.set("occ_kamar", True)
    home.set("sofa_zone_drv", False)
    home.set("kitchen_zone_drv", False)
    time.sleep(1)
    home.set("kitchen_zone_drv", True)
    check("57 App stopped: the designer's automation still runs (kitchen → spots on)", home.wait(KITCHEN, "on", 6))
    select("input_select.cover_mode", "Sleep")
    check("57 App stopped: cover modes still work (Sleep closes)", home.wait(WINDOW, "closed", 25), home.state(WINDOW)["state"])
    subprocess.run([str(stack), "start-app"], check=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and app.get("api/health")[0] != 200:
        time.sleep(1)
    time.sleep(3)
    check("57 after the start the devices are in sync", all(app.status(x) == "in_sync" for x in (device, desk_device, blinds)),
          [app.status(x) for x in (device, desk_device, blinds)])

    # ------------------------------------------------- 58. blueprint of a designer
    status, bp = app.get(f"api/blueprint/{device}")
    keys = {i["key"]: i for i in bp["inputs"]}
    wanted = [k for k in keys if k.startswith("controlled_") or k in ("input_select_mood", "binary_sensor_kitchen_zone", "binary_sensor_sofa_zone",
                                                                     "binary_sensor_sod_kamar_occupied", "sensor_test_lux")]
    check("58 the designer's entities and values are blueprint inputs, each once", {"input_select_mood", "sensor_test_lux", "binary_sensor_sofa_zone"} <= set(keys)
          and len(keys) == len(bp["inputs"]), sorted(keys))
    settings = {"name": "Mood and zones", "description": "", "automation": None, "inputs": {k: {"name": "", "description": ""} for k in wanted}}
    status, preview = app.post("api/blueprint/preview", {"device_id": device, "settings": settings})
    app.change("api/devices/update", {"device_id": device, "patch": {"blueprint": settings}})
    status, bp = app.get(f"api/blueprint/{device}")
    check("58 the blueprint validates in Home Assistant and equals the automation at its defaults", bp["valid"]
          and any(v["level"] == "ok" and "accepts" in v["message"] for v in bp["validation"]), bp["validation"])
    status, installed = app.post("api/blueprint/install", {"device_id": device, "overwrite": True})
    check("58 installed through Home Assistant's blueprint command", status == 200, installed)
    inputs = {k: v["value"] for k, v in keys.items() if k in wanted}
    inputs = {**inputs, next(k for k in wanted if k == f"controlled_{'ceiling_lights'}"): "light.bed_light",
              "input_select_mood": "input_select.main_mood"}
    status, _ = request(f"{HA}/api/config/automation/config/from_designer_blueprint", "POST", {
        "alias": "From the designer blueprint", "use_blueprint": {"path": bp["path"], "input": inputs}}, home.headers)
    home.call("light.turn_off", "light.bed_light")
    select("input_select.main_mood", "Dagur")
    subprocess.run([str(stack), "stop-app"], check=True)
    home.set("kitchen_zone_drv", False)
    home.set("sofa_zone_drv", False)
    home.lux(50)
    select("input_select.main_mood", "Hygge")
    time.sleep(1)
    home.set("sofa_zone_drv", True)
    check("58 an automation made from the blueprint, with another lamp and mood selector, runs without the App",
          status == 200 and home.wait("light.bed_light", lambda s: s["state"] == "on" and s["attributes"].get("color_temp_kelvin") == 2700, 8),
          home.state("light.bed_light"))
    subprocess.run([str(stack), "start-app"], check=True)
    request(f"{HA}/api/config/automation/config/from_designer_blueprint", "DELETE", None, home.headers)
    for entity_id in paused:
        home.call("automation.turn_on", entity_id)
    return summary(args.out)


if __name__ == "__main__":
    sys.exit(main())
