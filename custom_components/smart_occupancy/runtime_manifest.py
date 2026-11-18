"""The lighting runtime shared with the Smart Lighting App (Phase A4).

The App runs these modules of this integration, byte-identical: the lighting
controller and everything it needs (moods, profiles, planning, arbitration,
daylight, ambient levels and brightness rules, zones, shared spaces, the Room
Supervisor, switches, Priority Inputs, decisions, conflicts) and the lighting
websocket handlers, so a room behaves the same whichever side controls it and
the App validates edits with the same code.

``scripts/sync_lighting_runtime.py`` copies them into
``smart_lighting/backend/smart_occupancy/`` (the App provides the Home Assistant
interfaces they use and the ``occupancy_core`` package). Both test suites fail
when a copy differs; :func:`fingerprint` is exchanged on the lighting bridge and a
handover is refused when the App runs different code.

This module only lists files; it imports nothing from Home Assistant.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Bumped when the bridge contract of the shared runtime changes (not for every rule change: the
#: fingerprint covers those).
RUNTIME_VERSION = "1.0.0"

#: Every module of the shared lighting runtime (file names in this package).
MODULES: tuple[str, ...] = (
    "adaptive.py",
    "ambient.py",
    "ambient_levels.py",
    "arbitration.py",
    "brightness.py",
    "brightness_runtime.py",
    "brightness_websocket.py",
    "conflicts.py",
    "const.py",
    "controller.py",
    "daylight.py",
    "daylight_monitor.py",
    "decisions.py",
    "discovery.py",
    "engine.py",
    "fixtures.py",
    "health.py",
    "lighting.py",
    "lighting_config.py",
    "lighting_decisions.py",
    "lighting_sync.py",
    "lighting_websocket.py",
    "models.py",
    "mood.py",
    "occupancy.py",
    "planner.py",
    "preliminary.py",
    "preliminary_websocket.py",
    "presence.py",
    "priority.py",
    "priority_runtime.py",
    "priority_websocket.py",
    "profile_websocket.py",
    "profiles.py",
    "radar.py",
    "runtime_manifest.py",
    "supervision.py",
    "supervisor.py",
    "switch_control.py",
    "switch_discovery.py",
    "switch_inputs.py",
    "switch_websocket.py",
    "switches.py",
    "targets.py",
    "timing_validation.py",
    "validation.py",
    "zone_runtime.py",
    "zone_websocket.py",
    "zones.py",
)


def fingerprint(base: Path | None = None) -> str:
    """SHA-256 over the names and bytes of :data:`MODULES` (the same on both sides when the code is)."""
    base = base or Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in MODULES:
        data = (base / name).read_bytes()
        digest.update(name.encode())
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()
