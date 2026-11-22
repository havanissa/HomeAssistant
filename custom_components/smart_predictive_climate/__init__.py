"""Smart Adaptive & Predictive Climate: native Home Assistant entities for the App of the same name.

The App observes, learns and predicts room temperatures. This minimal companion
only shows the App's results as native entities, which dashboards and ordinary
automations can use:

* per room: one device with the sensors and binary sensors the App describes
  (generic: the App names them and gives device class, unit and value);
* ``binary_sensor`` App connected (connectivity).

It contains no model, registers no service, and cannot command a device. It
accepts a result only from the paired App session that holds the current lease
(``protocol.py``). When the App is away every room entity is unavailable (a
stale forecast must never look like a recommendation). Entity identity is the
App's room id: renaming a room renames the device, never the entity ids.

It is independent of the other integrations of this repository
(``smart_occupancy``, ``smart_occupancy_detection``): its own domain, unique
ids, entity ids and storage.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.typing import ConfigType

from .api import async_register_api
from .const import DOMAIN, SIGNAL_FORGET, SIGNAL_ROOMS
from .entity import async_sync_devices, device_room
from .hub import Hub

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_api(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hub = Hub(hass, entry)
    await hub.async_load()
    entry.runtime_data = hub
    entry.async_on_unload(hub.async_stop)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def rooms_changed() -> None:
        async_sync_devices(hass, hub)

    rooms_changed()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS, rooms_changed))
    _LOGGER.info("spc_started paired=%s rooms=%d epoch=%d", hub.paired, len(hub.rooms()), hub.epoch)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hub: Hub = entry.runtime_data
        await hub.async_flush()
        hub.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from .const import STORE_KEY, STORE_VERSION  # noqa: PLC0415

    await Store(hass, STORE_VERSION, STORE_KEY).async_remove()


async def async_remove_config_entry_device(hass: HomeAssistant, entry: ConfigEntry,
                                           device: dr.DeviceEntry) -> bool:
    """An administrator may delete the device of a room the App no longer publishes (never a live one)."""
    hub: Hub = entry.runtime_data
    room_id = device_room(device)
    if room_id is None:
        return False  # the App device (its connection sensor) stays
    if room_id in hub.views:
        return False
    if hub.orphaned.pop(room_id, None) is not None:
        await hub.async_flush()
        async_dispatcher_send(hass, SIGNAL_FORGET)  # the platforms forget it: it gets entities again if it returns
    return True
