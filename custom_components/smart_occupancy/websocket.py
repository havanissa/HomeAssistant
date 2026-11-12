"""Websocket API used by the mood lighting dashboard cards.

Every command needs an authenticated Home Assistant connection (the frontend's
own websocket). Commands that change configuration or lighting control are
admin only (``require_admin``). Reading the editor model and previews is
allowed for every authenticated user, and never changes a light.

The configuration is owned by the integration (lighting store); the cards
only edit it. Changes are saved without reloading the integration.
``smart_occupancy/lighting/subscribe`` pushes a small ``changed`` event
(throttled) when configuration, diagnostics or registries change, so cards
refetch only then instead of polling.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .adaptive import read_brightness, read_color_temp
from .const import (
    DAYLIGHT_FACTOR_MAX,
    DAYLIGHT_FACTOR_MIN,
    LUX_MAX_THRESHOLD,
    LUX_MIN,
    OUTDOOR_WINDOW_MAX_SECONDS,
    SAMPLE_MAX_AGE_MAX_MINUTES,
    SENSOR_MAX_AGE_MAX_MINUTES,
    SETTLE_SECONDS_MAX,
    STABLE_SECONDS_MAX,
    AutoOn,
    Reliability,
    ReportMode,
    UncertainPolicy,
    ADAPTIVE_BRIGHTNESS_STEP_MAX,
    ADAPTIVE_KELVIN_STEP_MAX,
    ADAPTIVE_MIN_INTERVAL_MAX,
    ALL_MOODS,
    DOMAIN,
    MANUAL_OVERRIDE_MAX_MINUTES,
    RGB_SUFFIX,
    SIGNAL_LIGHTING_UPDATED,
    VERSION,
    TRANSITION_MAX,
    CategoryKind,
    SourceScale,
    UnattributedPolicy,
    UnrepresentableMode,
    VacancyAction,
    Profile,
)
from .controller import LightingController
from .daylight import DaylightConfig, RoomDaylight, parse_lux, round_lux
from .discovery import area_name
from .health import room_health
from .lighting import light_capabilities
from .lighting_config import (
    AdaptiveConfig,
    UnrepresentableHandling,
    parent_cycle,
    set_profile,
    table_get,
    without_categories,
)
from .models import IntegrationConfig, LightCategory, ProfileSetting, guess_kind, unique_category_key
from .occupancy import friendly_name
from .presence import PRESENCE_HANDLERS, presence_model
from .profiles import validate_setting

_THROTTLE_SECONDS = 0.3
_REGISTERED = f"{DOMAIN}_websocket_registered"

ERROR_TEXT = {
    "invalid_brightness": "Brightness must be a whole number from 1 to 100 %",
    "invalid_kelvin": "Colour temperature must be a whole number from 1000 to 10000 K",
    "invalid_rgb": "RGB must be three whole numbers from 0 to 255",
    "invalid_hs": "HS must be a hue of 0-360 and a saturation of 0-100",
    "several_colors": "Choose only one of colour temperature, RGB or HS",
    "invalid_transition": "Transition must be 0-300 seconds",
    "invalid_effect": "Effect must not be empty",
    "off_with_attributes": "Off cannot have brightness, colour or effect",
    "invalid_brightness_offset": "Brightness offset must be a whole number from -99 to +99 percentage points",
    "invalid_kelvin_offset": "Colour temperature offset must be a whole number from -5000 to +5000 K",
    "all_moods_action": "An entry for every mood cannot switch lights on or off; set that per mood",
    "all_moods_field": "An entry for every mood cannot set an effect or ambient lighting; set that per mood",
    "vacant_ambient": "Ambient belongs to the Occupied profile; a Vacant profile cannot set it",
}
# Words that make an entity a likely global source (shown first in the picker).
_SOURCE_HINTS = ("adaptive", "circadian", "bright", "birti", "ljósstyrk", "ljosstyrk", "dim", "colour_temp",
                 "color_temp", "kelvin", "litatemp", "hiti", "temperature")
_NOT_SOURCES = frozenset({"battery", "humidity", "moisture", "power_factor", "signal_strength", "temperature",
                          "illuminance", "volume_flow_rate", "aqi", "pm25", "pm10"})


def _controller(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> LightingController | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.lighting


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


# ------------------------------------------------------------------ models
def _resolved(ctrl: LightingController, mood: str, area_id: str | None, entity_id: str | None,
              category: str | None, levels: tuple[str, ...], config=None,
              profile: Profile = Profile.OCCUPIED) -> dict[str, Any]:
    """Resolved setting with the current global values filled in (what the light gets now)."""
    return ctrl.resolve(mood, area_id, entity_id, category, levels=levels, config=config, profile=profile).as_dict()


def source_candidates(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    """Entities that could be the existing system's brightness / colour temperature outputs.

    Read only. Likely ones first (Adaptive Lighting switches, names that mention brightness,
    colour temperature or "adaptive"); measurement sensors such as battery or humidity are left out.
    """
    out: dict[str, list[dict[str, Any]]] = {"brightness": [], "color_temp": []}
    ent_reg = er.async_get(hass)
    for state in hass.states.async_all(("sensor", "input_number", "number", "switch")):
        if (entry := ent_reg.async_get(state.entity_id)) is not None and entry.platform == DOMAIN:
            continue  # our own sensors (e.g. confidence %) are not adjustment outputs
        attrs = state.attributes
        domain = state.domain
        if domain == "switch" and attrs.get("brightness_pct") is None and attrs.get("color_temp_kelvin") is None:
            continue
        if (attrs.get("device_class") or "") in _NOT_SOURCES:
            continue
        unit = str(attrs.get("unit_of_measurement") or "")
        text = f"{state.entity_id} {attrs.get('friendly_name') or ''}".casefold()
        likely = any(h in text for h in _SOURCE_HINTS) or domain == "switch"
        base = {"entity_id": state.entity_id, "name": attrs.get("friendly_name") or state.entity_id, "likely": likely}
        b = read_brightness(state, state.entity_id)
        if b.ok and (domain == "switch" or unit == "%" or (likely and unit == "")):
            out["brightness"].append({**base, "value": round(b.value or 0), "unit": b.unit})
        k = read_color_temp(state, state.entity_id)
        if k.ok and (domain == "switch" or unit.casefold() in ("k", "kelvin", "mired", "mireds") or likely):
            out["color_temp"].append({**base, "value": round(k.value or 0), "unit": k.unit})
    for items in out.values():
        items.sort(key=lambda x: (not x["likely"], x["name"].casefold()))
    return out


_OUTDOOR_HINTS = ("outdoor", "outside", "uti", "úti", "weather", "veður", "vedur", "garden", "balcony", "altan")


def illuminance_candidates(hass: HomeAssistant, area_id: str | None = None) -> list[dict[str, Any]]:
    """Illuminance sensors (device class illuminance or unit lx). Read only; the room's own first."""
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    out = []
    for state in hass.states.async_all("sensor"):
        attrs = state.attributes
        unit = str(attrs.get("unit_of_measurement") or "").strip().casefold()
        if attrs.get("device_class") != "illuminance" and unit not in ("lx", "lux"):
            continue
        entry = ent_reg.async_get(state.entity_id)
        if entry is not None and entry.platform == DOMAIN:
            continue
        area = entry.area_id if entry else None
        if area is None and entry and entry.device_id and (device := dev_reg.async_get(entry.device_id)):
            area = device.area_id
        value, status, _ = parse_lux(state)
        text = f"{state.entity_id} {attrs.get('friendly_name') or ''}".casefold()
        out.append({
            "entity_id": state.entity_id, "name": attrs.get("friendly_name") or state.entity_id,
            "value": round_lux(value) if status == "ok" else None, "status": status,
            "area_id": area, "area": area_name(hass, area) if area else None,
            "in_room": bool(area_id and area == area_id),
            "outdoor": any(h in text for h in _OUTDOOR_HINTS),
        })
    out.sort(key=lambda c: (not c["in_room"], c["outdoor"], c["name"].casefold()))
    return out


def daylight_global(hass: HomeAssistant, ctrl: LightingController) -> dict[str, Any]:
    cfg = ctrl.lighting.daylight
    state = hass.states.get(cfg.outdoor_entity) if cfg.outdoor_entity else None
    return {**cfg.as_dict(), "outdoor_name": friendly_name(hass, cfg.outdoor_entity) if cfg.outdoor_entity else None,
            "outdoor_exists": state is not None or not cfg.outdoor_entity}


def daylight_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    status = ctrl.daylight.evaluate(area_id, notify=False)
    rt = ctrl.runtime.get(area_id)
    return {
        "config": ctrl.lighting.room(area_id).daylight.as_dict(),
        "status": status.as_dict(),
        "global": daylight_global(hass, ctrl),
        "candidates": illuminance_candidates(hass, area_id),
        "influencing": ctrl.daylight.influencing(area_id),
        "activation": rt.activation if rt else None,
        "last_activation_at": rt.last_activation_at if rt else None,
    }


def adaptive_model(hass: HomeAssistant, ctrl: LightingController, *, candidates: bool) -> dict[str, Any]:
    values = ctrl.global_values()
    model: dict[str, Any] = {
        "config": ctrl.lighting.adaptive.as_dict(),
        "values": values.as_dict(),
        "status": values.status,
        "controllers": [c.as_dict() for c in ctrl.source_controllers()],
    }
    if candidates:
        model["candidates"] = source_candidates(hass)
    return model


def _caps(hass: HomeAssistant, entity_ids: list[str]) -> dict[str, Any]:
    """What the lights of a target can do together (controls are shown only if some light can)."""
    caps = [light_capabilities(hass, e) for e in entity_ids]
    known = [c for c in caps if c.known]
    effects = sorted({e for c in known for e in c.effects})
    mins = [c.min_kelvin for c in known if c.color_temp and c.min_kelvin]
    maxs = [c.max_kelvin for c in known if c.color_temp and c.max_kelvin]
    if not known:  # no light reports yet: offer the common controls
        return {"known": False, "brightness": True, "color_temp": True, "color": False, "effects": [],
                "transition": False, "min_kelvin": None, "max_kelvin": None}
    return {
        "known": True,
        "brightness": any(c.brightness for c in known),
        "color_temp": any(c.color_temp for c in known),
        # Colour only when every light can show it; mixed types use their "+ RGB" part.
        "color": all(c.color for c in known),
        "effects": effects,
        "transition": any(c.transition for c in known),
        "min_kelvin": min(mins) if mins else None,
        "max_kelvin": max(maxs) if maxs else None,
    }


def target_name(config, target: str) -> str:
    base = target.removesuffix(RGB_SUFFIX)
    cat = config.category(base)
    name = cat.name if cat else base
    return f"{name} + RGB" if target.endswith(RGB_SUFFIX) else name


def _target_keys(target: str) -> list[str]:
    """What RGB-tagged lights of a "type + RGB" target get: the type, then the variant."""
    return [target.removesuffix(RGB_SUFFIX), target] if target.endswith(RGB_SUFFIX) else [target]


def _targets_for(ctrl: LightingController, mood: str | None, tables: list) -> list[str]:
    """Every category, plus "type + RGB" variants that are configured or possible."""
    out: list[str] = []
    for cat in ctrl.config.categories:
        out.append(cat.key)
        if cat.kind == CategoryKind.RGB:
            continue
        variant = cat.key + RGB_SUFFIX
        configured = any(variant in t for table in tables for m, t in table.items() if mood and m.casefold() == mood.casefold())
        if configured or ctrl.index.target_lights(variant):
            out.append(variant)
    return out


def _type_entry(hass, ctrl: LightingController, mood: str, target: str, area_id: str | None,
                lights: list[str], profile: Profile = Profile.OCCUPIED) -> dict[str, Any]:
    lighting = ctrl.lighting
    config = ctrl.config
    keys = _target_keys(target)
    base = config.category(target.removesuffix(RGB_SUFFIX))
    if area_id:
        own = table_get(lighting.room(area_id).type_table(profile), mood).get(target)
        inherited = _resolved(ctrl, mood, area_id, None, keys, ("global",), profile=profile)
        effective = _resolved(ctrl, mood, area_id, None, keys, ("global", "room"), profile=profile)
    else:
        own = table_get(lighting.global_table(profile), mood).get(target)
        without = set_profile(lighting, "global", mood, target, None, profile=profile)
        inherited = _resolved(ctrl, mood, None, None, keys, ("global",), without, profile=profile)
        effective = _resolved(ctrl, mood, None, None, keys, ("global",), profile=profile)
    return {
        "target": target,
        "key": target.removesuffix(RGB_SUFFIX),
        "rgb": target.endswith(RGB_SUFFIX) or bool(base and base.kind == CategoryKind.RGB),
        "variant": target.endswith(RGB_SUFFIX),
        "name": target_name(config, target),
        "kind": base.kind.value if base else "source",
        "lights": [{"entity_id": e, "name": friendly_name(hass, e)} for e in lights],
        "caps": _caps(hass, lights) | (
            {"color": True} if target.endswith(RGB_SUFFIX) or (base and base.kind == CategoryKind.RGB) else {}),
        "own": own.as_dict() if own else None,
        "inherited": inherited,
        "effective": effective,
        "configured": own is not None,
    }


def label_suggestions(hass: HomeAssistant, ctrl: LightingController) -> list[dict[str, Any]]:
    """Existing labels that look like a function or the RGB tag but are not used by a category yet."""
    used = {c.label_id for c in ctrl.config.categories if c.label_id}
    out = []
    for label in lr.async_get(hass).async_list_labels():
        if label.label_id in used:
            continue
        kind = guess_kind(label.label_id, label.name, label.label_id)
        if kind != CategoryKind.SOURCE and ctrl.config.category(label.label_id) is None:
            out.append({"label_id": label.label_id, "name": label.name, "kind": kind.value})
    return out


def editor_model(hass: HomeAssistant, ctrl: LightingController, mood: str | None, area_id: str | None,
                 is_admin: bool, profile: Profile = Profile.OCCUPIED) -> dict[str, Any]:
    status = ctrl.mood_status()
    lighting = ctrl.lighting
    config = ctrl.config
    index = ctrl.index
    selected = mood or (ctrl.room_mood(area_id, status)[0] if area_id else status.mood)
    moods = ctrl.discovered_moods()
    if not selected and moods:
        selected = moods[0]["name"]
    label_reg = lr.async_get(hass)
    snapshot = ctrl.coordinator.data

    categories = []
    for cat in config.categories:
        label = label_reg.async_get_label(cat.label_id) if cat.label_id else None
        lights = index.by_category.get(cat.key, [])
        categories.append({
            "key": cat.key, "name": cat.name, "kind": cat.kind.value, "label_id": cat.label_id,
            "label_name": label.name if label else None, "label_missing": bool(cat.label_id and not label),
            "lights": [{"entity_id": e, "name": friendly_name(hass, e)} for e in lights],
            "rgb_lights": len(index.target_lights(cat.key + RGB_SUFFIX)) if cat.kind != CategoryKind.RGB else len(lights),
        })
    types = [
        _type_entry(hass, ctrl, selected, t, None, index.target_lights(t), profile)
        for t in _targets_for(ctrl, selected, [lighting.global_table(profile)])
    ] if selected else []

    problems = []
    if snapshot is not None:
        problems = [
            snapshot.problem_dict(p) for p in snapshot.problems
            if p.code.startswith(("profile_", "mood_", "global_", "fallback_", "room_mood", "exclude_label",
                                  "lighting_", "automation_conflict", "group_", "light_", "excluded_",
                                  "manual_switch", "category_", "label_"))
            and (area_id is None or p.area_id in (None, area_id))
        ]

    model: dict[str, Any] = {
        "version": VERSION,
        "revision": ctrl.store.revision,
        "can_edit": is_admin,
        "mood": status.as_dict(),
        "moods": moods,
        "selected_mood": selected,
        # Which profile the light-type and single-light entries below belong to (0.4.0).
        "profile": profile.value,
        "vacant_defaults": lighting.vacant.as_dict(),
        "categories": categories,
        "types": types,
        "global": {t["target"]: t for t in types},
        "label_suggestions": label_suggestions(hass, ctrl),
        "all_moods": ALL_MOODS,
        "manual_mode": status.manual,
        "adaptive": adaptive_model(hass, ctrl, candidates=is_admin and area_id is None),
        "settings": {
            "manual_option": lighting.manual_option,
            "fallback_mood": lighting.fallback_mood,
            "exclude_label": lighting.exclude_label,
            "manual_override_minutes": lighting.manual_override_minutes,
            "unattributed": lighting.unattributed.value,
            "unrepresentable": {m: h.as_dict() for m, h in lighting.unrepresentable.items()},
        },
        "daylight": daylight_global(hass, ctrl) | (
            {"candidates": illuminance_candidates(hass)} if is_admin and area_id is None else {}),
        "labels": sorted(
            ({"label_id": label.label_id, "name": label.name} for label in label_reg.async_list_labels()),
            key=lambda x: x["name"].casefold(),
        ),
        "rooms": [
            {"area_id": a, "name": area_name(hass, a) or a, "active": ctrl.room_active(a),
             "control_mode": ctrl.room_diagnostics(a)["control_mode"]}
            for a in sorted(config.enabled_rooms, key=lambda a: (area_name(hass, a) or a).casefold())
        ],
        "problems": problems,
    }
    if area_id:
        model["room"] = room_model(hass, ctrl, area_id, selected, profile)
    return model


def _group_issues(ctrl: LightingController, area_id: str) -> list[dict[str, Any]]:
    """Groups that need a decision: manage as group, individually, or ignore the warning."""
    managed = ctrl.managed_room(area_id)
    dismissed = set(ctrl.lighting.room(area_id).dismissed)
    out = []
    for group, members in managed.members.items():
        both = sorted(m for m in members if m in managed.targets and m not in managed.excluded)
        if group not in managed.excluded and both:
            out.append({"group": group, "members": both, "members_known": True,
                        "dismissed": group in dismissed})
    index = ctrl.index
    for group in managed.opaque_groups:
        category = index.category_of(group)
        same = [e for e in managed.controlled if e != group and category and index.category_of(e) == category]
        if group not in managed.excluded and same:
            out.append({"group": group, "members": same, "members_known": False,
                        "dismissed": f"opaque:{group}" in dismissed})
    return out


def _preliminary_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    from .preliminary_websocket import preliminary_model

    return preliminary_model(hass, ctrl, area_id)


def _vacant_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    from .profile_websocket import vacant_model

    return vacant_model(hass, ctrl, area_id)


def switches_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    from .switch_websocket import switches_model as model

    return model(hass, ctrl, area_id)


def room_model(hass: HomeAssistant, ctrl: LightingController, area_id: str, mood: str | None,
               profile: Profile = Profile.OCCUPIED) -> dict[str, Any]:
    lighting = ctrl.lighting
    room_lighting = lighting.room(area_id)
    managed = ctrl.managed_room(area_id)
    rt = ctrl.runtime.get(area_id)
    result = ctrl.coordinator.occupancy.result(area_id)
    index = ctrl.index
    in_room = set(managed.controlled)
    types: list[dict[str, Any]] = []
    if mood:
        for target in _targets_for(ctrl, mood, [lighting.global_table(profile), room_lighting.type_table(profile)]):
            lights = [e for e in index.target_lights(target) if e in in_room]
            entry = _type_entry(hass, ctrl, mood, target, area_id, lights, profile)
            entry["follows_global"] = entry["own"] is None
            types.append(entry)
    conflicts = ctrl.conflicts(area_id)
    acknowledged = set(room_lighting.acknowledged)
    cat_names = {c.key: c.name for c in ctrl.config.categories}
    now_mood, _ = ctrl.room_mood(area_id)
    # What each light gets now: the Vacant profile while it is in charge of the vacant room, else the Occupied one.
    now_profile = Profile.VACANT if ctrl.vacant_profile_room(area_id) else Profile.OCCUPIED
    lights: list[dict[str, Any]] = []
    for entity_id in managed.targets:
        state = hass.states.get(entity_id)
        category = index.category_of(entity_id)
        targets = index.profile_targets(entity_id)
        own = table_get(room_lighting.light_table(profile), mood).get(entity_id) if mood else None
        lights.append({
            "entity_id": entity_id,
            "name": friendly_name(hass, entity_id),
            "category": category,
            "category_name": cat_names.get(category) if category else None,
            "labels": [cat_names[k] for k in index.categories_of.get(entity_id, []) if k in cat_names],
            "rgb": index.rgb_eligible(entity_id),
            "state": state.state if state else None,
            "capabilities": light_capabilities(hass, entity_id).as_dict(),
            "is_group": entity_id in managed.members or entity_id in managed.opaque_groups,
            "members": sorted(managed.members.get(entity_id, ())),
            "opaque_group": entity_id in managed.opaque_groups,
            "covered_by": managed.covered_by.get(entity_id),
            "excluded": managed.excluded.get(entity_id),
            "explicit": entity_id in managed.explicit,
            "discovered": entity_id in managed.discovered,
            "override": rt.overrides.get(entity_id) if rt else None,
            "conflicts": [{**c.as_dict(), "acknowledged": c.automation in acknowledged}
                          for c in conflicts.get(entity_id, [])],
            "own": own.as_dict() if own else None,
            "inherited": _resolved(ctrl, mood, area_id, entity_id, targets, ("global", "room"), profile=profile)
            if mood else None,
            "effective": _resolved(ctrl, mood, area_id, entity_id, targets, ("global", "room", "light"),
                                   profile=profile) if mood else None,
            # What the light gets right now (current room mood, current global values).
            "now": _resolved(ctrl, now_mood, area_id, entity_id, targets, ("global", "room", "light"),
                             profile=now_profile) if now_mood else None,
        })
    # Excluded lights stay listed, so they can be included again.
    for entity_id in room_lighting.excluded:
        if entity_id not in managed.targets:
            lights.append({"entity_id": entity_id, "name": friendly_name(hass, entity_id),
                           "excluded": "excluded in this room", "missing": True})
    snapshot = ctrl.coordinator.data
    return {
        "area_id": area_id,
        "name": area_name(hass, area_id) or area_id,
        "now_mood": now_mood,
        "enabled": area_id in ctrl.config.enabled_rooms,
        "diagnostics": ctrl.room_diagnostics(area_id),
        "settings": room_lighting.as_dict() | {"mood_overrides": None, "light_overrides": None,
                                               "vacant_overrides": None, "vacant_light_overrides": None},
        "profile": profile.value,
        "now_profile": now_profile.value,
        # Vacant profile (0.4.0): settings (room and global), its daylight decision, which moods have entries.
        "vacant": _vacant_model(hass, ctrl, area_id) if area_id in ctrl.config.enabled_rooms else None,
        "types": types,
        "categories": {t["target"]: t for t in types},
        "lights": lights,
        "group_issues": _group_issues(ctrl, area_id),
        "daylight": daylight_model(hass, ctrl, area_id) if area_id in ctrl.config.enabled_rooms else None,
        "switches": switches_model(hass, ctrl, area_id),
        # Decision log (0.3.7): the latest decision and the last ones, newest first (backend records only).
        "decisions": ctrl.decisions.model(area_id),
        # Presence tab (0.3.8): occupancy inputs, their live states, presence settings (global and room).
        "presence": presence_model(hass, ctrl.coordinator, area_id) if area_id in ctrl.config.enabled_rooms else None,
        # When the room becomes vacant (0.3.8), and what the last vacancy did.
        "vacancy": {**room_lighting.vacancy.as_dict(), "last": rt.last_vacancy if rt else None},
        # Possible occupancy lighting (0.3.9): settings (room and global), lights, signals, the running stage.
        "preliminary": _preliminary_model(hass, ctrl, area_id) if area_id in ctrl.config.enabled_rooms else None,
        "occupancy": {
            "state": result.state.value if result else None,
            "occupied": result.occupied if result else None,
            "confidence": result.confidence.score if result else None,
            "reason": result.reason if result else None,
        },
        "health": room_health(snapshot, area_id, result) if snapshot else None,
        "problems": [snapshot.problem_dict(p) for p in snapshot.problems_where(area_id=area_id, include_info=True)]
        if snapshot else [],
    }


# ---------------------------------------------------------------- commands
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/editor",
    vol.Optional("mood"): vol.Any(str, None),
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Optional("profile"): vol.Any(None, vol.In([p.value for p in Profile])),
})
@callback
def ws_editor(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Everything an editor card needs, for one mood (and optionally one room)."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg.get("area_id")
    if area_id and area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return
    profile = Profile(msg.get("profile") or Profile.OCCUPIED)
    connection.send_result(msg["id"], editor_model(hass, ctrl, msg.get("mood"), area_id, connection.user.is_admin,
                                                   profile))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/lighting/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Push a throttled ``changed`` event when anything shown by the cards changes."""
    timer = None

    @callback
    def send(_now: datetime) -> None:
        nonlocal timer
        timer = None
        connection.send_message(websocket_api.event_message(msg["id"], {"changed": True}))

    @callback
    def schedule() -> None:
        nonlocal timer
        if timer is None:
            timer = async_call_later(hass, _THROTTLE_SECONDS, send)

    unsub = async_dispatcher_connect(hass, SIGNAL_LIGHTING_UPDATED, schedule)

    @callback
    def cancel() -> None:
        unsub()
        if timer is not None:
            timer()

    connection.subscriptions[msg["id"]] = cancel
    connection.send_result(msg["id"])


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/preview",
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Optional("mood"): vol.Any(str, None),
    vol.Optional("profile"): vol.Any(None, vol.In([p.value for p in Profile])),
})
@callback
def ws_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Plan for one room or every enabled room. Never sends a light command.

    Without ``profile``: what lighting would do now (the Vacant profile while it is in charge of a vacant room).
    With ``profile`` (0.4.0): that profile as if the room were occupied (confirmed) or vacant now.
    """
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    areas = [msg["area_id"]] if msg.get("area_id") else sorted(ctrl.config.enabled_rooms)
    plans = []
    for area_id in areas:
        if area_id not in ctrl.config.enabled_rooms:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
            return
        if msg.get("profile"):
            plan = ctrl.plan(area_id, "preview", msg.get("mood"), assume=Profile(msg["profile"]))
        else:
            plan = ctrl.profile_plan(area_id, "preview", msg.get("mood"))
        plans.append({**plan.as_dict(), "room": area_name(hass, area_id) or area_id,
                      "profile": msg.get("profile") or ("vacant" if plan.vacancy else "occupied")})
    connection.send_result(msg["id"], {"plans": plans, "generated_at": _iso(dt_util.utcnow())})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/profile/set",
    vol.Required("scope"): vol.In(["global", "room", "light"]),
    vol.Required("mood"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Required("target"): vol.All(str, vol.Length(min=1, max=255)),
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Required("setting"): vol.Any(dict, None),
    vol.Optional("profile", default=Profile.OCCUPIED.value): vol.In([p.value for p in Profile]),
})
@websocket_api.require_admin
@callback
def ws_profile_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Save (or with ``setting: null`` remove) one profile entry. Never sends a light command (0.4.0)."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    scope, mood, target, area_id = msg["scope"], msg["mood"].strip(), msg["target"], msg.get("area_id")
    if scope != "global" and area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A configured room is required")
        return
    if scope in ("global", "room"):
        cat = ctrl.config.category(target.removesuffix(RGB_SUFFIX))
        if cat is None or (target.endswith(RGB_SUFFIX) and cat.kind == CategoryKind.RGB):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"Unknown light type {target}")
            return
    if scope == "light":
        try:
            cv.entity_domain("light")(target)
        except vol.Invalid:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{target} is not a light")
            return
    setting = None
    if msg["setting"] is not None:
        setting = ProfileSetting.from_dict(msg["setting"])
        if errors := validate_setting(setting, mood, Profile(msg["profile"])):
            connection.send_error(
                msg["id"], "invalid_profile",
                "; ".join(ERROR_TEXT.get(e, e) for e in errors.values()),
            )
            return
    ctrl.store.async_set(set_profile(ctrl.lighting, scope, mood, target, setting, area_id,
                                     profile=Profile(msg["profile"])))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/mood/set",
    vol.Required("mood"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Required("parent"): vol.Any(vol.All(str, vol.Length(min=1, max=100)), None),
})
@websocket_api.require_admin
@callback
def ws_mood_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Set the parent mood a mood inherits from. Loops are refused."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    mood, parent = msg["mood"], msg["parent"]
    if cycle := parent_cycle(ctrl.lighting, mood, parent):
        connection.send_error(msg["id"], "inheritance_loop", "This creates a loop: " + " → ".join(cycle))
        return
    parents = {m: p for m, p in ctrl.lighting.parents.items() if m.casefold() != mood.casefold()}
    if parent:
        parents[mood] = parent
    ctrl.store.async_set(replace(ctrl.lighting, parents=parents))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/settings/set",
    vol.Optional("fallback_mood"): vol.Any(str, None),
    vol.Optional("exclude_label"): vol.Any(str, None),
    vol.Optional("manual_override_minutes"): vol.All(vol.Coerce(int), vol.Range(min=0, max=MANUAL_OVERRIDE_MAX_MINUTES)),
    vol.Optional("unattributed"): vol.In([p.value for p in UnattributedPolicy]),
    vol.Optional("manual_option"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Optional("unrepresentable"): {str: vol.Any(None, {
        vol.Required("mode"): vol.In([m.value for m in UnrepresentableMode]),
        vol.Optional("mood"): vol.Any(str, None),
    })},
})
@websocket_api.require_admin
@callback
def ws_settings_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Global mood lighting settings."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    lighting = ctrl.lighting
    changes: dict[str, Any] = {}
    if "fallback_mood" in msg:
        fallback = (msg["fallback_mood"] or "").strip() or None
        changes["fallback_mood"] = fallback
    if "exclude_label" in msg:
        label = msg["exclude_label"] or None
        if label and lr.async_get(hass).async_get_label(label) is None:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"Label {label} does not exist")
            return
        changes["exclude_label"] = label
    if "manual_override_minutes" in msg:
        changes["manual_override_minutes"] = msg["manual_override_minutes"]
    if "unattributed" in msg:
        changes["unattributed"] = UnattributedPolicy(msg["unattributed"])
    if "manual_option" in msg:
        changes["manual_option"] = msg["manual_option"].strip() or lighting.manual_option
    if "unrepresentable" in msg:
        handling = dict(lighting.unrepresentable)
        for mood, value in msg["unrepresentable"].items():
            handling = {m: h for m, h in handling.items() if m.casefold() != mood.casefold()}
            if value is not None:
                h = UnrepresentableHandling(UnrepresentableMode(value["mode"]), value.get("mood") or None)
                if h.mode == UnrepresentableMode.USE_MOOD and not h.mood:
                    connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                          f"Choose the mood to use for {mood}")
                    return
                handling[mood] = h
        changes["unrepresentable"] = handling
    ctrl.store.async_set(replace(lighting, **changes))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


_SOURCE_ENTITY = vol.Any(None, vol.All(str, vol.Length(max=255)))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/adaptive/set",
    vol.Optional("brightness_entity"): _SOURCE_ENTITY,
    vol.Optional("brightness_attribute"): vol.Any(None, vol.All(str, vol.Length(max=64))),
    vol.Optional("brightness_scale"): vol.In([SourceScale.AUTO, SourceScale.PERCENT, SourceScale.BYTE]),
    vol.Optional("color_temp_entity"): _SOURCE_ENTITY,
    vol.Optional("color_temp_attribute"): vol.Any(None, vol.All(str, vol.Length(max=64))),
    vol.Optional("color_temp_scale"): vol.In([SourceScale.AUTO, SourceScale.KELVIN, SourceScale.MIRED]),
    vol.Optional("min_interval"): vol.All(vol.Coerce(int), vol.Range(min=0, max=ADAPTIVE_MIN_INTERVAL_MAX)),
    vol.Optional("brightness_step"): vol.All(vol.Coerce(int), vol.Range(min=1, max=ADAPTIVE_BRIGHTNESS_STEP_MAX)),
    vol.Optional("kelvin_step"): vol.All(vol.Coerce(int), vol.Range(min=10, max=ADAPTIVE_KELVIN_STEP_MAX)),
    vol.Optional("transition"): vol.All(vol.Coerce(float), vol.Range(min=0, max=TRANSITION_MAX)),
})
@websocket_api.require_admin
@callback
def ws_adaptive_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Bind (or unbind) the existing global brightness / colour temperature outputs. Read only on them."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    current = ctrl.lighting.adaptive.as_dict()
    for key in ("brightness_entity", "color_temp_entity"):
        if key not in msg:
            continue
        value = (msg[key] or "").strip() or None
        if value is not None:
            try:
                cv.entity_id(value)
            except vol.Invalid:
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{value} is not an entity id")
                return
            if hass.states.get(value) is None and er.async_get(hass).async_get(value) is None:
                connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{value} does not exist")
                return
        current[key] = value
    for key in ("brightness_attribute", "color_temp_attribute"):
        if key in msg:
            current[key] = (msg[key] or "").strip() or None
    for key in ("brightness_scale", "color_temp_scale", "min_interval", "brightness_step", "kelvin_step",
                "transition"):
        if key in msg:
            current[key] = msg[key]
    ctrl.store.async_set(replace(ctrl.lighting, adaptive=AdaptiveConfig.from_dict(current)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision,
                                       "adaptive": adaptive_model(hass, ctrl, candidates=False)})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/set",
    vol.Required("area_id"): str,
    vol.Optional("mood_override"): vol.Any(str, None),
    vol.Optional("auto_discover"): bool,
    vol.Optional("excluded"): [cv.entity_id],
    vol.Optional("manual_override_minutes"): vol.Any(
        None, vol.All(vol.Coerce(int), vol.Range(min=0, max=MANUAL_OVERRIDE_MAX_MINUTES))
    ),
    vol.Optional("manual_switches"): [cv.entity_id],
    # 0.3.8: when the room becomes vacant.
    vol.Optional("vacancy_action"): vol.In([a.value for a in VacancyAction]),
    vol.Optional("vacancy_mood"): vol.Any(None, vol.All(str, vol.Length(max=100))),
})
@websocket_api.require_admin
@callback
def ws_room_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Room settings: room mood override, discovery, exclusions, manual-override handling."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg["area_id"]
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return
    changes: dict[str, Any] = {}
    if "mood_override" in msg:
        changes["mood_override"] = (msg["mood_override"] or "").strip() or None
    for key in ("auto_discover", "manual_override_minutes"):
        if key in msg:
            changes[key] = msg[key]
    for key in ("excluded", "manual_switches"):
        if key in msg:
            changes[key] = tuple(dict.fromkeys(msg[key]))
    if "vacancy_action" in msg or "vacancy_mood" in msg:
        current = ctrl.lighting.room(area_id).vacancy
        action = VacancyAction(msg.get("vacancy_action", current.action))
        mood = (msg["vacancy_mood"] or "").strip() or None if "vacancy_mood" in msg else current.mood
        if mood and (ctrl.lighting.is_manual(mood) or mood == ALL_MOODS):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{mood} is not a mood")
            return
        # (0.4.0: Vacant mood applies the room's Vacant profile of the current mood; no mood needs to be chosen.)
        changes["vacancy"] = replace(current, action=action, mood=mood)
    ctrl.store.async_set(ctrl.with_room(area_id, **changes))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


def _exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None


_INDOOR_SENSOR = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("max_age"): vol.All(vol.Coerce(int), vol.Range(min=0, max=SENSOR_MAX_AGE_MAX_MINUTES)),
    vol.Optional("report_mode"): vol.In([m.value for m in ReportMode]),
    vol.Optional("reliability"): vol.In([r.value for r in Reliability]),
})
_LUX = vol.All(vol.Coerce(int), vol.Range(min=LUX_MIN, max=LUX_MAX_THRESHOLD))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/daylight",
    vol.Required("area_id"): str,
    vol.Optional("auto_on"): vol.In([p.value for p in AutoOn]),
    vol.Optional("on_first_motion"): bool,
    vol.Optional("uncertain"): vol.In([p.value for p in UncertainPolicy]),
    vol.Optional("sensors"): [_INDOOR_SENSOR],
    vol.Optional("on_below"): _LUX,
    vol.Optional("off_above"): vol.All(vol.Coerce(int), vol.Range(min=LUX_MIN, max=LUX_MAX_THRESHOLD * 2)),
    vol.Optional("stable_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=STABLE_SECONDS_MAX)),
    vol.Optional("settle_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=SETTLE_SECONDS_MAX)),
    vol.Optional("sample_max_age"): vol.All(vol.Coerce(int), vol.Range(min=1, max=SAMPLE_MAX_AGE_MAX_MINUTES)),
    vol.Optional("influencing"): [cv.entity_id],
    vol.Optional("area_lights"): bool,
    vol.Optional("factor"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=DAYLIGHT_FACTOR_MIN,
                                                                               max=DAYLIGHT_FACTOR_MAX))),
    vol.Optional("reset_calibration"): bool,
})
@websocket_api.require_admin
@callback
def ws_room_daylight(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Automatic switch-on settings of one room (daylight sensors, thresholds, policy). Read only on sensors."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg["area_id"]
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not configured")
        return
    current = ctrl.lighting.room(area_id).daylight.as_dict()
    if "sensors" in msg:
        for item in msg["sensors"]:
            if not _exists(hass, item["entity_id"]):
                connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{item['entity_id']} does not exist")
                return
            if item["entity_id"].split(".", 1)[0] != "sensor":
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                      f"{item['entity_id']} is not a sensor")
                return
        previous = {s["entity_id"]: s for s in current["sensors"]}
        current["sensors"] = [{**previous.get(i["entity_id"], {}), **i} for i in msg["sensors"]]
    if "influencing" in msg:
        for entity_id in msg["influencing"]:
            if entity_id.split(".", 1)[0] not in ("light", "switch"):
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                      f"{entity_id} is not a light or switch")
                return
        current["influencing"] = list(dict.fromkeys(msg["influencing"]))
    for key in ("auto_on", "on_first_motion", "uncertain", "stable_seconds", "settle_seconds", "sample_max_age",
                "area_lights", "factor"):
        if key in msg:
            current[key] = msg[key]
    if "on_below" in msg:
        current["on_below"] = msg["on_below"]
        if "off_above" not in msg and current["off_above"] <= msg["on_below"]:
            current["off_above"] = msg["on_below"] * 2
    if "off_above" in msg:
        current["off_above"] = msg["off_above"]
    if current["off_above"] <= current["on_below"]:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              "The light-not-needed level must be above the turn-on level")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, daylight=RoomDaylight.from_dict(current)))
    if msg.get("reset_calibration"):
        ctrl.daylight.reset_calibration(area_id)
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/daylight/set",
    vol.Optional("outdoor_entity"): vol.Any(None, vol.All(str, vol.Length(max=255))),
    vol.Optional("outdoor_dark_lux"): vol.All(vol.Coerce(int), vol.Range(min=0, max=LUX_MAX_THRESHOLD)),
    vol.Optional("outdoor_window"): vol.All(vol.Coerce(int), vol.Range(min=0, max=OUTDOOR_WINDOW_MAX_SECONDS)),
})
@websocket_api.require_admin
@callback
def ws_daylight_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Global daylight settings: the optional outdoor light sensor (read only) and its smoothing."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    current = ctrl.lighting.daylight.as_dict()
    if "outdoor_entity" in msg:
        value = (msg["outdoor_entity"] or "").strip() or None
        if value is not None:
            try:
                cv.entity_id(value)
            except vol.Invalid:
                connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{value} is not an entity id")
                return
            if not _exists(hass, value):
                connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{value} does not exist")
                return
        current["outdoor_entity"] = value
    for key in ("outdoor_dark_lux", "outdoor_window"):
        if key in msg:
            current[key] = msg[key]
    ctrl.store.async_set(replace(ctrl.lighting, daylight=DaylightConfig.from_dict(current)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/activation",
    vol.Required("area_id"): str,
})
@callback
def ws_activation(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """What enabling mood lighting would control, and which automations compete."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    if msg["area_id"] not in ctrl.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Room is not enabled")
        return
    connection.send_result(msg["id"], ctrl.activation_review(msg["area_id"]))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/activate",
    vol.Required("area_id"): str,
    vol.Required("confirm"): True,
    vol.Optional("acknowledge", default=[]): [cv.entity_id],
})
@websocket_api.require_admin
@callback
def ws_activate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Explicitly enable mood lighting control for one room."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg["area_id"]
    if area_id not in ctrl.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Room is not enabled")
        return
    room = ctrl.lighting.room(area_id)
    acknowledged = tuple(dict.fromkeys([*room.acknowledged, *msg["acknowledge"]]))
    ctrl.store.async_set(ctrl.with_room(
        area_id, control_enabled=True, activated_at=_iso(dt_util.utcnow()),
        activated_by=connection.user.id, acknowledged=acknowledged,
    ))
    connection.send_result(msg["id"], ctrl.activation_review(area_id))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/deactivate",
    vol.Required("area_id"): str,
})
@websocket_api.require_admin
@callback
def ws_deactivate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Return a room to Observe mode. Lights are left as they are."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id = msg["area_id"]
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Room is not configured")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, control_enabled=False))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/acknowledge",
    vol.Required("area_id"): str,
    vol.Required("automation"): cv.entity_id,
    vol.Required("acknowledged"): bool,
})
@websocket_api.require_admin
@callback
def ws_acknowledge(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Accept (or withdraw) an automation as a co-controller of a room's lights."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id, automation = msg["area_id"], msg["automation"]
    if area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Room is not configured")
        return
    current = [a for a in ctrl.lighting.room(area_id).acknowledged if a != automation]
    if msg["acknowledged"]:
        current.append(automation)
    ctrl.store.async_set(ctrl.with_room(area_id, acknowledged=tuple(current)))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/override/clear",
    vol.Required("area_id"): str,
    vol.Optional("entity_id"): vol.Any(cv.entity_id, None),
})
@websocket_api.require_admin
@callback
def ws_override_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Forget manual overrides, so the next trigger may change those lights again."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    cleared = ctrl.async_clear_override(msg["area_id"], msg.get("entity_id"))
    connection.send_result(msg["id"], {"cleared": cleared})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/room/group_choice",
    vol.Required("area_id"): str,
    vol.Required("group"): cv.entity_id,
    vol.Required("choice"): vol.In(["group", "individual", "dismiss", "restore"]),
})
@websocket_api.require_admin
@callback
def ws_group_choice(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Resolve a group/member overlap with one choice.

    group: the group is controlled; its member lights in the room are excluded
    (for groups that hide their members: the lights flagged as possible members).
    individual: the group is excluded; its lights are controlled one by one.
    dismiss: keep things as they are and stop warning. restore: undo all three.
    """
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    area_id, group, choice = msg["area_id"], msg["group"], msg["choice"]
    if area_id not in ctrl.config.enabled_rooms:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Room is not enabled")
        return
    issue = next((i for i in _group_issues(ctrl, area_id) if i["group"] == group), None)
    room = ctrl.lighting.room(area_id)
    excluded = list(room.excluded)
    dismissed = [d for d in room.dismissed if d not in (group, f"opaque:{group}")]
    if choice == "restore":
        excluded = [e for e in excluded if e != group]
    elif issue is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"No open question about {group}")
        return
    elif choice == "group":
        excluded += [m for m in issue["members"] if m not in excluded]
        dismissed.append(group if issue["members_known"] else f"opaque:{group}")
    elif choice == "individual":
        excluded = [*[e for e in excluded if e != group], group]
        excluded = [e for e in excluded if e not in issue["members"]]
    else:
        dismissed.append(group if issue["members_known"] else f"opaque:{group}")
    ctrl.store.async_set(ctrl.with_room(area_id, excluded=tuple(dict.fromkeys(excluded)),
                                        dismissed=tuple(dict.fromkeys(dismissed))))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/category/set",
    vol.Optional("key"): vol.Any(str, None),
    vol.Required("name"): vol.All(str, vol.Length(min=1, max=60)),
    vol.Optional("label_id"): vol.Any(str, None),
    vol.Required("kind"): vol.In([k.value for k in CategoryKind]),
})
@websocket_api.require_admin
@callback
def ws_category_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Add or change a light type / function / RGB tag. Saved in the options (the integration reloads)."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return
    entry = entries[0]
    config = IntegrationConfig.from_dict(dict(entry.options))
    name = msg["name"].strip()
    label = msg.get("label_id") or None
    if label and lr.async_get(hass).async_get_label(label) is None:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"Label {label} does not exist")
        return
    key = msg.get("key")
    others = [c for c in config.categories if c.key != key]
    if name.casefold() in {c.name.casefold() for c in others}:
        connection.send_error(msg["id"], "category_exists", f"A light type named {name} already exists")
        return
    if key and config.category(key) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Unknown light type {key}")
        return
    if not key:
        key = unique_category_key(name, {c.key for c in config.categories})
        categories = (*config.categories, LightCategory(key, name, label, CategoryKind(msg["kind"])))
    else:
        categories = tuple(
            replace(c, name=name, label_id=label, kind=CategoryKind(msg["kind"])) if c.key == key else c
            for c in config.categories
        )
    hass.config_entries.async_update_entry(entry, options=replace(config, categories=categories).as_dict())
    connection.send_result(msg["id"], {"key": key})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/category/remove",
    vol.Required("key"): str,
})
@websocket_api.require_admin
@callback
def ws_category_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Remove a light type and its profile entries."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    config = IntegrationConfig.from_dict(dict(entry.options))
    key = msg["key"]
    if config.category(key) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Unknown light type {key}")
        return
    ctrl.store.async_set(without_categories(ctrl.lighting, {key, key + RGB_SUFFIX}))
    hass.config_entries.async_update_entry(entry, options=replace(
        config, categories=tuple(c for c in config.categories if c.key != key)).as_dict())
    connection.send_result(msg["id"], {"removed": key})


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    from .preliminary_websocket import PRELIMINARY_HANDLERS
    from .profile_websocket import PROFILE_HANDLERS
    from .switch_websocket import SWITCH_HANDLERS

    for handler in (*SWITCH_HANDLERS, *PRESENCE_HANDLERS, *PRELIMINARY_HANDLERS, *PROFILE_HANDLERS):
        websocket_api.async_register_command(hass, handler)
    for handler in (
        ws_editor, ws_subscribe, ws_preview, ws_profile_set, ws_mood_set, ws_settings_set, ws_adaptive_set, ws_room_set,
        ws_activation, ws_activate, ws_deactivate, ws_acknowledge, ws_override_clear, ws_group_choice,
        ws_category_set, ws_category_remove, ws_room_daylight, ws_daylight_set,
    ):
        websocket_api.async_register_command(hass, handler)
