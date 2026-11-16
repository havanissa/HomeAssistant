"""Global Curtain Modes page (0.6.4, the user's Phase 4.6.3): model, validated changes, preview, commands.

The page is a central way to edit *which preset a condition uses*; it is not a
scheduler and never moves anything itself. Every edit is a list of operations
(``ops``) sent to ``smart_occupancy/curtains/modes/change``:

* ``assign`` / ``unassign`` - a Global Modes assignment of a whole window or of
  one cover of a layered window (``replace: true`` swaps out an overlapping
  assignment of the same window instead of refusing it);
* ``set`` - a room, window or cover override: a preset, ``no_action`` or
  ``null`` (inherit, "Use Global Setting");
* ``adopt`` - move a window's overrides into Global Modes (the reviewable
  migration path from 0.6.3: a window override becomes the window's Global
  Modes assignment when it has none, and is then cleared).

``dry_run: true`` validates the operations on a copy and returns the draft page
model and the impact: per affected window the previous and proposed effective
preset per mode, its current automatic condition, whether the change would
move covers now, a manual hold, a block and competing automations. Saving
needs ``confirm: true`` when an Automatic window's *active* target changes,
together with a policy: ``now`` (the controller applies it through all its
normal safety limits) or ``next_change`` (kept until the window's condition
changes). Observing and Off windows never move because of a mapping change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from ..occupancy import friendly_name
from .config import (
    ADVANCED_MODES,
    COMMON_MODES,
    MAX_COVER_PRESETS,
    MODE_WORDS,
    MODES,
    NO_ACTION,
    Assignment,
    CoverLayer,
    CoverPreset,
    CurtainConfig,
    CurtainConfigError,
    Window,
    clean_name,
    new_id,
    overlapping,
    valid_position,
)
from .discovery import area_name, available, members
from .manager import CurtainManager
from .modes import SOURCE_WORDS, describe, effective, hidden_by, layer_name, resolve_layer
from .websocket import _guard, _window

MODE_ICONS = {
    "morning": "mdi:weather-sunset-up", "day": "mdi:white-balance-sunny", "evening_privacy": "mdi:weather-sunset-down",
    "night": "mdi:weather-night", "sleep": "mdi:sleep", "sun": "mdi:weather-sunny-alert", "lamp": "mdi:lamp",
    "glare": "mdi:sunglasses", "heat": "mdi:thermometer-high",
}
MODE_HINTS = {
    "morning": "After the night and sleep, once daylight is measured",
    "day": "Daylight, nobody asleep",
    "evening_privacy": "Dark outside in the evening (windows with evening privacy on)",
    "night": "The household night",
    "sleep": "Sleep mode is on (windows with sleep protection on)",
    "sun": "The sun in front of the window and strong measured light (windows with sun protection on)",
    "lamp": "While the window lamp is on (set per window: Automatic → Window lamp)",
    "glare": "Glare-sensitive window in direct sunlight; nothing assigned = its Sun Protection preset",
    "heat": "Direct sunlight while the room is above its maximum temperature; nothing assigned = Sun Protection",
}
POLICIES = ("now", "next_change")
LOG_ROWS = 20


# =================================================================== model
def _target_type(window: Window, layer: CoverLayer | None) -> str:
    if layer is not None:
        return "cover"
    return "window" if window.layered else "single"


TYPE_WORDS = {"window": "Whole window", "cover": "Single cover", "single": "Cover"}


def _options(window: Window, layer: CoverLayer | None) -> list[dict[str, Any]]:
    if layer is not None:
        return [{"preset_id": p.preset_id, "name": p.name, "detail": f"{p.position} %"} for p in layer.presets]
    return [{"preset_id": p.preset_id, "name": p.name,
             "detail": " · ".join(f"{layer_name(x)} {'—' if (v := p.positions.get(x.layer_id)) is None else f'{v} %'}"
                                  for x in window.layers)} for p in window.presets]


def _name(window: Window, layer: CoverLayer | None) -> str:
    return f"{window.name} — {layer_name(layer)}" if layer is not None else window.name


def _status(mgr: CurtainManager, window: Window) -> dict[str, Any]:
    """Automatic / Observing / Off, a manual hold and the condition the controller has now."""
    rt = mgr.auto.runtimes.get(window.window_id)
    decision = rt.decision if rt else None
    active = None
    if decision is not None and window.mode != "off":
        active = decision.phase if decision.condition == "solar_heating" else decision.condition
    return {"mode": window.mode, "mode_word": {"off": "Off", "observing": "Observing", "automatic": "Automatic"}[
        window.mode], "hold": bool(rt and rt.hold), "condition": decision.condition if decision else None,
        "condition_word": MODE_WORDS.get(decision.condition, decision.condition) if decision else None,
        "active_mode": active, "deferred": bool(rt and rt.defer)}


def _cover_problems(hass: HomeAssistant, window: Window, layer: CoverLayer | None) -> list[str]:
    out = []
    for x in [layer] if layer is not None else window.layers:
        state = hass.states.get(x.entity_id)
        if state is None:
            out.append(f"{x.entity_id} does not exist in Home Assistant")
        elif not available(state):
            out.append(f"{x.label or friendly_name(hass, x.entity_id) or x.entity_id} is {state.state}")
    return out


def modes_model(hass: HomeAssistant, mgr: CurtainManager, config: CurtainConfig) -> dict[str, Any]:
    """Everything the Global Modes page shows, for ``config`` (the saved one, or a draft)."""
    windows = config.windows
    rooms_by_id = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    conflicts = {w.window_id: [c["name"] or c["automation"] for c in mgr.blocking_automations(w)]
                 for w in windows.values()}
    statuses = {w.window_id: _status(mgr, w) for w in windows.values()}

    def room(window: Window) -> str:
        return rooms_by_id.get(window.area_id or "", "No room")

    out_modes = []
    for mode in MODES:
        rows = []
        in_rows: set[tuple[str, str | None]] = set()
        warnings: list[str] = []
        for a in config.modes.get(mode, ()):
            window = windows.get(a.window_id)
            if window is None:
                continue
            layer = window.layer(a.layer_id) if a.layer_id else None
            if a.layer_id and layer is None:
                continue
            in_rows.add(a.target)
            name = _name(window, layer)
            if layer is not None:
                t = resolve_layer(config, window, layer, mode)
                where = f"Global {MODE_WORDS[mode]}" if t.source == "global" else SOURCE_WORDS.get(t.source, "")
                eff_text = (f"{t.preset_name} — {where}" if t.state == "preset" else
                            "Missing preset — " + where if t.state == "missing" else "No action — " + where)
                missing = layer.preset(a.preset_id) is None
            else:
                eff = effective(config, window, mode)
                eff_text = describe(eff, mode)
                missing = window.preset(a.preset_id) is None
            hidden = hidden_by(config, window, mode, a.layer_id)
            problems = _cover_problems(hass, window, layer)
            status = statuses[window.window_id]
            if missing:
                warnings.append(f"{name}: its preset no longer exists; nothing is sent for it until you choose one.")
            for h in hidden:
                warnings.append(f"{name}: {h['word'].lower()}{(' (' + h['cover'] + ')') if h.get('cover') else ''} "
                                f"({h['label']}) hides this Global Modes setting.")
            warnings += [f"{name}: {p}." for p in problems]
            if conflicts[window.window_id]:
                warnings.append(f"{name}: enabled automation(s) may also move these covers "
                                f"({', '.join(conflicts[window.window_id][:3])}).")
            rows.append({"window_id": window.window_id, "layer_id": a.layer_id, "preset_id": a.preset_id,
                         "name": name, "window_name": window.name, "cover_name": layer_name(layer) if layer else None,
                         "room": room(window), "area_id": window.area_id,
                         "type": _target_type(window, layer), "type_word": TYPE_WORDS[_target_type(window, layer)],
                         "options": _options(window, layer), "missing": missing, "effective": eff_text,
                         "hidden": hidden, "problems": problems, "conflicts": conflicts[window.window_id],
                         "status": status, "active": status["active_mode"] == mode})
        rows.sort(key=lambda r: (r["room"].casefold(), r["window_name"].casefold(), r["layer_id"] or ""))
        # Targets set for this mode only in window settings (0.6.3 mappings and later overrides).
        others = []
        for window in windows.values():
            for layer in [None, *(window.layers if window.layered else ())]:
                level = "cover" if layer else "window"
                value = window.cover_modes.get(layer.layer_id, {}).get(mode) if layer else window.conditions.get(mode)
                if not value or (window.window_id, layer.layer_id if layer else None) in in_rows:
                    continue
                if layer is None and any(t[0] == window.window_id for t in in_rows):
                    continue
                label = "No action" if value == NO_ACTION else (
                    (p.name if (p := (layer.preset(value) if layer else window.preset(value))) else "missing preset"))
                others.append({"window_id": window.window_id, "layer_id": layer.layer_id if layer else None,
                               "name": _name(window, layer), "room": room(window), "level": level,
                               "word": SOURCE_WORDS[level], "label": label, "value": value,
                               "status": statuses[window.window_id]})
        # Room overrides of this mode.
        for area_id, values in config.room_modes.items():
            if (value := values.get(mode)):
                others.append({"area_id": area_id, "name": rooms_by_id.get(area_id, "A removed room"),
                               "room": rooms_by_id.get(area_id, "—"), "level": "room", "word": SOURCE_WORDS["room"],
                               "label": "No action" if value == NO_ACTION else _shared_name(config, area_id, value),
                               "value": value})
        others.sort(key=lambda r: (r["room"].casefold(), r["name"].casefold()))
        quiet = [r["name"] for r in rows if r["status"]["mode"] != "automatic"]
        if quiet:
            warnings.append(f"{len(quiet)} assigned target{'s are' if len(quiet) != 1 else ' is'} not Automatic "
                            f"({', '.join(quiet[:4])}{'…' if len(quiet) > 4 else ''}): Observing only explains, Off is "
                            "not evaluated.")
        out_modes.append({"key": mode, "word": MODE_WORDS[mode], "icon": MODE_ICONS[mode], "hint": MODE_HINTS[mode],
                          "advanced": mode in ADVANCED_MODES, "count": len(rows), "rows": rows, "others": others,
                          "warnings": list(dict.fromkeys(warnings))})

    targets = []
    for window in sorted(windows.values(), key=lambda w: (room(w).casefold(), w.name.casefold())):
        for layer in [None, *(window.layers if window.layered else ())]:
            targets.append({"window_id": window.window_id, "layer_id": layer.layer_id if layer else None,
                            "name": _name(window, layer), "room": room(window),
                            "type": _target_type(window, layer), "type_word": TYPE_WORDS[_target_type(window, layer)],
                            "options": _options(window, layer), "has_covers": bool(window.layers),
                            "status": statuses[window.window_id]})
    overrides = sum(1 for w in windows.values() for m in MODES if w.conditions.get(m)) + sum(
        len(v) for w in windows.values() for v in w.cover_modes.values())
    return {"modes": out_modes, "common": list(COMMON_MODES), "advanced": list(ADVANCED_MODES),
            "targets": targets, "matrix": _matrix(config, windows, rooms_by_id), "overrides": overrides,
            "log": _recent(mgr), "policies": list(POLICIES)}


def _room_windows(config: CurtainConfig, area_id: str) -> list[Window]:
    return [w for w in config.windows.values() if w.area_id == area_id]


def _shared(config: CurtainConfig, area_id: str) -> list[dict[str, Any]]:
    """Presets (by stable id) that every window of a room has: what a room override can choose."""
    windows = [w for w in _room_windows(config, area_id) if w.presets]
    if not windows:
        return []
    ids = set.intersection(*({p.preset_id for p in w.presets} for w in windows))
    return [{"preset_id": p.preset_id, "name": p.name} for p in windows[0].presets if p.preset_id in ids]


def _shared_name(config: CurtainConfig, area_id: str, preset_id: str) -> str:
    for window in _room_windows(config, area_id):
        if (p := window.preset(preset_id)) is not None:
            return p.name
    return "missing preset"


def _matrix(config: CurtainConfig, windows: Mapping[str, Window], rooms_by_id: Mapping[str, str]) -> list[dict]:
    """All windows × modes: rooms, their windows and (layered) covers, each cell with its value and source."""
    rows: list[dict[str, Any]] = []
    groups: dict[str | None, list[Window]] = {}
    for window in windows.values():
        groups.setdefault(window.area_id if window.area_id in rooms_by_id else None, []).append(window)
    order = sorted(groups, key=lambda a: (a is None, rooms_by_id.get(a or "", "").casefold()))
    for area_id in order:
        if area_id is not None:
            cells = {}
            for mode in MODES:
                value = config.room_modes.get(area_id, {}).get(mode)
                cells[mode] = {"value": value, "state": "inherit" if not value else
                               "no_action" if value == NO_ACTION else "preset",
                               "label": "Inherit" if not value else "No action" if value == NO_ACTION else
                               _shared_name(config, area_id, value), "source": "room" if value else None}
            rows.append({"kind": "room", "area_id": area_id, "name": rooms_by_id[area_id], "cells": cells,
                         "options": _shared(config, area_id)})
        else:
            rows.append({"kind": "room", "area_id": None, "name": "No room", "cells": None, "options": []})
        for window in sorted(groups[area_id], key=lambda w: w.name.casefold()):
            cells = {}
            for mode in MODES:
                eff = effective(config, window, mode)
                own = config.assignment(mode, window.window_id, None)
                combined = eff.has_target and eff.preset is not None and eff.preset.preset_id.startswith("mix:")
                cells[mode] = {"label": "Per cover" if combined else eff.label if eff.state != "nothing" else "—",
                               "state": eff.state, "source": eff.source, "source_word": eff.source_word,
                               "fallback": eff.fallback, "text": describe(eff, mode),
                               "global": own.preset_id if own else None, "override": window.conditions.get(mode),
                               "covers_assigned": any(a.window_id == window.window_id and a.layer_id
                                                      for a in config.modes.get(mode, ())),
                               "problems": list(eff.problems)}
            rows.append({"kind": "window", "window_id": window.window_id, "layer_id": None, "name": window.name,
                         "mode": window.mode, "type": _target_type(window, None), "cells": cells,
                         "options": _options(window, None)})
            if not window.layered:
                continue
            for layer in window.layers:
                cells = {}
                for mode in MODES:
                    t = resolve_layer(config, window, layer, mode)
                    own = config.assignment(mode, window.window_id, layer.layer_id)
                    label = {"preset": t.preset_name, "no_action": "No action", "missing": "Missing preset",
                             "nothing": "—"}[t.state]
                    if t.state == "preset" and t.library == "window":
                        label = f"{t.position} %" if t.position is not None else "unchanged"
                    cells[mode] = {"label": label, "state": t.state, "source": t.source,
                                   "source_word": SOURCE_WORDS.get(t.source, ""), "inherited": t.library == "window",
                                   "global": own.preset_id if own else None,
                                   "override": window.cover_modes.get(layer.layer_id, {}).get(mode)}
                rows.append({"kind": "cover", "window_id": window.window_id, "layer_id": layer.layer_id,
                             "name": layer_name(layer), "window_name": window.name, "type": "cover", "cells": cells,
                             "options": _options(window, layer)})
    return rows


def _recent(mgr: CurtainManager) -> list[dict[str, Any]]:
    """The newest decision-log lines of every window (each says where its preset came from)."""
    items = []
    for window_id, rt in mgr.auto.runtimes.items():
        window = mgr.config.windows.get(window_id)
        if window is None:
            continue
        for entry in rt.log[:LOG_ROWS]:
            items.append({"at": entry.get("at"), "action": entry.get("action"), "word": entry.get("word"),
                          "window": window.name, "summary": entry.get("summary") or entry.get("text"),
                          "source_word": entry.get("source_word")})
    items.sort(key=lambda x: x["at"] or "", reverse=True)
    return items[:LOG_ROWS]


# ================================================================== changes
def _mode(op: Mapping[str, Any]) -> str:
    mode = op.get("mode")
    if mode not in MODES:
        raise CurtainConfigError("invalid_mode", "Choose one of the curtain modes")
    return mode


def _target(config: CurtainConfig, op: Mapping[str, Any]) -> tuple[Window, CoverLayer | None]:
    window = config.windows.get(op.get("window_id") or "")
    if window is None:
        raise CurtainConfigError("not_found", "This window does not exist")
    layer_id = op.get("layer_id") or None
    if layer_id is None:
        return window, None
    layer = window.layer(layer_id)
    if layer is None:
        raise CurtainConfigError("unknown_layer", f"{layer_id} is not a cover of {window.name}")
    if not window.layered:
        raise CurtainConfigError("single_cover", f"{window.name} has one cover: assign the window itself")
    return window, layer


def _physical(hass: HomeAssistant, window: Window, layer: CoverLayer | None) -> set[str]:
    out: set[str] = set()
    for x in [layer] if layer is not None else window.layers:
        out |= members(hass, x.entity_id) | {x.entity_id}
    return out


def _assign(hass: HomeAssistant, config: CurtainConfig, op: Mapping[str, Any]) -> tuple[CurtainConfig, str]:
    mode = _mode(op)
    window, layer = _target(config, op)
    preset_id = op.get("preset_id")
    preset = layer.preset(preset_id) if layer is not None else window.preset(preset_id)
    if preset is None:
        raise CurtainConfigError("unknown_preset", f"{_name(window, layer)} has no such preset: choose one of its own")
    items = list(config.modes.get(mode, ()))
    lid = layer.layer_id if layer else None
    clash = [a for a in overlapping(items, window.window_id, lid) if a.target != (window.window_id, lid)]
    if clash:
        if not op.get("replace"):
            other = clash[0]
            what = "the whole window" if other.layer_id is None else \
                f"its cover {layer_name(window.layer(other.layer_id))}"
            raise CurtainConfigError(
                "overlap", f"{window.name} is already in {MODE_WORDS[mode]} as {what}. A window and one of its covers "
                "cannot both be assigned in one mode: replace it, or remove it first")
        items = [a for a in items if a not in clash]
    # The same physical cover can never be controlled twice in one mode (also not through a cover group).
    mine = _physical(hass, window, layer)
    for a in items:
        if a.window_id == window.window_id:
            continue
        other = config.windows.get(a.window_id)
        if other is None:
            continue
        if both := mine & _physical(hass, other, other.layer(a.layer_id) if a.layer_id else None):
            raise CurtainConfigError("duplicate_cover", f"{sorted(both)[0]} is already controlled in "
                                     f"{MODE_WORDS[mode]} through {other.name}")
    new = Assignment(window.window_id, lid, preset.preset_id)
    exact = next((i for i, a in enumerate(items) if a.target == new.target), None)
    if exact is None:
        items.append(new)
    else:
        items[exact] = new
    return config.with_modes({**config.modes, mode: items}), \
        f"{MODE_WORDS[mode]}: {_name(window, layer)} → {preset.name} (Global Modes)"


def _unassign(config: CurtainConfig, op: Mapping[str, Any]) -> tuple[CurtainConfig, str]:
    mode = _mode(op)
    window_id, layer_id = op.get("window_id"), op.get("layer_id") or None
    items = [a for a in config.modes.get(mode, ()) if a.target != (window_id, layer_id)]
    window = config.windows.get(window_id or "")
    layer = window.layer(layer_id) if window and layer_id else None
    name = _name(window, layer) if window else str(window_id)
    return config.with_modes({**config.modes, mode: items}), f"{MODE_WORDS[mode]}: {name} removed (no assignment)"


def _set(hass: HomeAssistant, config: CurtainConfig, op: Mapping[str, Any]) -> tuple[CurtainConfig, str]:
    mode = _mode(op)
    level = op.get("level")
    value = op.get("value") or None
    word = "Use Global Setting" if value is None else "No action" if value == NO_ACTION else None
    if level == "room":
        area_id = op.get("area_id")
        if not area_id or ar.async_get(hass).async_get_area(area_id) is None:
            raise CurtainConfigError("unknown_area", "Choose an existing room (area)")
        if value not in (None, NO_ACTION) and not any(w.preset(value) for w in _room_windows(config, area_id)):
            raise CurtainConfigError("unknown_preset", "No window in this room has that preset")
        current = {k: v for k, v in config.room_modes.get(area_id, {}).items() if k != mode}
        if value:
            current[mode] = value
        rooms = {k: v for k, v in config.room_modes.items() if k != area_id}
        if current:
            rooms[area_id] = current
        label = word or _shared_name(config, area_id, value)
        return replace(config, room_modes=rooms), \
            f"{MODE_WORDS[mode]}: room {ar.async_get(hass).async_get_area(area_id).name} → {label} (room override)"
    if level not in ("window", "cover"):
        raise CurtainConfigError("invalid_level", "Overrides are set for a room, a window or a cover")
    window, layer = _target(config, op)
    if level == "cover":
        if layer is None:
            raise CurtainConfigError("unknown_layer", "Choose one of the window's covers")
        if value not in (None, NO_ACTION) and layer.preset(value) is None:
            raise CurtainConfigError("unknown_preset", f"{layer_name(layer)} has no such preset")
        own = {k: v for k, v in window.cover_modes.get(layer.layer_id, {}).items() if k != mode}
        if value:
            own[mode] = value
        cover_modes = {k: v for k, v in window.cover_modes.items() if k != layer.layer_id}
        if own:
            cover_modes[layer.layer_id] = own
        window = replace(window, cover_modes=cover_modes)
        label = word or layer.preset(value).name
        return config.with_window(window), f"{MODE_WORDS[mode]}: {_name(window, layer)} → {label} (cover override)"
    if value not in (None, NO_ACTION) and window.preset(value) is None:
        raise CurtainConfigError("unknown_preset", f"{window.name} has no such preset")
    window = replace(window, conditions={**window.conditions, mode: value})
    label = word or window.preset(value).name
    return config.with_window(window), f"{MODE_WORDS[mode]}: {window.name} → {label} (window override)"


def _adopt(config: CurtainConfig, op: Mapping[str, Any]) -> tuple[CurtainConfig, str]:
    """Move a window's overrides into Global Modes (for all modes, or the ones listed)."""
    window, _ = _target(config, {"window_id": op.get("window_id")})
    only = op.get("modes")
    chosen = [m for m in MODES if not only or m in only]
    modes = {m: list(v) for m, v in config.modes.items()}
    conditions = dict(window.conditions)
    cover_modes = {k: dict(v) for k, v in window.cover_modes.items()}
    moved = cleared = 0
    for mode in chosen:
        items = modes.setdefault(mode, [])
        value = conditions.get(mode)
        if value:
            if value != NO_ACTION and window.preset(value) is not None and not overlapping(items, window.window_id,
                                                                                            None):
                items.append(Assignment(window.window_id, None, value))
                moved += 1
            else:
                cleared += 1
            conditions[mode] = None
        for layer in window.layers if window.layered else ():
            value = cover_modes.get(layer.layer_id, {}).pop(mode, None)
            if not value:
                continue
            if value != NO_ACTION and layer.preset(value) is not None and not overlapping(
                    items, window.window_id, layer.layer_id):
                items.append(Assignment(window.window_id, layer.layer_id, value))
                moved += 1
            else:
                cleared += 1
    window = replace(window, conditions=conditions, cover_modes={k: v for k, v in cover_modes.items() if v})
    new = config.with_window(window).with_modes(modes)
    return new, (f"{window.name}: {moved} override{'s' if moved != 1 else ''} moved to Global Modes"
                 + (f", {cleared} cleared (Global Modes applies)" if cleared else ""))


def apply_ops(hass: HomeAssistant, config: CurtainConfig, ops: list[Any]) -> tuple[CurtainConfig, list[str]]:
    """Every operation in order on a copy; one invalid operation refuses them all."""
    if not isinstance(ops, list) or not ops or len(ops) > 400:
        raise CurtainConfigError("invalid_ops", "Nothing to change")
    lines: list[str] = []
    for op in ops:
        if not isinstance(op, Mapping):
            raise CurtainConfigError("invalid_ops", "Invalid change")
        kind = op.get("op")
        if kind == "assign":
            config, line = _assign(hass, config, op)
        elif kind == "unassign":
            config, line = _unassign(config, op)
        elif kind == "set":
            config, line = _set(hass, config, op)
        elif kind == "adopt":
            config, line = _adopt(config, op)
        else:
            raise CurtainConfigError("invalid_ops", f"Unknown change {kind}")
        lines.append(line)
    return config, lines


def _positions(eff) -> dict[str, int | None]:
    return dict(eff.preset.positions) if eff.has_target and eff.preset is not None else {}


def impact(hass: HomeAssistant, mgr: CurtainManager, old: CurtainConfig, new: CurtainConfig) -> list[dict[str, Any]]:
    """Per window whose effective mapping changes: what changes, and what it would do now. Nothing moves."""
    out = []
    for window in new.windows.values():
        before_window = old.windows.get(window.window_id, window)
        changes = []
        for mode in MODES:
            a, b = effective(old, before_window, mode), effective(new, window, mode)
            if (a.state, _positions(a), a.source, a.preset_id) == (b.state, _positions(b), b.source, b.preset_id):
                continue
            changes.append({"mode": mode, "word": MODE_WORDS[mode], "before": describe(a, mode),
                            "after": describe(b, mode), "targets_change": (a.state, _positions(a)) != (b.state,
                                                                                                       _positions(b))})
        if not changes:
            continue
        rt = mgr.auto.runtimes.get(window.window_id)
        current = rt.decision if rt else None
        entry: dict[str, Any] = {
            "window_id": window.window_id, "name": window.name, "room": area_name(hass, window.area_id) or "No room",
            "mode": window.mode, "changes": changes,
            "condition": current.condition if current else None,
            "condition_word": MODE_WORDS.get(current.condition, current.condition) if current else None,
            "before": current.preset_name if current else None, "after": None, "active": False, "moves": 0,
            "items": [], "hold": bool(rt and rt.hold), "status": rt.status.get("text") if rt and rt.status else None,
            "blocked": rt.status.get("code") if rt and rt.status.get("action") == "blocked" else None,
            "conflicts": [c["name"] or c["automation"] for c in mgr.blocking_automations(window)],
            "needs_confirm": False,
        }
        if window.mode != "off" and mgr.auto.running:
            values, _ = new.rules_for(window)
            predicted = mgr.auto.simulate(window, values, new)
            entry["predicted"] = predicted.condition
            entry["predicted_word"] = MODE_WORDS.get(predicted.condition, predicted.condition)
            entry["after"] = predicted.preset_name
            before_key = (current.condition, _positions(current.target)) if current else None
            entry["active"] = before_key != (predicted.condition, _positions(predicted.target))
            if entry["active"] and predicted.target.has_target and predicted.condition not in ("manual_override",
                                                                                               "waiting"):
                plan = mgr.plan(window, predicted.target.preset)
                entry["items"] = plan["items"]
                entry["moves"] = plan["moves"]
            entry["needs_confirm"] = window.mode == "automatic" and entry["active"] and entry["moves"] > 0
        out.append(entry)
    out.sort(key=lambda e: (not e["needs_confirm"], e["room"].casefold(), e["name"].casefold()))
    return out


# ================================================================= commands
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/modes/change",
    vol.Required("ops"): list,
    vol.Optional("dry_run", default=False): bool,
    vol.Optional("confirm", default=False): bool,
    vol.Optional("policy", default="now"): vol.In(POLICIES),
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_modes_change(hass, mgr, msg, connection):
    """Validate (``dry_run``) or save Global Modes / override changes. Saving never moves a cover by itself:
    an Automatic window follows a changed active mapping only after ``confirm`` and with its policy."""
    old = mgr.config
    new, lines = apply_ops(hass, old, msg["ops"])
    found = impact(hass, mgr, old, new)
    needs = any(e["needs_confirm"] for e in found)
    result: dict[str, Any] = {"changes": lines, "impact": found, "needs_confirm": needs, "saved": False}
    if msg["dry_run"]:
        result["global_modes"] = modes_model(hass, mgr, new)
        return result
    if needs and not msg["confirm"]:
        raise CurtainConfigError("confirm_required", "This changes what an Automatic window does now: review the "
                                 "preview and confirm")
    now = dt_util.utcnow().isoformat(timespec="seconds")
    for entry in found:
        window = new.windows[entry["window_id"]]
        words = "; ".join(f"{c['word']}: {c['before']} → {c['after']}" for c in entry["changes"])
        defer = entry["needs_confirm"] and msg["policy"] == "next_change"
        mgr.auto.config_changed(window, f"Mapping changed by an administrator ({words})"
                                + (". Applies at the next change of condition" if defer else ""),
                                {"condition": entry.get("predicted"), "at": now} if defer else None)
    mgr.async_set(new)
    result["saved"] = True
    return result


def _layer(window: Window, layer_id: str) -> CoverLayer:
    layer = window.layer(layer_id)
    if layer is None:
        raise CurtainConfigError("unknown_layer", "This cover is not part of the window")
    return layer


def _with_layer(window: Window, layer: CoverLayer) -> Window:
    return replace(window, layers=tuple(layer if x.layer_id == layer.layer_id else x for x in window.layers))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/cover_preset/save",
    vol.Required("window_id"): str,
    vol.Required("layer_id"): str,
    vol.Optional("preset_id"): vol.Any(str, None),
    vol.Optional("name"): str,
    vol.Optional("position"): vol.Any(int, float),
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_cover_preset_save(hass, mgr, msg, connection):
    """Create (no preset_id) or edit a preset of one cover: its name and position. Moves nothing; every
    assignment that refers to it follows the new position (it refers to the id)."""
    window = _window(mgr, msg["window_id"])
    layer = _layer(window, msg["layer_id"])
    preset_id = msg.get("preset_id")
    current = layer.preset(preset_id) if preset_id else None
    if preset_id and current is None:
        raise CurtainConfigError("not_found", "This preset does not exist")
    name = clean_name(msg["name"], "The preset's name") if "name" in msg else current.name if current else None
    if name is None:
        raise CurtainConfigError("invalid_name", "The preset's name must not be empty")
    if any(p.name.casefold() == name.casefold() and p.preset_id != preset_id for p in layer.presets):
        raise CurtainConfigError("duplicate_name", f"This cover already has a preset called {name}")
    position = valid_position(msg["position"]) if "position" in msg else current.position if current else None
    if position is None:
        raise CurtainConfigError("invalid_position", "Positions must be whole numbers from 0 to 100 %")
    if current is None:
        if len(layer.presets) >= MAX_COVER_PRESETS:
            raise CurtainConfigError("too_many_presets", f"A cover can have at most {MAX_COVER_PRESETS} presets")
        preset_id = new_id("c_", (p.preset_id for p in layer.presets))
        presets = (*layer.presets, CoverPreset(preset_id, name, position))
    else:
        presets = tuple(CoverPreset(preset_id, name, position) if p.preset_id == preset_id else p
                        for p in layer.presets)
    mgr.async_set(mgr.config.with_window(_with_layer(window, replace(layer, presets=presets))))
    return {"preset_id": preset_id}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/cover_preset/delete",
    vol.Required("window_id"): str,
    vol.Required("layer_id"): str,
    vol.Required("preset_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_cover_preset_delete(hass, mgr, msg, connection):
    """Delete a cover preset: cover overrides using it inherit again, Global Modes assignments using it go."""
    window = _window(mgr, msg["window_id"])
    layer = _layer(window, msg["layer_id"])
    if layer.preset(msg["preset_id"]) is None:
        raise CurtainConfigError("not_found", "This preset does not exist")
    layer = replace(layer, presets=tuple(p for p in layer.presets if p.preset_id != msg["preset_id"]))
    own = {m: v for m, v in window.cover_modes.get(layer.layer_id, {}).items() if v != msg["preset_id"]}
    cover_modes = {k: v for k, v in window.cover_modes.items() if k != layer.layer_id}
    if own:
        cover_modes[layer.layer_id] = own
    window = replace(_with_layer(window, layer), cover_modes=cover_modes)
    mgr.async_set(mgr.config.with_window(window))


MODE_HANDLERS = (ws_modes_change, ws_cover_preset_save, ws_cover_preset_delete)
