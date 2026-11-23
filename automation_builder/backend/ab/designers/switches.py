"""Switches, buttons and remotes for the Lighting Designer: what Home Assistant offers for them, discovered, never
assumed.

Two kinds of control, kept apart on purpose:

* **latched** - a switch that *stays* on or off (a ``switch``, ``input_boolean`` or ``binary_sensor`` entity). Its
  interactions are the moments its state changes (``off`` -> ``on``, ``on`` -> ``off``), and the same entity can be a
  source of a matrix (its state).
* **momentary** - a button or remote that *reports presses*: Home Assistant device triggers (ZHA, Zigbee2MQTT over
  MQTT, Hue, deCONZ …), event entities (``event.*``, whose state is only the time of the last event), or events and
  MQTT messages mapped by hand. A press is never a state: it has no "is pressed" to look at later.

Only what the source really offers is listed: the device triggers Home Assistant returns for the device, the event
types an event entity declares, the on/off states of an entity. Anything else (a ``zha_event`` command, an MQTT
payload) is mapped by hand and validated like any other trigger.
"""

from __future__ import annotations

import re
from typing import Any

LATCHED_DOMAINS = ("switch", "input_boolean", "binary_sensor")
EVENT_DOMAIN = "event"
SOURCE_DOMAINS = (*LATCHED_DOMAINS, EVENT_DOMAIN)


def classify(*texts: Any) -> str:
    """The kind of an interaction from Home Assistant's own words for it (type, subtype, event type)."""
    tokens = set()
    for text in texts:
        tokens |= {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t}
    if "triple" in tokens:
        return "triple"
    if "double" in tokens:
        return "double"
    if tokens & {"quadruple", "quintuple", "many"}:
        return "other"
    if tokens & {"release", "released", "stop"}:
        return "release"
    if tokens & {"long", "hold", "held", "move"}:
        return "long"
    if tokens & {"up", "increase", "brighten"}:
        return "up"
    if tokens & {"down", "decrease", "dim"}:
        return "down"
    if tokens & {"short", "single", "press", "pressed", "click", "toggle"}:
        return "single"
    if "on" in tokens:
        return "on"
    if "off" in tokens:
        return "off"
    if tokens & {"changed", "change"}:
        return "changed"
    return "other"


def from_device_triggers(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Interactions from the device triggers Home Assistant lists for a device."""
    out = []
    for item in items:
        config = item.get("config") or {}
        found = {"kind": classify(config.get("type"), config.get("subtype")), "label": item.get("label") or "",
                 "source": f"device trigger ({config.get('domain')})",
                 "trigger": {"type": "device", "config": config, "label": item.get("label") or ""}}
        subtype = config.get("subtype")
        # the button or channel, as the integration names it (ZHA "button_1"/"turn_on", Hue "1"); a Zigbee2MQTT
        # action ("on_press") names the action itself, not a button
        if subtype not in (None, "") and config.get("domain") != "mqtt":
            found["button"] = (f"button {subtype}" if isinstance(subtype, int) or str(subtype).isdigit() else str(subtype))[:40]
        out.append(found)
    return out


def from_entity(index: Any, entity_id: str) -> list[dict[str, Any]]:
    """Interactions of one entity: the event types of an event entity, the state changes of a latched switch."""
    domain = entity_id.split(".", 1)[0]
    name = index.name(entity_id)
    attributes = (index.states.get(entity_id) or {}).get("attributes") or {}
    if domain == EVENT_DOMAIN:
        types = attributes.get("event_types") if isinstance(attributes.get("event_types"), list) else []
        return [{"kind": classify(t), "label": f"{name}: {t}", "source": "event entity",
                 "trigger": {"type": "button", "entity_id": entity_id, "event_types": [str(t)]}} for t in types]
    if domain in LATCHED_DOMAINS:
        return [{"kind": "on", "label": f"{name} switched on", "source": "state",
                 "trigger": {"type": "state", "entity_id": entity_id, "to": "on", "from": "off", "for_seconds": 0}},
                {"kind": "off", "label": f"{name} switched off", "source": "state",
                 "trigger": {"type": "state", "entity_id": entity_id, "to": "off", "from": "on", "for_seconds": 0}}]
    return []


def kind_of(found: list[dict[str, Any]]) -> str | None:
    if not found:
        return None
    return "latched" if all(i["trigger"]["type"] == "state" for i in found) else "momentary"


def notes(index: Any, ha_device_id: Any, entity_id: Any, found: list[dict[str, Any]]) -> list[str]:
    out = []
    domain = str(entity_id or "").split(".", 1)[0]
    if domain == "switch":
        out.append("A switch reports whether it is on or off. If it also powers the light directly, switching it off "
                   "cuts the light's power: no automation can switch that light on then, and it gets no commands while "
                   "it is unavailable. Some smart switches offer a detached (decoupled) mode for this; not all do.")
    if domain == EVENT_DOMAIN:
        out.append("An event entity reports presses; its state is only the time of the last one, so it cannot be a "
                   "matrix source.")
    if not found:
        out.append("Home Assistant offers no interactions for this. If the remote sends ZHA events or MQTT messages, map "
                   "them by hand (Manual event), with the values its integration documents.")
    if any(i["kind"] == "release" for i in found) and any(i["kind"] == "long" for i in found):
        out.append("It reports a long press and a release: “dim while held” can use them.")
    return out


def room_switches(index: Any, area: str | None, query: str = "", every_room: bool = False) -> list[dict[str, Any]]:
    """The devices of a room (or of every room) that can be a switch, button or remote, with their candidate entities.

    A remote often has no entity to control (ZHA remotes only have device triggers), so every device of the room is
    listed; its interactions are looked up when it is chosen.
    """
    tokens = [t for t in query.casefold().split() if t]
    by_device: dict[str | None, list[str]] = {}
    for entity_id, entry in index.registry.items():
        if not entity_id or entity_id.split(".", 1)[0] not in SOURCE_DOMAINS or index.hidden(entity_id):
            continue
        if not every_room and index.area_of(entity_id) != area:
            continue
        device_id = entry.get("device_id") if entry.get("device_id") in index.devices else None
        by_device.setdefault(device_id, []).append(entity_id)
    for entity_id in index.states:  # helpers made in YAML have no registry entry
        if entity_id.split(".", 1)[0] in SOURCE_DOMAINS and entity_id not in index.registry and (every_room or area is None):
            by_device.setdefault(None, []).append(entity_id)
    controls = ("light", "cover", "climate", "media_player", "fan", "lock", "vacuum", "water_heater", "valve")
    for device_id, device in index.devices.items():  # remotes: often nothing but device triggers and a battery sensor
        if device_id and not device.get("disabled_by") and device.get("entry_type") != "service" and (
                every_room or device.get("area_id") == area) and device_id not in by_device:
            own = [e for e, entry in index.registry.items() if entry.get("device_id") == device_id]
            if not any(e.split(".", 1)[0] in controls for e in own):
                by_device[device_id] = []
    out = []
    for device_id, entity_ids in by_device.items():
        device = index.devices.get(device_id) or {}
        name = (index.device_name(device_id) or device_id) if device_id else "Entities without a device"
        model_text = " ".join(str(v) for v in (device.get("manufacturer"), device.get("model")) if v)
        entities = sorted((index.entity_choice(e) for e in entity_ids), key=lambda v: str(v["name"]).casefold())
        haystack = " ".join([name, model_text, *(f"{v['entity_id']} {v['name']}" for v in entities)]).casefold()
        if all(t in haystack for t in tokens):
            out.append({"device_id": device_id, "name": name, "model": model_text, "entities": entities,
                        "area": index.areas.get(device.get("area_id")) if device_id else None})
    out.sort(key=lambda d: (d["device_id"] is None, str(d["name"]).casefold()))
    return out
