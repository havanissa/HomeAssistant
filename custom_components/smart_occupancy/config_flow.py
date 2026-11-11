"""Config and options flows (UI configuration)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import label_registry as lr, selector as sel

from . import discovery
from .const import (
    DEFAULT_AUTO_MOOD_ENTITY,
    DEFAULT_MAIN_MOOD_ENTITY,
    DEFAULT_MANUAL_MOOD_ENTITY,
    DEFAULT_MISMATCH_GRACE_SECONDS,
    DEFAULT_ROLE_FOR_KIND,
    DOMAIN,
    KELVIN_MAX,
    KELVIN_MIN,
    MOOD_ENTITY_DOMAINS,
    NAME,
    TIMING_PARAMETERS,
    TRANSITION_MAX,
    LightAction,
    OperatingMode,
    SensorKind,
    SensorRole,
)
from .models import (
    IntegrationConfig,
    LightCategory,
    MoodConfig,
    OccupancyTiming,
    ProfileSetting,
    RoomConfig,
    SensorAssignment,
    default_categories,
    unique_category_key,
)
from .mood import mood_names
from .profiles import setting_from_form, validate_setting
from .supervision import mode_permitted

CONF_AUTO_MOOD = "auto_mood_entity"
CONF_MANUAL_MOOD = "manual_mood_entity"
CONF_MAIN_MOOD = "main_mood_entity"
CONF_AUTO_OPTION = "auto_option"
CONF_GRACE = "mismatch_grace_seconds"
CONF_MODE = "operating_mode"
CONF_CREATE_LABELS = "create_missing_labels"
CONF_ROOMS = "rooms"
CONF_ENABLED = "enabled"
CONF_LIGHTS = "lights"
CONF_REFERENCE = "reference_occupancy_entity"
CONF_RESET = "reset"
CONF_AREA = "area"
CONF_NAME = "name"
CONF_LABEL = "label"
CONF_CATEGORY = "category"
CONF_CATEGORIES = "categories"
CONF_SCOPE = "scope"
CONF_MOOD = "mood"
CONF_ROOM = "room"
CONF_LIGHT = "light"
CONF_REMOVE = "remove"
CONF_INVERTED = "inverted_signals"
CONF_ADJACENT = "adjacent_rooms"

# A stuck detection shorter than this would flag normal sensors.
MIN_STUCK_SECONDS = 60

# Room form field -> (sensor kind, allowed domains or None for any)
KIND_FIELDS: dict[str, tuple[SensorKind, list[str] | None]] = {
    "pir": (SensorKind.PIR, ["binary_sensor"]),
    "radar": (SensorKind.RADAR, ["binary_sensor"]),
    "door": (SensorKind.DOOR, ["binary_sensor"]),
    "pc_activity": (
        SensorKind.PC_ACTIVITY,
        ["binary_sensor", "sensor", "input_boolean", "switch", "media_player", "device_tracker"],
    ),
    "bed": (SensorKind.BED, ["binary_sensor", "input_boolean"]),
    "other": (SensorKind.OTHER, None),
}


def _entity_selector(domains: list[str] | None, multiple: bool = True) -> sel.EntitySelector:
    cfg = sel.EntitySelectorConfig(multiple=multiple)
    if domains:
        cfg["domain"] = domains
    return sel.EntitySelector(cfg)


def _select(options: list[sel.SelectOptionDict] | list[str], *, translation_key: str | None = None,
            multiple: bool = False, custom_value: bool = False) -> sel.SelectSelector:
    cfg = sel.SelectSelectorConfig(
        options=options, multiple=multiple, custom_value=custom_value,
        mode=sel.SelectSelectorMode.DROPDOWN,
    )
    if translation_key:
        cfg["translation_key"] = translation_key
    return sel.SelectSelector(cfg)


# --------------------------------------------------------------------- mood
def mood_schema(hass: HomeAssistant, mood: MoodConfig, include_mode: OperatingMode | None) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(CONF_AUTO_MOOD): _entity_selector(MOOD_ENTITY_DOMAINS, multiple=False),
        vol.Required(CONF_MANUAL_MOOD): _entity_selector(MOOD_ENTITY_DOMAINS, multiple=False),
        vol.Required(CONF_MAIN_MOOD): _entity_selector(MOOD_ENTITY_DOMAINS, multiple=False),
        vol.Required(CONF_AUTO_OPTION, default=mood.auto_option): sel.TextSelector(),
        vol.Required(CONF_GRACE, default=mood.mismatch_grace_seconds): sel.NumberSelector(
            sel.NumberSelectorConfig(min=0, max=600, step=1, unit_of_measurement="s",
                                     mode=sel.NumberSelectorMode.BOX)
        ),
    }
    if include_mode is not None:
        fields[vol.Required(CONF_MODE, default=include_mode.value)] = _select(
            [m.value for m in OperatingMode], translation_key="operating_mode"
        )
    return vol.Schema(fields)


def mood_suggestions(hass: HomeAssistant, mood: MoodConfig) -> dict[str, Any]:
    """Prefill with configured entities, else the documented defaults if they exist."""
    out: dict[str, Any] = {}
    for key, current, default in (
        (CONF_AUTO_MOOD, mood.auto_mood_entity, DEFAULT_AUTO_MOOD_ENTITY),
        (CONF_MANUAL_MOOD, mood.manual_mood_entity, DEFAULT_MANUAL_MOOD_ENTITY),
        (CONF_MAIN_MOOD, mood.main_mood_entity, DEFAULT_MAIN_MOOD_ENTITY),
    ):
        if current:
            out[key] = current
        elif hass.states.get(default) is not None:
            out[key] = default
    return out


def validate_mood_input(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in (CONF_AUTO_MOOD, CONF_MANUAL_MOOD, CONF_MAIN_MOOD):
        entity_id = user_input.get(key)
        if not entity_id or hass.states.get(entity_id) is None:
            errors[key] = "entity_not_found"
    ids = [user_input.get(k) for k in (CONF_AUTO_MOOD, CONF_MANUAL_MOOD, CONF_MAIN_MOOD)]
    if not errors and len(set(ids)) != 3:
        errors["base"] = "mood_entities_not_distinct"
    manual = hass.states.get(user_input.get(CONF_MANUAL_MOOD) or "")
    auto_option = (user_input.get(CONF_AUTO_OPTION) or "").strip()
    if not auto_option:
        errors[CONF_AUTO_OPTION] = "auto_option_not_found"
    elif manual is not None and (options := manual.attributes.get("options")):
        if auto_option.casefold() not in {o.casefold() for o in options}:
            errors[CONF_AUTO_OPTION] = "auto_option_not_found"
    return errors


def mood_from_input(user_input: dict[str, Any]) -> MoodConfig:
    return MoodConfig(
        auto_mood_entity=user_input[CONF_AUTO_MOOD],
        manual_mood_entity=user_input[CONF_MANUAL_MOOD],
        main_mood_entity=user_input[CONF_MAIN_MOOD],
        auto_option=user_input[CONF_AUTO_OPTION].strip(),
        mismatch_grace_seconds=int(user_input.get(CONF_GRACE, DEFAULT_MISMATCH_GRACE_SECONDS)),
    )


# ---------------------------------------------------------------- occupancy
def timing_schema(timing: OccupancyTiming | None) -> vol.Schema:
    """Timing fields. With ``timing`` None every field is optional (= inherit)."""
    fields: dict[Any, Any] = {}
    for key, (_, low, high) in TIMING_PARAMETERS.items():
        selector = sel.NumberSelector(sel.NumberSelectorConfig(
            min=low, max=high, step=1, unit_of_measurement="s", mode=sel.NumberSelectorMode.BOX,
        ))
        if timing is None:
            fields[vol.Optional(key)] = selector
        else:
            fields[vol.Required(key, default=getattr(timing, key))] = selector
    return vol.Schema(fields)


def timing_from_input(user_input: dict[str, Any]) -> dict[str, int]:
    return {
        key: int(value)
        for key, value in user_input.items()
        if key in TIMING_PARAMETERS and value is not None and value != ""
    }


def validate_timing(values: dict[str, int]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in ("trigger_stuck_after", "presence_stuck_after"):
        if (value := values.get(key)) and value < MIN_STUCK_SECONDS:
            errors[key] = "stuck_too_short"
    return errors


def timing_text(timing: OccupancyTiming) -> str:
    return ", ".join(f"{key.replace('_', ' ')} {value} s" for key, value in timing.as_dict().items())


# --------------------------------------------------------------- categories
def ensure_label(hass: HomeAssistant, name: str) -> str:
    """Return the id of a label with this name, creating the label if needed.

    Only creates the label; it is never applied to any entity.
    """
    reg = lr.async_get(hass)
    if existing := reg.async_get_label_by_name(name):
        return existing.label_id
    return reg.async_create(name).label_id


def matching_label(hass: HomeAssistant, category: LightCategory) -> str | None:
    """An existing label whose name equals the category name or key."""
    reg = lr.async_get(hass)
    for candidate in (category.name, category.key.replace("_", " ")):
        if label := reg.async_get_label_by_name(candidate):
            return label.label_id
    return None


# -------------------------------------------------------------------- rooms
def area_options(hass: HomeAssistant, config: IntegrationConfig) -> list[sel.SelectOptionDict]:
    options: list[sel.SelectOptionDict] = []
    known: set[str] = set()
    for area in discovery.list_areas(hass):
        known.add(area.id)
        room = config.rooms.get(area.id)
        suffix = " ✓" if room and room.enabled else ""
        options.append(sel.SelectOptionDict(value=area.id, label=f"{area.name}{suffix}"))
    for area_id in sorted(set(config.rooms) - known):
        options.append(sel.SelectOptionDict(value=area_id, label=f"{area_id} (area missing)"))
    return options


def discovery_text(found: discovery.AreaDiscovery | None) -> str:
    if found is None:
        return "Area no longer exists."
    lines = [
        f"- {e.entity_id} ({e.device_class} → suggested {e.suggested_kind})"
        for e in found.signals
    ]
    lines += [f"- {e.entity_id} ({e.device_class}, not pre-assigned)" for e in found.other_binary_sensors]
    if found.lights:
        lines.append(f"- Lights in area: {', '.join(found.lights)}")
    return "\n".join(lines) or "No sensors or lights found in this area."


class RoomFlowMixin:
    """Room assignment steps shared by the config flow and the options flow."""

    hass: HomeAssistant
    _config: IntegrationConfig
    _area_id: str
    _pending_room: RoomConfig

    async def _room_done(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _room_assign_schema(self, room: RoomConfig, *, editing: bool) -> vol.Schema:
        fields: dict[Any, Any] = {}
        if editing:
            fields[vol.Required(CONF_ENABLED, default=room.enabled)] = sel.BooleanSelector()
        for key, (_, domains) in KIND_FIELDS.items():
            fields[vol.Optional(key)] = _entity_selector(domains)
        fields[vol.Optional(CONF_LIGHTS)] = _entity_selector(["light"])
        fields[vol.Optional(CONF_REFERENCE)] = _entity_selector(
            ["input_boolean", "binary_sensor"], multiple=False
        )
        if editing:
            fields[vol.Required(CONF_RESET, default=False)] = sel.BooleanSelector()
        return vol.Schema(fields)

    def _room_suggestions(self, room: RoomConfig, found: discovery.AreaDiscovery | None) -> dict[str, Any]:
        suggested: dict[str, Any] = {}
        has_config = bool(room.sensors or room.lights)
        for key, (kind, _) in KIND_FIELDS.items():
            if has_config:
                suggested[key] = [s.entity_id for s in room.sensors if s.kind == kind]
            elif found is not None:
                suggested[key] = found.suggested(kind)
        suggested[CONF_LIGHTS] = list(room.lights)
        if room.reference_occupancy_entity:
            suggested[CONF_REFERENCE] = room.reference_occupancy_entity
        return suggested

    async def _async_room_assign(
        self, user_input: dict[str, Any] | None, *, editing: bool, step_id: str
    ) -> ConfigFlowResult:
        room = self._config.room(self._area_id)
        found = (
            discovery.discover_area(self.hass, self._area_id)
            if discovery.area_name(self.hass, self._area_id) is not None
            else None
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if editing and user_input.get(CONF_RESET):
                self._config = self._config.without_room(self._area_id)
                return await self._room_done()
            seen: set[str] = set()
            sensors: list[SensorAssignment] = []
            previous = {(s.entity_id, s.kind): s for s in room.sensors}
            for key, (kind, _) in KIND_FIELDS.items():
                for entity_id in user_input.get(key) or []:
                    if entity_id in seen:
                        errors[key] = "duplicate_assignment"
                    seen.add(entity_id)
                    old = previous.get((entity_id, kind))
                    sensors.append(SensorAssignment(
                        entity_id, kind,
                        old.role if old else DEFAULT_ROLE_FOR_KIND[kind],
                        old.inverted if old else False,
                    ))
            if not errors:
                self._pending_room = replace(
                    room,
                    enabled=bool(user_input.get(CONF_ENABLED, True)),
                    sensors=tuple(sensors),
                    lights=tuple(dict.fromkeys(user_input.get(CONF_LIGHTS) or [])),
                    reference_occupancy_entity=user_input.get(CONF_REFERENCE) or None,
                )
                if sensors:
                    return await self.async_step_room_roles()  # type: ignore[attr-defined]
                self._config = self._config.with_room(self._pending_room)
                return await self._room_done()
        schema = self._room_assign_schema(room, editing=editing)
        suggested = user_input if user_input is not None else self._room_suggestions(room, found)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),  # type: ignore[attr-defined]
            errors=errors,
            description_placeholders={
                "room": discovery.area_name(self.hass, self._area_id) or self._area_id,
                "discovered": discovery_text(found),
            },
        )

    async def _after_roles(self) -> ConfigFlowResult:
        return await self._room_done()

    async def async_step_room_roles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._pending_room
        if user_input is not None:
            inverted = set(user_input.get(CONF_INVERTED) or [])
            sensors = tuple(
                replace(
                    s, role=SensorRole(user_input.get(s.entity_id, s.role)), inverted=s.entity_id in inverted
                )
                for s in room.sensors
            )
            self._pending_room = replace(room, sensors=sensors)
            self._config = self._config.with_room(self._pending_room)
            return await self._after_roles()
        fields: dict[Any, Any] = {
            vol.Required(s.entity_id, default=s.role.value): _select(
                [r.value for r in SensorRole], translation_key="sensor_role"
            )
            for s in room.sensors
        }
        fields[vol.Optional(CONF_INVERTED, default=[s.entity_id for s in room.sensors if s.inverted])] = _select(
            [s.entity_id for s in room.sensors], multiple=True
        )
        schema = vol.Schema(fields)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="room_roles",
            data_schema=schema,
            description_placeholders={
                "room": discovery.area_name(self.hass, room.area_id) or room.area_id,
                "sensors": "\n".join(f"- {s.entity_id}: {s.kind.value}" for s in room.sensors),
            },
        )


# -------------------------------------------------------------- config flow
class SmartOccupancyConfigFlow(RoomFlowMixin, ConfigFlow, domain=DOMAIN):
    """Initial setup: mood helpers -> categories -> rooms -> per-room assignment."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        self._config = IntegrationConfig(categories=default_categories())
        self._room_queue: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> SmartOccupancyOptionsFlow:
        return SmartOccupancyOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_mood_input(self.hass, user_input)
            if not errors:
                self._config = replace(self._config, mood=mood_from_input(user_input))
                return await self.async_step_categories()
        schema = mood_schema(self.hass, self._config.mood, include_mode=None)
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or mood_suggestions(self.hass, self._config.mood)
            ),
            errors=errors,
        )

    async def async_step_categories(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            create = user_input.get(CONF_CREATE_LABELS, False)
            categories = []
            for cat in self._config.categories:
                label_id = user_input.get(cat.key) or None
                if label_id is None and create:
                    label_id = ensure_label(self.hass, cat.name)
                categories.append(replace(cat, label_id=label_id))
            self._config = replace(self._config, categories=tuple(categories))
            return await self.async_step_rooms()
        fields: dict[Any, Any] = {
            vol.Optional(cat.key): sel.LabelSelector() for cat in self._config.categories
        }
        fields[vol.Required(CONF_CREATE_LABELS, default=False)] = sel.BooleanSelector()
        suggested = {
            cat.key: label for cat in self._config.categories
            if (label := matching_label(self.hass, cat))
        }
        return self.async_show_form(
            step_id="categories",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), suggested),
        )

    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._room_queue = list(user_input.get(CONF_ROOMS) or [])
            for area_id in self._room_queue:
                self._config = self._config.with_room(RoomConfig(area_id=area_id, enabled=True))
            return await self._room_done()
        pilot = discovery.find_pilot_area(self.hass)
        schema = vol.Schema({
            vol.Optional(CONF_ROOMS): _select(area_options(self.hass, self._config), multiple=True)
        })
        return self.async_show_form(
            step_id="rooms",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_ROOMS: [pilot] if pilot else []}
            ),
        )

    async def async_step_room_assign(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_room_assign(user_input, editing=False, step_id="room_assign")

    async def _room_done(self) -> ConfigFlowResult:
        if self._room_queue:
            self._area_id = self._room_queue.pop(0)
            return await self.async_step_room_assign()
        return self.async_create_entry(title=NAME, data={}, options=self._config.as_dict())


# ------------------------------------------------------------- options flow
class SmartOccupancyOptionsFlow(RoomFlowMixin, OptionsFlow):
    """Edit any part of the configuration after setup. Each path saves on finish."""

    _config: IntegrationConfig
    _category_key: str | None = None
    _scope: str = "global"
    _mood: str = ""
    _profile_area: str | None = None
    _target: str = ""

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self._config.as_dict())

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._config = IntegrationConfig.from_dict(dict(self.config_entry.options))
        return self.async_show_menu(
            step_id="init", menu_options=["general", "occupancy", "rooms", "categories", "profiles"]
        )

    # occupancy timing ----------------------------------------------------
    async def async_step_occupancy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            values = timing_from_input(user_input)
            errors = validate_timing(values)
            if not errors:
                self._config = replace(self._config, occupancy=OccupancyTiming.from_dict(values))
                return self._save()
        return self.async_show_form(
            step_id="occupancy",
            data_schema=self.add_suggested_values_to_schema(
                timing_schema(self._config.occupancy), user_input or self._config.occupancy.as_dict()
            ),
            errors=errors,
        )

    async def _after_roles(self) -> ConfigFlowResult:
        return await self.async_step_room_occupancy()

    async def async_step_room_occupancy(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._config.room(self._area_id)
        errors: dict[str, str] = {}
        if user_input is not None:
            overrides = timing_from_input(user_input)
            errors = validate_timing(overrides)
            if not errors:
                adjacent = tuple(a for a in user_input.get(CONF_ADJACENT) or [] if a != self._area_id)
                self._config = self._config.with_room(replace(room, timing=overrides, adjacent_rooms=adjacent))
                return self._save()
        schema = timing_schema(None).extend({
            vol.Optional(CONF_ADJACENT): _select(
                [o for o in area_options(self.hass, self._config) if o["value"] != self._area_id], multiple=True
            ),
        })
        suggested = user_input if user_input is not None else {
            **room.timing, CONF_ADJACENT: list(room.adjacent_rooms),
        }
        return self.async_show_form(
            step_id="room_occupancy",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "room": discovery.area_name(self.hass, self._area_id) or self._area_id,
                "global": timing_text(self._config.occupancy),
            },
        )

    # general -------------------------------------------------------------
    async def async_step_general(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate_mood_input(self.hass, user_input)
            mode = OperatingMode(user_input.get(CONF_MODE, OperatingMode.OBSERVE))
            if not mode_permitted(mode):
                errors[CONF_MODE] = "mode_not_available"
            if not errors:
                self._config = replace(
                    self._config, mood=mood_from_input(user_input), operating_mode=mode
                )
                return self._save()
        schema = mood_schema(self.hass, self._config.mood, include_mode=self._config.operating_mode)
        return self.async_show_form(
            step_id="general",
            data_schema=self.add_suggested_values_to_schema(
                schema, user_input or mood_suggestions(self.hass, self._config.mood)
            ),
            errors=errors,
        )

    # rooms ---------------------------------------------------------------
    async def async_step_rooms(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._area_id = user_input[CONF_AREA]
            return await self.async_step_room_edit()
        return self.async_show_form(
            step_id="rooms",
            data_schema=vol.Schema({
                vol.Required(CONF_AREA): _select(area_options(self.hass, self._config))
            }),
        )

    async def async_step_room_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_room_assign(user_input, editing=True, step_id="room_edit")

    async def _room_done(self) -> ConfigFlowResult:
        return self._save()

    # categories ----------------------------------------------------------
    async def async_step_categories(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="categories",
            menu_options=["category_add", "category_select", "category_remove", "category_create_labels"],
        )

    def _category_options(self) -> list[sel.SelectOptionDict]:
        return [sel.SelectOptionDict(value=c.key, label=c.name) for c in self._config.categories]

    async def async_step_category_add(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            if not name:
                errors[CONF_NAME] = "name_required"
            elif name.casefold() in {c.name.casefold() for c in self._config.categories}:
                errors[CONF_NAME] = "category_exists"
            else:
                key = unique_category_key(name, {c.key for c in self._config.categories})
                cat = LightCategory(key=key, name=name, label_id=user_input.get(CONF_LABEL) or None)
                self._config = replace(self._config, categories=(*self._config.categories, cat))
                return self._save()
        return self.async_show_form(
            step_id="category_add",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME): sel.TextSelector(),
                vol.Optional(CONF_LABEL): sel.LabelSelector(),
            }),
            errors=errors,
        )

    async def async_step_category_select(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._config.categories:
            return self.async_abort(reason="no_categories")
        if user_input is not None:
            self._category_key = user_input[CONF_CATEGORY]
            return await self.async_step_category_edit()
        return self.async_show_form(
            step_id="category_select",
            data_schema=vol.Schema({vol.Required(CONF_CATEGORY): _select(self._category_options())}),
        )

    async def async_step_category_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        cat = self._config.category(self._category_key or "")
        if cat is None:
            return self.async_abort(reason="no_categories")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            others = {c.name.casefold() for c in self._config.categories if c.key != cat.key}
            if not name:
                errors[CONF_NAME] = "name_required"
            elif name.casefold() in others:
                errors[CONF_NAME] = "category_exists"
            else:
                updated = replace(cat, name=name, label_id=user_input.get(CONF_LABEL) or None)
                self._config = replace(self._config, categories=tuple(
                    updated if c.key == cat.key else c for c in self._config.categories
                ))
                return self._save()
        schema = vol.Schema({
            vol.Required(CONF_NAME, default=cat.name): sel.TextSelector(),
            vol.Optional(CONF_LABEL): sel.LabelSelector(),
        })
        return self.async_show_form(
            step_id="category_edit",
            data_schema=self.add_suggested_values_to_schema(schema, {CONF_LABEL: cat.label_id}),
            errors=errors,
            description_placeholders={"category": cat.name},
        )

    async def async_step_category_remove(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._config.categories:
            return self.async_abort(reason="no_categories")
        if user_input is not None:
            remove = set(user_input.get(CONF_CATEGORIES) or [])
            self._config = _without_categories(self._config, remove)
            return self._save()
        return self.async_show_form(
            step_id="category_remove",
            data_schema=vol.Schema({
                vol.Optional(CONF_CATEGORIES): _select(self._category_options(), multiple=True)
            }),
        )

    async def async_step_category_create_labels(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        unmapped = [c for c in self._config.categories if not c.label_id]
        if not unmapped:
            return self.async_abort(reason="all_categories_labelled")
        if user_input is not None:
            self._config = replace(self._config, categories=tuple(
                replace(c, label_id=ensure_label(self.hass, c.name)) if not c.label_id else c
                for c in self._config.categories
            ))
            return self._save()
        return self.async_show_form(
            step_id="category_create_labels",
            data_schema=vol.Schema({}),
            description_placeholders={"categories": ", ".join(c.name for c in unmapped)},
        )

    # profiles ------------------------------------------------------------
    async def async_step_profiles(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._scope = user_input[CONF_SCOPE]
            self._mood = user_input[CONF_MOOD].strip()
            if self._scope == "global":
                self._profile_area = None
                return await self.async_step_profile_target()
            return await self.async_step_profile_room()
        moods = mood_names(self.hass, self._config.mood)
        moods += [m for m in self._config.global_profiles if m not in moods]
        return self.async_show_form(
            step_id="profiles",
            data_schema=vol.Schema({
                vol.Required(CONF_SCOPE, default="global"): _select(
                    ["global", "room", "light"], translation_key="profile_scope"
                ),
                vol.Required(CONF_MOOD): _select(moods, custom_value=True),
            }),
        )

    async def async_step_profile_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rooms = self._config.enabled_rooms
        if not rooms:
            return self.async_abort(reason="no_rooms")
        if user_input is not None:
            self._profile_area = user_input[CONF_ROOM]
            return await self.async_step_profile_target()
        options = [
            sel.SelectOptionDict(value=a, label=discovery.area_name(self.hass, a) or a)
            for a in sorted(rooms)
        ]
        return self.async_show_form(
            step_id="profile_room",
            data_schema=vol.Schema({vol.Required(CONF_ROOM): _select(options)}),
        )

    async def async_step_profile_target(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._scope == "light":
            lights = list(self._config.room(self._profile_area or "").lights)
            if not lights:
                return self.async_abort(reason="no_lights")
            field_key, options = CONF_LIGHT, lights
        else:
            if not self._config.categories:
                return self.async_abort(reason="no_categories")
            field_key, options = CONF_CATEGORY, self._category_options()
        if user_input is not None:
            self._target = user_input[field_key]
            return await self.async_step_profile_edit()
        return self.async_show_form(
            step_id="profile_target",
            data_schema=vol.Schema({vol.Required(field_key): _select(options)}),
        )

    def _profile_table(self, config: IntegrationConfig) -> dict[str, dict[str, ProfileSetting]]:
        if self._scope == "global":
            return config.global_profiles
        room = config.room(self._profile_area or "")
        return room.mood_overrides if self._scope == "room" else room.light_overrides

    def _with_profile(self, setting: ProfileSetting | None) -> IntegrationConfig:
        table = {m: dict(t) for m, t in self._profile_table(self._config).items()}
        targets = table.setdefault(self._mood, {})
        if setting is None:
            targets.pop(self._target, None)
        else:
            targets[self._target] = setting
        table = {m: t for m, t in table.items() if t}
        if self._scope == "global":
            return replace(self._config, global_profiles=table)
        room = self._config.room(self._profile_area or "")
        field_name = "mood_overrides" if self._scope == "room" else "light_overrides"
        return self._config.with_room(replace(room, **{field_name: table}))

    async def async_step_profile_edit(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = self._profile_table(self._config).get(self._mood, {}).get(self._target)
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_REMOVE):
                self._config = self._with_profile(None)
                return self._save()
            try:
                setting = setting_from_form(user_input)
            except (TypeError, ValueError):
                errors["base"] = "invalid_profile"
            else:
                errors = validate_setting(setting)
                if not errors:
                    self._config = self._with_profile(setting)
                    return self._save()
        schema = vol.Schema({
            vol.Required("action", default=(current.action if current else LightAction.INHERIT).value):
                _select([a.value for a in LightAction], translation_key="light_action"),
            vol.Optional("brightness_pct"): sel.NumberSelector(sel.NumberSelectorConfig(
                min=0, max=100, step=1, unit_of_measurement="%", mode=sel.NumberSelectorMode.SLIDER)),
            vol.Optional("color_temp_kelvin"): sel.NumberSelector(sel.NumberSelectorConfig(
                min=KELVIN_MIN, max=KELVIN_MAX, step=50, unit_of_measurement="K",
                mode=sel.NumberSelectorMode.BOX)),
            vol.Optional("rgb_color"): sel.ColorRGBSelector(),
            vol.Optional("effect"): sel.TextSelector(),
            vol.Optional("transition"): sel.NumberSelector(sel.NumberSelectorConfig(
                min=0, max=TRANSITION_MAX, step=0.1, unit_of_measurement="s",
                mode=sel.NumberSelectorMode.BOX)),
            vol.Required(CONF_REMOVE, default=False): sel.BooleanSelector(),
        })
        suggested = user_input if user_input is not None else (current.as_dict() if current else {})
        target_name = self._target
        if self._scope != "light" and (cat := self._config.category(self._target)):
            target_name = cat.name
        return self.async_show_form(
            step_id="profile_edit",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "scope": self._scope,
                "mood": self._mood,
                "target": target_name,
                "room": discovery.area_name(self.hass, self._profile_area) if self._profile_area else "-",
            },
        )


def _without_categories(config: IntegrationConfig, keys: set[str]) -> IntegrationConfig:
    """Remove categories and the profile entries that referenced them."""
    def strip(table):
        return {m: {t: s for t, s in targets.items() if t not in keys} for m, targets in table.items()}

    rooms = {
        a: replace(r, mood_overrides=strip(r.mood_overrides)) for a, r in config.rooms.items()
    }
    return replace(
        config,
        categories=tuple(c for c in config.categories if c.key not in keys),
        global_profiles=strip(config.global_profiles),
        rooms=rooms,
    )
