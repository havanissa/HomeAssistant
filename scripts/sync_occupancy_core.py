"""Copy the shared occupancy engine into the App (its build context is smart_lighting/ only).

    python3 scripts/sync_occupancy_core.py          # copy
    python3 scripts/sync_occupancy_core.py --check  # exit 1 when the copies differ

The original is custom_components/smart_occupancy/occupancy_core (installed by
HACS with the integration). The App's copy must be byte-identical; both test
suites check it, and the bridge exchanges the package fingerprint.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "custom_components/smart_occupancy/occupancy_core"
TARGET = REPO / "smart_lighting/backend/occupancy_core"


def files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts}


def main() -> int:
    if "--check" in sys.argv:
        same = files(SOURCE) == (files(TARGET) if TARGET.is_dir() else {})
        print("identical" if same else "DIFFERENT: run scripts/sync_occupancy_core.py")
        return 0 if same else 1
    if TARGET.exists():
        shutil.rmtree(TARGET)
    shutil.copytree(SOURCE, TARGET, ignore=shutil.ignore_patterns("__pycache__"))
    print(f"copied {len(files(TARGET))} files to {TARGET.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
