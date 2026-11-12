"""Websocket API of possible occupancy lighting (0.3.9): the editor model and its one write command.

Reads are part of the room editor model (``smart_occupancy/lighting/editor``,
key ``preliminary``); writes are admin-only and go to the lighting store (no
reload). Without ``area_id`` the command changes the global defaults; a room
value of ``null`` means "use the global default". Changing a global default
never changes a room's own values.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, LUX_MAX_THRESHOLD, LUX_MIN, TRANSITION_MAX, LightAction
from .controller import LightingController
from .lighting import light_capabilities
from .occupancy import friendly_name
from .presence import _KIND_WORDS
from .preliminary import (
    KELVIN_MAX,
    KELVIN_MIN,
    TIMEOUT_MAX,
    TIMEOUT_MIN,
    ConfirmAction,
    PreliminaryDefaults,
    PreliminaryTarget,
    RoomPreliminary,
    default_signals,
)


def _controller(hass: HomeAssistant, connection, msg) -> LightingController | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.lighting


def preliminary_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    """Everything the Automatic tab shows about possible occupancy lighting in one room."""
    lighting = ctrl.lighting
    room_lighting = lighting.room(area_id)
    settings = room_lighting.preliminary
    eff = ctrl.preliminary_settings(area_id)
    sensors = ctrl.config.room(area_id).sensors
    defaults = set(default_signals(sensors))
    managed = ctrl.managed_room(area_id)
    controlled = managed.controlled
    now_mood, _ = ctrl.room_mood(area_id)
    rt = ctrl.runtime.get(area_id)
    result = ctrl.coordinator.occupancy.result(area_id)

    def scene(entity_id: str) -> dict[str, Any] | None:
        """What the room's current mood does to the light: the occupied scene that takes precedence."""
        if not now_mood or entity_id not in controlled:
            return None
        r = ctrl.resolve(now_mood, area_id, entity_id, ctrl.index.profile_targets(entity_id))
        return {"mood": now_mood, "action": r.action.value, "brightness_pct": r.brightness_pct,
                "color_temp_kelvin": r.color_temp_kelvin, "switches_on": r.action == LightAction.ON}

    def light(entity_id: str) -> dict[str, Any]:
        state = hass.states.get(entity_id)
        return {"entity_id": entity_id, "name": friendly_name(hass, entity_id) or entity_id,
                "state": state.state if state else None,
                "capabilities": light_capabilities(hass, entity_id).as_dict(),
                "is_group": entity_id in managed.members or entity_id in managed.opaque_groups}

    targets = []
    for t in settings.targets:
        e = eff.target(t.entity_id)
        targets.append({**light(t.entity_id), "settings": t.as_dict(), "effective": e.as_dict() if e else None,
                        "managed": t.entity_id in controlled, "scene": scene(t.entity_id)})
    stage = None
    if rt is not None and rt.preliminary is not None:
        p = rt.preliminary
        stage = {"started": p.get("started"), "deadline": p.get("deadline"),
                 "evidence": [friendly_name(hass, x) or x for x in p.get("evidence") or []],
                 "lights": [friendly_name(hass, x) or x for x in (p.get("owned") or {})],
                 "would": [friendly_name(hass, x) or x for x in p.get("would") or []]}
    daylight = room_lighting.daylight
    return {
        "settings": settings.as_dict(),
        "global": lighting.preliminary.as_dict(),
        "effective": eff.as_dict(),
        "normal_on_below": daylight.on_below,
        "auto_on": daylight.auto_on.value,
        "on_first_motion": daylight.on_first_motion,
        "correlation_window": ctrl.config.timing_for(area_id).correlation_window,
        "sensors": [{"entity_id": s.entity_id, "name": friendly_name(hass, s.entity_id) or s.entity_id,
                     "kind": s.kind.value, "kind_name": _KIND_WORDS.get(s.kind, s.kind.value), "role": s.role.value,
                     "used": s.participates, "selected": s.entity_id in eff.signals,
                     "default": s.entity_id in defaults} for s in sensors],
        "candidates": [light(e) for e in controlled],
        "targets": targets,
        "stage": stage,
        "occupancy": result.state.value if result else None,
        "limits": {"timeout": [TIMEOUT_MIN, TIMEOUT_MAX], "kelvin": [KELVIN_MIN, KELVIN_MAX],
                   "lux": [LUX_MIN, LUX_MAX_THRESHOLD], "transition": [0, TRANSITION_MAX]},
    }


_NONE_OR = lambda schema: vol.Any(None, schema)  # noqa: E731
_TIMEOUT = vol.All(vol.Coerce(int), vol.Range(min=TIMEOUT_MIN, max=TIMEOUT_MAX))
_LUX = vol.All(vol.Coerce(int), vol.Range(min=LUX_MIN, max=LUX_MAX_THRESHOLD))
_BRIGHTNESS = vol.All(vol.Coerce(int), vol.Range(min=1, max=100))
_KELVIN = vol.All(vol.Coerce(int), vol.Range(min=KELVIN_MIN, max=KELVIN_MAX))
_TRANSITION = vol.All(vol.Coerce(float), vol.Range(min=0, max=TRANSITION_MAX))
_CONFIRM = vol.In([c.value for c in ConfirmAction])
_TARGET = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("brightness_pct"): _NONE_OR(_BRIGHTNESS),
    vol.Optional("color_temp_kelvin"): _NONE_OR(_KELVIN),
    vol.Optional("transition"): _NONE_OR(_TRANSITION),
    vol.Optional("on_confirm"): _NONE_OR(_CONFIRM),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/preliminary/set",
    vol.Optional("area_id"): str,
    vol.Optional("enabled"): vol.Any(None, bool),
    vol.Optional("timeout"): _NONE_OR(_TIMEOUT),
    vol.Optional("on_below"): _NONE_OR(_LUX),
    vol.Optional("signals"): vol.Any(None, [cv.entity_id]),
    vol.Optional("targets"): [_TARGET],
    # Global defaults only:
    vol.Optional("brightness_pct"): _BRIGHTNESS,
    vol.Optional("transition"): _TRANSITION,
    vol.Optional("on_confirm"): _CONFIRM,
})
@websocket_api.require_admin
@callback
def ws_preliminary_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Possible occupancy lighting: a room's settings (``area_id``) or the global defaults."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg.get("area_id")
    if area_id is None:
        _set_global(ctrl, connection, msg)
        return
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return
    for key in ("brightness_pct", "transition", "on_confirm"):
        if key in msg:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  f"{key} is set per preliminary light (or as a global default)")
            return
    current = ctrl.lighting.room(area_id).preliminary
    changes: dict[str, Any] = {}
    for key in ("enabled", "timeout", "on_below"):
        if key in msg:
            changes[key] = msg[key]
    if "signals" in msg:
        if msg["signals"] is None:
            changes["signals"] = None
        else:
            assigned = {s.entity_id for s in ctrl.config.room(area_id).sensors}
            unknown = [e for e in msg["signals"] if e not in assigned]
            if unknown:
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                      f"{', '.join(unknown)} is not a signal of this room")
                return
            changes["signals"] = tuple(dict.fromkeys(msg["signals"]))
    if "targets" in msg:
        controlled = set(ctrl.managed_room(area_id).controlled)
        targets: list[PreliminaryTarget] = []
        for item in msg["targets"]:
            entity_id = item["entity_id"]
            if entity_id.split(".", 1)[0] != "light":
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{entity_id} is not a light")
                return
            if entity_id not in controlled and current.target(entity_id) is None:
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                      f"{entity_id} is not a controlled light of this room")
                return
            target = PreliminaryTarget.from_dict(item)
            if target is not None and all(t.entity_id != entity_id for t in targets):
                targets.append(target)
        changes["targets"] = tuple(targets)
    ctrl.store.async_set(ctrl.with_room(area_id, preliminary=replace(current, **changes)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


def _set_global(ctrl: LightingController, connection, msg: dict) -> None:
    for key in ("signals", "targets"):
        if key in msg:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{key} is set per room")
            return
    for key in ("enabled", "timeout"):
        if key in msg and msg[key] is None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"The global {key} needs a value")
            return
    current = ctrl.lighting.preliminary.as_dict()
    for key in ("enabled", "timeout", "on_below", "brightness_pct", "transition", "on_confirm"):
        if key in msg:
            current[key] = msg[key]
    ctrl.store.async_set(replace(ctrl.lighting, preliminary=PreliminaryDefaults.from_dict(current)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


PRELIMINARY_HANDLERS = (ws_preliminary_set,)

__all__ = ["PRELIMINARY_HANDLERS", "RoomPreliminary", "preliminary_model"]
