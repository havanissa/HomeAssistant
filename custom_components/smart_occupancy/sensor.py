"""Diagnostic sensors.

Problem details are exposed as attributes so they can be read in the UI
(entity → Attributes, or Developer tools → States) without opening logs.

Recorder load is kept low: states change only when the observed situation
changes, list-valued attributes and timestamps are excluded from the
Recorder, and "last checked" is republished at most every
LAST_CHECKED_REFRESH_MINUTES while problems exist. No sensor has a
state_class, so no long-term statistics are written.
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

from .const import PHASE, Classification, OperatingMode, ProblemCategory, Severity
from .coordinator import (
    CONFIGURATION_INVALID,
    OPERATIONAL_UNAVAILABLE,
    STATUS_OPTIONS,
    Snapshot,
    SmartOccupancyConfigEntry,
    SmartOccupancyCoordinator,
)
from .discovery import area_name
from .entity import SmartOccupancyEntity, global_unique_id, room_unique_id
from .occupancy import AVAILABLE, PENDING

PARALLEL_UPDATES = 0

# Never recorded: lists of details and timestamps that change with every check.
UNRECORDED = frozenset({
    "rooms", "categories", "sensors", "entities", "items", "problems", "notes", "last_checked",
})


def _issues(s: Snapshot, healths: frozenset[str]) -> dict[str, Any]:
    entities = s.entity_issues(healths)
    return {
        "items": [Snapshot.issue_line(i) for i in entities],
        "entities": entities,
        "last_checked": s.last_checked,
    }


def _config_problems(s: Snapshot) -> dict[str, Any]:
    config = s.problems_where(category=ProblemCategory.CONFIGURATION, include_info=True)
    return {
        "errors": sum(1 for p in config if p.severity == Severity.ERROR),
        "warnings": sum(1 for p in config if p.severity == Severity.WARNING),
        "problems": [s.problem_dict(p) for p in config if p.severity != Severity.INFO],
        "notes": [s.problem_dict(p) for p in config if p.severity == Severity.INFO],
        "last_checked": s.last_checked,
    }


@dataclass(frozen=True, kw_only=True)
class SmartSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Snapshot], Any]
    attrs_fn: Callable[[Snapshot], dict[str, Any]] | None = None


SENSORS: tuple[SmartSensorDescription, ...] = (
    SmartSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=STATUS_OPTIONS,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.status.value,
        attrs_fn=lambda s: {
            "summary": s.summary(),
            "phase": PHASE,
            "effective_mode": s.effective_mode.value,
            "config_errors": s.count(Classification.CONFIG_ERROR),
            "unavailable": s.count(Classification.UNAVAILABLE),
            "warnings": s.count(Classification.WARNING),
            "problems": [s.problem_dict(p) for p in s.problems_where()],
            "last_checked": s.last_checked,
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
        attrs_fn=lambda s: {"rooms": {r.name: r.status.value for r in s.rooms}},
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
    ),
    SmartSensorDescription(
        key="missing_entities",
        translation_key="missing_entities",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.entity_issues(CONFIGURATION_INVALID)),
        attrs_fn=lambda s: _issues(s, CONFIGURATION_INVALID),
    ),
    SmartSensorDescription(
        key="unavailable_entities",
        translation_key="unavailable_entities",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.entity_issues(OPERATIONAL_UNAVAILABLE)),
        attrs_fn=lambda s: _issues(s, OPERATIONAL_UNAVAILABLE),
    ),
    SmartSensorDescription(
        key="config_problems",
        translation_key="config_problems",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: len(s.problems_where(category=ProblemCategory.CONFIGURATION)),
        attrs_fn=_config_problems,
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
    # Home Assistant reads unrecorded attributes at class level.
    _unrecorded_attributes = UNRECORDED

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
    """Diagnostic health of one room. Not an occupancy state."""

    _attr_translation_key = "room_observation"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({
        "reasons", "sensors_available", "sensors_unavailable", "lights_available",
        "lights_unavailable", "pending", "problems", "last_checked",
    })

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
        return room.status.value if room else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        room = self._room()
        if room is None:
            return None
        data = self.coordinator.data

        def split(items):
            ok = [i.as_dict() for i in items if i.health == AVAILABLE]
            bad = [i.as_dict() for i in items if i.health not in (AVAILABLE, PENDING)]
            return ok, bad

        sensors_ok, sensors_bad = split(room.sensors)
        lights_ok, lights_bad = split(room.lights)
        return {
            "area_id": room.area_id,
            "operating_mode": data.effective_mode.value,
            "configuration_valid": room.configuration_valid,
            "usable": room.usable,
            "reasons": list(room.reasons),
            "sensors_available": sensors_ok,
            "sensors_unavailable": sensors_bad,
            "lights_available": lights_ok,
            "lights_unavailable": lights_bad,
            "pending": [i.entity_id for i in (*room.sensors, *room.lights) if i.health == PENDING],
            "reference_occupancy": room.reference_occupancy.as_dict() if room.reference_occupancy else None,
            "problems": [data.problem_dict(p) for p in data.problems_where(area_id=room.area_id)],
            "last_checked": data.last_checked,
        }
