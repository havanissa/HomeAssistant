"""Pure logic: settings, integration report, modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_lighting import integration
from smart_lighting.modules import AVAILABLE, GUI, MODULES, PENDING, migration, modules
from smart_lighting.settings import load_settings


def test_settings_defaults_are_production() -> None:
    s = load_settings({"SUPERVISOR_TOKEN": "t" * 20}, options_path=Path("/nonexistent"))
    assert s.ha_ws_url == "ws://supervisor/core/websocket"
    assert s.supervisor_url == "http://supervisor"
    assert s.port == 8099
    assert [str(n) for n in s.allowed_networks] == ["172.30.32.2/32"]
    assert s.is_allowed("172.30.32.2")
    assert s.is_allowed("::ffff:172.30.32.2")
    assert not s.is_allowed("172.30.32.3")
    assert not s.is_allowed("192.168.1.10")
    assert not s.is_allowed(None)
    assert not s.is_allowed("not-an-ip")
    assert s.log_level == "info"
    assert not s.dev_mode


def test_token_is_hidden() -> None:
    token = "secret-token-0123456789"
    s = load_settings({"SUPERVISOR_TOKEN": token})
    assert token not in repr(s)
    assert token not in json.dumps(s.public())
    assert s.public()["token"] == "present (hidden)"


def test_options_file(tmp_path: Path) -> None:
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"log_level": "debug"}))
    assert load_settings({}, options_path=options).log_level == "debug"
    options.write_text(json.dumps({"log_level": "loud"}))
    assert load_settings({}, options_path=options).log_level == "info"
    options.write_text("{broken")
    assert load_settings({}, options_path=options).log_level == "info"


def test_allow_any_for_development() -> None:
    s = load_settings({"SL_ALLOWED_CLIENTS": "any"})
    assert s.is_allowed("10.1.2.3")


ENTRY = {"domain": "smart_occupancy", "title": "Smart", "state": "loaded", "disabled_by": None}
REG = [{"entity_id": "sensor.a", "platform": "smart_occupancy"}, {"entity_id": "light.b", "platform": "hue"}]


@pytest.mark.parametrize(
    ("entries", "manifest", "status"),
    [
        ([ENTRY], {"version": "0.6.8"}, integration.LOADED),
        ([{**ENTRY, "state": "setup_error"}], {"version": "0.6.8"}, integration.NOT_RUNNING),
        ([{**ENTRY, "state": "not_loaded", "disabled_by": "user"}], {"version": "0.6.8"}, integration.NOT_RUNNING),
        ([], {"version": "0.6.8"}, integration.INSTALLED),
        ([], None, integration.NOT_INSTALLED),
        (None, None, integration.UNKNOWN),
    ],
)
def test_integration_status(entries: list | None, manifest: dict | None, status: str) -> None:
    report = integration.build_report("smart_occupancy", entries, manifest, REG)
    assert report.status == status
    assert report.entity_ids == ("sensor.a",)
    assert report.detected == (status in {integration.LOADED, integration.NOT_RUNNING, integration.INSTALLED})


def test_integration_report_ignores_other_domains() -> None:
    other = {**ENTRY, "domain": "hue"}
    report = integration.build_report("smart_occupancy", [other], None, [])
    assert report.status == integration.NOT_INSTALLED


def test_modules_and_migration() -> None:
    keys = [m.key for m in MODULES]
    assert len(keys) == len(set(keys)) == 12
    features = [m for m in MODULES if m.kind == "feature"]
    assert [m.key for m in features] == ["rooms", "presence", "lighting", "moods", "switches", "curtains", "predictions"]
    # Phases A2, A3.1/A3.2, A4 and A5: every feature module has its interface in the App.
    assert [(m.key, m.phase) for m in features if m.status == GUI] == [
        ("rooms", "A2"), ("presence", "A3.2"), ("lighting", "A4"), ("moods", "A4"), ("switches", "A4"),
        ("curtains", "A5"), ("predictions", "A5")]
    assert not [m for m in features if m.status == PENDING]
    assert all(m.status == AVAILABLE for m in MODULES if m.kind == "foundation")
    # Phase A2.1: the floor plan is an App tool; it migrates no module and counts in no migration figure.
    tools = [m for m in MODULES if m.kind == "tool"]
    assert [(m.key, m.status, m.controlled_by, m.automation_owner) for m in tools] == [
        ("floorplan", AVAILABLE, None, None)]
    # Phase A7.0: Climate is native to the App (never in the integration): no migration status, observing only.
    native = [m for m in MODULES if m.kind == "native"]
    assert [(m.key, m.status, m.controlled_by, m.automation_owner, m.phase) for m in native] == [
        ("climate", AVAILABLE, None, None, "A7.0")]
    m = migration()
    assert m["current_phase"] == "A6"
    assert m["modules_migrated"] == 0 and m["percent"] == 0  # by default no automation has moved (occupancy too)
    assert m["interfaces_migrated"] == 7
    presence = next(i for i in m["items"] if i["key"] == "presence")
    assert (presence["gui_owner"], presence["config_source"], presence["automation_owner"], presence["phase"]) == (
        "app", "integration", "integration", "A3.2")
    # Phase A3.2: Presence counts as migrated only while the integration acknowledges the App as the owner.
    owned = migration("app")
    assert owned["modules_migrated"] == 1 and owned["controlled_by_app"] == 1
    assert next(i for i in owned["items"] if i["key"] == "presence")["automation_owner"] == "app"
    assert next(x for x in modules("app") if x["key"] == "lighting")["automation_owner"] == "integration"
    assert next(x for x in modules() if x["key"] == "presence")["controlled_by"] == "integration"
    # Phase A4: Lighting, Moods and Switches move together: one acknowledged lighting owner, configuration included.
    lit = migration("integration", "app")
    assert lit["modules_migrated"] == 3 and lit["controlled_by_app"] == 3
    for key in ("lighting", "moods", "switches"):
        item = next(i for i in lit["items"] if i["key"] == key)
        assert (item["gui_owner"], item["config_source"], item["automation_owner"]) == ("app", "app", "app")
        item = next(i for i in m["items"] if i["key"] == key)
        assert (item["gui_owner"], item["config_source"], item["automation_owner"]) == (
            "app", "integration", "integration")
    assert migration("app", "app")["modules_migrated"] == 4
    # Phase A5: Curtains and Predictions each have their own acknowledged owner (configuration included).
    cur = migration("integration", "integration", "app")
    assert cur["modules_migrated"] == 1 and cur["controlled_by_app"] == 1
    item = next(i for i in cur["items"] if i["key"] == "curtains")
    assert (item["gui_owner"], item["config_source"], item["automation_owner"]) == ("app", "app", "app")
    pre = migration("integration", "integration", "integration", "app")
    assert next(i for i in pre["items"] if i["key"] == "predictions")["automation_owner"] == "app"
    assert next(i for i in pre["items"] if i["key"] == "curtains")["automation_owner"] == "integration"
    # Every automation in the App (Rooms is configuration only; it has no automation of its own).
    assert migration("app", "app", "app", "app")["modules_migrated"] == 6
    assert migration("app", "app", "app", "app")["controlled_by_integration"] == 1
    assert m["automatic_migration"] is False
    assert m["items"][0]["key"] == "foundation" and m["items"][0]["status"] == "done"


def test_one_system_in_control_per_module() -> None:
    from smart_lighting.modules import APP, INTEGRATION

    features = [m for m in MODULES if m.kind == "feature"]
    # By default the integration executes every automation (occupancy too); the App controls nothing.
    assert {m.controlled_by for m in features} == {INTEGRATION}
    assert {m.automation_owner for m in features} == {INTEGRATION}
    assert {m.config_source for m in features} == {INTEGRATION}  # one authoritative configuration
    rooms = next(m for m in features if m.key == "rooms")
    assert (rooms.gui_owner, rooms.config_source, rooms.automation_owner) == (APP, INTEGRATION, INTEGRATION)
    presence = next(m for m in features if m.key == "presence")
    assert (presence.gui_owner, presence.config_source, presence.automation_owner) == (APP, INTEGRATION, INTEGRATION)
    assert {m.gui_owner for m in features if m.key in ("curtains", "predictions")} == {APP}
    assert {m.gui_owner for m in features if m.key in ("lighting", "moods", "switches")} == {APP}
    assert all(m.controlled_by is None for m in MODULES if m.kind in ("foundation", "tool", "native"))
    m = migration()
    assert m["controlled_by_integration"] == 7 and m["controlled_by_app"] == 0
    assert all(i["controlled_by"] in (INTEGRATION, APP) for i in m["items"][1:])
