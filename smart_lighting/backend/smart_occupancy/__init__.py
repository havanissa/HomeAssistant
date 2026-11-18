"""The integration's automation runtime, vendored into the Smart Lighting App (Phase A4 lighting, A5 curtains and forecasts).

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
