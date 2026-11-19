"""End-to-end checks of Phase A6.2 (App 0.9.0) with a real Home Assistant, the companion 1.0.0 and the real App image.

    python -I a62_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]

Runs right after ``system_checks.py --phase fresh`` in the same stack (Kamar Norður with the template radar and door,
``light.bed_light`` at the Dagur level, the App owning every domain through the companion):

* Presence zones: a zone created and edited through Presence's commands (detection only; a lighting key refused);
  Lighting sees the same zone and adds its light rule; saving sends nothing; deleting needs a confirmation.
* The light inspector: what controls ``light.bed_light`` (rule, last command, reported state, device).
* Identify on a light that is on (dimmed, with its colour) and off: full brightness, then exactly the previous
  state, through the companion's executor; no manual override; exactly two commands each.
* Brightness delays: the zone's cell switches the light on after its Delay On and off after its Delay Off (the
  room stays occupied); the inspector shows the pending action; one command each.
* The interface: Presence → Zones, Lighting → Zone lighting, the inspector drawer, the matrix cell editor with its
  delays (desktop, tablet, phone), without a single service call.

People and the door are moved by writing states (``POST /api/states``), never by a service call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, results  # noqa: E402
from system_checks import (  # noqa: E402
    INGRESS as _I, KAMAR, LIGHT, MAIN, PC, POST, app, app_post, command, light_calls, pct, state, wait, write_state)
from rooms_checks import http  # noqa: E402

# The desk's presence sensor (a template entity whose state the run writes): added to Kamar Norður for the zone.
# (A door contact cannot make a zone occupied: doors only count as movement.)
DOOR = "binary_sensor.pir_stova"


def add_desk_sensor() -> None:
    export = http(_I + "api/config/export", POST)
    rooms = export["options"]["rooms"]
    sensors = [s for s in rooms[KAMAR]["sensors"] if s["entity_id"] != DOOR]
    sensors.append({"entity_id": DOOR, "kind": "radar", "role": "maintain", "inverted": False})
    doc = {**export, "options": {**export["options"], "rooms": {**rooms, KAMAR: {**rooms[KAMAR], "sensors": sensors}}}}
    st, review = app_post("api/config/import/review", {"file": doc})
    check("desk sensor: import reviewed", st == 200, review)
    st, imported = app_post("api/config/import", {"file": doc, "revision": app("api/system")["config"]["revision"]})
    check("desk sensor added to the room", st == 200, imported)


def zones(kind: str, **params: Any) -> tuple[int, Any]:
    return app_post("api/lighting/command", {"type": f"smart_occupancy/zones/{kind}", **params})


def inspect() -> dict[str, Any]:
    st, body = command("light/inspect", entity_id=LIGHT, area_id=KAMAR)
    assert st == 200, body
    return body["result"]


def desk_active() -> bool | None:
    st, body = zones("room", area_id=KAMAR)
    zone = next((z for z in body["result"]["zones"] if z["id"] == "desk"), None) if st == 200 else None
    return (zone.get("status") or {}).get("active") if zone else None


def look(args: argparse.Namespace) -> dict[str, Any]:
    attrs = (state(args, LIGHT) or {}).get("attributes") or {}
    return {k: attrs.get(k) for k in ("brightness", "color_mode", "color_temp_kelvin", "hs_color", "rgb_color", "effect")}


def phase(args: argparse.Namespace, out: Path) -> None:
    w = args.watcher
    write_state(args, PC, "on")
    write_state(args, DOOR, "off")
    wait("the light at the Dagur level (fresh phase)", lambda: pct(args) == 80, 60)
    add_desk_sensor()
    wait("the desk sensor is a presence signal of the room", lambda: DOOR in [
        x["entity_id"] for x in zones("room", area_id=KAMAR)[1]["result"]["sensors"]], 60, 2)
    wait("the light still at the Dagur level", lambda: pct(args) == 80, 60)
    time.sleep(3)

    # ----------------------------------------------------------- Presence zones
    st, room = zones("room", area_id=KAMAR)
    check("Presence → Zones answers (App)", st == 200 and room["result"]["available"] and room["result"]["zones"] == [], room)
    start = len(w.events)
    st, body = zones("set", area_id=KAMAR, zone={"name": "Desk", "sensors": [DOOR], "holds_room": False,
                                                  "timing": {"inactivity_timeout": 30, "exit_confirmation": 0}})
    check("a zone created from Presence (stable id, nothing sent)", st == 200 and body["result"]["zone"]["id"] == "desk"
          and body["result"]["sent"] is False, body)
    st, body = zones("set", area_id=KAMAR, zone={"id": "desk", "targets": [{"entity_id": LIGHT}]})
    check("Presence refuses a lighting setting", st == 400 and body.get("code") == "not_presence", body)
    st, editor = command("editor", area_id=KAMAR)
    names = [z["name"] for z in editor["result"]["room"]["zones"]["zones"]]
    check("Lighting sees the same zone", st == 200 and names == ["Desk"], names)
    st, body = command("zone/set", area_id=KAMAR, zone={"id": "desk", "targets": [{"entity_id": LIGHT, "brightness_pct": 60}]})
    check("Lighting adds the zone's light rule", st == 200, body)
    st, body = zones("set", area_id=KAMAR, zone={"id": "desk", "name": "Desk area"})
    zone = next(z for z in command("editor", area_id=KAMAR)[1]["result"]["room"]["zones"]["zones"] if z["id"] == "desk")
    check("a Presence edit keeps the light rule and the id", st == 200 and zone["name"] == "Desk area"
          and zone["targets"][0]["entity_id"] == LIGHT, zone)
    time.sleep(3)
    check("saving zones sent nothing", not light_calls(args, start), light_calls(args, start)[:3])

    # ----------------------------------------------------------- inspector
    ins = inspect()
    c = ins["control"]
    check("inspector: the winning rule, the last command and the reported state",
          c["mode"] == "automatic" and (c["winning"]["requested"] or {}).get("brightness_pct") == 80
          and c["reported"]["brightness_pct"] == 80 and c["last_command"] is not None, c)
    check("inspector: device and capabilities", ins["device"]["integration"] == "demo" and ins["device"]["capabilities"]["brightness"]
          and ins["can_identify"] is True, ins["device"])

    # ----------------------------------------------------------- Identify (on)
    before = look(args)
    start = len(w.events)
    st, body = command("light/identify", entity_id=LIGHT, area_id=KAMAR)
    check("Identify (on): restored", st == 200 and body["result"]["outcome"] == "restored", body)
    calls = light_calls(args, start)
    check("Identify (on): full brightness, then one restore", len(calls) == 2
          and calls[0]["service_data"].get("brightness_pct") == 100 and calls[1]["service"] == "turn_on", calls)
    time.sleep(1.5)
    check("Identify (on): exactly the previous brightness and colour", look(args) == before, (before, look(args)))
    c = inspect()["control"]
    check("Identify left no manual trace", c["override"] is None and c["last_external"] is None, (c["override"], c["last_external"]))

    # ----------------------------------------------------------- brightness delays
    st, body = command("ambient/set", area_id=KAMAR, enabled=True)
    check("ambient brightness control on", st == 200, body)
    levels = [lv["key"] for lv in command("editor", area_id=KAMAR)[1]["result"]["room"]["brightness"]["levels"]]
    matrix = {"enabled": True, "room": {k: 0 for k in levels}, "zones": {"desk": {k: 50 for k in levels}},
              "delays": {"desk": {k: {"on": 4, "off": 6} for k in levels}}}
    start = len(w.events)
    st, preview = command("brightness/preview", area_id=KAMAR, entity_id=LIGHT, matrix=matrix)
    st2, saved = command("brightness/set", area_id=KAMAR, entity_id=LIGHT, matrix=matrix, confirm=True)
    check("matrix with delays enrolled", st == 200 and st2 == 200 and saved["result"]["matrix"]["delays"]["desk"][levels[0]] == {"on": 4, "off": 6}, saved)
    time.sleep(3)
    check("saving the matrix sent nothing", not light_calls(args, start), light_calls(args, start)[:3])
    write_state(args, MAIN, "Kvøld")  # a trigger: the matrix's Room occupied cell (Off) applies
    wait("the light off (Room occupied cell: Off)", lambda: (state(args, LIGHT) or {}).get("state") == "off", 30)
    time.sleep(2)

    # Identify (off): on at full brightness, then off again.
    start = len(w.events)
    st, body = command("light/identify", entity_id=LIGHT, area_id=KAMAR)
    calls = light_calls(args, start)
    check("Identify (off): restored to off", st == 200 and body["result"]["outcome"] == "restored"
          and [x["service"] for x in calls] == ["turn_on", "turn_off"], (body, calls))
    time.sleep(1.5)
    check("Identify (off): the light is off", (state(args, LIGHT) or {}).get("state") == "off")

    # Delay On.
    start = len(w.events)
    t0 = time.time()
    write_state(args, DOOR, "on")
    wait("the Desk zone active", lambda: desk_active() is True, 20, 0.5)
    pend = wait("Delay On pending in the inspector", lambda: (inspect()["control"]["delay"] or {}).get("pending"), 5, 0.3)
    check("Delay On pending: action, condition, remaining", pend["action"] == "on" and pend["seconds"] == 4
          and "Desk area" in pend["condition"], pend)
    check("nothing sent during Delay On", not light_calls(args, start), light_calls(args, start))
    wait("the light on after its Delay On", lambda: pct(args) == 50, 20, 0.3)
    took = time.time() - t0
    check(f"switched on after the delay ({took:.1f} s)", took >= 3.5, took)
    on_calls = light_calls(args, start)
    check("exactly one command for the switch-on", len(on_calls) == 1, on_calls)

    # Delay Off: the zone ends (30 s after the last door movement) while the room stays occupied.
    write_state(args, DOOR, "off")
    start = len(w.events)
    try:
        wait("the Desk zone ended", lambda: desk_active() is False, 150, 1)
    except SystemExit:
        (out / "a62-diagnose.json").write_text(json.dumps({"zones": zones("room", area_id=KAMAR)[1], "sensor": state(
            args, DOOR), "inspect": inspect()}, indent=1, default=str))
        raise
    t1 = time.time()
    pend = wait("Delay Off pending", lambda: (inspect()["control"]["delay"] or {}).get("pending"), 5, 0.3)
    check("Delay Off pending, light kept on", pend["action"] == "off" and pct(args) == 50, pend)
    wait("the light off after its Delay Off", lambda: (state(args, LIGHT) or {}).get("state") == "off", 20, 0.3)
    took = time.time() - t1
    check(f"switched off after the delay ({took:.1f} s)", took >= 4.0, took)
    off_calls = light_calls(args, start)
    check("exactly one command for the switch-off", [x["service"] for x in off_calls] == ["turn_off"], off_calls)
    history = [e["event"] for e in inspect()["control"]["delay"]["history"]]
    check("the delay history: started and executed", history[:2] == ["executed", "started"], history)

    # ----------------------------------------------------------- the interface (no service call)
    start = len(w.events)
    if args.chromium:
        browser_checks(args, out)
    time.sleep(1)
    check("the interface sent nothing", not light_calls(args, start), light_calls(args, start)[:3])

    # ----------------------------------------------------------- deleting the zone
    st, body = zones("remove", area_id=KAMAR, zone_id="desk")
    check("deleting a zone lights respond to needs a confirmation", st == 409 and body.get("code") == "confirmation_required", body)
    st, body = zones("remove", area_id=KAMAR, zone_id="desk", confirm=True)
    m = next(x for x in command("editor", area_id=KAMAR)[1]["result"]["room"]["brightness"]["lights"] if x["entity_id"] == LIGHT)
    check("zone deleted with its matrix column and delays", st == 200 and "desk" not in (m["matrix"]["zones"] or {})
          and "desk" not in (m["matrix"].get("delays") or {}), (body, m["matrix"]))


def browser_checks(args: argparse.Namespace, out: Path) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    shots = out / "screenshots"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        for label, size in (("desktop", (1440, 900)), ("tablet", (1024, 768)), ("phone", (390, 844))):
            page = browser.new_page(viewport={"width": size[0], "height": size[1]})
            errors: list[str] = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.goto(PANEL)
            frame = app_frame(page)
            frame.goto(INGRESS + f"#/presence/{KAMAR}?tab=zones")
            ok = frame.wait_for_selector("[data-testid=presence-zones-editor]", timeout=20000) is not None
            time.sleep(1)
            check(f"{label}: Presence → Zones shows the zone", ok and "Desk area" in frame.inner_text("[data-testid=presence-zones-editor]"))
            frame.click("[data-zone=desk]")
            frame.wait_for_selector("[data-testid=lighting-zone-form]", timeout=5000)
            check(f"{label}: the zone's detection form (no lighting settings)", frame.locator("[data-testid=presence-zone-save]").count() == 1
                  and frame.locator("[data-testid=zone-lighting-uses]").count() == 1)
            page.screenshot(path=str(shots / f"a62-presence-zones-{label}.png"), full_page=True)
            frame.goto(INGRESS + f"#/lighting/{KAMAR}/zones")
            frame.wait_for_selector("[data-testid=lighting-zones]", timeout=20000)
            time.sleep(1)
            check(f"{label}: Lighting → Zone lighting links to Presence", frame.locator("[data-testid=lighting-zones-presence-link]").count() == 1)
            page.screenshot(path=str(shots / f"a62-zone-lighting-{label}.png"), full_page=True)
            frame.goto(INGRESS + f"#/lighting/{KAMAR}/overview")
            frame.wait_for_selector(f"[data-testid=lighting-lights-now] [data-inspect='{LIGHT}']", timeout=20000)
            frame.click(f"[data-testid=lighting-lights-now] [data-inspect='{LIGHT}']")
            frame.wait_for_selector("[data-testid=light-control]", timeout=10000)
            time.sleep(1)
            text = frame.locator("[data-testid=light-inspector]").text_content() or ""
            check(f"{label}: the inspector answers what controls the light", "What is controlling it now?" in text
                  and "Winning rule wants" in text and "Device reports" in text and "Identify" in text, text[:300])
            width = frame.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
            check(f"{label}: no horizontal page scroll with the inspector", width[0] <= width[1] + 1, width)
            page.screenshot(path=str(shots / f"a62-inspector-{label}.png"))
            frame.press("[data-testid=light-inspector]", "Escape")
            frame.goto(INGRESS + f"#/lighting/{KAMAR}/brightness")
            frame.wait_for_selector("[data-testid=lighting-matrices]", timeout=20000)
            frame.locator(".lp-pick", has_text="Bed Light").first.click()
            frame.wait_for_selector("[data-testid=lighting-matrix]", timeout=10000)
            check(f"{label}: cells with delays are marked", frame.locator("[data-testid=lighting-matrix] .lp-cell[data-delay]").count() >= 1)
            frame.locator("[data-testid=lighting-matrix] .lp-cell[data-delay]").first.click()
            frame.wait_for_selector("[data-testid=lighting-cell-delays]", timeout=5000)
            editor = frame.inner_text("[data-testid=lighting-cell-editor]")
            check(f"{label}: the cell editor shows Delay On and Delay Off", "Delay On" in editor and "Delay Off" in editor
                  and "Zone: Desk area" in editor, editor[:200])
            page.screenshot(path=str(shots / f"a62-matrix-cell-{label}.png"), full_page=True)
            check(f"{label}: no page error", not [e for e in errors if e != "Object"], errors)
            page.close()
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chromium", default=None)
    args = parser.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(10)
    try:
        phase(args, out)
    except SystemExit:
        pass
    except Exception as err:  # noqa: BLE001
        check("phase a62 crashed", False, f"{type(err).__name__}: {err}")
    finally:
        args.watcher.stop_flag = True
    (out / "a62.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
