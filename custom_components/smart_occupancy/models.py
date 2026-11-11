"""Configuration model.

The whole integration configuration lives in the config entry ``options`` as
plain JSON. These dataclasses are the typed view of that payload. They are
deliberately independent of Home Assistant so the model can be validated and
unit tested in isolation, and extended in later phases without touching the
runtime modules.

Persisted shape (CONFIG_VERSION 1)::

    {
      "config_version": 1,
      "operating_mode": "observe",
      "mood": {"auto_mood_entity": ..., "manual_mood_entity": ...,
               "main_mood_entity": ..., "auto_option": "Auto",
               "mismatch_grace_seconds": 30},
      "categories": [{"key": "lamps", "name": "Lamps", "label_id": "lamps"}],
      "rooms": {"<area_id>": {"enabled": true,
                              "sensors": [{"entity_id": ..., "kind": "pir",
                                           "role": "trigger"}],
                              "lights": ["light.x"],
                              "reference_occupancy_entity": null,
                              "mood_overrides": {mood: {category_key: setting}},
                              "light_overrides": {mood: {entity_id: setting}}}},
      "global_profiles": {mood: {category_key: setting}}
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .const import (
    CONFIG_VERSION,
    DEFAULT_AUTO_OPTION,
    DEFAULT_CATEGORIES,
    DEFAULT_MISMATCH_GRACE_SECONDS,
    DEFAULT_ROLE_FOR_KIND,
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


@dataclass(frozen=True, slots=True)
class LightCategory:
    """A light category, bound to a Home Assistant label."""

    key: str
    name: str
    label_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LightCategory:
        return cls(key=data["key"], name=data["name"], label_id=data.get("label_id"))

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "name": self.name, "label_id": self.label_id}


@dataclass(frozen=True, slots=True)
class SensorAssignment:
    """A signal entity assigned to a room."""

    entity_id: str
    kind: SensorKind
    role: SensorRole

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorAssignment:
        kind = _enum_or(SensorKind, data.get("kind"), SensorKind.OTHER)
        role = _enum_or(SensorRole, data.get("role"), DEFAULT_ROLE_FOR_KIND[kind])
        return cls(entity_id=data["entity_id"], kind=kind, role=role)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind.value,
            "role": self.role.value,
        }


@dataclass(frozen=True, slots=True)
class ProfileSetting:
    """One mood profile entry. ``None`` on any field means "inherit"."""

    action: LightAction = LightAction.INHERIT
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    rgb_color: tuple[int, int, int] | None = None
    effect: str | None = None
    transition: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfileSetting:
        rgb = data.get("rgb_color")
        return cls(
            action=_enum_or(LightAction, data.get("action"), LightAction.INHERIT),
            brightness_pct=data.get("brightness_pct"),
            color_temp_kelvin=data.get("color_temp_kelvin"),
            rgb_color=tuple(rgb) if rgb is not None else None,  # type: ignore[arg-type]
            effect=data.get("effect") or None,
            transition=data.get("transition"),
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action.value}
        if self.brightness_pct is not None:
            out["brightness_pct"] = self.brightness_pct
        if self.color_temp_kelvin is not None:
            out["color_temp_kelvin"] = self.color_temp_kelvin
        if self.rgb_color is not None:
            out["rgb_color"] = list(self.rgb_color)
        if self.effect is not None:
            out["effect"] = self.effect
        if self.transition is not None:
            out["transition"] = self.transition
        return out


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
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sensors": [s.as_dict() for s in self.sensors],
            "lights": list(self.lights),
            "reference_occupancy_entity": self.reference_occupancy_entity,
            "mood_overrides": _table_as_dict(self.mood_overrides),
            "light_overrides": _table_as_dict(self.light_overrides),
        }

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
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "operating_mode": self.operating_mode.value,
            "mood": self.mood.as_dict(),
            "categories": [c.as_dict() for c in self.categories],
            "rooms": {a: r.as_dict() for a, r in self.rooms.items()},
            "global_profiles": _table_as_dict(self.global_profiles),
        }

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


def default_categories() -> tuple[LightCategory, ...]:
    return tuple(LightCategory(key=k, name=n) for k, n in DEFAULT_CATEGORIES)


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
