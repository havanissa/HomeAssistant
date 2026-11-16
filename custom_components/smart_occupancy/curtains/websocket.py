"""Websocket API of the Curtains card (0.6.1, automatic 0.6.2, environment 0.6.3): ``smart_occupancy/curtains/*``.

Reads (``model``, ``preview``, ``auto/preview``, ``subscribe``) are open to
every authenticated user and never move anything. Every write is admin only and
saves to the curtain store without a reload. Two commands can lead to cover
commands: ``apply`` (a manual preset; needs ``confirm: true`` and the exact
targets the person confirmed) and ``auto/enable`` (turns one window to
Automatic after the person reviewed its intended preset, safety conditions and
conflicts; needs ``confirm: true`` and is refused while a blocker remains).
``resume`` releases a manual hold of a window, which an Automatic window
then follows at once. Automatic cannot be set through ``window/update``.

0.6.4: Global Curtain Modes (``modes/*`` and ``cover_preset/*`` in
``global_modes.py``): the model carries every mode's assignments and each
window's effective preset per mode with its source.

0.6.5: the Global Curtain Schedule (``schedule/change`` in ``schedule_page.py``):
the model's ``schedule`` holds the household phase, the next transitions and
today's timeline.

0.6.3: ``auto/simulate`` (read only) predicts what every affected window would
decide with changed settings *before* they are saved, so a new automatic
feature (temperature-aware sun, glare, weather evidence) is never switched on
blind. The model carries each window's environment (sun angle relative to the
window, light, temperatures, weather, direct-sun and temperature assessments)
and a sensor-quality summary of every candidate entity.
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
from . import conflicts as cover_conflicts, rules as curtain_rules
from .config import (
    COMPASS,
    CONDITION_WORDS,
    CONDITIONS,
    COVER_KINDS,
    ELEVATION_LIMITS,
    EXPOSURE_WIDTH_LIMITS,
    MAX_ELEVATION_LIMITS,
    KIND_WORDS,
    MAX_LAYERS,
    MAX_OBSTRUCTIONS,
    MAX_PRESETS,
    MAX_WINDOWS,
    MODES,
    NO_ACTION,
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
from .environment import BAND_WORDS, ROLE_WORDS, STATUS_WORDS, discover
from .executor import OVERALL_WORDS, PLAN_WORDS, RESULT_WORDS, preset_matches, recognise
from .manager import SIGNAL_CURTAINS_UPDATED, CurtainManager
from .modes import describe, effective, overrides_of
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
        "presets": [p.as_dict() for p in layer.presets],
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
        "cover_modes": {k: dict(v) for k, v in window.cover_modes.items()},
        "layered": window.layered,
        "effective": _effective_model(mgr.config, window),
        "overrides": overrides_of(mgr.config, window),
        "usage": _usage(mgr.config, window),
        "exposure": exposure(bearing, sun, window.solar),
        "manual": mgr.manual.get(window.window_id),
        "last": mgr.last.get(window.window_id),
        "running": mgr.running(window.window_id) is not None,
        "history": mgr.history.get(window.window_id, []),
        "problems": _problems(hass, window, layers, window.solar_protection, mgr.config),
        "auto": mgr.auto.model(window),
        "allowed_automations": list(window.allowed_automations),
    }


def _effective_model(config: CurtainConfig, window: Window) -> dict[str, Any]:
    """Per mode: the window's effective preset, its source ("Half shade — Global Morning") and each cover's."""
    out = {}
    for mode in MODES:
        eff = effective(config, window, mode)
        out[mode] = {**eff.as_dict(), "text": describe(eff, mode)}
    return out


def _usage(config: CurtainConfig, window: Window) -> dict[str, list[str]]:
    """Where each of the window's presets is used (shown before a preset is deleted)."""
    from .config import MODE_WORDS

    out: dict[str, list[str]] = {}
    for mode in MODES:
        if (v := window.conditions.get(mode)) and v != NO_ACTION:
            out.setdefault(v, []).append(f"{MODE_WORDS[mode]} (window override)")
        for a in config.modes.get(mode, ()):
            if a.window_id == window.window_id and a.layer_id is None:
                out.setdefault(a.preset_id, []).append(f"{MODE_WORDS[mode]} (Global Modes)")
        if window.area_id and (v := config.room_modes.get(window.area_id, {}).get(mode)) and v != NO_ACTION:
            out.setdefault(v, []).append(f"{MODE_WORDS[mode]} (room override)")
    return out


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
                   "elevation": ELEVATION_LIMITS, "max_elevation": MAX_ELEVATION_LIMITS},
        "compass": list(COMPASS),
        "kinds": list(COVER_KINDS),
        "modes": list(WINDOW_MODES),
        "permitted_modes": sorted(PERMITTED_MODES),
        "conditions": list(CONDITIONS),
        "rules": {"schema": curtain_rules.schema(), "groups": [list(g) for g in curtain_rules.GROUPS],
                  "global": curtain_rules.dump(config.defaults),
                  "rooms": {k: curtain_rules.dump(v) for k, v in config.rooms.items()},
                  "defaults": curtain_rules.dump(curtain_rules.defaults()), "weekdays": list(curtain_rules.WEEKDAYS)},
        "entities": _entity_choices(hass),
        "environment": environment_summary(hass, mgr),
        "time_zone": hass.config.time_zone,
        "global_modes": modes_model(hass, mgr, mgr.config),
        "schedule": schedule_model(hass, mgr),
        "migration": migration_summary(hass, config),
        "centralized": config.centralized,
    }


def environment_summary(hass: HomeAssistant, mgr: CurtainManager) -> dict[str, Any]:
    """The sensor-quality summary: every candidate per role, its validity now, and which windows use it."""
    config = mgr.config
    chosen: dict[str, set[str]] = {r: set() for r in ROLE_WORDS}
    role_of = {"lux_entity": "outdoor_lux", "indoor_lux_entity": "indoor_lux", "room_temp_entity": "room_temp",
               "outdoor_temp_entity": "outdoor_temp", "weather_entity": "weather", "cloud_entity": "cloud",
               "thermostat_entity": "thermostat"}
    for window in config.windows.values():
        values, _ = config.rules_for(window)
        for key, role in role_of.items():
            if values[key]:
                chosen[role].add(values[key])
    global_values, _ = curtain_rules.resolve(config.defaults, None, {})
    for key, role in role_of.items():
        if global_values.get(key):
            chosen[role].add(global_values[key])
    found = discover(hass, chosen=chosen, values=global_values, elevation=mgr.sun().elevation,
                     since_of=lambda e: (mgr.auto.value_since.get(e) or (None, None))[1])
    return {"roles": [{"role": r, "word": ROLE_WORDS[r], "candidates": found[r],
                       "chosen": sorted(chosen[r])} for r in ROLE_WORDS],
            "global": {k: global_values[k] for k in role_of}, "status_words": STATUS_WORDS,
            "band_words": BAND_WORDS}


def _entity_choices(hass: HomeAssistant) -> dict[str, list[dict[str, Any]]]:
    """Suggestions for the entity pickers (any entity id can still be typed)."""
    def item(state) -> dict[str, Any]:
        return {"entity_id": state.entity_id, "name": friendly_name(hass, state.entity_id) or state.entity_id}

    sleep = [item(x) for x in hass.states.async_all(("input_boolean", "binary_sensor"))
             if x.domain == "input_boolean" or x.attributes.get("device_class") in (None, "occupancy", "presence")]
    lux = [item(x) for x in hass.states.async_all("sensor")
           if x.attributes.get("device_class") == "illuminance" or x.attributes.get("unit_of_measurement") == "lx"]
    lights = [item(x) for x in hass.states.async_all("light")]
    temperature = [item(x) for x in hass.states.async_all(("sensor", "climate", "weather"))
                   if x.domain != "sensor" or x.attributes.get("device_class") == "temperature"
                   or x.attributes.get("unit_of_measurement") in ("°C", "°F")]
    weather = [item(x) for x in hass.states.async_all("weather")]
    cloud = [item(x) for x in hass.states.async_all("sensor") if x.attributes.get("unit_of_measurement") == "%"
             and "cloud" in x.entity_id]
    thermostat = [item(x) for x in hass.states.async_all(("climate", "input_number"))]

    def key(d: dict[str, Any]) -> tuple[bool, str]:
        return not d["entity_id"].startswith("input_boolean."), d["name"].casefold()

    return {"sleep": sorted(sleep, key=key)[:400], "lux": sorted(lux, key=key)[:200],
            "light": sorted(lights, key=key)[:400], "temperature": sorted(temperature, key=key)[:300],
            "weather": sorted(weather, key=key)[:50], "cloud": sorted(cloud, key=key)[:50],
            "thermostat": sorted(thermostat, key=key)[:200]}


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
        # 0.6.4: a kept layer keeps its own presets (also when its entity is replaced); a new one starts with
        # Fully open / Half open / Fully closed.
        kept = existing.get(layer_id)
        layers.append(CoverLayer(layer_id, entity_id, kind, label, *((kept.presets,) if kept else ())))
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
    top = raw.get("max_elevation", current.max_elevation)
    if top == "":
        top = None
    if top is not None:
        if isinstance(top, bool) or not isinstance(top, (int, float)) \
                or not MAX_ELEVATION_LIMITS[0] <= top <= MAX_ELEVATION_LIMITS[1]:
            raise CurtainConfigError("invalid_max_elevation", "The overhang limit must be "
                                     f"{MAX_ELEVATION_LIMITS[0]:.0f} to {MAX_ELEVATION_LIMITS[1]:.0f}° (or none)")
        if top <= elevation:
            raise CurtainConfigError("invalid_max_elevation", "The overhang limit must be above the minimum elevation")
        top = round(float(top), 1)
    return SolarSettings(int(width), round(float(elevation), 1), obstructions, top)


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
        if mode == "automatic" and window.mode != "automatic":
            raise CurtainConfigError("confirm_required", "Automatic is enabled with its own confirmation: review the "
                                     "intended preset, safety and conflicts first (auto/enable)")
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
        if any(v is not None for v in raw.values()):
            # 0.6.6: which preset a mode uses is set only in Curtains → Global Modes (clearing an old one is fine).
            raise CurtainConfigError("managed_globally", "Presets for modes are assigned in Curtains → Global Modes")
        ids = {p.preset_id for p in window.presets}
        if bad := [v for c, v in raw.items() if v is not None and v not in ids and not (v == NO_ACTION and c in MODES)]:
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
    updated = _apply_fields(hass, mgr.config, window, msg)
    if updated.mode != window.mode:
        mgr.auto.mode_changing(window, updated.mode)
    mgr.async_set(mgr.config.with_window(updated))


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
    """Delete a preset; the selection and window overrides that used it become "Not set" (inherit), and
    Global Modes assignments of this window that used it are removed (0.6.4)."""
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


# ------------------------------------------------------- automatic (0.6.2)
def enable_review(hass: HomeAssistant, mgr: CurtainManager, window: Window) -> dict[str, Any]:
    """What enabling Automatic would do now: the intended condition and preset, every cover's plan,
    the blockers that refuse it and the warnings the person should know about. Nothing moves."""
    decision = mgr.auto.evaluate(window.window_id)
    model = mgr.auto.model(window)
    values = model["values"]
    blockers: list[str] = []
    warnings: list[str] = []
    if not window.layers:
        blockers.append("The window has no covers.")
    if not window.presets:
        blockers.append("The window has no presets.")
    for layer in window.layers:
        if hass.states.get(layer.entity_id) is None:
            blockers.append(f"{layer.entity_id} does not exist in Home Assistant.")
        elif not available(hass.states.get(layer.entity_id)):
            warnings.append(f"{layer.label or friendly_name(hass, layer.entity_id) or layer.entity_id} is unavailable: "
                            "nothing is sent until it reports again.")
    conflicts = mgr.blocking_automations(window)
    for c in conflicts:
        blockers.append(f"Enabled automation {c['name'] or c['automation']} may also move "
                        f"{friendly_name(hass, c['entity_id']) or c['entity_id']} ({c['detail']}). Disable it, or allow "
                        "it in Diagnostics if it does not compete.")
    blockers += [f"Settings: {w}." for w in model["warnings"]]
    if not values["lux_entity"]:
        warnings.append("No outdoor light sensor: " + ("the window opens when the sun is clearly up (fallback)."
                        if values["fallback"] == "sun" else "the window never opens automatically. Choose a sensor "
                        "under Automatic → Daylight, or allow the sun fallback."))
    elif model["light"]["value"] is None:
        warnings.append(f"The outdoor light sensor {values['lux_entity']} is unavailable: no automatic opening.")
    if window.sleep_protection:
        state = hass.states.get(values["sleep_entity"]) if values["sleep_entity"] else None
        if state is None or state.state not in ("on", "off"):
            warnings.append(f"The sleep source {values['sleep_entity'] or '(none)'} is unavailable: the window does "
                            "not open automatically until it reports.")
    config = mgr.config
    if values["lamp_enabled"] and (not values["lamp_entity"] or not effective(config, window, "lamp").has_target):
        warnings.append("The window lamp override needs a lamp and a Window Lamp preset.")
    unset = [CONDITION_WORDS[c] for c in ("morning", "day", "night", "sleep", "evening_privacy", "sun")
             if not effective(config, window, c).has_target and (c != "sleep" or window.sleep_protection)
             and (c != "evening_privacy" or window.privacy) and (c != "sun" or window.solar_protection)]
    if unset:
        warnings.append(f"No preset for {', '.join(unset)}: the covers are kept as they are then.")
    for mode in MODES:
        for problem in effective(config, window, mode).problems:
            warnings.append(f"{CONDITION_WORDS.get(mode, mode)}: {problem}.")
    if window.solar_protection and window.effective_bearing is None:
        warnings.append("Sun protection needs the window's direction.")
    env = model["environment"]["readings"]
    lux_reading = env.get("outdoor_lux") or {}
    if lux_reading.get("status") in ("stuck", "implausible"):
        warnings.append(f"The outdoor light sensor is not trusted now ({lux_reading.get('detail')}): nothing opens "
                        "or shades until it reports plausible values.")
    if values["thermal_enabled"]:
        room = env.get("room_temp") or {}
        if room.get("status") != "ok":
            warnings.append("Temperature-aware sun is on, but the room temperature is not usable "
                            f"({room.get('detail') or 'not chosen'}): normal sun protection applies.")
        if not window.solar_protection:
            warnings.append("Temperature-aware sun only acts on windows with sun protection on.")
    if values["glare_sensitive"] and not window.solar_protection:
        warnings.append("Glare protection only acts on windows with sun protection on.")
    if values["sun_evidence"] == "weather" and not values["lux_entity"]:
        warnings.append("Without a light sensor a clear sky from the weather may confirm sunshine for sun protection "
                        "(it never opens a window).")
    if model["hold"]:
        warnings.append("A manual hold is active; it is kept until it ends (or use Resume).")
    preset = decision.target.preset if decision and decision.target.has_target else None
    plan = mgr.plan(window, preset)["items"] if preset else []
    return {"window_id": window.window_id, "mode": window.mode, "decision": decision.as_dict() if decision else None,
            "preset_id": preset.preset_id if preset else None, "preset_name": preset.name if preset else None,
            "items": plan, "moves": sum(1 for i in plan if i["decision"] == "move"), "blockers": blockers,
            "warnings": warnings, "conflicts": conflicts, "precedence": model["precedence"], "hold": model["hold"]}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/auto/preview",
                                  vol.Required("window_id"): str})
@websocket_api.async_response
@_guard
def ws_auto_preview(hass, mgr, msg, connection):
    return enable_review(hass, mgr, _window(mgr, msg["window_id"]))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/auto/enable",
                                  vol.Required("window_id"): str, vol.Required("confirm"): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_auto_enable(hass, mgr, msg, connection):
    """Turn one window to Automatic after the person reviewed ``auto/preview``. Other windows are not touched."""
    window = _window(mgr, msg["window_id"])
    if msg["confirm"] is not True:
        raise CurtainConfigError("confirm_required", "Enabling Automatic needs an explicit confirmation")
    if window.mode == "automatic":
        return {"mode": "automatic"}
    review = enable_review(hass, mgr, window)
    if review["blockers"]:
        raise CurtainConfigError("blocked", review["blockers"][0])
    mgr.auto.mode_changing(window, "automatic")
    mgr.async_set(mgr.config.with_window(replace(window, mode="automatic")))
    return {"mode": "automatic"}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/resume",
                                  vol.Required("window_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_resume(hass, mgr, msg, connection):
    """Resume Automatic Curtains: release the window's manual hold and apply the current condition."""
    window = _window(mgr, msg["window_id"])
    if window.mode == "off":
        raise CurtainConfigError("off", "This window is Off")
    mgr.auto.resume(window.window_id)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/auto/settings",
    vol.Required("scope"): vol.In(curtain_rules.ALL_SCOPES),
    vol.Optional("area_id"): str,
    vol.Optional("window_id"): str,
    vol.Required("values"): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_auto_settings(hass, mgr, msg, connection):
    """Set (or, with null, remove) automatic rule settings for every window, one room or one window."""
    new, affected = _settings_change(hass, mgr, msg)
    for window in affected or [Window("_", "_")]:
        values, _ = new.rules_for(window)
        if problems := curtain_rules.consistency(values):
            raise CurtainConfigError("inconsistent", problems[0])
    mgr.async_set(new)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/curtains/automation/allow",
                                  vol.Required("window_id"): str, vol.Required("automation"): str,
                                  vol.Required("allowed"): bool})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_automation_allow(hass, mgr, msg, connection):
    """Mark a listed automation as reviewed: it may coexist with Automatic for this window (or undo that).
    The automation itself is never changed, disabled or deleted."""
    window = _window(mgr, msg["window_id"])
    automation = msg["automation"]
    if not automation.startswith("automation."):
        raise CurtainConfigError("invalid_automation", "Choose an automation")
    allowed = [a for a in window.allowed_automations if a != automation]
    if msg["allowed"]:
        allowed.append(automation)
    mgr.async_set(mgr.config.with_window(replace(window, allowed_automations=tuple(allowed))))
    mgr.auto.evaluate(window.window_id, force_notify=True)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/auto/simulate",
    vol.Required("scope"): vol.In(curtain_rules.ALL_SCOPES),
    vol.Optional("area_id"): str,
    vol.Optional("window_id"): str,
    vol.Required("values"): dict,
})
@websocket_api.async_response
@_guard
def ws_auto_simulate(hass, mgr, msg, connection):
    """What every affected window would decide now if these settings were saved. Saves and moves nothing.

    Shown before a new automatic feature is switched on (the card's confirmation)."""
    new, affected = _settings_change(hass, mgr, msg)
    out = []
    for window in affected[:MAX_WINDOWS]:
        values, _ = new.rules_for(window)
        current = mgr.auto.rt(window.window_id).decision
        if window.mode == "off":
            out.append({"window_id": window.window_id, "name": window.name, "mode": window.mode, "current": None,
                        "predicted": None, "preset_name": None, "moves": 0, "changes": False})
            continue
        predicted = mgr.auto.simulate(window, values)
        preset = predicted.target.preset if predicted.target.has_target else None
        items = mgr.plan(window, preset)["items"] if preset else []
        moves = sum(1 for i in items if i["decision"] == "move") if predicted.condition not in (
            "manual_override", "waiting") else 0
        out.append({"window_id": window.window_id, "name": window.name, "mode": window.mode,
                    "current": current.as_dict() if current else None,
                    "current_preset_name": current.preset_name if current else None,
                    "predicted": predicted.as_dict(), "preset_name": preset.name if preset else None,
                    "items": items, "moves": moves,
                    "changes": current is None or (current.condition, current.preset_id) != (predicted.condition,
                                                                                              predicted.preset_id),
                    "warnings": curtain_rules.consistency(values)})
    return {"windows": out}


def _settings_change(hass: HomeAssistant, mgr: CurtainManager, msg: dict[str, Any]) -> tuple[CurtainConfig, list[Window]]:
    """The configuration after an ``auto/settings`` change and the windows it affects (not saved)."""
    scope = msg["scope"]
    config = mgr.config

    def merged(current: dict[str, Any]) -> dict[str, Any]:
        out = dict(current)
        for key, value in msg["values"].items():
            clean = curtain_rules.validate(key, value, scope)
            if clean is None:
                out.pop(key, None)
            else:
                out[key] = clean
        return out

    if scope == curtain_rules.GLOBAL:
        new = replace(config, defaults=merged(dict(config.defaults)))
        affected = list(new.windows.values())
    elif scope == curtain_rules.ROOM:
        area_id = msg.get("area_id")
        if not area_id or ar.async_get(hass).async_get_area(area_id) is None:
            raise CurtainConfigError("unknown_area", "Choose an existing room (area)")
        room = merged(dict(config.rooms.get(area_id, {})))
        rooms = {k: v for k, v in config.rooms.items() if k != area_id}
        if room:
            rooms[area_id] = room
        new = replace(config, rooms=rooms)
        affected = [w for w in new.windows.values() if w.area_id == area_id]
    else:
        window = _window(mgr, msg.get("window_id") or "")
        updated = replace(window, auto=merged(dict(window.auto)))
        new = config.with_window(updated)
        affected = [updated]
    return new, affected


from .global_modes import MODE_HANDLERS, modes_model  # noqa: E402  (it uses the helpers above)
from .schedule_page import SCHEDULE_HANDLERS, schedule_model  # noqa: E402
from .migration import MIGRATION_HANDLERS, summary as migration_summary  # noqa: E402

CURTAIN_HANDLERS = (
    ws_model, ws_subscribe, ws_window_create, ws_window_update, ws_window_remove, ws_preset_save,
    ws_preset_duplicate, ws_preset_delete, ws_preset_select, ws_preview, ws_apply, ws_manual_clear, ws_settings,
    ws_rescan, ws_calibration_reset, ws_auto_preview, ws_auto_enable, ws_resume, ws_auto_settings,
    ws_automation_allow, ws_auto_simulate, *MODE_HANDLERS, *SCHEDULE_HANDLERS, *MIGRATION_HANDLERS,
)
