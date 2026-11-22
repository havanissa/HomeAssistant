"""The Lighting Designer, version 2: Truth and Brightness Matrices, Colour & Mood, Switches & Remotes, Advanced Rules
and an explicit, property-specific priority. Everything compiles into native Home Assistant automations.

How a light is controlled (one *evaluation* per light, always from the current states):

    Truth Matrices       ON / OFF            (several: combined explicitly)
    Brightness Matrices  0-100 %             (several: combined explicitly)
    Colour & Mood        colour temperature, colour, brightness, effect, transition (per mood value)
    Manual override      while it is active, the layers below it change nothing
    Advanced Rules       "while ..." rules that set properties (and "when ..." one-shot rules)

Each property (on/off, brightness, colour, effect, transition) is resolved on its own, by walking the layers in
the configured priority order (default: Advanced Rules, override, mood, brightness, truth). The first layer that
*decides* the property wins: a value, or NO ACTION (= leave it as it is). UNSET means "this layer does not
decide", so the next one is asked. Then the result is applied with truth gating:

* resolved OFF            -> the light is switched off (brightness and colour are not sent);
* resolved ON             -> switched on with the resolved attributes;
* not decided / NO ACTION -> an ON light gets the resolved attributes; an OFF light stays off (brightness and
                             colour never switch a light on by themselves).

Nothing is sent when the light already is what the evaluation wants, or when it is unavailable (a bulb whose power a
wall switch removed gets no retries). The evaluation runs whenever one of its inputs changes - a matrix source, the
mood, an Advanced Rule's condition, the manual override - so when an Advanced Rule or an override ends the light
returns to the *currently* calculated baseline, without a rule of its own for that.

Native form (per light, its own automation in mode queued):

    variables            the matrices' values, unset
    choose (per matrix)  branches in the matrix's explicit order -> variables (the first matching branch wins)
    choose (mood)        the current mood value -> its attributes
    if (per rule)        an Advanced Rule's conditions -> it is active
    variables            the resolution per property (templates over those variables) and the command
    if                   turn_off / turn_on with exactly the resolved attributes

The Python twin (:func:`evaluate`) replays the *same plan* (branches and conditions) with the simulation's
evaluator, so the simulation and the live "who controls this light" view follow the compiler's semantics.

Durable state for manual overrides lives in Home Assistant helpers that the App creates at deployment and owns
(``input_boolean.ab_<device>_<light>_override`` and, for a timed override, ``timer.ab_<device>_<light>_override``):
they survive restarts and the App does not need to run.
"""

from __future__ import annotations

import re
from typing import Any

from .. import model
from ..compiler import plain, quote
from ..model import Problems
from . import common as C, presence as P

KIND = "lighting"
VERSION = 2
ROWS = "lights"
DEFAULT_NAME = "Lighting"
EVENT = "automation_builder_lighting"

LAYERS = ("advanced", "override", "mood", "brightness", "truth")
LAYER_NAMES = {"advanced": "Advanced Rules", "override": "Switch & remote overrides", "mood": "Colour & Mood",
               "brightness": "Brightness Matrix", "truth": "Truth Matrix"}
#: Which layers can decide which property.
PROPERTIES = {"on": ("advanced", "override", "truth"), "brightness": ("advanced", "override", "mood", "brightness"),
              "color": ("advanced", "override", "mood"), "effect": ("advanced", "override", "mood"),
              "transition": ("advanced", "mood")}
PROPERTY_NAMES = {"on": "On/off", "brightness": "Brightness", "color": "Colour", "effect": "Effect", "transition": "Transition"}

TRUTH_KINDS = ("on", "off", "none", "default")
BRIGHT_KINDS = ("set", "none", "default")
MOOD_KINDS = ("set", "none", "default")
COLUMN_KINDS = ("presence", "state", "numeric", "time", "always")
POLICIES = {"truth": ("priority", "on_wins", "off_wins", "exclusive"), "brightness": ("priority", "max", "min", "exclusive")}
COMBINE = {"truth": ("priority", "on_wins", "off_wins"), "brightness": ("priority", "max", "min")}
POLICY_NAMES = {"priority": "explicit ranking", "on_wins": "ON wins", "off_wins": "OFF wins", "max": "the highest wins",
                "min": "the lowest wins", "exclusive": "nothing while they disagree"}
ZERO = ("off", "minimum", "unchanged")
OVERRIDE_POLICIES = ("until_resumed", "duration", "until_vacant", "until_event")
ACTIONS = ("none", "toggle", "on", "off", "brightness", "step", "mood", "select_mood", "override", "resume", "advanced", "dim",
           "native")
ONE_SHOT_ACTIONS = ("toggle", "on", "off", "brightness", "step", "mood", "resume", "native")
INTERACTIONS = ("on", "off", "changed", "single", "double", "triple", "long", "release", "hold", "up", "down", "other")
INTERACTION_NAMES = {"on": "Switched on", "off": "Switched off", "changed": "Switched (either way)",
                     "single": "Single press", "double": "Double press", "triple": "Triple press", "long": "Long press",
                     "release": "Release", "hold": "Hold", "up": "Brightness up", "down": "Brightness down", "other": "Other"}
LATCHED_TRIGGERS = ("state",)
MOMENTARY_TRIGGERS = ("device", "button", "event", "mqtt")
NATIVE_LIGHT_ACTIONS = ("light.turn_on", "light.turn_off", "light.toggle")
MOOD_DOMAINS = ("input_select", "select", "sensor", "input_text")
MAX_MATRICES = 4
MAX_COLUMNS = 12
MAX_VALUES = 30
MAX_SWITCHES = 12
MAX_INTERACTIONS = 16
MAX_MAPPINGS = 24
MAX_ADVANCED = 16
MAX_RELEASE = 6
DIM_STEPS = 40
TOL_BRIGHTNESS = 3      # of 255: what counts as "already at this brightness"
TOL_KELVIN = 60
TOL_HUE = 4
TOL_SAT = 4
ID = re.compile(r"^[a-z]_[0-9a-f]{4,8}$")


# ================================================================== validation
def _id(raw: Any, prefix: str, path: str, p: Problems, seen: set[str]) -> str:
    value = C.short_id(raw, prefix, path, p)
    if value in seen:
        p.add(path, "Two items share an id")
    seen.add(value)
    return value


def _lights_of(raw: Any, path: str, p: Problems, lights: list[str], *, required: bool = False) -> list[str]:
    chosen = raw if isinstance(raw, list) else []
    out = [light for light in lights if light in chosen]
    for light in chosen:
        if light not in lights:
            p.add(path, f"{light} is not one of this designer's lights")
    if required and not out:
        p.add(path, "Choose at least one light")
    return out


def truth_cell(raw: Any, path: str, p: Problems, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "Unknown cell value (a Truth Matrix cell is ON, OFF, NO ACTION or USE DEFAULT)")
        return {"kind": "none"}
    return {"kind": raw["kind"]}


def bright_cell(raw: Any, path: str, p: Problems, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "Unknown cell value (a Brightness Matrix cell is a percentage, NO ACTION or USE DEFAULT)")
        return {"kind": "none"}
    if raw["kind"] != "set":
        return {"kind": raw["kind"]}
    pct = C.number(raw.get("pct"), f"{path}.pct", p, 0, 100, integer=True)
    if pct is None and raw.get("pct") in (None, ""):
        p.add(f"{path}.pct", "Give the brightness (0-100 %)")
    return {"kind": "set", "pct": pct}


def _color(raw: Any, path: str, p: Problems) -> dict[str, Any] | None:
    if raw in (None, {}):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("rgb"), list) and len(raw["rgb"]) == 3:
        rgb = [C.number(v, f"{path}.rgb", p, 0, 255, integer=True) for v in raw["rgb"]]
        if None in rgb:
            return None
        if rgb == [0, 0, 0]:
            p.add(path, "Black is no colour: use OFF to switch the light off")
            return None
        return {"rgb": rgb}
    if isinstance(raw, dict) and isinstance(raw.get("hs"), list) and len(raw["hs"]) == 2:
        hs = [C.number(raw["hs"][0], f"{path}.hs", p, 0, 360), C.number(raw["hs"][1], f"{path}.hs", p, 0, 100)]
        return {"hs": hs} if None not in hs else None
    p.add(path, "A colour is RGB (three numbers) or hue/saturation (two)")
    return None


def attributes(raw: dict[str, Any], path: str, p: Problems, *, keep: bool = False) -> dict[str, Any]:
    """The light attributes of a mood cell or an Advanced Rule: each one unset (None), a value, or (``keep``:
    Advanced Rules only) "keep" = NO ACTION for that property."""
    def kept(key: str) -> bool:
        return keep and raw.get(key) == "keep"
    out: dict[str, Any] = {}
    out["brightness_pct"] = "keep" if kept("brightness_pct") else C.number(raw.get("brightness_pct"), f"{path}.brightness_pct", p, 1, 100, integer=True)
    if kept("color"):
        out["color_temp_kelvin"], out["color"] = None, "keep"
    else:
        out["color_temp_kelvin"] = C.number(raw.get("color_temp_kelvin"), f"{path}.color_temp_kelvin", p, 1000, 12000, integer=True)
        out["color"] = _color(raw.get("color"), f"{path}.color", p)
        if out["color_temp_kelvin"] is not None and out["color"] is not None:
            p.add(path, "Choose a colour temperature or a colour, not both: Home Assistant refuses the combination")
    effect = raw.get("effect")
    if isinstance(effect, str) and re.search(r"[{}%#]", effect):
        p.add(f"{path}.effect", "An effect name has no template characters")
        effect = None
    out["effect"] = "keep" if kept("effect") else (effect.strip()[:60] if isinstance(effect, str) and effect.strip() else None)
    out["transition"] = C.number(raw.get("transition"), f"{path}.transition", p, 0, 300)
    return out


def mood_cell(raw: Any, path: str, p: Problems, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "Unknown cell value")
        return {"kind": "none"}
    if raw["kind"] != "set":
        return {"kind": raw["kind"]}
    return {"kind": "set", **attributes(raw, path, p)}


def _column(raw: Any, path: str, p: Problems, seen: set[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind") if raw.get("kind") in COLUMN_KINDS else None
    if kind is None:
        p.add(f"{path}.kind", "Choose what this column is about")
        kind = "always"
    out: dict[str, Any] = {"id": _id(raw.get("id"), "c", f"{path}.id", p, seen), "kind": kind,
                           "label": C.text(raw.get("label"), f"{path}.label", p, limit=60)}
    if kind == "presence":
        source, state = raw.get("source"), raw.get("state")
        if not isinstance(source, str) or not P.SOURCE_ID.match(source) or state not in ("occupied", "vacant", "possible"):
            p.add(path, "A presence column is a presence source and a state")
            source, state = None, "occupied"
        out.update(source=source, state=state)
    elif kind == "state":
        out.update(entity_id=C.entity(raw.get("entity_id"), f"{path}.entity_id", p, what="an entity"),
                   state=model._state(raw.get("state"), f"{path}.state", p, required=False),  # noqa: SLF001
                   attribute=model._attribute(raw.get("attribute"), f"{path}.attribute", p),  # noqa: SLF001
                   for_seconds=C.seconds(raw.get("for_seconds"), f"{path}.for_seconds", p, 86400))
    elif kind == "numeric":
        low, high = C.number(raw.get("from"), f"{path}.from", p), C.number(raw.get("below"), f"{path}.below", p)
        if low is not None and high is not None and low >= high:
            p.add(path, "“From” must be lower than “below”")
        out.update(entity_id=C.entity(raw.get("entity_id"), f"{path}.entity_id", p, what="a sensor with a number"),
                   attribute=model._attribute(raw.get("attribute"), f"{path}.attribute", p),  # noqa: SLF001
                   **{"from": low, "below": high},
                   for_seconds=C.seconds(raw.get("for_seconds"), f"{path}.for_seconds", p, 3600))
    elif kind == "time":
        out.update(after=C.time_of_day(raw.get("after"), f"{path}.after", p),
                   before=C.time_of_day(raw.get("before"), f"{path}.before", p),
                   weekdays=C.weekdays(raw.get("weekdays"), f"{path}.weekdays", p))
    return out


def _matrix(raw: Any, path: str, p: Problems, lights: list[str], layer: str, seen: set[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    cseen: set[str] = set()
    columns = [_column(c, f"{path}.columns[{i}]", p, cseen)
               for i, c in enumerate((raw.get("columns") if isinstance(raw.get("columns"), list) else [])[:MAX_COLUMNS])]
    if isinstance(raw.get("columns"), list) and len(raw["columns"]) > MAX_COLUMNS:
        p.add(f"{path}.columns", f"At most {MAX_COLUMNS} columns per matrix")
    ids = [c["id"] for c in columns]
    cell, kinds = (truth_cell, TRUTH_KINDS) if layer == "truth" else (bright_cell, BRIGHT_KINDS)
    policy = raw.get("policy") if raw.get("policy") in POLICIES[layer] else None
    ranks = [r for r in (raw.get("ranks") if isinstance(raw.get("ranks"), list) else []) if r in ids]
    return {"id": _id(raw.get("id"), "m", f"{path}.id", p, seen),
            "name": C.text(raw.get("name"), f"{path}.name", p, limit=40) or ("Truth" if layer == "truth" else "Brightness"),
            "enabled": raw.get("enabled") is not False, "columns": columns,
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, lights, ids, cell, kinds),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, lights, cell, kinds),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, cell, kinds),
            "policy": policy, "ranks": list(dict.fromkeys(ranks))}


def _layer(raw: Any, path: str, p: Problems, lights: list[str], layer: str, seen: set[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    items = raw.get("matrices") if isinstance(raw.get("matrices"), list) else []
    if len(items) > MAX_MATRICES:
        p.add(f"{path}.matrices", f"At most {MAX_MATRICES} matrices")
    out = {"matrices": [_matrix(m, f"{path}.matrices[{i}]", p, lights, layer, seen) for i, m in enumerate(items[:MAX_MATRICES])],
           "combine": raw.get("combine") if raw.get("combine") in COMBINE[layer] else None}
    if layer == "brightness":
        out["zero"] = raw.get("zero") if raw.get("zero") in ZERO else None
    return out


def _mood(raw: Any, path: str, p: Problems, lights: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    values: list[str] = []
    for i, value in enumerate((raw.get("values") if isinstance(raw.get("values"), list) else [])[:MAX_VALUES]):
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            p.add(f"{path}.values[{i}]", "A mood value is the text of an option")
        elif value in values:
            p.add(f"{path}.values[{i}]", "This value is listed twice")
        else:
            values.append(value)
    auto = None
    if isinstance(raw.get("auto"), dict) and isinstance(raw["auto"].get("value"), str) and raw["auto"]["value"].strip():
        auto = {"value": raw["auto"]["value"], "entity_id": C.entity(raw["auto"].get("entity_id"), f"{path}.auto.entity_id",
                                                                     p, MOOD_DOMAINS, "the entity that holds the effective mood")}
        if auto["value"] in values:
            p.add(f"{path}.auto.value", f"“{auto['value']}” is a mood column and the automatic mode at once")
    return {"enabled": C.flag(raw.get("enabled")),
            "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, MOOD_DOMAINS, "the mood selector"),
            "values": values, "auto": auto,
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, lights, values, mood_cell, MOOD_KINDS),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, lights, mood_cell, MOOD_KINDS),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, mood_cell, MOOD_KINDS)}


def _trigger(raw: Any, path: str, p: Problems, allowed: tuple[str, ...] | None = None) -> dict[str, Any] | None:
    found = model._triggers([raw] if raw is not None else [], path, p, allowed=allowed)  # noqa: SLF001
    return found[0] if found else None


def _action(raw: Any, path: str, p: Problems, lights: list[str], allowed: tuple[str, ...]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind")
    if kind not in allowed:
        p.add(f"{path}.kind", "Choose what happens")
        return {"kind": "none"}
    out: dict[str, Any] = {"kind": kind}
    if kind == "brightness":
        out["pct"] = C.number(raw.get("pct"), f"{path}.pct", p, 1, 100, integer=True)
        if out["pct"] is None:
            p.add(f"{path}.pct", "Give the brightness (1-100 %; use OFF to switch off)")
    elif kind == "step":
        out["pct"] = C.number(raw.get("pct"), f"{path}.pct", p, -100, 100, integer=True)
        if not out["pct"]:
            p.add(f"{path}.pct", "Give the change in % (for example 10 or -10)")
    elif kind in ("mood", "select_mood"):
        out["value"] = raw.get("value") if isinstance(raw.get("value"), str) and raw["value"] else None
        if out["value"] is None:
            p.add(f"{path}.value", "Choose the mood")
    elif kind == "advanced":
        out["rule"] = raw.get("rule") if isinstance(raw.get("rule"), str) and ID.match(raw["rule"]) else None
        if out["rule"] is None:
            p.add(f"{path}.rule", "Choose the Advanced Rule")
    elif kind == "dim":
        out["direction"] = raw.get("direction") if raw.get("direction") in ("up", "down") else None
        out["step"] = C.number(raw.get("step"), f"{path}.step", p, 1, 50, integer=True)
        out["interval"] = C.number(raw.get("interval"), f"{path}.interval", p, 1, 5, integer=True)
        out["release"] = raw.get("release") if isinstance(raw.get("release"), str) and ID.match(raw["release"]) else None
        if out["direction"] is None or out["step"] is None or out["interval"] is None:
            p.add(path, "Hold-to-dim needs a direction, a step and an interval")
    elif kind == "native":
        action = raw.get("action")
        if action not in NATIVE_LIGHT_ACTIONS:
            p.add(f"{path}.action", "Choose a light action")
            action = "light.turn_on"
        data = raw.get("data") or {}
        if not isinstance(data, dict) or not model._json_size(data) or "entity_id" in data:  # noqa: SLF001
            p.add(f"{path}.data", "The action data must be a small JSON object without entity_id")
            data = {}
        colours = [k for k in model.LIGHT_COLOUR_KEYS if k in data]
        if len(colours) > 1:
            p.add(f"{path}.data", "Choose one way to set the colour")
        out.update(action=action, data={k: data[k] for k in sorted(data)})
    return out


def _switch(raw: Any, path: str, p: Problems, lights: list[str], seen: set[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind") if raw.get("kind") in ("latched", "momentary") else None
    if kind is None:
        p.add(f"{path}.kind", "Is it a latched switch (it stays on or off) or a momentary button?")
        kind = "momentary"
    iseen: set[str] = set()
    interactions = []
    for i, item in enumerate((raw.get("interactions") if isinstance(raw.get("interactions"), list) else [])[:MAX_INTERACTIONS]):
        item = item if isinstance(item, dict) else {}
        here = f"{path}.interactions[{i}]"
        allowed = LATCHED_TRIGGERS if kind == "latched" else MOMENTARY_TRIGGERS
        trigger = _trigger(item.get("trigger"), f"{here}.trigger", p, allowed)
        if trigger is None:
            p.add(f"{here}.trigger", "A latched switch reports states; a button reports presses (device triggers, "
                                     "events, MQTT or an event entity). Choose one Home Assistant offers.")
        if trigger is not None and kind == "latched" and trigger["for_seconds"]:
            p.add(f"{here}.trigger", "A switch interaction is the moment it changes, without “for …”")
        interactions.append({"id": _id(item.get("id"), "i", f"{here}.id", p, iseen),
                             "kind": item.get("kind") if item.get("kind") in INTERACTIONS else "other",
                             "label": C.text(item.get("label"), f"{here}.label", p, limit=60), "trigger": trigger})
    ids = [i["id"] for i in interactions]
    mseen: set[str] = set()
    mappings = []
    for i, item in enumerate((raw.get("mappings") if isinstance(raw.get("mappings"), list) else [])[:MAX_MAPPINGS]):
        item = item if isinstance(item, dict) else {}
        here = f"{path}.mappings[{i}]"
        interaction = item.get("interaction")
        if interaction not in ids:
            p.add(f"{here}.interaction", "Choose one of this control's interactions")
        mappings.append({"id": _id(item.get("id"), "p", f"{here}.id", p, mseen), "interaction": interaction if interaction in ids else None,
                         "enabled": item.get("enabled") is not False,
                         "lights": _lights_of(item.get("lights"), f"{here}.lights", p, lights),
                         "action": _action(item.get("action"), f"{here}.action", p, lights, ACTIONS),
                         "override": C.flag(item.get("override"))})
    return {"id": _id(raw.get("id"), "w", f"{path}.id", p, seen), "name": C.text(raw.get("name"), f"{path}.name", p, limit=60) or "Switch",
            "kind": kind, "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p),
            "ha_device_id": raw.get("ha_device_id") if isinstance(raw.get("ha_device_id"), str) and 0 < len(raw["ha_device_id"]) <= 64 else None,
            "interactions": interactions, "mappings": mappings,
            # where it came from, when that matters to the interface: events mapped by hand, or ordinary rules moved here
            **{flag: True for flag in ("manual", "adopted") if raw.get(flag) is True}}


def _advanced(raw: Any, path: str, p: Problems, lights: list[str], seen: set[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind") if raw.get("kind") in ("while", "when") else None
    if kind is None:
        p.add(f"{path}.kind", "Choose “while …” (a conditional override) or “when …” (a one-shot action)")
        kind = "while"
    conditions = model._group(raw.get("conditions"), f"{path}.conditions", p, 1, [model.MAX_CONDITIONS], 0)  # noqa: SLF001
    out: dict[str, Any] = {"id": _id(raw.get("id"), "a", f"{path}.id", p, seen),
                           "name": C.text(raw.get("name"), f"{path}.name", p, limit=60) or "Advanced Rule",
                           "enabled": raw.get("enabled") is not False, "kind": kind,
                           "lights": _lights_of(raw.get("lights"), f"{path}.lights", p, lights), "conditions": conditions}
    if kind == "while":
        set_raw = raw.get("set") if isinstance(raw.get("set"), dict) else {}
        truth = set_raw.get("truth") if set_raw.get("truth") in ("on", "off", "keep") else None
        out["set"] = {"truth": truth, **attributes(set_raw, f"{path}.set", p, keep=True)}
        out["triggers"], out["actions"] = [], []
    else:
        out["triggers"] = model._triggers(raw.get("triggers"), f"{path}.triggers", p, what="Add what starts it")  # noqa: SLF001
        out["actions"] = [_action(a, f"{path}.actions[{i}]", p, lights, ONE_SHOT_ACTIONS)
                          for i, a in enumerate((raw.get("actions") if isinstance(raw.get("actions"), list) else [])[:6])]
        out["set"] = None
    return out


def _override(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    release = []
    for i, item in enumerate((raw.get("release") if isinstance(raw.get("release"), list) else [])[:MAX_RELEASE]):
        found = _trigger(item, f"{path}.release[{i}]", p, tuple(x for x in model.TRIGGER_TYPES if x != "occupancy"))
        if found is not None and found not in release:
            release.append(found)
    source = raw.get("source") if isinstance(raw.get("source"), str) and P.SOURCE_ID.match(raw["source"]) else None
    return {"policy": raw.get("policy") if raw.get("policy") in OVERRIDE_POLICIES else None,
            "minutes": C.number(raw.get("minutes"), f"{path}.minutes", p, 1, 1440, integer=True),
            "source": source, "release": release, "manual": C.flag(raw.get("manual"))}


def validate(raw: dict[str, Any], designer_id: str, targets: list[str], path: str, p: Problems) -> dict[str, Any]:
    if raw.get("version") == 1:
        from . import lighting
        return lighting.validate(raw, designer_id, targets, path, p)
    lights: list[str] = []
    for i, light in enumerate(raw.get("lights") if isinstance(raw.get("lights"), list) else []):
        if light not in targets or not str(light).startswith("light."):
            p.add(f"{path}.lights[{i}]", f"{light} is not one of this device's lights")
        elif light not in lights:
            lights.append(light)
    lights = [t for t in targets if t in lights]
    seen: set[str] = set()
    priority = raw.get("priority")
    if not (isinstance(priority, list) and sorted(priority) == sorted(LAYERS)):
        if priority is not None:
            p.add(f"{path}.priority", "The priority lists every layer once")
        priority = list(LAYERS)
    switches = [_switch(s, f"{path}.switches[{i}]", p, lights, seen)
                for i, s in enumerate((raw.get("switches") if isinstance(raw.get("switches"), list) else [])[:MAX_SWITCHES])]
    advanced = [_advanced(a, f"{path}.advanced[{i}]", p, lights, seen)
                for i, a in enumerate((raw.get("advanced") if isinstance(raw.get("advanced"), list) else [])[:MAX_ADVANCED])]
    reconcile = raw.get("reconcile_minutes")
    if reconcile not in (None, "", 0) and reconcile not in model.PERIODIC_CHOICES:
        p.add(f"{path}.reconcile_minutes", f"Choose one of {', '.join(map(str, model.PERIODIC_CHOICES[1:]))} minutes")
        reconcile = None
    out = {"id": designer_id, "kind": KIND, "version": VERSION,
           "name": C.text(raw.get("name"), f"{path}.name", p) or DEFAULT_NAME,
           "enabled": raw.get("enabled") is not False, "lights": lights,
           "truth": _layer(raw.get("truth"), f"{path}.truth", p, lights, "truth", seen),
           "brightness": _layer(raw.get("brightness"), f"{path}.brightness", p, lights, "brightness", seen),
           "mood": _mood(raw.get("mood"), f"{path}.mood", p, lights),
           "switches": switches, "advanced": advanced,
           "override": _override(raw.get("override"), f"{path}.override", p),
           "priority": list(priority), "reconcile_minutes": reconcile or None,
           "on_available": C.flag(raw.get("on_available"))}
    if isinstance(raw.get("converted_from"), dict):  # the record of a conversion from version 1 (kept for the report)
        out["converted_from"] = {k: v for k, v in raw["converted_from"].items() if k in ("version", "at", "notes")
                                 and model._json_size(v)}  # noqa: SLF001
    return out


def sources_of(designer: dict[str, Any]) -> set[str]:
    if designer.get("version") == 1:
        from . import lighting
        return lighting.sources_of(designer)
    out = {c["source"] for layer in ("truth", "brightness") for m in designer[layer]["matrices"]
           for c in m["columns"] if c["kind"] == "presence" and c["source"]}
    if designer["override"]["source"]:
        out.add(designer["override"]["source"])
    return out


# ================================================================== helpers in Home Assistant
def _slug(text: str) -> str:
    """What Home Assistant's ``slugify`` makes of a helper name (lower case, runs of other characters -> one _)."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def helper_ids(device_id: str, light: str) -> dict[str, str]:
    """The helpers that hold a light's manual override (created by the App at deployment, owned by it)."""
    hexid = device_id.split("_", 1)[-1]
    name = f"AB {hexid} {light.split('.', 1)[1]} override"
    item = _slug(name)
    return {"name": name, "item_id": item, "flag": f"input_boolean.{item}", "timer": f"timer.{item}"}


# ================================================================== labels
def truth_label(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "UNSET"
    return {"on": "ON", "off": "OFF", "none": "NO ACTION", "default": "USE DEFAULT"}[cell["kind"]]


def bright_label(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "UNSET"
    if cell["kind"] == "set":
        return f"{plain(cell['pct'])}%" if cell["pct"] is not None else "…%"
    return {"none": "NO ACTION", "default": "USE DEFAULT"}[cell["kind"]]


def mood_label(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "UNSET"
    if cell["kind"] != "set":
        return {"none": "NO ACTION", "default": "USE DEFAULT"}[cell["kind"]]
    return attributes_label(cell) or "(nothing set)"


def attributes_label(a: dict[str, Any]) -> str:
    parts = []
    if a.get("brightness_pct") is not None:
        parts.append("brightness unchanged" if a["brightness_pct"] == "keep" else f"{plain(a['brightness_pct'])}%")
    if a.get("color") == "keep":
        parts.append("colour unchanged")
    elif a.get("color_temp_kelvin") is not None:
        parts.append(f"{plain(a['color_temp_kelvin'])}K")
    elif a.get("color"):
        parts.append("RGB " + ",".join(str(plain(v)) for v in a["color"]["rgb"]) if "rgb" in a["color"]
                     else f"HS {plain(a['color']['hs'][0]):g}/{plain(a['color']['hs'][1]):g}")
    if a.get("effect"):
        parts.append("effect unchanged" if a["effect"] == "keep" else f"effect {a['effect']}")
    if a.get("transition") not in (None, 0):
        parts.append(f"{plain(a['transition']):g} s fade")
    return " · ".join(parts)


def resolved_label(res: dict[str, Any], label_fn: Any) -> str:
    if res["state"] == "unset":
        return "UNSET"
    if res["state"] == "missing_default":
        return "DEFAULT: none configured"
    if res["state"] == "default":
        return f"DEFAULT: {label_fn(res['cell'])}"
    if res["state"] == "none" and res.get("inherited"):
        return "DEFAULT: NO ACTION"
    return label_fn(res["cell"])


def _clean(text: Any) -> str:
    """A label that goes into a template literal: without the characters that start template syntax."""
    return re.sub(r"[{}%#\\]", "", str(plain(text)))[:90]


def hs_of(rgb: list[int]) -> list[float]:
    """Hue (degrees) and saturation (%) of an RGB colour, as Home Assistant reports ``hs_color``."""
    r, g, b = (plain(c) / 255 for c in rgb)
    high, low = max(r, g, b), min(r, g, b)
    span = high - low
    if span == 0:
        return [0.0, 0.0]
    if high == r:
        hue = ((g - b) / span) % 6
    elif high == g:
        hue = (b - r) / span + 2
    else:
        hue = (r - g) / span + 4
    return [round(hue * 60 % 360, 1), round(span / high * 100, 1)]


def color_value(a: dict[str, Any]) -> Any:
    """The colour of a mood cell or rule as the evaluation carries it: {'ct'}, {'rgb', 'hs'}, {'hs'}, 'keep' or None."""
    if a.get("color") == "keep":
        return "keep"
    if a.get("color_temp_kelvin") is not None:
        return {"ct": plain(a["color_temp_kelvin"])}
    color = a.get("color")
    if color and "rgb" in color:
        return {"rgb": list(color["rgb"]), "hs": hs_of(color["rgb"])}
    if color and "hs" in color:
        return {"hs": list(color["hs"])}
    return None


# ================================================================== templates and their Python twins
ALWAYS_TEMPLATE = "{{ true }}"


def j_pick(names: list[str], how: str) -> str:
    """Combine [value, by] pairs (None = undecided) of several matrices."""
    items = "[" + ", ".join(names) + "]"
    if len(names) == 1:
        return "{{ " + names[0] + " }}"
    if how == "priority":
        return "{{ (" + items + " | reject('none') | list + [none]) | first }}"
    if how in ("on_wins", "off_wins", "exclusive_values"):
        order = ["on", "off", "keep"] if how == "on_wins" else ["off", "on", "keep"]
        return ("{% set ns = namespace(r=none) %}{% for want in " + str(order) + " %}{% for p in " + items + " %}"
                "{% if ns.r is none and p is not none and p[0] == want %}{% set ns.r = p %}{% endif %}{% endfor %}"
                "{% endfor %}{{ ns.r }}")
    cmp = ">" if how == "max" else "<"
    return ("{% set ns = namespace(r=none, n=none) %}{% for p in " + items + " %}{% if p is not none and p[0] != 'keep' %}"
            "{% set n = 0 if p[0] == 'off' else p[0] %}{% if ns.n is none or n " + cmp + " ns.n %}{% set ns.r = p %}"
            "{% set ns.n = n %}{% endif %}{% endif %}{% endfor %}{% if ns.r is none %}{% for p in " + items + " %}"
            "{% if ns.r is none and p is not none %}{% set ns.r = p %}{% endif %}{% endfor %}{% endif %}{{ ns.r }}")


def pick(values: list[Any], how: str) -> Any:
    """The Python twin of :func:`j_pick`."""
    if len(values) == 1:
        return values[0]
    present = [v for v in values if v is not None]
    if how == "priority":
        return present[0] if present else None
    if how in ("on_wins", "off_wins"):
        for want in (["on", "off", "keep"] if how == "on_wins" else ["off", "on", "keep"]):
            for v in present:
                if v[0] == want:
                    return v
        return None
    best, number = None, None
    for v in present:
        if v[0] == "keep":
            continue
        n = 0 if v[0] == "off" else v[0]
        if number is None or (n > number if how == "max" else n < number):
            best, number = v, n
    return best if best is not None else (present[0] if present else None)


def j_resolve(entries: list[dict[str, Any]]) -> str:
    out = "{% set ns = namespace(r=none) %}"
    for e in entries:
        out += "{% if ns.r is none and (" + e["when"] + ") %}{% set ns.r = " + e["then"] + " %}{% endif %}"
    return out + "{{ ns.r }}"


def _num(v: Any, default: int) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return default


COMMAND_TEMPLATE = (
    "{% set s = states[LIGHT] %}"
    "{% set on = (ab_on or [none])[0] %}{% set bri = (ab_bri or [none])[0] %}{% set col = (ab_col or [none])[0] %}"
    "{% set fx = (ab_fx or [none])[0] %}{% set tr = (ab_tr or [none])[0] %}"
    "{% if s is none or s.state not in ['on', 'off'] %}{{ {'service': none, 'why': 'unavailable'} }}"
    "{% else %}"
    "{% if bri == 'off' and on != 'off' and (on == 'on' or s.state == 'on') %}{% set on = 'off' %}{% endif %}"
    "{% if on == 'off' %}"
    "{% if s.state == 'on' %}{{ {'service': 'turn_off', 'data': ({'transition': tr} if tr is number else {})} }}"
    "{% else %}{{ {'service': none, 'why': 'already off'} }}{% endif %}"
    "{% elif on == 'on' or s.state == 'on' %}"
    "{% set a = s.attributes %}{% set d = namespace(v={}, change=(s.state != 'on')) %}"
    "{% if bri is number %}{% set d.v = dict(d.v, brightness_pct=bri) %}"
    "{% if ((a.get('brightness') | int(-99)) - (bri * 2.55) | round(0)) | abs > TOL_B %}{% set d.change = true %}{% endif %}{% endif %}"
    "{% if col is mapping %}"
    "{% if 'ct' in col %}{% set d.v = dict(d.v, color_temp_kelvin=col['ct']) %}"
    "{% set want = [[col['ct'], a.get('min_color_temp_kelvin') | int(0)] | max, a.get('max_color_temp_kelvin') | int(100000)] | min %}"
    "{% if a.get('color_mode') != 'color_temp' or ((a.get('color_temp_kelvin') | int(-9999)) - want) | abs > TOL_K %}"
    "{% set d.change = true %}{% endif %}"
    "{% else %}"
    "{% if 'rgb' in col %}{% set d.v = dict(d.v, rgb_color=col['rgb']) %}{% else %}{% set d.v = dict(d.v, hs_color=col['hs']) %}{% endif %}"
    "{% set hs = a.get('hs_color') %}"
    "{% if a.get('color_mode') in [none, 'color_temp', 'onoff', 'brightness', 'white'] or hs is none "
    "or (((hs[0] - col['hs'][0]) + 540) % 360 - 180) | abs > TOL_H or (hs[1] - col['hs'][1]) | abs > TOL_S %}"
    "{% set d.change = true %}{% endif %}"
    "{% endif %}{% endif %}"
    "{% if fx is string and fx != 'keep' %}{% set d.v = dict(d.v, effect=fx) %}"
    "{% if a.get('effect') != fx %}{% set d.change = true %}{% endif %}{% endif %}"
    "{% if d.change %}{% if tr is number %}{% set d.v = dict(d.v, transition=tr) %}{% endif %}"
    "{{ {'service': 'turn_on', 'data': d.v} }}{% else %}{{ {'service': none, 'why': 'already so'} }}{% endif %}"
    "{% else %}{{ {'service': none, 'why': 'off, and nothing switches it on'} }}{% endif %}"
    "{% endif %}"
).replace("TOL_B", str(TOL_BRIGHTNESS)).replace("TOL_K", str(TOL_KELVIN)).replace("TOL_H", str(TOL_HUE)).replace("TOL_S", str(TOL_SAT))


def command(state: dict[str, Any] | None, resolved: dict[str, Any]) -> dict[str, Any]:
    """The Python twin of :data:`COMMAND_TEMPLATE`: what the light gets (``service`` None: nothing)."""
    value = lambda key: (resolved.get(key) or [None])[0]  # noqa: E731
    on, bri, col, fx, tr = value("on"), value("brightness"), value("color"), value("effect"), value("transition")
    if state is None or state.get("state") not in ("on", "off"):
        return {"service": None, "why": "unavailable"}
    lit = state["state"] == "on"
    if bri == "off" and on != "off" and (on == "on" or lit):
        on = "off"
    if on == "off":
        if lit:
            return {"service": "turn_off", "data": {"transition": tr} if isinstance(tr, (int, float)) else {}}
        return {"service": None, "why": "already off"}
    if not (on == "on" or lit):
        return {"service": None, "why": "off, and nothing switches it on"}
    a = state.get("attributes") or {}
    data: dict[str, Any] = {}
    change = not lit
    if isinstance(bri, (int, float)) and not isinstance(bri, bool):
        data["brightness_pct"] = bri
        if abs(_num(a.get("brightness"), -99) - round(bri * 2.55)) > TOL_BRIGHTNESS:
            change = True
    if isinstance(col, dict):
        if "ct" in col:
            data["color_temp_kelvin"] = col["ct"]
            want = min(max(col["ct"], _num(a.get("min_color_temp_kelvin"), 0)), _num(a.get("max_color_temp_kelvin"), 100000))
            if a.get("color_mode") != "color_temp" or abs(_num(a.get("color_temp_kelvin"), -9999) - want) > TOL_KELVIN:
                change = True
        else:
            if "rgb" in col:
                data["rgb_color"] = col["rgb"]
            else:
                data["hs_color"] = col["hs"]
            hs = a.get("hs_color")
            if (a.get("color_mode") in (None, "color_temp", "onoff", "brightness", "white") or hs is None
                    or abs(((hs[0] - col["hs"][0]) + 540) % 360 - 180) > TOL_HUE or abs(hs[1] - col["hs"][1]) > TOL_SAT):
                change = True
    if isinstance(fx, str) and fx != "keep":
        data["effect"] = fx
        if a.get("effect") != fx:
            change = True
    if not change:
        return {"service": None, "why": "already so"}
    if isinstance(tr, (int, float)) and not isinstance(tr, bool):
        data["transition"] = tr
    return {"service": "turn_on", "data": data}


def manual_template(light_ref: str) -> str:
    return ("{{ trigger.from_state is not none and trigger.to_state is not none and trigger.from_state.state in ['on', 'off'] "
            "and trigger.to_state.state in ['on', 'off'] and trigger.to_state.context.parent_id is none }}")


# ================================================================== the plan of one light
def _svc(action: str, entities: list[Any], data: Any = None) -> dict[str, Any]:
    return {"type": "service", "action": action, "target": list(entities), "data": data if data is not None else {}}


def _group(items: list[dict[str, Any]], kind: str = "and") -> dict[str, Any]:
    return {"type": kind, "items": list(items)}


def _if(conditions: list[dict[str, Any]], then: list[dict[str, Any]], otherwise: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "if", "conditions": _group(conditions), "then": then, "else": otherwise or []}


def _template(text: str) -> dict[str, Any]:
    return {"type": "template", "value_template": text}


def _duration(minutes: Any) -> str:
    total = int(plain(minutes)) * 60
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:00"


class _Gen:
    """The expansion of one Lighting Designer (version 2) into rules, and the plan of each light."""

    def __init__(self, d: dict[str, Any], x: Any) -> None:
        self.d, self.x = d, x
        self.did = d["id"]
        self.sources = x.sources
        self.ok = True
        self.plans: dict[str, dict[str, Any]] = {}
        self.view: dict[str, Any] = {"rules": [], "problems": [], "lights": {}, "helpers": [], "version": VERSION}
        self.items: dict[str, list[dict[str, Any]]] = {}   # column id -> its conditions
        self.device_id = x.device["id"]

    # ------------------------------------------------------------ problems
    def block(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("blocking", code, message, self.did, layer)
        self.ok = False

    def warn(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("warning", code, message, self.did, layer)

    def info(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("info", code, message, self.did, layer)

    def name(self, *parts: Any) -> str:
        return " · ".join(str(plain(p)) for p in (self.d["name"], *parts) if p not in (None, ""))

    # ------------------------------------------------------------ columns
    def source(self, col: dict[str, Any]) -> dict[str, Any] | None:
        return self.sources.get(col.get("source")) if col["kind"] == "presence" else None

    def col_label(self, col: dict[str, Any]) -> str:
        if col["label"]:
            return col["label"]
        kind = col["kind"]
        if kind == "presence":
            source = self.source(col)
            return P.label(source, col["state"]) if source else "(missing presence source)"
        if kind == "state":
            what = f"{col['entity_id'] or '…'}{'.' + col['attribute'] if col['attribute'] else ''}"
            return f"{what} is {col['state'] if col['state'] is not None else '…'}"
        if kind == "numeric":
            low, high = col["from"], col["below"]
            span = (f"{plain(low):g}–{plain(high):g}" if low is not None and high is not None
                    else f"below {plain(high):g}" if high is not None else f"from {plain(low):g}" if low is not None else "…")
            return f"{col['entity_id'] or '…'} {span}"
        if kind == "time":
            days = f" ({', '.join(col['weekdays'])})" if col["weekdays"] else ""
            return f"{(col['after'] or '00:00')[:5]}–{(col['before'] or '24:00')[:5]}{days}"
        return "Always"

    def col_missing(self, col: dict[str, Any]) -> str | None:
        kind = col["kind"]
        if kind == "presence":
            source = self.source(col)
            if source is None:
                return "a presence column refers to a source that no longer exists"
            if col["state"] not in P.STATES[source["kind"]]:
                return f"{source['name']} cannot be “{col['state']}”"
            return P.missing(source, col["state"])
        if kind == "state" and (not col["entity_id"] or col["state"] is None):
            return f"the column “{self.col_label(col)}” needs an entity and a state"
        if kind == "numeric" and (not col["entity_id"] or (col["from"] is None and col["below"] is None)):
            return f"the column “{self.col_label(col)}” needs a sensor and a range (from, below or both)"
        if kind == "time" and col["after"] is None and col["before"] is None and not col["weekdays"]:
            return f"the column “{self.col_label(col)}” needs a time range or weekdays"
        return None

    def col_items(self, col: dict[str, Any]) -> list[dict[str, Any]]:
        """The conditions that hold while the column's situation holds (all of them)."""
        if col["id"] in self.items:
            return self.items[col["id"]]
        x = self.x
        kind = col["kind"]
        if kind == "presence":
            items = list(P.active(self.source(col), col["state"], x))
        elif kind == "state":
            items = [C.c_state(x.e(col["entity_id"], "Matrix source", group="Sensor entities"), col["state"],
                               for_seconds=col["for_seconds"], attribute=col["attribute"])]
        elif kind == "numeric":
            sensor = x.e(col["entity_id"], "Matrix source", group="Sensor entities")
            items = []
            if col["below"] is not None:
                items.append(C.c_numeric(sensor, below=col["below"], attribute=col["attribute"]))
            if col["from"] is not None:
                items.append(C.group("not", [C.c_numeric(sensor, below=col["from"], attribute=col["attribute"])]))
            if col["below"] is None:  # "not below" is also true for a sensor without a number
                items += [C.c_state(sensor, "unavailable", negate=True), C.c_state(sensor, "unknown", negate=True)]
        elif kind == "time":
            items = [C.c_time(col["after"], col["before"], col["weekdays"])]
        else:
            items = [x.refs.template(ALWAYS_TEMPLATE, lambda states: True)]
        self.items[col["id"]] = items
        return items

    def col_triggers(self, col: dict[str, Any]) -> list[dict[str, Any]]:
        """When the column's situation can begin or end (the evaluation runs then)."""
        x = self.x
        kind = col["kind"]
        if kind == "presence":
            source = self.source(col)
            return [P.entered(source, col["state"], x), P.ended(source, col["state"], x)]
        if kind == "state":
            entity = x.e(col["entity_id"], "Matrix source", group="Sensor entities")
            return [C.t_state(entity, to=col["state"], for_seconds=col["for_seconds"], attribute=col["attribute"]),
                    C.t_state(entity, frm=col["state"], attribute=col["attribute"])]
        if kind == "numeric":
            sensor = x.e(col["entity_id"], "Matrix source", group="Sensor entities")
            out = []
            for limit in (col["below"], col["from"]):
                if limit is not None:  # crossing the limit either way, held for the delay
                    out += [C.t_numeric(sensor, below=limit, for_seconds=col["for_seconds"], attribute=col["attribute"]),
                            C.t_numeric(sensor, above=limit, for_seconds=col["for_seconds"], attribute=col["attribute"])]
            return out
        if kind == "time":
            out = [C.t_time(t) for t in (col["after"], col["before"]) if t]
            if col["weekdays"]:
                out.append(C.t_time("00:00:00"))
            return out
        return []

    def overlaps(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Can two columns hold at the same time? Only the certain exclusions count."""
        if "always" in (a["kind"], b["kind"]):
            return True
        if a["kind"] != b["kind"]:
            return True
        if a["kind"] == "presence":
            return not (a["source"] == b["source"] and {a["state"], b["state"]} == {"occupied", "vacant"})
        if a["kind"] == "state":
            return not (a["entity_id"] == b["entity_id"] and a["attribute"] == b["attribute"] and a["state"] != b["state"])
        if a["kind"] == "numeric":
            if a["entity_id"] != b["entity_id"] or a["attribute"] != b["attribute"]:
                return True
            lo_a, hi_a = a["from"] if a["from"] is not None else float("-inf"), a["below"] if a["below"] is not None else float("inf")
            lo_b, hi_b = b["from"] if b["from"] is not None else float("-inf"), b["below"] if b["below"] is not None else float("inf")
            return lo_a < hi_b and lo_b < hi_a
        if a["kind"] == "time":
            days = lambda c: set(c["weekdays"] or model.WEEKDAYS)  # noqa: E731
            if not days(a) & days(b):
                return False
            span = lambda c: _minutes(c["after"], c["before"])  # noqa: E731
            return bool(span(a) & span(b))
        return True

    # ------------------------------------------------------------ values
    def value(self, layer: str, res: dict[str, Any], where: str) -> Any:
        """A resolved cell as the evaluation carries it: 'on'/'off'/'keep' or a percentage/'off'/'keep'; None: unset."""
        if res["state"] == "missing_default":
            self.block("missing_default", f"“{self.d['name']}”: {where} says “Use default”, but no default is configured "
                       "(for the light or the matrix).", layer)
            return None
        if res["state"] == "none":
            return "keep"
        cell = C.acting(res)
        if cell is None:
            return None
        if layer == "truth":
            return cell["kind"]
        pct = plain(cell["pct"])
        if pct == 0:
            zero = self.d["brightness"]["zero"]
            if zero is None:
                self.block("zero_brightness", f"“{self.d['name']}”: {where} is 0 %. 0 % never becomes OFF by itself: "
                           "choose under Brightness what 0 % means (switch the light off, the lowest brightness, or "
                           "leave the brightness unchanged).", layer)
                return None
            return {"off": "off", "minimum": 1, "unchanged": "keep"}[zero]
        return pct

    def matrix_plan(self, m: dict[str, Any], layer: str, light: str) -> dict[str, Any] | None:
        """The branches of one matrix for one light (first matching branch wins), or None when it decides nothing."""
        tag = "Truth" if layer == "truth" else "Brightness"
        decided = []
        for col in m["columns"]:
            res = C.resolve((m["cells"].get(light) or {}).get(col["id"]), m["row_defaults"].get(light), m["default"])
            v = self.value(layer, res, f"{light} / {m['name']} / {self.col_label(col)}")
            if v is not None:
                decided.append((col, v))
        if not decided:
            return None
        for col, _v in decided:
            missing = self.col_missing(col)
            if missing:
                self.block("missing_source", f"“{self.d['name']}”: {missing}. Nothing is chosen for you.", layer)
            elif col["kind"] == "presence" and self.source(col)["kind"] == "position" and not (
                    self.source(col)["entry_seconds"] or self.source(col)["exit_seconds"]):
                self.warn("position_delay", f"“{self.source(col)['name']}” is a position zone without entry or exit delay: "
                          "a half-updated x/y reading can count for a moment (a delay of one or two seconds avoids that).", layer)
        if not self.ok:
            return None
        clashes = [(a, b) for i, (a, va) in enumerate(decided) for b, vb in decided[i + 1:] if va != vb and self.overlaps(a, b)]
        policy = m["policy"]
        label = lambda col: _clean(f"{tag} · {m['name']}: {self.col_label(col)}")  # noqa: E731 (a variable value: no template syntax)
        cond = lambda cols: (_group(self.col_items(cols[0])) if len(cols) == 1 else  # noqa: E731
                             _group([_group([_group(self.col_items(c)) for c in cols], "or")]))
        if clashes and policy is None:
            a, b = clashes[0]
            self.block("no_policy", f"“{self.d['name']}”: in “{m['name']}”, {self.col_label(a)} and {self.col_label(b)} can hold "
                       f"at the same time and want different things for {light}. Choose how this matrix decides then "
                       "(an explicit ranking, " + ("ON wins, OFF wins" if layer == "truth" else "the highest, the lowest")
                       + " or nothing while they disagree). The order of the columns decides nothing.", layer)
            return None
        branches: list[dict[str, Any]] = []
        if not clashes:
            branches = [{"value": [v, label(c)], "conditions": cond([c]), "columns": [c["id"]]} for c, v in decided]
        elif policy == "priority":
            unranked = sorted({c["id"] for pair in clashes for c in pair} - set(m["ranks"]))
            if unranked:
                names = ", ".join(self.col_label(c) for c, _v in decided if c["id"] in unranked)
                self.block("unranked", f"“{self.d['name']}”: “{m['name']}” decides by an explicit ranking, but {names} "
                           "are not ranked yet (Priority & Resolution).", layer)
                return None
            order = [c for r in m["ranks"] for c, _v in decided if c["id"] == r] + [c for c, _v in decided if c["id"] not in m["ranks"]]
            branches = [{"value": [dict((c["id"], v) for c, v in decided)[col["id"]], label(col)], "conditions": cond([col]),
                         "columns": [col["id"]]} for col in order]
        elif policy == "exclusive":
            values = list(dict.fromkeys(v for _c, v in decided))
            for v in values:
                mine = [c for c, cv in decided if cv == v]
                others = [c for c, cv in decided if cv != v]
                items = [*cond(mine)["items"]]
                if others:
                    items.append(C.negation([_group([_group(self.col_items(c)) for c in others], "or")]))
                branches.append({"value": [v, _clean(f"{tag} · {m['name']} ({POLICY_NAMES['exclusive']})")], "conditions": _group(items),
                                 "columns": [c["id"] for c in mine]})
        else:
            values = list(dict.fromkeys(v for _c, v in decided))
            if policy in ("on_wins", "off_wins"):
                rank = {"on": 0, "off": 1, "keep": 2} if policy == "on_wins" else {"off": 0, "on": 1, "keep": 2}
                values.sort(key=lambda v: rank[v])
            else:
                numeric = lambda v: -1 if v == "keep" else 0 if v == "off" else v  # noqa: E731
                values.sort(key=lambda v: (v == "keep", -numeric(v) if policy == "max" else numeric(v)))
            for v in values:
                mine = [c for c, cv in decided if cv == v]
                branches.append({"value": [v, _clean(f"{tag} · {m['name']} ({POLICY_NAMES[policy]})")], "conditions": cond(mine),
                                 "columns": [c["id"] for c in mine]})
        triggers = [t for c, _v in decided for t in self.col_triggers(c)]
        return {"id": m["id"], "name": m["name"], "var": f"{layer[0]}_{m['id']}", "branches": branches, "triggers": triggers}

    # ------------------------------------------------------------ mood
    def mood_entity(self) -> Any:
        return self.x.e(self.d["mood"]["entity_id"], "The mood selector", group="Trigger entities",
                        name=f"{self.d['name']}: mood selector")

    def mood_is(self, value: str) -> list[dict[str, Any]]:
        mood = self.d["mood"]
        own = C.c_state(self.mood_entity(), value)
        if mood["auto"]:
            auto_entity = self.x.e(mood["auto"]["entity_id"], "The entity that holds the effective mood", group="Trigger entities")
            return [C.any_of([own, C.group("and", [C.c_state(self.mood_entity(), mood["auto"]["value"]),
                                                   C.c_state(auto_entity, value)])])]
        return [own]

    def mood_plan(self, light: str) -> dict[str, Any] | None:
        mood = self.d["mood"]
        if not mood["enabled"]:
            return None
        branches = []
        for value in mood["values"]:
            res = C.resolve((mood["cells"].get(light) or {}).get(value), mood["row_defaults"].get(light), mood["default"])
            if res["state"] == "missing_default":
                self.block("missing_default", f"“{self.d['name']}”: {light} / mood {value} says “Use default”, but no "
                           "default is configured.", "mood")
                continue
            cell = C.acting(res)
            if cell is None:
                continue
            carried = {"by": f"Mood: {_clean(value)}"}
            if cell["brightness_pct"] is not None:
                carried["bri"] = plain(cell["brightness_pct"])
            if color_value(cell) is not None:
                carried["color"] = color_value(cell)
            if cell["effect"]:
                carried["effect"] = cell["effect"]
            if cell["transition"] is not None:
                carried["tr"] = plain(cell["transition"])
            if len(carried) > 1:
                branches.append({"value": carried, "conditions": _group(self.mood_is(value)), "mood": value})
        if not branches:
            return None
        if not mood["entity_id"]:
            self.block("missing_source", f"“{self.d['name']}”: choose the mood selector (the entity whose state is the "
                       "mood). Nothing is chosen for you.", "mood")
            return None
        if mood["auto"] and not mood["auto"]["entity_id"]:
            self.block("missing_source", f"“{self.d['name']}”: “{mood['auto']['value']}” is the automatic mode: choose "
                       "the entity that holds the effective mood.", "mood")
            return None
        triggers = [C.t_state(self.mood_entity())]
        if mood["auto"]:
            triggers.append(C.t_state(self.x.e(mood["auto"]["entity_id"], "The entity that holds the effective mood",
                                               group="Trigger entities")))
        return {"branches": branches, "triggers": triggers}

    # ------------------------------------------------------------ Advanced Rules
    def condition_triggers(self, group: dict[str, Any], where: str) -> list[dict[str, Any]]:
        """When the conditions of a "while" rule can change (so the evaluation runs at that moment)."""
        out: list[dict[str, Any]] = []
        for leaf in model.condition_leaves(group):
            kind = leaf["type"]
            if kind == "state":
                out += [C.t_state(leaf["entity_id"], to=leaf["state"], for_seconds=leaf["for_seconds"], attribute=leaf["attribute"]),
                        C.t_state(leaf["entity_id"], frm=leaf["state"], attribute=leaf["attribute"])]
            elif kind == "numeric":
                for limit in (leaf["above"], leaf["below"]):
                    if limit is not None:
                        out += [C.t_numeric(leaf["entity_id"], above=limit, attribute=leaf["attribute"]),
                                C.t_numeric(leaf["entity_id"], below=limit, attribute=leaf["attribute"])]
            elif kind == "time":
                out += [C.t_time(t) for t in (leaf["after"], leaf["before"]) if t]
                if leaf["weekdays"]:
                    out.append(C.t_time("00:00:00"))
            elif kind == "sun":
                for key in ("after", "before"):
                    if leaf[key]:
                        out.append({"type": "sun", "event": leaf[key], "offset_minutes": leaf[f"{key}_offset_minutes"]})
            elif kind == "template":
                body = model.simple_template(leaf["value_template"])
                out.append(C.t_template(leaf["value_template"]))
                if body is not None:
                    out.append(C.t_template("{{ not (" + body + ") }}"))
                else:
                    self.warn("template_release", f"“{where}”: a template with statements is checked when it becomes "
                              "true; its end is noticed at the next change of another input (or the reconciliation).", "advanced")
            else:
                self.warn("no_trigger", f"“{where}”: a {kind} condition has no moment Home Assistant can report; it is "
                          "checked whenever another input changes (or at the reconciliation).", "advanced")
        return out

    def advanced_plan(self, light: str) -> list[dict[str, Any]]:
        out = []
        for rule in self.d["advanced"]:
            if not rule["enabled"] or rule["kind"] != "while" or light not in rule["lights"]:
                continue
            if not rule["conditions"]["items"]:
                self.block("no_condition", f"“{rule['name']}”: a “while” rule needs a condition.", "advanced")
                continue
            if all(v is None for v in rule["set"].values()):
                self.info("sets_nothing", f"“{rule['name']}” sets nothing while it is active.", "advanced")
                continue
            out.append({"id": rule["id"], "name": rule["name"], "var": f"a_{rule['id']}", "conditions": rule["conditions"],
                        "set": rule["set"], "triggers": self.condition_triggers(rule["conditions"], rule["name"])})
        return out

    # ------------------------------------------------------------ overrides
    def overridable(self) -> list[str]:
        """The lights that can be under a manual override (they get the helpers)."""
        d = self.d
        if d["override"]["manual"]:
            return list(d["lights"])
        found = set()
        for switch in d["switches"]:
            for m in switch["mappings"]:
                if m["enabled"] and (m["override"] or m["action"]["kind"] == "override"):
                    found.update(m["lights"])
        return [light for light in d["lights"] if light in found]

    def helpers(self, light: str) -> dict[str, str]:
        return helper_ids(self.device_id, light)

    # ------------------------------------------------------------ resolution
    def resolve_entries(self, plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
        """Per property, in priority order: what can decide it (a Jinja condition and value, and the Python twin)."""
        out: dict[str, list[dict[str, Any]]] = {prop: [] for prop in PROPERTIES}
        for layer in self.d["priority"]:
            for prop in PROPERTIES:
                if layer not in PROPERTIES[prop]:
                    continue
                if layer == "advanced":
                    for rule in plan["advanced"]:
                        v = _rule_value(rule["set"], prop)
                        if v is not None:
                            pair = [v, f"Advanced: {_clean(rule['name'])}"]
                            out[prop].append({"layer": layer, "when": rule["var"], "then": _jinja(pair),
                                              "twin": ("rule", rule["var"], pair)})
                elif layer == "override" and plan["override"]:
                    pair = ["keep", "Manual override"]
                    out[prop].append({"layer": layer, "when": "ab_ovr", "then": _jinja(pair), "twin": ("override", pair)})
                elif layer == "truth" and plan["truth"]:
                    out[prop].append({"layer": layer, "when": "ab_truth is not none", "then": "ab_truth", "twin": ("var", "ab_truth")})
                elif layer == "brightness" and plan["brightness"] and prop == "brightness":
                    out[prop].append({"layer": layer, "when": "ab_bright is not none", "then": "ab_bright", "twin": ("var", "ab_bright")})
                elif layer == "mood" and plan["mood"]:
                    key = {"brightness": "bri", "color": "color", "effect": "effect", "transition": "tr"}[prop]
                    out[prop].append({"layer": layer, "when": f"ab_mood.get('{key}') is not none",
                                      "then": f"[ab_mood.get('{key}'), ab_mood.get('by')]", "twin": ("mood", key)})
        return out

    # ------------------------------------------------------------ the evaluation of one light
    def light_plan(self, light: str) -> dict[str, Any] | None:
        d = self.d
        plan: dict[str, Any] = {"light": light, "truth": [], "brightness": [], "mood": None, "advanced": [], "override": None}
        for layer in ("truth", "brightness"):
            for m in d[layer]["matrices"]:
                if m["enabled"]:
                    found = self.matrix_plan(m, layer, light)
                    if found:
                        plan[layer].append(found)
            if len(plan[layer]) > 1 and d[layer]["combine"] is None:
                self.block("no_combine", f"“{d['name']}”: {len(plan[layer])} {LAYER_NAMES[layer]} matrices decide for {light}. "
                           "Choose how they combine (Priority & Resolution): " + ("an explicit order, ON wins or OFF wins."
                                                                                  if layer == "truth" else "an explicit order, the highest or the lowest."), layer)
        plan["truth_combine"] = d["truth"]["combine"] or "priority"
        plan["bright_combine"] = d["brightness"]["combine"] or "priority"
        plan["mood"] = self.mood_plan(light)
        plan["advanced"] = self.advanced_plan(light)
        if light in self.overridable():
            plan["override"] = self.helpers(light)["flag"]
        if not (plan["truth"] or plan["brightness"] or plan["mood"] or plan["advanced"]):
            return None
        plan["resolve"] = self.resolve_entries(plan)
        return plan

    def evaluator_rule(self, plan: dict[str, Any]) -> None:
        light = plan["light"]
        x = self.x
        target = x.e(light, "Controlled light")
        init: dict[str, Any] = {}
        actions: list[dict[str, Any]] = []
        for layer in ("truth", "brightness"):
            for m in plan[layer]:
                init[m["var"]] = None
        if plan["mood"]:
            init["ab_mood"] = {}
        for rule in plan["advanced"]:
            init[rule["var"]] = False
        if init:
            actions.append({"type": "variables", "variables": init})
        for layer in ("truth", "brightness"):
            for m in plan[layer]:
                actions.append({"type": "choose", "default": [], "branches": [
                    {"conditions": b["conditions"], "actions": [{"type": "variables", "variables": {m["var"]: b["value"]}}]}
                    for b in m["branches"]]})
        if plan["mood"]:
            actions.append({"type": "choose", "default": [], "branches": [
                {"conditions": b["conditions"], "actions": [{"type": "variables", "variables": {"ab_mood": b["value"]}}]}
                for b in plan["mood"]["branches"]]})
        for rule in plan["advanced"]:
            actions.append({"type": "if", "conditions": rule["conditions"],
                            "then": [{"type": "variables", "variables": {rule["var"]: True}}], "else": []})
        resolution: dict[str, Any] = {}
        if plan["override"]:
            resolution["ab_ovr"] = "{{ is_state(" + quote(plan["override"]) + ", 'on') }}"
        if plan["truth"]:
            resolution["ab_truth"] = j_pick([m["var"] for m in plan["truth"]], plan["truth_combine"])
        if plan["brightness"]:
            resolution["ab_bright"] = j_pick([m["var"] for m in plan["brightness"]], plan["bright_combine"])
        for prop, var in (("on", "ab_on"), ("brightness", "ab_bri"), ("color", "ab_col"), ("effect", "ab_fx"), ("transition", "ab_tr")):
            resolution[var] = j_resolve(plan["resolve"][prop]) if plan["resolve"][prop] else None
        actions.append({"type": "variables", "variables": resolution})
        actions.append({"type": "variables", "variables": {
            "ab_cmd": COMMAND_TEMPLATE.replace("LIGHT", x.refs.ref(target))}})
        for service in ("turn_off", "turn_on"):
            actions.append(_if([_template("{{ ab_cmd.service == '" + service + "' }}")],
                               [_svc(f"light.{service}", [target], "{{ ab_cmd.data }}")]))
        triggers = [t for layer in ("truth", "brightness") for m in plan[layer] for t in m["triggers"]]
        triggers += plan["mood"]["triggers"] if plan["mood"] else []
        triggers += [t for rule in plan["advanced"] for t in rule["triggers"]]
        if plan["override"]:
            triggers.append(C.t_state(plan["override"]))
        triggers.append({"type": "event", "event_type": EVENT, "event_data": {"light": plain(light)}})
        if self.d["reconcile_minutes"]:
            triggers.append({"type": "pattern", "hours": None, "minutes": f"/{self.d['reconcile_minutes']}", "seconds": None})
        if self.d["on_available"]:
            triggers.append(C.t_state(target, frm="unavailable"))
        self.add("evaluate", light, f"{light} · automatic control", triggers, actions, group=light)

    def add(self, layer: str, light: str | None, label: str, triggers: list[dict[str, Any]], actions: list[dict[str, Any]],
            *, conditions: list[dict[str, Any]] | None = None, group: str | None = None, key: Any = None) -> str:
        rule_id = C.generated_rule_id(self.did, layer, light, key)
        meta = {"designer_id": self.did, "designer": KIND, "layer": layer, "target": light, "column": key, "mood": None,
                "exclusive": {}, "cell": None, "resolution": {}}
        if group:
            meta["group"] = group
        raw = C.rule(rule_id, self.name(label), triggers, conditions or [], actions, meta)
        self.x.add_rule(raw, self.did)
        self.view["rules"].append(rule_id)
        return rule_id

    # ------------------------------------------------------------ switches and remotes
    def actions_for(self, action: dict[str, Any], lights: list[str], where: str) -> list[dict[str, Any]]:
        """The native steps of a switch mapping's (or a one-shot rule's) action on these lights."""
        x = self.x
        targets = [x.e(light, "Controlled light") for light in lights]
        kind = action["kind"]
        if kind == "toggle":
            return [_svc("light.toggle", targets)]
        if kind == "on":
            return [_svc("light.turn_on", targets)]
        if kind == "off":
            return [_svc("light.turn_off", targets)]
        if kind == "brightness":
            return [_svc("light.turn_on", targets, {"brightness_pct": action["pct"]})]
        if kind == "step":
            return [_svc("light.turn_on", targets, {"brightness_step_pct": action["pct"]})]
        if kind == "native":
            return [_svc(action["action"], targets, dict(action["data"]))]
        if kind == "none":  # explicitly nothing (NO ACTION), unlike an interaction without an action (UNSET)
            return []
        if kind == "select_mood":
            mood = self.d["mood"]["entity_id"]
            if not mood or mood.split(".", 1)[0] not in ("input_select", "select"):
                self.block("no_mood_selector", f"“{where}”: selecting a mood needs a mood selector (a dropdown helper or "
                           "select) under Colour & Mood.", "switches")
                return []
            self.x.extra_targets.add(mood)
            return [_svc(f"{mood.split('.', 1)[0]}.select_option", [mood], {"option": action["value"]})]
        if kind == "mood":
            mood = self.d["mood"]
            if action["value"] not in mood["values"]:
                self.block("unknown_mood", f"“{where}”: the mood “{action['value']}” is not one of the moods of Colour & Mood.", "switches")
                return []
            groups: dict[str, tuple[dict[str, Any], list[Any]]] = {}
            for light, target in zip(lights, targets):
                res = C.resolve((mood["cells"].get(light) or {}).get(action["value"]), mood["row_defaults"].get(light), mood["default"])
                cell = C.acting(res)
                if cell is None:
                    self.info("mood_nothing", f"“{where}”: {light} has no setting for the mood “{action['value']}”; it is left "
                              "out of this action.", "switches")
                    continue
                data = _mood_data(cell)
                key = repr(sorted(data.items()))
                groups.setdefault(key, (data, []))[1].append(target)
            return [_svc("light.turn_on", ts, data) for data, ts in groups.values()]
        if kind == "resume":
            out = []
            for light, target in zip(lights, targets):
                again = {"type": "event", "event_type": EVENT, "event_data": {"light": plain(light)}}
                if light in self.overridable():
                    h = self.helpers(light)
                    stop = [_svc("input_boolean.turn_off", [h["flag"]])]
                    if self.d["override"]["policy"] == "duration":
                        stop.append(_svc("timer.cancel", [h["timer"]]))
                    out.append(_if([C.c_state(h["flag"], "on")], stop, [again]))
                else:
                    out.append(again)
            return out
        if kind == "advanced":
            rule = next((r for r in self.d["advanced"] if r["id"] == action["rule"]), None)
            if rule is None or rule["kind"] != "when":
                self.block("unknown_rule", f"“{where}”: the Advanced Rule it runs does not exist (or is not a one-shot rule).", "switches")
                return []
            steps = [s for a in rule["actions"] for s in self.actions_for(a, rule["lights"], rule["name"])]
            if steps and rule["conditions"]["items"]:
                return [{"type": "if", "conditions": rule["conditions"], "then": steps, "else": []}]
            return steps
        return []

    def override_start(self, lights: list[str]) -> list[dict[str, Any]]:
        out = []
        for light in lights:
            if light not in self.overridable():
                continue
            h = self.helpers(light)
            out.append(_svc("input_boolean.turn_on", [h["flag"]]))
            if self.d["override"]["policy"] == "duration" and self.d["override"]["minutes"]:
                out.append(_svc("timer.start", [h["timer"]], {"duration": _duration(self.d["override"]["minutes"])}))
        return out

    def switches(self) -> None:
        d = self.d
        for switch in d["switches"]:
            by_id = {i["id"]: i for i in switch["interactions"]}
            for m in switch["mappings"]:
                if not m["enabled"] or m["interaction"] is None:
                    continue
                interaction = by_id[m["interaction"]]
                where = f"{switch['name']}: {interaction['label'] or INTERACTION_NAMES[interaction['kind']]}"
                if interaction["trigger"] is None:
                    continue
                lights = m["lights"]
                kind = m["action"]["kind"]
                if not lights and kind not in ("advanced", "select_mood", "none"):
                    self.block("no_lights", f"“{where}”: choose the lights this action controls. Nothing is assigned for you.", "switches")
                    continue
                if kind == "dim":
                    self.dim_rule(switch, m, interaction, where)
                    continue
                steps = []
                if m["override"] or kind == "override":
                    steps += self.override_start(lights)
                steps += self.actions_for(m["action"], lights, where)
                if not steps:
                    if kind == "none":
                        self.info("no_action", f"“{where}”: NO ACTION - this interaction is explicitly ignored.", "switches")
                    continue
                self.add("switch", lights[0] if lights else None, f"{where} → {ACTION_WORDS[kind]}", [interaction["trigger"]],
                         steps, key=f"{switch['id']}:{m['id']}")

    def dim_rule(self, switch: dict[str, Any], m: dict[str, Any], interaction: dict[str, Any], where: str) -> None:
        a = m["action"]
        release = next((i for i in switch["interactions"] if i["id"] == a["release"]), None)
        targets = [self.x.e(light, "Controlled light") for light in m["lights"]]
        if release is None or release["trigger"] is None:
            self.block("no_release", f"“{where}”: hold-to-dim needs the interaction that ends it (the release).", "switches")
            return
        wait_trigger = dict(release["trigger"])
        if wait_trigger["type"] == "button":  # a wait reacts to any event of an event entity: the next one (the release)
            wait_trigger["event_types"] = []
        step = a["step"] if a["direction"] == "up" else -a["step"]
        steps = []
        if m["override"]:
            steps += self.override_start(m["lights"])
        steps.append({"type": "repeat", "mode": "until", "count": None,
                      "conditions": _group([_template("{{ wait.trigger is not none or repeat.index >= " + str(DIM_STEPS) + " }}")]),
                      "actions": [_svc("light.turn_on", targets, {"brightness_step_pct": step}),
                                  {"type": "wait_trigger", "triggers": [wait_trigger], "timeout_seconds": a["interval"],
                                   "on_timeout": "continue"}]})
        self.add("switch", m["lights"][0], f"{where} → dim {a['direction']} while held", [interaction["trigger"]], steps,
                 key=f"{switch['id']}:{m['id']}")

    def overrides(self) -> None:
        d = self.d
        o = d["override"]
        lights = self.overridable()
        if not lights:
            return
        if o["policy"] is None:
            self.block("no_override_policy", f"“{d['name']}”: some switch actions start a manual override. Choose how long "
                       "an override lasts (Switches & Remotes): until resumed, a duration, until the room is vacant, or "
                       "until an event.", "switches")
            return
        if o["policy"] == "duration" and not o["minutes"]:
            self.block("no_minutes", f"“{d['name']}”: give the duration of a manual override.", "switches")
            return
        if o["policy"] == "until_vacant":
            source = self.sources.get(o["source"]) if o["source"] else None
            if source is None or P.missing(source, "vacant"):
                self.block("missing_source", f"“{d['name']}”: choose the presence source whose vacancy ends a manual override.", "switches")
                return
        if o["policy"] == "until_event" and not o["release"]:
            self.block("no_release", f"“{d['name']}”: choose the event that ends a manual override.", "switches")
            return
        for light in lights:
            h = self.helpers(light)
            self.view["helpers"].append({"entity_id": h["flag"], "domain": "input_boolean", "item_id": h["item_id"],
                                         "name": h["name"], "light": light, "purpose": "manual override (on = active)"})
            off = [_svc("input_boolean.turn_off", [h["flag"]])]
            flag_on = [C.c_state(h["flag"], "on")]
            if o["policy"] == "duration":
                self.view["helpers"].append({"entity_id": h["timer"], "domain": "timer", "item_id": h["item_id"],
                                             "name": h["name"], "light": light, "purpose": "how long the override lasts"})
                self.add("override", light, f"{light} · override ends (time is up)",
                         [{"type": "event", "event_type": "timer.finished", "event_data": {"entity_id": h["timer"]}}], off,
                         conditions=flag_on, group=light, key="timer")
                self.add("override", light, f"{light} · override ended during a restart",
                         [{"type": "homeassistant", "event": "start"}], off,
                         conditions=[*flag_on, C.c_state(h["timer"], "idle")], group=light, key="start")
            elif o["policy"] == "until_vacant":
                self.add("override", light, f"{light} · override ends (vacant)", [P.entered(self.sources[o["source"]], "vacant", self.x)],
                         off, conditions=flag_on, group=light, key="vacant")
            elif o["policy"] == "until_event":
                self.add("override", light, f"{light} · override ends (event)", list(o["release"]), off,
                         conditions=flag_on, group=light, key="event")
            if o["manual"]:
                self.add("override", light, f"{light} · changed by hand → override", [C.t_state(self.x.e(light, "Controlled light"))],
                         self.override_start([light]), conditions=[_template(manual_template(light))], group=light, key="manual")
        self.x.extra_targets.update(h["entity_id"] for h in self.view["helpers"])

    def one_shots(self) -> None:
        for rule in self.d["advanced"]:
            if not rule["enabled"] or rule["kind"] != "when":
                continue
            if not rule["lights"]:
                self.block("no_lights", f"“{rule['name']}”: choose the lights it controls.", "advanced")
                continue
            if not rule["actions"]:
                self.block("no_actions", f"“{rule['name']}”: add what it does.", "advanced")
                continue
            if not rule["triggers"]:
                continue  # only run by a switch mapping ("Execute an Advanced Rule")
            steps = [s for a in rule["actions"] for s in self.actions_for(a, rule["lights"], rule["name"])]
            respect = self.d["priority"].index("override") < self.d["priority"].index("advanced")
            if respect and set(rule["lights"]) & set(self.overridable()):
                # a manual override ranks above the Advanced Rules: a light under one is left out
                steps = []
                for light in rule["lights"]:
                    lone = [s for a in rule["actions"] for s in self.actions_for(a, [light], rule["name"])]
                    if light in self.overridable():
                        steps.append(_if([C.c_state(self.helpers(light)["flag"], "on", negate=True)], lone))
                    else:
                        steps += lone
            if steps:
                self.add("advanced", rule["lights"][0], f"{rule['name']} (one-shot)", list(rule["triggers"]), steps,
                         conditions=list(rule["conditions"]["items"]), key=rule["id"])

    # ------------------------------------------------------------ the whole designer
    def run(self) -> None:
        d = self.d
        if not d["lights"]:
            self.block("no_lights", f"“{d['name']}”: choose the lights it controls (the rows of its matrices).")
            return
        self.x.extra_targets.update(h for light in self.overridable() for h in (self.helpers(light)["flag"], self.helpers(light)["timer"]))
        for light in d["lights"]:
            plan = self.light_plan(light)
            if plan is not None:
                self.plans[light] = plan
        if not self.ok:
            return
        self.overrides()
        if not self.ok:
            return
        for plan in self.plans.values():
            self.evaluator_rule(plan)
        self.switches()
        self.one_shots()
        self.zero_check()

    def zero_check(self) -> None:
        """0 % cells are fine once their meaning is chosen; say what it is."""
        if self.d["brightness"]["zero"]:
            self.info("zero_meaning", f"0 % in a Brightness Matrix means: {ZERO_WORDS[self.d['brightness']['zero']]}.", "brightness")


ACTION_WORDS = {"none": "no action", "select_mood": "select a mood", "toggle": "toggle", "on": "on", "off": "off", "brightness": "set brightness", "step": "change brightness",
                "mood": "apply a mood", "override": "manual override", "resume": "resume automatic control",
                "advanced": "run an Advanced Rule", "dim": "dim while held", "native": "light action"}
ZERO_WORDS = {"off": "switch the light off", "minimum": "the lowest brightness (1 %)", "unchanged": "leave the brightness as it is"}


def _mood_data(cell: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if cell["brightness_pct"] is not None:
        data["brightness_pct"] = plain(cell["brightness_pct"])
    if cell["color_temp_kelvin"] is not None:
        data["color_temp_kelvin"] = plain(cell["color_temp_kelvin"])
    elif cell["color"]:
        data["rgb_color" if "rgb" in cell["color"] else "hs_color"] = list(cell["color"].get("rgb") or cell["color"]["hs"])
    if cell["effect"]:
        data["effect"] = cell["effect"]
    if cell["transition"] is not None:
        data["transition"] = plain(cell["transition"])
    return data


def _rule_value(s: dict[str, Any], prop: str) -> Any:
    if prop == "on":
        return s["truth"]
    if prop == "brightness":
        return plain(s["brightness_pct"])
    if prop == "color":
        return color_value(s)
    if prop == "effect":
        return s["effect"]
    return plain(s["transition"])


def _jinja(value: Any) -> str:
    """A value written as a template literal."""
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return quote(_clean(value))
    if isinstance(value, list):
        return "[" + ", ".join(_jinja(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{quote(k)}: {_jinja(v)}" for k, v in value.items()) + "}"
    raise TypeError(value)


def _minutes(after: str | None, before: str | None) -> set[int]:
    def at(t: str | None, default: int) -> int:
        if not t:
            return default
        h, m, *_s = (int(v) for v in t.split(":"))
        return h * 60 + m
    a, b = at(after, 0), at(before, 1440)
    return set(range(a, b)) if a <= b else set(range(a, 1440)) | set(range(0, b))


# ================================================================== the Python twin: evaluation and simulation
def evaluate_plan(plan: dict[str, Any], ev: Any, light_state: dict[str, Any] | None, *,
                  override: bool | None = None) -> dict[str, Any]:
    """What the light's automation computes right now (the same branches, conditions and resolution)."""
    v: dict[str, Any] = {}
    unknown: list[str] = []
    matrices = []
    for layer in ("truth", "brightness"):
        for m in plan[layer]:
            value, matched = None, None
            for b in m["branches"]:
                found = ev.condition(b["conditions"], set())
                if found is None:
                    unknown.append(f"{m['name']}: a condition cannot be evaluated offline")
                    break
                if found:
                    value, matched = b["value"], b
                    break
            v[m["var"]] = value
            matrices.append({"layer": layer, "id": m["id"], "name": m["name"], "value": value,
                             "columns": matched["columns"] if matched else []})
    mood: dict[str, Any] = {}
    mood_value = None
    if plan["mood"]:
        for b in plan["mood"]["branches"]:
            found = ev.condition(b["conditions"], set())
            if found is None:
                unknown.append("the mood cannot be evaluated offline")
                break
            if found:
                mood, mood_value = b["value"], b["mood"]
                break
    advanced = []
    for rule in plan["advanced"]:
        found = ev.condition(rule["conditions"], set())
        if found is None:
            unknown.append(f"{rule['name']}: a condition cannot be evaluated offline")
        v[rule["var"]] = bool(found)
        advanced.append({"id": rule["id"], "name": rule["name"], "active": found})
    ovr = override
    if ovr is None and plan["override"]:
        ovr = ((ev.states.get(plan["override"]) or {}).get("state") == "on")
    truth = pick([v[m["var"]] for m in plan["truth"]], plan["truth_combine"]) if plan["truth"] else None
    bright = pick([v[m["var"]] for m in plan["brightness"]], plan["bright_combine"]) if plan["brightness"] else None
    v["ab_truth"], v["ab_bright"] = truth, bright
    resolved: dict[str, Any] = {}
    for prop, entries in plan["resolve"].items():
        found = None
        for e in entries:
            kind = e["twin"][0]
            if kind == "rule" and v.get(e["twin"][1]):
                found = e["twin"][2]
            elif kind == "override" and ovr:
                found = e["twin"][1]
            elif kind == "var" and v.get(e["twin"][1]) is not None:
                found = v[e["twin"][1]]
            elif kind == "mood" and mood.get(e["twin"][1]) is not None:
                found = [mood[e["twin"][1]], mood["by"]]
            if found is not None:
                found = [*found, e["layer"]]
                break
        resolved[prop] = found
    return {"matrices": matrices, "truth": truth, "brightness": bright, "mood": mood_value, "mood_settings": mood,
            "advanced": advanced, "override": bool(ovr) if plan["override"] else None, "resolved": resolved,
            "command": command(light_state, resolved), "unknown": unknown}


def live_states(index: Any, now: Any) -> dict[str, Any]:
    """Home Assistant's states as the evaluator reads them (with how long each has held)."""
    from datetime import datetime
    out = {}
    for entity_id, state in index.states.items():
        held = age = None
        for key, target in (("last_changed", "held"), ("last_updated", "age")):
            try:
                seconds = (now - datetime.fromisoformat(str(state.get(key)))).total_seconds()
            except (TypeError, ValueError):
                seconds = None
            if target == "held":
                held = seconds
            else:
                age = seconds
        out[entity_id] = {**state, "held": held, "age": age}
    return out


def override_text(d: dict[str, Any], names: Any) -> str:
    o = d["override"]
    if o["policy"] == "duration":
        text = f"ends after {plain(o['minutes'] or 0)} min (a timer helper in Home Assistant)"
    elif o["policy"] == "until_vacant":
        text = "ends when the chosen presence source becomes vacant"
    elif o["policy"] == "until_event":
        text = "ends at the chosen event"
    elif o["policy"] == "until_resumed":
        text = "lasts until a “resume” action (or until the helper is turned off in Home Assistant)"
    else:
        text = "how long it lasts is not chosen yet"
    return text + "; a “resume” action always ends it, and the light then returns to the calculated baseline."


def interaction_view(d: dict[str, Any], interaction_id: str | None) -> dict[str, Any] | None:
    for switch in d["switches"]:
        for i in switch["interactions"]:
            if i["id"] == interaction_id:
                return {"switch": switch, "interaction": i,
                        "mappings": [m for m in switch["mappings"] if m["enabled"] and m["interaction"] == i["id"]]}
    return None


def simulate(gen: "_Gen", ev: Any, scenario: dict[str, Any], names: Any) -> dict[str, Any]:
    """The designer's part of the simulation: per light, every layer and the resolution. Nothing is sent."""
    d = gen.d
    states = ev.states
    event = interaction_view(d, scenario.get("event"))
    forced: dict[str, bool] = {}
    happened = []
    if event:
        for m in event["mappings"]:
            kind = m["action"]["kind"]
            text = f"{ACTION_WORDS[kind]} for {', '.join(names(light) for light in m['lights']) or '…'}"
            if m["override"] or kind == "override":
                for light in m["lights"]:
                    if light in gen.overridable():
                        forced[light] = True
                text += ", and a manual override starts (" + override_text(d, names) + ")"
            elif kind == "resume":
                for light in m["lights"]:
                    forced[light] = False
                text += ": the override ends and the baseline is applied"
            elif kind == "select_mood":
                text = f"select the mood “{m['action']['value']}” (every light that uses it is evaluated again)"
            elif kind == "none":
                text = "no action (explicitly ignored)"
            elif kind in ONE_SHOT_ACTIONS:
                text += " (one-shot: the next evaluation applies the baseline again)"
            happened.append({"mapping": m["id"], "text": text, "lights": m["lights"]})
    lights = []
    for light in d["lights"]:
        plan = gen.plans.get(light)
        state = states.get(light)
        entry: dict[str, Any] = {"entity_id": light, "name": names(light), "state": (state or {}).get("state")}
        if plan is None:
            lights.append({**entry, "controlled": False})
            continue
        found = evaluate_plan(plan, ev, state, override=forced.get(light))
        holding = {}
        for layer in ("truth", "brightness"):
            for m in d[layer]["matrices"]:
                holding[m["id"]] = [{"id": c["id"], "label": gen.col_label(c),
                                     "holds": ev.condition(_group(gen.col_items(c)), set()) if not gen.col_missing(c) else None}
                                    for c in m["columns"]]
        lights.append({**entry, "controlled": True, **found, "columns": holding,
                       "helper": plan["override"], "override_ends": override_text(d, names) if plan["override"] else None})
    return {"event": ({"switch": event["switch"]["name"], "interaction": event["interaction"]["label"]
                       or INTERACTION_NAMES[event["interaction"]["kind"]], "happened": happened} if event else None),
            "lights": lights, "priority": [{"layer": layer, "name": LAYER_NAMES[layer]} for layer in d["priority"]]}


# ================================================================== views, capabilities
def resolved_views(d: dict[str, Any], x: Any) -> dict[str, Any]:
    if d.get("version") == 1:
        from . import lighting
        return lighting.resolved_views(d, x)
    out: dict[str, Any] = {}
    for layer, label_fn in (("truth", truth_label), ("brightness", bright_label)):
        for m in d[layer]["matrices"]:
            out[m["id"]] = {light: {c["id"]: _cell_view(C.resolve((m["cells"].get(light) or {}).get(c["id"]),
                                                                  m["row_defaults"].get(light), m["default"]),
                                                        (m["cells"].get(light) or {}).get(c["id"]), label_fn)
                                    for c in m["columns"]} for light in d["lights"]}
    mood = d["mood"]
    out["mood"] = {light: {v: _cell_view(C.resolve((mood["cells"].get(light) or {}).get(v), mood["row_defaults"].get(light),
                                                   mood["default"]), (mood["cells"].get(light) or {}).get(v), mood_label)
                           for v in mood["values"]} for light in d["lights"]}
    return out


def _cell_view(res: dict[str, Any], own: dict[str, Any] | None, label_fn: Any) -> dict[str, Any]:
    return {"label": resolved_label(res, label_fn), "state": res["state"], "source": res["source"], "own": own is not None,
            "explicit_default": bool(own and own["kind"] == "default"),
            "zero": bool(res.get("cell") and res["cell"].get("kind") == "set" and res["cell"].get("pct") == 0)}


def _needs(a: dict[str, Any]) -> set[str]:
    out = set()
    if isinstance(a.get("brightness_pct"), (int, float)):
        out.add("brightness")
    if a.get("color_temp_kelvin") is not None:
        out.add("color_temp")
    if isinstance(a.get("color"), dict):
        out.add("color")
    if a.get("effect") not in (None, "keep"):
        out.add("effect")
    return out


def capability_problems(d: dict[str, Any], caps: dict[str, dict[str, Any]], x: Any) -> None:
    if d.get("version") == 1:
        from . import lighting
        lighting.capability_problems(d, caps, x)
        return
    for light in d["lights"]:
        cap = caps.get(light)
        if not cap or cap.get("unknown"):
            continue
        wanted: set[str] = set()
        effects: set[str] = set()
        for m in d["brightness"]["matrices"]:
            cells = [*((m["cells"].get(light) or {}).values()), m["row_defaults"].get(light), m["default"]]
            if any(c and c["kind"] == "set" and c["pct"] not in (None, 0) for c in cells):
                wanted.add("brightness")
        mood = d["mood"]
        if mood["enabled"]:
            for c in [*((mood["cells"].get(light) or {}).values()), mood["row_defaults"].get(light), mood["default"]]:
                if c and c["kind"] == "set":
                    wanted |= _needs(c)
                    if c["effect"]:
                        effects.add(c["effect"])
        for rule in d["advanced"]:
            if rule["enabled"] and light in rule["lights"]:
                if rule["set"]:
                    wanted |= _needs(rule["set"])
                for a in rule["actions"]:
                    if a["kind"] in ("brightness", "step"):
                        wanted.add("brightness")
        for switch in d["switches"]:
            for m in switch["mappings"]:
                if m["enabled"] and light in m["lights"] and m["action"]["kind"] in ("brightness", "step", "dim"):
                    wanted.add("brightness")
        missing = sorted(w for w in wanted if not cap.get(w))
        if missing:
            x.problem("blocking", "capability", f"“{d['name']}”: {cap.get('name') or light} cannot "
                      + ", ".join({"brightness": "dim", "color_temp": "change its colour temperature", "color": "show colours",
                                   "effect": "run effects"}[w] for w in missing)
                      + " (Home Assistant reports what it supports). Change what asks for it.", d["id"], None)
        unknown = sorted(e for e in effects if cap.get("effects") is not None and e not in cap["effects"])
        if unknown:
            x.problem("warning", "capability", f"“{d['name']}”: {cap.get('name') or light} does not list the effect "
                      f"{', '.join(unknown)}.", d["id"], None)


def index_problems(d: dict[str, Any], index: Any, x: Any) -> None:
    """What Home Assistant reports now about the switches and lights (never blocking: states change)."""
    if d.get("version") == 1:
        return
    for light in d["lights"]:
        if (index.states.get(light) or {}).get("state") == "unavailable":
            x.problem("info", "light_unavailable", f"{index.name(light)} is unavailable now: it gets no commands until it "
                      "is back (when a wall switch cuts its power, software cannot switch it on).", d["id"], "switches")
    for switch in d["switches"]:
        entity = switch["entity_id"]
        if entity and (index.states.get(entity) or {}).get("state") == "unavailable":
            x.problem("warning", "switch_unavailable", f"“{switch['name']}” ({entity}) is unavailable now: its presses or "
                      "states cannot reach Home Assistant.", d["id"], "switches")


def expand(designer: dict[str, Any], x: Any) -> dict[str, Any]:
    if designer.get("version") == 1:
        from . import lighting
        return lighting.expand(designer, x)
    gen = _Gen(designer, x)
    if designer["enabled"]:
        gen.run()
    x.generators[designer["id"]] = gen
    lights = {}
    for light, plan in gen.plans.items():
        lights[light] = {"layers": [layer for layer in designer["priority"] if any(
            e["layer"] == layer for entries in plan["resolve"].values() for e in entries)],
                         "override": plan["override"], "matrices": [m["id"] for m in plan["truth"] + plan["brightness"]]}
    return {**gen.view, "lights": lights, "resolved": resolved_views(designer, x),
            "clashes": clashes(gen)}


def clashes(gen: "_Gen") -> dict[str, list[dict[str, Any]]]:
    """For the interface: per matrix, the column pairs that can hold together (whether they disagree or not)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for layer in ("truth", "brightness"):
        for m in gen.d[layer]["matrices"]:
            pairs = []
            for i, a in enumerate(m["columns"]):
                for b in m["columns"][i + 1:]:
                    if gen.overlaps(a, b):
                        pairs.append({"a": a["id"], "b": b["id"]})
            out[m["id"]] = pairs
    return out


# ================================================================== new designers, templates, conversion
def new_matrix(layer: str, name: str, ident: str) -> dict[str, Any]:
    return {"id": ident, "name": name, "enabled": True, "columns": [], "cells": {}, "row_defaults": {}, "default": None,
            "policy": None, "ranks": []}


def _short(prefix: str) -> str:
    import secrets
    return f"{prefix}_{secrets.token_hex(2)}"


TEMPLATES: list[dict[str, Any]] = [
    {"id": "l2_presence", "name": "Presence lighting", "tab": "truth",
     "description": "A Truth Matrix: which lights are ON or OFF while rooms or zones are occupied or vacant.",
     "required": ["Presence sources (rooms, zones, position zones)"], "optional": ["A Brightness Matrix", "Colour & Mood"],
     "add": {"truth": "Presence"}},
    {"id": "l2_lux", "name": "Light-level lighting", "tab": "truth",
     "description": "A Truth Matrix with light-level columns (a lux sensor and the ranges you name).",
     "required": ["A light-level sensor", "Ranges"], "optional": ["A delay against flicker"], "add": {"truth": "Light level"}},
    {"id": "l2_brightness", "name": "Brightness by situation", "tab": "brightness",
     "description": "A Brightness Matrix: the brightness per situation, while the Truth Matrix allows the light to be on.",
     "required": ["Columns (presence, light level, a switch state, a time …)"], "optional": ["Defaults"],
     "add": {"brightness": "Brightness"}},
    {"id": "l2_mood", "name": "Colour & mood", "tab": "mood",
     "description": "Colour temperature, colour, brightness or an effect per mood value of a selector.",
     "required": ["A mood selector"], "optional": ["Transitions", "Defaults"], "add": {"mood": True}},
    {"id": "l2_switch", "name": "Wall switch or remote", "tab": "switches",
     "description": "Map the presses or states of a switch, button or remote to actions, with a manual override that "
                    "ends when you choose.", "required": ["The switch or remote", "What each interaction does"],
     "optional": ["The override policy"], "add": {}},
]


def apply_template(template_id: str, designer: dict[str, Any]) -> dict[str, Any]:
    import copy
    template = next((t for t in TEMPLATES if t["id"] == template_id), None)
    if template is None:
        raise KeyError(template_id)
    out = copy.deepcopy(designer)
    for layer in ("truth", "brightness"):
        if layer in template["add"] and len(out[layer]["matrices"]) < MAX_MATRICES:
            out[layer]["matrices"].append(new_matrix(layer, template["add"][layer], _short("m")))
    if template["add"].get("mood"):
        out["mood"]["enabled"] = True
    return out


def convert(v1: dict[str, Any], sources: dict[str, Any]) -> dict[str, Any]:
    """A version 1 Lighting Designer as version 2, when that can be done without guessing.

    ``{"designer": doc | None, "changes": [...], "refused": [...]}``. Refused parts are named; nothing is converted
    then (the version 1 designer keeps working exactly as before). The deployed automation stays until the device is
    deployed again.
    """
    changes = ["The lights now follow the matrices continuously: whenever an input changes, the current situation "
               "is evaluated again (version 1 acted only at the moment a situation started)."]
    refused: list[str] = []
    out: dict[str, Any] = {"id": v1["id"], "kind": KIND, "version": VERSION, "name": v1["name"], "enabled": v1["enabled"],
                           "lights": list(v1["lights"]), "truth": {"matrices": [], "combine": None},
                           "brightness": {"matrices": [], "combine": None, "zero": None},
                           "mood": {"enabled": False, "entity_id": None, "values": [], "auto": None, "cells": {},
                                    "row_defaults": {}, "default": None},
                           "switches": [], "advanced": [], "override": {"policy": None, "minutes": None, "source": None,
                                                                         "release": [], "manual": False},
                           "priority": list(LAYERS), "reconcile_minutes": None, "on_available": False}
    primary = v1["primary"] or next((layer for layer in ("mood", "presence", "lux") if v1[layer]["enabled"]), None)
    enabled = [layer for layer in ("mood", "presence", "lux") if v1[layer]["enabled"]]
    for layer in enabled:
        if layer != primary and layer != "mood":
            refused.append(f"The {layer} matrix is a permission (gate) in version 1. A permission that blocks a command "
                           "without sending one has no exact equivalent; rebuild it as a Truth Matrix by hand.")
    if v1["presence"]["policy"] == "compatible" and primary == "presence":
        refused.append("“Both, when compatible” lets the situation that started last decide; version 2 never decides by "
                       "timing. Choose a ranking or “nothing while ambiguous” in version 1 first, or rebuild by hand.")
    if v1["schedule"]["enabled"] and v1["schedule"]["windows"]:
        refused.append("Time windows that limit when the deciding matrix acts have no exact equivalent (add a time "
                       "column to a Truth Matrix by hand).")
    if v1["rechecks"]:
        changes.append("The occupancy rechecks are no longer needed: every change of an input evaluates the light again.")
    uses_mood = False

    def cells_of(m: dict[str, Any], columns: list[tuple[str, str]], where: str) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal uses_mood
        truth: dict[str, Any] = {}
        bright: dict[str, Any] = {}
        for light in v1["lights"]:
            for old, new in columns:
                cell = (m["cells"].get(light) or {}).get(old)
                if cell is None:
                    continue
                kind = cell["kind"]
                if kind in ("none", "default", "off"):
                    truth.setdefault(light, {})[new] = {"kind": kind}
                elif kind == "mood":
                    uses_mood = True
                    truth.setdefault(light, {})[new] = {"kind": "on"}
                else:
                    truth.setdefault(light, {})[new] = {"kind": "on"}
                    if cell["brightness_pct"] is not None:
                        bright.setdefault(light, {})[new] = {"kind": "set", "pct": cell["brightness_pct"]}
                    if any(cell.get(k) for k in ("color_temp_kelvin", "color", "effect")) or cell.get("transition") not in (None, 0):
                        refused.append(f"{where}: {light} has a colour, effect or transition in a situation cell; in version 2 "
                                       "colours come from Colour & Mood. Move it there by hand.")
        return truth, bright

    def defaults_of(m: dict[str, Any], where: str) -> tuple[dict[str, Any], Any]:
        rows = {}
        for light, cell in m["row_defaults"].items():
            if cell["kind"] in ("on", "off", "none") and not (cell["kind"] == "on" and any(
                    cell.get(k) is not None for k in ("brightness_pct", "color_temp_kelvin", "color", "effect", "transition"))):
                rows[light] = {"kind": cell["kind"]}
            else:
                refused.append(f"{where}: the row default of {light} carries settings; set it again in version 2.")
        default = m["default"]
        if default is not None and not (default["kind"] in ("on", "off", "none") and not any(
                default.get(k) is not None for k in ("brightness_pct", "color_temp_kelvin", "color", "effect", "transition"))):
            refused.append(f"{where}: the matrix default carries settings; set it again in version 2.")
            default = None
        return rows, ({"kind": default["kind"]} if default else None)

    if primary == "presence":
        p = v1["presence"]
        columns = [(f"{c['source']}:{c['state']}", _short("c")) for c in p["columns"]]
        truth, bright = cells_of(p, columns, "Presence")
        rows, default = defaults_of(p, "Presence")
        cols = [{"id": new, "kind": "presence", "label": "", "source": c["source"], "state": c["state"]}
                for (old, new), c in zip(columns, p["columns"])]
        matrix = {**new_matrix("truth", "Presence", _short("m")), "columns": cols, "cells": truth, "row_defaults": rows,
                  "default": default, "policy": {"priority": "priority", "exclusive": "exclusive"}.get(p["policy"]),
                  "ranks": [new for _old, new in columns] if p["policy"] == "priority" else []}
        out["truth"]["matrices"].append(matrix)
        if p["policy"] == "priority":
            changes.append("“Priority: the left column wins” becomes an explicit ranking in the same order.")
        if bright:
            out["brightness"]["matrices"].append({**new_matrix("brightness", "Presence brightness", _short("m")),
                                                  "columns": [dict(c, id=_short("c")) for c in cols], "cells": {},
                                                  "policy": matrix["policy"], "ranks": []})
            bm = out["brightness"]["matrices"][-1]
            ids = {c["id"]: b["id"] for c, b in zip(cols, bm["columns"])}
            bm["cells"] = {light: {ids[k]: v for k, v in row.items()} for light, row in bright.items()}
            bm["ranks"] = [ids[r] for r in matrix["ranks"]]
    elif primary == "lux":
        lux = v1["lux"]
        if plain(lux["hysteresis"]):
            refused.append("Version 2 light-level columns have a delay but no hysteresis; set the hysteresis to 0 in "
                           "version 1 first (or rebuild by hand).")
        columns, cols = [], []
        for i, band in enumerate(lux["bands"]):
            new = _short("c")
            columns.append((band["id"], new))
            cols.append({"id": new, "kind": "numeric", "label": band["name"], "entity_id": lux["entity_id"], "attribute": None,
                         "from": lux["bands"][i - 1]["below"] if i > 0 else None, "below": band["below"],
                         "for_seconds": lux["delay_seconds"]})
        truth, bright = cells_of(lux, columns, "Light level")
        rows, default = defaults_of(lux, "Light level")
        out["truth"]["matrices"].append({**new_matrix("truth", "Light level", _short("m")), "columns": cols, "cells": truth,
                                         "row_defaults": rows, "default": default})
        if bright:
            bcols = [dict(c, id=_short("c")) for c in cols]
            ids = {c["id"]: b["id"] for c, b in zip(cols, bcols)}
            out["brightness"]["matrices"].append({**new_matrix("brightness", "Light-level brightness", _short("m")),
                                                  "columns": bcols, "cells": {light: {ids[k]: v for k, v in row.items()}
                                                                              for light, row in bright.items()}})
    elif primary == "mood":
        refused.append("A mood matrix that decides ON and OFF becomes a Truth Matrix over the mood selector plus Colour & "
                       "Mood; build it in version 2 (the colours can be copied cell by cell).")
    mood = v1["mood"]
    if (uses_mood or primary == "mood") and mood["enabled"]:
        if mood["auto"]:
            out["mood"]["auto"] = dict(mood["auto"])
        out["mood"].update(enabled=True, entity_id=mood["entity_id"], values=list(mood["values"]))
        for light, row in mood["cells"].items():
            for value, cell in row.items():
                if cell["kind"] == "on":
                    out["mood"]["cells"].setdefault(light, {})[value] = {"kind": "set", **{k: cell.get(k) for k in (
                        "brightness_pct", "color_temp_kelvin", "color", "effect", "transition")}}
                elif cell["kind"] in ("none", "default"):
                    out["mood"]["cells"].setdefault(light, {})[value] = {"kind": cell["kind"]}
                else:
                    refused.append(f"Mood {value}: {light} is OFF in that mood; in version 2 that is a Truth Matrix over "
                                   "the mood selector. Build it by hand.")
        changes.append("Cells that said “Use mood” switch the light ON; its settings come from Colour & Mood.")
    entries = v1["schedule"]["entries"] if v1["schedule"]["enabled"] else []
    for entry in entries:
        for light, cell in entry["cells"].items():
            if cell["kind"] in ("mood",) or (cell["kind"] == "on" and any(
                    cell.get(k) is not None for k in ("color_temp_kelvin", "color", "effect", "transition"))):
                refused.append(f"The timed entry {entry['name'] or entry['at']} has settings other than on/off/brightness "
                               f"for {light}; rebuild it as a one-shot Advanced Rule.")
        acts = []
        for light, cell in entry["cells"].items():
            if cell["kind"] == "off":
                acts.append(({"kind": "off"}, light))
            elif cell["kind"] == "on":
                acts.append(({"kind": "brightness", "pct": cell["brightness_pct"]} if cell["brightness_pct"] else {"kind": "on"}, light))
        for action, light in acts:
            out["advanced"].append({"id": _short("a"), "name": f"{entry['name'] or 'At ' + entry['at'][:5]} · {light}"[:60],
                                    "enabled": True, "kind": "when", "lights": [light],
                                    "conditions": {"type": "and", "items": []}, "set": None,
                                    "triggers": [{"type": "time", "at": entry["at"], "weekdays": list(entry["weekdays"])}],
                                    "actions": [action]})
        if acts:
            changes.append(f"The timed entry {entry['name'] or entry['at'][:5]} becomes a one-shot Advanced Rule.")
    override = v1["override"]
    if override["entity_id"]:
        out["advanced"].insert(0, {"id": _short("a"), "name": "Paused while the override switch is on", "enabled": True,
                                   "kind": "while", "lights": list(v1["lights"]),
                                   "conditions": {"type": "and", "items": [{"type": "state", "entity_id": override["entity_id"],
                                                                            "state": "on", "negate": False, "for_seconds": 0,
                                                                            "attribute": None}]},
                                   "set": {"truth": "keep", "brightness_pct": "keep", "color_temp_kelvin": None,
                                           "color": "keep", "effect": "keep", "transition": None},
                                   "triggers": [], "actions": []})
        changes.append("The override switch becomes an Advanced Rule at the top: while it is on, nothing changes.")
    if override["manual_minutes"]:
        out["override"].update(policy="duration", minutes=override["manual_minutes"], manual=True)
        changes.append(f"“Pause after a change by hand” becomes a manual override of {override['manual_minutes']} min. A "
                       "change counts as manual only when the light is switched on or off by something that is not an "
                       "automation; when the time is up the light returns to the calculated baseline (version 1 left it).")
    if refused:
        return {"designer": None, "changes": changes, "refused": list(dict.fromkeys(refused))}
    from datetime import UTC, datetime
    out["converted_from"] = {"version": 1, "at": datetime.now(UTC).isoformat(timespec="seconds"), "notes": changes[:8]}
    return {"designer": out, "changes": changes, "refused": []}


# ================================================================== ordinary button rules -> Switches & Remotes
SIMPLE_LIGHT_ACTIONS = {"light.toggle": "toggle", "light.turn_off": "off", "light.turn_on": "on"}


def adoptable(device: dict[str, Any], d: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordinary rules of the device that a switch mapping can express exactly: one button-like trigger, no conditions,
    one light action on the designer's lights (toggle, off, on, on with only a brightness). Moving one replaces the
    rule; nothing is duplicated."""
    out = []
    for rule in device["rules"]:
        if not rule["enabled"] or len(rule["triggers"]) != 1 or rule["conditions"]["items"] or len(rule["actions"]) != 1:
            continue
        trigger, action = rule["triggers"][0], rule["actions"][0]
        if trigger["type"] not in MOMENTARY_TRIGGERS + LATCHED_TRIGGERS or action["type"] != "service":
            continue
        if trigger["type"] == "state" and (trigger["for_seconds"] or trigger["attribute"] or trigger["to"] not in ("on", "off")):
            continue
        kind = SIMPLE_LIGHT_ACTIONS.get(action["action"])
        data = action["data"] if isinstance(action["data"], dict) else None
        lights = list(device["targets"]) if action["target"] == "all" else list(action["target"]) if isinstance(action["target"], list) else []
        if kind is None or data is None or not lights or any(light not in d["lights"] for light in lights):
            continue
        if data and not (kind == "on" and set(data) == {"brightness_pct"} and isinstance(data["brightness_pct"], int)
                         and 1 <= data["brightness_pct"] <= 100):
            continue
        mapped = {"kind": "brightness", "pct": data["brightness_pct"]} if data else {"kind": kind}
        out.append({"rule_id": rule["id"], "name": rule["name"], "trigger": trigger, "lights": lights, "action": mapped,
                    "latched": trigger["type"] == "state"})
    return out


def adopt(d: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """The designer with the rule as an interaction and its mapping (on a control of its own kind)."""
    import copy
    import secrets
    out = copy.deepcopy(d)
    kind = "latched" if found["latched"] else "momentary"
    entity = found["trigger"].get("entity_id")
    switch = next((w for w in out["switches"] if w["kind"] == kind and w["entity_id"] == entity and w.get("adopted")), None)
    if switch is None:
        switch = {"id": f"w_{secrets.token_hex(2)}", "name": f"From the rule “{found['name']}”"[:60], "kind": kind,
                  "entity_id": entity, "ha_device_id": (found["trigger"].get("config") or {}).get("device_id"),
                  "interactions": [], "mappings": [], "adopted": True}
        out["switches"].append(switch)
    interaction = {"id": f"i_{secrets.token_hex(2)}", "kind": "on" if found["trigger"].get("to") == "on" else "off"
                   if found["trigger"].get("to") == "off" else "other", "label": found["name"][:60], "trigger": found["trigger"]}
    switch["interactions"].append(interaction)
    switch["mappings"].append({"id": f"p_{secrets.token_hex(2)}", "interaction": interaction["id"], "enabled": True,
                               "lights": found["lights"], "action": found["action"], "override": False})
    return out
