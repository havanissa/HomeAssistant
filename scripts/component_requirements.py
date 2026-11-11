"""Print the Python requirements Home Assistant declares for some of its own components.

The test harness (pytest-homeassistant-custom-component) pins the libraries of
the Home Assistant release it was built for. When the tests run on a newer
Home Assistant (installed over the harness), components the tests set up
(MQTT, and the Supervisor client its config flow imports) need the versions
that release declares in its manifests. Usage:

    pip install $(python scripts/component_requirements.py mqtt hassio)

Reads the manifests only; nothing is imported from the components.
"""

import importlib.util
import json
import sys
from pathlib import Path

spec = importlib.util.find_spec("homeassistant")
if spec is None or not spec.submodule_search_locations:
    sys.exit("homeassistant is not installed")
components = Path(next(iter(spec.submodule_search_locations))) / "components"
requirements: list[str] = []
for name in sys.argv[1:]:
    manifest = json.loads((components / name / "manifest.json").read_text(encoding="utf-8"))
    requirements += [r for r in manifest.get("requirements", []) if r not in requirements]
print(" ".join(requirements))
