"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, PHASE
from .coordinator import SmartOccupancyCoordinator
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
    ids |= {room_unique_id(a) for a in config.enabled_rooms}
    return ids


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
