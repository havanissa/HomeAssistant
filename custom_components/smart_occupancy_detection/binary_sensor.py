"""Binary sensors: room occupancy (the primary output) and the App connection."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, NAME, SIGNAL_FORGET, SIGNAL_ROOMS, VERSION
from .entity import APP_DEVICE, RoomEntity, SodEntity
from .hub import Hub

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub: Hub = entry.runtime_data
    async_add_entities([AppConnectedSensor(hub)])
    created: set[str] = set()

    @callback
    def sync() -> None:
        new = [RoomOccupiedSensor(hub, a) for a in sorted(hub.rooms()) if a not in created]
        created.update(e.area_id for e in new)
        created.intersection_update(hub.rooms())
        if new:
            async_add_entities(new)

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS, sync))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_FORGET, sync))


class RoomOccupiedSensor(RoomEntity, BinarySensorEntity):
    """Occupied / vacant as the App decided. Keeps the last decision while the App is away (never inferred)."""

    _key = "occupied"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def is_on(self) -> bool | None:
        view = self.view
        return view.get("occupied") if view else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self.view
        if view is None:
            return None
        # Only what changes with the decision itself: the details are on the state sensor (not recorded).
        return {"state": view["state"], "fresh": self.hub.fresh}


class AppConnectedSensor(SodEntity, BinarySensorEntity):
    """On while the App holds a valid lease (its results are fresh)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "app_connected"

    def __init__(self, hub: Hub) -> None:
        super().__init__(hub, "app_connected", DeviceInfo(
            identifiers={(DOMAIN, APP_DEVICE)}, name=NAME, manufacturer=NAME, model="App connection",
            sw_version=VERSION, entry_type=DeviceEntryType.SERVICE))
        self.entity_id = "binary_sensor.sod_app_connected"

    @property
    def is_on(self) -> bool:
        return self.hub.fresh

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"paired": self.hub.paired, "app_version": self.hub.app_version, "rooms": len(self.hub.views)}
