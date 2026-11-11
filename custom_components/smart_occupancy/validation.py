"""Configuration validation and entity-health problems.

Every problem carries enough context to be acted on from the UI alone: a
type, a title, a plain-language explanation, a recommended action, the
affected room and entity (with its friendly name), and whether it is a
configuration problem or an operational one (e.g. a device that is offline).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er, label_registry as lr
from homeassistant.util import dt as dt_util

from . import lighting
from .const import (
    MOOD_ENTITY_DOMAINS,
    RGB_SUFFIX,
    CategoryKind,
    Classification,
    ProblemCategory,
    SensorKind,
    Severity,
)
from .discovery import area_name
from .models import IntegrationConfig
from .lighting_config import parent_cycle
from .const import ALL_MOODS, MANUAL_PAUSED_TEXT, AdjustMode
from .models import LightCategory
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
from .profiles import iter_profile_entries, mood_chain, validate_setting
from .supervision import mode_permitted

if TYPE_CHECKING:
    from .controller import LightingController

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
    # "lighting" for problems that affect mood lighting but not occupancy
    # (shown in the Lighting health row instead of Occupancy).
    aspect: str | None = None

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
        for key in ("entity_id", "name", "room", "area_id", "health", "aspect"):
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


def validate(
    hass: HomeAssistant,
    config: IntegrationConfig,
    *,
    settled: bool = True,
    index: lighting.LightIndex | None = None,
    managed: dict[str, list[str]] | None = None,
) -> list[Problem]:
    """Validate the configuration and the health of every configured entity. Read only.

    ``managed`` maps each room to its managed lights (configured and
    discovered by label); without it the configured lights are used.
    """
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

    index = index or lighting.LightIndex(hass, config.categories)
    light_categories = index.categories_of

    # Rooms ----------------------------------------------------------------
    area_reg = ar.async_get(hass)
    managed_in = managed
    managed = {}
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

        for light in room.lights:
            if light.split(".", 1)[0] != "light":
                add(Problem(
                    "not_a_light", Severity.ERROR, CONFIG, "Not a light",
                    f"{light} is selected as a managed light but is not a light entity.",
                    f"Remove it from the managed lights in Configure → Rooms → {room_name}.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))
        room_lights = [
            e for e in (managed_in.get(area_id) if managed_in is not None else room.lights)
            if e.split(".", 1)[0] == "light"
        ]
        managed[area_id] = room_lights
        for light in room_lights:
            if problem := entity_problem(hass, light, health(light), "light", essential=False, **in_room):
                add(problem)
            cats = light_categories.get(light, [])
            if not cats:
                add(Problem(
                    "light_without_category", Severity.WARNING, CONFIG, "Light has no category",
                    f"{light} has no category label, so no global mood profile applies to it (only an "
                    "individual light override can).",
                    "Apply one of the category labels to the light (or its device) under Settings → Entities.",
                    entity_id=light, name=friendly_name(hass, light), **in_room,
                ))
            if index.is_opaque_group(light):
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
        if c.member_room == c.group_room:
            add(Problem(
                "group_member_overlap", Severity.INFO, CONFIG, "Group and member both managed",
                f"Group {c.group} and its member {c.member} are both managed in {group_room}. Mood lighting "
                "sends commands to the group only, so the member does not get duplicate commands.",
                f"To control {c.member} individually, exclude the group in the Smart Room Lighting card.",
                area_id=c.member_room, room=member_room, entity_id=c.member,
                name=friendly_name(hass, c.member), subject=c.group, aspect="lighting",
            ))
            continue
        add(Problem(
            "group_member_conflict", Severity.WARNING, CONFIG, "Group and member in different rooms",
            f"Group {c.group} ({group_room}) and its member {c.member} ({member_room}) are managed by two "
            "rooms, so both rooms could send commands to the same light.",
            f"Keep the group or its member lights in one room only (Configure → Rooms → {member_room}), "
            "or exclude one of them in the Smart Room Lighting card.",
            area_id=c.member_room, room=member_room, entity_id=c.member,
            name=friendly_name(hass, c.member), subject=c.group, aspect="lighting",
        ))

    return problems


def validate_lighting(hass: HomeAssistant, config: IntegrationConfig, controller: LightingController) -> list[Problem]:
    """Problems of the mood lighting configuration and its runtime. Read only."""
    problems: list[Problem] = []
    add = problems.append
    lighting_cfg = controller.lighting
    index = controller.index
    moods = controller.discovered_moods()
    known = {m["name"].casefold(): m for m in moods}
    main_known = any(m["in_main"] for m in moods)
    category_keys = {c.key for c in config.categories}
    main_entity = config.mood.main_mood_entity
    fix = "Edit or remove the entry in the Smart Lighting or Smart Room Lighting card."

    def room_ctx(area_id: str | None) -> dict[str, Any]:
        return {"area_id": area_id, "room": (area_name(hass, area_id) or area_id) if area_id else None}

    # Moods and inheritance ------------------------------------------------
    reported_main: set[str] = set()
    uses_global: set[str] = set()
    for scope, area_id, mood_name, target, setting in iter_profile_entries(lighting_cfg):
        where = f"{scope} profile '{mood_name}' / {target}"
        subject = f"{scope}:{area_id or ''}:{mood_name}:{target}"
        ctx = room_ctx(area_id)

        def profile(code: str, severity: Severity, title: str, text: str, suffix: str = "", *,
                    where: str = where, subject: str = subject, ctx: dict[str, Any] = ctx,
                    action: str = fix, **extra: Any) -> None:
            add(Problem(code, severity, CONFIG, title, f"{where}: {text}", action,
                        subject=subject + suffix, **ctx, **extra))

        info = known.get(mood_name.casefold())
        if mood_name == ALL_MOODS:
            info = {"in_main": True}  # entries for every mood
        if main_known and info is None:
            profile("profile_unknown_mood", Severity.WARNING, "Profile for unknown mood",
                    f"mood '{mood_name}' is not an option of any mood selector.")
        elif main_known and info and not info["in_main"] and mood_name.casefold() not in reported_main:
            reported_main.add(mood_name.casefold())
            add(Problem(
                "mood_not_in_main", Severity.WARNING, CONFIG, "Mood missing from Main Mood",
                f"Profiles exist for '{mood_name}', but Main Mood ({main_entity}) has no such option, so the "
                "profile can only be used as a room mood override or a parent/fallback mood.",
                f"Add '{mood_name}' to the options of {main_entity} (Settings → Helpers) to use it as the "
                "effective mood.",
                entity_id=main_entity, name=friendly_name(hass, main_entity) if main_entity else None,
                subject=f"mood:{mood_name}",
            ))
        base = target.removesuffix(RGB_SUFFIX)
        if scope in ("global", "room") and (
            base not in category_keys
            or (target.endswith(RGB_SUFFIX) and (config.category(base) or LightCategory("", "")).kind == CategoryKind.RGB)
        ):
            profile("profile_unknown_category", Severity.WARNING, "Profile for unknown category",
                    "the category does not exist.")
        if scope == "light":
            managed = controller.managed.get(area_id or "")
            if managed is None or target not in managed.targets:
                profile("profile_light_not_managed", Severity.WARNING, "Profile for unmanaged light",
                        "the light is not a managed light of this room.", entity_id=target)
            if caps := lighting.capabilities(hass, target):
                if (setting.rgb_color is not None or setting.hs_color is not None) and not caps["rgb"]:
                    profile("profile_rgb_unsupported", Severity.WARNING, "Colour not supported",
                            "the light does not support RGB/HS colour.", entity_id=target, aspect="lighting")
                if setting.color_temp_kelvin is not None and not caps["color_temp"] and not caps["rgb"]:
                    profile("profile_kelvin_unsupported", Severity.WARNING, "Colour temperature not supported",
                            "the light does not support colour temperature.", entity_id=target, aspect="lighting")
                if setting.brightness_pct is not None and not caps["brightness"]:
                    profile("profile_brightness_unsupported", Severity.WARNING, "Brightness not supported",
                            "the light can only be switched on and off.", entity_id=target, aspect="lighting")
                if setting.effect and setting.effect not in caps["effects"]:
                    profile("profile_effect_unsupported", Severity.WARNING, "Effect not supported",
                            f"effect '{setting.effect}' is not offered by the light.", entity_id=target,
                            aspect="lighting")
        if scope == "global" and mood_name != ALL_MOODS and setting.action.value == "inherit" \
                and not lighting_cfg.parent_of(mood_name) \
                and not lighting_cfg.fallback_mood:
            profile("global_inherit", Severity.INFO, "Inherit without a parent mood",
                    "'inherit' at global level needs a parent or fallback mood; without one it means 'ignore'.")
        following = (AdjustMode.GLOBAL, AdjustMode.OFFSET)
        if setting.brightness_mode in following:
            uses_global.add("brightness")
        if setting.color_temp_mode in following:
            uses_global.add("color_temp")
        for field_name, error in validate_setting(setting, mood_name).items():
            profile("profile_invalid", Severity.ERROR, "Invalid profile value",
                    f"{field_name} is invalid ({error}).", suffix=f":{field_name}")

    for mood_name, parent in lighting_cfg.parents.items():
        chain = mood_chain(lighting_cfg, mood_name)
        if main_known and parent.casefold() not in known:
            add(Problem(
                "mood_parent_unknown", Severity.WARNING, CONFIG, "Parent mood unknown",
                f"'{mood_name}' inherits from '{parent}', which is not an option of any mood selector.",
                "Choose another parent mood in the Smart Lighting card.", subject=f"parent:{mood_name}",
            ))
        if cycle := parent_cycle(lighting_cfg, mood_name, parent):
            add(Problem(
                "mood_inheritance_loop", Severity.ERROR, CONFIG, "Mood inheritance loop",
                f"'{mood_name}' inherits from '{parent}', which leads back to it: {' → '.join(cycle)}. The "
                f"loop is cut off; resolution uses {' → '.join(chain)}.",
                "Choose a different parent mood in the Smart Lighting card.", subject=f"loop:{mood_name}",
            ))
    if (fb := lighting_cfg.fallback_mood) and main_known and fb.casefold() not in known:
        add(Problem(
            "fallback_mood_unknown", Severity.WARNING, CONFIG, "Fallback mood unknown",
            f"The fallback mood '{fb}' is not an option of any mood selector.",
            "Choose another fallback mood under Smart Lighting → Advanced.", subject="fallback",
        ))

    # Global light adjustment ------------------------------------------------
    values = controller.global_values()
    words = {"brightness": "brightness", "color_temp": "colour temperature"}
    for kind in sorted(uses_global):
        src = getattr(values, kind)
        if src.status == "not_configured":
            add(Problem(
                "global_source_not_configured", Severity.WARNING, CONFIG, "Global source not configured",
                f"Some profiles follow the global {words[kind]}, but no {words[kind]} source is configured, so "
                f"they leave the {words[kind]} unchanged.",
                "Choose the existing adjustment's output entity under Smart Lighting → Global light adjustment.",
                subject=f"global_source:{kind}", aspect="lighting",
            ))
        elif src.status in ("unavailable", "stale"):
            add(Problem(
                "global_source_unavailable", Severity.WARNING, OPERATIONAL, "Global source unavailable",
                f"The global {words[kind]} source {src.entity_id} has no usable value ({src.detail}). "
                + ("The last good value is used meanwhile." if src.status == "stale"
                   else f"Profiles that follow it leave the {words[kind]} unchanged; it is never taken as 0."),
                "Check the entity of the existing adjustment system, or choose another source.",
                entity_id=src.entity_id, subject=f"global_source:{kind}", aspect="lighting",
            ))

    status = controller.mood_status()
    if status.manual:
        add(Problem(
            "lighting_manual_mode", Severity.INFO, OPERATIONAL, "Manual control",
            f"{MANUAL_PAUSED_TEXT}. Main Mood is '{status.main_mood}'. Occupancy and diagnostics continue; "
            "nothing is queued for later.",
            "Choose another mood in Main Mood (or Manual Mood) to resume automatic lighting.",
            entity_id=main_entity, subject="manual_mode", aspect="lighting",
        ))
    if status.unrepresentable:
        add(Problem(
            "mood_unrepresentable", Severity.WARNING, OPERATIONAL, "Mood cannot be represented by Main Mood",
            f"{status.detail}. Main Mood ({main_entity}) has no option '{status.unrepresentable}'.",
            f"Add '{status.unrepresentable}' to {main_entity} if it is a real mood, or choose how lighting "
            "handles it in the Smart Lighting card (follow Main Mood, no mood, or use another mood).",
            entity_id=main_entity, name=friendly_name(hass, main_entity) if main_entity else None,
            subject=f"unrepresentable:{status.unrepresentable}", aspect="lighting",
        ))
    if lighting_cfg.exclude_label and lr.async_get(hass).async_get_label(lighting_cfg.exclude_label) is None:
        add(Problem(
            "exclude_label_missing", Severity.WARNING, CONFIG, "Exclusion label deleted",
            f"The exclusion label '{lighting_cfg.exclude_label}' no longer exists, so no light is excluded by label.",
            "Choose another exclusion label under Smart Lighting → Advanced.", subject="exclude_label",
        ))

    # Rooms ------------------------------------------------------------------
    for area_id in sorted(set(lighting_cfg.rooms) | set(config.enabled_rooms)):
        room_lighting = lighting_cfg.room(area_id)
        ctx = room_ctx(area_id)
        room_name = ctx["room"]
        enabled = area_id in config.enabled_rooms
        if room_lighting.control_enabled and not enabled:
            add(Problem(
                "lighting_room_not_enabled", Severity.WARNING, CONFIG, "Mood lighting enabled for a disabled room",
                f"Mood lighting is enabled for {room_name}, but the room is not enabled, so nothing is controlled.",
                f"Enable the room in Configure → Rooms → {room_name}, or disable its mood lighting.",
                subject=f"lighting_room:{area_id}", **ctx,
            ))
        if room_lighting.mood_override and main_known and room_lighting.mood_override.casefold() not in known:
            add(Problem(
                "room_mood_unknown", Severity.WARNING, CONFIG, "Room mood unknown",
                f"{room_name} uses the mood '{room_lighting.mood_override}', which no mood selector offers.",
                "Choose another room mood, or follow the global mood, in the Smart Room Lighting card.",
                subject=f"room_mood:{area_id}", **ctx,
            ))
        for switch in room_lighting.manual_switches:
            if hass.states.get(switch) is None and er.async_get(hass).async_get(switch) is None:
                add(Problem(
                    "manual_switch_missing", Severity.WARNING, CONFIG, "Wall switch entity missing",
                    f"{switch} is configured as a wall switch of {room_name} but does not exist.",
                    "Remove or replace it in the Smart Room Lighting card.",
                    entity_id=switch, subject=f"switch:{switch}", **ctx,
                ))
        if not enabled:
            continue
        managed = controller.managed_room(area_id)
        active = controller.room_active(area_id)
        for entity_id in room_lighting.excluded:
            if hass.states.get(entity_id) is None and er.async_get(hass).async_get(entity_id) is None:
                add(Problem(
                    "excluded_light_missing", Severity.INFO, CONFIG, "Excluded light no longer exists",
                    f"{entity_id} is excluded in {room_name} but no longer exists.",
                    "Remove it from the excluded lights in the Smart Room Lighting card.",
                    entity_id=entity_id, subject=f"excluded:{entity_id}", **ctx,
                ))
        for entity_id in managed.outside_area:
            info = index.info(entity_id)
            add(Problem(
                "light_outside_area", Severity.WARNING, CONFIG, "Managed light is in another area",
                f"{entity_id} is a configured light of {room_name}, but it is now assigned to the area "
                f"'{area_name(hass, info.area_id) if info and info.area_id else '?'}'. It is still managed here.",
                f"Move it back, or remove it from the lights in Configure → Rooms → {room_name}.",
                entity_id=entity_id, name=friendly_name(hass, entity_id), **ctx,
            ))
        for group in managed.opaque_groups:
            category = index.category_of(group)
            same = [e for e in managed.controlled if e != group and category and index.category_of(e) == category]
            if same:
                add(Problem(
                    "group_overlap_unknown", Severity.WARNING, OPERATIONAL, "Possible duplicate group commands",
                    f"{group} is a group whose members Home Assistant cannot see (for example a Zigbee2MQTT "
                    f"group), and {room_name} also manages {', '.join(same)} in the same category. If they are "
                    "members of the group, they receive the same command twice.",
                    "Exclude either the group or its member lights in the Smart Room Lighting card.",
                    entity_id=group, name=friendly_name(hass, group), subject=f"opaque:{group}",
                    aspect="lighting", **ctx,
                ))
        acknowledged = set(room_lighting.acknowledged)
        by_automation: dict[str, list[Any]] = {}
        for conflicts in controller.conflicts(area_id).values():
            for c in conflicts:
                by_automation.setdefault(c.automation, []).append(c)
        for automation, conflicts in sorted(by_automation.items()):
            c0 = conflicts[0]
            lights = sorted({c.light for c in conflicts})
            blocking = active and c0.enabled and automation not in acknowledged
            state = "enabled" if c0.enabled else "disabled"
            if blocking:
                text = ("Mood lighting leaves these lights alone until you acknowledge the automation or "
                        "resolve the conflict.")
            elif automation in acknowledged:
                text = "Acknowledged: mood lighting and the automation both control these lights."
            elif c0.enabled:
                text = "Before you enable mood lighting here, decide how both should work together."
            else:
                text = "It is disabled, so it does not compete at the moment."
            add(Problem(
                "automation_conflict", Severity.WARNING if blocking else Severity.INFO, OPERATIONAL,
                "Another automation controls these lights",
                f"The {state} automation {c0.name or automation} ({automation}) appears to control "
                f"{', '.join(lights)} in {room_name} ({c0.detail}). {text}",
                "Disable or change that automation yourself, exclude the lights, or acknowledge it in the "
                "Smart Room Lighting card. Existing automations are never changed by this integration.",
                entity_id=automation, name=c0.name, subject=f"conflict:{automation}", aspect="lighting", **ctx,
            ))
        for light, found in sorted(controller.adaptive_conflicts(area_id).items() if uses_global else ()):
            names = ", ".join(c.get("name") or c["entity_id"] for c in found)
            add(Problem(
                "adaptive_controller_conflict", Severity.WARNING if active else Severity.INFO, OPERATIONAL,
                "Existing adjustment also controls this light",
                f"{names} (the existing global adjustment) also adjusts {light} in {room_name}. Smart Occupancy "
                "does not send global adjustment updates to it, so the two never compete.",
                "To let Smart Occupancy follow the global values for this light, stop the existing system from "
                "commanding it (for example remove it from that automation or Adaptive Lighting switch). "
                "Nothing is changed automatically.",
                entity_id=light, name=friendly_name(hass, light), subject=f"adaptive_conflict:{light}",
                aspect="lighting", **ctx,
            ))
        if active:
            rt = controller.runtime.get(area_id)
            if rt and rt.last_result in ("failed", "partial"):
                add(Problem(
                    "lighting_execution_failed", Severity.WARNING, OPERATIONAL, "Mood lighting command failed",
                    f"The last mood lighting application in {room_name} ({rt.last_decision_at}) "
                    f"{'partly ' if rt.last_result == 'partial' else ''}failed: {'; '.join(rt.last_errors)}.",
                    "Check the lights and their integration. The next mood change or arrival tries again.",
                    subject=f"execution:{area_id}", aspect="lighting", **ctx,
                ))
            mood, _ = controller.room_mood(area_id, status)
            if mood:
                plan = controller.plan(area_id, "preview", mood)
                for item in plan.items:
                    if item.unsupported and item.decision != "skipped":
                        dropped = any(n.get("level") == "warning" for n in item.unsupported)
                        add(Problem(
                            "lighting_unsupported", Severity.WARNING if dropped else Severity.INFO, OPERATIONAL,
                            "Unsupported light settings" if dropped else "Light settings adapted",
                            f"In {mood}, {item.entity_id}: "
                            + "; ".join(n["message"] for n in item.unsupported) + ". The rest is applied.",
                            "Adjust the profile for this light or category, or ignore this if intended.",
                            entity_id=item.entity_id, name=item.name, subject=f"unsupported:{item.entity_id}",
                            aspect="lighting", **ctx,
                        ))
    # Warnings the user chose to ignore stay visible as notes (Advanced), not as warnings.
    for i, p in enumerate(problems):
        if p.area_id and p.severity == Severity.WARNING and p.subject \
                and p.subject in lighting_cfg.room(p.area_id).dismissed:
            problems[i] = replace(p, severity=Severity.INFO)
    return problems


def validate_daylight(hass: HomeAssistant, config: IntegrationConfig, controller: LightingController, *,
                      settled: bool = True) -> list[Problem]:
    """Daylight sensing problems (0.3.3). Aspect "daylight": kept apart from occupancy and lighting health."""
    problems: list[Problem] = []
    add = problems.append
    glob = controller.lighting.daylight
    ent_reg = er.async_get(hass)
    if (outdoor := glob.outdoor_entity) is not None:
        state = hass.states.get(outdoor)
        name = friendly_name(hass, outdoor)
        if state is None and ent_reg.async_get(outdoor) is None:
            add(Problem(
                "daylight_outdoor_missing", Severity.WARNING, CONFIG, "Outdoor light sensor missing",
                f"The outdoor light sensor {outdoor} does not exist, so outdoor light is not used.",
                "Choose another outdoor light sensor in the Smart Room Lighting card (Advanced → Daylight).",
                entity_id=outdoor, subject="daylight_outdoor", aspect="daylight",
            ))
        else:
            reading = controller.daylight._outdoor_reading(dt_util.utcnow())
            if reading.status != "ok" and (settled or reading.status == "invalid"):
                add(Problem(
                    "daylight_outdoor_unavailable", Severity.WARNING, OPERATIONAL, "Outdoor light sensor unusable",
                    f"{reading.detail}. It is never taken as 0 lx: rooms use indoor sensors and the sun meanwhile.",
                    "Check the outdoor sensor's battery and connection.",
                    entity_id=outdoor, name=name, subject="daylight_outdoor", aspect="daylight",
                ))
    for area_id in sorted(config.enabled_rooms):
        cfg = controller.lighting.room(area_id).daylight
        room_name = area_name(hass, area_id) or area_id
        ctx = {"area_id": area_id, "room": room_name}
        status = controller.daylight.status(area_id)
        usable = [r for r in status.readings if r.usable]
        for reading in status.readings:
            entity_id = reading.entity_id
            if reading.status == "missing" and ent_reg.async_get(entity_id) is None:
                add(Problem(
                    "daylight_sensor_missing", Severity.WARNING, CONFIG, "Light sensor missing",
                    f"{entity_id} is a light sensor of {room_name} but does not exist.",
                    "Remove or replace it in the Smart Room Lighting card.",
                    entity_id=entity_id, subject=f"daylight_sensor:{entity_id}", aspect="daylight", **ctx,
                ))
            elif reading.status != "ok":
                if not settled and reading.status in ("missing", "unavailable", "unknown"):
                    continue  # still loading after a start
                fallback = next((r for r in usable if r.entity_id != entity_id), None)
                tail = (f" {fallback.name or fallback.entity_id} is used instead." if fallback
                        else " Daylight is estimated from the outdoor light and the sun meanwhile.")
                add(Problem(
                    "daylight_sensor_unavailable", Severity.WARNING if not fallback else Severity.INFO,
                    OPERATIONAL, "Light sensor not usable",
                    f"{reading.name or entity_id} in {room_name} {reading.detail}; it is never read as 0 lx.{tail}",
                    "Check the sensor's battery and connection, or its freshness limit (Advanced → Daylight).",
                    entity_id=entity_id, name=reading.name, subject=f"daylight_sensor:{entity_id}",
                    aspect="daylight", **ctx,
                ))
        for entity_id in cfg.influencing:
            if hass.states.get(entity_id) is None and ent_reg.async_get(entity_id) is None:
                add(Problem(
                    "daylight_influencing_missing", Severity.INFO, CONFIG, "Influencing light missing",
                    f"{entity_id} is listed as a light that reaches the sensors of {room_name}, but it does not exist.",
                    "Remove it from the influencing lights (Advanced → Daylight).",
                    entity_id=entity_id, subject=f"daylight_influencing:{entity_id}", aspect="daylight", **ctx,
                ))
        for note in status.estimate.notes:
            if "not listed as influencing" in note:
                add(Problem(
                    "daylight_unknown_light", Severity.INFO, OPERATIONAL, "Light sensor sees another light",
                    f"In {room_name}: {note}. That reading is not used as daylight.",
                    "Add the light that reaches the sensor as an influencing light (Advanced → Daylight).",
                    subject=f"daylight_unknown_light:{area_id}", aspect="daylight", **ctx,
                ))
    return problems
