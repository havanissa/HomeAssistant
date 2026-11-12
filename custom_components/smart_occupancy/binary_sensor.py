"""Diagnostic binary sensors."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .forecast_entities import RoomPredictedSensor
from .entity import RoomOccupancyEntity, SmartOccupancyEntity, global_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartOccupancyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[BinarySensorEntity] = [MoodMismatchSensor(coordinator)]
    entities += [RoomOccupiedSensor(coordinator, a) for a in sorted(coordinator.config.enabled_rooms)]
    # Occupancy forecast (0.5.0, observation only).
    entities += [RoomPredictedSensor(coordinator, a) for a in sorted(coordinator.config.enabled_rooms)]
    async_add_entities(entities)


class MoodMismatchSensor(SmartOccupancyEntity, BinarySensorEntity):
    """On when Main Mood disagrees with the existing rule beyond the grace period."""

    _attr_translation_key = "mood_mismatch"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        super().__init__(coordinator, global_unique_id("mood_mismatch"))

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data.mood.consistent is None:
            return None  # cannot be judged (helper missing or unavailable)
        return data.mood_mismatch

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        mood = self.coordinator.data.mood
        return {
            "main_mood": mood.main_mood,
            "expected_main_mood": mood.expected_main_mood,
            "grace_seconds": self.coordinator.config.mood.mismatch_grace_seconds,
        }


class RoomOccupiedSensor(RoomOccupancyEntity, BinarySensorEntity):
    """Occupancy computed by this integration's engine (Observe only: nothing acts on it here).

    On for possible occupancy, confirmed occupancy and while vacancy is being
    confirmed. Off when vacant. During an outage of every occupancy sensor it
    keeps its last value; it is unknown only if nothing has ever been known.
    """

    _platform_domain = "binary_sensor"
    _key = "occupied"
    _attr_translation_key = "room_occupied"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def is_on(self) -> bool | None:
        result = self.result
        return result.occupied if result else None
