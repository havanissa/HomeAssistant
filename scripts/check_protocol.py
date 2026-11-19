#!/usr/bin/env python3
"""The companion protocol module must be byte-identical on both sides (Phase A6).

    python scripts/check_protocol.py          # verify (exit 1 when they differ)
    python scripts/check_protocol.py --sync   # copy the companion's module into the App

The companion (``custom_components/smart_occupancy/protocol.py``) is the source of truth: it fences what the App may
do. The App ships its copy as ``smart_lighting/backend/smart_lighting/companion_protocol.py`` because the App
container has no access to the integration's files. Both test suites run this check.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "smart_occupancy" / "protocol.py"
COPY = ROOT / "smart_lighting" / "backend" / "smart_lighting" / "companion_protocol.py"


def identical() -> bool:
    return SOURCE.read_bytes() == COPY.read_bytes()


def main(argv: list[str]) -> int:
    if "--sync" in argv:
        shutil.copyfile(SOURCE, COPY)
        print(f"copied {SOURCE.relative_to(ROOT)} -> {COPY.relative_to(ROOT)}")
        return 0
    if identical():
        print("companion protocol: identical")
        return 0
    print(f"companion protocol differs: run `python {Path(__file__).relative_to(ROOT)} --sync`", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
