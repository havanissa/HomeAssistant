"""Binary sensors of the companion: room occupancy, predicted occupancy and the mood mismatch."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .companion import Companion
from .const import SIGNAL_ROOMS_CHANGED
from .entity import CompanionEntity, RoomEntity, global_unique_id
from .sensor import _remove

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    companion: Companion = entry.runtime_data
    async_add_entities([MoodMismatchSensor(companion)])
    created: dict[str, list[RoomEntity]] = {}

    @callback
    def sync() -> None:
        rooms = companion.rooms
        new: list[RoomEntity] = []
        for area_id in sorted(rooms):
            if area_id not in created:
                created[area_id] = [RoomOccupiedSensor(companion, area_id), RoomPredictedSensor(companion, area_id)]
                new.extend(created[area_id])
        if new:
            async_add_entities(new)
        for area_id in [a for a in created if a not in rooms]:
            for entity in created.pop(area_id):
                hass.async_create_task(_remove(hass, entity))

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS_CHANGED, sync))


class MoodMismatchSensor(CompanionEntity, BinarySensorEntity):
    _view_domain = "system"
    _attr_translation_key = "mood_mismatch"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, companion: Companion) -> None:
        super().__init__(companion, global_unique_id("mood_mismatch"))

    def _entity_view(self) -> dict[str, Any] | None:
        return (self.companion.view("system") or {}).get("mood_mismatch")

    @property
    def is_on(self) -> bool | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        return dict(view.get("attributes") or {}) if view else None


class RoomOccupiedSensor(RoomEntity, BinarySensorEntity):
    """Occupancy decided by the App's engine. Keeps its last value while the App is away (never inferred)."""

    _platform_domain = "binary_sensor"
    _key = "occupied"
    _view_domain = "occupancy"
    _attr_translation_key = "room_occupied"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def is_on(self) -> bool | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        return {**dict(view.get("attributes") or {}), "fresh": self.fresh}


class RoomPredictedSensor(RoomEntity, BinarySensorEntity):
    _platform_domain = "binary_sensor"
    _key = "predicted"
    _view_domain = "forecast"
    _attr_translation_key = "room_predicted"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:crystal-ball"
    _unrecorded_attributes = frozenset({"explanation"})

    @property
    def is_on(self) -> bool | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if isinstance(value, bool) else None
