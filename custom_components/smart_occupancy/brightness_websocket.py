"""Websocket API of ambient light levels and brightness rules (0.6.7).

Reads are part of the room editor model (``brightness_model``, key
``brightness`` of ``smart_occupancy/lighting/editor``) and
``brightness/preview`` (what enrolling a light, or a draft matrix, would mean
now). Writes are admin only and go to the lighting store (no reload).
Nothing here sends a light command: an enrolled light follows its matrix at
the next lighting trigger (occupancy, a zone, the ambient level, the mood).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .ambient import (
    DEAD_BAND_MAX,
    HYSTERESIS_MAX,
    LEVEL_KEYS,
    LEVELS,
    MIN_INTERVAL_MAX,
    STABLE_MAX,
    THRESHOLD_MAX,
    TRANSITION_LIMIT,
    AmbientFallback,
    AmbientSettings,
    valid_thresholds,
)
from .brightness import (
    BUILTIN_TEMPLATES,
    DEFAULT_TEMPLATE,
    MAX_TEMPLATES,
    LightMatrix,
    Template,
    cells_from,
    find_template,
    with_matrix,
)
from .controller import LightingController
from .occupancy import friendly_name
from .targets import capabilities, is_relay
from .zone_websocket import _controller, _room
from .zones import slug


def brightness_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    """Everything Settings → Lighting shows: the ambient levels now, and each light's brightness rules."""
    lighting = ctrl.lighting
    room = lighting.room(area_id)
    managed = ctrl.managed_room(area_id)
    rt = ctrl.runtime.get(area_id)
    status = ctrl.ambient.status(area_id)
    daylight = ctrl.daylight.status(area_id)
    plan = ctrl.effective_plan(area_id, "preview")
    items = {i.entity_id: i for i in plan.items}
    active = {z.zone.id for z in ctrl.zones.active(area_id)}
    lights = []
    for entity_id in managed.controlled:
        matrix = room.brightness.get(entity_id)
        item = items.get(entity_id)
        rule = (item.rule or {}) if item else {}
        lights.append({
            "entity_id": entity_id, "name": friendly_name(hass, entity_id) or entity_id,
            "type": "relay" if is_relay(entity_id) else "light",
            "capabilities": capabilities(hass, entity_id).as_dict(),
            "is_group": entity_id in managed.members or entity_id in managed.opaque_groups,
            "enrolled": matrix is not None, "matrix": matrix.as_dict() if matrix else None,
            "columns": [{"id": "room", "name": "Room occupied"}] + [
                {"id": z.id, "name": z.name, "priority": z.priority, "enabled": z.enabled, "active": z.id in active,
                 "target": z.target(entity_id).as_dict()} for z in room.zones if z.target(entity_id)],
            "now": {"decision": item.decision, "reason": item.reason, "reason_code": item.reason_code,
                    "requested": item.requested, "current": item.current, "rule": rule.get("name"),
                    "brightness": rule.get("brightness"), "superseded": rule.get("superseded") or []}
            if item else None,
            "override": rt.overrides.get(entity_id) if rt else None,
        })
    settings = room.ambient.settings
    return {
        "ambient": {
            "enabled": room.ambient.enabled,
            "settings": settings.as_dict(),
            "global": lighting.ambient.as_dict(),
            "effective": lighting.ambient_for(area_id).as_dict(),
            "sources": lighting.ambient.sources(settings),
            "status": status.as_dict(),
            "sensors": [r.as_dict() for r in daylight.readings],
            "history": ctrl.ambient.history(area_id)[-10:],
        },
        "levels": [{"key": k, "name": n} for k, n in LEVELS],
        "lights": lights,
        "zones": [{"id": z.id, "name": z.name, "priority": z.priority, "active": z.id in active} for z in room.zones],
        "templates": [t.as_dict() for t in (*BUILTIN_TEMPLATES, *lighting.brightness_templates)],
        "default_template": DEFAULT_TEMPLATE,
        "limits": {"threshold": [1, THRESHOLD_MAX], "hysteresis": [0, HYSTERESIS_MAX], "stable": [0, STABLE_MAX],
                   "transition": [0, TRANSITION_LIMIT], "dead_band": [0, DEAD_BAND_MAX],
                   "min_interval": [0, MIN_INTERVAL_MAX], "max_templates": MAX_TEMPLATES},
    }


# ------------------------------------------------------------------- schemas
_NONE_OR = lambda schema: vol.Any(None, schema)  # noqa: E731
_PCT = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100)))
_CELLS = vol.Schema({vol.Optional(k): _PCT for k in LEVEL_KEYS})
_TUNING = {
    vol.Optional("thresholds"): _NONE_OR(vol.All([vol.All(vol.Coerce(int), vol.Range(min=1, max=THRESHOLD_MAX))],
                                                 vol.Length(min=len(LEVELS) - 1, max=len(LEVELS) - 1))),
    vol.Optional("hysteresis"): _NONE_OR(vol.All(vol.Coerce(float), vol.Range(min=0, max=HYSTERESIS_MAX))),
    vol.Optional("stable_seconds"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=0, max=STABLE_MAX))),
    vol.Optional("fallback"): _NONE_OR(vol.In([f.value for f in AmbientFallback])),
    vol.Optional("fallback_level"): _NONE_OR(vol.In(LEVEL_KEYS)),
    vol.Optional("transition"): _NONE_OR(vol.All(vol.Coerce(float), vol.Range(min=0, max=TRANSITION_LIMIT))),
    vol.Optional("dead_band"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=0, max=DEAD_BAND_MAX))),
    vol.Optional("min_interval"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=0, max=MIN_INTERVAL_MAX))),
}
_MATRIX = vol.Schema({
    vol.Optional("enabled"): bool,
    vol.Optional("room"): _CELLS,
    vol.Optional("zones"): {str: _CELLS},
    vol.Optional("template"): vol.Any(None, str),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/ambient/set",
    vol.Optional("area_id"): vol.Any(None, str),
    vol.Optional("enabled"): bool,
    **_TUNING,
})
@websocket_api.require_admin
@callback
def ws_ambient_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Ambient levels: a room's switch and own values (``null``: global default) or, without a room, the defaults.

    Changing the levels never sends a command: the new category is used at the next trigger.
    """
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    keys = [k for k in ("thresholds", "hysteresis", "stable_seconds", "fallback", "fallback_level", "transition",
                        "dead_band", "min_interval") if k in msg]
    if "thresholds" in msg and msg["thresholds"] is not None and valid_thresholds(msg["thresholds"]) is None:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              "Each threshold must be higher than the one before (e.g. 5 and 20 lx)")
        return
    area_id = msg.get("area_id")
    if area_id is None:
        if "enabled" in msg:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Ambient control is switched per room")
            return
        if any(msg[k] is None for k in keys):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A global default needs a value")
            return
        current = ctrl.lighting.ambient.as_dict() | {k: msg[k] for k in keys}
        ctrl.store.async_set(replace(ctrl.lighting, ambient=AmbientSettings.from_dict(current, defaults=True)))
    else:
        if _room(ctrl, connection, msg) is None:
            return
        room = ctrl.lighting.room(area_id)
        current = room.ambient.settings.as_dict() | {k: msg[k] for k in keys}
        ambient = replace(room.ambient, settings=AmbientSettings.from_dict(current),
                          enabled=msg.get("enabled", room.ambient.enabled))
        ctrl.store.async_set(ctrl.with_room(area_id, ambient=ambient))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


def _light(ctrl: LightingController, connection, msg, area_id: str) -> str | None:
    entity_id = msg["entity_id"]
    if entity_id not in ctrl.managed_room(area_id).controlled and entity_id not in ctrl.lighting.room(
            area_id).brightness:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{entity_id} is not a light or relay output this room controls")
        return None
    return entity_id


def _matrix_from(ctrl: LightingController, connection, msg, area_id: str, entity_id: str,
                 data: dict[str, Any]) -> LightMatrix | None:
    """A matrix from a (partial) message: given keys replace the stored ones; zone columns must list the light."""
    room = ctrl.lighting.room(area_id)
    current = room.brightness.get(entity_id) or LightMatrix(entity_id=entity_id)
    zones = {z.id: z for z in room.zones}
    if bad := [z for z in data.get("zones", {}) if z not in zones or zones[z].target(entity_id) is None]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{', '.join(bad)}: not a zone of this room that lists the light")
        return None
    room_cells = cells_from({**current.room, **data["room"]}) if "room" in data else current.room
    columns = dict(current.zones)
    for zone_id, cells in data.get("zones", {}).items():
        columns[zone_id] = cells_from({**columns.get(zone_id, {}), **cells})
    return replace(current, enabled=data.get("enabled", current.enabled), room=room_cells, zones=columns,
                   template=data.get("template", current.template))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/brightness/preview",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("matrix"): _MATRIX,
    vol.Optional("template"): str,
})
@callback
def ws_brightness_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """What enrolling a light (or a draft of its matrix) would do now. Never sends anything."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None \
            or (entity_id := _light(ctrl, connection, msg, area_id)) is None:
        return
    matrix = None
    room = ctrl.lighting.room(area_id)
    if "matrix" in msg:
        matrix = _matrix_from(ctrl, connection, msg, area_id, entity_id, msg["matrix"])
        if matrix is None:
            return
    elif "template" in msg:
        template = find_template(ctrl.lighting.brightness_templates, msg["template"])
        if template is None:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Template {msg['template']} does not exist")
            return
        matrix = template.matrix(entity_id, [z.id for z in room.zones if z.target(entity_id)])
    connection.send_result(msg["id"], ctrl.brightness_preview(area_id, entity_id, matrix))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/brightness/set",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("matrix"): _MATRIX,
    vol.Optional("confirm"): bool,
})
@websocket_api.require_admin
@callback
def ws_brightness_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Enrol a light (``confirm`` after its preview) or change its matrix. Never sends a command."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None \
            or (entity_id := _light(ctrl, connection, msg, area_id)) is None:
        return
    room = ctrl.lighting.room(area_id)
    new = entity_id not in room.brightness
    if new and not msg.get("confirm"):
        connection.send_error(msg["id"], "preview_required",
                              "Enrolling a light needs its preview first (brightness/preview), then confirm")
        return
    matrix = _matrix_from(ctrl, connection, msg, area_id, entity_id, msg["matrix"])
    if matrix is None:
        return
    if new:
        matrix = replace(matrix, enrolled_at=dt_util.utcnow().isoformat(timespec="seconds"),
                         enrolled_by=connection.user.id if connection.user else None)
    ctrl.store.async_set(ctrl.with_room(area_id, brightness=with_matrix(room.brightness, matrix, entity_id)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "matrix": matrix.as_dict(), "sent": False})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/brightness/remove",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): cv.entity_id,
})
@websocket_api.require_admin
@callback
def ws_brightness_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Leave the brightness rules: the light follows its mood profile and zone targets again (next trigger)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    room = ctrl.lighting.room(area_id)
    if msg["entity_id"] not in room.brightness:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{msg['entity_id']} is not enrolled")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, brightness=with_matrix(room.brightness, None, msg["entity_id"])))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/brightness/template",
    vol.Required("op"): vol.In(["save", "delete"]),
    vol.Optional("template_id"): str,
    vol.Optional("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=60)),
    vol.Optional("room"): _CELLS,
    vol.Optional("zone"): _CELLS,
})
@websocket_api.require_admin
@callback
def ws_brightness_template(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Save (new without ``template_id``) or delete a global brightness template. Built-ins cannot change."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    lighting = ctrl.lighting
    templates = list(lighting.brightness_templates)
    template_id = msg.get("template_id")
    if template_id and any(t.id == template_id for t in BUILTIN_TEMPLATES):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Built-in templates cannot be changed")
        return
    if msg["op"] == "delete":
        if not any(t.id == template_id for t in templates):
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Template {template_id} does not exist")
            return
        templates = [t for t in templates if t.id != template_id]
        result: dict[str, Any] = {}
    else:
        current = next((t for t in templates if t.id == template_id), None)
        if template_id and current is None:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Template {template_id} does not exist")
            return
        if current is None and len(templates) >= MAX_TEMPLATES:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"At most {MAX_TEMPLATES} templates")
            return
        name = msg.get("name") or (current.name if current else "Template")
        taken = {t.id for t in (*BUILTIN_TEMPLATES, *templates)}
        template = Template(id=current.id if current else slug(name, taken, "template"), name=name,
                            room=cells_from(msg.get("room", current.room if current else {})),
                            zone=cells_from(msg.get("zone", current.zone if current else {})))
        templates = [template if t.id == template.id else t for t in templates] if current else [*templates, template]
        result = {"template": template.as_dict()}
    ctrl.store.async_set(replace(lighting, brightness_templates=tuple(templates)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, **result})


BRIGHTNESS_HANDLERS = (ws_ambient_set, ws_brightness_preview, ws_brightness_set, ws_brightness_remove,
                       ws_brightness_template)
