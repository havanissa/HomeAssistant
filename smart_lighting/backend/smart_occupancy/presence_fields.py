"""The editor's sensor kinds with their allowed domains (the rooms API's schema; the 0.9.0 options flow's fields).

App-only module (Phase A6): ``presence.py`` is part of the 0.9.0 runtime fingerprint and stays byte-identical so the
App can still hand over from a 0.9.0 installation during the migration.
"""

from __future__ import annotations

from .presence import SensorKind

KIND_FIELDS: dict[str, tuple[SensorKind, list[str] | None]] = {
    "pir": (SensorKind.PIR, ["binary_sensor"]),
    "radar": (SensorKind.RADAR, ["binary_sensor"]),
    "door": (SensorKind.DOOR, ["binary_sensor"]),
    "pc_activity": (
        SensorKind.PC_ACTIVITY,
        ["binary_sensor", "sensor", "input_boolean", "switch", "media_player", "device_tracker"],
    ),
    "bed": (SensorKind.BED, ["binary_sensor", "input_boolean"]),
    "other": (SensorKind.OTHER, None),
}
