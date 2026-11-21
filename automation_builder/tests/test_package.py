"""Packaging and architecture: versions, independence from the other Apps, what the App may do in Home Assistant."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from ab import ha
from ab.const import APP_VERSION

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
STATIC = APP / "backend" / "ab" / "static"


def test_versions_agree():
    config = yaml.safe_load((APP / "config.yaml").read_text())
    assert config["version"] == APP_VERSION
    assert re.search(r"^## (\S+)", (APP / "CHANGELOG.md").read_text(), re.M).group(1) == APP_VERSION
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "Dockerfile").read_text()
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "tests" / "e2e" / "stack.sh").read_text()


def test_app_identity_is_independent():
    ours = yaml.safe_load((APP / "config.yaml").read_text())
    others = [yaml.safe_load((REPO / name / "config.yaml").read_text()) for name in ("smart_lighting", "smart_occupancy_detection")]
    assert ours["slug"] == "automation_builder" and ours["slug"] not in {o["slug"] for o in others}
    assert ours["name"] not in {o["name"] for o in others}
    assert ours["ingress"] is True and "ports" not in ours and ours.get("hassio_api") is False
    assert not ours.get("map")  # no access to Home Assistant's configuration folder or other Apps' data
    assert ours["panel_admin"] is True


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


def test_no_dependency_on_the_other_apps_or_an_integration():
    imports = _imports(APP / "backend")
    assert not imports & {"sod", "sod_engine", "occupancy_core", "smart_lighting", "smart_occupancy", "custom_components",
                          "homeassistant"}
    assert imports <= {"__future__", "aiohttp", "yaml", "asyncio", "collections", "contextlib", "copy", "dataclasses",
                       "datetime", "difflib", "hashlib", "ipaddress", "json", "logging", "math", "os", "pathlib", "re",
                       "secrets", "time", "typing", "urllib"}
    # no companion integration: nothing of this App lives in custom_components
    assert not (REPO / "custom_components" / "automation_builder").exists()
    for path in (APP / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "smart_occupancy_detection/" not in text and "sod/" not in text, path.name


def test_the_allowlist_has_no_way_to_control_a_device():
    forbidden = {"call_service", "execute_script", "fire_event", "config/entity_registry/update", "subscribe_trigger",
                 "config/device_registry/update", "config/area_registry/create", "conversation/process", "render_template",
                 "test_condition"}
    assert not forbidden & ha.ALLOWED_COMMANDS
    assert ha.ALLOWED_EVENT_TYPES == {"state_changed", "area_registry_updated", "device_registry_updated",
                                      "entity_registry_updated", "homeassistant_started"}
    for root in (APP / "backend",):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in ('"call_service"', "/api/services", "/api/states/", "automation.reload"):
                assert pattern not in text, f"{path.name}: {pattern}"


async def test_forbidden_commands_and_foreign_automations_are_refused_before_sending():
    client = ha.HomeAssistantClient(None, "ws://x", "token")  # type: ignore[arg-type]
    with pytest.raises(ha.NotAllowed):
        await client.command("call_service", domain="light", service="turn_off")
    with pytest.raises(ha.NotAllowed):
        await client.command("subscribe_events", event_type="call_service")
    api = ha.AutomationConfigApi(None, "http://supervisor/core/api", "token")  # type: ignore[arg-type]
    for foreign in ("1699999999999", "my_automation", "automation_builder_x/../../core/config", "automation_builder_a?x=1", "automation_builder_É", ""):
        for call in (api.get(foreign), api.put(foreign, {}), api.delete(foreign)):
            with pytest.raises(ha.NotAllowed):
                await call
    assert api._url("automation_builder_d_0a1b2c3d") == (  # noqa: SLF001
        "http://supervisor/core/api/config/automation/config/automation_builder_d_0a1b2c3d")


def test_container_files():
    dockerfile = (APP / "Dockerfile").read_text()
    assert "COPY backend/ /opt/ab/" in dockerfile and "PYTHONPATH=/opt/ab" in dockerfile
    run = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "ab" / "run"
    assert "python3 -m ab" in run.read_text() and run.stat().st_mode & 0o111
    assert (APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "user" / "contents.d" / "ab").exists()
    for name in ("icon.png", "logo.png", "README.md", "DOCS.md", "CHANGELOG.md", "translations/en.yaml"):
        assert (APP / name).exists(), name
    requirements = (APP / "requirements.txt").read_text()
    assert all("==" in line for line in requirements.splitlines() if line and not line.startswith("#"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_interface_scripts_parse():
    for name in ("app.js", "ui.js", "editor.js", "pages.js"):
        subprocess.run(["node", "--check", str(STATIC / name)], check=True)


def test_interface_has_no_inline_styles_or_scripts():
    """The Content-Security-Policy allows neither: everything is in app.css and the script files."""
    html = (STATIC / "index.html").read_text()
    js = "".join((STATIC / name).read_text() for name in ("app.js", "ui.js", "editor.js", "pages.js"))
    assert ' style="' not in js and "<style" not in html and "onclick=" not in js + html
    assert "innerHTML" in js and "esc(" in js


def test_the_other_apps_are_untouched():
    """Automation Builder adds its own folder (and its runtime tests); it changes no file of the other Apps."""
    result = subprocess.run(["git", "status", "--porcelain", "--", "smart_lighting/backend", "smart_occupancy_detection/backend",
                             "custom_components"], cwd=REPO, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""
