"""Sensors of every room: detailed state, confidence, reason and last activity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import protocol as P
from .const import SIGNAL_FORGET, SIGNAL_ROOMS
from .entity import RoomEntity
from .hub import Hub

PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    hub: Hub = entry.runtime_data
    created: set[str] = set()

    @callback
    def sync() -> None:
        rooms = [a for a in sorted(hub.rooms()) if a not in created]
        created.update(rooms)
        created.intersection_update(hub.rooms())
        new: list[RoomEntity] = []
        for area_id in rooms:
            new += [RoomStateSensor(hub, area_id), RoomConfidenceSensor(hub, area_id),
                    RoomReasonSensor(hub, area_id), RoomLastActivitySensor(hub, area_id)]
        if new:
            async_add_entities(new)

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS, sync))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_FORGET, sync))


class RoomStateSensor(RoomEntity, SensorEntity):
    _key = "state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(P.STATES)
    _unrecorded_attributes = frozenset({"reason_code", "vacancy_at", "starting_up", "sensor_problems"})

    @property
    def native_value(self) -> str | None:
        view = self.view
        return view["state"] if view else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self.view
        if view is None:
            return None
        return {"fresh": self.hub.fresh, "reason_code": view["reason_code"], "vacancy_at": view["vacancy_at"],
                "starting_up": view["starting_up"], "sensor_problems": view["sensor_problems"]}


class RoomConfidenceSensor(RoomEntity, SensorEntity):
    _key = "confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:gauge"

    @property
    def native_value(self) -> int | None:
        view = self.view
        return view["confidence"] if view else None


class RoomReasonSensor(RoomEntity, SensorEntity):
    _key = "reason"
    _attr_icon = "mdi:comment-question-outline"

    @property
    def native_value(self) -> str | None:
        view = self.view
        if view is None:
            return None
        if not self.hub.fresh:
            return f"The App is not connected; last decision: {view['reason']}"[:255]
        return view["reason"]


class RoomLastActivitySensor(RoomEntity, SensorEntity):
    _key = "last_activity"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        view = self.view
        value = view.get("last_activity") if view else None
        return dt_util.parse_datetime(value) if value else None
