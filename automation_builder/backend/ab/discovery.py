"""Entity and device discovery from Home Assistant's states and registries."""

from __future__ import annotations

from typing import Any

from .catalog import CONTROLLABLE_DOMAINS, DOMAINS
from .compiler import is_managed_id

OCCUPANCY_CLASSES = ("occupancy", "presence", "motion")
OPTION_ATTRIBUTES = ("event_types", "options", "hvac_modes", "preset_modes", "fan_modes", "effect_list", "min_color_temp_kelvin",
                     "max_color_temp_kelvin", "supported_color_modes", "min", "max", "step", "min_temp", "max_temp")
DOMAIN_STATES = {
    "binary_sensor": ["on", "off"], "light": ["on", "off"], "switch": ["on", "off"], "input_boolean": ["on", "off"],
    "fan": ["on", "off"], "siren": ["on", "off"], "humidifier": ["on", "off"],
    "cover": ["open", "closed", "opening", "closing"], "valve": ["open", "closed"],
    "lock": ["locked", "unlocked"], "person": ["home", "not_home"], "device_tracker": ["home", "not_home"],
    "sun": ["above_horizon", "below_horizon"], "media_player": ["off", "idle", "playing", "paused"],
    "vacuum": ["docked", "cleaning", "returning", "idle"], "alarm_control_panel": ["disarmed", "armed_home", "armed_away"],
}


#: Entity pickers that are limited to some domains.
PURPOSE_DOMAINS = {
    "button": ("event",), "script": ("script",), "scene": ("scene",), "notify": ("notify",),
    "select": ("input_select", "select"),
    "mood": ("input_select", "select", "sensor", "input_text"),
    "presence": ("binary_sensor", "input_boolean", "switch"),
    "person": ("person", "device_tracker"),
    "weather": ("weather",),
    "binary": ("binary_sensor", "input_boolean"),
    "light": ("light",),
    "cover": ("cover",),
    "helper": ("input_boolean", "input_button", "input_number", "input_select", "input_text", "input_datetime",
               "counter", "timer", "schedule"),
}
SEARCH_PURPOSES = ("any", "target", "occupancy", "numeric", "override", "time", *PURPOSE_DOMAINS)


class Index:
    """Lookups over one snapshot of Home Assistant's states and registries."""

    def __init__(self, states: dict[str, dict[str, Any]], entity_registry: list[dict[str, Any]],
                 devices: list[dict[str, Any]], areas: list[dict[str, Any]]) -> None:
        self.states = states
        self.registry = {e.get("entity_id"): e for e in entity_registry}
        self.devices = {d.get("id"): d for d in devices}
        self.areas = {a.get("area_id"): a.get("name") or a.get("area_id") for a in areas}

    def exists(self, entity_id: str) -> bool:
        return entity_id in self.states or entity_id in self.registry

    def name(self, entity_id: str) -> str:
        state = self.states.get(entity_id)
        if state and (state.get("attributes") or {}).get("friendly_name"):
            return str(state["attributes"]["friendly_name"])
        entry = self.registry.get(entity_id) or {}
        return str(entry.get("name") or entry.get("original_name") or entity_id)

    def device_name(self, device_id: str | None) -> str | None:
        device = self.devices.get(device_id) or {}
        return device.get("name_by_user") or device.get("name")

    def is_own_automation(self, entity_id: str) -> bool:
        attributes = (self.states.get(entity_id) or {}).get("attributes") or {}
        return entity_id.startswith("automation.") and is_managed_id(attributes.get("id"))

    def hidden(self, entity_id: str) -> bool:
        entry = self.registry.get(entity_id) or {}
        return bool(entry.get("disabled_by") or entry.get("hidden_by"))

    def area_of(self, entity_id: str) -> str | None:
        """The Home Assistant area of an entity: its own, or the one it inherits from its device."""
        entry = self.registry.get(entity_id) or {}
        area_id = entry.get("area_id") or (self.devices.get(entry.get("device_id")) or {}).get("area_id")
        return area_id if area_id in self.areas else None

    def room_of(self, entity_ids: list[str]) -> dict[str, Any]:
        """Where a managed device is listed: its room, "multiple" when its entities are in several, or "unassigned".

        A device is listed once. Rooms only organise the interface; they change nothing about its automation.
        """
        areas = []
        for entity_id in entity_ids:
            area_id = self.area_of(entity_id)
            if area_id not in areas:
                areas.append(area_id)
        named = [{"area_id": a, "name": self.areas[a]} for a in areas if a]
        if len(areas) == 1 and areas[0]:
            return {"kind": "room", "area_id": areas[0], "name": self.areas[areas[0]], "areas": named}
        if not named:
            return {"kind": "unassigned", "area_id": None, "name": "Unassigned", "areas": []}
        return {"kind": "multiple", "area_id": None, "name": "Multiple rooms", "areas": named,
                "partly_unassigned": None in areas}

    def _controllable_by_area(self) -> dict[str | None, dict[str | None, list[str]]]:
        """``{area: {Home Assistant device id (None: no device): [controllable entities]}}``."""
        out: dict[str | None, dict[str | None, list[str]]] = {}
        for entity_id in set(self.states) | {e for e in self.registry if e}:
            if not self._wanted(entity_id, "target"):
                continue
            device_id = (self.registry.get(entity_id) or {}).get("device_id")
            out.setdefault(self.area_of(entity_id), {}).setdefault(device_id if device_id in self.devices else None, []).append(entity_id)
        return out

    def rooms(self, query: str = "") -> list[dict[str, Any]]:
        """Every area as a room, with how much there is to control in it; "Unassigned" last."""
        tokens = [t for t in query.casefold().split() if t]
        found = self._controllable_by_area()
        out = []
        for area_id, name in sorted(self.areas.items(), key=lambda a: str(a[1]).casefold()):
            devices = found.get(area_id, {})
            if all(t in str(name).casefold() for t in tokens):
                out.append({"area_id": area_id, "name": name, "devices": sum(1 for d in devices if d),
                            "entities": sum(len(e) for e in devices.values())})
        loose = found.get(None, {})
        if all(t in "unassigned" for t in tokens):
            out.append({"area_id": None, "name": "Unassigned", "devices": sum(1 for d in loose if d),
                        "entities": sum(len(e) for e in loose.values()), "unassigned": True})
        return out

    def room_devices(self, area_id: str | None, query: str = "") -> list[dict[str, Any]]:
        """The devices of one room that have something to control, each with those entities.

        A physical device is listed with *all* its controllable entities, also those assigned to another room
        (they carry their room's name). Entities without a device come as one entry at the end.
        """
        tokens = [t for t in query.casefold().split() if t]
        found = self._controllable_by_area().get(area_id, {})
        out = []
        for device_id in found:
            if device_id is None:
                continue
            device = self.devices[device_id]
            entity_ids = [e for e, entry in self.registry.items()
                          if e and entry.get("device_id") == device_id and self._wanted(e, "target")]
            entities = sorted((self.entity_choice(e) for e in entity_ids), key=lambda v: str(v["name"]).casefold())
            name = self.device_name(device_id) or device_id
            model = " ".join(str(v) for v in (device.get("manufacturer"), device.get("model")) if v)
            haystack = " ".join([name, model, *(f"{v['entity_id']} {v['name']}" for v in entities)]).casefold()
            if all(t in haystack for t in tokens):
                out.append({"device_id": device_id, "name": name, "model": model, "entities": entities,
                            "domains": sorted({v["domain"] for v in entities})})
        out.sort(key=lambda d: str(d["name"]).casefold())
        loose = sorted((self.entity_choice(e) for e in found.get(None, [])), key=lambda v: str(v["name"]).casefold())
        loose = [v for v in loose if all(t in f"{v['entity_id']} {v['name']}".casefold() for t in tokens)]
        if loose:
            out.append({"device_id": None, "name": "Entities without a device", "model": "helpers, groups, scripts, scenes …",
                        "entities": loose, "domains": sorted({v["domain"] for v in loose})})
        return out

    def entity_choice(self, entity_id: str) -> dict[str, Any]:
        view = self.view(entity_id)
        area_id = self.area_of(entity_id)
        return {**view, "area_id": area_id, "area": self.areas.get(area_id)}

    def view(self, entity_id: str, detail: bool = False) -> dict[str, Any]:
        state = self.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        entry = self.registry.get(entity_id) or {}
        device_id = entry.get("device_id")
        device = self.devices.get(device_id) or {}
        area_id = self.area_of(entity_id)
        domain = entity_id.split(".", 1)[0]
        out = {
            "entity_id": entity_id, "name": self.name(entity_id), "domain": domain,
            "domain_label": DOMAINS.get(domain, {}).get("label") or domain.replace("_", " "),
            "device_class": attributes.get("device_class"), "state": state.get("state"),
            "unit": attributes.get("unit_of_measurement"), "area": self.areas.get(area_id),
            "device_id": device_id, "device_name": self.device_name(device_id), "exists": self.exists(entity_id),
            "controllable": domain in CONTROLLABLE_DOMAINS,
        }
        if detail:
            out["attributes"] = {k: attributes[k] for k in OPTION_ATTRIBUTES if k in attributes}
            out["suggested_states"] = self.suggested_states(entity_id)
        return out

    def suggested_states(self, entity_id: str) -> list[str]:
        domain = entity_id.split(".", 1)[0]
        state = self.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        if domain in ("input_select", "select") and isinstance(attributes.get("options"), list):
            return [str(o) for o in attributes["options"]][:100]
        if domain == "climate" and isinstance(attributes.get("hvac_modes"), list):
            return [str(o) for o in attributes["hvac_modes"]]
        if domain in DOMAIN_STATES:
            return list(DOMAIN_STATES[domain])
        current = state.get("state")
        return [current] if current not in (None, "unavailable", "unknown") else []

    # ------------------------------------------------------------ search
    def _wanted(self, entity_id: str, purpose: str) -> bool:
        domain = entity_id.split(".", 1)[0]
        if self.is_own_automation(entity_id) or self.hidden(entity_id):
            return False
        state = self.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        if purpose == "target":
            return domain in CONTROLLABLE_DOMAINS
        if purpose == "occupancy":
            return domain == "binary_sensor"
        if purpose == "numeric":
            if domain in ("sensor", "number", "input_number", "counter"):
                return True
            try:
                float(state.get("state"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return False
            return True
        if purpose in PURPOSE_DOMAINS:
            return domain in PURPOSE_DOMAINS[purpose]
        if purpose == "time":
            return domain == "input_datetime" or (domain == "sensor" and attributes.get("device_class") == "timestamp")
        if purpose == "override":
            return domain in ("input_boolean", "switch", "binary_sensor") and not attributes.get("device_class") == "occupancy"
        return True

    def search(self, query: str, purpose: str = "any", limit: int = 60, area: str | None = None,
               keep: str | None = None) -> list[dict[str, Any]]:
        """Entities for a picker. ``area`` limits it to one room (the entity's own area or its device's);
        ``keep`` is the entity chosen at present: it is always listed first, also when it is outside the room."""
        tokens = [t for t in query.casefold().split() if t]
        scored = []
        for entity_id in set(self.states) | {e for e in self.registry if e}:
            if entity_id == keep or not self._wanted(entity_id, purpose):
                continue
            if area is not None and self.area_of(entity_id) != area:
                continue
            view = self.view(entity_id)
            haystack = " ".join(str(v) for v in (entity_id, view["name"], view["area"], view["device_name"],
                                                 view["domain_label"]) if v).casefold()
            if not all(t in haystack for t in tokens):
                continue
            rank = 1
            if purpose == "occupancy":
                likely = view["device_class"] in OCCUPANCY_CLASSES or "occup" in entity_id
                rank = 0 if likely else 1
            scored.append((rank, str(view["name"]).casefold(), entity_id, view))
        scored.sort(key=lambda item: item[:3])
        current = [{**self.view(keep), "current": True}] if keep and self.exists(keep) else []
        return [*current, *(view for *_, view in scored[:limit])]

    def all_devices(self, query: str, limit: int = 60) -> list[dict[str, Any]]:
        """Every Home Assistant device (for device triggers: a remote has no controllable entity)."""
        tokens = [t for t in query.casefold().split() if t]
        out = []
        for device_id, device in self.devices.items():
            if not device_id or device.get("disabled_by") or device.get("entry_type") == "service":
                continue
            name = self.device_name(device_id) or device_id
            area = self.areas.get(device.get("area_id"))
            model = " ".join(str(v) for v in (device.get("manufacturer"), device.get("model")) if v)
            if all(t in f"{name} {area or ''} {model}".casefold() for t in tokens):
                out.append({"device_id": device_id, "name": name, "area": area, "model": model})
        out.sort(key=lambda d: str(d["name"]).casefold())
        return out[:limit]

    def entity_by_registry_id(self, registry_id: Any) -> str | None:
        if not isinstance(registry_id, str) or not registry_id:
            return None
        for entity_id, entry in self.registry.items():
            if entry.get("id") == registry_id:
                return entity_id
        return None

    def physical_devices(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        """Home Assistant devices that have at least one controllable entity, each with those entities."""
        tokens = [t for t in query.casefold().split() if t]
        by_device: dict[str, list[str]] = {}
        for entity_id, entry in self.registry.items():
            if entity_id and entry.get("device_id") and self._wanted(entity_id, "target"):
                by_device.setdefault(entry["device_id"], []).append(entity_id)
        out = []
        for device_id, entity_ids in by_device.items():
            device = self.devices.get(device_id) or {}
            name = self.device_name(device_id) or device_id
            area = self.areas.get(device.get("area_id"))
            entities = sorted((self.view(e) for e in entity_ids), key=lambda v: str(v["name"]).casefold())
            haystack = " ".join([name, area or "", device.get("manufacturer") or "", device.get("model") or "",
                                 *(f"{v['entity_id']} {v['name']}" for v in entities)]).casefold()
            if all(t in haystack for t in tokens):
                out.append({"device_id": device_id, "name": name, "area": area,
                            "model": " ".join(str(v) for v in (device.get("manufacturer"), device.get("model")) if v),
                            "entities": entities})
        out.sort(key=lambda d: str(d["name"]).casefold())
        return out[:limit]
