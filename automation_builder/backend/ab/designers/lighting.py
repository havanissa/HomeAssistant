"""The Lighting Designer: mood, presence and light-level matrices for the lights of one managed device.

Rows are the selected lights; each matrix has its own columns (the values of a mood selector, presence situations,
light-level bands). Cells follow the unset/default rules of ``common``.

How the matrices work together is the user's explicit choice, shown in the interface as the *combination*:

* exactly one matrix **decides** (``primary``): its source changing makes the lights act, according to its cells;
* the mood matrix, when it does not decide, is the **palette** for cells that say "Use mood" (and, when the user
  ticks it, re-applies when the mood changes);
* the light level and presence matrices, when they do not decide, are **permissions** (gates): "allow ON only
  in these bands", "allow OFF only in these bands", "allow ON only while one of these situations holds". A
  permission never sends a command by itself, unless the user ticks "also act when it changes".

When two presence situations can hold at the same time (two zones, a zone and the room) and both have a cell for
a light, the user chooses a policy: priority (the column order), exclusive (nothing while it is ambiguous) or
compatible (both apply; different commands are a conflict that blocks deployment until resolved). Nothing is
prioritised silently.

Everything becomes ordinary rules of the device, compiled into its one shared native automation.
"""

from __future__ import annotations

from typing import Any

from ..compiler import plain
from ..model import Problems
from . import common as C, presence as P

KIND = "lighting"
VERSION = 1
ROWS = "lights"
DEFAULT_NAME = "Lighting"
LAYERS = ("mood", "presence", "lux")
CELL_KINDS = ("on", "off", "none", "default", "mood")
MOOD_KINDS = ("on", "off", "none", "default")
ENTRY_KINDS = ("on", "off", "none", "mood")
POLICIES = ("priority", "exclusive", "compatible")
MOOD_DOMAINS = ("input_select", "select", "sensor", "input_text")
LUX_DOMAINS = ("sensor", "input_number", "number")
OVERRIDE_DOMAINS = ("input_boolean", "switch", "binary_sensor")
MAX_VALUES = 30
MAX_COLUMNS = 12
MAX_BANDS = 6
MAX_ENTRIES = 12
MAX_WINDOWS = 6
LAYER_NAMES = {"mood": "Mood", "presence": "Presence", "lux": "Light level", "schedule": "Schedule"}


# ------------------------------------------------------------------ cells
def validate_cell(raw: Any, path: str, p: Problems, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "Unknown cell value")
        return {"kind": "none"}
    kind = raw["kind"]
    if kind in ("none", "default", "mood"):
        return {"kind": kind}
    transition = C.number(raw.get("transition"), f"{path}.transition", p, 0, 300)
    if kind == "off":
        return {"kind": "off", "transition": transition}
    color = None
    raw_color = raw.get("color")
    if isinstance(raw_color, dict) and isinstance(raw_color.get("rgb"), list) and len(raw_color["rgb"]) == 3:
        rgb = [C.number(v, f"{path}.color.rgb", p, 0, 255, integer=True) for v in raw_color["rgb"]]
        color = {"rgb": rgb} if None not in rgb else None
    elif isinstance(raw_color, dict) and isinstance(raw_color.get("hs"), list) and len(raw_color["hs"]) == 2:
        hs = [C.number(raw_color["hs"][0], f"{path}.color.hs", p, 0, 360), C.number(raw_color["hs"][1], f"{path}.color.hs", p, 0, 100)]
        color = {"hs": hs} if None not in hs else None
    elif raw_color not in (None, {}):
        p.add(f"{path}.color", "A colour is RGB (three numbers) or hue/saturation (two)")
    kelvin = C.number(raw.get("color_temp_kelvin"), f"{path}.color_temp_kelvin", p, 1000, 12000, integer=True)
    if kelvin is not None and color is not None:
        p.add(path, "Choose a colour temperature or a colour, not both: Home Assistant refuses the combination")
    effect = raw.get("effect")
    effect = effect.strip()[:60] if isinstance(effect, str) and effect.strip() else None
    return {"kind": "on", "brightness_pct": C.number(raw.get("brightness_pct"), f"{path}.brightness_pct", p, 1, 100, integer=True),
            "color_temp_kelvin": kelvin, "color": color, "effect": effect, "transition": transition}


def label(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "UNSET"
    kind = cell["kind"]
    if kind == "none":
        return "NO ACTION"
    if kind == "mood":
        return "USE MOOD"
    if kind == "default":
        return "USE DEFAULT"
    extra = [f"{plain(cell['transition']):g} s fade"] if cell.get("transition") not in (None, 0) else []
    if kind == "off":
        return " · ".join(["OFF", *extra])
    parts = []
    if cell["brightness_pct"] is not None:
        parts.append(f"{plain(cell['brightness_pct'])}%")
    if cell["color_temp_kelvin"] is not None:
        parts.append(f"{plain(cell['color_temp_kelvin'])}K")
    if cell["color"]:
        parts.append("RGB " + ",".join(str(plain(v)) for v in cell["color"]["rgb"]) if "rgb" in cell["color"]
                     else f"HS {plain(cell['color']['hs'][0]):g}/{plain(cell['color']['hs'][1]):g}")
    if cell["effect"]:
        parts.append(f"effect {cell['effect']}")
    return " · ".join(parts + extra) if parts else " · ".join(["ON", *extra])


def resolved_label(res: dict[str, Any]) -> str:
    if res["state"] == "unset":
        return "UNSET"
    if res["state"] == "missing_default":
        return "DEFAULT: none configured"
    if res["state"] in ("default",):
        return f"DEFAULT: {label(res['cell'])}"
    if res["state"] == "none" and res.get("inherited"):
        return "DEFAULT: NO ACTION"
    return label(res["cell"])


FIELD_INPUT = {
    "brightness_pct": ("Brightness", "brightness", {"number": {"min": 1, "max": 100, "unit_of_measurement": "%", "mode": "slider"}}),
    "color_temp_kelvin": ("Colour temperature", "colour temperature", {"color_temp": {"unit": "kelvin", "min": 1000, "max": 12000}}),
    "transition": ("Action parameters", "transition time", {"number": {"min": 0, "max": 300, "unit_of_measurement": "s", "mode": "box"}}),
    "effect": ("Action parameters", "effect", {"text": {}}),
}


def command(cell: dict[str, Any], light: Any, lights: list[Any], x: Any = None, key: str = "", name: str = "") -> dict[str, Any]:
    """The light action of a cell. With an expansion ``x``, every value can become a blueprint input."""
    def value(field: str, raw: Any) -> Any:
        if x is None or raw is None:
            return raw
        group, words, selector = FIELD_INPUT[field]
        from . import input_key
        return x.v(input_key(key, field), raw, kind="data", group=group, name=f"{name}: {words}",
                   description=f"The {words} of this cell.", selector=selector)
    if cell["kind"] == "off":
        data = {"transition": value("transition", cell["transition"])} if cell["transition"] is not None else {}
        return C.call("light.turn_off", [light], lights, data)
    data: dict[str, Any] = {}
    for field in ("brightness_pct", "color_temp_kelvin", "effect", "transition"):
        if cell.get(field) is not None:
            data[field] = value(field, cell[field])
    if cell.get("color"):
        if "rgb" in cell["color"]:
            data["rgb_color"] = list(cell["color"]["rgb"])
        else:
            data["hs_color"] = list(cell["color"]["hs"])
    return C.call("light.turn_on", [light], lights, data)


def needs(cell: dict[str, Any]) -> set[str]:
    """The capabilities a cell needs from a light."""
    if cell["kind"] != "on":
        return set()
    out = set()
    if cell["brightness_pct"] is not None:
        out.add("brightness")
    if cell["color_temp_kelvin"] is not None:
        out.add("color_temp")
    if cell["color"]:
        out.add("color")
    if cell["effect"]:
        out.add("effect")
    return out


# ------------------------------------------------------------------ validation
def _values(raw: Any, path: str, p: Problems) -> list[str]:
    out: list[str] = []
    for i, value in enumerate((raw if isinstance(raw, list) else [])[:MAX_VALUES]):
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            p.add(f"{path}[{i}]", "A mood value is the text of an option")
        elif value in out:
            p.add(f"{path}[{i}]", "This value is listed twice")
        else:
            out.append(value)
    return out


def _mood(raw: Any, path: str, p: Problems, lights: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    values = _values(raw.get("values"), f"{path}.values", p)
    auto = None
    if isinstance(raw.get("auto"), dict):
        value = raw["auto"].get("value")
        if isinstance(value, str) and value.strip():
            auto = {"value": value, "entity_id": C.entity(raw["auto"].get("entity_id"), f"{path}.auto.entity_id", p, MOOD_DOMAINS,
                                                          "the entity that holds the effective mood")}
            if value in values:
                p.add(f"{path}.auto.value", f"“{value}” is a mood column and the automatic mode at once")
    return {"enabled": C.flag(raw.get("enabled")),
            "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, MOOD_DOMAINS, "the mood selector"),
            "values": values, "auto": auto,
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, lights, values, validate_cell, MOOD_KINDS),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, lights, validate_cell, MOOD_KINDS),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, validate_cell, MOOD_KINDS),
            "apply_on_change": C.flag(raw.get("apply_on_change"))}


def column_id(column: dict[str, Any]) -> str:
    return f"{column['source']}:{column['state']}"


def _columns(raw: Any, path: str, p: Problems) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, column in enumerate((raw if isinstance(raw, list) else [])[:MAX_COLUMNS]):
        column = column if isinstance(column, dict) else {}
        source, state = column.get("source"), column.get("state")
        if not isinstance(source, str) or not P.SOURCE_ID.match(source) or state not in ("occupied", "vacant", "possible"):
            p.add(f"{path}[{i}]", "A column is a presence source and a state")
            continue
        clean = {"source": source, "state": state}
        if clean in out:
            p.add(f"{path}[{i}]", "This column is listed twice")
        else:
            out.append(clean)
    return out


def _presence(raw: Any, path: str, p: Problems, lights: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    columns = _columns(raw.get("columns"), f"{path}.columns", p)
    ids = [column_id(c) for c in columns]
    gate: dict[str, list[str]] = {}
    for light, chosen in (raw.get("gate") if isinstance(raw.get("gate"), dict) else {}).items():
        if light not in lights or not isinstance(chosen, list):
            if chosen is not None:
                p.add(f"{path}.gate.{light}", "Unknown light")
            continue
        unknown = [c for c in chosen if c not in ids]
        if unknown:
            p.add(f"{path}.gate.{light}", "The permission names a column that does not exist")
        gate[light] = [c for c in ids if c in chosen]
    policy = raw.get("policy") if raw.get("policy") in POLICIES else None
    return {"enabled": C.flag(raw.get("enabled")), "columns": columns,
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, lights, ids, validate_cell, CELL_KINDS),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, lights, validate_cell, CELL_KINDS),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, validate_cell, CELL_KINDS),
            "policy": policy, "gate": gate, "apply_on_change": C.flag(raw.get("apply_on_change"))}


def _bands(raw: Any, path: str, p: Problems) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    raw = raw if isinstance(raw, list) else []
    if len(raw) > MAX_BANDS:
        p.add(path, f"At most {MAX_BANDS} light-level bands")
    for i, band in enumerate(raw[:MAX_BANDS]):
        band = band if isinstance(band, dict) else {}
        band_id = C.short_id(band.get("id"), "b", f"{path}[{i}].id", p)
        if any(b["id"] == band_id for b in out):
            p.add(f"{path}[{i}].id", "Two bands share an id")
        below = C.number(band.get("below"), f"{path}[{i}].below", p, 0, 1_000_000)
        out.append({"id": band_id, "name": C.text(band.get("name"), f"{path}[{i}].name", p, limit=40, required=True),
                    "below": below if i < len(raw[:MAX_BANDS]) - 1 else None})
        if i == len(raw[:MAX_BANDS]) - 1 and band.get("below") not in (None, ""):
            p.add(f"{path}[{i}].below", "The brightest band has no upper limit")
    limits = [b["below"] for b in out[:-1] if b["below"] is not None]
    if limits != sorted(set(limits)):
        p.add(path, "The upper limits must rise from the darkest band to the brightest")
    return out


def _lux(raw: Any, path: str, p: Problems, lights: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    bands = _bands(raw.get("bands"), f"{path}.bands", p)
    ids = [b["id"] for b in bands]
    gate: dict[str, dict[str, Any]] = {}
    for light, chosen in (raw.get("gate") if isinstance(raw.get("gate"), dict) else {}).items():
        if light not in lights or not isinstance(chosen, dict):
            if chosen:
                p.add(f"{path}.gate.{light}", "Unknown light")
            continue
        entry = {}
        for key in ("on", "off"):
            value = chosen.get(key)
            if value is None:
                entry[key] = None
            elif isinstance(value, list) and all(v in ids for v in value):
                entry[key] = [i for i in ids if i in value]
            else:
                p.add(f"{path}.gate.{light}.{key}", "The permission names a band that does not exist")
                entry[key] = None
        if entry["on"] is not None or entry["off"] is not None:
            gate[light] = entry
    return {"enabled": C.flag(raw.get("enabled")),
            "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, LUX_DOMAINS, "a light-level sensor"),
            "bands": bands, "hysteresis": C.number(raw.get("hysteresis"), f"{path}.hysteresis", p, 0, 100_000),
            "delay_seconds": C.seconds(raw.get("delay_seconds"), f"{path}.delay_seconds", p, 3600),
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, lights, ids, validate_cell, CELL_KINDS),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, lights, validate_cell, CELL_KINDS),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, validate_cell, CELL_KINDS),
            "gate": gate, "apply_on_change": C.flag(raw.get("apply_on_change"))}


def validate_schedule(raw: Any, path: str, p: Problems, rows: list[str], cell: Any, kinds: tuple[str, ...]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    windows = []
    for i, window in enumerate((raw.get("windows") if isinstance(raw.get("windows"), list) else [])[:MAX_WINDOWS]):
        window = window if isinstance(window, dict) else {}
        clean = {"after": C.time_of_day(window.get("after"), f"{path}.windows[{i}].after", p),
                 "before": C.time_of_day(window.get("before"), f"{path}.windows[{i}].before", p),
                 "weekdays": C.weekdays(window.get("weekdays"), f"{path}.windows[{i}].weekdays", p)}
        if clean["after"] is None and clean["before"] is None and not clean["weekdays"]:
            p.add(f"{path}.windows[{i}]", "Give a time range, weekdays or both")
        windows.append(clean)
    entries = []
    for i, entry in enumerate((raw.get("entries") if isinstance(raw.get("entries"), list) else [])[:MAX_ENTRIES]):
        entry = entry if isinstance(entry, dict) else {}
        cells = {}
        for row, value in (entry.get("cells") if isinstance(entry.get("cells"), dict) else {}).items():
            if value is None:
                continue
            if row not in rows:
                p.add(f"{path}.entries[{i}].cells.{row}", f"{row} is not one of this designer's rows")
                continue
            cells[row] = cell(value, f"{path}.entries[{i}].cells.{row}", p, kinds)
        entries.append({"id": C.short_id(entry.get("id"), "s", f"{path}.entries[{i}].id", p),
                        "name": C.text(entry.get("name"), f"{path}.entries[{i}].name", p, limit=40),
                        "at": C.time_of_day(entry.get("at"), f"{path}.entries[{i}].at", p, required=True),
                        "weekdays": C.weekdays(entry.get("weekdays"), f"{path}.entries[{i}].weekdays", p),
                        "cells": {r: cells[r] for r in rows if r in cells}})
    return {"enabled": C.flag(raw.get("enabled")), "windows": windows, "entries": entries}


def validate_override(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, OVERRIDE_DOMAINS,
                                  "a switch, toggle helper or binary sensor (on = automatic changes paused)"),
            "manual_minutes": C.number(raw.get("manual_minutes"), f"{path}.manual_minutes", p, 1, 1440, integer=True)}


def validate(raw: dict[str, Any], designer_id: str, targets: list[str], path: str, p: Problems) -> dict[str, Any]:
    lights: list[str] = []
    for i, light in enumerate(raw.get("lights") if isinstance(raw.get("lights"), list) else []):
        if light not in targets or not str(light).startswith("light."):
            p.add(f"{path}.lights[{i}]", f"{light} is not one of this device's lights")
        elif light not in lights:
            lights.append(light)
    lights = [t for t in targets if t in lights]
    primary = raw.get("primary") if raw.get("primary") in LAYERS else None
    return {"id": designer_id, "kind": KIND, "version": VERSION,
            "name": C.text(raw.get("name"), f"{path}.name", p) or DEFAULT_NAME,
            "enabled": raw.get("enabled") is not False, "lights": lights, "primary": primary,
            "mood": _mood(raw.get("mood"), f"{path}.mood", p, lights),
            "presence": _presence(raw.get("presence"), f"{path}.presence", p, lights),
            "lux": _lux(raw.get("lux"), f"{path}.lux", p, lights),
            "schedule": validate_schedule(raw.get("schedule"), f"{path}.schedule", p, lights, validate_cell, ENTRY_KINDS),
            "override": validate_override(raw.get("override"), f"{path}.override", p),
            "rechecks": C.flag(raw.get("rechecks"))}


def sources_of(designer: dict[str, Any]) -> set[str]:
    return {c["source"] for c in designer["presence"]["columns"]}


# ------------------------------------------------------------------ expansion
class _Gen:
    """The expansion of one lighting designer into rules (see ``designers.expand``)."""

    def __init__(self, d: dict[str, Any], x: Any) -> None:
        self.d, self.x = d, x
        self.did = d["id"]
        self.refs = x.refs
        self.lights = d["lights"]
        self.d_primary: str | None = None
        self.mood_ready = False
        self.sources = x.sources
        self.ok = True
        self.view: dict[str, Any] = {"layers": {}, "rules": [], "primary": None, "problems": []}

    # ------------------------------------------------------------ problems
    def block(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("blocking", code, message, self.did, layer)
        self.ok = False

    def warn(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("warning", code, message, self.did, layer)

    def info(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("info", code, message, self.did, layer)

    # ------------------------------------------------------------ pieces
    def name(self, *parts: Any) -> str:
        return " · ".join(str(plain(p)) for p in (self.d["name"], *parts) if p not in (None, ""))

    def source(self, column: dict[str, Any]) -> dict[str, Any] | None:
        return self.sources.get(column["source"])

    def col_label(self, column: dict[str, Any]) -> str:
        source = self.source(column)
        return P.label(source, column["state"]) if source else f"(missing source {column['source']})"

    def overlaps(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """Can these two situations hold at the same time? Only opposite states of one source cannot."""
        if a["source"] == b["source"]:
            return {a["state"], b["state"]} not in ({"occupied", "vacant"},)
        return True

    # values that can become blueprint inputs
    def key(self, *parts: Any) -> str:
        from . import input_key
        return input_key(self.did, *parts)

    def mood_entity(self) -> Any:
        return self.x.e(self.d["mood"]["entity_id"], "The mood selector", group="Trigger entities",
                        name=f"{self.d['name']}: mood selector")

    def mood_value(self, value: str) -> Any:
        return self.x.text(self.key("mood", value), value, f"{self.d['name']}: mood “{value}”",
                           f"The state of the mood selector that stands for “{value}”.")

    def auto(self) -> tuple[Any, Any]:
        auto = self.d["mood"]["auto"]
        return (self.x.text(self.key("mood_auto"), auto["value"], f"{self.d['name']}: automatic mode",
                            "The state of the mood selector that means “automatic”."),
                self.x.e(auto["entity_id"], "The entity that holds the effective mood", group="Trigger entities"))

    def lux_entity(self) -> Any:
        return self.x.e(self.d["lux"]["entity_id"], "The light-level sensor", group="Sensor entities",
                        name=f"{self.d['name']}: light-level sensor")

    def mood_is(self, value: str) -> list[dict[str, Any]]:
        mood = self.d["mood"]
        own = C.c_state(self.mood_entity(), self.mood_value(value))
        if mood["auto"]:
            auto_value, auto_entity = self.auto()
            return [C.any_of([own, C.group("and", [C.c_state(self.mood_entity(), auto_value),
                                                   C.c_state(auto_entity, self.mood_value(value))])])]
        return [own]

    def mood_triggers(self, value: str) -> list[dict[str, Any]]:
        mood = self.d["mood"]
        out = [C.t_state(self.mood_entity(), to=self.mood_value(value))]
        if mood["auto"]:
            auto_value, auto_entity = self.auto()
            out += [C.t_state(self.mood_entity(), to=auto_value), C.t_state(auto_entity, to=self.mood_value(value))]
        return out

    def limit(self, i: int) -> Any:
        band = self.d["lux"]["bands"][i]
        return self.x.number(self.key(band["id"], "below"), band["below"], f"{self.d['name']}: {band['name']} up to",
                             f"The upper limit of the light-level band “{band['name']}”.")

    def band_bounds(self, first: int, last: int) -> tuple[Any, Any]:
        bands = self.d["lux"]["bands"]
        lower = self.limit(first - 1) if first > 0 else None
        upper = self.limit(last) if last < len(bands) - 1 else None
        return lower, upper

    def band_items(self, first: int, last: int) -> list[dict[str, Any]]:
        """Conditions for "the light level is in bands first..last" (thresholds without hysteresis)."""
        sensor = self.lux_entity()
        lower, upper = self.band_bounds(first, last)
        items = []
        if upper is not None:
            items.append(C.c_numeric(sensor, below=upper))
        if lower is not None:
            items.append(C.group("not", [C.c_numeric(sensor, below=lower)]))
        if upper is None:  # no "below" that is false for a sensor without a number
            items += [C.c_state(sensor, "unavailable", negate=True), C.c_state(sensor, "unknown", negate=True)]
        return items

    def band_entries(self, first: int, last: int) -> list[dict[str, Any]]:
        """Triggers "the light level has come into bands first..last": falling below the upper limit, or rising
        above the lower limit plus the hysteresis, held for the delay."""
        lux = self.d["lux"]
        lower, upper = self.band_bounds(first, last)
        delay = self.x.seconds(self.key("lux_delay"), lux["delay_seconds"], f"{self.d['name']}: light-level delay",
                               "How long a new light level must hold before the lights react.")
        out = []
        if upper is not None:
            out.append(C.t_numeric(self.lux_entity(), below=upper, for_seconds=delay))
        if lower is not None:
            above = lower if not plain(lux["hysteresis"]) else _round(plain(lower) + plain(lux["hysteresis"]))
            out.append(C.t_numeric(self.lux_entity(), above=above, for_seconds=delay))
        return out

    def runs(self, band_ids: list[str]) -> list[tuple[int, int]]:
        ids = [b["id"] for b in self.d["lux"]["bands"]]
        idx = sorted(ids.index(b) for b in band_ids)
        out: list[tuple[int, int]] = []
        for i in idx:
            if out and out[-1][1] == i - 1:
                out[-1] = (out[-1][0], i)
            else:
                out.append((i, i))
        return out

    def lux_gate(self, light: str, kind: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """``(conditions, act-on-change triggers)`` of the light-level permission for an ON or OFF; conditions
        None: never allowed."""
        lux = self.d["lux"]
        if not lux["enabled"] or self.d_primary == "lux":
            return [], []
        allowed = (lux["gate"].get(light) or {}).get(kind)
        if allowed is None:
            return [], []
        if not allowed:
            return None, []
        runs = self.runs(allowed)
        options = [self.band_items(a, b) for a, b in runs]
        condition = options[0] if len(options) == 1 else [C.group("or", [C.group("and", o) for o in options])]
        triggers = [t for a, b in runs for t in self.band_entries(a, b)] if lux["apply_on_change"] else []
        return condition, triggers

    def presence_gate(self, light: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        pres = self.d["presence"]
        if not pres["enabled"] or self.d_primary == "presence" or light not in pres["gate"]:
            return [], []
        chosen = [c for c in pres["columns"] if column_id(c) in pres["gate"][light]]
        if not chosen:
            return None, []  # an explicit empty permission: ON is never allowed
        options = [P.active(self.source(c), c["state"], self.x) for c in chosen]
        condition = options[0] if len(options) == 1 else [C.group("or", [C.group("and", o) for o in options])]
        triggers = [P.entered(self.source(c), c["state"], self.x) for c in chosen] if pres["apply_on_change"] else []
        return condition, triggers

    def windows(self) -> list[dict[str, Any]]:
        schedule = self.d["schedule"]
        if not schedule["enabled"] or not schedule["windows"]:
            return []
        items = []
        for i, w in enumerate(schedule["windows"]):
            after = self.x.time(self.key("window", i, "after"), w["after"], f"{self.d['name']}: active from", "Start of a time window.")
            before = self.x.time(self.key("window", i, "before"), w["before"], f"{self.d['name']}: active until", "End of a time window.")
            items.append(C.c_time(after, before, w["weekdays"]))
        return [C.any_of(items)]

    def override(self, light: Any) -> list[dict[str, Any]]:
        override = self.d["override"]
        out = []
        if override["entity_id"]:
            out.append(C.c_state(self.override_entity(), "off"))
        if override["manual_minutes"]:
            minutes = self.x.number(self.key("manual_minutes"), override["manual_minutes"],
                                    f"{self.d['name']}: pause after a manual change",
                                    "Minutes without automatic changes after somebody changed a light.",
                                    group="Delays and durations", selector={"number": {"min": 1, "max": 1440, "unit_of_measurement": "min", "mode": "box"}})
            ref = self.refs.ref(self.x.e(light, "Controlled light"))
            text = (f"{{{{ states[{ref}] is none or states[{ref}].context.parent_id is not none or "
                    f"(as_timestamp(now()) - as_timestamp(states[{ref}].last_updated)) >= {self.refs.num(minutes)} * 60 }}}}")
            out.append(self.refs.template(text, lambda states, e=light, m=minutes: _not_manual(states, e, m)))
        return out

    def override_entity(self) -> Any:
        return self.x.e(self.d["override"]["entity_id"], "Manual override signal: while it is on, nothing is changed",
                        group="Sensor entities")

    # ------------------------------------------------------------ emitting
    def emit(self, layer: str, light: Any, column: str | None, value: str | None, cell: dict[str, Any],
             triggers: list[dict[str, Any]], conditions: list[dict[str, Any]], resolution: dict[str, Any],
             label_parts: list[str], *, gated: bool = True, recheck: bool = False) -> None:
        conditions = list(conditions)
        if gated:
            kind = "on" if cell["kind"] == "on" else "off"
            lux_cond, lux_triggers = self.lux_gate(plain(light), kind)
            if lux_cond is None:
                self.info("never_allowed", f"{self.name(plain(light), *label_parts)}: {kind.upper()} is never allowed by the "
                          "light-level permissions, so this cell does nothing.", "lux")
                return
            pres_cond, pres_triggers = self.presence_gate(plain(light)) if kind == "on" else ([], [])
            if pres_cond is None:
                self.info("never_allowed", f"{self.name(plain(light), *label_parts)}: ON is never allowed by the presence "
                          "permissions, so this cell does nothing.", "presence")
                return
            conditions += lux_cond + pres_cond + self.windows()
            triggers = triggers + lux_triggers + pres_triggers
        conditions += self.override(light)
        exclusive = {}
        if value is not None:
            exclusive[f"{self.did}:mood"] = value
        if layer == "lux" and column:
            exclusive[f"{self.did}:band"] = column
        rule_id = C.generated_rule_id(self.did, layer, plain(light), column, value)
        meta = {"designer_id": self.did, "designer": KIND, "layer": layer, "target": plain(light), "column": column,
                "mood": value, "exclusive": exclusive, "cell": cell, "resolution": {k: v for k, v in resolution.items() if k != "cell"}}
        target = self.x.e(light, "Controlled light")
        name = self.name(plain(light), *label_parts)
        action = command(cell, target, self.x.targets, self.x, self.key(layer, plain(light), column, value), name)
        raw = C.rule(rule_id, name, triggers, conditions, [action], meta, recheck=recheck,
                     override_entity=self.override_entity() if self.d["override"]["entity_id"] else None)
        self.x.add_rule(raw, self.did)
        self.view["rules"].append(rule_id)

    def emit_resolved(self, layer: str, light: Any, column: str | None, res: dict[str, Any], triggers: list[dict[str, Any]],
                      conditions: list[dict[str, Any]], parts: list[str], *, gated: bool = True, recheck: bool = False) -> None:
        cell = C.acting(res)
        if cell is None:
            return
        if cell["kind"] != "mood":
            self.emit(layer, light, column, None, cell, triggers, conditions, res, parts, gated=gated, recheck=recheck)
            return
        mood = self.d["mood"]
        if not self.mood_ready:
            return
        for value in mood["values"]:
            mres = C.resolve((mood["cells"].get(plain(light)) or {}).get(value), mood["row_defaults"].get(plain(light)), mood["default"])
            mcell = C.acting(mres)
            if mcell is None:
                continue
            extra = self.mood_triggers(value) if mood["apply_on_change"] else []
            self.emit(layer, light, column, value, mcell, triggers + extra, conditions + self.mood_is(value),
                      {**res, "mood": {k: v for k, v in mres.items() if k != "cell"}}, [*parts, value],
                      gated=gated, recheck=recheck)

    # ------------------------------------------------------------ the whole designer
    def run(self) -> None:
        d = self.d
        enabled = [layer for layer in LAYERS if d[layer]["enabled"]]
        primary = d["primary"] if d["primary"] else (enabled[0] if len(enabled) == 1 else None)
        self.d_primary = primary
        self.view["primary"] = primary
        if not d["lights"]:
            self.block("no_lights", f"“{d['name']}”: choose the lights it controls (the rows of its matrices).")
        if len(enabled) > 1 and not d["primary"]:
            self.block("no_primary", f"“{d['name']}”: several matrices are on. Choose which one decides what the lights "
                       "do; the others then give permissions or the mood palette.")
        if primary and primary not in enabled:
            self.block("primary_off", f"“{d['name']}”: the deciding matrix ({LAYER_NAMES[primary]}) is turned off.")
        self.check_sources(primary)
        if not self.ok:
            return
        for light in d["lights"]:
            if primary == "mood":
                self.from_mood(light)
            elif primary == "presence":
                self.from_presence(light)
            elif primary == "lux":
                self.from_lux(light)
            self.from_schedule(light)
        if primary == "presence":
            self.policy_conflicts()

    def uses_mood(self, primary: str | None) -> bool:
        """Do the cells that can act (of the deciding matrix and the timed entries) refer to the mood?"""
        d = self.d
        cells = []
        if primary in ("presence", "lux"):
            m = d[primary]
            cells += [c for row in m["cells"].values() for c in row.values()]
            cells += list(m["row_defaults"].values()) + ([m["default"]] if m["default"] else [])
        if d["schedule"]["enabled"]:
            cells += [c for e in d["schedule"]["entries"] for c in e["cells"].values()]
        return any(c["kind"] == "mood" for c in cells)

    def check_sources(self, primary: str | None) -> None:
        d = self.d
        mood = d["mood"]
        self.mood_ready = False
        needs_mood = primary == "mood" or self.uses_mood(primary)
        if needs_mood:
            if not mood["enabled"]:
                self.block("mood_off", f"“{d['name']}”: some cells say “Use mood”, but the mood matrix is off.", "mood")
            elif not mood["entity_id"]:
                self.block("missing_source", f"“{d['name']}”: choose the mood selector (the entity whose state is the "
                           "mood). Nothing is chosen for you.", "mood")
            elif mood["auto"] and not mood["auto"]["entity_id"]:
                self.block("missing_source", f"“{d['name']}”: “{mood['auto']['value']}” is set up as the automatic "
                           "mode: choose the entity that holds the effective mood.", "mood")
            elif not mood["values"]:
                self.block("no_values", f"“{d['name']}”: choose the mood values (columns) the lights react to.", "mood")
            else:
                self.mood_ready = True
        pres = d["presence"]
        if not pres["enabled"]:
            used_columns = []
        elif primary == "presence":
            used_columns = list(pres["columns"])
        else:  # a permission: only the columns it names
            named = {c for chosen in pres["gate"].values() for c in chosen}
            used_columns = [c for c in pres["columns"] if column_id(c) in named]
        for column in used_columns:
            source = self.source(column)
            if source is None:
                self.block("missing_source", f"“{d['name']}”: a presence column refers to a source that no longer "
                           "exists.", "presence")
                continue
            if column["state"] not in P.STATES[source["kind"]]:
                self.block("invalid_column", f"“{d['name']}”: {source['name']} cannot be “{column['state']}”.", "presence")
                continue
            missing = P.missing(source, column["state"])
            if missing:
                self.block("missing_source", f"“{d['name']}”: {missing}. Choose it in Presence sources; nothing is "
                           "chosen for you.", "presence")
            elif source["kind"] == "position" and not source["entry_seconds"] and not source["exit_seconds"]:
                self.warn("position_delay", f"“{source['name']}” is a position zone without entry or exit delay. Position "
                          "sensors report x and y as separate changes, so a half-updated reading can count for a moment: "
                          "a delay of one or two seconds avoids that.", "presence")
        if primary == "presence" and not pres["columns"]:
            self.block("no_columns", f"“{d['name']}”: add the presence situations (columns) the lights react to.", "presence")
        lux = d["lux"]
        if lux["enabled"] and (primary == "lux" or lux["gate"]):
            if not lux["entity_id"]:
                self.block("missing_source", f"“{d['name']}”: choose the light-level sensor.", "lux")
            if len(lux["bands"]) < 2:
                self.block("no_bands", f"“{d['name']}”: define at least two light-level bands (names and limits).", "lux")
            for band in lux["bands"][:-1]:
                if band["below"] is None:
                    self.block("missing_threshold", f"“{d['name']}”: give the upper limit of the band “{band['name']}”. "
                               "No threshold is assumed.", "lux")

    def from_mood(self, light: str) -> None:
        mood = self.d["mood"]
        for value in mood["values"]:
            res = C.resolve((mood["cells"].get(light) or {}).get(value), mood["row_defaults"].get(light), mood["default"])
            if res["state"] == "missing_default":
                self.block("missing_default", f"“{self.d['name']}”: {light} / {value} says “Use default”, but no default "
                           "is configured (for the light or the matrix).", "mood")
                continue
            cell = C.acting(res)
            if cell is None:
                continue
            self.emit("mood", light, value, value, cell, self.mood_triggers(value), self.mood_is(value), res, [value])

    def dominating(self, light: str, column: dict[str, Any], decided: list[dict[str, Any]]) -> list[dict[str, Any]]:
        policy = self.d["presence"]["policy"]
        columns = self.d["presence"]["columns"]
        if policy == "priority":
            return [c for c in decided if columns.index(c) < columns.index(column) and self.overlaps(c, column)]
        if policy == "exclusive":
            return [c for c in decided if c != column and self.overlaps(c, column)]
        return []

    def from_presence(self, light: str) -> None:
        pres = self.d["presence"]
        resolved = {column_id(c): C.resolve((pres["cells"].get(light) or {}).get(column_id(c)), pres["row_defaults"].get(light),
                                            pres["default"]) for c in pres["columns"]}
        decided = [c for c in pres["columns"] if resolved[column_id(c)]["state"] in ("explicit", "default", "none")]
        acting = [c for c in pres["columns"] if C.acting(resolved[column_id(c)]) is not None]
        for column in pres["columns"]:
            if resolved[column_id(column)]["state"] == "missing_default":
                self.block("missing_default", f"“{self.d['name']}”: {light} / {self.col_label(column)} says “Use default”, "
                           "but no default is configured.", "presence")
        overlapping = [(a, b) for i, a in enumerate(acting) for b in acting[i + 1:] if self.overlaps(a, b)]
        if overlapping and pres["policy"] is None:
            a, b = overlapping[0]
            self.block("no_policy", f"“{self.d['name']}”: {self.col_label(a)} and {self.col_label(b)} can hold at the same "
                       f"time and both have a cell for {light}. Choose what happens then (priority by column order, "
                       "nothing while ambiguous, or both when compatible).", "presence")
            return
        for column in acting:
            source = self.source(column)
            dominating = self.dominating(light, column, decided)
            recheck = self.d["rechecks"] and P.recheckable(source, column["state"]) and not dominating
            triggers = [P.entered(source, column["state"], self.x, occupancy_type=recheck)]
            triggers += [P.ended(self.source(d), d["state"], self.x) for d in dominating]
            conditions = list(P.active(source, column["state"], self.x))
            for d in dominating:
                conditions.append(C.negation(P.active(self.source(d), d["state"], self.x)))
            self.emit_resolved("presence", light, column_id(column), resolved[column_id(column)], triggers, conditions,
                               [self.col_label(column)], recheck=recheck)

    def from_lux(self, light: str) -> None:
        lux = self.d["lux"]
        for i, band in enumerate(lux["bands"]):
            res = C.resolve((lux["cells"].get(light) or {}).get(band["id"]), lux["row_defaults"].get(light), lux["default"])
            if res["state"] == "missing_default":
                self.block("missing_default", f"“{self.d['name']}”: {light} / {band['name']} says “Use default”, but no "
                           "default is configured.", "lux")
                continue
            self.emit_resolved("lux", light, band["id"], res, self.band_entries(i, i), self.band_items(i, i), [band["name"]])

    def from_schedule(self, light: str) -> None:
        schedule = self.d["schedule"]
        if not schedule["enabled"]:
            return
        for entry in schedule["entries"]:
            cell = entry["cells"].get(light)
            if cell is None:
                continue
            res = {"state": "none" if cell["kind"] == "none" else "explicit", "cell": cell, "source": "cell"}
            at = self.x.time(self.key(entry["id"], "at"), entry["at"], f"{self.d['name']}: {entry['name'] or 'timed entry'} at",
                             "The time of a timed entry.")
            self.emit_resolved("schedule", light, entry["id"], res, [C.t_time(at, entry["weekdays"])], [],
                               [entry["name"] or f"at {str(plain(entry['at']))[:5]}"], gated=False)

    def policy_conflicts(self) -> None:
        """With the "compatible" policy, two situations that can hold together must not want different things."""
        pres = self.d["presence"]
        if pres["policy"] != "compatible":
            return
        for light in self.d["lights"]:
            commands = {}
            for column in pres["columns"]:
                res = C.resolve((pres["cells"].get(light) or {}).get(column_id(column)), pres["row_defaults"].get(light), pres["default"])
                cell = C.acting(res)
                if cell is not None:
                    commands[column_id(column)] = (column, cell)
            items = list(commands.values())
            for i, (a, ca) in enumerate(items):
                for b, cb in items[i + 1:]:
                    if not self.overlaps(a, b) or ca == cb:
                        continue
                    ids = [r["id"] for r in self.x.rules if r["generated"].get("designer_id") == self.did
                           and r["generated"]["target"] == light and r["generated"]["column"] in (column_id(a), column_id(b))]
                    self.x.conflict(
                        f"zone_overlap:{self.did}:{light}:{column_id(a)}:{column_id(b)}", "zone_overlap",
                        f"{self.col_label(a)} and {self.col_label(b)} can hold at the same time, and for {light} one wants "
                        f"{label(ca)} and the other {label(cb)}. With “both when compatible” the situation that started "
                        "last has the last word. Make the cells equal, choose priority or “nothing while ambiguous”, or "
                        "confirm that the latest situation may win.", sorted(set(ids)), light)


def _round(value: float) -> float | int:
    value = round(float(value), 4)
    return int(value) if value.is_integer() else value


def _not_manual(states: dict[str, Any], entity_id: Any, minutes: Any) -> bool:
    state = states.get(plain(entity_id))
    if state is None:
        return True
    manual = state.get("manual") is True
    age = state.get("age")
    return not manual or age is None or age >= plain(minutes) * 60


def expand(designer: dict[str, Any], x: Any) -> dict[str, Any]:
    gen = _Gen(designer, x)
    if designer["enabled"]:
        gen.run()
    return {**gen.view, "resolved": resolved_views(designer, x)}


def resolved_views(d: dict[str, Any], x: Any) -> dict[str, Any]:
    """For the interface: what every cell means, after defaults, with where it comes from."""
    out: dict[str, Any] = {}
    for layer, columns in (("mood", d["mood"]["values"]), ("presence", [column_id(c) for c in d["presence"]["columns"]]),
                           ("lux", [b["id"] for b in d["lux"]["bands"]])):
        m = d[layer]
        out[layer] = {light: {c: _cell_view(C.resolve((m["cells"].get(light) or {}).get(c), m["row_defaults"].get(light), m["default"]),
                                            (m["cells"].get(light) or {}).get(c)) for c in columns} for light in d["lights"]}
    return out


def _cell_view(res: dict[str, Any], own: dict[str, Any] | None, label_fn: Any = None) -> dict[str, Any]:
    return {"label": (label_fn or resolved_label)(res), "state": res["state"], "source": res["source"],
            "own": own is not None, "explicit_default": bool(own and own["kind"] == "default")}


def capability_problems(d: dict[str, Any], caps: dict[str, dict[str, Any]], x: Any) -> None:
    """Cells that ask a light for something it cannot do (checked against what Home Assistant reports)."""
    for light in d["lights"]:
        cap = caps.get(light)
        if not cap or cap.get("unknown"):
            continue
        cells = []
        for layer in LAYERS:
            m = d[layer]
            cells += list((m["cells"].get(light) or {}).values())
            if m["row_defaults"].get(light):
                cells.append(m["row_defaults"][light])
            if m["default"]:
                cells.append(m["default"])
        cells += [e["cells"][light] for e in d["schedule"]["entries"] if light in e["cells"]]
        wanted = set().union(*(needs(c) for c in cells)) if cells else set()
        missing = sorted(w for w in wanted if not cap.get(w))
        if missing:
            x.problem("blocking", "capability", f"“{d['name']}”: {cap.get('name') or light} cannot "
                      + ", ".join({"brightness": "dim", "color_temp": "change its colour temperature",
                                   "color": "show colours", "effect": "run effects"}[m] for m in missing)
                      + " (Home Assistant reports what it supports). Change the cells that ask for it.", d["id"], None)
        effects = {c["effect"] for c in cells if c["kind"] == "on" and c["effect"]}
        unknown = sorted(e for e in effects if cap.get("effects") is not None and e not in cap["effects"])
        if unknown:
            x.problem("warning", "capability", f"“{d['name']}”: {cap.get('name') or light} does not list the effect "
                      f"{', '.join(unknown)}.", d["id"], None)
