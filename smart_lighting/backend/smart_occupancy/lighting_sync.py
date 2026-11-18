"""Lighting bridge protocol (Phase A4): shared by the integration and the Smart Lighting App.

One record decides who controls lighting (``lighting_bridge.py``). This module
holds what both sides must agree on: the protocol constants, the configuration
revision, the command allowlist and the validation of every message that can
reach a device or change the mirror. It imports nothing from Home Assistant.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
import hashlib
import json
import re
from typing import Any

PROTOCOL_VERSION = 1
LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
RESUME_GRACE_SECONDS = 120
#: How long a handover or a fallback waits for commands that are already being sent.
DRAIN_SECONDS = 10.0
#: How long a sent command's context is remembered as "ours" by the next owner (attribution).
OWN_CONTEXT_SECONDS = 60
HISTORY_SIZE = 30
MAX_ENTITIES = 100
MAX_STATE_BYTES = 2_000_000
MAX_CONFIG_BYTES = 2_000_000

OWNER_INTEGRATION = "integration"
OWNER_APP = "app"

REASON_HANDOVER = "handover"
REASON_RELEASE = "released"
REASON_SERVICE = "returned_by_service"
REASON_LEASE_EXPIRED = "lease_expired"
REASON_APP_RESTARTED = "app_restarted"
REASON_NOT_RESUMED = "not_resumed"
REASON_RESUMED = "resumed"
REASON_REVOKED = "revoked"

REJECT_INVALID = "invalid"
REJECT_VERSION = "version_mismatch"
REJECT_BUSY = "busy"
REJECT_NOT_READY = "not_ready"
REJECT_CONFIG = "config_mismatch"
REJECT_NOT_OWNER = "not_owner"
REJECT_STALE_EPOCH = "stale_epoch"
REJECT_LEASE = "lease_invalid"
REJECT_SEQUENCE = "sequence"
REJECT_UNKNOWN_SESSION = "unknown_session"
REJECT_NOT_ALLOWED = "not_allowed"
REJECT_FAILED = "service_failed"
#: Codes that tell the App it no longer owns lighting.
LOST_CODES = frozenset({REJECT_NOT_OWNER, REJECT_STALE_EPOCH, REJECT_LEASE, REJECT_UNKNOWN_SESSION})

#: Purposes of a command (what in the lighting runtime sent it).
PURPOSES = frozenset({"lighting", "switch", "person", "test", "supervisor", "priority", "preliminary", "vacancy",
                      "adaptive", "zone", "ambient"})

#: Light service data a command may carry (Home Assistant's light.turn_on fields).
LIGHT_DATA = frozenset({
    "brightness", "brightness_pct", "brightness_step", "brightness_step_pct", "color_temp_kelvin", "kelvin",
    "rgb_color", "rgbw_color", "rgbww_color", "hs_color", "xy_color", "color_name", "white", "effect",
    "transition", "flash", "profile",
})
OFF_DATA = frozenset({"transition", "flash"})
LIGHT_SERVICES = frozenset({"turn_on", "turn_off", "toggle"})

_ENTITY = re.compile(r"^(?!.+__)(?!_)[\da-z_]+(?<!_)\.(?!_)[\da-z_]+(?<!_)$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_ID = re.compile(r"^[0-9a-f]{32}$")


class ProtocolError(ValueError):
    """A message was refused (``code`` is one of the REJECT_* codes)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID.match(value))


def valid_context_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ULID.match(value))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _normal(value: Any) -> Any:
    """Equal configurations, equal text: tuples are lists and whole floats are ints (2.0 and 2 are one value)."""
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


def config_revision(doc: dict[str, Any]) -> str:
    """Revision of a lighting configuration document (``LightingConfig.as_dict()``): equal documents, equal revision."""
    return hashlib.sha256(canonical(doc).encode()).hexdigest()[:16]


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def referenced_entities(*docs: Any) -> set[str]:
    """Every light / switch / scene / script / select entity id mentioned anywhere in the documents."""
    found: set[str] = set()
    for doc in docs:
        for text in _strings(doc):
            if _ENTITY.match(text) and text.split(".", 1)[0] in ("light", "switch", "scene", "script", "select"):
                found.add(text)
    return found


def relays(doc: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for room in (doc.get("rooms") or {}).values():
        if isinstance(room, dict):
            out.update(e for e in room.get("relays") or () if isinstance(e, str) and e.startswith("switch."))
    return out


def switch_action_entities(doc: dict[str, Any]) -> set[str]:
    """Scenes, scripts and selects (WLED presets) that configured switch mappings activate."""
    out: set[str] = set()
    for room in (doc.get("rooms") or {}).values():
        for switch in (room or {}).get("switches") or () if isinstance(room, dict) else ():
            mappings = (switch or {}).get("mappings") if isinstance(switch, dict) else None
            for mapping in (mappings.values() if isinstance(mappings, dict) else mappings or ()):
                entity = mapping.get("entity") if isinstance(mapping, dict) else None
                if isinstance(entity, str) and entity.split(".", 1)[0] in ("scene", "script", "select"):
                    out.add(entity)
    return out


def check_command(domain: Any, service: Any, data: Any, *, lights: Iterable[str], doc: dict[str, Any]) -> list[str]:
    """Validate one command; returns its entity ids. Raises ProtocolError (REJECT_NOT_ALLOWED / REJECT_INVALID).

    ``lights`` are the light targets the configuration controls (managed room lights, their group members and
    every light the configuration names). Relays must be configured relays; scenes, scripts and selects must be
    activated by a configured switch mapping.
    """
    if not isinstance(domain, str) or not isinstance(service, str) or not isinstance(data, dict):
        raise ProtocolError(REJECT_INVALID, "invalid command")
    raw = data.get("entity_id")
    ids = [raw] if isinstance(raw, str) else raw
    if not isinstance(ids, list) or not ids or len(ids) > MAX_ENTITIES or \
            not all(isinstance(e, str) and _ENTITY.match(e) for e in ids):
        raise ProtocolError(REJECT_INVALID, "invalid entity_id")
    if any(e.split(".", 1)[0] != domain for e in ids):
        raise ProtocolError(REJECT_NOT_ALLOWED, "every target must belong to the service's domain")
    extra = set(data) - {"entity_id"}
    if domain == "light":
        if service not in LIGHT_SERVICES:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"light.{service} is not allowed")
        allowed = OFF_DATA if service == "turn_off" else LIGHT_DATA
        if extra - allowed:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"light data not allowed: {sorted(extra - allowed)}")
        known = set(lights) | referenced_entities(doc)
        if unknown := [e for e in ids if e not in known]:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured lighting target: {', '.join(unknown)}")
    elif domain == "switch":
        if service not in LIGHT_SERVICES or extra:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"switch.{service} is not allowed")
        if unknown := [e for e in ids if e not in relays(doc)]:
            raise ProtocolError(REJECT_NOT_ALLOWED, f"not a configured relay: {', '.join(unknown)}")
    elif (domain, service) in (("scene", "turn_on"), ("script", "turn_on"), ("select", "select_option")):
        if len(ids) != 1 or extra - ({"option"} if domain == "select" else {"transition"} if domain == "scene" else set()):
            raise ProtocolError(REJECT_NOT_ALLOWED, f"{domain}.{service}: invalid data")
        if domain == "select" and not isinstance(data.get("option"), str):
            raise ProtocolError(REJECT_INVALID, "select_option needs an option")
        if ids[0] not in switch_action_entities(doc):
            raise ProtocolError(REJECT_NOT_ALLOWED, f"{ids[0]} is not used by a configured switch mapping")
    else:
        raise ProtocolError(REJECT_NOT_ALLOWED, f"{domain}.{service} is not allowed")
    return ids


def check_state(state: Any) -> dict[str, Any]:
    """A published runtime state (``LightingController`` state store data): an object of the known sections."""
    if not isinstance(state, dict):
        raise ProtocolError(REJECT_INVALID, "state must be an object")
    known = {"rooms", "adaptive", "daylight", "switches", "decisions", "zones", "ambient", "priority"}
    if set(state) - known:
        raise ProtocolError(REJECT_INVALID, f"unknown state sections: {sorted(set(state) - known)}")
    if len(canonical(state)) > MAX_STATE_BYTES:
        raise ProtocolError(REJECT_INVALID, "state too large")
    return state


def merge_decisions(app_state: dict[str, Any], local: dict[str, Any] | None,
                    keep_local: Iterable[str] = ("forecast",)) -> dict[str, Any]:
    """The App's state with the integration's own decision subjects kept (the forecasts log there too)."""
    merged = dict(app_state)
    logs = dict(((app_state.get("decisions") or {}).get("logs") or {}))
    prefixes = tuple(keep_local)
    for subject, items in ((local or {}).get("logs") or {}).items():
        if isinstance(subject, str) and subject.startswith(prefixes):
            logs[subject] = items
    merged["decisions"] = {"version": 1, "logs": logs}
    return merged
