"""The built-in mood selector (Phase A4).

For installations without mood helpers: one ``select`` entity whose options are
the moods defined in the Smart Lighting App (plus the Manual option, which pauses
automatic lighting). It is the Main Mood of the mood source ``builtin``, so the
lighting rules read it exactly like a Main Mood helper, and automations,
dashboards and voice assistants change the mood with ``select.select_option``.
The selected mood survives restarts. Nothing else is created; with helpers or an
existing selector as the mood source this platform adds no entity.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import BUILTIN_MOOD_ENTITY
from .coordinator import SmartOccupancyConfigEntry, SmartOccupancyCoordinator
from .entity import SmartOccupancyEntity, global_unique_id

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartOccupancyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    if coordinator.config.mood.builtin:
        async_add_entities([BuiltinMoodSelect(coordinator)])


class BuiltinMoodSelect(SmartOccupancyEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "mood"

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        super().__init__(coordinator, global_unique_id("mood"))
        self.entity_id = BUILTIN_MOOD_ENTITY  # suggested; a rename is followed by the options
        self._current: str | None = None

    @property
    def options(self) -> list[str]:
        moods = list(self.coordinator.config.mood.builtin or ())
        manual = self.coordinator.lighting.lighting.manual_option
        if manual and manual.casefold() not in {m.casefold() for m in moods}:
            moods.append(manual)
        return moods

    @property
    def current_option(self) -> str | None:
        options = self.options
        if self._current in options:
            return self._current
        return options[0] if options else None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state in self.options:
            self._current = last.state

    async def async_select_option(self, option: str) -> None:
        self._current = option
        self.async_write_ha_state()
