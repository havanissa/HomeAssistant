"""Diagnostic button: re-evaluate all diagnostics on demand.

Evaluation is already event driven; this only lets you confirm the current
state immediately (for example after fixing a device). It reads states only.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .entity import SmartOccupancyEntity, global_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartOccupancyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([RecheckButton(entry.runtime_data)])


class RecheckButton(SmartOccupancyEntity, ButtonEntity):
    _attr_translation_key = "recheck"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        super().__init__(coordinator, global_unique_id("recheck"))

    async def async_press(self) -> None:
        await self.coordinator.async_recheck()
