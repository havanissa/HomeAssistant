"""Websocket API of the Curtains card (0.6.1): ``smart_occupancy/curtains/*``.

Reads (``model``, ``preview``, ``subscribe``) are open to every authenticated
user and never move anything. Every write is admin only and saves to the
curtain store without a reload. ``apply`` is the only command that leads to
cover commands: it needs ``confirm: true`` and the exact targets the person
confirmed (``expected``); the integration then sends the commands itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from ..const import DOMAIN, VERSION
from ..occupancy import friendly_name
from ..supervision import ControlNotPermittedError, CurtainOrigin
from . import conflicts as cover_conflicts
from .config import (
    COMPASS,
    CONDITION_WORDS,
    CONDITIONS,
    COVER_KINDS,
    ELEVATION_LIMITS,
    EXPOSURE_WIDTH_LIMITS,
    KIND_WORDS,
    MAX_LAYERS,
    MAX_OBSTRUCTIONS,
    MAX_PRESETS,
    MAX_WINDOWS,
    PERMITTED_MODES,
    TOLERANCE_LIMITS,
    VERIFY_TIMEOUT_LIMITS,
    WINDOW_MODES,
    CoverLayer,
    CurtainConfig,
    CurtainConfigError,
    CurtainSettings,
    Obstruction,
    Preset,
    SolarSettings,
    Window,
    clean_name,
    compass_label,
    new_id,
    normalize_bearing,
    valid_position,
    with_layers,
)
from .discovery import (
    MOVING_STATES,
    area_name,
    available,
    candidates,
    capabilities,
    current_position,
    members,
    proposals,
)
from .executor import OVERALL_WORDS, PLAN_WORDS, RESULT_WORDS, preset_matches, recognise
from .manager import SIGNAL_CURTAINS_UPDATED, CurtainManager
from .solar import calibrate, exposure, relation_text

_THROTTLE_SECONDS = 0.3
MODE_WORDS = {"off": "Off", "observing": "Observing", "automatic": "Automatic"}


def _manager(hass: HomeAssistant, connection, msg) -> CurtainManager | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
        return None
    return entries[0].runtime_data.curtains


def _guard(handler: Callable) -> Callable:
    """Turn configuration and safety errors into websocket errors with a readable message."""

    async def wrapper(hass: HomeAssistant, connection, msg) -> None:
        if (mgr := _manager(hass, connection, msg)) is None:
            return
        try:
            result = handler(hass, mgr, msg, connection)
            if hasattr(result, "__await__"):
                result = await result
        except CurtainConfigError as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        except ControlNotPermittedError as err:
            connection.send_error(msg["id"], "not_permitted", str(err))
            return
        connection.send_result(msg["id"], result if result is not None else {"ok": True})

    return wrapper


# ------------------------------------------------------------------ model
def _layer_model(hass: HomeAssistant, window: Window, layer: CoverLayer, desired: int | None, tolerance: int,
                 conflicts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    state = hass.states.get(layer.entity_id)
    position = current_position(state)
    caps = capabilities(state)
    return {
        "layer_id": layer.layer_id, "entity_id": layer.entity_id, "kind": layer.kind, "kind_word": KIND_WORDS[layer.kind],
        "label": layer.label, "name": layer.label or friendly_name(hass, layer.entity_id) or layer.entity_id,
        "entity_name": friendly_name(hass, layer.entity_id) or layer.entity_id,
        "state": state.state if state else None, "missing": state is None, "available": available(state),
        "moving": bool(state and state.state in MOVING_STATES), "position": position, "caps": caps,
        "members": sorted(members(hass, layer.entity_id) - {layer.entity_id}),
        "desired": desired,
        "matches_desired": None if desired is None or position is None else abs(position - desired) <= tolerance,
        "conflicts": conflicts.get(layer.entity_id, []),
    }


def _problems(hass: HomeAssistant, window: Window, layers: list[dict[str, Any]], solar_on: bool,
              config: CurtainConfig) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not window.layers:
        out.append({"severity": "info", "text": "No covers yet: add one under Covers."})
    if window.area_id and area_name(hass, window.area_id) is None:
        out.append({"severity": "warning", "text": "Its room no longer exists in Home Assistant: choose a room."})
    for layer in layers:
        if layer["missing"]:
            out.append({"severity": "warning", "text": f"{layer['entity_id']} does not exist in Home Assistant."})
        elif not layer["available"]:
            out.append({"severity": "warning", "text": f"{layer['name']} is {layer['state']}."})
        elif not layer["caps"]["position"]:
            bad = [p.name for p in window.presets if (t := p.positions.get(layer["layer_id"])) is not None
                   and t not in (0, 100)]
            if bad:
                out.append({"severity": "warning", "text": f"{layer['name']} can only open or close fully, but "
                            f"{', '.join(bad)} set it in between."})
        if enabled := [c for c in layer["conflicts"] if c["enabled"]]:
            names = ", ".join(c["name"] or c["automation"] for c in enabled[:3])
            out.append({"severity": "warning", "text": f"{len(enabled)} enabled automation(s) may also move "
                        f"{layer['name']}: {names}. They are not changed."})
        others = {e for w in config.windows.values() if w.window_id != window.window_id for x in w.entity_ids
                  for e in members(hass, x) | {x}}
        if overlap := (members(hass, layer["entity_id"]) | {layer["entity_id"]}) & others:
            out.append({"severity": "warning", "text": f"{layer['name']} shares {', '.join(sorted(overlap))} with "
                        "another window."})
    if solar_on and window.effective_bearing is None:
        out.append({"severity": "info", "text": "Sun protection is on, but the window has no direction yet."})
    return out


def window_model(hass: HomeAssistant, mgr: CurtainManager, window: Window,
                 conflicts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    tolerance = mgr.config.settings.tolerance
    selected = window.preset(window.selected_preset)
    layers = [_layer_model(hass, window, x, selected.positions.get(x.layer_id) if selected else None, tolerance,
                           conflicts) for x in window.layers]
    sun = mgr.sun()
    bearing = window.effective_bearing
    return {
        "window_id": window.window_id, "name": window.name, "area_id": window.area_id,
        "area_name": area_name(hass, window.area_id),
        "direction": window.direction, "bearing": window.bearing, "effective_bearing": bearing,
        "compass": compass_label(bearing), "mode": window.mode, "mode_word": MODE_WORDS[window.mode],
        "solar_protection": window.solar_protection, "privacy": window.privacy,
        "sleep_protection": window.sleep_protection, "solar": window.solar.as_dict(),
        "layers": layers,
        "presets": [{**p.as_dict(), "matches": preset_matches(hass, window, p, tolerance)} for p in window.presets],
        "selected_preset": window.selected_preset,
        "current": recognise(hass, window, tolerance),
        "conditions": {c: window.conditions.get(c) for c in CONDITIONS},
        "exposure": exposure(bearing, sun, window.solar),
        "manual": mgr.manual.get(window.window_id),
        "last": mgr.last.get(window.window_id),
        "running": mgr.running(window.window_id) is not None,
        "history": mgr.history.get(window.window_id, []),
        "problems": _problems(hass, window, layers, window.solar_protection, mgr.config),
    }


def build_model(hass: HomeAssistant, mgr: CurtainManager, connection) -> dict[str, Any]:
    config = mgr.config
    covers = sorted({e for w in config.windows.values() for e in w.entity_ids})
    conflicts = cover_conflicts.find(hass, covers, mgr.automations())
    windows = [window_model(hass, mgr, w, conflicts) for w in config.windows.values()]
    rooms: dict[str | None, dict[str, Any]] = {}
    for w in windows:
        key = w["area_id"] if w["area_name"] else None
        room = rooms.setdefault(key, {"area_id": key, "name": w["area_name"] or "No room", "windows": []})
        room["windows"].append(w["window_id"])
    sun = mgr.sun()
    cal = calibrate(mgr.samples)
    s = config.settings
    return {
        "version": VERSION,
        "can_edit": bool(connection.user and connection.user.is_admin),
        "settings": s.as_dict(),
        "sun": {**sun.as_dict(), "calibration": {**cal, "text": relation_text(cal)},
                "sources": {"azimuth": s.azimuth_entity, "elevation": s.elevation_entity,
                            "sun_angle": s.sun_angle_entity,
                            "sun_angle_exists": bool(s.sun_angle_entity and hass.states.get(s.sun_angle_entity))}},
        "rooms": sorted(rooms.values(), key=lambda r: (r["area_id"] is None, r["name"].casefold())),
        "windows": windows,
        "areas": sorted(({"area_id": a.id, "name": a.name} for a in ar.async_get(hass).async_list_areas()),
                        key=lambda a: a["name"].casefold()),
        "candidates": [c.as_dict(hass) for c in candidates(hass, config)],
        "proposals": proposals(hass, config),
        "automations": len(mgr.automations()),
        "words": {"kinds": KIND_WORDS, "conditions": CONDITION_WORDS, "modes": MODE_WORDS, "plan": PLAN_WORDS,
                  "results": RESULT_WORDS, "overall": OVERALL_WORDS},
        "limits": {"windows": MAX_WINDOWS, "layers": MAX_LAYERS, "presets": MAX_PRESETS,
                   "obstructions": MAX_OBSTRUCTIONS, "tolerance": TOLERANCE_LIMITS,
                   "verify_timeout": VERIFY_TIMEOUT_LIMITS, "exposure_width": EXPOSURE_WIDTH_LIMITS,
                   "elevation": ELEVATION_LIMITS},
        "compass": list(COMPASS),
        "kinds": list(COVER_KINDS),
        "modes": list(WINDOW_MODES),
        "permitted_modes": sorted(PERMITTED_MODES),
        "conditions": list(CONDITIONS),
    }


# ------------------------------------------------------------ validation
def _physical(hass: HomeAssistant, entity_id: str) -> set[str]:
    return members(hass, entity_id) | {entity_id}


def _layers_from(hass: HomeAssistant, config: CurtainConfig, window: Window | None, window_id: str,
                 raw: list[Any]) -> tuple[CoverLayer, ...]:
    if len(raw) > MAX_LAYERS:
        raise CurtainConfigError("too_many_covers", f"A window can have at most {MAX_LAYERS} covers")
    existing = {x.layer_id: x for x in window.layers} if window else {}
    by_entity = {x.entity_id: x for x in window.layers} if window else {}
    taken_elsewhere: dict[str, str] = {}
    for other in config.windows.values():
        if other.window_id == window_id:
            continue
        for entity_id in other.entity_ids:
            for e in _physical(hass, entity_id):
                taken_elsewhere[e] = other.name
    layers: list[CoverLayer] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise CurtainConfigError("invalid_cover", "Every cover needs an entity")
        entity_id = item.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id.startswith("cover."):
            raise CurtainConfigError("invalid_cover", "Only cover entities can be assigned to a window")
        known = by_entity.get(entity_id)
        if known is None and hass.states.get(entity_id) is None:
            raise CurtainConfigError("unknown_cover", f"{entity_id} does not exist in Home Assistant")
        physical = _physical(hass, entity_id)
        if physical & seen:
            raise CurtainConfigError("duplicate_cover", f"{entity_id} is already part of this window")
        if clash := sorted(physical & taken_elsewhere.keys()):
            raise CurtainConfigError("cover_in_use", f"{clash[0]} already belongs to the window "
                                     f"{taken_elsewhere[clash[0]]}: one cover can only belong to one window")
        seen |= physical
        kind = item.get("kind", known.kind if known else "custom")
        if kind not in COVER_KINDS:
            raise CurtainConfigError("invalid_kind", "The cover type must be blackout, sheer, shade or custom")
        label = item.get("label", known.label if known else None)
        if label is not None:
            label = clean_name(label, "The cover's name") if str(label).strip() else None
        layer_id = item.get("layer_id")
        if layer_id not in existing:
            layer_id = known.layer_id if known else new_id("l_", {*existing, *(x.layer_id for x in layers)})
        layers.append(CoverLayer(layer_id, entity_id, kind, label))
    return tuple(layers)


def _solar_from(raw: Any, current: SolarSettings) -> SolarSettings:
    if not isinstance(raw, dict):
        raise CurtainConfigError("invalid_solar", "Invalid sun protection settings")
    width = raw.get("exposure_width", current.exposure_width)
    if isinstance(width, bool) or not isinstance(width, (int, float)) or width != int(width) \
            or not EXPOSURE_WIDTH_LIMITS[0] <= width <= EXPOSURE_WIDTH_LIMITS[1]:
        raise CurtainConfigError("invalid_exposure_width", "The exposure width must be a whole number of "
                                 f"{EXPOSURE_WIDTH_LIMITS[0]}-{EXPOSURE_WIDTH_LIMITS[1]}°")
    elevation = raw.get("min_elevation", current.min_elevation)
    if isinstance(elevation, bool) or not isinstance(elevation, (int, float)) \
            or not ELEVATION_LIMITS[0] <= elevation <= ELEVATION_LIMITS[1]:
        raise CurtainConfigError("invalid_elevation", f"The minimum elevation must be {ELEVATION_LIMITS[0]:.0f} to "
                                 f"{ELEVATION_LIMITS[1]:.0f}°")
    obstructions = current.obstructions
    if "obstructions" in raw:
        items = raw["obstructions"] or []
        if not isinstance(items, list) or len(items) > MAX_OBSTRUCTIONS:
            raise CurtainConfigError("invalid_obstruction", f"At most {MAX_OBSTRUCTIONS} obstructions")
        obstructions = tuple(Obstruction.from_dict(o) for o in items)
    return SolarSettings(int(width), round(float(elevation), 1), obstructions)


def _apply_fields(hass: HomeAssistant, config: CurtainConfig, window: Window, msg: dict[str, Any]) -> Window:
    changes: dict[str, Any] = {}
    if "name" in msg:
        changes["name"] = clean_name(msg["name"], "The window's name")
    if "area_id" in msg:
        area_id = msg["area_id"] or None
        if area_id is not None and ar.async_get(hass).async_get_area(area_id) is None:
            raise CurtainConfigError("unknown_area", "Choose an existing room (area)")
        changes["area_id"] = area_id
    if "direction" in msg or "bearing" in msg:
        direction, bearing = msg.get("direction"), normalize_bearing(msg.get("bearing"))
        if direction is not None and direction not in COMPASS:
            raise CurtainConfigError("invalid_direction", "The direction must be N, NE, E, SE, S, SW, W or NW")
        if bearing is not None:
            direction = None
        changes["direction"], changes["bearing"] = direction, bearing
    if "mode" in msg:
        mode = msg["mode"]
        if mode not in WINDOW_MODES:
            raise CurtainConfigError("invalid_mode", "The mode must be Off, Observing or Automatic")
        if mode not in PERMITTED_MODES:
            raise CurtainConfigError("mode_not_available",
                                     "Automatic curtain control comes in a later phase: use Observing for now")
        changes["mode"] = mode
    for flag in ("solar_protection", "privacy", "sleep_protection"):
        if flag in msg:
            if not isinstance(msg[flag], bool):
                raise CurtainConfigError("invalid_flag", f"{flag} must be on or off")
            changes[flag] = msg[flag]
    if "solar" in msg:
        changes["solar"] = _solar_from(msg["solar"], window.solar)
    window = replace(window, **changes)
    if "layers" in msg:
        window = with_layers(window, _layers_from(hass, config, window, window.window_id, msg["layers"] or []))
    if "conditions" in msg:
        raw = msg["conditions"]
        if not isinstance(raw, dict) or set(raw) - set(CONDITIONS):
            raise CurtainConfigError("invalid_condition", "Unknown condition")
        ids = {p.preset_id for p in window.presets}
        if bad := [v for v in raw.values() if v is not None and v not in ids]:
            raise CurtainConfigError("unknown_preset", f"Preset {bad[0]} does not exist in this window")
        window = replace(window, conditions={**window.conditions, **raw})
    return window


def _positions_from(window: Window, raw: Any, base: dict[str, int | None]) -> dict[str, int | None]:
    if not isinstance(raw, dict):
        raise CurtainConfigError("invalid_position", "Positions must be given per cover")
    if unknown := set(raw) - set(window.layer_ids):
        raise CurtainConfigError("unknown_layer", f"{sorted(unknown)[0]} is not a cover of this window")
    positions = dict(base)
    for layer_id, value in raw.items():
        positions[layer_id] = valid_position(value)
    return {layer_id: positions.get(layer_id) for layer_id in window.layer_ids}


def _unique_name(window: Window, name: str, preset_id: str | None) -> str:
    if any(p.name.casefold() == name.casefold() and p.preset_id != preset_id for p in window.presets):
        raise CurtainConfigError("duplicate_name", f"This window already has a preset called {name}")
    return name


def _window(mgr: CurtainManager, window_id: str) -> Window:
    window = mgr.config.windows.get(window_id)
    if window is None:
        raise CurtainConfigError("not_found", "This window does not exist")
    return window


# ---------------------------------------------------------------- handlers
@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/model"})
@websocket_api.async_response
@_guard
def ws_model(hass, mgr, msg, connection):
    return build_model(hass, mgr, connection)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection, msg: dict) -> None:
    """A throttled ``changed`` event when configuration, covers, the sun or a result changes."""
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

    unsub = async_dispatcher_connect(hass, SIGNAL_CURTAINS_UPDATED, schedule)

    @callback
    def cancel() -> None:
        unsub()
        if timer is not None:
            timer()

    connection.subscriptions[msg["id"]] = cancel
    connection.send_result(msg["id"])


_WINDOW_FIELDS = {
    vol.Optional("name"): str,
    vol.Optional("area_id"): vol.Any(str, None),
    vol.Optional("direction"): vol.Any(str, None),
    vol.Optional("bearing"): vol.Any(vol.Coerce(float), None, ""),
    vol.Optional("mode"): str,
    vol.Optional("solar_protection"): bool,
    vol.Optional("privacy"): bool,
    vol.Optional("sleep_protection"): bool,
    vol.Optional("solar"): dict,
    vol.Optional("layers"): list,
    vol.Optional("conditions"): dict,
}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/window/create",
                                  vol.Required("name"): str, **{k: v for k, v in _WINDOW_FIELDS.items()
                                                                if str(k) != "name"}})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_window_create(hass, mgr, msg, connection):
    config = mgr.config
    if len(config.windows) >= MAX_WINDOWS:
        raise CurtainConfigError("too_many_windows", f"At most {MAX_WINDOWS} windows")
    window_id = new_id("w_", config.windows)
    window = _apply_fields(hass, config, Window(window_id, clean_name(msg["name"], "The window's name")), msg)
    mgr.async_set(config.with_window(window))
    return {"window_id": window_id}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/window/update",
                                  vol.Required("window_id"): str, **_WINDOW_FIELDS})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_window_update(hass, mgr, msg, connection):
    window = _window(mgr, msg["window_id"])
    if "layers" in msg and mgr.running(window.window_id):
        raise CurtainConfigError("busy", "Wait until the preset being applied has finished")
    mgr.async_set(mgr.config.with_window(_apply_fields(hass, mgr.config, window, msg)))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/window/remove",
                                  vol.Required("window_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_window_remove(hass, mgr, msg, connection):
    window = _window(mgr, msg["window_id"])
    if mgr.running(window.window_id):
        raise CurtainConfigError("busy", "Wait until the preset being applied has finished")
    mgr.async_set(mgr.config.without_window(window.window_id))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/preset/save",
    vol.Required("window_id"): str,
    vol.Optional("preset_id"): vol.Any(str, None),
    vol.Optional("name"): str,
    vol.Optional("positions"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_preset_save(hass, mgr, msg, connection):
    """Create (no preset_id) or edit a preset: its name and any of its positions."""
    window = _window(mgr, msg["window_id"])
    preset_id = msg.get("preset_id")
    if preset_id is None:
        if len(window.presets) >= MAX_PRESETS:
            raise CurtainConfigError("too_many_presets", f"A window can have at most {MAX_PRESETS} presets")
        name = _unique_name(window, clean_name(msg.get("name", ""), "The preset's name"), None)
        preset_id = new_id("p_", (p.preset_id for p in window.presets))
        positions = _positions_from(window, msg.get("positions", {}), {})
        presets = (*window.presets, Preset(preset_id, name, positions))
    else:
        current = window.preset(preset_id)
        if current is None:
            raise CurtainConfigError("not_found", "This preset does not exist")
        name = _unique_name(window, clean_name(msg["name"], "The preset's name"), preset_id) if "name" in msg \
            else current.name
        positions = _positions_from(window, msg.get("positions", {}), dict(current.positions))
        presets = tuple(Preset(preset_id, name, positions) if p.preset_id == preset_id else p for p in window.presets)
    mgr.async_set(mgr.config.with_window(replace(window, presets=presets)))
    return {"preset_id": preset_id}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/preset/duplicate",
                                  vol.Required("window_id"): str, vol.Required("preset_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_preset_duplicate(hass, mgr, msg, connection):
    window = _window(mgr, msg["window_id"])
    source = window.preset(msg["preset_id"])
    if source is None:
        raise CurtainConfigError("not_found", "This preset does not exist")
    if len(window.presets) >= MAX_PRESETS:
        raise CurtainConfigError("too_many_presets", f"A window can have at most {MAX_PRESETS} presets")
    names = {p.name.casefold() for p in window.presets}
    name, n = f"{source.name} (copy)", 2
    while name.casefold() in names:
        name, n = f"{source.name} (copy {n})", n + 1
    preset_id = new_id("p_", (p.preset_id for p in window.presets))
    index = window.presets.index(source) + 1
    presets = (*window.presets[:index], Preset(preset_id, name[:60], dict(source.positions)),
               *window.presets[index:])
    mgr.async_set(mgr.config.with_window(replace(window, presets=presets)))
    return {"preset_id": preset_id}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/preset/delete",
                                  vol.Required("window_id"): str, vol.Required("preset_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_preset_delete(hass, mgr, msg, connection):
    """Delete a preset; the selection and condition mappings that used it become "Not set"."""
    window = _window(mgr, msg["window_id"])
    preset_id = msg["preset_id"]
    if window.preset(preset_id) is None:
        raise CurtainConfigError("not_found", "This preset does not exist")
    window = replace(
        window,
        presets=tuple(p for p in window.presets if p.preset_id != preset_id),
        selected_preset=None if window.selected_preset == preset_id else window.selected_preset,
        conditions={c: (None if v == preset_id else v) for c, v in window.conditions.items()},
    )
    mgr.async_set(mgr.config.with_window(window))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/preset/select",
                                  vol.Required("window_id"): str, vol.Required("preset_id"): vol.Any(str, None)})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_preset_select(hass, mgr, msg, connection):
    """Choose the window's desired preset. Selecting never moves anything; Apply does, after confirmation."""
    window = _window(mgr, msg["window_id"])
    if msg["preset_id"] is not None and window.preset(msg["preset_id"]) is None:
        raise CurtainConfigError("not_found", "This preset does not exist")
    mgr.async_set(mgr.config.with_window(replace(window, selected_preset=msg["preset_id"])))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/preview",
                                  vol.Required("window_id"): str, vol.Required("preset_id"): str})
@websocket_api.async_response
@_guard
def ws_preview(hass, mgr, msg, connection):
    """What a preset would do now: per cover target, current position and decision. Nothing moves."""
    return mgr.preview(msg["window_id"], msg["preset_id"])


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/apply",
    vol.Required("window_id"): str,
    vol.Required("preset_id"): str,
    vol.Required("confirm"): bool,
    vol.Required("expected"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
async def ws_apply(hass, mgr, msg, connection):
    """Apply a preset the person has just confirmed (the exact targets in ``expected``)."""
    return await mgr.async_apply(msg["window_id"], msg["preset_id"], origin=CurtainOrigin.MANUAL_CONFIRMED,
                                 confirmed=msg["confirm"] is True, expected=msg["expected"],
                                 user_id=connection.user.id if connection.user else None)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/manual/clear",
                                  vol.Required("window_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_manual_clear(hass, mgr, msg, connection):
    _window(mgr, msg["window_id"])
    mgr.clear_manual(msg["window_id"])


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/settings",
    vol.Optional("tolerance"): int,
    vol.Optional("verify_timeout"): int,
    vol.Optional("sun_angle_entity"): vol.Any(str, None),
    vol.Optional("azimuth_entity"): str,
    vol.Optional("elevation_entity"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_settings(hass, mgr, msg, connection):
    current = mgr.config.settings
    changes: dict[str, Any] = {}
    for key, (low, high) in (("tolerance", TOLERANCE_LIMITS), ("verify_timeout", VERIFY_TIMEOUT_LIMITS)):
        if key in msg:
            if not low <= msg[key] <= high:
                raise CurtainConfigError(f"invalid_{key}", f"{key.replace('_', ' ').capitalize()} must be {low}-{high}")
            changes[key] = msg[key]
    for key in ("sun_angle_entity", "azimuth_entity", "elevation_entity"):
        if key in msg:
            value = msg[key] or None
            if value is not None and (not isinstance(value, str) or "." not in value):
                raise CurtainConfigError("invalid_entity", "Choose an entity")
            if value is None and key != "sun_angle_entity":
                value = "sun.sun"
            changes[key] = value
    if changes.get("sun_angle_entity", current.sun_angle_entity) != current.sun_angle_entity \
            or changes.get("azimuth_entity", current.azimuth_entity) != current.azimuth_entity:
        mgr.samples.clear()  # samples of other sources say nothing about the new ones
    mgr.async_set(replace(mgr.config, settings=CurtainSettings(**{**current.as_dict(), **changes})))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/rescan"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_rescan(hass, mgr, msg, connection):
    """Scan the automations for competing cover control again (read only)."""
    mgr.rescan()


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/calibration/reset"})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_calibration_reset(hass, mgr, msg, connection):
    mgr.reset_calibration()


CURTAIN_HANDLERS = (
    ws_model, ws_subscribe, ws_window_create, ws_window_update, ws_window_remove, ws_preset_save,
    ws_preset_duplicate, ws_preset_delete, ws_preset_select, ws_preview, ws_apply, ws_manual_clear, ws_settings,
    ws_rescan, ws_calibration_reset,
)
