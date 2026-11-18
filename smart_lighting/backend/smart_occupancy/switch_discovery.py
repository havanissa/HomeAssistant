"""Wall switch discovery: candidate devices, their buttons, competing controls (read only).

Sources of information, all read only:

* the Home Assistant device and entity registries (name, manufacturer,
  model, area, event entities);
* Zigbee2MQTT's retained device list (``<base>/bridge/devices``): friendly
  name, IEEE address, model, the values of the ``action`` expose and the
  device's bindings. Nothing is ever published to Zigbee2MQTT;
* Home Assistant MQTT device triggers of a device;
* automations whose triggers reference the switch (its device, event
  entity or MQTT topic): possible competing controls.

A switch that is directly bound to lights or groups (a Zigbee binding)
switches them itself; Smart Occupancy control is then refused for it and it
can only be used in Listen only.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import BRIDGE_PAYLOAD_MAX_BYTES, DOMAIN, SwitchSource
from .switches import SwitchConfig, valid_action

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

_Z2M_ID = re.compile(r"zigbee2mqtt_(0x[0-9a-fA-F]{16})")
# Clusters whose bindings make a switch command lights directly.
_LIGHT_CLUSTERS = frozenset({"genOnOff", "genLevelCtrl", "lightingColorCtrl", "genScenes"})


@dataclass(frozen=True, slots=True)
class Z2MDevice:
    ieee: str
    friendly_name: str
    model: str | None = None
    vendor: str | None = None
    description: str | None = None
    actions: tuple[str, ...] = ()
    bindings: tuple[dict[str, Any], ...] = ()
    power_source: str | None = None
    type: str | None = None


def _action_values(exposes: Any) -> list[str]:
    """Values of the ``action`` expose (also inside composite exposes)."""
    out: list[str] = []
    stack = [exposes]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            if (item.get("name") == "action" or item.get("property") == "action") and isinstance(item.get("values"), list):
                out.extend(v for v in item["values"] if valid_action(v))
            if isinstance(item.get("features"), list):
                stack.append(item["features"])
    return list(dict.fromkeys(out))


def parse_bridge_devices(payload: Any) -> tuple[dict[str, Z2MDevice], str | None]:
    """Zigbee2MQTT's device list -> ({ieee: device}, coordinator ieee). Invalid input gives ({}, None)."""
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > BRIDGE_PAYLOAD_MAX_BYTES:
            return {}, None
        payload = payload.decode("utf-8", errors="replace")
    if not isinstance(payload, str) or len(payload) > BRIDGE_PAYLOAD_MAX_BYTES:
        return {}, None
    try:
        data = json.loads(payload)
    except ValueError:
        return {}, None
    if not isinstance(data, list):
        return {}, None
    devices: dict[str, Z2MDevice] = {}
    coordinator: str | None = None
    for item in data:
        if not isinstance(item, dict):
            continue
        ieee, name = item.get("ieee_address"), item.get("friendly_name")
        if not isinstance(ieee, str) or not isinstance(name, str) or not name:
            continue
        ieee = ieee.lower()
        if item.get("type") == "Coordinator":
            coordinator = ieee
            continue
        definition = item.get("definition") if isinstance(item.get("definition"), dict) else {}
        bindings: list[dict[str, Any]] = []
        endpoints = item.get("endpoints") if isinstance(item.get("endpoints"), dict) else {}
        for endpoint, info in endpoints.items():
            for b in (info.get("bindings") or []) if isinstance(info, dict) else []:
                if isinstance(b, dict) and isinstance(b.get("target"), dict):
                    bindings.append({"endpoint": str(endpoint), "cluster": b.get("cluster"), "target": b["target"]})
        devices[ieee] = Z2MDevice(
            ieee=ieee, friendly_name=name,
            model=definition.get("model") or item.get("model_id"),
            vendor=definition.get("vendor") or item.get("manufacturer"),
            description=definition.get("description"),
            actions=tuple(_action_values(definition.get("exposes"))),
            bindings=tuple(bindings),
            power_source=item.get("power_source"),
            type=item.get("type"),
        )
    return devices, coordinator


def light_bindings(device: Z2MDevice | None, coordinator: str | None) -> list[dict[str, Any]]:
    """Direct Zigbee bindings to lights or groups (bindings to the coordinator are normal and ignored)."""
    if device is None:
        return []
    out = []
    for b in device.bindings:
        if b.get("cluster") not in _LIGHT_CLUSTERS:
            continue
        target = b["target"]
        if target.get("type") == "endpoint" and str(target.get("ieee_address") or "").lower() in ("", coordinator or ""):
            continue
        what = f"group {target.get('id')}" if target.get("type") == "group" else f"device {target.get('ieee_address')}"
        out.append({"cluster": b["cluster"], "endpoint": b["endpoint"], "target": what})
    return out


def all_devices(hass: HomeAssistant) -> list[dr.DeviceEntry]:
    """Every device (iterating the registry: entries since 2026.11, device ids before)."""
    dev_reg = dr.async_get(hass)
    out = []
    for item in dev_reg.devices:
        device = item if isinstance(item, dr.DeviceEntry) else dev_reg.async_get(item)
        if device is not None:
            out.append(device)
    return out


def ieee_of(device: dr.DeviceEntry | None) -> str | None:
    if device is None:
        return None
    for domain, identifier in device.identifiers:
        if domain == "mqtt" and (m := _Z2M_ID.search(str(identifier))):
            return m.group(1).lower()
    return None


def event_entities(hass: HomeAssistant, device_id: str | None) -> list[str]:
    if not device_id:
        return []
    ent_reg = er.async_get(hass)
    return sorted(e.entity_id for e in er.async_entries_for_device(ent_reg, device_id) if e.domain == "event")


async def async_device_triggers(hass: HomeAssistant, device_ids: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """MQTT device triggers per device ({device_id: [trigger config]}). Empty if unavailable."""
    device_ids = [d for d in device_ids if d]
    if not device_ids or "mqtt" not in hass.config.components:
        return {}
    from homeassistant.components.device_automation import (
        DeviceAutomationType,
        async_get_device_automations,
    )

    try:
        found = await async_get_device_automations(hass, DeviceAutomationType.TRIGGER, device_ids)
    except Exception as err:  # noqa: BLE001 - discovery must never break the integration
        _LOGGER.debug("switch_device_triggers_failed error=%s", err)
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for device_id, triggers in found.items():
        mqtt = [t for t in triggers if t.get("domain") == "mqtt" and valid_action(t.get("subtype"))]
        if mqtt:
            out[device_id] = mqtt
    return out


def resolve_source(switch: SwitchConfig, available: Iterable[str]) -> str | None:
    """The authoritative input path: the configured one, or the best available (raw MQTT first)."""
    available = list(available)
    if switch.source != SwitchSource.AUTO:
        return switch.source.value if switch.source.value in available else None
    for source in (SwitchSource.ZIGBEE2MQTT, SwitchSource.DEVICE_TRIGGER, SwitchSource.EVENT_ENTITY):
        if source.value in available:
            return source.value
    return None


@dataclass(slots=True)
class CompetingAutomation:
    entity_id: str
    name: str | None
    enabled: bool
    via: str
    buttons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "enabled": self.enabled, "via": self.via,
                "buttons": self.buttons}


def _walk(config: Any):
    stack = [config]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value] if value is not None else []


def competing_automations(hass: HomeAssistant, *, device_id: str | None, entities: Iterable[str],
                          topics: Iterable[str]) -> list[CompetingAutomation]:
    """Automations whose triggers react to this switch (device, event entity or MQTT topic). Read only."""
    component = hass.data.get("automation")
    if component is None:
        return []
    entities = set(entities)
    topics = {t for t in topics if t}
    topics |= {f"{t}/action" for t in list(topics)}
    out: list[CompetingAutomation] = []
    for entity in component.entities:
        raw = getattr(entity, "raw_config", None) or {}
        triggers = raw.get("triggers", raw.get("trigger")) if isinstance(raw, dict) else None
        if not triggers:
            continue
        via: str | None = None
        buttons: list[str] = []
        for item in _walk(triggers):
            if device_id and item.get("device_id") == device_id:
                via = via or "device trigger"
                if isinstance(item.get("subtype"), str):
                    buttons.append(item["subtype"])
            if entities and set(map(str, _as_list(item.get("entity_id")))) & entities:
                via = via or "event entity"
            if topics and isinstance(item.get("topic"), str) and item["topic"] in topics:
                via = via or "MQTT topic"
                if isinstance(item.get("payload"), str):
                    buttons.append(item["payload"])
            data = item.get("event_data")
            if device_id and isinstance(data, dict) and data.get("device_id") == device_id:
                via = via or "device event"
        if via:
            out.append(CompetingAutomation(entity.entity_id, (hass.states.get(entity.entity_id).attributes.get(
                "friendly_name") if hass.states.get(entity.entity_id) else None),
                hass.states.is_state(entity.entity_id, "on"), via, sorted(set(buttons))))
    out.sort(key=lambda a: (not a.enabled, a.entity_id))
    return out


def device_summary(hass: HomeAssistant, device_id: str | None) -> dict[str, Any]:
    """Name, manufacturer, model, area and availability of a Home Assistant device."""
    if not device_id or (device := dr.async_get(hass).async_get(device_id)) is None:
        return {"exists": False}
    ent_reg = er.async_get(hass)
    entities = [e.entity_id for e in er.async_entries_for_device(ent_reg, device_id)
                if e.platform != DOMAIN and not e.disabled_by]
    states = [hass.states.get(e) for e in entities]
    known = [s for s in states if s is not None]
    area = ar.async_get(hass).async_get_area(device.area_id) if device.area_id else None
    return {
        "exists": True,
        "name": device.name_by_user or device.name or device_id,
        "manufacturer": device.manufacturer,
        "model": device.model,
        "area": area.name if area else None,
        "entities": entities[:12],
        "available": None if not known else any(s.state != STATE_UNAVAILABLE for s in known),
    }


def candidates(hass: HomeAssistant, ctrl: LightingController, triggers: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Devices that look like wall switches or remotes, best first. Read only."""
    inputs = ctrl.switch_inputs
    bridge = inputs.bridge_devices
    ent_reg = er.async_get(hass)
    assigned = {
        (s.device_id or s.ieee or s.topic): (area_id, s.name)
        for area_id, room in ctrl.lighting.rooms.items() for s in room.switches
    }
    out: list[dict[str, Any]] = []
    seen_ieee: set[str] = set()
    for device in all_devices(hass):
        if device.disabled_by:
            continue
        ieee = ieee_of(device)
        z2m = bridge.get(ieee) if ieee else None
        events = sorted(e.entity_id for e in er.async_entries_for_device(ent_reg, device.id) if e.domain == "event")
        dev_triggers = triggers.get(device.id, [])
        if not (z2m and z2m.actions) and not events and not dev_triggers:
            continue
        if ieee:
            seen_ieee.add(ieee)
        sources = []
        if ieee:
            sources.append(SwitchSource.ZIGBEE2MQTT.value)
        if dev_triggers:
            sources.append(SwitchSource.DEVICE_TRIGGER.value)
        if events:
            sources.append(SwitchSource.EVENT_ENTITY.value)
        actions = list(dict.fromkeys([*(z2m.actions if z2m else ()), *(t["subtype"] for t in dev_triggers)]))
        for e in events:
            state = hass.states.get(e)
            for a in (state.attributes.get("event_types") or []) if state else []:
                if valid_action(a) and a not in actions:
                    actions.append(a)
        where = assigned.get(device.id) or (assigned.get(ieee) if ieee else None)
        summary = device_summary(hass, device.id)
        out.append({
            "device_id": device.id, "ieee": ieee, "name": summary.get("name"),
            "z2m_name": z2m.friendly_name if z2m else (device.name if ieee else None),
            "manufacturer": device.manufacturer or (z2m.vendor if z2m else None),
            "model": device.model or (z2m.model if z2m else None),
            "description": z2m.description if z2m else None,
            "area": summary.get("area"), "available": summary.get("available"),
            "sources": sources, "event_entities": events, "actions": actions[:64],
            "bindings": len(light_bindings(z2m, inputs.coordinator)) if z2m else 0,
            "assigned_room": where[0] if where else None,
        })
    for ieee, z2m in bridge.items():
        if ieee in seen_ieee or not z2m.actions:
            continue
        where = assigned.get(ieee)
        out.append({
            "device_id": None, "ieee": ieee, "name": z2m.friendly_name, "z2m_name": z2m.friendly_name,
            "manufacturer": z2m.vendor, "model": z2m.model, "description": z2m.description, "area": None,
            "available": None, "sources": [SwitchSource.ZIGBEE2MQTT.value], "event_entities": [],
            "actions": list(z2m.actions)[:64], "bindings": len(light_bindings(z2m, inputs.coordinator)),
            "assigned_room": where[0] if where else None,
        })
    out.sort(key=lambda c: (c["assigned_room"] is not None, (c["name"] or "").casefold()))
    return out
