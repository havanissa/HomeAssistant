"""The Light Level Manager: light-level templates, default matrices and one light level per room.

Configuration (``config["light_levels"]``, absent unless configured)::

    templates         {id: {name, version, bands[], hysteresis, darker_seconds, brighter_seconds, unavailable_seconds}}
    matrices          {id: {name, version, template, layer: truth|brightness, cells {band id: cell}}}
    default_template  the template a new room starts with (optional)
    default_matrices  {truth, brightness}: the matrices a new room starts with (optional)
    rooms             {area id: {device_id, version, template, source, overrides, matrices, matrix_overrides, ambient}}

A **band** is a named range of illuminance with a stable id (``dark``): the id never changes when the band is renamed,
so matrices and room overrides keep pointing at the same band. Bands are ordered from dark to bright and each one but
the last has an upper limit (``below``, exclusive); band *i* is ``[below of i-1, below of i)``. There are no gaps and no
overlaps by construction, and the limits must increase.

**Inheritance**: template (global) → room → light. A room inherits every value of its template it does not override
(band limits, hysteresis, the stabilisation delays, what happens without a reading); an override is kept when the
template changes. The matrices: the global default matrix the room adopted → the room's own cells → the cells a light
sets in its Lighting Designer. Nothing is inherited before the room explicitly adopts a template or a matrix, and a
value unset at every level stays unset.

**One light level per room, in Home Assistant**: each configured room is deployed as a small managed device (its
"vehicle", ``Light Level · <room>``) whose automation keeps the room's current band in a text helper the App creates
(``input_text.ab_<device>_<room>_light_level``). Every light that uses the room's light level reads that helper (a
state column), so all lights of a room agree on the band, and the band changes only

* when the reading has been past a limit **plus the hysteresis** for the **stabilisation delay** (separately for
  brighter and darker), so a lamp that lifts the reading a little, a passing cloud or a noisy sensor changes nothing;
* without a reading for ``unavailable_seconds`` (when configured): the band becomes ``no_reading`` and no matrix column
  holds (the lights are left as they are); unset = the last band is kept.

**Ambient estimate** (optional, per room) against the lamps' own light in the measurement:

``reference``   another, deliberately chosen sensor that the room's lamps do not reach (window, outdoor), times a factor.
``off_sample``  the reading is sampled into a number helper while all chosen lights have been off for the settle
                time; with a lamp on, the last sample is used until it is ``max_age_minutes`` old (then: no reading).
``calibrated``  reading minus each chosen light's contribution (lux at full brightness × its brightness), clamped at 0.
                Contributions are entered or taken over from observations of real switching (the App watches when a
                light changes and the reading follows; it never switches a light itself).

No method can tell daylight from lamplight in one ordinary reading: the result is labelled *estimated*, with how
fresh or how well calibrated it is, and without enough data it is *no reading*, never a guess.

Everything is native: template triggers with ``for`` (stabilisation), ``input_text.set_value`` and
``input_number.set_value`` on the App's own helpers. The App need not run.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import statistics
import time
from typing import Any

from .. import model
from ..compiler import plain, quote
from ..model import Problems
from . import common as C

_LOGGER = logging.getLogger(__name__)

KIND = "light_level"
VERSION = 1
ROWS = "rooms"
DEFAULT_NAME = "Light Level"

TEMPLATE_ID = re.compile(r"^t_[0-9a-f]{8}$")
MATRIX_ID = re.compile(r"^x_[0-9a-f]{8}$")
BAND_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
AREA_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
NO_READING = "no_reading"
RESERVED = {NO_READING, "unknown", "unavailable", "none", "off", "on"}
LAYERS = ("truth", "brightness")
METHODS = ("none", "reference", "off_sample", "calibrated")
METHOD_NAMES = {"none": "the sensor as it reads", "reference": "a reference sensor",
                "off_sample": "sampled while the lights are off",
                "calibrated": "reading minus the lamps' calibrated contribution"}
SOURCE_DOMAINS = ("sensor", "input_number", "number")
SETTINGS = ("hysteresis", "darker_seconds", "brighter_seconds", "unavailable_seconds")
SETTING_NAMES = {"hysteresis": "Hysteresis", "darker_seconds": "Stabilisation, darker", "brighter_seconds":
                 "Stabilisation, brighter", "unavailable_seconds": "Without a reading for"}
MIN_BANDS, MAX_BANDS = 2, 8
MAX_TEMPLATES = 12
MAX_MATRICES = 24
MAX_ROOMS = 60
MAX_AMBIENT_LIGHTS = 8
MAX_LUX = 200000
RECONCILE_MINUTES = 5
HELPER_ICON = "mdi:brightness-6"


# ================================================================== validation
def _lux(raw: Any, path: str, p: Problems, *, low: float = 0) -> Any:
    return C.number(raw, path, p, low, MAX_LUX)


def _bands(raw: Any, path: str, p: Problems) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else []
    if not MIN_BANDS <= len(items) <= MAX_BANDS:
        p.add(path, f"A template has {MIN_BANDS} to {MAX_BANDS} light levels")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(items[:MAX_BANDS]):
        item = item if isinstance(item, dict) else {}
        here = f"{path}[{i}]"
        band_id = item.get("id")
        if not isinstance(band_id, str) or not BAND_ID.match(band_id) or band_id in RESERVED:
            p.add(f"{here}.id", "A light level's id is lower case letters, digits and _ (not a reserved word)")
            band_id = f"band_{i}"
        if band_id in seen:
            p.add(f"{here}.id", "Two light levels share an id")
        seen.add(band_id)
        last = i == len(items[:MAX_BANDS]) - 1
        below = None if last else _lux(item.get("below"), f"{here}.below", p, low=0.1)
        if not last and below is None:
            p.add(f"{here}.below", "Give the upper limit of this light level (lux)")
        if last and item.get("below") not in (None, ""):
            p.add(f"{here}.below", "The brightest light level has no upper limit")
        out.append({"id": band_id, "name": C.text(item.get("name"), f"{here}.name", p, limit=40, required=True),
                    "below": below})
    limits = [b["below"] for b in out[:-1]]
    if all(v is not None for v in limits) and any(b <= a for a, b in zip(limits, limits[1:])):
        p.add(path, "The limits must increase from dark to bright (no gaps, no overlaps)")
    return out


def _settings(raw: dict[str, Any], path: str, p: Problems, only_present: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in SETTINGS:
        if only_present and key not in raw:
            continue
        value = raw.get(key)
        if key == "hysteresis":
            out[key] = _lux(value, f"{path}.{key}", p)
        elif key == "unavailable_seconds":
            out[key] = None if value in (None, "") else C.seconds(value, f"{path}.{key}", p, 86400)
        else:
            out[key] = None if value in (None, "") else C.seconds(value, f"{path}.{key}", p, 3600)
    return out


def _version(raw: Any) -> int:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 10**6 else 1


def validate_template(template_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"id": template_id, "name": C.text(raw.get("name"), f"{path}.name", p, limit=60, required=True),
            "version": _version(raw.get("version")), "bands": _bands(raw.get("bands"), f"{path}.bands", p),
            **_settings(raw, path, p)}


def _cell(layer: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    from . import lighting2 as L2
    if layer == "truth":
        return L2.truth_cell(raw, path, p, ("on", "off", "none"))
    return L2.bright_cell(raw, path, p, ("set", "none"))


def _cells(layer: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    out = {}
    for band, cell in (raw if isinstance(raw, dict) else {}).items():
        if not isinstance(band, str) or not BAND_ID.match(band):
            p.add(f"{path}.{band}", "Not a light level")
        elif cell is not None:
            out[band] = _cell(layer, cell, f"{path}.{band}", p)
    return {k: out[k] for k in sorted(out)}


def validate_matrix(matrix_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    layer = raw.get("layer") if raw.get("layer") in LAYERS else None
    if layer is None:
        p.add(f"{path}.layer", "A default matrix is a Truth Matrix or a Brightness Matrix")
        layer = "truth"
    template = raw.get("template") if isinstance(raw.get("template"), str) and TEMPLATE_ID.match(raw["template"]) else None
    if template is None:
        p.add(f"{path}.template", "Choose the light-level template whose levels this matrix uses")
    return {"id": matrix_id, "name": C.text(raw.get("name"), f"{path}.name", p, limit=60, required=True),
            "version": _version(raw.get("version")), "layer": layer, "template": template,
            "cells": _cells(layer, raw.get("cells"), f"{path}.cells", p)}


def _source(raw: Any, path: str, p: Problems, what: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or raw.get("entity_id") in (None, ""):
        return None
    return {"entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, SOURCE_DOMAINS, what),
            "attribute": model._attribute(raw.get("attribute"), f"{path}.attribute", p)}  # noqa: SLF001


def _ambient(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    method = raw.get("method") if raw.get("method") in METHODS else "none"
    lights = []
    for i, light in enumerate((raw.get("lights") if isinstance(raw.get("lights"), list) else [])[:MAX_AMBIENT_LIGHTS]):
        entity_id = C.entity(light, f"{path}.lights[{i}]", p, ("light",), "a light")
        if entity_id and entity_id not in lights:
            lights.append(entity_id)
    contributions = {}
    for light, value in (raw.get("contributions") if isinstance(raw.get("contributions"), dict) else {}).items():
        if light in lights:
            found = _lux(value, f"{path}.contributions.{light}", p)
            if found is not None:
                contributions[light] = found
    return {"method": method,
            "reference": _source(raw.get("reference"), f"{path}.reference", p, "a reference sensor (lux)"),
            "factor": C.number(raw.get("factor"), f"{path}.factor", p, 0.001, 1000),
            "lights": lights,
            "settle_seconds": C.seconds(raw.get("settle_seconds"), f"{path}.settle_seconds", p, 600) or None,
            "max_age_minutes": C.number(raw.get("max_age_minutes"), f"{path}.max_age_minutes", p, 1, 1440, integer=True),
            "contributions": {k: contributions[k] for k in sorted(contributions)}}


def validate_room(area_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    device_id = raw.get("device_id") if isinstance(raw.get("device_id"), str) and model.DEVICE_ID.match(raw["device_id"]) else None
    if device_id is None:
        p.add(f"{path}.device_id", "Internal: the room's light level has no device")
    template = raw.get("template") if isinstance(raw.get("template"), str) and TEMPLATE_ID.match(raw["template"]) else None
    over = raw.get("overrides") if isinstance(raw.get("overrides"), dict) else {}
    bands = {}
    for band, value in (over.get("bands") if isinstance(over.get("bands"), dict) else {}).items():
        if isinstance(band, str) and BAND_ID.match(band) and isinstance(value, dict) and "below" in value:
            below = _lux(value.get("below"), f"{path}.overrides.bands.{band}.below", p, low=0.1)
            if below is not None:
                bands[band] = {"below": below}
    overrides = {"bands": {k: bands[k] for k in sorted(bands)}, **_settings(over, f"{path}.overrides", p, only_present=True)}
    matrices_raw = raw.get("matrices") if isinstance(raw.get("matrices"), dict) else {}
    mo_raw = raw.get("matrix_overrides") if isinstance(raw.get("matrix_overrides"), dict) else {}
    return {"area_id": area_id, "device_id": device_id, "version": _version(raw.get("version")), "template": template,
            "source": _source(raw.get("source"), f"{path}.source", p, "a sensor that reports lux"),
            "overrides": overrides,
            "matrices": {layer: matrices_raw.get(layer) if isinstance(matrices_raw.get(layer), str)
                         and MATRIX_ID.match(matrices_raw[layer]) else None for layer in LAYERS},
            "matrix_overrides": {layer: _cells(layer, mo_raw.get(layer), f"{path}.matrix_overrides.{layer}", p) for layer in LAYERS},
            "ambient": _ambient(raw.get("ambient"), f"{path}.ambient", p)}


def validate_all(raw: Any, p: Problems) -> dict[str, Any] | None:
    """``config["light_levels"]``: the canonical document, or None when nothing is configured."""
    if not isinstance(raw, dict) or not raw:
        return None
    path = "light_levels"
    templates_raw = raw.get("templates") if isinstance(raw.get("templates"), dict) else {}
    matrices_raw = raw.get("matrices") if isinstance(raw.get("matrices"), dict) else {}
    rooms_raw = raw.get("rooms") if isinstance(raw.get("rooms"), dict) else {}
    for what, items, limit in (("templates", templates_raw, MAX_TEMPLATES), ("matrices", matrices_raw, MAX_MATRICES),
                               ("rooms", rooms_raw, MAX_ROOMS)):
        if len(items) > limit:
            p.add(f"{path}.{what}", f"At most {limit} {what}")
    templates = {}
    for tid in sorted(templates_raw)[:MAX_TEMPLATES]:
        if not TEMPLATE_ID.match(str(tid)):
            p.add(f"{path}.templates.{tid}", "Invalid template id")
            continue
        templates[tid] = validate_template(tid, templates_raw[tid], f"{path}.templates.{tid}", p)
    matrices = {}
    for mid in sorted(matrices_raw)[:MAX_MATRICES]:
        if not MATRIX_ID.match(str(mid)):
            p.add(f"{path}.matrices.{mid}", "Invalid matrix id")
            continue
        found = validate_matrix(mid, matrices_raw[mid], f"{path}.matrices.{mid}", p)
        if found["template"] and found["template"] not in templates:
            p.add(f"{path}.matrices.{mid}.template", "This template does not exist")
        matrices[mid] = found
    rooms = {}
    devices = set()
    for area_id in sorted(rooms_raw)[:MAX_ROOMS]:
        if not isinstance(area_id, str) or not AREA_ID.match(area_id):
            p.add(f"{path}.rooms.{area_id}", "Invalid room")
            continue
        room = validate_room(area_id, rooms_raw[area_id], f"{path}.rooms.{area_id}", p)
        if room["template"] and room["template"] not in templates:
            p.add(f"{path}.rooms.{area_id}.template", "This template does not exist")
        for layer in LAYERS:
            mid = room["matrices"][layer]
            if mid and (mid not in matrices or matrices[mid]["layer"] != layer):
                p.add(f"{path}.rooms.{area_id}.matrices.{layer}", f"Choose a {layer} matrix that exists")
        if room["device_id"] in devices:
            p.add(f"{path}.rooms.{area_id}.device_id", "Two rooms share a device")
        devices.add(room["device_id"])
        rooms[area_id] = room
    defaults_raw = raw.get("default_matrices") if isinstance(raw.get("default_matrices"), dict) else {}
    default_matrices = {}
    for layer in LAYERS:
        mid = defaults_raw.get(layer)
        if mid in (None, ""):
            default_matrices[layer] = None
        elif mid not in matrices or matrices[mid]["layer"] != layer:
            p.add(f"{path}.default_matrices.{layer}", f"The standard {layer} matrix does not exist")
            default_matrices[layer] = None
        else:
            default_matrices[layer] = mid
    default_template = raw.get("default_template") if raw.get("default_template") in templates else None
    if raw.get("default_template") not in (None, "") and default_template is None:
        p.add(f"{path}.default_template", "The standard template does not exist")
    if not (templates or matrices or rooms):
        return None
    return {"templates": templates, "matrices": matrices, "default_template": default_template,
            "default_matrices": default_matrices, "rooms": rooms}


def check_references(config: dict[str, Any], p: Problems) -> None:
    """Each room's vehicle device exists and holds this room's light level designer; a Lighting Designer's light-level
    matrix names a configured room."""
    levels = config.get("light_levels") or {}
    rooms = levels.get("rooms") or {}
    for area_id, room in rooms.items():
        device = config["devices"].get(room["device_id"])
        if device is None or not any(d.get("kind") == KIND and d.get("room") == area_id for d in device.get("designers") or []):
            p.add(f"light_levels.rooms.{area_id}.device_id", "Internal: the device of this room's light level is missing")
    for device_id, device in config["devices"].items():
        for d in device.get("designers") or []:
            if d.get("kind") == KIND and d.get("room") not in rooms:
                p.add(f"devices.{device_id}.designers", "This light level device belongs to a room without a light level")
            for room in referenced_rooms(d):
                if room not in rooms:
                    p.add(f"devices.{device_id}.designers", f"A matrix uses the light level of a room that is not configured: {room}")


def referenced_rooms(designer: dict[str, Any]) -> set[str]:
    """The rooms whose light level a Lighting Designer (version 2) reads."""
    if designer.get("kind") != "lighting" or designer.get("version") != 2:
        return set()
    return {m["level"]["room"] for layer in LAYERS for m in (designer.get(layer) or {}).get("matrices") or []
            if isinstance(m.get("level"), dict) and m["level"].get("room")}


# ================================================================== the light level designer (the room's vehicle)
def validate(raw: dict[str, Any], designer_id: str, targets: list[str], path: str, p: Problems) -> dict[str, Any]:
    room = raw.get("room")
    if not isinstance(room, str) or not AREA_ID.match(room):
        p.add(f"{path}.room", "Internal: the room of this light level")
        room = "x"
    return {"id": designer_id, "kind": KIND, "version": VERSION, "name": C.text(raw.get("name"), f"{path}.name", p) or DEFAULT_NAME,
            "enabled": raw.get("enabled") is not False, "room": room}


def sources_of(_designer: dict[str, Any]) -> set[str]:
    return set()


def helper_ids(device_id: str, area_id: str) -> dict[str, Any]:
    """The helpers of a room's light level: the band (text) and, with ``off_sample``, the ambient sample (number)."""
    from . import lighting2 as L2
    dev = device_id.split("_", 1)[1]
    slug = L2._slug(area_id)  # noqa: SLF001
    return {"band": f"input_text.ab_{dev}_{slug}_light_level", "band_item": f"ab_{dev}_{slug}_light_level",
            "band_name": f"AB {dev} {slug} light level",
            "ambient": f"input_number.ab_{dev}_{slug}_ambient_lux", "ambient_item": f"ab_{dev}_{slug}_ambient_lux",
            "ambient_name": f"AB {dev} {slug} ambient lux"}


def device_targets(room: dict[str, Any]) -> list[str]:
    ids = helper_ids(room["device_id"], room["area_id"])
    return [ids["band"]] + ([ids["ambient"]] if room["ambient"]["method"] == "off_sample" else [])


def effective(room: dict[str, Any], template: dict[str, Any] | None) -> dict[str, Any]:
    """The room's values with where each comes from: ``room`` (an override), ``global`` (the template) or ``unset``."""
    if template is None:
        return {"template": None, "bands": [], "settings": {k: {"value": None, "source": "unset"} for k in SETTINGS},
                "problems": [], "orphans": sorted(room["overrides"]["bands"])}
    bands = []
    for b in template["bands"]:
        over = room["overrides"]["bands"].get(b["id"])
        if b["below"] is None:
            bands.append({**b, "global": None, "source": "none"})
        elif over is not None:
            bands.append({**b, "below": over["below"], "global": b["below"], "source": "room"})
        else:
            bands.append({**b, "global": b["below"], "source": "global"})
    settings = {}
    for key in SETTINGS:
        if key in room["overrides"]:
            settings[key] = {"value": room["overrides"][key], "global": template.get(key), "source": "room"}
        else:
            settings[key] = {"value": template.get(key), "global": template.get(key),
                             "source": "global" if template.get(key) is not None else "unset"}
    problems = []
    limits = [b["below"] for b in bands[:-1]]
    if any(b <= a for a, b in zip(limits, limits[1:])):
        problems.append("With the room's own limits the light levels no longer increase from dark to bright.")
    orphans = sorted(set(room["overrides"]["bands"]) - {b["id"] for b in template["bands"]})
    return {"template": template["id"], "template_version": template["version"], "bands": bands, "settings": settings,
            "problems": problems, "orphans": orphans}


def inherited_cell(levels: dict[str, Any], area_id: str, layer: str, band: str) -> tuple[dict[str, Any] | None, str | None]:
    """A light's inherited cell for a band: ``(cell, "room" | "global")`` or ``(None, None)``."""
    room = (levels.get("rooms") or {}).get(area_id)
    if room is None:
        return None, None
    own = room["matrix_overrides"][layer].get(band)
    if own is not None:
        return own, "room"
    matrix = (levels.get("matrices") or {}).get(room["matrices"][layer] or "")
    if matrix is not None and matrix["template"] == room["template"] and band in matrix["cells"]:
        return matrix["cells"][band], "global"
    return None, None


# ------------------------------------------------------------------ the generated templates (and their Python twins)
def _reading(src: dict[str, Any]) -> str:
    entity = quote(src["entity_id"])
    if src["attribute"]:
        return f"state_attr({entity}, {quote(src['attribute'])}) | float(none)"
    return f"states({entity}) | float(none)"


def value_jinja(room: dict[str, Any]) -> str:
    """A Jinja prefix that sets ``v``: the value the band follows (a number, or none without a reading)."""
    a = room["ambient"]
    method = a["method"]
    if method == "reference":
        factor = plain(a["factor"]) if a["factor"] is not None else 1
        return f"{{% set v = {_reading(a['reference'])} %}}{{% set v = v * {factor} if v is number else none %}}"
    if method == "off_sample":
        ids = helper_ids(room["device_id"], room["area_id"])
        age = int(a["max_age_minutes"]) * 60
        # a negative sample is "no sample yet" (the helper is created at -1: a new helper is never a 0 lx reading)
        return (f"{{% set s = states[{quote(ids['ambient'])}] %}}{{% set v = s.state | float(none) if s is not none and "
                f"(now() - s.last_reported).total_seconds() < {age} else none %}}{{% set v = v if v is number and v >= 0 else none %}}")
    raw = f"{{% set v = {_reading(room['source'])} %}}"
    if method == "calibrated":
        pairs = ", ".join(f"[{quote(light)}, {plain(a['contributions'][light])}]" for light in a["lights"])
        raw += ("{% if v is number %}{% set a = namespace(x=0) %}{% for e, k in [" + pairs + "] %}"
                "{% if is_state(e, 'on') %}{% set a.x = a.x + k * ((state_attr(e, 'brightness') or 255) | float(255)) / 255 %}"
                "{% endif %}{% endfor %}{% set v = [v - a.x, 0] | max %}{% endif %}")
    return raw


def band_jinja(room: dict[str, Any], eff: dict[str, Any]) -> str:
    """The prefix that sets ``i`` (band ids), ``c`` (the current band's index, -1 = none) and ``new`` (the band the
    value calls for, with hysteresis; -1 without a reading)."""
    ids = helper_ids(room["device_id"], room["area_id"])
    limits = ", ".join(f"{plain(b['below'])}" for b in eff["bands"][:-1])
    names = ", ".join(quote(b["id"]) for b in eff["bands"])
    h = plain(eff["settings"]["hysteresis"]["value"] or 0)
    return (value_jinja(room)
            + f"{{% set t = [{limits}] %}}{{% set i = [{names}] %}}{{% set h = states({quote(ids['band'])}) %}}"
            "{% set c = i.index(h) if h in i else -1 %}{% set n = namespace(r=0, u=0, d=0) %}"
            "{% if v is number %}{% for x in t %}{% set n.r = n.r + (1 if v >= x else 0) %}"
            f"{{% set n.u = n.u + (1 if v >= x + {h} else 0) %}}{{% set n.d = n.d + (1 if v >= x - {h} else 0) %}}"
            "{% endfor %}{% endif %}"
            "{% set new = -1 if v is not number else (n.r if c < 0 else (n.u if c < n.u else (n.d if c > n.d else c))) %}")


def band_index(value: Any, limits: list[float], hysteresis: float, current: int) -> int:
    """The Python twin of :func:`band_jinja`."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return -1
    h = hysteresis or 0
    raw = sum(1 for x in limits if value >= x)
    if current < 0:
        return raw
    up = sum(1 for x in limits if value >= x + h)
    down = sum(1 for x in limits if value >= x - h)
    if current < up:
        return up
    if current > down:
        return down
    return current


def _number(value: Any) -> float | None:
    try:
        found = float(value)
    except (TypeError, ValueError):
        return None
    return found if found == found and found not in (float("inf"), float("-inf")) else None


def _read(states: dict[str, Any], src: dict[str, Any] | None) -> float | None:
    if not src or not src.get("entity_id"):
        return None
    state = states.get(src["entity_id"]) or {}
    raw = (state.get("attributes") or {}).get(src["attribute"]) if src.get("attribute") else state.get("state")
    return _number(raw)


def reading(room: dict[str, Any], states: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """What the room's automation reads now (the Python twin of :func:`value_jinja`), with the parts and labels the
    interface shows: raw, estimated lamp contribution, value used, method, measured or estimated, freshness."""
    a = room["ambient"]
    method = a["method"]
    raw = _read(states, room["source"])
    out: dict[str, Any] = {"method": method, "method_text": METHOD_NAMES[method], "raw": raw, "artificial": None,
                           "value": None, "estimated": method != "none", "fresh": None, "confidence": None}
    if method == "none":
        out["value"] = raw
    elif method == "reference":
        ref = _read(states, a["reference"])
        out["reference"] = ref
        out["value"] = ref * (a["factor"] if a["factor"] is not None else 1) if ref is not None else None
        out["confidence"] = "as good as the reference sensor's placement"
    elif method == "off_sample":
        ids = helper_ids(room["device_id"], room["area_id"])
        sample = states.get(ids["ambient"]) or {}
        out["sample"] = _number(sample.get("state"))
        age = None
        stamp = sample.get("last_reported") or sample.get("last_updated")
        if stamp and now is not None:
            try:
                age = (now - datetime.fromisoformat(str(stamp))).total_seconds()
            except ValueError:
                age = None
        out["sample_age_seconds"] = age
        limit = (a["max_age_minutes"] or 0) * 60
        if out["sample"] is not None and out["sample"] < 0:
            out["sample"] = None   # created, never sampled yet
        out["fresh"] = out["sample"] is not None and age is not None and age < limit
        out["value"] = out["sample"] if out["fresh"] else None
        if raw is not None and out["sample"] is not None:
            out["artificial"] = max(raw - out["sample"], 0)
        out["confidence"] = ("fresh sample" if out["fresh"] else "no fresh sample (a light was on too long, or none yet)")
    elif method == "calibrated":
        lamps = 0.0
        for light in a["lights"]:
            state = states.get(light) or {}
            if state.get("state") == "on":
                brightness = _number((state.get("attributes") or {}).get("brightness")) or 255
                lamps += a["contributions"].get(light, 0) * brightness / 255
        out["artificial"] = lamps if raw is not None else None
        out["value"] = max(raw - lamps, 0) if raw is not None else None
        missing = [light for light in a["lights"] if light not in a["contributions"]]
        out["confidence"] = ("uncalibrated: " + ", ".join(missing)) if missing else "calibrated contributions"
    return out


# ------------------------------------------------------------------ expansion
def expand(designer: dict[str, Any], x: Any) -> dict[str, Any]:
    gen = _Gen(designer, x)
    if designer["enabled"]:
        gen.run()
    x.generators[designer["id"]] = gen
    return gen.view


def resolved_views(_designer: dict[str, Any], _x: Any) -> dict[str, Any]:
    return {}


def capability_problems(_d: dict[str, Any], _caps: dict[str, Any], _x: Any) -> None:
    return None


def index_problems(d: dict[str, Any], index: Any, x: Any) -> None:
    room = ((x.levels or {}).get("rooms") or {}).get(d["room"])
    if room is None:
        return
    src = room["ambient"]["reference"] if room["ambient"]["method"] == "reference" else room["source"]
    if src and src.get("entity_id") and not index.exists(src["entity_id"]):
        x.problem("warning", "missing_source", f"The light sensor {src['entity_id']} does not exist in Home Assistant.", d["id"])
    for light in room["ambient"]["lights"]:
        if not index.exists(light):
            x.problem("warning", "missing_light", f"{light} (ambient estimate) does not exist in Home Assistant.", d["id"])


class _Gen:
    def __init__(self, d: dict[str, Any], x: Any) -> None:
        self.d, self.x = d, x
        self.did = d["id"]
        self.view: dict[str, Any] = {"rules": [], "problems": [], "helpers": [], "version": VERSION}
        self.ok = True

    def block(self, code: str, message: str) -> None:
        self.x.problem("blocking", code, message, self.did)
        self.ok = False

    def warn(self, code: str, message: str) -> None:
        self.x.problem("warning", code, message, self.did)

    def add(self, key: str, name: str, triggers: list[dict[str, Any]], conditions: list[dict[str, Any]],
            actions: list[dict[str, Any]]) -> None:
        rule_id = C.generated_rule_id(self.did, "level", self.d["room"], key)
        meta = {"designer_id": self.did, "designer": KIND, "layer": "level", "target": None, "column": key, "mood": None,
                "exclusive": {}, "cell": None, "resolution": {}}
        self.x.add_rule(C.rule(rule_id, name, triggers, conditions, actions, meta), self.did)
        self.view["rules"].append(rule_id)

    def run(self) -> None:
        levels = self.x.levels or {}
        room = (levels.get("rooms") or {}).get(self.d["room"])
        if room is None:
            self.block("missing_room", "This room has no light level configuration (anymore).")
            return
        template = (levels.get("templates") or {}).get(room["template"] or "")
        ids = helper_ids(room["device_id"], room["area_id"])
        if sorted(self.x.device["targets"]) != sorted(device_targets(room)):
            self.block("internal", "Internal: the entities of this light level device are out of date. Save the room again.")
            return
        self.view["helpers"].append({"entity_id": ids["band"], "domain": "input_text", "item_id": ids["band_item"],
                                     "name": ids["band_name"], "light": room["area_id"],
                                     "purpose": "the room's current light level (a band id)",
                                     "params": {"min": 0, "max": 32, "mode": "text", "icon": HELPER_ICON}})
        a = room["ambient"]
        if a["method"] == "off_sample":
            self.view["helpers"].append({"entity_id": ids["ambient"], "domain": "input_number", "item_id": ids["ambient_item"],
                                         "name": ids["ambient_name"], "light": room["area_id"],
                                         "purpose": "the ambient light sampled while the lights were off (lux)",
                                         "params": {"min": -1, "max": MAX_LUX, "step": 1, "mode": "box", "initial": -1,
                                                    "unit_of_measurement": "lx", "icon": "mdi:weather-sunny"}})
        if template is None:
            self.block("no_template", "Choose the light-level template of this room. Nothing is chosen for you.")
            return
        eff = effective(room, template)
        for text in eff["problems"]:
            self.block("bad_limits", text)
        if a["method"] == "reference":
            if not a["reference"] or not a["reference"]["entity_id"]:
                self.block("missing_source", "Choose the reference sensor (one the room's lamps do not reach).")
        elif not room["source"] or not room["source"]["entity_id"]:
            self.block("missing_source", "Choose the room's light sensor. A room never uses another room's sensor by itself.")
        if a["method"] in ("off_sample", "calibrated") and not a["lights"]:
            self.block("no_lights", "Choose the lights whose light the estimate takes into account.")
        if a["method"] == "off_sample" and not a["max_age_minutes"]:
            self.block("no_max_age", "Choose how old the sample may be (minutes) before the level has no reading.")
        if a["method"] == "calibrated":
            missing = [light for light in a["lights"] if light not in a["contributions"]]
            if missing:
                self.block("uncalibrated", "Give the contribution (lux at full brightness) of " + ", ".join(missing)
                           + ", or take over an observed value. Nothing is assumed.")
        if not self.ok:
            return
        h = eff["settings"]["hysteresis"]["value"]
        limits = [b["below"] for b in eff["bands"][:-1]]
        gaps = [b - a_ for a_, b in zip([0, *limits], limits)]
        if not h:
            self.warn("no_hysteresis", "No hysteresis: a reading that hovers at a limit changes the level back and forth "
                      "(and lights that follow it). A hysteresis larger than a lamp's own contribution prevents that.")
        elif any(2 * h >= g for g in gaps[1:]):
            self.warn("wide_hysteresis", "The hysteresis is wider than half the span of a light level: some levels can "
                      "only be reached by a large change.")
        prefix = band_jinja(room, eff)
        darker = eff["settings"]["darker_seconds"]["value"] or 0
        brighter = eff["settings"]["brighter_seconds"]["value"] or 0
        unavailable = eff["settings"]["unavailable_seconds"]["value"]
        helper = ids["band"]
        differs = {"type": "template", "value_template": "{{ ll_new != '' and ll_new != states(" + quote(helper) + ") }}"}
        set_band = [{"type": "variables", "variables": {"ll_new": prefix + "{{ i[new] if new >= 0 else '' }}"}},
                    {"type": "if", "conditions": C.group("and", [differs]),
                     "then": [{"type": "service", "action": "input_text.set_value", "target": [helper],
                               "data": {"value": "{{ ll_new }}"}}], "else": []}]
        tpl = lambda body: prefix + "{{ " + body + " }}"  # noqa: E731
        name = "Light level"
        version = f"“{template['name'][:28]}” v{template['version']}"
        self.add("band", f"{name} follows the reading ({version})",
                 [C.t_template(tpl("c >= 0 and new > c"), brighter), C.t_template(tpl("c >= 0 and new >= 0 and new < c"), darker),
                  C.t_template(tpl("c < 0 and new >= 0")), {"type": "homeassistant", "event": "start"},
                  {"type": "event", "event_type": "automation_reloaded", "event_data": {}}],
                 [], set_band)
        stable = max(darker, brighter)
        watched = (a["reference"] if a["method"] == "reference" else room["source"])
        steady = (f" and (c < 0 or (now() - states[{quote(watched['entity_id'])}].last_changed).total_seconds() >= {stable})"
                  if a["method"] != "off_sample" and watched and stable else "")
        self.add("check", f"{name}: regular check",
                 [{"type": "pattern", "hours": None, "minutes": f"/{RECONCILE_MINUTES}", "seconds": None}],
                 [{"type": "template", "value_template": tpl("new >= 0 and new != c" + steady)}], set_band)
        if unavailable is not None:
            self.add("none", f"{name}: no reading",
                     [C.t_template(tpl("v is not number"), unavailable)],
                     [{"type": "template", "value_template": tpl("v is not number")}, C.c_state(helper, NO_READING, negate=True)],
                     [{"type": "service", "action": "input_text.set_value", "target": [helper], "data": {"value": NO_READING}}])
        if a["method"] == "off_sample":
            settle = a["settle_seconds"] or 0
            off = [C.c_state(light, "off", for_seconds=settle) for light in a["lights"]]
            read = _reading(room["source"])
            self.add("sample", f"{name}: sample the ambient light while the lights are off",
                     [*(C.t_state(light, to="off", for_seconds=settle) for light in a["lights"]),
                      {"type": "pattern", "hours": None, "minutes": f"/{RECONCILE_MINUTES}", "seconds": None}],
                     [*off, {"type": "template", "value_template": "{{ (" + read + ") is number }}"}],
                     [{"type": "service", "action": "input_number.set_value", "target": [ids["ambient"]],
                       "data": {"value": "{{ [[(" + read + ") | round(0), 0] | max, " + str(MAX_LUX) + "] | min }}"}}])
        self.view["effective"] = eff
        self.view["band_helper"] = helper


# ================================================================== observations (calibration help)
class Observations:
    """Passive calibration help: when a light that an ambient estimate takes into account changes while the room's
    sensor keeps reporting, the step of the reading is noted (lux per full brightness). The App never switches a light
    for this; it only watches what happens anyway. A value is only *offered*: the user takes it over or not."""

    WINDOW = (3.0, 90.0)   # seconds after the light's change in which the reading's step counts
    KEEP = 12
    FILE = "light_observations.json"

    def __init__(self, data_dir: Path | str | None) -> None:
        self.path = Path(data_dir) / self.FILE if data_dir else None
        self.samples: dict[str, list[dict[str, Any]]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.last: dict[str, dict[str, Any] | None] = {}   # the previous state of each watched light
        if self.path and self.path.exists():
            try:
                found = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(found, dict):
                    self.samples = {k: v for k, v in found.items() if isinstance(v, list)}
            except (OSError, ValueError):
                _LOGGER.warning("Could not read %s; starting without light observations", self.path)

    @staticmethod
    def key(area_id: str, light: str) -> str:
        return f"{area_id}|{light}"

    def on_state(self, levels: dict[str, Any] | None, states: dict[str, Any], entity_id: str,
                 new: dict[str, Any] | None, now: float | None = None) -> None:
        """A state changed in Home Assistant (``states`` holds the new state already; the previous state of the
        lights that ambient estimates take into account is remembered here)."""
        now = time.monotonic() if now is None else now
        rooms = (levels or {}).get("rooms") or {}
        watched = {light for room in rooms.values() for light in room["ambient"]["lights"]}
        old = self.last.get(entity_id)
        if entity_id in watched:
            self.last[entity_id] = new
        for area_id, room in rooms.items():
            src = room["source"]
            lights = room["ambient"]["lights"]
            if not src or not lights:
                continue
            if entity_id in lights:
                fraction = lambda s: (0.0 if not s or s.get("state") != "on" else  # noqa: E731
                                      (_number((s.get("attributes") or {}).get("brightness")) or 255) / 255)
                before, after = fraction(old), fraction(new)
                if (old or {}).get("state") in ("unavailable", "unknown", None) or abs(after - before) < 0.2:
                    continue
                others = {light: (states.get(light) or {}).get("state") for light in lights if light != entity_id}
                self.pending[area_id] = {"light": entity_id, "at": now, "delta": after - before,
                                         "before": _read(states, src) if old is not None else None, "others": others}
            elif entity_id == src["entity_id"] and area_id in self.pending:
                pend = self.pending[area_id]
                age = now - pend["at"]
                if age > self.WINDOW[1]:
                    del self.pending[area_id]
                    continue
                if age < self.WINDOW[0]:
                    continue
                others = {light: (states.get(light) or {}).get("state") for light in lights if light != pend["light"]}
                value = _read(states, src)
                del self.pending[area_id]
                if others != pend["others"] or value is None or pend["before"] is None:
                    continue
                step = (value - pend["before"]) / pend["delta"]
                if step <= 0:
                    continue
                key = self.key(area_id, pend["light"])
                self.samples.setdefault(key, []).append({"lux": round(step, 1), "at": datetime.now(UTC).isoformat(timespec="seconds")})
                self.samples[key] = self.samples[key][-self.KEEP:]
                self._write()

    def _write(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.samples), encoding="utf-8")
            tmp.replace(self.path)
        except OSError:
            _LOGGER.warning("Could not write %s", self.path)

    def summary(self, area_id: str, light: str) -> dict[str, Any] | None:
        found = [s["lux"] for s in self.samples.get(self.key(area_id, light), [])]
        if not found:
            return None
        median = statistics.median(found)
        spread = (max(found) - min(found)) / 2 if len(found) > 1 else None
        steady = len(found) >= 3 and spread is not None and spread <= max(0.25 * median, 5)
        return {"count": len(found), "median": round(median, 1), "spread": round(spread, 1) if spread is not None else None,
                "confidence": "consistent" if steady else "too few or too varied observations",
                "last": self.samples[self.key(area_id, light)][-1]["at"]}

    def clear(self, area_id: str, light: str | None = None) -> None:
        for key in list(self.samples):
            if key.startswith(f"{area_id}|") and (light is None or key == self.key(area_id, light)):
                del self.samples[key]
        self._write()


# ================================================================== presets the user can start from
def preset_template(template_id: str) -> dict[str, Any]:
    """An illustrative starting template (the user adds it on purpose and adjusts it; nothing is assigned)."""
    return {"id": template_id, "name": "Standard light levels", "version": 1,
            "bands": [{"id": "very_dark", "name": "Very dark", "below": 20}, {"id": "dark", "name": "Dark", "below": 80},
                      {"id": "dim", "name": "Dim", "below": 200}, {"id": "bright", "name": "Bright", "below": None}],
            "hysteresis": 10, "darker_seconds": 60, "brighter_seconds": 180, "unavailable_seconds": 600}


def blank_room(area_id: str, device_id: str, levels: dict[str, Any] | None) -> dict[str, Any]:
    levels = levels or {}
    defaults = levels.get("default_matrices") or {}
    template = levels.get("default_template")
    matrices = {layer: (defaults.get(layer) if defaults.get(layer) and (levels.get("matrices") or {}).get(defaults[layer], {}).get("template") == template
                        else None) for layer in LAYERS}
    return {"area_id": area_id, "device_id": device_id, "version": 1, "template": template, "source": None,
            "overrides": {"bands": {}}, "matrices": matrices, "matrix_overrides": {"truth": {}, "brightness": {}},
            "ambient": {"method": "none", "reference": None, "factor": None, "lights": [], "settle_seconds": None,
                        "max_age_minutes": None, "contributions": {}}}


def band_of_state(eff: dict[str, Any], state: Any) -> dict[str, Any] | None:
    return next((b for b in eff["bands"] if b["id"] == state), None)
