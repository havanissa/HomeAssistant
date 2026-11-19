"""Unique ids of the native entities (shared by the companion integration's entities and the App's views)."""

from __future__ import annotations

from .const import DOMAIN


def global_unique_id(key: str) -> str:
    return f"{DOMAIN}_{key}"


def room_unique_id(area_id: str) -> str:
    return f"{DOMAIN}_room_{area_id}_observation"


def room_entity_unique_id(area_id: str, key: str) -> str:
    """Entities of a room. Keyed by area_id: stable across area renames."""
    return f"{DOMAIN}_room_{area_id}_{key}"
