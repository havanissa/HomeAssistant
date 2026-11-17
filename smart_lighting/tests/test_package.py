"""The App package: config.yaml, repository.yaml, Dockerfile, s6 service, build."""

from __future__ import annotations

import os
from pathlib import Path
import re

import yaml

from smart_lighting.const import APP_VERSION, DEFAULT_PORT
from smart_lighting.modules import MODULES

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
CONFIG = yaml.safe_load((APP / "config.yaml").read_text(encoding="utf-8"))


def test_repository_descriptor() -> None:
    repo = yaml.safe_load((REPO / "repository.yaml").read_text(encoding="utf-8"))
    assert repo["name"]
    assert repo["url"] == "https://github.com/havanissa/HomeAssistant"


def test_only_one_app_config_in_repository() -> None:
    # The Supervisor treats every folder with a config.yaml as an App.
    found = [
        p.relative_to(REPO).as_posix()
        for p in REPO.rglob("config.y*ml")
        if not any(part.startswith(".") or part in {"node_modules", "rootfs"} for part in p.relative_to(REPO).parts)
    ]
    assert found == ["smart_lighting/config.yaml"]


def test_required_identity() -> None:
    assert CONFIG["name"] == "Smart Occupancy & Lighting"
    assert CONFIG["slug"] == "smart_lighting"
    # Supervisor rules (supervisor/apps/const.py RE_SLUG, SLUG_MAX_LENGTH).
    assert re.fullmatch(r"[-_.A-Za-z0-9]+", CONFIG["slug"]) and len(CONFIG["slug"]) <= 60
    assert CONFIG["version"] == APP_VERSION
    assert isinstance(CONFIG["version"], str)
    assert CONFIG["description"]
    assert set(CONFIG["arch"]) == {"aarch64", "amd64"}  # armv7/armhf/i386 are deprecated


def test_ingress_panel() -> None:
    assert CONFIG["ingress"] is True
    assert CONFIG["ingress_port"] == DEFAULT_PORT
    assert CONFIG["panel_title"] == "Smart Lighting"
    assert CONFIG["panel_icon"] == "mdi:lightbulb-auto"
    assert CONFIG.get("panel_admin", True) is True


def test_no_exposed_port_and_no_extra_privileges() -> None:
    assert "ports" not in CONFIG
    assert "webui" not in CONFIG
    assert "host_network" not in CONFIG
    assert "privileged" not in CONFIG
    assert "full_access" not in CONFIG
    assert "docker_api" not in CONFIG
    assert CONFIG.get("hassio_api", False) is False
    assert "hassio_role" not in CONFIG
    assert "auth_api" not in CONFIG
    assert "map" not in CONFIG  # no access to the Home Assistant configuration folder
    assert CONFIG["homeassistant_api"] is True
    assert CONFIG["init"] is False  # S6 overlay is PID 1


def test_local_build() -> None:
    # No `image:` -> the Supervisor builds the Dockerfile on the device.
    assert "image" not in CONFIG
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    base = re.search(r"^FROM\s+(\S+)", dockerfile, re.M).group(1)
    assert base == "ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.0"
    assert "{arch}" not in base and "BUILD_FROM" not in dockerfile
    assert 'io.hass.type="app"' in dockerfile
    assert not (APP / "build.yaml").exists()  # deprecated


def test_requirements_pinned() -> None:
    lines = [
        line.strip()
        for line in (APP / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert lines
    assert all(re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][0-9A-Za-z.]*", line) for line in lines)


def test_options_schema_and_translations() -> None:
    assert CONFIG["options"] == {"log_level": "info"}
    assert CONFIG["schema"] == {"log_level": "list(debug|info|warning|error)"}
    # Supervisor list() element (supervisor/apps/options.py RE_SCHEMA_ELEMENT).
    choices = re.fullmatch(r"list\((?P<list>.+)\)", CONFIG["schema"]["log_level"]).group("list").split("|")
    assert CONFIG["options"]["log_level"] in choices
    translations = yaml.safe_load((APP / "translations" / "en.yaml").read_text(encoding="utf-8"))
    assert set(translations["configuration"]) == set(CONFIG["schema"])
    assert all(v["name"] for v in translations["configuration"].values())


def test_s6_service() -> None:
    service = APP / "rootfs/etc/s6-overlay/s6-rc.d/smart-lighting"
    assert (service / "type").read_text().strip() == "longrun"
    assert (service / "dependencies.d/base").exists()
    assert (APP / "rootfs/etc/s6-overlay/s6-rc.d/user/contents.d/smart-lighting").exists()
    for script in ("run", "finish"):
        path = service / script
        assert path.read_text().startswith("#!/command/with-contenv bashio")
        assert os.access(path, os.X_OK), script
    assert "exec python3 -m smart_lighting" in (service / "run").read_text()


def test_presentation_files() -> None:
    for name in ("README.md", "DOCS.md", "CHANGELOG.md", "icon.png", "logo.png"):
        assert (APP / name).is_file(), name
    assert (APP / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    changelog = (APP / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## {APP_VERSION}" in changelog


def test_built_interface_is_present_and_current() -> None:
    static = APP / "backend/smart_lighting/static"
    page = (static / "index.html").read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="\./(assets/[^"]+)"', page)
    assert any(a.endswith(".js") for a in assets) and any(a.endswith(".css") for a in assets)
    for asset in assets:
        assert (static / asset).is_file(), asset
    # Every asset file is referenced (no stale build output left behind).
    on_disk = {f"assets/{p.name}" for p in (static / "assets").iterdir()}
    assert on_disk == set(assets)
    # No external URLs in the shipped interface (no internet dependency).
    for path in [static / "index.html", *(static / "assets").iterdir()]:
        text = path.read_text(encoding="utf-8")
        external = set(re.findall(r"https?://[A-Za-z0-9.\-]+", text)) - {"http://www.w3.org"}
        assert not external, (path.name, external)
    # The build is newer than every frontend source file.
    built = min(p.stat().st_mtime for p in (static / "assets").iterdir())
    sources = [p for p in (APP / "frontend/src").rglob("*") if p.is_file()] + [APP / "frontend/index.html"]
    assert all(p.stat().st_mtime <= built + 1 for p in sources), "run `npm run build` in smart_lighting/frontend"


def test_frontend_navigation_matches_modules() -> None:
    router = (APP / "frontend/src/router.ts").read_text(encoding="utf-8")
    nav_keys = re.findall(r'\{ key: "([a-z]+)", title: "([A-Za-z ]+)"', router)
    assert [k for k, _ in nav_keys] == [m.key for m in MODULES]
    assert [t for _, t in nav_keys] == [
        "Dashboard", "Rooms", "Floor plan", "Presence", "Lighting", "Moods", "Switches", "Curtains", "Predictions",
        "Diagnostics", "Settings",
    ]
    assert dict(nav_keys) == {m.key: m.title for m in MODULES}
