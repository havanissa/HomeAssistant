"""The diagnostics snapshot: every room's health, the mood observation, the problems (the "status" entities' data).

Until Phase A6 this was the integration's coordinator; the App builds the same
snapshot from the same validation code and publishes it to the companion's
entities. Every evaluation is complete: the snapshot is rebuilt from scratch,
so a problem disappears as soon as its cause is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import label_registry as lr

from . import discovery, mood as mood_mod
from .const import Classification, OperatingMode, ProblemCategory, Severity
from .models import IntegrationConfig
from .occupancy import AVAILABLE, PENDING, SignalHealth, entity_health, friendly_name, signal_health
from .supervision import effective_mode
from .validation import ENTITY_PROBLEM_CODES, Problem, validate, validate_daylight, validate_lighting

HEALTHY = Classification.HEALTHY
WARNING = Classification.WARNING
CONFIG_ERROR = Classification.CONFIG_ERROR
UNAVAILABLE = Classification.UNAVAILABLE
STATUS_OPTIONS = [HEALTHY.value, WARNING.value, CONFIG_ERROR.value, UNAVAILABLE.value]

# Light attributes the diagnostics depend on (group membership, capabilities).
_LIGHT_ATTRIBUTES = ("entity_id", "supported_color_modes", "effect_list", "is_hue_group")


def _availability(state: State) -> tuple[str, bool]:
    value = state.state if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) else "known"
    return value, bool(state.attributes.get("restored"))


def diagnostics_relevant(old: State | None, new: State | None, *, value_matters: bool) -> bool:
    """Does this state change affect the diagnostics snapshot?"""
    if old is None or new is None or value_matters:
        return True
    if _availability(old) != _availability(new):
        return True
    if old.attributes.get("friendly_name") != new.attributes.get("friendly_name"):
        return True
    if new.domain == "light":
        return any(old.attributes.get(k) != new.attributes.get(k) for k in _LIGHT_ATTRIBUTES)
    return False


@dataclass(slots=True, frozen=True)
class EntityStatus:
    entity_id: str
    name: str | None
    health: str
    detail: str  # sensor kind/role, or light categories

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "health": self.health, "detail": self.detail}


@dataclass(slots=True, frozen=True)
class RoomSnapshot:
    area_id: str
    name: str
    status: Classification
    usable: bool
    configuration_valid: bool
    signals: tuple[SignalHealth, ...]
    sensors: tuple[EntityStatus, ...]
    lights: tuple[EntityStatus, ...]
    reference_occupancy: EntityStatus | None
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "name": self.name,
            "status": self.status.value,
            "usable": self.usable,
            "configuration_valid": self.configuration_valid,
            "sensors": [s.as_dict() for s in self.sensors],
            "lights": [s.as_dict() for s in self.lights],
            "reference_occupancy": self.reference_occupancy.as_dict() if self.reference_occupancy else None,
            "reasons": list(self.reasons),
        }


@dataclass(slots=True, frozen=True)
class CategorySnapshot:
    key: str
    name: str
    label_id: str | None
    label_name: str | None
    lights: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "label_id": self.label_id,
            "label_name": self.label_name,
            "lights": list(self.lights),
        }


@dataclass(slots=True, frozen=True)
class Snapshot:
    status: Classification
    configured_mode: OperatingMode
    effective_mode: OperatingMode
    mood: mood_mod.MoodObservation
    mood_mismatch: bool
    rooms: tuple[RoomSnapshot, ...]
    categories: tuple[CategorySnapshot, ...]
    problems: tuple[Problem, ...]
    discovered_signals: tuple[tuple[str, str, str], ...]  # (area_id, entity_id, kind)
    settled: bool
    # Bumps every LAST_CHECKED_REFRESH_MINUTES while problems exist, so the
    # "last checked" attributes are republished at a bounded rate.
    checked_bucket: str | None
    evaluated_at: datetime = field(compare=False)
    first_detected: dict[str, str] = field(compare=False, default_factory=dict)
    # Rooms with mood lighting control enabled (area ids).
    mood_lighting_rooms: tuple[str, ...] = ()

    @property
    def last_checked(self) -> str:
        return self.evaluated_at.isoformat(timespec="seconds")

    def problem_dict(self, p: Problem) -> dict[str, Any]:
        return p.as_dict(self.first_detected.get(p.key), self.last_checked)

    def problems_where(self, *, category: ProblemCategory | None = None, area_id: str | None = None,
                       include_info: bool = False) -> list[Problem]:
        return [
            p for p in self.problems
            if (include_info or p.severity != Severity.INFO)
            and (category is None or p.category == category)
            and (area_id is None or p.area_id == area_id)
        ]

    def count(self, classification: Classification) -> int:
        return sum(1 for p in self.problems if p.classification == classification)

    def entity_issues(self, healths: frozenset[str]) -> list[dict[str, Any]]:
        """One entry per affected entity (an entity can be in several rooms)."""
        out: dict[str, dict[str, Any]] = {}
        for p in self.problems:
            if p.code not in ENTITY_PROBLEM_CODES or p.health not in healths or not p.entity_id:
                continue
            item = out.get(p.entity_id)
            if item is None:
                item = out[p.entity_id] = {
                    "entity_id": p.entity_id,
                    "name": p.name,
                    "rooms": [],
                    **{k: v for k, v in self.problem_dict(p).items()
                       if k not in ("entity_id", "name", "room", "area_id")},
                }
            if p.room and p.room not in item["rooms"]:
                item["rooms"].append(p.room)
        return sorted(out.values(), key=lambda i: i["entity_id"])

    @staticmethod
    def issue_line(item: dict[str, Any]) -> str:
        name = f" ({item['name']})" if item.get("name") else ""
        rooms = ", ".join(item["rooms"]) or "Global"
        return f"{item['entity_id']}{name} — {rooms} — {item['title']}"

    def summary(self) -> str:
        if self.status == HEALTHY:
            return "No problems."
        parts = []
        for cls, word in ((CONFIG_ERROR, "configuration error"), (UNAVAILABLE, "unavailable"),
                          (WARNING, "warning")):
            if n := self.count(cls):
                parts.append(f"{n} {word}{'s' if n != 1 and cls != UNAVAILABLE else ''}")
        return ", ".join(parts) + "."

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "settled": self.settled,
            "last_checked": self.last_checked,
            "configured_mode": self.configured_mode.value,
            "effective_mode": self.effective_mode.value,
            "mood_lighting_rooms": list(self.mood_lighting_rooms),
            "mood": self.mood.as_dict(),
            "mood_mismatch": self.mood_mismatch,
            "rooms": [r.as_dict() for r in self.rooms],
            "categories": [c.as_dict() for c in self.categories],
            "problems": [self.problem_dict(p) for p in self.problems],
            "discovered_signals": [
                {"area_id": a, "entity_id": e, "suggested_kind": k}
                for a, e, k in self.discovered_signals
            ],
        }


def worst(statuses: list[Classification]) -> Classification:
    for status in (CONFIG_ERROR, UNAVAILABLE, WARNING):
        if status in statuses:
            return status
    return HEALTHY


def room_snapshot(hass: HomeAssistant, area_id: str, room, problems: list[Problem], by_light, settled: bool,
                  controlled: tuple[str, ...]) -> RoomSnapshot:
    name = discovery.area_name(hass, area_id)
    signals = tuple(signal_health(hass, room, settled=settled))
    sensors = tuple(
        EntityStatus(s.entity_id, friendly_name(hass, s.entity_id), s.health, f"{s.kind}/{s.role}")
        for s in signals
    )
    lights = tuple(
        EntityStatus(e, friendly_name(hass, e), entity_health(hass, e, settled=settled),
                     ", ".join(by_light.get(e, [])) or "no category")
        for e in controlled
    )
    ref = None
    if room.reference_occupancy_entity:
        e = room.reference_occupancy_entity
        ref = EntityStatus(e, friendly_name(hass, e), entity_health(hass, e, settled=settled), "reference")

    room_problems = [p for p in problems if p.area_id == area_id and p.severity != Severity.INFO]
    configuration_valid = not any(p.classification == CONFIG_ERROR for p in room_problems)
    essential = [s for s in signals if s.essential]
    usable = name is not None and any(s.health in (AVAILABLE, PENDING) for s in essential)

    if not configuration_valid:
        status = CONFIG_ERROR
    elif essential and not any(s.health in (AVAILABLE, PENDING) for s in essential):
        status = UNAVAILABLE
    elif room_problems:
        status = WARNING
    else:
        status = HEALTHY

    reasons = tuple(
        f"{p.title}: {p.entity_id}" + (f" ({p.name})" if p.name else "") if p.entity_id else p.title
        for p in room_problems
    )
    if any(h.health == PENDING for h in (*sensors, *lights)):
        reasons += ("Waiting for Home Assistant to finish starting",)
    return RoomSnapshot(
        area_id=area_id,
        name=name or area_id,
        status=status,
        usable=usable,
        configuration_valid=configuration_valid,
        signals=signals,
        sensors=sensors,
        lights=lights,
        reference_occupancy=ref,
        reasons=reasons,
    )


def build_snapshot(hass: HomeAssistant, config: IntegrationConfig, *, settled: bool, lighting: Any,
                   observation: mood_mod.MoodObservation, mismatch: bool, first_detected: dict[str, str],
                   now: datetime, checked_bucket: str | None) -> Snapshot:
    """One complete evaluation. ``lighting`` is the lighting controller (its index, managed lights, active rooms);
    without one (the App before lighting runs) the lighting rules are not validated and no light is managed."""
    if lighting is None:
        from .controller import ManagedRoom  # noqa: PLC0415
        from .lighting import LightIndex  # noqa: PLC0415

        class _NoLighting:
            index = LightIndex(hass, config.categories, frozenset())
            managed: dict[str, Any] = {}

            class lighting:  # noqa: N801 - the controller's attribute name
                active_rooms: tuple[str, ...] = ()

            @staticmethod
            def managed_room(area_id: str) -> Any:
                return ManagedRoom(area_id)

        lighting = _NoLighting()
        # No controller: the rooms' configured lights are validated as they are (``managed=None``).
        problems = validate(hass, config, settled=settled, index=lighting.index, managed=None)
    else:
        problems = validate(hass, config, settled=settled, index=lighting.index,
                            managed={a: m.targets for a, m in lighting.managed.items()})
        problems += validate_lighting(hass, config, lighting)
        problems += validate_daylight(hass, config, lighting, settled=settled)
    if mismatch:
        problems.append(Problem(
            "mood_mismatch", Severity.WARNING, ProblemCategory.OPERATIONAL, "Main Mood does not follow the rule",
            f"Main Mood is '{observation.main_mood}', but the existing rule expects "
            f"'{observation.expected_main_mood}' (it should follow "
            f"{'Auto Mood' if observation.source == 'auto' else 'Manual Mood'}). This clears as soon as "
            "Main Mood matches again.",
            "Check the automation that sets Main Mood from Auto/Manual Mood, and its trace.",
            entity_id=config.mood.main_mood_entity,
            name=friendly_name(hass, config.mood.main_mood_entity) if config.mood.main_mood_entity else None,
            subject="mood",
        ))

    index = lighting.index
    by_category, by_light = index.by_category, index.categories_of
    label_reg = lr.async_get(hass)
    categories = tuple(
        CategorySnapshot(
            key=c.key,
            name=c.name,
            label_id=c.label_id,
            label_name=(
                label.name
                if c.label_id and (label := label_reg.async_get_label(c.label_id))
                else None
            ),
            lights=tuple(by_category.get(c.key, [])),
        )
        for c in config.categories
    )

    rooms: list[RoomSnapshot] = []
    discovered: list[tuple[str, str, str]] = []
    for area_id, room in sorted(config.enabled_rooms.items()):
        rooms.append(room_snapshot(hass, area_id, room, problems, by_light, settled,
                                   lighting.managed_room(area_id).controlled))
        if discovery.area_name(hass, area_id) is not None:
            found = discovery.discover_area(hass, area_id)
            discovered.extend(
                (area_id, e.entity_id, e.suggested_kind.value) for e in found.signals if e.suggested_kind
            )

    statuses = [r.status for r in rooms]
    if any(p.classification == CONFIG_ERROR for p in problems):
        statuses.append(CONFIG_ERROR)
    if any(p.classification in (WARNING, UNAVAILABLE) for p in problems):
        statuses.append(WARNING)
    status = worst(statuses)

    return Snapshot(
        status=status,
        configured_mode=config.operating_mode,
        effective_mode=effective_mode(config.operating_mode),
        mood=observation,
        mood_mismatch=mismatch,
        rooms=tuple(rooms),
        categories=categories,
        problems=tuple(problems),
        discovered_signals=tuple(discovered),
        settled=settled,
        checked_bucket=checked_bucket,
        evaluated_at=now,
        first_detected=first_detected,
        mood_lighting_rooms=tuple(a for a in lighting.lighting.active_rooms if a in config.enabled_rooms),
    )
