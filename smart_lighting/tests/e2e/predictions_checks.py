"""End-to-end checks of Phase A5 (prediction migration) with a real Home Assistant and the real App image.

Needs the stack from stack.sh started with lowered handover thresholds
(``FORECAST_MIN_SHADOW_SECONDS=60 FORECAST_MIN_MATCHES=1``), a real Home
Assistant with Smart Occupancy & Lighting 0.9.0 and the Recorder, the A3.1 seed
(Kamar Norður with the template radar that follows ``input_boolean.nobara_pc``),
Docker and Playwright:

    python -I predictions_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check|finish]

Forecasts are observation only: the run fails on ANY service call of a light,
switch or cover during it.

``all``: the App's forecast runtime in shadow (the integration's learned data in
its copy, history read through the websocket API), a settings change through the
App (forwarded, mirrored), the comparison on real occupancy changes, a reviewed
handover in the browser (the learned data moves, verified), the integration's
forecast entities showing the App's published forecasts, the integration's
forecast editors read-only, an edit saved in the App, a crash of the App container
(the integration produces again from the App's learned data), the restarted App in
shadow, a second handover, "Return control" in the browser, and a third handover
left in place. ``restart-check``: the same App resumed. ``finish``: rollback with
the administrator service.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

from playwright.sync_api import expect, sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, no_horizontal_overflow, results  # noqa: E402
from rooms_checks import ha_ws, http  # noqa: E402

KAMAR = "kamar_nordur"
PC = "input_boolean.nobara_pc"
POST = {"X-Smart-Lighting": "1"}
FORECAST_SENSOR = "sensor.smart_occupancy_kamar_nordur_forecast"
# The learning sensor's id depends on when the integration was first installed (older installations keep the
# id without the device prefix); resolved from the states at run time.
LEARNING_SENSORS = ("sensor.smart_occupancy_lighting_forecast_learning", "sensor.smart_occupancy_forecast_learning")


def app(path: str, method: str = "GET", body: Any = None) -> Any:
    return http(INGRESS + path, POST if method == "POST" else None, method, body)


def app_post_status(path: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(INGRESS + path, method="POST", data=json.dumps(body).encode(),
                                 headers={**POST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def command(type_: str, **params: Any) -> tuple[int, Any]:
    return app_post_status("api/predictions/command", {"type": f"smart_occupancy/forecast/{type_}", **params})


def predictions() -> dict[str, Any]:
    return app("api/predictions")


def ha(args: argparse.Namespace, path: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {args.ha_token}"}
    return http(args.ha_url + path, headers, "POST" if body is not None else "GET", body)


def state(args: argparse.Namespace, entity_id: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        return ha(args, f"/api/states/{entity_id}")
    except urllib.error.HTTPError as err:
        if missing_ok and err.code == 404:
            return None
        raise


def write_state(args: argparse.Namespace, entity_id: str, value: str) -> None:
    current = state(args, entity_id)
    ha(args, f"/api/states/{entity_id}", {"state": value, "attributes": current.get("attributes", {})})


def wait(what: str, condition, timeout: float = 60, interval: float = 1.0) -> Any:
    end = time.time() + timeout
    value = None
    while time.time() < end:
        try:
            value = condition()
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return value
        time.sleep(interval)
    check(f"waited for {what}", False, f"not reached within {timeout} s")
    raise SystemExit(1)


def device_calls(args: argparse.Namespace, since: int) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events[since:] if (e.get("domain") or "") in ("light", "switch", "cover")]


def integration(args: argparse.Namespace) -> dict[str, Any]:
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/forecast/bridge/status"})["result"]


def room_direct(args: argparse.Namespace) -> dict[str, Any]:
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/forecast/room", "area_id": KAMAR})["result"]


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
        frame.evaluate("location.hash = '#/predictions/control'")
        expect(frame.locator("[data-testid=predictions-owner]")).to_be_visible(timeout=20000)
        page.screenshot(path=str(out / f"{name}-before.png"), full_page=True)
        action(frame)
        page.screenshot(path=str(out / f"{name}-after.png"), full_page=True)
        check(f"{name}: no horizontal overflow", no_horizontal_overflow(frame))
        for route in ("predictions", "predictions/patterns", "predictions/accuracy", "predictions/settings",
                      f"predictions/rooms/{KAMAR}"):
            frame.evaluate(f"location.hash = '#/{route}'")
            expect(frame.locator("[data-testid=predictions-banner]")).to_be_visible(timeout=20000)
            page.screenshot(path=str(out / f"{name}-{route.replace('/', '-')}.png"), full_page=True)
            check(f"{name}: #/{route} shows who produces the forecasts, no overflow", no_horizontal_overflow(frame))
        check(f"{name}: no page error", not [e for e in errors if e != "Object"], errors[:3])
        browser.close()


def ui_handover(frame: Any) -> None:
    button = frame.locator("[data-testid=predictions-handover]")
    expect(button).to_be_enabled(timeout=20000)
    button.click()
    dialog = frame.locator("[data-testid=predictions-dialog]")
    expect(dialog).to_be_visible()
    confirm = dialog.locator("[data-testid=predictions-confirm]")
    check("handover needs the confirmation box", confirm.is_disabled())
    dialog.locator("[data-testid=predictions-understood]").check()
    confirm.click()
    expect(frame.locator("[data-testid=predictions-owner]")).to_contain_text("Smart Comfort", timeout=60000)


def ui_release(frame: Any) -> None:
    frame.locator("[data-testid=predictions-release]").click()
    dialog = frame.locator("[data-testid=predictions-dialog]")
    expect(dialog).to_be_visible()
    dialog.locator("[data-testid=predictions-confirm]").click()
    expect(frame.locator("[data-testid=predictions-owner]")).to_contain_text("integration", timeout=60000)


def compare_round(args: argparse.Namespace) -> None:
    """Occupancy changes: both sides recompute their forecasts and the comparison pairs the views."""
    for value in ("on", "off", "on"):
        write_state(args, PC, value)
        time.sleep(8)


def phase_all(args: argparse.Namespace, out: Path) -> None:
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)
    own = integration(args)
    check("integration 0.9.0 produces the forecasts by default", own["owner"] == "integration"
          and own["integration_version"] == "0.9.0" and own["manager"] == "running", own["state"])
    data = wait("App in shadow mode", lambda: (d := predictions())["mode"] == "shadow" and d, 120)
    check("App: shadow, bridge connected, the integration acknowledged", data["bridge"] == "connected"
          and data["acknowledged_owner"] == "integration" and data["commands_go_to"] == "integration", data["bridge"])
    checks = {c["key"]: c["status"] for c in data["readiness"]["checks"]}
    check("App: same forecast code", checks["fingerprint"] == "ok", checks)
    check("the App's copy holds the integration's learned days", data["days_learned"] == own["days_learned"]
          and data["learned_revision"] == own["learned_revision"], (data["days_learned"], own["days_learned"]))
    # (Before any long wait: with the fast test thresholds the shadow is ready after a minute.)
    status, body = app_post_status("api/predictions/handover", {"confirm": True})
    check("handover refused while not ready", status == 409 and body.get("code") == "not_ready", (status, body))
    wait("the App read history through the API", lambda: predictions()["history_calls"] >= 1, 180, 5)

    # A settings change through the App while the integration owns it: forwarded, mirrored on the shadow.
    status, body = command("settings", area_id=None, values={"probability_threshold": 55})
    check("global forecast settings through the App (forwarded)", status == 200 and body["handled_by"] == "integration", body)
    wait("settings in sync", lambda: {c["key"]: c["status"] for c in predictions()["readiness"]["checks"]}["settings"] == "ok", 30)
    check("the integration has the change", room_direct(args)["settings"]["global"]["probability_threshold"] == 55)
    house = predictions()["house"]
    check("house overview from the App", house and house["source"] == "integration" and any(
        r["area_id"] == KAMAR for r in house["rooms"]), house and [r["area_id"] for r in house["rooms"]])
    room = app(f"api/predictions/rooms/{KAMAR}")
    check("room detail with patterns and accuracy", "patterns" in room and "accuracy" in room and room["source"] == "integration")

    # Comparison on real occupancy changes.
    compare_round(args)
    data = wait("readiness", lambda: (d := predictions())["readiness"]["ready"] and d, 180, 3)
    c = data["comparison"]
    check("the predictions agreed", c["agreements"] >= 1, c)
    check("no bridge publish from the shadow", data["counters"].get("views_published", 0) == 0, data["counters"])

    # Handover in the browser: the learned data moves.
    before = len(args.watcher.events)
    ui(args, shots, "handover", ui_handover)
    own = integration(args)
    check("the integration acknowledges the App", own["owner"] == "app" and own["manager"] == "dormant"
          and own["history"][0]["reason"] == "handover", own["state"])
    data = predictions()
    check("the learned data was transferred and verified", data["last_copy"] and data["last_copy"]["identical"],
          data["last_copy"])
    check("no device command because of the handover", not device_calls(args, before), device_calls(args, before)[:3])
    wait("the App published its view", lambda: integration(args)["published"] >= 1, 60)
    learning = next(s for eid in LEARNING_SENSORS if (s := state(args, eid, missing_ok=True)) is not None)
    check("the forecast learning sensor shows the App's status", learning["state"] in ("ready", "learning", "waiting"),
          learning["state"])
    reply = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/forecast/settings", "area_id": None,
                                               "values": {"probability_threshold": 60}})
    check("the integration refuses forecast changes (managed by the App)", not reply["success"]
          and reply["error"]["code"] == "managed_by_app", reply)
    check("the integration's room view still answers (read-only)", room_direct(args)["area_id"] == KAMAR)
    status, body = command("settings", area_id=None, values={"probability_threshold": 65})
    check("edits are handled by the App", status == 200 and body["handled_by"] == "app", body)
    wait("the App's view reaches the integration",
         lambda: room_direct(args)["settings"]["global"]["probability_threshold"] == 65, 60)
    compare_round(args)
    check("the forecast sensor exists and follows the App", state(args, FORECAST_SENSOR)["entity_id"] == FORECAST_SENSOR)

    # Crash the App: the integration produces again, from the App's learned data.
    before = len(args.watcher.events)
    days_app = predictions()["days_learned"]
    subprocess.run(["docker", "kill", "sl-test-app"], check=True, capture_output=True)
    crashed = time.time()
    own = wait("fallback after the lease", lambda: (o := integration(args))["owner"] == "integration" and o, 70)
    check("fallback within the lease (30 s) and a margin", time.time() - crashed < 50, round(time.time() - crashed))
    check("fallback reason: lease expired", own["history"][0]["reason"] == "lease_expired", own["history"][0])
    check("the integration kept every learned day", own["days_learned"] >= days_app, (own["days_learned"], days_app))
    check("the App's settings are kept", room_direct(args)["settings"]["global"]["probability_threshold"] == 65)
    time.sleep(3)
    check("no device command because of the fallback", not device_calls(args, before), device_calls(args, before)[:3])
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    data = wait("restarted App in shadow mode", lambda: (d := predictions())["mode"] == "shadow" and d, 180, 2)
    time.sleep(5)
    check("the restarted App does not take control back", integration(args)["owner"] == "integration"
          and data["acknowledged_owner"] == "integration")

    # A second handover and "Return control" in the browser.
    compare_round(args)
    wait("readiness again", lambda: predictions()["readiness"]["ready"], 180, 3)
    status, body = app_post_status("api/predictions/handover", {"confirm": True})
    check("second handover", status == 200 and integration(args)["owner"] == "app", (status, body))
    before = len(args.watcher.events)
    ui(args, shots, "release", ui_release)
    own = integration(args)
    check("Return control to the integration (browser)", own["owner"] == "integration"
          and own["history"][0]["reason"] == "released", own["history"][0])
    time.sleep(3)
    check("no device command because of the return", not device_calls(args, before), device_calls(args, before)[:3])
    compare_round(args)
    wait("readiness for the third handover", lambda: predictions()["readiness"]["ready"], 180, 3)
    status, _ = app_post_status("api/predictions/handover", {"confirm": True})
    check("third handover (left in place for the restart check)", status == 200 and integration(args)["owner"] == "app")
    safety = app("api/diagnostics")["safety"]
    commands = safety["commands_sent"]
    check("the App never called a service directly",
          not any(c.startswith(("call_service", "execute_script", "fire_event")) for c in commands), sorted(commands))
    check("forecasts never command anything", not any("forecast/bridge/execute" in c for c in commands)
          and safety["forecast_owner"] == "app", safety["forecast_owner"])
    check("no device command during the whole run", not device_calls(args, 0), device_calls(args, 0)[:3])


def phase_restart(args: argparse.Namespace, out: Path) -> None:
    wait("the integration back after the restart", lambda: integration(args), 120, 2)
    data = wait("the same App resumed", lambda: (d := predictions())["mode"] == "owner" and d, 150, 2)
    own = integration(args)
    check("Home Assistant restart: the same App process resumed", own["owner"] == "app"
          and own["history"][0]["reason"] == "resumed", own["history"][:2])
    check("the integration's manager stayed dormant", own["manager"] == "dormant")
    published = own["published"]
    compare_round(args)
    wait("the resumed App publishes", lambda: integration(args)["published"] > published, 60)
    check("no device command during the restart check", not device_calls(args, 0), device_calls(args, 0)[:3])


def phase_finish(args: argparse.Namespace, out: Path) -> None:
    before = len(args.watcher.events)
    ha(args, "/api/services/smart_occupancy/forecast_return_to_integration", {})
    own = wait("rollback by the administrator service", lambda: (o := integration(args))["owner"] == "integration" and o, 30)
    check("the service returned control", own["history"][0]["reason"] == "returned_by_service", own["history"][0])
    data = wait("App back in shadow mode", lambda: (d := predictions())["mode"] == "shadow" and d, 60, 2)
    check("App in shadow after the service", data["acknowledged_owner"] == "integration")
    time.sleep(3)
    check("no device command by the rollback", not device_calls(args, before))


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
    (out / f"predictions-{args.phase}.json").write_text(json.dumps(results, indent=1))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
