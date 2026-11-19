"""End-to-end checks of Phase A6 (the App becomes the product, the integration the companion) with a real Home
Assistant and the real App image.

    python -I system_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        --phase legacy|activate|failure|restart-check|fresh [--ha-config <dir>]

Phases (run by the driver in this order, with the Home Assistant changes between them):

``legacy``   Home Assistant with the monolithic integration 0.9.0, the App 0.7.0 with lowered handover
             thresholds: the wizard's steps 1–5 (inspect, backup, validate, transfer config as a staged copy,
             transfer control in the safe order), Activate refused without the companion, the entity ids recorded.
``activate`` After the integration was replaced by the companion 1.0.0 and Home Assistant restarted (the App kept
             running): the entities kept their ids, nothing was switched, Activate (pair, promote, claim), Verify,
             the App's light commands through the companion's executor, the compact cards served, a card action
             relayed, Retire (legacy stores gone, backup store present, the App's backups kept).
``failure``  The App container is killed while a room is occupied: the entities keep their state (not fresh), no
             service call, no vacancy; the App restarts and claims again with a new epoch; a revoke from Home
             Assistant stops the App at once and it reclaims; a stale command never reaches a device.
``restart-check`` After a Home Assistant restart: the records came back as "away", the App claimed again
             (new epochs), the entities are fresh again, nothing was switched.
``reinstall`` Companion 1.0.0 with the complete 0.9.0 data and an App with empty storage: Transfer config imports
             the options and the engine stores, Activate claims, a light follows through the companion.
``fresh``    A new Home Assistant (companion added, no legacy data) and an App with empty storage: Setup → create
             the configuration, pair, import a room, the entities appear, the App claims, a light is commanded
             through the companion.

People are moved by writing the helper's state (``POST /api/states``), never by a service call; every
``call_service`` event seen while nothing should be switched fails the run.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, results  # noqa: E402
from rooms_checks import ha_ws, http  # noqa: E402

KAMAR = "kamar_nordur"
LIGHT = "light.bed_light"
PC = "input_boolean.nobara_pc"
MAIN = "input_select.main_mood"
POST = {"X-Smart-Lighting": "1"}
STATE_FILE = "system_e2e_state.json"
DOMAINS = ("occupancy", "lighting", "curtains", "forecast")
LEVELS = {"Dagur": 80, "Kvøld": 45, "Nátt": 10}


# ------------------------------------------------------------------ helpers
def app(path: str, method: str = "GET", body: Any = None) -> Any:
    return http(INGRESS + path, POST if method == "POST" else None, method, body)


def app_post(path: str, body: Any) -> tuple[int, Any]:
    req = urllib.request.Request(INGRESS + path, method="POST", data=json.dumps(body).encode(),
                                 headers={**POST, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"{}")


def step(name: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    return app_post(f"api/migration/{name}", body or {})


def command(type_: str, **params: Any) -> tuple[int, Any]:
    return app_post("api/lighting/command", {"type": f"smart_occupancy/lighting/{type_}", **params})


def ha(args: argparse.Namespace, path: str, body: Any = None, method: str | None = None) -> Any:
    headers = {"Authorization": f"Bearer {args.ha_token}"}
    return http(args.ha_url + path, headers, method or ("POST" if body is not None else "GET"), body)


def state(args: argparse.Namespace, entity_id: str) -> dict[str, Any] | None:
    try:
        return ha(args, f"/api/states/{entity_id}")
    except urllib.error.HTTPError:
        return None


def write_state(args: argparse.Namespace, entity_id: str, value: str) -> None:
    current = state(args, entity_id) or {}
    ha(args, f"/api/states/{entity_id}", {"state": value, "attributes": current.get("attributes", {})})


def companion(args: argparse.Namespace) -> dict[str, Any] | None:
    r = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/status"})
    return r["result"] if r.get("success") else None


def record(args: argparse.Namespace, domain: str) -> dict[str, Any]:
    status = companion(args)
    assert status is not None, "the companion is not loaded"
    return status["ownership"][domain]


def our_entities(args: argparse.Namespace) -> dict[str, dict[str, Any]]:
    states = ha(args, "/api/states")
    return {s["entity_id"]: s for s in states if s["entity_id"].split(".", 1)[1].startswith("smart_occupancy_")
            or s["entity_id"] == "select.smart_lighting_mood"}


def wait(what: str, condition, timeout: float = 60, interval: float = 1.0) -> Any:
    end = time.time() + timeout
    value = None
    while time.time() < end:
        try:
            value = condition()
        except Exception:  # noqa: BLE001 - the App or Home Assistant may be restarting
            value = None
        if value:
            return value
        time.sleep(interval)
    check(f"waited for {what}", False, f"not reached within {timeout} s; readiness: {LAST_READINESS}")
    raise SystemExit(1)


def light_calls(args: argparse.Namespace, since: int) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events[since:] if e.get("domain") in ("light", "cover", "switch")]


def pct(args: argparse.Namespace) -> int | None:
    s = state(args, LIGHT) or {}
    b = (s.get("attributes") or {}).get("brightness")
    return round(b / 2.55) if s.get("state") == "on" and b is not None else None


def save(out: Path, data: dict[str, Any]) -> None:
    (out / STATE_FILE).write_text(json.dumps(data, indent=1))


def load(out: Path) -> dict[str, Any]:
    path = out / STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


def screenshot(args: argparse.Namespace, out: Path, name: str, route: str) -> None:
    if not args.chromium:
        return
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(PANEL)
        frame = app_frame(page)
        frame.goto(INGRESS + f"#/{route}")
        frame.wait_for_selector("[data-testid=system-hero], [data-testid=setup-progress], [data-testid=card-house]",
                                timeout=20000)
        time.sleep(1.5)
        page.screenshot(path=str(out / "screenshots" / f"{name}.png"), full_page=True)
        browser.close()


def diagnose(args: argparse.Namespace, out: Path) -> None:
    """What the App and the companion think when a light did not follow: written next to the results."""
    dump: dict[str, Any] = {}
    for name, fetch in (("lighting", lambda: app("api/lighting")), ("engine", lambda: app("api/engine")),
                        ("system", lambda: app("api/system")), ("dashboard", lambda: app("api/dashboard")),
                        ("editor", lambda: app_post("api/lighting/command", {"type": "smart_occupancy/lighting/editor", "area_id": KAMAR})),
                        ("companion", lambda: companion(args)), ("occupancy_entity", lambda: state(args, f"sensor.smart_occupancy_{KAMAR}_state")),
                        ("light", lambda: state(args, LIGHT)), ("mood", lambda: state(args, MAIN))):
        try:
            dump[name] = fetch()
        except Exception as err:  # noqa: BLE001
            dump[name] = f"{type(err).__name__}: {err}"
    (out / "diagnose.json").write_text(json.dumps(dump, indent=1, ensure_ascii=False, default=str))


def all_owned(args: argparse.Namespace) -> bool:
    status = companion(args)
    return status is not None and all(status["ownership"][d]["state"] == "app" for d in DOMAINS)


# ------------------------------------------------------------------ phases
def phase_legacy(args: argparse.Namespace, out: Path) -> None:
    system = wait("the App connected to 0.9.0", lambda: (s := app("api/system"))["migration"]["mode"] == "legacy" and s, 120, 2)
    check("mode legacy, the integration holds the configuration", system["config_authority"] == "integration"
          and system["companion"]["state"] == "absent", (system["config_authority"], system["companion"]["state"]))
    before = our_entities(args)
    check("the 0.9.0 entities exist", len(before) >= 10 and f"sensor.smart_occupancy_{KAMAR}_state" in before, sorted(before)[:5])
    # 1 inspect
    status, inspect = step("inspect")
    check("inspect: legacy, 0.9.0, owners from the bridges", status == 200 and inspect["result"]["mode"] == "legacy"
          and inspect["result"]["versions"]["integration"] == "0.9.0"
          and inspect["result"]["owners"]["lighting"]["source"] == "legacy", inspect.get("result", inspect))
    # 2 backup
    status, backup = step("backup")
    check("backup written without credentials", status == 200 and backup["result"]["size"] > 1000, backup)
    backups = app("api/migration/backups")
    check("backup listed", any(b["id"] == backup["result"]["id"] for b in backups["backups"]), backups)
    # 3 validate
    status, validate = step("validate")
    check("validate ran", status == 200 and "checks" in validate["result"], validate)
    # 4 transfer config: review, then staged
    status, review = step("transfer_config", {"apply": False})
    check("config review (nothing applied)", status == 200 and review["result"]["applied"] is False
          and app("api/system")["config_authority"] == "integration", review.get("result"))
    status, applied = step("transfer_config", {"apply": True})
    check("config staged", status == 200 and applied["result"]["authority"] == "staged"
          and app("api/system")["config_authority"] == "staged", applied.get("result"))
    staged = http(INGRESS + "api/config/export", POST)["options"]
    check("the staged copy is the complete 0.9.0 configuration (mood source, light types, rooms)",
          (staged.get("mood") or {}).get("main_mood_entity") == MAIN and staged.get("categories") and KAMAR in staged["rooms"],
          {k: staged.get(k) for k in ("mood", "categories")})
    # Activate is refused on 0.9.0.
    status, body = step("activate")
    check("activate refused without the companion", status == 409 and body.get("code") == "companion_missing", (status, body))
    # 5 transfer control in the safe order (the App's shadow runtimes must be ready: lowered thresholds).
    status, body = step("transfer_control", {"domain": "lighting"})
    check("order enforced (lighting before occupancy refused)", status == 409 and body.get("code") == "order", (status, body))
    start = len(args.watcher.events)
    for domain in DOMAINS:
        wait(f"{domain} ready for a handover",
             lambda d=domain: app("api/system")["runtimes"][d]["mode"] == "shadow" and _ready(d), 240, 3)
        status, body = step("transfer_control", {"domain": domain})
        check(f"transfer control: {domain}", status == 200 and body["result"]["domains"].get(domain), (status, body))
    check("no device command during the handovers", not light_calls(args, start), light_calls(args, start)[:3])
    system = app("api/system")
    check("the App runs every subsystem on 0.9.0", all(system["owners"][d] == "app" for d in DOMAINS), system["owners"])
    migration = app("api/migration")
    done = [s["key"] for s in migration["steps"] if s["status"] == "done"]
    check("steps 1-5 recorded", done == ["inspect", "backup", "validate", "transfer_config", "transfer_control"], done)
    save(out, {"entities": sorted(before), "backup": backup["result"]["id"],
               "states": {e: s["state"] for e, s in before.items()}})
    screenshot(args, out, "legacy-wizard", "settings/system/migration")


LAST_READINESS: dict[str, Any] = {}


def _ready(domain: str) -> bool:
    page = {"occupancy": "api/engine", "lighting": "api/lighting", "curtains": "api/curtains",
            "forecast": "api/predictions"}[domain]
    try:
        data = app(page)
    except Exception:  # noqa: BLE001
        return False
    readiness = data.get("readiness") or (data.get("engine") or {}).get("readiness") or {}
    LAST_READINESS[domain] = [c for c in readiness.get("checks", []) if c.get("status") != "ok"]
    return bool(readiness.get("ready"))


def phase_activate(args: argparse.Namespace, out: Path) -> None:
    saved = load(out)
    status = wait("the companion loaded", lambda: companion(args), 120, 2)
    check("companion 1.0.0, not paired yet", status["companion_version"] == "1.0.0" and not status["paired"], status)
    system = wait("the App sees the companion", lambda: (s := app("api/system"))["companion"]["state"] == "present" and s, 120, 2)
    check("mode companion, configuration still staged", system["migration"]["mode"] == "companion"
          and system["config_authority"] == "staged", (system["migration"]["mode"], system["config_authority"]))
    now = our_entities(args)
    kept = set(saved.get("entities", [])) & set(now)
    check("every entity id kept across the replacement", kept == set(saved.get("entities", [])),
          sorted(set(saved.get("entities", [])) - set(now))[:10])
    # Right after the replacement the companion has no published view yet: the entities exist (same ids) but are
    # unavailable or not fresh; nothing is invented.
    check("room entities unavailable or not fresh before Activate",
          all(s["state"] in ("unavailable", "unknown") or s["attributes"].get("fresh") is False
              for e, s in now.items() if e.startswith("sensor.smart_occupancy_kamar")),
          {e: (s["state"], s["attributes"].get("fresh")) for e, s in now.items() if "kamar" in e})
    start = len(args.watcher.events)
    # 6 activate
    status, activated = step("activate")
    check("activate: paired, promoted, claimed", status == 200 and activated["result"]["activated"] is True, activated)
    wait("every domain owned by the App", lambda: all_owned(args), 60, 2)
    system = app("api/system")
    check("the App is the configuration authority", system["config_authority"] == "app" and system["companion"]["trusted"])
    rec = record(args, "lighting")
    check("lighting record: epoch 1, targets published", rec["epoch"] == 1 and (rec["targets"].get("lights") or 0) >= 1, rec)
    check("nothing was switched by the activation", not light_calls(args, start), light_calls(args, start)[:3])
    wait("room entities fresh again", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("attributes", {}).get("fresh") is True, 60, 2)
    # 7 verify
    status, verify = step("verify")
    degraded = verify.get("result", {}).get("degraded", [])
    check("verify passed (restart recovery informational)", status == 200 and verify["result"]["ok"] is True
          and set(degraded) <= {"Restart recovery"}, verify.get("result"))
    # The App commands the light through the companion's executor.
    status, body = command("room/activate", area_id=KAMAR, confirm=True)
    check("room activated by the App's own handler", status == 200 and body.get("handled_by") == "app", body)
    for mood, level in LEVELS.items():
        status, body = command("profile/set", scope="light", area_id=KAMAR, mood=mood, target=LIGHT,
                               setting={"action": "on", "brightness_pct": level})
        check(f"profile {mood} (App)", status == 200, body)
    executed = record(args, "lighting")["executed"]
    write_state(args, PC, "on")
    write_state(args, MAIN, "Kvøld")
    try:
        wait(f"{LIGHT} at {LEVELS['Kvøld']} %", lambda: pct(args) == LEVELS["Kvøld"], 60)
    except SystemExit:
        diagnose(args, out)
        raise
    rec = record(args, "lighting")
    check("the command ran through the companion's executor", rec["executed"] >= executed + 1 and rec["execute_errors"] == 0, rec)
    calls = [e for e in args.watcher.events if e.get("domain") == "light"]
    check("the light service was called with the App's context", bool(calls), calls[-1:] )
    wait(f"{KAMAR} occupied on the native entity", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("state") == "confirmed_occupancy", 60)
    # The compact cards and a relayed action.
    req = urllib.request.Request(f"{args.ha_url}/smart_occupancy_static/smart-occupancy-cards.js",
                                 headers={"Authorization": f"Bearer {args.ha_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        js = resp.read().decode()
    check("compact cards served by the companion", "smart-occupancy-house-card" in js and "<time24>" in js)
    view = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/view"})
    check("card view has the room and the owners", view["success"] and KAMAR in view["result"]["rooms"]
          and view["result"]["owners"]["lighting"] == "app", view.get("result", view).get("owners"))
    action = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/action", "kind": "room/resume",
                                                "params": {"area_id": KAMAR}})
    check("a card action is relayed to the App and answered", action["success"], action)
    # 8 retire
    storage = Path(args.ha_config) / ".storage" if args.ha_config else None
    before_retire = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/legacy/status"})["result"]
    status, retired = step("retire", {"confirm": True})
    check("retire done", status == 200 and retired["result"].get("outcome", {}).get("stores_removed") is not None
          or retired.get("result", {}).get("already"), retired)
    legacy = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/legacy/status"})
    check("legacy data retired, backup present", legacy["success"] and legacy["result"]["present"] is False
          and legacy["result"]["backup"] is not None, legacy.get("result"))
    if storage is not None:
        time.sleep(6)  # the Store writes with a delay
        gone = [k for k in ("smart_occupancy.lighting", "smart_occupancy.curtains_state", "smart_occupancy.forecast_days")
                if not (storage / k).exists()]
        check("legacy store files removed", len(gone) == 3 and (storage / "smart_occupancy.legacy_backup").exists(), gone)
    check("the App's migration backup is kept", any(b["id"] == saved.get("backup") for b in app("api/migration/backups")["backups"]))
    migration = app("api/migration")
    check("migration completed", bool(migration["completed_at"]), migration.get("completed_at"))
    # Rollback path: the legacy data can be restored (for a downgrade to 0.9.0) and is complete again.
    status, aborted = step("abort", {"restore_legacy": True})
    check("legacy data restored for a rollback", status == 200 and any("restored" in a for a in aborted["result"]["actions"]), aborted)
    legacy = ha_ws(args.ha_url, args.ha_token, {"type": "smart_occupancy/companion/legacy/status"})["result"]
    check("the restored options are the retired ones", legacy["present"] is True
          and legacy["options_revision"] == before_retire["options_revision"], (legacy, before_retire))
    check("the App stays the authority after a restore", app("api/system")["config_authority"] == "app")
    screenshot(args, out, "companion-system", "settings/system")
    screenshot(args, out, "dashboard", "dashboard")
    save(out, {**saved, "epochs": {d: record(args, d)["epoch"] for d in DOMAINS}})


def phase_failure(args: argparse.Namespace, out: Path) -> None:
    saved = load(out)
    wait("every domain owned by the App", lambda: all_owned(args), 120, 2)
    write_state(args, PC, "on")
    wait(f"{KAMAR} occupied", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("state") == "confirmed_occupancy", 60)
    before_light = state(args, LIGHT)["state"]
    start = len(args.watcher.events)
    epochs = {d: record(args, d)["epoch"] for d in DOMAINS}
    subprocess.run(["docker", "kill", "sl-test-app"], check=True, capture_output=True)
    killed = time.time()
    wait("the companion marks the App away", lambda: record(args, "lighting")["state"] == "app_away", 60, 2)
    check("away within the lease and a margin", time.time() - killed < 45, round(time.time() - killed))
    s = state(args, f"sensor.smart_occupancy_{KAMAR}_state")
    check("occupancy keeps its last state, marked not fresh", s["state"] == "confirmed_occupancy"
          and s["attributes"].get("fresh") is False, (s["state"], s["attributes"].get("fresh")))
    write_state(args, PC, "off")  # the person leaves while the App is away
    time.sleep(8)
    s = state(args, f"sensor.smart_occupancy_{KAMAR}_state")
    check("no vacancy is inferred while the App is away", s["state"] == "confirmed_occupancy", s["state"])
    check("nothing was switched while the App is away", not light_calls(args, start) and state(args, LIGHT)["state"] == before_light,
          light_calls(args, start)[:3])
    check("the owner is kept (app, away)", record(args, "lighting")["owner"] == "app")
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    wait("the restarted App owns every domain again", lambda: all_owned(args), 180, 2)
    now = {d: record(args, d)["epoch"] for d in DOMAINS}
    check("new epochs after the restart (never the old lease)", all(now[d] == epochs[d] + 1 for d in DOMAINS), (epochs, now))
    wait("the engine re-read the states: the room is vacant now", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("state") in ("vacant", "possible_vacancy", "uncertain"), 120, 2)
    # A revoke from Home Assistant: the App stops at once and claims again.
    start = len(args.watcher.events)
    ha(args, "/api/services/smart_occupancy/revoke_app_control", {"domain": "lighting"})
    wait("lighting revoked", lambda: record(args, "lighting")["owner"] == "none", 15)
    wait("the App claimed lighting again", lambda: record(args, "lighting")["state"] == "app" and record(args, "lighting")["epoch"] >= now["lighting"] + 2, 60, 2)
    check("no device command around the revoke", not light_calls(args, start), light_calls(args, start)[:3])
    rec = record(args, "lighting")
    check("the record's history explains it", any(h.get("reason") == "revoked" for h in rec["history"]), rec["history"][:3])
    save(out, {**saved, "epochs": {d: record(args, d)["epoch"] for d in DOMAINS}})
    screenshot(args, out, "after-failure", "settings/system")


def phase_restart_check(args: argparse.Namespace, out: Path) -> None:
    saved = load(out)
    start = len(args.watcher.events)
    wait("the companion loaded after the restart", lambda: companion(args), 120, 2)
    wait("the App owns every domain again", lambda: all_owned(args), 180, 2)
    epochs = saved.get("epochs") or {}
    now = {d: record(args, d)["epoch"] for d in DOMAINS}
    check("new epochs after the Home Assistant restart", all(now[d] > epochs.get(d, 0) for d in DOMAINS), (epochs, now))
    rec = record(args, "lighting")
    check("the restart is in the record's history", any(h.get("reason") == "home_assistant_restarted" for h in rec["history"]), rec["history"][:4])
    wait("entities fresh", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("attributes", {}).get("fresh") is True, 60, 2)
    check("nothing was switched by the restart recovery", not light_calls(args, start), light_calls(args, start)[:3])
    status, verify = step("verify")
    check("verify after a restart: no degraded function", status == 200 and verify["result"]["ok"] and not verify["result"].get("degraded"), verify.get("result", {}).get("degraded"))


def phase_reinstall(args: argparse.Namespace, out: Path) -> None:
    """Companion 1.0.0 with the complete 0.9.0 data, the App installed fresh (empty storage): Transfer config
    imports the options and the engine stores, Activate claims, the light follows."""
    status = wait("the companion loaded", lambda: companion(args), 120, 2)
    check("companion holds the legacy data", status["legacy"]["present"] is True and status["legacy"]["rooms"] >= 1, status["legacy"])
    write_state(args, PC, "off")  # a previous run may have left the person in the room and the mood set
    write_state(args, MAIN, "Nátt")
    system = wait("the App sees the companion", lambda: (s := app("api/system"))["companion"]["state"] == "present" and s, 120, 2)
    check("no configuration and nothing claimed", system["config_authority"] == "integration"
          and all(v == "none" for v in system["owners"].values()), (system["config_authority"], system["owners"]))
    setup = app("api/setup")
    check("setup refuses to start fresh (legacy data present)", app_post("api/setup/start", {})[0] == 409, setup["config"])
    st, review = step("transfer_config", {"apply": False})
    plan = {x["key"]: x["import"] for x in review["result"]["stores"]}
    check("review lists the stores to import", st == 200 and plan.get("smart_occupancy.lighting") is True, plan)
    st, applied = step("transfer_config", {"apply": True})
    check("options and stores imported", st == 200 and applied["result"]["authority"] == "app"
          and "smart_occupancy.lighting" in applied["result"]["stores_imported"], applied.get("result", {}).get("stores_imported"))
    start = len(args.watcher.events)
    st, activated = step("activate")
    check("activate claims every domain", st == 200 and activated["result"]["activated"] is True, activated)
    wait("every domain owned by the App", lambda: all_owned(args), 60, 2)
    check("nothing was switched by the import or the claims", not light_calls(args, start), light_calls(args, start)[:3])
    wait("room entities fresh", lambda: (state(args, f"sensor.smart_occupancy_{KAMAR}_state") or {}).get("attributes", {}).get("fresh") is True, 60, 2)
    export = http(INGRESS + "api/lighting/export", POST)  # an administrator's download (CSRF header)
    rooms = (export.get("config") or {}).get("rooms") or {}
    check("the App's lighting configuration came from the integration's store (room already Automatic)",
          KAMAR in rooms and rooms[KAMAR].get("control_enabled") is True, {k: (v or {}).get("control_enabled") for k, v in rooms.items()})
    st, body = command("room/activate", area_id=KAMAR, confirm=True)
    check("room activated (App)", st == 200 and body.get("handled_by") == "app", body)
    for mood, level in LEVELS.items():
        command("profile/set", scope="light", area_id=KAMAR, mood=mood, target=LIGHT, setting={"action": "on", "brightness_pct": level})
    write_state(args, PC, "on")
    write_state(args, MAIN, "Dagur")
    try:
        wait(f"{LIGHT} at {LEVELS['Dagur']} % through the companion", lambda: pct(args) == LEVELS["Dagur"], 60)
    except SystemExit:
        diagnose(args, out)
        raise
    st, verify = step("verify")
    check("verify ok", st == 200 and verify["result"]["ok"] is True, verify.get("result"))
    screenshot(args, out, "reinstall-system", "settings/system/migration")


def phase_fresh(args: argparse.Namespace, out: Path) -> None:
    status = wait("the companion loaded", lambda: companion(args), 120, 2)
    check("companion without legacy data", status["legacy"]["present"] is False, status["legacy"])
    setup = wait("the App's setup page", lambda: app("api/setup"), 120, 2)
    check("setup: no legacy, no configuration yet", setup["legacy_installed"] is False and not setup["config"]["authoritative"], setup["config"])
    status_code, created = app_post("api/setup/start", {})
    check("configuration created with safe defaults", status_code == 200 and created["result"]["created"], created)
    status_code, paired = app_post("api/system/pair", {})
    check("paired", status_code == 200, paired)
    wait("every domain claimed", lambda: all_owned(args), 60, 2)
    check("no room entities yet", not [e for e in our_entities(args) if KAMAR in e])
    # A room through the configuration import (reviewed): the entities appear, the targets follow.
    export = http(INGRESS + "api/config/export", POST)
    doc = {**export, "options": {**export["options"], "rooms": {KAMAR: {
        "enabled": True, "sensors": [{"entity_id": "binary_sensor.radar_kamar", "kind": "radar", "role": "maintain", "inverted": False},
                                     {"entity_id": "binary_sensor.door_kamar", "kind": "door", "role": "trigger", "inverted": False}],
        "lights": [LIGHT], "reference_occupancy_entity": None, "mood_overrides": {}, "light_overrides": {}, "timing": {},
        "adjacent_rooms": []}}, "mood": {**export["options"].get("mood", {}), "main_mood_entity": MAIN}}}
    status_code, review = app_post("api/config/import/review", {"file": doc})
    check("import reviewed", status_code == 200 and review.get("changes"), review)
    status_code, imported = app_post("api/config/import", {"file": doc, "revision": app("api/system")["config"]["revision"]})
    check("import applied", status_code == 200, imported)
    wait("room entities created by the companion", lambda: f"sensor.smart_occupancy_{KAMAR}_state" in our_entities(args), 60, 2)
    wait("lighting targets published", lambda: (record(args, "lighting")["targets"].get("lights") or 0) >= 1, 60, 2)
    status_code, body = command("room/activate", area_id=KAMAR, confirm=True)
    check("room activated (App)", status_code == 200 and body.get("handled_by") == "app", body)
    for mood, level in LEVELS.items():
        command("profile/set", scope="light", area_id=KAMAR, mood=mood, target=LIGHT, setting={"action": "on", "brightness_pct": level})
    write_state(args, PC, "on")
    write_state(args, MAIN, "Dagur")
    wait(f"{LIGHT} at {LEVELS['Dagur']} % through the companion", lambda: pct(args) == LEVELS["Dagur"], 60)
    check("executed through the companion", record(args, "lighting")["executed"] >= 1 and record(args, "lighting")["execute_errors"] == 0)
    dash = app("api/dashboard")
    check("dashboard shows the room from the App's runtimes", dash["house"]["rooms"] == 1 and dash["engines"]["lighting"]["owner"] == "app", dash["engines"])
    screenshot(args, out, "fresh-setup", "settings/setup")
    screenshot(args, out, "fresh-dashboard", "dashboard")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--ha-config", default=None)
    parser.add_argument("--phase", default="legacy", choices=["legacy", "activate", "failure", "restart-check", "fresh", "reinstall"])
    args = parser.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(10)
    try:
        {"legacy": phase_legacy, "activate": phase_activate, "failure": phase_failure,
         "restart-check": phase_restart_check, "fresh": phase_fresh, "reinstall": phase_reinstall}[args.phase](args, out)
    except SystemExit:
        pass
    except Exception as err:  # noqa: BLE001
        check(f"phase {args.phase} crashed", False, f"{type(err).__name__}: {err}")
    finally:
        args.watcher.stop_flag = True
    (out / f"system-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
