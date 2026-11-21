"""Entity base: every entity shows what the App published (nothing is computed here).

Identity: unique id ``<area_id>_<key>`` within this integration's own domain (it
cannot collide with Smart Comfort's ``smart_occupancy`` entities, which live in
another platform), one device per room identified by the area id. The entity id
is suggested once, from the room's name (``binary_sensor.sod_<room>_occupied``);
Home Assistant keeps it when the area is renamed later.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.util import slugify

from .const import DOMAIN, NAME, SIGNAL_FRESHNESS, SIGNAL_VIEWS, VERSION
from .hub import Hub

APP_DEVICE = "app"
#: key -> (platform, entity id suffix)
ROOM_ENTITIES: dict[str, tuple[str, str]] = {
    "occupied": ("binary_sensor", "occupied"),
    "state": ("sensor", "state"),
    "confidence": ("sensor", "confidence"),
    "reason": ("sensor", "reason"),
    "last_activity": ("sensor", "last_activity"),
}


def room_identifier(area_id: str) -> str:
    """Device identifier of a room (prefixed, so no area id can collide with the App device)."""
    return f"room_{area_id}"


def room_device(area_id: str, name: str) -> DeviceInfo:
    return DeviceInfo(identifiers={(DOMAIN, room_identifier(area_id))}, name=name, manufacturer=NAME, model="Room occupancy",
                      sw_version=VERSION, entry_type=DeviceEntryType.SERVICE)


class SodEntity(Entity):
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
        self._written = self._fingerprint()

    def _fingerprint(self) -> Any:
        return (self.available, self.state, self.extra_state_attributes)

    @callback
    def _refresh(self) -> None:
        """Write the state only when something visible changed (keeps the Recorder small)."""
        if self.hass is None or not self.enabled:
            return
        fingerprint = self._fingerprint()
        if fingerprint != self._written:
            self._written = fingerprint
            self.async_write_ha_state()


class RoomEntity(SodEntity):
    _key: str = ""

    def __init__(self, hub: Hub, area_id: str) -> None:
        name = hub.rooms().get(area_id) or area_id
        super().__init__(hub, f"{area_id}_{self._key}", room_device(area_id, name))
        self.area_id = area_id
        platform, suffix = ROOM_ENTITIES[self._key]
        self.entity_id = f"{platform}.sod_{slugify(name) or slugify(area_id)}_{suffix}"
        self._attr_translation_key = f"room_{self._key}"

    @property
    def view(self) -> dict[str, Any] | None:
        return self.hub.views.get(self.area_id)

    @property
    def available(self) -> bool:
        return self.view is not None


@callback
def async_sync_devices(hass: HomeAssistant, hub: Hub) -> None:
    """Rename room devices when their area was renamed; remove the devices (and entities) of deleted rooms."""
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    rooms = hub.rooms()
    if not rooms:
        return  # nothing known (fresh or lost storage): never delete entities on missing information
    for device in dr.async_entries_for_config_entry(devices, hub.entry.entry_id):
        ident = next((i for d, i in device.identifiers if d == DOMAIN), None)
        if ident is None or not ident.startswith("room_"):
            continue
        area_id = ident.removeprefix("room_")
        if area_id not in rooms:
            for entry in er.async_entries_for_device(entities, device.id, include_disabled_entities=True):
                entities.async_remove(entry.entity_id)
            devices.async_remove_device(device.id)
        elif device.name != rooms[area_id]:
            devices.async_update_device(device.id, name=rooms[area_id])
