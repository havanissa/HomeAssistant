"""TEST HARNESS ONLY: the retired Smart Occupancy & Lighting 0.9.0 integration, assembled for the engine tests.

Phase A6 retired the monolithic integration. Its automation engines (occupancy,
lighting, curtains, forecasts) now live only in the Smart Lighting App
(``smart_lighting/backend/smart_occupancy`` and ``smart_lighting/backend/occupancy_core``);
the deployed ``custom_components/smart_occupancy`` is the minimal companion.

This folder keeps the 0.9.0 *glue* (coordinator, entities, bridges, options
flow, APIs) so that the engine regression suite keeps running the App's engine
modules inside a real (test) Home Assistant: the package path is extended with
the App's engine folder, so ``custom_components.smart_occupancy.controller`` is
the App's ``controller.py``. Nothing in here is installed anywhere; it is the
behavioural oracle of the engines, not a product.
"""

from __future__ import annotations

import importlib as _importlib
import importlib.util as _importlib_util
import pathlib as _pathlib
import sys as _sys

_BACKEND = _pathlib.Path(__file__).resolve().parents[4] / "smart_lighting" / "backend"
__path__.append(str(_BACKEND / "smart_occupancy"))  # the App's engine modules complete this package

if "occupancy_core" not in _sys.modules:
    _spec = _importlib_util.spec_from_file_location(
        "occupancy_core", _BACKEND / "occupancy_core" / "__init__.py",
        submodule_search_locations=[str(_BACKEND / "occupancy_core")])
    _core = _importlib_util.module_from_spec(_spec)
    _sys.modules["occupancy_core"] = _core
    _spec.loader.exec_module(_core)
_sys.modules[__name__ + ".occupancy_core"] = _sys.modules["occupancy_core"]
for _name in ("engine", "golden", "house", "protocol", "types", "wire"):
    _sys.modules[f"{__name__}.occupancy_core.{_name}"] = _importlib.import_module(f"occupancy_core.{_name}")


import logging

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .lighting_config import rename_entity
from .const import DOMAIN
from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .entity import expected_unique_ids
from .models import IntegrationConfig, migrate_options, rename_entity_references
from .presence_log import record_config_change, record_reload

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SELECT]

# Config entry major/minor version. Minor 2 = options payload CONFIG_VERSION 2.
ENTRY_VERSION = 1
ENTRY_MINOR_VERSION = 2

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the dashboard cards and register the websocket API (once per run)."""
    from .curtain_bridge_api import async_register_curtain_service
    from .engine_api import async_register_engine_service
    from .forecast_bridge_api import async_register_forecast_service
    from .lighting_bridge_api import async_register_lighting_service
    from .websocket import async_register_websocket_commands

    async_register_websocket_commands(hass)
    # Phase A3.2: return occupancy control to the integration's engine without the App (administrators only).
    async_register_engine_service(hass)
    # Phase A4: return lighting control to the integration without the App (administrators only).
    async_register_lighting_service(hass)
    # Phase A5: return curtain control and forecast production to the integration without the App.
    async_register_curtain_service(hass)
    async_register_forecast_service(hass)
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
    # Phase A4: who controls lighting. While the Smart Lighting App does, this controller stays dormant.
    await coordinator.lighting_bridge.async_load()
    coordinator.lighting_bridge.async_prepare_controller()
    # Occupancy forecast (0.5.0): settings and the learned daily summaries.
    await coordinator.forecast.async_load()
    # Curtains (0.6.1): windows, presets and the last results. Loading never moves a cover.
    await coordinator.curtains.async_load()
    # Phase A5: who produces the forecasts and who commands the covers. While the App does, the integration's
    # manager stays dormant (it shows the App's published forecasts; it never sends a cover command).
    await coordinator.forecast_bridge.async_load()
    coordinator.forecast_bridge.async_prepare_manager()
    await coordinator.curtain_bridge.async_load()
    coordinator.curtain_bridge.async_prepare_manager()
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_start()
    coordinator.occupancy.async_start()
    if not coordinator.lighting_bridge.app_owns:
        coordinator.lighting.async_start()
    coordinator.lighting_bridge.async_start()
    if not coordinator.forecast_bridge.app_owns:
        coordinator.forecast.async_start()
    coordinator.forecast_bridge.async_start()
    if not coordinator.curtain_bridge.app_owns:
        coordinator.curtains.async_start()
    coordinator.curtain_bridge.async_start()
    entry.async_on_unload(coordinator.async_stop)
    entry.async_on_unload(coordinator.occupancy.async_stop)
    # (The bridges may replace the controller / manager: stop whichever one is current.)
    entry.async_on_unload(lambda: coordinator.lighting.async_stop())
    entry.async_on_unload(coordinator.lighting_bridge.async_stop)
    entry.async_on_unload(lambda: coordinator.forecast.async_stop())
    entry.async_on_unload(coordinator.forecast_bridge.async_stop)
    entry.async_on_unload(lambda: coordinator.curtains.async_stop())
    entry.async_on_unload(coordinator.curtain_bridge.async_stop)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _rename_listener(hass, entry))
    )
    entry.async_on_unload(
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, _area_listener(hass, entry))
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    record_reload(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.tracker.async_flush()
        await entry.runtime_data.occupancy.async_flush()
        await entry.runtime_data.lighting.async_flush()
        # Stopped first: nothing is saved by this bridge after its final write (the next one loads it).
        entry.runtime_data.lighting_bridge.async_stop()
        await entry.runtime_data.lighting_bridge.async_flush()
        await entry.runtime_data.forecast.async_flush()
        await entry.runtime_data.curtains.async_flush()
        entry.runtime_data.forecast_bridge.async_stop()
        await entry.runtime_data.forecast_bridge.async_flush()
        entry.runtime_data.curtain_bridge.async_stop()
        await entry.runtime_data.curtain_bridge.async_flush()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: SmartOccupancyConfigEntry) -> None:
    """Delete this integration's own stored data when it is removed."""
    from homeassistant.helpers.storage import Store

    from .curtain_bridge import MIRROR_STORE_KEY as CURTAINS_MIRROR_KEY, OWNERSHIP_STORE_KEY as CURTAINS_OWNERSHIP_KEY
    from .curtains.manager import CurtainManager
    from .forecast_bridge import MIRROR_STORE_KEY as FORECAST_MIRROR_KEY, OWNERSHIP_STORE_KEY as FORECAST_OWNERSHIP_KEY
    from .const import (
        FORECAST_DAYS_STORE_KEY,
        FORECAST_DAYS_STORE_VERSION,
        FORECAST_SETTINGS_STORE_KEY,
        FORECAST_SETTINGS_STORE_VERSION,
        LIGHTING_STATE_STORE_KEY,
        LIGHTING_STORE_KEY,
    )
    from .lighting_config import LightingStore
    from .occupancy_manager import OccupancyManager
    from .problems import ProblemTracker

    await ProblemTracker(hass).async_remove()
    await OccupancyManager(hass, IntegrationConfig()).async_remove()
    await LightingStore(hass).async_remove()
    await Store(hass, 1, LIGHTING_STATE_STORE_KEY).async_remove()
    await Store(hass, FORECAST_SETTINGS_STORE_VERSION, FORECAST_SETTINGS_STORE_KEY).async_remove()
    await Store(hass, FORECAST_DAYS_STORE_VERSION, FORECAST_DAYS_STORE_KEY).async_remove()
    await CurtainManager(hass).async_remove()
    for key in (CURTAINS_OWNERSHIP_KEY, CURTAINS_MIRROR_KEY, FORECAST_OWNERSHIP_KEY, FORECAST_MIRROR_KEY):
        await Store(hass, 1, key).async_remove()
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
    record_config_change(hass, entry.runtime_data.config, IntegrationConfig.from_dict(dict(entry.options)),
                         applied="reload")
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
        curtain_bridge = entry.runtime_data.curtain_bridge
        if curtain_bridge.app_owns or curtain_bridge.busy or entry.runtime_data.curtains.locked:
            # The App owns the curtain configuration and follows the rename itself (kept for it while it is away);
            # during a change of owner the rename is kept too and applied by whichever side ends up with it.
            curtain_bridge.note_rename(old, new)
        else:
            entry.runtime_data.curtains.rename_entity(old, new)
        store = entry.runtime_data.lighting.store
        if entry.runtime_data.lighting_bridge.app_owns:
            # The Smart Lighting App owns the lighting configuration and follows the rename itself; one made
            # while it is away is handed to it when it resumes (or applied when the integration takes over).
            entry.runtime_data.lighting_bridge.note_rename(old, new)
        elif (lighting := rename_entity(store.config, old, new)) is not None:
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
