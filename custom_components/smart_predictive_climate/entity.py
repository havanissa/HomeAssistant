"""Entity base: every entity shows what the App published (nothing is computed here).

The entities are generic: the App describes each one (platform, name, device class, unit, state class, icon,
category) and this integration creates it. Identity: unique id ``<room_id>_<key>`` within this integration's own
domain, one device per room identified by the room id. The entity id is suggested once, from the room's name
(``sensor.spc_<room>_<key>``); Home Assistant keeps it when the room is renamed later.

Freshness: a room entity is available only while the App holds a valid lease, published in it, and the snapshot
contains the entity. Otherwise it is unavailable and shows neither a value nor attributes: a stale forecast must
never look like a recommendation.
"""

from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import protocol as P
from .const import DOMAIN, NAME, SIGNAL_FORGET, SIGNAL_FRESHNESS, SIGNAL_ROOMS, SIGNAL_VIEWS, VERSION
from .hub import Hub

_LOGGER = logging.getLogger(__name__)

APP_DEVICE = "app"
#: Not a possible ``<room_id>_<key>`` (those have no hyphen), so no room entity can collide with it.
APP_CONNECTED_UNIQUE_ID = "app-connected"
ROOM_PREFIX = "room_"


def room_identifier(room_id: str) -> str:
    """Device identifier of a room (prefixed, so no room id can collide with the App device)."""
    return f"{ROOM_PREFIX}{room_id}"


def room_device(room_id: str, name: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, room_identifier(room_id))}, name=name, manufacturer=NAME,
                      model="Room climate forecast", sw_version=VERSION, entry_type=DeviceEntryType.SERVICE)


def device_room(device: dr.DeviceEntry | None) -> str | None:
    """The room id of a room device (None for the App device and for other devices)."""
    if device is None:
        return None
    ident = next((i for d, i in device.identifiers if d == DOMAIN), None)
    return ident.removeprefix(ROOM_PREFIX) if ident is not None and ident.startswith(ROOM_PREFIX) else None


class SpcEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub, unique_id: str, device: DeviceInfo) -> None:
        self.hub = hub
        self._attr_unique_id = unique_id
        self._attr_device_info = device
        self._written: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_VIEWS, self._refresh))
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_FRESHNESS, self._refresh))
        self._written = self._update()

    def _update(self) -> Any:
        """Take over what the hub holds now; returns everything visible (compared before a state is written)."""
        raise NotImplementedError

    @callback
    def _refresh(self) -> None:
        """Write the state only when something visible changed (keeps the Recorder small)."""
        if self.hass is None or not self.enabled:
            return
        fingerprint = self._update()
        if fingerprint != self._written:
            self._written = fingerprint
            self.async_write_ha_state()


class RoomEntity(SpcEntity):
    """One published entity of a room; the platform classes map the description and the value."""

    def __init__(self, hub: Hub, room_id: str, key: str) -> None:
        name = hub.rooms().get(room_id) or room_id
        super().__init__(hub, P.unique_id(room_id, key), room_device(room_id, name))
        self.room_id = room_id
        self.key = key
        self._classes: tuple[Any, Any, Any] | None = None
        self._warned = False
        description = hub.entity(room_id, key) or {}
        self.entity_id = f"{description.get('platform', 'sensor')}.spc_{slugify(name) or room_id}_{key}"
        self._attr_available = False
        self._update()

    def _update(self) -> Any:
        description = self.hub.entity(self.room_id, self.key)
        if description is not None:
            classes = (description["device_class"], description["unit"], description["state_class"])
            if self._classes is not None and classes != self._classes and not self._warned:
                self._warned = True
                _LOGGER.warning(
                    "The App changed device class, unit or state class of %s from %s to %s: the new description is "
                    "used, but Home Assistant's recorded history of this entity may no longer fit",
                    self.entity_id, self._classes, classes)
            self._classes = classes
            self._attr_name = description["name"]
            self._attr_icon = description["icon"]
            self._attr_entity_category = EntityCategory.DIAGNOSTIC if description["category"] == "diagnostic" else None
            self._describe(description)
        shown = self.hub.value(self.room_id, self.key)
        self._attr_available = shown is not None
        attributes = None
        if shown is not None:
            attributes = {k: v for k, v in shown["attributes"].items() if k not in self._hidden_attributes()} or None
        self._attr_extra_state_attributes = attributes
        self._show(shown)
        return (self._attr_available, shown["value"] if shown else None, attributes, self._attr_name, self._classes,
                self._attr_icon)

    def _hidden_attributes(self) -> tuple[str, ...]:
        return ()

    def _describe(self, description: dict[str, Any]) -> None:
        """Take over device class, unit and state class."""
        raise NotImplementedError

    def _show(self, shown: dict[str, Any] | None) -> None:
        """Take over the value (``shown`` None: unavailable, show nothing)."""
        raise NotImplementedError


@callback
def async_setup_room_entities(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback,
                              platform: str, factory: Callable[[Hub, str, str], RoomEntity]) -> None:
    """Create this platform's entity for every published (room, key), now and whenever the rooms change."""
    hub: Hub = entry.runtime_data
    created: set[tuple[str, str]] = set()

    @callback
    def sync() -> None:
        wanted = {(room_id, key) for room_id, room in hub.known().items()
                  for key, description in room["entities"].items() if description["platform"] == platform}
        created.intersection_update(wanted)  # removed ones may come back later: they get an entity again
        new = sorted(wanted - created)
        created.update(new)
        if new:
            async_add_entities([factory(hub, room_id, key) for room_id, key in new])

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_ROOMS, sync))
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_FORGET, sync))


@callback
def async_sync_devices(hass: HomeAssistant, hub: Hub) -> None:
    """Rename room devices when the App renamed the room; remove the entities of keys the App no longer publishes
    for a room, and the devices (with their entities) of rooms deleted in the App.

    Nothing is ever removed on missing information: a room that is unknown here (fresh or lost storage) and was not
    deleted by the App keeps everything.
    """
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    known = hub.known()
    expected = {(description["platform"], P.unique_id(room_id, key)) for room_id, room in known.items()
                for key, description in room["entities"].items()}
    for entry in er.async_entries_for_config_entry(entities, hub.entry.entry_id):
        if (entry.domain, entry.unique_id) in expected or entry.unique_id == APP_CONNECTED_UNIQUE_ID:
            continue
        room_id = device_room(devices.async_get(entry.device_id) if entry.device_id else None)
        if room_id is not None and (room_id in known or room_id in hub.deleted):
            entities.async_remove(entry.entity_id)
    for device in dr.async_entries_for_config_entry(devices, hub.entry.entry_id):
        room_id = device_room(device)
        if room_id is None:
            continue
        if room_id in known:
            if device.name != known[room_id]["name"]:
                devices.async_update_device(device.id, name=known[room_id]["name"])
        elif room_id in hub.deleted:
            for entry in er.async_entries_for_device(entities, device.id, include_disabled_entities=True):
                entities.async_remove(entry.entity_id)
            if devices.async_get(device.id) is not None:
                devices.async_remove_device(device.id)
