"""Binary sensors: whatever binary sensor the App describes for a room, and the App connection."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, NAME, VERSION
from .entity import APP_CONNECTED_UNIQUE_ID, APP_DEVICE, RoomEntity, SpcEntity, async_setup_room_entities
from .hub import Hub

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([AppConnectedSensor(entry.runtime_data)])
    async_setup_room_entities(hass, entry, async_add_entities, "binary_sensor", RoomBinarySensor)


class RoomBinarySensor(RoomEntity, BinarySensorEntity):
    """A binary sensor published by the App. Unavailable while the App is away (never a stale value)."""

    def _describe(self, description: dict[str, Any]) -> None:
        device_class = description["device_class"]
        self._attr_device_class = BinarySensorDeviceClass(device_class) if device_class else None

    def _show(self, shown: dict[str, Any] | None) -> None:
        self._attr_is_on = shown["value"] if shown else None


class AppConnectedSensor(SpcEntity, BinarySensorEntity):
    """On while the App holds a valid lease and published in it (its results are fresh)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "app_connected"

    def __init__(self, hub: Hub) -> None:
        super().__init__(hub, APP_CONNECTED_UNIQUE_ID, DeviceInfo(
            identifiers={(DOMAIN, APP_DEVICE)}, name=NAME, manufacturer=NAME, model="App connection",
            sw_version=VERSION, entry_type=DeviceEntryType.SERVICE))
        self.entity_id = "binary_sensor.spc_app_connected"
        self._update()

    def _update(self) -> Any:
        self._attr_is_on = self.hub.fresh
        self._attr_extra_state_attributes = {"paired": self.hub.paired, "app_version": self.hub.app_version,
                                             "rooms": len(self.hub.views)}
        return (self._attr_is_on, tuple(self._attr_extra_state_attributes.items()))
