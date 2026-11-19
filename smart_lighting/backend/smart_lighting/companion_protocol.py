"""Companion protocol (Phase A6): shared by the companion integration and the Smart Lighting App.

The companion integration is the App's bridge into Home Assistant: it owns the
native entities, executes the App's device commands behind an ownership fence,
and keeps the ownership records. This module holds everything both sides must
agree on: the protocol constants, the ownership rules (epoch, lease, sequence),
the command policy (which services, which data, which targets) and the shape of
every published view. It imports nothing from Home Assistant and nothing from
the App, so the two copies can be byte-identical (``scripts/check_protocol.py``).

Ownership is per control domain: ``occupancy`` (who publishes room occupancy),
``lighting`` (who may command lights, relays and switch actions), ``curtains``
(who may command covers) and ``forecast`` (who publishes the forecasts). The
companion itself never decides anything about a room; it only verifies that a
command comes from the one App session that holds the current lease, and that
the command is one the policy allows for a configured target.
"""

from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import re
from typing import Any

PROTOCOL_VERSION = 1
#: The control domains with their own ownership record, lease and sequence.
DOMAINS = ("occupancy", "lighting", "curtains", "forecast")
#: Domains whose owner may command physical devices through ``execute``.
COMMAND_DOMAINS = ("lighting", "curtains")

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
#: An accepted command may take this long in Home Assistant before it is reported as failed.
COMMAND_TIMEOUT_SECONDS = 20.0
#: How long an action relayed from a dashboard card waits for the App's answer.
ACTION_TIMEOUT_SECONDS = 20.0
HISTORY_SIZE = 30
MAX_SESSIONS = 4
MAX_RENAMES = 50
MAX_ENTITIES = 100
MAX_ROOMS = 500
MAX_VIEW_BYTES = 4_000_000
MAX_TARGETS = 2000

OWNER_NONE = "none"
OWNER_APP = "app"

#: Ownership states as the companion reports them: ``none`` (never claimed or revoked), ``app`` (the App holds a
#: live lease) and ``app_away`` (the App is the designated controller, but its session or lease is gone; the
#: published views are kept and marked stale; nothing is commanded).
STATE_NONE = "none"
STATE_APP = "app"
STATE_APP_AWAY = "app_away"

REASON_CLAIM = "claimed"
REASON_RELEASE = "released"
REASON_REVOKED = "revoked"
REASON_LEASE_EXPIRED = "lease_expired"
REASON_SESSION_CLOSED = "session_closed"
REASON_RESTART = "home_assistant_restarted"
REASON_PAIRED = "paired"

REJECT_INVALID = "invalid"
REJECT_VERSION = "version_mismatch"
REJECT_BUSY = "busy"
REJECT_NOT_PAIRED = "not_paired"
REJECT_NOT_OWNER = "not_owner"
REJECT_STALE_EPOCH = "stale_epoch"
REJECT_LEASE = "lease_invalid"
REJECT_SEQUENCE = "sequence"
REJECT_UNKNOWN_SESSION = "unknown_session"
REJECT_NOT_ALLOWED = "not_allowed"
REJECT_FAILED = "service_failed"
REJECT_TIMEOUT = "timeout"
REJECT_NOT_RETIRED = "not_retired"
REJECT_NO_LEGACY = "no_legacy_data"
#: Codes that tell the App it no longer owns a domain (it must stop sending for it at once).
LOST_CODES = frozenset({REJECT_NOT_OWNER, REJECT_STALE_EPOCH, REJECT_LEASE, REJECT_UNKNOWN_SESSION})

#: Purposes of a lighting command (what in the App's lighting runtime sent it) and of a cover command.
LIGHTING_PURPOSES = frozenset({"lighting", "switch", "person", "test", "supervisor", "priority", "preliminary",
                               "vacancy", "adaptive", "zone", "ambient", "mood"})
CURTAIN_PURPOSES = frozenset({"manual_confirmed", "automatic"})

#: Light service data a command may carry (Home Assistant's light.turn_on fields).
LIGHT_DATA = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct", "color_temp_kelvin", "kelvin",
    "rgb_color", "rgbw_color", "rgbww_color", "hs_color", "xy_color", "color_name", "white", "effect",
    "transition", "flash", "profile",
})
OFF_DATA = frozenset({"transition", "flash"})
LIGHT_SERVICES = frozenset({"turn_on", "turn_off", "toggle"})
#: Numeric light data and its bounds (Home Assistant's own ranges); colour tuples are lists of numbers.
LIGHT_RANGES: dict[str, tuple[float, float]] = {
    "brightness": (0, 255), "brightness_pct": (0, 100), "brightness_step": (-255, 255),
    "brightness_step_pct": (-100, 100), "color_temp_kelvin": (1000, 10000), "kelvin": (1000, 10000),
    "transition": (0, 6553), "white": (0, 255),
}
LIGHT_TUPLES: dict[str, tuple[int, float, float]] = {
    "rgb_color": (3, 0, 255), "rgbw_color": (4, 0, 255), "rgbww_color": (5, 0, 255), "hs_color": (2, 0, 360),
    "xy_color": (2, 0, 1),
}


def _check_light_data(data: dict[str, Any]) -> None:
    """Every value of a light command is of the right type and within Home Assistant's range."""
    for key, value in data.items():
        if key == "entity_id":
            continue
        if key in LIGHT_RANGES:
            low, high = LIGHT_RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"{key} out of range: {value!r}")
        elif key in LIGHT_TUPLES:
            size, low, high = LIGHT_TUPLES[key]
            if not isinstance(value, list) or len(value) != size or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and low <= v <= high for v in value):
                raise ProtocolError(REJECT_NOT_ALLOWED, f"{key} is not a valid colour: {value!r}")
        elif key in ("color_name", "effect", "flash", "profile"):
            if not isinstance(value, str) or not 0 < len(value) <= 64:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"{key} is not a short string")
COVER_SERVICES = frozenset({"set_cover_position", "open_cover", "close_cover"})

#: Actions a dashboard card may ask the companion to relay to the App (the App performs them with its own
#: validated handlers; the companion only relays and reports the answer).
ACTIONS = frozenset({"room/resume", "room/mode", "mood/select", "curtains/apply", "curtains/resume",
                     "curtains/mode"})

#: Keys of the per-room occupancy entity views the App publishes (``occupancy`` domain).
OCCUPANCY_ENTITY_KEYS = ("occupied", "state", "confidence", "last_activity", "reason", "helper_comparison")
LIGHTING_ENTITY_KEYS = ("mood_lighting", "daylight")
FORECAST_ENTITY_KEYS = ("forecast", "forecast_confidence", "predicted")
GLOBAL_SENSOR_KEYS = ("status", "operating_mode", "effective_mood", "configured_rooms", "light_categories",
                      "discovered_sensors", "missing_entities", "unavailable_entities", "config_problems")

_ENTITY = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
_AREA = re.compile(r"^[\w\-]{1,255}$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ID = re.compile(r"^[0-9a-f]{32}$")
_REVISION = re.compile(r"^[0-9a-f]{1,64}$")


class ProtocolError(ValueError):
    """A message was refused (``code`` is one of the REJECT_* codes); nothing changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def valid_context_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ULID.match(value))


def valid_entity_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ENTITY.match(value))


def valid_area_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_AREA.match(value))


def valid_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(_REVISION.match(value))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normal(value: Any) -> Any:
    """Equal documents, equal text: tuples are lists and whole floats are ints (2.0 and 2 are one value)."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if isinstance(value, dict):
        return {str(k): _normal(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normal(v) for v in value]
    return value


def canonical(doc: Any) -> str:
    return json.dumps(_normal(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def revision(doc: Any) -> str:
    """Short digest of a document: equal documents, equal revision."""
    return hashlib.sha256(canonical(doc).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- targets
def check_targets(domain: str, targets: Any) -> dict[str, list[str]]:
    """The owner's command targets, published with its claim and every configuration change.

    ``lighting``: ``lights`` (managed lights and the members of managed groups), ``relays`` (configured
    ``switch.*`` relays) and ``actions`` (scenes, scripts and selects a configured switch mapping activates, plus
    the configured mood selector entities). ``curtains``: ``covers`` (every cover of a configured window).
    """
    if not isinstance(targets, dict):
        raise ProtocolError(REJECT_INVALID, "targets must be an object")
    allowed = {"lighting": ("lights", "relays", "actions"), "curtains": ("covers",)}.get(domain)
    if allowed is None:
        return {}
    if set(targets) - set(allowed):
        raise ProtocolError(REJECT_INVALID, f"unknown target sections: {sorted(set(targets) - set(allowed))}")
    out: dict[str, list[str]] = {}
    total = 0
    for key in allowed:
        items = targets.get(key) or []
        if not isinstance(items, list) or not all(valid_entity_id(e) for e in items):
            raise ProtocolError(REJECT_INVALID, f"invalid {key}")
        domains = {"lights": ("light",), "relays": ("switch",), "actions": ("scene", "script", "select",
                                                                            "input_select"),
                   "covers": ("cover",)}[key]
        if any(e.split(".", 1)[0] not in domains for e in items):
            raise ProtocolError(REJECT_INVALID, f"{key} must be {' / '.join(domains)} entities")
        out[key] = sorted(set(items))
        total += len(out[key])
    if total > MAX_TARGETS:
        raise ProtocolError(REJECT_INVALID, "too many targets")
    return out


def check_command(domain: str, service_domain: Any, service: Any, data: Any, *,
                  targets: dict[str, Iterable[str]]) -> list[str]:
    """Validate one command of the owner of ``domain``; returns its entity ids. Raises ProtocolError."""
    if not isinstance(service_domain, str) or not isinstance(service, str) or not isinstance(data, dict):
        raise ProtocolError(REJECT_INVALID, "invalid command")
    raw = data.get("entity_id")
    ids = [raw] if isinstance(raw, str) else raw
    if not isinstance(ids, list) or not ids or len(ids) > MAX_ENTITIES or not all(valid_entity_id(e) for e in ids):
        raise ProtocolError(REJECT_INVALID, "invalid entity_id")
    if any(e.split(".", 1)[0] != service_domain for e in ids):
        raise ProtocolError(REJECT_NOT_ALLOWED, "every target must belong to the service's domain")
    extra = set(data) - {"entity_id"}
    if domain == "lighting":
        lights = set(targets.get("lights") or ())
        relays = set(targets.get("relays") or ())
        actions = set(targets.get("actions") or ())
        if service_domain == "light":
            if service not in LIGHT_SERVICES:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"light.{service} is not allowed")
            allowed = OFF_DATA if service == "turn_off" else LIGHT_DATA
            if extra - allowed:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"light data not allowed: {sorted(extra - allowed)}")
            _check_light_data(data)
            if unknown := [e for e in ids if e not in lights]:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured lighting target: {', '.join(unknown)}")
        elif service_domain == "switch":
            if service not in LIGHT_SERVICES or extra:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"switch.{service} is not allowed")
            if unknown := [e for e in ids if e not in relays]:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured relay: {', '.join(unknown)}")
        elif (service_domain, service) in (("scene", "turn_on"), ("script", "turn_on"),
                                           ("select", "select_option"), ("input_select", "select_option")):
            allowed = {"option"} if service.endswith("select_option") else {"transition"} if service_domain == "scene" \
                else set()
            if len(ids) != 1 or extra - allowed:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"{service_domain}.{service}: invalid data")
            if service.endswith("select_option") and not isinstance(data.get("option"), str):
                raise ProtocolError(REJECT_INVALID, "select_option needs an option")
            if ids[0] not in actions:
                raise ProtocolError(REJECT_NOT_ALLOWED, f"{ids[0]} is not a configured switch action or mood selector")
        else:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"{service_domain}.{service} is not allowed")
    elif domain == "curtains":
        covers = set(targets.get("covers") or ())
        if service_domain != "cover" or service not in COVER_SERVICES:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"{service_domain}.{service} is not allowed")
        if len(ids) != 1:
            raise ProtocolError(REJECT_NOT_ALLOWED, "one cover per command")
        if service == "set_cover_position":
            position = data.get("position")
            if extra != {"position"} or isinstance(position, bool) or not isinstance(position, int) \
                    or not 0 <= position <= 100:
                raise ProtocolError(REJECT_NOT_ALLOWED, "set_cover_position needs a whole-number position 0-100")
        elif extra:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"cover.{service} takes no data")
        if ids[0] not in covers:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured cover: {ids[0]}")
    else:
        raise ProtocolError(REJECT_NOT_ALLOWED, f"{domain} commands nothing")
    return ids


# ------------------------------------------------------------------ views
def _check_entity_view(value: Any, key: str) -> dict[str, Any]:
    """One entity's published view: ``{"value": state or None, "attributes": {...}, "available": bool}``."""
    if not isinstance(value, dict):
        raise ProtocolError(REJECT_INVALID, f"{key}: not an object")
    if set(value) - {"value", "attributes", "available"}:
        raise ProtocolError(REJECT_INVALID, f"{key}: unknown fields")
    state = value.get("value")
    if state is not None and not isinstance(state, (str, int, float, bool)):
        raise ProtocolError(REJECT_INVALID, f"{key}: invalid value")
    attributes = value.get("attributes") or {}
    if not isinstance(attributes, dict) or not all(isinstance(k, str) for k in attributes):
        raise ProtocolError(REJECT_INVALID, f"{key}: invalid attributes")
    available = value.get("available", True)
    if not isinstance(available, bool):
        raise ProtocolError(REJECT_INVALID, f"{key}: invalid availability")
    return {"value": state, "attributes": attributes, "available": available}


def _check_room_views(rooms: Any, keys: tuple[str, ...], what: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rooms, dict) or len(rooms) > MAX_ROOMS:
        raise ProtocolError(REJECT_INVALID, f"{what}: invalid rooms")
    out: dict[str, dict[str, Any]] = {}
    for area_id, entities in rooms.items():
        if not valid_area_id(area_id) or not isinstance(entities, dict):
            raise ProtocolError(REJECT_INVALID, f"{what}: invalid room {area_id!r}")
        if set(entities) - set(keys):
            raise ProtocolError(REJECT_INVALID, f"{what} {area_id}: unknown entities {sorted(set(entities) - set(keys))}")
        out[area_id] = {k: (None if v is None else _check_entity_view(v, f"{area_id}.{k}")) for k, v in entities.items()}
    return out


def check_view(domain: str, view: Any) -> dict[str, Any]:
    """A published view of one domain (what the native entities show). Validated, bounded."""
    if not isinstance(view, dict):
        raise ProtocolError(REJECT_INVALID, "view must be an object")
    if len(canonical(view)) > MAX_VIEW_BYTES:
        raise ProtocolError(REJECT_INVALID, "view too large")
    if domain == "occupancy":
        if set(view) - {"rooms"}:
            raise ProtocolError(REJECT_INVALID, "occupancy view: unknown fields")
        return {"rooms": _check_room_views(view.get("rooms") or {}, OCCUPANCY_ENTITY_KEYS, "occupancy")}
    if domain == "lighting":
        if set(view) - {"rooms", "manual_mode", "summary"}:
            raise ProtocolError(REJECT_INVALID, "lighting view: unknown fields")
        summary = view.get("summary")
        if summary is not None and not isinstance(summary, dict):
            raise ProtocolError(REJECT_INVALID, "lighting view: invalid summary")
        return {"rooms": _check_room_views(view.get("rooms") or {}, LIGHTING_ENTITY_KEYS, "lighting"),
                "manual_mode": bool(view.get("manual_mode")), "summary": summary or {}}
    if domain == "curtains":
        if set(view) - {"windows", "summary"}:
            raise ProtocolError(REJECT_INVALID, "curtains view: unknown fields")
        windows = view.get("windows") or {}
        if not isinstance(windows, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in windows.items()):
            raise ProtocolError(REJECT_INVALID, "curtains view: invalid windows")
        summary = view.get("summary")
        if summary is not None and not isinstance(summary, dict):
            raise ProtocolError(REJECT_INVALID, "curtains view: invalid summary")
        return {"windows": windows, "summary": summary or {}}
    if domain == "forecast":
        if set(view) - {"rooms", "predicted_next_room", "forecast_learning"}:
            raise ProtocolError(REJECT_INVALID, "forecast view: unknown fields")
        out: dict[str, Any] = {"rooms": _check_room_views(view.get("rooms") or {}, FORECAST_ENTITY_KEYS, "forecast")}
        for key in ("predicted_next_room", "forecast_learning"):
            out[key] = None if view.get(key) is None else _check_entity_view(view[key], key)
        return out
    if domain == "system":
        if set(view) - {"rooms", "sensors", "room_observation", "mood_mismatch", "mood_select", "operating_mode"}:
            raise ProtocolError(REJECT_INVALID, "system view: unknown fields")
        rooms = view.get("rooms") or {}
        if not isinstance(rooms, dict) or len(rooms) > MAX_ROOMS:
            raise ProtocolError(REJECT_INVALID, "system view: invalid rooms")
        clean_rooms: dict[str, dict[str, Any]] = {}
        for area_id, room in rooms.items():
            if not valid_area_id(area_id) or not isinstance(room, dict) or not isinstance(room.get("name"), str) \
                    or not 1 <= len(room["name"]) <= 255:
                raise ProtocolError(REJECT_INVALID, f"system view: invalid room {area_id!r}")
            reference = room.get("reference")
            if reference is not None and not valid_entity_id(reference):
                raise ProtocolError(REJECT_INVALID, f"system view: invalid reference of {area_id}")
            clean_rooms[area_id] = {"name": room["name"], "reference": reference}
        sensors = view.get("sensors") or {}
        if not isinstance(sensors, dict) or set(sensors) - set(GLOBAL_SENSOR_KEYS):
            raise ProtocolError(REJECT_INVALID, "system view: invalid sensors")
        observation = view.get("room_observation") or {}
        if not isinstance(observation, dict):
            raise ProtocolError(REJECT_INVALID, "system view: invalid room observation")
        select = view.get("mood_select") or {"enabled": False, "options": []}
        if not isinstance(select, dict) or not isinstance(select.get("enabled"), bool) \
                or not isinstance(select.get("options"), list) or len(select["options"]) > 100 \
                or not all(isinstance(o, str) and 1 <= len(o) <= 64 for o in select["options"]):
            raise ProtocolError(REJECT_INVALID, "system view: invalid mood selector")
        return {
            "rooms": clean_rooms,
            "sensors": {k: _check_entity_view(v, k) for k, v in sensors.items()},
            "room_observation": {a: _check_entity_view(v, a) for a, v in observation.items() if valid_area_id(a)},
            "mood_mismatch": None if view.get("mood_mismatch") is None else _check_entity_view(view["mood_mismatch"],
                                                                                              "mood_mismatch"),
            "mood_select": {"enabled": select["enabled"], "options": list(select["options"])},
        }
    raise ProtocolError(REJECT_INVALID, f"unknown view {domain}")


def check_renames(value: Any) -> list[list[str]]:
    out: list[list[str]] = []
    for item in value if isinstance(value, list) else ():
        if isinstance(item, list) and len(item) == 2 and all(valid_entity_id(e) for e in item):
            out.append([item[0], item[1]])
    return out[-MAX_RENAMES:]
