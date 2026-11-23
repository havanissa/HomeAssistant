"""End-to-end checks of the room-first selection and the Default Rule (0.2.1): the real App image against a real
Home Assistant, whose demo devices were given areas by ``prepare.py``.

    python e2e_03_checks.py <tokens.json> [--out results.json]

Runs on the local stack (default) or a Home Assistant OS test system (variables of ``haos.py env``), on a fresh
installation or after the other scenarios; it uses its own device.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

from e2e_checks import skip, App, Home, brightness_pct, call, check, summary, t_state

LAMP = "light.living_room_rgbww_lights"
FAN = "fan.living_room_fan"
MOTION = "binary_sensor.test_motion"
DOOR = "binary_sensor.test_door"
LUX = "sensor.test_lux"
LATE = "input_boolean.guest_mode"
MANUAL = "input_boolean.manual_override"


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    home, app = Home(json.loads(Path(args.tokens).read_text())["llat"]), App()
    app.get("api/overview")
    for helper in ("test_motion", "ab_door", "guest_mode", "manual_override"):
        home.set(helper, False)
    home.lux(500)

    # ------------------------------------------------- 40. rooms -> devices -> entities
    _, found = app.get("api/rooms")
    rooms = {r["name"]: r for r in found["rooms"]}
    check("40 Home Assistant's areas are the rooms, with an Unassigned entry last", {"Kitchen", "Bedroom", "Living Room", "Office", "Hall"} <= set(rooms)
          and found["rooms"][-1]["name"] == "Unassigned" and found["rooms"][-1]["area_id"] is None, list(rooms))
    # The demo's Sonos device ("Living Room", 9 entities) exists only in Home Assistant instances whose registry
    # predates 2026.11 (the current demo creates its media players without a device): its checks are then SKIPPED.
    _, probe = app.get(f"api/room-devices?area={rooms['Living Room']['area_id']}")
    has_sonos = any(d["name"] == "Living Room" for d in probe["devices"])
    check("40 a room shows how much there is to control", rooms["Living Room"]["devices"] == (3 if has_sonos else 2)
          and rooms["Living Room"]["entities"] >= 3, rooms["Living Room"])
    _, searched = app.get("api/rooms?q=liv")
    check("40 rooms can be searched", [r["name"] for r in searched["rooms"]] == ["Living Room"], searched)
    living = rooms["Living Room"]["area_id"]
    _, listed = app.get(f"api/room-devices?area={living}")
    names = [d["name"] for d in listed["devices"]]
    check("40 only the devices of the chosen room are listed, entities without a device as one entry at the end",
          names == ["Ceiling Lights", *(["Living Room"] if has_sonos else []), "Living Room RGBWW Lights", "Entities without a device"], names)
    sonos = next((d for d in listed["devices"] if d["name"] == "Living Room"), None)
    if sonos is None:
        skip("40 a physical device with several entities brings all its controllable ones, with names and domains",
             "the demo's Sonos device does not exist in this Home Assistant (2026.11 demo media players have no device)")
    else:
        check("40 a physical device with several entities brings all its controllable ones, with names and domains",
              len(sonos["entities"]) == 9 and sonos["domains"] == ["media_player", "number", "switch"] and sonos["model"] == "Sonos Play:5"
              and all(e["name"] and e["domain_label"] for e in sonos["entities"]), sonos["domains"])
    check("40 an entity that has a room but no device is offered in that room", [e["entity_id"] for e in listed["devices"][-1]["entities"]] == [FAN]
          and listed["devices"][-1]["device_id"] is None, listed["devices"][-1])
    _, kitchen = app.get(f"api/room-devices?area={rooms['Kitchen']['area_id']}")
    check("40 sensors are not offered as something to control", all(e["controllable"] for d in kitchen["devices"] for e in d["entities"])
          and [d["name"] for d in kitchen["devices"]] == ["Kitchen Lights", "Kitchen Window"], [d["name"] for d in kitchen["devices"]])
    _, loose = app.get("api/room-devices")
    entry = next((d for d in loose["devices"] if d["device_id"] is None), None)
    check("40 Unassigned: entities without a physical device come as one entry", entry is not None and "light.flaky_lamp" in [e["entity_id"] for e in entry["entities"]]
          and "input_select.mood" in [e["entity_id"] for e in entry["entities"]], [d["name"] for d in loose["devices"]])
    status, missing = app.get("api/room-devices?area=nope")
    check("40 an unknown room is a clear error", status == 404, missing)

    # ------------------------------------------------- 41. room of a managed device, groups
    device = app.add_device("Living room lamp", [LAMP])
    mixed = app.add_device("Fan and flaky lamp", [FAN, "light.flaky_lamp"])
    across = app.add_device("Fan and bed light", [FAN, "light.bed_light"])
    _, overview = app.get("api/overview")
    by_id = {d["id"]: d for d in overview["devices"]}
    groups = {g["name"]: g for g in overview["rooms"]}
    check("41 a device is grouped under the room of its entities", by_id[device]["room"]["name"] == "Living Room" and device in groups["Living Room"]["device_ids"], by_id[device]["room"])
    check("41 entities from a room and from none: listed once, under Multiple rooms", by_id[mixed]["room"]["kind"] == "multiple"
          and mixed in groups["Multiple rooms"]["device_ids"] and sum(mixed in g["device_ids"] for g in overview["rooms"]) == 1, by_id[mixed]["room"])
    check("41 entities of two rooms: one device, listed once, naming both rooms", by_id[across]["room"]["kind"] == "multiple"
          and [a["name"] for a in by_id[across]["room"]["areas"]] == ["Living Room", "Bedroom"]
          and sum(across in g["device_ids"] for g in overview["rooms"]) == 1, by_id[across]["room"])
    check("41 every device is in exactly one group", sorted(i for g in overview["rooms"] for i in g["device_ids"]) == sorted(by_id))
    for extra in (mixed, across):
        app.change("api/devices/delete", {"device_id": extra, "overwrite": False})

    # ------------------------------------------------- 42. the room filter of the pickers
    _, everything = app.get("api/entities?purpose=any&q=test")
    _, in_room = app.get(f"api/entities?purpose=any&q=&area={living}")
    ids = [e["entity_id"] for e in in_room["results"]]
    check("42 with the filter only entities of the room are offered (own area or the device's)", LAMP in ids and FAN in ids and MOTION not in ids and LUX not in ids
          and len(everything["results"]) >= 3, ids[:8])
    _, kept = app.get(f"api/entities?purpose=any&q=&area={living}&keep={LUX}")
    check("42 the entity chosen now stays listed although it is in another room", kept["results"][0]["entity_id"] == LUX and kept["results"][0]["current"] is True)
    _, numeric = app.get(f"api/entities?purpose=numeric&q=&area={rooms['Kitchen']['area_id']}")
    check("42 the filter combines with the kind of entity a picker asks for", [e["entity_id"] for e in numeric["results"]] == [LUX], numeric)

    # ------------------------------------------------- 43. rules across rooms, and the Default Rule
    dark = app.add_rule(device, "Dark", [t_state(MOTION)], [call("light.turn_on", brightness_pct=80)],
                        [{"type": "state", "entity_id": MOTION, "state": "on", "negate": False, "for_seconds": 0},
                         {"type": "numeric", "entity_id": LUX, "below": 100, "above": None}])
    late = app.add_rule(device, "Late", [t_state(MOTION)], [call("light.turn_on", brightness_pct=10)],
                        [{"type": "state", "entity_id": MOTION, "state": "on", "negate": False, "for_seconds": 0},
                         {"type": "state", "entity_id": LATE, "state": "on", "negate": False, "for_seconds": 0}])
    view = app.device(device)
    check("43 a rule may use sensors of another room (the lamp is in the living room, the sensors in the kitchen)", view["room"]["name"] == "Living Room"
          and view["entity_problems"] == [] and view["default_summary"] is None)
    conflict = next((c for c in view["conflicts"] if c["level"] == "blocking"), None)
    check("43 two rules that can both set the lamp are still a conflict to confirm (unchanged by the default)", conflict is not None and conflict["code"] == "competing", view["conflicts"])
    app.change("api/devices/update", {"device_id": device, "patch": {"acknowledge": conflict["key"]}})  # "Confirm this order"
    status, deployed = app.deploy(device)
    auto = f"automation_builder_{device}"
    without = home.automation(auto)[1]
    check("43 deployed without a default: one block per rule, as always", status == 200 and len(without["actions"]) == 2 and "else" not in without["actions"][0])
    home.call("light.turn_on", LAMP)
    home.set("test_motion", True)
    check("43 without a default nothing happens when no rule matches", home.stays(LAMP, "on", 3))
    home.set("test_motion", False)
    time.sleep(1)

    status, preview = app.post("api/default/preview", {"device_id": device, "default_rule": {"enabled": True, "actions": []}})
    check("43 an enabled default needs an action", status == 200 and preview["valid"] is False, preview)
    status, preview = app.post("api/default/preview", {"device_id": device, "default_rule": {"enabled": True, "actions": [
        {"type": "delay", "seconds": 5}, call("light.turn_off")]}})
    check("43 a default that waits is refused, with the reason", preview["valid"] is False and "cannot wait" in preview["problems"][0]["message"], preview)
    default = {"enabled": True, "actions": [call("light.turn_off")], "override_entity": MANUAL}
    status, preview = app.post("api/default/preview", {"device_id": device, "default_rule": default})
    check("43 preview of the default: readable, and part of the device's one automation", preview["valid"] and preview["active"]
          and preview["summary"]["text"].startswith("DEFAULT (no rule matched)") and "alias: Default" in preview["yaml"], preview.get("summary"))
    check("43 a preview saves nothing", app.device(device)["device"].get("default_rule") is None)
    status, _ = app.change("api/devices/update", {"device_id": device, "patch": {"default_rule": default}})
    check("43 saving the default deploys nothing", status == 200 and home.automation(auto)[1] == without and app.status(device) == "changes_pending")
    plan = app.plan(device)
    check("43 Home Assistant validates the automation with the default", plan["can_deploy"] and not plan["items"][0]["errors"], plan["reasons"])
    status, _ = app.deploy(device)
    with_default = home.automation(auto)[1]
    wrapper = with_default["actions"][0]
    check("43 deployed: still one automation, the rules unchanged inside, the default behind them", status == 200 and len(home.managed()) == len(set(home.managed()))
          and wrapper["then"] == without["actions"] and wrapper["else"][0]["alias"] == "Default" and with_default["mode"] == "queued"
          and with_default["triggers"] == without["triggers"], wrapper.get("alias"))

    def motion(on: bool) -> None:
        home.set("test_motion", on)
        time.sleep(1.5)

    home.call("light.turn_on", LAMP, brightness_pct=50)
    time.sleep(1)
    motion(True)                                   # bright, not late: no rule matches
    check("44 no rule matches: the default runs (lamp off)", home.wait(LAMP, "off", 5))
    home.lux(40)
    motion(False)
    check("44 ...also when the motion ends", home.stays(LAMP, "off", 2))
    motion(True)                                   # dark: the first rule
    check("44 a rule matches: it runs and the default does not", home.wait(LAMP, "on", 5) and home.stays(LAMP, "on", 3) and brightness_pct(home, LAMP) == 80, brightness_pct(home, LAMP))
    home.set("guest_mode", True)
    motion(False)
    home.wait(LAMP, "off", 5)
    motion(True)                                   # dark and late: both rules, in order
    check("44 two rules match: both run in the listed order (the last word: 10 %), no default", home.wait(LAMP, "on", 5) and home.stays(LAMP, "on", 3) and brightness_pct(home, LAMP) == 10, brightness_pct(home, LAMP))
    home.lux(900)
    home.set("guest_mode", False)
    check("44 conditions changing without a trigger call for nothing: no state is enforced", home.stays(LAMP, "on", 5))
    home.set("manual_override", True)
    motion(False)
    check("44 the manual override signal keeps the default away", home.stays(LAMP, "on", 3))
    home.set("manual_override", False)
    motion(True)
    check("44 ...and without it the default runs again", home.wait(LAMP, "off", 5))
    diag = app.device(device)["rule_diagnostics"][dark]
    check("44 the rule diagnostics still find the rules inside the automation", (diag["executions"] or 0) >= 2 and (diag["blocks"] or 0) >= 1, {k: diag[k] for k in ("runs", "executions", "blocks")})

    # disabled: exactly the automation of before
    app.change("api/devices/update", {"device_id": device, "patch": {"default_rule": {**default, "enabled": False}}})
    status, _ = app.deploy(device)
    check("45 a disabled default is not in the automation: byte for byte what it was without one", status == 200 and home.automation(auto)[1] == without)
    home.call("light.turn_on", LAMP)
    motion(False)
    check("45 ...and nothing happens when no rule matches", home.stays(LAMP, "on", 3))
    _, overview = app.get("api/overview")
    check("45 the configured default is kept, shown as off", app.device(device)["device"]["default_rule"]["enabled"] is False
          and next(d for d in overview["devices"] if d["id"] == device)["default"] is False)
    status, removed = app.change("api/devices/delete", {"device_id": device, "overwrite": False})
    check("45 clean-up", status == 200 and auto not in home.managed(), removed)
    _ = late
    return summary(args.out)


if __name__ == "__main__":
    sys.exit(main())
