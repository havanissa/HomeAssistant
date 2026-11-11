"""Light classification (via Home Assistant labels) and group analysis.

Lights are classified *only* through labels: a light belongs to a category
when the category's label is applied to the light entity, or to the device
the light entity belongs to (the same rule Home Assistant uses for
``label_id`` service targets). Entity names are never used.

No light is ever controlled from this module.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .models import LightCategory

LIGHT_DOMAIN = "light"
_RGB_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}


def entity_labels(hass: HomeAssistant, entity_id: str) -> set[str]:
    """Labels of an entity, including labels of its device."""
    ent_reg = er.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    if entry is None:
        return set()
    labels = set(entry.labels)
    if entry.device_id and (device := dr.async_get(hass).async_get(entry.device_id)):
        labels |= set(device.labels)
    return labels


def classify_lights(
    hass: HomeAssistant, categories: Iterable[LightCategory]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Classify all enabled light entities.

    Returns (category_key -> [entity_id], entity_id -> [category_key]).
    """
    label_to_keys: dict[str, list[str]] = {}
    by_category: dict[str, list[str]] = {}
    for cat in categories:
        by_category[cat.key] = []
        if cat.label_id:
            label_to_keys.setdefault(cat.label_id, []).append(cat.key)

    by_light: dict[str, list[str]] = {}
    if not label_to_keys:
        return by_category, by_light

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    for entry in ent_reg.entities.values():
        if entry.domain != LIGHT_DOMAIN or entry.disabled_by is not None:
            continue
        labels = set(entry.labels)
        if entry.device_id and (device := dev_reg.async_get(entry.device_id)):
            labels |= set(device.labels)
        keys = [k for label in labels for k in label_to_keys.get(label, [])]
        if not keys:
            continue
        # Keep category order stable (configuration order).
        ordered = [c for c in by_category if c in keys]
        by_light[entry.entity_id] = ordered
        for key in ordered:
            by_category[key].append(entry.entity_id)
    for members in by_category.values():
        members.sort()
    return by_category, by_light


def group_members(hass: HomeAssistant, entity_id: str) -> set[str] | None:
    """Direct members of a light group, or None if not a (known) group.

    Uses the ``entity_id`` state attribute exposed by Home Assistant light
    groups, falling back to the group helper's stored options so membership is
    still known while the group is unavailable. Groups that do not expose their
    members (for example Zigbee2MQTT or Hue bridge groups) return None.
    """
    state = hass.states.get(entity_id)
    if state is not None:
        members = state.attributes.get(ATTR_ENTITY_ID)
        if isinstance(members, (list, tuple)) and members:
            return set(members)
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and entry.platform == "group" and entry.config_entry_id:
        config_entry = hass.config_entries.async_get_entry(entry.config_entry_id)
        if config_entry is not None:
            members = config_entry.options.get("entities")
            if isinstance(members, list) and members:
                return set(members)
    return None


def expand_group(hass: HomeAssistant, entity_id: str) -> set[str]:
    """All lights (recursively) contained in a group. Empty for non-groups."""
    result: set[str] = set()
    stack = [entity_id]
    seen = {entity_id}
    while stack:
        members = group_members(hass, stack.pop())
        if not members:
            continue
        for member in members:
            if member in seen:
                continue
            seen.add(member)
            result.add(member)
            stack.append(member)
    return result


@dataclass(slots=True, frozen=True)
class GroupConflict:
    """A group and one of its members are both managed independently."""

    group: str
    member: str
    group_room: str
    member_room: str


def find_group_conflicts(
    hass: HomeAssistant, managed: dict[str, list[str]]
) -> list[GroupConflict]:
    """Find groups whose members are also managed lights.

    ``managed`` maps area_id -> managed light entity ids.
    """
    owner: dict[str, list[str]] = {}
    for area_id, lights in managed.items():
        for light in lights:
            owner.setdefault(light, []).append(area_id)
    conflicts: list[GroupConflict] = []
    for group, group_rooms in owner.items():
        for member in sorted(expand_group(hass, group)):
            for member_room in owner.get(member, []):
                for group_room in group_rooms:
                    conflicts.append(
                        GroupConflict(group, member, group_room, member_room)
                    )
    return conflicts


def is_unexposed_group(hass: HomeAssistant, entity_id: str) -> bool:
    """True for groups known to hide their membership (e.g. Hue groups)."""
    state = hass.states.get(entity_id)
    return bool(state and state.attributes.get("is_hue_group"))


def capabilities(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Colour/effect capabilities from the light state, None if unknown."""
    state = hass.states.get(entity_id)
    if state is None or not state.attributes.get("supported_color_modes"):
        return None
    modes = set(state.attributes.get("supported_color_modes") or [])
    return {
        "color_temp": "color_temp" in modes,
        "rgb": bool(modes & _RGB_MODES),
        "effects": list(state.attributes.get("effect_list") or []),
        "min_kelvin": state.attributes.get("min_color_temp_kelvin"),
        "max_kelvin": state.attributes.get("max_color_temp_kelvin"),
    }
