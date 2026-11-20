"""End-to-end checks of Phase A7.0 (Smart Comfort 0.10.0: the rebranding and the Climate module) with a real Home
Assistant, the companion 1.0.0 and the real App image.

    python -I a7_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> [--chromium <path>]
        --phase all|restart-check

Runs after ``system_checks.py --phase fresh`` (and ``a62_checks.py``) in the same stack. The Home Assistant has the
``demo`` climate entities (``climate.hvac`` in Kamar Norður, ``climate.heatpump`` in Stova, ``climate.ecobee`` without
an area), a generic thermostat ``climate.stova_thermostat`` (Stova, off), template temperature sensors
(``sensor.kamar_temperature``, ``sensor.kamar_shelf_temperature``, ``sensor.stova_temperature``,
``sensor.outdoor_temperature``) and ``binary_sensor.window_stova``.

``all``
    Branding (name, subtitle, panel, page title) and the existing modules still answering; Climate on a fresh
    installation; discovery (capabilities, the TRV/thermostat kinds, a thermostat's own sensor kept apart, outdoor
    and window sensors); the reviewed configuration through the interface (suggested rooms, primary and secondary
    sensors, outdoor, review, save); a thermal zone, validation errors and stale revisions through the API; live
    values, an unavailable sensor, an open window; the history with the Recorder's backfill; backups, restore and
    export; a renamed sensor followed; the pages on desktop, tablet and phone; and no ``climate`` service call at all.
``restart-check``
    After the App container and Home Assistant were restarted: the configuration (same revision) and the
    observations are kept, still no thermostat command.
``app-outage``
    The App container is stopped for more than two minutes while a room sensor changes; after it starts again the
    change is read from the Recorder (the gap fill), with the value and time Home Assistant recorded.

States are changed by writing them (``POST /api/states``), never by a service call.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, results  # noqa: E402
from rooms_checks import ha_ws, http  # noqa: E402
from system_checks import KAMAR, POST, app, app_post, ha, state, wait  # noqa: E402

STOVA = "stova"
KT = "sensor.kamar_temperature"
KS = "sensor.kamar_shelf_temperature"
ST = "sensor.stova_temperature"
OUT = "sensor.outdoor_temperature"
WINDOW = "binary_sensor.window_stova"
STATE_FILE = "a7_e2e_state.json"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "phone": (390, 844)}


def write(args: argparse.Namespace, entity_id: str, value: str, **attrs: Any) -> None:
    current = state(args, entity_id) or {}
    ha(args, f"/api/states/{entity_id}", {"state": value, "attributes": {**current.get("attributes", {}), **attrs}})


def climate_calls(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events if e.get("domain") == "climate"]


def admin_get(path: str) -> Any:
    """An administrator-only read (the backend checks the CSRF header)."""
    return http(INGRESS + path, POST)


def doc() -> dict[str, Any]:
    return app("api/climate")


def save_config(config: dict[str, Any]) -> tuple[int, Any]:
    return app_post("api/climate/save", {"config": config, "revision": doc()["revision"]})


def thermostat_states(args: argparse.Namespace) -> dict[str, Any]:
    out = {}
    for e in ("climate.hvac", "climate.heatpump", "climate.ecobee", "climate.stova_thermostat"):
        s = state(args, e) or {}
        a = s.get("attributes", {})
        out[e] = (s.get("state"), a.get("temperature"), a.get("target_temp_low"), a.get("target_temp_high"))
    return out


# ------------------------------------------------------------------ phases
def phase_all(args: argparse.Namespace, out: Path) -> None:
    before = thermostat_states(args)
    ov = app("api/overview")
    check("branding: Smart Comfort with its subtitle", ov["app"]["name"] == "Smart Comfort"
          and ov["app"]["subtitle"] == "Intelligent Home Automation" and ov["app"]["version"] == "0.10.0", ov["app"])
    check("the Climate module is native and observing", any(m["key"] == "climate" and m["kind"] == "native"
                                                            for m in ov["modules"]) and ov["safety"]["heating_control"] is False)
    check("migration counts unchanged (seven modules)", ov["migration"]["modules_total"] == 7, ov["migration"]["modules_total"])
    dash = app("api/dashboard")
    check("existing modules still answer (dashboard, lighting owner)", dash["engines"]["lighting"]["owner"] == "app", dash["engines"])
    check("presence and predictions still answer", app("api/presence") is not None and app("api/predictions") is not None)

    fresh = app("api/climate/overview")
    check("fresh Climate: no rooms, observing", fresh["rooms"] == [] and fresh["mode"] == "observing", fresh["mode"])

    d = app("api/climate/discovery")
    by = {t["entity_id"]: t for t in d["thermostats"]}
    check("discovery: every climate entity", {"climate.hvac", "climate.heatpump", "climate.ecobee",
                                              "climate.stova_thermostat"} <= set(by), sorted(by))
    check("discovery: areas of the thermostats", by["climate.hvac"]["area_id"] == KAMAR
          and by["climate.heatpump"]["area_id"] == STOVA and by["climate.ecobee"]["area_id"] is None,
          {k: v["area_id"] for k, v in by.items()})
    check("discovery: the generic thermostat is recognised", by["climate.stova_thermostat"]["kind"] == "generic",
          by["climate.stova_thermostat"]["kind"])
    check("discovery: capabilities read from the entity (ecobee: a target range)",
          by["climate.ecobee"]["capabilities"]["target_range"] is True
          and by["climate.stova_thermostat"]["capabilities"]["target"] is True, by["climate.ecobee"]["capabilities"])
    sensors = {s["entity_id"]: s for s in d["sensors"]}
    check("discovery: temperature sensors with their areas", sensors[KT]["area_id"] == KAMAR
          and sensors[ST]["area_id"] == STOVA and sensors[OUT]["outdoor"], {k: (v["area_id"], v["outdoor"]) for k, v in sensors.items()})
    check("discovery: windows", any(o["entity_id"] == WINDOW and o["kind"] == "window" for o in d["openings"]))
    check("suggestions: Kamar's room sensor, Stova's two devices", d["suggestions"]["rooms"][KAMAR]["primary_sensor"] in (KT, KS)
          and {t["entity_id"] for t in d["suggestions"]["rooms"][STOVA]["thermostats"]} == {"climate.heatpump", "climate.stova_thermostat"},
          d["suggestions"]["rooms"])
    check("suggestions: outdoor source", d["suggestions"]["outdoor"] == OUT, d["suggestions"]["outdoor"])

    ui_setup(args, out)
    cfg = doc()["config"]
    check("saved through the interface: two rooms", set(cfg["rooms"]) == {KAMAR, STOVA}, sorted(cfg["rooms"]))
    check("saved: Kamar primary + secondary sensor, its thermostat", cfg["rooms"][KAMAR]["primary_sensor"]["entity_id"] == KT
          and [s["entity_id"] for s in cfg["rooms"][KAMAR]["secondary_sensors"]] == [KS]
          and [t["entity_id"] for t in cfg["rooms"][KAMAR]["thermostats"]] == ["climate.hvac"], cfg["rooms"][KAMAR])
    check("saved: Stova with two heating devices and its window", len(cfg["rooms"][STOVA]["thermostats"]) == 2
          and [o["entity_id"] for o in cfg["rooms"][STOVA]["openings"]] == [WINDOW], cfg["rooms"][STOVA])
    check("saved: outdoor source and registry ids", cfg["outdoor"]["entity_id"] == OUT and cfg["rooms"][KAMAR]["primary_sensor"]["uid"],
          cfg["outdoor"])

    # A thermal zone (connected rooms, one shared heating device), through the API's reviewed save.
    config = json.loads(json.dumps(cfg))
    config["zones"] = [{"id": "nordur", "name": "Norður", "rooms": [KAMAR, STOVA], "layout": "connected",
                        "shared_thermostats": ["climate.heatpump"], "heating_circuit": "Radiators", "origin": "manual"}]
    status, rev = app_post("api/climate/review", {"config": config, "revision": doc()["revision"]})
    check("zone: reviewed", status == 200 and rev["valid"] and any(c["kind"] == "zone_added" for c in rev["changes"]), rev)
    status, saved = save_config(config)
    check("zone: saved", status == 200 and saved["result"]["saved"], saved)
    bad = json.loads(json.dumps(config))
    bad["mode"] = "automatic"
    status, body = save_config(bad)
    check("automatic mode refused (observing only)", status == 400 and body["errors"][0]["path"] == "mode", body)
    bad = json.loads(json.dumps(config))
    bad["rooms"][STOVA]["thermostats"].append({"entity_id": "climate.hvac", "kind": "other"})
    status, body = save_config(bad)
    check("a thermostat in two rooms refused", status == 400, body)
    status, body = app_post("api/climate/save", {"config": config, "revision": "0" * 16})
    check("stale revision refused", status == 409 and body["code"] == "stale_revision", body)

    # Live values.
    write(args, KT, "21.3")
    wait("the new Kamar temperature", lambda: next(r for r in app("api/climate/overview")["rooms"]
                                                   if r["area_id"] == KAMAR)["temperature"]["value_c"] == 21.3, 30)
    room = app(f"api/climate/rooms/{KAMAR}")
    check("thermostat's own reading kept apart from the room's", room["thermostats"][0]["measured"]["value_c"] == 22.0
          and room["temperature"]["value_c"] == 21.3, room["thermostats"][0]["measured"])
    check("indoor/outdoor difference", room["outdoor_difference_c"] == round(21.3 - 6.5, 2), room["outdoor_difference_c"])
    stova = app(f"api/climate/rooms/{STOVA}")
    hp = next(t for t in stova["thermostats"] if t["entity_id"] == "climate.heatpump")
    check("reported heating (heat pump)", hp["heating"] == {"state": "heating", "basis": "reported"}, hp["heating"])
    check("zone shown on the room", stova["zone"]["id"] == "nordur", stova["zone"])
    write(args, KT, "unavailable")
    wait("the unavailable sensor reported", lambda: any(p["entity_id"] == KT for p in app("api/climate/overview")["problems"]), 30)
    room = app(f"api/climate/rooms/{KAMAR}")
    check("unavailable primary: the secondary sensor is used, never the thermostat", room["temperature"]["source"] == "secondary"
          and room["temperature"]["value_c"] == 20.9, room["temperature"])
    write(args, KT, "21.0")
    write(args, WINDOW, "on")
    wait("the open window", lambda: app(f"api/climate/rooms/{STOVA}")["any_open"], 30)
    write(args, WINDOW, "off")

    # History: the Recorder's backfill plus the live records.
    wait("the Recorder backfill", lambda: doc()["status"]["observation"]["backfill"]["state"] == "done", 120, 2)
    time.sleep(16)  # one flush interval
    hist = app(f"api/climate/rooms/{KAMAR}/history?window=21600")
    primary = next(s for s in hist["series"] if s["key"] == "primary")
    values = [p[1] for p in primary["points"]]
    check("history: room temperature points (Recorder and live)", len([v for v in values if v is not None]) >= 2, primary["points"][-5:])
    check("history: the unavailable reading is a gap", None in values, values[-6:])
    check("history: the thermostat's setpoint series", any(s["kind"] == "setpoint" and s["points"] for s in hist["series"]))
    shist = app(f"api/climate/rooms/{STOVA}/history?window=21600")
    heat = next(b for b in shist["bands"] if b["entity_id"] == "climate.heatpump")
    check("history: the heat pump's reported heating band", heat["reported"] and heat["intervals"], heat)
    opened = next(b for b in shist["bands"] if b["kind"] == "open")
    check("history: the window was open", bool(opened["intervals"]), opened)

    # Backups, restore, export.
    changed = json.loads(json.dumps(doc()["config"]))
    changed["defaults"]["comfort_c"] = 21.5
    status, saved = save_config(changed)
    status_code = None
    try:
        app("api/climate/backups")
    except Exception as err:  # noqa: BLE001
        status_code = getattr(err, "code", None)
    check("climate backups need the administrator request header", status_code == 403, status_code)
    backups = admin_get("api/climate/backups")["backups"]
    check("a backup before saving", status == 200 and backups, backups[:1])
    status, review = app_post("api/climate/restore/review", {"backup": backups[0]["id"]})
    check("restore reviewed", status == 200 and review["valid"], review)
    export = admin_get("api/climate/export")
    status, review = app_post("api/climate/import/review", {"file": export})
    check("export re-imports identically", status == 200 and review["identical"], review)

    # A renamed sensor is followed (renamed by the test in Home Assistant's registry, not by the App).
    r = ha_ws(args.ha_url, args.ha_token, {"type": "config/entity_registry/update", "entity_id": KS,
                                           "new_entity_id": "sensor.kamar_shelf_temp_2"})
    check("sensor renamed in Home Assistant", r.get("success"), r)
    wait("the rename followed", lambda: doc()["config"]["rooms"][KAMAR]["secondary_sensors"][0]["entity_id"]
         == "sensor.kamar_shelf_temp_2", 60, 2)
    ha_ws(args.ha_url, args.ha_token, {"type": "config/entity_registry/update", "entity_id": "sensor.kamar_shelf_temp_2",
                                       "new_entity_id": KS})
    wait("the rename back followed", lambda: doc()["config"]["rooms"][KAMAR]["secondary_sensors"][0]["entity_id"] == KS, 60, 2)

    ui_pages(args, out)
    after = thermostat_states(args)
    check("no thermostat changed", before == after, {"before": before, "after": after})
    check("no climate service call during the run", not climate_calls(args), climate_calls(args)[:3])
    sent = app("api/overview")["safety"]
    check("the App sent no service call", sent["service_calls_sent"] == 0, sent["service_calls_sent"])
    (out / STATE_FILE).write_text(json.dumps({"revision": doc()["revision"], "records": doc()["status"]["observation"]["written"]}))


def ui_setup(args: argparse.Namespace, out: Path) -> None:
    """Climate → Settings through the interface: suggested rooms, sensors, outdoor, review, save."""
    from playwright.sync_api import expect, sync_playwright  # noqa: PLC0415

    shots = out / "screenshots"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(PANEL)
        frame = app_frame(page)
        expect(frame.locator(".brand-text")).to_contain_text("Smart Comfort", timeout=20000)
        expect(frame.locator(".brand-sub")).to_have_text("Intelligent Home Automation")
        check("page title is Smart Comfort", frame.evaluate("document.title") == "Smart Comfort", frame.evaluate("document.title"))
        frame.goto(INGRESS + "#/climate")
        expect(frame.locator("[data-testid=climate-empty]")).to_be_visible(timeout=20000)
        expect(frame.locator("[data-testid=climate-observing]")).to_be_visible()
        page.screenshot(path=str(shots / "a7-climate-empty.png"), full_page=True)
        frame.locator("[data-testid=climate-setup]").click()
        expect(frame.locator("[data-testid=climate-room-list]")).to_be_visible(timeout=20000)
        frame.locator(f"[data-testid=climate-add-suggested][data-area={KAMAR}]").click()
        expect(frame.locator(f"[data-testid=climate-room-editor][data-area={KAMAR}]")).to_be_visible()
        frame.locator("[data-testid=climate-primary]").select_option(KT)
        frame.locator("[data-testid=climate-secondary] input[type=checkbox]").evaluate_all("els => els.forEach(e => { if (e.checked) e.click(); })")
        sec = frame.locator(f"[data-testid=climate-secondary] input[data-entity='{KS}']")
        if not sec.is_visible():
            frame.locator("[data-testid=climate-room-editor] details summary", has_text="Secondary").click()
        sec.check()
        frame.locator(f"[data-testid=climate-add-suggested][data-area={STOVA}]").click()
        expect(frame.locator(f"[data-testid=climate-room-editor][data-area={STOVA}]")).to_be_visible()
        frame.locator("[data-testid=climate-secondary] input[type=checkbox]").evaluate_all("els => els.forEach(e => { if (e.checked) e.click(); })")
        frame.goto(INGRESS + "#/climate/settings/outdoor")
        frame.locator("[data-testid=climate-outdoor]").select_option(OUT)
        expect(frame.locator("[data-testid=climate-unsaved]")).to_be_visible()
        page.screenshot(path=str(shots / "a7-climate-settings-outdoor.png"), full_page=True)
        frame.locator("[data-testid=climate-review-button]").click()
        dialog = frame.locator("[data-testid=climate-review]")
        expect(dialog.locator("[data-group=changes]")).to_be_visible(timeout=20000)
        text = dialog.text_content() or ""
        check("review lists the added rooms", "Climate room added: Kamar Norður" in text and "Climate room added: Stova" in text, text[:400])
        page.screenshot(path=str(shots / "a7-climate-review.png"))
        frame.locator("[data-testid=climate-confirm]").click()
        expect(frame.locator("[data-testid=climate-notice]")).to_contain_text("Saved", timeout=20000)
        expect(frame.locator("[data-testid=climate-unsaved]")).to_have_count(0)
        check("no page errors while configuring", not errors, errors[:3])
        browser.close()


def ui_pages(args: argparse.Namespace, out: Path) -> None:
    from playwright.sync_api import expect, sync_playwright  # noqa: PLC0415

    shots = out / "screenshots"
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium)
        for label, size in SIZES.items():
            page = browser.new_page(viewport={"width": size[0], "height": size[1]})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(PANEL)
            frame = app_frame(page)
            frame.goto(INGRESS + "#/climate")
            expect(frame.locator("[data-testid=climate-room-card]")).to_have_count(2, timeout=20000)
            expect(frame.locator(f"[data-testid=climate-room-card][data-area={KAMAR}] [data-testid=climate-room-temp]")).to_contain_text("°C")
            check(f"{label}: overview without horizontal overflow", frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            page.screenshot(path=str(shots / f"a7-climate-overview-{label}.png"), full_page=True)
            frame.goto(INGRESS + "#/climate/rooms")
            expect(frame.locator("[data-testid=climate-rooms-table]")).to_be_visible()
            page.screenshot(path=str(shots / f"a7-climate-rooms-{label}.png"), full_page=True)
            frame.goto(INGRESS + f"#/climate/rooms/{STOVA}")
            expect(frame.locator("[data-testid=climate-history-chart]")).to_be_visible(timeout=20000)
            expect(frame.locator("[data-testid=climate-thermostat]")).to_have_count(2)
            check(f"{label}: room page without horizontal overflow", frame.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))
            frame.locator("[data-window='86400']").click()
            expect(frame.locator("[data-window='86400']")).to_have_attribute("aria-pressed", "true")
            page.screenshot(path=str(shots / f"a7-climate-room-{label}.png"), full_page=True)
            frame.goto(INGRESS + "#/climate/settings/rooms")
            expect(frame.locator("[data-testid=climate-room-editor]")).to_be_visible(timeout=20000)
            page.screenshot(path=str(shots / f"a7-climate-settings-{label}.png"), full_page=True)
            frame.goto(INGRESS + "#/climate/settings/zones")
            expect(frame.locator("[data-testid=climate-zone]")).to_have_count(1)
            page.screenshot(path=str(shots / f"a7-climate-zones-{label}.png"), full_page=True)
            if label == "desktop":
                # Export buttons fetch with the request header (a plain link was refused with 403 before 0.10.0).
                for route, testid, prefix in (("climate/settings/backups", "climate-export", "smart-comfort-climate-"),
                                              ("lighting/control", "lighting-export", "smart-comfort-lighting-")):
                    frame.goto(INGRESS + f"#/{route}")
                    with page.expect_download(timeout=20000) as dl:
                        frame.locator(f"[data-testid={testid}]").click()
                    name = dl.value.suggested_filename
                    body = json.loads(Path(dl.value.path()).read_text())
                    check(f"{testid} downloads", name.startswith(prefix) and isinstance(body, dict), name)
            for route in ("dashboard", "lighting", "presence", "curtains", "predictions"):
                frame.goto(INGRESS + f"#/{route}")
                expect(frame.locator(".page-header h2")).to_be_visible(timeout=20000)
            check(f"{label}: no page errors", not errors, errors[:3])
            page.close()
        browser.close()


def phase_restart_check(args: argparse.Namespace, out: Path) -> None:
    saved = json.loads((out / STATE_FILE).read_text())
    d = wait("the App answers", lambda: doc(), 120, 2)
    check("configuration kept (same revision)", d["revision"] == saved["revision"], d["revision"])
    wait("the gap filled from the Recorder", lambda: doc()["status"]["observation"]["backfill"]["state"] == "done", 180, 2)
    hist = app(f"api/climate/rooms/{KAMAR}/history?window=21600")
    primary = next(s for s in hist["series"] if s["key"] == "primary")
    check("observations kept across the restarts", len([p for p in primary["points"] if p[1] is not None]) >= 2, primary["points"][-4:])
    write(args, KT, "22.2")
    wait("live values after the restarts", lambda: next(r for r in app("api/climate/overview")["rooms"]
                                                        if r["area_id"] == KAMAR)["temperature"]["value_c"] == 22.2, 60)
    check("no climate service call", not climate_calls(args), climate_calls(args)[:3])


def phase_app_outage(args: argparse.Namespace, out: Path) -> None:
    time.sleep(60)  # the chart averages per 45 s bucket (6 h window): keep this value apart from earlier writes
    subprocess.run(["docker", "stop", "sl-test-app"], check=True, capture_output=True)
    time.sleep(5)
    write(args, KT, "24.1")
    changed_at = time.time()
    time.sleep(135)  # longer than the 2-minute minimum gap
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    wait("the App back", lambda: doc(), 120, 2)
    status = wait("the gap filled from the Recorder", lambda: (b := doc()["status"]["observation"]["backfill"])["state"] == "done"
                  and b["windows"] >= 1 and b, 180, 2)
    check("the Recorder was read for the outage", status["records"] >= 1, status)
    time.sleep(16)
    hist = app(f"api/climate/rooms/{KAMAR}/history?window=21600")
    primary = next(s for s in hist["series"] if s["key"] == "primary")
    near = [p for p in primary["points"] if p[1] == 24.1]
    check("the change made while the App was away is in the history", bool(near) and abs(near[-1][0] - changed_at) < 60,
          primary["points"][-4:])
    check("no climate service call", not climate_calls(args), climate_calls(args)[:3])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--phase", default="all", choices=["all", "restart-check", "app-outage"])
    args = parser.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(10)
    try:
        {"all": phase_all, "restart-check": phase_restart_check, "app-outage": phase_app_outage}[args.phase](args, out)
    except SystemExit:
        pass
    except Exception as err:  # noqa: BLE001
        check(f"phase {args.phase} crashed", False, f"{type(err).__name__}: {err}")
    finally:
        args.watcher.stop_flag = True
    (out / f"a7-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
