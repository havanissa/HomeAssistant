"""Centralizing curtain configuration (0.6.6, the Phase 4.6.4 addendum): the reviewed migration.

From 0.6.6 on, **Global Schedule** decides when Morning, Daytime, Evening and
Night happen and **Global Modes** decides which preset each window or cover
uses. A store written by 0.6.5 or earlier may still hold the old per-place
settings:

* window overrides (``Window.conditions``, the 0.6.3 mappings), cover
  overrides (``Window.cover_modes``) and room overrides
  (``CurtainConfig.room_modes``);
* room / window schedule values (the ``schedule`` settings of ``rules.py``).

They stay active, unchanged, until an administrator reviews and applies this
migration (``CurtainConfig.centralized`` is False until then); none can be
created any more. The plan:

1. every window and mode with an old mapping gets Global Modes assignments
   that give **exactly the same position per cover** (a whole-window preset
   when one matches every cover, otherwise single-cover assignments; a cover
   preset with the needed position is created when the cover has none). Where
   a Global Modes assignment was hidden by an old override, the review asks
   which one to keep (default: what the window does now);
2. a window whose Morning had no preset used to start its Daytime preset at
   the first daylight; with the Global Schedule it keeps its position during
   Morning. The default keeps the old behaviour by assigning its Daytime
   preset to Morning; "exclude" keeps it unchanged during Morning;
3. a Glare / Excess heat "No action" cannot be expressed globally (nothing
   assigned there uses the Sun Protection preset): it must be acknowledged;
4. a room / window schedule value that differs from the Global Schedule needs
   a deliberate choice: follow the Global Schedule, or make it the Global
   Schedule value for every window. Equal values are simply removed.

Applying checks that every converted mapping resolves to the same positions as
before and only then removes the old settings. Nothing here sends a command.
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

from . import rules as curtain_rules
from .config import (
    MAX_COVER_PRESETS,
    MODE_WORDS,
    MODES,
    NO_ACTION,
    SUN_FALLBACK,
    Assignment,
    CoverPreset,
    CurtainConfig,
    CurtainConfigError,
    Window,
    new_id,
)
from .modes import effective, layer_name, resolve_layer

CHOICE_WORDS = {
    "keep": "Keep what the window does now", "global": "Use the Global Modes assignment",
    "exclude": "Leave it unchanged during Morning", "accept": "Use the Sun Protection preset (fallback)",
    "follow": "Follow the Global Schedule", "adopt": "Make it the Global Schedule value for every window",
}


def _targets(config: CurtainConfig, window: Window, mode: str, legacy: bool) -> dict[str, int | None]:
    out = {}
    for layer in window.layers:
        t = resolve_layer(config, window, layer, mode, legacy=legacy)
        out[layer.layer_id] = t.position if t.state == "preset" else None
    return out


def _legacy_here(config: CurtainConfig, window: Window, mode: str) -> bool:
    if window.conditions.get(mode):
        return True
    if any(window.cover_modes.get(x.layer_id, {}).get(mode) for x in window.layers):
        return True
    return bool(window.area_id and config.room_modes.get(window.area_id, {}).get(mode))


def _text(window: Window, targets: Mapping[str, int | None]) -> str:
    parts = [f"{layer_name(x)} {targets[x.layer_id]} %" for x in window.layers if targets.get(x.layer_id) is not None]
    return ", ".join(parts) if parts else "unchanged"


def pending(config: CurtainConfig) -> bool:
    return not config.centralized


def plan(config: CurtainConfig, room_names: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """What the migration would do, item by item (empty when there is nothing to migrate)."""
    if config.centralized:
        return []
    names = room_names or {}
    items: list[dict[str, Any]] = []
    for window in config.windows.values():
        values, _ = config.rules_for(window)
        for mode in MODES:
            word = MODE_WORDS[mode]
            old = _targets(config, window, mode, legacy=True)
            glob = _targets(config, window, mode, legacy=False)
            if _legacy_here(config, window, mode):
                assigned = any(a.window_id == window.window_id for a in config.modes.get(mode, ()))
                base = {"window_id": window.window_id, "mode": mode, "window": window.name, "mode_word": word}
                if assigned and old != glob:
                    items.append({**base, "id": f"conflict:{window.window_id}:{mode}", "kind": "conflict",
                                  "title": f"{word}: {window.name}",
                                  "detail": f"An old override sets {_text(window, old)}; Global Modes says "
                                            f"{_text(window, glob)} but was hidden",
                                  "choices": ["keep", "global"], "default": "keep", "required": False})
                else:
                    items.append({**base, "id": f"mapping:{window.window_id}:{mode}", "kind": "mapping",
                                  "title": f"{word}: {window.name}",
                                  "detail": f"Moves to Global Modes: {_text(window, old)}", "choices": [],
                                  "default": None, "required": False})
                if mode in SUN_FALLBACK:
                    sun = _targets(config, window, "sun", legacy=True)
                    lost = [x for x in window.layers
                            if resolve_layer(config, window, x, mode, legacy=True).state in ("no_action", "missing")
                            and sun.get(x.layer_id) is not None]
                    if lost:
                        items.append({**base, "id": f"inexact:{window.window_id}:{mode}", "kind": "inexact",
                                      "title": f"{word}: {window.name}",
                                      "detail": f"“No action” for {', '.join(layer_name(x) for x in lost)} cannot be "
                                                "kept: without an assignment the Sun Protection preset is used",
                                      "choices": ["accept"], "default": None, "required": True})
        old_morning = effective(config, window, "morning", legacy=True)
        old_day = effective(config, window, "day", legacy=True)
        if values["morning_enabled"] and old_morning.state == "nothing" and old_day.has_target:
            items.append({"id": f"morning:{window.window_id}", "kind": "morning", "window_id": window.window_id,
                          "mode": "morning", "window": window.name, "mode_word": "Morning",
                          "title": f"Morning: {window.name}",
                          "detail": "No Morning preset: it used to start its Daytime preset at the first daylight. "
                                    "With the Global Schedule a window without a Morning preset keeps its position "
                                    "during Morning",
                          "choices": ["keep", "exclude"], "default": "keep", "required": False})
    # Old room / window schedules
    global_values = curtain_rules.resolve(config.defaults, None, {})[0]
    places: list[tuple[str, str, str, Mapping[str, Any]]] = [
        ("room", area_id, names.get(area_id, area_id), room) for area_id, room in config.rooms.items()]
    places += [("window", w.window_id, w.name, w.auto) for w in config.windows.values()]
    for scope, ident, name, layer in places:
        for key in sorted(k for k in layer if k in curtain_rules.SCHEDULE_KEYS):
            label = curtain_rules.BY_KEY[key].label
            value, glob = layer[key], global_values[key]
            item = {"id": f"schedule:{scope}:{ident}:{key}", "kind": "schedule", "scope": scope, "target": ident,
                    "key": key, "value": curtain_rules.dump({key: value})[key], "title": f"{label}: {name}",
                    "window": name}
            if value == glob:
                items.append({**item, "detail": f"Same as the Global Schedule ({_value(key, value)}): removed",
                              "choices": [], "default": None, "required": False})
            else:
                items.append({**item, "detail": f"This {scope} uses {_value(key, value)}; the Global Schedule says "
                                                f"{_value(key, glob)}",
                              "choices": ["follow", "adopt"], "default": None, "required": True})
    return items


def _value(key: str, value: Any) -> str:
    from .schedule_page import _text as text

    return text(key, value)


def _convert(window: Window, targets: Mapping[str, int | None], hint: str | None
             ) -> tuple[Window, list[Assignment], list[str]]:
    """Global Modes assignments with exactly these positions (creating a cover preset where needed)."""
    if all(v is None for v in targets.values()):
        return window, [], []
    ids = [x.layer_id for x in window.layers]
    ordered = sorted(window.presets, key=lambda p: p.preset_id != hint)
    for p in ordered:
        if all(p.positions.get(i) == targets.get(i) for i in ids):
            return window, [Assignment(window.window_id, None, p.preset_id)], []
    if not window.layered:
        raise CurtainConfigError("migration_failed", f"{window.name}: no preset gives the current position")
    source = window.preset(hint)
    layers = list(window.layers)
    out: list[Assignment] = []
    notes: list[str] = []
    for i, layer in enumerate(layers):
        position = targets.get(layer.layer_id)
        if position is None:
            continue
        cp = next((c for c in layer.presets if c.position == position), None)
        if cp is None:
            if len(layer.presets) >= MAX_COVER_PRESETS:
                raise CurtainConfigError("migration_failed", f"{window.name}: {layer_name(layer)} has no room for "
                                         "another preset")
            name = source.name if source else f"{position} %"
            taken = {c.name.casefold() for c in layer.presets}
            if name.casefold() in taken:
                name = f"{name} ({position} %)"
            cp = CoverPreset(new_id("c_", (c.preset_id for c in layer.presets)), name[:60], position)
            layer = replace(layer, presets=(*layer.presets, cp))
            layers[i] = layer
            notes.append(f"{window.name}: new {layer_name(layer)} preset “{cp.name}” ({position} %)")
        out.append(Assignment(window.window_id, layer.layer_id, cp.preset_id))
    return replace(window, layers=tuple(layers)), out, notes


def apply(config: CurtainConfig, choices: Mapping[str, str], room_names: Mapping[str, str] | None = None
          ) -> tuple[CurtainConfig, list[str], list[dict[str, Any]]]:
    """The centralized configuration after the reviewed choices. Refuses missing choices and any conversion
    that would not give the windows the same positions as before."""
    items = plan(config, room_names)
    missing = [x for x in items if x["required"] and choices.get(x["id"]) not in x["choices"]]
    if missing:
        raise CurtainConfigError("choice_required", f"Choose what happens with: {missing[0]['title']}")
    chosen = {x["id"]: choices.get(x["id"], x["default"]) for x in items}
    windows = dict(config.windows)
    modes = {m: list(v) for m, v in config.modes.items()}
    lines: list[str] = []
    expected: list[tuple[str, str, dict[str, int | None]]] = []
    for window in config.windows.values():
        current = windows[window.window_id]
        for mode in MODES:
            targets = None
            if _legacy_here(config, window, mode):
                if chosen.get(f"conflict:{window.window_id}:{mode}") == "global":
                    lines.append(f"{MODE_WORDS[mode]}: {window.name} uses its Global Modes assignment")
                    continue
                targets = _targets(config, window, mode, legacy=True)
                hint = window.conditions.get(mode)
            elif mode == "morning" and chosen.get(f"morning:{window.window_id}") == "keep":
                targets = _targets(config, window, "day", legacy=True)
                hint = (next((a.preset_id for a in config.modes.get("day", ())
                              if a.window_id == window.window_id and a.layer_id is None), None)
                        or window.conditions.get("day"))
            if targets is None:
                continue
            current, assignments, notes = _convert(current, targets, hint if hint != NO_ACTION else None)
            modes[mode] = [a for a in modes.get(mode, []) if a.window_id != window.window_id] + assignments
            lines += notes
            lines.append(f"{MODE_WORDS[mode]}: {window.name} → {_text(window, targets)} (Global Modes)"
                         if assignments else f"{MODE_WORDS[mode]}: {window.name} → no assignment (unchanged)")
            expected.append((window.window_id, mode, targets))
        windows[window.window_id] = current
    # Old room / window schedules
    defaults = dict(config.defaults)
    adopted: dict[str, Any] = {}
    for item in items:
        if item["kind"] != "schedule":
            continue
        label = curtain_rules.BY_KEY[item["key"]].label
        if chosen.get(item["id"]) == "adopt":
            value = curtain_rules.validate(item["key"], item["value"], curtain_rules.GLOBAL)
            if item["key"] in adopted and adopted[item["key"]] != value:
                raise CurtainConfigError("choice_required", f"{label}: choose one value for every window")
            adopted[item["key"]] = defaults[item["key"]] = value
            lines.append(f"Global Schedule: {label} → {_value(item['key'], value)} (from {item['window']})")
        elif item["choices"]:
            lines.append(f"{item['window']}: {label} follows the Global Schedule")
    rooms = {k: kept for k, v in config.rooms.items()
             if (kept := {x: y for x, y in v.items() if x not in curtain_rules.SCHEDULE_KEYS})}
    windows = {k: replace(w, conditions={}, cover_modes={},
                          auto={x: y for x, y in w.auto.items() if x not in curtain_rules.SCHEDULE_KEYS})
               for k, w in windows.items()}
    new = replace(config, windows=windows, rooms=rooms, defaults=defaults, room_modes={}, centralized=True,
                  modes={m: tuple(v) for m, v in modes.items() if v})
    for window_id, mode, targets in expected:
        got = _targets(new, new.windows[window_id], mode, legacy=False)
        if got != targets:
            raise CurtainConfigError("migration_failed", f"{MODE_WORDS[mode]} of {new.windows[window_id].name} "
                                     "would change; nothing was migrated")
    lines.append("Old window, cover and room overrides and room / window schedules are removed")
    return new, lines, items


# ================================================================= command
def _impact(hass: HomeAssistant, mgr, new: CurtainConfig) -> list[dict[str, Any]]:
    from .discovery import area_name
    from .global_modes import _positions

    out = []
    for window in new.windows.values():
        rt = mgr.auto.runtimes.get(window.window_id)
        current = rt.decision if rt else None
        if window.mode == "off" or not mgr.auto.running or current is None:
            continue
        predicted = mgr.auto.simulate(window, new.rules_for(window)[0], new)
        if (current.condition, _positions(current.target)) == (predicted.condition, _positions(predicted.target)):
            continue
        entry = {"window_id": window.window_id, "name": window.name, "room": area_name(hass, window.area_id) or
                 "No room", "mode": window.mode,
                 "changes": [{"mode": "migration", "word": "Now",
                              "before": f"{MODE_WORDS.get(current.condition, current.condition)}: "
                                        f"{current.preset_name or 'keeps the position'}",
                              "after": f"{MODE_WORDS.get(predicted.condition, predicted.condition)}: "
                                       f"{predicted.preset_name or 'keeps the position'}", "targets_change": True}],
                 "condition": current.condition,
                 "condition_word": MODE_WORDS.get(current.condition, current.condition),
                 "before": current.preset_name, "after": predicted.preset_name, "predicted": predicted.condition,
                 "predicted_word": MODE_WORDS.get(predicted.condition, predicted.condition), "active": True,
                 "moves": 0, "items": [], "hold": bool(rt and rt.hold),
                 "status": rt.status.get("text") if rt and rt.status else None,
                 "blocked": rt.status.get("code") if rt and rt.status.get("action") == "blocked" else None,
                 "conflicts": [c["name"] or c["automation"] for c in mgr.blocking_automations(window)]}
        if predicted.target.has_target and predicted.condition not in ("manual_override", "waiting"):
            plan_ = mgr.plan(window, predicted.target.preset)
            entry["items"], entry["moves"] = plan_["items"], plan_["moves"]
        entry["needs_confirm"] = window.mode == "automatic" and entry["moves"] > 0
        out.append(entry)
    return out


def summary(hass: HomeAssistant, config: CurtainConfig) -> dict[str, Any]:
    names = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    items = plan(config, names)
    return {"pending": not config.centralized, "count": len(items),
            "required": sum(1 for x in items if x["required"]),
            "words": CHOICE_WORDS,
            "text": "Global Schedule and Global Modes are the only curtain configuration"
            if config.centralized else f"{len(items)} setting(s) from an earlier version are still active in rooms, "
            "windows or covers: review the migration"}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/migration/change",
    vol.Optional("choices", default={}): dict,
    vol.Optional("dry_run", default=False): bool,
    vol.Optional("confirm", default=False): bool,
    vol.Optional("policy", default="now"): vol.In(("now", "next_change")),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_migration_change(hass, connection, msg):
    """Review (``dry_run``) or apply the migration to Global Schedule and Global Modes."""
    from .websocket import _manager

    if (mgr := _manager(hass, connection, msg)) is None:
        return
    names = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    config = mgr.config
    items = plan(config, names)
    result: dict[str, Any] = {"pending": not config.centralized, "items": items, "words": CHOICE_WORDS,
                              "changes": [], "impact": [], "needs_confirm": False, "saved": False,
                              "missing": [x["id"] for x in items if x["required"] and
                                          msg["choices"].get(x["id"]) not in x["choices"]]}
    if config.centralized:
        connection.send_result(msg["id"], result)
        return
    try:
        if result["missing"] and msg["dry_run"]:
            new, lines = None, []
        else:
            new, lines, _ = apply(config, msg["choices"], names)
    except CurtainConfigError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    if new is not None:
        result["changes"] = lines
        result["impact"] = _impact(hass, mgr, new)
        result["needs_confirm"] = any(e["needs_confirm"] for e in result["impact"])
    if msg["dry_run"]:
        connection.send_result(msg["id"], result)
        return
    if result["needs_confirm"] and not msg["confirm"]:
        connection.send_error(msg["id"], "confirm_required", "The migration changes what an Automatic window does "
                              "now: review it and confirm")
        return
    stamp = dt_util.utcnow().isoformat(timespec="seconds")
    for entry in result["impact"]:
        defer = entry["needs_confirm"] and msg["policy"] == "next_change"
        mgr.auto.config_changed(new.windows[entry["window_id"]], "Configuration centralized (migration applied)"
                                + (". Applies at the next change of condition" if defer else ""),
                                {"condition": entry["predicted"], "at": stamp} if defer else None)
    for window in new.windows.values():
        if window.window_id not in {e["window_id"] for e in result["impact"]}:
            mgr.auto.config_changed(window, "Configuration centralized: this window now follows Global Schedule and "
                                    "Global Modes (same behaviour)", None)
    mgr.async_set(new)
    result["saved"] = True
    result["pending"] = False
    connection.send_result(msg["id"], result)


MIGRATION_HANDLERS = (ws_migration_change,)
