"""Mood profiles: validation and inheritance resolution.

Inheritance order (most specific wins, field by field)::

    global profile [mood][category]
        -> room override [mood][category]
            -> individual light override [mood][light entity]

Phase 0.1 only stores, validates and resolves profiles. Nothing here sends
commands to lights.
"""

from __future__ import annotations

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
from .models import IntegrationConfig, ProfileSetting

_FIELDS = ("brightness_pct", "color_temp_kelvin", "rgb_color", "effect", "transition")


def validate_setting(setting: ProfileSetting) -> dict[str, str]:
    """Validate one profile entry. Returns {field: error_key}."""
    errors: dict[str, str] = {}
    b = setting.brightness_pct
    if b is not None and (
        not isinstance(b, int) or isinstance(b, bool) or not BRIGHTNESS_MIN <= b <= BRIGHTNESS_MAX
    ):
        errors["brightness_pct"] = "invalid_brightness"
    k = setting.color_temp_kelvin
    if k is not None and (
        not isinstance(k, int) or isinstance(k, bool) or not KELVIN_MIN <= k <= KELVIN_MAX
    ):
        errors["color_temp_kelvin"] = "invalid_kelvin"
    rgb = setting.rgb_color
    if rgb is not None and (
        len(rgb) != 3
        or not all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in rgb)
    ):
        errors["rgb_color"] = "invalid_rgb"
    if k is not None and rgb is not None and "rgb_color" not in errors:
        # light.turn_on rejects colour temperature and RGB in the same call.
        errors["rgb_color"] = "kelvin_and_rgb"
    t = setting.transition
    if t is not None and (
        not isinstance(t, (int, float))
        or isinstance(t, bool)
        or not TRANSITION_MIN <= t <= TRANSITION_MAX
    ):
        errors["transition"] = "invalid_transition"
    if setting.effect is not None and not str(setting.effect).strip():
        errors["effect"] = "invalid_effect"
    if setting.action == LightAction.OFF and any(
        getattr(setting, f) is not None for f in ("brightness_pct", "color_temp_kelvin", "rgb_color", "effect")
    ):
        errors["action"] = "off_with_attributes"
    return errors


def setting_from_form(data: dict[str, Any]) -> ProfileSetting:
    """Build a setting from options-flow input (missing fields mean inherit)."""
    def _int(key: str) -> int | None:
        v = data.get(key)
        return None if v is None or v == "" else int(v)

    rgb = data.get("rgb_color")
    transition = data.get("transition")
    return ProfileSetting(
        action=LightAction(data.get("action", LightAction.INHERIT)),
        brightness_pct=_int("brightness_pct"),
        color_temp_kelvin=_int("color_temp_kelvin"),
        rgb_color=tuple(int(c) for c in rgb) if rgb else None,  # type: ignore[arg-type]
        effect=(data.get("effect") or "").strip() or None,
        transition=None if transition in (None, "") else float(transition),
    )


@dataclass(slots=True)
class ResolvedSetting:
    """Effective profile for one light in one room and mood."""

    action: LightAction = LightAction.IGNORE
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    rgb_color: tuple[int, int, int] | None = None
    effect: str | None = None
    transition: float | None = None
    # field -> "global" | "room" | "light"
    sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action.value}
        for f in _FIELDS:
            v = getattr(self, f)
            if v is not None:
                out[f] = list(v) if isinstance(v, tuple) else v
        out["sources"] = dict(self.sources)
        return out


def resolve(
    config: IntegrationConfig,
    mood: str,
    area_id: str,
    light_entity_id: str,
    category_key: str | None,
) -> ResolvedSetting:
    """Resolve the effective setting for a light through the inheritance chain."""
    room = config.room(area_id)
    layers: list[tuple[str, ProfileSetting | None]] = [
        ("global", config.global_profiles.get(mood, {}).get(category_key) if category_key else None),
        ("room", room.mood_overrides.get(mood, {}).get(category_key) if category_key else None),
        ("light", room.light_overrides.get(mood, {}).get(light_entity_id)),
    ]
    result = ResolvedSetting()
    for source, layer in layers:
        if layer is None:
            continue
        if layer.action != LightAction.INHERIT:
            result.action = layer.action
            result.sources["action"] = source
        for f in _FIELDS:
            v = getattr(layer, f)
            if v is None:
                continue
            # Colour temperature and RGB are mutually exclusive; a more
            # specific layer choosing one clears the other.
            if f == "rgb_color":
                result.color_temp_kelvin = None
                result.sources.pop("color_temp_kelvin", None)
            elif f == "color_temp_kelvin":
                result.rgb_color = None
                result.sources.pop("rgb_color", None)
            setattr(result, f, v)
            result.sources[f] = source
    if result.action == LightAction.OFF:
        for f in ("brightness_pct", "color_temp_kelvin", "rgb_color", "effect"):
            setattr(result, f, None)
            result.sources.pop(f, None)
    return result


def iter_profile_entries(config: IntegrationConfig):
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
