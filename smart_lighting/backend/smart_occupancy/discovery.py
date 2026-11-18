"""Discovery of areas and of the entities inside them.

Everything is read from the area, device and entity registries at runtime.
Suggestions are based on domain and device class, never on entity names.
They are only suggestions: the user confirms every assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

from .const import DOMAIN, PILOT_AREA_NAME, SensorKind

# binary_sensor device class -> suggested kind.
# Note: many Zigbee PIR sensors report "occupancy" just like radars, and bed
# sensors often do too, so the PIR/radar/bed split must be confirmed by the user.
_SUGGESTED_KIND: dict[str, SensorKind] = {
    BinarySensorDeviceClass.MOTION: SensorKind.PIR,
    BinarySensorDeviceClass.OCCUPANCY: SensorKind.RADAR,
    BinarySensorDeviceClass.PRESENCE: SensorKind.RADAR,
    BinarySensorDeviceClass.DOOR: SensorKind.DOOR,
    BinarySensorDeviceClass.GARAGE_DOOR: SensorKind.DOOR,
}
# Reported in discovery but not pre-assigned.
_INFORMATIONAL_CLASSES = {
    BinarySensorDeviceClass.WINDOW,
    BinarySensorDeviceClass.OPENING,
}


@dataclass(slots=True, frozen=True)
class DiscoveredEntity:
    entity_id: str
    domain: str
    device_class: str | None
    suggested_kind: SensorKind | None
    name: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "device_class": self.device_class,
            "suggested_kind": self.suggested_kind.value if self.suggested_kind else None,
            "name": self.name,
        }


@dataclass(slots=True, frozen=True)
class AreaDiscovery:
    area_id: str
    name: str
    signals: tuple[DiscoveredEntity, ...]
    other_binary_sensors: tuple[DiscoveredEntity, ...]
    lights: tuple[str, ...]

    def suggested(self, kind: SensorKind) -> list[str]:
        return [e.entity_id for e in self.signals if e.suggested_kind == kind]

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "signals": [e.as_dict() for e in self.signals],
            "other_binary_sensors": [e.as_dict() for e in self.other_binary_sensors],
            "lights": list(self.lights),
        }


def list_areas(hass: HomeAssistant) -> list[ar.AreaEntry]:
    return sorted(ar.async_get(hass).async_list_areas(), key=lambda a: a.name.casefold())


def area_name(hass: HomeAssistant, area_id: str) -> str | None:
    area = ar.async_get(hass).async_get_area(area_id)
    return area.name if area else None


def find_pilot_area(hass: HomeAssistant) -> str | None:
    """Area id of the pilot room, matched by area name (not entity names)."""
    area = ar.async_get(hass).async_get_area_by_name(PILOT_AREA_NAME)
    return area.id if area else None


def entity_area_id(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    """Area of an entity: its own area, else its device's area."""
    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        return device.area_id
    return None


def _device_class(hass: HomeAssistant, entry: er.RegistryEntry) -> str | None:
    if entry.device_class or entry.original_device_class:
        return entry.device_class or entry.original_device_class
    state = hass.states.get(entry.entity_id)
    return state.attributes.get("device_class") if state else None


def discover_area(hass: HomeAssistant, area_id: str) -> AreaDiscovery:
    """Discover signal candidates and lights in one area."""
    signals: list[DiscoveredEntity] = []
    others: list[DiscoveredEntity] = []
    lights: list[str] = []
    for entry in er.async_get(hass).entities.values():
        if entry.disabled_by is not None or entry.platform == DOMAIN:
            continue
        if entry.domain not in ("binary_sensor", "light"):
            continue
        if entity_area_id(hass, entry) != area_id:
            continue
        if entry.domain == "light":
            lights.append(entry.entity_id)
            continue
        if entry.entity_category is not None:
            continue  # diagnostic/config entities (tamper, battery, ...)
        device_class = _device_class(hass, entry)
        disc = DiscoveredEntity(
            entity_id=entry.entity_id,
            domain=entry.domain,
            device_class=device_class,
            suggested_kind=_SUGGESTED_KIND.get(device_class) if device_class else None,
            name=entry.name or entry.original_name,
        )
        if disc.suggested_kind is not None:
            signals.append(disc)
        elif device_class in _INFORMATIONAL_CLASSES:
            others.append(disc)
    return AreaDiscovery(
        area_id=area_id,
        name=area_name(hass, area_id) or area_id,
        signals=tuple(sorted(signals, key=lambda e: e.entity_id)),
        other_binary_sensors=tuple(sorted(others, key=lambda e: e.entity_id)),
        lights=tuple(sorted(lights)),
    )
