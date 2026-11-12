"""Presence tab (0.3.8): a room's occupancy inputs, their live states, and its presence settings.

Read side (``presence_model``): every signal assigned to the room, with its
friendly name, entity id, kind and role, the Home Assistant state, a display
label for that state (by domain and device class: "Detected" / "Clear",
"Open" / "Closed", ...), the value the occupancy engine uses, what the signal
contributes right now, its availability and its last change. The display
label is for people only: the engine works on the raw state (``on`` /
``off`` / ``unknown`` / ``unavailable`` for binary sensors), never on a label.

Settings (admin only, websocket):

* ``smart_occupancy/lighting/presence/sensor`` - include or exclude a signal
  from this room's occupancy. Excluding keeps the assignment (kind, role,
  inverted) and leaves the entity, other rooms and other integrations alone.
* ``smart_occupancy/lighting/presence/timing`` - the existing occupancy timing:
  global defaults (``area_id`` null) or per-room overrides (a value) and
  "use global default" (null), or ``reset_all`` for a room.

Both are the existing configuration (config entry options: ``rooms.*.sensors``,
``occupancy`` and ``rooms.*.timing``); there is no second settings store.
They are applied at once without a reload (``coordinator.async_apply_presence``):
the engines keep every signal's value and re-evaluate with the new settings.
Rooms that inherit a global value follow it; room overrides stay unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback

from .config_flow import MIN_STUCK_SECONDS, validate_timing
from .const import DOMAIN, TIMING_PARAMETERS, SensorKind, SensorRole
from .engine import SignalStatus, Value, interpret
from .models import IntegrationConfig, OccupancyTiming
from .occupancy import AVAILABLE, PENDING, entity_health, friendly_name

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

# Display labels of binary sensor states by device class: (on, off). Display only.
_BINARY_LABELS: dict[str | None, tuple[str, str]] = {
    "motion": ("Detected", "Clear"), "occupancy": ("Detected", "Clear"), "presence": ("Home", "Away"),
    "moving": ("Moving", "Not moving"), "sound": ("Detected", "Clear"), "vibration": ("Detected", "Clear"),
    "door": ("Open", "Closed"), "garage_door": ("Open", "Closed"), "opening": ("Open", "Closed"),
    "window": ("Open", "Closed"), "running": ("Running", "Not running"), "plug": ("Plugged in", "Unplugged"),
    "power": ("On", "Off"), "connectivity": ("Connected", "Disconnected"), "light": ("Light", "No light"),
    "lock": ("Unlocked", "Locked"), None: ("On", "Off"),
}
_KIND_WORDS = {
    SensorKind.PIR: "PIR (motion)", SensorKind.RADAR: "Radar (presence)", SensorKind.DOOR: "Door contact",
    SensorKind.PC_ACTIVITY: "PC activity", SensorKind.BED: "Bed sensor", SensorKind.OTHER: "Other",
}
_ROLE_WORDS = {
    SensorRole.TRIGGER: "Trigger: starts occupancy", SensorRole.MAINTAIN: "Maintain: holds occupancy",
    SensorRole.CONFIRM_VACANCY: "Confirm vacancy: holds occupancy; its clear speeds up vacancy",
    SensorRole.IGNORE: "Ignore: not used",
}
_DOOR_ROLE = "Context: door movements count as activity and exit evidence, never as presence"
# Presence settings shown in the Presence tab (the engine's timing parameters), most used first.
TIMING_INFO: dict[str, tuple[str, str]] = {
    "inactivity_timeout": ("Vacancy timeout",
                           "Quiet time before the room is vacant (and its vacancy action runs), without exit evidence."),
    "exit_confirmation": ("Fast vacancy timeout",
                          "Quiet time before vacancy when there is exit evidence: a Confirm-vacancy sensor cleared, "
                          "a door exit, or the visit was never confirmed."),
    "correlation_window": ("Occupancy confirmation",
                           "Two signals within this time, or one presence signal active this long, confirm "
                           "occupancy. Door movements and motion are correlated within it."),
    "dropout_grace": ("Radar dropout bridge",
                      "Short clears of radar, PC and other Maintain / Confirm-vacancy signals are bridged."),
    "trigger_stuck_after": ("PIR stuck after", "A Trigger (PIR) active longer than this is ignored as stuck. 0 = off."),
    "presence_stuck_after": ("Presence stuck after",
                             "The same for Maintain / Confirm-vacancy signals. 0 = off (people can sit still for hours)."),
    "startup_hold": ("Start-up hold", "After a restart or reload, an occupied room stays occupied at least this long."),
}
BASIC_TIMING = ("inactivity_timeout", "exit_confirmation", "correlation_window", "dropout_grace")


def display_state(hass: HomeAssistant, entity_id: str) -> str:
    """A person's word for an entity's state ("Detected", "Closed", ...). Never used for decisions."""
    state = hass.states.get(entity_id)
    if state is None:
        return "Missing"
    if state.state == STATE_UNAVAILABLE:
        return "Unavailable"
    if state.state == STATE_UNKNOWN:
        return "Unknown"
    if state.domain == "binary_sensor":
        on, off = _BINARY_LABELS.get(state.attributes.get("device_class"), _BINARY_LABELS[None])
        return on if state.state == "on" else off if state.state == "off" else state.state
    unit = state.attributes.get("unit_of_measurement")
    text = f"{state.state} {unit}" if unit else state.state
    return text[:1].upper() + text[1:]


def _condition(enabled: bool, role: SensorRole, health: str, value: Value, status: SignalStatus | None) -> str:
    """active | clear | disabled | ignored | unavailable | invalid | stuck (see the card's legend)."""
    if not enabled:
        return "disabled"
    if role == SensorRole.IGNORE:
        return "ignored"
    if health not in (AVAILABLE, PENDING):
        return "unavailable"
    if value == Value.UNKNOWN:
        return "unavailable" if health == PENDING else "invalid"
    if status == SignalStatus.STUCK:
        return "stuck"
    if status in (SignalStatus.ACTIVE, SignalStatus.GRACE, SignalStatus.RECENT) or (
            status is None and value == Value.ACTIVE):
        return "active"
    return "clear"


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def timing_rows(config: IntegrationConfig, area_id: str | None) -> list[dict[str, Any]]:
    """Each presence setting: the effective value, the global default and whether the room overrides it."""
    glob = config.occupancy
    room = config.room(area_id).timing if area_id else {}
    rows = []
    for key, (label, help_text) in TIMING_INFO.items():
        default, low, high = TIMING_PARAMETERS[key]
        rows.append({
            "key": key, "label": label, "help": help_text, "unit": "s", "min": low, "max": high,
            "default": default, "global": getattr(glob, key),
            "value": room.get(key, getattr(glob, key)), "overridden": key in room, "basic": key in BASIC_TIMING,
        })
    return rows


def presence_model(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator, area_id: str) -> dict[str, Any]:
    config = coordinator.config
    room = config.room(area_id)
    result = coordinator.occupancy.result(area_id)
    views = {v.track.entity_id: v for v in result.signals} if result else {}
    sensors: list[dict[str, Any]] = []
    for s in room.sensors:
        state = hass.states.get(s.entity_id)
        health = entity_health(hass, s.entity_id, settled=coordinator.settled)
        value = interpret(state.state if state else None, inverted=s.inverted)
        view = views.get(s.entity_id)
        status = view.status if view and view.status != SignalStatus.IGNORED else None
        sensors.append({
            "entity_id": s.entity_id,
            "name": friendly_name(hass, s.entity_id) or s.entity_id,
            "domain": s.entity_id.split(".", 1)[0],
            "device_class": state.attributes.get("device_class") if state else None,
            "kind": s.kind.value, "kind_name": _KIND_WORDS.get(s.kind, s.kind.value),
            "role": s.role.value,
            "role_name": _DOOR_ROLE if s.kind == SensorKind.DOOR and s.role != SensorRole.IGNORE
            else _ROLE_WORDS.get(s.role, s.role.value),
            "inverted": s.inverted,
            "enabled": s.enabled,
            "state": state.state if state else None,
            "label": display_state(hass, s.entity_id) + (" (inverted: counts as active when off)" if s.inverted else ""),
            "value": value.value,
            "status": status.value if status else None,
            "health": health,
            "available": health in (AVAILABLE, PENDING) and value != Value.UNKNOWN,
            "condition": _condition(s.enabled, s.role, health, value, status),
            "last_changed": _iso(state.last_changed) if state else None,
        })
    return {
        "area_id": area_id,
        "sensors": sensors,
        "occupancy": {
            "state": result.state.value if result else None,
            "occupied": result.occupied if result else None,
            "reason": result.reason if result else None,
            "vacancy_at": _iso(result.vacancy_at) if result else None,
            "required_quiet": result.required_quiet if result else None,
        },
        "timing": timing_rows(config, area_id),
        "min_stuck_seconds": MIN_STUCK_SECONDS,
    }


# ---------------------------------------------------------------- commands
def _coordinator(hass: HomeAssistant, connection, msg) -> SmartOccupancyCoordinator | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data


def _apply(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator, config: IntegrationConfig) -> None:
    if not coordinator.async_apply_presence(config):
        # Not a presence-only change (should not happen from these commands): the integration reloads.
        hass.config_entries.async_update_entry(coordinator.config_entry, options=config.as_dict())


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/presence/sensor",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): str,
    vol.Required("enabled"): bool,
})
@websocket_api.require_admin
@callback
def ws_presence_sensor(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Include or exclude one assigned signal from this room's occupancy decisions."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    config = coordinator.config
    area_id, entity_id = msg["area_id"], msg["entity_id"]
    if area_id not in config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
        return
    room = config.room(area_id)
    if not any(s.entity_id == entity_id for s in room.sensors):
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{entity_id} is not assigned to this room")
        return
    sensors = tuple(replace(s, enabled=msg["enabled"]) if s.entity_id == entity_id else s for s in room.sensors)
    _apply(hass, coordinator, config.with_room(replace(room, sensors=sensors)))
    connection.send_result(msg["id"], {"entity_id": entity_id, "enabled": msg["enabled"]})


_VALUE = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0)))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/presence/timing",
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Optional("values", default={}): {vol.In(list(TIMING_PARAMETERS)): _VALUE},
    vol.Optional("reset_all", default=False): bool,
})
@websocket_api.require_admin
@callback
def ws_presence_timing(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Global presence defaults (no area) or one room's overrides; None means "use the default"."""
    if (coordinator := _coordinator(hass, connection, msg)) is None:
        return
    config = coordinator.config
    area_id = msg.get("area_id")
    values: dict[str, int | None] = msg["values"]
    for key, value in values.items():
        _default, low, high = TIMING_PARAMETERS[key]
        if value is not None and not low <= value <= high:
            label = TIMING_INFO[key][0]
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  f"{label} must be from {low} to {high} seconds")
            return
    if errors := validate_timing({k: v for k, v in values.items() if v is not None}):
        key = next(iter(errors))
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{TIMING_INFO[key][0]} must be 0 (off) or at least {MIN_STUCK_SECONDS} seconds")
        return
    if area_id is None:
        if msg["reset_all"]:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "reset_all needs a room")
            return
        current = config.occupancy.as_dict()
        for key, value in values.items():
            current[key] = TIMING_PARAMETERS[key][0] if value is None else value
        new = replace(config, occupancy=OccupancyTiming.from_dict(current))
    else:
        if area_id not in config.enabled_rooms:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
            return
        room = config.room(area_id)
        timing = {} if msg["reset_all"] else dict(room.timing)
        for key, value in values.items():
            if value is None:
                timing.pop(key, None)
            else:
                timing[key] = value
        new = config.with_room(replace(room, timing=timing))
    _apply(hass, coordinator, new)
    connection.send_result(msg["id"], {"timing": timing_rows(coordinator.config, area_id)})


PRESENCE_HANDLERS = (ws_presence_sensor, ws_presence_timing)
