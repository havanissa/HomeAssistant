"""Packaging and architecture: versions, independence from the other Apps, container files, interface rules."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from aie.const import APP_VERSION

APP = Path(__file__).resolve().parents[1]
REPO = APP.parent
STATIC = APP / "backend" / "aie" / "static"
OTHERS = ("smart_lighting", "smart_occupancy_detection", "automation_builder", "smart_predictive_climate")


def test_versions_agree():
    config = yaml.safe_load((APP / "config.yaml").read_text())
    assert config["version"] == APP_VERSION
    assert re.search(r"^## (\S+)", (APP / "CHANGELOG.md").read_text(), re.M).group(1) == APP_VERSION
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "Dockerfile").read_text()
    assert f"BUILD_VERSION={APP_VERSION}" in (APP / "tests" / "e2e" / "stack.sh").read_text()


def test_app_identity_is_independent():
    ours = yaml.safe_load((APP / "config.yaml").read_text())
    others = [yaml.safe_load((REPO / name / "config.yaml").read_text()) for name in OTHERS]
    assert ours["slug"] == "automation_import_export" and ours["slug"] not in {o["slug"] for o in others}
    assert ours["name"] not in {o["name"] for o in others}
    assert ours["ingress"] is True and "ports" not in ours and ours.get("hassio_api") is False
    assert ours["panel_admin"] is True and ours["homeassistant_api"] is True
    # Home Assistant's configuration folder, read-only; nothing else (no share, no other Apps' data)
    assert ours["map"] == [{"type": "homeassistant_config", "read_only": True}]


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
    assert not imports & {"ab", "sod", "spc", "occupancy_core", "smart_lighting", "smart_occupancy", "custom_components",
                          "homeassistant", "automation_builder"}
    assert imports <= {"__future__", "aiohttp", "yaml", "asyncio", "collections", "contextlib", "copy", "dataclasses",
                       "datetime", "hashlib", "io", "ipaddress", "json", "logging", "os", "pathlib", "re", "secrets",
                       "stat", "time", "typing", "urllib", "uuid", "zipfile"}, imports
    assert not (REPO / "custom_components" / "automation_import_export").exists()  # no companion integration


def test_the_app_never_writes_into_home_assistants_configuration_folder():
    for path in (APP / "backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "haconfig.py":  # the only module that touches /homeassistant: reads only
            assert not re.search(r"write_text|write_bytes|open\(|os\.replace|unlink|mkdir|rmtree", text)
            # the only file of .storage it reads: the helper entries' options (usage search)
            assert set(re.findall(r'"\.storage" / "([a-z_.]+)"', text)) == {"core.config_entries"}
            assert '"secrets.yaml"' in text  # never read (SKIP_FILES)
        else:
            assert ".storage" not in text, path.name
        for pattern in ('"/api/services', "/api/states/", "fire_event"):
            assert pattern not in text, f"{path.name}: {pattern}"


def test_container_files():
    dockerfile = (APP / "Dockerfile").read_text()
    assert "COPY backend/ /opt/aie/" in dockerfile and "PYTHONPATH=/opt/aie" in dockerfile
    run = APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "aie" / "run"
    assert "python3 -m aie" in run.read_text() and "umask 077" in run.read_text() and run.stat().st_mode & 0o111
    assert (APP / "rootfs" / "etc" / "s6-overlay" / "s6-rc.d" / "user" / "contents.d" / "aie").exists()
    for name in ("icon.png", "logo.png", "README.md", "DOCS.md", "CHANGELOG.md", "translations/en.yaml"):
        assert (APP / name).exists(), name
    requirements = (APP / "requirements.txt").read_text()
    assert all("==" in line for line in requirements.splitlines() if line and not line.startswith("#"))


SCRIPTS = ("app.js", "ui.js", "export.js", "restore.js", "registry.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_interface_scripts_parse():
    for name in SCRIPTS:
        subprocess.run(["node", "--check", str(STATIC / name)], check=True)


def test_interface_has_no_inline_styles_scripts_or_external_resources():
    """The Content-Security-Policy allows neither: everything is in app.css and the script files."""
    html = (STATIC / "index.html").read_text()
    js = "".join((STATIC / name).read_text() for name in SCRIPTS)
    assert ' style="' not in js and "<style" not in html and "onclick=" not in js + html
    assert "http://" not in js + html and "https://" not in js + html
    assert "innerHTML" in js and "esc(" in js
    assert "eval(" not in js and "new Function" not in js


def test_the_other_apps_are_untouched():
    """This App adds its own folder (and repository docs); it changes no file of the other Apps."""
    result = subprocess.run(["git", "status", "--porcelain", "--", *OTHERS, "custom_components"], cwd=REPO,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == ""
    result = subprocess.run(["git", "diff", "--name-only", "origin/main...HEAD", "--", *OTHERS, "custom_components"],
                            cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0 or result.stdout.strip() == ""
