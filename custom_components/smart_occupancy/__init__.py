"""Smart Occupancy & Lighting - Phase 0.3 (mood lighting, per-room activation, wall switches).

This integration reads states and registries, computes its own occupancy
per room, exposes diagnostics, and plans mood lighting for every room. Light
commands are sent only in rooms where mood lighting was explicitly enabled;
every other room stays in Observe mode. It never modifies automations or
other integrations' entities, and does not poll. Helpers (including the mood
selectors) are only read, with one explicit exception (0.3.5): a wall switch
button assigned to "Select mood (all rooms)" selects that option on the
Manual Mood selector when a person presses it. Main Mood is never written.
Wall switch presses arrive through Home Assistant's MQTT integration
(subscriptions to the assigned switches' topics only), MQTT device triggers or
event entities; nothing is ever published.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .card import async_register_card
from .lighting_config import rename_entity
from .const import DOMAIN
from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .entity import expected_unique_ids
from .models import IntegrationConfig, migrate_options, rename_entity_references

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON]

# Config entry major/minor version. Minor 2 = options payload CONFIG_VERSION 2.
ENTRY_VERSION = 1
ENTRY_MINOR_VERSION = 2

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard cards and register the websocket API (once per run)."""
    from .websocket import async_register_websocket_commands

    async_register_websocket_commands(hass)
    await async_register_card(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    coordinator = SmartOccupancyCoordinator(hass, entry)
    entry.runtime_data = coordinator

    _remove_stale_own_entities(hass, entry, coordinator.config)

    # Restore "first detected" timestamps and the previous occupancy decisions
    # before the first evaluation.
    await coordinator.tracker.async_load()
    await coordinator.occupancy.async_load()
    # Mood lighting configuration (seeded from the options on first start).
    await coordinator.lighting.async_load()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start()
    coordinator.occupancy.async_start()
    coordinator.lighting.async_start()
    entry.async_on_unload(coordinator.async_stop)
    entry.async_on_unload(coordinator.occupancy.async_stop)
    entry.async_on_unload(coordinator.lighting.async_stop)

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
        await entry.runtime_data.occupancy.async_flush()
        await entry.runtime_data.lighting.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
    """Delete this integration's own stored data when it is removed."""
    from homeassistant.helpers.storage import Store

    from .const import LIGHTING_STATE_STORE_KEY, LIGHTING_STORE_KEY
    from .lighting_config import LightingStore
    from .occupancy_manager import OccupancyManager
    from .problems import ProblemTracker

    await ProblemTracker(hass).async_remove()
    await OccupancyManager(hass, IntegrationConfig()).async_remove()
    await LightingStore(hass).async_remove()
    await Store(hass, 1, LIGHTING_STATE_STORE_KEY).async_remove()
    _LOGGER.debug("removed_storage keys=%s,%s", LIGHTING_STORE_KEY, LIGHTING_STATE_STORE_KEY)


async def async_migrate_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    """Migrate stored configuration between versions.

    1.1 -> 1.2 (Phase 0.1.x -> 0.2): adds occupancy timing, adjacent rooms and
    inverted signals with defaults. Everything already configured is kept.
    Minor versions are backwards compatible, so 0.1.1 can still load a 1.2
    entry after a downgrade (it ignores the new keys).
    """
    if entry.version > ENTRY_VERSION:
        # Downgrade from a newer major release: refuse rather than corrupt.
        _LOGGER.error(
            "Configuration version %s is newer than supported version %s",
            entry.version, ENTRY_VERSION,
        )
        return False
    if (old_minor := entry.minor_version) < ENTRY_MINOR_VERSION:
        options = migrate_options(dict(entry.options))
        hass.config_entries.async_update_entry(entry, options=options, minor_version=ENTRY_MINOR_VERSION)
        _LOGGER.info(
            "migrated_configuration from=%s.%s to=%s.%s rooms=%d",
            entry.version, old_minor, ENTRY_VERSION, ENTRY_MINOR_VERSION, len(options.get("rooms", {})),
        )
    return True


async def _async_options_updated(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
    # Presence settings saved from the room card (0.3.8) are applied live; every other change reloads.
    if entry.runtime_data.consume_live_options(dict(entry.options)):
        return
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
        store = entry.runtime_data.lighting.store
        if (lighting := rename_entity(store.config, old, new)) is not None:
            _LOGGER.info("entity_renamed old=%s new=%s (lighting configuration updated)", old, new)
            store.async_set(lighting)
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
