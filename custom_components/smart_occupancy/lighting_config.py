"""Mood lighting configuration, owned and persisted by the integration.

Stored in ``.storage/smart_occupancy.lighting`` (a Home Assistant ``Store``),
not in the config entry options, so the dashboard editors can save changes
without reloading the integration. Settings work without any dashboard open.

Persisted shape (store version 1)::

    {
      "global_profiles": {mood: {category_key: setting}},
      "parents": {mood: parent_mood},
      "fallback_mood": "Default" | null,
      "unrepresentable": {mood: {"mode": "follow_main", "mood": null}},
      "exclude_label": "label_id" | null,
      "manual_override_minutes": 60,
      "unattributed": "ignore",
      "manual_option": "Manual",                    # 0.3.2: Main Mood option that pauses lighting
      "adaptive": {                                 # 0.3.2: existing global adjustment sources
          "brightness_entity": null, "brightness_attribute": null, "brightness_scale": "auto",
          "color_temp_entity": null, "color_temp_attribute": null, "color_temp_scale": "auto",
          "min_interval": 60, "brightness_step": 3, "kelvin_step": 100, "transition": 10},
      "rooms": {area_id: {
          "control_enabled": false, "activated_at": iso | null, "activated_by": user_id | null,
          "acknowledged": [automation entity ids],
          "mood_override": mood | null,
          "auto_discover": true,
          "excluded": [light entity ids],
          "mood_overrides": {mood: {category_key: setting}},
          "light_overrides": {mood: {light entity id: setting}},
          "manual_override_minutes": null,
          "manual_switches": [entity ids],
          "dismissed": [problem subjects]}}
    }

Profile tables may contain the pseudo mood ``"*"`` (every mood, 0.3.2).

On first start the profiles are copied from the Phase 0.2 options (which are
left untouched, so a downgrade still finds them). Every room starts with
lighting control disabled (Observe).

0.3.2 only adds keys (``manual_option``, ``adaptive``, the adjustment modes
of profile entries), so the store version is unchanged: a 0.3.1 store loads
as is, keeps every fixed value fixed, and has no global source configured.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    ADAPTIVE_BRIGHTNESS_STEP_MAX,
    ADAPTIVE_KELVIN_STEP_MAX,
    ADAPTIVE_MIN_INTERVAL_MAX,
    DEFAULT_ADAPTIVE_BRIGHTNESS_STEP,
    DEFAULT_ADAPTIVE_KELVIN_STEP,
    DEFAULT_ADAPTIVE_MIN_INTERVAL,
    DEFAULT_ADAPTIVE_TRANSITION,
    DEFAULT_MANUAL_OPTION,
    DEFAULT_MANUAL_OVERRIDE_MINUTES,
    LIGHTING_SAVE_DELAY_SECONDS,
    LIGHTING_STORE_KEY,
    LIGHTING_STORE_VERSION,
    MANUAL_OVERRIDE_MAX_MINUTES,
    TRANSITION_MAX,
    SourceScale,
    UnattributedPolicy,
    UnrepresentableMode,
)
from .models import IntegrationConfig, ProfileSetting, ProfileTable, _enum_or

_LOGGER = logging.getLogger(__name__)


def _norm(value: str | None) -> str:
    return value.casefold().strip() if isinstance(value, str) else ""


def table_get(table: ProfileTable, mood: str | None) -> dict[str, ProfileSetting]:
    """Entries of one mood; mood names match case-insensitively."""
    if not mood:
        return {}
    if mood in table:
        return table[mood]
    key = _norm(mood)
    return next((targets for m, targets in table.items() if _norm(m) == key), {})


def _table_from(data: Any) -> ProfileTable:
    if not isinstance(data, dict):
        return {}
    return {
        str(mood): {str(t): ProfileSetting.from_dict(s) for t, s in targets.items() if isinstance(s, dict)}
        for mood, targets in data.items()
        if isinstance(targets, dict)
    }


def _table_as_dict(table: ProfileTable) -> dict[str, Any]:
    return {
        mood: {target: s.as_dict() for target, s in targets.items()}
        for mood, targets in table.items()
        if targets
    }


def _minutes(value: Any, default: int | None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return min(max(int(value), 0), MANUAL_OVERRIDE_MAX_MINUTES)
    except (TypeError, ValueError):
        return default


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(v for v in value if isinstance(v, str) and v))


def _bounded(value: Any, default: float, low: float, high: float, cast: type = int) -> Any:
    if value is None or isinstance(value, bool):
        return default
    try:
        return cast(min(max(float(value), low), high))
    except (TypeError, ValueError):
        return default


def _entity_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and "." in value.strip() else None


@dataclass(frozen=True, slots=True)
class AdaptiveConfig:
    """Bindings to the existing global brightness / colour temperature system.

    Each source is optional and independent. ``*_attribute`` reads an
    attribute instead of the state (e.g. ``brightness_pct`` of an Adaptive
    Lighting switch); ``None`` picks it automatically.
    """

    brightness_entity: str | None = None
    brightness_attribute: str | None = None
    brightness_scale: SourceScale = SourceScale.AUTO
    color_temp_entity: str | None = None
    color_temp_attribute: str | None = None
    color_temp_scale: SourceScale = SourceScale.AUTO
    min_interval: int = DEFAULT_ADAPTIVE_MIN_INTERVAL
    brightness_step: int = DEFAULT_ADAPTIVE_BRIGHTNESS_STEP
    kelvin_step: int = DEFAULT_ADAPTIVE_KELVIN_STEP
    transition: float = DEFAULT_ADAPTIVE_TRANSITION

    @classmethod
    def from_dict(cls, data: Any) -> AdaptiveConfig:
        data = data if isinstance(data, dict) else {}
        return cls(
            brightness_entity=_entity_id(data.get("brightness_entity")),
            brightness_attribute=data.get("brightness_attribute") or None,
            brightness_scale=_enum_or(SourceScale, data.get("brightness_scale"), SourceScale.AUTO),
            color_temp_entity=_entity_id(data.get("color_temp_entity")),
            color_temp_attribute=data.get("color_temp_attribute") or None,
            color_temp_scale=_enum_or(SourceScale, data.get("color_temp_scale"), SourceScale.AUTO),
            min_interval=_bounded(data.get("min_interval"), DEFAULT_ADAPTIVE_MIN_INTERVAL, 0,
                                  ADAPTIVE_MIN_INTERVAL_MAX),
            brightness_step=_bounded(data.get("brightness_step"), DEFAULT_ADAPTIVE_BRIGHTNESS_STEP, 1,
                                     ADAPTIVE_BRIGHTNESS_STEP_MAX),
            kelvin_step=_bounded(data.get("kelvin_step"), DEFAULT_ADAPTIVE_KELVIN_STEP, 10, ADAPTIVE_KELVIN_STEP_MAX),
            transition=_bounded(data.get("transition"), DEFAULT_ADAPTIVE_TRANSITION, 0, TRANSITION_MAX, float),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "brightness_entity": self.brightness_entity,
            "brightness_attribute": self.brightness_attribute,
            "brightness_scale": self.brightness_scale.value,
            "color_temp_entity": self.color_temp_entity,
            "color_temp_attribute": self.color_temp_attribute,
            "color_temp_scale": self.color_temp_scale.value,
            "min_interval": self.min_interval,
            "brightness_step": self.brightness_step,
            "kelvin_step": self.kelvin_step,
            "transition": self.transition,
        }

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(e for e in (self.brightness_entity, self.color_temp_entity) if e)


@dataclass(frozen=True, slots=True)
class UnrepresentableHandling:
    """Handling of a mood that Main Mood cannot represent (e.g. ``Onki``)."""

    mode: UnrepresentableMode = UnrepresentableMode.FOLLOW_MAIN
    mood: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> UnrepresentableHandling:
        data = data if isinstance(data, dict) else {}
        return cls(
            mode=_enum_or(UnrepresentableMode, data.get("mode"), UnrepresentableMode.FOLLOW_MAIN),
            mood=data.get("mood") or None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode.value, "mood": self.mood}


@dataclass(frozen=True, slots=True)
class RoomLighting:
    """Mood lighting settings of one room."""

    control_enabled: bool = False
    activated_at: str | None = None
    activated_by: str | None = None
    # Automations the user accepted as co-controllers of this room's lights.
    acknowledged: tuple[str, ...] = ()
    # A mood for this room only; None follows the global (Main) mood.
    mood_override: str | None = None
    # Lights in the room's area that carry a category label are managed too.
    auto_discover: bool = True
    excluded: tuple[str, ...] = ()
    mood_overrides: ProfileTable = field(default_factory=dict)
    light_overrides: ProfileTable = field(default_factory=dict)
    # None: use the global value.
    manual_override_minutes: int | None = None
    # Wall switch / remote entities: a light change right after one of them
    # changed is attributed to a person at the switch.
    manual_switches: tuple[str, ...] = ()
    # Warnings the user chose to ignore ("Ignore warning for now"); shown as notes.
    dismissed: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> RoomLighting:
        data = data if isinstance(data, dict) else {}
        return cls(
            control_enabled=bool(data.get("control_enabled", False)),
            activated_at=data.get("activated_at"),
            activated_by=data.get("activated_by"),
            acknowledged=_ids(data.get("acknowledged")),
            mood_override=data.get("mood_override") or None,
            auto_discover=bool(data.get("auto_discover", True)),
            excluded=_ids(data.get("excluded")),
            mood_overrides=_table_from(data.get("mood_overrides")),
            light_overrides=_table_from(data.get("light_overrides")),
            manual_override_minutes=_minutes(data.get("manual_override_minutes"), None),
            manual_switches=_ids(data.get("manual_switches")),
            dismissed=_ids(data.get("dismissed")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "control_enabled": self.control_enabled,
            "activated_at": self.activated_at,
            "activated_by": self.activated_by,
            "acknowledged": list(self.acknowledged),
            "mood_override": self.mood_override,
            "auto_discover": self.auto_discover,
            "excluded": list(self.excluded),
            "mood_overrides": _table_as_dict(self.mood_overrides),
            "light_overrides": _table_as_dict(self.light_overrides),
            "manual_override_minutes": self.manual_override_minutes,
            "manual_switches": list(self.manual_switches),
            "dismissed": list(self.dismissed),
        }


@dataclass(frozen=True, slots=True)
class LightingConfig:
    """The complete mood lighting configuration."""

    global_profiles: ProfileTable = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    fallback_mood: str | None = None
    unrepresentable: dict[str, UnrepresentableHandling] = field(default_factory=dict)
    exclude_label: str | None = None
    manual_override_minutes: int = DEFAULT_MANUAL_OVERRIDE_MINUTES
    unattributed: UnattributedPolicy = UnattributedPolicy.IGNORE
    rooms: dict[str, RoomLighting] = field(default_factory=dict)
    # Main Mood option that pauses all automatic lighting (not a mood).
    manual_option: str = DEFAULT_MANUAL_OPTION
    adaptive: AdaptiveConfig = field(default_factory=AdaptiveConfig)

    @classmethod
    def from_dict(cls, data: Any) -> LightingConfig:
        data = data if isinstance(data, dict) else {}
        parents = data.get("parents") if isinstance(data.get("parents"), dict) else {}
        unrep = data.get("unrepresentable") if isinstance(data.get("unrepresentable"), dict) else {}
        rooms = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
        return cls(
            global_profiles=_table_from(data.get("global_profiles")),
            parents={str(m): str(p) for m, p in parents.items() if isinstance(p, str) and p},
            fallback_mood=data.get("fallback_mood") or None,
            unrepresentable={str(m): UnrepresentableHandling.from_dict(h) for m, h in unrep.items()},
            exclude_label=data.get("exclude_label") or None,
            manual_override_minutes=_minutes(
                data.get("manual_override_minutes"), DEFAULT_MANUAL_OVERRIDE_MINUTES
            ) or 0,
            unattributed=_enum_or(UnattributedPolicy, data.get("unattributed"), UnattributedPolicy.IGNORE),
            rooms={str(a): RoomLighting.from_dict(r) for a, r in rooms.items()},
            manual_option=str(data.get("manual_option") or DEFAULT_MANUAL_OPTION).strip() or DEFAULT_MANUAL_OPTION,
            adaptive=AdaptiveConfig.from_dict(data.get("adaptive")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "global_profiles": _table_as_dict(self.global_profiles),
            "parents": dict(self.parents),
            "fallback_mood": self.fallback_mood,
            "unrepresentable": {m: h.as_dict() for m, h in self.unrepresentable.items()},
            "exclude_label": self.exclude_label,
            "manual_override_minutes": self.manual_override_minutes,
            "unattributed": self.unattributed.value,
            "rooms": {a: r.as_dict() for a, r in self.rooms.items()},
            "manual_option": self.manual_option,
            "adaptive": self.adaptive.as_dict(),
        }

    def room(self, area_id: str) -> RoomLighting:
        return self.rooms.get(area_id) or RoomLighting()

    def with_room(self, area_id: str, room: RoomLighting) -> LightingConfig:
        return replace(self, rooms={**self.rooms, area_id: room})

    def is_manual(self, mood: str | None) -> bool:
        """The Main Mood value is the Manual option (pauses lighting)."""
        return bool(mood) and _norm(mood) == _norm(self.manual_option)

    def parent_of(self, mood: str) -> str | None:
        if mood in self.parents:
            return self.parents[mood]
        key = _norm(mood)
        return next((p for m, p in self.parents.items() if _norm(m) == key), None)

    def handling_for(self, mood: str) -> UnrepresentableHandling:
        key = _norm(mood)
        return next(
            (h for m, h in self.unrepresentable.items() if _norm(m) == key), UnrepresentableHandling()
        )

    def override_minutes(self, area_id: str) -> int:
        room = self.room(area_id)
        return self.manual_override_minutes if room.manual_override_minutes is None else room.manual_override_minutes

    @property
    def active_rooms(self) -> list[str]:
        return sorted(a for a, r in self.rooms.items() if r.control_enabled)


def seed_from_options(config: IntegrationConfig) -> LightingConfig:
    """Initial lighting configuration: the profiles stored by Phase 0.1/0.2.

    Lighting control starts disabled in every room.
    """
    rooms = {
        area_id: RoomLighting(mood_overrides=room.mood_overrides, light_overrides=room.light_overrides)
        for area_id, room in config.rooms.items()
        if room.mood_overrides or room.light_overrides
    }
    return LightingConfig(global_profiles=config.global_profiles, rooms=rooms)


def set_profile(
    config: LightingConfig,
    scope: str,
    mood: str,
    target: str,
    setting: ProfileSetting | None,
    area_id: str | None = None,
) -> LightingConfig:
    """Store (or with ``None`` remove) one profile entry."""

    def update(table: ProfileTable) -> ProfileTable:
        out = {m: dict(t) for m, t in table.items()}
        # Reuse the stored spelling of the mood name.
        key = next((m for m in out if _norm(m) == _norm(mood)), mood)
        targets = out.setdefault(key, {})
        if setting is None or setting.is_empty:
            targets.pop(target, None)
        else:
            targets[target] = setting
        return {m: t for m, t in out.items() if t}

    if scope == "global":
        return replace(config, global_profiles=update(config.global_profiles))
    if not area_id:
        raise ValueError("area_id is required for room and light entries")
    room = config.room(area_id)
    if scope == "room":
        return config.with_room(area_id, replace(room, mood_overrides=update(room.mood_overrides)))
    if scope == "light":
        return config.with_room(area_id, replace(room, light_overrides=update(room.light_overrides)))
    raise ValueError(f"unknown scope {scope}")


def parent_cycle(config: LightingConfig, mood: str, parent: str | None) -> list[str] | None:
    """The loop that ``mood -> parent`` would create, or None."""
    if not parent:
        return None
    chain = [mood]
    current: str | None = parent
    seen = {_norm(mood)}
    while current:
        chain.append(current)
        if _norm(current) in seen:
            return chain
        seen.add(_norm(current))
        current = config.parent_of(current)
    return None


def without_categories(config: LightingConfig, keys: set[str]) -> LightingConfig:
    """Remove the profile entries of deleted categories."""

    def strip(table: ProfileTable) -> ProfileTable:
        return {m: {t: s for t, s in targets.items() if t not in keys} for m, targets in table.items()}

    return replace(
        config,
        global_profiles=strip(config.global_profiles),
        rooms={a: replace(r, mood_overrides=strip(r.mood_overrides)) for a, r in config.rooms.items()},
    )


def rename_entity(config: LightingConfig, old: str, new: str) -> LightingConfig | None:
    """Follow an entity id rename. None when the entity is not referenced."""
    changed = False

    def sub_ids(ids: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal changed
        if old not in ids:
            return ids
        changed = True
        return tuple(new if i == old else i for i in ids)

    def sub_table(table: ProfileTable) -> ProfileTable:
        nonlocal changed
        out: ProfileTable = {}
        for mood, targets in table.items():
            if old in targets:
                changed = True
            out[mood] = {(new if t == old else t): s for t, s in targets.items()}
        return out

    rooms = {
        area_id: replace(
            room,
            acknowledged=sub_ids(room.acknowledged),
            excluded=sub_ids(room.excluded),
            manual_switches=sub_ids(room.manual_switches),
            light_overrides=sub_table(room.light_overrides),
        )
        for area_id, room in config.rooms.items()
    }
    adaptive = config.adaptive
    for key in ("brightness_entity", "color_temp_entity"):
        if getattr(adaptive, key) == old:
            adaptive = replace(adaptive, **{key: new})
            changed = True
    return replace(config, rooms=rooms, adaptive=adaptive) if changed else None


class LightingStore:
    """Loads, holds and saves the lighting configuration; notifies on change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, LIGHTING_STORE_VERSION, LIGHTING_STORE_KEY)
        self.config = LightingConfig()
        # Bumped on every change; used to invalidate cached resolutions.
        self.revision = 0
        self.seeded = False
        self._listeners: list[Callable[[LightingConfig, LightingConfig], None]] = []
        self._dirty = False

    async def async_load(self, options: IntegrationConfig) -> None:
        data = await self._store.async_load()
        if data is None:
            self.config = seed_from_options(options)
            self.seeded = True
            self._dirty = True
            await self.async_flush()
            _LOGGER.info(
                "lighting_config_created global_moods=%d rooms_with_overrides=%d (copied from options)",
                len(self.config.global_profiles), len(self.config.rooms),
            )
        else:
            self.config = LightingConfig.from_dict(data)

    @callback
    def async_add_listener(self, listener: Callable[[LightingConfig, LightingConfig], None]) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    @callback
    def async_set(self, config: LightingConfig) -> bool:
        """Replace the configuration. Saved shortly after; listeners run now."""
        if config == self.config:
            return False
        old, self.config = self.config, config
        self.revision += 1
        self._dirty = True
        self._store.async_delay_save(self._data, LIGHTING_SAVE_DELAY_SECONDS)
        for listener in list(self._listeners):
            listener(old, config)
        return True

    def _data(self) -> dict[str, Any]:
        self._dirty = False
        return self.config.as_dict()

    async def async_flush(self) -> None:
        if self._dirty:
            await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()
