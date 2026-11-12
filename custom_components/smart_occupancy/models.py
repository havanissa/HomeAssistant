"""Configuration model.

The whole integration configuration lives in the config entry ``options`` as
plain JSON. These dataclasses are the typed view of that payload. They are
deliberately independent of Home Assistant so the model can be validated and
unit tested in isolation, and extended in later phases without touching the
runtime modules.

Persisted shape (CONFIG_VERSION 2)::

    {
      "config_version": 2,
      "operating_mode": "observe",
      "mood": {"auto_mood_entity": ..., "manual_mood_entity": ...,
               "main_mood_entity": ..., "auto_option": "Auto",
               "mismatch_grace_seconds": 30},
      "categories": [{"key": "lamps", "name": "Lamps", "label_id": "lamps"}],
      "rooms": {"<area_id>": {"enabled": true,
                              "sensors": [{"entity_id": ..., "kind": "pir",
                                           "role": "trigger",
                                           "inverted": false,
                                           "enabled": false}],   # 0.3.8, only when excluded
                              "lights": ["light.x"],
                              "reference_occupancy_entity": null,
                              "mood_overrides": {mood: {category_key: setting}},
                              "light_overrides": {mood: {entity_id: setting}},
                              "timing": {"exit_confirmation": 120},
                              "adjacent_rooms": ["<area_id>"]}},
      "global_profiles": {mood: {category_key: setting}},
      "occupancy": {"correlation_window": 10, "exit_confirmation": 300, ...}
    }

Version 2 only adds keys, each with a default, so ``migrate_options`` is a
pure function that keeps every existing value.

Since Phase 0.3 the mood profiles (``global_profiles`` and the rooms'
``mood_overrides`` / ``light_overrides``) live in the lighting store
(lighting_config.py), which is seeded once from these keys. The keys are
left in the options unchanged, so a downgrade still finds them.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .const import (
    CONFIG_VERSION,
    FUNCTION_CATEGORY_NAMES,
    RGB_CATEGORY_NAMES,
    AdjustMode,
    CategoryKind,
    DEFAULT_AUTO_OPTION,
    DEFAULT_CATEGORIES,
    DEFAULT_MISMATCH_GRACE_SECONDS,
    DEFAULT_ROLE_FOR_KIND,
    TIMING_PARAMETERS,
    LightAction,
    OperatingMode,
    SensorKind,
    SensorRole,
)


def _enum_or(enum_cls: type, value: Any, default: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class MoodConfig:
    """Which helpers make up the existing mood system."""

    auto_mood_entity: str | None = None
    manual_mood_entity: str | None = None
    main_mood_entity: str | None = None
    auto_option: str = DEFAULT_AUTO_OPTION
    mismatch_grace_seconds: int = DEFAULT_MISMATCH_GRACE_SECONDS

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MoodConfig:
        data = data or {}
        return cls(
            auto_mood_entity=data.get("auto_mood_entity"),
            manual_mood_entity=data.get("manual_mood_entity"),
            main_mood_entity=data.get("main_mood_entity"),
            auto_option=data.get("auto_option") or DEFAULT_AUTO_OPTION,
            mismatch_grace_seconds=int(
                data.get("mismatch_grace_seconds", DEFAULT_MISMATCH_GRACE_SECONDS)
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_mood_entity": self.auto_mood_entity,
            "manual_mood_entity": self.manual_mood_entity,
            "main_mood_entity": self.main_mood_entity,
            "auto_option": self.auto_option,
            "mismatch_grace_seconds": self.mismatch_grace_seconds,
        }

    @property
    def entity_ids(self) -> list[str]:
        return [
            e
            for e in (
                self.auto_mood_entity,
                self.manual_mood_entity,
                self.main_mood_entity,
            )
            if e
        ]


def guess_kind(key: str, name: str, label_id: str | None) -> CategoryKind:
    """Kind of a category stored before 0.3.1 (by its key, name or label)."""
    names = {v.casefold().strip() for v in (key, name, label_id or "") if v}
    if names & RGB_CATEGORY_NAMES:
        return CategoryKind.RGB
    if names & FUNCTION_CATEGORY_NAMES:
        return CategoryKind.FUNCTION
    return CategoryKind.SOURCE


@dataclass(frozen=True, slots=True)
class LightCategory:
    """A light category, bound to a Home Assistant label."""

    key: str
    name: str
    label_id: str | None = None
    kind: CategoryKind = CategoryKind.SOURCE

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LightCategory:
        kind = data.get("kind")
        return cls(
            key=data["key"], name=data["name"], label_id=data.get("label_id"),
            kind=_enum_or(CategoryKind, kind, None) or guess_kind(data["key"], data["name"], data.get("label_id")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "label_id": self.label_id, "kind": self.kind.value}


@dataclass(frozen=True, slots=True)
class SensorAssignment:
    """A signal entity assigned to a room."""

    entity_id: str
    kind: SensorKind
    role: SensorRole
    # The signal means "active" when it is off (e.g. a "PC idle" sensor).
    inverted: bool = False
    # 0.3.8: False excludes the signal from this room's occupancy decisions (Presence tab). The assignment,
    # its kind and role are kept, so enabling it again restores it exactly; the entity itself is untouched.
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorAssignment:
        kind = _enum_or(SensorKind, data.get("kind"), SensorKind.OTHER)
        role = _enum_or(SensorRole, data.get("role"), DEFAULT_ROLE_FOR_KIND[kind])
        enabled = data.get("enabled", True)
        return cls(
            entity_id=data["entity_id"], kind=kind, role=role,
            inverted=bool(data.get("inverted", False)),
            enabled=enabled if isinstance(enabled, bool) else True,
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entity_id": self.entity_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "inverted": self.inverted,
        }
        if not self.enabled:
            # Only written when excluded: options saved before 0.3.8 keep their exact shape.
            out["enabled"] = False
        return out

    @property
    def participates(self) -> bool:
        """Takes part in occupancy decisions: enabled and not set to Ignore."""
        return self.enabled and self.role != SensorRole.IGNORE


def _clean_timing(data: Any, *, fill_defaults: bool) -> dict[str, int]:
    """Valid timing values only (unknown keys and bad values are dropped)."""
    out: dict[str, int] = {}
    data = data if isinstance(data, dict) else {}
    for key, (default, low, high) in TIMING_PARAMETERS.items():
        value = data.get(key)
        if value is None or isinstance(value, bool):
            if fill_defaults:
                out[key] = default
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            if fill_defaults:
                out[key] = default
            continue
        out[key] = min(max(number, low), high)
    return out


@dataclass(frozen=True, slots=True)
class OccupancyTiming:
    """Occupancy timing parameters, in seconds (see const.TIMING_PARAMETERS)."""

    correlation_window: int = TIMING_PARAMETERS["correlation_window"][0]
    exit_confirmation: int = TIMING_PARAMETERS["exit_confirmation"][0]
    inactivity_timeout: int = TIMING_PARAMETERS["inactivity_timeout"][0]
    dropout_grace: int = TIMING_PARAMETERS["dropout_grace"][0]
    trigger_stuck_after: int = TIMING_PARAMETERS["trigger_stuck_after"][0]
    presence_stuck_after: int = TIMING_PARAMETERS["presence_stuck_after"][0]
    startup_hold: int = TIMING_PARAMETERS["startup_hold"][0]

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OccupancyTiming:
        return cls(**_clean_timing(data, fill_defaults=True))

    def as_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in TIMING_PARAMETERS}

    def with_overrides(self, overrides: dict[str, int]) -> OccupancyTiming:
        return replace(self, **_clean_timing(overrides, fill_defaults=False))


def _number(value: Any) -> Any:
    """Keep numbers as they are; parse numeric strings; leave anything else for validation."""
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def _sequence(value: Any) -> tuple[Any, ...] | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return tuple(_number(v) for v in value)
    return (value,)  # invalid; reported by validation


@dataclass(frozen=True, slots=True)
class ProfileSetting:
    """One mood profile entry. ``None`` on any field means "inherit".

    At most one colour field (``color_temp_kelvin``, ``rgb_color``,
    ``hs_color``) is set per entry. ``persistent`` marks ambient lighting
    that follows the mood whether or not the room is occupied.

    ``brightness_mode`` / ``color_temp_mode`` (0.3.2) choose between the
    profile's own value (fixed), the existing global adjustment (global),
    the global value plus ``brightness_offset`` (percentage points) or
    ``color_temp_offset`` (K), or leaving the property unchanged (ignore).
    Not set means inherit; a value without a mode is fixed.
    """

    action: LightAction = LightAction.INHERIT
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

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileSetting:
        persistent = data.get("persistent")
        return cls(
            action=_enum_or(LightAction, data.get("action"), LightAction.INHERIT),
            brightness_pct=_number(data.get("brightness_pct")),
            color_temp_kelvin=_number(data.get("color_temp_kelvin")),
            rgb_color=_sequence(data.get("rgb_color")),  # type: ignore[arg-type]
            hs_color=_sequence(data.get("hs_color")),  # type: ignore[arg-type]
            effect=data.get("effect") or None,
            transition=_number(data.get("transition")),
            persistent=persistent if isinstance(persistent, bool) else None,
            brightness_mode=_enum_or(AdjustMode, data.get("brightness_mode"), None),
            brightness_offset=_number(data.get("brightness_offset")),
            color_temp_mode=_enum_or(AdjustMode, data.get("color_temp_mode"), None),
            color_temp_offset=_number(data.get("color_temp_offset")),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action.value}
        for key in ("brightness_pct", "color_temp_kelvin", "effect", "transition", "persistent",
                    "brightness_offset", "color_temp_offset"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        for key in ("brightness_mode", "color_temp_mode"):
            if (value := getattr(self, key)) is not None:
                out[key] = value.value
        for key in ("rgb_color", "hs_color"):
            if (value := getattr(self, key)) is not None:
                out[key] = list(value)
        return out

    @property
    def is_empty(self) -> bool:
        """Nothing set: the entry inherits everything."""
        return self.as_dict() == {"action": LightAction.INHERIT.value}


# mood -> target (category key or light entity id) -> setting
type ProfileTable = dict[str, dict[str, ProfileSetting]]


def _table_from_dict(data: dict[str, Any] | None) -> ProfileTable:
    return {
        mood: {target: ProfileSetting.from_dict(s) for target, s in targets.items()}
        for mood, targets in (data or {}).items()
    }


def _table_as_dict(table: ProfileTable) -> dict[str, Any]:
    return {
        mood: {target: s.as_dict() for target, s in targets.items()}
        for mood, targets in table.items()
        if targets
    }


@dataclass(frozen=True, slots=True)
class RoomConfig:
    """Configuration of a single room (bound to a Home Assistant area)."""

    area_id: str
    enabled: bool = False
    sensors: tuple[SensorAssignment, ...] = ()
    lights: tuple[str, ...] = ()
    reference_occupancy_entity: str | None = None
    mood_overrides: ProfileTable = field(default_factory=dict)
    light_overrides: ProfileTable = field(default_factory=dict)
    # Per-room timing overrides (only the keys that differ from global).
    timing: dict[str, int] = field(default_factory=dict)
    # Neighbouring rooms, used to recognise movement out through a door.
    adjacent_rooms: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, area_id: str, data: dict[str, Any]) -> RoomConfig:
        return cls(
            area_id=area_id,
            enabled=bool(data.get("enabled", False)),
            sensors=tuple(SensorAssignment.from_dict(s) for s in data.get("sensors", [])),
            lights=tuple(data.get("lights", [])),
            reference_occupancy_entity=data.get("reference_occupancy_entity"),
            mood_overrides=_table_from_dict(data.get("mood_overrides")),
            light_overrides=_table_from_dict(data.get("light_overrides")),
            timing=_clean_timing(data.get("timing"), fill_defaults=False),
            adjacent_rooms=tuple(
                a for a in data.get("adjacent_rooms") or [] if isinstance(a, str) and a != area_id
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sensors": [s.as_dict() for s in self.sensors],
            "lights": list(self.lights),
            "reference_occupancy_entity": self.reference_occupancy_entity,
            "mood_overrides": _table_as_dict(self.mood_overrides),
            "light_overrides": _table_as_dict(self.light_overrides),
            "timing": dict(self.timing),
            "adjacent_rooms": list(self.adjacent_rooms),
        }

    @property
    def occupancy_sensors(self) -> tuple[SensorAssignment, ...]:
        """Signals that take part in occupancy decisions (enabled, role is not Ignore)."""
        return tuple(s for s in self.sensors if s.participates)

    @property
    def referenced_entities(self) -> list[str]:
        """Every entity id this room configuration points at."""
        ids = [s.entity_id for s in self.sensors] + list(self.lights)
        if self.reference_occupancy_entity:
            ids.append(self.reference_occupancy_entity)
        return ids


@dataclass(frozen=True, slots=True)
class IntegrationConfig:
    """Typed view of the complete persisted configuration."""

    operating_mode: OperatingMode = OperatingMode.OBSERVE
    mood: MoodConfig = field(default_factory=MoodConfig)
    categories: tuple[LightCategory, ...] = ()
    rooms: dict[str, RoomConfig] = field(default_factory=dict)
    global_profiles: ProfileTable = field(default_factory=dict)
    occupancy: OccupancyTiming = field(default_factory=OccupancyTiming)
    config_version: int = CONFIG_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> IntegrationConfig:
        data = data or {}
        return cls(
            config_version=int(data.get("config_version", CONFIG_VERSION)),
            operating_mode=_enum_or(
                OperatingMode, data.get("operating_mode"), OperatingMode.OBSERVE
            ),
            mood=MoodConfig.from_dict(data.get("mood")),
            categories=tuple(
                LightCategory.from_dict(c) for c in data.get("categories", [])
            ),
            rooms={
                area_id: RoomConfig.from_dict(area_id, room)
                for area_id, room in data.get("rooms", {}).items()
            },
            global_profiles=_table_from_dict(data.get("global_profiles")),
            occupancy=OccupancyTiming.from_dict(data.get("occupancy")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "operating_mode": self.operating_mode.value,
            "mood": self.mood.as_dict(),
            "categories": [c.as_dict() for c in self.categories],
            "rooms": {a: r.as_dict() for a, r in self.rooms.items()},
            "global_profiles": _table_as_dict(self.global_profiles),
            "occupancy": self.occupancy.as_dict(),
        }

    def timing_for(self, area_id: str) -> OccupancyTiming:
        """Effective timing of a room: global values with its overrides applied."""
        return self.occupancy.with_overrides(self.room(area_id).timing)

    @property
    def enabled_rooms(self) -> dict[str, RoomConfig]:
        return {a: r for a, r in self.rooms.items() if r.enabled}

    def category(self, key: str) -> LightCategory | None:
        return next((c for c in self.categories if c.key == key), None)

    def room(self, area_id: str) -> RoomConfig:
        return self.rooms.get(area_id) or RoomConfig(area_id=area_id)

    def with_room(self, room: RoomConfig) -> IntegrationConfig:
        return replace(self, rooms={**self.rooms, room.area_id: room})

    def without_room(self, area_id: str) -> IntegrationConfig:
        return replace(
            self, rooms={a: r for a, r in self.rooms.items() if a != area_id}
        )


def migrate_options(data: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a stored options payload to CONFIG_VERSION.

    Version 1 -> 2 adds occupancy timing (global and per room), adjacent
    rooms and the per-signal ``inverted`` flag, all with defaults. Every
    existing value (mood helpers, categories, labels, lights, roles,
    profiles, reference helpers) is carried over unchanged.
    """
    config = IntegrationConfig.from_dict(data)
    return replace(config, config_version=CONFIG_VERSION).as_dict()


def default_categories() -> tuple[LightCategory, ...]:
    return tuple(LightCategory(key=k, name=n, kind=kind) for k, n, kind in DEFAULT_CATEGORIES)


def unique_category_key(name: str, existing: set[str]) -> str:
    """Build a stable, unique key for a new category."""
    base = "".join(ch if ch.isalnum() else "_" for ch in name.lower()).strip("_")
    base = "_".join(filter(None, base.split("_"))) or "category"
    key, n = base, 2
    while key in existing:
        key, n = f"{base}_{n}", n + 1
    return key


def rename_entity_references(
    data: dict[str, Any], old_entity_id: str, new_entity_id: str
) -> dict[str, Any] | None:
    """Return a copy of the options with an entity id renamed, or None if unused."""
    config = IntegrationConfig.from_dict(data)
    changed = False

    def sub(entity_id: str | None) -> str | None:
        nonlocal changed
        if entity_id == old_entity_id:
            changed = True
            return new_entity_id
        return entity_id

    mood = MoodConfig(
        auto_mood_entity=sub(config.mood.auto_mood_entity),
        manual_mood_entity=sub(config.mood.manual_mood_entity),
        main_mood_entity=sub(config.mood.main_mood_entity),
        auto_option=config.mood.auto_option,
        mismatch_grace_seconds=config.mood.mismatch_grace_seconds,
    )
    rooms: dict[str, RoomConfig] = {}
    for area_id, room in config.rooms.items():
        light_overrides = {
            mood_name: {sub(target) or target: s for target, s in targets.items()}
            for mood_name, targets in room.light_overrides.items()
        }
        rooms[area_id] = replace(
            room,
            sensors=tuple(
                replace(s, entity_id=sub(s.entity_id) or s.entity_id)
                for s in room.sensors
            ),
            lights=tuple(sub(e) or e for e in room.lights),
            reference_occupancy_entity=sub(room.reference_occupancy_entity),
            light_overrides=light_overrides,
        )
    if not changed:
        return None
    return replace(config, mood=mood, rooms=rooms).as_dict()
