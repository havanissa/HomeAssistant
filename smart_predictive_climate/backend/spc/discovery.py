"""Room-first discovery: Home Assistant's areas, the devices in them, and which entities could serve which input.

Suggestions are only suggestions: nothing is ever added to a room by itself. Each suggested entity carries the
reason it is suggested and, where it matters, a caution (a thermostat's own temperature sensor measures next to
the radiator, not the room).
"""

from __future__ import annotations

from typing import Any

#: What each input of a room looks like in Home Assistant.
CATEGORIES: dict[str, dict[str, Any]] = {
    "temperature": {"label": "Room temperature", "domains": ("sensor",), "classes": ("temperature",)},
    "thermostats": {"label": "Thermostats, TRVs and HVAC", "domains": ("climate",), "classes": None},
    "humidity": {"label": "Humidity", "domains": ("sensor",), "classes": ("humidity",)},
    "openings": {"label": "Windows and doors", "domains": ("binary_sensor",),
                 "classes": ("window", "door", "opening", "garage_door")},
    "occupancy": {"label": "Occupancy and presence", "domains": ("binary_sensor",),
                  "classes": ("occupancy", "presence", "motion")},
    "covers": {"label": "Covers and blinds", "domains": ("cover",), "classes": None},
    "sunlight": {"label": "Sunlight", "domains": ("sensor",), "classes": ("illuminance", "irradiance")},
    "energy": {"label": "Energy and power", "domains": ("sensor",), "classes": ("energy", "power")},
    "running": {"label": "Heating system state", "domains": ("binary_sensor", "switch", "input_boolean"),
                "classes": ("heat", "running", "power", None)},
}
OUTDOOR_WORDS = ("outdoor", "outside", "exterior", "garden", "uti", "úti", "ute", "ude", "aussen", "außen",
                 "balcony", "porch", "weather")
NOT_ROOM_WORDS = ("cpu", "processor", "chip", "battery", "dew", "feels", "apparent", "water", "boiler", "flow",
                  "return", "pipe", "device_temperature", "internal", "fridge", "freezer", "router", "nas", "disk",
                  "gpu", "probe")
HEATING_WORDS = ("heat", "boiler", "furnace", "pump", "varm", "hiti", "radiator")


class Index:
    def __init__(self, states: dict[str, dict[str, Any]], entity_registry: list[dict[str, Any]],
                 devices: list[dict[str, Any]], areas: list[dict[str, Any]]) -> None:
        self.states = states
        self.registry = {e.get("entity_id"): e for e in entity_registry if e.get("entity_id")}
        self.devices = {d.get("id"): d for d in devices if d.get("id")}
        self.areas = {a.get("area_id"): a.get("name") or a.get("area_id") for a in areas if a.get("area_id")}

    # --------------------------------------------------------------- basics
    def exists(self, entity_id: str) -> bool:
        return entity_id in self.states or entity_id in self.registry

    def name(self, entity_id: str) -> str:
        state = self.states.get(entity_id) or {}
        name = (state.get("attributes") or {}).get("friendly_name")
        if name:
            return str(name)
        entry = self.registry.get(entity_id) or {}
        return str(entry.get("name") or entry.get("original_name") or entity_id)

    def hidden(self, entity_id: str) -> bool:
        entry = self.registry.get(entity_id) or {}
        return bool(entry.get("disabled_by") or entry.get("hidden_by"))

    def device_name(self, device_id: str | None) -> str | None:
        device = self.devices.get(device_id) or {}
        return device.get("name_by_user") or device.get("name")

    def area_of(self, entity_id: str) -> str | None:
        entry = self.registry.get(entity_id) or {}
        area = entry.get("area_id") or (self.devices.get(entry.get("device_id")) or {}).get("area_id")
        return area if area in self.areas else None

    def device_class(self, entity_id: str) -> str | None:
        state = self.states.get(entity_id) or {}
        entry = self.registry.get(entity_id) or {}
        return (state.get("attributes") or {}).get("device_class") or entry.get("device_class") \
            or entry.get("original_device_class")

    def view(self, entity_id: str) -> dict[str, Any]:
        state = self.states.get(entity_id) or {}
        attrs = state.get("attributes") or {}
        entry = self.registry.get(entity_id) or {}
        device_id = entry.get("device_id")
        area_id = self.area_of(entity_id)
        out = {"entity_id": entity_id, "name": self.name(entity_id), "domain": entity_id.split(".", 1)[0],
               "device_class": self.device_class(entity_id), "state": state.get("state"),
               "unit": attrs.get("unit_of_measurement"), "area_id": area_id, "area": self.areas.get(area_id),
               "device_id": device_id, "device_name": self.device_name(device_id), "exists": self.exists(entity_id)}
        if entity_id.startswith("climate."):
            out.update(current_temperature=attrs.get("current_temperature"), target=attrs.get("temperature"),
                       hvac_action=attrs.get("hvac_action"), reports_action="hvac_action" in attrs)
        if entity_id.startswith("weather."):
            out.update(temperature=attrs.get("temperature"), cloud_coverage=attrs.get("cloud_coverage"),
                       supported_features=attrs.get("supported_features"))
        if entity_id.startswith("cover."):
            out["position"] = attrs.get("current_position")
        return out

    # -------------------------------------------------------------- categories
    def category_of(self, entity_id: str) -> list[str]:
        domain = entity_id.split(".", 1)[0]
        cls = self.device_class(entity_id)
        unit = ((self.states.get(entity_id) or {}).get("attributes") or {}).get("unit_of_measurement")
        out = []
        for key, cat in CATEGORIES.items():
            if domain not in cat["domains"]:
                continue
            if key == "temperature":
                if cls == "temperature" or (cls is None and unit in ("°C", "°F")):
                    out.append(key)
            elif key == "running":
                lowered = entity_id.lower() + " " + self.name(entity_id).lower()
                if domain == "binary_sensor" and cls in ("heat", "running") or any(w in lowered for w in HEATING_WORDS):
                    out.append(key)
            elif cat["classes"] is None or cls in cat["classes"]:
                out.append(key)
        return out

    def _caution(self, entity_id: str, category: str) -> str | None:
        lowered = f"{entity_id} {self.name(entity_id)}".lower()
        if category == "temperature":
            if any(w in lowered for w in NOT_ROOM_WORDS):
                return "Probably not a room temperature (the name suggests a device or water temperature)"
            entry = self.registry.get(entity_id) or {}
            device = entry.get("device_id")
            if device and any((self.registry.get(e) or {}).get("device_id") == device and e.startswith("climate.")
                               for e in self.registry):
                return ("Belongs to a thermostat or TRV: it measures next to the heater and can differ from the room. "
                        "An independent room sensor is preferred")
            if any(w in lowered for w in OUTDOOR_WORDS):
                return "Looks like an outdoor sensor"
        return None

    # ------------------------------------------------------------- rooms
    def rooms(self, configured: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Every area with how many climate-relevant entities it has; configured rooms are marked."""
        configured = configured or {}
        counts: dict[str | None, dict[str, int]] = {}
        for entity_id in set(self.states) | set(self.registry):
            if self.hidden(entity_id):
                continue
            cats = self.category_of(entity_id)
            if not cats:
                continue
            bucket = counts.setdefault(self.area_of(entity_id), {})
            for cat in cats:
                bucket[cat] = bucket.get(cat, 0) + 1
        out = []
        for area_id, name in sorted(self.areas.items(), key=lambda a: str(a[1]).casefold()):
            c = counts.get(area_id, {})
            out.append({"area_id": area_id, "name": name, "configured": area_id in configured,
                        "temperature_sensors": c.get("temperature", 0), "thermostats": c.get("thermostats", 0),
                        "openings": c.get("openings", 0), "covers": c.get("covers", 0)})
        return out

    def room_devices(self, area_id: str) -> list[dict[str, Any]]:
        """The devices of one area with their climate-relevant entities, and the area's entities without device."""
        by_device: dict[str | None, list[str]] = {}
        for entity_id in set(self.states) | set(self.registry):
            if self.hidden(entity_id) or self.area_of(entity_id) != area_id or not self.category_of(entity_id):
                continue
            device = (self.registry.get(entity_id) or {}).get("device_id")
            by_device.setdefault(device if device in self.devices else None, []).append(entity_id)
        out = []
        for device_id, entity_ids in by_device.items():
            device = self.devices.get(device_id) or {}
            entities = [{**self.view(e), "categories": self.category_of(e),
                         "caution": next((c for c in (self._caution(e, cat) for cat in self.category_of(e)) if c), None)}
                        for e in sorted(entity_ids, key=lambda e: self.name(e).casefold())]
            out.append({"device_id": device_id,
                        "name": self.device_name(device_id) or ("Entities without a device" if device_id is None else device_id),
                        "model": " ".join(str(v) for v in (device.get("manufacturer"), device.get("model")) if v),
                        "entities": entities})
        out.sort(key=lambda d: (d["device_id"] is None, str(d["name"]).casefold()))
        return out

    def suggest(self, area_id: str) -> dict[str, list[dict[str, Any]]]:
        """Per input of a room: the entities of that area that could serve it, best first. Nothing is chosen."""
        out: dict[str, list[dict[str, Any]]] = {k: [] for k in CATEGORIES}
        for device in self.room_devices(area_id):
            for entity in device["entities"]:
                for cat in entity["categories"]:
                    out[cat].append({**entity, "caution": self._caution(entity["entity_id"], cat)})
        out["temperature"].sort(key=lambda e: (e["caution"] is not None, e["name"].casefold()))
        return out

    def outdoor_candidates(self) -> list[dict[str, Any]]:
        """Outdoor temperature sources: weather entities and temperature sensors that look like outdoor ones."""
        out = []
        for entity_id in sorted(set(self.states) | set(self.registry)):
            if self.hidden(entity_id):
                continue
            if entity_id.startswith("weather."):
                out.append({**self.view(entity_id), "reason": "weather entity"})
            elif "temperature" in self.category_of(entity_id):
                lowered = f"{entity_id} {self.name(entity_id)}".lower()
                if any(w in lowered for w in OUTDOOR_WORDS):
                    out.append({**self.view(entity_id), "reason": "the name suggests an outdoor sensor"})
        out.sort(key=lambda e: (e["domain"] != "sensor", e["name"].casefold()))
        return out

    def search(self, query: str, category: str | None = None, area: str | None = None, limit: int = 60
               ) -> list[dict[str, Any]]:
        tokens = [t for t in query.casefold().split() if t]
        found = []
        for entity_id in set(self.states) | set(self.registry):
            if self.hidden(entity_id):
                continue
            if category == "outdoor":
                if not (entity_id.startswith("weather.") or "temperature" in self.category_of(entity_id)):
                    continue
            elif category == "weather":
                if not entity_id.startswith("weather."):
                    continue
            elif category == "away":
                if entity_id.split(".", 1)[0] not in ("binary_sensor", "input_boolean", "person", "device_tracker",
                                                      "switch", "schedule"):
                    continue
            elif category and category not in self.category_of(entity_id):
                continue
            if area is not None and self.area_of(entity_id) != area:
                continue
            view = self.view(entity_id)
            haystack = " ".join(str(v) for v in (entity_id, view["name"], view["area"], view["device_name"]) if v).casefold()
            if all(t in haystack for t in tokens):
                found.append(view)
        found.sort(key=lambda v: str(v["name"]).casefold())
        return found[:limit]
