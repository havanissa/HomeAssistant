"""Mood profiles: validation and inheritance resolution.

Three configuration levels, merged field by field (the most specific
configured value wins)::

    global   [mood][category]        (lighting store: global_profiles)
      -> room  [mood][category]      (rooms[area].mood_overrides)
        -> light [mood][light/group] (rooms[area].light_overrides)

Moods can inherit too: a mood's global profile inherits every field it does
not set from its *parent* mood, then from the *fallback* mood (for example
``Default``). The chain is followed only at the global level; room and light
overrides belong to one mood. Loops are refused when saved and cut off when
resolving.

Field rules:

* ``Inherit`` (action) or an empty field takes the value of the less specific
  level. If no level sets an action, the result is ``Ignore``.
* ``Ignore`` means no command is sent to the light.
* Colour temperature, RGB and HS are one setting: a more specific level that
  sets one of them replaces an inherited one of the others.
* ``Off`` drops brightness, colour and effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .const import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    TRANSITION_MAX,
    TRANSITION_MIN,
    LightAction,
)
from .lighting_config import LightingConfig, table_get
from .models import ProfileSetting

COLOR_FIELDS = ("color_temp_kelvin", "rgb_color", "hs_color")
VALUE_FIELDS = ("brightness_pct", *COLOR_FIELDS, "effect", "transition", "persistent")
# Dropped when the action is Off.
_ON_ONLY = ("brightness_pct", *COLOR_FIELDS, "effect")

LEVELS = ("global", "room", "light")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_setting(setting: ProfileSetting) -> dict[str, str]:
    """Validate one profile entry. Returns {field: error_key}."""
    errors: dict[str, str] = {}
    b = setting.brightness_pct
    if b is not None and (not _is_int(b) or not BRIGHTNESS_MIN <= b <= BRIGHTNESS_MAX):
        errors["brightness_pct"] = "invalid_brightness"
    k = setting.color_temp_kelvin
    if k is not None and (not _is_int(k) or not KELVIN_MIN <= k <= KELVIN_MAX):
        errors["color_temp_kelvin"] = "invalid_kelvin"
    rgb = setting.rgb_color
    if rgb is not None and (
        not isinstance(rgb, tuple) or len(rgb) != 3 or not all(_is_int(c) and 0 <= c <= 255 for c in rgb)
    ):
        errors["rgb_color"] = "invalid_rgb"
    hs = setting.hs_color
    if hs is not None and (
        not isinstance(hs, tuple) or len(hs) != 2
        or not all(_is_number(c) for c in hs)
        or not (0 <= hs[0] <= 360 and 0 <= hs[1] <= 100)
    ):
        errors["hs_color"] = "invalid_hs"
    colours = [f for f in COLOR_FIELDS if getattr(setting, f) is not None]
    if len(colours) > 1 and not any(f in errors for f in colours):
        # light.turn_on accepts only one colour parameter per call.
        errors[colours[-1]] = "several_colors"
    t = setting.transition
    if t is not None and (not _is_number(t) or not TRANSITION_MIN <= t <= TRANSITION_MAX):
        errors["transition"] = "invalid_transition"
    if setting.effect is not None and (not isinstance(setting.effect, str) or not setting.effect.strip()):
        errors["effect"] = "invalid_effect"
    if setting.action == LightAction.OFF and any(getattr(setting, f) is not None for f in _ON_ONLY):
        errors["action"] = "off_with_attributes"
    return errors


def setting_from_form(data: dict[str, Any]) -> ProfileSetting:
    """Build a setting from options-flow input (missing fields mean inherit)."""
    def _int(key: str) -> int | None:
        v = data.get(key)
        return None if v is None or v == "" else int(v)

    rgb = data.get("rgb_color")
    transition = data.get("transition")
    persistent = data.get("persistent")
    return ProfileSetting(
        action=LightAction(data.get("action", LightAction.INHERIT)),
        brightness_pct=_int("brightness_pct"),
        color_temp_kelvin=_int("color_temp_kelvin"),
        rgb_color=tuple(int(c) for c in rgb) if rgb else None,  # type: ignore[arg-type]
        effect=(data.get("effect") or "").strip() or None,
        transition=None if transition in (None, "") else float(transition),
        persistent={"yes": True, "no": False}.get(persistent) if isinstance(persistent, str) else None,
    )


def mood_chain(config: LightingConfig, mood: str | None) -> list[str]:
    """The mood, its parents, then the fallback mood. Loop safe."""
    if not mood:
        return []
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = mood
    while current and current.casefold() not in seen:
        chain.append(current)
        seen.add(current.casefold())
        current = config.parent_of(current)
    fallback = config.fallback_mood
    if fallback and fallback.casefold() not in seen:
        chain.append(fallback)
    return chain


@dataclass(slots=True)
class ResolvedSetting:
    """Effective profile for one light in one room and mood."""

    action: LightAction = LightAction.IGNORE
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    rgb_color: tuple[int, int, int] | None = None
    hs_color: tuple[float, float] | None = None
    effect: str | None = None
    transition: float | None = None
    persistent: bool | None = None
    # field -> {"level": "global" | "room" | "light", "mood": mood}
    sources: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def is_persistent(self) -> bool:
        return bool(self.persistent)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action.value}
        for f in VALUE_FIELDS:
            v = getattr(self, f)
            if v is not None:
                out[f] = list(v) if isinstance(v, tuple) else v
        out["sources"] = {k: dict(v) for k, v in self.sources.items()}
        return out


def _apply(result: ResolvedSetting, layer: ProfileSetting, source: dict[str, str]) -> None:
    if layer.action != LightAction.INHERIT:
        result.action = layer.action
        result.sources["action"] = source
    for f in VALUE_FIELDS:
        v = getattr(layer, f)
        if v is None:
            continue
        if f in COLOR_FIELDS:
            for other in COLOR_FIELDS:
                if other != f:
                    setattr(result, other, None)
                    result.sources.pop(other, None)
        setattr(result, f, v)
        result.sources[f] = source


def layers(
    config: LightingConfig,
    mood: str,
    area_id: str | None,
    light_entity_id: str | None,
    category_key: str | None,
    *,
    levels: tuple[str, ...] = LEVELS,
) -> list[tuple[dict[str, str], ProfileSetting]]:
    """The configured entries that apply, least specific first."""
    out: list[tuple[dict[str, str], ProfileSetting]] = []
    if "global" in levels and category_key:
        for m in reversed(mood_chain(config, mood)):
            if (entry := table_get(config.global_profiles, m).get(category_key)) is not None:
                out.append(({"level": "global", "mood": m}, entry))
    room = config.room(area_id) if area_id else None
    if room is not None and "room" in levels and category_key:
        if (entry := table_get(room.mood_overrides, mood).get(category_key)) is not None:
            out.append(({"level": "room", "mood": mood}, entry))
    if room is not None and "light" in levels and light_entity_id:
        if (entry := table_get(room.light_overrides, mood).get(light_entity_id)) is not None:
            out.append(({"level": "light", "mood": mood}, entry))
    return out


def resolve(
    config: LightingConfig,
    mood: str,
    area_id: str | None,
    light_entity_id: str | None,
    category_key: str | None,
    *,
    levels: tuple[str, ...] = LEVELS,
) -> ResolvedSetting:
    """Resolve the effective setting through the inheritance chain.

    ``levels`` limits the levels used, e.g. ``("global",)`` gives what a room
    override would inherit.
    """
    result = ResolvedSetting()
    for source, layer in layers(config, mood, area_id, light_entity_id, category_key, levels=levels):
        _apply(result, layer, source)
    if result.action == LightAction.OFF:
        for f in _ON_ONLY:
            setattr(result, f, None)
            result.sources.pop(f, None)
    return result


def iter_profile_entries(config: LightingConfig) -> Iterator[tuple[str, str | None, str, str, ProfileSetting]]:
    """Yield (scope, area_id|None, mood, target, setting) for every stored entry."""
    for mood, targets in config.global_profiles.items():
        for target, setting in targets.items():
            yield "global", None, mood, target, setting
    for area_id, room in config.rooms.items():
        for mood, targets in room.mood_overrides.items():
            for target, setting in targets.items():
                yield "room", area_id, mood, target, setting
        for mood, targets in room.light_overrides.items():
            for target, setting in targets.items():
                yield "light", area_id, mood, target, setting
