"""Smart Occupancy & Lighting - Phase 0.1 (Observe only).

This integration reads states and registries and exposes diagnostics. It
does not call any light service, does not modify automations, helpers or
other integrations' entities, and does not poll.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, entity_registry as er

from .const import CONFIG_VERSION, DOMAIN
from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .entity import expected_unique_ids
from .models import IntegrationConfig, rename_entity_references

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    coordinator = SmartOccupancyCoordinator(hass, entry)
    entry.runtime_data = coordinator

    _remove_stale_own_entities(hass, entry, coordinator.config)

    # Restore "first detected" timestamps before the first evaluation.
    await coordinator.tracker.async_load()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start()
    entry.async_on_unload(coordinator.async_stop)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _rename_listener(hass, entry))
    )
    entry.async_on_unload(
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _area_listener(hass, entry))
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.tracker.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
    """Delete this integration's own problem history when it is removed."""
    from .problems import ProblemTracker

    await ProblemTracker(hass).async_remove()


async def async_migrate_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    """Migrate stored configuration between versions."""
    if entry.version > CONFIG_VERSION:
        # Downgrade from a newer release: refuse rather than corrupt.
        _LOGGER.error(
            "Configuration version %s is newer than supported version %s",
            entry.version, CONFIG_VERSION,
        )
        return False
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _remove_stale_own_entities(
    hass: HomeAssistant, entry: SmartOccupancyConfigEntry, config: IntegrationConfig
) -> None:
    """Remove this integration's own entities for rooms that are no longer enabled.

    Only entities created by this config entry are touched.
    """
    ent_reg = er.async_get(hass)
    expected = expected_unique_ids(config)
    for reg_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        if reg_entry.platform == DOMAIN and reg_entry.unique_id not in expected:
            _LOGGER.debug("remove_own_entity entity=%s", reg_entry.entity_id)
            ent_reg.async_remove(reg_entry.entity_id)


def _rename_listener(hass: HomeAssistant, entry: SmartOccupancyConfigEntry):
    """Follow entity id renames so configuration keeps pointing at the right entity."""

    @callback
    def _handle(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        old, new = data["old_entity_id"], data["entity_id"]
        updated = rename_entity_references(dict(entry.options), old, new)
        if updated is None:
            return
        _LOGGER.info("entity_renamed old=%s new=%s (configuration updated)", old, new)
        hass.config_entries.async_update_entry(entry, options=updated)

    return _handle


def _area_listener(hass: HomeAssistant, entry: SmartOccupancyConfigEntry):
    """Reload when an enabled room's area is renamed or removed (entity names)."""

    @callback
    def _handle(event: Event[ar.EventAreaRegistryUpdatedData]) -> None:
        area_id = event.data.get("area_id")
        if event.data["action"] in ("update", "remove") and area_id in entry.runtime_data.config.enabled_rooms:
            hass.config_entries.async_schedule_reload(entry.entry_id)

    return _handle
