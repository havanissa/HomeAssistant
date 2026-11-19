"""Diagnostic button: ask the App to re-evaluate its diagnostics now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .companion import Companion
from .entity import CompanionEntity, global_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([RecheckButton(entry.runtime_data)])


class RecheckButton(CompanionEntity, ButtonEntity):
    _attr_translation_key = "recheck"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, companion: Companion) -> None:
        super().__init__(companion, global_unique_id("recheck"))

    def _entity_view(self):
        return {"value": None, "attributes": {}, "available": True}

    async def async_press(self) -> None:
        self.companion.recheck()
