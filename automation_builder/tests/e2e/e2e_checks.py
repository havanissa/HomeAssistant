"""End-to-end checks: the real App image, through Ingress, against a real (disposable) Home Assistant.

    python e2e_checks.py <tokens.json> [--restart-ha "<command that restarts the test Home Assistant>"]
                         [--out results.json]

Needs ``stack.sh up`` on a **fresh** data folder and the Home Assistant prepared by ``prepare.py`` with the test
configuration described in DOCS.md ("Testing"). It never talks to anything but 127.0.0.1:18123 (the disposable
Home Assistant) and 127.0.0.1:18400 (the stack).

Everything is driven the way a user would: rules are created through the App's API, deployed, and then the test
changes sensor states in Home Assistant and looks at what the devices do.
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

HA = "http://127.0.0.1:18123"
STACK = "http://127.0.0.1:18400"
INGRESS = f"{STACK}/api/hassio_ingress/abIngressToken_XyZ789"
UNMANAGED = "1700000000001"

BED = "light.bed_light"
FLAKY = "light.flaky_lamp"
COVER = "cover.kitchen_window"
DECOR = "switch.decorative_lights"
MOTION = "binary_sensor.test_motion"
LUX = "sensor.test_lux"
MOOD = "input_select.mood"
OCC = "binary_sensor.sod_kamar_occupied"

results: list[dict[str, Any]] = []


def check(name: str, ok: Any, detail: Any = "") -> bool:
    results.append({"check": name, "ok": bool(ok), "detail": str(detail)[:400]})
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:300]}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def request(url: str, method: str = "GET", body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as err:
        raw, status = err.read(), err.code
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, raw.decode(errors="replace")


class Home:
    """The disposable Home Assistant, as the test's own administrator (not the App's user)."""

    def __init__(self, token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}

    def state(self, entity_id: str) -> dict[str, Any]:
        status, data = request(f"{HA}/api/states/{entity_id}", headers=self.headers)
        return data if status == 200 else {"state": None, "attributes": {}}

    def value(self, entity_id: str) -> Any:
        return self.state(entity_id)["state"]

    def call(self, action: str, entity_id: str, **data: Any) -> None:
        domain, service = action.split(".")
        status, _ = request(f"{HA}/api/services/{domain}/{service}", "POST", {"entity_id": entity_id, **data}, self.headers)
        assert status == 200, (action, status)

    def set(self, helper: str, on: bool) -> None:
        self.call(f"input_boolean.turn_{'on' if on else 'off'}", f"input_boolean.{helper}")

    def lux(self, value: int) -> None:
        self.call("input_number.set_value", "input_number.test_lux", value=value)

    def wait(self, entity_id: str, wanted: Any, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        accept = wanted if callable(wanted) else (lambda s: s["state"] in (wanted if isinstance(wanted, tuple) else (wanted,)))
        while time.monotonic() < deadline:
            if accept(self.state(entity_id)):
                return True
            time.sleep(0.25)
        return False

    def stays(self, entity_id: str, wanted: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.value(entity_id) != wanted:
                return False
            time.sleep(0.5)
        return True

    def automation(self, automation_id: str) -> tuple[int, Any]:
        return request(f"{HA}/api/config/automation/config/{automation_id}", headers=self.headers)

    def automations(self) -> list[dict[str, Any]]:
        _, states = request(f"{HA}/api/states", headers=self.headers)
        return [s for s in states if s["entity_id"].startswith("automation.")]

    def managed(self) -> list[str]:
        return sorted(s["attributes"].get("id") for s in self.automations()
                      if str(s["attributes"].get("id", "")).startswith("automation_builder_"))

    def wait_up(self, timeout: float = 180) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status, config = request(f"{HA}/api/config", headers=self.headers)
                if status == 200 and config.get("state") == "RUNNING":
                    return True
            except OSError:
                pass
            time.sleep(1)
        return False


class App:
    """Automation Builder through Ingress."""

    def __init__(self) -> None:
        self.revision: str | None = None

    def get(self, path: str, user: str | None = None) -> tuple[int, Any]:
        status, data = request(f"{INGRESS}/{path}", headers={"X-Test-User": user} if user else None)
        if isinstance(data, dict) and isinstance(data.get("revision"), str):
            self.revision = data["revision"]
        return status, data

    def post(self, path: str, body: dict[str, Any], user: str | None = None, csrf: bool = True) -> tuple[int, Any]:
        headers = {"X-AB": "1"} if csrf else {}
        if user:
            headers["X-Test-User"] = user
        status, data = request(f"{INGRESS}/{path}", "POST", body, headers)
        if status == 200 and isinstance(data, dict) and isinstance(data.get("revision"), str):
            self.revision = data["revision"]
        return status, data

    def change(self, path: str, body: dict[str, Any]) -> tuple[int, Any]:
        return self.post(path, {**body, "expected_revision": self.revision})

    def device(self, device_id: str) -> dict[str, Any]:
        return self.get(f"api/devices/{device_id}")[1]

    def add_device(self, name: str, targets: list[str]) -> str:
        status, data = self.change("api/devices/create", {"name": name, "targets": targets})
        assert status == 200, data
        return data["device_id"]

    def add_rule(self, device_id: str, name: str, triggers: list, actions: list, conditions: list | None = None,
                 **extra: Any) -> str:
        rule = {"name": name, "enabled": True, "triggers": triggers,
                "conditions": {"type": "and", "items": conditions or []}, "actions": actions, **extra}
        status, data = self.change("api/rules/save", {"device_id": device_id, "rule": rule})
        assert status == 200, data
        return data["rule_id"]

    def edit_rule(self, device_id: str, rule_id: str, **changes: Any) -> tuple[int, Any]:
        rule = next(r for r in self.device(device_id)["device"]["rules"] if r["id"] == rule_id)
        return self.change("api/rules/save", {"device_id": device_id, "rule": {**rule, **changes}})

    def plan(self, device_id: str) -> dict[str, Any]:
        status, data = self.post("api/devices/plan", {"device_id": device_id})
        assert status == 200, data
        return data

    def deploy(self, device_id: str, overwrite: bool = False) -> tuple[int, Any]:
        plan = self.plan(device_id)
        return self.change("api/devices/deploy", {"device_id": device_id, "plan_hash": plan["plan_hash"],
                                                  "overwrite": overwrite})

    def status(self, device_id: str) -> str:
        return self.device(device_id)["live"]["status"]


def t_state(entity_id: str, to: Any = None, frm: Any = None, for_seconds: int = 0) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "to": to, "from": frm, "for_seconds": for_seconds}


def call(action: str, **data: Any) -> dict[str, Any]:
    return {"type": "service", "action": action, "target": "all", "data": data}


def brightness_pct(home: Home, entity_id: str) -> int:
    return round((home.state(entity_id)["attributes"].get("brightness") or 0) / 255 * 100)


def main() -> int:  # noqa: PLR0915 - one linear scenario
    parser = argparse.ArgumentParser()
    parser.add_argument("tokens")
    parser.add_argument("--restart-ha", default="")
    parser.add_argument("--restart-app", default="docker restart ab-test-app")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    tokens = json.loads(Path(args.tokens).read_text())
    home, app = Home(tokens["llat"]), App()
    unmanaged_before = home.automation(UNMANAGED)[1]

    # ------------------------------------------------- 1. fresh installation and connection
    status, overview = app.get("api/overview")
    ha = overview["connection"]["home_assistant"]
    check("1 fresh installation: no devices, no leftovers", status == 200 and overview["devices"] == [] and overview["orphans"] == [])
    check("1 connected to Home Assistant 2026.11.1 through the Supervisor proxy", ha["connected"] and ha["ha_version"] == "2026.11.1", ha)
    check("1 the Ingress user is recognised as an administrator", overview["user"]["is_admin"])
    check("1 nothing of this App is in Home Assistant yet", home.managed() == [])
    status, page = request(f"{INGRESS}/")
    check("1 the interface is served with the Ingress base path", status == 200 and '<base href="/api/hassio_ingress/' in page)

    # ------------------------------------------------- 2. discovery
    _, found = app.get("api/entities?purpose=target&q=")
    domains = {e["domain"] for e in found["results"]}
    check("2 controllable entities of many domains are offered", {"light", "switch", "cover", "climate", "fan"} <= domains, domains)
    _, devices = app.get("api/ha-devices?q=bed")
    check("2 physical devices are listed with their controllable entities",
          any(BED in [e["entity_id"] for e in d["entities"]] for d in devices["devices"]), devices)
    _, occupancy = app.get("api/entities?purpose=occupancy&q=kamar")
    check("2 an occupancy sensor is found for occupancy triggers", occupancy["results"] and occupancy["results"][0]["entity_id"] == OCC)
    _, detail = app.get(f"api/entity?ids={MOOD}")
    check("2 selector choices come from the entity (mood options)", detail["entities"][MOOD]["suggested_states"] == ["Dagur", "Hygge", "Nátt"])

    # ------------------------------------------------- 3. a basic IF/THEN automation
    home.call("light.turn_off", BED)
    home.set("test_motion", False)
    home.lux(400)
    bed = app.add_device("Bed light", [BED])
    motion = app.add_rule(bed, "Motion when dark", [t_state(MOTION, to="on")], [call("light.turn_on", brightness_pct=70)],
                          [{"type": "numeric", "entity_id": LUX, "below": 100, "above": None}])
    view = app.device(bed)
    check("3 readable rule summary", view["summaries"][motion]["text"] ==
          "IF Test motion changes to “on” AND Test lux is below 100 THEN turn Bed Light on (brightness 70 %)", view["summaries"][motion]["text"])
    check("3 YAML preview before anything is deployed", "trigger: state" in view["yaml"] and view["live"]["status"] == "not_deployed")
    check("3 saving a rule changes nothing in Home Assistant", home.managed() == [])
    plan = app.plan(bed)
    check("3 plan: Home Assistant validated the automation, to be created", plan["can_deploy"] and plan["items"][0]["operation"] == "create" and not plan["items"][0]["errors"], plan["reasons"])
    status, deployed = app.deploy(bed)
    bed_auto = f"automation_builder_{bed}"
    check("3 deployed and confirmed by Home Assistant", status == 200 and deployed["deployed"], deployed)
    check("3 Home Assistant runs exactly one managed automation", home.managed() == [bed_auto], home.managed())
    status, in_ha = home.automation(bed_auto)
    check("3 the automation is in automations.yaml with the expected content", status == 200 and in_ha["alias"] == "Bed light (Automation Builder)" and in_ha["mode"] == "queued")
    home.set("test_motion", True)
    check("3 runs in Home Assistant: motion while bright does nothing", home.stays(BED, "off", 2))
    home.set("test_motion", False)
    home.lux(40)
    time.sleep(1)
    home.set("test_motion", True)
    check("3 runs in Home Assistant: motion while dark turns the lamp on", home.wait(BED, "on", 5))
    check("3 ...at 70 %", brightness_pct(home, BED) == 70, brightness_pct(home, BED))

    # ------------------------------------------------- 4. several rules, one automation
    app.add_rule(bed, "No motion", [t_state(MOTION, to="off", for_seconds=3)], [call("light.turn_off")])
    hygge = app.add_rule(bed, "Hygge", [t_state(MOOD, to="Hygge")], [call("light.turn_on", brightness_pct=30, color_temp_kelvin=2400)])
    app.add_rule(bed, "Bedtime", [{"type": "time", "at": "23:30", "weekdays": []}], [call("light.turn_off")])
    check("4 pending changes are shown", app.status(bed) == "changes_pending")
    plan = app.plan(bed)
    check("4 the plan shows the difference to what Home Assistant has", "+" in plan["items"][0]["diff"] and plan["items"][0]["operation"] == "update")
    status, _ = app.deploy(bed)
    _, in_ha = home.automation(bed_auto)
    check("4 four rules compile into one automation", status == 200 and home.managed() == [bed_auto] and len(in_ha["actions"]) == 4 and len(in_ha["triggers"]) == 4, home.managed())
    home.call("input_select.select_option", MOOD, option="Hygge")
    check("4 the mood rule runs (30 %)", home.wait(BED, lambda s: s["state"] == "on" and round((s["attributes"].get("brightness") or 0) / 2.55) == 30, 5), brightness_pct(home, BED))
    home.set("test_motion", False)
    check("4 the no-motion rule runs after its 3 s", home.wait(BED, "off", 8))
    view = app.device(bed)
    related = [r["automation_id"] for r in view["related"]]
    check("4 an unmanaged automation on the same lamp is reported", related == [UNMANAGED], view["related"])
    traces = view["live"]["automations"][0]["traces"]
    check("4 execution traces are shown", len(traces) >= 3 and traces[0]["trigger"], traces[:1])

    # ------------------------------------------------- 5. editing and disabling single rules
    app.edit_rule(bed, motion, actions=[call("light.turn_on", brightness_pct=40)])
    app.edit_rule(bed, hygge, enabled=False)
    status, _ = app.deploy(bed)
    _, in_ha = home.automation(bed_auto)
    check("5 a disabled rule is left out of the automation", status == 200 and len(in_ha["actions"]) == 3)
    home.call("input_select.select_option", MOOD, option="Dagur")
    time.sleep(1)
    home.call("input_select.select_option", MOOD, option="Hygge")
    check("5 the disabled rule does not run", home.stays(BED, "off", 3))
    home.set("test_motion", True)
    check("5 the edited rule runs with its new brightness", home.wait(BED, "on", 5) and brightness_pct(home, BED) == 40, brightness_pct(home, BED))
    rule_ids = [r["id"] for r in app.device(bed)["device"]["rules"]]
    check("5 rule identities survive editing", rule_ids[0] == motion and rule_ids[2] == hygge)

    # ------------------------------------------------- 6. conflicting rules
    clash = app.add_rule(bed, "Motion off", [t_state(MOTION, to="on")], [call("light.turn_off")])
    view = app.device(bed)
    check("6 a contradiction is detected", [c["code"] for c in view["conflicts"]] == ["contradiction"] and view["blocking"] == 1, view["conflicts"])
    plan = app.plan(bed)
    status, refused = app.change("api/devices/deploy", {"device_id": bed, "plan_hash": plan["plan_hash"]})
    check("6 deployment is refused while the conflict is open", not plan["can_deploy"] and status == 409 and refused["error"] == "blocked", refused)
    _, still = home.automation(bed_auto)
    check("6 Home Assistant still has the previous automation", len(still["actions"]) == 3)
    app.change("api/rules/delete", {"device_id": bed, "rule_id": clash})
    check("6 resolved by removing the rule", app.device(bed)["conflicts"] == [] and app.status(bed) == "in_sync")

    # ------------------------------------------------- 8/9. occupancy with generated rechecks
    _, settings = app.get("api/settings")
    occ_settings = {**settings["settings"]["occupancy"], "startup_grace_seconds": 20, "periodic_minutes": 1}
    status, _ = app.change("api/settings/save", {"settings": {**settings["settings"], "occupancy": occ_settings}})
    check("9 recheck timing is configurable in Settings", status == 200)
    for helper, on in (("occ_kamar", False), ("occ_available", True), ("occ_fresh", True), ("drop_commands", False), ("flaky_state", False)):
        home.set(helper, on)
    flaky = app.add_device("Flaky lamp", [FLAKY])
    occupied = app.add_rule(flaky, "Occupied", [{"type": "occupancy", "entity_id": OCC, "to": "occupied"}], [call("light.turn_on")])
    app.add_rule(flaky, "Vacant", [{"type": "occupancy", "entity_id": OCC, "to": "vacant"}], [call("light.turn_off")])
    status, _ = app.deploy(flaky)
    flaky_auto = f"automation_builder_{flaky}"
    _, in_ha = home.automation(flaky_auto)
    kinds = sorted({t["id"].split("_")[0] for t in in_ha["triggers"]})
    check("9 occupancy rules get immediate, follow-up and periodic triggers in the device's automation",
          status == 200 and kinds == ["occupancy", "periodic", "recheck"] and len(in_ha["triggers"]) == 7, in_ha["triggers"])
    check("9 the rechecks are triggers, not waiting sequences", "delay" not in json.dumps(in_ha["actions"]) and in_ha["mode"] == "queued")
    time.sleep(24)  # past the start-up grace of the freshly loaded automation
    home.set("occ_kamar", True)
    check("9 occupied: the lamp turns on at once", home.wait(FLAKY, "on", 4))
    home.set("occ_kamar", False)
    check("9 vacant: the lamp turns off", home.wait(FLAKY, "off", 4))
    home.set("drop_commands", True)
    home.set("occ_kamar", True)
    check("9 a lost command leaves the lamp off", home.stays(FLAKY, "off", 1))
    home.set("drop_commands", False)
    check("9 the follow-up check corrects the lost command", home.wait(FLAKY, "on", 6))
    started = home.state("input_boolean.flaky_state")["last_changed"]
    time.sleep(18)
    check("9 further rechecks send nothing more (state unchanged)", home.state("input_boolean.flaky_state")["last_changed"] == started)
    home.call("light.turn_off", FLAKY)  # a person switches the lamp off while the room stays occupied
    check("9 a manual change is respected by the 15 s and periodic rechecks", home.stays(FLAKY, "off", 80))
    mismatch = app.device(flaky)["mismatches"]
    check("9 the mismatch is reported instead of forced", len(mismatch) == 1 and "may have been a person" in mismatch[0]["message"], mismatch)

    # ------------------------------------------------- 10. stale and unavailable occupancy
    home.set("occ_kamar", False)
    home.wait(FLAKY, "off", 4)
    home.set("drop_commands", True)
    home.set("occ_fresh", False)
    home.set("occ_kamar", True)
    time.sleep(1)
    home.set("drop_commands", False)
    check("10 stale occupancy (fresh: false): no corrective action", home.stays(FLAKY, "off", 20))
    home.set("occ_fresh", True)
    check("10 fresh again: the periodic check reconciles", home.wait(FLAKY, "on", 70))
    home.set("occ_kamar", False)
    home.wait(FLAKY, "off", 4)
    home.set("drop_commands", True)
    home.set("occ_kamar", True)
    time.sleep(0.5)
    home.set("occ_available", False)
    home.set("drop_commands", False)
    check("10 occupancy unavailable: no corrective action", home.value(OCC) == "unavailable" and home.stays(FLAKY, "off", 20))
    home.set("occ_available", True)
    check("10 available again and still occupied: corrected", home.wait(FLAKY, "on", 70))

    # ------------------------------------------------- 7. non-idempotent actions
    status, _ = app.edit_rule(flaky, occupied, actions=[call("light.toggle")])
    view = app.device(flaky)
    check("7 toggle: the rule gets no rechecks, and says why", any(n["code"] == "recheck_off_non_idempotent" for n in view["notes"]), view["notes"])
    status, _ = app.deploy(flaky)
    _, in_ha = home.automation(flaky_auto)
    home.set("occ_kamar", False)
    home.wait(FLAKY, "off", 4)
    time.sleep(1)
    before = home.state("input_boolean.flaky_state")["last_changed"]
    home.set("occ_kamar", True)
    toggled = home.wait(FLAKY, "on", 4)
    after = home.state("input_boolean.flaky_state")["last_changed"]
    time.sleep(22)
    check("7 toggle runs exactly once per occupancy change", status == 200 and toggled and after != before
          and home.state("input_boolean.flaky_state")["last_changed"] == after and home.value(FLAKY) == "on")
    app.edit_rule(flaky, occupied, actions=[call("light.turn_on")])
    app.deploy(flaky)

    # ------------------------------------------------- 11. other domains, delay rules, no occupancy or mood needed
    home.lux(400)
    home.call("cover.open_cover", COVER)
    home.wait(COVER, "open", 20)
    cover = app.add_device("Kitchen window", [COVER])
    app.add_rule(cover, "Dusk", [{"type": "numeric", "entity_id": LUX, "below": 20, "above": None, "for_seconds": 0}], [call("cover.close_cover")])
    status, _ = app.deploy(cover)
    home.lux(5)
    check("11 a cover is controlled by an ordinary threshold rule", status == 200 and home.wait(COVER, ("closing", "closed"), 6), home.value(COVER))
    home.call("switch.turn_off", DECOR)
    decor = app.add_device("Decorative lights", [DECOR])
    pulse = app.add_rule(decor, "Pulse", [t_state("input_button.wall_switch")],
                         [call("switch.turn_on"), {"type": "delay", "seconds": 4}, call("switch.turn_off")])
    app.add_rule(decor, "Night", [t_state(MOOD, to="Nátt")], [call("switch.turn_off")])
    view = app.device(decor)
    separate = [a for a in view["live"]["automations"] if a["role"] == "separate"]
    check("11 a rule with a restarting delay gets its own automation, with the reason",
          len(view["live"]["automations"]) == 2 and len(separate) == 1 and "its own automation because" in separate[0]["reason"], view["live"]["automations"])
    status, _ = app.deploy(decor)
    check("11 both automations are deployed", status == 200 and f"automation_builder_{decor}_{pulse}" in home.managed())
    home.call("input_button.press", "input_button.wall_switch")
    on = home.wait(DECOR, "on", 4)
    check("11 the delay rule runs: on, then off after its wait", on and home.wait(DECOR, "off", 8))
    check("11 the unmanaged automation on the same button still works and is unchanged", home.automation(UNMANAGED)[1] == unmanaged_before)

    # ------------------------------------------------- 12. external modification
    _, current = home.automation(bed_auto)
    edited = {k: v for k, v in current.items() if k != "id"} | {"alias": "Edited by hand", "mode": "single"}
    status, _ = request(f"{HA}/api/config/automation/config/{bed_auto}", "POST", edited, home.headers)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and app.status(bed) != "modified":
        time.sleep(0.5)
    check("12 an edit in Home Assistant's own editor is detected", status == 200 and app.status(bed) == "modified", app.status(bed))
    plan = app.plan(bed)
    check("12 the plan shows the outside change", plan["needs_overwrite"] and plan["items"][0]["external"] == "modified" and "Edited by hand" in plan["items"][0]["diff"])
    status, refused = app.change("api/devices/deploy", {"device_id": bed, "plan_hash": plan["plan_hash"], "overwrite": False})
    check("12 it is not overwritten without an explicit yes", status == 409 and refused["error"] == "external_change" and home.automation(bed_auto)[1]["alias"] == "Edited by hand")
    status, _ = app.change("api/devices/deploy", {"device_id": bed, "plan_hash": plan["plan_hash"], "overwrite": True})
    check("12 with the explicit yes it is replaced", status == 200 and home.automation(bed_auto)[1]["alias"] == "Bed light (Automation Builder)" and app.status(bed) == "in_sync")
    request(f"{HA}/api/config/automation/config/{bed_auto}", "DELETE", headers=home.headers)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and app.status(bed) != "missing":
        time.sleep(0.5)
    check("12 a deletion in Home Assistant is detected", app.status(bed) == "missing", app.status(bed))
    status, _ = app.deploy(bed)
    check("12 ...and the automation is created again on request", status == 200 and bed_auto in home.managed())

    # ------------------------------------------------- 13. failed deployments and recovery
    _, good = home.automation(bed_auto)
    bad = app.add_rule(bed, "Broken", [t_state(MOTION, to="off")],
                       [{"type": "service", "action": "light.turn_on", "target": "all", "data": {"brightness_pct": "{{ states( }}"}}])
    plan = app.plan(bed)
    status, refused = app.change("api/devices/deploy", {"device_id": bed, "plan_hash": plan["plan_hash"]})
    check("13 Home Assistant's own validation rejects a broken automation before anything is written",
          not plan["can_deploy"] and plan["items"][0]["errors"] and status == 409, plan["items"][0]["errors"])
    app.change("api/rules/delete", {"device_id": bed, "rule_id": bad})
    extra = app.add_rule(bed, "Extra", [t_state(MOOD, to="Nátt")], [call("light.turn_off")])
    plan = app.plan(bed)
    request(f"{STACK}/_test/fail_api?mode=refuse", "POST")
    status, failed = app.change("api/devices/deploy", {"device_id": bed, "plan_hash": plan["plan_hash"]})
    request(f"{STACK}/_test/fail_api", "POST")
    check("13 a refused deployment is reported with Home Assistant's message", status == 502 and failed["error"] == "deploy_failed" and "refused by the test" in failed["message"], failed)
    check("13 ...and Home Assistant keeps the previous automation", home.automation(bed_auto)[1] == good)
    view = app.device(bed)
    check("13 ...and the device shows the failure and its pending changes", view["last_result"]["ok"] is False and view["live"]["status"] == "changes_pending")
    request(f"{STACK}/_test/fail_api?mode=down", "POST")
    status, down = app.post("api/devices/plan", {"device_id": bed})
    request(f"{STACK}/_test/fail_api", "POST")
    check("13 an unreachable API is a clear error", status == 502 and "could not read" in down["message"], down)
    status, _ = app.deploy(bed)
    check("13 recovery: the same rules deploy afterwards", status == 200 and app.status(bed) == "in_sync" and len(home.automation(bed_auto)[1]["actions"]) == 4)
    app.change("api/devices/restore", {"device_id": bed, "version": "previous"})
    names = [r["name"] for r in app.device(bed)["device"]["rules"]]
    check("13 rollback: the rules of the previous deployment are restored, deploying stays explicit",
          extra not in [r["id"] for r in app.device(bed)["device"]["rules"]] and app.status(bed) == "changes_pending", names)
    status, _ = app.deploy(bed)
    check("13 ...and deploy again", status == 200 and home.automation(bed_auto)[1] == good)

    # ------------------------------------------------- 14. backup and restore
    _, export = app.get("api/export")
    status, removed = app.change("api/devices/delete", {"device_id": cover, "overwrite": False})
    check("14 deleting a device removes its automation from Home Assistant", status == 200 and removed["removed_automations"] == 1 and f"automation_builder_{cover}" not in home.managed())
    _, settings = app.get("api/settings")
    check("14 a backup is written before every change", len(settings["backups"]) >= 10 and settings["backups"][0]["devices"] == 4)
    status, review = app.post("api/import/review", {"document": export})
    check("14 an import is reviewed first", status == 200 and [c["type"] for c in review["changes"]] == ["added"], review)
    status, _ = app.change("api/import/apply", {"document": export})
    check("14 the export restores the device, not deployed", status == 200 and app.status(cover) == "not_deployed" and f"automation_builder_{cover}" not in home.managed())
    status, _ = app.deploy(cover)
    check("14 ...and it deploys again", status == 200 and app.status(cover) == "in_sync")
    subprocess.run(args.restart_app, shell=True, check=True, capture_output=True)  # noqa: S602 - the test's own command
    deadline = time.monotonic() + 60
    ready = False
    while time.monotonic() < deadline and not ready:
        try:
            status, overview = app.get("api/overview")
            ready = status == 200 and overview["connection"]["home_assistant"]["connected"]
        except OSError:
            pass
        time.sleep(1)
    statuses = {d["name"]: d["status"] for d in overview["devices"]} if ready else {}
    check("14 an App restart keeps rules, records and status", ready and set(statuses.values()) == {"in_sync"} and len(statuses) == 4, statuses)

    # ------------------------------------------------- access and what the App sent
    status, _ = app.post("api/devices/plan", {"device_id": bed}, user="somebody-else")
    check("15 a user who is not an administrator is refused", status == 403)
    status, _ = app.post("api/devices/plan", {"device_id": bed}, csrf=False)
    check("15 a request without the App's header is refused", status == 403)
    _, sent = request(f"{STACK}/_test/commands")
    kinds = {c["type"] for c in sent["commands"]}
    check("15 the App never called a service or fired an event",
          not kinds & {"call_service", "fire_event", "execute_script"} and kinds <= {
              "auth", "ping", "get_config", "get_states", "subscribe_events", "config/area_registry/list",
              "config/device_registry/list", "config/entity_registry/list", "config/auth/list", "validate_config",
              "automation/config", "search/related", "trace/list", "unsubscribe_events"}, kinds)
    paths = {h["path"] for h in sent["http"]}
    check("15 over HTTP it only touched its own automations", paths and all(p.startswith("config/automation/config/automation_builder_") for p in paths), paths)
    check("16 the unmanaged automation is byte-for-byte unchanged", home.automation(UNMANAGED)[1] == unmanaged_before)

    # ------------------------------------------------- Home Assistant restart
    if args.restart_ha:
        home.set("occ_kamar", False)
        home.wait(FLAKY, "off", 4)
        home.set("occ_kamar", True)
        home.wait(FLAKY, "on", 4)
        time.sleep(1)
        home.call("light.turn_off", FLAKY)  # occupied room, lamp switched off by a person, then a restart
        time.sleep(2)
        subprocess.run(args.restart_ha, shell=True, check=True, capture_output=True)  # noqa: S602
        up = home.wait_up()
        check("17 Home Assistant restarted", up)
        home.wait(OCC, "on", 60)
        check("17 after the restart the managed automations are loaded again", len(home.managed()) == 5, home.managed())
        check("17 an occupied room's lamp that a person switched off stays off across the restart", home.value(OCC) == "on" and home.stays(FLAKY, "off", 95))
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _, overview = app.get("api/overview")
            if overview["connection"]["home_assistant"]["connected"] and {d["status"] for d in overview["devices"]} == {"in_sync"}:
                break
            time.sleep(2)
        check("17 the App reconnected and every device is in sync", {d["status"] for d in overview["devices"]} == {"in_sync"}, [(d["name"], d["status"]) for d in overview["devices"]])
        home.set("occ_kamar", False)
        check("17 rules keep working after the restart", home.wait(OCC, "off", 5) and home.stays(FLAKY, "off", 2))
        home.set("occ_kamar", True)
        check("17 ...occupied turns the lamp on again", home.wait(FLAKY, "on", 5))

    failed = [r for r in results if not r["ok"]]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    if args.out:
        Path(args.out).write_text(json.dumps({"passed": len(results) - len(failed), "total": len(results), "results": results}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
