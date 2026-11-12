"""Websocket API of the Switches tab (Phase 0.3.5).

Reading the switch model is part of ``smart_occupancy/lighting/editor``
(``room.switches``). Every command here is admin only and validated before
anything is saved. No command sends a light command or publishes MQTT:

* ``switch/candidates`` - devices that look like switches or remotes;
* ``switch/add`` / ``switch/remove`` - assign or remove a switch (starts Observing);
* ``switch/set`` - name, input source, MQTT topic (Advanced), dimming, debounce,
  activity evidence, manual override time;
* ``switch/mapping`` - assign (or clear) one button's action, or forget a button;
* ``switch/mode`` - Observing (``listen``) / Active (``control``; explicit confirmation;
  refused while the switch is directly bound to lights, or while an enabled
  automation that reacts to it is not acknowledged);
* ``switch/learn`` - start or cancel learning mode;
* ``switch/settings`` - the Zigbee2MQTT base topic.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    DEFAULT_LEARN_SECONDS,
    DIM_INTERVAL_MAX,
    DIM_INTERVAL_MIN,
    DIM_TIMEOUT_MAX,
    DOMAIN,
    KELVIN_MAX,
    KELVIN_MIN,
    MANUAL_OVERRIDE_MAX_MINUTES,
    MAX_DEBOUNCE_MS,
    MAX_LEARN_SECONDS,
    MAX_SWITCHES_PER_ROOM,
    ButtonAction,
    OnValues,
    SwitchMode,
    SwitchSource,
)
from .controller import LightingController
from .discovery import area_name
from .occupancy import friendly_name
from .switch_discovery import (
    async_device_triggers,
    candidates,
    competing_automations,
    device_summary,
    event_entities,
    ieee_of,
    light_bindings,
)
from .switch_inputs import SwitchStatus
from .switches import (
    ACTION_WORDS,
    IGNORED_WORDS,
    ROOM_TARGET,
    ButtonMapping,
    SwitchConfig,
    SwitchSettings,
    action_label,
    valid_action,
    validate_base_topic,
    validate_topic,
)

TOPIC_ERRORS = {
    "topic_empty": "Enter an MQTT topic",
    "topic_wildcard": "Wildcards (+ and #) are not allowed: enter the exact topic of one device",
    "topic_invalid": "Not a valid device topic (for example zigbee2mqtt/avb_ksu)",
    "topic_reserved": "Bridge and discovery topics cannot be used for a switch",
}
SOURCE_WORDS = {
    SwitchSource.AUTO.value: "Automatic",
    SwitchSource.ZIGBEE2MQTT.value: "Zigbee2MQTT (MQTT)",
    SwitchSource.DEVICE_TRIGGER.value: "MQTT device triggers",
    SwitchSource.EVENT_ENTITY.value: "Event entity",
}


def _ctrl(hass: HomeAssistant, connection, msg) -> LightingController | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.lighting


def _room(ctrl: LightingController, connection, msg) -> bool:
    if msg["area_id"] not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {msg['area_id']} is not configured")
        return False
    return True


def _switch(ctrl: LightingController, connection, msg) -> SwitchConfig | None:
    if not _room(ctrl, connection, msg):
        return None
    switch = ctrl.lighting.room(msg["area_id"]).switch(msg["switch_id"])
    if switch is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Switch not found")
    return switch


def _save(ctrl: LightingController, area_id: str, switch: SwitchConfig) -> None:
    ctrl.store.async_set(ctrl.lighting.with_room(area_id, ctrl.lighting.room(area_id).with_switch(switch)))


# ------------------------------------------------------------------ model
def _competing(hass: HomeAssistant, ctrl: LightingController, switch: SwitchConfig) -> list[dict[str, Any]]:
    inputs = ctrl.switch_inputs
    entity = inputs.event_entity_for(switch)
    return [a.as_dict() for a in competing_automations(
        hass, device_id=switch.device_id, entities=[entity] if entity else [], topics=[inputs.topic_for(switch) or ""])]


def declared_actions(hass: HomeAssistant, ctrl: LightingController, switch: SwitchConfig) -> list[str]:
    """Button actions the device itself declares (Zigbee2MQTT definition, device triggers, event types)."""
    inputs = ctrl.switch_inputs
    z2m = inputs.bridge_devices.get(switch.ieee) if switch.ieee else None
    out = list(z2m.actions) if z2m else []
    out += [t["subtype"] for t in inputs.triggers_for(switch)]
    entity = inputs.event_entity_for(switch)
    state = hass.states.get(entity) if entity else None
    out += [a for a in (state.attributes.get("event_types") or []) if valid_action(a)] if state else []
    return list(dict.fromkeys(out))


def switch_model(hass: HomeAssistant, ctrl: LightingController, area_id: str, switch: SwitchConfig) -> dict[str, Any]:
    inputs = ctrl.switch_inputs
    status = inputs.status.get((area_id, switch.id)) or SwitchStatus()
    device = device_summary(hass, switch.device_id)
    z2m = inputs.bridge_devices.get(switch.ieee) if switch.ieee else None
    bindings = light_bindings(z2m, inputs.coordinator)
    competing = _competing(hass, ctrl, switch)
    open_competing = [a for a in competing if a["enabled"] and a["entity_id"] not in switch.acknowledged]
    declared = declared_actions(hass, ctrl, switch)
    buttons = []
    for raw in dict.fromkeys([*switch.buttons, *switch.mappings]):
        learned = switch.buttons.get(raw)
        mapping = switch.mappings.get(raw)
        buttons.append({
            "raw": raw, "label": action_label(raw), "button": learned.button if learned else None,
            "press": learned.press if learned else None, "source": learned.source if learned else None,
            "learned_at": learned.learned_at if learned else None,
            "mapping": mapping.as_dict() if mapping else None,
            "describe": mapping.describe() if mapping else ACTION_WORDS[ButtonAction.NONE],
        })
    warnings: list[dict[str, str]] = []
    if bindings:
        warnings.append({"code": "binding", "text": (
            f"Directly bound in Zigbee2MQTT ({', '.join(b['cluster'] + ' → ' + b['target'] for b in bindings)}): the "
            "switch controls those lights itself. Keep it Observing, or remove the binding in Zigbee2MQTT first.")})
    for a in open_competing:
        warnings.append({"code": "competing_automation", "text": (
            f"Automation “{a['name'] or a['entity_id']}” also reacts to this switch ({a['via']}"
            + (f": {', '.join(a['buttons'])}" if a["buttons"] else "") + "). Disable it or change it before "
            "setting the switch to Active, or acknowledge that both may act.")})
    if status.problem:
        warnings.append({"code": "input", "text": status.problem})
    if not device.get("exists") and switch.device_id:
        warnings.append({"code": "device_missing", "text": "The Home Assistant device was removed"})
    session = inputs.learning.get((area_id, switch.id))
    return {
        **switch.as_dict(),
        "status": status.as_dict(),
        "device": device,
        "manufacturer": device.get("manufacturer") or (z2m.vendor if z2m else None),
        "model": device.get("model") or (z2m.model if z2m else None),
        "description": z2m.description if z2m else None,
        "connection": SOURCE_WORDS.get(status.authoritative or "", "Not connected"),
        "topic_now": inputs.topic_for(switch),
        "event_entity_now": inputs.event_entity_for(switch),
        "buttons_list": buttons,
        "declared": [a for a in declared if a not in switch.buttons],
        "bindings": bindings,
        "competing": competing,
        "warnings": warnings,
        "operating": operating_state(ctrl, area_id, switch, status, buttons),
        "can_control": not bindings,
        "learning": session.as_dict() if session else None,
        "dimming_now": ctrl.switch_control.dimming(area_id, switch.id),
    }


def operating_state(ctrl: LightingController, area_id: str, switch: SwitchConfig, status: SwitchStatus,
                    buttons: list[dict[str, Any]]) -> dict[str, Any]:
    """What the switch really does with a press now: never "active" while its actions are suppressed.

    ``mode`` is the chosen mode (observe / active); ``state`` is the effective
    one: observing, active, blocked (Active, but every action is suppressed)
    or attention (Active, but presses cannot arrive or nothing is assigned).
    The room's automatic lighting mode never blocks a switch (0.3.6).
    """
    if not switch.controls:
        return {"mode": "observe", "state": "observing", "blocked_by": None,
                "text": "Observing: presses are registered and logged; no action is executed."}
    if area_id not in ctrl.config.enabled_rooms:
        return {"mode": "active", "state": "blocked", "blocked_by": "room_disabled",
                "text": "Blocked: the room is not enabled in Smart Occupancy (integration options), so no "
                        "action is executed."}
    if status.problem:
        return {"mode": "active", "state": "attention", "blocked_by": "input",
                "text": f"Active, but presses are not received: {status.problem}"}
    if not any(b["mapping"] and b["mapping"]["action"] != ButtonAction.NONE.value for b in buttons):
        return {"mode": "active", "state": "attention", "blocked_by": None,
                "text": "Active, but no button has an action assigned yet (Configure)."}
    room_auto = ctrl.room_active(area_id)
    return {"mode": "active", "state": "active", "blocked_by": None,
            "text": "Active: assigned button actions are executed"
                    + ("." if room_auto else " (independent of automatic lighting, which is Observing).")}


def _wled_presets(hass: HomeAssistant) -> list[dict[str, Any]]:
    ent_reg = er.async_get(hass)
    out = []
    for entry in ent_reg.entities.values():
        if entry.platform != "wled" or entry.domain != "select" or entry.disabled_by:
            continue
        if entry.translation_key != "preset" and not entry.entity_id.endswith("_preset"):
            continue
        state = hass.states.get(entry.entity_id)
        out.append({"entity_id": entry.entity_id, "name": friendly_name(hass, entry.entity_id),
                    "options": list(state.attributes.get("options") or []) if state else []})
    return out


def switches_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    room = ctrl.lighting.room(area_id)
    managed = ctrl.managed_room(area_id)
    helper = ctrl.config.mood.manual_mood_entity
    helper_state = hass.states.get(helper) if helper else None
    return {
        "items": [switch_model(hass, ctrl, area_id, s) for s in room.switches],
        "mqtt": ctrl.switch_inputs.mqtt_state,
        "mqtt_configured": ctrl.switch_inputs.mqtt_configured,
        "settings": ctrl.lighting.switch_settings.as_dict(),
        "legacy_entities": list(room.manual_switches),
        "actions": [{"value": a.value, "label": w} for a, w in ACTION_WORDS.items()],
        "ignored_words": IGNORED_WORDS,
        "targets": [{"entity_id": e, "name": friendly_name(hass, e), "group": e in managed.members or
                     e in managed.opaque_groups} for e in managed.controlled],
        "scenes": sorted(({"entity_id": s.entity_id, "name": s.name} for s in hass.states.async_all("scene")),
                         key=lambda x: x["name"].casefold()),
        "scripts": sorted(({"entity_id": s.entity_id, "name": s.name} for s in hass.states.async_all("script")),
                          key=lambda x: x["name"].casefold()),
        "wled_presets": _wled_presets(hass),
        "global_mood_helper": helper,
        "global_mood_options": [o for o in (helper_state.attributes.get("options") or [])] if helper_state else [],
    }


# --------------------------------------------------------------- commands
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/candidates",
    vol.Required("area_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_candidates(hass: HomeAssistant, connection, msg) -> None:
    """Devices that look like switches or remotes. Read only (also starts reading Zigbee2MQTT's device list)."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or not _room(ctrl, connection, msg):
        return
    ctrl.switch_inputs.async_request_bridge()
    mqtt_devices = [d.id for entry in hass.config_entries.async_entries("mqtt")
                    for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)]
    triggers = await async_device_triggers(hass, mqtt_devices)
    connection.send_result(msg["id"], {
        "candidates": candidates(hass, ctrl, triggers),
        "mqtt": ctrl.switch_inputs.mqtt_state,
        "mqtt_configured": ctrl.switch_inputs.mqtt_configured,
        "bridge_seen_at": ctrl.switch_inputs.bridge_seen_at,
        "base_topic": ctrl.lighting.switch_settings.mqtt_base_topic,
    })


def _already_assigned(ctrl: LightingController, device_id: str | None, ieee: str | None,
                      topic: str | None) -> tuple[str, SwitchConfig] | None:
    for area_id, room in ctrl.lighting.rooms.items():
        for s in room.switches:
            if (device_id and s.device_id == device_id) or (ieee and s.ieee == ieee) or (topic and s.topic == topic):
                return area_id, s
    return None


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/add",
    vol.Required("area_id"): str,
    vol.Optional("device_id"): vol.Any(None, vol.All(str, vol.Length(min=1, max=64))),
    vol.Optional("ieee"): vol.Any(None, vol.All(str, vol.Match(r"^0x[0-9a-fA-F]{16}$"))),
    vol.Optional("topic"): vol.Any(None, vol.All(str, vol.Length(max=256))),
    vol.Optional("event_entity"): vol.Any(None, cv.entity_id),
    vol.Optional("name"): vol.Any(None, vol.All(str, vol.Length(min=1, max=100))),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection, msg) -> None:
    """Assign a switch to a room (Observing). Its declared buttons are added; nothing is controlled."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or not _room(ctrl, connection, msg):
        return
    area_id = msg["area_id"]
    room = ctrl.lighting.room(area_id)
    if len(room.switches) >= MAX_SWITCHES_PER_ROOM:
        connection.send_error(msg["id"], "too_many", f"A room can have at most {MAX_SWITCHES_PER_ROOM} switches")
        return
    device_id, ieee = msg.get("device_id") or None, (msg.get("ieee") or "").lower() or None
    topic = (msg.get("topic") or "").strip() or None
    event_entity = msg.get("event_entity") or None
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    if device_id and device is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Device not found")
        return
    if topic and (error := validate_topic(topic)):
        connection.send_error(msg["id"], error, TOPIC_ERRORS[error])
        return
    if event_entity and (not event_entity.startswith("event.") or (
            hass.states.get(event_entity) is None and er.async_get(hass).async_get(event_entity) is None)):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{event_entity} is not an event entity")
        return
    ieee = ieee or ieee_of(device)
    if not (device_id or ieee or topic or event_entity):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Choose a device, a topic or an event entity")
        return
    if found := _already_assigned(ctrl, device_id, ieee, topic):
        connection.send_error(msg["id"], "already_assigned",
                              f"Already assigned as “{found[1].name}” in {area_name(hass, found[0]) or found[0]}")
        return
    inputs = ctrl.switch_inputs
    z2m = inputs.bridge_devices.get(ieee) if ieee else None
    z2m_name = z2m.friendly_name if z2m else (device.name if device is not None and ieee else None)
    name = (msg.get("name") or "").strip() or (device.name_by_user or device.name if device else None) or \
        (z2m.friendly_name if z2m else None) or topic or event_entity or "Switch"
    switch = SwitchConfig(id=uuid.uuid4().hex[:12], name=name, device_id=device_id, ieee=ieee, z2m_name=z2m_name,
                          topic=topic, event_entity=event_entity)
    # Buttons the device declares (Zigbee2MQTT definition, device triggers, event types): known, not learned.
    declared = list(z2m.actions) if z2m else []
    if device_id:
        declared += [t["subtype"] for t in (await async_device_triggers(hass, [device_id])).get(device_id, [])]
        for e in event_entities(hass, device_id):
            state = hass.states.get(e)
            declared += [a for a in (state.attributes.get("event_types") or [])] if state else []
    now = dt_util.utcnow().isoformat(timespec="seconds")
    for raw in dict.fromkeys(declared):
        if valid_action(raw):
            switch = switch.with_button(raw, "discovered", now)
    _save(ctrl, area_id, switch)
    connection.send_result(msg["id"], {"switch_id": switch.id, "revision": ctrl.store.revision})


_DIMMING = vol.Schema({
    vol.Optional("step"): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
    vol.Optional("hold_step"): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
    vol.Optional("interval"): vol.All(vol.Coerce(float), vol.Range(min=DIM_INTERVAL_MIN, max=DIM_INTERVAL_MAX)),
    vol.Optional("min_brightness"): vol.All(vol.Coerce(int), vol.Range(min=BRIGHTNESS_MIN, max=BRIGHTNESS_MAX)),
    vol.Optional("max_brightness"): vol.All(vol.Coerce(int), vol.Range(min=BRIGHTNESS_MIN, max=BRIGHTNESS_MAX)),
    vol.Optional("timeout"): vol.All(vol.Coerce(int), vol.Range(min=1, max=DIM_TIMEOUT_MAX)),
})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/set",
    vol.Required("area_id"): str,
    vol.Required("switch_id"): str,
    vol.Optional("name"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Optional("source"): vol.In([s.value for s in SwitchSource]),
    vol.Optional("topic"): vol.Any(None, vol.All(str, vol.Length(max=256))),
    vol.Optional("event_entity"): vol.Any(None, cv.entity_id),
    vol.Optional("debounce_ms"): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_DEBOUNCE_MS)),
    vol.Optional("activity_evidence"): bool,
    vol.Optional("override_minutes"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(
        min=0, max=MANUAL_OVERRIDE_MAX_MINUTES))),
    vol.Optional("dimming"): _DIMMING,
})
@websocket_api.require_admin
@callback
def ws_set(hass: HomeAssistant, connection, msg) -> None:
    if (ctrl := _ctrl(hass, connection, msg)) is None or (switch := _switch(ctrl, connection, msg)) is None:
        return
    changes: dict[str, Any] = {}
    if "name" in msg:
        changes["name"] = msg["name"].strip() or switch.name
    if "source" in msg:
        changes["source"] = SwitchSource(msg["source"])
    if "topic" in msg:
        topic = (msg["topic"] or "").strip() or None
        if topic and (error := validate_topic(topic)):
            connection.send_error(msg["id"], error, TOPIC_ERRORS[error])
            return
        changes["topic"] = topic
    if "event_entity" in msg:
        entity = msg["event_entity"] or None
        if entity and not entity.startswith("event."):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{entity} is not an event entity")
            return
        changes["event_entity"] = entity
    for key in ("debounce_ms", "activity_evidence", "override_minutes"):
        if key in msg:
            changes[key] = msg[key]
    if "dimming" in msg:
        dimming = {**switch.dimming.as_dict(), **msg["dimming"]}
        if dimming["min_brightness"] > dimming["max_brightness"]:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  "The minimum brightness must not be above the maximum")
            return
        changes["dimming"] = type(switch.dimming).from_dict(dimming)
    _save(ctrl, msg["area_id"], replace(switch, **changes))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


def validate_mapping(hass: HomeAssistant, ctrl: LightingController, area_id: str,
                     mapping: ButtonMapping) -> str | None:
    """Why a mapping cannot be saved, or None. Only fixed, safe actions on validated entities."""
    a = mapping.action
    if a in (ButtonAction.ON, ButtonAction.OFF, ButtonAction.TOGGLE, ButtonAction.BRIGHTNESS_UP,
             ButtonAction.BRIGHTNESS_DOWN, ButtonAction.DIM_UP, ButtonAction.DIM_DOWN, ButtonAction.DIM_STOP,
             ButtonAction.SET_BRIGHTNESS, ButtonAction.SET_COLOR_TEMP):
        if mapping.target != ROOM_TARGET and (not mapping.target.startswith("light.") or (
                hass.states.get(mapping.target) is None and er.async_get(hass).async_get(mapping.target) is None)):
            return f"{mapping.target} is not a light or light group"
        if a == ButtonAction.ON and mapping.on_values == OnValues.FIXED and not (mapping.brightness or mapping.color_temp):
            return "Choose a brightness or colour temperature for fixed values"
        if a == ButtonAction.SET_BRIGHTNESS and not mapping.brightness:
            return f"Choose a brightness ({BRIGHTNESS_MIN}-{BRIGHTNESS_MAX} %)"
        if a == ButtonAction.SET_COLOR_TEMP and not mapping.color_temp:
            return f"Choose a colour temperature ({KELVIN_MIN}-{KELVIN_MAX} K)"
    elif a == ButtonAction.MOOD:
        if not mapping.mood:
            return "Choose a mood"
        if mapping.scope == "global":
            helper = ctrl.config.mood.manual_mood_entity
            state = hass.states.get(helper) if helper else None
            if state is None:
                return "The mood selector (Manual Mood) is not configured or not available"
            if mapping.mood.casefold() not in {str(o).casefold() for o in state.attributes.get("options") or []}:
                return f"'{mapping.mood}' is not an option of {helper}"
        elif mapping.mood.casefold() not in {m["name"].casefold() for m in ctrl.discovered_moods()}:
            return f"Mood '{mapping.mood}' does not exist"
    elif a == ButtonAction.MOOD_AUTO:
        if mapping.scope == "global" and not ctrl.config.mood.manual_mood_entity:
            return "The mood selector (Manual Mood) is not configured"
    elif a in (ButtonAction.SCENE, ButtonAction.SCRIPT):
        domain = "scene" if a == ButtonAction.SCENE else "script"
        if not mapping.entity or not mapping.entity.startswith(f"{domain}.") or hass.states.get(mapping.entity) is None:
            return f"Choose an existing {domain}"
    elif a == ButtonAction.WLED_PRESET:
        entry = er.async_get(hass).async_get(mapping.entity or "")
        if entry is None or entry.platform != "wled" or entry.domain != "select":
            return "Choose a WLED preset selector (WLED integration)"
        state = hass.states.get(entry.entity_id)
        if not mapping.option or state is None or mapping.option not in (state.attributes.get("options") or []):
            return "Choose one of the WLED presets"
    return None


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/mapping",
    vol.Required("area_id"): str,
    vol.Required("switch_id"): str,
    vol.Required("raw"): vol.All(str, vol.Length(min=1, max=64)),
    vol.Optional("mapping"): vol.Any(None, {
        vol.Required("action"): vol.In([a.value for a in ButtonAction]),
        vol.Optional("target"): vol.All(str, vol.Length(min=1, max=255)),
        vol.Optional("on_values"): vol.In([v.value for v in OnValues]),
        vol.Optional("brightness"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=BRIGHTNESS_MIN,
                                                                                      max=BRIGHTNESS_MAX))),
        vol.Optional("color_temp"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=KELVIN_MIN, max=KELVIN_MAX))),
        vol.Optional("mood"): vol.Any(None, vol.All(str, vol.Length(min=1, max=100))),
        vol.Optional("scope"): vol.In(["room", "global"]),
        vol.Optional("entity"): vol.Any(None, cv.entity_id),
        vol.Optional("option"): vol.Any(None, vol.All(str, vol.Length(min=1, max=255))),
        vol.Optional("minutes"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=1,
                                                                                  max=MANUAL_OVERRIDE_MAX_MINUTES))),
    }),
    vol.Optional("forget"): bool,
    vol.Optional("add"): bool,
})
@websocket_api.require_admin
@callback
def ws_mapping(hass: HomeAssistant, connection, msg) -> None:
    """Assign one button's action (``mapping: null`` leaves it unassigned), forget the button, or add a
    button the device declares (``add``)."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or (switch := _switch(ctrl, connection, msg)) is None:
        return
    raw = msg["raw"]
    if not valid_action(raw):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "Not a valid button action")
        return
    if msg.get("forget"):
        buttons = {k: v for k, v in switch.buttons.items() if k != raw}
        mappings = {k: v for k, v in switch.mappings.items() if k != raw}
        _save(ctrl, msg["area_id"], replace(switch, buttons=buttons, mappings=mappings))
        connection.send_result(msg["id"], {"revision": ctrl.store.revision})
        return
    if msg.get("add"):
        if raw not in declared_actions(hass, ctrl, switch):
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "The device does not declare this button")
            return
        _save(ctrl, msg["area_id"], switch.with_button(raw, "discovered", dt_util.utcnow().isoformat(timespec="seconds")))
        connection.send_result(msg["id"], {"revision": ctrl.store.revision})
        return
    if raw not in switch.buttons:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Learn or discover this button first")
        return
    mappings = dict(switch.mappings)
    if msg.get("mapping") is None:
        mappings[raw] = ButtonMapping()  # explicitly unassigned (no default suggestion)
    else:
        mapping = ButtonMapping.from_dict(msg["mapping"])
        if error := validate_mapping(hass, ctrl, msg["area_id"], mapping):
            connection.send_error(msg["id"], "invalid_mapping", error)
            return
        mappings[raw] = mapping
    _save(ctrl, msg["area_id"], replace(switch, mappings=mappings))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/mode",
    vol.Required("area_id"): str,
    vol.Required("switch_id"): str,
    vol.Required("mode"): vol.In([m.value for m in SwitchMode]),
    vol.Optional("confirm"): bool,
    vol.Optional("acknowledge", default=[]): [cv.entity_id],
})
@websocket_api.require_admin
@callback
def ws_mode(hass: HomeAssistant, connection, msg) -> None:
    """Observing (listen), or (explicitly confirmed) Active (control). The room's lighting mode is not involved."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or (switch := _switch(ctrl, connection, msg)) is None:
        return
    if msg["mode"] == SwitchMode.LISTEN:
        _save(ctrl, msg["area_id"], replace(switch, mode=SwitchMode.LISTEN))
        connection.send_result(msg["id"], {"revision": ctrl.store.revision})
        return
    if msg.get("confirm") is not True:
        connection.send_error(msg["id"], "confirmation_required", "Confirm to set this switch to Active")
        return
    inputs = ctrl.switch_inputs
    z2m = inputs.bridge_devices.get(switch.ieee) if switch.ieee else None
    if bindings := light_bindings(z2m, inputs.coordinator):
        connection.send_error(msg["id"], "binding", (
            "This switch is directly bound to lights in Zigbee2MQTT ("
            + ", ".join(f"{b['cluster']} → {b['target']}" for b in bindings)
            + "). Both would switch the lights. Remove the binding in Zigbee2MQTT first, or keep it Observing."))
        return
    acknowledged = tuple(dict.fromkeys([*switch.acknowledged, *msg["acknowledge"]]))
    open_competing = [a for a in _competing(hass, ctrl, switch) if a["enabled"] and a["entity_id"] not in acknowledged]
    if open_competing:
        connection.send_error(msg["id"], "competing_automation", (
            "Enabled automations also react to this switch: "
            + ", ".join(a["name"] or a["entity_id"] for a in open_competing)
            + ". Disable or change them first, or acknowledge them."))
        return
    _save(ctrl, msg["area_id"], replace(switch, mode=SwitchMode.CONTROL, acknowledged=acknowledged,
                                        activated_at=dt_util.utcnow().isoformat(timespec="seconds"),
                                        activated_by=connection.user.id))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/learn",
    vol.Required("area_id"): str,
    vol.Required("switch_id"): str,
    vol.Required("action"): vol.In(["start", "stop"]),
    vol.Optional("seconds", default=DEFAULT_LEARN_SECONDS): vol.All(vol.Coerce(int), vol.Range(
        min=5, max=MAX_LEARN_SECONDS)),
})
@websocket_api.require_admin
@callback
def ws_learn(hass: HomeAssistant, connection, msg) -> None:
    """Learn buttons: captured presses are saved as buttons; nothing is executed while learning."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or (switch := _switch(ctrl, connection, msg)) is None:
        return
    inputs = ctrl.switch_inputs
    if msg["action"] == "stop":
        captured = inputs.async_stop_learning(msg["area_id"], switch.id)
        connection.send_result(msg["id"], {"active": False, "captured": captured})
        return
    if msg["area_id"] not in ctrl.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "The room is not enabled")
        return
    session = inputs.async_start_learning(msg["area_id"], switch.id, msg["seconds"])
    connection.send_result(msg["id"], session.as_dict())


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/remove",
    vol.Required("area_id"): str,
    vol.Required("switch_id"): str,
})
@websocket_api.require_admin
@callback
def ws_remove(hass: HomeAssistant, connection, msg) -> None:
    """Remove a switch from the room (its subscriptions end). Existing overrides expire as usual."""
    if (ctrl := _ctrl(hass, connection, msg)) is None or (switch := _switch(ctrl, connection, msg)) is None:
        return
    ctrl.switch_inputs.async_stop_learning(msg["area_id"], switch.id)
    ctrl.store.async_set(ctrl.lighting.with_room(msg["area_id"], ctrl.lighting.room(msg["area_id"]).without_switch(
        switch.id)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/switch/settings",
    vol.Required("mqtt_base_topic"): vol.All(str, vol.Length(min=1, max=128)),
})
@websocket_api.require_admin
@callback
def ws_settings(hass: HomeAssistant, connection, msg) -> None:
    if (ctrl := _ctrl(hass, connection, msg)) is None:
        return
    base = msg["mqtt_base_topic"].strip()
    if error := validate_base_topic(base):
        connection.send_error(msg["id"], error, TOPIC_ERRORS.get(error, "Not a valid base topic"))
        return
    ctrl.store.async_set(replace(ctrl.lighting, switch_settings=SwitchSettings(mqtt_base_topic=base)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


SWITCH_HANDLERS = (ws_candidates, ws_add, ws_set, ws_mapping, ws_mode, ws_learn, ws_remove, ws_settings)
