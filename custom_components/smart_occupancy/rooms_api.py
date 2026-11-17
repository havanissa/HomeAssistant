"""Rooms configuration API for the Smart Lighting App (0.6.9, App Phase A2).

The App edits the room configuration that this integration already owns: the
config entry options ``rooms.<area_id>`` (``enabled``, ``sensors``,
``lights``, ``reference_occupancy_entity``, ``adjacent_rooms``). There is no
second configuration store; the integration's own room editor (Configure →
Rooms) reads and writes exactly the same keys, so both editors stay
interchangeable.

Commands (all administrator only, ``require_admin``):

* ``smart_occupancy/rooms/get`` - the room configuration, its revisions, the
  editor schema (kinds, roles, allowed domains) and read-only runtime data per
  room: occupancy decision, problems, lighting / switch / curtain summary.
* ``smart_occupancy/rooms/review`` - dry run of one room change: what changes,
  blocking errors, the problems the integration would report afterwards, and
  the impacts that need an explicit confirmation. Changes nothing.
* ``smart_occupancy/rooms/set`` - apply one room change. Refused when the
  room's revision differs from ``expected_revision`` (changed elsewhere in the
  meantime), when it has errors, or when an impact that needs confirmation is
  not listed in ``confirmed``.
* ``smart_occupancy/rooms/restore/review`` and ``smart_occupancy/rooms/restore``
  - the same for replacing the whole ``rooms`` section with a backup.

Writes only replace ``options["rooms"]`` (one room, or all rooms for a
restore); every other option key is passed through unchanged. Unknown keys
inside a room are kept, and settings the App does not edit (mood overrides,
timing overrides) are carried over. A sensor that is still assigned keeps its
role, inverted flag and Presence-tab exclusion unless the request changes them.

Applying a change uses the integration's existing paths: a change that only
includes or excludes already-assigned sensors is applied live (like the
Presence tab, ``coordinator.async_apply_presence``); every other change
updates the options, and the options update listener reloads the integration
as the options flow does. Neither path sends a device command: on start the
lighting controller takes the current mood and occupancy as its baseline and
applies nothing, and curtains never move at start-up. Automatic behaviour
that follows from the new configuration (for example a newly assigned light
in a room with Automatic lighting) is reported as an impact that must be
confirmed.

Revisions are short SHA-256 digests of the stored JSON (canonical form), so
they survive restarts and change whenever anything (including the options
flow) changes the stored configuration.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, config_validation as cv, entity_registry as er

from .config_flow import KIND_FIELDS
from .const import DEFAULT_ROLE_FOR_KIND, DOMAIN, VERSION, SensorKind, SensorRole
from .discovery import area_name
from .models import IntegrationConfig, RoomConfig, SensorAssignment
from .presence import _KIND_WORDS, _ROLE_WORDS, presence_model
from .validation import validate

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

API_VERSION = 1
BACKUP_FORMAT = "smart_occupancy.rooms_backup"

MAX_SENSORS = 100
MAX_LIGHTS = 200
MAX_ADJACENT = 50
MAX_ROOMS = 500
REFERENCE_DOMAINS = ("input_boolean", "binary_sensor")

# Impact levels: "confirm" impacts must be listed in ``confirmed`` to save.
CONFIRM = "confirm"
INFO = "info"

SENSOR_INPUT = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("kind"): vol.In([k.value for k in SensorKind]),
    vol.Optional("role"): vol.In([r.value for r in SensorRole]),
    vol.Optional("inverted"): bool,
    vol.Optional("enabled"): bool,
})
ROOM_INPUT = vol.Schema({
    vol.Required("enabled"): bool,
    vol.Required("sensors"): vol.All([SENSOR_INPUT], vol.Length(max=MAX_SENSORS)),
    vol.Required("lights"): vol.All([cv.entity_id], vol.Length(max=MAX_LIGHTS)),
    vol.Optional("reference_occupancy_entity", default=None): vol.Any(None, cv.entity_id),
    vol.Optional("adjacent_rooms", default=list): vol.All([vol.All(str, vol.Length(min=1, max=255))],
                                                          vol.Length(max=MAX_ADJACENT)),
})
# A backed-up room: the stored shape (as written by RoomConfig.as_dict()).
BACKUP_ROOM = vol.Schema({
    vol.Optional("enabled", default=False): bool,
    vol.Optional("sensors", default=list): vol.All([vol.Schema({
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("kind"): vol.In([k.value for k in SensorKind]),
        vol.Required("role"): vol.In([r.value for r in SensorRole]),
        vol.Optional("inverted", default=False): bool,
        vol.Optional("enabled"): bool,
    })], vol.Length(max=MAX_SENSORS)),
    vol.Optional("lights", default=list): vol.All([cv.entity_id], vol.Length(max=MAX_LIGHTS)),
    vol.Optional("reference_occupancy_entity", default=None): vol.Any(None, cv.entity_id),
    vol.Optional("adjacent_rooms", default=list): vol.All([str], vol.Length(max=MAX_ADJACENT)),
    vol.Optional("mood_overrides", default=dict): dict,
    vol.Optional("light_overrides", default=dict): dict,
    vol.Optional("timing", default=dict): dict,
}, extra=vol.ALLOW_EXTRA)


class RoomsApiError(Exception):
    """A request was refused; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


# ---------------------------------------------------------------- revisions
def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def revision_of(value: Any) -> str:
    """Short, stable digest of stored JSON (None for an absent room)."""
    return hashlib.sha256(_canonical(value).encode()).hexdigest()[:16]


def rooms_section(options: Any) -> dict[str, Any]:
    rooms = options.get("rooms") if isinstance(options, dict) else None
    return rooms if isinstance(rooms, dict) else {}


def room_revision(options: dict[str, Any], area_id: str) -> str:
    return revision_of(rooms_section(options).get(area_id))


# ------------------------------------------------------------------ helpers
def _entry(hass: HomeAssistant) -> ConfigEntry | None:
    entries = hass.config_entries.async_entries(DOMAIN)
    return entries[0] if entries else None


def _loaded_entry(hass: HomeAssistant) -> ConfigEntry:
    entry = _entry(hass)
    if entry is None:
        raise RoomsApiError("not_set_up", "Smart Occupancy & Lighting is not set up")
    if entry.state is not ConfigEntryState.LOADED:
        raise RoomsApiError(
            "not_loaded",
            f"Smart Occupancy & Lighting is not running (state: {entry.state.value}); configuration changes are "
            "refused until it runs again",
        )
    return entry


def _coordinator(entry: ConfigEntry | None) -> SmartOccupancyCoordinator | None:
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(entry, "runtime_data", None)


def _area_exists(hass: HomeAssistant, area_id: str) -> bool:
    return ar.async_get(hass).async_get_area(area_id) is not None


def _exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None


def _own(hass: HomeAssistant, entity_id: str) -> bool:
    entry = er.async_get(hass).async_get(entity_id)
    return entry is not None and entry.platform == DOMAIN


def _ordered(previous: list[str], wanted: list[str]) -> list[str]:
    """``wanted`` with the entries that were already there in their previous order, new ones appended."""
    keep = [e for e in previous if e in wanted]
    return keep + [e for e in wanted if e not in keep]


def _humanize(err: vol.Invalid) -> str:
    path = ".".join(str(p) for p in err.path)
    return f"{err.msg} ({path})" if path else str(err.msg)


def schema_info() -> dict[str, Any]:
    """What the App needs to build its forms (from the integration's own definitions)."""
    kinds = []
    for key, (kind, domains) in KIND_FIELDS.items():
        kinds.append({
            "value": kind.value, "key": key, "label": _KIND_WORDS.get(kind, kind.value),
            "domains": domains, "default_role": DEFAULT_ROLE_FOR_KIND[kind].value,
        })
    return {
        "kinds": kinds,
        "roles": [{"value": r.value, "label": _ROLE_WORDS.get(r, r.value)} for r in SensorRole],
        "reference_domains": list(REFERENCE_DOMAINS),
        "light_domains": ["light"],
        "limits": {"sensors": MAX_SENSORS, "lights": MAX_LIGHTS, "adjacent_rooms": MAX_ADJACENT},
        "editable": ["enabled", "sensors", "lights", "reference_occupancy_entity", "adjacent_rooms"],
    }


# ------------------------------------------------------------- build / diff
def build_room(area_id: str, old_raw: dict[str, Any] | None, edit: dict[str, Any]) -> dict[str, Any]:
    """The stored room after applying ``edit`` (already schema-validated) to ``old_raw``.

    Keeps every key the App does not edit. A sensor that stays assigned keeps
    its inverted flag and exclusion, and its role while its kind is the same
    (as the options flow does), unless the edit sets them.
    """
    old = RoomConfig.from_dict(area_id, old_raw or {})
    previous = {s.entity_id: s for s in old.sensors}
    by_id: dict[str, SensorAssignment] = {}
    for item in edit["sensors"]:
        kind = SensorKind(item["kind"])
        before = previous.get(item["entity_id"])
        same_kind = before is not None and before.kind == kind
        role = SensorRole(item["role"]) if "role" in item else (
            before.role if same_kind and before else DEFAULT_ROLE_FOR_KIND[kind])
        by_id[item["entity_id"]] = SensorAssignment(
            item["entity_id"], kind, role,
            item["inverted"] if "inverted" in item else (before.inverted if before else False),
            item["enabled"] if "enabled" in item else (before.enabled if before else True),
        )
    order = _ordered([s.entity_id for s in old.sensors], [s["entity_id"] for s in edit["sensors"]])
    lights = _ordered(list(old.lights), list(dict.fromkeys(edit["lights"])))
    adjacent = _ordered(list(old.adjacent_rooms),
                        [a for a in dict.fromkeys(edit.get("adjacent_rooms") or []) if a != area_id])
    room = replace(
        old,
        enabled=edit["enabled"],
        sensors=tuple(by_id[e] for e in order),
        lights=tuple(lights),
        reference_occupancy_entity=edit.get("reference_occupancy_entity") or None,
        adjacent_rooms=tuple(adjacent),
    )
    return {**(old_raw or {}), **room.as_dict()}


def diff_room(area_id: str, old_raw: dict[str, Any] | None, new_raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Every change between two stored rooms, in a stable order."""
    changes: list[dict[str, Any]] = []
    if old_raw is None and new_raw is None:
        return changes
    if new_raw is None:
        return [{"type": "room_removed", "area_id": area_id}]
    old = RoomConfig.from_dict(area_id, old_raw or {})
    new = RoomConfig.from_dict(area_id, new_raw)
    if old_raw is None:
        changes.append({"type": "room_added", "area_id": area_id})
    if old.enabled != new.enabled and not (old_raw is None and not new.enabled):
        changes.append({"type": "room_enabled" if new.enabled else "room_disabled", "area_id": area_id})
    before = {s.entity_id: s for s in old.sensors}
    after = {s.entity_id: s for s in new.sensors}
    for entity_id, s in after.items():
        if entity_id not in before:
            changes.append({"type": "sensor_added", "entity_id": entity_id, "kind": s.kind.value,
                            "role": s.role.value, "inverted": s.inverted, "enabled": s.enabled})
    for entity_id, s in before.items():
        if entity_id not in after:
            changes.append({"type": "sensor_removed", "entity_id": entity_id, "kind": s.kind.value,
                            "role": s.role.value})
    for entity_id, s in after.items():
        if (o := before.get(entity_id)) is None:
            continue
        for field, kind in (("kind", "sensor_kind"), ("role", "sensor_role"), ("inverted", "sensor_inverted"),
                            ("enabled", "sensor_enabled")):
            old_value, new_value = getattr(o, field), getattr(s, field)
            if old_value != new_value:
                changes.append({"type": kind, "entity_id": entity_id,
                                "old": getattr(old_value, "value", old_value),
                                "new": getattr(new_value, "value", new_value)})
    for light in new.lights:
        if light not in old.lights:
            changes.append({"type": "light_added", "entity_id": light})
    for light in old.lights:
        if light not in new.lights:
            changes.append({"type": "light_removed", "entity_id": light})
    if old.reference_occupancy_entity != new.reference_occupancy_entity:
        changes.append({"type": "reference_changed", "old": old.reference_occupancy_entity,
                        "new": new.reference_occupancy_entity})
    for a in new.adjacent_rooms:
        if a not in old.adjacent_rooms:
            changes.append({"type": "adjacent_added", "area_id": a})
    for a in old.adjacent_rooms:
        if a not in new.adjacent_rooms:
            changes.append({"type": "adjacent_removed", "area_id": a})
    if not changes and _canonical(old_raw) != _canonical(new_raw):
        changes.append({"type": "normalized", "area_id": area_id})
    return changes


# ------------------------------------------------------------------- checks
def room_errors(hass: HomeAssistant, area_id: str, old_raw: dict[str, Any] | None,
                edit: dict[str, Any] | None, *, restore: bool = False) -> list[dict[str, Any]]:
    """Blocking errors of an edited room (None: the room is removed)."""
    if edit is None:
        return []
    errors: list[dict[str, Any]] = []

    def err(code: str, message: str, entity_id: str | None = None) -> None:
        errors.append({"code": code, "message": message, **({"entity_id": entity_id} if entity_id else {})})

    old = RoomConfig.from_dict(area_id, old_raw or {})
    known = set(old.referenced_entities) | set(old.adjacent_rooms)
    if not _area_exists(hass, area_id) and not restore:
        err("area_missing", f"Area {area_id} does not exist in Home Assistant")
    seen: set[str] = set()
    for s in edit.get("sensors") or []:
        entity_id = s["entity_id"]
        if entity_id in seen:
            err("duplicate_sensor", f"{entity_id} is assigned twice", entity_id)
        seen.add(entity_id)
        domains = next((d for _, (k, d) in KIND_FIELDS.items() if k.value == s["kind"]), None)
        if domains is not None and entity_id.split(".", 1)[0] not in domains:
            err("sensor_domain",
                f"{entity_id} cannot be a {_KIND_WORDS[SensorKind(s['kind'])]} sensor "
                f"(allowed: {', '.join(domains)})", entity_id)
    lights = edit.get("lights") or []
    for light in {e for e in lights if lights.count(e) > 1}:
        err("duplicate_light", f"{light} is selected twice", light)
    for light in dict.fromkeys(lights):
        if light.split(".", 1)[0] != "light":
            err("not_a_light", f"{light} is not a light entity", light)
    ref = edit.get("reference_occupancy_entity")
    if ref and ref.split(".", 1)[0] not in REFERENCE_DOMAINS:
        err("reference_domain", f"The reference occupancy entity must be an {' or '.join(REFERENCE_DOMAINS)}", ref)
    for entity_id in [*seen, *dict.fromkeys(lights), *([ref] if ref else [])]:
        if _own(hass, entity_id):
            err("own_entity", f"{entity_id} belongs to Smart Occupancy itself and cannot be an input", entity_id)
        elif not restore and entity_id not in known and not _exists(hass, entity_id):
            # Assignments that already exist may point at a missing entity (kept, reported as a problem);
            # a new assignment must exist.
            err("entity_missing", f"{entity_id} does not exist in Home Assistant", entity_id)
    for neighbour in edit.get("adjacent_rooms") or []:
        if neighbour == area_id:
            continue
        if neighbour not in known and not _area_exists(hass, neighbour) and not restore:
            err("adjacent_unknown", f"Adjacent area {neighbour} does not exist")
    return errors


def _room_problems(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator | None,
                   config: IntegrationConfig, area_id: str) -> list[dict[str, Any]]:
    """What the integration's validation reports for this room (same code as its health checks)."""
    index = coordinator.lighting.index if coordinator else None
    settled = coordinator.settled if coordinator else True
    room = config.room(area_id)
    refs = set(room.referenced_entities)
    out = []
    for p in validate(hass, config, settled=settled, index=index):
        if p.area_id == area_id or (p.area_id is None and p.entity_id in refs):
            out.append({**p.as_dict(), "key": p.key})
    return out


def impacts(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator | None, area_id: str,
            old_raw: dict[str, Any] | None, new_raw: dict[str, Any] | None,
            changes: list[dict[str, Any]], *, live: bool) -> list[dict[str, Any]]:
    """What saving would do beyond the configuration itself. ``confirm`` items need explicit consent."""
    if not changes:
        return []
    out: list[dict[str, Any]] = []
    name = area_name(hass, area_id) or area_id

    def add(code: str, level: str, title: str, detail: str) -> None:
        out.append({"code": code, "level": level, "title": title, "detail": detail})

    old = RoomConfig.from_dict(area_id, old_raw or {})
    new = RoomConfig.from_dict(area_id, new_raw) if new_raw is not None else None
    was_enabled = old_raw is not None and old.enabled
    is_enabled = new is not None and new.enabled
    types = {c["type"] for c in changes}
    lighting = coordinator.lighting.lighting.room(area_id) if coordinator else None
    automatic = bool(lighting and lighting.control_enabled)

    if live:
        add("live_apply", INFO, "Applied without a reload",
            "Only the inclusion of already-assigned sensors changes. The integration applies it at once, like its "
            "Presence tab; nothing is switched or moved.")
    else:
        add("reload", INFO, "The integration reloads",
            "Smart Occupancy & Lighting reloads to apply the change (normally about a second), as it does after its "
            "own room editor. Occupancy is restored from its saved state, and the reload itself switches no light "
            "and moves no cover.")
    if is_enabled and not was_enabled:
        add("room_enabled", INFO, "Occupancy starts for this room",
            f"The integration creates occupancy entities for {name} and starts observing its sensors.")
        if automatic:
            add("automatic_lighting_resumes", CONFIRM, "Automatic lighting resumes",
                f"Automatic lighting was activated for {name} earlier. When the room is enabled, the integration "
                "controls its lights again whenever occupancy changes.")
    if was_enabled and not is_enabled:
        add("room_disabled" if new is not None else "room_removed", CONFIRM,
            "Room stops being observed" if new is not None else "Room configuration is removed",
            f"Occupancy for {name} stops and its Smart Occupancy entities are removed from Home Assistant. "
            "Automations, scripts and dashboards that use them stop working"
            + (" until the room is enabled again." if new is not None else ".")
            + (" Automatic lighting in this room stops." if automatic else ""))
    elif new is None and old_raw is not None:
        add("room_removed", CONFIRM, "Room configuration is removed",
            f"The sensor and light assignments of {name} are deleted from the integration's configuration.")
    if new is None and old_raw is not None and (old.timing or old.mood_overrides or old.light_overrides):
        add("room_settings_removed", CONFIRM, "Room presence timing is removed",
            "The room's own presence timing (and legacy mood overrides) stored with it are deleted too.")
    assignment_change = types & {"sensor_added", "sensor_removed", "sensor_kind", "sensor_role", "sensor_inverted",
                                 "sensor_enabled", "light_added", "light_removed"}
    if automatic and is_enabled and was_enabled and assignment_change:
        add("automatic_lighting", CONFIRM, "Automatic lighting is active in this room",
            f"After saving, the integration's automatic lighting in {name} uses the new assignments: an added light "
            "can be switched on or off when occupancy changes, and changed sensors change when that happens.")
    removed = {c["entity_id"] for c in changes if c["type"] == "sensor_removed"}
    if lighting and removed:
        zones = [z.name for z in lighting.zones if set(z.sensors) & removed]
        if zones:
            add("zone_sensor_removed", CONFIRM, "Sensors used by lighting zones",
                f"Removed sensors are used by the zones {', '.join(zones)}; those zones lose them.")
    if coordinator is not None:
        windows = [w for w in coordinator.curtains.config.windows.values()
                   if w.area_id == area_id and w.mode == "automatic"]
        if windows and (assignment_change or types & {"room_enabled", "room_disabled", "room_removed"}):
            add("automatic_curtains", INFO, "Automatic curtains use this room's occupancy",
                f"{', '.join(w.name for w in windows)} run in Automatic mode; their decisions follow the room's "
                "occupancy, which these changes affect.")
    return out


# ------------------------------------------------------------- read model
def _lighting_info(coordinator: SmartOccupancyCoordinator, area_id: str) -> dict[str, Any]:
    ctrl = coordinator.lighting
    room = ctrl.lighting.room(area_id)
    managed = ctrl.managed.get(area_id)
    return {
        "automatic": ctrl.room_active(area_id),
        "control_enabled": room.control_enabled,
        "activated_at": room.activated_at,
        "managed": list(managed.targets) if managed else [],
        "controlled": managed.controlled if managed else [],
        "discovered": sorted(managed.discovered) if managed else [],
        "excluded": dict(managed.excluded) if managed else {},
        "auto_discover": room.auto_discover,
        "relays": list(room.relays),
        "zones": [{"id": z.id, "name": z.name, "enabled": z.enabled, "sensors": list(z.sensors)} for z in room.zones],
        "switches": [{"id": s.id, "name": s.name, "mode": s.mode.value, "device_id": s.device_id,
                      "event_entity": s.event_entity} for s in room.switches],
        "manual_switches": list(room.manual_switches),
        "priority_inputs": [{"id": p.id, "name": p.name, "entity_id": p.entity_id, "enabled": p.enabled}
                            for p in room.priority_inputs],
        "vacancy_action": room.vacancy.action.value,
    }


def _curtain_info(coordinator: SmartOccupancyCoordinator, area_id: str) -> list[dict[str, Any]]:
    return [
        {"window_id": w.window_id, "name": w.name, "mode": w.mode,
         "covers": [layer.entity_id for layer in w.layers]}
        for w in coordinator.curtains.config.windows.values() if w.area_id == area_id
    ]


def _occupancy_info(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator, area_id: str) -> dict[str, Any] | None:
    result = coordinator.occupancy.result(area_id)
    if result is None:
        return None
    model = presence_model(hass, coordinator, area_id)
    return {
        **model["occupancy"],
        "reason_code": result.reason_code,
        "confidence": getattr(result.confidence, "value", result.confidence),
        "usable": result.usable,
        "last_activity": result.last_activity.isoformat(timespec="seconds") if result.last_activity else None,
        "last_activity_source": result.last_activity_source,
        "signals": [
            {k: s[k] for k in ("entity_id", "value", "status", "condition", "health", "label", "available")}
            for s in model["sensors"]
        ],
    }


def rooms_model(hass: HomeAssistant) -> dict[str, Any]:
    entry = _entry(hass)
    options = dict(entry.options) if entry else {}
    coordinator = _coordinator(entry)
    config = IntegrationConfig.from_dict(options)
    snapshot = coordinator.data if coordinator else None
    statuses = {r.area_id: r.status.value for r in snapshot.rooms} if snapshot else {}
    problems: dict[str, list[dict[str, Any]]] = {}
    if snapshot is not None:
        for p in snapshot.problems:
            if p.area_id:
                problems.setdefault(p.area_id, []).append({**snapshot.problem_dict(p), "key": p.key})
    rooms: dict[str, Any] = {}
    for area_id, raw in rooms_section(options).items():
        enabled = area_id in config.enabled_rooms
        info: dict[str, Any] = {
            "config": raw,
            "revision": revision_of(raw),
            "area_exists": _area_exists(hass, area_id),
            "status": statuses.get(area_id),
            "problems": problems.get(area_id, []),
            "occupancy": None,
            "lighting": None,
            "curtains": [],
        }
        if coordinator is not None:
            if enabled:
                info["occupancy"] = _occupancy_info(hass, coordinator, area_id)
            info["lighting"] = _lighting_info(coordinator, area_id)
            info["curtains"] = _curtain_info(coordinator, area_id)
        rooms[area_id] = info
    # Curtain windows and lighting of areas that have no room configuration (read-only context).
    unconfigured: dict[str, Any] = {}
    if coordinator is not None:
        for w in coordinator.curtains.config.windows.values():
            if w.area_id and w.area_id not in rooms:
                unconfigured.setdefault(w.area_id, {"curtains": []})["curtains"].append(
                    {"window_id": w.window_id, "name": w.name, "mode": w.mode,
                     "covers": [layer.entity_id for layer in w.layers]})
    return {
        "api_version": API_VERSION,
        "integration_version": VERSION,
        "entry_id": entry.entry_id if entry else None,
        "state": entry.state.value if entry else "not_set_up",
        "loaded": coordinator is not None,
        "writable": coordinator is not None,
        "operating_mode": config.operating_mode.value,
        "revision": revision_of(rooms_section(options)),
        "rooms": rooms,
        "other_areas": unconfigured,
        "timing": config.occupancy.as_dict(),
        "schema": schema_info(),
        "backup_format": BACKUP_FORMAT,
    }


# ----------------------------------------------------------------- planning
def _validated_edit(edit: Any) -> dict[str, Any] | None:
    if edit is None:
        return None
    try:
        return ROOM_INPUT(edit)
    except vol.Invalid as err:
        raise RoomsApiError("invalid_room", f"Invalid room: {_humanize(err)}") from err


def plan_room(hass: HomeAssistant, area_id: str, edit: Any, expected_revision: str | None) -> dict[str, Any]:
    """Everything ``review`` shows and ``set`` checks, for one room."""
    entry = _entry(hass)
    if entry is None:
        raise RoomsApiError("not_set_up", "Smart Occupancy & Lighting is not set up")
    coordinator = _coordinator(entry)
    options = dict(entry.options)
    current_rev = room_revision(options, area_id)
    old_raw = rooms_section(options).get(area_id)
    edit = _validated_edit(edit)
    errors = room_errors(hass, area_id, old_raw, edit)
    new_raw = build_room(area_id, old_raw, edit) if edit is not None and not errors else (
        None if edit is None else old_raw)
    changes = diff_room(area_id, old_raw, new_raw) if not errors else []
    rooms = dict(rooms_section(options))
    if new_raw is None:
        rooms.pop(area_id, None)
    else:
        rooms[area_id] = new_raw
    new_options = {**options, "rooms": rooms}
    old_config = IntegrationConfig.from_dict(options)
    new_config = IntegrationConfig.from_dict(new_options)
    live = bool(
        changes and coordinator is not None and {c["type"] for c in changes} == {"sensor_enabled"}
        and coordinator.occupancy.same_signals(new_config)
    )
    before = {p["key"]: p for p in _room_problems(hass, coordinator, old_config, area_id)}
    after = {p["key"]: p for p in _room_problems(hass, coordinator, new_config, area_id)}
    found = impacts(hass, coordinator, area_id, old_raw, new_raw, changes, live=live) if not errors else []
    return {
        "area_id": area_id,
        "room_revision": current_rev,
        "revision": revision_of(rooms_section(options)),
        "stale": expected_revision is not None and expected_revision != current_rev,
        "errors": errors,
        "changes": changes,
        "problems_after": list(after.values()),
        "problems_new": [p for k, p in after.items() if k not in before],
        "problems_resolved": [p for k, p in before.items() if k not in after],
        "impacts": found,
        "requires_confirmation": sorted({i["code"] for i in found if i["level"] == CONFIRM}),
        "apply": "none" if not changes else "live" if live else "reload",
        "writable": coordinator is not None,
        "_new_options": new_options,
        "_new_config": new_config,
    }


def plan_restore(hass: HomeAssistant, backup_rooms: Any, expected_revision: str | None) -> dict[str, Any]:
    entry = _entry(hass)
    if entry is None:
        raise RoomsApiError("not_set_up", "Smart Occupancy & Lighting is not set up")
    coordinator = _coordinator(entry)
    if not isinstance(backup_rooms, dict) or len(backup_rooms) > MAX_ROOMS:
        raise RoomsApiError("invalid_backup", "The backup has no valid rooms section")
    options = dict(entry.options)
    current = rooms_section(options)
    restored: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    for area_id, raw in backup_rooms.items():
        if not isinstance(area_id, str) or not area_id or len(area_id) > 255:
            raise RoomsApiError("invalid_backup", "The backup contains an invalid area id")
        try:
            cleaned = BACKUP_ROOM(raw)
            normalized = RoomConfig.from_dict(area_id, cleaned).as_dict()
        except (vol.Invalid, KeyError, TypeError, ValueError, AttributeError) as err:
            message = _humanize(err) if isinstance(err, vol.Invalid) else str(err)
            raise RoomsApiError("invalid_backup", f"Room {area_id} in the backup is invalid: {message}") from err
        for e in room_errors(hass, area_id, current.get(area_id), cleaned, restore=True):
            errors.append({**e, "area_id": area_id})
        restored[area_id] = {**raw, **normalized}
    per_room = []
    found: list[dict[str, Any]] = []
    for area_id in sorted(set(current) | set(restored)):
        changes = diff_room(area_id, current.get(area_id), restored.get(area_id))
        if not changes:
            continue
        missing = [e for e in RoomConfig.from_dict(area_id, restored.get(area_id) or {}).referenced_entities
                   if not _exists(hass, e)]
        per_room.append({"area_id": area_id, "name": area_name(hass, area_id), "changes": changes,
                         "area_exists": _area_exists(hass, area_id), "missing_entities": missing})
        for i in impacts(hass, coordinator, area_id, current.get(area_id), restored.get(area_id), changes,
                         live=False):
            if i["code"] != "reload":
                found.append({**i, "area_id": area_id})
    if per_room:
        found.insert(0, {"code": "restore", "level": CONFIRM, "title": "Replace the room configuration",
                         "detail": "Every room's sensor and light assignments are replaced with the backup; the "
                                   "integration reloads. Other settings (moods, lighting, curtains) are not changed."})
    return {
        "revision": revision_of(current),
        "stale": expected_revision is not None and expected_revision != revision_of(current),
        "errors": errors,
        "rooms": per_room,
        "impacts": found,
        "requires_confirmation": sorted({i["code"] for i in found if i["level"] == CONFIRM}),
        "writable": coordinator is not None,
        "_new_options": {**options, "rooms": restored},
    }


def _public(plan: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in plan.items() if not k.startswith("_")}


def _check_write(plan: dict[str, Any], confirmed: list[str]) -> None:
    if plan["stale"]:
        raise RoomsApiError(
            "stale_revision",
            "The room configuration was changed elsewhere since it was loaded. Review the current configuration "
            "and save again.",
            {"revision": plan.get("room_revision", plan["revision"])},
        )
    if plan["errors"]:
        raise RoomsApiError("invalid_room", "; ".join(e["message"] for e in plan["errors"]),
                            {"errors": plan["errors"]})
    if missing := [c for c in plan["requires_confirmation"] if c not in confirmed]:
        raise RoomsApiError("confirmation_required",
                            "These consequences must be confirmed: " + ", ".join(missing),
                            {"requires_confirmation": missing})


# ------------------------------------------------------------------ commands
def _send_error(connection: websocket_api.ActiveConnection, msg: dict, err: RoomsApiError) -> None:
    # Stable code + a message for people; a client gets the details (revision, errors, confirmations) by
    # running the matching review command again.
    connection.send_error(msg["id"], err.code, str(err))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/rooms/get"})
@websocket_api.require_admin
@callback
def ws_rooms_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The room configuration with revisions, the editor schema and read-only runtime data."""
    connection.send_result(msg["id"], rooms_model(hass))


_ROOM_MSG = {
    vol.Required("area_id"): vol.All(str, vol.Length(min=1, max=255)),
    vol.Required("room"): vol.Any(None, dict),
    vol.Optional("expected_revision"): vol.Any(None, str),
}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/rooms/review", **_ROOM_MSG})
@websocket_api.require_admin
@callback
def ws_rooms_review(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Dry run of one room change. Changes nothing."""
    try:
        plan = plan_room(hass, msg["area_id"], msg["room"], msg.get("expected_revision"))
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    connection.send_result(msg["id"], _public(plan))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/rooms/set", **_ROOM_MSG,
    vol.Required("expected_revision"): str,
    vol.Optional("confirmed", default=list): [str],
})
@websocket_api.require_admin
@callback
def ws_rooms_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Apply one reviewed room change (see the module docstring)."""
    try:
        entry = _loaded_entry(hass)
        plan = plan_room(hass, msg["area_id"], msg["room"], msg["expected_revision"])
        _check_write(plan, msg["confirmed"])
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    coordinator: SmartOccupancyCoordinator = entry.runtime_data
    if plan["apply"] != "none":
        from .presence_log import presence_log

        presence_log(hass).expect_source("app")  # the Rooms API is the Smart Lighting App's
    if plan["apply"] == "live" and coordinator.async_apply_presence(plan["_new_config"], options=plan["_new_options"]):
        pass
    elif plan["apply"] != "none":
        hass.config_entries.async_update_entry(entry, options=plan["_new_options"])
    options = dict(entry.options)
    connection.send_result(msg["id"], {
        "applied": plan["apply"],
        "changes": plan["changes"],
        "room_revision": room_revision(options, msg["area_id"]),
        "revision": revision_of(rooms_section(options)),
    })


_RESTORE_MSG = {
    vol.Required("rooms"): dict,
    vol.Optional("expected_revision"): vol.Any(None, str),
}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/rooms/restore/review", **_RESTORE_MSG})
@websocket_api.require_admin
@callback
def ws_rooms_restore_review(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Dry run of restoring a backed-up rooms section. Changes nothing."""
    try:
        plan = plan_restore(hass, msg["rooms"], msg.get("expected_revision"))
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    connection.send_result(msg["id"], _public(plan))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/rooms/restore", **_RESTORE_MSG,
    vol.Required("expected_revision"): str,
    vol.Optional("confirmed", default=list): [str],
})
@websocket_api.require_admin
@callback
def ws_rooms_restore(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Replace the rooms section with a reviewed backup (the integration reloads)."""
    try:
        entry = _loaded_entry(hass)
        plan = plan_restore(hass, msg["rooms"], msg["expected_revision"])
        _check_write(plan, msg["confirmed"])
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    if plan["rooms"]:
        hass.config_entries.async_update_entry(entry, options=plan["_new_options"])
    connection.send_result(msg["id"], {
        "applied": "reload" if plan["rooms"] else "none",
        "rooms": [r["area_id"] for r in plan["rooms"]],
        "revision": revision_of(rooms_section(dict(entry.options))),
    })


ROOMS_HANDLERS = (ws_rooms_get, ws_rooms_review, ws_rooms_set, ws_rooms_restore_review, ws_rooms_restore)
