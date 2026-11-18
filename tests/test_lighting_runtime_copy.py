"""Phase A4: the App runs a byte-identical copy of this integration's lighting runtime (runtime_manifest.py)."""

from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

from custom_components.smart_occupancy.runtime_manifest import MODULES, fingerprint

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "custom_components/smart_occupancy"


def test_the_apps_copy_is_identical() -> None:
    result = subprocess.run([sys.executable, str(REPO / "scripts/sync_lighting_runtime.py"), "--check"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, "run scripts/sync_lighting_runtime.py: " + result.stdout
    assert fingerprint() == fingerprint(REPO / "smart_lighting/backend/smart_occupancy")


def test_the_manifest_covers_every_module_the_runtime_imports() -> None:
    """The closure of the controller and the lighting handlers stays inside the manifest (else the App breaks)."""
    names = set(MODULES)
    for name in MODULES:
        tree = ast.parse((PACKAGE / name).read_text())
        guarded = {id(n) for node in ast.walk(tree) if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(
            node.test) for n in ast.walk(node)}
        for node in ast.walk(tree):
            if id(node) in guarded or not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            module = (node.module or "").split(".")[0]
            if module and module != "occupancy_core":
                assert f"{module}.py" in names, f"{name} imports .{module}, which is not in runtime_manifest.MODULES"
