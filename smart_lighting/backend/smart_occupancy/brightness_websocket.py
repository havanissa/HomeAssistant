"""Websocket API of ambient light levels and brightness rules (0.6.7).

Reads are part of the room editor model (``brightness_model``, key
``brightness`` of ``smart_occupancy/lighting/editor``) and
``brightness/preview`` (what enrolling a light, or a draft matrix, would mean
now). Writes are admin only and go to the lighting store (no reload).
Nothing here sends a light command: an enrolled light follows its matrix at
the next lighting trigger (occupancy, a zone, the ambient level, the mood).

0.6.8: ``ambient/levels`` previews and (``confirm``) saves a category change -
the global categories, a room's own categories, or *Use Global Levels* - with
the review of removed categories (``ambient_levels.py``). Matrices use the
room's effective categories; templates are mapped onto them.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from . import ambient_levels
from .ambient import (
    DEAD_BAND_MAX,
    DEFAULT_LEVELS,
    HYSTERESIS_MAX,
    MAX_LEVELS_LIMIT,
    MIN_INTERVAL_MAX,
    MIN_LEVELS,
    STABLE_MAX,
    THRESHOLD_MAX,
    TRANSITION_LIMIT,
    AmbientFallback,
    AmbientLevel,
    AmbientSettings,
    level_error,
    valid_thresholds,
)
from .discovery import area_name
from .fixtures import KIND_NAMES, onoff_only
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
from .targets import is_relay
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
    effective = lighting.ambient_for(area_id)
    levels = effective.levels or DEFAULT_LEVELS
    targets = {t.entity_id: t for t in ctrl._targets(area_id)}
    lights = []
    for entity_id in managed.controlled:
        matrix = room.brightness.get(entity_id)
        item = items.get(entity_id)
        rule = (item.rule or {}) if item else {}
        target = targets.get(entity_id)
        caps = target.capabilities if target else None
        fixture = room.fixtures.get(entity_id)
        lights.append({
            "entity_id": entity_id, "name": (target.name if target else None) or friendly_name(hass, entity_id)
            or entity_id,
            "type": "relay" if is_relay(entity_id) else "light",
            "capabilities": caps.as_dict() if caps else {},
            # On/Off matrix values (relays, on/off lights, Dumb Lights): 0 = Off, any other value = On.
            "onoff": bool(caps is not None and onoff_only(caps)),
            "fixture": ({**fixture.as_dict(), "text": fixture.describe()} if fixture else None),
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
    inheriting = sorted(a for a in ctrl.config.enabled_rooms if lighting.room(a).ambient.settings.levels is None)
    return {
        "ambient": {
            "enabled": room.ambient.enabled,
            "settings": settings.as_dict(),
            "global": lighting.ambient.as_dict(),
            "effective": effective.as_dict(),
            "sources": lighting.ambient.sources(settings),
            "status": status.as_dict(),
            "sensors": [r.as_dict() for r in daylight.readings],
            "history": ctrl.ambient.history(area_id)[-10:],
            "levels_source": "room" if settings.levels is not None else "global",
            "global_levels": ambient_levels.ranges(lighting.ambient.levels or DEFAULT_LEVELS),
            "global_rooms": [area_name(hass, a) or a for a in inheriting],
            "max_levels": lighting.ambient.max_levels,
        },
        "levels": [{"key": r["id"], **r} for r in ambient_levels.ranges(levels)],
        "lights": lights,
        "zones": [{"id": z.id, "name": z.name, "priority": z.priority, "active": z.id in active} for z in room.zones],
        # (Mapped onto the room's own categories.)
        "templates": [t.for_levels(levels).as_dict() for t in (*BUILTIN_TEMPLATES, *lighting.brightness_templates)],
        "default_template": DEFAULT_TEMPLATE,
        "limits": {"threshold": [1, THRESHOLD_MAX], "hysteresis": [0, HYSTERESIS_MAX], "stable": [0, STABLE_MAX],
                   "transition": [0, TRANSITION_LIMIT], "dead_band": [0, DEAD_BAND_MAX],
                   "min_interval": [0, MIN_INTERVAL_MAX], "max_templates": MAX_TEMPLATES,
                   "levels": [MIN_LEVELS, lighting.ambient.max_levels or MAX_LEVELS_LIMIT],
                   "max_levels": MAX_LEVELS_LIMIT},
        "kinds": [{"id": k.value, "name": n} for k, n in KIND_NAMES.items()],
    }


# ------------------------------------------------------------------- schemas
_NONE_OR = lambda schema: vol.Any(None, schema)  # noqa: E731
_PCT = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100)))
_CELLS = vol.Schema({vol.All(str, vol.Match(r"^[a-z0-9_]{1,40}$")): _PCT})
_TUNING = {
    vol.Optional("thresholds"): _NONE_OR(vol.All([vol.All(vol.Coerce(int), vol.Range(min=1, max=THRESHOLD_MAX))],
                                                 vol.Length(min=MIN_LEVELS - 1, max=MAX_LEVELS_LIMIT - 1))),
    vol.Optional("hysteresis"): _NONE_OR(vol.All(vol.Coerce(float), vol.Range(min=0, max=HYSTERESIS_MAX))),
    vol.Optional("stable_seconds"): _NONE_OR(vol.All(vol.Coerce(int), vol.Range(min=0, max=STABLE_MAX))),
    vol.Optional("fallback"): _NONE_OR(vol.In([f.value for f in AmbientFallback])),
    vol.Optional("fallback_level"): _NONE_OR(vol.All(str, vol.Match(r"^[a-z0-9_]{1,40}$"))),
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
    keys = [k for k in ("hysteresis", "stable_seconds", "fallback", "fallback_level", "transition",
                        "dead_band", "min_interval") if k in msg]
    area_id = msg.get("area_id")
    if area_id is not None and _room(ctrl, connection, msg) is None:
        return
    lighting = ctrl.lighting
    effective = lighting.ambient_for(area_id) if area_id is not None else lighting.ambient
    target = effective.levels or DEFAULT_LEVELS
    if area_id is None:
        target = lighting.ambient.levels or DEFAULT_LEVELS
    levels = None
    if "thresholds" in msg and msg["thresholds"] is not None:
        # The boundaries of the current categories (their ids and names are kept).
        thresholds = valid_thresholds(msg["thresholds"], len(target))
        if thresholds is None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  "Each threshold must be higher than the one before (e.g. 5 and 20 lx), "
                                  f"one for each of the {len(target) - 1} boundaries")
            return
        levels = tuple(replace(lv, start=0 if i == 0 else thresholds[i - 1]) for i, lv in enumerate(target))
    if msg.get("fallback_level") is not None and msg["fallback_level"] not in {lv.id for lv in target}:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{msg['fallback_level']} is not a category")
        return
    if area_id is None:
        if "enabled" in msg:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Ambient control is switched per room")
            return
        if any(msg[k] is None for k in keys) or ("thresholds" in msg and msg["thresholds"] is None):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A global default needs a value")
            return
        settings = replace(lighting.ambient, **{k: getattr(AmbientSettings.from_dict({k: msg[k]}), k)
                                                 for k in keys})
        if levels is not None:
            settings = replace(settings, levels=levels)
        ctrl.store.async_set(replace(lighting, ambient=settings))
    else:
        room = lighting.room(area_id)
        own = room.ambient.settings
        if "thresholds" in msg and msg["thresholds"] is None:
            glob = lighting.ambient.levels or DEFAULT_LEVELS
            if own.levels is not None and [lv.id for lv in own.levels] != [lv.id for lv in glob]:
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                      "This room has its own categories: use Use Global Levels (ambient/levels)")
                return
            own = replace(own, levels=None)
        elif levels is not None:
            own = replace(own, levels=levels)
        settings = replace(own, **{k: getattr(AmbientSettings.from_dict({k: msg[k]}), k) for k in keys})
        ambient = replace(room.ambient, settings=settings, enabled=msg.get("enabled", room.ambient.enabled))
        ctrl.store.async_set(ctrl.with_room(area_id, ambient=ambient))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


_LEVEL = vol.Schema({
    vol.Optional("id"): vol.Any(None, vol.All(str, vol.Match(r"^[a-z0-9_]{1,40}$"))),
    vol.Required("name"): str,
    vol.Required("start"): vol.Any(int, float),
})


def _assign_ids(items: list[dict[str, Any]], taken: set[str]) -> list[dict[str, Any]]:
    """New categories get a stable id from their name (never one that exists or was just removed)."""
    out = []
    used = set(taken) | {i["id"] for i in items if i.get("id")}
    for item in items:
        if item.get("id"):
            out.append({"id": item["id"], "name": item["name"], "start": item["start"]})
            continue
        new_id = slug(item["name"] or "level", used, "level")
        used.add(new_id)
        out.append({"id": new_id, "name": item["name"], "start": item["start"]})
    return out


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/ambient/levels",
    vol.Optional("area_id"): vol.Any(None, str),
    vol.Optional("levels"): vol.Any(None, vol.All([_LEVEL], vol.Length(max=MAX_LEVELS_LIMIT))),
    vol.Optional("resolve"): {str: vol.Any(None, str)},
    vol.Optional("max_levels"): vol.All(vol.Coerce(int), vol.Range(min=MIN_LEVELS, max=MAX_LEVELS_LIMIT)),
    vol.Optional("confirm"): bool,
})
@websocket_api.require_admin
@callback
def ws_ambient_levels(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Preview (default) or save (``confirm``) a category change: the global categories (no ``area_id``), a
    room's own categories, or Use Global Levels (a room with ``levels`` null). Removed categories that have
    values need a ``resolve`` entry first (move to another category, or null: delete). Never sends a command."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg.get("area_id")
    if area_id is not None and _room(ctrl, connection, msg) is None:
        return
    lighting = ctrl.lighting
    if "max_levels" in msg:
        if area_id is not None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The maximum is a global setting")
            return
        in_use = max((len(lighting.ambient_for(a).levels or DEFAULT_LEVELS) for a in {*ctrl.config.enabled_rooms,
                                                                                      *lighting.rooms}), default=0)
        if msg["max_levels"] < max(in_use, len(lighting.ambient.levels or DEFAULT_LEVELS)):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  f"A room already uses {max(in_use, len(lighting.ambient.levels or ()))} categories")
            return
        if "levels" not in msg:
            ctrl.store.async_set(replace(lighting, ambient=replace(lighting.ambient, max_levels=msg["max_levels"])))
            connection.send_result(msg["id"], {"revision": ctrl.store.revision, "saved": True})
            return
    if "levels" not in msg:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "levels is required")
        return
    raw = msg["levels"]
    if raw is None and area_id is None:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "The global categories need a definition")
        return
    levels: tuple[AmbientLevel, ...] | None = None
    if raw is not None:
        current = lighting.ambient_for(area_id).levels if area_id else lighting.ambient.levels
        # (A new category never reuses the id of an existing or just removed one.)
        items = _assign_ids([dict(i) for i in raw], {lv.id for lv in current or DEFAULT_LEVELS})
        maximum = msg.get("max_levels") or lighting.ambient.max_levels or MAX_LEVELS_LIMIT
        if (error := level_error(items, max_levels=maximum)) is not None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, error)
            return
        levels = tuple(AmbientLevel(i["id"], i["name"].strip(), int(i["start"])) for i in items)
    change = ambient_levels.plan(lighting, ctrl.config.enabled_rooms, area_id, levels, msg.get("resolve"))
    names = lambda a: area_name(hass, a) or a  # noqa: E731
    lights = lambda e: friendly_name(hass, e) or e  # noqa: E731
    preview = change.as_dict(names, lights)
    if not msg.get("confirm"):
        connection.send_result(msg["id"], {"preview": preview, "saved": False})
        return
    if not change.ready:
        connection.send_error(msg["id"], "review_required",
                              "; ".join(change.errors) or "Choose what happens to the values of "
                              + ", ".join(dict.fromkeys(n for r in preview["rooms"] for n in r["unresolved"]))
                              + " first")
        return
    new = ambient_levels.apply(lighting, change)
    if "max_levels" in msg:
        new = replace(new, ambient=replace(new.ambient, max_levels=msg["max_levels"]))
    ctrl.store.async_set(new)
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "saved": True, "preview": preview,
                                       "sent": False})


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
    keys = list(ctrl.lighting.ambient_for(area_id).keys)
    given = {k for cells in (data.get("room") or {}, *(data.get("zones") or {}).values()) for k in cells}
    stored = {k for cells in current.columns().values() for k in cells}
    if bad := sorted(given - set(keys) - stored):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{', '.join(bad)}: not an ambient category of this room")
        return None

    def cells(old: dict[str, int | None], new: dict[str, Any]) -> dict[str, int | None]:
        # Every category of the room (Not configured where unset); values of removed categories are kept
        # until the reviewed removal deals with them.
        merged = {**old, **new}
        return {**cells_from(merged, keys), **{k: v for k, v in cells_from(merged).items() if k not in keys}}

    room_cells = cells(current.room, data["room"]) if "room" in data else cells(current.room, {})
    columns = {z: cells(c, {}) for z, c in current.zones.items()}
    for zone_id, new in data.get("zones", {}).items():
        columns[zone_id] = cells(columns.get(zone_id, {}), new)
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
        matrix = template.matrix(entity_id, [z.id for z in room.zones if z.target(entity_id)],
                                 levels=ctrl.lighting.ambient_for(area_id).levels)
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
    vol.Optional("area_id"): vol.Any(None, str),
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
        # (A template keeps the categories it was made with: the room's, else the global ones.)
        area = msg.get("area_id")
        levels = (lighting.ambient_for(area).levels if area in ctrl.config.rooms else None) or (
            current.levels if current and "room" not in msg else lighting.ambient.levels) or DEFAULT_LEVELS
        keys = [lv.id for lv in levels]
        template = Template(id=current.id if current else slug(name, taken, "template"), name=name,
                            room=cells_from(msg.get("room", current.room if current else {}), keys),
                            zone=cells_from(msg.get("zone", current.zone if current else {}), keys), levels=levels)
        templates = [template if t.id == template.id else t for t in templates] if current else [*templates, template]
        result = {"template": template.as_dict()}
    ctrl.store.async_set(replace(lighting, brightness_templates=tuple(templates)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, **result})


BRIGHTNESS_HANDLERS = (ws_ambient_set, ws_ambient_levels, ws_brightness_preview, ws_brightness_set,
                       ws_brightness_remove, ws_brightness_template)
