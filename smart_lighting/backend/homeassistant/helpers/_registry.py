"""Shared storage of the registries (refreshed by the host from ``config/*_registry/list``)."""

from __future__ import annotations

from typing import Any

KEY = "_host_registries"


def lists(hass: Any) -> dict[str, Any]:
    return hass.data.setdefault(KEY, {})


def install(hass: Any, *, entities: list[dict[str, Any]] | None = None, devices: list[dict[str, Any]] | None = None,
            areas: list[dict[str, Any]] | None = None, labels: list[dict[str, Any]] | None = None) -> None:
    """Replace the registries the host knows (only the lists given)."""
    from . import area_registry, device_registry, entity_registry, label_registry  # noqa: PLC0415

    store = lists(hass)
    if entities is not None:
        store["entity"] = entity_registry.EntityRegistry(entities)
    if devices is not None:
        store["device"] = device_registry.DeviceRegistry(devices)
    if areas is not None:
        store["area"] = area_registry.AreaRegistry(areas)
    if labels is not None:
        store["label"] = label_registry.LabelRegistry(labels)
