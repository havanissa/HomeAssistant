"""Packaging and architecture: versions, the shared protocol copy, independence from the other Apps, no device control."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from spc import ha
from spc.const import APP_NAME, APP_VERSION

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
BACKEND = APP / "backend"
STATIC = BACKEND / "spc" / "static"
COMPANION = REPO / "custom_components" / "smart_predictive_climate"
DOMAIN = "smart_predictive_climate"
OTHER_APPS = ("smart_lighting", "smart_occupancy_detection", "automation_builder")


def test_versions_agree():
    config = yaml.safe_load((APP / "config.yaml").read_text())
    assert config["version"] == APP_VERSION == "0.1.0"
    assert re.search(r"^## (\S+)", (APP / "CHANGELOG.md").read_text(), re.M).group(1) == APP_VERSION
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "Dockerfile").read_text()
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "tests" / "e2e" / "stack.sh").read_text()
    manifest = json.loads((COMPANION / "manifest.json").read_text())
    const = (COMPANION / "const.py").read_text()
    assert manifest["version"] == "0.1.0" and f'VERSION: Final = "{manifest["version"]}"' in const
    assert re.search(r"^## (\S+)", (APP / "companion_dist" / "CHANGELOG.md").read_text(), re.M).group(1) == \
        manifest["version"]
    from spc import compat

    assert compat.check(manifest["version"], 1)["level"] == "ok"
    assert compat.check("0.0.9", 1)["level"] == "incompatible" and compat.check("0.1.0", 2)["level"] == "incompatible"


def test_app_identity_is_independent_of_the_other_apps():
    ours = yaml.safe_load((APP / "config.yaml").read_text())
    others = [yaml.safe_load((REPO / name / "config.yaml").read_text()) for name in OTHER_APPS]
    assert ours["slug"] == DOMAIN and ours["slug"] not in {o["slug"] for o in others}
    assert ours["name"] == APP_NAME and ours["name"] not in {o["name"] for o in others}
    assert ours["panel_title"] == "Predictive Climate" and ours["panel_icon"] == "mdi:home-thermometer-outline"
    assert ours["ingress"] is True and "ports" not in ours and ours["panel_admin"] is True
    assert ours["homeassistant_api"] is True and ours.get("hassio_api") is False and ours["backup"] == "hot"
    assert not ours.get("map")  # no access to Home Assistant's configuration or other Apps' data
    assert not {"host_network", "privileged", "full_access", "docker_api", "auth_api", "devices", "uart", "usb",
                "hassio_role"} & set(ours)
    assert "never controls" in ours["description"]
    manifest = json.loads((COMPANION / "manifest.json").read_text())
    assert manifest["domain"] == DOMAIN and manifest["iot_class"] == "local_push"
    assert manifest["integration_type"] == "service" and manifest["config_flow"] is True
    assert manifest["codeowners"] == ["@havanissa"] and manifest["requirements"] == []
    assert manifest["documentation"] == "https://github.com/havanissa/smart-predictive-climate"
    assert manifest["issue_tracker"] == "https://github.com/havanissa/smart-predictive-climate/issues"
    assert (COMPANION / "strings.json").read_bytes() == (COMPANION / "translations" / "en.json").read_bytes()
    assert 'STORE_KEY: Final = f"{DOMAIN}.published"' in (COMPANION / "const.py").read_text()


def test_protocol_copies_are_byte_identical():
    assert (BACKEND / "spc" / "protocol.py").read_bytes() == (COMPANION / "protocol.py").read_bytes()
    imports = _imports_of(COMPANION / "protocol.py")
    assert imports <= {"__future__", "datetime", "hashlib", "json", "math", "re", "typing"}, imports


#: Everything the App may ever send to Home Assistant. A new entry needs a deliberate change of this test.
EXPECTED_COMMANDS = {
    "ping", "get_config", "get_states", "subscribe_events", "unsubscribe_events",
    "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list", "config/auth/list",
    "manifest/get", "history/history_during_period", "recorder/statistics_during_period",
    "recorder/list_statistic_ids",
    f"{DOMAIN}/status", f"{DOMAIN}/pair", f"{DOMAIN}/unpair", f"{DOMAIN}/claim", f"{DOMAIN}/heartbeat",
    f"{DOMAIN}/publish", f"{DOMAIN}/release", f"{DOMAIN}/session", "weather/subscribe_forecast",
}


def test_the_allowlist_cannot_control_anything():
    assert ha.ALLOWED_COMMANDS == EXPECTED_COMMANDS
    assert ha.SUBSCRIPTIONS == {f"{DOMAIN}/session", "weather/subscribe_forecast"}
    assert ha.COMPANION_COMMANDS == {f"{DOMAIN}/{c}" for c in ("pair", "unpair", "claim", "heartbeat", "publish",
                                                              "release")}
    forbidden = {"call_service", "execute_script", "fire_event", "subscribe_trigger", "render_template",
                 "blueprint/save", "blueprint/import", "blueprint/substitute", "conversation/process",
                 "assist_pipeline/run", "config/core/update", "config/entity_registry/update",
                 "config/entity_registry/remove", "config/device_registry/update", "config/area_registry/create",
                 "config/area_registry/update", "config/area_registry/delete", "config/auth/create",
                 "config/auth/delete", "config/automation/config", "config/script/config", "config/scene/config",
                 "config_entries/flow", "lovelace/config/save", "auth/long_lived_access_token", "history/stream",
                 "logbook/event_stream", "recorder/import_statistics", "recorder/clear_statistics",
                 "recorder/update_statistics_metadata", "recorder/adjust_sum_statistics", "system_log/clear",
                 "supervisor/api", "hassio/update/addon", "backup/generate", "persistent_notification/dismiss",
                 "weather/convertible_units", "climate/set_temperature", "smart_occupancy_detection/publish",
                 "smart_occupancy/companion/publish"}
    assert not forbidden & ha.ALLOWED_COMMANDS
    for command in ha.ALLOWED_COMMANDS:
        assert not re.search(r"(^|/)(update|create|delete|remove|save|set|import|clear|adjust|call|fire|execute|"
                             r"run|write)($|/|_)", command), command
        assert not command.startswith(("smart_occupancy/", "smart_occupancy_detection/", "automation_builder",
                                       "supervisor", "hassio", "auth/", "config_entries", "lovelace", "blueprint"))
    # The only commands outside the plain reads belong to this App's own companion or read a forecast.
    reads = {c for c in ha.ALLOWED_COMMANDS if not c.startswith(f"{DOMAIN}/")} - {"weather/subscribe_forecast"}
    assert reads == ha.READ_COMMANDS - {f"{DOMAIN}/status"}
    assert all(c.startswith(("config/", "get_", "subscribe_events", "unsubscribe_events", "ping", "manifest/get",
                             "history/history_during_period", "recorder/statistics_during_period",
                             "recorder/list_statistic_ids")) for c in reads)
    assert all(c.endswith("/list") for c in reads if c.startswith("config/"))
    assert ha.ALLOWED_EVENT_TYPES == {"state_changed", "area_registry_updated", "device_registry_updated",
                                      "entity_registry_updated", "homeassistant_started"}


async def test_a_forbidden_command_is_refused_before_sending():
    client = ha.HomeAssistantClient(None, "ws://x", "token")  # type: ignore[arg-type]
    for command, params in (("call_service", {"domain": "climate", "service": "set_temperature"}),
                            ("subscribe_events", {"event_type": "call_service"}),
                            ("subscribe_events", {}),
                            ("fire_event", {"event_type": "x"}),
                            ("config/entity_registry/update", {"entity_id": "sensor.x"}),
                            ("weather/subscribe_forecast", {"forecast_type": "hourly", "entity_id": "weather.home"}),
                            (f"{DOMAIN}/session", {})):
        with pytest.raises(ha.NotAllowed):
            await client.command(command, **params)
    for command in ("subscribe_trigger", "subscribe_events", "call_service", "history/stream", "get_states"):
        with pytest.raises(ha.NotAllowed):
            await client.subscribe(command, {}, lambda e: None)
    assert client.commands_sent == {}  # nothing was even counted as sent
    assert not hasattr(client, "call_service") and not hasattr(client, "fire_event")


def _imports_of(path: Path) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _imports(root: Path) -> set[str]:
    return set().union(*(_imports_of(path) for path in root.rglob("*.py")))


def test_runtime_code_shares_nothing_with_the_other_apps():
    imports = _imports(BACKEND)
    assert not imports & {"sod", "sod_engine", "occupancy_core", "smart_lighting", "smart_occupancy", "ab",
                          "automation_builder", "custom_components", "homeassistant", "hassio"}, imports
    assert {p.name for p in BACKEND.iterdir() if p.is_dir() and p.name != "__pycache__"} == {"spc"}
    for path in BACKEND.rglob("*.py"):  # nor by path: no file of another App is read or put on sys.path
        text = path.read_text(encoding="utf-8")
        for other in ("smart_occupancy_detection/", "smart_lighting/", "automation_builder/", "sod_engine"):
            assert other not in text, f"{path.name}: {other}"
    companion = _imports(COMPANION)
    assert not companion & {"spc", "sod", "sod_engine", "occupancy_core", "smart_lighting", "smart_occupancy",
                            "smart_occupancy_detection", "ab", "aiohttp"}, companion
    for path in COMPANION.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "custom_components." not in text and "smart_occupancy_detection" not in text.replace(
            "``smart_occupancy_detection``", ""), path.name
    # Third-party runtime dependencies are exactly the pinned ones.
    stdlib_or_own = {"spc", "__future__"} | set(__import__("sys").stdlib_module_names)
    third_party = imports - stdlib_or_own
    pinned = {line.split("==")[0].lower() for line in (APP / "requirements.txt").read_text().splitlines()
              if "==" in line}
    assert third_party <= pinned, third_party - pinned


def test_no_device_control_path_anywhere():
    for root in (BACKEND, COMPANION):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for pattern in ("async_call(", "services.async_register", "async_register_admin_service",
                            "async_register_entity_service", 'call_service"', "call_service'", "async_fire(",
                            "hass.bus.async_fire", "hass.bus.fire", "hass.states.async_set", "states.async_set(",
                            "hass.states.set(", "async_set_updated_data", "/api/services/"):
                assert pattern not in text, f"{path.name}: {pattern}"
    assert not (COMPANION / "services.yaml").exists()
    # The companion's platforms only show values.
    assert {p.stem for p in COMPANION.glob("*.py")} == {"__init__", "api", "binary_sensor", "config_flow", "const",
                                                        "diagnostics", "entity", "hub", "protocol", "sensor"}
    assert 'PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]' in \
        (COMPANION / "__init__.py").read_text()
    from spc import protocol as P

    assert P.PLATFORMS == ("sensor", "binary_sensor")
    # Its websocket commands are the protocol's, all for administrators only.
    api = (COMPANION / "api.py").read_text()
    commands = re.findall(r'vol\.Required\("type"\): f"\{DOMAIN\}/([a-z]+)"', api)
    assert sorted(commands) == ["claim", "heartbeat", "pair", "publish", "release", "session", "status", "unpair"]
    assert api.count("@websocket_api.require_admin") == len(commands) == api.count("@websocket_api.websocket_command(")


def test_companion_is_small_and_has_no_engine():
    lines = sum(len(p.read_text().splitlines()) for p in COMPANION.glob("*.py"))
    assert lines < 1500, lines  # SOD's companion: about 1200
    companion = _imports(COMPANION)
    assert not companion & {"numpy", "scipy", "math_engine"} and "thermal" not in " ".join(
        p.name for p in COMPANION.rglob("*"))


def test_container_files():
    dockerfile = (APP / "Dockerfile").read_text()
    assert "COPY backend/ /opt/spc/" in dockerfile and "PYTHONPATH=/opt/spc" in dockerfile
    theirs = (REPO / "smart_occupancy_detection" / "Dockerfile").read_text()
    base = re.search(r"^FROM (\S+)", dockerfile, re.M).group(1)
    assert base == re.search(r"^FROM (\S+)", theirs, re.M).group(1) and ":" in base and "latest" not in base
    service = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "spc"
    run, finish = service / "run", service / "finish"
    assert "python3 -m spc" in run.read_text() and "cd /opt/spc" in run.read_text()
    assert run.stat().st_mode & 0o111 and finish.stat().st_mode & 0o111
    assert (service / "type").read_text().strip() == "longrun" and (service / "dependencies.d" / "base").exists()
    assert (APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "user" / "contents.d" / "spc").exists()
    for name in ("icon.png", "logo.png", "README.md", "CHANGELOG.md", "translations/en.yaml", ".dockerignore",
                 "requirements_test.txt", "backend/spc/__main__.py", "tests/e2e/stack.sh",
                 "tests/e2e/fake_supervisor.py", "tests/e2e/prepare.py"):
        assert (APP / name).exists(), name
    for name in ("icon.png", "logo.png"):
        assert (APP / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    requirements = (APP / "requirements.txt").read_text()
    assert all("==" in line for line in requirements.splitlines() if line and not line.startswith("#"))
    # The same pins as Smart Occupancy Detection, plus the time zone database (local comfort schedules: the base
    # image has no /usr/share/zoneinfo).
    assert requirements == (REPO / "smart_occupancy_detection" / "requirements.txt").read_text() + "tzdata==2025.2\n"
    assert yaml.safe_load((APP / "translations" / "en.yaml").read_text())["configuration"]["log_level"]["name"]
    assert yaml.safe_load((APP / "config.yaml").read_text())["schema"] == {"log_level": "list(debug|info|warning|error)"}


def test_e2e_scaffolding_is_this_apps_own():
    stack = (APP / "tests" / "e2e" / "stack.sh").read_text()
    for needed in ("spc-test-app", "spc-test-supervisor", "172.30.33.252", "spc-supervisor", "127.0.0.1:18500",
                   "spcIngressToken_XyZ789", "SPC_HA_WS_URL", "SPC_ALLOWED_CLIENTS"):
        assert needed in stack, needed
    assert "sod" not in stack.replace("sl-hassio-test", "") and "18300" not in stack and "172.30.33.250" not in stack
    assert (APP / "tests" / "e2e" / "stack.sh").stat().st_mode & 0o111
    supervisor = (APP / "tests" / "e2e" / "fake_supervisor.py").read_text()
    assert "/_test/commands" in supervisor and "recorded.append" in supervisor and "/hassio/ingress/spc" in supervisor
    assert "app_to_core" in supervisor and "core_to_app" in supervisor  # both directions, subscriptions included
    prepare = (APP / "tests" / "e2e" / "prepare.py").read_text()
    assert '"SPC App"' in prepare and "spc_llat" in prepare and f'"handler": "{DOMAIN}"' in prepare
    assert "/api/onboarding/users" in prepare


def test_static_page():
    html = (STATIC / "index.html").read_text()
    for link in ('href="#/"', 'href="#/rooms"', 'href="#/zones"', 'href="#/settings"'):
        assert link in html, link
    for element in ('<div id="banners">', '<main id="view">', '<dialog id="dialog">', '<div id="toast"'):
        assert element in html, element
    assert 'href="assets/app.css?v=__VERSION__"' in html
    assert '<script type="module" src="assets/app.js?v=__VERSION__">' in html
    assert "<head>" in html  # the place of the <base href> for Ingress
    assert not re.search(r"(?:src|href)=\"(?:https?:)?//", html)  # nothing is loaded from elsewhere
    icon = (STATIC / "icon.svg").read_text()
    assert icon.startswith("<svg") and "href" not in icon and "http://www.w3.org/2000/svg" in icon
    assert "<script" not in icon and "url(" not in icon and icon.count("http") == 1


def test_interface_has_no_inline_styles_or_scripts():
    """The Content-Security-Policy allows neither: everything is in app.css and app.js (checked when they exist)."""
    html = (STATIC / "index.html").read_text()
    assert "<style" not in html and ' style="' not in html
    assert not re.search(r"\son[a-z]+\s*=", html)  # no inline event handler
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html)  # no inline script
    assert "javascript:" not in html
    js = STATIC / "app.js"
    if js.exists():
        text = js.read_text()
        assert ' style="' not in text and "onclick=" not in text and "javascript:" not in text
        assert "eval(" not in text and "new Function(" not in text
    for other in STATIC.glob("*.html"):
        assert "<style" not in other.read_text() and ' style="' not in other.read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_interface_script_parses():
    js = STATIC / "app.js"
    if not js.exists():
        pytest.skip("app.js is not written yet")
    subprocess.run(["node", "--check", str(js)], check=True)
