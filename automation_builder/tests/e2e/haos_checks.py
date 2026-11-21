"""Checks that only a genuine Home Assistant OS can answer: the Supervisor, real Ingress, restarts, backups, updates.

    python haos_checks.py --phase before-update | after-update | main [--state state.json] [--out results.json]

Environment as for ``haos.py`` (AB_HAOS_URL, AB_HAOS_TOKENS, AB_HAOS_SLUG). Phases:

* ``before-update`` on an installed **0.1.0**: creates a device with rules and deploys it (stored in ``--state``);
* ``after-update`` once the Supervisor updated the App to 0.2.0: the same device must be there, deployed and
  unchanged, and the new functions must work on it;
* ``main``: installation facts, Ingress, the Supervisor's Core proxy, create / update / remove of managed
  automations, an automation reload, restarts of the App, of Core and of the Supervisor, a Supervisor backup
  and restore of the App, and a deployment that is interrupted by stopping the App.

Run ``e2e_checks.py`` and ``e2e_02_checks.py`` with the variables of ``haos.py env`` for everything else.
It needs the test entities of ``ha/ab_test_package.yaml`` in the test Home Assistant.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import threading
import time
from typing import Any
import urllib.error
import urllib.request

import haos

OFFICE = "light.office_rgbw_lights"
MOTION = "binary_sensor.test_motion"
LUX = "sensor.test_lux"
UNRELATED = ("1700000000001", "1700000000002", "1700000000003", "1700000000004")
results: list[dict[str, Any]] = []


def check(name: str, ok: Any, detail: Any = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:400]}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def note(name: str, detail: Any) -> None:
    results.append({"check": name, "ok": None, "detail": str(detail)[:500]})
    print(f"NOTE {name}  [{str(detail)[:400]}]", flush=True)


class App:
    """The App through real Ingress (a browser session of the test administrator)."""

    def __init__(self) -> None:
        self.base, self.cookie = haos.ingress()
        self.at = time.monotonic()
        self.revision: str | None = None

    def call(self, path: str, body: Any = None, cookie: bool = True, csrf: bool = True) -> tuple[int, Any]:
        if time.monotonic() - self.at > 240:
            self.base, self.cookie = haos.ingress()
            self.at = time.monotonic()
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = self.cookie
        if body is not None and csrf:
            headers["X-AB"] = "1"
        request = urllib.request.Request(f"{self.base}/{path}", data=json.dumps(body).encode() if body is not None else None,
                                         method="POST" if body is not None else "GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                raw, status = response.read(), response.status
        except urllib.error.HTTPError as err:
            raw, status = err.read(), err.code
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = raw.decode(errors="replace")
        if status == 200 and isinstance(data, dict) and isinstance(data.get("revision"), str):
            self.revision = data["revision"]
        return status, data

    def change(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return self.call(path, {**body, "expected_revision": self.revision})

    def overview(self) -> dict[str, Any]:
        return self.call("api/overview")[1]

    def device(self, device_id: str) -> dict[str, Any]:
        return self.call(f"api/devices/{device_id}")[1]

    def deploy(self, device_id: str) -> tuple[int, Any]:
        _, plan = self.call("api/devices/plan", {"device_id": device_id})
        return self.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan.get("plan_hash")})

    def wait_sync(self, device_id: str, wanted: tuple[str, ...] = ("in_sync",), timeout: float = 180) -> str:
        deadline = time.monotonic() + timeout
        status = "?"
        while time.monotonic() < deadline:
            try:
                view = self.device(device_id)
                status = view["live"]["status"] if isinstance(view, dict) and "live" in view else "?"
                if status in wanted:
                    return status
            except Exception as err:  # noqa: BLE001 - the App or Core is restarting
                status = type(err).__name__
            time.sleep(3)
        return status


def state(entity_id: str) -> dict[str, Any]:
    status, data = haos.http(f"/api/states/{entity_id}")
    return data if status == 200 else {"state": None, "attributes": {}}


def service(action: str, entity_id: str, **data: Any) -> None:
    domain, name = action.split(".")
    status, _ = haos.http(f"/api/services/{domain}/{name}", "POST", {"entity_id": entity_id, **data})
    assert status == 200, (action, status)


def wait_state(entity_id: str, wanted: str, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state(entity_id)["state"] == wanted:
            return True
        time.sleep(0.3)
    return False


def automation(automation_id: str) -> Any:
    return haos.http(f"/api/config/automation/config/{automation_id}")[1]


def managed() -> list[str]:
    return sorted(str(s["attributes"].get("id")) for s in haos.http("/api/states")[1]
                  if s["entity_id"].startswith("automation.") and str(s["attributes"].get("id", "")).startswith("automation_builder_"))


RULE = {"name": "Motion when dark", "enabled": True,
        "triggers": [{"type": "state", "entity_id": MOTION, "to": "on", "from": None, "for_seconds": 0}],
        "conditions": {"type": "and", "items": [{"type": "numeric", "entity_id": LUX, "below": 100, "above": None}]},
        "actions": [{"type": "service", "action": "light.turn_on", "target": "all", "data": {"brightness_pct": 70}}]}
DELAY_RULE = {"name": "Off after a while", "enabled": True, "retrigger": "parallel",
              "triggers": [{"type": "state", "entity_id": MOTION, "to": "off", "from": None, "for_seconds": 0}],
              "conditions": {"type": "and", "items": []},
              "actions": [{"type": "delay", "seconds": 3},
                          {"type": "service", "action": "light.turn_off", "target": "all", "data": {}}]}


def runs(device_lamp: str) -> bool:
    """Does the deployed rule work in Home Assistant? (motion while dark turns the lamp on at 70 %)"""
    service("light.turn_off", device_lamp)
    service("input_boolean.turn_off", "input_boolean.test_motion")
    service("input_number.set_value", "input_number.test_lux", value=40)
    time.sleep(1.5)
    service("input_boolean.turn_on", "input_boolean.test_motion")
    ok = wait_state(device_lamp, "on", 8) and round((state(device_lamp)["attributes"].get("brightness") or 0) / 255 * 100) == 70
    service("input_boolean.turn_off", "input_boolean.test_motion")
    return ok


def before_update(app: App, path: Path) -> None:
    info = haos.sup(f"/addons/{haos.SLUG}/info")
    check("U1 version 0.1.0 is installed and running", info["version"] == "0.1.0" and info["state"] == "started", (info["version"], info["state"]))
    app.overview()  # the revision
    status, created = app.change("api/devices/create", {"name": "Upgrade lamp", "targets": [OFFICE]})
    device_id = created["device_id"]
    for rule in (RULE, DELAY_RULE):
        status, saved = app.change("api/rules/save", {"device_id": device_id, "rule": rule})
        assert status == 200, saved
    status, deployed = app.deploy(device_id)
    check("U1 a device with an instant and a waiting rule is deployed by 0.1.0", status == 200 and deployed["deployed"], deployed)
    view = app.device(device_id)
    check("U1 0.1.0: one shared automation (parallel, as 0.1 did for this waiting rule)", [a["mode"] for a in view["live"]["automations"]] == ["parallel"], view["live"]["automations"])
    check("U1 0.1.0: it runs in Home Assistant", runs(OFFICE))
    status, second = app.change("api/devices/create", {"name": "Upgrade plain", "targets": ["light.kitchen_lights"]})
    app.change("api/rules/save", {"device_id": second["device_id"], "rule": {**RULE, "name": "Plain"}})
    status, deployed = app.deploy(second["device_id"])
    check("U1 a second device with only instant rules is deployed", status == 200, deployed)
    path.write_text(json.dumps({"device": device_id, "plain": second["device_id"],
                                "automations": {i: automation(i) for i in managed()},
                                "rules": app.device(device_id)["device"]["rules"]}))


def after_update(app: App, path: Path) -> None:
    saved = json.loads(path.read_text())
    info = haos.sup(f"/addons/{haos.SLUG}/info")
    check("U2 the Supervisor updated the App to 0.2.0 and it runs", info["version"] == "0.2.0" and info["state"] == "started" and not info["update_available"], (info["version"], info["state"]))
    overview = app.overview()
    check("U2 the App reports 0.2.0 and is connected", overview["app"]["version"] == "0.2.0" and overview["connection"]["home_assistant"]["connected"], overview["app"])
    check("U2 the devices and rules of 0.1.0 are there", {d["id"] for d in overview["devices"]} >= {saved["device"], saved["plain"]}
          and [r["name"] for r in app.device(saved["device"])["device"]["rules"]] == [r["name"] for r in saved["rules"]])
    check("U2 the update itself changed nothing in Home Assistant", {i: automation(i) for i in managed()} == saved["automations"])
    plain = app.device(saved["plain"])
    check("U2 a device with instant rules is in sync without a new deployment (same automation, byte for byte)",
          plain["live"]["status"] == "in_sync", plain["live"]["status"])
    view = app.device(saved["device"])
    check("U2 the waiting rule now wants its own automation: shown as pending, with the reason, nothing changed silently",
          view["live"]["status"] == "changes_pending" and any(n["code"] == "separate_automation" for n in view["notes"]), (view["live"]["status"], view["notes"]))
    check("U2 the automation deployed by 0.1.0 keeps running until then", runs(OFFICE))
    status, deployed = app.deploy(saved["device"])
    view = app.device(saved["device"])
    check("U2 deploying applies the new layout: a queued shared automation and the rule's own",
          status == 200 and sorted(a["mode"] for a in view["live"]["automations"]) == ["parallel", "queued"] and view["live"]["status"] == "in_sync", view["live"]["automations"])
    check("U2 ...and it runs", runs(OFFICE))
    diag = app.device(saved["device"])["rule_diagnostics"]
    check("U2 the records of 0.1.0 work with the new rule diagnostics", all(d["sync"]["status"] == "in_sync" for d in diag.values()), {k: v["sync"] for k, v in diag.items()})
    status, blueprint = app.call(f"api/blueprint/{saved['plain']}")
    check("U2 a device made in 0.1.0 can be exported as a blueprint", status == 200 and blueprint["valid"] and len(blueprint["inputs"]) >= 4, blueprint.get("validation"))
    _, settings = app.call("api/settings")
    check("U2 the automatic backups of 0.1.0 are still listed", len(settings["backups"]) >= 4, len(settings["backups"]))
    for device_id in (saved["device"], saved["plain"]):
        status, removed = app.change("api/devices/delete", {"device_id": device_id, "overwrite": False})
        check(f"U2 clean-up: device {device_id} and its automations are removed", status == 200, removed)
    check("U2 nothing of the upgrade test is left in Home Assistant", managed() == [], managed())


def main_phase(app: App) -> None:  # noqa: PLR0915 - one linear scenario
    unrelated = {i: automation(i) for i in UNRELATED}
    info = haos.sup(f"/addons/{haos.SLUG}/info")
    host = haos.sup("/info")
    check("H1 genuine Home Assistant OS with a Supervisor", str(host.get("operating_system", "")).startswith("Home Assistant OS") and host.get("supervisor"), {k: host.get(k) for k in ("operating_system", "supervisor", "homeassistant")})
    check("H1 the App is installed, started and current", info["state"] == "started" and info["version"] == "0.2.0", (info["state"], info["version"]))
    check("H1 Ingress only: no host port, a sidebar panel for administrators", info["ingress"] and not info.get("network") and info.get("ingress_panel") is not None, {k: info.get(k) for k in ("ingress", "network", "ingress_panel")})
    check("H1 permissions: Home Assistant API only", info["homeassistant_api"] is True and info["hassio_api"] is False and info["auth_api"] is False
          and info["host_network"] is False and info.get("full_access") is False and not info.get("privileged") and info.get("docker_api") is False
          and info.get("protected") is True, {k: info.get(k) for k in ("homeassistant_api", "hassio_api", "auth_api", "host_network", "full_access", "privileged", "docker_api", "protected", "hassio_role", "rating")})
    note("H1 Supervisor's security rating of the App", info.get("rating"))
    # Ingress authentication
    status, _ = app.call("api/overview", cookie=False)
    check("H2 Ingress without a Home Assistant session is refused", status in (401, 403), status)
    overview = app.overview()
    check("H2 with a session: the administrator is recognised through Ingress", overview["user"]["is_admin"] is True, overview["user"])
    status, page = app.call("")
    check("H2 the interface is served below the Ingress path", status == 200 and f'<base href="{info["ingress_url"].rstrip("/")}/">' in page, str(page)[:200])
    for asset in ("assets/app.js", "assets/pages.js", "assets/app.css"):
        status, _ = app.call(asset)
        check(f"H2 {asset} loads through Ingress", status == 200, status)
    status, refused = app.call("api/devices/plan", {"device_id": "d_00000000"}, csrf=False)
    check("H2 a request without the App's header is refused", status == 403, refused)
    # Supervisor proxy
    ha = overview["connection"]["home_assistant"]
    core = haos.sup("/core/info")
    check("H3 connected to Core through the Supervisor's websocket proxy", ha["connected"] and ha["ha_version"] == core["version"], (ha, core["version"]))
    _, settings = app.call("api/settings")
    check("H3 it uses the Supervisor's addresses and no other", settings["mechanism"]["websocket"] == "ws://supervisor/core/websocket"
          and settings["mechanism"]["api"] == "http://supervisor/core/api", settings["mechanism"])
    _, found = app.call("api/entities?purpose=target&q=office")
    check("H3 entity discovery through the proxy", OFFICE in [e["entity_id"] for e in found["results"]], found)
    _, devices = app.call("api/ha-devices?q=office")
    check("H3 device discovery through the proxy", any(OFFICE in [e["entity_id"] for e in d["entities"]] for d in devices["devices"]), devices)

    # create, update, remove
    before = managed()
    status, created = app.change("api/devices/create", {"name": "HAOS lamp", "targets": [OFFICE]})
    device_id = created["device_id"]
    automation_id = f"automation_builder_{device_id}"
    status, saved = app.change("api/rules/save", {"device_id": device_id, "rule": RULE})
    rule_id = saved["rule_id"]
    status, plan = app.call("api/devices/plan", {"device_id": device_id})
    check("H4 Home Assistant validated the automation through the proxy (validate_config)", status == 200 and plan["can_deploy"] and not plan["items"][0]["errors"], plan.get("reasons"))
    status, deployed = app.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan["plan_hash"]})
    check("H4 create: the automation configuration endpoint accepts the App's request through the real Supervisor proxy", status == 200 and deployed["deployed"], deployed)
    in_ha = automation(automation_id)
    check("H4 ...the automation is in automations.yaml and loaded", in_ha["alias"] == "HAOS lamp (Automation Builder)" and managed() == sorted([*before, automation_id]), managed())
    check("H4 ...and runs", runs(OFFICE))
    rule = next(r for r in app.device(device_id)["device"]["rules"] if r["id"] == rule_id)
    rule["actions"][0]["data"]["brightness_pct"] = 40
    app.change("api/rules/save", {"device_id": device_id, "rule": rule})
    status, _ = app.deploy(device_id)
    check("H4 update: the new version replaces the old one", status == 200 and automation(automation_id)["actions"][0]["then"][0]["data"]["brightness_pct"] == 40)
    rule["actions"][0]["data"]["brightness_pct"] = 70
    app.change("api/rules/save", {"device_id": device_id, "rule": rule})
    app.deploy(device_id)
    good = automation(automation_id)
    broken = {**RULE, "name": "Broken", "actions": [{"type": "service", "action": "light.turn_on", "target": "all", "data": {"brightness_pct": "{{ states( }}"}}]}
    status, bad = app.change("api/rules/save", {"device_id": device_id, "rule": broken})
    status, plan = app.call("api/devices/plan", {"device_id": device_id})
    status, refused = app.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan["plan_hash"]})
    check("H4 Home Assistant rejects a broken automation: reported, nothing written", not plan["can_deploy"] and plan["items"][0]["errors"] and status == 409
          and automation(automation_id) == good, plan["items"][0]["errors"])
    app.change("api/rules/delete", {"device_id": device_id, "rule_id": bad["rule_id"]})
    check("H4 ...and the device is in sync again without a deployment", app.wait_sync(device_id, timeout=10) == "in_sync")
    # an automation reload by the user
    haos.http("/api/services/automation/reload", "POST", {})
    time.sleep(4)
    check("H5 after “reload automations” the managed automation is loaded and in sync", automation_id in managed() and app.wait_sync(device_id, timeout=30) == "in_sync" and runs(OFFICE))
    # an edit outside
    edited = {**{k: v for k, v in automation(automation_id).items() if k != "id"}, "alias": "Edited by hand"}
    haos.http(f"/api/config/automation/config/{automation_id}", "POST", edited)
    check("H5 an edit in Home Assistant's own editor is detected", app.wait_sync(device_id, ("modified",), 30) == "modified")
    status, refused = app.deploy(device_id)
    check("H5 ...and not overwritten without a yes", status == 409 and refused["error"] == "external_change", refused)
    _, plan = app.call("api/devices/plan", {"device_id": device_id})
    status, _ = app.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan["plan_hash"], "overwrite": True})
    check("H5 ...with a yes it is replaced", status == 200 and app.wait_sync(device_id, timeout=20) == "in_sync")

    # restarts
    haos.app("restart")
    check("H6 App restart: it comes back through Ingress", haos.app_ready())
    app = App()
    check("H6 ...with its rules and records: still in sync", app.wait_sync(device_id) == "in_sync" and runs(OFFICE))
    haos.core("restart")
    check("H7 Core restart: Home Assistant runs again", haos.core_running())
    check("H7 ...the App reconnects by itself", haos.app_ready())
    app = App()
    check("H7 ...and the automation is loaded, in sync and runs", app.wait_sync(device_id) == "in_sync" and automation_id in managed() and runs(OFFICE))
    reconnects = app.overview()["connection"]["home_assistant"]["reconnects"]
    check("H7 the App noticed the interruption (it reconnected)", reconnects >= 1, reconnects)
    haos.supervisor("restart")
    check("H8 Supervisor restart: Core and the App keep running or come back", haos.wait_core(300) and haos.app_ready(300))
    app = App()
    check("H8 ...still in sync, and a deployment works afterwards", app.wait_sync(device_id) == "in_sync")
    rule["actions"][0]["data"]["brightness_pct"] = 55
    app.change("api/rules/save", {"device_id": device_id, "rule": rule})
    status, _ = app.deploy(device_id)
    check("H8 ...a deployment through the restarted Supervisor's proxy", status == 200 and automation(automation_id)["actions"][0]["then"][0]["data"]["brightness_pct"] == 55)
    rule["actions"][0]["data"]["brightness_pct"] = 70
    app.change("api/rules/save", {"device_id": device_id, "rule": rule})
    app.deploy(device_id)

    # Supervisor backup and restore of the App
    backup = haos.sup("/backups/new/partial", "post", {"name": "ab-e2e", "addons": [haos.SLUG]}, 600)
    slug = backup["slug"]
    check("H9 the Supervisor made a backup of the App (hot, while it runs)", bool(slug), backup)
    status, extra = app.change("api/rules/save", {"device_id": device_id, "rule": {**RULE, "name": "Added after the backup"}})
    app.change("api/devices/update", {"device_id": device_id, "patch": {"name": "Renamed after the backup"}})
    check("H9 changes after the backup", len(app.device(device_id)["device"]["rules"]) == 2)
    haos.sup(f"/backups/{slug}/restore/partial", "post", {"addons": [haos.SLUG]}, 900)
    check("H9 restore: the App is back", haos.app_ready())
    app = App()
    view = app.device(device_id)
    check("H9 ...with the rules, the name and the deployment records of the backup", view["device"]["name"] == "HAOS lamp" and len(view["device"]["rules"]) == 1
          and view["live"]["status"] == "in_sync" and view["live"]["automations"][0]["deployed_at"], (view["device"]["name"], view["live"]["status"]))
    check("H9 ...and Home Assistant's automation was not touched by the restore", automation(automation_id)["alias"] == "HAOS lamp (Automation Builder)" and runs(OFFICE))
    haos.sup(f"/backups/{slug}", "delete")

    # a deployment interrupted by stopping the App
    rule = next(r for r in view["device"]["rules"])
    rule["actions"][0]["data"]["brightness_pct"] = 33
    app.change("api/rules/save", {"device_id": device_id, "rule": rule})
    _, plan = app.call("api/devices/plan", {"device_id": device_id})
    outcome: dict[str, Any] = {}

    def deploy() -> None:
        try:
            outcome["answer"] = app.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan["plan_hash"]})
        except Exception as err:  # noqa: BLE001 - the App went away under the request
            outcome["answer"] = (0, type(err).__name__)

    thread = threading.Thread(target=deploy, daemon=True)
    thread.start()
    time.sleep(0.15)
    haos.app("stop")
    thread.join(60)
    written = automation(automation_id)["actions"][0]["then"][0]["data"]["brightness_pct"] == 33
    haos.app("start")
    check("H10 the App was stopped during a deployment and starts again", haos.app_ready())
    app = App()
    status_now = app.wait_sync(device_id, ("in_sync", "changes_pending"), 60)
    view = app.device(device_id)
    note("H10 what the interruption hit", f"request answered: {outcome.get('answer', ('none',))[0]}; automation written in Home Assistant: {written}; "
         f"status after restart: {status_now}; message: {(view.get('last_result') or {}).get('message')}")
    check("H10 afterwards the App's view matches Home Assistant: in sync if the write went through, pending if not - never “changed outside”",
          status_now == ("in_sync" if written else "changes_pending"), (status_now, written))
    if status_now != "in_sync":
        status, _ = app.deploy(device_id)
        check("H10 ...and the pending change deploys normally", status == 200 and app.wait_sync(device_id, timeout=20) == "in_sync")
    check("H10 the automation runs with the new value", automation(automation_id)["actions"][0]["then"][0]["data"]["brightness_pct"] == 33)

    # remove
    status, removed = app.change("api/devices/delete", {"device_id": device_id, "overwrite": False})
    check("H11 remove: deleting the device removes its automation from Home Assistant", status == 200 and removed["removed_automations"] == 1 and managed() == before, managed())
    check("H11 the unrelated automations are exactly what they were", {i: automation(i) for i in UNRELATED} == unrelated)
    _, settings = app.call("api/settings")
    commands = set(settings["mechanism"]["commands"])
    check("H11 the App's own record of what it sent: no service call, no registry change", not commands & {"call_service", "fire_event", "config/entity_registry/update"}, commands)
    try:
        logs = haos.http(f"/api/hassio/addons/{haos.SLUG}/logs")[1]
        text = logs if isinstance(logs, str) else json.dumps(logs)
        check("H11 the App's log has no traceback", "Traceback" not in text, [line for line in text.splitlines() if "Traceback" in line or "ERROR" in line][:3])
    except Exception as err:  # noqa: BLE001
        note("H11 the App's log could not be read", err)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", default="main", choices=("before-update", "after-update", "main"))
    parser.add_argument("--state", default="haos-upgrade-state.json")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    assert haos.SLUG, "set AB_HAOS_SLUG"
    app = App()
    if args.phase == "before-update":
        before_update(app, Path(args.state))
    elif args.phase == "after-update":
        after_update(app, Path(args.state))
    else:
        main_phase(app)
    failed = [r for r in results if r["ok"] is False]
    notes = [r for r in results if r["ok"] is None]
    passed = len(results) - len(failed) - len(notes)
    print(f"\n{passed} of {passed + len(failed)} checks passed ({args.phase})")
    if args.out:
        Path(args.out).write_text(json.dumps({"phase": args.phase, "passed": passed, "failed": len(failed), "results": results}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
