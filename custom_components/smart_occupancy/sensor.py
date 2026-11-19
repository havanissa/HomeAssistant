"""Sensors of the companion: the App's published views, on the entities of the monolithic integration.

Recorder load stays what it was: list-valued attributes and timestamps are
excluded from the Recorder, no sensor has a state class, and a state is
written only when the published view changed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .companion import Companion
from .const import (
    COMPARISON_OPTIONS,
    CONFIDENCE_LEVELS,
    DARKNESS,
    DOMAIN,
    LEARNING_STATES,
    LIGHTING_MODES,
    OCCUPANCY_STATES,
    OPERATING_MODES,
    SIGNAL_ROOMS_CHANGED,
    STATUS_OPTIONS,
)
from .entity import (
    GLOBAL_SENSOR_KEYS,
    ROOM_COMPARISON_ENTITY,
    ROOM_DAYLIGHT_ENTITY,
    ROOM_LIGHTING_ENTITY,
    CompanionEntity,
    RoomEntity,
    global_unique_id,
    parse_time,
    room_entity_unique_id,
    room_name,
    room_unique_id,
)

PARALLEL_UPDATES = 0

UNRECORDED = frozenset({
    "rooms", "categories", "sensors", "entities", "items", "problems", "notes", "last_checked", "companion",
})
UNRECORDED_FORECAST = frozenset({
    "explanation", "factors", "window", "schedule", "entry_probability", "updated", "reasons", "components",
    "candidates", "detail", "last_update", "queries",
})


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    companion: Companion = entry.runtime_data
    async_add_entities([GlobalSensor(companion, key) for key in GLOBAL_SENSOR_KEYS]
                       + [PredictedNextRoomSensor(companion), ForecastLearningSensor(companion)])
    tracker = RoomTracker(hass, companion, async_add_entities)
    tracker.sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS_CHANGED, tracker.sync))


class RoomTracker:
    """Creates the room sensors of every published room and removes those of rooms that disappeared."""

    def __init__(self, hass: HomeAssistant, companion: Companion, add: AddConfigEntryEntitiesCallback) -> None:
        self.hass, self.companion, self.add = hass, companion, add
        self.entities: dict[str, list[RoomEntity]] = {}

    @callback
    def sync(self) -> None:
        rooms = self.companion.rooms
        for area_id, room in sorted(rooms.items()):
            current = self.entities.setdefault(area_id, [])
            keys = {e._key for e in current}  # noqa: SLF001
            new: list[Any] = []
            if "observation" not in keys:
                new.append(RoomObservationSensor(self.companion, area_id))
            for cls in (RoomStateSensor, RoomConfidenceSensor, RoomLastActivitySensor, RoomReasonSensor,
                        RoomMoodLightingSensor, RoomDaylightSensor, RoomForecastSensor, RoomForecastConfidenceSensor):
                if cls._key not in keys:
                    new.append(cls(self.companion, area_id))
            if room.get("reference") and ROOM_COMPARISON_ENTITY[0] not in keys:
                new.append(RoomHelperComparisonSensor(self.companion, area_id))
            if new:
                current.extend(new)
                self.add(new)
            if not room.get("reference"):
                for entity in [e for e in current if e._key == ROOM_COMPARISON_ENTITY[0]]:  # noqa: SLF001
                    current.remove(entity)
                    self.hass.async_create_task(_remove(self.hass, entity))
        for area_id in [a for a in self.entities if a not in rooms]:
            for entity in self.entities.pop(area_id):
                self.hass.async_create_task(_remove(self.hass, entity))


async def _remove(hass: HomeAssistant, entity: Any) -> None:
    entity_id = entity.entity_id
    await entity.async_remove(force_remove=True)
    reg = er.async_get(hass)
    if reg.async_get(entity_id) is not None:
        reg.async_remove(entity_id)


class GlobalSensor(CompanionEntity, SensorEntity):
    _unrecorded_attributes = UNRECORDED
    _view_domain = "system"

    def __init__(self, companion: Companion, key: str) -> None:
        super().__init__(companion, global_unique_id(key))
        self._key = key
        self._attr_translation_key = key
        if key in ("status", "operating_mode", "configured_rooms", "light_categories", "discovered_sensors",
                   "missing_entities", "unavailable_entities", "config_problems"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key == "status":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = STATUS_OPTIONS
        elif key == "operating_mode":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = OPERATING_MODES

    def _entity_view(self) -> dict[str, Any] | None:
        return ((self.companion.view("system") or {}).get("sensors") or {}).get(self._key)

    @property
    def native_value(self) -> Any:
        view = self._entity_view()
        if view is None:
            return None
        value = view.get("value")
        if self._key in ("configured_rooms", "light_categories", "discovered_sensors", "missing_entities",
                         "unavailable_entities", "config_problems"):
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if self._key == "status" and value not in STATUS_OPTIONS:
            return None
        if self._key == "operating_mode" and value not in OPERATING_MODES:
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        attributes = dict(view.get("attributes") or {})
        if self._key == "status":
            attributes["companion"] = {
                "app_connected": any(self.companion.fresh(d) for d in ("occupancy", "lighting")),
                "owners": {d: self.companion.state(d) for d in ("occupancy", "lighting", "curtains", "forecast")},
            }
        return attributes


class RoomObservationSensor(CompanionEntity, SensorEntity):
    """Diagnostic health of one room. Not an occupancy state."""

    _key = "observation"
    _attr_translation_key = "room_observation"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({
        "reasons", "sensors_available", "sensors_unavailable", "lights_available",
        "lights_unavailable", "pending", "problems", "last_checked",
    })
    _view_domain = "system"

    def __init__(self, companion: Companion, area_id: str) -> None:
        super().__init__(companion, room_unique_id(area_id))
        self._area_id = area_id
        self._attr_translation_placeholders = {"room": room_name(companion, area_id)}

    def _entity_view(self) -> dict[str, Any] | None:
        return ((self.companion.view("system") or {}).get("room_observation") or {}).get(self._area_id)

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in STATUS_OPTIONS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        return dict(view.get("attributes") or {}) if view else None


class RoomStateSensor(RoomEntity, SensorEntity):
    """Occupancy state of a room, with the evidence behind it (the Room Status card's data)."""

    _platform_domain = "sensor"
    _key = "state"
    _view_domain = "occupancy"
    _attr_translation_key = "room_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = OCCUPANCY_STATES
    _unrecorded_attributes = frozenset({
        "room", "occupied", "state_since", "confidence", "reason", "reason_code", "active_sensors",
        "recent_door_movement", "stuck_sensors", "unavailable_sensors", "pending_vacancy",
        "vacancy_confirm_at", "quiet_since", "required_quiet_seconds", "vacancy_evidence",
        "starting_up", "last_activity", "last_activity_source", "signals", "health",
        "occupancy_usable", "observe_only", "operating_mode", "lighting_control", "lighting_paused", "comparison",
        "entities", "timing", "adjacent_rooms", "fresh", "decided_by",
    })

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in OCCUPANCY_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        attributes = dict(view.get("attributes") or {})
        attributes.setdefault("integration", DOMAIN)
        attributes.setdefault("area_id", self._area_id)
        reg = er.async_get(self.hass) if self.hass else None
        if reg is not None:
            attributes["entities"] = {
                key: reg.async_get_entity_id(platform, DOMAIN, room_entity_unique_id(self._area_id, key))
                for key, platform in (("occupied", "binary_sensor"), ("state", "sensor"), ("confidence", "sensor"),
                                      ("last_activity", "sensor"), ("reason", "sensor"),
                                      ("helper_comparison", "sensor"))
            }
        attributes["decided_by"] = "app"
        attributes["fresh"] = self.fresh
        return attributes


class RoomConfidenceSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = "confidence"
    _view_domain = "occupancy"
    _attr_translation_key = "room_confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0

    @property
    def native_value(self) -> int | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class RoomLastActivitySensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = "last_activity"
    _view_domain = "occupancy"
    _attr_translation_key = "room_last_activity"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        view = self._entity_view()
        return parse_time(view.get("value")) if view else None


class RoomReasonSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = "reason"
    _view_domain = "occupancy"
    _attr_translation_key = "room_reason"

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return str(value)[:255] if isinstance(value, str) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        return {**dict(view.get("attributes") or {}), "decided_by": "app", "fresh": self.fresh}


class RoomHelperComparisonSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = ROOM_COMPARISON_ENTITY[0]
    _view_domain = "occupancy"
    _attr_translation_key = "room_helper_comparison"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = COMPARISON_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _unrecorded_attributes = frozenset({"disagreement_since", "episode_open_since", "statistics_since"})

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in COMPARISON_OPTIONS else "unknown"


class RoomMoodLightingSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = ROOM_LIGHTING_ENTITY[0]
    _view_domain = "lighting"
    _attr_translation_key = ROOM_LIGHTING_ENTITY[2]
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LIGHTING_MODES
    _unrecorded_attributes = frozenset({
        "area_id", "room", "control_mode_reason", "control_enabled", "activated_at", "global_mood",
        "global_mood_detail", "mood_source", "room_mood", "follows_global", "room_mood_override",
        "room_profile_overrides", "managed_lights", "managed", "discovered", "excluded", "covered_by_group",
        "opaque_groups", "offline_lights", "unsupported", "conflicts", "blocking_automations",
        "manual_overrides", "manual_override_minutes", "last_applied_profile", "last_applied_at",
        "last_trigger", "last_result", "last_reason", "last_counts", "last_errors", "last_decision_at",
        "integration", "manual_mode", "global_adjustment", "last_adaptive_at", "last_adaptive", "adaptive_conflicts",
        "auto_on", "vacancy", "active_zones", "supervisor", "supervisor_discrepancies", "relays", "shared_space",
        "space_deferred", "brightness", "fresh",
    })

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in LIGHTING_MODES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        return {"integration": DOMAIN, **dict(view.get("attributes") or {}), "fresh": self.fresh}


class RoomDaylightSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = ROOM_DAYLIGHT_ENTITY[0]
    _view_domain = "lighting"
    _attr_translation_key = ROOM_DAYLIGHT_ENTITY[2]
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = DARKNESS
    _unrecorded_attributes = frozenset({
        "integration", "area_id", "natural_light", "lux", "estimated", "upper_bound", "confidence", "basis",
        "reason", "policy", "contaminated", "contamination", "sensor", "outdoor_lux", "sun_elevation", "errors",
        "last_activation_at", "activation",
    })

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in DARKNESS else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        return {"integration": DOMAIN, "area_id": self._area_id, **dict(view.get("attributes") or {})}


class RoomForecastSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = "forecast"
    _view_domain = "forecast"
    _attr_translation_key = "room_forecast"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _attr_icon = "mdi:crystal-ball"
    _unrecorded_attributes = UNRECORDED_FORECAST

    @property
    def native_value(self) -> int | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class RoomForecastConfidenceSensor(RoomEntity, SensorEntity):
    _platform_domain = "sensor"
    _key = "forecast_confidence"
    _view_domain = "forecast"
    _attr_translation_key = "room_forecast_confidence"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CONFIDENCE_LEVELS
    _attr_icon = "mdi:gauge"
    _unrecorded_attributes = UNRECORDED_FORECAST

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in CONFIDENCE_LEVELS else None


class PredictedNextRoomSensor(CompanionEntity, SensorEntity):
    _view_domain = "forecast"
    _attr_translation_key = "predicted_next_room"
    _attr_icon = "mdi:walk"
    _unrecorded_attributes = UNRECORDED_FORECAST

    def __init__(self, companion: Companion) -> None:
        super().__init__(companion, global_unique_id("predicted_next_room"))

    def _entity_view(self) -> dict[str, Any] | None:
        return (self.companion.view("forecast") or {}).get("predicted_next_room")

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return str(value)[:255] if isinstance(value, str) else "none"

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        return dict(view.get("attributes") or {}) if view else {"area_id": None, "observation_only": True}


class ForecastLearningSensor(CompanionEntity, SensorEntity):
    _view_domain = "forecast"
    _attr_translation_key = "forecast_learning"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = LEARNING_STATES
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:school-outline"
    _unrecorded_attributes = UNRECORDED_FORECAST

    def __init__(self, companion: Companion) -> None:
        super().__init__(companion, global_unique_id("forecast_learning"))

    def _entity_view(self) -> dict[str, Any] | None:
        return (self.companion.view("forecast") or {}).get("forecast_learning")

    @property
    def native_value(self) -> str | None:
        view = self._entity_view()
        value = view.get("value") if view else None
        return value if value in LEARNING_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        return dict(view.get("attributes") or {}) if view else None
