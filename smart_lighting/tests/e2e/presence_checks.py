"""End-to-end checks of Phase A3.1 (Presence) through simulated Ingress against a real Home Assistant.

Needs the stack from stack.sh, a real Home Assistant with Smart Occupancy &
Lighting 0.6.10 (Kamar Norður seeded with the template radar
``binary_sensor.radar_kamar``, which follows ``input_boolean.nobara_pc``, and
the door ``binary_sensor.door_kamar``), and Playwright:

    python -I presence_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check]

The driver moves "people" by writing the helper's state (``POST /api/states``,
not a service call), so any service call seen during the run comes from the
App or the integration and fails the run. It checks: the Presence dashboard
and its live update without reloading, the room workspace (live decision,
confirmation, visit chain, timeline), room timing override and restore,
sensor role and inclusion changes with the review's prediction, adding and
removing a sensor (reload), the vacancy countdown, a stale write after an
edit in the integration's own room card command, global defaults, the
diagnostics, the floor plan Presence overlay, desktop and tablet layouts, and
that only allowlisted commands reached Home Assistant. ``--phase
restart-check`` runs after Home Assistant and the App were restarted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any
import urllib.request

from playwright.sync_api import Frame, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import (  # noqa: E402
    BASE, INGRESS, PANEL, READ_ONLY, ServiceCallWatcher, app_frame, check, no_horizontal_overflow, results,
    wait_connected,
)
from rooms_checks import ha_ws, http, styled  # noqa: E402

WRITE = {"smart_occupancy/presence/set", "smart_occupancy/rooms/set", "smart_occupancy/rooms/restore"}
PRESENCE_READS = {"smart_occupancy/presence/get", "smart_occupancy/presence/review", "history/history_during_period"}
KAMAR = "kamar_nordur"
RADAR = "binary_sensor.radar_kamar"
DOOR = "binary_sensor.door_kamar"
PC = "input_boolean.nobara_pc"
STATE_FILE = "presence_e2e_state.json"


def presence(args: argparse.Namespace) -> dict[str, Any]:
    r = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/presence/get", "area_id": KAMAR})
    assert r["success"], r
    return r["result"]


def set_person(args: argparse.Namespace, on: bool) -> None:
    """Someone sits at the PC (the template radar follows the helper). A state write, not a service call."""
    req = urllib.request.Request(f"{args.ha_url}/api/states/{PC}", method="POST",
                                 data=json.dumps({"state": "on" if on else "off",
                                                  "attributes": {"friendly_name": "Nobara PC active"}}).encode(),
                                 headers={"Authorization": f"Bearer {args.ha_token}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def wait_state(args: argparse.Namespace, wanted: str, timeout: float = 40) -> dict[str, Any]:
    end = time.time() + timeout
    while True:
        data = presence(args)
        room = data["rooms"][KAMAR]
        if room["occupancy"] and room["occupancy"]["state"] == wanted:
            return data
        if time.time() > end:
            raise AssertionError(f"state {wanted} not reached: {room['occupancy']}")
        time.sleep(1)


def open_room(frame: Frame, tab: str | None = None) -> None:
    frame.locator(".sidebar [data-nav=presence]").click()
    frame.locator(f"[data-room={KAMAR}] [data-testid=presence-room-link]").click()
    expect(frame.locator("[data-testid=presence-left]")).to_be_visible(timeout=15000)
    if tab:
        frame.locator(f".tabs [data-tab={tab}]").click()


def review(frame: Frame) -> Any:
    frame.locator("[data-testid=presence-review-open]").click()
    dialog = frame.locator("[data-testid=presence-review]")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-testid=review-changes]")).to_be_visible(timeout=15000)
    return dialog


def save(frame: Frame, dialog: Any, confirm: bool = True) -> None:
    if confirm:
        for box in dialog.locator("input[data-confirm]").all():
            box.check()
    dialog.locator("[data-testid=presence-save]").click()
    expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=20000)


def set_timing(frame: Frame, key: str, value: str, unit: str | None = None) -> None:
    row = frame.locator(f"[data-timing={key}]")
    if unit:
        row.locator("select").select_option(unit)
    row.locator("input").fill(value)
    row.locator("input").press("Tab")


def plan_with_kamar() -> dict[str, Any]:
    vs = [("vx_a", 0, 0), ("vx_b", 4, 0), ("vx_c", 4, 3), ("vx_d", 0, 3)]
    return {
        "format": "smart_lighting.floorplan", "version": 1,
        "building": {"id": "bld_e2e", "name": "Home", "north_deg": None},
        "floors": [{
            "id": "fl_e2e", "name": "Ground floor", "level": 0, "ha_floor_id": None, "measured": True,
            "vertices": [{"id": i, "x": x, "y": y} for i, x, y in vs],
            "rooms": [{"id": "rm_kamar", "name": "", "area_id": KAMAR, "area_name": "Kamar Norður", "shares_area": False,
                       "vertices": [v[0] for v in vs]}],
            "walls": [{"id": f"wl_{n}", "a": vs[n][0], "b": vs[(n + 1) % 4][0]} for n in range(4)],
            "openings": [],
            "placements": [
                {"id": "pl_radar", "entity_id": RADAR, "entity_uid": None, "device_id": None, "x": 1.0, "y": 1.0, "label": None},
                {"id": "pl_door", "entity_id": DOOR, "entity_uid": None, "device_id": None, "x": 3.6, "y": 1.5, "label": None},
                {"id": "pl_lamp", "entity_id": "light.bed_light", "entity_uid": None, "device_id": None, "x": 2.0, "y": 2.0, "label": None},
            ],
            "background": None,
        }],
        "dismissed": [], "updated_at": None,
    }


def phase_all(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    shots = out / "screenshots"
    data = presence(args)
    base_timing = dict(data["rooms"][KAMAR]["timing_overrides"])
    base_global = dict(data["global"]["timing"])
    check("integration presence API answers", data["integration_version"] == "0.8.0" and data["writable"],
          data["integration_version"])
    set_person(args, False)
    wait_state(args, "vacant", 900)  # a previous run may have left the room occupied
    watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    watcher.start()
    watcher.ready.wait(10)

    # A floor plan with Kamar Norður and its sensors (through the App's own API, as the editor would save it).
    doc = http(f"{INGRESS}api/floorplan")
    http(f"{INGRESS}api/floorplan/save", {"X-Smart-Lighting": "1"}, "POST",
         {"plan": plan_with_kamar(), "revision": doc.get("revision")})

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(PANEL)
        frame = app_frame(page)
        wait_connected(frame)
        if not styled(frame):
            errors.clear()
            page.reload()
            frame = app_frame(page)
            wait_connected(frame)

        # 1. Dashboard: every configured room, ownership, live update without reloading.
        frame.locator(".sidebar [data-nav=presence]").click()
        card = frame.locator(f"[data-room={KAMAR}]")
        expect(card).to_be_visible(timeout=15000)
        expect(frame.locator("[data-testid=presence-ownership]")).to_contain_text("Smart Occupancy integration")
        expect(card).to_contain_text("Vacant")
        check("dashboard: room card with its sensors", card.locator(".sig-list li").count() == 2)
        check("dashboard: no horizontal scrolling", no_horizontal_overflow(frame))
        frame.locator("[data-filter=occupied]").check()
        expect(card).to_have_count(0)
        frame.locator("[data-filter=all]").check()
        set_person(args, True)
        expect(card).to_contain_text("Possible occupancy", timeout=15000)
        expect(card.locator("[data-testid=countdown]")).to_contain_text("Confirmed in")
        check("dashboard: live possible occupancy with the engine's confirmation countdown", True)
        expect(card).to_contain_text("Occupied", timeout=30000)
        check("dashboard: becomes Occupied live (no reload)", True)
        page.screenshot(path=str(shots / "presence-dashboard.png"), full_page=True)

        # 2. Workspace: live decision, evidence, visit chain, timeline.
        open_room(frame)
        expect(frame.locator("[data-testid=live-state]")).to_have_text("Occupied")
        expect(frame.locator("[data-testid=engine-reason]")).to_contain_text("Radar Kamar")
        chain = frame.locator("[data-testid=visit-chain]")
        expect(chain).to_contain_text("Radar Kamar detected")
        expect(chain).to_contain_text("Occupied")
        expect(frame.locator("[data-testid=state-machine]")).to_have_attribute("data-state", "confirmed_occupancy")
        tl = frame.locator("[data-testid=presence-timeline]")
        expect(tl.locator("[data-source=engine][data-kind=occupancy]").first).to_contain_text("Occupied", timeout=15000)
        check("workspace: live decision, visit chain and engine events", True)
        check("workspace: no horizontal scrolling", no_horizontal_overflow(frame))
        page.screenshot(path=str(shots / "presence-room-live.png"), full_page=True)
        frame.locator(".tabs [data-tab=timeline]").click()
        expect(frame.locator("[data-testid=timeline-lanes]")).to_be_visible(timeout=15000)
        frame.locator("[data-testid=timeline-window]").select_option("900")
        expect(frame.locator("[data-testid=timeline-events] li").first).to_be_visible(timeout=15000)
        frame.locator("[data-testid=timeline-events] li[data-kind=occupancy] .tl-row").first.click()
        expect(frame.locator("[data-testid=timeline-details]")).to_contain_text("Source")
        frame.locator("[data-kind=sensor]").first.wait_for(timeout=5000)
        check("timeline: lanes, history window and event details", True)
        page.screenshot(path=str(shots / "presence-timeline.png"), full_page=True)

        # 3. Room timing override, reviewed and saved live; then back to the global default.
        frame.locator(".tabs [data-tab=timing]").click()
        set_timing(frame, "inactivity_timeout", "2", "min")
        expect(frame.locator("[data-timing=inactivity_timeout] [data-testid=timing-override]")).to_be_visible()
        dialog = review(frame)
        expect(dialog.locator("[data-testid=review-changes]")).to_contain_text("Vacancy timeout: global (10 min) → 2 min")
        expect(dialog.locator("[data-testid=review-predictions]")).to_be_visible()
        save(frame, dialog)
        stored = presence(args)["rooms"][KAMAR]
        check("timing: the room override is stored in the integration", stored["timing_overrides"].get("inactivity_timeout") == 120,
              stored["timing_overrides"])
        check("timing: applied live (the engine kept its state)", stored["occupancy"]["state"] == "confirmed_occupancy",
              stored["occupancy"]["state"])
        frame.locator("[data-timing=inactivity_timeout] [data-testid=timing-use-global]").click()
        save(frame, review(frame))
        stored = presence(args)["rooms"][KAMAR]
        check("timing: Use global removes the override", "inactivity_timeout" not in stored["timing_overrides"],
              stored["timing_overrides"])

        # 4. Invalid value: refused before review, the integration would refuse it too.
        frame.locator("[data-testid=timing-advanced]").click()
        set_timing(frame, "trigger_stuck_after", "30", "s")
        expect(frame.locator("[data-timing=trigger_stuck_after] [role=alert]")).to_be_visible()
        check("timing: invalid value blocks the review", frame.locator("[data-testid=presence-review-open]").is_disabled())
        frame.locator("[data-testid=presence-discard]").click()

        # 5. Sensor role: Confirm vacancy (live), with the review's prediction; then back.
        frame.locator(f"[data-testid=presence-left] [data-sensor='{RADAR}']").click()
        inspector = frame.locator("[data-testid=presence-inspector]")
        expect(inspector).to_contain_text("Radar Kamar")
        inspector.locator("[data-role=confirm_vacancy]").check()
        dialog = review(frame)
        expect(dialog.locator("[data-testid=review-changes]")).to_contain_text("role")
        expect(dialog).to_contain_text("Applied at once")
        save(frame, dialog)
        stored = presence(args)["rooms"][KAMAR]
        check("sensors: role saved in the integration",
              next(s for s in stored["sensors"] if s["entity_id"] == RADAR)["role"] == "confirm_vacancy")
        frame.locator(f"[data-testid=presence-left] [data-sensor='{RADAR}']").click()
        inspector.locator("[data-role=maintain]").check()
        save(frame, review(frame))

        # 6. Exclude the door and include it again (live; assignment kept).
        frame.locator(f"[data-testid=presence-left] [data-sensor='{DOOR}']").click()
        inspector.locator("[data-testid=enabled-toggle]").uncheck()
        save(frame, review(frame))
        stored = presence(args)["rooms"][KAMAR]
        door = next(s for s in stored["sensors"] if s["entity_id"] == DOOR)
        check("sensors: excluded sensor keeps its assignment", door["enabled"] is False and door["kind"] == "door", door)
        frame.locator(f"[data-testid=presence-left] [data-sensor='{DOOR}']").click()
        inspector.locator("[data-testid=enabled-toggle]").check()
        save(frame, review(frame))

        # 7. Add an unassigned sensor from the area (reload), then remove it again.
        candidate = frame.locator("[data-testid=candidate-sensors] [data-add]").first
        if candidate.count():
            added = candidate.get_attribute("data-add")
            candidate.click()
            dialog = review(frame)
            expect(dialog.locator("[data-testid=review-changes]")).to_contain_text("added")
            expect(dialog).to_contain_text("reloads")
            save(frame, dialog)
            stored = presence(args)["rooms"][KAMAR]
            check("sensors: added sensor stored (integration reloaded)", any(s["entity_id"] == added for s in stored["sensors"]))
            check("sensors: occupancy survived the reload", stored["occupancy"]["occupied"] is True, stored["occupancy"])
            frame.locator(f"[data-testid=presence-left] [data-sensor='{added}']").click()
            frame.locator("[data-testid=remove-sensor]").click()
            save(frame, review(frame))
            stored = presence(args)["rooms"][KAMAR]
            check("sensors: removed again", [s["entity_id"] for s in stored["sensors"]] == [RADAR, DOOR], stored["sensors"])
        else:
            check("sensors: an unassigned presence sensor is offered in the area", False, "no candidate")

        # 8. Stale write: the integration's own room card changes the timing while the App has a draft.
        frame.locator(".tabs [data-tab=timing]").click()
        set_timing(frame, "correlation_window", "15", "s")
        r = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/lighting/presence/timing", "area_id": KAMAR,
                                               "values": {"exit_confirmation": 240}})
        check("the integration's own presence command still works", r["success"], r)
        expect(frame.locator("[data-testid=changed-elsewhere]")).to_be_visible(timeout=15000)
        frame.locator("[data-testid=review-current]").click()
        dialog = frame.locator("[data-testid=presence-review]")
        expect(dialog.locator("[data-testid=review-changes]")).to_contain_text("Occupancy confirmation", timeout=15000)
        save(frame, dialog)
        stored = presence(args)["rooms"][KAMAR]
        check("stale write: refused, re-reviewed, then both edits kept",
              stored["timing_overrides"] == {**base_timing, "exit_confirmation": 240, "correlation_window": 15},
              stored["timing_overrides"])

        # 9. Vacancy countdown from the engine.
        set_person(args, False)
        frame.locator(".tabs [data-tab=live]").click()
        expect(frame.locator("[data-testid=live-state]")).to_have_text("Vacancy pending", timeout=60000)
        expect(frame.locator("[data-testid=vacancy-progress]")).to_be_visible()
        expect(frame.locator("[data-next=await_vacancy]")).to_contain_text("Vacant after")
        check("vacancy: pending with the engine's countdown", True)
        page.screenshot(path=str(shots / "presence-vacancy.png"), full_page=True)
        set_person(args, True)

        # 10. Global defaults.
        frame.locator("[data-testid=back-to-presence]").click()
        frame.locator("[data-testid=presence-settings-link]").click()
        expect(frame.locator("[data-testid=timing-editor]")).to_be_visible()
        set_timing(frame, "dropout_grace", "45", "s")
        dialog = review(frame)
        expect(dialog).to_contain_text("Radar dropout bridge: 30 s → 45 s")
        save(frame, dialog)
        g = presence(args)["global"]["timing"]
        check("global: default saved in the integration", g["dropout_grace"] == 45, g)
        frame.locator("[data-timing=dropout_grace] button").click()
        save(frame, review(frame))
        check("global: restored to the engine default", presence(args)["global"]["timing"] == base_global)

        # 11. Diagnostics link to the configuration screens.
        page.goto(f"{PANEL}")
        frame = app_frame(page)
        wait_connected(frame)
        frame.locator(".sidebar [data-nav=presence]").click()
        frame.locator("[data-testid=presence-diagnostics-link]").click()
        expect(frame.locator("[data-testid=diag-room]").first).to_be_visible(timeout=15000)
        frame.locator("[data-testid=diag-go]").first.click()
        expect(frame.locator("[data-testid=presence-left]")).to_be_visible(timeout=15000)
        check("diagnostics: Go to opens the room's presence configuration", True)

        # 12. Floor plan Presence overlay.
        frame.locator(".sidebar [data-nav=floorplan]").click()
        frame.locator(".fp-modes [data-mode=live]").click()
        frame.locator("[data-testid=fp-presence-toggle]").check()
        expect(frame.locator("[data-testid=fp-presence-summary]")).to_contain_text("Kamar", timeout=15000)
        expect(frame.locator("[data-placement=pl_lamp]")).to_have_count(0)  # lights are not part of the overlay
        frame.locator("[data-placement=pl_radar]").click()
        expect(frame.locator("[data-testid=fp-sensor-presence]")).to_contain_text("Kamar Norður")
        frame.locator("[data-testid=fp-open-sensor]").click()
        expect(frame.locator("[data-testid=presence-inspector]")).to_contain_text("Radar Kamar", timeout=15000)
        check("floor plan: a sensor opens its Presence inspector", True)
        frame.locator(".sidebar [data-nav=floorplan]").click()
        frame.locator("[data-testid=fp-presence-summary] .fp-room-row").first.click()
        expect(frame.locator("[data-testid=fp-room-presence]")).to_be_visible()
        page.screenshot(path=str(shots / "presence-floorplan.png"), full_page=True)
        frame.locator("[data-testid=fp-open-presence]").click()
        expect(frame.locator("[data-testid=live-card]")).to_be_visible(timeout=15000)
        check("floor plan: a room opens its Presence workspace", True)
        ctx.close()

        # 13. Tablet and desktop layouts, light and dark.
        for name, opts in (("tablet-768", {"viewport": {"width": 768, "height": 1024}, "has_touch": True}),
                           ("tablet-1024", {"viewport": {"width": 1024, "height": 768}, "has_touch": True}),
                           ("tablet-1366", {"viewport": {"width": 1366, "height": 1024}, "has_touch": True}),
                           ("desktop-1920", {"viewport": {"width": 1920, "height": 1080}})):
            for scheme in ("light", "dark"):
                c = browser.new_context(color_scheme=scheme, **opts)
                pg = c.new_page()
                pg.on("pageerror", lambda e, n=name: errors.append(f"{n}: pageerror: {e}"))
                pg.goto(PANEL)
                fr = app_frame(pg)
                wait_connected(fr)
                open_room(fr)
                expect(fr.locator("[data-testid=live-card]")).to_be_visible()
                ok = no_horizontal_overflow(fr)
                fr.locator(f"[data-testid=presence-left] [data-sensor='{RADAR}']").click()
                expect(fr.locator("[data-testid=presence-inspector] [data-testid=inspector-config]")).to_be_visible()
                pg.screenshot(path=str(shots / f"presence-{name}-{scheme}.png"), full_page=False)
                check(f"{name} {scheme}: workspace without horizontal scrolling, inspector reachable", ok)
                c.close()
        browser.close()

    watcher.stop_flag = True
    time.sleep(0.7)
    check("no service calls on Home Assistant during the run", not watcher.events, watcher.events)
    commands = http(f"{BASE}/_test/commands")["commands"]
    types = {c["type"] for c in commands}
    allowed = READ_ONLY | PRESENCE_READS | WRITE
    check("only allowlisted commands reached Home Assistant", types <= allowed, sorted(types - allowed))
    check("presence writes only through the integration's presence command", "smart_occupancy/presence/set" in types
          and "smart_occupancy/rooms/set" not in types)
    (out / STATE_FILE).write_text(json.dumps({"timing_overrides": presence(args)["rooms"][KAMAR]["timing_overrides"]}))


def phase_restart(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    expected = json.loads((out / STATE_FILE).read_text())["timing_overrides"]
    data = presence(args)
    check("restart: the integration keeps the presence configuration",
          data["rooms"][KAMAR]["timing_overrides"] == expected, data["rooms"][KAMAR]["timing_overrides"])
    check("restart: a start entry in the presence log", any(e["kind"] == "start" for e in data["log"]))
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"restart: pageerror: {e}"))
        page.goto(PANEL)
        frame = app_frame(page)
        wait_connected(frame)
        open_room(frame, "timing")
        expect(frame.locator("[data-timing=correlation_window] [data-testid=timing-override]")).to_be_visible(timeout=15000)
        expect(frame.locator("[data-timing=correlation_window] input")).to_have_value("15")
        check("restart: the App shows the saved room timing", True)
        browser.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ha-url", required=True)
    ap.add_argument("--ha-token", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chromium")
    ap.add_argument("--phase", default="all", choices=["all", "restart-check"])
    args = ap.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    if args.phase == "all":
        phase_all(args, out, errors)
    else:
        phase_restart(args, out, errors)
    filtered = [e for e in errors if "favicon" not in e]
    check("no browser console or page errors", not filtered, filtered)
    (out / f"presence-results-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
