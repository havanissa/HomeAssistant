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
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import PHASE, Classification, OccupancyState, OperatingMode, ProblemCategory, Severity
from .coordinator import (
    CONFIGURATION_INVALID,
    OPERATIONAL_UNAVAILABLE,
    STATUS_OPTIONS,
    Snapshot,
    SmartOccupancyConfigEntry,
    SmartOccupancyCoordinator,
)
from .discovery import area_name
from .engine import SignalStatus
from .entity import (
    ROOM_COMPARISON_ENTITY,
    ROOM_DAYLIGHT_ENTITY,
    ROOM_LIGHTING_ENTITY,
    ROOM_OCCUPANCY_ENTITIES,
    RoomOccupancyEntity,
    SmartOccupancyEntity,
    global_unique_id,
    room_entity_unique_id,
    room_unique_id,
)
from .health import room_health
from .occupancy import AVAILABLE, PENDING
from .const import SIGNAL_LIGHTING_UPDATED, Darkness, LightingMode

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
            # The integration-wide mode stays Observe; mood lighting is enabled per room.
            "lighting_control": bool(s.mood_lighting_rooms),
            "mood_lighting_rooms": list(s.mood_lighting_rooms),
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
    for area_id, room in sorted(coordinator.config.enabled_rooms.items()):
        entities += [
            RoomObservationSensor(coordinator, area_id),
            RoomStateSensor(coordinator, area_id),
            RoomConfidenceSensor(coordinator, area_id),
            RoomLastActivitySensor(coordinator, area_id),
            RoomReasonSensor(coordinator, area_id),
        ]
        if room.reference_occupancy_entity:
            entities.append(RoomHelperComparisonSensor(coordinator, area_id))
        entities.append(RoomMoodLightingSensor(coordinator, area_id))
        entities.append(RoomDaylightSensor(coordinator, area_id))
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


def _iso(value) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class RoomStateSensor(RoomOccupancyEntity, SensorEntity):
    """Occupancy state of a room, with the evidence behind it.

    The attributes are the data source of the Room Status dashboard card.
    They change only when the evidence changes (never on a clock tick: the
    card counts down to ``vacancy_confirm_at`` itself) and are excluded
    from the Recorder.
    """

    _platform_domain = "sensor"
    _key = "state"
    _attr_translation_key = "room_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [s.value for s in OccupancyState]
    _unrecorded_attributes = frozenset({
        "room", "occupied", "state_since", "confidence", "reason", "reason_code", "active_sensors",
        "recent_door_movement", "stuck_sensors", "unavailable_sensors", "pending_vacancy",
        "vacancy_confirm_at", "quiet_since", "required_quiet_seconds", "vacancy_evidence",
        "starting_up", "last_activity", "last_activity_source", "signals", "health",
        "occupancy_usable", "observe_only", "operating_mode", "lighting_control", "lighting_paused", "comparison",
        "entities", "timing", "adjacent_rooms",
    })

    @property
    def native_value(self) -> str | None:
        result = self.result
        return result.state.value if result else None

    def _entity_ids(self) -> dict[str, str | None]:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(self.hass)
        keys = dict(ROOM_OCCUPANCY_ENTITIES)
        keys[ROOM_COMPARISON_ENTITY[0]] = ROOM_COMPARISON_ENTITY[1:]  # type: ignore[assignment]
        return {
            key: reg.async_get_entity_id(platform, "smart_occupancy", room_entity_unique_id(self._area_id, key))
            for key, (platform, _) in keys.items()
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        result = self.result
        if result is None:
            return None
        coordinator = self.coordinator
        config = coordinator.config
        room_config = config.room(self._area_id)
        health = room_health(coordinator.data, self._area_id, result)
        comparison = coordinator.occupancy.comparison(self._area_id)

        def labels(*statuses: SignalStatus) -> list[str]:
            return result.names(*statuses)

        active = [
            v.track.label() + (" (dropout grace)" if v.status == SignalStatus.GRACE else "")
            for v in result.views(SignalStatus.ACTIVE, SignalStatus.GRACE)
        ]
        mode = coordinator.data.effective_mode if coordinator.data else OperatingMode.OBSERVE
        lighting_active = coordinator.lighting.room_active(self._area_id)
        return {
            "integration": "smart_occupancy",
            "area_id": self._area_id,
            "room": area_name(self.hass, self._area_id) or self._area_id,
            "occupied": result.occupied,
            "state_since": _iso(result.state_since),
            "confidence": result.confidence.score,
            "reason": result.reason,
            "reason_code": result.reason_code,
            "active_sensors": active,
            "recent_door_movement": labels(SignalStatus.RECENT),
            "stuck_sensors": labels(SignalStatus.STUCK),
            "unavailable_sensors": health["occupancy"]["unavailable_sensors"],
            "pending_vacancy": result.state == OccupancyState.POSSIBLE_VACANCY,
            "vacancy_confirm_at": _iso(result.vacancy_at),
            "quiet_since": _iso(result.quiet_since),
            "required_quiet_seconds": result.required_quiet,
            "vacancy_evidence": list(result.vacancy_evidence),
            "starting_up": result.starting_up,
            "last_activity": _iso(result.last_activity),
            "last_activity_source": result.last_activity_source,
            "signals": [v.as_dict() for v in result.signals],
            "health": health,
            "occupancy_usable": health["occupancy"]["usable"],
            "operating_mode": mode.value,
            # Per room: Observe unless mood lighting was explicitly enabled here.
            "observe_only": not lighting_active,
            "lighting_control": lighting_active,
            # Main Mood is Manual: automatic lighting is paused (also in this room).
            "lighting_paused": lighting_active and coordinator.lighting.manual_mode,
            "comparison": (
                {"helper": room_config.reference_occupancy_entity, **comparison.as_dict()}
                if comparison else None
            ),
            "entities": self._entity_ids(),
            "timing": config.timing_for(self._area_id).as_dict(),
            "adjacent_rooms": list(room_config.adjacent_rooms),
        }


class RoomConfidenceSensor(RoomOccupancyEntity, SensorEntity):
    """Evidence quality (0-100 %). A transparent score, not a predicted probability."""

    _platform_domain = "sensor"
    _key = "confidence"
    _attr_translation_key = "room_confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> int | None:
        result = self.result
        return result.confidence.score if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        result = self.result
        if result is None:
            return None
        return {**result.confidence.as_dict(), "meaning": "evidence quality, not predictive accuracy"}


class RoomLastActivitySensor(RoomOccupancyEntity, SensorEntity):
    """When the latest activity started (motion, presence or door movement)."""

    _platform_domain = "sensor"
    _key = "last_activity"
    _attr_translation_key = "room_last_activity"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        result = self.result
        return result.last_activity if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        result = self.result
        if result is None or result.last_activity_source is None:
            return None
        track = self.coordinator.occupancy.engines[self._area_id].tracks.get(result.last_activity_source)
        return {
            "source": result.last_activity_source,
            "source_name": track.name if track else None,
        }


class RoomReasonSensor(RoomOccupancyEntity, SensorEntity):
    """Plain-language reason for the current occupancy state."""

    _platform_domain = "sensor"
    _key = "reason"
    _attr_translation_key = "room_reason"

    @property
    def native_value(self) -> str | None:
        result = self.result
        return result.reason if result else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        result = self.result
        return {"reason_code": result.reason_code} if result else None


COMPARISON_OPTIONS = ["agree", "disagree", "unknown"]


class RoomHelperComparisonSensor(RoomOccupancyEntity, SensorEntity):
    """Agreement with the existing occupancy helper (which is only read)."""

    _platform_domain = "sensor"
    _key = ROOM_COMPARISON_ENTITY[0]
    _attr_translation_key = "room_helper_comparison"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = COMPARISON_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"disagreement_since", "episode_open_since", "statistics_since"})

    @property
    def native_value(self) -> str | None:
        tracker = self.coordinator.occupancy.comparison(self._area_id)
        if tracker is None or tracker.reported_agreement is None:
            return "unknown"
        return "agree" if tracker.reported_agreement else "disagree"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        tracker = self.coordinator.occupancy.comparison(self._area_id)
        if tracker is None:
            return None
        return {
            "helper": self.coordinator.config.room(self._area_id).reference_occupancy_entity,
            **tracker.as_dict(),
        }


class RoomMoodLightingSensor(RoomOccupancyEntity, SensorEntity):
    """Mood lighting state of a room: observe, active or blocked, with its diagnostics.

    Updated when the lighting controller reports a change. Every attribute is
    excluded from the Recorder; the state changes only with the control mode.
    """

    _platform_domain = "sensor"
    _key = ROOM_LIGHTING_ENTITY[0]
    _attr_translation_key = ROOM_LIGHTING_ENTITY[2]
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [m.value for m in LightingMode]
    _unrecorded_attributes = frozenset({
        "area_id", "room", "control_mode_reason", "control_enabled", "activated_at", "global_mood",
        "global_mood_detail", "mood_source", "room_mood", "follows_global", "room_mood_override",
        "room_profile_overrides", "managed_lights", "managed", "discovered", "excluded", "covered_by_group",
        "opaque_groups", "offline_lights", "unsupported", "conflicts", "blocking_automations",
        "manual_overrides", "manual_override_minutes", "last_applied_profile", "last_applied_at",
        "last_trigger", "last_result", "last_reason", "last_counts", "last_errors", "last_decision_at",
        "integration", "manual_mode", "global_adjustment", "last_adaptive_at", "last_adaptive", "adaptive_conflicts",
        "auto_on",
    })

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_LIGHTING_UPDATED, self._write_if_changed))

    def _diagnostics(self) -> dict[str, Any]:
        return self.coordinator.lighting.room_diagnostics(self._area_id)

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _fingerprint(self) -> Any:
        diag = self._diagnostics()
        diag.pop("last_external_changes", None)
        return (self.available, diag)

    @property
    def native_value(self) -> str | None:
        return self._diagnostics()["control_mode"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        diag = self._diagnostics()
        diag.pop("last_external_changes", None)
        return {"integration": "smart_occupancy", **diag}


class RoomDaylightSensor(RoomOccupancyEntity, SensorEntity):
    """Daylight decision of a room: dark, bright or uncertain (Phase 0.3.3).

    Says whether occupancy may switch the room's lights on. The attributes
    (rounded, never recorded) explain how natural light was determined; the
    entity is written only when that summary changes.
    """

    _platform_domain = "sensor"
    _key = ROOM_DAYLIGHT_ENTITY[0]
    _attr_translation_key = ROOM_DAYLIGHT_ENTITY[2]
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [d.value for d in Darkness]
    _unrecorded_attributes = frozenset({
        "integration", "area_id", "natural_light", "lux", "estimated", "upper_bound", "confidence", "basis",
        "reason", "policy", "contaminated", "contamination", "sensor", "outdoor_lux", "sun_elevation", "errors",
        "last_activation_at", "activation",
    })

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_LIGHTING_UPDATED, self._write_if_changed))

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success

    def _summary(self) -> dict[str, Any]:
        return self.coordinator.lighting.daylight.status(self._area_id).summary()

    @property
    def native_value(self) -> str | None:
        return self._summary()["decision"]

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rt = self.coordinator.lighting.runtime.get(self._area_id)
        activation = rt.activation if rt else None
        return {
            "integration": "smart_occupancy",
            "area_id": self._area_id,
            **self._summary(),
            "last_activation_at": rt.last_activation_at if rt else None,
            "activation": activation.get("text") if activation else None,
        }
