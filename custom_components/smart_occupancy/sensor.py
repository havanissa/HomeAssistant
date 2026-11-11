"""Diagnostic sensors.

Recorder load is kept low: states change only when the observed situation
changes, and list-valued attributes are excluded from the Recorder. No
sensor has a state_class, so no long-term statistics are written.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PHASE, OperatingMode, Severity
from .coordinator import (
    ROOM_AREA_MISSING,
    ROOM_DEGRADED,
    ROOM_NOT_CONFIGURED,
    ROOM_OK,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_WARNING,
    Snapshot,
    SmartOccupancyConfigEntry,
    SmartOccupancyCoordinator,
)
from .discovery import area_name
from .entity import SmartOccupancyEntity, global_unique_id, room_unique_id

PARALLEL_UPDATES = 0


def _count(sev: Severity, s: Snapshot) -> int:
    return sum(1 for p in s.problems if p.severity == sev)


@dataclass(frozen=True, kw_only=True)
class SmartSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Snapshot], Any]
    attrs_fn: Callable[[Snapshot], dict[str, Any]] | None = None
    unrecorded: frozenset[str] = frozenset()


SENSORS: tuple[SmartSensorDescription, ...] = (
    SmartSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=[STATUS_OK, STATUS_WARNING, STATUS_ERROR],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.status,
        attrs_fn=lambda s: {
            "phase": PHASE,
            "effective_mode": s.effective_mode.value,
            "errors": _count(Severity.ERROR, s),
            "warnings": _count(Severity.WARNING, s),
            "info": _count(Severity.INFO, s),
        },
    ),
    SmartSensorDescription(
        key="operating_mode",
        translation_key="operating_mode",
        device_class=SensorDeviceClass.ENUM,
        options=[m.value for m in OperatingMode],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.effective_mode.value,
        attrs_fn=lambda s: {
            "configured_mode": s.configured_mode.value,
            "lighting_control": False,
        },
    ),
    SmartSensorDescription(
        key="effective_mood",
        translation_key="effective_mood",
        value_fn=lambda s: s.mood.main_mood,
        attrs_fn=lambda s: {
            "auto_mood": s.mood.auto_mood,
            "manual_mood": s.mood.manual_mood,
            "expected_main_mood": s.mood.expected_main_mood,
            "follows": s.mood.source,
            "consistent": s.mood.consistent,
        },
    ),
    SmartSensorDescription(
        key="configured_rooms",
        translation_key="configured_rooms",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.rooms),
        attrs_fn=lambda s: {"rooms": {r.name: r.status for r in s.rooms}},
        unrecorded=frozenset({"rooms"}),
    ),
    SmartSensorDescription(
        key="light_categories",
        translation_key="light_categories",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.categories),
        attrs_fn=lambda s: {
            "categories": {
                c.name: {"label": c.label_name or c.label_id, "lights": len(c.lights)}
                for c in s.categories
            }
        },
        unrecorded=frozenset({"categories"}),
    ),
    SmartSensorDescription(
        key="discovered_sensors",
        translation_key="discovered_sensors",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.discovered_signals),
        attrs_fn=lambda s: {
            "sensors": [
                {"area": a, "entity_id": e, "suggested_kind": k}
                for a, e, k in s.discovered_signals
            ]
        },
        unrecorded=frozenset({"sensors"}),
    ),
    SmartSensorDescription(
        key="missing_entities",
        translation_key="missing_entities",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.missing_entities),
        attrs_fn=lambda s: {"entities": list(s.missing_entities)},
        unrecorded=frozenset({"entities"}),
    ),
    SmartSensorDescription(
        key="unavailable_entities",
        translation_key="unavailable_entities",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.unavailable_entities),
        attrs_fn=lambda s: {"entities": list(s.unavailable_entities)},
        unrecorded=frozenset({"entities"}),
    ),
    SmartSensorDescription(
        key="config_problems",
        translation_key="config_problems",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: sum(1 for p in s.problems if p.severity != Severity.INFO),
        attrs_fn=lambda s: {"problems": [p.as_dict() for p in s.problems]},
        unrecorded=frozenset({"problems"}),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SmartOccupancyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [SmartSensor(coordinator, d) for d in SENSORS]
    entities += [
        RoomObservationSensor(coordinator, area_id)
        for area_id in sorted(coordinator.config.enabled_rooms)
    ]
    async_add_entities(entities)


class SmartSensor(SmartOccupancyEntity, SensorEntity):
    entity_description: SmartSensorDescription
    # Home Assistant reads unrecorded attributes at class level, so the union of
    # every description's list-valued attributes is declared here.
    _unrecorded_attributes = frozenset().union(*(d.unrecorded for d in SENSORS))

    def __init__(self, coordinator: SmartOccupancyCoordinator, description: SmartSensorDescription) -> None:
        super().__init__(coordinator, global_unique_id(description.key))
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class RoomObservationSensor(SmartOccupancyEntity, SensorEntity):
    """Health of a room's configured signals and lights. Not an occupancy state."""

    _attr_translation_key = "room_observation"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [ROOM_OK, ROOM_DEGRADED, ROOM_NOT_CONFIGURED, ROOM_AREA_MISSING]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"signals", "lights"})

    def __init__(self, coordinator: SmartOccupancyCoordinator, area_id: str) -> None:
        super().__init__(coordinator, room_unique_id(area_id))
        self._area_id = area_id
        self._attr_translation_placeholders = {
            "room": area_name(coordinator.hass, area_id) or area_id
        }

    def _room(self):
        return next((r for r in self.coordinator.data.rooms if r.area_id == self._area_id), None)

    @property
    def native_value(self) -> str | None:
        room = self._room()
        return room.status if room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        room = self._room()
        if room is None:
            return None
        return {
            "area_id": room.area_id,
            "problems": room.problem_count,
            "signals": [s.as_dict() for s in room.signals],
            "lights": [
                {"entity_id": e, "health": h, "categories": list(c)} for e, h, c in room.lights
            ],
            "reference_occupancy": room.reference_occupancy[0] if room.reference_occupancy else None,
        }
