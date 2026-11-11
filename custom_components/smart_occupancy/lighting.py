"""Light classification (via Home Assistant labels), discovery and group analysis.

Lights are classified *only* through labels: a light belongs to a category
when the category's label is applied to the light entity, or to the device
the light entity belongs to (the same rule Home Assistant uses for
``label_id`` service targets). Entity names are never used.

``LightIndex`` caches what is derived from the registries (labels, areas,
categories, group type) and is invalidated by registry events, so large
installations don't rescan every entity on each evaluation. Group membership
and capabilities are read from the current light states.

Nothing in this module controls a light.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import RGB_SUFFIX, CategoryKind
from .models import LightCategory

LIGHT_DOMAIN = "light"
_COLOR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
_BRIGHTNESS_MODES = frozenset({"brightness", "color_temp", "white"}) | _COLOR_MODES
_FEATURE_EFFECT = 4
_FEATURE_TRANSITION = 32
# Device models of group devices that do not expose their members to Home
# Assistant (Zigbee2MQTT publishes its groups as devices with model "Group").
_GROUP_DEVICE_MODELS = frozenset({"group", "zigbee group", "light group"})


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
    index = LightIndex(hass, tuple(categories))
    return index.by_category, index.categories_of


@dataclass(slots=True, frozen=True)
class LightInfo:
    """Registry facts about one light entity."""

    entity_id: str
    area_id: str | None
    device_id: str | None
    labels: frozenset[str]
    categories: tuple[str, ...]
    platform: str
    # A group whose members Home Assistant cannot see (e.g. Zigbee2MQTT).
    opaque_group: bool = False

    @property
    def category(self) -> str | None:
        """The category used for profiles (first in configuration order)."""
        return self.categories[0] if self.categories else None


@dataclass(slots=True)
class LightIndex:
    """Cached registry view of every enabled light entity."""

    hass: HomeAssistant
    categories: tuple[LightCategory, ...]
    lights: dict[str, LightInfo] = field(default_factory=dict)
    by_area: dict[str, list[str]] = field(default_factory=dict)
    by_category: dict[str, list[str]] = field(default_factory=dict)
    categories_of: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rebuild()

    def rebuild(self) -> None:
        hass = self.hass
        label_to_keys: dict[str, list[str]] = {}
        for cat in self.categories:
            if cat.label_id:
                label_to_keys.setdefault(cat.label_id, []).append(cat.key)
        order = [c.key for c in self.categories]
        lights: dict[str, LightInfo] = {}
        by_area: dict[str, list[str]] = {}
        by_category: dict[str, list[str]] = {k: [] for k in order}
        categories_of: dict[str, list[str]] = {}
        dev_reg = dr.async_get(hass)
        for entry in er.async_get(hass).entities.values():
            if entry.domain != LIGHT_DOMAIN or entry.disabled_by is not None:
                continue
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            labels = set(entry.labels) | (set(device.labels) if device else set())
            keys = {k for label in labels for k in label_to_keys.get(label, [])}
            cats = tuple(k for k in order if k in keys)
            area_id = entry.area_id or (device.area_id if device else None)
            opaque = bool(
                device and (device.model or "").casefold() in _GROUP_DEVICE_MODELS
            )
            info = LightInfo(
                entity_id=entry.entity_id, area_id=area_id, device_id=entry.device_id,
                labels=frozenset(labels), categories=cats, platform=entry.platform, opaque_group=opaque,
            )
            lights[entry.entity_id] = info
            if area_id:
                by_area.setdefault(area_id, []).append(entry.entity_id)
            if cats:
                categories_of[entry.entity_id] = list(cats)
                for key in cats:
                    by_category[key].append(entry.entity_id)
        for members in (*by_area.values(), *by_category.values()):
            members.sort()
        self.lights, self.by_area = lights, by_area
        self.by_category, self.categories_of = by_category, categories_of

    def info(self, entity_id: str) -> LightInfo | None:
        return self.lights.get(entity_id)

    def _kind(self, key: str) -> CategoryKind:
        cat = next((c for c in self.categories if c.key == key), None)
        return cat.kind if cat else CategoryKind.SOURCE

    def category_of(self, entity_id: str) -> str | None:
        """Primary category, for display: the first source type, else the first function."""
        cats = self.categories_of.get(entity_id) or []
        for kind in (CategoryKind.SOURCE, CategoryKind.FUNCTION):
            if key := next((k for k in cats if self._kind(k) == kind), None):
                return key
        return None

    @property
    def rgb_keys(self) -> list[str]:
        return [c.key for c in self.categories if c.kind == CategoryKind.RGB]

    def rgb_eligible(self, entity_id: str) -> bool:
        """May get colour profiles: carries the RGB tag (or, without an RGB tag configured, supports colour)."""
        if rgb := self.rgb_keys:
            return any(k in rgb for k in self.categories_of.get(entity_id, []))
        return light_capabilities(self.hass, entity_id).color

    def profile_targets(self, entity_id: str) -> list[str]:
        """Profile targets of a light, least specific first.

        Plain source types < plain functions < the RGB tag < "type + RGB" <
        "function + RGB". Within a kind, the category listed first in the
        configuration is the most specific. Entries of all of them are merged
        field by field, so e.g. an "Uppljós + RGB" colour applies on top of the
        "Loft ljós" brightness.
        """
        cats = self.categories_of.get(entity_id) or []
        sources = [k for k in cats if self._kind(k) == CategoryKind.SOURCE]
        functions = [k for k in cats if self._kind(k) == CategoryKind.FUNCTION]
        out = [*reversed(sources), *reversed(functions)]
        if self.rgb_eligible(entity_id):
            out += [k for k in cats if self._kind(k) == CategoryKind.RGB][::-1]
            out += [k + RGB_SUFFIX for k in reversed(sources)] + [k + RGB_SUFFIX for k in reversed(functions)]
        return out

    def target_lights(self, target: str) -> list[str]:
        """Lights a profile target applies to (a category, or its RGB-tagged part)."""
        key, rgb = (target[: -len(RGB_SUFFIX)], True) if target.endswith(RGB_SUFFIX) else (target, False)
        lights = self.by_category.get(key, [])
        return [e for e in lights if self.rgb_eligible(e)] if rgb else list(lights)

    def labelled_in_area(self, area_id: str, *, exclude_label: str | None = None) -> list[str]:
        """Lights in an area that carry a category label (automatic discovery)."""
        return [
            e for e in self.by_area.get(area_id, [])
            if self.categories_of.get(e) and not (exclude_label and exclude_label in self.lights[e].labels)
        ]

    def has_label(self, entity_id: str, label_id: str | None) -> bool:
        info = self.lights.get(entity_id)
        return bool(label_id and info and label_id in info.labels)

    def is_opaque_group(self, entity_id: str) -> bool:
        info = self.lights.get(entity_id)
        return bool((info and info.opaque_group) or is_unexposed_group(self.hass, entity_id))

    def groups_containing(self, entity_ids: Iterable[str]) -> dict[str, set[str]]:
        """Light groups (with known members) that contain any of the given lights."""
        wanted = set(entity_ids)
        out: dict[str, set[str]] = {}
        for group in self.lights:
            members = expand_group(self.hass, group)
            for member in members & wanted:
                out.setdefault(member, set()).add(group)
        return out


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


@dataclass(slots=True, frozen=True)
class Capabilities:
    """What a light can do, from its state attributes."""

    known: bool = False
    brightness: bool = False
    color_temp: bool = False
    color: bool = False
    min_kelvin: int | None = None
    max_kelvin: int | None = None
    effects: tuple[str, ...] = ()
    effect: bool = False
    transition: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "brightness": self.brightness,
            "color_temp": self.color_temp,
            "color": self.color,
            "min_kelvin": self.min_kelvin,
            "max_kelvin": self.max_kelvin,
            "effects": list(self.effects),
            "transition": self.transition,
        }


def light_capabilities(hass: HomeAssistant, entity_id: str) -> Capabilities:
    state = hass.states.get(entity_id)
    if state is None:
        return Capabilities()
    attrs = state.attributes
    modes = set(attrs.get("supported_color_modes") or [])
    features = attrs.get(ATTR_SUPPORTED_FEATURES) or 0
    effects = tuple(str(e) for e in attrs.get("effect_list") or [])

    def _int(key: str) -> int | None:
        value = attrs.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    return Capabilities(
        known=bool(modes),
        brightness=bool(modes & _BRIGHTNESS_MODES),
        color_temp="color_temp" in modes,
        color=bool(modes & _COLOR_MODES),
        min_kelvin=_int("min_color_temp_kelvin"),
        max_kelvin=_int("max_color_temp_kelvin"),
        effects=effects,
        effect=bool(features & _FEATURE_EFFECT) or bool(effects),
        transition=bool(features & _FEATURE_TRANSITION),
    )


def capabilities(hass: HomeAssistant, entity_id: str) -> dict[str, Any] | None:
    """Colour/effect capabilities from the light state, None if unknown."""
    caps = light_capabilities(hass, entity_id)
    if not caps.known:
        return None
    return {
        "color_temp": caps.color_temp,
        "rgb": caps.color,
        "brightness": caps.brightness,
        "effects": list(caps.effects),
        "min_kelvin": caps.min_kelvin,
        "max_kelvin": caps.max_kelvin,
        "transition": caps.transition,
    }


def is_offline(hass: HomeAssistant, entity_id: str) -> bool:
    state = hass.states.get(entity_id)
    return state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
