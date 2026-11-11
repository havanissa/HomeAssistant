"""Shared entity base."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, NAME, PHASE
from .coordinator import SmartOccupancyCoordinator
from .discovery import area_name
from .engine import RoomResult
from .models import IntegrationConfig

GLOBAL_SENSOR_KEYS = (
    "status",
    "operating_mode",
    "effective_mood",
    "configured_rooms",
    "light_categories",
    "discovered_sensors",
    "missing_entities",
    "unavailable_entities",
    "config_problems",
)
GLOBAL_BINARY_SENSOR_KEYS = ("mood_mismatch",)
GLOBAL_BUTTON_KEYS = ("recheck",)

# Per-room occupancy entities: key -> (platform, translation key).
ROOM_OCCUPANCY_ENTITIES: dict[str, tuple[str, str]] = {
    "occupied": ("binary_sensor", "room_occupied"),
    "state": ("sensor", "room_state"),
    "confidence": ("sensor", "room_confidence"),
    "last_activity": ("sensor", "room_last_activity"),
    "reason": ("sensor", "room_reason"),
}
# Only for rooms with an existing occupancy helper configured.
ROOM_COMPARISON_ENTITY: tuple[str, str, str] = ("helper_comparison", "sensor", "room_helper_comparison")
# Mood lighting state and diagnostics of every enabled room (Phase 0.3).
ROOM_LIGHTING_ENTITY: tuple[str, str, str] = ("mood_lighting", "sensor", "room_mood_lighting")


def global_unique_id(key: str) -> str:
    # Single-instance integration: unique ids are stable across re-installs.
    return f"{DOMAIN}_{key}"


def room_unique_id(area_id: str) -> str:
    # area_id is stable when an area is renamed.
    return f"{DOMAIN}_room_{area_id}_observation"


def expected_unique_ids(config: IntegrationConfig) -> set[str]:
    ids = {
        global_unique_id(k)
        for k in GLOBAL_SENSOR_KEYS + GLOBAL_BINARY_SENSOR_KEYS + GLOBAL_BUTTON_KEYS
    }
    for area_id, room in config.enabled_rooms.items():
        ids.add(room_unique_id(area_id))
        ids |= {room_entity_unique_id(area_id, key) for key in ROOM_OCCUPANCY_ENTITIES}
        ids.add(room_entity_unique_id(area_id, ROOM_LIGHTING_ENTITY[0]))
        if room.reference_occupancy_entity:
            ids.add(room_entity_unique_id(area_id, ROOM_COMPARISON_ENTITY[0]))
    return ids


def room_entity_unique_id(area_id: str, key: str) -> str:
    """Occupancy entities of a room. Keyed by area_id: stable across area renames."""
    return f"{DOMAIN}_room_{area_id}_{key}"


class SmartOccupancyEntity(CoordinatorEntity[SmartOccupancyCoordinator]):
    """Base entity attached to the integration's service device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SmartOccupancyCoordinator, unique_id: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "observer")},
            name=NAME,
            manufacturer=NAME,
            model=f"Phase {PHASE}",
            entry_type=DeviceEntryType.SERVICE,
        )


class RoomOccupancyEntity(SmartOccupancyEntity):
    """Base of a room's occupancy entities.

    Updated by the occupancy engine (immediately, per evaluation) and by the
    diagnostics coordinator (health). The state is written only when the
    published state or attributes actually change.
    """

    _platform_domain: str
    _key: str

    def __init__(self, coordinator: SmartOccupancyCoordinator, area_id: str) -> None:
        super().__init__(coordinator, room_entity_unique_id(area_id, self._key))
        self._area_id = area_id
        room_name = area_name(coordinator.hass, area_id) or area_id
        self._attr_translation_placeholders = {"room": room_name}
        # Suggested entity id, e.g. sensor.smart_occupancy_kamar_nordur_state.
        # Once registered, the entity id stays the same when the area is renamed.
        self.entity_id = f"{self._platform_domain}.{DOMAIN}_{slugify(room_name) or slugify(area_id)}_{self._key}"
        self._published: Any = None

    @property
    def result(self) -> RoomResult | None:
        return self.coordinator.occupancy.result(self._area_id)

    @property
    def available(self) -> bool:
        return super().available and self.result is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.occupancy.async_add_listener(self._area_id, self._write_if_changed)
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._write_if_changed()

    def _fingerprint(self) -> Any:
        return (self.available, self.state, self.extra_state_attributes)

    @callback
    def _write_if_changed(self) -> None:
        if self.hass is None:
            return
        fingerprint = self._fingerprint()
        if fingerprint != self._published:
            self._published = fingerprint
            self.async_write_ha_state()
