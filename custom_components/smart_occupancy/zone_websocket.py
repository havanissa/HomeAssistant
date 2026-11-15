"""Websocket API of zones, relay lighting outputs, shared spaces and the Room Supervisor (0.6.0).

Reads are part of the room editor model (``zones_model``, key ``zones`` of
``smart_occupancy/lighting/editor``) and ``supervisor/room`` (the details of
every supervised target). Writes are admin only and go to the lighting store
(no reload). Nothing here sends a light command; ``supervisor/check`` runs
one reconciliation now (which may correct a device only in a room whose
supervisor is Automatic, exactly like the periodic check).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN, LUX_MAX_THRESHOLD, LUX_MIN, SUPERVISOR_LIMITS, TIMING_PARAMETERS, TRANSITION_MAX, ZONE_PRIORITY_MAX
from .controller import LightingController
from .discovery import area_name
from .occupancy import friendly_name
from .presence import _KIND_WORDS
from .targets import capabilities, is_relay
from .zones import (
    KELVIN_MAX,
    KELVIN_MIN,
    MAX_SPACES,
    MAX_ZONE_TARGETS,
    MAX_ZONES,
    SharedSpace,
    SpaceVacancy,
    SupervisorMode,
    SupervisorSettings,
    ZoneActivation,
    ZoneAction,
    ZoneConfig,
    ZoneDaylight,
    ZoneManual,
    ZoneRelease,
    ZoneUnavailable,
    slug,
)


def _controller(hass: HomeAssistant, connection, msg) -> LightingController | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.lighting


def _room(ctrl: LightingController, connection, msg) -> str | None:
    area_id = msg.get("area_id")
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return None
    return area_id


def relay_candidates(hass: HomeAssistant, area_id: str, chosen: tuple[str, ...]) -> list[dict[str, Any]]:
    """Switch entities that could be lighting outputs: those of the room's area first. Never our own."""
    ent_reg = er.async_get(hass)
    from homeassistant.helpers import device_registry as dr

    dev_reg = dr.async_get(hass)
    out = []
    for entry in ent_reg.entities.values():
        if entry.domain != "switch" or entry.platform == DOMAIN or entry.disabled_by is not None \
                or entry.entity_category is not None:
            continue
        device = dev_reg.async_get(entry.device_id) if entry.device_id else None
        area = entry.area_id or (device.area_id if device else None)
        out.append({"entity_id": entry.entity_id, "name": friendly_name(hass, entry.entity_id) or entry.entity_id,
                    "in_area": area == area_id, "chosen": entry.entity_id in chosen})
    for entity_id in chosen:
        if all(c["entity_id"] != entity_id for c in out):
            out.append({"entity_id": entity_id, "name": friendly_name(hass, entity_id) or entity_id,
                        "in_area": False, "chosen": True, "missing": True})
    out.sort(key=lambda c: (not c["chosen"], not c["in_area"], c["name"].casefold()))
    return out[:200]


def zones_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    """Everything Settings → Presence → Zones shows: zones, their live state, which rule controls each light."""
    lighting = ctrl.lighting
    room_lighting = lighting.room(area_id)
    room_cfg = ctrl.config.room(area_id)
    managed = ctrl.managed_room(area_id)
    status = {z["id"]: z for z in ctrl.zones.status(area_id)}
    plan = ctrl.effective_plan(area_id, "preview") if area_id in ctrl.config.enabled_rooms else None
    rules = []
    for item in plan.items if plan else []:
        rule = item.rule or {}
        if item.decision == "skipped" and item.reason_code in ("excluded", "covered_by_group"):
            continue
        rules.append({"entity_id": item.entity_id, "name": item.name or item.entity_id, "rule": rule.get("name"),
                      "kind": rule.get("kind"), "superseded": rule.get("superseded") or [],
                      "decision": item.decision, "reason": item.reason, "requested": item.requested})
    timing = ctrl.config.timing_for(area_id)
    space = lighting.space_of(area_id)
    return {
        "zones": [{**z.as_dict(), "status": status.get(z.id)} for z in room_lighting.zones],
        "rules": rules,
        "sensors": [{"entity_id": s.entity_id, "name": friendly_name(hass, s.entity_id) or s.entity_id,
                     "kind": s.kind.value, "kind_name": _KIND_WORDS.get(s.kind, s.kind.value), "used": s.participates}
                    for s in room_cfg.sensors],
        "targets": [{"entity_id": e, "name": friendly_name(hass, e) or e, "type": "relay" if is_relay(e) else "light",
                     "capabilities": capabilities(hass, e).as_dict(),
                     "is_group": e in managed.members or e in managed.opaque_groups}
                    for e in managed.controlled],
        "room_timing": {k: getattr(timing, k) for k in ("correlation_window", "exit_confirmation",
                                                         "inactivity_timeout", "dropout_grace")},
        "limits": {"priority": [1, ZONE_PRIORITY_MAX], "kelvin": [KELVIN_MIN, KELVIN_MAX],
                   "lux": [LUX_MIN, LUX_MAX_THRESHOLD], "transition": [0, TRANSITION_MAX],
                   "timing": {k: list(TIMING_PARAMETERS[k][1:]) for k in ("correlation_window", "exit_confirmation",
                                                                          "inactivity_timeout", "dropout_grace")},
                   "max_zones": MAX_ZONES, "max_targets": MAX_ZONE_TARGETS},
        "relays": list(room_lighting.relays),
        "relay_candidates": relay_candidates(hass, area_id, room_lighting.relays),
        "space": space.as_dict() | {"room_names": {r: area_name(hass, r) or r for r in space.rooms}} if space else None,
        "spaces": [s.as_dict() for s in lighting.shared_spaces],
        "space_rooms": [{"area_id": a, "name": area_name(hass, a) or a,
                         "adjacent": a in room_cfg.adjacent_rooms} for a in sorted(
                            ctrl.config.enabled_rooms, key=lambda a: (area_name(hass, a) or a).casefold())],
        "history": ctrl.zones.history(area_id)[-10:],
    }


# ------------------------------------------------------------------- schemas
_NONE_OR = lambda schema: vol.Any(None, schema)  # noqa: E731
_SECONDS = {k: vol.All(vol.Coerce(int), vol.Range(min=TIMING_PARAMETERS[k][1], max=TIMING_PARAMETERS[k][2]))
            for k in ("correlation_window", "exit_confirmation", "inactivity_timeout", "dropout_grace")}
_TARGET = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("action", default=ZoneAction.ON.value): vol.In([a.value for a in ZoneAction]),
    vol.Optional("brightness_pct"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=1, max=100))),
    vol.Optional("color_temp_kelvin"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=KELVIN_MIN, max=KELVIN_MAX))),
    vol.Optional("transition"): _NONE_OR(vol.All(vol.Coerce(float), vol.Range(min=0, max=TRANSITION_MAX))),
})
_ZONE = vol.Schema({
    vol.Optional("id"): vol.All(str, vol.Length(min=1, max=60)),
    vol.Optional("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=60)),
    vol.Optional("enabled"): bool,
    vol.Optional("sensors"): [cv.entity_id],
    vol.Optional("activation"): vol.In([a.value for a in ZoneActivation]),
    vol.Optional("timing"): {vol.Optional(k): _NONE_OR(v) for k, v in _SECONDS.items()},
    vol.Optional("unavailable"): vol.In([a.value for a in ZoneUnavailable]),
    vol.Optional("holds_room"): bool,
    vol.Optional("priority"): vol.All(vol.Coerce(int), vol.Range(min=1, max=ZONE_PRIORITY_MAX)),
    vol.Optional("manual"): vol.In([a.value for a in ZoneManual]),
    vol.Optional("daylight"): vol.In([a.value for a in ZoneDaylight]),
    vol.Optional("on_below"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=LUX_MIN, max=LUX_MAX_THRESHOLD))),
    vol.Optional("release"): vol.In([a.value for a in ZoneRelease]),
    vol.Optional("targets"): vol.All([_TARGET], vol.Length(max=MAX_ZONE_TARGETS)),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/zone/set",
    vol.Required("area_id"): str,
    vol.Required("zone"): _ZONE,
})
@websocket_api.require_admin
@callback
def ws_zone_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Add a zone (no ``id``) or change one (only the given keys change)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    room = ctrl.lighting.room(area_id)
    data = dict(msg["zone"])
    zone_id = data.pop("id", None)
    current = room.zone(zone_id) if zone_id else None
    if zone_id and current is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Zone {zone_id} does not exist")
        return
    if current is None:
        if len(room.zones) >= MAX_ZONES:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"At most {MAX_ZONES} zones per room")
            return
        name = data.get("name") or f"Zone {len(room.zones) + 1}"
        current = ZoneConfig(id=slug(name, {z.id for z in room.zones}), name=name)
    assigned = {s.entity_id for s in ctrl.config.room(area_id).sensors}
    if unknown := [e for e in data.get("sensors", []) if e not in assigned]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{', '.join(unknown)} is not a presence signal of this room")
        return
    if "targets" in data:
        controlled = set(ctrl.managed_room(area_id).controlled)
        allowed = controlled | {t.entity_id for t in current.targets}
        if bad := [t["entity_id"] for t in data["targets"] if t["entity_id"] not in allowed]:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  f"{', '.join(bad)} is not a light or relay output this room controls")
            return
    merged = current.as_dict()
    if "timing" in data:
        timing = dict(merged["timing"])
        for key, value in data.pop("timing").items():
            if value is None:
                timing.pop(key, None)
            else:
                timing[key] = value
        merged["timing"] = timing
    merged.update(data)
    zone = ZoneConfig.from_dict(merged)
    if zone is None:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Invalid zone")
        return
    zones = tuple(zone if z.id == zone.id else z for z in room.zones) if room.zone(zone.id) else (*room.zones, zone)
    ctrl.store.async_set(ctrl.with_room(area_id, zones=zones))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "zone": zone.as_dict()})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/zone/remove",
    vol.Required("area_id"): str,
    vol.Required("zone_id"): str,
})
@websocket_api.require_admin
@callback
def ws_zone_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Delete a zone. If it is active, its lights follow the remaining rules at once (like the zone ending)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    room = ctrl.lighting.room(area_id)
    if room.zone(msg["zone_id"]) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Zone {msg['zone_id']} does not exist")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, zones=tuple(z for z in room.zones if z.id != msg["zone_id"])))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/relay/set",
    vol.Required("area_id"): str,
    vol.Required("relays"): [cv.entity_id],
})
@websocket_api.require_admin
@callback
def ws_relay_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The switch entities that are lighting outputs of a room (explicit; switches are never assumed to be lights)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    relays = tuple(dict.fromkeys(msg["relays"]))
    if bad := [e for e in relays if not is_relay(e)]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{', '.join(bad)} is not a switch entity")
        return
    ent_reg = er.async_get(hass)
    if own := [e for e in relays if (entry := ent_reg.async_get(e)) is not None and entry.platform == DOMAIN]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{', '.join(own)} belongs to Smart Occupancy")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, relays=relays))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


_SPACE = vol.Schema({
    vol.Optional("id"): vol.All(str, vol.Length(min=1, max=60)),
    vol.Optional("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=60)),
    vol.Optional("rooms"): [str],
    vol.Optional("mood_override"): vol.Any(None, vol.All(str, vol.Length(max=100))),
    vol.Optional("vacancy"): vol.In([v.value for v in SpaceVacancy]),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/space/set",
    vol.Required("space"): _SPACE,
})
@websocket_api.require_admin
@callback
def ws_space_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Add (no ``id``) or change a shared space. A room belongs to at most one space."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    lighting = ctrl.lighting
    data = dict(msg["space"])
    space_id = data.pop("id", None)
    current = next((s for s in lighting.shared_spaces if s.id == space_id), None) if space_id else None
    if space_id and current is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Shared space {space_id} does not exist")
        return
    if current is None:
        if len(lighting.shared_spaces) >= MAX_SPACES:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"At most {MAX_SPACES} shared spaces")
            return
        name = data.get("name") or "Shared space"
        current = SharedSpace(id=slug(name, {s.id for s in lighting.shared_spaces}, "space"), name=name)
    rooms = tuple(dict.fromkeys(data.get("rooms", current.rooms)))
    if unknown := [r for r in rooms if r not in ctrl.config.rooms]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{', '.join(unknown)} is not a room")
        return
    taken = {r: s.name for s in lighting.shared_spaces if s.id != current.id for r in s.rooms}
    if clash := [r for r in rooms if r in taken]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{', '.join(area_name(hass, r) or r for r in clash)} already belongs to {taken[clash[0]]}")
        return
    mood = data.get("mood_override", current.mood_override)
    if isinstance(mood, str):
        mood = mood.strip() or None
    if mood and lighting.is_manual(mood):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{mood} is not a mood")
        return
    space = replace(current, name=data.get("name", current.name), rooms=rooms, mood_override=mood,
                    vacancy=SpaceVacancy(data.get("vacancy", current.vacancy)))
    spaces = tuple(space if s.id == space.id else s for s in lighting.shared_spaces) \
        if any(s.id == space.id for s in lighting.shared_spaces) else (*lighting.shared_spaces, space)
    ctrl.store.async_set(replace(lighting, shared_spaces=spaces))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "space": space.as_dict()})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/space/remove",
    vol.Required("space_id"): str,
})
@websocket_api.require_admin
@callback
def ws_space_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    lighting = ctrl.lighting
    if not any(s.id == msg["space_id"] for s in lighting.shared_spaces):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Shared space {msg['space_id']} does not exist")
        return
    ctrl.store.async_set(replace(lighting, shared_spaces=tuple(s for s in lighting.shared_spaces
                                                                if s.id != msg["space_id"])))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


_SUP_VALUES = {vol.Optional(k): _NONE_OR(vol.All(vol.Coerce(float), vol.Range(min=low, max=high)))
               for k, (_d, low, high) in SUPERVISOR_LIMITS.items()}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/supervisor/set",
    vol.Optional("area_id"): vol.Any(None, str),
    vol.Optional("mode"): vol.Any(None, vol.In([m.value for m in SupervisorMode])),
    vol.Optional("correct_external"): vol.Any(None, bool),
    **_SUP_VALUES,
})
@websocket_api.require_admin
@callback
def ws_supervisor_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Room Supervisor settings: a room's own values (``null``: global default) or, without a room, the defaults."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    keys = [k for k in (*SUPERVISOR_LIMITS, "mode", "correct_external") if k in msg]
    area_id = msg.get("area_id")
    if area_id is None:
        if any(msg[k] is None for k in keys):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A global default needs a value")
            return
        current = ctrl.lighting.supervisor.as_dict() | {k: msg[k] for k in keys}
        ctrl.store.async_set(replace(ctrl.lighting, supervisor=SupervisorSettings.from_dict(current, defaults=True)))
    else:
        if _room(ctrl, connection, msg) is None:
            return
        current = ctrl.lighting.room(area_id).supervisor.as_dict() | {k: msg[k] for k in keys}
        ctrl.store.async_set(ctrl.with_room(area_id, supervisor=SupervisorSettings.from_dict(current)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/supervisor/room",
    vol.Required("area_id"): str,
    vol.Optional("check"): bool,
})
@callback
def ws_supervisor_room(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The Room Supervisor's view of a room (every target: rule, desired, actual, commands, corrections).

    ``check`` (admin): reconcile now instead of waiting for the next periodic check.
    """
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    if msg.get("check"):
        if not connection.user.is_admin:
            connection.send_error(msg["id"], websocket_api.ERR_UNAUTHORIZED, "Only an administrator can run a check")
            return
        ctrl.supervisor.reconcile(area_id)
    connection.send_result(msg["id"], ctrl.supervisor.model(area_id, details=True))


ZONE_HANDLERS = (ws_zone_set, ws_zone_remove, ws_relay_set, ws_space_set, ws_space_remove, ws_supervisor_set,
                 ws_supervisor_room)
