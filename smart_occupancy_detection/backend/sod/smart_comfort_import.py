"""Reviewed, read-only import of the occupancy settings in a Smart Comfort export.

Smart Comfort's storage is another App's ``/data``: this App never reads it. The
user downloads an export in Smart Comfort and uploads it here. Accepted
documents: the core configuration export (``smart_lighting.core_config``), the
rooms backup (``smart_occupancy.rooms_backup``) and the migration backup
(``smart_lighting.migration_backup``).

Only occupancy settings are taken: rooms, their sensors with kind, role,
inversion and exclusion, per-room timing overrides, adjacent rooms and the
global timing defaults. Lights, moods, curtains, switches, climate and the
comparison helper are ignored (and counted, so the review can say so). The
uploaded document is never changed and nothing is written to Smart Comfort.
"""

from __future__ import annotations

import copy
from typing import Any

from sod_engine.types import SensorAssignment, clean_timing

from .config import ConfigError, validate_config

FORMATS = ("smart_lighting.core_config", "smart_occupancy.rooms_backup", "smart_lighting.migration_backup")
IGNORED_ROOM_KEYS = ("lights", "mood_overrides", "light_overrides", "reference_occupancy_entity")


def _occupancy_part(document: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """(rooms, global timing) of a Smart Comfort export."""
    if not isinstance(document, dict) or document.get("format") not in FORMATS:
        raise ConfigError("invalid_import", "This is not a Smart Comfort export (expected the core configuration "
                                            "export, a rooms backup or a migration backup).")
    fmt = document["format"]
    if fmt == "smart_lighting.migration_backup":
        inner = document.get("core_config")
        if isinstance(inner, dict) and inner.get("format") == "smart_lighting.core_config":
            return _occupancy_part(inner)
        options = document.get("legacy_options")
        if not isinstance(options, dict):
            raise ConfigError("invalid_import", "This migration backup contains no room configuration.")
        return options.get("rooms") or {}, options.get("occupancy") or {}
    if fmt == "smart_lighting.core_config":
        options = document.get("options")
        if not isinstance(options, dict):
            raise ConfigError("invalid_import", "This export contains no configuration.")
        return options.get("rooms") or {}, options.get("occupancy") or {}
    return document.get("rooms") or {}, document.get("occupancy") or {}


MAX_IMPORT_ROOMS = 100


def _clean_timing(raw: Any) -> dict[str, int]:
    """Exactly as Smart Comfort reads it (``occupancy_core.types.clean_timing``: int(), clamped to the range)."""
    return clean_timing(raw, fill_defaults=False)


def convert_room(area_id: str, raw: Any, name: str) -> tuple[dict[str, Any], list[str]]:
    """One Smart Comfort room in this App's form, and notes about what was left out."""
    notes: list[str] = []
    raw = raw if isinstance(raw, dict) else {}
    sensors = []
    for s in raw.get("sensors") or []:
        if not isinstance(s, dict) or not isinstance(s.get("entity_id"), str):
            continue
        # The same reading as Smart Comfort's engine: unknown kinds are "other", missing or unknown roles the
        # kind's default role.
        a = SensorAssignment.from_dict(s)
        sensors.append({"entity_id": a.entity_id, "kind": a.kind.value, "role": a.role.value,
                        "inverted": a.inverted, "enabled": a.enabled})
    ignored = [k for k in IGNORED_ROOM_KEYS if raw.get(k)]
    if ignored:
        notes.append("Ignored (not occupancy): " + ", ".join(k.replace("_", " ") for k in ignored) + ".")
    room = {"name": name, "enabled": bool(raw.get("enabled", False)), "sensors": sensors, "activity": [],
            "timing": _clean_timing(raw.get("timing")),
            "adjacent": [a for a in raw.get("adjacent_rooms") or [] if isinstance(a, str) and a != area_id]}
    usable = [s for s in sensors if s["enabled"] and s["role"] != "ignore" and s["kind"] != "door"]
    if room["enabled"] and not usable:
        room["enabled"] = False
        notes.append("Imported with detection turned off: it has no sensor that can detect presence.")
    return room, notes


def review(document: Any, current: dict[str, Any], *, area_name, area_exists, entity_exists) -> dict[str, Any]:
    rooms_raw, timing_raw = _occupancy_part(document)
    if not isinstance(rooms_raw, dict) or len(rooms_raw) > MAX_IMPORT_ROOMS:
        raise ConfigError("invalid_import", "The export's rooms are not readable.")
    rooms = []
    for area_id, raw in sorted(rooms_raw.items()):
        if not isinstance(area_id, str):
            continue
        room, notes = convert_room(area_id, raw, area_name(area_id) or area_id)
        if not room["sensors"] and not room["enabled"]:
            continue  # a Smart Comfort room without occupancy settings (lighting only)
        rooms.append({
            "area_id": area_id, "name": room["name"], "room": room, "notes": notes,
            "area_exists": area_exists(area_id), "replaces": area_id in (current.get("rooms") or {}),
            "missing_entities": [s["entity_id"] for s in room["sensors"] if not entity_exists(s["entity_id"])],
        })
    timing = _clean_timing(timing_raw)
    return {"rooms": rooms, "timing": timing, "timing_differs": any(
        (current.get("timing") or {}).get(k) != v for k, v in timing.items())}


def apply(document: Any, current: dict[str, Any], selected: Any, include_timing: bool, *, area_name) -> tuple[
        dict[str, Any], list[str]]:
    """The configuration after importing the selected rooms (and, optionally, the global timing)."""
    rooms_raw, timing_raw = _occupancy_part(document)
    if not isinstance(selected, list) or not selected or not all(isinstance(a, str) for a in selected):
        raise ConfigError("invalid", "Select at least one room to import")
    unknown = [a for a in selected if a not in rooms_raw]
    if unknown:
        raise ConfigError("invalid", f"Not in the export: {', '.join(unknown)}")
    new = copy.deepcopy(current)
    notes = []
    for area_id in selected:
        room, _ = convert_room(area_id, rooms_raw[area_id], area_name(area_id) or area_id)
        new["rooms"][area_id] = room
    if include_timing:
        new["timing"] = {**new["timing"], **_clean_timing(timing_raw)}
    for area_id, room in new["rooms"].items():
        dropped = [a for a in room.get("adjacent", []) if a not in new["rooms"]]
        if dropped:
            room["adjacent"] = [a for a in room["adjacent"] if a in new["rooms"]]
            notes.append(f"{room.get('name') or area_id}: adjacent rooms that are not configured here were left "
                         f"out ({', '.join(dropped)}).")
    return validate_config(new), notes
