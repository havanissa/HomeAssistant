"""The App-only engine modules (Phase A6): the ones ``runtime_manifest.MODULES`` does not list.

``runtime_manifest.py`` and the modules it lists are the 0.9.0 runtime, byte-identical, so that the App's
fingerprint still matches a 0.9.0 installation during the migration (the bridges' handover checks it). Everything the
App added since lives here. Both lists are checked against the host interface by the App tests.
"""

from __future__ import annotations

from .runtime_manifest import MODULES as RUNTIME_MODULES

APP_MODULES: tuple[str, ...] = (
    "app_manifest.py",
    "brightness_delays.py",
    "comparison.py",
    "curtains/multisun.py",
    "entity_ids.py",
    "light_identify.py",
    "light_inspector.py",
    "prediction.py",
    "presence_api.py",
    "presence_fields.py",
    "presence_log.py",
    "problems.py",
    "rooms_api.py",
    "snapshot.py",
)

#: Every engine module of the App.
ALL_MODULES: tuple[str, ...] = tuple(sorted({*RUNTIME_MODULES, *APP_MODULES}))

#: The runtime fingerprint of Smart Occupancy & Lighting 0.9.0 (the last monolithic release). Until App 0.7.x the
#: listed modules stayed byte-identical to it; App 0.8.0 (multi-sensor sunlight, cross-room switch targets) changed
#: them on purpose, so 0.9.0 refuses lighting, curtain and forecast handovers to it and the migration wizard says
#: so (occupancy keeps its own, unchanged, engine fingerprint).
FINGERPRINT_0_9_0 = "227a7fd829eb512237871ca5f542c7ce8757f48e499dc1b03b9b433e6b7afd9f"
