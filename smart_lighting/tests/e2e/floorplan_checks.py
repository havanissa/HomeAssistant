"""End-to-end checks of Phase A2.1 (Floor plan) through simulated Ingress against a real Home Assistant.

Needs the stack from stack.sh (with a fresh DATA_DIR), the Phase A2 test
Home Assistant (areas Kamar Norður, Stova and Gongd; the Kamar Norður room
configured in Smart Occupancy 0.6.9; demo lights and covers; template
sensors) and Playwright:

    python -I floorplan_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check]

It opens Smart Lighting → Floor plan from the sidebar, draws two floors with
rectangular and polygon rooms, doors, windows and an open-plan opening,
imports and calibrates a background image, links rooms to areas, places real
devices (drag and drop, tap on a touch screen), inspects them, follows a
suggested adjacency into the Rooms editor's reviewed save, watches live
states, checks saving, reloading, undo/redo, deleting, two browser sessions,
the leave guard, export/import and backups, and that nothing in Home
Assistant changes except the one reviewed adjacency: no service call, no
registry change. ``--phase restart-check`` runs after the App and Home
Assistant were restarted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import struct
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import zlib

from playwright.sync_api import Frame, Page, expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))  # this folder's helpers (python -I omits it)
from browser_checks import (  # noqa: E402
    BASE, INGRESS, PANEL, READ_ONLY, SUPERVISOR_TOKEN, ServiceCallWatcher, app_frame, check, no_horizontal_overflow,
    results, wait_connected,
)
from rooms_checks import ha_ws, styled  # noqa: E402

KAMAR, STOVA, GONGD = "kamar_nordur", "stova", "gongd"
TEMP = "sensor.e2e_fp_temperature"
STATE_FILE = "floorplan_e2e_state.json"
ALLOWED = READ_ONLY | {"config/floor_registry/list", "smart_occupancy/rooms/set"}


# ------------------------------------------------------------------ helpers
def http(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read(), dict(err.headers)


def api(path: str) -> Any:
    return json.loads(http(INGRESS + path)[1])


def set_state(ha_url: str, token: str, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
    """Simulate a device report (POST /api/states: no service call)."""
    body = json.dumps({"state": state, "attributes": attributes}).encode()
    status, _, _ = http(f"{ha_url}/api/states/{entity_id}", method="POST", body=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert status in (200, 201), status


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes((230 if (x // 20 + y // 20) % 2 else 200) for x in range(width)) for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def registries(ha_url: str, token: str) -> dict[str, Any]:
    out = {}
    for kind in ("area", "device", "entity", "floor"):
        r = ha_ws(ha_url, token, {"type": f"config/{kind}_registry/list"})
        items = r["result"]
        # Compare what a floor plan could change: areas of devices/entities, names, the areas and floors themselves.
        if kind == "device":
            items = sorted((d["id"], d.get("area_id"), d.get("name_by_user")) for d in items)
        elif kind == "entity":
            items = sorted((e["entity_id"], e.get("area_id"), e.get("name"), e.get("disabled_by")) for e in items)
        else:
            items = sorted(json.dumps(i, sort_keys=True) for i in items)
        out[kind] = items
    return out


def shared_walls(floor: dict[str, Any]) -> list[dict[str, Any]]:
    """Walls that two rooms have as an edge."""
    def edges(room: dict[str, Any]) -> set[frozenset[str]]:
        v = room["vertices"]
        return {frozenset((v[i], v[(i + 1) % len(v)])) for i in range(len(v))}
    room_edges = [edges(r) for r in floor["rooms"]]
    return [w for w in floor["walls"] if sum(frozenset((w["a"], w["b"])) in e for e in room_edges) == 2]


def plan(frame: Frame) -> dict[str, Any]:
    return frame.evaluate("() => document.querySelector('sl-floorplan').plan")


def screen(frame: Frame, x: float, y: float) -> tuple[float, float]:
    """Page coordinates of a world point (metres) on the canvas."""
    box = frame.locator("sl-plan-canvas").bounding_box()
    v = frame.evaluate("() => { const c = document.querySelector('sl-plan-canvas'); return {s: c.view.scale, tx: c.view.tx, ty: c.view.ty} }")
    return box["x"] + v["tx"] + x * v["s"], box["y"] + v["ty"] + y * v["s"]


def drag(page: Page, frame: Frame, a: tuple[float, float], b: tuple[float, float]) -> None:
    pa, pb = screen(frame, *a), screen(frame, *b)
    page.mouse.move(*pa)
    page.mouse.down()
    page.mouse.move((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2, steps=6)
    page.mouse.move(*pb, steps=6)
    page.mouse.up()
    time.sleep(0.15)


def click(page: Page, frame: Frame, x: float, y: float) -> None:
    page.mouse.click(*screen(frame, x, y))
    time.sleep(0.15)


def tool(frame: Frame, name: str) -> None:
    frame.locator(f".fp-tools [data-tool={name}]").click()


def mode(frame: Frame, name: str) -> None:
    frame.locator(f".fp-modes [data-mode={name}]").click()
    expect(frame.locator(f".fp-page.mode-{name}")).to_be_visible()


def save(frame: Frame) -> None:
    frame.locator("[data-testid=fp-save]").click()
    expect(frame.locator("[data-testid=fp-save-status]")).to_contain_text("Saved", timeout=10000)


def room_id(frame: Frame, name: str) -> str:
    return frame.evaluate("(n) => document.querySelector('sl-floorplan').plan.floors.flatMap(f => f.rooms).find(r => r.name === n).id", name)


def open_floorplan(page: Page) -> Frame:
    page.goto(PANEL)
    frame = app_frame(page)
    wait_connected(frame)
    if not styled(frame):  # Chromium aborts loads with ERR_NETWORK_CHANGED right after Docker changes interfaces
        page.reload()
        frame = app_frame(page)
        wait_connected(frame)
    frame.locator(".sidebar [data-nav=floorplan]").click()
    expect(frame.locator(".fp-page")).to_be_visible()
    return frame


def link_area(frame: Frame, page: Page, at: tuple[float, float], area: str) -> None:
    click(page, frame, *at)
    frame.locator("[data-testid=fp-area-select]").select_option(area)
    time.sleep(0.1)


def place(frame: Frame, page: Page, entity_id: str, at: tuple[float, float]) -> None:
    frame.locator("[data-testid=fp-palette] input[type=search]").fill(entity_id)
    frame.locator(f"[data-entity='{entity_id}']").click()
    expect(frame.locator("[data-testid=fp-placing]")).to_be_visible()
    click(page, frame, *at)
    expect(frame.locator("[data-testid=fp-placing]")).to_have_count(0)


def placement_at(frame: Frame, entity_id: str) -> dict[str, Any]:
    return frame.evaluate("(e) => document.querySelector('sl-floorplan').plan.floors.flatMap(f => f.placements).find(p => p.entity_id === e)", entity_id)


# ------------------------------------------------------------------- phases
def phase_all(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    shots = out / "screenshots"
    ha, token = args.ha_url, args.ha_token
    set_state(ha, token, TEMP, "21.5", {"friendly_name": "E2E Temperature", "unit_of_measurement": "°C",
                                         "device_class": "temperature"})
    before_registries = registries(ha, token)
    before_rooms = ha_ws(ha, token, {"type": "smart_occupancy/rooms/get"})["result"]
    commands_before = len(json.loads(http(f"{BASE}/_test/commands")[1])["commands"])
    doc = api("api/floorplan")
    check("starts with an empty floor plan (fresh App data)", doc["plan"]["floors"] == [] and doc["status"]["writable"], doc["status"])
    watcher = ServiceCallWatcher(ha, token)
    watcher.start()
    watcher.ready.wait(10)

    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        # 1. Smart Lighting → Floor plan from the sidebar.
        frame = open_floorplan(page)
        check("1. Floor plan opens from the sidebar (Build for an empty plan)", "#/floorplan/build" in frame.evaluate("location.hash"))
        page.screenshot(path=str(shots / "a21-empty-1920.png"))

        # 2. Floors and rooms.
        frame.locator("[data-testid=fp-first-floor], [data-testid=fp-add-floor]").first.click()
        frame.locator("[data-testid=fp-floor-name]").fill("Ground floor")
        frame.locator("[data-testid=fp-floor-save]").click()
        tool(frame, "rect")
        drag(page, frame, (0, 0), (6, 4))
        tool(frame, "rect")
        drag(page, frame, (6, 0), (10, 4))
        tool(frame, "polygon")
        for pt in ((0, 4), (5, 4), (5, 6), (3, 6), (3, 8), (0, 8)):
            click(page, frame, *pt)
        click(page, frame, 0, 4)  # the first corner closes the room
        fp = plan(frame)
        floor = fp["floors"][0]
        shared = shared_walls(floor)
        check("2. three rooms drawn (two rectangles, one L-shaped polygon)", [len(r["vertices"]) >= 4 for r in floor["rooms"]] == [True] * 3
              and sorted(len(r["vertices"]) for r in floor["rooms"])[-1] >= 6, [r["vertices"] for r in floor["rooms"]])
        check("2. connected rooms share walls (welded corners, split walls)", len(shared) >= 2, len(shared))
        tool(frame, "door")
        click(page, frame, 6, 2)
        tool(frame, "window")
        click(page, frame, 3, 0)
        tool(frame, "opening")
        click(page, frame, 1.5, 4)
        tool(frame, "select")
        openings = plan(frame)["floors"][0]["openings"]
        check("2. door, window and open-plan opening placed on walls", sorted(o["type"] for o in openings) == ["door", "opening", "window"], openings)
        frame.locator("[data-testid=fp-undo]").click()
        check("2. undo removes the last edit", len(plan(frame)["floors"][0]["openings"]) == 2)
        frame.locator("[data-testid=fp-redo]").click()
        check("2. redo restores it", len(plan(frame)["floors"][0]["openings"]) == 3)

        # Edit after drawing: move the outer wall of the second room by 1 m; the door stays on its wall.
        click(page, frame, 10, 2)
        expect(frame.locator("[data-testid=fp-inspector]")).to_contain_text("Wall")
        drag(page, frame, (10, 2), (11, 2))
        xs = sorted({v["x"] for v in plan(frame)["floors"][0]["vertices"]})
        check("2. a wall moves (both corners of the straight wall)", 11 in xs and 10 not in xs, xs)
        frame.locator("[data-testid=fp-dims]").check()
        click(page, frame, 8, 0)
        frame.locator("[data-testid=fp-wall-length]").fill("5.5")
        frame.locator("[data-testid=fp-wall-length]").press("Enter")
        time.sleep(0.2)
        xs = {v["x"] for v in plan(frame)["floors"][0]["vertices"]}
        check("2. optional measurement: a wall length typed in moves its end corner", 11.5 in xs or 5.5 in xs, sorted(xs))
        frame.locator("[data-testid=fp-undo]").click()
        page.keyboard.press("Escape")
        # Corner editing: a corner of the L-shaped room moves; a crossing edit is refused.
        click(page, frame, 1, 6)  # inside the L-shaped room: its corners get handles
        drag(page, frame, (3, 8), (3, 8.5))
        check("2. a corner moves (vertex editing)", any(v["x"] == 3 and v["y"] == 8.5 for v in plan(frame)["floors"][0]["vertices"]))
        page.screenshot(path=str(shots / "a21-build-1920.png"))

        # Background: import, calibrate with a known distance.
        img = out / "plan-background.png"
        img.write_bytes(png(400, 300))
        frame.locator("[data-testid=fp-bg-file]").set_input_files(str(img))
        expect(frame.locator("[data-testid=fp-bg-inspector]")).to_be_visible(timeout=10000)
        bg = plan(frame)["floors"][0]["background"]
        check("3. background image imported and stored (content-addressed id)", bg and bg["image_id"].startswith("img_"), bg)
        status, body, headers = http(f"{INGRESS}api/floorplan/images/{bg['image_id']}")
        check("3. image served through Ingress with its type", status == 200 and headers.get("Content-Type") == "image/png"
              and headers.get("X-Content-Type-Options") == "nosniff", headers)
        frame.locator("[data-testid=fp-bg-opacity]").fill("0.4")
        frame.locator("[data-testid=fp-bg-opacity]").dispatch_event("change")
        frame.locator("[data-testid=fp-calibrate]").click()
        click(page, frame, 0, 0)
        click(page, frame, 6, 0)
        frame.locator("[data-testid=fp-calib-value]").fill("6")
        frame.locator("[data-testid=fp-calib-apply]").click()
        bg2 = plan(frame)["floors"][0]["background"]
        check("3. calibration keeps a correct drawing and marks the floor measured",
              abs(bg2["scale"] - bg["scale"]) < 1e-9 and plan(frame)["floors"][0]["measured"], bg2)
        page.screenshot(path=str(shots / "a21-background-1920.png"))
        # Upload validation through the real Ingress path.
        status, body, _ = http(f"{INGRESS}api/floorplan/images", method="POST", body=b"<svg/>",
                               headers={"Content-Type": "image/svg+xml", "X-Smart-Lighting": "1"})
        check("10. SVG uploads are refused", status == 415, (status, body[:120].decode(errors="replace")))
        status, _, _ = http(f"{INGRESS}api/floorplan/images/..%2Fplan.json")
        check("10. no file outside the image store is served", status == 404, status)

        # Second floor with one room; a temporary room deleted with confirmation.
        frame.locator("[data-testid=fp-add-floor]").click()
        frame.locator("[data-testid=fp-floor-name]").fill("First floor")
        frame.locator("[data-testid=fp-floor-save]").click()
        tool(frame, "rect")
        drag(page, frame, (0, 0), (5, 4))
        tool(frame, "rect")
        drag(page, frame, (5, 0), (8, 4))
        frame.locator("[data-testid=fp-delete-room]").click()
        expect(frame.locator("[data-testid=fp-dialog-confirm]")).to_be_visible()
        frame.locator("[data-testid=fp-confirm]").click()
        check("2. deleting a room asks first", len(plan(frame)["floors"][1]["rooms"]) == 1)
        click(page, frame, 2, 2)
        frame.locator("[data-testid=fp-room-name]").fill("Bedroom")
        frame.locator("[data-testid=fp-room-name]").press("Enter")
        save(frame)
        check("8. saved", api("api/floorplan")["status"]["summary"]["floors"] == 2)

        # 4. Configure: link rooms to areas (ground floor).
        frame.locator(".fp-floor", has_text="Ground floor").click()
        mode(frame, "configure")
        link_area(frame, page, (2, 1), KAMAR)
        link_area(frame, page, (8.5, 1), STOVA)
        link_area(frame, page, (1, 7), GONGD)
        names = [r["name"] for r in plan(frame)["floors"][0]["rooms"]]
        check("4. rooms linked to areas by stable id (named after them)", sorted(r["area_id"] for r in plan(frame)["floors"][0]["rooms"]) == sorted([KAMAR, STOVA, GONGD])
              and "Kamar Norður" in names, names)
        # Linking the same area again asks (duplicate links are not silent).
        frame.locator(".fp-floor", has_text="First floor").click()
        link_area(frame, page, (2, 2), KAMAR)
        expect(frame.locator("[data-testid=fp-dialog-link]")).to_be_visible()
        frame.locator("[data-testid=fp-dialog-link] .sheet-foot .btn", has_text="Cancel").click()
        check("4. a second link to the same area needs a decision (cancelled: not linked)",
              plan(frame)["floors"][1]["rooms"][0]["area_id"] is None)
        frame.locator("[data-left-tab=areas]").click()
        expect(frame.locator("[data-testid=fp-areas]")).to_contain_text("Home Assistant areas not on the plan")
        frame.locator("[data-left-tab=devices]").click()

        # 5. Devices: drag and drop, and click/tap then place.
        frame.locator(".fp-floor", has_text="Ground floor").click()
        frame.locator("[data-testid=fp-palette] input[type=search]").fill("radar_kamar")
        target = frame.locator("sl-plan-canvas")
        box = target.bounding_box()
        sx, sy = screen(frame, 1, 1)
        frame.locator("[data-entity='binary_sensor.radar_kamar']").drag_to(target, target_position={"x": sx - box["x"], "y": sy - box["y"]})
        check("5. a device dragged from the palette onto the plan", placement_at(frame, "binary_sensor.radar_kamar") is not None)
        for eid, at in (("light.bed_light", (3, 2)), ("binary_sensor.door_kamar", (5.5, 2)), ("binary_sensor.pir_stova", (8, 1)),
                        ("light.kitchen_lights", (2, 3)), ("cover.kitchen_window", (9, 3)), (TEMP, (1, 6))):
            place(frame, page, eid, at)
        check("5. devices placed with click and tap", len(plan(frame)["floors"][0]["placements"]) == 7)
        frame.locator("[data-testid=fp-palette] input[type=search]").fill("")
        # Inspector: identity, state, availability, area, automation assignment (kept apart from the placement).
        click(page, frame, 1, 1)
        insp = frame.locator("[data-testid=fp-inspector]")
        expect(insp).to_contain_text("binary_sensor.radar_kamar")
        expect(frame.locator("[data-testid=fp-device-automation]")).to_contain_text("Kamar Norður")
        expect(frame.locator("[data-testid=fp-device-automation]")).to_contain_text("Sensor")
        expect(frame.locator("[data-testid=fp-device-placement]")).to_contain_text("Inside Kamar Norður")
        check("5. device inspector: identity, state, assignment and visual placement shown separately", True)
        page.screenshot(path=str(shots / "a21-configure-device-1920.png"))
        # Moving an icon changes no area and no assignment.
        rooms_cfg = ha_ws(ha, token, {"type": "smart_occupancy/rooms/get"})["result"]["rooms"][KAMAR]["config"]
        drag(page, frame, (1, 1), (8.5, 2.5))  # the radar icon into Stova
        expect(frame.locator("[data-testid=fp-device-placement]")).to_contain_text("Inside Stova")
        after_cfg = ha_ws(ha, token, {"type": "smart_occupancy/rooms/get"})["result"]["rooms"][KAMAR]["config"]
        check("5. moving an icon does not change the automation assignment", after_cfg == rooms_cfg)
        frame.locator("[data-testid=fp-undo]").click()
        save(frame)

        # Assignment through the reviewed workflow: the kitchen light placed in Kamar Norður.
        click(page, frame, 2, 3)
        expect(frame.locator("[data-testid=fp-assign]")).to_be_visible()
        frame.locator("[data-testid=fp-assign]").click()
        expect(frame.locator("[data-testid=room-title]")).to_have_text("Kamar Norður", timeout=10000)
        expect(frame.locator("[data-testid=saved-notice]")).to_contain_text("Prepared from the floor plan")
        expect(frame.locator("[data-testid=unsaved-bar]")).to_be_visible()
        frame.locator("[data-testid=review]").click()
        dialog = frame.locator("[data-testid=review-dialog]")
        expect(dialog).to_contain_text("Kitchen Lights added as a light", timeout=10000)
        check("5. an assignment opens the Rooms editor's reviewed workflow (prepared, not saved)", True)
        dialog.locator(".sheet-foot .btn", has_text="Cancel").click()
        frame.locator("[data-testid=discard]").click()
        frame.locator("[data-testid=back-to-floorplan]").click()
        expect(frame.locator(".fp-page.mode-configure")).to_be_visible()

        # 7. Relationships: a door between Kamar Norður and Stova; reviewed adjacency through the Rooms editor.
        frame.locator("[data-left-tab=relations]").click()
        rel = frame.locator("[data-testid=fp-relations]")
        expect(rel).to_contain_text("connected by a door")
        expect(rel).to_contain_text("open plan")
        button = rel.locator(f"[data-review-adjacent='{KAMAR}:{STOVA}']")
        expect(button).to_be_visible()
        page.screenshot(path=str(shots / "a21-relations-1920.png"))
        check("7. suggested relationships with reasons, not applied", "stova" not in rooms_cfg.get("adjacent_rooms", []))
        button.click()
        expect(frame.locator("[data-testid=saved-notice]")).to_contain_text("Stova added as an adjacent room", timeout=10000)
        frame.locator("[data-testid=review]").click()
        dialog = frame.locator("[data-testid=review-dialog]")
        expect(dialog).to_contain_text("Stova added as an adjacent room", timeout=10000)
        for impact in dialog.locator("[data-impact] input").all():
            impact.check()
        dialog.locator("[data-testid=save]").click()
        expect(frame.locator("[data-testid=saved-notice]")).to_contain_text("Saved", timeout=20000)
        stored = ha_ws(ha, token, {"type": "smart_occupancy/rooms/get"})["result"]["rooms"][KAMAR]["config"]
        check("7. accepted adjacency saved by the integration after review", STOVA in stored["adjacent_rooms"], stored["adjacent_rooms"])
        frame.locator("[data-testid=back-to-floorplan]").click()
        frame.locator("[data-left-tab=relations]").click()
        expect(frame.locator(f"[data-review-adjacent='{KAMAR}:{STOVA}']")).to_have_count(0, timeout=10000)
        check("7. the suggestion is resolved after saving", True)

        # 6. Live: occupancy from the integration's room entity, lights, unavailable distinct from off.
        ctxdata = api("api/floorplan/context")
        state_entity = ctxdata["room_entities"].get(KAMAR, {}).get("state")
        check("6. the integration's room state entity is found by its unique id", bool(state_entity), ctxdata["room_entities"])
        mode(frame, "live")
        set_state(ha, token, "light.kitchen_lights", "on", {"friendly_name": "Kitchen Lights", "brightness": 128,
                                                              "supported_color_modes": ["brightness"], "color_mode": "brightness"})
        set_state(ha, token, state_entity, "confirmed_occupancy", {"friendly_name": "Kamar Norður", "occupied": True,
                                                                     "reason": "E2E: radar active", "area_id": KAMAR})
        canvas = frame.locator("sl-plan-canvas")
        expect(canvas.locator(".fp-room.tone-occupied")).to_have_count(1, timeout=10000)
        expect(canvas.locator(".fp-device.tone-active").first).to_be_visible()
        expect(canvas).to_contain_text("On · 50 %")
        check("6. live: room occupancy and light state with brightness", True)
        set_state(ha, token, TEMP, "unavailable", {"friendly_name": "E2E Temperature"})
        expect(canvas.locator(".fp-device.tone-unavailable")).to_have_count(1, timeout=10000)
        set_state(ha, token, state_entity, "unavailable", {"friendly_name": "Kamar Norður"})
        expect(canvas.locator(".fp-room.tone-unavailable")).to_have_count(1, timeout=10000)
        check("6. unavailable devices and rooms are shown as unavailable, not as off or vacant",
              canvas.locator(".fp-room.tone-unavailable .fp-room-hatch").count() == 1)
        frame.locator("[data-testid=fp-problems-only]").check()
        check("6. filter: only devices that need attention", canvas.locator(".fp-device").count() == 1, canvas.locator(".fp-device").count())
        frame.locator("[data-testid=fp-problems-only]").uncheck()
        page.screenshot(path=str(shots / "a21-live-1920.png"))
        before = placement_at(frame, "light.bed_light")
        drag(page, frame, (3, 2), (4, 3))
        check("6. live mode is read-only (icons cannot be moved)", placement_at(frame, "light.bed_light") == before
              and frame.locator("[data-testid=fp-save]").is_disabled())
        click(page, frame, 3, 2)
        expect(frame.locator("[data-testid=fp-inspector]")).to_contain_text("Bed Light")
        page.emulate_media(color_scheme="dark")
        time.sleep(0.3)
        page.screenshot(path=str(shots / "a21-live-dark-1920.png"))
        page.emulate_media(color_scheme="light")
        set_state(ha, token, TEMP, "21.5", {"friendly_name": "E2E Temperature", "unit_of_measurement": "°C", "device_class": "temperature"})

        # 8. Reload: everything is still there; editing after placing keeps placements.
        page.reload()
        frame = open_floorplan(page)
        expect(frame.locator(".fp-page.mode-live, .fp-page.mode-configure")).to_be_visible()
        fp = plan(frame)
        check("8. reload: floors, rooms, openings, devices and background kept",
              len(fp["floors"]) == 2 and len(fp["floors"][0]["rooms"]) == 3 and len(fp["floors"][0]["placements"]) == 7
              and fp["floors"][0]["background"] is not None)
        mode(frame, "build")
        frame.locator(".fp-floor", has_text="Ground floor").click()
        click(page, frame, 11, 2)  # the outer wall of Stova
        drag(page, frame, (11, 2), (11.5, 2))
        check("8. editing walls after placing devices keeps every placement", len(plan(frame)["floors"][0]["placements"]) == 7)

        # Leave guard with unsaved edits.
        page.once("dialog", lambda d: d.dismiss())
        frame.locator(".sidebar [data-nav=rooms]").click()
        time.sleep(0.3)
        check("8. leaving with unsaved changes asks first (stayed)", frame.locator(".fp-page").count() == 1)
        save(frame)

        # Two sessions: a clean session follows; a dirty one is told; a stale save asks.
        page2 = ctx.new_page()
        page2.on("pageerror", lambda e: errors.append(f"session 2: pageerror: {e}"))
        frame2 = open_floorplan(page2)
        mode(frame, "configure")
        mode(frame2, "configure")
        page.bring_to_front()
        frame.locator(".fp-floor", has_text="First floor").click()
        click(page, frame, 2, 2)
        frame.locator("[data-testid=fp-room-name]").fill("Main bedroom")
        frame.locator("[data-testid=fp-room-name]").press("Enter")
        save(frame)
        page2.bring_to_front()
        expect(frame2.locator("[data-testid=fp-notice]")).to_contain_text("another session", timeout=10000)
        check("8. another clean session loads the saved change", any(r["name"] == "Main bedroom" for f in plan(frame2)["floors"] for r in f["rooms"]))
        frame2.locator(".fp-floor", has_text="First floor").click()
        click(page2, frame2, 2, 2)
        frame2.locator("[data-testid=fp-room-name]").fill("Guest room")
        frame2.locator("[data-testid=fp-room-name]").press("Enter")
        page.bring_to_front()
        click(page, frame, 2, 2)
        frame.locator("[data-testid=fp-room-name]").fill("Bedroom 1")
        frame.locator("[data-testid=fp-room-name]").press("Enter")
        save(frame)
        page2.bring_to_front()
        expect(frame2.locator("[data-testid=fp-changed-elsewhere]")).to_be_visible(timeout=10000)
        frame2.locator("[data-testid=fp-save]").click()
        expect(frame2.locator("[data-testid=fp-dialog-conflict]")).to_be_visible()
        check("8. a stale save is never silent (conflict dialog)", True)
        frame2.locator("[data-testid=fp-overwrite]").click()
        expect(frame2.locator("[data-testid=fp-save-status]")).to_contain_text("Saved", timeout=10000)
        check("8. explicit overwrite saves on top of the newer version",
              any(r["name"] == "Guest room" for f in api("api/floorplan")["plan"]["floors"] for r in f["rooms"]))
        page2.close()
        page.bring_to_front()

        # Export, import and backups.
        page.reload()
        frame = open_floorplan(page)
        frame.locator("[data-testid=fp-menu]").click()
        with page.expect_download() as dl:
            frame.locator("[data-testid=fp-export]").click()
        export_path = out / "floorplan-export.json"
        dl.value.save_as(export_path)
        exported = json.loads(export_path.read_text())
        check("8. export contains the plan and its images", exported["plan"]["floors"] and len(exported["images"]) == 1
              and SUPERVISOR_TOKEN not in export_path.read_text())
        revision = api("api/floorplan")["revision"]
        frame.locator("[data-testid=fp-menu]").click()
        frame.locator("[data-testid=fp-import-file]").set_input_files(str(export_path))
        expect(frame.locator("[data-testid=fp-dialog-import]")).to_contain_text("identical", timeout=10000)
        frame.locator("[data-testid=fp-dialog-import] .sheet-foot .btn", has_text="Cancel").click()
        check("8. import reviews the file first (identical: nothing to do)", api("api/floorplan")["revision"] == revision)
        frame.locator("[data-testid=fp-menu]").click()
        frame.locator("[data-testid=fp-backups]").click()
        expect(frame.locator("[data-testid=fp-dialog-backups] .backup-list li").first).to_be_visible()
        check("8. backups listed", frame.locator("[data-testid=fp-dialog-backups] .backup-list li").count() >= 1)
        frame.locator("[data-testid=fp-dialog-backups] .sheet-foot .btn").click()

        # The Rooms editor still works on its own, and links back to the plan.
        frame.locator(".sidebar [data-nav=rooms]").click()
        frame.locator(f"[data-room={KAMAR}]").click()
        expect(frame.locator("[data-testid=room-title]")).to_have_text("Kamar Norður")
        frame.locator("[data-testid=show-on-floorplan]").click()
        expect(frame.locator("[data-testid=fp-inspector]")).to_contain_text("Kamar Norður", timeout=10000)
        check("9. the Rooms editor works and links to the room on the plan", True)

        # Security through Ingress: a changing request without the App's header is refused.
        status, _, _ = http(f"{INGRESS}api/floorplan/save", method="POST", body=json.dumps({"plan": {}, "revision": revision}).encode(),
                            headers={"Content-Type": "application/json"})
        check("10. writes without the App's CSRF header are refused", status == 403, status)
        page_html = http(INGRESS)[1].decode()
        check("10. no token in the page", SUPERVISOR_TOKEN not in page_html)
        page.screenshot(path=str(shots / "a21-configure-room-1920.png"))
        ctx.close()

        # Tablet with touch: tap to place, panels collapse.
        tctx = browser.new_context(viewport={"width": 1024, "height": 768}, is_mobile=True, has_touch=True, device_scale_factor=2)
        tpage = tctx.new_page()
        tpage.on("pageerror", lambda e: errors.append(f"tablet: pageerror: {e}"))
        tframe = open_floorplan(tpage)
        tframe.locator(".fp-modes [data-mode=configure]").tap()
        tframe.locator(".fp-floor", has_text="Ground floor").tap()
        if not tframe.locator("[data-testid=fp-left]").count():
            tframe.locator("[data-testid=fp-toggle-left]").tap()
        tframe.locator("[data-testid=fp-palette] input[type=search]").fill("ceiling_lights")
        tframe.locator("[data-entity='light.ceiling_lights']").tap()
        tpage.touchscreen.tap(*screen(tframe, 4.5, 1))
        time.sleep(0.3)
        check("5. touch: tap a device, then tap the plan to place it", placement_at(tframe, "light.ceiling_lights") is not None)
        tpage.screenshot(path=str(shots / "a21-tablet-1024-configure.png"))
        tframe.locator("[data-testid=fp-inspector] .inspector-head .icon-btn").tap()  # the inspector is a drawer here
        width_before = tframe.locator(".fp-center").bounding_box()["width"]
        tframe.locator("[data-testid=fp-toggle-left]").tap()
        time.sleep(0.2)
        check("11. panels collapse to give the canvas the room", tframe.locator(".fp-center").bounding_box()["width"] > width_before + 100)
        check("11. no horizontal scrolling on a landscape tablet", no_horizontal_overflow(tframe))
        tframe.locator("[data-testid=fp-save]").tap()
        expect(tframe.locator("[data-testid=fp-save-status]")).to_contain_text("Saved", timeout=10000)
        tctx.close()
        for size in ((768, 1024), (1366, 1024), (1440, 900)):
            vctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, is_mobile=size[0] < 1440, has_touch=size[0] < 1440)
            vpage = vctx.new_page()
            vframe = open_floorplan(vpage)
            for m in ("live", "configure", "build"):
                vframe.locator(f".fp-modes [data-mode={m}]").click()
                time.sleep(0.2)
            check(f"11. {size[0]}x{size[1]}: no horizontal scrolling", no_horizontal_overflow(vframe))
            vpage.screenshot(path=str(shots / f"a21-{size[0]}-build.png"))
            vctx.close()
        browser.close()

    watcher.stop_flag = True
    time.sleep(0.6)
    check("10. no service call reached Home Assistant", not watcher.events, watcher.events)
    commands = json.loads(http(f"{BASE}/_test/commands")[1])["commands"][commands_before:]
    types = [c.get("type") for c in commands]
    check("10. the App sent only allowlisted commands", set(types) <= ALLOWED, sorted(set(types) - ALLOWED))
    check("10. exactly one configuration write: the reviewed adjacency", types.count("smart_occupancy/rooms/set") == 1,
          types.count("smart_occupancy/rooms/set"))
    check("10. Home Assistant registries unchanged (no area, device or entity edits)", registries(ha, token) == before_registries)
    after_rooms = ha_ws(ha, token, {"type": "smart_occupancy/rooms/get"})["result"]
    changed = {a for a in set(after_rooms["rooms"]) | set(before_rooms["rooms"])
               if (after_rooms["rooms"].get(a) or {}).get("config") != (before_rooms["rooms"].get(a) or {}).get("config")}
    kamar_before = dict(before_rooms["rooms"][KAMAR]["config"])
    kamar_after = dict(after_rooms["rooms"][KAMAR]["config"])
    kamar_before.pop("adjacent_rooms", None)
    kamar_after.pop("adjacent_rooms", None)
    check("10. automation configuration unchanged except the reviewed adjacency", changed <= {KAMAR} and kamar_before == kamar_after,
          sorted(changed))
    final = api("api/floorplan")
    (out / STATE_FILE).write_text(json.dumps({"revision": final["revision"], "summary": final["status"]["summary"],
                                              "image": final["plan"]["floors"][0]["background"]["image_id"]}))


def phase_restart(args: argparse.Namespace, out: Path, errors: list[str]) -> None:
    expected = json.loads((out / STATE_FILE).read_text())
    doc = api("api/floorplan")
    check("restart: the floor plan is unchanged (same revision)", doc["revision"] == expected["revision"], doc["revision"])
    check("restart: no recovery was needed", not doc["status"]["notices"], doc["status"]["notices"])
    status, _, _ = http(f"{INGRESS}api/floorplan/images/{expected['image']}")
    check("restart: the background image is still served", status == 200, status)
    stored = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/rooms/get"})["result"]["rooms"][KAMAR]["config"]
    check("restart: the integration keeps the reviewed adjacency", STOVA in stored["adjacent_rooms"])
    with sync_playwright() as p:
        browser = p.chromium.launch(**({"executable_path": args.chromium} if args.chromium else {}))
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("pageerror", lambda e: errors.append(f"restart: pageerror: {e}"))
        frame = open_floorplan(page)
        expect(frame.locator(".fp-page.mode-live")).to_be_visible()
        expect(frame.locator("sl-plan-canvas .fp-room")).to_have_count(3, timeout=10000)
        expect(frame.locator("sl-plan-canvas .fp-device").first).to_be_visible()
        page.screenshot(path=str(out / "screenshots" / "a21-restart-live-1440.png"))
        check("restart: the App shows the saved plan live", True)
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
    (out / f"floorplan-results-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
