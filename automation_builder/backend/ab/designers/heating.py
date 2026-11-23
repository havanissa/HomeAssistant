"""The Heating Designer: ordinary thermostat automation, compiled to native Home Assistant automations.

    room -> thermostat (climate entity) -> heating templates -> configure -> deploy

No thermal model, no learning, no prediction, and no dependency on Smart Adaptive & Predictive Climate or any other
App: the designer decides *which setpoint applies now* from rules the user can read, and the automation sends it.

Heating profiles (``config["heating"]``, absent unless configured)::

    templates         {id: {name, version, modes [{id, name, temp}], frost}}   e.g. Comfort 21, Setback 18, Night 17, Away 16
    default_template  the template a new room profile starts with (optional)
    rooms             {area id: {template, version, overrides {mode id: cell}}}

Inheritance: template (global) → room profile → thermostat (the designer's own cells). A cell is a temperature,
NO ACTION (the thermostat is left as it is in that mode) or, on a thermostat, a preset of the device. Unset at every
level stays unset: that mode then decides nothing and the next layer in the priority decides.

The designer document (``device["designers"]``, kind ``heating``)::

    thermostats       the rows (climate entities, explicitly ticked)
    profile.room      the room profile the thermostats derive from (explicit; the designer never picks one)
    cells             {thermostat: {mode: cell}}   thermostat-specific overrides
    modes             which mode applies by time: a weekly schedule (one list, or weekdays + weekend; a slot is a time
                      and a mode or a temperature), day/night (two slots), or a helper whose state names the mode
    presence          a room or zone presence source: vacant for N minutes -> a mode (setback)
    window            window/door sensors: open for N seconds -> a mode, frost protection, OFF or NO ACTION;
                      unavailable -> an explicit fallback (ignore / as open / leave as it is)
    away              a person, tracker, group or helper: not home for N minutes -> Away; back home -> Return (optional)
    override          manual changes (by hand, on the device or in the app - not by an automation) suspend the
                      automatic setpoint: until the next scheduled change, for a time, until resumed, or until an event
    priority          the order of window, override, away, presence, schedule (the first that decides wins)
    when_off          a thermostat that is OFF: leave it off, or switch it to the chosen HVAC mode with the setpoint
    sensor            an optional room temperature sensor (shown in the preview; never assumed to steer the device)

Each thermostat gets its own automation (``queued``), re-evaluated whenever an input can change: one ``choose``
walks the layers in priority order and sets the wanted value; one comparing template turns it into exactly one
command, or none when the thermostat already is right (clamped to its minimum/maximum and rounded to its step, so
no identical setpoint is sent again). When a temporary layer ends (a window closes, the override ends, someone comes
home) the evaluation applies what the profile and the schedule say *now* - never a stale earlier target.
"""

from __future__ import annotations

from typing import Any
import re

from .. import model
from ..compiler import plain, quote
from ..model import Problems
from . import common as C, presence as P

KIND = "heating"
VERSION = 1
ROWS = "thermostats"
DEFAULT_NAME = "Heating"
EVENT = "automation_builder_heating"

TEMPLATE_ID = re.compile(r"^h_[0-9a-f]{8}$")
MODE_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
AREA_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
PRESET = re.compile(r"^[A-Za-z0-9_ \-]{1,40}$")
LAYERS = ("window", "override", "away", "presence", "schedule")
LAYER_NAMES = {"window": "Windows & doors", "override": "Manual override", "away": "Away / Home",
               "presence": "Presence", "schedule": "Schedule & modes"}
CELL_KINDS = ("set", "none", "preset")
WINDOW_ACTIONS = ("mode", "frost", "off", "none")
WINDOW_UNAVAILABLE = ("ignore", "open", "keep")
OVERRIDE_POLICIES = ("until_next", "duration", "until_resumed", "until_event")
HEAT_MODES = ("heat", "heat_cool", "auto")
MIN_T, MAX_T = 4, 35
MAX_MODES = 8
MAX_SLOTS = 12
MAX_THERMOSTATS = 8
MAX_SENSORS = 8
MAX_TEMPLATES = 8
MAX_ROOMS = 60
STEP_DEFAULT = 0.5
SUPPORT_TARGET, SUPPORT_RANGE, SUPPORT_PRESET = 1, 2, 16


# ================================================================== heating profiles
def _temp(raw: Any, path: str, p: Problems) -> Any:
    return C.number(raw, path, p, MIN_T, MAX_T)


def _version(raw: Any) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 10**6 else 1


def validate_template(template_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    modes, seen = [], set()
    items = raw.get("modes") if isinstance(raw.get("modes"), list) else []
    if not 1 <= len(items) <= MAX_MODES:
        p.add(f"{path}.modes", f"A heating template has 1 to {MAX_MODES} modes")
    for i, item in enumerate(items[:MAX_MODES]):
        item = item if isinstance(item, dict) else {}
        mode_id = item.get("id")
        if not isinstance(mode_id, str) or not MODE_ID.match(mode_id) or mode_id in ("off", "keep", "frost"):
            p.add(f"{path}.modes[{i}].id", "A mode's id is lower case letters, digits and _")
            mode_id = f"mode_{i}"
        if mode_id in seen:
            p.add(f"{path}.modes[{i}].id", "Two modes share an id")
        seen.add(mode_id)
        modes.append({"id": mode_id, "name": C.text(item.get("name"), f"{path}.modes[{i}].name", p, limit=40, required=True),
                      "temp": _temp(item.get("temp"), f"{path}.modes[{i}].temp", p)})
    return {"id": template_id, "name": C.text(raw.get("name"), f"{path}.name", p, limit=60, required=True),
            "version": _version(raw.get("version")), "modes": modes, "frost": _temp(raw.get("frost"), f"{path}.frost", p)}


def cell(raw: Any, path: str, p: Problems, *, preset: bool = False) -> dict[str, Any] | None:
    if raw is None:
        return None
    kinds = CELL_KINDS if preset else ("set", "none")
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "A cell is a temperature" + (", a preset" if preset else "") + " or NO ACTION")
        return None
    if raw["kind"] == "set":
        temp = _temp(raw.get("temp"), f"{path}.temp", p)
        if temp is None:
            p.add(f"{path}.temp", f"Give the temperature ({MIN_T}–{MAX_T} °)")
        return {"kind": "set", "temp": temp}
    if raw["kind"] == "preset":
        value = raw.get("preset")
        if not isinstance(value, str) or not PRESET.match(value):
            p.add(f"{path}.preset", "Choose one of the thermostat's presets")
            value = None
        return {"kind": "preset", "preset": value}
    return {"kind": "none"}


def _cells(raw: Any, path: str, p: Problems, *, preset: bool = False) -> dict[str, Any]:
    out = {}
    for mode, value in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(mode, str) and MODE_ID.match(mode):
            found = cell(value, f"{path}.{mode}", p, preset=preset)
            if found is not None:
                out[mode] = found
        else:
            p.add(f"{path}.{mode}", "Not a mode")
    return {k: out[k] for k in sorted(out)}


def validate_room(area_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    template = raw.get("template") if isinstance(raw.get("template"), str) and TEMPLATE_ID.match(raw["template"]) else None
    if template is None:
        p.add(f"{path}.template", "Choose the heating template of this room profile")
    return {"area_id": area_id, "version": _version(raw.get("version")), "template": template,
            "overrides": _cells(raw.get("overrides"), f"{path}.overrides", p)}


def validate_all(raw: Any, p: Problems) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    path = "heating"
    t_raw = raw.get("templates") if isinstance(raw.get("templates"), dict) else {}
    r_raw = raw.get("rooms") if isinstance(raw.get("rooms"), dict) else {}
    if len(t_raw) > MAX_TEMPLATES or len(r_raw) > MAX_ROOMS:
        p.add(path, f"At most {MAX_TEMPLATES} templates and {MAX_ROOMS} room profiles")
    templates = {}
    for tid in sorted(t_raw)[:MAX_TEMPLATES]:
        if not TEMPLATE_ID.match(str(tid)):
            p.add(f"{path}.templates.{tid}", "Invalid template id")
            continue
        templates[tid] = validate_template(tid, t_raw[tid], f"{path}.templates.{tid}", p)
    rooms = {}
    for area_id in sorted(r_raw)[:MAX_ROOMS]:
        if not isinstance(area_id, str) or not AREA_ID.match(area_id):
            p.add(f"{path}.rooms.{area_id}", "Invalid room")
            continue
        room = validate_room(area_id, r_raw[area_id], f"{path}.rooms.{area_id}", p)
        if room["template"] and room["template"] not in templates:
            p.add(f"{path}.rooms.{area_id}.template", "This template does not exist")
        rooms[area_id] = room
    default = raw.get("default_template") if raw.get("default_template") in templates else None
    if not (templates or rooms):
        return None
    return {"templates": templates, "default_template": default, "rooms": rooms}


def check_references(config: dict[str, Any], p: Problems) -> None:
    rooms = ((config.get("heating") or {}).get("rooms") or {})
    for device_id, device in config["devices"].items():
        for d in device.get("designers") or []:
            room = (d.get("profile") or {}).get("room") if d.get("kind") == KIND else None
            if room and room not in rooms:
                p.add(f"devices.{device_id}.designers", f"The heating designer uses a room profile that does not exist: {room}")


def modes_of(heating: dict[str, Any] | None, area_id: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """``(room profile, its template)``."""
    room = ((heating or {}).get("rooms") or {}).get(area_id or "")
    template = ((heating or {}).get("templates") or {}).get(room["template"] or "") if room else None
    return room, template


def resolve(heating: dict[str, Any] | None, area_id: str | None, own: dict[str, Any] | None,
            mode: str) -> dict[str, Any]:
    """A thermostat's value in a mode: ``{"cell", "source": thermostat|room|global|unset}``."""
    if own is not None:
        return {"cell": own, "source": "thermostat"}
    room, template = modes_of(heating, area_id)
    if room and mode in room["overrides"]:
        return {"cell": room["overrides"][mode], "source": "room"}
    found = next((m for m in (template or {}).get("modes") or [] if m["id"] == mode), None)
    if found and found["temp"] is not None:
        return {"cell": {"kind": "set", "temp": found["temp"]}, "source": "global"}
    return {"cell": None, "source": "unset"}


def frost_of(heating: dict[str, Any] | None, area_id: str | None) -> Any:
    _room, template = modes_of(heating, area_id)
    return (template or {}).get("frost")


# ================================================================== the designer document
def _slot(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    at = C.time_of_day(raw.get("at"), f"{path}.at", p, required=True)
    mode = raw.get("mode") if isinstance(raw.get("mode"), str) and MODE_ID.match(raw["mode"]) else None
    temp = _temp(raw.get("temp"), f"{path}.temp", p)
    if (mode is None) == (temp is None):
        p.add(path, "A schedule entry sets a mode or a temperature")
    return {"at": at, "mode": mode, "temp": temp}


def _slots(raw: Any, path: str, p: Problems) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    out = [_slot(s, f"{path}[{i}]", p) for i, s in enumerate(items[:MAX_SLOTS])]
    times = [s["at"] for s in out if s["at"]]
    if len(times) != len(set(times)):
        p.add(path, "Two entries start at the same time")
    return sorted(out, key=lambda s: s["at"] or "")


def _modes(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    source = raw.get("source") if raw.get("source") in ("schedule", "entity") else None
    sched = raw.get("schedule") if isinstance(raw.get("schedule"), dict) else {}
    split = sched.get("split") if sched.get("split") in ("all", "weekdays") else "all"
    ent = raw.get("entity") if isinstance(raw.get("entity"), dict) else {}
    mapping = {}
    for option, mode in (ent.get("map") if isinstance(ent.get("map"), dict) else {}).items():
        if isinstance(option, str) and 0 < len(option) <= 120 and isinstance(mode, str) and MODE_ID.match(mode):
            mapping[option] = mode
    return {"source": source,
            "schedule": {"split": split, "all": _slots(sched.get("all"), f"{path}.schedule.all", p),
                         "weekdays": _slots(sched.get("weekdays"), f"{path}.schedule.weekdays", p),
                         "weekend": _slots(sched.get("weekend"), f"{path}.schedule.weekend", p)},
            "entity": {"entity_id": C.entity(ent.get("entity_id"), f"{path}.entity.entity_id", p,
                                             ("input_select", "select", "input_boolean", "binary_sensor", "sensor", "schedule"),
                                             "the helper whose state names the mode"),
                       "map": {k: mapping[k] for k in sorted(mapping)}}}


def _mode_ref(raw: Any) -> str | None:
    return raw if isinstance(raw, str) and MODE_ID.match(raw) else None


def validate(raw: dict[str, Any], designer_id: str, targets: list[str], path: str, p: Problems) -> dict[str, Any]:
    thermostats: list[str] = []
    for i, entity_id in enumerate(raw.get("thermostats") if isinstance(raw.get("thermostats"), list) else []):
        if entity_id not in targets or not str(entity_id).startswith("climate."):
            p.add(f"{path}.thermostats[{i}]", f"{entity_id} is not one of this device's thermostats")
        elif entity_id not in thermostats:
            thermostats.append(entity_id)
    thermostats = [t for t in targets if t in thermostats][:MAX_THERMOSTATS]
    profile = raw.get("profile") if isinstance(raw.get("profile"), dict) else {}
    room = profile.get("room") if isinstance(profile.get("room"), str) and AREA_ID.match(profile["room"]) else None
    cells = {}
    for t, row in (raw.get("cells") if isinstance(raw.get("cells"), dict) else {}).items():
        if t in thermostats:
            found = _cells(row, f"{path}.cells.{t}", p, preset=True)
            if found:
                cells[t] = found
        elif row:
            p.add(f"{path}.cells.{t}", f"{t} is not one of this designer's thermostats")
    pres = raw.get("presence") if isinstance(raw.get("presence"), dict) else {}
    win = raw.get("window") if isinstance(raw.get("window"), dict) else {}
    away = raw.get("away") if isinstance(raw.get("away"), dict) else {}
    over = raw.get("override") if isinstance(raw.get("override"), dict) else {}
    sensors = []
    for i, s in enumerate((win.get("sensors") if isinstance(win.get("sensors"), list) else [])[:MAX_SENSORS]):
        found = C.entity(s, f"{path}.window.sensors[{i}]", p, ("binary_sensor", "input_boolean"), "a window or door sensor")
        if found and found not in sensors:
            sensors.append(found)
    release = []
    for key in ("release", "resume"):
        found_list = []
        for i, item in enumerate((over.get(key) if isinstance(over.get(key), list) else [])[:6]):
            found = model._triggers([item], f"{path}.override.{key}[{i}]", p,  # noqa: SLF001
                                    allowed=tuple(t for t in model.TRIGGER_TYPES if t != "occupancy"))
            if found and found[0] not in found_list:
                found_list.append(found[0])
        release.append(found_list)
    priority = raw.get("priority")
    if not (isinstance(priority, list) and sorted(priority) == sorted(LAYERS)):
        if priority is not None:
            p.add(f"{path}.priority", "The priority lists every layer once")
        priority = list(LAYERS)
    reconcile = raw.get("reconcile_minutes")
    if reconcile not in (None, "", 0) and reconcile not in model.PERIODIC_CHOICES:
        p.add(f"{path}.reconcile_minutes", "Choose one of the offered minutes")
        reconcile = None
    sensor = raw.get("sensor") if isinstance(raw.get("sensor"), dict) else {}
    return {
        "id": designer_id, "kind": KIND, "version": VERSION, "name": C.text(raw.get("name"), f"{path}.name", p) or DEFAULT_NAME,
        "enabled": raw.get("enabled") is not False, "thermostats": thermostats, "profile": {"room": room}, "cells": cells,
        "modes": _modes(raw.get("modes"), f"{path}.modes", p),
        "presence": {"enabled": C.flag(pres.get("enabled")),
                     "source": pres.get("source") if isinstance(pres.get("source"), str) and P.SOURCE_ID.match(pres["source"]) else None,
                     "vacant_minutes": C.number(pres.get("vacant_minutes"), f"{path}.presence.vacant_minutes", p, 0, 1440, integer=True),
                     "mode": _mode_ref(pres.get("mode")), "occupied_mode": _mode_ref(pres.get("occupied_mode"))},
        "window": {"enabled": C.flag(win.get("enabled")), "sensors": sensors,
                   "open_seconds": C.seconds(win.get("open_seconds"), f"{path}.window.open_seconds", p, 3600),
                   "close_seconds": C.seconds(win.get("close_seconds"), f"{path}.window.close_seconds", p, 3600),
                   "action": win.get("action") if win.get("action") in WINDOW_ACTIONS else None, "mode": _mode_ref(win.get("mode")),
                   "unavailable": win.get("unavailable") if win.get("unavailable") in WINDOW_UNAVAILABLE else None},
        "away": {"enabled": C.flag(away.get("enabled")),
                 "entity_id": C.entity(away.get("entity_id"), f"{path}.away.entity_id", p,
                                       ("person", "device_tracker", "group", "zone", "input_boolean", "binary_sensor", "sensor"),
                                       "who is home (a person, tracker, group or helper)"),
                 "home_state": away["home_state"].strip()[:60] if isinstance(away.get("home_state"), str) and away["home_state"].strip() else None,
                 "away_minutes": C.number(away.get("away_minutes"), f"{path}.away.away_minutes", p, 0, 1440, integer=True),
                 "mode": _mode_ref(away.get("mode")), "return_mode": _mode_ref(away.get("return_mode")),
                 "return_minutes": C.number(away.get("return_minutes"), f"{path}.away.return_minutes", p, 1, 600, integer=True)},
        "override": {"manual": C.flag(over.get("manual")),
                     "policy": over.get("policy") if over.get("policy") in OVERRIDE_POLICIES else None,
                     "minutes": C.number(over.get("minutes"), f"{path}.override.minutes", p, 1, 1440, integer=True),
                     "release": release[0], "resume": release[1]},
        "priority": list(priority),
        "when_off": raw.get("when_off") if raw.get("when_off") in ("leave", "heat") else "leave",
        "hvac_mode": raw.get("hvac_mode") if raw.get("hvac_mode") in HEAT_MODES else None,
        "sensor": {"entity_id": C.entity(sensor.get("entity_id"), f"{path}.sensor.entity_id", p, ("sensor", "input_number"),
                                         "a temperature sensor")} if sensor.get("entity_id") else None,
        "reconcile_minutes": reconcile or None,
    }


def sources_of(designer: dict[str, Any]) -> set[str]:
    source = (designer.get("presence") or {}).get("source")
    return {source} if source and (designer.get("presence") or {}).get("enabled") else set()


def helper_ids(device_id: str, thermostat: str) -> dict[str, str]:
    from . import lighting2 as L2
    return L2.helper_ids(device_id, thermostat)


# ================================================================== the schedule
def day_slots(schedule: dict[str, Any], day: str) -> list[dict[str, Any]]:
    if schedule["split"] == "all":
        return schedule["all"]
    return schedule["weekend"] if day in ("sat", "sun") else schedule["weekdays"]


def windows(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    """The schedule as time windows within each day (a window that runs past midnight is split there; the time
    before a day's first entry belongs to the previous day's last entry). Identical windows of several days are one."""
    days = model.WEEKDAYS
    found: list[dict[str, Any]] = []
    for k, day in enumerate(days):
        slots = [s for s in day_slots(schedule, day) if s["at"]]
        prev = [s for s in day_slots(schedule, days[k - 1]) if s["at"]]
        carry = prev[-1] if prev else (slots[-1] if slots else None)
        if carry is None:
            continue
        points = [("00:00:00", carry)] + [(s["at"], s) for s in slots]
        for j, (start, slot) in enumerate(points):
            end = points[j + 1][0] if j + 1 < len(points) else None
            if start == end:
                continue
            key = (start, end, slot["mode"], plain(slot["temp"]))
            existing = next((w for w in found if w["key"] == key), None)
            if existing:
                existing["weekdays"].append(day)
            else:
                found.append({"key": key, "after": None if start == "00:00:00" else start, "before": end,
                              "weekdays": [day], "slot": slot})
    for w in found:
        if sorted(w["weekdays"]) == sorted(days):
            w["weekdays"] = []
    return found


def _secs(t: str) -> int:
    h, m, *s = (int(v) for v in t.split(":"))
    return h * 3600 + m * 60 + (s[0] if s else 0)


def next_transition(schedule: dict[str, Any], weekday: str, time: str) -> dict[str, Any] | None:
    """The next schedule entry after ``weekday time`` (for the preview)."""
    days = model.WEEKDAYS
    start = days.index(weekday)
    now = _secs(time)
    for offset in range(8):
        day = days[(start + offset) % 7]
        for slot in day_slots(schedule, day):
            if slot["at"] and (offset > 0 or _secs(slot["at"]) > now):
                return {"weekday": day, "at": slot["at"], "mode": slot["mode"], "temp": slot["temp"], "in_days": offset}
    return None


# ================================================================== expansion
COMMAND_TEMPLATE = (
    "{% set s = states[CLIMATE] %}{% set v = ab_t[0] if ab_t else none %}"
    "{% if s is none or s.state in ['unavailable', 'unknown'] or v is none or v == 'keep' %}{{ {'service': none} }}"
    "{% elif v == 'off' %}{{ {'service': 'set_hvac_mode', 'data': {'hvac_mode': 'off'}} if s.state != 'off' else {'service': none} }}"
    "{% elif v is string and v.startswith('preset:') %}{% set p = v[7:] %}"
    "{{ {'service': 'set_preset_mode', 'data': {'preset_mode': p}} if s.state != 'off' and s.attributes.get('preset_mode') != p else {'service': none} }}"
    "{% else %}{% set st = s.attributes.get('target_temp_step') | float(STEP) %}{% set st = st if st > 0 else STEP %}"
    "{% set lo = s.attributes.get('min_temp') | float(-100) %}{% set hi = s.attributes.get('max_temp') | float(100) %}"
    "{% set t = ((([[v | float, lo] | max, hi] | min) / st) | round(0) * st) | round(2) %}"
    "{% set f = s.attributes.get('supported_features') | int(0) %}{% set rg = f | bitwise_and(1) == 0 and f | bitwise_and(2) == 2 %}"
    "{% set cur = s.attributes.get('target_temp_low') if rg else s.attributes.get('temperature') %}"
    "{% set hi2 = [s.attributes.get('target_temp_high') | float(t + 2), t + st] | max %}{% set off = s.state == 'off' %}"
    "{% if off and not HEAT %}{{ {'service': none} }}"
    "{% elif not off and cur is number and (cur - t) | abs < st / 2 %}{{ {'service': none} }}"
    "{% elif rg and off %}{{ {'service': 'set_temperature', 'data': {'target_temp_low': t, 'target_temp_high': hi2, 'hvac_mode': HM}} }}"
    "{% elif rg %}{{ {'service': 'set_temperature', 'data': {'target_temp_low': t, 'target_temp_high': hi2}} }}"
    "{% elif off %}{{ {'service': 'set_temperature', 'data': {'temperature': t, 'hvac_mode': HM}} }}"
    "{% else %}{{ {'service': 'set_temperature', 'data': {'temperature': t}} }}{% endif %}{% endif %}"
)
MANUAL_TEMPLATE = ("{{ trigger.to_state is not none and trigger.from_state is not none and trigger.to_state.context.parent_id is none "
                   "and trigger.to_state.state not in ['unavailable', 'unknown'] and trigger.from_state.state not in ['unavailable', 'unknown'] "
                   "and (trigger.to_state.attributes.get('temperature') != trigger.from_state.attributes.get('temperature') "
                   "or trigger.to_state.attributes.get('target_temp_low') != trigger.from_state.attributes.get('target_temp_low')) "
                   "and ab_cmd.service == 'set_temperature' }}")


def _float(value: Any, default: float) -> float:
    try:
        found = float(value)
    except (TypeError, ValueError):
        return default
    return found if found == found else default


def command(state: dict[str, Any] | None, value: Any, *, heat: bool, hvac_mode: str | None) -> dict[str, Any]:
    """The Python twin of :data:`COMMAND_TEMPLATE`: ``{"service": None | name, "data": {...}}``."""
    none = {"service": None}
    if state is None or state.get("state") in ("unavailable", "unknown") or value is None or value == "keep":
        return none
    attrs = state.get("attributes") or {}
    if value == "off":
        return {"service": "set_hvac_mode", "data": {"hvac_mode": "off"}} if state.get("state") != "off" else none
    if isinstance(value, str) and value.startswith("preset:"):
        preset = value[7:]
        return ({"service": "set_preset_mode", "data": {"preset_mode": preset}}
                if state.get("state") != "off" and attrs.get("preset_mode") != preset else none)
    st = _float(attrs.get("target_temp_step"), STEP_DEFAULT)
    st = st if st > 0 else STEP_DEFAULT
    lo, hi = _float(attrs.get("min_temp"), -100), _float(attrs.get("max_temp"), 100)
    t = round(round(min(max(float(value), lo), hi) / st) * st, 2)
    features = int(_float(attrs.get("supported_features"), 0))
    rg = not features & SUPPORT_TARGET and bool(features & SUPPORT_RANGE)
    cur = attrs.get("target_temp_low") if rg else attrs.get("temperature")
    hi2 = max(_float(attrs.get("target_temp_high"), t + 2), t + st)
    off = state.get("state") == "off"
    if off and not heat:
        return none
    if not off and isinstance(cur, (int, float)) and not isinstance(cur, bool) and abs(cur - t) < st / 2:
        return none
    data: dict[str, Any] = {"target_temp_low": t, "target_temp_high": hi2} if rg else {"temperature": t}
    if off:
        data["hvac_mode"] = hvac_mode
    return {"service": "set_temperature", "data": data}


def expand(designer: dict[str, Any], x: Any) -> dict[str, Any]:
    gen = _Gen(designer, x)
    if designer["enabled"]:
        gen.run()
    x.generators[designer["id"]] = gen
    gen.view["resolved"] = resolved_views(designer, x)
    gen.view["windows"] = [{k: w[k] for k in ("after", "before", "weekdays")} | {"mode": w["slot"]["mode"], "temp": plain(w["slot"]["temp"])}
                           for w in windows(designer["modes"]["schedule"])] if designer["modes"]["source"] == "schedule" else []
    return gen.view


def resolved_views(d: dict[str, Any], x: Any) -> dict[str, Any]:
    """Per thermostat and mode: the value and where it comes from (thermostat / room / global / unset)."""
    room, template = modes_of(x.heating, d["profile"]["room"])
    modes = [m["id"] for m in (template or {}).get("modes") or []]
    return {"modes": [{"id": m["id"], "name": m["name"]} for m in (template or {}).get("modes") or []],
            "cells": {t: {mode: resolve(x.heating, d["profile"]["room"], (d["cells"].get(t) or {}).get(mode), mode)
                          for mode in modes} for t in d["thermostats"]},
            "frost": (template or {}).get("frost"), "template": (template or {}).get("name"),
            "room": d["profile"]["room"] if room else None}


def capability_problems(d: dict[str, Any], caps: dict[str, dict[str, Any]], x: Any) -> None:
    for t in d["thermostats"]:
        cap = caps.get(t)
        if not cap or cap.get("unknown"):
            continue
        modes = cap.get("hvac_modes") or []
        if d["hvac_mode"] and d["hvac_mode"] not in modes:
            x.problem("blocking", "unsupported_hvac", f"{t} does not offer the HVAC mode “{d['hvac_mode']}” "
                      f"(it offers {', '.join(modes) or 'none'}).", d["id"])
        if d["window"]["enabled"] and d["window"]["action"] == "off" and "off" not in modes:
            x.problem("blocking", "unsupported_off", f"{t} cannot be switched off (no “off” HVAC mode).", d["id"])
        if not cap.get("target") and not cap.get("range"):
            x.problem("blocking", "no_setpoint", f"{t} does not report a settable target temperature.", d["id"])
        presets = cap.get("preset_modes") or []
        for mode, c in (d["cells"].get(t) or {}).items():
            if c["kind"] == "preset" and c["preset"] not in presets:
                x.problem("blocking", "unsupported_preset", f"{t} has no preset “{c['preset']}” (mode {mode}).", d["id"])


def index_problems(d: dict[str, Any], index: Any, x: Any) -> None:
    for t in d["thermostats"]:
        state = index.states.get(t) or {}
        if state.get("state") == "unavailable":
            x.problem("info", "thermostat_unavailable", f"{index.name(t)} is unavailable now: it gets no commands until it is back.", d["id"])
    for s in d["window"]["sensors"] if d["window"]["enabled"] else []:
        if (index.states.get(s) or {}).get("state") == "unavailable":
            x.problem("warning", "window_unavailable", f"{index.name(s)} is unavailable now: the configured fallback applies.", d["id"], "window")


class _Gen:
    def __init__(self, d: dict[str, Any], x: Any) -> None:
        self.d, self.x = d, x
        self.did = d["id"]
        self.device_id = x.device["id"]
        self.ok = True
        self.plans: dict[str, dict[str, Any]] = {}
        self.view: dict[str, Any] = {"rules": [], "problems": [], "helpers": [], "version": VERSION}

    def block(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("blocking", code, message, self.did, layer)
        self.ok = False

    def warn(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("warning", code, message, self.did, layer)

    def info(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("info", code, message, self.did, layer)

    def add(self, layer: str, target: str, label: str, triggers: list[dict[str, Any]], actions: list[dict[str, Any]], *,
            conditions: list[dict[str, Any]] | None = None, key: Any = None) -> str:
        rule_id = C.generated_rule_id(self.did, layer, target, key)
        meta = {"designer_id": self.did, "designer": KIND, "layer": layer, "target": target, "column": key, "mood": None,
                "exclusive": {}, "cell": None, "resolution": {}, "group": target}
        raw = C.rule(rule_id, " · ".join([self.d["name"], label])[: model.MAX_NAME], triggers, conditions or [], actions, meta)
        self.x.add_rule(raw, self.did)
        self.view["rules"].append(rule_id)
        return rule_id

    # ------------------------------------------------------------ values
    def mode_name(self, mode: str | None) -> str:
        _room, template = modes_of(self.x.heating, self.d["profile"]["room"])
        found = next((m for m in (template or {}).get("modes") or [] if m["id"] == mode), None)
        return found["name"] if found else (mode or "…")

    def known_modes(self) -> list[str]:
        _room, template = modes_of(self.x.heating, self.d["profile"]["room"])
        return [m["id"] for m in (template or {}).get("modes") or []]

    def value(self, t: str, mode: str | None, where: str, layer: str) -> Any:
        """A mode's value for a thermostat as the evaluation carries it (a number, 'keep', 'preset:x'), or None."""
        if mode is None:
            return None
        if mode not in self.known_modes():
            self.block("unknown_mode", f"“{self.d['name']}”: {where} uses the mode “{mode}”, which the room profile's "
                       "template does not have.", layer)
            return None
        res = resolve(self.x.heating, self.d["profile"]["room"], (self.d["cells"].get(t) or {}).get(mode), mode)
        c = res["cell"]
        if c is None:
            self.warn("unset_mode", f"“{self.d['name']}”: {t} has no temperature for the mode “{self.mode_name(mode)}” "
                      f"({where}): it is UNSET at every level, so the next layer in the priority decides then.", layer)
            return None
        if c["kind"] == "none":
            return "keep"
        if c["kind"] == "preset":
            return f"preset:{c['preset']}"
        return self.x.number(f"{C.slug(t.split('.', 1)[1])}_{mode}", c["temp"], f"{t}: {self.mode_name(mode)}",
                             f"The temperature of {t} in the mode {self.mode_name(mode)}.", group="Temperatures",
                             selector={"number": {"min": MIN_T, "max": MAX_T, "step": 0.5, "mode": "box", "unit_of_measurement": "°"}})

    def at(self, time: str | None) -> Any:
        """A schedule time (a blueprint input shared by the triggers and the time windows)."""
        if not time:
            return None
        return self.x.time(f"at_{time[:5].replace(':', '')}", time, f"Schedule {time[:5]}", "A time of the schedule.")

    def delay(self, key: str, seconds: Any, name: str) -> Any:
        return self.x.seconds(key, seconds, name, f"{name} (a delay of the Heating Designer).")

    # ------------------------------------------------------------ the layers of one thermostat
    def branches(self, t: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``(branches in priority order, triggers)`` for one thermostat."""
        d, x = self.d, self.x
        out: dict[str, list[dict[str, Any]]] = {layer: [] for layer in LAYERS}
        triggers: list[dict[str, Any]] = []
        # windows and doors
        w = d["window"]
        if w["enabled"]:
            if not w["sensors"]:
                self.block("no_sensors", "Choose the window or door sensors (Windows & doors).", "window")
            if w["action"] is None:
                self.block("no_window_action", "Choose what happens while a window is open: a mode, frost protection, OFF "
                           "or no action. Heating is never switched off by itself.", "window")
            if w["unavailable"] is None:
                self.block("no_window_fallback", "Choose what happens while a window sensor is unavailable.", "window")
            value = None
            if w["action"] == "mode":
                if w["mode"] is None:
                    self.block("no_window_mode", "Choose the mode while a window is open.", "window")
                value = self.value(t, w["mode"], "Windows & doors", "window")
            elif w["action"] == "frost":
                frost = frost_of(x.heating, d["profile"]["room"])
                if frost is None:
                    self.block("no_frost", "Frost protection needs the frost temperature of the room profile's template.", "window")
                value = x.number(f"{C.slug(t.split('.', 1)[1])}_frost", frost, f"{t}: frost protection",
                                 "The temperature while a window is open.", group="Temperatures") if frost is not None else None
            elif w["action"] == "off":
                value = "off"
                if d["when_off"] != "heat" or not d["hvac_mode"]:
                    self.block("off_needs_heat", "OFF while a window is open needs a way back: choose under Thermostats that "
                               "a thermostat that is off is switched to an HVAC mode again (and which one).", "window")
            elif w["action"] == "none":
                value = "keep"
            opened = [x.e(s, "Window or door sensor", group="Sensor entities") for s in w["sensors"]]
            open_s = self.delay("window_open", w["open_seconds"], "Window open for")
            close_s = self.delay("window_closed", w["close_seconds"], "Window closed for")
            is_open = [C.c_state(s, "on", for_seconds=open_s) for s in opened]
            if w["unavailable"] == "open":
                is_open += [C.c_state(s, "unavailable", for_seconds=open_s) for s in opened]
            if value is not None and is_open:
                out["window"].append({"conditions": C.group("or", is_open), "value": [value, "Window open"]})
            if w["unavailable"] == "keep" and opened:
                out["window"].append({"conditions": C.group("or", [C.c_state(s, "unavailable") for s in opened]),
                                      "value": ["keep", "Window sensor unavailable"]})
            for s in opened:
                triggers += [C.t_state(s, to="on", for_seconds=open_s), C.t_state(s, to="off", for_seconds=close_s),
                             C.t_state(s, to="unavailable", for_seconds=open_s), C.t_state(s, frm="unavailable")]
        # manual override
        o = d["override"]
        if o["manual"]:
            h = helper_ids(self.device_id, t)
            out["override"].append({"conditions": C.group("and", [C.c_state(h["flag"], "on")]), "value": ["keep", "Manual override"]})
            triggers.append(C.t_state(h["flag"]))
        # away / home
        a = d["away"]
        if a["enabled"]:
            if not a["entity_id"] or not a["home_state"]:
                self.block("no_away_source", "Choose who is home (a person, tracker, group or helper) and its “home” state. "
                           "Nothing is chosen for you.", "away")
            elif a["mode"] is None:
                self.block("no_away_mode", "Choose the mode while nobody is home.", "away")
            else:
                who = x.e(a["entity_id"], "Home / away source", group="Sensor entities")
                hold = int(a["away_minutes"] or 0) * 60
                value = self.value(t, a["mode"], "Away", "away")
                # "not home" can be several states (not_home, a zone, …): held for the delay = a template that stays true
                ref, home = x.refs.ref(who), quote(a["home_state"])
                away_now = ("{{ not is_state(" + ref + ", " + home + ") and states(" + ref + ") not in ['unavailable', 'unknown'] }}")
                held = ("{{ not is_state(" + ref + ", " + home + ") and states(" + ref + ") not in ['unavailable', 'unknown'] and "
                        "(now() - states[" + ref + "].last_changed).total_seconds() >= " + str(hold) + " }}")
                if value is not None:
                    out["away"].append({"conditions": C.group("and", [x.refs.template(held if hold else away_now, _away_twin(a, hold))]),
                                        "value": [value, "Away"]})
                triggers += [C.t_template(away_now, hold), C.t_state(who, to=a["home_state"])]
                if a["return_mode"]:
                    if not a["return_minutes"]:
                        self.block("no_return_minutes", "Give how long the return-home mode lasts.", "away")
                    back = int(a["return_minutes"] or 0) * 60
                    value = self.value(t, a["return_mode"], "Return home", "away")
                    if value is not None:
                        out["away"].append({"conditions": C.group("and", [
                            C.c_state(who, a["home_state"]), C.negation([C.c_state(who, a["home_state"], for_seconds=back)])]),
                            "value": [value, "Returned home"]})
                    triggers.append(C.t_state(who, to=a["home_state"], for_seconds=back))
        # presence
        pr = d["presence"]
        if pr["enabled"]:
            source = x.sources.get(pr["source"]) if pr["source"] else None
            if source is None:
                self.block("no_presence_source", "Choose the presence source (a room or zone of the Presence page).", "presence")
            elif source["kind"] not in ("room", "zone") or not source["entity_id"]:
                self.block("presence_kind", "Presence for heating uses a room or zone source with its own entity.", "presence")
            elif pr["mode"] is None:
                self.block("no_presence_mode", "Choose the mode while the room is vacant.", "presence")
            else:
                entity = x.e(source["entity_id"], "Presence entity", group="Sensor entities")
                hold = self.delay("vacant_for", int(pr["vacant_minutes"] or 0) * 60 + int(plain(source["exit_seconds"]) or 0), "Vacant for")
                value = self.value(t, pr["mode"], "Presence (vacant)", "presence")
                if value is not None:
                    out["presence"].append({"conditions": C.group("and", [C.c_state(entity, "off", for_seconds=hold)]),
                                            "value": [value, f"{source['name']} vacant"]})
                if pr["occupied_mode"]:
                    value = self.value(t, pr["occupied_mode"], "Presence (occupied)", "presence")
                    if value is not None:
                        out["presence"].append({"conditions": C.group("and", [C.c_state(entity, "on")]),
                                                "value": [value, f"{source['name']} occupied"]})
                triggers += [C.t_state(entity, to="off", for_seconds=hold), C.t_state(entity, to="on")]
        # schedule / mode helper
        m = d["modes"]
        if m["source"] == "schedule":
            sched = m["schedule"]
            lists = [sched["all"]] if sched["split"] == "all" else [sched["weekdays"], sched["weekend"]]
            if any(not lst for lst in lists):
                self.block("empty_schedule", "Add at least one entry to "
                           + ("the schedule" if sched["split"] == "all" else "both the weekday and the weekend schedule") + ".", "schedule")
            else:
                for win in windows(sched):
                    slot = win["slot"]
                    if slot["mode"]:
                        value, by = self.value(t, slot["mode"], f"the schedule entry at {slot['at'][:5]}", "schedule"), \
                            f"Schedule {slot['at'][:5]}: {self.mode_name(slot['mode'])}"
                    else:
                        value = x.number(f"{C.slug(t.split('.', 1)[1])}_at_{slot['at'][:5].replace(':', '')}", slot["temp"],
                                         f"{t}: from {slot['at'][:5]}", "The temperature of this schedule entry.", group="Temperatures")
                        by = f"Schedule {slot['at'][:5]}: {plain(slot['temp'])}°"
                    if value is None:
                        continue
                    # the same blueprint inputs as the times of the triggers (a time changed in a blueprint moves both)
                    when = C.c_time(self.at(win["after"]), self.at(win["before"]), win["weekdays"])
                    out["schedule"].append({"conditions": C.group("and", [when]), "value": [value, by]})
                for lst, days in ((sched["all"], []),) if sched["split"] == "all" else (
                        (sched["weekdays"], ["mon", "tue", "wed", "thu", "fri"]), (sched["weekend"], ["sat", "sun"])):
                    triggers += [C.t_time(self.at(s["at"]), days) for s in lst if s["at"]]
        elif m["source"] == "entity":
            ent = m["entity"]
            if not ent["entity_id"] or not ent["map"]:
                self.block("no_mode_entity", "Choose the helper whose state names the mode, and which state means which mode.", "schedule")
            else:
                entity = x.e(ent["entity_id"], "Mode helper", group="Trigger entities")
                for option, mode in ent["map"].items():
                    value = self.value(t, mode, f"the state “{option}”", "schedule")
                    if value is not None:
                        out["schedule"].append({"conditions": C.group("and", [C.c_state(entity, option)]),
                                                "value": [value, f"{ent['entity_id']} is {option}: {self.mode_name(mode)}"]})
                triggers.append(C.t_state(entity))
        from .lighting2 import _clean  # labels (mode, source and option names) must not reach a variable as template syntax
        ordered = [b | {"layer": layer, "value": [b["value"][0], _clean(b["value"][1])]} for layer in d["priority"] for b in out[layer]]
        return ordered, triggers

    def run(self) -> None:
        d = self.d
        if not d["thermostats"]:
            self.block("no_thermostats", f"“{d['name']}”: choose the thermostats it controls.")
            return
        room, template = modes_of(self.x.heating, d["profile"]["room"])
        if d["profile"]["room"] is None:
            self.block("no_profile", "Choose the room heating profile these thermostats derive from (Thermostats). "
                       "Nothing is chosen for you.")
            return
        if room is None or template is None:
            self.block("missing_profile", "The chosen room heating profile (or its template) does not exist (anymore).")
            return
        if d["modes"]["source"] is None and not (d["presence"]["enabled"] or d["away"]["enabled"] or d["window"]["enabled"]):
            self.info("nothing", "Nothing decides a setpoint yet: choose a schedule or a mode helper, or a presence, "
                      "window or away rule.", "schedule")
        if d["when_off"] == "heat" and not d["hvac_mode"]:
            self.block("no_hvac_mode", "Choose the HVAC mode a thermostat that is off is switched to.")
        o = d["override"]
        if o["manual"]:
            if o["policy"] is None:
                self.block("no_override_policy", "Choose how long a manual change suspends the automatic setpoint.", "override")
            elif o["policy"] == "duration" and not o["minutes"]:
                self.block("no_minutes", "Give the duration of a manual override.", "override")
            elif o["policy"] == "until_event" and not o["release"]:
                self.block("no_release", "Choose the event that ends a manual override.", "override")
            elif o["policy"] == "until_next" and d["modes"]["source"] is None:
                self.block("no_next", "“Until the next scheduled change” needs a schedule or a mode helper.", "override")
        if o["manual"]:
            self.x.extra_targets.update(h for t in d["thermostats"] for h in (helper_ids(self.device_id, t)["flag"],
                                                                            helper_ids(self.device_id, t)["timer"]))
        for t in d["thermostats"]:
            branches, triggers = self.branches(t)
            self.plans[t] = {"thermostat": t, "branches": branches}
            if self.ok:
                self.evaluation(t, branches, triggers)
        if self.ok and o["manual"]:
            self.overrides()

    def evaluation(self, t: str, branches: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> None:
        d, x = self.d, self.x
        target = x.e(t, "Controlled thermostat")
        triggers = list(triggers)
        triggers.append({"type": "event", "event_type": EVENT, "event_data": {"climate": plain(t)}})
        triggers.append(C.t_state(target, frm="unavailable"))
        if d["reconcile_minutes"]:
            triggers.append({"type": "pattern", "hours": None, "minutes": f"/{d['reconcile_minutes']}", "seconds": None})
        manual = None
        if d["override"]["manual"]:
            manual = C.t_state(target, attribute="temperature")
            triggers.append(manual)
            triggers.append(C.t_state(target, attribute="target_temp_low"))
        unique: list[dict[str, Any]] = []
        for trig in triggers:
            if trig not in unique:
                unique.append(trig)
        heat = d["when_off"] == "heat"
        hm = d["hvac_mode"] or "heat"
        cmd = (COMMAND_TEMPLATE.replace("CLIMATE", x.refs.ref(target)).replace("STEP", str(STEP_DEFAULT))
               .replace("HEAT", "true" if heat else "false").replace("HM", quote(hm)))
        actions: list[dict[str, Any]] = [{"type": "variables", "variables": {"ab_t": None}}]
        if branches:
            actions.append({"type": "choose", "default": [], "branches": [
                {"conditions": b["conditions"], "actions": [{"type": "variables", "variables": {"ab_t": b["value"]}}]} for b in branches]})
        actions.append({"type": "variables", "variables": {"ab_cmd": cmd}})
        send = [{"type": "if", "conditions": C.group("and", [{"type": "template", "value_template": "{{ ab_cmd.service == '" + svc + "' }}"}]),
                 "then": [{"type": "service", "action": f"climate.{svc}", "target": [target], "data": "{{ ab_cmd.data }}"}], "else": []}
                for svc in ("set_hvac_mode", "set_preset_mode", "set_temperature")]
        if manual is not None:
            index = [i for i, trig in enumerate(unique) if trig in (manual, C.t_state(target, attribute="target_temp_low"))]
            start = self.override_start(t)
            actions.append({"type": "if", "conditions": C.group("and", [{"type": "trigger", "triggers": index},
                                                                       {"type": "template", "value_template": MANUAL_TEMPLATE}]),
                            "then": start, "else": [{"type": "if", "conditions": C.group("and", [
                                C.negation([{"type": "trigger", "triggers": index}])]), "then": send, "else": []}]})
        else:
            actions += send
        self.add("evaluate", t, f"{t} · automatic setpoint", unique, actions, key="evaluate")

    def override_start(self, t: str) -> list[dict[str, Any]]:
        h = helper_ids(self.device_id, t)
        out = [{"type": "service", "action": "input_boolean.turn_on", "target": [h["flag"]], "data": {}}]
        if self.d["override"]["policy"] == "duration" and self.d["override"]["minutes"]:
            minutes = int(plain(self.d["override"]["minutes"]))
            out.append({"type": "service", "action": "timer.start", "target": [h["timer"]],
                        "data": {"duration": f"{minutes // 60:02d}:{minutes % 60:02d}:00"}})
        return out

    def overrides(self) -> None:
        d, x = self.d, self.x
        o = d["override"]
        for t in d["thermostats"]:
            h = helper_ids(self.device_id, t)
            self.view["helpers"].append({"entity_id": h["flag"], "domain": "input_boolean", "item_id": h["item_id"],
                                         "name": h["name"], "light": t, "purpose": "manual override of the setpoint (on = active)"})
            off = [{"type": "service", "action": "input_boolean.turn_off", "target": [h["flag"]], "data": {}}]
            if o["policy"] == "duration":
                self.view["helpers"].append({"entity_id": h["timer"], "domain": "timer", "item_id": h["item_id"],
                                             "name": h["name"], "light": t, "purpose": "how long the override lasts"})
                off_t = off + [{"type": "service", "action": "timer.cancel", "target": [h["timer"]], "data": {}}]
                self.add("override", t, f"{t} · override ends (time is up)",
                         [{"type": "event", "event_type": "timer.finished", "event_data": {"entity_id": h["timer"]}}], off,
                         conditions=[C.c_state(h["flag"], "on")], key="timer")
                self.add("override", t, f"{t} · override ended during a restart", [{"type": "homeassistant", "event": "start"}], off,
                         conditions=[C.c_state(h["flag"], "on"), C.c_state(h["timer"], "idle")], key="start")
            else:
                off_t = off
            release: list[dict[str, Any]] = []
            if o["policy"] == "until_next":
                m = d["modes"]
                if m["source"] == "schedule":
                    sched = m["schedule"]
                    pairs = [(sched["all"], [])] if sched["split"] == "all" else [
                        (sched["weekdays"], ["mon", "tue", "wed", "thu", "fri"]), (sched["weekend"], ["sat", "sun"])]
                    release = [C.t_time(self.at(s["at"]), days) for lst, days in pairs for s in lst if s["at"]]
                elif m["entity"]["entity_id"]:
                    release = [C.t_state(x.e(m["entity"]["entity_id"], "Mode helper", group="Trigger entities"))]
            elif o["policy"] == "until_event":
                release = list(o["release"])
            if release:
                self.add("override", t, f"{t} · override ends ({'next scheduled change' if o['policy'] == 'until_next' else 'event'})",
                         release, off_t, conditions=[C.c_state(h["flag"], "on")], key="release")
            if o["resume"]:
                self.add("override", t, f"{t} · resume automatic", list(o["resume"]), off_t + [
                    {"type": "event", "event_type": EVENT, "event_data": {"climate": plain(t)}}], key="resume")
        x.extra_targets.update(h["entity_id"] for h in self.view["helpers"])


def _away_twin(a: dict[str, Any], hold: int) -> Any:
    """The Python twin of the away template (states as the simulation carries them, with ``held`` seconds)."""
    def evaluate(states: dict[str, Any]) -> bool | None:
        state = states.get(a["entity_id"])
        if state is None:
            return None
        if state.get("state") in ("unavailable", "unknown") or state.get("state") == a["home_state"]:
            return False
        held = state.get("held")
        return True if not hold else (None if held is None else held >= hold)
    return evaluate


# ================================================================== preview (the Python twin)
def evaluate(gen: _Gen, t: str, ev: Any, *, override: bool | None = None) -> dict[str, Any]:
    plan = gen.plans.get(t) or {"branches": []}
    found, unknown, layers = None, [], []
    for b in plan["branches"]:
        if b["layer"] == "override" and override is not None:
            holds = override
        else:
            holds = ev.condition(b["conditions"], set())
        layers.append({"layer": b["layer"], "by": b["value"][1], "value": plain(b["value"][0]), "holds": holds})
        if holds is None:
            unknown.append(f"{b['value'][1]}: cannot be evaluated offline")
            break
        if holds and found is None:
            found = [plain(b["value"][0]), b["value"][1], b["layer"]]
            break
    state = ev.states.get(t)
    d = gen.d
    cmd = command(state, found[0] if found else None, heat=d["when_off"] == "heat", hvac_mode=d["hvac_mode"] or "heat")
    return {"result": found, "layers": layers, "command": cmd, "unknown": unknown}


def simulate(gen: _Gen, ev: Any, scenario: dict[str, Any], names: Any) -> dict[str, Any]:
    d = gen.d
    out = []
    for t in d["thermostats"]:
        state = ev.states.get(t) or {}
        attrs = state.get("attributes") or {}
        over = None
        if d["override"]["manual"]:
            flag = helper_ids(gen.device_id, t)["flag"]
            over = (ev.states.get(flag) or {}).get("state") == "on"
        found = evaluate(gen, t, ev)
        sensor = (d["sensor"] or {}).get("entity_id")
        out.append({"entity_id": t, "name": names(t), "state": state.get("state"), "target": attrs.get("temperature"),
                    "target_low": attrs.get("target_temp_low"), "measured": attrs.get("current_temperature"),
                    "room_temperature": (ev.states.get(sensor) or {}).get("state") if sensor else None,
                    "hvac_action": attrs.get("hvac_action"), "override": over, **found})
    nxt = None
    if d["modes"]["source"] == "schedule":
        nxt = next_transition(d["modes"]["schedule"], ev.now.get("weekday") or "mon", ev.now.get("time") or "12:00:00")
        if nxt and nxt["mode"]:
            nxt["mode_name"] = gen.mode_name(nxt["mode"])
    return {"thermostats": out, "next": nxt, "priority": [{"layer": layer, "name": LAYER_NAMES[layer]} for layer in d["priority"]],
            "at": ev.now}


# ================================================================== presets the user can start from
def preset_template(template_id: str) -> dict[str, Any]:
    """Illustrative values the user adds on purpose and adjusts (nothing is assigned to a thermostat)."""
    return {"id": template_id, "name": "Standard Heating", "version": 1, "frost": 7,
            "modes": [{"id": "comfort", "name": "Comfort", "temp": 21}, {"id": "setback", "name": "Setback", "temp": 18},
                      {"id": "night", "name": "Night", "temp": 17}, {"id": "away", "name": "Away", "temp": 16}]}


QUICK = {
    "schedule": "Basic temperature schedule (weekdays and weekend)",
    "day_night": "Day / night",
    "presence": "Presence-based setback",
    "window": "Window-aware heating",
    "away": "Away / home",
    "override": "Manual thermostat override",
}


def quick_start(kind: str, d: dict[str, Any], modes: list[str]) -> dict[str, Any]:
    """Fill in one of the built-in templates (A-F) with illustrative times; the user adjusts everything. Sources
    (sensors, persons, presence) are never chosen here."""
    import copy
    d = copy.deepcopy(d)
    first = lambda *names: next((m for m in names if m in modes), modes[0] if modes else None)  # noqa: E731
    comfort, setback, night, away = first("comfort"), first("setback"), first("night", "setback"), first("away", "setback")
    if kind == "schedule":
        d["modes"]["source"] = "schedule"
        d["modes"]["schedule"] = {"split": "weekdays", "all": [],
                                  "weekdays": [{"at": "06:00:00", "mode": comfort, "temp": None}, {"at": "09:00:00", "mode": setback, "temp": None},
                                               {"at": "17:00:00", "mode": comfort, "temp": None}, {"at": "23:00:00", "mode": night, "temp": None}],
                                  "weekend": [{"at": "08:00:00", "mode": comfort, "temp": None}, {"at": "23:30:00", "mode": night, "temp": None}]}
    elif kind == "day_night":
        d["modes"]["source"] = "schedule"
        d["modes"]["schedule"] = {"split": "all", "weekdays": [], "weekend": [],
                                  "all": [{"at": "06:30:00", "mode": comfort, "temp": None}, {"at": "22:30:00", "mode": night, "temp": None}]}
    elif kind == "presence":
        d["presence"] = {**d["presence"], "enabled": True, "vacant_minutes": d["presence"]["vacant_minutes"] or 30, "mode": setback}
    elif kind == "window":
        d["window"] = {**d["window"], "enabled": True, "open_seconds": d["window"]["open_seconds"] or 60,
                       "close_seconds": d["window"]["close_seconds"] or 30, "action": d["window"]["action"] or "frost",
                       "unavailable": d["window"]["unavailable"] or "ignore"}
    elif kind == "away":
        d["away"] = {**d["away"], "enabled": True, "away_minutes": d["away"]["away_minutes"] or 30, "mode": away}
    elif kind == "override":
        d["override"] = {**d["override"], "manual": True, "policy": d["override"]["policy"] or "until_next"}
    return d
