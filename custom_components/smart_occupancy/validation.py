"""Configuration validation and entity-health problems.

Every problem carries enough context to be acted on from the UI alone: a
type, a title, a plain-language explanation, a recommended action, the
affected room and entity (with its friendly name), and whether it is a
configuration problem or an operational one (e.g. a device that is offline).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, label_registry as lr

from . import lighting
from .const import (
    MOOD_ENTITY_DOMAINS,
    Classification,
    ProblemCategory,
    SensorKind,
    Severity,
)
from .discovery import area_name
from .models import IntegrationConfig
from .mood import mood_names
from .occupancy import (
    AVAILABLE,
    DISABLED,
    ESSENTIAL_ROLES,
    MISSING,
    NOT_PROVIDED,
    OPERATIONAL_UNAVAILABLE,
    PENDING,
    UNAVAILABLE,
    UNKNOWN,
    entity_health,
    friendly_name,
)
from .profiles import iter_profile_entries, validate_setting
from .supervision import mode_permitted

CONFIG = ProblemCategory.CONFIGURATION
OPERATIONAL = ProblemCategory.OPERATIONAL

ENTITY_PROBLEM_CODES = frozenset(
    {"entity_missing", "entity_disabled", "entity_not_provided", "entity_unavailable", "entity_unknown"}
)


@dataclass(slots=True, frozen=True)
class Problem:
    """A configuration or operational problem."""

    code: str
    severity: Severity
    category: ProblemCategory
    title: str
    explanation: str
    action: str
    area_id: str | None = None
    room: str | None = None
    entity_id: str | None = None
    name: str | None = None
    # Identifies non-entity subjects (category, label, profile entry, ...).
    subject: str | None = None
    health: str | None = None

    @property
    def key(self) -> str:
        """Stable identity, used to remember when the problem was first seen."""
        return "|".join((self.code, self.area_id or "", self.entity_id or "", self.subject or ""))

    @property
    def classification(self) -> Classification:
        if self.severity == Severity.INFO:
            return Classification.INFO
        if self.category == CONFIG and self.severity == Severity.ERROR:
            return Classification.CONFIG_ERROR
        if self.health in OPERATIONAL_UNAVAILABLE:
            return Classification.UNAVAILABLE
        return Classification.WARNING

    @property
    def message(self) -> str:
        return self.explanation

    def as_dict(self, first_detected: str | None = None, last_checked: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.code,
            "title": self.title,
            "classification": self.classification.value,
            "severity": self.severity.value,
            "category": self.category.value,
            "explanation": self.explanation,
            "action": self.action,
        }
        for key in ("entity_id", "name", "room", "area_id", "health"):
            if (value := getattr(self, key)) is not None:
                out[key] = value
        if first_detected:
            out["first_detected"] = first_detected
        if last_checked:
            out["last_checked"] = last_checked
        return out


# What an entity is, for wording: (noun, title for "unavailable", action when unavailable)
_WHAT: dict[str, tuple[str, str, str]] = {
    "light": (
        "light", "Light unavailable",
        "Check the light's power (wall switch, fuse, smart plug) and its Zigbee/Wi-Fi connection.",
    ),
    "sensor": (
        "sensor", "Sensor unavailable",
        "Check the sensor's battery and its Zigbee/Wi-Fi connection.",
    ),
    "reference": (
        "occupancy helper", "Occupancy helper unavailable",
        "Check the helper or the integration that provides it.",
    ),
    "mood": (
        "mood helper", "Mood helper unavailable",
        "Check the helper under Settings → Devices & services → Helpers.",
    ),
}


def entity_problem(
    hass: HomeAssistant,
    entity_id: str,
    health: str,
    what: str,
    *,
    area_id: str | None = None,
    room: str | None = None,
    essential: bool = True,
) -> Problem | None:
    """Problem for a configured entity in the given health, or None if healthy."""
    if health in (AVAILABLE, PENDING):
        return None
    noun, unavailable_title, unavailable_action = _WHAT[what]
    name = friendly_name(hass, entity_id)
    where = f" in {room}" if room else ""
    fix_where = f"Configure → Rooms → {room}" if room else "Configure → Mood helpers and operating mode"
    common = {"area_id": area_id, "room": room, "entity_id": entity_id, "name": name, "health": health}
    if health == MISSING:
        return Problem(
            "entity_missing", Severity.ERROR, CONFIG, "Entity missing",
            f"The {noun} {entity_id}{where} is configured but does not exist. It was deleted, or "
            "renamed while this integration was not running.",
            f"Select the replacement entity in {fix_where}, or remove it there.",
            **common,
        )
    if health == DISABLED:
        return Problem(
            "entity_disabled", Severity.ERROR, CONFIG, "Entity disabled",
            f"The {noun} {entity_id}{where} is disabled in Home Assistant, so it has no state.",
            f"Enable it under Settings → Entities, or remove it in {fix_where}.",
            **common,
        )
    if health == NOT_PROVIDED:
        return Problem(
            "entity_not_provided", Severity.WARNING, OPERATIONAL, f"{noun.capitalize()} not provided by its integration",
            f"The {noun} {entity_id}{where} is registered, but its integration is not providing it: "
            "the integration failed to load, or the device was removed from it. This does not clear by itself.",
            "Check the integration under Settings → Devices & services. If the device was removed or "
            f"replaced, select the new entity (or remove this one) in {fix_where}.",
            **common,
        )
    optional = "" if essential else f" The {noun} is optional, so the room remains usable."
    if health == UNAVAILABLE:
        return Problem(
            "entity_unavailable", Severity.WARNING, OPERATIONAL, unavailable_title,
            f"The {noun} {entity_id}{where} is unavailable: Home Assistant cannot reach the device. "
            f"This clears automatically when it reconnects.{optional}",
            unavailable_action,
            **common,
        )
    if health == UNKNOWN:
        return Problem(
            "entity_unknown", Severity.WARNING, OPERATIONAL, "No state reported",
            f"The {noun} {entity_id}{where} has not reported a value since Home Assistant started. "
            f"Battery-powered devices often report only on their next change.{optional}",
            "Trigger or switch the device once, or check its battery.",
            **common,
        )
    return None


def validate(hass: HomeAssistant, config: IntegrationConfig, *, settled: bool = True) -> list[Problem]:
    """Validate the configuration and the health of every configured entity. Read only."""
    problems: list[Problem] = []
    add = problems.append

    def health(entity_id: str) -> str:
        return entity_health(hass, entity_id, settled=settled)

    # Operating mode -------------------------------------------------------
    if not mode_permitted(config.operating_mode):
        add(Problem(
            "mode_not_permitted", Severity.ERROR, CONFIG, "Operating mode not available",
            f"Operating mode '{config.operating_mode}' is not available in this phase; the integration "
            "runs in Observe mode.",
            "Set the operating mode to Observe in Configure → Mood helpers and operating mode.",
            subject="operating_mode",
        ))

    # Mood helpers ---------------------------------------------------------
    mood = config.mood
    for label, entity_id in (
        ("Auto Mood", mood.auto_mood_entity),
        ("Manual Mood", mood.manual_mood_entity),
        ("Main Mood", mood.main_mood_entity),
    ):
        if not entity_id:
            add(Problem(
                "mood_entity_not_configured", Severity.ERROR, CONFIG, "Mood helper not configured",
                f"No {label} entity is configured, so the mood relationship cannot be observed.",
                "Select it in Configure → Mood helpers and operating mode.",
                subject=label,
            ))
            continue
        if entity_id.split(".", 1)[0] not in MOOD_ENTITY_DOMAINS:
            add(Problem(
                "mood_entity_wrong_domain", Severity.ERROR, CONFIG, "Mood helper has the wrong type",
                f"{label} entity {entity_id} must be an input_select or select.",
                "Select a dropdown helper in Configure → Mood helpers and operating mode.",
                entity_id=entity_id, name=friendly_name(hass, entity_id),
            ))
        if problem := entity_problem(hass, entity_id, health(entity_id), "mood"):
            add(problem)
    if mood.manual_mood_entity and (state := hass.states.get(mood.manual_mood_entity)):
        options = state.attributes.get("options")
        if options and mood.auto_option.casefold() not in {o.casefold() for o in options}:
            add(Problem(
                "auto_option_not_found", Severity.WARNING, CONFIG, "Auto option not found",
                f"'{mood.auto_option}' is not an option of {mood.manual_mood_entity}, so Main Mood is "
                "assumed to always follow Manual Mood.",
                "Enter the Manual Mood option that means Auto in Configure → Mood helpers and operating mode.",
                entity_id=mood.manual_mood_entity, name=friendly_name(hass, mood.manual_mood_entity),
            ))
    if (
        mood.main_mood_entity
        and mood.auto_mood_entity
        and (main := hass.states.get(mood.main_mood_entity))
        and (auto := hass.states.get(mood.auto_mood_entity))
    ):
        main_opts = {o.casefold() for o in main.attributes.get("options") or []}
        missing_opts = [o for o in auto.attributes.get("options") or [] if o.casefold() not in main_opts]
        if main_opts and missing_opts:
            add(Problem(
                "mood_options_mismatch", Severity.WARNING, CONFIG, "Mood options differ",
                f"Auto Mood can select {', '.join(missing_opts)}, which Main Mood does not offer, so "
                "Main Mood cannot follow Auto Mood for those moods.",
                f"Add the missing options to {mood.main_mood_entity}, or ignore this if it is intentional.",
                entity_id=mood.main_mood_entity, name=friendly_name(hass, mood.main_mood_entity),
            ))

    # Light categories -----------------------------------------------------
    label_reg = lr.async_get(hass)
    seen_labels: dict[str, str] = {}
    for cat in config.categories:
        if not cat.label_id:
            add(Problem(
                "category_without_label", Severity.INFO, CONFIG, "Category has no label",
                f"Category '{cat.name}' has no label, so no light belongs to it.",
                "Assign a label in Configure → Light categories → Edit category, if you want to use it.",
                subject=cat.key,
            ))
            continue
        if label_reg.async_get_label(cat.label_id) is None:
            add(Problem(
                "category_label_missing", Severity.WARNING, CONFIG, "Category label deleted",
                f"Label '{cat.label_id}' of category '{cat.name}' no longer exists.",
                "Choose another label in Configure → Light categories → Edit category, or use "
                "Create missing labels.",
                subject=cat.key,
            ))
        if cat.label_id in seen_labels:
            add(Problem(
                "label_used_twice", Severity.WARNING, CONFIG, "Label used by two categories",
                f"Categories '{seen_labels[cat.label_id]}' and '{cat.name}' share label '{cat.label_id}', "
                "so their lights cannot be told apart.",
                "Give each category its own label in Configure → Light categories.",
                subject=cat.key,
            ))
        seen_labels.setdefault(cat.label_id, cat.name)

    _, light_categories = lighting.classify_lights(hass, config.categories)

    # Rooms ----------------------------------------------------------------
    area_reg = ar.async_get(hass)
    managed: dict[str, list[str]] = {}
    signal_owner: dict[str, str] = {}
    for area_id, room in config.enabled_rooms.items():
        room_name = area_name(hass, area_id) or area_id
        in_room = {"area_id": area_id, "room": room_name}
        if area_reg.async_get_area(area_id) is None:
            add(Problem(
                "area_missing", Severity.ERROR, CONFIG, "Area deleted",
                f"The area '{area_id}' of this room no longer exists. Its configuration is kept.",
                "Re-create the area, or remove the room in Configure → Rooms.",
                subject=area_id, **in_room,
            ))

        essential = [s for s in room.sensors if s.role in ESSENTIAL_ROLES]
        if not essential:
            add(Problem(
                "room_without_signals", Severity.WARNING, CONFIG, "No occupancy signals",
                f"{room_name} has no signal with the role Trigger, Maintain or Confirm vacancy, so its "
                "occupancy cannot be observed.",
                f"Assign sensors and roles in Configure → Rooms → {room_name}.",
                subject=area_id, **in_room,
            ))
        elif all(s.kind == SensorKind.DOOR for s in essential):
            add(Problem(
                "room_only_doors", Severity.WARNING, CONFIG, "Only door contacts",
                f"{room_name} has only door contacts as occupancy signals. A door shows movement but cannot "
                "prove that someone is inside, so the room is never reported occupied.",
                f"Add a PIR, radar or other presence sensor in Configure → Rooms → {room_name}.",
                subject=area_id, **in_room,
            ))
        for neighbour in room.adjacent_rooms:
            if neighbour not in config.enabled_rooms:
                add(Problem(
                    "adjacent_room_not_enabled", Severity.INFO, CONFIG, "Adjacent room not enabled",
                    f"{room_name} lists {area_name(hass, neighbour) or neighbour} as adjacent, but that room is "
                    "not enabled, so movement there is not seen.",
                    f"Enable that room, or remove it from the adjacent rooms of {room_name}.",
                    subject=f"adjacent:{neighbour}", **in_room,
                ))

        essential_health: list[str] = []
        for s in room.sensors:
            h = health(s.entity_id)
            if s.role in ESSENTIAL_ROLES:
                essential_health.append(h)
            if problem := entity_problem(
                hass, s.entity_id, h, "sensor", essential=s.role in ESSENTIAL_ROLES, **in_room
            ):
                add(problem)
            if (other := signal_owner.get(s.entity_id)) and other != area_id:
                add(Problem(
                    "signal_shared", Severity.INFO, CONFIG, "Signal used by several rooms",
                    f"{s.entity_id} is assigned to several rooms.",
                    "No action needed if the sensor really covers both rooms.",
                    entity_id=s.entity_id, name=friendly_name(hass, s.entity_id), **in_room,
                ))
            signal_owner.setdefault(s.entity_id, area_id)
        if essential_health and not any(h in (AVAILABLE, PENDING) for h in essential_health):
            add(Problem(
                "room_signals_unavailable", Severity.WARNING, OPERATIONAL, "Room cannot be observed",
                f"None of the Trigger or Maintain signals in {room_name} is available, so its occupancy "
                "cannot be observed. This clears as soon as one of them is available again.",
                "See the problems of the individual sensors in this room.",
                subject=area_id, health=UNAVAILABLE, **in_room,
            ))

        if room.reference_occupancy_entity:
            ref = room.reference_occupancy_entity
            if problem := entity_problem(hass, ref, health(ref), "reference", essential=False, **in_room):
                add(problem)

        managed[area_id] = list(room.lights)
        for light in room.lights:
            if light.split(".", 1)[0] != "light":
                add(Problem(
                    "not_a_light", Severity.ERROR, CONFIG, "Not a light",
                    f"{light} is selected as a managed light but is not a light entity.",
                    f"Remove it from the managed lights in Configure → Rooms → {room_name}.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))
                continue
            if problem := entity_problem(hass, light, health(light), "light", essential=False, **in_room):
                add(problem)
            cats = light_categories.get(light, [])
            if not cats:
                add(Problem(
                    "light_without_category", Severity.WARNING, CONFIG, "Light has no category",
                    f"{light} has no category label, so no global mood profile will apply to it in later phases. "
                    "This does not affect anything in Observe mode.",
                    "Apply one of the category labels to the light (or its device) under Settings → Entities.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))
            elif len(cats) > 1:
                add(Problem(
                    "light_in_multiple_categories", Severity.WARNING, CONFIG, "Light in several categories",
                    f"{light} has labels of several categories ({', '.join(cats)}); '{cats[0]}' takes precedence.",
                    "Keep only one category label on the light.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))
            if lighting.is_unexposed_group(hass, light):
                add(Problem(
                    "group_members_unknown", Severity.INFO, CONFIG, "Group members not exposed",
                    f"{light} is a group that does not expose its members, so overlap with its member "
                    "lights cannot be checked.",
                    "Make sure you have not also selected its member lights.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))

    owners: dict[str, list[str]] = {}
    for area_id, lights in managed.items():
        for light in lights:
            owners.setdefault(light, []).append(area_id)
    for light, rooms in owners.items():
        if len(rooms) > 1:
            names = [area_name(hass, a) or a for a in rooms]
            add(Problem(
                "light_in_multiple_rooms", Severity.WARNING, CONFIG, "Light managed by several rooms",
                f"{light} is a managed light of {', '.join(names)}.",
                "Keep it in one room only.",
                entity_id=light, name=friendly_name(hass, light),
            ))
    for c in lighting.find_group_conflicts(hass, managed):
        member_room = area_name(hass, c.member_room) or c.member_room
        group_room = area_name(hass, c.group_room) or c.group_room
        add(Problem(
            "group_member_conflict", Severity.WARNING, CONFIG, "Group and member both selected",
            f"Group {c.group} ({group_room}) and its member {c.member} ({member_room}) are both selected "
            "for independent control.",
            f"Select either the group or its member lights in Configure → Rooms → {member_room}.",
            area_id=c.member_room, room=member_room, entity_id=c.member,
            name=friendly_name(hass, c.member), subject=c.group,
        ))

    # Profiles -------------------------------------------------------------
    known_moods = {m.casefold() for m in mood_names(hass, mood)}
    category_keys = {c.key for c in config.categories}
    for scope, area_id, mood_name, target, setting in iter_profile_entries(config):
        where = f"{scope} profile '{mood_name}' / {target}"
        subject = f"{scope}:{area_id or ''}:{mood_name}:{target}"
        ctx: dict[str, Any] = {
            "area_id": area_id,
            "room": area_name(hass, area_id) or area_id if area_id else None,
            "subject": subject,
        }
        fix = "Edit or remove the entry in Configure → Mood profiles."

        def profile(code: str, severity: Severity, title: str, text: str, suffix: str = "",
                    *, where: str = where, fix: str = fix, ctx: dict[str, Any] = ctx, **extra: Any) -> None:
            add(Problem(code, severity, CONFIG, title, f"{where}: {text}", fix,
                        **{**ctx, "subject": ctx["subject"] + suffix}, **extra))

        if known_moods and mood_name.casefold() not in known_moods:
            profile("profile_unknown_mood", Severity.WARNING, "Profile for unknown mood",
                    f"mood '{mood_name}' is not a Main Mood option.")
        if scope in ("global", "room") and target not in category_keys:
            profile("profile_unknown_category", Severity.WARNING, "Profile for unknown category",
                    "the category does not exist.")
        if scope == "light":
            room = config.rooms.get(area_id or "")
            if room is None or target not in room.lights:
                profile("profile_light_not_managed", Severity.WARNING, "Profile for unmanaged light",
                        "the light is not a managed light of this room.", entity_id=target)
            if caps := lighting.capabilities(hass, target):
                if setting.rgb_color is not None and not caps["rgb"]:
                    profile("profile_rgb_unsupported", Severity.WARNING, "RGB not supported",
                            "the light does not support RGB colour.", entity_id=target)
                if setting.color_temp_kelvin is not None and not caps["color_temp"]:
                    profile("profile_kelvin_unsupported", Severity.WARNING, "Colour temperature not supported",
                            "the light does not support colour temperature.", entity_id=target)
                if setting.effect and setting.effect not in caps["effects"]:
                    profile("profile_effect_unsupported", Severity.WARNING, "Effect not supported",
                            f"effect '{setting.effect}' is not offered by the light.", entity_id=target)
        if scope == "global" and setting.action.value == "inherit":
            profile("global_inherit", Severity.INFO, "Inherit at global level",
                    "'inherit' at global level means 'ignore'.")
        for field_name, error in validate_setting(setting).items():
            profile("profile_invalid", Severity.ERROR, "Invalid profile value",
                    f"{field_name} is invalid ({error}).", suffix=f":{field_name}")
    return problems
