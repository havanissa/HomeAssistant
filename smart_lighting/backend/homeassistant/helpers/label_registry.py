"""The label registry (``homeassistant.helpers.label_registry`` subset, read only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import _registry

EVENT_LABEL_REGISTRY_UPDATED = "label_registry_updated"


@dataclass(slots=True, frozen=True)
class LabelEntry:
    label_id: str
    name: str
    color: str | None = None
    icon: str | None = None
    description: str | None = None


class LabelRegistry:
    def __init__(self, items: list[dict[str, Any]] | None = None) -> None:
        self.labels: dict[str, LabelEntry] = {}
        for item in items or ():
            if isinstance(item, dict) and isinstance(item.get("label_id"), str):
                self.labels[item["label_id"]] = LabelEntry(
                    label_id=item["label_id"], name=str(item.get("name") or item["label_id"]), color=item.get("color"),
                    icon=item.get("icon"), description=item.get("description"))

    def async_get_label(self, label_id: str) -> LabelEntry | None:
        return self.labels.get(label_id)

    def async_get_label_by_name(self, name: str) -> LabelEntry | None:
        wanted = name.casefold().strip()
        return next((label for label in self.labels.values() if label.name.casefold().strip() == wanted), None)

    def async_list_labels(self) -> list[LabelEntry]:
        return list(self.labels.values())


def async_get(hass: Any) -> LabelRegistry:
    return _registry.lists(hass).get("label") or _registry.lists(hass).setdefault("label", LabelRegistry())
