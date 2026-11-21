"""Packaging and architecture: versions, the shared protocol copy, independence from Smart Comfort, no device control."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from sod import ha
from sod.const import APP_VERSION

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
COMPANION = REPO / "custom_components" / "smart_occupancy_detection"


def test_versions_agree():
    config = yaml.safe_load((APP / "config.yaml").read_text())
    assert config["version"] == APP_VERSION
    assert re.search(r"^## (\S+)", (APP / "CHANGELOG.md").read_text(), re.M).group(1) == APP_VERSION
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "Dockerfile").read_text()
    manifest = json.loads((COMPANION / "manifest.json").read_text())
    const = (COMPANION / "const.py").read_text()
    assert f'VERSION: Final = "{manifest["version"]}"' in const


def test_app_identity_is_independent_of_smart_comfort():
    ours = yaml.safe_load((APP / "config.yaml").read_text())
    theirs = yaml.safe_load((REPO / "smart_lighting" / "config.yaml").read_text())
    assert ours["slug"] == "smart_occupancy_detection" != theirs["slug"]
    assert ours["name"] != theirs["name"]
    assert ours["ingress"] is True and "ports" not in ours and ours.get("hassio_api") is False
    assert not ours.get("map")  # no access to Home Assistant's configuration or other Apps' data
    manifest = json.loads((COMPANION / "manifest.json").read_text())
    assert manifest["domain"] == "smart_occupancy_detection" != "smart_occupancy"


def test_protocol_copies_are_byte_identical():
    assert (APP / "backend" / "sod" / "protocol.py").read_bytes() == (COMPANION / "protocol.py").read_bytes()


def test_the_allowlist_cannot_control_anything():
    forbidden = {"call_service", "execute_script", "fire_event", "config/entity_registry/update",
                 "config/device_registry/update", "config/area_registry/create", "conversation/process"}
    assert not forbidden & ha.ALLOWED_COMMANDS
    assert all(c.startswith(("smart_occupancy_detection/", "config/", "get_", "subscribe_", "unsubscribe_",
                             "ping", "manifest/")) for c in ha.ALLOWED_COMMANDS)
    assert ha.ALLOWED_EVENT_TYPES == {"state_changed", "area_registry_updated", "device_registry_updated",
                                      "entity_registry_updated", "homeassistant_started"}


async def test_a_forbidden_command_is_refused_before_sending():
    client = ha.HomeAssistantClient(None, "ws://x", "token")  # type: ignore[arg-type]
    with pytest.raises(ha.NotAllowed):
        await client.command("call_service", domain="light", service="turn_off")
    with pytest.raises(ha.NotAllowed):
        await client.command("subscribe_events", event_type="call_service")
    with pytest.raises(ha.NotAllowed):
        await client.subscribe("subscribe_trigger", lambda e: None)


def _imports(root: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def test_runtime_code_never_imports_smart_comfort():
    imports = _imports(APP / "backend")
    assert not imports & {"occupancy_core", "smart_lighting", "smart_occupancy", "custom_components", "homeassistant"}
    assert _imports(APP / "backend" / "sod_engine") <= {"__future__", "collections", "dataclasses", "datetime",
                                                        "enum", "typing", "hashlib", "json", "pathlib"}
    companion = _imports(COMPANION)
    assert not companion & {"occupancy_core", "sod_engine", "sod", "smart_lighting"}
    assert "smart_occupancy" not in {i for i in companion}


def test_no_service_call_path_anywhere():
    for root in (APP / "backend", COMPANION):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in ("async_call(", "services.async_register", "call_service\"", "async_fire(",
                            "hass.states.async_set"):
                assert pattern not in text, f"{path.name}: {pattern}"


def test_companion_is_small_and_has_no_engine():
    lines = sum(len(p.read_text().splitlines()) for p in COMPANION.glob("*.py"))
    theirs = sum(len(p.read_text().splitlines()) for p in (REPO / "custom_components" / "smart_occupancy").glob("*.py"))
    assert lines < theirs / 2, (lines, theirs)
    assert not (COMPANION / "services.yaml").exists()


def test_container_files():
    dockerfile = (APP / "Dockerfile").read_text()
    assert "COPY backend/ /opt/sod/" in dockerfile and "PYTHONPATH=/opt/sod" in dockerfile
    run = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "sod" / "run"
    assert "python3 -m sod" in run.read_text() and run.stat().st_mode & 0o111
    assert (APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "user" / "contents.d" / "sod").exists()
    for name in ("icon.png", "logo.png", "README.md", "DOCS.md", "CHANGELOG.md", "translations/en.yaml"):
        assert (APP / name).exists(), name
    requirements = (APP / "requirements.txt").read_text()
    assert all("==" in line for line in requirements.splitlines() if line and not line.startswith("#"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_interface_script_parses():
    subprocess.run(["node", "--check", str(APP / "backend" / "sod" / "static" / "app.js")], check=True)


def test_interface_has_no_inline_styles_or_scripts():
    """The Content-Security-Policy allows neither: everything is in app.css and app.js."""
    js = (APP / "backend" / "sod" / "static" / "app.js").read_text()
    html = (APP / "backend" / "sod" / "static" / "index.html").read_text()
    assert ' style="' not in js and "<style" not in html and "onclick=" not in js + html
    assert "innerHTML" in js and "esc(" in js
