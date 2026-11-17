"""Pure logic: settings, integration report, modules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from smart_lighting import integration
from smart_lighting.modules import AVAILABLE, GUI, MODULES, PENDING, migration
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
    assert len(keys) == len(set(keys)) == 10
    features = [m for m in MODULES if m.kind == "feature"]
    assert [m.key for m in features] == ["rooms", "presence", "lighting", "moods", "switches", "curtains", "predictions"]
    # Phase A2: Rooms has its configuration interface in the App; every other feature is pending.
    assert [m.key for m in features if m.status == GUI] == ["rooms"]
    assert all(m.status == PENDING and m.handled_by for m in features if m.key != "rooms")
    assert all(m.status == AVAILABLE for m in MODULES if m.kind == "foundation")
    m = migration()
    assert m["current_phase"] == "A2"
    assert m["modules_migrated"] == 0 and m["percent"] == 0  # no automation has moved
    assert m["interfaces_migrated"] == 1
    assert m["automatic_migration"] is False
    assert m["items"][0]["key"] == "foundation" and m["items"][0]["status"] == "done"


def test_one_system_in_control_per_module() -> None:
    from smart_lighting.modules import APP, INTEGRATION

    features = [m for m in MODULES if m.kind == "feature"]
    # Phase A2: the integration still executes every automation; the App controls nothing.
    assert {m.controlled_by for m in features} == {INTEGRATION}
    assert {m.automation_owner for m in features} == {INTEGRATION}
    assert {m.config_source for m in features} == {INTEGRATION}  # one authoritative configuration
    rooms = next(m for m in features if m.key == "rooms")
    assert (rooms.gui_owner, rooms.config_source, rooms.automation_owner) == (APP, INTEGRATION, INTEGRATION)
    assert {m.gui_owner for m in features if m.key != "rooms"} == {INTEGRATION}
    assert all(m.controlled_by is None for m in MODULES if m.kind == "foundation")
    m = migration()
    assert m["controlled_by_integration"] == 7 and m["controlled_by_app"] == 0
    assert all(i["controlled_by"] in (INTEGRATION, APP) for i in m["items"][1:])
