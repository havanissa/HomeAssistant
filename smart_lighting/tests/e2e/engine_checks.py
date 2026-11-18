"""End-to-end checks of Phase A3.2 (occupancy engine migration) with a real Home Assistant and the real App image.

Needs the stack from stack.sh started with lowered handover thresholds
(``ENGINE_MIN_SHADOW_SECONDS=60 ENGINE_MIN_TRANSITIONS=6``), a real Home
Assistant with Smart Occupancy & Lighting 0.8.0 (or 0.7.0) and the A3.1 seed (Kamar Norður
with the template radar that follows ``input_boolean.nobara_pc``), Docker
(to crash and restart the App container) and Playwright:

    python -I engine_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check|finish]

People are moved by writing the helper's state (``POST /api/states``), never
by a service call, so every service call seen during the run (other than the
explicit rollback service of the last phase) fails the run: an ownership
change must never switch a device.

``all``: shadow parity on real events, readiness, a reviewed handover in the
browser, the App's results published through the integration's entities, a
crash of the App container during occupancy (fallback without vacancy), the
restarted App staying in shadow mode, a second handover and the browser's
"Return control to integration", then a third handover left in place.
``restart-check`` (after Home Assistant was restarted, the App kept running):
the same App process resumed its ownership. ``finish``: rollback with the
administrator service, timing restored.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.request

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, no_horizontal_overflow, results  # noqa: E402
from rooms_checks import ha_ws, http  # noqa: E402

KAMAR = "kamar_nordur"
PC = "input_boolean.nobara_pc"
STATE_FILE = "engine_e2e_state.json"
# Short visits for the run (the start-up hold override applies from the next start, e.g. the restart check).
FAST = {"correlation_window": 1, "exit_confirmation": 5, "inactivity_timeout": 30, "dropout_grace": 0, "startup_hold": 0}
POST = {"X-Smart-Lighting": "1"}


def app(path: str, method: str = "GET", body: Any = None) -> Any:
    return http(INGRESS + path, POST if method == "POST" else None, method, body)


def app_post_status(path: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(INGRESS + path, method="POST", data=json.dumps(body).encode(),
                                 headers={**POST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def engine(args: argparse.Namespace) -> dict[str, Any]:
    r = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/engine/status"})
    assert r["success"], r
    return r["result"]


def presence_room(args: argparse.Namespace) -> dict[str, Any]:
    r = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/presence/get", "area_id": KAMAR})
    assert r["success"], r
    return r["result"]["rooms"][KAMAR]


def entity(args: argparse.Namespace, entity_id: str) -> dict[str, Any]:
    req = urllib.request.Request(f"{args.ha_url}/api/states/{entity_id}",
                                 headers={"Authorization": f"Bearer {args.ha_token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def set_person(args: argparse.Namespace, on: bool) -> None:
    req = urllib.request.Request(f"{args.ha_url}/api/states/{PC}", method="POST",
                                 data=json.dumps({"state": "on" if on else "off",
                                                  "attributes": {"friendly_name": "Nobara PC active"}}).encode(),
                                 headers={"Authorization": f"Bearer {args.ha_token}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def wait(what: str, condition, timeout: float = 60, interval: float = 1.0) -> Any:
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = condition()
        except Exception as err:  # noqa: BLE001 - the App may be restarting
            last = err
        if last and not isinstance(last, Exception):
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}: {last}")


def set_timing(args: argparse.Namespace, values: dict[str, Any]) -> None:
    room = presence_room(args)
    change = {"timing": values}
    review = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/presence/review", "area_id": KAMAR,
                                                 "change": change, "expected_revision": room["revision"]})
    assert review["success"], review
    reply = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/presence/set", "area_id": KAMAR, "change": change,
                                                "expected_revision": room["revision"],
                                                "confirmed": review["result"]["requires_confirmation"]})
    assert reply["success"], reply


def state_entity(args: argparse.Namespace) -> str:
    return presence_room(args)["entities"]["state"]


def visit(args: argparse.Namespace) -> None:
    """Someone comes in (radar on), is confirmed, leaves; vacant after the 30 s inactivity timeout."""
    sid = state_entity(args)
    set_person(args, True)
    wait("confirmed", lambda: entity(args, sid)["state"] == "confirmed_occupancy", 20)
    time.sleep(2)
    set_person(args, False)
    wait("vacant", lambda: entity(args, sid)["state"] == "vacant", 60)
    time.sleep(2)


def ready(min_matched: int = 6) -> dict[str, Any]:
    data = app("api/engine")
    return data if data["readiness"]["ready"] and data["comparison"]["totals"]["matched"] >= min_matched else None


def screenshot_engine(args: argparse.Namespace, out: Path, name: str, actions=None) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
        for label, size in (("desktop-1440", (1440, 900)), ("tablet-1024", (1024, 768))):
            page = browser.new_page(viewport={"width": size[0], "height": size[1]})
            page.goto(PANEL)
            frame = app_frame(page)
            expect(frame.locator("[data-testid=connection-pill]")).to_contain_text("Connected", timeout=20000)
            frame.locator(".sidebar [data-nav=presence]").click() if label.startswith("desktop") else \
                frame.evaluate("location.hash = '#/presence'")
            expect(frame.locator("[data-testid=presence-engine-link]")).to_be_visible(timeout=15000)
            frame.locator("[data-testid=presence-engine-link]").click()
            expect(frame.locator("[data-testid=engine-owner]")).to_be_visible(timeout=15000)
            check(f"{name} {label}: no horizontal overflow", no_horizontal_overflow(frame))
            page.screenshot(path=str(out / f"engine-{name}-{label}.png"), full_page=True)
            if actions and label.startswith("desktop"):
                actions(frame, page)
            page.close()
        browser.close()


def ui_handover(frame: Any, page: Any) -> None:
    button = frame.locator("[data-testid=engine-handover]")
    expect(button).to_be_enabled(timeout=15000)
    button.click()
    dialog = frame.locator("[data-testid=engine-dialog]")
    expect(dialog).to_be_visible()
    confirm = dialog.locator("[data-testid=engine-confirm]")
    check("handover needs the confirmation box", confirm.is_disabled())
    dialog.locator("[data-testid=engine-understood]").check()
    confirm.click()
    expect(frame.locator("[data-testid=engine-owner] .engine-owner")).to_contain_text("Smart Lighting App", timeout=20000)


def ui_release(frame: Any, page: Any) -> None:
    frame.locator("[data-testid=engine-release]").click()
    dialog = frame.locator("[data-testid=engine-dialog]")
    expect(dialog).to_be_visible()
    dialog.locator("[data-testid=engine-confirm]").click()
    expect(frame.locator("[data-testid=engine-owner] .engine-owner")).to_contain_text("integration", timeout=20000)


def phase_all(args: argparse.Namespace, out: Path) -> None:
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)
    state: dict[str, Any] = {"timing": presence_room(args)["timing_overrides"]}
    (out / STATE_FILE).write_text(json.dumps(state))
    own = engine(args)
    check("integration 0.8.0 is the owner by default", own["owner"] == "integration" and own["integration_version"] == "0.8.0",
          own)
    data = wait("App in shadow mode", lambda: (d := app("api/engine"))["mode"] == "shadow" and d, 60)
    check("App: shadow mode, bridge connected, owner acknowledged", data["bridge"] == "connected"
          and data["acknowledged_owner"] == "integration", data["bridge"])
    check("App: same engine (fingerprints match)", data["versions"]["fingerprint"] == own["fingerprint"])
    check("App: golden self-test passed", data["self_test"]["passed"], data["self_test"])
    check("App: configuration in sync", data["config"]["in_sync"], data["config"])
    check("App: not ready before any comparison", not data["readiness"]["ready"])
    status, body = app_post_status("api/engine/handover", {"confirm": True})
    check("handover refused while not ready", status == 409 and body.get("code") == "not_ready", (status, body))

    set_timing(args, FAST)  # live change: the App must follow it (configuration sync)
    wait("App follows the timing change", lambda: app("api/engine")["config"]["in_sync"], 20)
    set_person(args, False)
    # The rooms reset just reloaded the integration: its start-up hold (set at the start, not moved by a live
    # timing change) holds vacancy back for up to 120 s.
    wait("start-up hold over", lambda: not presence_room(args)["occupancy"]["starting_up"], 150, 3)
    for _ in range(2):
        visit(args)
    data = wait("readiness after two visits", ready, 90)
    totals = data["comparison"]["totals"]
    check("shadow parity on real events", totals["disagreements"] == 0 and totals["matched"] == totals["transitions"]
          and totals["matched"] >= 6, totals)
    check("no service call during shadow mode", not args.watcher.events, args.watcher.events[:3])

    # Handover in the browser (desktop), screenshots before and after.
    screenshot_engine(args, shots, "ready", ui_handover)
    own = engine(args)
    check("the integration acknowledges the App as the owner", own["owner"] == "app" and own["history"][0]["reason"] == "handover",
          own["state"])
    check("handover recorded the readiness", own["history"][0].get("readiness", {}).get("ready") is True)
    reason = entity(args, presence_room(args)["entities"]["reason"])
    check("the room's reason entity says the App decided", reason["attributes"].get("decided_by") == "app",
          reason["attributes"])
    before = own["accepted_total"]
    set_person(args, True)
    sid = state_entity(args)
    wait("App-published occupancy reaches the entity", lambda: entity(args, sid)["state"] == "confirmed_occupancy", 20)
    check("the App's results are accepted", engine(args)["accepted_total"] > before)
    check("no service call caused by the handover", not args.watcher.events, args.watcher.events[:3])

    # Crash the App during occupancy: the integration takes over without vacancy.
    subprocess.run(["docker", "kill", "sl-test-app"], check=True, capture_output=True)
    crashed = time.time()
    own = wait("fallback after the lease", lambda: (e := engine(args))["owner"] == "integration" and e, 60)
    check("fallback within the lease (30 s) and a margin", time.time() - crashed < 45, round(time.time() - crashed))
    check("fallback reason: lease expired", own["history"][0]["reason"] == "lease_expired", own["history"][0])
    check("occupied room stays occupied across the fallback", entity(args, sid)["state"] in (
        "confirmed_occupancy", "possible_vacancy"), entity(args, sid)["state"])
    check("no service call caused by the fallback", not args.watcher.events, args.watcher.events[:3])
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    data = wait("restarted App back in shadow mode", lambda: (d := app("api/engine"))["mode"] == "shadow" and d, 90)
    time.sleep(5)
    own = engine(args)
    check("the restarted App does not take ownership back", own["owner"] == "integration"
          and own["history"][0]["reason"] == "lease_expired" and data["acknowledged_owner"] == "integration", own["state"])
    set_person(args, False)
    wait("vacant through the integration engine", lambda: entity(args, sid)["state"] == "vacant", 60)

    # Comparison restarts with the new session: two more visits, then a handover and the browser rollback.
    for _ in range(2):
        visit(args)
    wait("readiness again", ready, 90)
    status, body = app_post_status("api/engine/handover", {"confirm": True})
    check("second handover", status == 200 and engine(args)["owner"] == "app", (status, body))
    set_person(args, True)
    wait("occupied (App)", lambda: entity(args, sid)["state"] == "confirmed_occupancy", 20)
    screenshot_engine(args, shots, "app-owner", ui_release)
    own = engine(args)
    check("Return control to integration (browser)", own["owner"] == "integration" and own["history"][0]["reason"] == "released",
          own["history"][0])
    check("rollback during occupancy keeps it occupied", entity(args, sid)["state"] == "confirmed_occupancy",
          entity(args, sid)["state"])
    set_person(args, False)
    wait("vacant", lambda: entity(args, sid)["state"] == "vacant", 60)
    status, _ = app_post_status("api/engine/handover", {"confirm": True})
    check("third handover (left in place for the restart check)", status == 200 and engine(args)["owner"] == "app")
    check("no service call in the whole run", not args.watcher.events, args.watcher.events[:5])
    commands = app("api/diagnostics")["safety"]["commands_sent"]
    check("the App sent no service call and only allowlisted commands",
          not any(c.startswith(("call_service", "execute_script", "fire_event")) for c in commands), sorted(commands))


def phase_restart(args: argparse.Namespace, out: Path) -> None:
    own = wait("the App resumed after the Home Assistant restart",
               lambda: (e := engine(args))["owner"] == "app" and e["state"] == "app" and e, 150, 3)
    check("same App process resumed its ownership", own["history"][0]["reason"] == "resumed", own["history"][0])
    sid = state_entity(args)
    set_person(args, True)
    wait("App-published occupancy after the restart", lambda: entity(args, sid)["state"] == "confirmed_occupancy", 30)
    set_person(args, False)
    check("no service call after the restart", not args.watcher.events, args.watcher.events[:3])


def phase_finish(args: argparse.Namespace, out: Path) -> None:
    req = urllib.request.Request(f"{args.ha_url}/api/services/smart_occupancy/engine_return_to_integration",
                                 method="POST", data=b"{}",
                                 headers={"Authorization": f"Bearer {args.ha_token}", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()
    own = engine(args)
    check("rollback with the administrator service (no App needed)", own["owner"] == "integration"
          and bool(own["history"]) and own["history"][0]["reason"] == "released", own["history"][:1])
    data = wait("App back in shadow mode", lambda: (d := app("api/engine"))["mode"] == "shadow" and d, 20)
    check("App acknowledges the integration", data["acknowledged_owner"] == "integration")
    saved = json.loads((out / STATE_FILE).read_text())["timing"]
    set_timing(args, {k: saved.get(k) for k in FAST})
    check("timing restored", {k: presence_room(args)["timing_overrides"].get(k) for k in FAST}
          == {k: saved.get(k) for k in FAST})
    other = [e for e in args.watcher.events if e.get("domain") != "smart_occupancy"]
    check("no service call besides the rollback service", not other, other[:3])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chromium")
    parser.add_argument("--phase", default="all", choices=["all", "restart-check", "finish"])
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(10)
    try:
        {"all": phase_all, "restart-check": phase_restart, "finish": phase_finish}[args.phase](args, out)
    except AssertionError as err:
        check("run completed", False, str(err))
    finally:
        args.watcher.stop_flag = True
    failed = [r for r in results if not r["ok"]]
    (out / f"engine-{args.phase}.json").write_text(json.dumps(results, indent=1))
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
