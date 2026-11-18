"""End-to-end checks of Phase A5 (curtain migration) with a real Home Assistant and the real App image.

Needs the stack from stack.sh started with lowered handover thresholds
(``CURTAINS_MIN_SHADOW_SECONDS=60 CURTAINS_MIN_DECISIONS=1``), a real Home
Assistant with Smart Occupancy & Lighting 0.9.0, its curtain configuration reset
(no ``.storage/smart_occupancy.curtains*`` files), the demo platform (the covers
``cover.hall_window``, which supports positions and travels for a few seconds,
and ``cover.kitchen_window``, open / close only), the A3.1 seed, Docker (to crash and restart
the App container) and Playwright:

    python -I curtains_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        [--phase all|restart-check|finish]

Every cover service call seen during the run is a curtain command (nothing else
moves covers here); around every change of control the run checks that none
happened.

``all``: a window created and configured through the App while the integration
controls the curtains (forwarded commands), a confirmed preset applied through
the App (the integration moves the cover; the shadow mirrors the operation and
the comparison matches the command), readiness, a reviewed handover in the
browser, the App moving the cover only through the integration's fenced executor
(with its own context: no manual change recorded), the integration's editors
read-only, edits saved in the App and mirrored, a crash of the App container (the
integration takes the curtains back without moving anything; manual hold kept),
the restarted App staying in shadow, a second handover, a manual move by hand
(hold), "Return control" in the browser (hold kept, nothing moved), a handover
refused while a cover is moving, and a third handover left in place.
``restart-check`` (after Home Assistant restarted, the App kept running): the
same App resumed and still controls the covers. ``finish``: rollback with the
administrator service.
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

STOVA = "stova"
COVER = "cover.hall_window"  # supports positions and travels for a few seconds
SECOND = "cover.kitchen_window"  # open / close only
POST = {"X-Smart-Lighting": "1"}


def app(path: str, method: str = "GET", body: Any = None) -> Any:
    return http(INGRESS + path, POST if method == "POST" else None, method, body)


def app_post_status(path: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(INGRESS + path, method="POST", data=json.dumps(body).encode(),
                                 headers={**POST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def command(type_: str, **params: Any) -> tuple[int, Any]:
    return app_post_status("api/curtains/command", {"type": f"smart_occupancy/curtains/{type_}", **params})


def curtains() -> dict[str, Any]:
    return app("api/curtains")


def ha(args: argparse.Namespace, path: str, body: Any = None) -> Any:
    headers = {"Authorization": f"Bearer {args.ha_token}"}
    return http(args.ha_url + path, headers, "POST" if body is not None else "GET", body)


def state(args: argparse.Namespace, entity_id: str) -> dict[str, Any]:
    return ha(args, f"/api/states/{entity_id}")


def position(args: argparse.Namespace, entity_id: str = COVER) -> int | None:
    s = state(args, entity_id)
    pos = s["attributes"].get("current_position")
    return int(pos) if pos is not None else None


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


def cover_calls(args: argparse.Namespace, since: int) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events[since:] if (e.get("domain") or "") == "cover"]


def integration(args: argparse.Namespace) -> dict[str, Any]:
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/curtains/bridge/status"})["result"]


def model_direct(args: argparse.Namespace) -> dict[str, Any]:
    """The integration's own curtain model (as its dashboard card reads it)."""
    return ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/curtains/model"})["result"]


def the_window(model: dict[str, Any], name: str = "Stova window") -> dict[str, Any] | None:
    return next((w for w in model["windows"] if w["name"] == name), None)


def preset_of(window: dict[str, Any], name: str) -> dict[str, Any]:
    return next(p for p in window["presets"] if p["name"] == name)


def apply_preset(args: argparse.Namespace, window: dict[str, Any], name: str, *, via_app: bool = True) -> tuple[int, Any]:
    preset = preset_of(window, name)
    if via_app:
        status, body = command("preview", window_id=window["window_id"], preset_id=preset["preset_id"])
        check(f"preview {name} answers (nothing moves)", status == 200, body)
        return command("apply", window_id=window["window_id"], preset_id=preset["preset_id"], confirm=True,
                       expected=preset["positions"])
    reply = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/curtains/apply", "window_id": window["window_id"],
                                               "preset_id": preset["preset_id"], "confirm": True,
                                               "expected": preset["positions"]})
    return (200 if reply["success"] else 409), reply


def ensure_cover_area(args: argparse.Namespace) -> None:
    """The demo covers belong to devices; an entity without a name of its own takes its area from its device."""
    for entity_id in (COVER, SECOND):
        entry = ha_ws(args.ha_url, args.ha_token, {"type": "config/entity_registry/get", "entity_id": entity_id})
        device_id = (entry.get("result") or {}).get("device_id") if entry.get("success") else None
        if device_id:
            reply = ha_ws(args.ha_url, args.ha_token, {"type": "config/device_registry/update", "device_id": device_id,
                                                       "area_id": STOVA})
        else:
            reply = ha_ws(args.ha_url, args.ha_token, {"type": "config/entity_registry/update", "entity_id": entity_id,
                                                       "area_id": STOVA})
        check(f"{entity_id} placed in Stova", reply["success"], reply)


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
        frame.evaluate("location.hash = '#/curtains/control'")
        expect(frame.locator("[data-testid=curtains-owner]")).to_be_visible(timeout=20000)
        page.screenshot(path=str(out / f"{name}-before.png"), full_page=True)
        action(frame)
        page.screenshot(path=str(out / f"{name}-after.png"), full_page=True)
        check(f"{name}: no horizontal overflow", no_horizontal_overflow(frame))
        for route in ("curtains", "curtains/windows", "curtains/modes", "curtains/schedule", "curtains/environment",
                      "curtains/diagnostics"):
            frame.evaluate(f"location.hash = '#/{route}'")
            expect(frame.locator("[data-testid=curtains-banner]")).to_be_visible(timeout=20000)
            page.screenshot(path=str(out / f"{name}-{route.replace('/', '-')}.png"), full_page=True)
            check(f"{name}: #/{route} shows who controls the curtains, no overflow", no_horizontal_overflow(frame))
        check(f"{name}: no page error", not [e for e in errors if e != "Object"], errors[:3])
        browser.close()


def ui_handover(frame: Any) -> None:
    button = frame.locator("[data-testid=curtains-handover]")
    expect(button).to_be_enabled(timeout=20000)
    button.click()
    dialog = frame.locator("[data-testid=curtains-dialog]")
    expect(dialog).to_be_visible()
    confirm = dialog.locator("[data-testid=curtains-confirm]")
    check("handover needs the confirmation box", confirm.is_disabled())
    dialog.locator("[data-testid=curtains-understood]").check()
    confirm.click()
    expect(frame.locator("[data-testid=curtains-owner]")).to_contain_text("Smart Lighting App", timeout=30000)


def ui_release(frame: Any) -> None:
    frame.locator("[data-testid=curtains-release]").click()
    dialog = frame.locator("[data-testid=curtains-dialog]")
    expect(dialog).to_be_visible()
    dialog.locator("[data-testid=curtains-confirm]").click()
    expect(frame.locator("[data-testid=curtains-owner]")).to_contain_text("integration", timeout=30000)


def settle(args: argparse.Namespace, entity_id: str = COVER, timeout: float = 40) -> None:
    wait(f"{entity_id} at rest", lambda: state(args, entity_id)["state"] in ("open", "closed"), timeout)


def compare_round(args: argparse.Namespace, window: dict[str, Any]) -> None:
    """Two confirmed presets through the App: the integration moves the cover, the shadow mirrors the operation."""
    for name in ("Fully closed", "Fully open"):
        status, body = apply_preset(args, window, name)
        check(f"apply {name} through the App (forwarded)", status == 200 and body["handled_by"] == "integration", body)
        settle(args)
        time.sleep(3)


def phase_all(args: argparse.Namespace, out: Path) -> None:
    shots = out / "screenshots"
    shots.mkdir(exist_ok=True)
    own = integration(args)
    check("integration 0.9.0 controls the curtains by default", own["owner"] == "integration"
          and own["integration_version"] == "0.9.0" and own["manager"] == "running", own["state"])
    data = wait("App in shadow mode", lambda: (d := curtains())["mode"] == "shadow" and d, 90)
    check("App: shadow, bridge connected, the integration acknowledged", data["bridge"] == "connected"
          and data["acknowledged_owner"] == "integration" and data["commands_go_to"] == "integration", data["bridge"])
    checks = {c["key"]: c["status"] for c in data["readiness"]["checks"]}
    check("App: same curtain code and occupancy engine", checks["fingerprint"] == "ok" and checks["core"] == "ok", checks)
    status, body = app_post_status("api/curtains/handover", {"confirm": True})
    check("handover refused while not ready", status == 409 and body.get("code") == "not_ready", (status, body))

    # Configuration through the App while the integration owns it: the integration's own commands, forwarded.
    ensure_cover_area(args)
    status, body = command("model")
    check("the curtain model through the App", status == 200 and body["handled_by"] == "integration", body)
    if the_window(body["result"]) is None:
        status, body = command("window/create", name="Stova window", area_id=STOVA, direction="S",
                               layers=[{"entity_id": COVER, "kind": "blackout"}])
        check("create a window through the App (forwarded)", status == 200 and body["handled_by"] == "integration", body)
    model = command("model")[1]["result"]
    window = the_window(model)
    check("the integration has the window (one configuration)", the_window(model_direct(args)) is not None)
    check("the new window is Observing and has presets", window["mode"] == "observing" and len(window["presets"]) >= 3)
    wait("configuration in sync", lambda: {c["key"]: c["status"] for c in curtains()["readiness"]["checks"]}["config"] == "ok", 30)
    status, body = command("auto/settings", scope="window", window_id=window["window_id"], values={"min_interval": 1})
    check("window settings through the App", status == 200, body)

    # Shadow comparison on a real cover: the confirmed presets are forwarded; the shadow mirrors them.
    compare_round(args, window)
    data = wait("readiness", lambda: (d := curtains())["readiness"]["ready"] and d, 120, 3)
    c = data["comparison"]["commands"]
    check("shadow parity: the cover commands matched", c["matched"] >= 2 and c["mismatches"] == 0, c)
    check("the App sent no cover command in shadow mode", data["counters"].get("commands", 0) == 0, data["counters"])
    check("no cover is moving", not data["moving"], data["moving"])

    # Handover in the browser (the integration refuses while a cover moves: proven below).
    before = len(args.watcher.events)
    ui(args, shots, "handover", ui_handover)
    own = integration(args)
    check("the integration acknowledges the App", own["owner"] == "app" and own["manager"] == "dormant"
          and own["history"][0]["reason"] == "handover", own["state"])
    check("no cover moved because of the handover", not cover_calls(args, before), cover_calls(args, before)[:3])

    # The App moves the cover through the integration's fenced executor, with its own context.
    executed = own["executed"]
    status, body = apply_preset(args, window, "Fully closed")
    check("a preset applied by the App (handled by the App)", status == 200 and body["handled_by"] == "app", body)
    wait("the App's command ran through the integration", lambda: integration(args)["executed"] >= executed + 1, 30)
    settle(args)
    check("the cover reached the preset", position(args) == 0, position(args))
    model = command("model")[1]["result"]
    window = the_window(model)
    check("the App's own command is not a manual change", window["manual"] is None and window["last"]["status"] == "succeeded",
          (window["manual"], window["last"]["status"]))
    legacy = model_direct(args)
    check("the integration's model says it is managed by the App", not legacy["can_edit"] or integration(args)["owner"] == "app")
    reply = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/curtains/settings", "tolerance": 5})
    check("the integration refuses curtain changes (managed by the App)", not reply["success"]
          and reply["error"]["code"] == "managed_by_app", reply)
    status, body = command("settings", tolerance=4)
    check("edits are handled by the App", status == 200 and body["handled_by"] == "app", body)
    wait("the change is mirrored to the integration", lambda: model_direct(args)["settings"]["tolerance"] == 4, 30)

    # A manual move by hand while the App owns: a hold in the App's state (survives every change of control).
    time.sleep(95)  # late reports within 90 s of an operation are still "ours": a manual move must come later
    ha(args, "/api/services/cover/set_cover_position", {"entity_id": COVER, "position": 60})
    settle(args)
    time.sleep(4)
    window = the_window(command("model")[1]["result"])
    check("the App records the person's move as a manual change", window["manual"] is not None
          and window["manual"]["to"] == 60, window["manual"])

    # Crash the App: the integration takes the curtains back by itself, moving nothing.
    before = len(args.watcher.events)
    subprocess.run(["docker", "kill", "sl-test-app"], check=True, capture_output=True)
    crashed = time.time()
    own = wait("fallback after the lease", lambda: (o := integration(args))["owner"] == "integration" and o, 70)
    check("fallback within the lease (30 s) and a margin", time.time() - crashed < 50, round(time.time() - crashed))
    check("fallback reason: lease expired", own["history"][0]["reason"] == "lease_expired", own["history"][0])
    time.sleep(3)
    check("no cover moved because of the fallback", not cover_calls(args, before), cover_calls(args, before)[:3])
    legacy = model_direct(args)
    check("the App's configuration is kept", legacy["settings"]["tolerance"] == 4)
    check("the manual change survives the fallback", the_window(legacy)["manual"] is not None
          and the_window(legacy)["manual"]["to"] == 60, the_window(legacy)["manual"])
    status, reply = apply_preset(args, the_window(legacy), "Fully open", via_app=False)
    check("the integration controls the cover again", status == 200, reply)
    settle(args)
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    data = wait("restarted App in shadow mode", lambda: (d := curtains())["mode"] == "shadow" and d, 120, 2)
    time.sleep(5)
    check("the restarted App does not take control back", integration(args)["owner"] == "integration"
          and data["acknowledged_owner"] == "integration")

    # A second handover; a handover is refused while a cover moves; "Return control" keeps the hold.
    window = the_window(command("model")[1]["result"])
    compare_round(args, window)
    wait("readiness again", lambda: curtains()["readiness"]["ready"], 120, 3)
    status, reply = apply_preset(args, window, "Fully closed", via_app=False)
    check("the integration starts a movement", status == 200, reply)
    time.sleep(1)
    status, body = app_post_status("api/curtains/handover", {"confirm": True})
    moving_refused = status == 409
    settle(args)
    check("a handover is refused while a cover is moving (or the move finished first)", moving_refused
          or integration(args)["owner"] == "app", (status, body))
    if not moving_refused:
        ui(args, shots, "release-early", ui_release)
    time.sleep(3)
    wait("readiness after the movement", lambda: curtains()["readiness"]["ready"], 120, 3)
    status, body = app_post_status("api/curtains/handover", {"confirm": True})
    check("second handover", status == 200 and integration(args)["owner"] == "app", (status, body))
    time.sleep(95)  # (the settle window after the last operation)
    ha(args, "/api/services/cover/set_cover_position", {"entity_id": COVER, "position": 35})  # a person's move
    settle(args)
    time.sleep(4)
    window = the_window(command("model")[1]["result"])
    check("the App records the manual move", window["manual"] is not None and window["manual"]["to"] == 35,
          window["manual"])
    before = len(args.watcher.events)
    ui(args, shots, "release", ui_release)
    own = integration(args)
    check("Return control to the integration (browser)", own["owner"] == "integration"
          and own["history"][0]["reason"] == "released", own["history"][0])
    legacy = model_direct(args)
    check("the manual change survives the return", the_window(legacy)["manual"] is not None
          and the_window(legacy)["manual"]["to"] == 35, the_window(legacy)["manual"])
    time.sleep(3)
    check("no cover moved because of the return", not cover_calls(args, before), cover_calls(args, before)[:3])
    check("editors writable again", legacy["can_edit"])

    status, body = command("manual/clear", window_id=window["window_id"])
    check("manual change cleared through the App (forwarded)", status == 200, body)
    compare_round(args, the_window(command("model")[1]["result"]))
    wait("readiness for the third handover", lambda: curtains()["readiness"]["ready"], 120, 3)
    status, _ = app_post_status("api/curtains/handover", {"confirm": True})
    check("third handover (left in place for the restart check)", status == 200 and integration(args)["owner"] == "app")
    executed = integration(args)["executed"]
    window = the_window(command("model")[1]["result"])
    status, body = apply_preset(args, window, "Fully closed" if position(args) != 0 else "Fully open")
    wait("the App commanded the cover after the third handover", lambda: integration(args)["executed"] > executed, 30)
    settle(args)
    safety = app("api/diagnostics")["safety"]
    commands = safety["commands_sent"]
    check("the App never called a service directly",
          not any(c.startswith(("call_service", "execute_script", "fire_event")) for c in commands), sorted(commands))
    check("the App's cover control went only through the bridge", safety["curtains_owner"] == "app" and
          safety["curtain_commands_sent"] >= 1, safety)


def phase_restart(args: argparse.Namespace, out: Path) -> None:
    wait("the integration back after the restart", lambda: integration(args), 120, 2)
    data = wait("the same App resumed", lambda: (d := curtains())["mode"] == "owner" and d, 150, 2)
    own = integration(args)
    check("Home Assistant restart: the same App process resumed", own["owner"] == "app"
          and own["history"][0]["reason"] == "resumed", own["history"][:2])
    check("the integration's manager stayed dormant", own["manager"] == "dormant")
    wait("the App sees Home Assistant running with its entities", lambda: (i := app("api/overview")["home_assistant"]
         ["info"]) and i["state"] == "RUNNING" and (i["entity_count"] or 0) > 10, 120, 2)
    before = len(args.watcher.events)
    time.sleep(5)
    check("nothing moved because of the restart", not cover_calls(args, before), cover_calls(args, before)[:3])
    model = command("model")[1]["result"]
    window = the_window(model)
    executed = own["executed"]
    status, body = apply_preset(args, window, "Fully open" if position(args) != 100 else "Fully closed")
    check("the resumed App controls the cover", status == 200 and body["handled_by"] == "app"
          and data["acknowledged_owner"] in ("app", "resume_pending"), body)
    wait("through the integration's executor", lambda: integration(args)["executed"] > executed, 30)
    settle(args)


def phase_finish(args: argparse.Namespace, out: Path) -> None:
    before = len(args.watcher.events)
    ha(args, "/api/services/smart_occupancy/curtains_return_to_integration", {})
    own = wait("rollback by the administrator service", lambda: (o := integration(args))["owner"] == "integration" and o, 30)
    check("the service returned control", own["history"][0]["reason"] == "returned_by_service", own["history"][0])
    data = wait("App back in shadow mode", lambda: (d := curtains())["mode"] == "shadow" and d, 60, 2)
    check("App in shadow after the service", data["acknowledged_owner"] == "integration")
    time.sleep(3)
    check("nothing moved by the rollback", not [e for e in cover_calls(args, before)])


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
    (out / f"curtains-{args.phase}.json").write_text(json.dumps(results, indent=1))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
