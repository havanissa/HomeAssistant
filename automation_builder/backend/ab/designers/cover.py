"""The Cover Designer: sun shading by angle, cover modes, mappings from household moods, schedules.

Rows are the selected covers. Each cover has its own window (orientation with 0° = South, sector, sun elevation
range) and its own response to the sun:

* a fixed position (or "close" for covers that can only open and close), or
* a position that follows the sun: linear between two points of the sun's elevation or of its profile angle
  (see ``solar``), so a blind can lower progressively as the sun reaches deeper into the room.

Cover *modes* (Daylight, Privacy, Sleep … - names the user chooses) are a matrix of covers × modes. A mode is
active when a selector shows its name, or (normal mood mapping) when a household mood that the user mapped to it
is active. Without either, modes can still be applied by timed entries.

Where sun shading and a mode can both want something, the user decides per mode, explicitly:

* ``mode_wins``: while the mode is active the sun shading does nothing for that cover;
* ``sun_wins``: the sun shading takes over while the sun shines in; when it stops, the mode's position returns.

Stability: hysteresis (degrees) for leaving the sector and the elevation range, a minimum change of position, a
minimum time between movements, manual override (a signal and/or a pause after somebody moved the cover). A
weather-sensitive cover (an awning) gets no automatic opening without explicit wind/rain protection or the
user's statement that it has its own. Unavailable sun or sensor data never moves a cover, unless the user
configured a fallback.
"""

from __future__ import annotations

from typing import Any

from ..compiler import plain
from ..model import Problems
from . import common as C, lighting as L, presence as P, solar

KIND = "cover"
VERSION = 1
ROWS = "covers"
DEFAULT_NAME = "Covers"
CELL_KINDS = ("open", "close", "position", "none", "default")
ENTRY_KINDS = ("open", "close", "position", "none")
SUN = solar.SUN
TEMP_DOMAINS = ("sensor", "input_number", "number", "weather")
MAX_MODES = 12
POSITION_SELECTOR = {"number": {"min": 0, "max": 100, "unit_of_measurement": "%", "mode": "slider"}}
ANGLE_SELECTOR = {"number": {"min": 0, "max": 360, "unit_of_measurement": "°", "mode": "box", "step": 0.1}}
ELEVATION_SELECTOR = {"number": {"min": -10, "max": 90, "unit_of_measurement": "°", "mode": "box", "step": 0.1}}


# ------------------------------------------------------------------ cells
def validate_cell(raw: Any, path: str, p: Problems, kinds: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in kinds:
        p.add(path, "Unknown cell value")
        return {"kind": "none"}
    kind = raw["kind"]
    if kind != "position":
        return {"kind": kind}
    position = C.number(raw.get("position"), f"{path}.position", p, 0, 100, integer=True)
    tilt = C.number(raw.get("tilt"), f"{path}.tilt", p, 0, 100, integer=True)
    if position is None and tilt is None:
        p.add(path, "Give a position, a tilt or both")
    return {"kind": "position", "position": position, "tilt": tilt}


def label(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "UNSET"
    kind = cell["kind"]
    if kind in ("none", "default"):
        return {"none": "NO ACTION", "default": "USE DEFAULT"}[kind]
    if kind in ("open", "close"):
        return kind.upper()
    parts = [f"{plain(cell['position'])}%"] if cell["position"] is not None else []
    if cell["tilt"] is not None:
        parts.append(f"tilt {plain(cell['tilt'])}%")
    return " · ".join(parts)


def resolved_label(res: dict[str, Any]) -> str:
    if res["state"] == "unset":
        return "UNSET"
    if res["state"] == "missing_default":
        return "DEFAULT: none configured"
    if res["state"] == "default":
        return f"DEFAULT: {label(res['cell'])}"
    if res["state"] == "none" and res.get("inherited"):
        return "DEFAULT: NO ACTION"
    return label(res["cell"])


def needs(cell: dict[str, Any]) -> set[str]:
    if cell["kind"] == "open":
        return {"open"}
    if cell["kind"] == "close":
        return {"close"}
    if cell["kind"] == "position":
        return ({"position"} if cell["position"] is not None else set()) | ({"tilt"} if cell["tilt"] is not None else set())
    return set()


def commands(cell: dict[str, Any], cover: Any, covers: list[Any], x: Any = None, key: str = "", name: str = "") -> list[dict[str, Any]]:
    def value(field: str, raw: Any) -> Any:
        if x is None:
            return raw
        from . import input_key
        return x.v(input_key(key, field), raw, kind="data", group="Cover positions", name=f"{name}: {field}",
                   description=f"The {field} of this cell (100 = open).", selector=POSITION_SELECTOR)
    if cell["kind"] == "open":
        return [C.call("cover.open_cover", [cover], covers)]
    if cell["kind"] == "close":
        return [C.call("cover.close_cover", [cover], covers)]
    out = []
    if cell["position"] is not None:
        out.append(C.call("cover.set_cover_position", [cover], covers, {"position": value("position", cell["position"])}))
    if cell["tilt"] is not None:
        out.append(C.call("cover.set_cover_tilt_position", [cover], covers, {"tilt_position": value("tilt", cell["tilt"])}))
    return out


def opens(cell: dict[str, Any]) -> bool:
    """Does the command move the cover out of its closed (retracted) position?"""
    return cell["kind"] == "open" or (cell["kind"] == "position" and (cell["position"] or 0) > 0) or (
        cell["kind"] == "position" and cell["position"] is None)


# ------------------------------------------------------------------ validation
def _curve(raw: Any, path: str, p: Problems, *, allow_close: bool) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("kind") in (None, ""):
        return None
    kind = raw.get("kind")
    if kind == "fixed":
        return {"kind": "fixed", "value": C.number(raw.get("value"), f"{path}.value", p, 0, 100, integer=True)}
    if kind == "close" and allow_close:
        return {"kind": "close"}
    if kind == "curve":
        basis = raw.get("basis") if raw.get("basis") in ("elevation", "profile") else None
        if basis is None:
            p.add(f"{path}.basis", "Choose the sun elevation or the profile angle")
        points = []
        for i, point in enumerate((raw.get("points") if isinstance(raw.get("points"), list) else [])[:2]):
            if isinstance(point, list) and len(point) == 2:
                a = C.number(point[0], f"{path}.points[{i}]", p, -10, 90)
                v = C.number(point[1], f"{path}.points[{i}]", p, 0, 100)
                points.append([a, v])
        if len(points) == 2 and None not in points[0] + points[1] and points[0][0] == points[1][0]:
            p.add(f"{path}.points", "The two points need different angles")
        return {"kind": "curve", "basis": basis, "points": points}
    p.add(path, "Unknown response")
    return None


def _window(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out = {"orientation": C.number(raw.get("orientation"), f"{path}.orientation", p, -180, 180),
           "offset": C.number(raw.get("offset"), f"{path}.offset", p, -90, 90),
           "left": C.number(raw.get("left"), f"{path}.left", p, 0, 89),
           "right": C.number(raw.get("right"), f"{path}.right", p, 0, 89),
           "elevation_min": C.number(raw.get("elevation_min"), f"{path}.elevation_min", p, -10, 90),
           "elevation_max": C.number(raw.get("elevation_max"), f"{path}.elevation_max", p, -10, 90),
           "position": _curve(raw.get("position"), f"{path}.position", p, allow_close=True),
           "tilt": _curve(raw.get("tilt"), f"{path}.tilt", p, allow_close=False),
           "leave": validate_cell(raw["leave"], f"{path}.leave", p, ENTRY_KINDS) if raw.get("leave") else None,
           "weather_sensitive": C.flag(raw.get("weather_sensitive"))}
    if out["elevation_min"] is not None and out["elevation_max"] is not None and out["elevation_min"] >= out["elevation_max"]:
        p.add(f"{path}.elevation_max", "The highest elevation must be above the lowest")
    return out


def _sensor(raw: Any, path: str, p: Problems, domains: tuple[str, ...], what: str, *, states: bool = False) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {"entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, domains, what)}
    if states:
        chosen = raw.get("states") if isinstance(raw.get("states"), list) else []
        out["states"] = [s for s in dict.fromkeys(chosen) if isinstance(s, str) and 0 < len(s) <= 60][:12]
    else:
        out["above"] = C.number(raw.get("above"), f"{path}.above", p)
        out["hysteresis"] = C.number(raw.get("hysteresis"), f"{path}.hysteresis", p, 0, 1_000_000)
    return out if out["entity_id"] or out.get("above") is not None or out.get("states") else None


def _sun(raw: Any, path: str, p: Problems, covers: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    windows_raw = raw.get("windows") if isinstance(raw.get("windows"), dict) else {}
    windows = {}
    for cover, window in windows_raw.items():
        if cover not in covers:
            if window:
                p.add(f"{path}.windows.{cover}", f"{cover} is not one of this designer's covers")
            continue
        windows[cover] = _window(window, f"{path}.windows.{cover}", p)
    safety_raw = raw.get("safety") if isinstance(raw.get("safety"), dict) else {}
    safety = {"wind_entity": C.entity(safety_raw.get("wind_entity"), f"{path}.safety.wind_entity", p, ("sensor", "weather"), "a wind sensor"),
              "wind_above": C.number(safety_raw.get("wind_above"), f"{path}.safety.wind_above", p, 0, 500),
              "rain_entity": C.entity(safety_raw.get("rain_entity"), f"{path}.safety.rain_entity", p, ("binary_sensor", "input_boolean"),
                                      "a rain sensor (on = raining)"),
              "acknowledged": C.flag(safety_raw.get("acknowledged"))}
    return {"enabled": C.flag(raw.get("enabled")), "windows": {c: windows[c] for c in covers if c in windows},
            "hysteresis": C.number(raw.get("hysteresis"), f"{path}.hysteresis", p, 0, 45),
            "min_change": C.number(raw.get("min_change"), f"{path}.min_change", p, 1, 100, integer=True),
            "min_interval_minutes": C.number(raw.get("min_interval_minutes"), f"{path}.min_interval_minutes", p, 1, 1440, integer=True),
            "lux": _sensor(raw.get("lux"), f"{path}.lux", p, L.LUX_DOMAINS, "a light-level sensor"),
            "temperature": _sensor(raw.get("temperature"), f"{path}.temperature", p, ("sensor", "input_number", "number"),
                                   "an outdoor temperature sensor"),
            "weather": _sensor(raw.get("weather"), f"{path}.weather", p, ("weather",), "a weather entity", states=True),
            "presence": L._columns(raw.get("presence"), f"{path}.presence", p),  # noqa: SLF001
            "fallback": validate_cell(raw["fallback"], f"{path}.fallback", p, ENTRY_KINDS) if raw.get("fallback") else None,
            "safety": safety}


def _modes(raw: Any, path: str, p: Problems, covers: list[str]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    names: list[str] = []
    for i, name in enumerate((raw.get("names") if isinstance(raw.get("names"), list) else [])[:MAX_MODES]):
        if not isinstance(name, str) or not name.strip() or len(name) > 60:
            p.add(f"{path}.names[{i}]", "A mode needs a name")
        elif name in names:
            p.add(f"{path}.names[{i}]", "Two modes share a name")
        else:
            names.append(name)
    driver = raw.get("driver") if raw.get("driver") in ("selector", "mood") else None
    mood_map = {}
    for value, mode in (raw.get("mood_map") if isinstance(raw.get("mood_map"), dict) else {}).items():
        if mode is None:
            continue
        if mode not in names or not isinstance(value, str) or not value or len(value) > 120:
            p.add(f"{path}.mood_map", f"“{value}” is mapped to a mode that does not exist")
            continue
        mood_map[value] = mode
    policy = {}
    for mode, choice in (raw.get("sun_policy") if isinstance(raw.get("sun_policy"), dict) else {}).items():
        if mode in names and choice in ("mode_wins", "sun_wins"):
            policy[mode] = choice
    return {"enabled": C.flag(raw.get("enabled")), "names": names, "driver": driver,
            "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, ("input_select", "select", "sensor", "input_text"),
                                  "the cover-mode selector"),
            "mood_entity": C.entity(raw.get("mood_entity"), f"{path}.mood_entity", p, L.MOOD_DOMAINS, "the mood selector"),
            "mood_map": {k: mood_map[k] for k in sorted(mood_map)},
            "cells": C.cell_map(raw.get("cells"), f"{path}.cells", p, covers, names, validate_cell, CELL_KINDS),
            "row_defaults": C.row_defaults(raw.get("row_defaults"), f"{path}.row_defaults", p, covers, validate_cell, CELL_KINDS),
            "default": C.default_cell(raw.get("default"), f"{path}.default", p, validate_cell, CELL_KINDS),
            "sun_policy": {m: policy[m] for m in names if m in policy}}


def validate(raw: dict[str, Any], designer_id: str, targets: list[str], path: str, p: Problems) -> dict[str, Any]:
    covers: list[str] = []
    for i, cover in enumerate(raw.get("covers") if isinstance(raw.get("covers"), list) else []):
        if cover not in targets or not str(cover).startswith("cover."):
            p.add(f"{path}.covers[{i}]", f"{cover} is not one of this device's covers")
        elif cover not in covers:
            covers.append(cover)
    covers = [t for t in targets if t in covers]
    modes = _modes(raw.get("modes"), f"{path}.modes", p, covers)
    schedule = L.validate_schedule(raw.get("schedule"), f"{path}.schedule", p, covers, validate_cell, ENTRY_KINDS)
    raw_entries = (raw.get("schedule") or {}).get("entries") if isinstance(raw.get("schedule"), dict) else None
    for i, entry in enumerate(schedule["entries"]):
        mode = (raw_entries[i] if isinstance(raw_entries, list) and i < len(raw_entries) and isinstance(raw_entries[i], dict)
                else {}).get("mode")
        if mode is not None and mode not in modes["names"]:
            p.add(f"{path}.schedule.entries[{i}].mode", "This mode does not exist")
            mode = None
        entry["mode"] = mode
    return {"id": designer_id, "kind": KIND, "version": VERSION, "name": C.text(raw.get("name"), f"{path}.name", p) or DEFAULT_NAME,
            "enabled": raw.get("enabled") is not False, "covers": covers,
            "sun": _sun(raw.get("sun"), f"{path}.sun", p, covers), "modes": modes, "schedule": schedule,
            "override": L.validate_override(raw.get("override"), f"{path}.override", p)}


def sources_of(designer: dict[str, Any]) -> set[str]:
    return {c["source"] for c in designer["sun"]["presence"]}


# ------------------------------------------------------------------ expansion
class _Gen:
    def __init__(self, d: dict[str, Any], x: Any) -> None:
        self.d, self.x, self.did = d, x, d["id"]
        self.refs = x.refs
        self.ok = True
        self.view: dict[str, Any] = {"rules": [], "windows": {}}
        self.sensitive: set[str] = set()

    def key(self, *parts: Any) -> str:
        from . import input_key
        return input_key(self.did, *parts)

    def block(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("blocking", code, message, self.did, layer)
        self.ok = False

    def info(self, code: str, message: str, layer: str | None = None) -> None:
        self.x.problem("info", code, message, self.did, layer)

    def name(self, *parts: Any) -> str:
        return " · ".join(str(plain(p)) for p in (self.d["name"], *parts) if p not in (None, ""))

    # ------------------------------------------------------------ the pieces
    def ready(self, cover: str) -> str | None:
        """What is missing for the sun shading of this cover (None: complete)."""
        w = self.d["sun"]["windows"].get(cover)
        if w is None:
            return None
        for field, words in (("orientation", "the direction the window faces"), ("left", "the sector towards the left"),
                             ("right", "the sector towards the right"), ("elevation_min", "the lowest sun elevation")):
            if w[field] is None:
                return words
        if w["position"] is None and w["tilt"] is None:
            return "what the cover does in the sun (a position or a tilt)"
        for which in ("position", "tilt"):
            r = w[which]
            if r and r["kind"] == "fixed" and r["value"] is None:
                return f"the {which} in the sun"
            if r and r["kind"] == "curve" and (r["basis"] is None or len(r["points"]) != 2 or None in r["points"][0] + r["points"][1]):
                return f"both points of the {which} curve"
            if r and r["kind"] == "curve" and self.d["sun"]["min_change"] is None:
                return "the minimum change of position (a following curve needs it, so tiny angle changes do not move the cover)"
        return None

    def center(self, w: dict[str, Any]) -> float:
        return float(plain(w["orientation"])) + float(plain(w["offset"]) or 0)

    def sector(self, cover: str, w: dict[str, Any], margin: float = 0) -> tuple[Any, Any, bool]:
        start, end = solar.sector(self.center(w), float(plain(w["left"])) + margin, float(plain(w["right"])) + margin)
        start, end = _round(start), _round(end)
        wraps = start > end
        if margin:
            return start, end, wraps
        start = self.x.number(self.key(cover, "azimuth_from"), start, f"{self.d['name']}: {cover} sun azimuth from",
                              "Where the sector starts, in Home Assistant's azimuth (0° = North, 90° = East, 180° = South).",
                              group="Angles", selector=ANGLE_SELECTOR)
        end = self.x.number(self.key(cover, "azimuth_to"), end, f"{self.d['name']}: {cover} sun azimuth to",
                            "Where the sector ends, in Home Assistant's azimuth (0° = North).", group="Angles", selector=ANGLE_SELECTOR)
        return start, end, wraps

    def elevation(self, cover: str, w: dict[str, Any]) -> tuple[Any, Any]:
        low = self.x.number(self.key(cover, "elevation_min"), w["elevation_min"], f"{self.d['name']}: {cover} lowest sun elevation",
                            "Below this elevation the sun does not reach the window.", group="Angles", selector=ELEVATION_SELECTOR)
        high = self.x.number(self.key(cover, "elevation_max"), w["elevation_max"], f"{self.d['name']}: {cover} highest sun elevation",
                             "Above this elevation the sun does not reach into the room.", group="Angles", selector=ELEVATION_SELECTOR)
        return low, high

    def sensor(self, which: str) -> dict[str, Any] | None:
        s = self.d["sun"][which]
        if not s or not s["entity_id"]:
            return None
        return s

    def sensor_entity(self, which: str) -> Any:
        words = {"lux": "Light-level sensor for sun shading", "temperature": "Outdoor temperature sensor",
                 "weather": "Weather entity"}[which]
        return self.x.e(self.d["sun"][which]["entity_id"], words, group="Sensor entities")

    def threshold(self, which: str) -> Any:
        s = self.d["sun"][which]
        return self.x.number(self.key(which, "above"), s["above"], f"{self.d['name']}: {which} above",
                             f"Sun shading only when the {which} is above this value.")

    def in_sector(self, cover: str, w: dict[str, Any]) -> list[dict[str, Any]]:
        start, end, wraps = self.sector(cover, w)
        low, high = self.elevation(cover, w)
        az = (C.any_of([C.c_numeric(SUN, above=start, attribute="azimuth"), C.c_numeric(SUN, below=end, attribute="azimuth")])
              if wraps else C.c_numeric(SUN, above=start, below=end, attribute="azimuth"))
        return [az, C.c_numeric(SUN, above=low, below=high, attribute="elevation")]

    def context_items(self) -> list[dict[str, Any]]:
        """Thresholds, presence and time windows of the sun shading (all covers)."""
        items = []
        for which in ("lux", "temperature"):
            s = self.sensor(which)
            if s and s["above"] is not None:
                items.append(C.c_numeric(self.sensor_entity(which), above=self.threshold(which)))
        weather = self.sensor("weather")
        if weather and weather["states"]:
            items.append(C.any_of([C.c_state(self.sensor_entity("weather"), s) for s in weather["states"]]))
        columns = self.d["sun"]["presence"]
        if columns:
            options = [P.active(self.x.sources[c["source"]], c["state"], self.x) for c in columns]
            items += options[0] if len(options) == 1 else [C.group("or", [C.group("and", o) for o in options])]
        items += self.windows()
        return items

    def windows(self) -> list[dict[str, Any]]:
        schedule = self.d["schedule"]
        if not schedule["enabled"] or not schedule["windows"]:
            return []
        items = []
        for i, w in enumerate(schedule["windows"]):
            after = self.x.time(self.key("window", i, "after"), w["after"], f"{self.d['name']}: shading from", "Start of a time window.")
            before = self.x.time(self.key("window", i, "before"), w["before"], f"{self.d['name']}: shading until", "End of a time window.")
            items.append(C.c_time(after, before, w["weekdays"]))
        return [C.any_of(items)]

    def shading(self, cover: str, w: dict[str, Any]) -> list[dict[str, Any]]:
        return self.in_sector(cover, w) + self.context_items()

    def entering(self, cover: str, w: dict[str, Any]) -> list[dict[str, Any]]:
        start, end, _wraps = self.sector(cover, w)
        low, high = self.elevation(cover, w)
        out = [C.t_numeric(SUN, above=start, attribute="azimuth"), C.t_numeric(SUN, below=end, attribute="azimuth"),
               C.t_numeric(SUN, above=low, attribute="elevation")]
        if high is not None:
            out.append(C.t_numeric(SUN, below=high, attribute="elevation"))
        for which in ("lux", "temperature"):
            s = self.sensor(which)
            if s and s["above"] is not None:
                out.append(C.t_numeric(self.sensor_entity(which), above=self.threshold(which)))
        weather = self.sensor("weather")
        if weather:
            out += [C.t_state(self.sensor_entity("weather"), to=s) for s in weather["states"]]
        out += [P.entered(self.x.sources[c["source"]], c["state"], self.x) for c in self.d["sun"]["presence"]]
        schedule = self.d["schedule"]
        if schedule["enabled"]:
            out += [C.t_time(self.x.time(self.key("window", i, "after"), win["after"], f"{self.d['name']}: shading from",
                                         "Start of a time window.")) for i, win in enumerate(schedule["windows"]) if win["after"]]
        return out

    def outside(self, cover: str, w: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """``(the condition "clearly no shading now", the triggers of reaching that)``, with the hysteresis."""
        sun = self.d["sun"]
        h = float(plain(sun["hysteresis"]) or 0)
        start, end, wraps = self.sector(cover, w, margin=h) if h else self.sector(cover, w)
        low, high = self.elevation(cover, w)
        low_h = low if not h else _round(float(plain(low)) - h)
        high_h = high if (high is None or not h) else _round(float(plain(high)) + h)
        options: list[dict[str, Any]] = [
            C.c_numeric(SUN, above=end, below=start, attribute="azimuth") if wraps else
            C.any_of([C.c_numeric(SUN, below=start, attribute="azimuth"), C.c_numeric(SUN, above=end, attribute="azimuth")]),
            C.c_numeric(SUN, below=low_h, attribute="elevation")]
        triggers = [C.t_numeric(SUN, above=end, attribute="azimuth"), C.t_numeric(SUN, below=start, attribute="azimuth"),
                    C.t_numeric(SUN, below=low_h, attribute="elevation")]
        if high is not None:
            options.append(C.c_numeric(SUN, above=high_h, attribute="elevation"))
            triggers.append(C.t_numeric(SUN, above=high_h, attribute="elevation"))
        for which in ("lux", "temperature"):
            s = self.sensor(which)
            if s and s["above"] is not None:
                threshold = self.threshold(which)
                hs = plain(s.get("hysteresis")) or 0
                below = threshold if not hs else _round(float(plain(threshold)) - float(hs))
                options.append(C.c_numeric(self.sensor_entity(which), below=below))
                triggers.append(C.t_numeric(self.sensor_entity(which), below=below))
        weather = self.sensor("weather")
        if weather and weather["states"]:
            entity = self.sensor_entity("weather")
            options.append(C.group("and", [*(C.c_state(entity, s, negate=True) for s in weather["states"]),
                                           C.c_state(entity, "unavailable", negate=True), C.c_state(entity, "unknown", negate=True)]))
            triggers += [C.t_state(entity, frm=s) for s in weather["states"]]
        columns = sun["presence"]
        if columns:
            alive = [P.active(self.x.sources[c["source"]], c["state"], self.x) for c in columns]
            options.append(C.group("not", [C.group("and", a) if len(a) > 1 else a[0] for a in alive]))
            triggers += [P.ended(self.x.sources[c["source"]], c["state"], self.x) for c in columns]
        windows = self.windows()
        if windows:
            options.append(C.group("not", windows))
            triggers += [C.t_time(self.x.time(self.key("window", i, "before"), win["before"], f"{self.d['name']}: shading until",
                                              "End of a time window.")) for i, win in enumerate(self.d["schedule"]["windows"]) if win["before"]]
        return [C.any_of(options)], triggers

    def mode_entity(self) -> Any:
        modes = self.d["modes"]
        if modes["driver"] == "selector":
            return self.x.e(modes["entity_id"], "The cover-mode selector", group="Trigger entities")
        return self.x.e(modes["mood_entity"], "The household mood selector", group="Trigger entities")

    def mode_value(self, value: str, what: str) -> Any:
        return self.x.text(self.key(what, value), value, f"{self.d['name']}: {what} “{value}”",
                           f"The state that stands for “{value}”.")

    def mode_active(self, mode: str) -> list[dict[str, Any]] | None:
        modes = self.d["modes"]
        if modes["driver"] == "selector":
            return [C.c_state(self.mode_entity(), self.mode_value(mode, "mode"))]
        if modes["driver"] == "mood":
            values = [v for v, m in modes["mood_map"].items() if m == mode]
            if not values:
                return None
            return [C.any_of([C.c_state(self.mode_entity(), self.mode_value(v, "mood")) for v in values])]
        return None

    def mode_triggers(self, mode: str) -> list[dict[str, Any]]:
        modes = self.d["modes"]
        if modes["driver"] == "selector":
            return [C.t_state(self.mode_entity(), to=self.mode_value(mode, "mode"))]
        if modes["driver"] == "mood":
            return [C.t_state(self.mode_entity(), to=self.mode_value(v, "mood")) for v, m in modes["mood_map"].items() if m == mode]
        return []

    def mode_cell(self, cover: str, mode: str) -> dict[str, Any]:
        m = self.d["modes"]
        return C.resolve((m["cells"].get(cover) or {}).get(mode), m["row_defaults"].get(cover), m["default"])

    def policy_modes(self, cover: str, choice: str) -> list[str]:
        """The modes with an acting cell for this cover whose sun policy is ``choice``."""
        m = self.d["modes"]
        if not m["enabled"] or not m["driver"]:
            return []
        return [mode for mode in m["names"] if C.acting(self.mode_cell(cover, mode)) is not None
                and m["sun_policy"].get(mode) == choice]

    def not_modes(self, modes: list[str]) -> list[dict[str, Any]]:
        out = []
        for mode in modes:
            active = self.mode_active(mode)
            if active:
                out.append(C.negation(active))
        return out

    def safety(self, cover: str, cell_opens: bool) -> list[dict[str, Any]]:
        if cover not in self.sensitive or not cell_opens:
            return []
        s = self.d["sun"]["safety"]
        out = []
        if s["wind_entity"] and s["wind_above"] is not None:
            wind = self.x.e(s["wind_entity"], "Wind sensor (safety)", group="Sensor entities")
            limit = self.x.number(self.key("wind_above"), s["wind_above"], f"{self.d['name']}: wind limit",
                                  "Above this wind speed the cover is retracted and not opened.")
            out.append(C.c_numeric(wind, below=limit))
        if s["rain_entity"]:
            out.append(C.c_state(self.x.e(s["rain_entity"], "Rain sensor (safety)", group="Sensor entities"), "on", negate=True))
        return out

    def guards(self, cover: str, *, interval: bool = False) -> list[dict[str, Any]]:
        override, out = self.d["override"], []
        if override["entity_id"]:
            out.append(C.c_state(self.x.e(override["entity_id"], "Manual override signal: while it is on, nothing is moved",
                                          group="Sensor entities"), "off"))
        target = self.x.e(cover, "Controlled cover")
        ref = self.refs.ref(target)
        if override["manual_minutes"]:
            minutes = self.x.number(self.key("manual_minutes"), override["manual_minutes"], f"{self.d['name']}: pause after a manual move",
                                    "Minutes without automatic movements after somebody moved a cover.", group="Delays and durations",
                                    selector={"number": {"min": 1, "max": 1440, "unit_of_measurement": "min", "mode": "box"}})
            text = (f"{{{{ states[{ref}] is none or states[{ref}].context.parent_id is not none or "
                    f"(as_timestamp(now()) - as_timestamp(states[{ref}].last_updated)) >= {self.refs.num(minutes)} * 60 }}}}")
            out.append(self.refs.template(text, lambda states, e=cover, m=minutes: L._not_manual(states, e, m)))  # noqa: SLF001
        if interval and self.d["sun"]["min_interval_minutes"]:
            minutes = self.x.number(self.key("min_interval"), self.d["sun"]["min_interval_minutes"],
                                    f"{self.d['name']}: minimum time between movements", "Minutes between two automatic movements.",
                                    group="Delays and durations", selector={"number": {"min": 1, "max": 1440, "unit_of_measurement": "min", "mode": "box"}})
            text = (f"{{{{ states[{ref}] is none or (as_timestamp(now()) - as_timestamp(states[{ref}].last_updated)) "
                    f">= {self.refs.num(minutes)} * 60 }}}}")
            out.append(self.refs.template(text, lambda states, e=cover, m=minutes: _interval_ok(states, e, m)))
        return out

    def emit(self, layer: str, cover: str, column: str | None, name_parts: list[Any], triggers: list[dict[str, Any]],
             conditions: list[dict[str, Any]], actions: list[dict[str, Any]], exclusive: dict[str, Any],
             cell: dict[str, Any] | None, resolution: dict[str, Any] | None = None) -> None:
        rule_id = C.generated_rule_id(self.did, layer, cover, column)
        meta = {"designer_id": self.did, "designer": KIND, "layer": layer, "target": cover, "column": column,
                "mood": None, "exclusive": exclusive, "cell": cell, "resolution": resolution or {}}
        self.x.add_rule(C.rule(rule_id, self.name(cover, *name_parts), triggers, conditions, actions, meta), self.did)
        self.view["rules"].append(rule_id)

    def cell_actions(self, cell: dict[str, Any], cover: str, key: str, name: str) -> list[dict[str, Any]]:
        return commands(cell, self.x.e(cover, "Controlled cover"), self.x.targets, self.x, key, name)

    # ------------------------------------------------------------ sun rules
    def sun_rules(self, cover: str) -> None:
        sun = self.d["sun"]
        w = sun["windows"].get(cover)
        if not sun["enabled"] or w is None:
            return
        missing = self.ready(cover)
        if missing:
            self.block("missing_input", f"“{self.d['name']}”: the sun shading of {cover} needs {missing}. Nothing is "
                       "assumed: the cover is not moved until it is given.", "sun")
            return
        if cover in self.sensitive and not ((sun["safety"]["wind_entity"] and sun["safety"]["wind_above"] is not None)
                                            or sun["safety"]["acknowledged"]):
            self.block("weather_safety", f"“{self.d['name']}”: {cover} is weather-sensitive (an awning or an exterior "
                       "cover). Sun shading alone does not protect it: choose a wind sensor and limit (and a rain sensor), "
                       "or confirm that it has its own independent weather protection.", "sun")
            return
        center = self.center(w)
        self.view["windows"][cover] = {"center": center, "sector_ha": solar.sector(center, float(w["left"]), float(w["right"]))}
        shading = self.shading(cover, w)
        policy = self.not_modes(self.policy_modes(cover, "mode_wins"))
        mode_triggers = []
        m = self.d["modes"]
        if m["enabled"] and m["driver"] and m["sun_policy"]:
            mode_triggers = [C.t_state(self.mode_entity())]
        actions, curve, guards = [], False, self.guards(cover, interval=True)
        name = self.name(cover, "sun")
        target = self.x.e(cover, "Controlled cover")
        for which, service, field in (("position", "cover.set_cover_position", "position"),
                                      ("tilt", "cover.set_cover_tilt_position", "tilt_position")):
            r = w[which]
            if r is None:
                continue
            if r["kind"] == "close":
                actions.append(C.call("cover.close_cover", [target], self.x.targets))
            elif r["kind"] == "fixed":
                value = self.x.v(self.key(cover, "sun", which), r["value"], kind="data", group="Cover positions",
                                 name=f"{name}: {which}", description=f"The {which} while the sun shines in.", selector=POSITION_SELECTOR)
                actions.append(C.call(service, [target], self.x.targets, {field: value}))
                if which == "position" and sun["min_change"]:
                    guards.append(self.min_change(cover, target, value, None, w))
            else:
                curve = True
                text = solar.response_template(r["basis"], r["points"], center)
                self.refs.evaluators[text] = lambda states, rr=r, c=center: _curve_value(states, rr, c)
                actions.append(C.call(service, [target], self.x.targets, {field: text}))
                if which == "position":
                    guards.append(self.min_change(cover, target, None, r, w))
        triggers = self.entering(cover, w) + mode_triggers
        if curve:
            triggers += [C.t_state(SUN, attribute="elevation"), C.t_state(SUN, attribute="azimuth")]
        conditions = shading + policy + self.safety(cover, True) + guards
        self.emit("sun", cover, "shade", ["sun"], triggers, conditions, actions, {f"{self.did}:{cover}:sun": "shade"}, None)
        # leaving the sun
        leave = w["leave"]
        if leave and leave["kind"] != "none":
            outside, leave_triggers = self.outside(cover, w)
            conditions = outside + self.not_modes(self.policy_modes(cover, "mode_wins") + self.policy_modes(cover, "sun_wins"))
            conditions += self.safety(cover, opens(leave)) + self.guards(cover)
            self.emit("sun", cover, "leave", ["sun leaves"], leave_triggers, conditions,
                      self.cell_actions(leave, cover, self.key(cover, "leave"), self.name(cover, "sun leaves")),
                      {f"{self.did}:{cover}:sun": "leave"}, leave)
        fallback = sun["fallback"]
        if fallback and fallback["kind"] != "none":
            triggers = [C.t_state(SUN, to="unavailable")]
            reasons = [C.c_state(SUN, "unavailable")]
            for which in ("lux", "temperature", "weather"):
                if self.sensor(which):
                    entity = self.sensor_entity(which)
                    triggers += [C.t_state(entity, to="unavailable"), C.t_state(entity, to="unknown")]
                    reasons += [C.c_state(entity, "unavailable"), C.c_state(entity, "unknown")]
            conditions = [C.any_of(reasons), *self.not_modes(self.policy_modes(cover, "mode_wins")),
                          *self.safety(cover, opens(fallback)), *self.guards(cover)]
            self.emit("sun", cover, "fallback", ["sun data unavailable"], triggers, conditions,
                      self.cell_actions(fallback, cover, self.key(cover, "fallback"), self.name(cover, "fallback")),
                      {f"{self.did}:{cover}:sun": "fallback"}, fallback)
        if cover in self.sensitive and sun["safety"]["wind_entity"] and sun["safety"]["wind_above"] is not None:
            s = sun["safety"]
            wind = self.x.e(s["wind_entity"], "Wind sensor (safety)", group="Sensor entities")
            limit = self.x.number(self.key("wind_above"), s["wind_above"], f"{self.d['name']}: wind limit",
                                  "Above this wind speed the cover is retracted and not opened.")
            triggers = [C.t_numeric(wind, above=limit)]
            if s["rain_entity"]:
                triggers.append(C.t_state(self.x.e(s["rain_entity"], "Rain sensor (safety)", group="Sensor entities"), to="on"))
            self.emit("safety", cover, "retract", ["weather protection"], triggers, [],
                      [C.call("cover.close_cover", [target], self.x.targets)], {}, {"kind": "close"})

    def min_change(self, cover: str, target: Any, fixed: Any, curve: dict[str, Any] | None, w: dict[str, Any]) -> dict[str, Any]:
        sun = self.d["sun"]
        minimum = self.x.number(self.key("min_change"), sun["min_change"], f"{self.d['name']}: minimum change",
                                "The smallest change of position the sun shading makes (percent).", group="Cover positions",
                                selector={"number": {"min": 1, "max": 100, "unit_of_measurement": "%", "mode": "box"}})
        ref = self.refs.ref(target)
        if curve is None:
            wanted = self.refs.num(fixed)
            text = f"{{{{ ((state_attr({ref}, 'current_position') | int(-1000)) - {wanted}) | abs >= {self.refs.num(minimum)} }}}}"
            return self.refs.template(text, lambda states, e=cover, v=fixed, m=minimum: _changes(states, e, plain(v), m))
        body = solar.response_template(curve["basis"], curve["points"], self.center(w))
        head, _, _tail = body.rpartition("{{")
        text = (head + "{{ ((state_attr(" + ref + ", 'current_position') | int(-1000)) - "
                "(([0, [100, v] | min] | max) | round(0) | int)) | abs >= " + self.refs.num(minimum) + " }}")
        return self.refs.template(text, lambda states, e=cover, r=curve, c=self.center(w), m=minimum:
                                  _changes(states, e, _curve_value(states, r, c), m))

    # ------------------------------------------------------------ modes and timed entries
    def mode_rules(self, cover: str) -> None:
        m = self.d["modes"]
        if not m["enabled"]:
            return
        sun = self.d["sun"]
        w = sun["windows"].get(cover) if sun["enabled"] else None
        for mode in m["names"]:
            res = self.mode_cell(cover, mode)
            if res["state"] == "missing_default":
                self.block("missing_default", f"“{self.d['name']}”: {cover} / {mode} says “Use default”, but no default is "
                           "configured.", "modes")
                continue
            cell = C.acting(res)
            if cell is None:
                continue
            if not m["driver"]:
                continue  # applied by timed entries only
            active = self.mode_active(mode)
            if active is None:
                self.info("mode_unmapped", f"“{self.d['name']}”: no mood is mapped to the mode “{mode}”, so it is used only "
                          "by timed entries.", "modes")
                continue
            triggers = self.mode_triggers(mode)
            conditions = list(active)
            if w is not None and self.ready(cover) is None:
                policy = m["sun_policy"].get(mode)
                if policy is None:
                    self.block("no_sun_policy", f"“{self.d['name']}”: while “{mode}” is active, both the mode and the sun "
                               f"shading want to move {cover}. Choose which one wins (nothing is decided for you).", "modes")
                    continue
                if policy == "sun_wins":
                    conditions.append(C.negation(self.shading(cover, w)))
                    triggers += self.outside(cover, w)[1]
            conditions += self.safety(cover, opens(cell)) + self.guards(cover)
            self.emit("mode", cover, mode, [mode], triggers, conditions,
                      self.cell_actions(cell, cover, self.key("mode", cover, mode), self.name(cover, mode)),
                      {f"{self.did}:mode": mode}, cell, {k: v for k, v in res.items() if k != "cell"})

    def entry_rules(self, cover: str) -> None:
        schedule = self.d["schedule"]
        if not schedule["enabled"]:
            return
        for entry in schedule["entries"]:
            if entry.get("mode"):
                res = self.mode_cell(cover, entry["mode"])
                cell = C.acting(res)
            else:
                cell = entry["cells"].get(cover)
                cell = None if cell is None or cell["kind"] == "none" else cell
                res = {"state": "explicit", "source": "cell"} if cell else {}
            if cell is None:
                continue
            at = self.x.time(self.key(entry["id"], "at"), entry["at"], f"{self.d['name']}: {entry['name'] or 'timed entry'} at",
                             "The time of a timed entry.")
            label_ = entry["name"] or f"at {str(plain(entry['at']))[:5]}"
            self.emit("schedule", cover, entry["id"], [label_], [C.t_time(at, entry["weekdays"])],
                      self.safety(cover, opens(cell)) + self.guards(cover),
                      self.cell_actions(cell, cover, self.key(entry["id"], cover), self.name(cover, label_)), {}, cell,
                      {k: v for k, v in res.items() if k != "cell"})

    def run(self) -> None:
        d = self.d
        if not d["covers"]:
            self.block("no_covers", f"“{d['name']}”: choose the covers it controls.")
            return
        self.sensitive = {c for c in d["covers"] if (d["sun"]["windows"].get(c) or {}).get("weather_sensitive")}
        m = d["modes"]
        if m["enabled"]:
            if m["driver"] == "selector" and not m["entity_id"]:
                self.block("missing_source", f"“{d['name']}”: choose the cover-mode selector, or let the modes follow the "
                           "household mood instead. Nothing is chosen for you.", "modes")
            if m["driver"] == "mood" and not m["mood_entity"]:
                self.block("missing_source", f"“{d['name']}”: choose the household mood selector for the mood mapping.", "modes")
        sun = d["sun"]
        if sun["enabled"]:
            for which in ("lux", "temperature", "weather"):
                s = sun[which]
                if s and not s["entity_id"]:
                    self.block("missing_source", f"“{d['name']}”: a {which} condition is set up without its entity.", "sun")
                if s and which != "weather" and s["entity_id"] and s["above"] is None:
                    self.block("missing_threshold", f"“{d['name']}”: give the {which} above which the sun shading acts.", "sun")
            for column in sun["presence"]:
                source = self.x.sources.get(column["source"])
                missing = P.missing(source, column["state"]) if source else "a presence source no longer exists"
                if missing:
                    self.block("missing_source", f"“{d['name']}”: {missing}.", "sun")
            if not sun["windows"]:
                self.block("no_windows", f"“{d['name']}”: sun shading is on, but no cover has a window set up.", "sun")
        if not self.ok:
            return
        for cover in d["covers"]:
            self.sun_rules(cover)
            self.mode_rules(cover)
            self.entry_rules(cover)


def _round(value: float) -> float | int:
    value = round(float(value), 2)
    return int(value) if value.is_integer() else value


def _interval_ok(states: dict[str, Any], entity_id: str, minutes: Any) -> bool:
    state = states.get(entity_id)
    return state is None or state.get("age") is None or state["age"] >= plain(minutes) * 60


def _sun_values(states: dict[str, Any]) -> tuple[float, float]:
    attributes = (states.get(SUN) or {}).get("attributes") or {}
    try:
        return float(attributes.get("elevation", 0)), float(attributes.get("azimuth", 0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def _curve_value(states: dict[str, Any], curve: dict[str, Any], center: float) -> int:
    elevation, azimuth = _sun_values(states)
    value = solar.response(curve["basis"], curve["points"], elevation, azimuth, center)
    if value is None:  # the template clamps instead (only outside the sector, where the rule does not run)
        value = solar.interpolate(90.0, curve["points"])
    return value


def _changes(states: dict[str, Any], entity_id: str, wanted: Any, minimum: Any) -> bool:
    current = ((states.get(entity_id) or {}).get("attributes") or {}).get("current_position")
    try:
        current = int(current)
    except (TypeError, ValueError):
        current = -1000
    return abs(current - int(wanted)) >= plain(minimum)


def expand(designer: dict[str, Any], x: Any) -> dict[str, Any]:
    gen = _Gen(designer, x)
    if designer["enabled"]:
        gen.run()
    return {**gen.view, "resolved": resolved_views(designer, x)}


def resolved_views(d: dict[str, Any], x: Any) -> dict[str, Any]:
    m = d["modes"]
    return {"modes": {cover: {mode: L._cell_view(C.resolve((m["cells"].get(cover) or {}).get(mode), m["row_defaults"].get(cover),  # noqa: SLF001
                                                            m["default"]), (m["cells"].get(cover) or {}).get(mode), label_fn=resolved_label)
                              for mode in m["names"]} for cover in d["covers"]}}


def capability_problems(d: dict[str, Any], caps: dict[str, dict[str, Any]], x: Any) -> None:
    for cover in d["covers"]:
        cap = caps.get(cover)
        if not cap or cap.get("unknown"):
            continue
        cells = [c for c in (d["modes"]["cells"].get(cover) or {}).values()]
        cells += [c for c in [d["modes"]["row_defaults"].get(cover), d["modes"]["default"], d["sun"]["fallback"]] if c]
        cells += [e["cells"][cover] for e in d["schedule"]["entries"] if cover in e["cells"]]
        w = d["sun"]["windows"].get(cover)
        if w:
            if w["leave"]:
                cells.append(w["leave"])
            for which in ("position", "tilt"):
                r = w[which]
                if r and r["kind"] in ("fixed", "curve"):
                    cells.append({"kind": "position", "position": 1 if which == "position" else None, "tilt": 1 if which == "tilt" else None})
                if r and r["kind"] == "close":
                    cells.append({"kind": "close"})
            curve = w["position"] and w["position"]["kind"] in ("curve", "fixed")
            if curve and d["sun"]["enabled"] and not cap.get("reports_position") and not d["sun"]["min_interval_minutes"]:
                x.problem("blocking", "capability", f"“{d['name']}”: {cap['name']} does not report its position, so the "
                          "minimum change cannot be checked: set a minimum time between movements.", d["id"], "sun")
        if cap.get("weather_sensitive") and w and not w["weather_sensitive"] and d["sun"]["enabled"]:
            x.problem("blocking", "weather_safety", f"“{d['name']}”: Home Assistant reports {cap['name']} as an awning. Mark it "
                      "weather-sensitive (Sun Position) so that it is never extended without weather protection.", d["id"], "sun")
        wanted = set().union(*(needs(c) for c in cells)) if cells else set()
        missing = sorted(w_ for w_ in wanted if not cap.get(w_))
        if missing:
            words = {"open": "open", "close": "close", "position": "move to a position", "tilt": "tilt"}
            x.problem("blocking", "capability", f"“{d['name']}”: {cap['name']} cannot " + ", ".join(words[m] for m in missing)
                      + " (Home Assistant reports what it supports). Change the settings that ask for it.", d["id"], None)


def index_problems(d: dict[str, Any], index: Any, x: Any) -> None:
    if d["sun"]["enabled"] and d["sun"]["windows"] and SUN not in index.states:
        x.problem("blocking", "missing_source", f"“{d['name']}”: Home Assistant has no sun.sun entity (the Sun integration "
                  "is not set up), so the sun position is not known.", d["id"], "sun")
