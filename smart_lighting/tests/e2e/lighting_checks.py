"""End-to-end checks of Phase A4 (lighting migration) with a real Home Assistant and the real App image.

Needs the stack from stack.sh started with lowered handover thresholds
(``LIGHTING_MIN_SHADOW_SECONDS=60 LIGHTING_MIN_COMMANDS=3``), a real Home
Assistant with Smart Occupancy & Lighting 0.8.0, its lighting configuration reset
(no ``.storage/smart_occupancy.lighting*`` files) and the A3.1 seed (Kamar Norður
with the template radar that follows ``input_boolean.nobara_pc`` and the demo
``light.bed_light``), Docker (to crash and restart the App container) and
Playwright:

    python -I lighting_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check|finish]

People and moods are changed by writing helper states (``POST /api/states``),
never by a service call, so every light service call seen during the run is a
lighting decision; around every change of control the run checks that none
happened.

``all``: configuration through the App while the integration controls lighting
(forwarded commands), shadow comparison on real events, readiness, a reviewed
handover in the browser, the App controlling the light through the integration's
fenced executor, the integration's editors read-only, edits saved in the App and
mirrored, a crash of the App container (the integration takes lighting back
without switching anything), the restarted App staying in shadow, a second
handover, a manual override, "Return control" in the browser (override kept,
nothing switched), and a third handover left in place. ``restart-check`` (after
Home Assistant restarted, the App kept running): the same App resumed and still
controls the light. ``finish``: rollback with the administrator service.
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
LIGHT = "light.bed_light"
PC = "input_boolean.nobara_pc"
MAIN = "input_select.main_mood"
POST = {"X-Smart-Lighting": "1"}
LEVELS = {"Dagur": 80, "Kvøld": 45, "Nátt": 10}


def app(path: str, method: str = "GET", body: Any = None) -> Any:
    return http(INGRESS + path, POST if method == "POST" else None, method, body)


def app_post_status(path: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(INGRESS + path, method="POST", data=json.dumps(body).encode(),
                                 headers={**POST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def command(type_: str, **params: Any) -> tuple[int, Any]:
    return app_post_status("api/lighting/command", {"type": f"smart_occupancy/lighting/{type_}", **params})


def lighting() -> dict[str, Any]:
    return app("api/lighting")


def ha(args: argparse.Namespace, path: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {args.ha_token}"}
    return http(args.ha_url + path, headers, "POST" if body is not None else "GET", body)


def state(args: argparse.Namespace, entity_id: str) -> dict[str, Any]:
    return ha(args, f"/api/states/{entity_id}")


def write_state(args: argparse.Namespace, entity_id: str, value: str) -> None:
    """A state write (as a device or helper would report it), not a service call."""
    current = state(args, entity_id)
    ha(args, f"/api/states/{entity_id}", {"state": value, "attributes": current.get("attributes", {})})


def wait(what: str, condition, timeout: float = 60, interval: float = 1.0) -> Any:
    end = time.time() + timeout
    value = None
    while time.time() < end:
        try:
            value = condition()
        except Exception:  # noqa: BLE001 - the App may be restarting
            value = None
        if value:
            return value
        time.sleep(interval)
    check(f"waited for {what}", False, f"not reached within {timeout} s")
    raise SystemExit(1)


def pct(args: argparse.Namespace) -> int | None:
    s = state(args, LIGHT)
    b = s["attributes"].get("brightness")
    return round(b / 2.55) if s["state"] == "on" and b is not None else None


def set_mood(args: argparse.Namespace, mood: str, expect_pct: bool = True) -> None:
    write_state(args, MAIN, mood)
    if expect_pct:
        wait(f"{LIGHT} at {LEVELS[mood]} % ({mood})", lambda: pct(args) == LEVELS[mood], 30)


def light_calls(args: argparse.Namespace, since: int) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events[since:] if (e.get("domain") or "") in ("light", "switch")]


def integration(args: argparse.Namespace) -> dict[str, Any]:
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/lighting/bridge/status"})["result"]


def editor_direct(args: argparse.Namespace) -> dict[str, Any]:
    """The integration's own editor model (as its dashboard card reads it)."""
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/lighting/editor", "area_id": KAMAR})["result"]


def ui(args: argparse.Namespace, out: Path, name: str, action) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(f"localStorage.setItem('hassTokens', JSON.stringify({{access_token: '{args.ha_token}', "
                             "token_type: 'Bearer', expires_in: 1800, hassUrl: location.origin, clientId: location.origin + '/', "
                             "expires: Date.now() + 10 * 365 * 86400000, refresh_token: ''}));")
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{PANEL}")
        frame = app_frame(page)
        frame.evaluate("location.hash = '#/lighting/control'")
        expect(frame.locator("[data-testid=lighting-owner]")).to_be_visible(timeout=20000)
        page.screenshot(path=str(out / f"{name}-before.png"), full_page=True)
        action(frame)
        page.screenshot(path=str(out / f"{name}-after.png"), full_page=True)
        check(f"{name}: no horizontal overflow", no_horizontal_overflow(frame))
        for route in ("lighting", "moods", "switches"):
            frame.evaluate(f"location.hash = '#/{route}'")
            expect(frame.locator("[data-testid=lighting-banner]")).to_be_visible(timeout=20000)
            page.screenshot(path=str(out / f"{name}-{route}.png"), full_page=True)
            check(f"{name}: #/{route} shows who controls lighting, no overflow", no_horizontal_overflow(frame))
        check(f"{name}: no page error", not [e for e in errors if e != "Object"], errors[:3])
        browser.close()


def ui_handover(frame: Any) -> None:
    button = frame.locator("[data-testid=lighting-handover]")
    expect(button).to_be_enabled(timeout=20000)
    button.click()
    dialog = frame.locator("[data-testid=lighting-dialog]")
    expect(dialog).to_be_visible()
    confirm = dialog.locator("[data-testid=lighting-confirm]")
    check("handover needs the confirmation box", confirm.is_disabled())
    dialog.locator("[data-testid=lighting-understood]").check()
    confirm.click()
    expect(frame.locator("[data-testid=lighting-owner] .engine-owner")).to_contain_text("Smart Lighting App", timeout=30000)


def ui_release(frame: Any) -> None:
    frame.locator("[data-testid=lighting-release]").click()
    dialog = frame.locator("[data-testid=lighting-dialog]")
    expect(dialog).to_be_visible()
    dialog.locator("[data-testid=lighting-confirm]").click()
    expect(frame.locator("[data-testid=lighting-owner] .engine-owner")).to_contain_text("integration", timeout=30000)


def compare_round(args: argparse.Namespace) -> None:
    """Three mood changes, each a command on both sides: the round starts with a mood other than the current one."""
    moods = ["Kvøld", "Nátt", "Dagur"]
    current = state(args, MAIN)["state"]
    while moods[0] == current:
        moods.append(moods.pop(0))
    for mood in moods:
        set_mood(args, mood)
        time.sleep(4)


def phase_all(args: argparse.Namespace, out: Path) -> None:
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)
    own = integration(args)
    check("integration 0.8.0 controls lighting by default", own["owner"] == "integration" and own["integration_version"] == "0.8.0"
          and own["controller"] == "running", own["state"])
    data = wait("App in shadow mode", lambda: (d := lighting())["mode"] == "shadow" and d, 90)
    check("App: shadow, bridge connected, the integration acknowledged", data["bridge"] == "connected"
          and data["acknowledged_owner"] == "integration" and data["commands_go_to"] == "integration", data["bridge"])
    checks = {c["key"]: c["status"] for c in data["readiness"]["checks"]}
    check("App: same lighting code and occupancy engine", checks["fingerprint"] == "ok" and checks["core"] == "ok", checks)
    status, body = app_post_status("api/lighting/handover", {"confirm": True})
    check("handover refused while not ready", status == 409 and body.get("code") == "not_ready", (status, body))

    # Configuration through the App while the integration owns it: the integration's own commands, forwarded.
    write_state(args, PC, "on")
    status, body = command("room/activate", area_id=KAMAR, confirm=True)
    check("activate the room through the App (forwarded)", status == 200 and body["handled_by"] == "integration", body)
    for mood, level in LEVELS.items():
        status, body = command("profile/set", scope="light", area_id=KAMAR, mood=mood, target=LIGHT,
                               setting={"action": "on", "brightness_pct": level})
        check(f"profile {mood} through the App", status == 200, body)
    check("the integration has the change (one configuration)",
          editor_direct(args)["room"]["diagnostics"]["control_mode"] in ("active", "automatic"))
    set_mood(args, "Dagur")
    wait("configuration in sync", lambda: {c["key"]: c["status"] for c in lighting()["readiness"]["checks"]}["config"] == "ok", 30)

    # Shadow comparison on real events.
    compare_round(args)
    data = wait("readiness", lambda: (d := lighting())["readiness"]["ready"] and d, 120, 3)
    c = data["comparison"]["commands"]
    check("shadow parity on real events", c["matched"] >= 3 and c["mismatches"] == 0, c)
    check("decisions match", data["comparison"]["decisions"]["differences"] == 0, data["comparison"]["decision_differences"][:2])
    check("the App sent no lighting command in shadow mode", data["counters"].get("commands", 0) == 0, data["counters"])

    # Handover in the browser.
    before = len(args.watcher.events)
    ui(args, shots, "handover", ui_handover)
    own = integration(args)
    check("the integration acknowledges the App", own["owner"] == "app" and own["controller"] == "dormant"
          and own["history"][0]["reason"] == "handover", own["state"])
    check("no light was switched by the handover", not light_calls(args, before), light_calls(args, before)[:3])

    # The App controls the light through the integration's fenced executor.
    executed = own["executed"]
    set_mood(args, "Kvøld")
    set_mood(args, "Nátt")
    check("the App's commands ran through the integration", integration(args)["executed"] >= executed + 2,
          integration(args)["executed"])
    model = editor_direct(args)
    check("the integration's editor is read-only and says why", model["managed_by_app"] and not model["can_edit"])
    reply = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/lighting/settings/set", "manual_override_minutes": 5})
    check("the integration refuses lighting changes (managed by the App)", not reply["success"]
          and reply["error"]["code"] == "managed_by_app", reply)
    status, body = command("settings/set", manual_override_minutes=33)
    check("edits are handled by the App", status == 200 and body["handled_by"] == "app", body)
    wait("the change is mirrored to the integration",
         lambda: editor_direct(args)["settings"]["manual_override_minutes"] == 33, 30)

    # Crash the App: the integration takes lighting back by itself, switching nothing.
    set_mood(args, "Dagur")
    time.sleep(3)
    before = len(args.watcher.events)
    subprocess.run(["docker", "kill", "sl-test-app"], check=True, capture_output=True)
    crashed = time.time()
    own = wait("fallback after the lease", lambda: (o := integration(args))["owner"] == "integration" and o, 70)
    check("fallback within the lease (30 s) and a margin", time.time() - crashed < 50, round(time.time() - crashed))
    check("fallback reason: lease expired", own["history"][0]["reason"] == "lease_expired", own["history"][0])
    time.sleep(3)
    check("nothing was switched by the fallback", not light_calls(args, before), light_calls(args, before)[:3])
    check("the App's configuration is kept", editor_direct(args)["settings"]["manual_override_minutes"] == 33)
    set_mood(args, "Kvøld")
    check("the integration controls the light again", pct(args) == LEVELS["Kvøld"])
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    data = wait("restarted App in shadow mode", lambda: (d := lighting())["mode"] == "shadow" and d, 120, 2)
    time.sleep(5)
    check("the restarted App does not take control back", integration(args)["owner"] == "integration"
          and data["acknowledged_owner"] == "integration")

    # A second handover; a manual override; "Return control" in the browser keeps it and switches nothing.
    compare_round(args)
    wait("readiness again", lambda: lighting()["readiness"]["ready"], 120, 3)
    status, body = app_post_status("api/lighting/handover", {"confirm": True})
    check("second handover", status == 200 and integration(args)["owner"] == "app", (status, body))
    ha(args, "/api/services/light/turn_on", {"entity_id": LIGHT, "brightness_pct": 22})  # a person's change
    time.sleep(3)
    model = command("editor", area_id=KAMAR)[1]["result"]
    override = next((li.get("override") for li in model["room"]["lights"] if li["entity_id"] == LIGHT), None)
    check("the App records the person's change as a manual override", bool(override) and override["source"] == "ui",
          override)
    time.sleep(4)  # (the state is mirrored when it is saved)
    before = len(args.watcher.events)
    ui(args, shots, "release", ui_release)
    own = integration(args)
    check("Return control to the integration (browser)", own["owner"] == "integration"
          and own["history"][0]["reason"] == "released", own["history"][0])
    model = editor_direct(args)
    override = next((li.get("override") for li in model["room"]["lights"] if li["entity_id"] == LIGHT), None)
    check("the manual override survives the return", bool(override) and override["source"] == "ui", override)
    time.sleep(3)
    check("nothing was switched by the return", not light_calls(args, before), light_calls(args, before)[:3])
    check("editors writable again", editor_direct(args)["can_edit"])

    status, body = command("override/clear", area_id=KAMAR)
    check("override cleared through the App (forwarded)", status == 200, body)
    compare_round(args)
    wait("readiness for the third handover", lambda: lighting()["readiness"]["ready"], 120, 3)
    status, _ = app_post_status("api/lighting/handover", {"confirm": True})
    check("third handover (left in place for the restart check)", status == 200 and integration(args)["owner"] == "app")
    # (The App process was restarted during this phase: its command counters start from the restart.)
    executed = integration(args)["executed"]
    set_mood(args, "Nátt" if state(args, MAIN)["state"] != "Nátt" else "Kvøld")
    wait("the App commanded the light after the third handover", lambda: integration(args)["executed"] > executed, 30)
    safety = app("api/diagnostics")["safety"]
    commands = safety["commands_sent"]
    check("the App never called a service directly",
          not any(c.startswith(("call_service", "execute_script", "fire_event")) for c in commands), sorted(commands))
    check("the App's device control went only through the bridge", safety["device_control"] and
          safety["lighting_commands_sent"] >= 1, safety)


def phase_restart(args: argparse.Namespace, out: Path) -> None:
    own = wait("the integration back after the restart", lambda: integration(args), 120, 2)
    data = wait("the same App resumed", lambda: (d := lighting())["mode"] == "owner" and d, 150, 2)
    own = integration(args)
    check("Home Assistant restart: the same App process resumed", own["owner"] == "app"
          and own["history"][0]["reason"] == "resumed", own["history"][:2])
    check("the integration's controller stayed dormant", own["controller"] == "dormant")
    # Home Assistant may still be starting: the App re-reads the states once it runs (nothing commands lights
    # whose state it does not know).
    wait("the App sees Home Assistant running with its entities", lambda: (i := app("api/overview")["home_assistant"]
         ["info"]) and i["state"] == "RUNNING" and (i["entity_count"] or 0) > 10, 120, 2)
    mood = "Nátt" if state(args, MAIN)["state"] != "Nátt" else "Kvøld"  # (a change: the demo light reset at the restart)
    set_mood(args, mood)
    check("the resumed App controls the light", pct(args) == LEVELS[mood] and data["acknowledged_owner"] in ("app", "resume_pending"))


def phase_finish(args: argparse.Namespace, out: Path) -> None:
    before = len(args.watcher.events)
    ha(args, "/api/services/smart_occupancy/lighting_return_to_integration", {})
    own = wait("rollback by the administrator service", lambda: (o := integration(args))["owner"] == "integration" and o, 30)
    check("the service returned control", own["history"][0]["reason"] == "returned_by_service", own["history"][0])
    data = wait("App back in shadow mode", lambda: (d := lighting())["mode"] == "shadow" and d, 60, 2)
    check("App in shadow after the service", data["acknowledged_owner"] == "integration")
    time.sleep(3)
    check("nothing switched by the rollback", not [e for e in light_calls(args, before)
                                                  if e.get("service") != "lighting_return_to_integration"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--phase", default="all", choices=["all", "restart-check", "finish"])
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(15)
    {"all": phase_all, "restart-check": phase_restart, "finish": phase_finish}[args.phase](args, out)
    args.watcher.stop_flag = True
    (out / f"lighting-{args.phase}.json").write_text(json.dumps(results, indent=1))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
