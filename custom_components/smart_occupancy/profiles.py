"""Mood profiles: validation and inheritance resolution.

Three configuration levels, merged field by field (the most specific
configured value wins)::

    global   [mood][target]          (lighting store: global_profiles)
      -> room  [mood][target]        (rooms[area].mood_overrides)
        -> light [mood][light/group] (rooms[area].light_overrides)

A target is a light category ("spottar", "uplight"), the RGB tag ("rgb"), or
the RGB-tagged part of a category ("uplight+rgb"). A light carrying several
labels gets the entries of all its targets, merged from the least to the most
specific (see ``LightIndex.profile_targets``).

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

Global adjustment (0.3.2):

* Brightness and colour temperature each have a mode: fixed, global,
  global + offset, ignore; not set = inherit. A value without a mode is
  fixed, so a fixed entry still overrides a "follow global" entry below it.
* A "follow global" colour temperature belongs to the colour setting: a more
  specific RGB/HS colour replaces it, and it replaces an inherited RGB/HS.
* Entries for the pseudo mood ``*`` apply in every mood, below the entries
  of the mood itself, at each level: global ``*`` < global mood chain <
  room ``*`` < room mood < light ``*`` < light mood. They cannot set an
  action, so they never switch a light on or off by themselves.
* The global values are filled in afterwards (``adaptive.apply_global``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .const import (
    ALL_MOODS,
    BRIGHTNESS_MAX,
    BRIGHTNESS_OFFSET_MAX,
    KELVIN_OFFSET_MAX,
    BRIGHTNESS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    TRANSITION_MAX,
    TRANSITION_MIN,
    AdjustMode,
    LightAction,
)
from .lighting_config import LightingConfig, table_get
from .models import ProfileSetting

COLOR_FIELDS = ("color_temp_kelvin", "rgb_color", "hs_color")
VALUE_FIELDS = ("brightness_pct", *COLOR_FIELDS, "effect", "transition", "persistent")
MODE_FIELDS = ("brightness_mode", "brightness_offset", "color_temp_mode", "color_temp_offset")
_CT_FIELDS = ("color_temp_kelvin", "color_temp_mode", "color_temp_offset")
# Dropped when the action is Off.
_ON_ONLY = ("brightness_pct", *COLOR_FIELDS, "effect", *MODE_FIELDS)
# Not allowed in "every mood" entries.
_NOT_FOR_ALL_MOODS = ("effect", "persistent")

LEVELS = ("global", "room", "light")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_setting(setting: ProfileSetting, mood: str | None = None) -> dict[str, str]:
    """Validate one profile entry. Returns {field: error_key}."""
    errors: dict[str, str] = {}
    for f, limit in (("brightness_offset", BRIGHTNESS_OFFSET_MAX), ("color_temp_offset", KELVIN_OFFSET_MAX)):
        v = getattr(setting, f)
        if v is not None and (not _is_int(v) or not -limit <= v <= limit):
            errors[f] = "invalid_brightness_offset" if f == "brightness_offset" else "invalid_kelvin_offset"
    if mood == ALL_MOODS:
        if setting.action not in (LightAction.INHERIT,):
            errors["action"] = "all_moods_action"
        for f in _NOT_FOR_ALL_MOODS:
            if getattr(setting, f) is not None:
                errors[f] = "all_moods_field"
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
    if setting.color_temp_mode in (AdjustMode.GLOBAL, AdjustMode.OFFSET, AdjustMode.FIXED) \
            and "color_temp_kelvin" not in colours and len(colours) == 1:
        colours.insert(0, "color_temp_mode")
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
    modes = {f: AdjustMode(data[f]) for f in ("brightness_mode", "color_temp_mode") if data.get(f)}
    persistent = data.get("persistent")
    return ProfileSetting(
        action=LightAction(data.get("action", LightAction.INHERIT)),
        brightness_pct=_int("brightness_pct"),
        color_temp_kelvin=_int("color_temp_kelvin"),
        rgb_color=tuple(int(c) for c in rgb) if rgb else None,  # type: ignore[arg-type]
        effect=(data.get("effect") or "").strip() or None,
        transition=None if transition in (None, "") else float(transition),
        persistent={"yes": True, "no": False}.get(persistent) if isinstance(persistent, str) else None,
        brightness_offset=_int("brightness_offset"),
        color_temp_offset=_int("color_temp_offset"),
        **modes,
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
    brightness_mode: AdjustMode | None = None
    brightness_offset: int | None = None
    color_temp_mode: AdjustMode | None = None
    color_temp_offset: int | None = None
    # field -> {"level": "global" | "room" | "light", "mood": mood}
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    # How the global adjustment was applied (``adaptive.apply_global``); None before.
    adaptive: dict[str, Any] | None = None

    @property
    def is_persistent(self) -> bool:
        return bool(self.persistent)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action.value}
        for f in (*VALUE_FIELDS, *MODE_FIELDS):
            v = getattr(self, f)
            if v is not None:
                out[f] = list(v) if isinstance(v, tuple) else v.value if isinstance(v, AdjustMode) else v
        out["sources"] = {k: dict(v) for k, v in self.sources.items()}
        if self.adaptive is not None:
            out["adaptive"] = {k: dict(v) for k, v in self.adaptive.items()}
        return out


def _set(result: ResolvedSetting, f: str, value: Any, source: dict[str, str]) -> None:
    setattr(result, f, value)
    if value is None:
        result.sources.pop(f, None)
    else:
        result.sources[f] = source


def _apply(result: ResolvedSetting, layer: ProfileSetting, source: dict[str, str]) -> None:
    if layer.action != LightAction.INHERIT:
        result.action = layer.action
        result.sources["action"] = source
    # A white mode (fixed or following) is a colour choice: it replaces an inherited RGB/HS.
    white = layer.color_temp_mode in (AdjustMode.FIXED, AdjustMode.GLOBAL, AdjustMode.OFFSET)
    if white:
        for other in ("rgb_color", "hs_color"):
            _set(result, other, None, source)
    for f in VALUE_FIELDS:
        v = getattr(layer, f)
        if v is None:
            continue
        if f in COLOR_FIELDS:
            for other in COLOR_FIELDS:
                if other != f:
                    _set(result, other, None, source)
            if f != "color_temp_kelvin":  # RGB/HS replaces any white setting, following or not
                for ct in ("color_temp_mode", "color_temp_offset"):
                    _set(result, ct, None, source)
        _set(result, f, v, source)
    # Modes: an explicit mode wins; a value without a mode is fixed (pre-0.3.2 behaviour).
    # (An implied fixed mode has no source of its own: it comes with the value.)
    if layer.brightness_mode is not None:
        _set(result, "brightness_mode", layer.brightness_mode, source)
    elif layer.brightness_pct is not None:
        result.brightness_mode = AdjustMode.FIXED
        result.sources.pop("brightness_mode", None)
    if layer.color_temp_mode is not None:
        _set(result, "color_temp_mode", layer.color_temp_mode, source)
    elif layer.color_temp_kelvin is not None:
        result.color_temp_mode = AdjustMode.FIXED
        result.sources.pop("color_temp_mode", None)
    for f in ("brightness_offset", "color_temp_offset"):
        if (v := getattr(layer, f)) is not None:
            _set(result, f, v, source)


Targets = str | list[str] | tuple[str, ...] | None


def _targets(targets: Targets) -> list[str]:
    if targets is None:
        return []
    return [targets] if isinstance(targets, str) else list(targets)


def layers(
    config: LightingConfig,
    mood: str,
    area_id: str | None,
    light_entity_id: str | None,
    targets: Targets,
    *,
    levels: tuple[str, ...] = LEVELS,
) -> list[tuple[dict[str, str], ProfileSetting]]:
    """The configured entries that apply, least specific first.

    ``targets`` are the light's profile targets, least specific first (see
    ``LightIndex.profile_targets``); a single category key is accepted too.
    Order: the mood chain (fallback, parents, the mood itself), and within a
    mood the targets; then the room override for the targets; then the light.
    """
    keys = _targets(targets)
    out: list[tuple[dict[str, str], ProfileSetting]] = []
    every = mood != ALL_MOODS  # resolving "*" itself: only the "*" entries

    def add_all(level: str, table, target_keys: list[str]) -> None:
        entries = table.get(ALL_MOODS, {})
        for key in target_keys:
            if (entry := entries.get(key)) is not None:
                out.append(({"level": level, "mood": ALL_MOODS, "target": key}, entry))

    if "global" in levels and keys:
        add_all("global", config.global_profiles, keys)
        for m in reversed(mood_chain(config, mood) if every else []):
            entries = table_get(config.global_profiles, m)
            for key in keys:
                if (entry := entries.get(key)) is not None:
                    out.append(({"level": "global", "mood": m, "target": key}, entry))
    room = config.room(area_id) if area_id else None
    if room is not None and "room" in levels and keys:
        add_all("room", room.mood_overrides, keys)
        entries = table_get(room.mood_overrides, mood) if every else {}
        for key in keys:
            if (entry := entries.get(key)) is not None:
                out.append(({"level": "room", "mood": mood, "target": key}, entry))
    if room is not None and "light" in levels and light_entity_id:
        add_all("light", room.light_overrides, [light_entity_id])
        if every and (entry := table_get(room.light_overrides, mood).get(light_entity_id)) is not None:
            out.append(({"level": "light", "mood": mood, "target": light_entity_id}, entry))
    return out


def resolve(
    config: LightingConfig,
    mood: str,
    area_id: str | None,
    light_entity_id: str | None,
    targets: Targets,
    *,
    levels: tuple[str, ...] = LEVELS,
) -> ResolvedSetting:
    """Resolve the effective setting through the inheritance chain.

    ``levels`` limits the levels used, e.g. ``("global",)`` gives what a room
    override would inherit.
    """
    result = ResolvedSetting()
    for source, layer in layers(config, mood, area_id, light_entity_id, targets, levels=levels):
        _apply(result, layer, source)
    if result.action == LightAction.OFF:
        for f in _ON_ONLY:
            setattr(result, f, None)
            result.sources.pop(f, None)
    if result.brightness_mode != AdjustMode.OFFSET:
        _set(result, "brightness_offset", None, {})
    if result.color_temp_mode != AdjustMode.OFFSET:
        _set(result, "color_temp_offset", None, {})
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
