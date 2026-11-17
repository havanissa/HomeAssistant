"""Rooms (Phase A2): discovery, room status and the room configuration editor.

The authoritative room configuration is the existing integration's config
entry options. The App reads it with ``smart_occupancy/rooms/get`` and changes
it only through the integration's validated, administrator-only commands
(``smart_occupancy/rooms/review`` / ``set`` / ``restore``); it keeps no room
configuration of its own. The only thing stored here is a backup of the rooms
section before each change (``/data/room-backups``), so a previous
configuration can be exported and restored after review.

Discovery reads Home Assistant's area, device and entity registries and the
current states. Categories are suggestions from domain and device class
(never from entity names alone); the user's own classification, the sensor
kind stored in the integration, always wins. Nothing here sends a service
call or changes a registry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
from typing import Any

from .const import APP_VERSION, INTEGRATION_DOMAIN
from .ha import HomeAssistantClient, HomeAssistantError, utcnow_iso

_LOGGER = logging.getLogger(__name__)

BACKUP_FORMAT = "smart_occupancy.rooms_backup"
BACKUP_KEEP = 30
BACKUP_ID = re.compile(r"^rooms-\d{8}T\d{6}Z-[0-9a-f]{1,16}(?:-\d+)?$")
AREA_ID = re.compile(r"^[\w\-]{1,255}$")
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
REVISION = re.compile(r"^[0-9a-f]{16}$")
IMPACT_CODE = re.compile(r"^[a-z_]{1,64}$")
ROOM_KEYS = ("enabled", "sensors", "lights", "reference_occupancy_entity", "adjacent_rooms")
SENSOR_KEYS = ("entity_id", "kind", "role", "inverted", "enabled")

# category -> (label, icon, group). Groups are the sections of the Devices tab.
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "pir": ("Motion sensor (PIR)", "motion-sensor", "presence"),
    "presence": ("Radar / presence sensor", "radar", "presence"),
    "door": ("Door contact", "door", "presence"),
    "window": ("Window contact", "window", "presence"),
    "pc_activity": ("PC activity", "monitor", "presence"),
    "bed": ("Bed sensor", "bed", "presence"),
    "presence_input": ("Other presence input", "account", "presence"),
    "temperature": ("Temperature sensor", "thermometer", "climate"),
    "illuminance": ("Illuminance sensor", "brightness", "climate"),
    "light": ("Light", "lightbulb", "lighting"),
    "light_group": ("Light group", "lightbulb-group", "lighting"),
    "relay": ("Relay (lighting output)", "electric-switch", "lighting"),
    "switch_output": ("Switch / relay output", "electric-switch", "lighting"),
    "wall_switch": ("Wall switch / remote", "light-switch", "switches"),
    "cover": ("Curtain / cover", "curtains", "curtains"),
    "other": ("Other", "puzzle", "other"),
}
# Sensor kinds of the integration (SensorKind) suggested for a category.
SUGGESTED_KIND = {
    "pir": "pir", "presence": "radar", "door": "door", "window": "other", "pc_activity": "pc_activity",
    "bed": "bed", "presence_input": "pc_activity", "illuminance": "other", "temperature": "other",
}
KIND_CATEGORY = {"pir": "pir", "radar": "presence", "door": "door", "pc_activity": "pc_activity", "bed": "bed"}
_RADAR_WORDS = re.compile(r"radar|mmwave|mm-wave|fp1|fp2|ld24\d\d|presence|human", re.I)
_PIR_WORDS = re.compile(r"\bpir\b|motion|rtcgq|sml00|occupancy sensor", re.I)
_SWITCH_WORDS = re.compile(r"switch|remote|button|dimmer|wxkg|opple|tradfri on/off|rodret|styrbar|symfonisk", re.I)
_ACTION_SENSOR = re.compile(r"_(action|click)$")
_UNAVAILABLE = {"unavailable"}
#: Attributes forwarded with live states (small, display-only values; Phase A2.1 floor plan).
LIVE_ATTRS = ("brightness", "current_position", "current_tilt_position", "unit_of_measurement", "device_class",
              "occupied", "lighting_control", "observe_only", "lighting_paused", "operating_mode", "reason")


def live_attributes(state: dict[str, Any] | None) -> dict[str, Any]:
    """A few display-only attributes of a state (no lists, no long texts); manual overrides as a count."""
    attrs = (state or {}).get("attributes") or {}
    out: dict[str, Any] = {}
    for key in LIVE_ATTRS:
        value = attrs.get(key)
        if isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= 300):
            out[key] = value
    if isinstance(attrs.get("manual_overrides"), list):
        out["manual_overrides"] = len(attrs["manual_overrides"])
    return out


class RoomsError(Exception):
    """A rooms request was refused or failed; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass
class Index:
    """Registries, indexed (rebuilt when they change; states are read live)."""

    areas: dict[str, dict[str, Any]] = field(default_factory=dict)
    devices: dict[str, dict[str, Any]] = field(default_factory=dict)
    registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    device_entities: dict[str, list[str]] = field(default_factory=dict)


def _now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------- discovery
def classify(entity_id: str, reg: dict[str, Any] | None, state: dict[str, Any] | None,
             device: dict[str, Any] | None, *, relays: set[str], switch_entities: set[str],
             assigned_kind: str | None = None) -> tuple[str, str, str]:
    """(category, confidence, reason). ``assigned_kind`` (the user's classification) wins."""
    if assigned_kind and assigned_kind in KIND_CATEGORY:
        return KIND_CATEGORY[assigned_kind], "user", "Classified when it was assigned"
    domain = entity_id.split(".", 1)[0]
    attrs = (state or {}).get("attributes") or {}
    dclass = attrs.get("device_class") or (reg or {}).get("device_class") or (reg or {}).get("original_device_class")
    words = " ".join(str(v) for v in ((device or {}).get("model"), (device or {}).get("name"),
                                      (device or {}).get("model_id")) if v)
    if domain == "binary_sensor":
        if dclass == "motion":
            return "pir", "high", "Device class: motion"
        if dclass in ("occupancy", "presence"):
            if _PIR_WORDS.search(words) and not _RADAR_WORDS.search(words):
                return "pir", "medium", f"Device class {dclass}; the device model looks like a PIR"
            if _RADAR_WORDS.search(words):
                return "presence", "high", f"Device class {dclass}; the device model is a presence radar"
            return "presence", "medium", f"Device class {dclass} (many Zigbee PIR sensors report this too)"
        if dclass in ("door", "garage_door"):
            return "door", "high", f"Device class: {dclass}"
        if dclass in ("window", "opening"):
            return "window", "high", f"Device class: {dclass}"
        return "other", "low", f"Binary sensor ({dclass or 'no device class'})"
    if domain == "sensor":
        if dclass == "illuminance":
            return "illuminance", "high", "Device class: illuminance"
        if dclass == "temperature":
            return "temperature", "high", "Device class: temperature"
        if _ACTION_SENSOR.search(entity_id) and _SWITCH_WORDS.search(words):
            return "wall_switch", "medium", "Action sensor of a switch or remote"
        return "other", "low", f"Sensor ({dclass or 'no device class'})"
    if domain == "light":
        if isinstance(attrs.get("entity_id"), list) or (reg or {}).get("platform") == "group":
            return "light_group", "high", "Light group (has member lights)"
        return "light", "high", "Light entity"
    if domain == "switch":
        if entity_id in relays:
            return "relay", "high", "Configured as a lighting relay in the integration"
        return "switch_output", "low", f"Switch entity ({dclass or 'switch'}); may control a light"
    if domain == "event":
        if entity_id in switch_entities or _SWITCH_WORDS.search(words) or dclass == "button":
            return "wall_switch", "high" if entity_id in switch_entities else "medium", "Button events"
        return "other", "low", "Event entity"
    if domain == "cover":
        return "cover", "high", f"Cover ({dclass or 'no device class'})"
    if domain in ("device_tracker", "person", "media_player", "input_boolean"):
        return "presence_input", "low", f"{domain.replace('_', ' ').capitalize()} (can serve as a presence input)"
    return "other", "low", f"{domain} entity"


def _availability(entity_id: str, reg: dict[str, Any] | None, state: dict[str, Any] | None,
                  index: Index, states: dict[str, dict[str, Any]]) -> str:
    """ok | unknown | unavailable | device_offline | not_provided | disabled | missing."""
    if reg is not None and reg.get("disabled_by"):
        return "disabled"
    if state is None:
        return "missing" if reg is None else "not_provided"
    value = state.get("state")
    if value in _UNAVAILABLE:
        if (state.get("attributes") or {}).get("restored"):
            return "not_provided"
        device_id = (reg or {}).get("device_id")
        siblings = [e for e in index.device_entities.get(device_id or "", [])
                    if not (index.registry.get(e) or {}).get("disabled_by")]
        if device_id and siblings and all((states.get(e) or {}).get("state") in _UNAVAILABLE for e in siblings):
            return "device_offline"
        return "unavailable"
    if value == "unknown":
        return "unknown"
    return "ok"


def _capabilities(entity_id: str, state: dict[str, Any] | None) -> list[str]:
    attrs = (state or {}).get("attributes") or {}
    domain = entity_id.split(".", 1)[0]
    caps: list[str] = []
    if domain == "light":
        modes = attrs.get("supported_color_modes") or []
        if any(m != "onoff" for m in modes):
            caps.append("brightness")
        if "color_temp" in modes:
            caps.append("colour temperature")
        if any(m in ("hs", "rgb", "rgbw", "rgbww", "xy") for m in modes):
            caps.append("colour")
        if modes == ["onoff"]:
            caps.append("on/off only")
        if attrs.get("effect_list"):
            caps.append(f"{len(attrs['effect_list'])} effects")
        if isinstance(attrs.get("entity_id"), list):
            caps.append(f"group of {len(attrs['entity_id'])}")
    elif domain == "cover":
        features = int(attrs.get("supported_features") or 0)
        for bit, name in ((1, "open"), (2, "close"), (4, "position"), (8, "stop"), (128, "tilt position")):
            if features & bit:
                caps.append(name)
    elif domain in ("sensor", "binary_sensor"):
        if attrs.get("device_class"):
            caps.append(str(attrs["device_class"]))
        if attrs.get("unit_of_measurement"):
            caps.append(str(attrs["unit_of_measurement"]))
    elif domain == "event":
        types = attrs.get("event_types") or []
        if types:
            caps.append(f"events: {', '.join(map(str, types[:6]))}{'…' if len(types) > 6 else ''}")
    elif domain == "switch":
        caps.append("on/off")
    return caps


def _device_view(device: dict[str, Any], index: Index, states: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entity_ids = [e for e in index.device_entities.get(device["id"], [])
                  if not (index.registry.get(e) or {}).get("disabled_by")]
    values = [(states.get(e) or {}).get("state") for e in entity_ids]
    if not entity_ids:
        status = "no_entities"
    elif all(v in _UNAVAILABLE for v in values):
        status = "offline"
    elif any(v in _UNAVAILABLE or v is None for v in values):
        status = "partial"
    else:
        status = "online"
    connections = [f"{c[0]}: {c[1]}" for c in device.get("connections") or [] if isinstance(c, list) and len(c) == 2]
    identifiers = [f"{i[0]}: {i[1]}" for i in device.get("identifiers") or [] if isinstance(i, list) and len(i) == 2]
    area_id = device.get("area_id")
    return {
        "id": device["id"],
        "name": device.get("name_by_user") or device.get("name") or device["id"],
        "manufacturer": device.get("manufacturer"),
        "model": device.get("model"),
        "model_id": device.get("model_id"),
        "sw_version": device.get("sw_version"),
        "hw_version": device.get("hw_version"),
        "area_id": area_id,
        "area": (index.areas.get(area_id) or {}).get("name") if area_id else None,
        "disabled": bool(device.get("disabled_by")),
        "identity": connections + identifiers,
        "entity_ids": entity_ids,
        "status": status,
    }


# ------------------------------------------------------------------ service
class RoomsService:
    """Room configuration through the existing integration's API, plus discovery from the registries."""

    def __init__(self, ha: HomeAssistantClient, data_dir: Path | None) -> None:
        self.ha = ha
        self.backup_dir = data_dir / "room-backups" if data_dir else None
        self.model: dict[str, Any] | None = None
        self.api_state = "unknown"  # unknown | ok | unsupported | unavailable | error
        self.api_error: str | None = None
        self.loaded_at: str | None = None
        self._index: Index | None = None
        self._lock = asyncio.Lock()
        self._admins: tuple[float, dict[str, bool]] | None = None

    # ---------------------------------------------------------- model / index
    def invalidate_index(self) -> None:
        self._index = None

    def index(self) -> Index:
        if self._index is None:
            ha = self.ha
            idx = Index(
                areas={a["area_id"]: a for a in ha.areas if a.get("area_id")},
                devices={d["id"]: d for d in ha.devices if d.get("id")},
                registry={e["entity_id"]: e for e in ha.entity_registry if e.get("entity_id")},
            )
            for entity_id, reg in idx.registry.items():
                if reg.get("device_id"):
                    idx.device_entities.setdefault(reg["device_id"], []).append(entity_id)
            self._index = idx
        return self._index

    async def refresh(self) -> bool:
        """Re-read the integration's room configuration. Returns True when it changed."""
        async with self._lock:
            before = (self.model or {}).get("revision"), (self.model or {}).get("loaded"), self.api_state
            try:
                model = await self.ha.command("smart_occupancy/rooms/get")
            except HomeAssistantError as err:
                if err.code in ("unknown_command", "not_found"):
                    self.api_state, self.api_error = "unsupported", (
                        "The installed Smart Occupancy integration has no room configuration API "
                        "(it needs version 0.6.9 or newer). Rooms are shown read-only.")
                else:
                    self.api_state, self.api_error = "error", f"{err.code}: {err}"
                self.model = None
            except (ConnectionError, TimeoutError) as err:
                self.api_state, self.api_error = "unavailable", f"Home Assistant is not connected ({type(err).__name__})"
            else:
                self.model = model if isinstance(model, dict) else None
                self.api_state, self.api_error = ("ok", None) if self.model else ("error", "Unexpected answer")
                self.loaded_at = utcnow_iso()
            after = (self.model or {}).get("revision"), (self.model or {}).get("loaded"), self.api_state
            return before != after

    def status(self) -> dict[str, Any]:
        model = self.model or {}
        return {
            "api": self.api_state,
            "error": self.api_error,
            "loaded": bool(model.get("loaded")),
            "writable": bool(model.get("writable")) and self.api_state == "ok",
            "integration_version": model.get("integration_version"),
            "integration_state": model.get("state"),
            "revision": model.get("revision"),
            "loaded_at": self.loaded_at,
            "backups": self.backup_dir is not None,
        }

    # ---------------------------------------------------------- admin check
    async def is_admin(self, user_id: str | None) -> bool:
        """Is this Home Assistant user an active administrator (Ingress passes the user id)?"""
        if not user_id:
            return False
        now = asyncio.get_running_loop().time()
        if self._admins is None or now - self._admins[0] > 60 or user_id not in self._admins[1]:
            users = await self.ha.command("config/auth/list") or []
            admins = {
                u.get("id"): bool(u.get("is_active", True)) and not u.get("system_generated")
                and (bool(u.get("is_owner")) or "system-admin" in (u.get("group_ids") or []))
                for u in users if isinstance(u, dict)
            }
            self._admins = (now, admins)
        return bool(self._admins[1].get(user_id))

    # ------------------------------------------------------------- reading
    def _rooms(self) -> dict[str, dict[str, Any]]:
        return (self.model or {}).get("rooms") or {}

    def _relays_and_switches(self) -> tuple[set[str], set[str], dict[str, str]]:
        relays: set[str] = set()
        switch_entities: set[str] = set()
        switch_devices: dict[str, str] = {}
        for area_id, room in self._rooms().items():
            lighting = room.get("lighting") or {}
            relays.update(lighting.get("relays") or [])
            for s in lighting.get("switches") or []:
                if s.get("event_entity"):
                    switch_entities.add(s["event_entity"])
                if s.get("device_id"):
                    switch_devices[s["device_id"]] = area_id
        return relays, switch_entities, switch_devices

    def _assignments(self) -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
        """entity -> rooms using it as a sensor / light, entity -> assigned kind (first room)."""
        sensors: dict[str, list[str]] = {}
        lights: dict[str, list[str]] = {}
        kinds: dict[str, str] = {}
        for area_id, room in self._rooms().items():
            cfg = room.get("config") or {}
            for s in cfg.get("sensors") or []:
                sensors.setdefault(s.get("entity_id"), []).append(area_id)
                kinds.setdefault(s.get("entity_id"), s.get("kind"))
            for light in cfg.get("lights") or []:
                lights.setdefault(light, []).append(area_id)
        return sensors, lights, kinds

    def entity_info(self, entity_id: str, *, context: tuple | None = None) -> dict[str, Any]:
        idx = self.index()
        states = self.ha.states
        relays, switch_entities, _ = context[0] if context else self._relays_and_switches()
        sensor_rooms, light_rooms, kinds = context[1] if context else self._assignments()
        reg = idx.registry.get(entity_id)
        state = states.get(entity_id)
        device = idx.devices.get((reg or {}).get("device_id") or "")
        attrs = (state or {}).get("attributes") or {}
        category, confidence, reason = classify(entity_id, reg, state, device, relays=relays,
                                                switch_entities=switch_entities, assigned_kind=kinds.get(entity_id))
        area_id = (reg or {}).get("area_id") or (device or {}).get("area_id")
        return {
            "entity_id": entity_id,
            "name": attrs.get("friendly_name") or (reg or {}).get("name") or (reg or {}).get("original_name")
            or entity_id,
            "domain": entity_id.split(".", 1)[0],
            "category": category,
            "category_label": CATEGORIES[category][0],
            "category_confidence": confidence,
            "category_reason": reason,
            "suggested_kind": SUGGESTED_KIND.get(category, "other"),
            "device_class": attrs.get("device_class"),
            "unit": attrs.get("unit_of_measurement"),
            "icon": attrs.get("icon") or (reg or {}).get("icon"),
            "state": (state or {}).get("state"),
            "last_changed": (state or {}).get("last_changed"),
            "last_updated": (state or {}).get("last_updated"),
            "availability": _availability(entity_id, reg, state, idx, states),
            "capabilities": _capabilities(entity_id, state),
            "platform": (reg or {}).get("platform"),
            "registered": reg is not None,
            "hidden": bool((reg or {}).get("hidden_by")),
            "entity_category": (reg or {}).get("entity_category"),
            "device_id": device["id"] if device else None,
            "area_id": area_id,
            "area": (idx.areas.get(area_id) or {}).get("name") if area_id else None,
            "sensor_rooms": sensor_rooms.get(entity_id, []),
            "light_rooms": light_rooms.get(entity_id, []),
            "own": (reg or {}).get("platform") == INTEGRATION_DOMAIN,
        }

    def entity_live(self, entity_id: str) -> dict[str, Any]:
        """Small live view of one entity (for the live channel)."""
        idx = self.index()
        reg = idx.registry.get(entity_id)
        state = self.ha.states.get(entity_id)
        return {
            "entity_id": entity_id,
            "state": (state or {}).get("state"),
            "last_changed": (state or {}).get("last_changed"),
            "last_updated": (state or {}).get("last_updated"),
            "availability": _availability(entity_id, reg, state, idx, self.ha.states),
            "attrs": live_attributes(state),
        }

    def _context(self) -> tuple:
        return self._relays_and_switches(), self._assignments()

    def _discoverable(self, info: dict[str, Any]) -> bool:
        return info["category"] != "other" and not info["own"] and not info["hidden"] and \
            info["entity_category"] is None and info["availability"] != "disabled"

    def candidates(self) -> list[dict[str, Any]]:
        """Every classified entity in Home Assistant (for the searchable selectors)."""
        idx = self.index()
        ctx = self._context()
        out = []
        for entity_id in sorted(set(self.ha.states) | set(idx.registry)):
            if not ENTITY_ID.match(entity_id):
                continue
            info = self.entity_info(entity_id, context=ctx)
            if info["own"] or info["availability"] == "disabled":
                continue
            out.append({k: info[k] for k in ("entity_id", "name", "domain", "category", "category_label",
                                              "suggested_kind", "area_id", "area", "state", "availability",
                                              "device_id", "sensor_rooms", "light_rooms")})
        return out

    def _room_entities(self, area_id: str, cfg: dict[str, Any], ctx: tuple) -> dict[str, dict[str, Any]]:
        """Assigned entities plus every discoverable entity in the area."""
        idx = self.index()
        wanted = [s.get("entity_id") for s in cfg.get("sensors") or []] + list(cfg.get("lights") or [])
        if cfg.get("reference_occupancy_entity"):
            wanted.append(cfg["reference_occupancy_entity"])
        lighting = (self._rooms().get(area_id) or {}).get("lighting") or {}
        wanted += list(lighting.get("relays") or []) + list(lighting.get("managed") or [])
        for w in (self._rooms().get(area_id) or {}).get("curtains") or []:
            wanted += list(w.get("covers") or [])
        out: dict[str, dict[str, Any]] = {}
        for entity_id in wanted:
            if isinstance(entity_id, str) and entity_id not in out:
                out[entity_id] = self.entity_info(entity_id, context=ctx)
        for entity_id, reg in idx.registry.items():
            if entity_id in out:
                continue
            device = idx.devices.get(reg.get("device_id") or "")
            if (reg.get("area_id") or (device or {}).get("area_id")) != area_id:
                continue
            info = self.entity_info(entity_id, context=ctx)
            if self._discoverable(info):
                out[entity_id] = info
        return out

    def _room_summary(self, area: dict[str, Any] | None, area_id: str, ctx: tuple) -> dict[str, Any]:
        room = self._rooms().get(area_id)
        cfg = (room or {}).get("config") or {}
        model = self.model or {}
        entities = self._room_entities(area_id, cfg, ctx)
        sensors = [s.get("entity_id") for s in cfg.get("sensors") or []]
        lights = list(cfg.get("lights") or [])
        assigned = set(sensors) | set(lights) | ({cfg["reference_occupancy_entity"]}
                                                  if cfg.get("reference_occupancy_entity") else set())
        warnings = self._warnings(area_id, area, room, entities, assigned)
        idx = self.index()
        device_ids = {e["device_id"] for e in entities.values() if e["device_id"]}
        devices = [_device_view(idx.devices[d], idx, self.ha.states) for d in device_ids if d in idx.devices]
        counts = {
            "online": sum(1 for d in devices if d["status"] == "online"),
            "offline": sum(1 for d in devices if d["status"] == "offline"),
            "partial": sum(1 for d in devices if d["status"] == "partial"),
            "unavailable_entities": sum(1 for e in entities.values() if e["availability"] in (
                "unavailable", "device_offline", "not_provided")),
            "missing_entities": sum(1 for e in entities.values() if e["availability"] == "missing"),
        }
        if room is None:
            status = "not_configured"
        elif not cfg.get("enabled"):
            status = "disabled"
        elif any(w["level"] == "error" for w in warnings):
            status = "problem"
        else:
            status = "configured"
        lighting = (room or {}).get("lighting")
        occupancy = (room or {}).get("occupancy")
        unassigned = [e for e in entities.values() if e["entity_id"] not in assigned and e["area_id"] == area_id
                      and CATEGORIES[e["category"]][2] in ("presence", "lighting") and e["category"] not in (
                          "relay", "switch_output") and not e["sensor_rooms"] and not e["light_rooms"]]
        return {
            "area_id": area_id,
            "name": (area or {}).get("name") or area_id,
            "icon": (area or {}).get("icon"),
            "floor_id": (area or {}).get("floor_id"),
            "area_exists": area is not None,
            "configured": room is not None,
            "enabled": bool(cfg.get("enabled")),
            "status": status,
            "integration_status": (room or {}).get("status"),
            "occupancy": {k: occupancy.get(k) for k in ("state", "occupied", "reason")} if occupancy else None,
            "sensors": len(sensors),
            "lights": len(lights),
            "sensor_ids": sensors,
            "light_ids": lights,
            "unassigned_candidates": len(unassigned),
            "devices": counts,
            "automation": {
                "owner": "integration",
                "running": bool(model.get("loaded")),
                "lighting": None if lighting is None else ("automatic" if lighting.get("automatic") else "observing"),
                "switches_active": sum(1 for s in (lighting or {}).get("switches") or [] if s.get("mode") == "control"),
                "curtains_automatic": sum(1 for w in (room or {}).get("curtains") or [] if w.get("mode") == "automatic"),
            },
            "warnings": warnings,
        }

    def _warnings(self, area_id: str, area: dict[str, Any] | None, room: dict[str, Any] | None,
                  entities: dict[str, dict[str, Any]], assigned: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if room is None:
            return out
        cfg = room.get("config") or {}
        enabled = bool(cfg.get("enabled"))

        def add(code: str, level: str, title: str, detail: str, entity_id: str | None = None) -> None:
            out.append({"code": code, "level": level, "title": title, "detail": detail, "entity_id": entity_id})

        if area is None:
            add("area_missing", "error", "Area deleted",
                "This room's Home Assistant area no longer exists; its configuration is kept.")
        if not (self.model or {}).get("loaded"):
            add("integration_not_running", "warning", "Integration not running",
                "Smart Occupancy & Lighting is not running, so this room is not observed and changes cannot be saved.")
        for entity_id in sorted(assigned):
            e = entities.get(entity_id)
            if e is None:
                continue
            level = "error" if enabled else "info"
            what = {
                "missing": ("entity_missing", "Entity missing", "is assigned but does not exist in Home Assistant."),
                "disabled": ("entity_disabled", "Entity disabled", "is assigned but disabled in Home Assistant."),
                "not_provided": ("entity_not_provided", "Not provided by its integration",
                                 "is registered, but its integration does not provide it."),
                "device_offline": ("device_offline", "Device offline",
                                   "belongs to a device that is unreachable (all of its entities are unavailable)."),
                "unavailable": ("entity_unavailable", "Entity unavailable", "is unavailable."),
            }.get(e["availability"])
            if what:
                add(what[0], level if what[0] in ("entity_missing", "entity_disabled") else "warning",
                    what[1], f"{e['name']} ({entity_id}) {what[2]}", entity_id)
            others = [a for a in (e["sensor_rooms"] + e["light_rooms"]) if a != area_id]
            if entity_id in (cfg.get("lights") or []) and len(e["light_rooms"]) > 1:
                add("light_conflict", "warning", "Light in several rooms",
                    f"{e['name']} is a managed light of several rooms; both could send it commands.", entity_id)
            elif others and entity_id in {s.get("entity_id") for s in cfg.get("sensors") or []}:
                add("sensor_shared", "info", "Sensor used by several rooms",
                    f"{e['name']} is also assigned to {', '.join(self._area_name(a) for a in sorted(set(others)))}.",
                    entity_id)
            if e["area_id"] and e["area_id"] != area_id:
                add("outside_area", "info", "From another area",
                    f"{e['name']} is in the area {e['area'] or e['area_id']} in Home Assistant.", entity_id)
        if enabled:
            for p in room.get("problems") or []:
                level = {"config_error": "error", "unavailable": "warning", "warning": "warning"}.get(
                    p.get("classification"), "info")
                code = f"integration:{p.get('type')}"
                if p.get("type") in ("entity_missing", "entity_disabled", "entity_unavailable", "entity_not_provided",
                                     "signal_shared", "light_in_multiple_rooms") and p.get("entity_id") in assigned:
                    continue  # already listed from the App's own check
                out.append({"code": code, "level": level, "title": p.get("title"), "detail": p.get("explanation"),
                            "action": p.get("action"), "entity_id": p.get("entity_id"), "source": "integration"})
        return out

    def _area_name(self, area_id: str) -> str:
        return (self.index().areas.get(area_id) or {}).get("name") or area_id

    def overview(self) -> dict[str, Any]:
        idx = self.index()
        ctx = self._context()
        area_ids = list(idx.areas) + [a for a in self._rooms() if a not in idx.areas]
        rooms = [self._room_summary(idx.areas.get(a), a, ctx) for a in area_ids]
        rooms.sort(key=lambda r: str(r["name"]).casefold())
        return {"status": self.status(), "rooms": rooms, "generated_at": utcnow_iso()}

    def workspace(self, area_id: str) -> dict[str, Any]:
        if not AREA_ID.match(area_id):
            raise RoomsError("invalid_area", "Invalid area id", 400)
        idx = self.index()
        area = idx.areas.get(area_id)
        room = self._rooms().get(area_id)
        if area is None and room is None:
            raise RoomsError("unknown_area", "This area does not exist in Home Assistant", 404)
        ctx = self._context()
        cfg = (room or {}).get("config") or {
            "enabled": False, "sensors": [], "lights": [], "reference_occupancy_entity": None, "adjacent_rooms": []}
        entities = self._room_entities(area_id, cfg, ctx)
        device_ids = {e["device_id"] for e in entities.values() if e["device_id"]}
        devices = {d: _device_view(idx.devices[d], idx, self.ha.states) for d in device_ids if d in idx.devices}
        _, _, switch_devices = ctx[0]
        model = self.model or {}
        other_rooms = {
            a: {"name": self._area_name(a), "enabled": bool((r.get("config") or {}).get("enabled")),
                "sensors": [s.get("entity_id") for s in (r.get("config") or {}).get("sensors") or []],
                "lights": list((r.get("config") or {}).get("lights") or [])}
            for a, r in self._rooms().items() if a != area_id
        }
        return {
            "status": self.status(),
            "summary": self._room_summary(area, area_id, ctx),
            "area": {"id": area_id, "name": (area or {}).get("name") or area_id, "icon": (area or {}).get("icon"),
                     "exists": area is not None, "aliases": (area or {}).get("aliases") or []},
            "room": {
                "configured": room is not None,
                "config": cfg,
                "revision": (room or {}).get("revision") or _absent_revision(),
                "integration_status": (room or {}).get("status"),
                "occupancy": (room or {}).get("occupancy"),
                "problems": (room or {}).get("problems") or [],
                "lighting": (room or {}).get("lighting"),
                "curtains": (room or {}).get("curtains") or ((model.get("other_areas") or {}).get(area_id) or {}).get(
                    "curtains") or [],
            },
            "entities": entities,
            "devices": devices,
            "switch_devices": [d for d, a in switch_devices.items() if a == area_id],
            "other_rooms": other_rooms,
            "areas": sorted(({"id": a, "name": v.get("name") or a} for a, v in idx.areas.items()),
                            key=lambda x: str(x["name"]).casefold()),
            "schema": model.get("schema"),
            "timing": model.get("timing"),
            "categories": {k: {"label": v[0], "icon": v[1], "group": v[2]} for k, v in CATEGORIES.items()},
            "generated_at": utcnow_iso(),
        }

    # ---------------------------------------------------------- editing
    async def ensure(self) -> None:
        """Read the integration's configuration if it has not been read yet (or the last read failed)."""
        if self.api_state != "ok" or not self.model:
            await self.refresh()

    def _require_api(self) -> None:
        if self.api_state != "ok" or not self.model:
            raise RoomsError("api_unavailable", self.api_error or "The integration's room API is not available", 503)

    @staticmethod
    def clean_room(room: Any) -> dict[str, Any] | None:
        """Only the editable fields, in the integration's shape; the integration validates the rest."""
        if room is None:
            return None
        if not isinstance(room, dict):
            raise RoomsError("invalid_room", "The room must be an object")
        out: dict[str, Any] = {k: room[k] for k in ROOM_KEYS if k in room}
        sensors = out.get("sensors")
        if isinstance(sensors, list):
            out["sensors"] = [{k: s[k] for k in SENSOR_KEYS if k in s} if isinstance(s, dict) else s for s in sensors]
        return out

    @staticmethod
    def _check_args(area_id: str, revision: Any = None, confirmed: Any = None) -> list[str]:
        if not AREA_ID.match(area_id):
            raise RoomsError("invalid_area", "Invalid area id")
        if revision is not None and (not isinstance(revision, str) or not REVISION.match(revision)):
            raise RoomsError("invalid_revision", "Invalid revision")
        if confirmed is None:
            return []
        if not isinstance(confirmed, list) or len(confirmed) > 50 or not all(
                isinstance(c, str) and IMPACT_CODE.match(c) for c in confirmed):
            raise RoomsError("invalid_confirmation", "Invalid confirmation list")
        return list(confirmed)

    def _annotate(self, review: dict[str, Any], area_id: str, room: dict[str, Any] | None) -> dict[str, Any]:
        """Add the App's live entity view: availability, other areas, other rooms (no new rules)."""
        ctx = self._context()
        ids: set[str] = set()
        for c in review.get("changes") or []:
            if c.get("entity_id"):
                ids.add(c["entity_id"])
        for s in (room or {}).get("sensors") or []:
            ids.add(s.get("entity_id"))
        ids.update((room or {}).get("lights") or [])
        if (room or {}).get("reference_occupancy_entity"):
            ids.add(room["reference_occupancy_entity"])
        entities = {e: self.entity_info(e, context=ctx) for e in ids if isinstance(e, str) and ENTITY_ID.match(e)}
        conflicts = []
        for c in review.get("changes") or []:
            e = entities.get(c.get("entity_id") or "")
            if e is None:
                continue
            if c["type"] == "sensor_added" and [a for a in e["sensor_rooms"] if a != area_id]:
                conflicts.append({"code": "sensor_shared", "entity_id": e["entity_id"],
                                  "rooms": [self._area_name(a) for a in e["sensor_rooms"] if a != area_id]})
            if c["type"] == "light_added" and [a for a in e["light_rooms"] if a != area_id]:
                conflicts.append({"code": "light_conflict", "entity_id": e["entity_id"],
                                  "rooms": [self._area_name(a) for a in e["light_rooms"] if a != area_id]})
            if c["type"] in ("sensor_added", "light_added") and e["area_id"] and e["area_id"] != area_id:
                conflicts.append({"code": "outside_area", "entity_id": e["entity_id"],
                                  "rooms": [e["area"] or e["area_id"]]})
            if c["type"] == "sensor_added" and e["category"] in ("light", "light_group", "cover"):
                conflicts.append({"code": "category_mismatch", "entity_id": e["entity_id"],
                                  "rooms": [e["category_label"]]})
        attention = [e for e in entities.values() if e["availability"] != "ok"]
        return {**review, "entities": entities, "conflicts": conflicts,
                "attention": [{"entity_id": e["entity_id"], "name": e["name"], "availability": e["availability"]}
                              for e in attention]}

    async def review(self, area_id: str, room: Any, revision: Any) -> dict[str, Any]:
        self._check_args(area_id, revision)
        await self.ensure()
        self._require_api()
        cleaned = self.clean_room(room)
        result = await self._call("smart_occupancy/rooms/review", area_id=area_id, room=cleaned,
                                  expected_revision=revision)
        return self._annotate(result, area_id, cleaned)

    async def save(self, area_id: str, room: Any, revision: Any, confirmed: Any) -> dict[str, Any]:
        confirmed = self._check_args(area_id, revision, confirmed)
        if not isinstance(revision, str):
            raise RoomsError("invalid_revision", "A revision is required to save")
        cleaned = self.clean_room(room)
        await self.refresh()
        self._require_api()
        backup = self._write_backup(f"Before saving {self._area_name(area_id)}")
        try:
            result = await self._call("smart_occupancy/rooms/set", write=True, area_id=area_id, room=cleaned,
                                      expected_revision=revision, confirmed=confirmed)
        except RoomsError as err:
            if err.code != "connection_lost":
                self._delete_backup(backup)
            raise
        _LOGGER.info("Room %s saved through the integration (%s, %d changes)", area_id, result.get("applied"),
                     len(result.get("changes") or []))
        await self._settle(result.get("applied"))
        return {**result, "backup": backup}

    async def _settle(self, applied: Any, timeout: float = 15.0) -> None:
        """After a write that reloads the integration, wait until it runs again (so the answer is accurate)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout if applied == "reload" else 0)
        while True:
            await self.refresh()
            if (self.model or {}).get("loaded") or loop.time() >= deadline:
                return
            await asyncio.sleep(0.25)

    async def _call(self, command: str, *, write: bool = False, **params: Any) -> dict[str, Any]:
        try:
            if write:
                result = await self.ha.config_write(command, **params)
            else:
                result = await self.ha.command(command, **params)
        except HomeAssistantError as err:
            status = {"stale_revision": 409, "confirmation_required": 409, "not_loaded": 503,
                      "not_set_up": 503, "unauthorized": 403, "unknown_command": 503}.get(err.code, 400)
            message = str(err).split(": ", 1)[-1]
            raise RoomsError(err.code, message, status) from err
        except (ConnectionError, TimeoutError) as err:
            if write:
                raise RoomsError("connection_lost",
                                 "The connection to Home Assistant was lost while saving. The change may or may not "
                                 "have been saved: reload the room to see the stored configuration.", 503) from err
            raise RoomsError("not_connected", "Home Assistant is not connected", 503) from err
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------ backups
    def export(self, reason: str = "Export") -> dict[str, Any]:
        self._require_api()
        model = self.model or {}
        return {
            "format": BACKUP_FORMAT,
            "version": 1,
            "created_at": utcnow_iso(),
            "reason": reason,
            "app_version": APP_VERSION,
            "integration_version": model.get("integration_version"),
            "revision": model.get("revision"),
            "rooms": {a: r.get("config") for a, r in self._rooms().items()},
        }

    def _write_backup(self, reason: str) -> str | None:
        if self.backup_dir is None:
            return None
        data = self.export(reason)
        stamp = _now_stamp()
        name = f"rooms-{stamp}-{(data.get('revision') or '0')[:16]}"
        try:
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            path = self.backup_dir / f"{name}.json"
            n = 1
            while path.exists():
                n += 1
                path = self.backup_dir / f"{name}-{n}.json"
            path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            files = sorted(self.backup_dir.glob("rooms-*.json"), reverse=True)
            for old in files[BACKUP_KEEP:]:
                old.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not store a room configuration backup: %s", err)
            return None
        return path.stem

    def _delete_backup(self, backup_id: str | None) -> None:
        if self.backup_dir and backup_id and BACKUP_ID.match(backup_id):
            (self.backup_dir / f"{backup_id}.json").unlink(missing_ok=True)

    def backups(self) -> list[dict[str, Any]]:
        if self.backup_dir is None or not self.backup_dir.is_dir():
            return []
        out = []
        for path in sorted(self.backup_dir.glob("rooms-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({"id": path.stem, "created_at": data.get("created_at"), "reason": data.get("reason"),
                        "rooms": len(data.get("rooms") or {}), "revision": data.get("revision")})
        return out

    def backup(self, backup_id: str) -> dict[str, Any]:
        if self.backup_dir is None or not BACKUP_ID.match(backup_id):
            raise RoomsError("unknown_backup", "Unknown backup", 404)
        try:
            return json.loads((self.backup_dir / f"{backup_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise RoomsError("unknown_backup", "Unknown backup", 404) from err

    @staticmethod
    def _backup_rooms(backup: Any) -> dict[str, Any]:
        if not isinstance(backup, dict) or backup.get("format") != BACKUP_FORMAT or backup.get("version") != 1:
            raise RoomsError("invalid_backup", "This is not a Smart Occupancy room configuration backup")
        rooms = backup.get("rooms")
        if not isinstance(rooms, dict) or not all(isinstance(a, str) and AREA_ID.match(a) for a in rooms):
            raise RoomsError("invalid_backup", "The backup has no valid rooms section")
        return rooms

    async def restore_review(self, backup: Any) -> dict[str, Any]:
        await self.ensure()
        self._require_api()
        rooms = self._backup_rooms(backup)
        return await self._call("smart_occupancy/rooms/restore/review", rooms=rooms,
                                expected_revision=(self.model or {}).get("revision"))

    async def restore(self, backup: Any, revision: Any, confirmed: Any) -> dict[str, Any]:
        confirmed = self._check_args("restore", revision, confirmed)
        if not isinstance(revision, str):
            raise RoomsError("invalid_revision", "A revision is required to restore")
        rooms = self._backup_rooms(backup)
        await self.refresh()
        self._require_api()
        saved = self._write_backup("Before restoring a backup")
        try:
            result = await self._call("smart_occupancy/rooms/restore", write=True, rooms=rooms,
                                      expected_revision=revision, confirmed=confirmed)
        except RoomsError as err:
            if err.code != "connection_lost":
                self._delete_backup(saved)
            raise
        _LOGGER.info("Room configuration restored from a backup (%d rooms changed)", len(result.get("rooms") or []))
        await self._settle(result.get("applied"))
        return {**result, "backup": saved}


def _absent_revision() -> str:
    """Revision of a room that is not configured (the integration's revision_of(None))."""
    import hashlib

    return hashlib.sha256(b"null").hexdigest()[:16]
