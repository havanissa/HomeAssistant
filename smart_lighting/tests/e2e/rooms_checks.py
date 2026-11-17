"""End-to-end checks of Phase A2 (Rooms) through simulated Ingress against a real Home Assistant.

Needs the stack from stack.sh, a real Home Assistant with Smart Occupancy &
Lighting 0.6.9 (the Kamar Norður room seeded through its own options flow,
demo lights and covers, template sensors) and Playwright:

    python -I rooms_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check]

It opens Smart Lighting → Rooms, selects a room, discovers and assigns
devices, reviews and saves, and checks that the existing integration stores
and uses the change: through its API, its own options flow (legacy editor)
and its runtime. It covers stale writes after a change in the legacy editor,
confirmation of disruptive changes, the unsaved-changes guard, connection loss
while editing, backups and restore, live states, the responsive layouts and
that no service call reaches Home Assistant. ``--phase restart-check`` runs
after the App and Home Assistant were restarted and checks persistence.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import threading
import time
from typing import Any
import urllib.request

import sys

import aiohttp
from playwright.sync_api import Frame, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this folder's browser_checks (python -I omits it)
from browser_checks import (  # noqa: E402
    BASE, INGRESS, PANEL, READ_ONLY, SUPERVISOR_TOKEN, ServiceCallWatcher, app_frame, check, no_horizontal_overflow,
    results, small_targets, wait_connected,
)

WRITE = {"smart_occupancy/rooms/set", "smart_occupancy/rooms/restore"}
KAMAR = "kamar_nordur"
STATE_FILE = "rooms_e2e_state.json"


def http(url: str, headers: dict[str, str] | None = None, method: str = "GET", body: Any = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {}, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def http_status(url: str, headers: dict[str, str], body: Any) -> int:
    req = urllib.request.Request(url, headers=headers, method="POST", data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def ha_ws(ha_url: str, token: str, msg: dict[str, Any]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with aiohttp.ClientSession() as s, s.ws_connect(ha_url.replace("http", "ws") + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": token})
            await ws.receive_json()
            await ws.send_json({"id": 1, **msg})
            while True:
                r = await ws.receive_json()
                if r.get("id") == 1 and r["type"] == "result":
                    return r
    box: dict[str, Any] = {}
    # Playwright's sync API runs an event loop in this thread: use a separate one.
    worker = threading.Thread(target=lambda: box.update(result=asyncio.run(run())))
    worker.start()
    worker.join(30)
    return box["result"]


def stored_room(ha_url: str, token: str) -> dict[str, Any]:
    r = ha_ws(ha_url, token, {"type": "smart_occupancy/rooms/get"})
    assert r["success"], r
    return r["result"]


def legacy_room_form(ha_url: str, auth: dict[str, str], entry_id: str, change: dict[str, Any] | None) -> dict[str, Any]:
    """Open (and optionally submit) the integration's own room editor: Configure → Rooms → Kamar Norður."""
    step = http(f"{ha_url}/api/config/config_entries/options/flow", auth, "POST", {"handler": entry_id})
    fid = step["flow_id"]
    http(f"{ha_url}/api/config/config_entries/options/flow/{fid}", auth, "POST", {"next_step_id": "rooms"})
    form = http(f"{ha_url}/api/config/config_entries/options/flow/{fid}", auth, "POST", {"area": KAMAR})
    values = {f["name"]: (f.get("description") or {}).get("suggested_value") for f in form["data_schema"]}
    if change is None:
        return values
    data = {k: v for k, v in values.items() if v not in (None, [])} | {"enabled": True, "reset": False} | change
    step = http(f"{ha_url}/api/config/config_entries/options/flow/{fid}", auth, "POST", data)
    for _ in range(3):
        if step.get("type") == "create_entry":
            break
        payload = {f["name"]: f.get("default") for f in step.get("data_schema", []) if "default" in f}
        step = http(f"{ha_url}/api/config/config_entries/options/flow/{fid}", auth, "POST", payload)
    assert step.get("type") == "create_entry", step
    return values


def styled(frame: Frame) -> bool:
    return frame.evaluate("getComputedStyle(document.body).backgroundColor") != "rgba(0, 0, 0, 0)"


def open_room(frame: Frame, area: str = KAMAR, tab: str | None = None) -> None:
    frame.locator(".sidebar [data-nav=rooms]").click()
    frame.locator(f"[data-room={area}]").click()
    expect(frame.locator("[data-testid=room-title]")).to_be_visible(timeout=10000)
    if tab:
        frame.locator(f".tabs [data-tab={tab}]").click()


def review_and_save(frame: Frame, confirm: list[str] | None = None) -> Any:
    frame.locator("[data-testid=review]").click()
    dialog = frame.locator("[data-testid=review-dialog]")
    expect(dialog).to_be_visible()
    expect(dialog.locator("[data-testid=save]")).to_be_visible()
    frame.wait_for_function("() => !document.querySelector('[data-testid=review-dialog]')?.textContent.includes('Checking with')")
    for code in confirm or []:
        dialog.locator(f"[data-impact={code}] input").check()
    return dialog


def phase_all(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    auth = {"Authorization": f"Bearer {args.ha_token}"}
    model = stored_room(args.ha_url, args.ha_token)
    entry_id = model["entry_id"]
    seed = model["rooms"][KAMAR]["config"]
    check("the room seeded by the integration's own editor is read", [s["entity_id"] for s in seed["sensors"]] ==
          ["binary_sensor.radar_kamar", "binary_sensor.door_kamar"] and seed["lights"] == ["light.bed_light"], seed)
    watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    watcher.start()
    watcher.ready.wait(10)
    shots = out / "screenshots"

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.goto(PANEL)
        frame = app_frame(page)
        wait_connected(frame)
        if not styled(frame):  # Chromium aborts loads with ERR_NETWORK_CHANGED right after Docker changes interfaces
            errors.clear()
            page.reload()
            frame = app_frame(page)
            wait_connected(frame)
        check("interface styles loaded", styled(frame))

        # --- Overview: every area, status, search and filter.
        frame.locator(".sidebar [data-nav=rooms]").click()
        grid = frame.locator("[data-testid=room-grid]")
        expect(grid.locator(".room-card")).to_have_count(len(http(INGRESS + "api/rooms")["rooms"]))
        kamar_card = grid.locator(f"[data-room={KAMAR}]")
        expect(kamar_card).to_contain_text("Configured")
        expect(grid.locator("[data-room=stova]")).to_contain_text("Not configured")
        check("overview: every Home Assistant area, configured and not", True)
        frame.locator(".rooms-toolbar input[type=search]").fill("radar_kamar")
        expect(grid.locator(".room-card")).to_have_count(1)
        frame.locator(".rooms-toolbar input[type=search]").fill("")
        frame.locator(".rooms-toolbar .segmented label", has_text="Not configured").click()
        check("overview: status filter", grid.locator(f"[data-room={KAMAR}]").count() == 0)
        frame.locator(".rooms-toolbar .segmented label", has_text="All").click()
        page.screenshot(path=str(shots / "a2-overview-1920.png"))

        # --- Workspace: three panels.
        open_room(frame)
        frame.locator(".tabs [data-tab=devices]").click()
        frame.locator("tr[data-entity='binary_sensor.radar_kamar']").click()
        inspector = frame.locator("[data-testid=inspector]")
        expect(inspector).to_be_visible()
        left, center, right = (frame.locator(s).bounding_box() for s in (".ws-left", ".ws-center", ".inspector"))
        check("workspace: room list | room | inspector side by side at 1920 px",
              left["x"] + left["width"] <= center["x"] and center["x"] + center["width"] <= right["x"], [left, center, right])
        expect(inspector).to_contain_text("Radar Kamar")
        expect(inspector).to_contain_text("Classification")
        page.screenshot(path=str(shots / "a2-workspace-devices-1920.png"))

        # --- Discovery and assignment.
        palette = frame.locator("[data-testid=available-devices]")
        expect(palette).to_contain_text("Ceiling Lights")
        expect(palette).to_contain_text("Nobara PC active")
        palette.locator("[data-assign='light.ceiling_lights']").click()
        frame.locator("tr[data-entity='input_boolean.nobara_pc']").click()
        expect(inspector.locator("[data-testid=inspector-assign-pc_activity]")).to_be_visible()
        inspector.locator("[data-testid=inspector-assign-pc_activity]").click()
        expect(inspector.locator("[data-testid=inspector-kind]")).to_have_value("pc_activity")
        frame.locator(".tabs [data-tab=presence]").click()
        frame.locator("tr[data-sensor='binary_sensor.radar_kamar'] select[aria-label^='Role']").select_option("confirm_vacancy")
        frame.locator("tr[data-sensor='binary_sensor.door_kamar'] input[aria-label$='inverted (active when off)']").check()
        bar = frame.locator("[data-testid=unsaved-bar]")
        expect(bar).to_contain_text("4 unsaved changes")
        check("draft: assignments and settings change only the draft", stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"] == seed)
        page.screenshot(path=str(shots / "a2-presence-draft-1920.png"))

        # --- Review and save.
        dialog = review_and_save(frame)
        for text in ("Added", "Ceiling Lights added as a light", "Nobara PC active added as PC activity",
                     "Changed classifications", "Radar Kamar: role Maintain → Confirm vacancy",
                     "Door Kamar: inverted (active when off)", "The integration reloads"):
            expect(dialog).to_contain_text(text)
        page.screenshot(path=str(shots / "a2-review-1920.png"))
        dialog.locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_contain_text("Saved", timeout=15000)
        expect(bar).to_be_hidden()
        stored = stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"]
        sensors = {s["entity_id"]: s for s in stored["sensors"]}
        check("saved: stored in the integration's options",
              stored["lights"] == ["light.bed_light", "light.ceiling_lights"]
              and sensors["input_boolean.nobara_pc"]["kind"] == "pc_activity"
              and sensors["binary_sensor.radar_kamar"]["role"] == "confirm_vacancy"
              and sensors["binary_sensor.door_kamar"]["inverted"] is True, stored)
        legacy = legacy_room_form(args.ha_url, auth, entry_id, None)
        check("saved: the integration's own room editor shows the change",
              legacy.get("lights") == ["light.bed_light", "light.ceiling_lights"]
              and legacy.get("pc_activity") == ["input_boolean.nobara_pc"], legacy)
        time.sleep(3)
        runtime = stored_room(args.ha_url, args.ha_token)
        check("saved: the integration runs with it (reloaded, loaded)", runtime["loaded"]
              and runtime["rooms"][KAMAR]["occupancy"] is not None
              and "input_boolean.nobara_pc" in [s["entity_id"] for s in runtime["rooms"][KAMAR]["occupancy"]["signals"]],
              runtime["rooms"][KAMAR].get("occupancy"))

        # --- Live state in the inspector (state written directly, not a service call).
        frame.locator(".tabs [data-tab=devices]").click()
        frame.locator("tr[data-entity='sensor.lux_kamar']").click()
        current = http(f"{args.ha_url}/api/states/sensor.lux_kamar", auth)
        http(f"{args.ha_url}/api/states/sensor.lux_kamar", auth, "POST", {"state": "777", "attributes": current["attributes"]})
        expect(inspector.locator(".state-value")).to_contain_text("777", timeout=5000)
        check("live: entity state reaches the inspector", True)

        # --- Changed in the integration's own editor while editing here: stale write refused, reviewed refresh.
        frame.locator("[data-testid=available-devices] [data-assign='sensor.lux_kamar']").click()
        expect(bar).to_contain_text("1 unsaved change")
        legacy_room_form(args.ha_url, auth, entry_id, {"lights": ["light.bed_light"]})
        expect(frame.locator("[data-testid=changed-elsewhere]")).to_be_visible(timeout=10000)
        check("concurrent: a change in the integration's editor is noticed while editing", True)
        frame.locator("[data-testid=changed-elsewhere] button").click()
        dialog = frame.locator("[data-testid=review-dialog]")
        expect(dialog).to_contain_text("Lux Kamar added")
        expect(dialog).not_to_contain_text("Ceiling Lights")  # compared with the current configuration
        dialog.locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=15000)
        stored = stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"]
        check("concurrent: the legacy editor's change is kept, the reviewed edit added",
              stored["lights"] == ["light.bed_light"] and "sensor.lux_kamar" in [s["entity_id"] for s in stored["sensors"]], stored)
        # A stale write through the API is refused (revision check).
        status = http_status(INGRESS + f"api/rooms/{KAMAR}/save", {"Content-Type": "application/json", "X-Smart-Lighting": "1"},
                             {"room": {**stored, "lights": []}, "revision": "0123456789abcdef"})
        check("concurrent: a stale save is refused", status == 409, status)

        # --- Unsaved-changes guard.
        frame.locator("[data-testid=available-devices] [data-assign='sensor.temperature_kamar']").click()
        page.once("dialog", lambda d: d.dismiss())
        frame.locator(".sidebar [data-nav=dashboard]").click()
        time.sleep(0.5)
        check("guard: leaving with unsaved changes asks first (cancel stays)", frame.locator("[data-testid=room-title]").is_visible())
        # --- Connection loss while editing: the draft survives, the App reconnects.
        http(f"{BASE}/_test/drop_core", method="POST", body={})
        expect(frame.locator("[data-testid=connection-pill]")).to_contain_text(re.compile("Reconnecting|Connecting"), timeout=5000)
        wait_connected(frame)
        expect(bar).to_contain_text("1 unsaved change")
        check("connection loss: unsaved edits are kept and the App reconnects", True)
        frame.locator("[data-testid=discard]").click()
        expect(bar).to_be_hidden()

        # --- Disruptive change needs explicit confirmation.
        frame.locator(".tabs [data-tab=overview]").click()
        frame.locator("[data-testid=room-enabled] input").uncheck()
        dialog = review_and_save(frame)
        expect(dialog).to_contain_text("Room stops being observed")
        check("confirm: save disabled until the consequence is confirmed", dialog.locator("[data-testid=save]").is_disabled())
        page.screenshot(path=str(shots / "a2-review-confirm-1920.png"))
        dialog.locator("[data-impact=room_disabled] input").check()
        dialog.locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=15000)
        check("confirm: room disabled in the integration", stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"]["enabled"] is False)
        frame.locator("[data-testid=room-enabled] input").check()
        review_and_save(frame).locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=15000)
        check("confirm: room enabled again", stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"]["enabled"] is True)

        # --- New room for an unconfigured area, device from another area (warning, not rejected).
        frame.locator(".ws-left .room-nav a", has_text="Stova").click()
        expect(frame.locator("[data-testid=room-title]")).to_have_text("Stova")
        frame.locator(".tabs [data-tab=devices]").click()
        frame.locator("[data-testid=add-sensor]").click()
        pick = frame.locator("[data-testid=entity-pick]")
        pick.locator("input[type=search]").fill("radar kamar")
        expect(pick.locator("[role=option]").first).to_contain_text("other area")
        pick.locator("[role=option]").first.click()
        frame.locator("tr[data-entity='binary_sensor.window_stova']").click()
        expect(inspector).to_contain_text("Entity unavailable")
        frame.locator(".tabs [data-tab=overview]").click()
        frame.locator("[data-testid=room-enabled] input").check()
        dialog = review_and_save(frame)
        expect(dialog).to_contain_text("Potential conflicts")
        expect(dialog).to_contain_text("is in Kamar Norður in Home Assistant")
        dialog.locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=15000)
        stova = stored_room(args.ha_url, args.ha_token)["rooms"].get("stova", {}).get("config", {})
        check("new room: created with a sensor from another area", stova.get("enabled") is True and
              [s["entity_id"] for s in stova.get("sensors", [])] == ["binary_sensor.radar_kamar"], stova)
        ent = ha_ws(args.ha_url, args.ha_token, {"type": "config/entity_registry/get", "entity_id": "binary_sensor.radar_kamar"})
        check("registries unchanged: the sensor's Home Assistant area is still Kamar Norður", ent["result"]["area_id"] == KAMAR, ent)

        # --- Diagnostics tab.
        frame.locator(".ws-left .room-nav a", has_text="Kamar Norður").click()
        frame.locator(".tabs [data-tab=diagnostics]").click()
        expect(frame.locator("[data-testid=diag-table]")).to_contain_text("Radar Kamar")
        page.screenshot(path=str(shots / "a2-diagnostics-1920.png"))
        check("diagnostics: entity status table", True)
        for tab in ("lighting", "switches", "curtains"):
            frame.locator(f".tabs [data-tab={tab}]").click()
            expect(frame.locator(".later-note")).to_contain_text("stays in the integration")
        check("later modules are read-only and say where they are configured", True)

        # --- Backups: export, then restore the configuration from before the first save (reviewed).
        frame.locator(".sidebar [data-nav=rooms]").click()
        frame.locator("[data-testid=open-backups]").click()
        backups = frame.locator(".backup-list li")
        expect(backups.first).to_be_visible()
        n = backups.count()
        check("backups: one before every save", n >= 5, n)
        with page.expect_download() as dl:
            frame.locator("[data-testid=export-rooms]").click()
        exported = json.loads(Path(dl.value.path()).read_text())
        check("export: current room configuration", exported["format"] == "smart_occupancy.rooms_backup" and KAMAR in exported["rooms"])
        check("export: no secrets", SUPERVISOR_TOKEN not in json.dumps(exported) and args.ha_token not in json.dumps(exported))
        backups.last.locator("button", has_text="Restore").click()
        review = frame.locator("[data-testid=restore-review]")
        expect(review).to_contain_text("Kamar Norður")
        check("restore: confirmation required", frame.locator("[data-testid=restore-confirm]").is_disabled())
        review.locator(".confirm-row input").first.check()
        for box in review.locator(".confirm-row input").all():
            box.check()
        page.screenshot(path=str(shots / "a2-restore-review-1920.png"))
        frame.locator("[data-testid=restore-confirm]").click()
        expect(frame.locator(".backups-dialog .notice-info")).to_contain_text("Restored", timeout=15000)
        restored = stored_room(args.ha_url, args.ha_token)["rooms"]
        check("restore: the configuration before the first save is back", restored[KAMAR]["config"] == seed and "stova" not in restored,
              restored.get(KAMAR, {}).get("config"))
        frame.locator(".backups-dialog .sheet-head .icon-btn").click()

        # --- Leave a known state for the restart check: one saved change.
        open_room(frame, KAMAR, "devices")
        frame.locator("[data-testid=available-devices] [data-assign='light.ceiling_lights']").click()
        review_and_save(frame).locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_be_visible(timeout=15000)
        final = stored_room(args.ha_url, args.ha_token)["rooms"][KAMAR]["config"]
        (out / STATE_FILE).write_text(json.dumps({"kamar": final}))

        # --- Dark theme screenshots.
        page.emulate_media(color_scheme="dark")
        frame.locator(".tabs [data-tab=presence]").click()
        page.screenshot(path=str(shots / "a2-presence-dark-1920.png"))
        page.emulate_media(color_scheme="light")
        if SUPERVISOR_TOKEN in frame.content() or args.ha_token in frame.content():
            check("no credentials in the page", False)
        ctx.close()

        # --- Responsive layouts of the workspace.
        for name, opts, layout in (
            ("desktop-1440", {"viewport": {"width": 1440, "height": 900}}, "desktop"),
            ("tablet-landscape-1366", {"viewport": {"width": 1366, "height": 1024}, "is_mobile": True, "has_touch": True}, "tablet"),
            ("tablet-landscape-1024", {"viewport": {"width": 1024, "height": 768}, "is_mobile": True, "has_touch": True}, "tablet"),
            ("tablet-portrait-768", {"viewport": {"width": 768, "height": 1024}, "is_mobile": True, "has_touch": True}, "tablet"),
            ("phone-390", {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True}, "phone"),
        ):
            c = browser.new_context(**opts)
            pg = c.new_page()
            pg.on("pageerror", lambda e, n=name: errors.append(f"{n}: pageerror: {e}"))
            pg.goto(INGRESS + f"#/rooms/{KAMAR}/devices")
            f = pg.main_frame
            expect(f.locator("[data-testid=room-title]")).to_be_visible(timeout=15000)
            overflow = []
            for tab in ("overview", "devices", "presence", "lighting", "diagnostics"):
                pg.goto(INGRESS + f"#/rooms/{KAMAR}/{tab}")
                expect(f.locator(f".tabs [data-tab={tab}].active")).to_be_visible()
                if not no_horizontal_overflow(f):
                    overflow.append(tab)
            check(f"{name}: no horizontal scrolling in the room workspace", not overflow, overflow)
            pg.goto(INGRESS + f"#/rooms/{KAMAR}/devices")
            row = f.locator("tr[data-entity='binary_sensor.radar_kamar']")
            row.tap() if layout != "desktop" else row.click()
            ins = f.locator("[data-testid=inspector]")
            expect(ins).to_be_visible()
            pos = f.evaluate("getComputedStyle(document.querySelector('.inspector')).position")
            if name == "desktop-1440":
                check(f"{name}: inspector is a third column", pos == "sticky", pos)
            elif layout == "tablet":
                check(f"{name}: inspector slides over the room", pos == "fixed", pos)
            pg.screenshot(path=str(shots / f"a2-{name}-inspector.png"))
            if layout == "tablet":
                issues = [i for i in small_targets(f) if "chip" not in i]
                check(f"{name}: touch targets >= 44px in the workspace", not issues, issues[:8])
            c.close()
        browser.close()

    watcher.stop_flag = True
    time.sleep(0.7)
    check("no service calls on Home Assistant during the run", not watcher.events, watcher.events)
    commands = http(f"{BASE}/_test/commands")["commands"]
    types = {c["type"] for c in commands}
    check("only allowlisted commands reached Home Assistant", types <= READ_ONLY | WRITE, sorted(types - READ_ONLY - WRITE))
    check("configuration writes only through the integration's room commands", bool(types & WRITE))


def phase_restart(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    expected = json.loads((out / STATE_FILE).read_text())["kamar"]
    stored = stored_room(args.ha_url, args.ha_token)
    check("restart: the integration keeps the saved configuration", stored["rooms"][KAMAR]["config"] == expected,
          stored["rooms"][KAMAR]["config"])
    check("restart: the integration is running", stored["loaded"])
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"restart: pageerror: {e}"))
        page.goto(PANEL)
        frame = app_frame(page)
        wait_connected(frame)
        open_room(frame, KAMAR, "lighting")
        expect(frame.locator("[data-testid=light-list]")).to_contain_text("Ceiling Lights")
        check("restart: the App shows the saved configuration", True)
        frame.locator(".sidebar [data-nav=rooms]").click()
        frame.locator("[data-testid=open-backups]").click()
        expect(frame.locator(".backup-list li").first).to_be_visible()
        check("restart: backups survive an App restart", frame.locator(".backup-list li").count() >= 5)
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
    (out / f"results-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
