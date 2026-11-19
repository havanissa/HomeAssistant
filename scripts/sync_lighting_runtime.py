"""Copy the shared automation runtime (lighting, curtains, forecasts) into the App (its build context is smart_lighting/ only).

    python3 scripts/sync_lighting_runtime.py          # copy
    python3 scripts/sync_lighting_runtime.py --check  # exit 1 when the copies differ

The originals are the modules of custom_components/smart_occupancy listed in its
``runtime_manifest.MODULES``. The App's copies in
smart_lighting/backend/smart_occupancy/ must be byte-identical; both test suites
check it, and the bridges exchange the fingerprint. The copy's ``__init__.py`` is
the App's own (it is not part of the manifest): it makes the App's
``occupancy_core`` package the one these modules import. Manifest entries may
name files in sub-packages (``curtains/auto.py``); those folders are mirrored.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "custom_components/smart_occupancy"
TARGET = REPO / "smart_lighting/backend/smart_occupancy"

INIT = '''"""The integration's automation runtime, vendored into the Smart Lighting App (Phase A4 lighting, A5 curtains and forecasts).

Every other ``.py`` file in this folder and its sub-packages is a byte-identical
copy of the integration's module of the same path (``runtime_manifest.MODULES``),
made by ``scripts/sync_lighting_runtime.py``. Do not edit them here.

They import Home Assistant through the App's host interface (the ``homeassistant``
package next to this one) and the occupancy engine through ``occupancy_core``: the
App's own copy is registered here as this package's ``occupancy_core``, so the
lighting runtime and the App's occupancy runtime share one set of classes.
"""

import importlib
import sys

import occupancy_core as _core

sys.modules[__name__ + ".occupancy_core"] = _core
for _name in ("engine", "golden", "house", "protocol", "types", "wire"):
    sys.modules[f"{__name__}.occupancy_core.{_name}"] = importlib.import_module(f"occupancy_core.{_name}")
'''


def manifest() -> tuple[str, ...]:
    """MODULES from the integration's runtime_manifest.py, read without importing it."""
    tree = ast.parse((SOURCE / "runtime_manifest.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "MODULES":
            return tuple(ast.literal_eval(node.value))
    raise SystemExit("runtime_manifest.MODULES not found")


def expected() -> dict[str, bytes]:
    out = {name: (SOURCE / name).read_bytes() for name in manifest()}
    out["__init__.py"] = INIT.encode()
    return out


def actual() -> dict[str, bytes]:
    if not TARGET.is_dir():
        return {}
    return {p.relative_to(TARGET).as_posix(): p.read_bytes() for p in TARGET.rglob("*.py")
            if p.is_file() and "__pycache__" not in p.parts}


def main() -> int:
    want = expected()
    if "--check" in sys.argv:
        same = actual() == want
        if not same:
            extra = sorted(set(actual()) - set(want))
            missing = sorted(set(want) - set(actual()))
            changed = sorted(k for k in set(want) & set(actual()) if actual()[k] != want[k])
            print(f"DIFFERENT: run scripts/sync_lighting_runtime.py (extra={extra} missing={missing} changed={changed})")
        else:
            print("identical")
        return 0 if same else 1
    TARGET.mkdir(exist_ok=True)
    for path in TARGET.rglob("*.py"):
        if path.is_file() and "__pycache__" not in path.parts and path.relative_to(TARGET).as_posix() not in want:
            path.unlink()
    for name, data in want.items():
        target = TARGET / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_bytes() != data:
            target.write_bytes(data)
    print(f"copied {len(want)} files to {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
