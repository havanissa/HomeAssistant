"""The generic sensor: whatever sensor the App describes for a room (a forecast temperature, a time, a text)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import RoomEntity, async_setup_room_entities

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_setup_room_entities(hass, entry, async_add_entities, "sensor", RoomSensor)


class RoomSensor(RoomEntity, SensorEntity):
    """A sensor published by the App. Unavailable while the App is away (never a stale value)."""

    def _hidden_attributes(self) -> tuple[str, ...]:
        # An enum's options are the entity's own "options" attribute already.
        return ("options",) if self._classes and self._classes[0] == "enum" else ()

    def _describe(self, description: dict[str, Any]) -> None:
        device_class = description["device_class"]
        self._attr_device_class = SensorDeviceClass(device_class) if device_class else None
        self._attr_native_unit_of_measurement = description["unit"]
        self._attr_state_class = SensorStateClass(description["state_class"]) if description["state_class"] else None
        self._attr_options = list(description["attributes"]["options"]) if device_class == "enum" else None

    def _show(self, shown: dict[str, Any] | None) -> None:
        value = shown["value"] if shown else None
        if value is not None and shown["device_class"] == "timestamp":
            value = datetime.fromisoformat(value)
        self._attr_native_value = value
