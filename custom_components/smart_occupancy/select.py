"""The built-in mood selector: one ``select`` whose options are the moods defined in the App.

The App reads this entity's state like any Main Mood helper; automations,
dashboards and voice assistants change the mood with ``select.select_option``.
The selected mood survives restarts. The entity exists only while the App's
mood source is the built-in selector (published in the system view).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .companion import Companion
from .const import BUILTIN_MOOD_ENTITY, SIGNAL_ROOMS_CHANGED
from .entity import CompanionEntity, global_unique_id
from .sensor import _remove

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    companion: Companion = entry.runtime_data
    created: list[BuiltinMoodSelect] = []

    @callback
    def sync() -> None:
        wanted = bool(companion.mood_select.get("enabled"))
        if wanted and not created:
            created.append(BuiltinMoodSelect(companion))
            async_add_entities([created[0]])
        elif not wanted and created:
            hass.async_create_task(_remove(hass, created.pop()))

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS_CHANGED, sync))


class BuiltinMoodSelect(CompanionEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "mood"
    _view_domain = "system"

    def __init__(self, companion: Companion) -> None:
        super().__init__(companion, global_unique_id("mood"))
        self.entity_id = BUILTIN_MOOD_ENTITY  # suggested; once registered it stays
        self._current: str | None = None

    def _entity_view(self):
        return {"value": self._current, "attributes": {}, "available": True}

    @property
    def options(self) -> list[str]:
        return list(self.companion.mood_select.get("options") or [])

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
