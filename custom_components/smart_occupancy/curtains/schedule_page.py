"""Global Curtain Schedule page (0.6.5, the user's Phase 4.6.4): model and the reviewed ``schedule/change``.

The page edits the schedule settings of ``rules.py`` (the ``schedule`` group plus
the sleep settings that decide when a Morning may start) for all windows, and
clears room / window schedule overrides ("Use Global Schedule"). Like
``modes/change`` it validates on a copy first (``dry_run``) and shows, before
anything is saved: the changes, the next transitions before and after, and per
affected window its current and predicted condition and preset, whether covers
would move now, a manual hold, a block or competing automations. Saving a
change that moves an Automatic window now needs ``confirm`` and a policy:
``now`` (through every normal safety gate) or ``next_change`` (kept until the
window's condition changes, e.g. at the next scheduled transition).
"""

from __future__ import annotations


from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from . import rules as curtain_rules
from .config import MODE_WORDS, CurtainConfig, CurtainConfigError
from .discovery import area_name
from .global_modes import POLICIES, _positions
from .manager import CurtainManager
from .websocket import _guard, _settings_change

SCHEDULE_KEYS = tuple(r.key for r in curtain_rules.RULES if r.group == "schedule")
SLEEP_KEYS = ("sleep_end", "sleep_require_daylight")
PAGE_KEYS = (*SCHEDULE_KEYS, *SLEEP_KEYS)
SCOPE_WORDS = {"global": "all windows", "room": "room", "window": "window"}


def _text(key: str, value: Any) -> str:
    rule = curtain_rules.BY_KEY[key]
    if value is None:
        return "—"
    if rule.kind == "bool":
        return "on" if value else "off"
    if rule.kind == "choice":
        return dict(rule.choices).get(value, str(value))
    if rule.kind == "days":
        return ", ".join(curtain_rules.WEEKDAYS[d] for d in value) or "none"
    if rule.kind == "dayplan":
        plan = curtain_rules.dump({key: value})[key] or {}
        return "; ".join(f"{curtain_rules.WEEKDAYS[int(d)]} " + ", ".join(f"{k} {v}" for k, v in t.items())
                         for d, t in sorted(plan.items())) or "none"
    return f"{value}{(' ' + rule.unit) if rule.unit and rule.kind in ('int', 'float') else ''}"


def schedule_model(hass: HomeAssistant, mgr: CurtainManager, config: CurtainConfig | None = None) -> dict[str, Any]:
    draft = config is not None
    config = config or mgr.config
    values, sources = curtain_rules.resolve(config.defaults, None, {})
    names = {a.id: a.name for a in ar.async_get(hass).async_list_areas()}
    overrides = []
    for area_id, room in config.rooms.items():
        if keys := [k for k in PAGE_KEYS if k in room]:
            overrides.append({"scope": "room", "area_id": area_id, "name": names.get(area_id, area_id),
                              "keys": [{"key": k, "label": curtain_rules.BY_KEY[k].label, "value": _text(k, room[k])}
                                       for k in keys]})
    for window in config.windows.values():
        if keys := [k for k in PAGE_KEYS if k in window.auto]:
            overrides.append({"scope": "window", "window_id": window.window_id, "name": window.name,
                              "keys": [{"key": k, "label": curtain_rules.BY_KEY[k].label,
                                        "value": _text(k, window.auto[k])} for k in keys]})
    warnings = list(dict.fromkeys(curtain_rules.consistency(values)))
    if values["evening_rule"] == "legacy":
        warnings.append("Evening uses the 0.6.4 behaviour (dark after 12:00, not kept through passing clouds). "
                        "Choose how Evening begins to use the Evening schedule.")
    if not values["lux_entity"]:
        warnings.append("No outdoor light sensor for all windows: Morning and Daytime never open a window "
                        + ("unless the sun is clearly up (fallback)." if values["fallback"] == "sun" else
                           "automatically (choose one in a window's Environment tab or allow the sun fallback)."))
    elif hass.states.get(values["lux_entity"]) is None:
        warnings.append(f"The outdoor light sensor {values['lux_entity']} does not exist: nothing opens automatically.")
    return {
        "house": mgr.auto.house_model(config if draft else None),
        "values": curtain_rules.dump({k: values[k] for k in PAGE_KEYS}),
        "sources": {k: sources[k] for k in PAGE_KEYS},
        "keys": list(SCHEDULE_KEYS), "sleep_keys": list(SLEEP_KEYS),
        "lux_entity": values["lux_entity"],
        "overrides": overrides,
        "implementation": "Global Curtain Schedule (0.6.5)" + (
            "; Evening: 0.6.4 behaviour" if values["evening_rule"] == "legacy" else ""),
        "warnings": warnings,
        "time_zone": hass.config.time_zone,
    }


def schedule_impact(hass: HomeAssistant, mgr: CurtainManager, old: CurtainConfig, new: CurtainConfig
                    ) -> list[dict[str, Any]]:
    """Every window whose schedule settings change: what it does now and what it would do. Nothing moves."""
    out = []
    for window in new.windows.values():
        before_values, _ = old.rules_for(old.windows.get(window.window_id, window))
        after_values, _ = new.rules_for(window)
        if all(before_values[k] == after_values[k] for k in PAGE_KEYS):
            continue
        rt = mgr.auto.runtimes.get(window.window_id)
        current = rt.decision if rt else None
        entry: dict[str, Any] = {
            "window_id": window.window_id, "name": window.name, "room": area_name(hass, window.area_id) or "No room",
            "mode": window.mode, "changes": [],
            "condition": current.condition if current else None,
            "condition_word": MODE_WORDS.get(current.condition, current.condition) if current else None,
            "before": current.preset_name if current else None, "after": None, "active": False, "moves": 0,
            "items": [], "hold": bool(rt and rt.hold), "status": rt.status.get("text") if rt and rt.status else None,
            "blocked": rt.status.get("code") if rt and rt.status.get("action") == "blocked" else None,
            "conflicts": [c["name"] or c["automation"] for c in mgr.blocking_automations(window)],
            "needs_confirm": False,
        }
        if window.mode != "off" and mgr.auto.running:
            predicted = mgr.auto.simulate(window, after_values, new)
            entry.update(predicted=predicted.condition,
                         predicted_word=MODE_WORDS.get(predicted.condition, predicted.condition),
                         after=predicted.preset_name)
            before_key = (current.condition, _positions(current.target)) if current else None
            entry["active"] = before_key != (predicted.condition, _positions(predicted.target))
            now_word = f"{entry['condition_word'] or '—'}: {entry['before'] or 'keeps the position'}"
            next_word = f"{entry['predicted_word']}: {entry['after'] or 'keeps the position'}"
            entry["changes"].append({"mode": "schedule", "word": "Now", "before": now_word,
                                     "after": next_word if entry["active"] else f"{now_word} (unchanged)",
                                     "targets_change": entry["active"]})
            if entry["active"] and predicted.target.has_target and predicted.condition not in ("manual_override",
                                                                                               "waiting"):
                plan = mgr.plan(window, predicted.target.preset)
                entry["items"], entry["moves"] = plan["items"], plan["moves"]
            entry["needs_confirm"] = window.mode == "automatic" and entry["active"] and entry["moves"] > 0
        else:
            entry["changes"].append({"mode": "schedule", "word": "Now", "before": "Off", "after": "Off",
                                     "targets_change": False})
        out.append(entry)
    out.sort(key=lambda e: (not e["needs_confirm"], e["room"].casefold(), e["name"].casefold()))
    return out


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/curtains/schedule/change",
    vol.Required("scope"): vol.In(curtain_rules.ALL_SCOPES),
    vol.Optional("area_id"): str,
    vol.Optional("window_id"): str,
    vol.Required("values"): dict,
    vol.Optional("dry_run", default=False): bool,
    vol.Optional("confirm", default=False): bool,
    vol.Optional("policy", default="now"): vol.In(POLICIES),
})
@websocket_api.require_admin
@websocket_api.async_response
@_guard
def ws_schedule_change(hass, mgr, msg, connection):
    """Validate (``dry_run``) or save schedule settings for all windows, a room or a window (``null`` = use the
    less specific value again). Saving never moves a cover by itself; see the module docstring."""
    if not msg["values"]:
        raise CurtainConfigError("invalid_ops", "Nothing to change")
    if unknown := [k for k in msg["values"] if k not in PAGE_KEYS]:
        raise CurtainConfigError("unknown_setting", f"{unknown[0]} is not a schedule setting")
    old = mgr.config
    new, affected = _settings_change(hass, mgr, msg)
    for window in affected or [None]:
        values = new.rules_for(window)[0] if window else curtain_rules.resolve(new.defaults, None, {})[0]
        if problems := curtain_rules.consistency(values):
            raise CurtainConfigError("inconsistent", problems[0])
    scope = msg["scope"]
    where = SCOPE_WORDS[scope] if scope == "global" else (
        f"room {area_name(hass, msg.get('area_id'))}" if scope == "room" else
        f"window {new.windows[msg['window_id']].name}")
    old_layer = old.defaults if scope == "global" else old.rooms.get(msg.get("area_id") or "", {}) if \
        scope == "room" else old.windows[msg["window_id"]].auto
    new_layer = new.defaults if scope == "global" else new.rooms.get(msg.get("area_id") or "", {}) if \
        scope == "room" else new.windows[msg["window_id"]].auto
    lines = []
    for key in msg["values"]:
        label = curtain_rules.BY_KEY[key].label
        default = f"default ({_text(key, curtain_rules.BY_KEY[key].default)})"
        before = _text(key, old_layer[key]) if key in old_layer else default if scope == "global" else "inherited"
        after = _text(key, new_layer[key]) if key in new_layer else (
            "Use Global Schedule" if scope != "global" else default)
        if before != after:
            lines.append(f"{label} ({where}): {before} → {after}")
    found = schedule_impact(hass, mgr, old, new)
    needs = any(e["needs_confirm"] for e in found)
    now = dt_util.now()
    events = mgr.auto.sun_events()
    result: dict[str, Any] = {
        "changes": lines or ["No effective change"], "impact": found, "needs_confirm": needs, "saved": False,
        "transitions": {"before": _next(mgr, old, now, events), "after": _next(mgr, new, now, events)}}
    if msg["dry_run"]:
        result["schedule"] = schedule_model(hass, mgr, new)
        return result
    if needs and not msg["confirm"]:
        raise CurtainConfigError("confirm_required", "This changes what an Automatic window does now: review the "
                                 "preview and confirm")
    stamp = dt_util.utcnow().isoformat(timespec="seconds")
    for entry in found:
        defer = entry["needs_confirm"] and msg["policy"] == "next_change"
        mgr.auto.config_changed(new.windows[entry["window_id"]], "Schedule changed by an administrator ("
                                + "; ".join(lines) + ")" + (". Applies at the next change of condition" if defer else ""),
                                {"condition": entry.get("predicted"), "at": stamp} if defer else None)
    mgr.async_set(new)
    mgr.auto.evaluate_house()
    result["saved"] = True
    return result


def _next(mgr: CurtainManager, config: CurtainConfig, now, events) -> list[dict[str, Any]]:
    from .schedule import upcoming

    values = curtain_rules.resolve(config.defaults, None, {})[0]
    return upcoming(now, values, events, mgr.auto.house)[:6]


SCHEDULE_HANDLERS = (ws_schedule_change,)

