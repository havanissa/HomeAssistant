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
          "dismissed": [problem subjects],
          "daylight": {...},                           # 0.3.3: daylight-aware switch-on (daylight.RoomDaylight)
          "switches": [{...}],                         # 0.3.5: wall switches / remotes (switches.SwitchConfig)
          "vacancy": {"action": "turn_off", "mood": null},  # 0.3.8: when the room becomes vacant
          "preliminary": {"enabled": null, "timeout": null, "on_below": null,   # 0.3.9: possible occupancy
                          "signals": null, "targets": [{...}]}}},          #   lighting (preliminary.RoomPreliminary)
      "daylight": {"outdoor_entity": null, ...},     # 0.3.3: global daylight settings (daylight.DaylightConfig)
      "switch_settings": {"mqtt_base_topic": "zigbee2mqtt"},  # 0.3.5 (switches.SwitchSettings)
      "preliminary": {"enabled": false, "timeout": 30, ...}   # 0.3.9: global defaults (preliminary.PreliminaryDefaults)
    }

Profile tables may contain the pseudo mood ``"*"`` (every mood, 0.3.2).

On first start the profiles are copied from the Phase 0.2 options (which are
left untouched, so a downgrade still finds them). Every room starts with
lighting control disabled (Observe).

0.3.2 only adds keys (``manual_option``, ``adaptive``, the adjustment modes
of profile entries), so the store version is unchanged: a 0.3.1 store loads
as is, keeps every fixed value fixed, and has no global source configured.

Store 1.2 (0.3.3) adds ``daylight`` (global and per room). The migration from
1.1 keeps the switch-on timing of rooms that were already activated: they wait
for confirmed occupancy as before (``on_first_motion: false``); every other
room gets the new defaults. All other values are kept. A 0.3.2 install reads a
1.2 store as is (same major version) and ignores the new keys.

Store 1.3 (0.3.5) adds wall switches: ``switches`` per room (none after the
migration) and the global ``switch_settings``. The existing wall switch
entities (``manual_switches``) are kept unchanged and stay listen-only
sources: nothing is converted into a command source. A 0.3.4 install reads a
1.3 store as is and ignores the new keys.

Store 1.4 (0.3.8) adds ``vacancy`` per room: what automatic lighting does
when the room becomes vacant (Turn off, a vacant mood, or Leave unchanged).
Before 0.3.8 nothing was done (the room's lights were left on), and there was
no setting to keep. The migration gives every room without one the default,
Turn off; ambient (persistent) lights are never switched off by it, and rooms
in Observing still send nothing. A room that has a setting keeps it. A 0.3.7
install reads a 1.4 store as is and ignores the new key.

Store 1.5 (0.3.9) adds possible occupancy lighting: the global defaults
(``preliminary``, disabled) and, per room, ``preliminary`` without any
light. A room changes nothing until it is enabled *and* has a preliminary
light, so no room gets extra lighting from the migration, and later changes
of the global defaults never overwrite a room's own values. A 0.3.8 install
reads a 1.5 store as is and ignores the new keys.
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
    DEFAULT_VACANCY_ACTION,
    SourceScale,
    UnattributedPolicy,
    UnrepresentableMode,
    VacancyAction,
)
from .const import LIGHTING_STORE_MINOR_VERSION
from .daylight import DaylightConfig, RoomDaylight
from .models import IntegrationConfig, ProfileSetting, ProfileTable, _enum_or
from .preliminary import PreliminaryDefaults, RoomPreliminary
from .switches import ROOM_TARGET, SwitchConfig, SwitchSettings, switches_from

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
class RoomVacancy:
    """What automatic lighting does when the room becomes vacant (0.3.8).

    ``mood`` is the mood whose profile is applied for ``vacant_mood`` (the
    existing profile system: light types, room and light overrides). It is
    kept when another action is chosen, so switching back restores it.
    """

    action: VacancyAction = DEFAULT_VACANCY_ACTION
    mood: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> RoomVacancy:
        data = data if isinstance(data, dict) else {}
        mood = data.get("mood")
        return cls(
            action=_enum_or(VacancyAction, data.get("action"), DEFAULT_VACANCY_ACTION),
            mood=(mood.strip() or None) if isinstance(mood, str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"action": self.action.value, "mood": self.mood}


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
    # Daylight-aware automatic switch-on (0.3.3).
    daylight: RoomDaylight = field(default_factory=RoomDaylight)
    # Wall switches and remotes (0.3.5); each starts Observing (mode "listen").
    switches: tuple[SwitchConfig, ...] = ()
    # When the room becomes vacant (0.3.8).
    vacancy: RoomVacancy = field(default_factory=RoomVacancy)
    # Possible occupancy lighting (0.3.9).
    preliminary: RoomPreliminary = field(default_factory=RoomPreliminary)

    def switch(self, switch_id: str) -> SwitchConfig | None:
        return next((s for s in self.switches if s.id == switch_id), None)

    def with_switch(self, switch: SwitchConfig) -> RoomLighting:
        """Add or replace one switch (by id)."""
        if any(s.id == switch.id for s in self.switches):
            return replace(self, switches=tuple(switch if s.id == switch.id else s for s in self.switches))
        return replace(self, switches=(*self.switches, switch))

    def without_switch(self, switch_id: str) -> RoomLighting:
        return replace(self, switches=tuple(s for s in self.switches if s.id != switch_id))

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
            daylight=RoomDaylight.from_dict(data.get("daylight")),
            switches=switches_from(data.get("switches")),
            vacancy=RoomVacancy.from_dict(data.get("vacancy")),
            preliminary=RoomPreliminary.from_dict(data.get("preliminary")),
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
            "daylight": self.daylight.as_dict(),
            "switches": [s.as_dict() for s in self.switches],
            "vacancy": self.vacancy.as_dict(),
            "preliminary": self.preliminary.as_dict(),
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
    daylight: DaylightConfig = field(default_factory=DaylightConfig)
    switch_settings: SwitchSettings = field(default_factory=SwitchSettings)
    # Possible occupancy lighting: global defaults (0.3.9).
    preliminary: PreliminaryDefaults = field(default_factory=PreliminaryDefaults)

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
            daylight=DaylightConfig.from_dict(data.get("daylight")),
            switch_settings=SwitchSettings.from_dict(data.get("switch_settings")),
            preliminary=PreliminaryDefaults.from_dict(data.get("preliminary")),
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
            "daylight": self.daylight.as_dict(),
            "switch_settings": self.switch_settings.as_dict(),
            "preliminary": self.preliminary.as_dict(),
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

    def sub_daylight(daylight: RoomDaylight) -> RoomDaylight:
        nonlocal changed
        sensors = daylight.sensors
        if any(s.entity_id == old for s in sensors):
            changed = True
            sensors = tuple(replace(s, entity_id=new) if s.entity_id == old else s for s in sensors)
        return replace(daylight, sensors=sensors, influencing=sub_ids(daylight.influencing))

    def sub_switch(switch: SwitchConfig) -> SwitchConfig:
        nonlocal changed
        updated = switch
        if switch.event_entity == old:
            updated = replace(updated, event_entity=new)
        mappings = {}
        for raw, m in switch.mappings.items():
            if m.target == old and m.target != ROOM_TARGET:
                m = replace(m, target=new)
            if m.entity == old:
                m = replace(m, entity=new)
            mappings[raw] = m
        if mappings != switch.mappings:
            updated = replace(updated, mappings=mappings)
        if old in switch.acknowledged:
            updated = replace(updated, acknowledged=sub_ids(switch.acknowledged))
        if updated != switch:
            changed = True
        return updated

    def sub_preliminary(prelim: RoomPreliminary) -> RoomPreliminary:
        nonlocal changed
        targets = prelim.targets
        if any(t.entity_id == old for t in targets):
            changed = True
            targets = tuple(replace(t, entity_id=new) if t.entity_id == old else t for t in targets)
        signals = sub_ids(prelim.signals) if prelim.signals is not None else None
        return replace(prelim, targets=targets, signals=signals)

    rooms = {
        area_id: replace(
            room,
            preliminary=sub_preliminary(room.preliminary),
            acknowledged=sub_ids(room.acknowledged),
            excluded=sub_ids(room.excluded),
            manual_switches=sub_ids(room.manual_switches),
            light_overrides=sub_table(room.light_overrides),
            daylight=sub_daylight(room.daylight),
            switches=tuple(sub_switch(s) for s in room.switches),
        )
        for area_id, room in config.rooms.items()
    }
    adaptive = config.adaptive
    for key in ("brightness_entity", "color_temp_entity"):
        if getattr(adaptive, key) == old:
            adaptive = replace(adaptive, **{key: new})
            changed = True
    daylight = config.daylight
    if daylight.outdoor_entity == old:
        daylight = replace(daylight, outdoor_entity=new)
        changed = True
    return replace(config, rooms=rooms, adaptive=adaptive, daylight=daylight) if changed else None


def migrate_store(old_major: int, old_minor: int, data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade the stored lighting configuration (pure function)."""
    if old_major != LIGHTING_STORE_VERSION:
        raise NotImplementedError
    data = dict(data or {})
    if old_minor < 2:
        rooms = {}
        for area_id, room in (data.get("rooms") or {}).items():
            room = dict(room) if isinstance(room, dict) else {}
            if "daylight" not in room:
                # Activated rooms keep their accepted timing: switch on at confirmed occupancy.
                room["daylight"] = {"on_first_motion": not bool(room.get("control_enabled"))}
            rooms[area_id] = room
        data["rooms"] = rooms
    if old_minor < 3:
        # Wall switches (0.3.5): no room has one yet. Wall switch entities stay listen-only.
        rooms = {}
        for area_id, room in (data.get("rooms") or {}).items():
            room = dict(room) if isinstance(room, dict) else {}
            room.setdefault("switches", [])
            rooms[area_id] = room
        data["rooms"] = rooms
    if old_minor < 4:
        # Vacancy action (0.3.8): there was no setting before (lights were left on). Rooms get the default,
        # Turn off; ambient (persistent) lights are never switched off by it. An existing value is kept.
        rooms = {}
        for area_id, room in (data.get("rooms") or {}).items():
            room = dict(room) if isinstance(room, dict) else {}
            if not isinstance(room.get("vacancy"), dict):
                room["vacancy"] = {"action": DEFAULT_VACANCY_ACTION.value, "mood": None}
            rooms[area_id] = room
        data["rooms"] = rooms
    if old_minor < 5:
        # Possible occupancy lighting (0.3.9): disabled, and no room has a preliminary light, so nothing changes.
        if not isinstance(data.get("preliminary"), dict):
            data["preliminary"] = PreliminaryDefaults().as_dict()
        rooms = {}
        for area_id, room in (data.get("rooms") or {}).items():
            room = dict(room) if isinstance(room, dict) else {}
            if not isinstance(room.get("preliminary"), dict):
                room["preliminary"] = RoomPreliminary().as_dict()
            rooms[area_id] = room
        data["rooms"] = rooms
    return data


class _Store(Store[dict[str, Any]]):
    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int,
                                  old_data: dict[str, Any]) -> dict[str, Any]:
        migrated = migrate_store(old_major_version, old_minor_version, old_data)
        _LOGGER.info("lighting_config_migrated from=%s.%s to=%s.%s rooms=%d", old_major_version,
                     old_minor_version, LIGHTING_STORE_VERSION, LIGHTING_STORE_MINOR_VERSION,
                     len(migrated.get("rooms") or {}))
        return migrated


class LightingStore:
    """Loads, holds and saves the lighting configuration; notifies on change."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = _Store(
            hass, LIGHTING_STORE_VERSION, LIGHTING_STORE_KEY, minor_version=LIGHTING_STORE_MINOR_VERSION)
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
