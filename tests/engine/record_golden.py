"""Record the golden scenarios' expected transitions with the FROZEN 0.6.10 engine (run by hand only).

    python -m tests.record_golden

Used once to create ``occupancy_core/golden/*.json`` from their inputs; a new
scenario is recorded the same way. The parity tests then require the shared
engine to reproduce exactly these transitions.
"""

from __future__ import annotations

import json

from custom_components.smart_occupancy.occupancy_core import golden

from .legacy_engine_0610 import LegacyHouse


def main() -> None:
    for path in sorted(golden.GOLDEN_DIR.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        scenario["expect"] = golden.run(scenario, LegacyHouse)
        path.write_text(json.dumps(scenario, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{path.name}: {len(scenario['expect'])} transitions")


if __name__ == "__main__":
    main()
