"""End-to-end checks of Phase A7.1 (Smart Comfort 0.11.0: thermal learning) with a real Home Assistant, the
companion 1.0.0 and the real App image.

    python -I a71_checks.py --ha-url http://127.0.0.1:18123 --ha-token <LLAT> --out <dir> --data-dir <App /data>
        [--chromium <path>] --phase all|restart-check

Runs after ``a7_checks.py --phase all`` in the same stack (Kamar Norður and Stova configured, the thermal zone
"Norður" sharing ``climate.heatpump``). Two weeks of history cannot be waited for, so ``all`` stops the App
container, adds 16 days of synthetic observations of a room with known behaviour (``tests/thermal_sim.py``; heating
1.6 °C/h, loss 0.04/h) for Kamar's *real* entities to the App's own observation store (only for days before today,
with the coverage and the gap up to the real observations recorded in ``state.json``), and starts it again.

``all``
    Learning status before and after; an administrator's rebuild through the API (the non-administrator refusal is
    covered by the unit tests: the fake Supervisor always sends the token owner's id);
    the learned heating response and accuracy against persistence; a room without enough data (Stova); the Overview
    cards and the Thermal learning page on desktop, tablet and phone (charts, parameters, evidence, model details);
    "Rebuild all" in the interface; opening types (a window set to be a door is named "Door open: …"); a changed
    primary sensor invalidates the model, changing it back makes it valid; the reset (confirmation, observations
    and configuration kept); thermostat states unchanged and no ``climate`` service call at all.
``restart-check``
    After the App container and Home Assistant were restarted: the same model (same training time), still valid,
    the page renders, still no thermostat command.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "backend"))
sys.path.insert(0, str(HERE.parent.parent))
from browser_checks import INGRESS, PANEL, ServiceCallWatcher, app_frame, check, no_horizontal_overflow, results  # noqa: E402
from rooms_checks import http  # noqa: E402
from system_checks import KAMAR, POST, app, app_post, ha, state, wait  # noqa: E402

from tests.thermal_sim import Scenario, Sim  # noqa: E402

STOVA = "stova"
KT = "sensor.kamar_temperature"
KS = "sensor.kamar_shelf_temperature"
WINDOW = "binary_sensor.window_stova"
STATE_FILE = "a71_e2e_state.json"
IMAGE = "local/smart_lighting:e2e"
SIZES = {"desktop": (1440, 900), "tablet": (1024, 768), "phone": (390, 844)}


def climate_calls(args: argparse.Namespace) -> list[dict[str, Any]]:
    return [e for e in args.watcher.events if e.get("domain") == "climate"]


def doc() -> dict[str, Any]:
    return app("api/climate")


def save_config(config: dict[str, Any]) -> tuple[int, Any]:
    return app_post("api/climate/save", {"config": config, "revision": doc()["revision"]})


def thermostat_states(args: argparse.Namespace) -> dict[str, Any]:
    out = {}
    for e in ("climate.hvac", "climate.heatpump", "climate.ecobee", "climate.stova_thermostat"):
        s = state(args, e) or {}
        a = s.get("attributes", {})
        out[e] = [s.get("state"), a.get("temperature"), a.get("target_temp_low"), a.get("target_temp_high")]
    return out


def learning(area: str) -> dict[str, Any]:
    return app(f"api/climate/rooms/{area}/learning")


def as_root(args: argparse.Namespace, script: str, *mounts: str) -> None:
    """Run a shell command as root in a throwaway container with the App's /data (the App writes it as root)."""
    vols = ["-v", f"{args.data_dir}:/data"]
    for m in mounts:
        vols += ["-v", m]
    subprocess.run(["docker", "run", "--rm", *vols, "--entrypoint", "sh", IMAGE, "-c", script], check=True,
                   capture_output=True)


def seed(args: argparse.Namespace, config: dict[str, Any]) -> dict[str, Any]:
    """16 days of synthetic observations for Kamar's configured entities, before today (UTC). The App is stopped."""
    room = config["rooms"][KAMAR]
    primary = room["primary_sensor"]["entity_id"]
    hvac = room["thermostats"][0]["entity_id"]
    shared = [s for z in config["zones"] if KAMAR in z["rooms"] for s in z["shared_thermostats"]]
    outdoor = config["outdoor"]["entity_id"]
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    start = midnight - 16 * 86400
    sc = Scenario(start=start, days=16, seed=11, outdoor_mean=3.0)
    sim = Sim(sc)
    sim.ids.update(room=primary, trv=hvac, outdoor=outdoor)
    sim.run()
    for e in shared:  # the zone's shared heat pump: off throughout (known not to heat)
        for k in range(int(16 * 24 / 2)):
            sim.records.append({"t": start + k * 7200 + 30, "e": e, "k": "thermostat", "s": "off", "u": "°C", "v": None,
                                "q": "ok", "src": "live", "a": {"cur": None, "tgt": None, "lo": None, "hi": None,
                                                                "action": None, "preset": None}})
    for o in room["openings"]:  # Kamar's door: closed throughout (one record; Home Assistant reports changes only)
        sim.records.append({"t": start + 10, "e": o["entity_id"], "k": "opening", "s": "off", "v": 0, "q": "ok",
                            "src": "live"})
    obs = Path(args.data_dir) / "climate" / "observations"
    existing = {p.name for p in obs.glob("obs-*.jsonl")}
    tmp = Path(tempfile.mkdtemp(prefix="a71seed"))
    sim.write(tmp)
    written = sorted(p.name for p in tmp.glob("obs-*.jsonl"))
    clash = [n for n in written if n in existing]
    check("seed: only days the App has not observed", not clash, clash)
    real = json.loads((obs / "state.json").read_text())
    entities = [primary, hvac, *shared, outdoor, *(o["entity_id"] for o in room["openings"])]
    for e in entities:
        span = real["coverage"].get(e)
        real_from = span["from"] if span else time.time()
        real["coverage"][e] = {"from": start, "to": span["to"] if span else real_from}
        if real_from - midnight > 120:
            real.setdefault("gaps", {}).setdefault(e, []).insert(0, [midnight, real_from])
    (tmp / "state.json").write_text(json.dumps(real))
    for n in clash:
        (tmp / n).unlink()
    as_root(args, "cp /seed/obs-*.jsonl /data/climate/observations/ && cp /seed/state.json /data/climate/observations/"
                  "state.json && chmod 644 /data/climate/observations/*", f"{tmp}:/seed:ro")
    return {"start": start, "end": midnight, "files": len(written) - len(clash), "entities": entities}


def browser(args: argparse.Namespace, out: Path, *, name: str, steps: Any) -> None:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=args.chromium)
        for size, (w, h) in SIZES.items():
            page = b.new_page(viewport={"width": w, "height": h})
            errors: list[str] = []
            page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
            page.goto(PANEL)
            frame = app_frame(page)
            steps(page, frame, size, out / "screenshots", errors)
            real = [e for e in errors if e.strip() not in ("Object", "")]
            check(f"{name} [{size}]: no page errors", not real, real[:3])
            page.close()
        b.close()


# ------------------------------------------------------------------ phases
def phase_all(args: argparse.Namespace, out: Path) -> None:
    from playwright.sync_api import expect  # noqa: PLC0415

    before = thermostat_states(args)
    ov = app("api/overview")
    check("version 0.11.0", ov["app"]["version"] == "0.11.0", ov["app"]["version"])
    lo = app("api/climate/learning")
    check("learning: every climate room has a status", set(lo["rooms"]) == {KAMAR, STOVA}
          and all(r["status"] in lo["statuses"] for r in lo["rooms"].values()), {k: v["status"] for k, v in lo["rooms"].items()})
    check("learning: nothing learned yet on a new installation", lo["store"]["models"] == 0
          and lo["rooms"][KAMAR]["status"] in ("no_data", "collecting"), lo["rooms"][KAMAR]["status"])

    config = doc()["config"]
    subprocess.run(["docker", "stop", "sl-test-app"], check=True, capture_output=True)
    seeded = seed(args, config)
    check("seed: 16 days of observations added", seeded["files"] >= 15, seeded)
    subprocess.run(["docker", "start", "sl-test-app"], check=True, capture_output=True)
    wait("the App back", lambda: doc(), 120, 2)
    wait("the Recorder gap fill", lambda: doc()["status"]["observation"]["backfill"]["state"] in ("done", "unavailable"),
         180, 2)

    status, body = app_post("api/climate/learning/rebuild", {"area_id": KAMAR})
    check("rebuild queued by an administrator", status == 200 and body["queued"] == [KAMAR], body)
    detail = wait("Kamar's model", lambda: (d := learning(KAMAR))["model"] is not None and not d["summary"]["running"] and d,
                  240, 2)
    model, summary = detail["model"], detail["summary"]
    check("model learned with a confidence", model["status"] in ("low", "moderate", "high") and not model["preliminary"],
          (model["status"], model["evidence"]["usable_days"]))
    check("usable history counted (≥ 14 days)", model["evidence"]["usable_days"] >= 14, model["evidence"]["usable_days"])
    heat = summary["heating"]
    # The input is the share of the room's heating devices that report heating: the TRV of the seeded history plus
    # the zone's shared heat pump (off) -> the TRV alone is half of "full heating" (1.6 °C/h each half).
    devices = len(model["parameters"]["heating_c_per_h"].get("devices") or []) or 1
    check("heating response learned from reported heating (≈1.6 °C/h per device)", heat["basis"] == "reported"
          and abs(heat["value"] - 1.6 * devices) / (1.6 * devices) < 0.15 and f"all {devices}" in
          model["parameters"]["heating_c_per_h"]["text"], (heat, devices))
    check("loss coefficient ≈0.04/h", abs(model["parameters"]["k_per_h"]["value"] - 0.04) / 0.04 < 0.35,
          model["parameters"]["k_per_h"])
    acc = summary["accuracy"]
    check("held-out accuracy beats persistence", acc["mae"] < 0.5 * acc["persistence_mae"], acc)
    check("explanations from learned values", any("°C/hour" in e for e in model["explanations"]), model["explanations"][:3])
    check("solar not manufactured (no window geometry)", not model["parameters"]["solar_c_per_h"]["available"]
          and "direction" in model["parameters"]["solar_c_per_h"]["reason"], model["parameters"]["solar_c_per_h"])
    check("model valid for the current configuration", summary["validity"]["state"] == "valid", summary["validity"])
    files = sorted(p.name for p in (Path(args.data_dir) / "climate" / "models").glob("*.json"))
    check("model stored as /data/climate/models/<area>.json", f"{KAMAR}.json" in files, files)

    status, body = app_post("api/climate/learning/rebuild", {"area_id": STOVA})
    st = wait("Stova's attempt", lambda: (d := learning(STOVA))["attempt"].get("finished_at") and d, 120, 2)
    check("a room without enough history: no model, status says why", st["model"] is None
          and st["summary"]["status"] in ("collecting", "insufficient"), (st["summary"]["status"], st["attempt"].get("diagnostic", {}).get("reason")))
    exp = http(INGRESS + f"api/climate/rooms/{KAMAR}/learning/export", POST)
    check("export: model and diagnostics", exp["format"] == "smart_comfort.thermal_model_export"
          and exp["model"]["area_id"] == KAMAR and "room_config" in exp, list(exp)[:8])

    # Opening types: the window is set to be a door; Climate names it so.
    config = doc()["config"]
    config["rooms"][STOVA]["openings"] = [{"entity_id": WINDOW, "kind": "door"}]
    status, _ = save_config(config)
    check("opening type saved", status == 200)
    room = app(f"api/climate/rooms/{STOVA}")
    check("opening named by the type a person set", room["openings"][0]["kind"] == "door"
          and room["openings"][0]["kind_source"] == "configured", room["openings"][0])
    current = state(args, WINDOW) or {}
    ha(args, f"/api/states/{WINDOW}", {"state": "on", "attributes": current.get("attributes", {})})
    wait("the open door", lambda: app(f"api/climate/rooms/{STOVA}")["any_open"], 30)

    def ui(page: Any, frame: Any, size: str, shots: Path, errors: list[str]) -> None:
        frame.goto(INGRESS + "#/climate")
        card = frame.locator(f"[data-testid=climate-room-card][data-area={KAMAR}] [data-testid=climate-room-learning]")
        expect(card).to_be_visible(timeout=20000)
        text = card.text_content() or ""
        check(f"overview [{size}]: Kamar's learning summary", "Heating" in text and "Confidence" in text
              and "persistence" in text, text[:200])
        chip = frame.locator(f"[data-testid=climate-room-card][data-area={STOVA}] [data-testid=climate-open-chip]")
        expect(chip).to_be_visible()
        check(f"overview [{size}]: the open door is named", (chip.text_content() or "").strip().startswith("Door open:"),
              chip.text_content())
        page.screenshot(path=str(shots / f"a71-overview-{size}.png"), full_page=True)
        check(f"overview [{size}]: no horizontal overflow", no_horizontal_overflow(frame))
        frame.locator(f"[data-testid=climate-room-card][data-area={KAMAR}] [data-testid=climate-learning-link]").click()
        expect(frame.locator("[data-testid=learning-parameters]")).to_be_visible(timeout=20000)
        rows = frame.locator("[data-testid=learning-parameters] tr[data-param]").count()
        heating_row = frame.locator("[data-testid=learning-parameters] tr[data-param=heating_c_per_h]").text_content() or ""
        check(f"learning page [{size}]: parameters with confidence", rows >= 6 and "°C/h" in heating_row, (rows, heating_row[:120]))
        expect(frame.locator("[data-testid=learning-accuracy] [data-testid=climate-history-chart] path.cl-line-model")).to_have_count(1)
        d = frame.locator("[data-testid=learning-accuracy] path.cl-line-model").first.get_attribute("d") or ""
        check(f"learning page [{size}]: reconstruction drawn, broken into paths", d.count("M") >= 3, d[:80])
        expect(frame.locator("[data-testid=climate-error-chart]")).to_be_visible()
        metrics = frame.locator("[data-testid=learning-metrics]").text_content() or ""
        check(f"learning page [{size}]: MAE/RMSE and the baseline", "1 h" in metrics and "3 h" in metrics, metrics[:160])
        expect(frame.locator("[data-testid=learning-evidence]")).to_be_visible()
        expect(frame.locator("[data-testid=learning-measurements] [data-testid=climate-history-chart]")).to_be_visible(timeout=20000)
        if size == "desktop":
            frame.locator("[data-testid=learning-details]").click()
            expect(frame.locator("[data-testid=learning-json]")).to_contain_text("smart_comfort.thermal_model")
            frame.locator("[data-testid=learning-details]").click()
        page.screenshot(path=str(shots / f"a71-learning-{size}.png"), full_page=True)
        check(f"learning page [{size}]: no horizontal overflow", no_horizontal_overflow(frame))
        frame.goto(INGRESS + f"#/climate/rooms/{STOVA}/learning")
        expect(frame.locator("[data-testid=learning-status]")).to_be_visible(timeout=20000)
        st_text = frame.locator("[data-testid=learning-status]").text_content() or ""
        check(f"Stova's page [{size}]: why there is no model yet", "observed" in st_text, st_text[:200])
        if size == "desktop":
            page.screenshot(path=str(shots / "a71-learning-stova.png"), full_page=True)

    browser(args, out, name="learning UI", steps=ui)
    ha(args, f"/api/states/{WINDOW}", {"state": "off", "attributes": current.get("attributes", {})})
    config = doc()["config"]
    config["rooms"][STOVA]["openings"] = [{"entity_id": WINDOW, "kind": None}]
    save_config(config)

    # A different primary sensor invalidates the model; changing it back makes it valid again.
    trained = learning(KAMAR)["model"]["trained_at"]
    config = doc()["config"]
    config["rooms"][KAMAR]["primary_sensor"] = {"entity_id": KS}
    config["rooms"][KAMAR]["secondary_sensors"] = []
    status, _ = save_config(config)
    s = learning(KAMAR)["summary"]
    check("changed primary sensor: model invalidated with the reason", status == 200 and s["validity"]["state"] == "invalidated"
          and "primary" in " ".join(s["validity"]["reasons"]), s["validity"])

    def invalid_ui(page: Any, frame: Any, size: str, shots: Path, errors: list[str]) -> None:
        if size != "desktop":
            return
        frame.goto(INGRESS + f"#/climate/rooms/{KAMAR}/learning")
        expect(frame.locator("[data-testid=learning-invalidated]")).to_be_visible(timeout=20000)
        page.screenshot(path=str(shots / "a71-learning-invalidated.png"), full_page=True)

    browser(args, out, name="invalidated model", steps=invalid_ui)
    config = doc()["config"]
    config["rooms"][KAMAR]["primary_sensor"] = {"entity_id": KT}
    config["rooms"][KAMAR]["secondary_sensors"] = [{"entity_id": KS}]
    save_config(config)
    s = learning(KAMAR)
    check("primary sensor back: the same model is valid again", s["summary"]["validity"]["state"] == "valid"
          and s["model"]["trained_at"] == trained, s["summary"]["validity"])

    # "Rebuild all" in the interface, then a reset with its confirmation.
    obs_before = sorted(p.name for p in (Path(args.data_dir) / "climate" / "observations").glob("obs-*.jsonl"))

    def rebuild_reset(page: Any, frame: Any, size: str, shots: Path, errors: list[str]) -> None:
        if size != "desktop":
            return
        frame.goto(INGRESS + "#/climate")
        frame.locator("[data-testid=climate-rebuild-all]").click()
        wait("rebuild all finished", lambda: (o := app("api/climate/learning"))["running"] is None and not o["queued"]
             and learning(KAMAR)["model"]["trained_at"] != trained, 240, 2)
        frame.goto(INGRESS + f"#/climate/rooms/{KAMAR}/learning")
        expect(frame.locator("[data-testid=learning-reset]")).to_be_enabled(timeout=20000)
        page.once("dialog", lambda d: d.accept())
        frame.locator("[data-testid=learning-reset]").click()
        wait("the model reset", lambda: learning(KAMAR)["model"] is None, 30)
        page.screenshot(path=str(shots / "a71-learning-reset.png"), full_page=True)

    browser(args, out, name="rebuild and reset", steps=rebuild_reset)
    status, body = app_post("api/climate/learning/reset", {"area_id": KAMAR})
    check("a reset without confirmation is refused", status == 409 and body["code"] == "confirm_required", body)
    obs_after = sorted(p.name for p in (Path(args.data_dir) / "climate" / "observations").glob("obs-*.jsonl"))
    check("reset: observations kept", set(obs_before) <= set(obs_after), (len(obs_before), len(obs_after)))
    check("reset: configuration kept", KAMAR in doc()["config"]["rooms"])
    archive = list((Path(args.data_dir) / "climate" / "models" / "archive").glob(f"{KAMAR}@*-reset.json"))
    check("reset: the model archived, not deleted", bool(archive), archive)
    app_post("api/climate/learning/rebuild", {"area_id": KAMAR})
    final = wait("Kamar's model again", lambda: (d := learning(KAMAR))["model"] is not None and not d["summary"]["running"] and d,
                 240, 2)
    (out / STATE_FILE).write_text(json.dumps({"trained_at": final["model"]["trained_at"], "thermostats": before,
                                              "status": final["model"]["status"]}))
    check("thermostats unchanged", thermostat_states(args) == before, thermostat_states(args))
    check("no climate service call", not climate_calls(args), climate_calls(args)[:3])


def phase_restart_check(args: argparse.Namespace, out: Path) -> None:
    from playwright.sync_api import expect  # noqa: PLC0415

    saved = json.loads((out / STATE_FILE).read_text())
    wait("the App", lambda: doc(), 120, 2)
    d = learning(KAMAR)
    check("restart: the same model", d["model"] is not None and d["model"]["trained_at"] == saved["trained_at"],
          (d["model"] or {}).get("trained_at"))
    check("restart: still valid, same status", d["summary"]["validity"]["state"] == "valid"
          and d["model"]["status"] == saved["status"], d["summary"]["validity"])

    def ui(page: Any, frame: Any, size: str, shots: Path, errors: list[str]) -> None:
        if size != "desktop":
            return
        frame.goto(INGRESS + f"#/climate/rooms/{KAMAR}/learning")
        expect(frame.locator("[data-testid=learning-parameters]")).to_be_visible(timeout=20000)
        page.screenshot(path=str(shots / "a71-learning-after-restart.png"), full_page=True)

    browser(args, out, name="after restart", steps=ui)
    check("thermostats unchanged", thermostat_states(args) == saved["thermostats"], thermostat_states(args))
    check("no climate service call", not climate_calls(args), climate_calls(args)[:3])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--chromium", default=None)
    parser.add_argument("--phase", default="all", choices=["all", "restart-check"])
    args = parser.parse_args()
    out = Path(args.out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    args.watcher = ServiceCallWatcher(args.ha_url, args.ha_token)
    args.watcher.start()
    args.watcher.ready.wait(10)
    try:
        {"all": phase_all, "restart-check": phase_restart_check}[args.phase](args, out)
    except SystemExit:
        pass
    except Exception as err:  # noqa: BLE001
        check(f"phase {args.phase} crashed", False, f"{type(err).__name__}: {err}")
    finally:
        args.watcher.stop_flag = True
    (out / f"a71-{args.phase}.json").write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str))
    failed = [r for r in results if not r["ok"]]
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
