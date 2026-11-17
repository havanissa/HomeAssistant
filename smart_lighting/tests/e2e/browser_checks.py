"""End-to-end browser checks of the App through (simulated) Home Assistant Ingress.

Needs the stack from stack.sh, a real Home Assistant and Playwright:

    python -I browser_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> \
        --ha-config <HA config dir> --out <output dir> [--chromium <path>]

Every page is used inside the sidebar-panel iframe. Desktop and tablet
(portrait and landscape) are the primary targets; phone sizes (360, 390) are
kept as a regression check of the fallback layout. Touch emulation on
tablets and phones. A websocket to Home Assistant records every call_service
event during the run: the App must cause none.
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

import aiohttp
from playwright.sync_api import Frame, Page, expect, sync_playwright

INGRESS_TOKEN = "e2eIngressToken_AbC123"
SUPERVISOR_TOKEN = "e2e-supervisor-token-0123456789abcdef"
BASE = "http://127.0.0.1:18200"
PANEL = f"{BASE}/hassio/ingress/smart_lighting"
INGRESS = f"{BASE}/api/hassio_ingress/{INGRESS_TOKEN}/"
ROUTES = ["dashboard", "rooms", "floorplan", "presence", "lighting", "moods", "switches", "curtains", "predictions", "diagnostics", "settings"]
TITLES = {r: r.capitalize() for r in ROUTES} | {"floorplan": "Floor plan"}
BOTTOM = ["dashboard", "rooms", "lighting", "moods"]
READ_ONLY = {
    "ping", "get_config", "get_states", "subscribe_events", "config_entries/get", "config_entries/subscribe",
    "manifest/get", "config/entity_registry/list", "config/area_registry/list", "config/device_registry/list",
    "config/floor_registry/list", "config/auth/list", "smart_occupancy/rooms/get", "smart_occupancy/rooms/review",
    "smart_occupancy/rooms/restore/review", "smart_occupancy/presence/get", "smart_occupancy/presence/review",
    "history/history_during_period", "smart_occupancy/engine/status",
    # Phase A3.2: the occupancy bridge session (shadow mode; it changes nothing by itself).
    "smart_occupancy/engine/subscribe",
}
APP_VERSION = "0.4.0"
INTEGRATION_VERSION = "0.7.0"

VIEWPORTS = {
    "phone-360": {"viewport": {"width": 360, "height": 740}, "is_mobile": True, "has_touch": True, "device_scale_factor": 3},
    "phone-390": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True, "device_scale_factor": 3},
    "tablet-768": {"viewport": {"width": 768, "height": 1024}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2},
    "tablet-landscape": {"viewport": {"width": 1024, "height": 768}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2},
    "phone-landscape": {"viewport": {"width": 740, "height": 360}, "is_mobile": True, "has_touch": True, "device_scale_factor": 3},
    "tablet-landscape-1366": {"viewport": {"width": 1366, "height": 1024}, "is_mobile": True, "has_touch": True, "device_scale_factor": 2},
    "desktop-1920": {"viewport": {"width": 1920, "height": 1080}, "is_mobile": False, "has_touch": False, "device_scale_factor": 1},
    "desktop-1440": {"viewport": {"width": 1440, "height": 900}, "is_mobile": False, "has_touch": False, "device_scale_factor": 1},
}

results: list[dict[str, Any]] = []


def check(name: str, ok: bool, detail: Any = "") -> None:
    results.append({"check": name, "ok": bool(ok), "detail": detail})
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail}" if detail and not ok else ""), flush=True)


class ServiceCallWatcher(threading.Thread):
    """Records call_service events on the real Home Assistant during the run."""

    def __init__(self, url: str, token: str) -> None:
        super().__init__(daemon=True)
        self.url, self.token = url, token
        self.events: list[dict[str, Any]] = []
        self.ready = threading.Event()
        self.stop_flag = False

    def run(self) -> None:
        asyncio.run(self._run())

    async def _run(self) -> None:
        async with aiohttp.ClientSession() as s, s.ws_connect(self.url.replace("http", "ws") + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": self.token})
            await ws.receive_json()
            await ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "call_service"})
            await ws.receive_json()
            self.ready.set()
            while not self.stop_flag:
                try:
                    msg = await ws.receive_json(timeout=0.5)
                except (TimeoutError, asyncio.TimeoutError):
                    continue
                if msg.get("type") == "event":
                    self.events.append(msg["event"]["data"])


def app_frame(page: Page) -> Frame:
    page.wait_for_selector("#ingress")
    for _ in range(100):
        frame = next((f for f in page.frames if "hassio_ingress" in f.url), None)
        if frame is not None:
            return frame
        time.sleep(0.05)
    raise AssertionError("iframe not found")


def wait_connected(frame: Frame) -> None:
    expect(frame.locator("[data-testid=connection-pill]")).to_contain_text("Connected", timeout=15000)


def no_horizontal_overflow(frame: Frame) -> bool:
    return frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def small_targets(frame: Frame) -> list[str]:
    """Visible interactive elements smaller than 44 x 44 CSS px (40 allowed for inline icons)."""
    return frame.evaluate(
        """() => {
        const out = [];
        const els = document.querySelectorAll('a[href], button, summary, .segmented label, .switch-row, .picker-field, [role=option]');
        for (const el of els) {
          const r = el.getBoundingClientRect();
          const style = getComputedStyle(el);
          if (!r.width || !r.height || style.visibility === 'hidden') continue;
          if (el.closest('dialog') && !el.closest('dialog').open) continue;
          if (el.classList.contains('skip-link')) continue;
          if (r.height < 43.5 || r.width < 43.5) out.push(`${el.tagName.toLowerCase()}.${el.className}|${(el.textContent||'').trim().slice(0,30)}|${Math.round(r.width)}x${Math.round(r.height)}`);
        }
        return out;
      }"""
    )


def go(frame: Frame, route: str, layout: str, page: Page) -> None:
    """Navigate like a user of that layout would (tap on touch screens)."""
    tap = layout != "desktop"
    def press(locator):
        locator.tap() if tap else locator.click()
    if layout == "phone":
        if route in BOTTOM:
            press(frame.locator(f".bottom-nav [data-bottom={route}]"))
        else:
            press(frame.locator(".bottom-nav [data-bottom=more]"))
            expect(frame.locator("dialog.more-sheet")).to_be_visible()
            press(frame.locator(f"dialog.more-sheet [data-nav={route}]"))
            expect(frame.locator("dialog.more-sheet")).to_have_count(0)
    elif layout == "tablet":
        press(frame.locator(f".sidebar [data-nav={route}]"))
    else:
        press(frame.locator(f".sidebar [data-nav={route}]"))
    expect(frame.locator("h1.page-title")).to_have_text(TITLES[route])


# Expected dashboard columns (content width without sidebar / rail).
EXPECTED_COLUMNS = {"tablet-768": 2, "tablet-landscape": 2, "phone-landscape": 2, "tablet-landscape-1366": 3,
                    "desktop-1440": 3, "desktop-1920": 3, "phone-360": 1, "phone-390": 1}


def layout_of(name: str) -> str:
    width = VIEWPORTS[name]["viewport"]["width"]
    return "phone" if width < 600 else "tablet" if width < 1024 else "desktop"


def run_viewport(browser: Any, name: str, out: Path, errors: list[str]) -> None:
    opts = VIEWPORTS[name]
    layout = layout_of(name)
    ctx = browser.new_context(**opts, color_scheme="light", permissions=["clipboard-read", "clipboard-write"])
    page = ctx.new_page()
    page.on("console", lambda m: m.type == "error" and errors.append(f"{name}: console: {m.text}"))
    page.on("pageerror", lambda e: errors.append(f"{name}: pageerror: {e}"))
    page.goto(PANEL)
    frame = app_frame(page)
    wait_connected(frame)
    shots = out / "screenshots"

    # Navigation structure for this layout.
    bottom_visible = frame.locator(".bottom-nav").is_visible()
    sidebar_box = frame.locator(".sidebar").bounding_box() if frame.locator(".sidebar").is_visible() else None
    if layout == "phone":
        check(f"{name}: bottom navigation shown, sidebar hidden", bottom_visible and sidebar_box is None)
        check(f"{name}: bottom navigation has 5 entries", frame.locator(".bottom-nav a, .bottom-nav button").count() == 5)
    elif layout == "tablet":
        check(f"{name}: collapsible rail (no bottom navigation)", not bottom_visible and sidebar_box and sidebar_box["width"] < 100, sidebar_box)
    else:
        check(f"{name}: sidebar with labels", not bottom_visible and sidebar_box and sidebar_box["width"] > 200, sidebar_box)

    page.screenshot(path=str(shots / f"{name}-dashboard-light.png"), full_page=False)
    columns = frame.evaluate("getComputedStyle(document.querySelector('sl-dashboard .grid')).gridTemplateColumns.split(' ').length")
    check(f"{name}: dashboard uses {EXPECTED_COLUMNS[name]} column(s)", columns == EXPECTED_COLUMNS[name], columns)

    # Every module, by touch/click, with no horizontal scrolling and no controls on pending screens.
    overflow, pending_controls, target_issues = [], [], {}
    for route in ROUTES:
        go(frame, route, layout, page)
        if not no_horizontal_overflow(frame):
            overflow.append(route)
        if route == "rooms":
            expect(frame.locator("[data-testid=room-grid] .room-card").first).to_be_visible()
        elif route == "floorplan":
            expect(frame.locator(".fp-page")).to_be_visible()
        elif route == "presence":
            # Phase A3.1: the Presence interface is in the App; the integration keeps the engine.
            expect(frame.locator("[data-testid=presence-ownership]")).to_contain_text("Smart Occupancy integration")
        elif route not in ("dashboard", "diagnostics", "settings"):
            card = frame.locator(f"[data-testid=pending-{route}]")
            expect(card).to_be_visible()
            expect(frame.locator(".page-header .chip-pending")).to_have_text("Migration pending")
            expect(card.locator("[data-testid=controlled-by]")).to_contain_text("Smart Occupancy integration")
            n = card.locator("input, select, textarea, button").count()
            if n:
                pending_controls.append(f"{route}:{n}")
        issues = small_targets(frame)
        if issues:
            target_issues[route] = issues
    check(f"{name}: all 11 modules reachable", True)
    check(f"{name}: no horizontal scrolling on any module", not overflow, overflow)
    check(f"{name}: pending modules show no controls and the integration in control", not pending_controls, pending_controls)
    go(frame, "rooms", layout, page)
    expect(frame.locator("[data-testid=room-grid] .room-card").first).to_be_visible()
    page.screenshot(path=str(shots / f"{name}-rooms-light.png"))
    if layout != "desktop":
        check(f"{name}: touch targets >= 44px", not target_issues, target_issues)

    if layout == "phone":
        go(frame, "dashboard", layout, page)
        frame.locator(".bottom-nav [data-bottom=more]").tap()
        sheet = frame.locator("dialog.more-sheet")
        expect(sheet).to_be_visible()
        check(f"{name}: More sheet lists every module", sheet.locator("[data-nav]").count() == 11)
        page.screenshot(path=str(shots / f"{name}-more-sheet.png"))
        frame.locator("dialog.more-sheet .sheet-head .icon-btn").tap()
        expect(sheet).to_have_count(0)

    if layout == "tablet":
        frame.locator(".rail-toggle").tap()
        expect(frame.locator(".shell.rail-expanded")).to_have_count(1)
        width = frame.locator(".sidebar").bounding_box()["width"]
        check(f"{name}: rail expands to a full navigation panel", width > 200, width)
        page.screenshot(path=str(shots / f"{name}-rail-expanded.png"))
        frame.locator(".sidebar [data-nav=curtains]").tap()
        expect(frame.locator("h1.page-title")).to_have_text("Curtains")
        check(f"{name}: rail collapses after choosing a module", frame.locator(".shell.rail-expanded").count() == 0)

    # Searchable selector: full screen on phones; search, simulated on-screen keyboard, choose.
    go(frame, "diagnostics", layout, page)
    lookup = frame.locator("[data-testid=card-lookup]")
    lookup.scroll_into_view_if_needed()
    field = lookup.locator("[data-testid=picker-field]")
    field.tap() if layout != "desktop" else field.click()
    dialog = lookup.locator("dialog.sheet")
    expect(dialog).to_be_visible()
    box = dialog.bounding_box()
    vw, vh = opts["viewport"]["width"], opts["viewport"]["height"]
    if layout == "phone":
        check(f"{name}: selector opens full screen", box["width"] >= vw - 1 and box["height"] >= vh - 1, box)
    search = dialog.locator("input[type=search]")
    expect(search).to_be_focused()
    search.fill("manual")
    first = dialog.locator("[role=option]").first
    expect(first).to_contain_text("Manual Mood")
    if layout == "phone":
        # On-screen keyboard: the visible area shrinks to ~half; search and results stay usable.
        page.set_viewport_size({"width": vw, "height": int(vh * 0.5)})
        time.sleep(0.3)
        s_box, o_box = search.bounding_box(), first.bounding_box()
        frame_h = page.viewport_size["height"]
        check(f"{name}: search field and first result visible above the keyboard",
              s_box["y"] >= 0 and s_box["y"] + s_box["height"] <= frame_h and o_box["y"] + o_box["height"] <= frame_h,
              {"search": s_box, "option": o_box, "height": frame_h})
        page.screenshot(path=str(shots / f"{name}-picker-keyboard.png"))
        first.tap()
        page.set_viewport_size(opts["viewport"])
    else:
        page.screenshot(path=str(shots / f"{name}-picker.png"))
        first.tap() if layout != "desktop" else search.press("Enter")
    expect(dialog).to_have_count(0)
    expect(lookup.locator("[data-testid=lookup-details]")).to_contain_text("input_select.manual_mood")
    if vw >= 1024:
        picker_box = lookup.locator("sl-picker").bounding_box()
        details_box = lookup.locator("[data-testid=lookup-details]").bounding_box()
        check(f"{name}: lookup selector and details side by side", details_box["x"] >= picker_box["x"] + picker_box["width"])
    check(f"{name}: entity selector search and choose", True)
    # Area search ignores accents and case.
    lookup.locator(".segmented label", has_text="Area").tap() if layout != "desktop" else lookup.locator(".segmented label", has_text="Area").click()
    field.tap() if layout != "desktop" else field.click()
    lookup.locator("dialog.sheet input[type=search]").fill("KITCH")
    expect(lookup.locator("dialog.sheet [role=option]").first).to_contain_text("Kitchen")
    lookup.locator("dialog.sheet [role=option]").first.click()
    expect(lookup.locator("[data-testid=lookup-details]")).to_contain_text("Kitchen")
    check(f"{name}: area selector search and choose", True)

    # Diagnostics data in-app (Companion app parity) and copy.
    frame.locator("[data-testid=view-diagnostics]").click()
    view = frame.locator("textarea.json-view")
    expect(view).to_be_visible()
    text = view.input_value()
    data = json.loads(text)
    check(f"{name}: diagnostics viewable in-app", data["safety"]["service_calls_sent"] == 0)
    check(f"{name}: no token in diagnostics view", SUPERVISOR_TOKEN not in text)
    frame.locator("[data-testid=copy-diagnostics]").click()
    expect(frame.locator("[data-testid=copy-diagnostics]")).to_contain_text(re.compile("Copied|Selected"))
    check(f"{name}: no horizontal scrolling with diagnostics data open", no_horizontal_overflow(frame))

    # Dark theme + screenshots.
    page.emulate_media(color_scheme="dark")
    go(frame, "dashboard", layout, page)
    bg = frame.evaluate("getComputedStyle(document.body).backgroundColor")
    check(f"{name}: automatic dark theme", bg == "rgb(17, 19, 21)", bg)
    page.screenshot(path=str(shots / f"{name}-dashboard-dark.png"))
    go(frame, "diagnostics", layout, page)
    page.screenshot(path=str(shots / f"{name}-diagnostics-dark.png"))
    page.emulate_media(color_scheme="light")

    # Accessibility text scaling (200 %) on phones: still no horizontal scrolling.
    if layout == "phone":
        frame.evaluate("document.documentElement.style.fontSize = '200%'")
        problems = []
        for route in ("dashboard", "diagnostics", "settings", "curtains"):
            go(frame, route, layout, page)
            if not no_horizontal_overflow(frame):
                problems.append(route)
        page.screenshot(path=str(shots / f"{name}-text-200.png"))
        check(f"{name}: 200% text size without horizontal scrolling", not problems, problems)
        nav_box = frame.locator(".bottom-nav").bounding_box()
        check(f"{name}: bottom navigation usable at 200% text", nav_box["height"] < vh * 0.25, nav_box)
        frame.evaluate("document.documentElement.style.fontSize = ''")
    ctx.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ha-url", required=True)
    ap.add_argument("--ha-token", required=True)
    ap.add_argument("--ha-config", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--chromium")
    args = ap.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    auth = {"Authorization": f"Bearer {args.ha_token}"}

    def entry_snapshot() -> Any:
        data = json.loads((Path(args.ha_config) / ".storage/core.config_entries").read_text())
        return [{k: e.get(k) for k in ("entry_id", "data", "options", "disabled_by", "version", "minor_version")}
                for e in data["data"]["entries"] if e["domain"] == "smart_occupancy"]

    before_entry = entry_snapshot()
    watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    watcher.start()
    watcher.ready.wait(10)

    import urllib.request

    def get_json(url: str, headers: dict[str, str] | None = None, method: str = "GET", body: Any = None) -> Any:
        req = urllib.request.Request(url, headers=headers or {}, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    overview = get_json(INGRESS + "api/overview")
    check("ingress: overview through Ingress", overview["home_assistant"]["connection"]["state"] == "connected")
    check("integration detected and running", overview["integration"]["status"] == "loaded", overview["integration"])
    check("integration version read", overview["integration"]["version"] == INTEGRATION_VERSION, overview["integration"]["version"])
    check("Home Assistant version read", overview["home_assistant"]["info"]["version"] == "2026.11.1")
    check("supervisor information read", overview["supervisor"]["available"] and not overview["supervisor"]["error"], overview["supervisor"])
    ent_ids = [e["entity_id"] for e in overview["integration_entities"]]
    check("integration entities listed", bool(ent_ids), ent_ids)

    with sync_playwright() as p:
        launch = {"executable_path": args.chromium} if args.chromium else {}
        browser = p.chromium.launch(**launch)
        for name in VIEWPORTS:
            run_viewport(browser, name, out, errors)

        # Desktop-only flows: live update, reconnect, deep links, theme persistence, download.
        ctx = browser.new_context(viewport={"width": 1280, "height": 900}, accept_downloads=True)
        page = ctx.new_page()
        page.on("console", lambda m: m.type == "error" and errors.append(f"flows: console: {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"flows: pageerror: {e}"))
        page.goto(PANEL)
        frame = app_frame(page)
        wait_connected(frame)
        target = next((e for e in ent_ids if e.startswith("sensor.")), ent_ids[0])
        frame.locator("[data-testid=card-integration] summary").click()
        marker = f"e2e-live-{int(time.time())}"
        current = get_json(f"{args.ha_url}/api/states/{target}", auth)
        get_json(f"{args.ha_url}/api/states/{target}", auth, "POST", {"state": marker, "attributes": current["attributes"]})
        started = time.monotonic()
        expect(frame.locator(f"[data-entity='{target}'] .state")).to_contain_text(marker, timeout=5000)
        check("live state update reaches the browser", True, f"{time.monotonic() - started:.2f} s")
        get_json(f"{args.ha_url}/api/states/{target}", auth, "POST", {"state": current["state"], "attributes": current["attributes"]})

        get_json(f"{BASE}/_test/drop_core", method="POST", body={})
        expect(frame.locator("[data-testid=connection-pill]")).to_contain_text(re.compile("Reconnecting|Connecting"), timeout=5000)
        wait_connected(frame)
        check("reconnects after the Home Assistant connection drops", True)

        go(frame, "settings", "desktop", page)
        frame.locator(".segmented label", has_text="Dark").click()
        check("theme can be forced to dark", frame.evaluate("document.documentElement.dataset.theme") == "dark")
        frame.locator("[data-testid=advanced-toggle]").click()
        expect(frame.locator("[data-testid=advanced-settings]")).to_contain_text("present (hidden)")
        check("advanced settings hidden until requested, token hidden", SUPERVISOR_TOKEN not in frame.content())
        page.reload()
        frame = app_frame(page)
        wait_connected(frame)
        check("theme choice persists", frame.evaluate("document.documentElement.dataset.theme") == "dark")
        frame.evaluate("localStorage.clear()")

        go(frame, "diagnostics", "desktop", page)
        with page.expect_download() as dl:
            frame.locator("[data-testid=download-diagnostics]").click()
        body = Path(dl.value.path()).read_text()
        check("diagnostics download", json.loads(body)["app"]["version"] == APP_VERSION)
        check("no secrets in the download", SUPERVISOR_TOKEN not in body and args.ha_token not in body)

        for url, title in ((INGRESS + "#/curtains", "Curtains"), (INGRESS.rstrip("/") + "#/settings", "Settings")):
            page.goto(url)
            expect(page.locator("h1.page-title")).to_have_text(title, timeout=10000)
            check(f"deep link {url[len(BASE):]}", True)
        ctx.close()
        browser.close()

    watcher.stop_flag = True
    time.sleep(0.7)
    check("no service calls on Home Assistant during the run", not watcher.events, watcher.events)
    commands = get_json(f"{BASE}/_test/commands")["commands"]
    types = {c["type"] for c in commands}
    check("only read-only commands reached Home Assistant (no configuration written in this run)", types <= READ_ONLY,
          sorted(types - READ_ONLY))
    check("integration configuration unchanged", entry_snapshot() == before_entry)
    filtered = [e for e in errors if "favicon" not in e]
    check("no browser console or page errors", not filtered, filtered)

    (out / "results.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
