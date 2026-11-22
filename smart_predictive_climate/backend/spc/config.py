"""The App's configuration: rooms (Home Assistant areas), their inputs, comfort schedules, windows and thermal zones.

A room is identified by its Home Assistant **area id**, which does not change when the area is renamed; the
published entities and the learned model hang on that id.

    {"settings": {"outdoor": entity | None, "weather": weather entity | None, "history_days": 90,
                  "min_history_days": 14, "horizon_hours": 24, "recommend_from": "moderate"},
     "rooms": {area_id: {
         "name", "enabled",
         "temperature": entity | None,        the deliberately chosen room sensor (preferred)
         "secondary": [entity],               further room sensors, used when the primary has no value
         "thermostats": [climate entity],     observed only: their setpoint, activity and own measurement
         "outdoor": entity | None,            overrides settings.outdoor for this room
         "comfort": {"day_c", "night_c" | None, "weekday": {"start", "end"}, "weekend": {"start", "end"},
                     "away_entity" | None, "away_c" | None, "follow_occupancy": bool},
         "openings": [{"entity_id", "kind": "window" | "door"}],
         "occupancy": [entity], "occupancy_learning": bool,
         "humidity": [entity], "sunlight": [entity], "wind": entity | None, "running": [entity],
         "energy": [entity],
         "windows": [{"id", "name", "orientation" (0 = South, clockwise), "area_m2", "shading" 0..1,
                      "obstructions": [{"from", "to", "elevation"}], "covers": [cover entity]}]}},
     "zones": {zone_id: {"name", "rooms": [area_id], "connections": [{"a", "b", "strength", "door"}]}}}

Only ``temperature`` (or a thermostat's own measurement as a fallback) and a comfort temperature are needed
for a room; every other input is optional, and none of them is required to complete the set-up.
``validate_config`` returns the canonical form or raises ``ConfigError`` with every problem.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from .store import ConfigError

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
ROOM_ID = re.compile(r"^[a-z0-9_]{1,100}$")
ZONE_ID = re.compile(r"^z_[0-9a-f]{8}$")
WINDOW_ID = re.compile(r"^w_[0-9a-f]{6}$")
TIME_OF_DAY = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
STRENGTHS = {"weak": 0.5, "normal": 1.0, "strong": 2.0, "open": 4.0}
CONFIDENCE_LEVELS = ("low", "moderate", "high")
MAX_ROOMS = 60
MAX_LIST = 12
MAX_WINDOWS = 8
DEFAULT_SETTINGS: dict[str, Any] = {"outdoor": None, "weather": None, "history_days": 90, "min_history_days": 14,
                                    "horizon_hours": 24, "recommend_from": "moderate"}
DEFAULT_COMFORT: dict[str, Any] = {"day_c": 21.0, "night_c": None, "weekday": {"start": "06:30", "end": "22:30"},
                                   "weekend": {"start": "08:00", "end": "23:00"}, "away_entity": None, "away_c": None,
                                   "follow_occupancy": False}
#: Which Home Assistant domains each input accepts (the interface offers exactly these).
DOMAINS = {
    "temperature": ("sensor",), "secondary": ("sensor",), "thermostats": ("climate",),
    "outdoor": ("sensor", "weather"), "weather": ("weather",), "openings": ("binary_sensor",),
    "occupancy": ("binary_sensor",), "humidity": ("sensor",), "sunlight": ("sensor",), "wind": ("sensor",),
    "running": ("binary_sensor", "switch", "input_boolean", "climate"), "energy": ("sensor",),
    "covers": ("cover",), "door": ("binary_sensor",),
    "away_entity": ("binary_sensor", "input_boolean", "person", "device_tracker", "schedule", "calendar", "switch"),
}


class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        if len(self.items) < 100:
            self.items.append({"path": path, "message": message})


def new_zone_id() -> str:
    return f"z_{secrets.token_hex(4)}"


def new_window_id() -> str:
    return f"w_{secrets.token_hex(3)}"


def empty_config() -> dict[str, Any]:
    return {"settings": dict(DEFAULT_SETTINGS), "rooms": {}, "zones": {}}


def _entity(raw: Any, path: str, p: _Problems, kind: str, *, required: bool = False) -> str | None:
    if raw in (None, ""):
        if required:
            p.add(path, "Choose an entity")
        return None
    if not isinstance(raw, str) or not ENTITY_ID.match(raw) or len(raw) > 255:
        p.add(path, "Not an entity id")
        return None
    if raw.split(".", 1)[0] not in DOMAINS[kind]:
        p.add(path, f"{raw} cannot be used here (expected: {', '.join(DOMAINS[kind])})")
        return None
    return raw


def _entities(raw: Any, path: str, p: _Problems, kind: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_LIST:
        p.add(path, f"Must be a list of at most {MAX_LIST} entities")
        return []
    out: list[str] = []
    for i, item in enumerate(raw):
        entity_id = _entity(item, f"{path}[{i}]", p, kind, required=True)
        if entity_id and entity_id not in out:
            out.append(entity_id)
    return out


def _number(raw: Any, path: str, p: _Problems, low: float, high: float, *, default: float | None = None,
            optional: bool = False) -> float | None:
    if raw in (None, ""):
        if optional:
            return None
        if default is not None:
            return default
        p.add(path, "Enter a number")
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw or not low <= raw <= high:
        p.add(path, f"Must be a number between {low:g} and {high:g}")
        return default
    return round(float(raw), 2)


def _period(raw: Any, path: str, p: _Problems, default: dict[str, str]) -> dict[str, str]:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in ("start", "end"):
        value = raw.get(key, default[key])
        if not isinstance(value, str) or not TIME_OF_DAY.match(value):
            p.add(f"{path}.{key}", "Must be a time of day (HH:MM)")
            value = default[key]
        out[key] = value
    if out["start"] == out["end"]:
        p.add(path, "The comfort period must have a length (start and end differ)")
    return out


def _comfort(raw: Any, path: str, p: _Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    day = _number(raw.get("day_c"), f"{path}.day_c", p, 5, 30, default=DEFAULT_COMFORT["day_c"])
    night = _number(raw.get("night_c"), f"{path}.night_c", p, 5, 30, optional=True)
    away = _number(raw.get("away_c"), f"{path}.away_c", p, 5, 30, optional=True)
    return {"day_c": day, "night_c": night,
            "weekday": _period(raw.get("weekday"), f"{path}.weekday", p, DEFAULT_COMFORT["weekday"]),
            "weekend": _period(raw.get("weekend"), f"{path}.weekend", p, DEFAULT_COMFORT["weekend"]),
            "away_entity": _entity(raw.get("away_entity"), f"{path}.away_entity", p, "away_entity"),
            "away_c": away, "follow_occupancy": raw.get("follow_occupancy") is True}


def _window(raw: Any, path: str, p: _Problems, seen: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        p.add(path, "Must be a window")
        return None
    window_id = raw.get("id")
    if not isinstance(window_id, str) or not WINDOW_ID.match(window_id) or window_id in seen:
        p.add(f"{path}.id", "Invalid window id")
        return None
    seen.add(window_id)
    name = raw.get("name")
    name = name.strip()[:60] if isinstance(name, str) and name.strip() else "Window"
    orientation = _number(raw.get("orientation"), f"{path}.orientation", p, 0, 360)
    obstructions = []
    raw_obstructions = raw.get("obstructions") if isinstance(raw.get("obstructions"), list) else []
    if len(raw_obstructions) > 4:
        p.add(f"{path}.obstructions", "At most 4 obstructions per window")
    for i, item in enumerate(raw_obstructions[:4]):
        item = item if isinstance(item, dict) else {}
        start = _number(item.get("from"), f"{path}.obstructions[{i}].from", p, 0, 360)
        end = _number(item.get("to"), f"{path}.obstructions[{i}].to", p, 0, 360)
        elevation = _number(item.get("elevation"), f"{path}.obstructions[{i}].elevation", p, 1, 90)
        if None not in (start, end, elevation):
            obstructions.append({"from": start, "to": end, "elevation": elevation})
    return {"id": window_id, "name": name, "orientation": None if orientation is None else orientation % 360.0,
            "area_m2": _number(raw.get("area_m2"), f"{path}.area_m2", p, 0.1, 50, default=1.5),
            "shading": _number(raw.get("shading"), f"{path}.shading", p, 0, 1, default=0.0),
            "obstructions": obstructions, "covers": _entities(raw.get("covers"), f"{path}.covers", p, "covers")}


def _room(area_id: str, raw: Any, path: str, p: _Problems) -> dict[str, Any]:
    if not isinstance(raw, dict):
        p.add(path, "Must be a room")
        raw = {}
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        p.add(f"{path}.name", "The room needs a name")
        name = area_id
    openings = []
    raw_openings = raw.get("openings") if isinstance(raw.get("openings"), list) else []
    if len(raw_openings) > MAX_LIST:
        p.add(f"{path}.openings", f"At most {MAX_LIST} window and door sensors")
    for i, item in enumerate(raw_openings[:MAX_LIST]):
        item = item if isinstance(item, dict) else {"entity_id": item}
        entity_id = _entity(item.get("entity_id"), f"{path}.openings[{i}]", p, "openings", required=True)
        if entity_id and entity_id not in [o["entity_id"] for o in openings]:
            openings.append({"entity_id": entity_id, "kind": "door" if item.get("kind") == "door" else "window"})
    windows, seen = [], set()
    raw_windows = raw.get("windows") if isinstance(raw.get("windows"), list) else []
    if len(raw_windows) > MAX_WINDOWS:
        p.add(f"{path}.windows", f"At most {MAX_WINDOWS} windows per room")
    for i, item in enumerate(raw_windows[:MAX_WINDOWS]):
        window = _window(item, f"{path}.windows[{i}]", p, seen)
        if window:
            windows.append(window)
    temperature = _entity(raw.get("temperature"), f"{path}.temperature", p, "temperature")
    secondary = [e for e in _entities(raw.get("secondary"), f"{path}.secondary", p, "secondary") if e != temperature]
    thermostats = _entities(raw.get("thermostats"), f"{path}.thermostats", p, "thermostats")
    if temperature is None and not secondary and not thermostats:
        p.add(f"{path}.temperature", "Choose the room's temperature sensor (or at least a thermostat that measures "
                                     "the room)")
    return {
        "name": name.strip()[:80], "enabled": raw.get("enabled") is not False,
        "temperature": temperature, "secondary": secondary, "thermostats": thermostats,
        "outdoor": _entity(raw.get("outdoor"), f"{path}.outdoor", p, "outdoor"),
        "comfort": _comfort(raw.get("comfort"), f"{path}.comfort", p),
        "openings": openings,
        "occupancy": _entities(raw.get("occupancy"), f"{path}.occupancy", p, "occupancy"),
        "occupancy_learning": raw.get("occupancy_learning") is True,
        "humidity": _entities(raw.get("humidity"), f"{path}.humidity", p, "humidity"),
        "sunlight": _entities(raw.get("sunlight"), f"{path}.sunlight", p, "sunlight"),
        "wind": _entity(raw.get("wind"), f"{path}.wind", p, "wind"),
        "running": _entities(raw.get("running"), f"{path}.running", p, "running"),
        "energy": _entities(raw.get("energy"), f"{path}.energy", p, "energy"),
        "windows": windows,
    }


def _zones(raw: Any, rooms: dict[str, Any], p: _Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    member_of: dict[str, str] = {}
    for zone_id in sorted(raw):
        path = f"zones.{zone_id}"
        zone = raw[zone_id] if isinstance(raw[zone_id], dict) else {}
        if not isinstance(zone_id, str) or not ZONE_ID.match(zone_id):
            p.add(path, "Invalid zone id")
            continue
        name = zone.get("name")
        name = name.strip()[:80] if isinstance(name, str) and name.strip() else "Thermal zone"
        members: list[str] = []
        for area_id in zone.get("rooms") if isinstance(zone.get("rooms"), list) else []:
            if area_id not in rooms:
                p.add(f"{path}.rooms", f"{area_id} is not a configured room")
            elif area_id in member_of:
                # One room, one thermal model: a room in two zones would be modelled twice with competing
                # neighbours. Connect the zones' rooms in one zone instead.
                p.add(f"{path}.rooms", f"{rooms[area_id]['name']} is already in another thermal zone; a room "
                                       "belongs to one zone")
            elif area_id not in members:
                members.append(area_id)
                member_of[area_id] = zone_id
        if len(members) < 2:
            p.add(f"{path}.rooms", "A thermal zone connects at least two rooms")
        connections, seen = [], set()
        for i, item in enumerate(zone.get("connections") if isinstance(zone.get("connections"), list) else []):
            item = item if isinstance(item, dict) else {}
            a, b = item.get("a"), item.get("b")
            if a not in members or b not in members or a == b:
                p.add(f"{path}.connections[{i}]", "A connection joins two different rooms of the zone")
                continue
            key = tuple(sorted((a, b)))
            if key in seen:
                p.add(f"{path}.connections[{i}]", "These two rooms are connected already")
                continue
            seen.add(key)
            strength = item.get("strength", "normal")
            if strength not in STRENGTHS:
                p.add(f"{path}.connections[{i}].strength", "Choose weak, normal, strong or open")
                strength = "normal"
            connections.append({"a": key[0], "b": key[1], "strength": strength,
                                "door": _entity(item.get("door"), f"{path}.connections[{i}].door", p, "door")})
        if not connections and len(members) >= 2:  # nothing said: every pair, normally connected
            connections = [{"a": a, "b": b, "strength": "normal", "door": None}
                           for i, a in enumerate(sorted(members)) for b in sorted(members)[i + 1:]]
        out[zone_id] = {"name": name, "rooms": members, "connections": sorted(connections, key=lambda c: (c["a"], c["b"]))}
    return out


def validate_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError("invalid", "The configuration must be an object")
    p = _Problems()
    settings_raw = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    recommend_from = settings_raw.get("recommend_from", DEFAULT_SETTINGS["recommend_from"])
    if recommend_from not in CONFIDENCE_LEVELS:
        p.add("settings.recommend_from", "Choose low, moderate or high")
        recommend_from = DEFAULT_SETTINGS["recommend_from"]
    settings = {
        "outdoor": _entity(settings_raw.get("outdoor"), "settings.outdoor", p, "outdoor"),
        "weather": _entity(settings_raw.get("weather"), "settings.weather", p, "weather"),
        "history_days": int(_number(settings_raw.get("history_days"), "settings.history_days", p, 7, 120, default=90) or 90),
        "min_history_days": int(_number(settings_raw.get("min_history_days"), "settings.min_history_days", p, 3, 60,
                                        default=14) or 14),
        "horizon_hours": int(_number(settings_raw.get("horizon_hours"), "settings.horizon_hours", p, 6, 72, default=24) or 24),
        "recommend_from": recommend_from,
    }
    rooms_raw = raw.get("rooms") if isinstance(raw.get("rooms"), dict) else {}
    if len(rooms_raw) > MAX_ROOMS:
        p.add("rooms", f"At most {MAX_ROOMS} rooms")
    rooms: dict[str, Any] = {}
    for area_id in sorted(rooms_raw)[:MAX_ROOMS]:
        if not isinstance(area_id, str) or not ROOM_ID.match(area_id):
            p.add(f"rooms.{area_id}", "Invalid room id")
            continue
        rooms[area_id] = _room(area_id, rooms_raw[area_id], f"rooms.{area_id}", p)
    config = {"settings": settings, "rooms": rooms, "zones": _zones(raw.get("zones"), rooms, p)}
    if p.items:
        raise ConfigError("invalid", "The configuration is not valid", problems=p.items)
    return config


# ------------------------------------------------------------------ helpers
def outdoor_of(config: dict[str, Any], area_id: str) -> str | None:
    """The room's outdoor temperature source: its own, else the general one, else the weather entity."""
    room = config["rooms"][area_id]
    return room["outdoor"] or config["settings"]["outdoor"] or config["settings"]["weather"]


def connections_of(config: dict[str, Any], area_id: str) -> list[dict[str, Any]]:
    """The thermal connections of a room: ``[{"other", "strength", "factor", "door"}]`` (enabled rooms only)."""
    out = []
    for zone in config["zones"].values():
        for c in zone["connections"]:
            if area_id in (c["a"], c["b"]):
                other = c["b"] if c["a"] == area_id else c["a"]
                if other in config["rooms"] and config["rooms"][other]["enabled"]:
                    out.append({"other": other, "strength": c["strength"], "factor": STRENGTHS[c["strength"]],
                                "door": c["door"]})
    return out


def zone_of(config: dict[str, Any], area_id: str) -> tuple[str, dict[str, Any]] | None:
    for zone_id, zone in config["zones"].items():
        if area_id in zone["rooms"]:
            return zone_id, zone
    return None


def tracked_entities(config: dict[str, Any]) -> dict[str, str]:
    """Every entity whose history the App keeps, with the kind of record it gives."""
    out: dict[str, str] = {}

    def add(entity_id: str | None, kind: str) -> None:
        if entity_id:
            out.setdefault(entity_id, "weather" if entity_id.startswith("weather.") else kind)

    add(config["settings"]["weather"], "weather")
    for area_id, room in config["rooms"].items():
        if not room["enabled"]:
            continue
        for entity_id in [room["temperature"], *room["secondary"]]:
            add(entity_id, "room_temp")
        for entity_id in room["thermostats"]:
            add(entity_id, "thermostat")
        add(outdoor_of(config, area_id), "outdoor")
        for opening in room["openings"]:
            add(opening["entity_id"], "opening")
        for entity_id in room["occupancy"]:
            add(entity_id, "occupancy")
        for entity_id in room["humidity"]:
            add(entity_id, "humidity")
        for entity_id in room["sunlight"]:
            add(entity_id, "illuminance")
        add(room["wind"], "wind")
        for entity_id in room["running"]:
            add(entity_id, "thermostat" if entity_id.startswith("climate.") else "running")
        for entity_id in room["energy"]:
            add(entity_id, "energy")
        add(room["comfort"]["away_entity"], "presence")
        for window in room["windows"]:
            for cover in window["covers"]:
                add(cover, "cover")
    for zone in config["zones"].values():
        for c in zone["connections"]:
            add(c["door"], "opening")
    return out
