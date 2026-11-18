"""The device registry (``homeassistant.helpers.device_registry`` subset, read only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _registry

EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"


def _pairs(value: Any) -> frozenset[tuple[str, str]]:
    out = set()
    for item in value or ():
        if isinstance(item, (list, tuple)) and len(item) == 2:
            out.add((str(item[0]), str(item[1])))
    return frozenset(out)


@dataclass(slots=True, frozen=True)
class DeviceEntry:
    id: str
    area_id: str | None = None
    name: str | None = None
    name_by_user: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    model_id: str | None = None
    identifiers: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    connections: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    config_entries: frozenset[str] = field(default_factory=frozenset)
    primary_config_entry: str | None = None
    disabled_by: str | None = None
    entry_type: str | None = None
    labels: frozenset[str] = field(default_factory=frozenset)
    via_device_id: str | None = None
    sw_version: str | None = None
    hw_version: str | None = None
    serial_number: str | None = None
    configuration_url: str | None = None
    suggested_area: str | None = None
    created_at: str | None = None
    modified_at: str | None = None

    @property
    def disabled(self) -> bool:
        return self.disabled_by is not None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> DeviceEntry:
        return cls(
            id=str(data["id"]), area_id=data.get("area_id"), name=data.get("name"),
            name_by_user=data.get("name_by_user"), manufacturer=data.get("manufacturer"), model=data.get("model"),
            model_id=data.get("model_id"), identifiers=_pairs(data.get("identifiers")),
            connections=_pairs(data.get("connections")), config_entries=frozenset(data.get("config_entries") or ()),
            primary_config_entry=data.get("primary_config_entry"), disabled_by=data.get("disabled_by"),
            entry_type=data.get("entry_type"), labels=frozenset(data.get("labels") or ()),
            via_device_id=data.get("via_device_id"), sw_version=data.get("sw_version"),
            hw_version=data.get("hw_version"), serial_number=data.get("serial_number"),
            configuration_url=data.get("configuration_url"), created_at=data.get("created_at"),
            modified_at=data.get("modified_at"),
        )


class DeviceRegistry:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.devices: dict[str, DeviceEntry] = {}
        for item in items or ():
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                entry = DeviceEntry.from_wire(item)
                self.devices[entry.id] = entry

    def async_get(self, device_id: str) -> DeviceEntry | None:
        return self.devices.get(device_id)

    def async_get_device(self, identifiers: set[tuple[str, str]] | None = None,
                         connections: set[tuple[str, str]] | None = None) -> DeviceEntry | None:
        for device in self.devices.values():
            if identifiers and device.identifiers & set(identifiers):
                return device
            if connections and device.connections & set(connections):
                return device
        return None


def async_get(hass: Any) -> DeviceRegistry:
    return _registry.lists(hass).get("device") or _registry.lists(hass).setdefault("device", DeviceRegistry())


def async_entries_for_config_entry(registry: DeviceRegistry, config_entry_id: str) -> list[DeviceEntry]:
    return [d for d in registry.devices.values() if config_entry_id in d.config_entries]


def async_entries_for_area(registry: DeviceRegistry, area_id: str) -> list[DeviceEntry]:
    return [d for d in registry.devices.values() if d.area_id == area_id]


def async_entries_for_label(registry: DeviceRegistry, label_id: str) -> list[DeviceEntry]:
    return [d for d in registry.devices.values() if label_id in d.labels]
