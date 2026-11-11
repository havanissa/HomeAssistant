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
from homeassistant.helpers import config_validation as cv, label_registry as lr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MANUAL_OVERRIDE_MAX_MINUTES,
    SIGNAL_LIGHTING_UPDATED,
    VERSION,
    UnattributedPolicy,
    UnrepresentableMode,
)
from .controller import LightingController
from .discovery import area_name
from .health import room_health
from .lighting import light_capabilities
from .lighting_config import UnrepresentableHandling, parent_cycle, set_profile, table_get
from .models import ProfileSetting
from .occupancy import friendly_name
from .profiles import resolve, validate_setting

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
}


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
              category: str | None, levels: tuple[str, ...], config=None) -> dict[str, Any]:
    return resolve(config or ctrl.lighting, mood, area_id, entity_id, category, levels=levels).as_dict()


def editor_model(hass: HomeAssistant, ctrl: LightingController, mood: str | None, area_id: str | None,
                 is_admin: bool) -> dict[str, Any]:
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
    global_entries: dict[str, Any] = {}
    for cat in config.categories:
        label = label_reg.async_get_label(cat.label_id) if cat.label_id else None
        lights = index.by_category.get(cat.key, [])
        categories.append({
            "key": cat.key, "name": cat.name, "label_id": cat.label_id,
            "label_name": label.name if label else None, "label_missing": bool(cat.label_id and not label),
            "lights": [{"entity_id": e, "name": friendly_name(hass, e)} for e in lights],
        })
        if selected:
            own = table_get(lighting.global_profiles, selected).get(cat.key)
            without = set_profile(lighting, "global", selected, cat.key, None)
            global_entries[cat.key] = {
                "own": own.as_dict() if own else None,
                "inherited": _resolved(ctrl, selected, None, None, cat.key, ("global",), without),
                "effective": _resolved(ctrl, selected, None, None, cat.key, ("global",)),
            }

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
        "categories": categories,
        "global": global_entries,
        "settings": {
            "fallback_mood": lighting.fallback_mood,
            "exclude_label": lighting.exclude_label,
            "manual_override_minutes": lighting.manual_override_minutes,
            "unattributed": lighting.unattributed.value,
            "unrepresentable": {m: h.as_dict() for m, h in lighting.unrepresentable.items()},
        },
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
        model["room"] = room_model(hass, ctrl, area_id, selected)
    return model


def room_model(hass: HomeAssistant, ctrl: LightingController, area_id: str, mood: str | None) -> dict[str, Any]:
    lighting = ctrl.lighting
    room_lighting = lighting.room(area_id)
    managed = ctrl.managed_room(area_id)
    rt = ctrl.runtime.get(area_id)
    result = ctrl.coordinator.occupancy.result(area_id)
    categories: dict[str, Any] = {}
    lights: list[dict[str, Any]] = []
    if mood:
        for cat in ctrl.config.categories:
            own = table_get(room_lighting.mood_overrides, mood).get(cat.key)
            categories[cat.key] = {
                "own": own.as_dict() if own else None,
                "inherited": _resolved(ctrl, mood, area_id, None, cat.key, ("global",)),
                "effective": _resolved(ctrl, mood, area_id, None, cat.key, ("global", "room")),
            }
    conflicts = ctrl.conflicts(area_id)
    acknowledged = set(room_lighting.acknowledged)
    for entity_id in managed.targets:
        state = hass.states.get(entity_id)
        category = ctrl.index.category_of(entity_id)
        cat = ctrl.config.category(category) if category else None
        own = table_get(room_lighting.light_overrides, mood).get(entity_id) if mood else None
        lights.append({
            "entity_id": entity_id,
            "name": friendly_name(hass, entity_id),
            "category": category,
            "category_name": cat.name if cat else None,
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
            "inherited": _resolved(ctrl, mood, area_id, entity_id, category, ("global", "room")) if mood else None,
            "effective": _resolved(ctrl, mood, area_id, entity_id, category, ("global", "room", "light"))
            if mood else None,
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
        "enabled": area_id in ctrl.config.enabled_rooms,
        "diagnostics": ctrl.room_diagnostics(area_id),
        "settings": room_lighting.as_dict() | {"mood_overrides": None, "light_overrides": None},
        "categories": categories,
        "lights": lights,
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
    connection.send_result(msg["id"], editor_model(hass, ctrl, msg.get("mood"), area_id, connection.user.is_admin))


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
})
@callback
def ws_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Plan for one room or every enabled room. Never sends a light command."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    areas = [msg["area_id"]] if msg.get("area_id") else sorted(ctrl.config.enabled_rooms)
    plans = []
    for area_id in areas:
        if area_id not in ctrl.config.enabled_rooms:
            connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Room {area_id} is not enabled")
            return
        plan = ctrl.plan(area_id, "preview", msg.get("mood"))
        plans.append({**plan.as_dict(), "room": area_name(hass, area_id) or area_id})
    connection.send_result(msg["id"], {"plans": plans, "generated_at": _iso(dt_util.utcnow())})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/profile/set",
    vol.Required("scope"): vol.In(["global", "room", "light"]),
    vol.Required("mood"): vol.All(str, vol.Length(min=1, max=100)),
    vol.Required("target"): vol.All(str, vol.Length(min=1, max=255)),
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Required("setting"): vol.Any(dict, None),
})
@websocket_api.require_admin
@callback
def ws_profile_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Save (or with ``setting: null`` remove) one profile entry."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    scope, mood, target, area_id = msg["scope"], msg["mood"].strip(), msg["target"], msg.get("area_id")
    if scope != "global" and area_id not in ctrl.config.rooms:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A configured room is required")
        return
    if scope in ("global", "room") and ctrl.config.category(target) is None:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"Unknown category {target}")
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
        if errors := validate_setting(setting):
            connection.send_error(
                msg["id"], "invalid_profile",
                "; ".join(ERROR_TEXT.get(e, e) for e in errors.values()),
            )
            return
    ctrl.store.async_set(set_profile(ctrl.lighting, scope, mood, target, setting, area_id))
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
    ctrl.store.async_set(ctrl.with_room(area_id, **changes))
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


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for handler in (
        ws_editor, ws_subscribe, ws_preview, ws_profile_set, ws_mood_set, ws_settings_set, ws_room_set,
        ws_activation, ws_activate, ws_deactivate, ws_acknowledge, ws_override_clear,
    ):
        websocket_api.async_register_command(hass, handler)
