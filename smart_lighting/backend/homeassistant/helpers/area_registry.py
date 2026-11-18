"""The area registry (``homeassistant.helpers.area_registry`` subset, read only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import _registry

EVENT_AREA_REGISTRY_UPDATED = "area_registry_updated"


def _normalize(name: str) -> str:
    return "".join(name.casefold().split())


@dataclass(slots=True, frozen=True)
class AreaEntry:
    id: str
    name: str
    floor_id: str | None = None
    icon: str | None = None
    picture: str | None = None
    aliases: frozenset[str] = field(default_factory=frozenset)
    labels: frozenset[str] = field(default_factory=frozenset)
    humidity_entity_id: str | None = None
    temperature_entity_id: str | None = None

    @property
    def area_id(self) -> str:
        return self.id

    @property
    def normalized_name(self) -> str:
        return _normalize(self.name)


class AreaRegistry:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.areas: dict[str, AreaEntry] = {}
        for item in items or ():
            if isinstance(item, dict) and isinstance(item.get("area_id"), str):
                self.areas[item["area_id"]] = AreaEntry(
                    id=item["area_id"], name=str(item.get("name") or item["area_id"]), floor_id=item.get("floor_id"),
                    icon=item.get("icon"), picture=item.get("picture"), aliases=frozenset(item.get("aliases") or ()),
                    labels=frozenset(item.get("labels") or ()), humidity_entity_id=item.get("humidity_entity_id"),
                    temperature_entity_id=item.get("temperature_entity_id"))

    def async_get_area(self, area_id: str) -> AreaEntry | None:
        return self.areas.get(area_id)

    def async_get_area_by_name(self, name: str) -> AreaEntry | None:
        wanted = _normalize(name)
        return next((a for a in self.areas.values() if a.normalized_name == wanted), None)

    def async_list_areas(self) -> list[AreaEntry]:
        return list(self.areas.values())


def async_get(hass: Any) -> AreaRegistry:
    return _registry.lists(hass).get("area") or _registry.lists(hass).setdefault("area", AreaRegistry())
