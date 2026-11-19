"""Shared entity base of the companion: every entity shows what the App published (nothing is computed here).

Unique ids and suggested entity ids are exactly those of the monolithic
integration (0.9.0), so an upgrade keeps every entity, its history and every
automation that uses it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.util import slugify

from .companion import Companion
from .const import DOMAIN, NAME, SIGNAL_OWNERSHIP, SIGNAL_VIEW_UPDATED, VERSION

GLOBAL_SENSOR_KEYS = (
    "status",
    "operating_mode",
    "effective_mood",
    "configured_rooms",
    "light_categories",
    "discovered_sensors",
    "missing_entities",
    "unavailable_entities",
    "config_problems",
)
GLOBAL_BINARY_SENSOR_KEYS = ("mood_mismatch",)
GLOBAL_BUTTON_KEYS = ("recheck",)
ROOM_OCCUPANCY_ENTITIES: dict[str, tuple[str, str]] = {
    "occupied": ("binary_sensor", "room_occupied"),
    "state": ("sensor", "room_state"),
    "confidence": ("sensor", "room_confidence"),
    "last_activity": ("sensor", "room_last_activity"),
    "reason": ("sensor", "room_reason"),
}
ROOM_COMPARISON_ENTITY: tuple[str, str, str] = ("helper_comparison", "sensor", "room_helper_comparison")
ROOM_LIGHTING_ENTITY: tuple[str, str, str] = ("mood_lighting", "sensor", "room_mood_lighting")
ROOM_DAYLIGHT_ENTITY: tuple[str, str, str] = ("daylight", "sensor", "room_daylight")
ROOM_FORECAST_ENTITIES: dict[str, tuple[str, str]] = {
    "forecast": ("sensor", "room_forecast"),
    "forecast_confidence": ("sensor", "room_forecast_confidence"),
    "predicted": ("binary_sensor", "room_predicted"),
}
GLOBAL_FORECAST_SENSOR_KEYS = ("predicted_next_room", "forecast_learning")


def global_unique_id(key: str) -> str:
    # Single-instance integration: unique ids are stable across re-installs.
    return f"{DOMAIN}_{key}"


def room_unique_id(area_id: str) -> str:
    # area_id is stable when an area is renamed.
    return f"{DOMAIN}_room_{area_id}_observation"


def room_entity_unique_id(area_id: str, key: str) -> str:
    """Entities of a room. Keyed by area_id: stable across area renames."""
    return f"{DOMAIN}_room_{area_id}_{key}"


def expected_unique_ids(rooms: dict[str, dict[str, Any]], mood_builtin: bool) -> set[str]:
    ids = {
        global_unique_id(k)
        for k in GLOBAL_SENSOR_KEYS + GLOBAL_BINARY_SENSOR_KEYS + GLOBAL_BUTTON_KEYS + GLOBAL_FORECAST_SENSOR_KEYS
    }
    if mood_builtin:
        ids.add(global_unique_id("mood"))
    for area_id, room in rooms.items():
        ids.add(room_unique_id(area_id))
        ids |= {room_entity_unique_id(area_id, key) for key in ROOM_OCCUPANCY_ENTITIES}
        ids.add(room_entity_unique_id(area_id, ROOM_LIGHTING_ENTITY[0]))
        ids.add(room_entity_unique_id(area_id, ROOM_DAYLIGHT_ENTITY[0]))
        ids |= {room_entity_unique_id(area_id, key) for key in ROOM_FORECAST_ENTITIES}
        if room.get("reference"):
            ids.add(room_entity_unique_id(area_id, ROOM_COMPARISON_ENTITY[0]))
    return ids


def room_name(companion: Companion, area_id: str) -> str:
    name = (companion.rooms.get(area_id) or {}).get("name")
    if isinstance(name, str) and name:
        return name
    area = ar.async_get(companion.hass).async_get_area(area_id)
    return area.name if area else area_id


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


class CompanionEntity(Entity):
    """Base: attached to the integration's service device; writes its state only when it changed."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    #: The view domain this entity follows ("occupancy", "lighting", "forecast", "system").
    _view_domain: str = "system"

    def __init__(self, companion: Companion, unique_id: str) -> None:
        self.companion = companion
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "observer")},
            name=NAME,
            manufacturer=NAME,
            model=f"Companion {VERSION}",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._published: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_VIEW_UPDATED, self._on_view))
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_OWNERSHIP, self._on_view))

    @callback
    def _on_view(self, domain: Any = None) -> None:
        if domain in (None, self._view_domain, "system"):
            self._write_if_changed()

    def _entity_view(self) -> dict[str, Any] | None:
        """The published ``{"value", "attributes", "available"}`` of this entity, or None."""
        raise NotImplementedError

    @property
    def available(self) -> bool:
        view = self._entity_view()
        return view is not None and view.get("available", True)

    @property
    def fresh(self) -> bool:
        return self.companion.fresh(self._view_domain if self._view_domain != "system" else "occupancy")

    def _fingerprint(self) -> Any:
        return (self.available, self.state, self.extra_state_attributes)

    @callback
    def _write_if_changed(self) -> None:
        if self.hass is None or not self.enabled:
            return
        fingerprint = self._fingerprint()
        if fingerprint != self._published:
            self._published = fingerprint
            self.async_write_ha_state()


class RoomEntity(CompanionEntity):
    """Base of a room's entities (occupancy, lighting, forecast views)."""

    _platform_domain: str
    _key: str

    def __init__(self, companion: Companion, area_id: str) -> None:
        super().__init__(companion, room_entity_unique_id(area_id, self._key))
        self._area_id = area_id
        name = room_name(companion, area_id)
        self._attr_translation_placeholders = {"room": name}
        # Suggested entity id, e.g. sensor.smart_occupancy_kamar_nordur_state. Once registered it stays.
        self.entity_id = f"{self._platform_domain}.{DOMAIN}_{slugify(name) or slugify(area_id)}_{self._key}"

    @property
    def area_id(self) -> str:
        return self._area_id

    def _entity_view(self) -> dict[str, Any] | None:
        rooms = (self.companion.view(self._view_domain) or {}).get("rooms") or {}
        return (rooms.get(self._area_id) or {}).get(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        view = self._entity_view()
        if view is None:
            return None
        return dict(view.get("attributes") or {})
