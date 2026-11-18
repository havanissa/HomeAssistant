"""The entity registry (``homeassistant.helpers.entity_registry`` subset, read only)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import _registry

EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"


@dataclass(slots=True, frozen=True)
class RegistryEntry:
    entity_id: str
    unique_id: str | None = None
    platform: str | None = None
    id: str | None = None
    device_id: str | None = None
    area_id: str | None = None
    config_entry_id: str | None = None
    disabled_by: str | None = None
    hidden_by: str | None = None
    entity_category: str | None = None
    labels: frozenset[str] = field(default_factory=frozenset)
    name: str | None = None
    original_name: str | None = None
    icon: str | None = None
    has_entity_name: bool = False
    translation_key: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    # Not part of Home Assistant's registry list: the runtime falls back to the state's attributes.
    device_class: str | None = None
    original_device_class: str | None = None
    capabilities: dict[str, Any] | None = None
    supported_features: int = 0

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def disabled(self) -> bool:
        return self.disabled_by is not None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> RegistryEntry:
        return cls(
            entity_id=str(data["entity_id"]), unique_id=data.get("unique_id"), platform=data.get("platform"),
            id=data.get("id"), device_id=data.get("device_id"), area_id=data.get("area_id"),
            config_entry_id=data.get("config_entry_id"), disabled_by=data.get("disabled_by"),
            hidden_by=data.get("hidden_by"), entity_category=data.get("entity_category"),
            labels=frozenset(data.get("labels") or ()), name=data.get("name"),
            original_name=data.get("original_name"), icon=data.get("icon"),
            has_entity_name=bool(data.get("has_entity_name")), translation_key=data.get("translation_key"),
            options=data.get("options") if isinstance(data.get("options"), dict) else {},
        )


class EntityRegistryItems(dict[str, RegistryEntry]):
    def get_entries_for_device_id(self, device_id: str, include_disabled_entities: bool = False) -> list[RegistryEntry]:
        return [e for e in self.values() if e.device_id == device_id and (include_disabled_entities or not e.disabled)]


class EntityRegistry:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.entities = EntityRegistryItems()
        for item in items or ():
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str):
                entry = RegistryEntry.from_wire(item)
                self.entities[entry.entity_id] = entry

    def async_get(self, entity_id: str) -> RegistryEntry | None:
        return self.entities.get(entity_id)

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        for entry in self.entities.values():
            if entry.domain == domain and entry.platform == platform and entry.unique_id == unique_id:
                return entry.entity_id
        return None

    def async_is_registered(self, entity_id: str) -> bool:
        return entity_id in self.entities

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self.entities.values())


def async_get(hass: Any) -> EntityRegistry:
    return _registry.lists(hass).get("entity") or _registry.lists(hass).setdefault("entity", EntityRegistry())


def async_entries_for_device(registry: EntityRegistry, device_id: str,
                             include_disabled_entities: bool = False) -> list[RegistryEntry]:
    return registry.entities.get_entries_for_device_id(device_id, include_disabled_entities)


def async_entries_for_area(registry: EntityRegistry, area_id: str) -> list[RegistryEntry]:
    return [e for e in registry.entities.values() if e.area_id == area_id]


def async_entries_for_config_entry(registry: EntityRegistry, config_entry_id: str) -> list[RegistryEntry]:
    return [e for e in registry.entities.values() if e.config_entry_id == config_entry_id]


def async_entries_for_label(registry: EntityRegistry, label_id: str) -> list[RegistryEntry]:
    return [e for e in registry.entities.values() if label_id in e.labels]
