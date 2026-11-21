"""Entity and device discovery from Home Assistant's states and registries."""

from __future__ import annotations

from typing import Any

from .catalog import CONTROLLABLE_DOMAINS, DOMAINS
from .compiler import is_managed_id

OCCUPANCY_CLASSES = ("occupancy", "presence", "motion")
OPTION_ATTRIBUTES = ("options", "hvac_modes", "preset_modes", "fan_modes", "effect_list", "min_color_temp_kelvin",
                     "max_color_temp_kelvin", "supported_color_modes", "min", "max", "step", "min_temp", "max_temp")
DOMAIN_STATES = {
    "binary_sensor": ["on", "off"], "light": ["on", "off"], "switch": ["on", "off"], "input_boolean": ["on", "off"],
    "fan": ["on", "off"], "siren": ["on", "off"], "humidifier": ["on", "off"],
    "cover": ["open", "closed", "opening", "closing"], "valve": ["open", "closed"],
    "lock": ["locked", "unlocked"], "person": ["home", "not_home"], "device_tracker": ["home", "not_home"],
    "sun": ["above_horizon", "below_horizon"], "media_player": ["off", "idle", "playing", "paused"],
    "vacuum": ["docked", "cleaning", "returning", "idle"], "alarm_control_panel": ["disarmed", "armed_home", "armed_away"],
}


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

    def view(self, entity_id: str, detail: bool = False) -> dict[str, Any]:
        state = self.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        entry = self.registry.get(entity_id) or {}
        device_id = entry.get("device_id")
        device = self.devices.get(device_id) or {}
        area_id = entry.get("area_id") or device.get("area_id")
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
        if purpose == "override":
            return domain in ("input_boolean", "switch", "binary_sensor") and not attributes.get("device_class") == "occupancy"
        return True

    def search(self, query: str, purpose: str = "any", limit: int = 60) -> list[dict[str, Any]]:
        tokens = [t for t in query.casefold().split() if t]
        scored = []
        for entity_id in set(self.states) | {e for e in self.registry if e}:
            if not self._wanted(entity_id, purpose):
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
        return [view for *_, view in scored[:limit]]

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
