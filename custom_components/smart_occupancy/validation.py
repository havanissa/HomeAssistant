"""Configuration validation against the live Home Assistant registries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, label_registry as lr

from . import lighting
from .const import MOOD_ENTITY_DOMAINS, Severity
from .models import IntegrationConfig
from .mood import mood_names
from .occupancy import MISSING, entity_health
from .profiles import iter_profile_entries, validate_setting
from .supervision import mode_permitted


@dataclass(slots=True, frozen=True)
class Problem:
    """A configuration or runtime problem."""

    severity: Severity
    code: str
    message: str
    area_id: str | None = None
    entity_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "severity": self.severity.value,
            "code": self.code,
            "message": self.message,
        }
        if self.area_id:
            out["area_id"] = self.area_id
        if self.entity_id:
            out["entity_id"] = self.entity_id
        return out


def validate(hass: HomeAssistant, config: IntegrationConfig) -> list[Problem]:
    """Validate the whole configuration. Pure read; changes nothing."""
    problems: list[Problem] = []
    add = problems.append

    # Operating mode -------------------------------------------------------
    if not mode_permitted(config.operating_mode):
        add(Problem(Severity.ERROR, "mode_not_permitted",
                    f"Operating mode '{config.operating_mode}' is not available in "
                    "this phase; running in Observe mode."))

    # Mood helpers ---------------------------------------------------------
    mood = config.mood
    for label, entity_id in (
        ("Auto Mood", mood.auto_mood_entity),
        ("Manual Mood", mood.manual_mood_entity),
        ("Main Mood", mood.main_mood_entity),
    ):
        if not entity_id:
            add(Problem(Severity.ERROR, "mood_entity_not_configured",
                        f"{label} entity is not configured."))
            continue
        if entity_id.split(".", 1)[0] not in MOOD_ENTITY_DOMAINS:
            add(Problem(Severity.ERROR, "mood_entity_wrong_domain",
                        f"{label} entity {entity_id} must be an input_select or select.",
                        entity_id=entity_id))
        if entity_health(hass, entity_id) == MISSING:
            add(Problem(Severity.ERROR, "missing_entity",
                        f"{label} entity {entity_id} does not exist.", entity_id=entity_id))
    if mood.manual_mood_entity and (state := hass.states.get(mood.manual_mood_entity)):
        options = state.attributes.get("options")
        if options and mood.auto_option.casefold() not in {o.casefold() for o in options}:
            add(Problem(Severity.WARNING, "auto_option_not_found",
                        f"'{mood.auto_option}' is not an option of "
                        f"{mood.manual_mood_entity}; Main Mood is assumed to follow "
                        "Manual Mood.", entity_id=mood.manual_mood_entity))
    if (
        mood.main_mood_entity
        and mood.auto_mood_entity
        and (main := hass.states.get(mood.main_mood_entity))
        and (auto := hass.states.get(mood.auto_mood_entity))
    ):
        main_opts = {o.casefold() for o in main.attributes.get("options") or []}
        missing_opts = [o for o in auto.attributes.get("options") or [] if o.casefold() not in main_opts]
        if main_opts and missing_opts:
            add(Problem(Severity.WARNING, "mood_options_mismatch",
                        f"Auto Mood options not available in Main Mood: {', '.join(missing_opts)}",
                        entity_id=mood.main_mood_entity))

    # Light categories -----------------------------------------------------
    label_reg = lr.async_get(hass)
    seen_labels: dict[str, str] = {}
    for cat in config.categories:
        if not cat.label_id:
            add(Problem(Severity.INFO, "category_without_label",
                        f"Category '{cat.name}' has no label assigned."))
            continue
        if label_reg.async_get_label(cat.label_id) is None:
            add(Problem(Severity.WARNING, "category_label_missing",
                        f"Label '{cat.label_id}' of category '{cat.name}' no longer exists."))
        if cat.label_id in seen_labels:
            add(Problem(Severity.WARNING, "label_used_twice",
                        f"Categories '{seen_labels[cat.label_id]}' and '{cat.name}' "
                        f"share label '{cat.label_id}'."))
        seen_labels.setdefault(cat.label_id, cat.name)

    _, light_categories = lighting.classify_lights(hass, config.categories)

    # Rooms ----------------------------------------------------------------
    area_reg = ar.async_get(hass)
    managed: dict[str, list[str]] = {}
    signal_owner: dict[str, str] = {}
    for area_id, room in config.enabled_rooms.items():
        if area_reg.async_get_area(area_id) is None:
            add(Problem(Severity.WARNING, "area_missing",
                        f"Area '{area_id}' no longer exists; its configuration is kept.",
                        area_id=area_id))
        if not room.sensors:
            add(Problem(Severity.INFO, "room_without_sensors",
                        "No occupancy signals assigned.", area_id=area_id))
        for entity_id in room.referenced_entities:
            if entity_health(hass, entity_id) == MISSING:
                add(Problem(Severity.WARNING, "missing_entity",
                            f"Configured entity {entity_id} does not exist "
                            "(removed or renamed outside Home Assistant's registry).",
                            area_id=area_id, entity_id=entity_id))
        for s in room.sensors:
            if (other := signal_owner.get(s.entity_id)) and other != area_id:
                add(Problem(Severity.INFO, "signal_shared",
                            f"{s.entity_id} is assigned to several rooms.",
                            area_id=area_id, entity_id=s.entity_id))
            signal_owner.setdefault(s.entity_id, area_id)
        managed[area_id] = list(room.lights)
        for light in room.lights:
            if light.split(".", 1)[0] != "light":
                add(Problem(Severity.ERROR, "not_a_light",
                            f"{light} is not a light entity.", area_id=area_id, entity_id=light))
                continue
            cats = light_categories.get(light, [])
            if not cats:
                add(Problem(Severity.WARNING, "light_without_category",
                            f"{light} has no category label; no global profile applies to it.",
                            area_id=area_id, entity_id=light))
            elif len(cats) > 1:
                add(Problem(Severity.WARNING, "light_in_multiple_categories",
                            f"{light} is in several categories ({', '.join(cats)}); "
                            f"'{cats[0]}' takes precedence.", area_id=area_id, entity_id=light))
            if lighting.is_unexposed_group(hass, light):
                add(Problem(Severity.INFO, "group_members_unknown",
                            f"{light} is a group that does not expose its members; "
                            "overlap with its member lights cannot be checked.",
                            area_id=area_id, entity_id=light))

    owners: dict[str, list[str]] = {}
    for area_id, lights in managed.items():
        for light in lights:
            owners.setdefault(light, []).append(area_id)
    for light, rooms in owners.items():
        if len(rooms) > 1:
            add(Problem(Severity.WARNING, "light_in_multiple_rooms",
                        f"{light} is managed by several rooms: {', '.join(rooms)}.",
                        entity_id=light))
    for c in lighting.find_group_conflicts(hass, managed):
        add(Problem(Severity.WARNING, "group_member_conflict",
                    f"Group {c.group} ({c.group_room}) and its member {c.member} "
                    f"({c.member_room}) are both selected for independent control.",
                    area_id=c.member_room, entity_id=c.member))

    # Profiles -------------------------------------------------------------
    known_moods = {m.casefold() for m in mood_names(hass, mood)}
    category_keys = {c.key for c in config.categories}
    for scope, area_id, mood_name, target, setting in iter_profile_entries(config):
        where = f"{scope} profile '{mood_name}' / {target}"
        if known_moods and mood_name.casefold() not in known_moods:
            add(Problem(Severity.WARNING, "profile_unknown_mood",
                        f"{where}: mood '{mood_name}' is not a Main Mood option.",
                        area_id=area_id))
        if scope in ("global", "room") and target not in category_keys:
            add(Problem(Severity.WARNING, "profile_unknown_category",
                        f"{where}: category does not exist.", area_id=area_id))
        if scope == "light":
            room = config.rooms.get(area_id or "")
            if room is None or target not in room.lights:
                add(Problem(Severity.WARNING, "profile_light_not_managed",
                            f"{where}: light is not a managed light of this room.",
                            area_id=area_id, entity_id=target))
            if caps := lighting.capabilities(hass, target):
                if setting.rgb_color is not None and not caps["rgb"]:
                    add(Problem(Severity.WARNING, "profile_rgb_unsupported",
                                f"{where}: light does not support RGB colour.",
                                area_id=area_id, entity_id=target))
                if setting.color_temp_kelvin is not None and not caps["color_temp"]:
                    add(Problem(Severity.WARNING, "profile_kelvin_unsupported",
                                f"{where}: light does not support colour temperature.",
                                area_id=area_id, entity_id=target))
                if setting.effect and setting.effect not in caps["effects"]:
                    add(Problem(Severity.WARNING, "profile_effect_unsupported",
                                f"{where}: effect '{setting.effect}' is not offered by the light.",
                                area_id=area_id, entity_id=target))
        if scope == "global" and setting.action.value == "inherit":
            add(Problem(Severity.INFO, "global_inherit",
                        f"{where}: 'inherit' at global level means 'ignore'.",
                        area_id=area_id))
        for field_name, error in validate_setting(setting).items():
            add(Problem(Severity.ERROR, "profile_invalid",
                        f"{where}: {field_name} is invalid ({error}).", area_id=area_id))
    return problems
