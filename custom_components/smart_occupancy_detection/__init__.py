"""Smart Occupancy Detection: native Home Assistant entities for the Smart Occupancy Detection App.

The App decides room occupancy. This minimal companion only shows the App's
results as native entities, which ordinary automations can use:

* per room: ``binary_sensor`` occupancy (the primary output) and sensors for the
  detailed state, the confidence, the reason and the last activity;
* ``binary_sensor`` App connected (connectivity).

It contains no occupancy engine, registers no service, and cannot command a
device. It accepts a result only from the paired App session that holds the
current lease (``protocol.py``). When the App is away, the entities keep the
last decision (an absent App never makes a room vacant) and show
``fresh: false``. Entity identity is the Home Assistant area id: renaming an
area renames the device, never the entity ids.

It is independent of Smart Comfort's integration (``smart_occupancy``): its own
domain, unique ids, entity ids and storage.
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
from .entity import async_sync_devices
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
    _LOGGER.info("sod_started paired=%s rooms=%d epoch=%d", hub.paired, len(hub.rooms()), hub.epoch)
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
    idents = [i for d, i in device.identifiers if d == DOMAIN]
    if not idents or not idents[0].startswith("room_"):
        return False  # the App device (its connection sensor) stays
    area_id = idents[0].removeprefix("room_")
    if area_id in hub.views or area_id in hub.disabled:
        return False
    if hub.orphaned.pop(area_id, None) is not None:
        await hub.async_flush()
        async_dispatcher_send(hass, SIGNAL_FORGET)  # the platforms forget it: it gets entities again if it returns
    return True
