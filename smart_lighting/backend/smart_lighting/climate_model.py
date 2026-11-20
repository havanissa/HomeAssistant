"""Climate (Phase A7.0): the configuration model, units, discovery rules and measurement quality.

Pure functions only (no I/O, no Home Assistant connection), so the rules can be
tested on their own and reused by the later climate phases.

Smart Comfort only *observes* heating in A7.0. Nothing in this module (or in
the App) can send a thermostat command: the App's Home Assistant client has no
service call, and the companion's fenced executor accepts light and cover
services only.

Configuration (``/data/climate/config.json``, format ``smart_comfort.climate``)::

    {
      "format": "smart_comfort.climate", "schema": 1,
      "mode": "observing",                       # the only mode in A7.0
      "units": {"display": "auto"},              # auto (Home Assistant's unit system) | celsius | fahrenheit
      "defaults": {"comfort_c": 21.0, "setback_c": 18.0, "min_target_c": 7.0, "max_target_c": 24.0,
                   "stale_minutes": 60},
      "observation": {"enabled": true, "retention_days": 90, "backfill_days": 7, "report_minutes": 15},
      "outdoor": {"entity_id": "sensor.outdoor_temperature", "uid": "<registry id>"} | null,
      "rooms": {
        "<area_id>": {
          "thermostats": [{"entity_id": "climate.x", "uid": "...", "kind": "trv",
                           "capabilities": {...}}],      # a snapshot taken when it was saved
          "primary_sensor": {"entity_id": "sensor.x", "uid": "..."} | null,
          "secondary_sensors": [{"entity_id": ..., "uid": ...}],
          "openings": [{"entity_id": "binary_sensor.window", "uid": ...}],
          "overrides": {"comfort_c": null, "setback_c": null, "min_target_c": null, "max_target_c": null,
                        "stale_minutes": null}
        }
      },
      "zones": [{"id": "open_plan", "name": "Open plan", "rooms": ["kitchen", "living"],
                 "layout": "open_plan", "shared_thermostats": ["climate.x"], "heating_circuit": null,
                 "origin": "manual"}],
      "prediction": {"min_history_days": 14},
      "updated_at": "..."
    }

Every temperature is stored in **°C**; the interface shows the unit chosen in
``units.display``. A room's own temperature comes only from its temperature
sensors; what a thermostat reports about itself (``current_temperature``) is
kept as a separate measurement and is never used as the room temperature
without saying so. A thermostat's ``hvac_action`` is *reported* activity; when
a device does not report it, the activity is unknown: a setpoint above the
room temperature is a possible heat demand, never "heating".

Entity references carry the entity registry id (``uid``) when Home Assistant
has one, so a renamed entity is followed (see ``ClimateService.follow_renames``).
"""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
import re
from typing import Any

FORMAT = "smart_comfort.climate"
EXPORT_FORMAT = "smart_comfort.climate_export"
BACKUP_FORMAT = "smart_comfort.climate_backup"
SCHEMA_VERSION = 1

MODES = ("observing",)  # A7.3 adds a reviewed "automatic"; A7.0 accepts nothing else
DISPLAY_UNITS = ("auto", "celsius", "fahrenheit")
THERMOSTAT_KINDS = {
    "trv": "Radiator valve (TRV)",
    "room_thermostat": "Room thermostat",
    "generic": "Generic thermostat (Home Assistant)",
    "heat_pump": "Heat pump / air conditioner",
    "other": "Other heating device",
}
ZONE_LAYOUTS = {
    "single": "A single room",
    "connected": "Connected rooms",
    "open_plan": "Open-plan space",
}
#: Safe limits for any target Smart Comfort will ever consider (°C). A7.3 clamps every command to the room's bounds.
HARD_MIN_C = 5.0
HARD_MAX_C = 30.0
DEFAULTS: dict[str, Any] = {"comfort_c": 21.0, "setback_c": 18.0, "min_target_c": 7.0, "max_target_c": 24.0,
                            "stale_minutes": 60}
OBSERVATION_DEFAULTS: dict[str, Any] = {"enabled": True, "retention_days": 90, "backfill_days": 7,
                                        "report_minutes": 15}
PREDICTION_DEFAULTS: dict[str, Any] = {"min_history_days": 14}
OVERRIDE_KEYS = ("comfort_c", "setback_c", "min_target_c", "max_target_c", "stale_minutes")
TEMP_KEYS = ("comfort_c", "setback_c", "min_target_c", "max_target_c")
LIMITS = {"rooms": 100, "thermostats": 12, "secondary_sensors": 8, "openings": 24, "zones": 50, "zone_rooms": 20,
          "name": 60}
RANGES: dict[str, tuple[float, float]] = {
    "stale_minutes": (5, 1440), "retention_days": (7, 730), "backfill_days": (0, 30), "report_minutes": (5, 120),
    "min_history_days": (3, 90),
}

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
AREA_ID = re.compile(r"^[\w\-]{1,255}$")
ZONE_ID = re.compile(r"^[a-z0-9_]{1,64}$")
REVISION = re.compile(r"^[0-9a-f]{16}$")

#: Climate entity features (homeassistant.components.climate.ClimateEntityFeature).
FEATURE_TARGET_TEMPERATURE = 1
FEATURE_TARGET_TEMPERATURE_RANGE = 2
FEATURE_PRESET_MODE = 16
FEATURE_TURN_OFF = 128
FEATURE_TURN_ON = 256
HVAC_ACTIONS = {"off", "heating", "cooling", "drying", "idle", "fan", "preheating", "defrosting"}
HEATING_ACTIONS = {"heating", "preheating"}
OPENING_CLASSES = {"window": "window", "door": "door", "opening": "opening", "garage_door": "door"}
#: Plausible readings (°C). Outside them a reading is kept but marked implausible.
PLAUSIBLE = {"indoor": (-20.0, 50.0), "outdoor": (-60.0, 60.0), "target": (0.0, 40.0)}
OUTDOOR_WORDS = ("outdoor", "outside", "exterior", "external_temp", "garden", "uti", "úti", "ute", "ude", "aussen",
                 "außen", "balcony", "porch")
NOT_ROOM_WORDS = ("cpu", "processor", "chip", "soc", "battery", "dew", "feels", "apparent", "water", "boiler",
                  "flow", "return", "pipe", "radiator_surface", "device_temperature", "internal", "fridge", "freezer",
                  "router", "nas", "disk", "gpu", "probe")
TRV_WORDS = ("trv", "radiator", "thermostatic", "valve", "ally", "etrv", "spirit", "eurotronic", "tado smart radiator",
             "sea801", "sea802", "ts0601", "trvzb", "bth-ra", "rbsh-trv", "thermostat_radiator", "srts", "e3")
EXTERNAL_INPUT_WORDS = ("external_temperature", "external_measured", "remote_temperature", "external_sensor",
                        "external_temp", "room_temperature_input", "external_measured_room_sensor")


class ClimateConfigError(Exception):
    """The configuration is not valid; ``errors`` lists every problem with its path."""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        super().__init__("; ".join(f"{e['path']}: {e['message']}" for e in errors[:5]))
        self.errors = errors


# ------------------------------------------------------------------ units
def to_celsius(value: Any, unit: str | None) -> float | None:
    """A temperature in °C, or None when it is not a number or the unit is not a temperature unit."""
    number = as_float(value)
    if number is None:
        return None
    unit = (unit or "").strip()
    if unit in ("°C", "C", "℃"):
        return number
    if unit in ("°F", "F", "℉"):
        return (number - 32.0) * 5.0 / 9.0
    if unit == "K":
        return number - 273.15
    return None


def from_celsius(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    return value * 9.0 / 5.0 + 32.0 if unit == "°F" else value


def as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def display_unit(config: dict[str, Any], ha_unit: str | None) -> str:
    choice = ((config.get("units") or {}).get("display")) or "auto"
    if choice == "celsius":
        return "°C"
    if choice == "fahrenheit":
        return "°F"
    return "°F" if ha_unit == "°F" else "°C"


# ---------------------------------------------------------------- defaults
def empty_config() -> dict[str, Any]:
    return {"format": FORMAT, "schema": SCHEMA_VERSION, "mode": "observing", "units": {"display": "auto"},
            "defaults": dict(DEFAULTS), "observation": dict(OBSERVATION_DEFAULTS), "outdoor": None, "rooms": {},
            "zones": [], "prediction": dict(PREDICTION_DEFAULTS), "updated_at": None}


def empty_room() -> dict[str, Any]:
    return {"thermostats": [], "primary_sensor": None, "secondary_sensors": [], "openings": [],
            "overrides": {k: None for k in OVERRIDE_KEYS}}


def revision_of(config: dict[str, Any]) -> str:
    body = {k: v for k, v in config.items() if k != "updated_at"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def migrate(raw: Any) -> tuple[dict[str, Any], list[int]]:
    """(config, schemas migrated from). Schema 1 is the first; newer files are refused (``newer_format``)."""
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise ClimateConfigError([{"path": "format", "message": "This is not a Smart Comfort climate configuration",
                                   "code": "invalid_file"}])
    schema = raw.get("schema")
    if not isinstance(schema, int) or schema < 1:
        raise ClimateConfigError([{"path": "schema", "message": "Unknown schema version", "code": "invalid_file"}])
    if schema > SCHEMA_VERSION:
        raise ClimateConfigError([{"path": "schema", "message": f"This configuration was written by a newer App "
                                   f"(schema {schema}); update the App to read it.", "code": "newer_format"}])
    return copy.deepcopy(raw), []


# --------------------------------------------------------------- validation
class _V:
    def __init__(self) -> None:
        self.errors: list[dict[str, str]] = []

    def err(self, path: str, message: str) -> None:
        self.errors.append({"path": path, "message": message, "code": "invalid"})

    def number(self, path: str, value: Any, lo: float, hi: float, *, optional: bool = False,
               integer: bool = False) -> float | int | None:
        if value is None and optional:
            return None
        number = as_float(value)
        if number is None:
            self.err(path, "must be a number")
            return None
        if number < lo or number > hi:
            self.err(path, f"must be between {lo:g} and {hi:g}")
            return None
        return int(round(number)) if integer else round(number, 2)


def _ref(v: _V, path: str, value: Any, domains: tuple[str, ...]) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = {"entity_id": value}
    if not isinstance(value, dict) or not isinstance(value.get("entity_id"), str) \
            or not ENTITY_ID.match(value["entity_id"]):
        v.err(path, "must be an entity id")
        return None
    entity_id = value["entity_id"]
    if entity_id.split(".", 1)[0] not in domains:
        v.err(path, f"must be a {' or '.join(d + '.*' for d in domains)} entity")
        return None
    uid = value.get("uid")
    return {"entity_id": entity_id, "uid": uid if isinstance(uid, str) and 0 < len(uid) <= 64 else None}


def _refs(v: _V, path: str, value: Any, domains: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        v.err(path, "must be a list")
        return []
    if len(value) > limit:
        v.err(path, f"at most {limit} entities")
        value = value[:limit]
    out, seen = [], set()
    for i, item in enumerate(value):
        ref = _ref(v, f"{path}[{i}]", item, domains)
        if ref is None:
            continue
        if ref["entity_id"] in seen:
            v.err(f"{path}[{i}]", f"{ref['entity_id']} is listed twice")
            continue
        seen.add(ref["entity_id"])
        out.append(ref)
    return out


def _temps(v: _V, path: str, raw: Any, *, optional: bool) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in TEMP_KEYS:
        value = raw.get(key, None if optional else DEFAULTS[key])
        out[key] = v.number(f"{path}.{key}", value, HARD_MIN_C, HARD_MAX_C, optional=optional)
    lo, hi = RANGES["stale_minutes"]
    out["stale_minutes"] = v.number(f"{path}.stale_minutes", raw.get("stale_minutes", None if optional
                                                                         else DEFAULTS["stale_minutes"]),
                                    lo, hi, optional=optional, integer=True)
    return out


def _check_order(v: _V, path: str, t: dict[str, Any]) -> None:
    """min ≤ setback ≤ comfort ≤ max for the values that are set (an override is checked with its defaults)."""
    lo, sb, cf, hi = t.get("min_target_c"), t.get("setback_c"), t.get("comfort_c"), t.get("max_target_c")
    if lo is not None and hi is not None and lo >= hi:
        v.err(f"{path}.min_target_c", "the minimum target must be below the maximum target")
    if sb is not None and cf is not None and sb > cf:
        v.err(f"{path}.setback_c", "the setback temperature must not be above the comfort temperature")
    for key, value in (("comfort_c", cf), ("setback_c", sb)):
        if value is None:
            continue
        if lo is not None and value < lo:
            v.err(f"{path}.{key}", "must not be below the minimum target")
        if hi is not None and value > hi:
            v.err(f"{path}.{key}", "must not be above the maximum target")


def effective_targets(config: dict[str, Any], area_id: str) -> dict[str, Any]:
    """The defaults with a room's overrides applied."""
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (config.get("defaults") or {}).items() if v is not None})
    room = (config.get("rooms") or {}).get(area_id) or {}
    out.update({k: v for k, v in (room.get("overrides") or {}).items() if v is not None})
    return out


def validate(raw: Any) -> dict[str, Any]:
    """A clean, complete configuration, or ClimateConfigError listing every problem."""
    v = _V()
    if not isinstance(raw, dict):
        raise ClimateConfigError([{"path": "", "message": "must be an object", "code": "invalid"}])
    out = empty_config()
    if raw.get("mode", "observing") not in MODES:
        v.err("mode", "only Observing is available in this version: Smart Comfort does not control thermostats yet")
    units = raw.get("units") if isinstance(raw.get("units"), dict) else {}
    if units.get("display", "auto") not in DISPLAY_UNITS:
        v.err("units.display", "must be auto, celsius or fahrenheit")
    else:
        out["units"] = {"display": units.get("display", "auto")}
    out["defaults"] = _temps(v, "defaults", raw.get("defaults"), optional=False)
    _check_order(v, "defaults", out["defaults"])
    obs = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    if not isinstance(obs.get("enabled", True), bool):
        v.err("observation.enabled", "must be true or false")
    out["observation"] = {
        "enabled": obs.get("enabled", True) is not False,
        **{k: v.number(f"observation.{k}", obs.get(k, OBSERVATION_DEFAULTS[k]), *RANGES[k], integer=True)
           for k in ("retention_days", "backfill_days", "report_minutes")},
    }
    pred = raw.get("prediction") if isinstance(raw.get("prediction"), dict) else {}
    out["prediction"] = {"min_history_days": v.number("prediction.min_history_days",
                                                      pred.get("min_history_days", 14),
                                                      *RANGES["min_history_days"], integer=True)}
    out["outdoor"] = _ref(v, "outdoor", raw.get("outdoor"), ("sensor", "weather"))

    rooms_raw = raw.get("rooms") if isinstance(raw.get("rooms"), dict) else {}
    if raw.get("rooms") is not None and not isinstance(raw.get("rooms"), dict):
        v.err("rooms", "must be an object keyed by area id")
    if len(rooms_raw) > LIMITS["rooms"]:
        v.err("rooms", f"at most {LIMITS['rooms']} rooms")
    thermostat_room: dict[str, str] = {}
    primary_room: dict[str, str] = {}
    for area_id, room_raw in sorted(rooms_raw.items()):
        path = f"rooms.{area_id}"
        if not isinstance(area_id, str) or not AREA_ID.match(area_id):
            v.err(path, "invalid area id")
            continue
        if not isinstance(room_raw, dict):
            v.err(path, "must be an object")
            continue
        room = empty_room()
        thermostats = []
        for i, item in enumerate((room_raw.get("thermostats") or [])[:LIMITS["thermostats"]]
                                 if isinstance(room_raw.get("thermostats"), list) else []):
            ref = _ref(v, f"{path}.thermostats[{i}]", item, ("climate",))
            if ref is None:
                continue
            other = thermostat_room.get(ref["entity_id"])
            if other is not None:
                v.err(f"{path}.thermostats[{i}]", f"{ref['entity_id']} is already assigned to the room {other}; a "
                      "thermostat belongs to one room (use a thermal zone for a shared space)")
                continue
            thermostat_room[ref["entity_id"]] = area_id
            kind = item.get("kind") if isinstance(item, dict) else None
            ref["kind"] = kind if kind in THERMOSTAT_KINDS else "other"
            caps = item.get("capabilities") if isinstance(item, dict) else None
            ref["capabilities"] = clean_capabilities(caps) if isinstance(caps, dict) else None
            thermostats.append(ref)
        if isinstance(room_raw.get("thermostats"), list) and len(room_raw["thermostats"]) > LIMITS["thermostats"]:
            v.err(f"{path}.thermostats", f"at most {LIMITS['thermostats']} heating devices per room")
        room["thermostats"] = thermostats
        room["primary_sensor"] = _ref(v, f"{path}.primary_sensor", room_raw.get("primary_sensor"), ("sensor",))
        room["secondary_sensors"] = _refs(v, f"{path}.secondary_sensors", room_raw.get("secondary_sensors"),
                                          ("sensor",), LIMITS["secondary_sensors"])
        primary = (room["primary_sensor"] or {}).get("entity_id")
        if primary and any(s["entity_id"] == primary for s in room["secondary_sensors"]):
            v.err(f"{path}.secondary_sensors", f"{primary} is already the primary sensor")
        if primary:
            if primary in primary_room:
                v.err(f"{path}.primary_sensor", f"{primary} is already the primary sensor of {primary_room[primary]}")
            primary_room[primary] = area_id
        room["openings"] = _refs(v, f"{path}.openings", room_raw.get("openings"), ("binary_sensor",),
                                 LIMITS["openings"])
        room["overrides"] = _temps(v, f"{path}.overrides", room_raw.get("overrides"), optional=True)
        merged = {**out["defaults"], **{k: x for k, x in room["overrides"].items() if x is not None}}
        if any(x is not None for x in room["overrides"].values()):
            _check_order(v, f"{path}.overrides", merged)
        out["rooms"][area_id] = room

    zones_raw = raw.get("zones") if isinstance(raw.get("zones"), list) else []
    if raw.get("zones") is not None and not isinstance(raw.get("zones"), list):
        v.err("zones", "must be a list")
    if len(zones_raw) > LIMITS["zones"]:
        v.err("zones", f"at most {LIMITS['zones']} thermal zones")
    zone_of: dict[str, str] = {}
    ids: set[str] = set()
    for i, z in enumerate(zones_raw[:LIMITS["zones"]]):
        path = f"zones[{i}]"
        if not isinstance(z, dict):
            v.err(path, "must be an object")
            continue
        zone_id = z.get("id")
        if not isinstance(zone_id, str) or not ZONE_ID.match(zone_id) or zone_id in ids:
            v.err(f"{path}.id", "must be a unique id (lower-case letters, digits and _)")
            continue
        ids.add(zone_id)
        name = str(z.get("name") or "").strip()[:LIMITS["name"]]
        if not name:
            v.err(f"{path}.name", "a thermal zone needs a name")
        rooms = [r for r in z.get("rooms") or [] if isinstance(r, str)] if isinstance(z.get("rooms"), list) else []
        if not rooms:
            v.err(f"{path}.rooms", "a thermal zone needs at least one room")
        if len(rooms) > LIMITS["zone_rooms"]:
            v.err(f"{path}.rooms", f"at most {LIMITS['zone_rooms']} rooms")
        for r in rooms:
            if r not in out["rooms"]:
                v.err(f"{path}.rooms", f"{r} is not a Smart Comfort climate room")
            elif r in zone_of:
                v.err(f"{path}.rooms", f"{r} is already in the thermal zone {zone_of[r]}")
            else:
                zone_of[r] = zone_id
        layout = z.get("layout") if z.get("layout") in ZONE_LAYOUTS else ("single" if len(rooms) == 1 else "connected")
        if layout == "single" and len(rooms) > 1:
            v.err(f"{path}.layout", "a single-room zone has exactly one room")
        shared = [s for s in z.get("shared_thermostats") or [] if isinstance(s, str)] \
            if isinstance(z.get("shared_thermostats"), list) else []
        for s in shared:
            if thermostat_room.get(s) not in rooms:
                v.err(f"{path}.shared_thermostats", f"{s} is not a heating device of one of the zone's rooms")
        circuit = z.get("heating_circuit")
        circuit = str(circuit).strip()[:LIMITS["name"]] or None if circuit else None
        origin = z.get("origin") if z.get("origin") in ("manual", "open_plan", "adjacent", "shared_thermostat") \
            else "manual"
        out["zones"].append({"id": zone_id, "name": name, "rooms": list(dict.fromkeys(rooms)), "layout": layout,
                             "shared_thermostats": list(dict.fromkeys(shared)), "heating_circuit": circuit,
                             "origin": origin})
    if v.errors:
        raise ClimateConfigError(v.errors)
    out["updated_at"] = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None
    return out


def zone_of(config: dict[str, Any], area_id: str) -> dict[str, Any] | None:
    return next((z for z in config.get("zones") or [] if area_id in z["rooms"]), None)


def entity_ids(config: dict[str, Any]) -> set[str]:
    """Every entity the configuration names."""
    out: set[str] = set()
    if config.get("outdoor"):
        out.add(config["outdoor"]["entity_id"])
    for room in (config.get("rooms") or {}).values():
        for t in room["thermostats"]:
            out.add(t["entity_id"])
        if room["primary_sensor"]:
            out.add(room["primary_sensor"]["entity_id"])
        out.update(s["entity_id"] for s in room["secondary_sensors"])
        out.update(o["entity_id"] for o in room["openings"])
    return out


def rename(config: dict[str, Any], old: str, new: str) -> bool:
    """Follow an entity id rename everywhere in the configuration (in place). True when something changed."""
    changed = False

    def fix(ref: dict[str, Any] | None) -> None:
        nonlocal changed
        if ref and ref.get("entity_id") == old:
            ref["entity_id"] = new
            changed = True

    fix(config.get("outdoor"))
    for room in (config.get("rooms") or {}).values():
        for ref in [*room["thermostats"], room["primary_sensor"], *room["secondary_sensors"], *room["openings"]]:
            fix(ref)
    for zone in config.get("zones") or []:
        if old in zone["shared_thermostats"]:
            zone["shared_thermostats"] = [new if s == old else s for s in zone["shared_thermostats"]]
            changed = True
    return changed


def refs(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every entity reference (the dicts themselves), for following renames by registry id."""
    out = [config["outdoor"]] if config.get("outdoor") else []
    for room in (config.get("rooms") or {}).values():
        out += [*room["thermostats"], *([room["primary_sensor"]] if room["primary_sensor"] else []),
                *room["secondary_sensors"], *room["openings"]]
    return out


# -------------------------------------------------------------------- diff
def _flat(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, x in value.items():
            if k in ("updated_at", "uid", "capabilities"):
                continue
            out.update(_flat(x, f"{prefix}.{k}" if prefix else str(k)))
        return out
    if isinstance(value, list) and value and all(isinstance(x, dict) and "entity_id" in x for x in value):
        return {prefix: sorted(x["entity_id"] + (f" ({x['kind']})" if x.get("kind") else "") for x in value)}
    if isinstance(value, list) and value and all(isinstance(x, dict) and "id" in x for x in value):
        out = {}
        for x in value:
            out.update(_flat(x, f"{prefix}.{x['id']}"))
        return out
    return {prefix: value}


def changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What a save would change, as paths with the old and the new value (rooms and zones added or removed first)."""
    out: list[dict[str, Any]] = []
    b_rooms, a_rooms = set(before.get("rooms") or {}), set(after.get("rooms") or {})
    for r in sorted(a_rooms - b_rooms):
        out.append({"path": f"rooms.{r}", "kind": "room_added", "area_id": r, "before": None, "after": r})
    for r in sorted(b_rooms - a_rooms):
        out.append({"path": f"rooms.{r}", "kind": "room_removed", "area_id": r, "before": r, "after": None})
    b_zones = {z["id"]: z for z in before.get("zones") or []}
    a_zones = {z["id"]: z for z in after.get("zones") or []}
    for z in sorted(set(a_zones) - set(b_zones)):
        out.append({"path": f"zones.{z}", "kind": "zone_added", "zone_id": z, "before": None,
                    "after": a_zones[z]["name"]})
    for z in sorted(set(b_zones) - set(a_zones)):
        out.append({"path": f"zones.{z}", "kind": "zone_removed", "zone_id": z, "before": b_zones[z]["name"],
                    "after": None})
    fb, fa = _flat(before), _flat(after)
    skip = tuple(f"rooms.{r}." for r in (a_rooms ^ b_rooms)) + tuple(f"zones.{z}." for z in set(a_zones) ^ set(b_zones))
    for path in sorted(set(fb) | set(fa)):
        if path.startswith(skip) or path in ("format", "schema"):
            continue
        old, new = fb.get(path), fa.get(path)
        if old != new and not (old in (None, [], {}) and new in (None, [], {})):
            out.append({"path": path, "kind": "changed", "before": old, "after": new})
    return out


# --------------------------------------------------------------- discovery
def clean_capabilities(caps: dict[str, Any]) -> dict[str, Any]:
    keys = ("hvac_modes", "min_temp_c", "max_temp_c", "step", "target", "target_range", "presets", "reports_action",
            "reports_current", "turn_off", "external_input")
    out: dict[str, Any] = {}
    for k in keys:
        value = caps.get(k)
        if isinstance(value, list):
            out[k] = [str(x)[:40] for x in value[:20]]
        elif isinstance(value, (bool, int, float)) or value is None:
            out[k] = value
        elif isinstance(value, str):
            out[k] = value[:80]
    return out


def climate_unit(attrs: dict[str, Any], ha_unit: str) -> str:
    """The unit of a climate entity's temperatures: its ``temperature_unit`` attribute, else the system unit."""
    unit = attrs.get("temperature_unit")
    return unit if unit in ("°C", "°F") else ha_unit


def thermostat_capabilities(state: dict[str, Any] | None, ha_unit: str, *, external_input: str | None = None
                            ) -> dict[str, Any] | None:
    """What a climate entity reports it can do (read from its attributes, never assumed)."""
    if not state:
        return None
    attrs = state.get("attributes") or {}
    ha_unit = climate_unit(attrs, ha_unit)
    features = int(as_float(attrs.get("supported_features")) or 0)
    return {
        "hvac_modes": [str(m) for m in attrs.get("hvac_modes") or []][:20],
        "min_temp_c": _round(to_celsius(attrs.get("min_temp"), ha_unit)),
        "max_temp_c": _round(to_celsius(attrs.get("max_temp"), ha_unit)),
        "step": as_float(attrs.get("target_temp_step")),
        "target": bool(features & FEATURE_TARGET_TEMPERATURE),
        "target_range": bool(features & FEATURE_TARGET_TEMPERATURE_RANGE),
        "presets": [str(p) for p in attrs.get("preset_modes") or []][:20] if features & FEATURE_PRESET_MODE else [],
        "reports_action": "hvac_action" in attrs,
        "reports_current": "current_temperature" in attrs,
        "turn_off": bool(features & FEATURE_TURN_OFF) or "off" in (attrs.get("hvac_modes") or []),
        "external_input": external_input,
    }


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _text(*parts: Any) -> str:
    return " ".join(str(p) for p in parts if p).casefold()


def thermostat_kind(entity_id: str, reg: dict[str, Any] | None, device: dict[str, Any] | None) -> str:
    """A guess of the kind of heating device (the user's choice always wins)."""
    platform = (reg or {}).get("platform")
    if platform == "generic_thermostat":
        return "generic"
    text = _text(entity_id, (reg or {}).get("original_name"), (reg or {}).get("name"), (device or {}).get("name"),
                 (device or {}).get("model"), (device or {}).get("manufacturer"))
    if any(w in text for w in TRV_WORDS):
        return "trv"
    if any(w in text for w in ("heat pump", "heatpump", "air condition", "aircon", "mitsubishi", "daikin", "split")):
        return "heat_pump"
    return "room_thermostat"


def is_temperature_sensor(entity_id: str, state: dict[str, Any] | None, reg: dict[str, Any] | None) -> bool:
    if not entity_id.startswith("sensor."):
        return False
    attrs = (state or {}).get("attributes") or {}
    device_class = attrs.get("device_class") or (reg or {}).get("original_device_class") \
        or (reg or {}).get("device_class")
    unit = attrs.get("unit_of_measurement") or (reg or {}).get("unit_of_measurement")
    if device_class not in (None, "temperature"):
        return False
    return unit in ("°C", "°F", "K") and (device_class == "temperature" or unit in ("°C", "°F"))


def sensor_hints(entity_id: str, state: dict[str, Any] | None, reg: dict[str, Any] | None,
                 device: dict[str, Any] | None, *, thermostat_devices: set[str]) -> dict[str, Any]:
    """How a temperature sensor looks: outdoor, a thermostat's own sensor, or not a room temperature."""
    text = _text(entity_id, (state or {}).get("attributes", {}).get("friendly_name"), (reg or {}).get("original_name"),
                 (device or {}).get("name"), (device or {}).get("name_by_user"))
    device_id = (reg or {}).get("device_id")
    return {
        "outdoor": any(w in text for w in OUTDOOR_WORDS),
        "on_thermostat": bool(device_id and device_id in thermostat_devices),
        "not_room": any(w in text for w in NOT_ROOM_WORDS) or (reg or {}).get("entity_category") == "diagnostic",
    }


def external_input_of(device_entities: list[str], registry: dict[str, dict[str, Any]]) -> str | None:
    """A thermostat device's own input for an external temperature (a ``number.*`` entity), when it has one.

    Shown as information only: Smart Comfort never writes to it in A7.0.
    """
    for entity_id in sorted(device_entities):
        if not entity_id.startswith("number."):
            continue
        reg = registry.get(entity_id) or {}
        text = _text(entity_id, reg.get("original_name"), reg.get("name"), reg.get("translation_key"))
        text = text.replace(" ", "_")
        if any(w in text for w in EXTERNAL_INPUT_WORDS):
            return entity_id
    return None


def opening_kind(entity_id: str, state: dict[str, Any] | None, reg: dict[str, Any] | None) -> str | None:
    if not entity_id.startswith("binary_sensor."):
        return None
    attrs = (state or {}).get("attributes") or {}
    device_class = attrs.get("device_class") or (reg or {}).get("original_device_class") \
        or (reg or {}).get("device_class")
    return OPENING_CLASSES.get(device_class or "")


# ------------------------------------------------------------- measurements
def _ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _age(state: dict[str, Any], now: float) -> float | None:
    seen = max(filter(None, (_ts(state.get("last_reported")), _ts(state.get("last_updated")),
                             _ts(state.get("last_changed")))), default=None)
    return None if seen is None else max(0.0, now - seen)


def reading(entity_id: str | None, state: dict[str, Any] | None, *, now: float, stale_minutes: float, ha_unit: str,
            place: str = "indoor") -> dict[str, Any]:
    """One temperature measurement with its quality.

    quality: ``ok`` · ``stale`` (no report within the freshness limit) · ``unavailable`` · ``unknown`` (no value) ·
    ``missing`` (the entity does not exist) · ``not_numeric`` · ``wrong_unit`` · ``implausible`` · ``none``
    (nothing assigned). The original state and unit are always kept.
    """
    out: dict[str, Any] = {"entity_id": entity_id, "value_c": None, "raw": None, "unit": None, "quality": "none",
                           "age_s": None, "last_changed": None, "name": None}
    if not entity_id:
        return out
    if state is None:
        out["quality"] = "missing"
        return out
    attrs = state.get("attributes") or {}
    raw = state.get("state")
    out.update(raw=raw, name=attrs.get("friendly_name"), last_changed=state.get("last_changed"))
    age = _age(state, now)
    out["age_s"] = None if age is None else round(age)
    if raw == "unavailable":
        out["quality"] = "unavailable"
        return out
    domain = entity_id.split(".", 1)[0]
    if domain == "weather":
        unit = attrs.get("temperature_unit") or ha_unit
        source = attrs.get("temperature")
    elif domain == "climate":
        unit, source = climate_unit(attrs, ha_unit), attrs.get("current_temperature")
    else:
        unit, source = attrs.get("unit_of_measurement"), raw
    out["unit"] = unit
    if raw in (None, "unknown", "") or (domain != "sensor" and source is None):
        out["quality"] = "unknown"
        return out
    if as_float(source) is None:
        out["quality"] = "not_numeric"
        return out
    value = to_celsius(source, unit)
    if value is None:
        out["quality"] = "wrong_unit"
        return out
    out["value_c"] = round(value, 2)
    lo, hi = PLAUSIBLE.get(place, PLAUSIBLE["indoor"])
    if not lo <= value <= hi:
        out["quality"] = "implausible"
    elif age is not None and age > stale_minutes * 60:
        out["quality"] = "stale"
    else:
        out["quality"] = "ok"
    return out


USABLE = ("ok",)


def thermostat_view(entity_id: str, state: dict[str, Any] | None, *, now: float, stale_minutes: float,
                    ha_unit: str) -> dict[str, Any]:
    """What a thermostat reports: its own measurement, its setpoint(s), mode and (reported) action."""
    measured = reading(entity_id, state, now=now, stale_minutes=stale_minutes, ha_unit=ha_unit)
    attrs = (state or {}).get("attributes") or {}
    ha_unit = climate_unit(attrs, ha_unit)
    available = bool(state) and state.get("state") not in ("unavailable", "unknown", None)
    target = to_celsius(attrs.get("temperature"), ha_unit)
    low = to_celsius(attrs.get("target_temp_low"), ha_unit)
    high = to_celsius(attrs.get("target_temp_high"), ha_unit)
    action = attrs.get("hvac_action") if "hvac_action" in attrs else None
    if "hvac_action" not in attrs or not available:
        heating = {"state": "unknown", "basis": "not_reported" if available else "unavailable"}
    elif action in HEATING_ACTIONS:
        heating = {"state": "heating", "basis": "reported"}
    elif action in HVAC_ACTIONS:
        heating = {"state": "not_heating", "basis": "reported"}
    else:
        heating = {"state": "unknown", "basis": "not_reported"}
    return {
        "entity_id": entity_id,
        "name": attrs.get("friendly_name"),
        "available": available,
        "state": (state or {}).get("state") if state else "missing",
        "hvac_mode": (state or {}).get("state") if available else None,
        "hvac_action": action,
        "heating": heating,
        "measured": measured,
        "target_c": _round(target),
        "target_low_c": _round(low),
        "target_high_c": _round(high),
        "preset": attrs.get("preset_mode"),
        "last_changed": (state or {}).get("last_changed"),
    }


def opening_view(entity_id: str, state: dict[str, Any] | None, *, kind: str | None) -> dict[str, Any]:
    raw = (state or {}).get("state")
    status = "missing" if state is None else "open" if raw == "on" else "closed" if raw == "off" else \
        "unavailable" if raw == "unavailable" else "unknown"
    return {"entity_id": entity_id, "name": ((state or {}).get("attributes") or {}).get("friendly_name"),
            "kind": kind, "status": status, "since": (state or {}).get("last_changed")}


def room_temperature(primary: dict[str, Any], secondary: list[dict[str, Any]]) -> dict[str, Any]:
    """The room's own temperature: the primary sensor, else the mean of the usable secondary sensors.

    Never a thermostat's own measurement.
    """
    if primary["quality"] in USABLE:
        return {"value_c": primary["value_c"], "source": "primary", "entity_ids": [primary["entity_id"]]}
    usable = [s for s in secondary if s["quality"] in USABLE]
    if usable:
        value = sum(s["value_c"] for s in usable) / len(usable)
        return {"value_c": round(value, 2), "source": "secondary", "entity_ids": [s["entity_id"] for s in usable]}
    return {"value_c": None, "source": "none", "entity_ids": []}


def demand_hint(room_c: float | None, thermostats: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A setpoint above the measured room temperature: heat *may* be requested. Explicitly not "heating"."""
    if room_c is None:
        return None
    above = [t["entity_id"] for t in thermostats if t["available"] and t["hvac_mode"] not in ("off", None)
             and t["target_c"] is not None and t["target_c"] > room_c + 0.2]
    if not above:
        return None
    return {"basis": "inferred", "entity_ids": above,
            "text": "A setpoint is above the measured room temperature: heat may be requested. This is not a "
                    "report that the device is heating."}


def completeness(room: dict[str, Any], view: dict[str, Any], *, outdoor: bool) -> dict[str, Any]:
    """How ready a room's configuration is for observation and (later) learning."""
    items = [
        {"key": "primary_sensor", "ok": room["primary_sensor"] is not None, "required": True,
         "text": "A primary room temperature sensor is assigned"},
        {"key": "primary_fresh", "ok": view["primary"]["quality"] in USABLE, "required": True,
         "text": "The primary sensor reports a current, plausible value"},
        {"key": "thermostat", "ok": bool(room["thermostats"]), "required": False,
         "text": "A heating device is assigned (without one the room is observed only)"},
        {"key": "action", "ok": any(t["heating"]["basis"] == "reported" for t in view["thermostats"]),
         "required": False, "text": "A heating device reports whether it is heating"},
        {"key": "openings", "ok": bool(room["openings"]), "required": False,
         "text": "Windows or doors are assigned (recommended)"},
        {"key": "outdoor", "ok": outdoor, "required": False, "text": "An outdoor temperature source is configured"},
    ]
    required = [i for i in items if i["required"]]
    score = round(100 * sum(1 for i in items if i["ok"]) / len(items))
    return {"score": score, "ready": all(i["ok"] for i in required), "items": items}
