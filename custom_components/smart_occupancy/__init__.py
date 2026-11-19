"""Smart Occupancy & Lighting companion (Phase A6): the Smart Lighting App's bridge into Home Assistant.

Since 1.0.0 this integration runs no automation. The Smart Lighting App
decides room occupancy, controls lights, switches and curtains and produces
the forecasts, and owns every automation configuration. This companion provides
what an App cannot:

* the native entities (room occupancy, lighting state, daylight, forecasts, the
  built-in mood selector, diagnostics), with the unique ids and entity ids of
  the previous releases;
* a fenced executor: the App's device commands run here, only from the one App
  session that holds the current lease of a domain, only for allowlisted
  services of configured targets, with the App's own context (attribution);
* the ownership records (occupancy, lighting, curtains, forecast), pairing,
  entity rename events and the relay of dashboard-card actions;
* a read-only export of the retired 0.9.0 data for the App's migration, and the
  reviewed retirement and restoration of that data.

It reads nothing from the App's storage and never changes an automation, a
helper or another integration's entity. Nothing happens because the companion
was installed or updated: the App has to be paired and has to claim control.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api import async_register_api
from .card import async_register_card
from .companion import Companion
from .const import DOMAIN, ENTRY_MINOR_VERSION, ENTRY_VERSION
from .entity import expected_unique_ids

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async_register_api(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    companion = Companion(hass, entry)
    entry.runtime_data = companion
    await companion.async_load()
    _remove_stale_own_entities(hass, entry, companion)
    companion.async_start()
    entry.async_on_unload(companion.async_stop)
    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _rename_listener(companion)))
    entry.async_on_unload(hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _area_listener(hass, entry, companion)))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("companion_started version=%s paired=%s rooms=%d", entry.minor_version, companion.paired,
                 len(companion.rooms))
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        companion: Companion = entry.runtime_data
        companion.async_stop()
        await companion.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry) -> None:
    """Delete the companion's own store. The legacy backup (if any) is kept: never deleted automatically."""
    from homeassistant.helpers.storage import Store

    from .const import STORE_KEY, STORE_VERSION

    await Store(hass, STORE_VERSION, STORE_KEY).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry) -> bool:
    """1.2 (0.9.0, monolithic) -> 1.3 (companion). The legacy options are kept untouched until the App's
    migration retires them (``companion/legacy/retire``); nothing else changes."""
    if entry.version > ENTRY_VERSION:
        _LOGGER.error("Configuration version %s is newer than supported version %s", entry.version, ENTRY_VERSION)
        return False
    if entry.minor_version < ENTRY_MINOR_VERSION:
        _LOGGER.info("migrated_entry from=%s.%s to=%s.%s (legacy options kept read-only: %d rooms)", entry.version,
                     entry.minor_version, ENTRY_VERSION, ENTRY_MINOR_VERSION,
                     len((entry.options.get("rooms") or {}) if isinstance(entry.options.get("rooms"), dict) else {}))
        hass.config_entries.async_update_entry(entry, minor_version=ENTRY_MINOR_VERSION)
    return True


def _remove_stale_own_entities(hass: HomeAssistant, entry, companion: Companion) -> None:
    """Remove this integration's own entities for rooms the App no longer publishes (only ours are touched)."""
    ent_reg = er.async_get(hass)
    expected = expected_unique_ids(companion.rooms, bool(companion.mood_select.get("enabled")))
    if "system" not in companion.views:
        return  # no system view published yet (first start after the upgrade): keep every registered entity
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.platform == DOMAIN and reg_entry.unique_id not in expected:
            _LOGGER.debug("remove_own_entity entity=%s", reg_entry.entity_id)
            ent_reg.async_remove(reg_entry.entity_id)


def _rename_listener(companion: Companion):
    @callback
    def _handle(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        companion.note_rename(data["old_entity_id"], data["entity_id"])

    return _handle


def _area_listener(hass: HomeAssistant, entry, companion: Companion):
    @callback
    def _handle(event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        companion.areas_changed()
        area_id = event.data.get("area_id")
        if event.data["action"] in ("update", "remove") and area_id in companion.rooms:
            hass.config_entries.async_schedule_reload(entry.entry_id)  # entity names follow the area name

    return _handle
