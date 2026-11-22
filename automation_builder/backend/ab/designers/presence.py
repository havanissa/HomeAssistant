"""Presence sources, shared by every designer: rooms, named zones inside rooms, position zones and persons.

A *source* says where presence information comes from; it never detects anything itself (that is the job of
the integration behind the entity, for example Smart Occupancy Detection, a radar or a phone). Kinds:

``room``      an occupancy or presence entity of a whole room (on = occupied), optionally a second entity and
              state for "possibly occupied" where the source supplies one.
``zone``      a named area inside a room (Sofa, Desk …) with its own on/off entity (a radar zone, a binary sensor).
``position``  a named area drawn in the coordinates of a sensor that reports positions (x/y per target). The
              zone is occupied while one of the targets is inside the shape. Anonymous: it never tells who.
``person``    a Home Assistant person or device tracker at a *geographic* zone (``home`` or another zone of
              Home Assistant). Kept apart from the indoor zones above.

A zone becoming vacant does not make its room vacant: every source is evaluated on its own. A source whose entity
is not chosen is *unset*: the rules that need it are not generated and deployment is blocked until it is chosen
or the rules no longer use it. An entity that exists but is unavailable is something else: the generated
conditions then simply do not hold (nothing happens).

Everything compiles to native Home Assistant triggers and conditions: states (with ``for``) and, for position
zones, a template that tests the targets against the shape. Automation Builder does not need to run for them.
"""

from __future__ import annotations

import math
import re
from typing import Any

from ..compiler import plain
from ..model import Problems
from . import common as C

SOURCE_ID = re.compile(r"^z_[0-9a-f]{8}$")
KINDS = ("room", "zone", "position", "person")
STATES = {"room": ("occupied", "vacant", "possible"), "zone": ("occupied", "vacant"),
          "position": ("occupied", "vacant"), "person": ("occupied", "vacant")}
STATE_WORDS = {"occupied": "occupied", "vacant": "vacant", "possible": "possibly occupied"}
ON_OFF_DOMAINS = ("binary_sensor", "input_boolean", "switch")
PERSON_DOMAINS = ("person", "device_tracker")
UNITS = ("mm", "cm", "m")
MAX_SOURCES = 100
MAX_TARGETS = 3
MAX_POINTS = 12


# ------------------------------------------------------------------ validation
def _ref(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {"entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, what="the entity with this coordinate"),
            "attribute": raw.get("attribute") if isinstance(raw.get("attribute"), str)
            and re.fullmatch(r"[A-Za-z0-9_]{1,60}", raw["attribute"]) else None}


def _position(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    targets = []
    for i, target in enumerate((raw.get("targets") if isinstance(raw.get("targets"), list) else [])[:MAX_TARGETS]):
        target = target if isinstance(target, dict) else {}
        targets.append({"x": _ref(target.get("x"), f"{path}.targets[{i}].x", p),
                        "y": _ref(target.get("y"), f"{path}.targets[{i}].y", p)})
    shape = raw.get("shape") if isinstance(raw.get("shape"), dict) else {}
    kind = shape.get("type") if shape.get("type") in ("rect", "polygon") else None
    points = []
    for i, point in enumerate((shape.get("points") if isinstance(shape.get("points"), list) else [])[:MAX_POINTS]):
        if isinstance(point, list) and len(point) == 2:
            x, y = C.number(point[0], f"{path}.shape.points[{i}]", p), C.number(point[1], f"{path}.shape.points[{i}]", p)
            if x is not None and y is not None:
                points.append([x, y])
                continue
        p.add(f"{path}.shape.points[{i}]", "A point has two numbers (x, y)")
    if kind == "rect" and len(points) > 2:
        p.add(f"{path}.shape", "A rectangle has two corners")
    bounds = raw.get("bounds") if isinstance(raw.get("bounds"), dict) else {}
    view = {}
    for axis in ("x", "y"):
        pair = bounds.get(axis)
        if isinstance(pair, list) and len(pair) == 2:
            low, high = C.number(pair[0], f"{path}.bounds.{axis}", p), C.number(pair[1], f"{path}.bounds.{axis}", p)
            if low is not None and high is not None and low < high:
                view[axis] = [low, high]
    units = raw.get("units") if raw.get("units") in UNITS else None
    return {"targets": targets, "units": units,
            "notes": C.text(raw.get("notes"), f"{path}.notes", p, limit=600),
            "shape": {"type": kind, "points": points} if kind else None,
            "ignore_origin": C.flag(raw.get("ignore_origin")),
            "max_age_seconds": C.number(raw.get("max_age_seconds"), f"{path}.max_age_seconds", p, 1, 3600, integer=True),
            "bounds": view or None}


def validate_source(source_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    kind = raw.get("kind")
    if kind not in KINDS:
        p.add(f"{path}.kind", "Choose what kind of presence source this is")
        kind = "zone"
    domains = PERSON_DOMAINS if kind == "person" else ON_OFF_DOMAINS if kind in ("room", "zone") else None
    what = {"person": "a person or device tracker", "room": "an occupancy entity (on = occupied)",
            "zone": "a zone entity (on = occupied)"}.get(kind, "an entity")
    out: dict[str, Any] = {
        "id": source_id, "name": C.text(raw.get("name"), f"{path}.name", p, required=True), "kind": kind,
        "area_id": raw.get("area_id") if isinstance(raw.get("area_id"), str) and 0 < len(raw["area_id"]) <= 64 else None,
        "entity_id": C.entity(raw.get("entity_id"), f"{path}.entity_id", p, domains, what) if kind != "position" else None,
        "active_state": None, "possible": None,
        "entry_seconds": C.seconds(raw.get("entry_seconds"), f"{path}.entry_seconds", p, 3600),
        "exit_seconds": C.seconds(raw.get("exit_seconds"), f"{path}.exit_seconds", p),
        "respect_fresh": C.flag(raw.get("respect_fresh")),
        "position": _position(raw.get("position"), f"{path}.position", p) if kind == "position" else None,
    }
    if kind == "person":
        state = raw.get("active_state")
        out["active_state"] = state.strip()[:60] if isinstance(state, str) and state.strip() else None
    if kind == "room" and isinstance(raw.get("possible"), dict):
        possible = raw["possible"]
        state = possible.get("state")
        found = {"entity_id": C.entity(possible.get("entity_id"), f"{path}.possible.entity_id", p),
                 "state": state.strip()[:60] if isinstance(state, str) and state.strip() else None}
        out["possible"] = found if found["entity_id"] or found["state"] else None
    return out


def validate_all(raw: Any, p: Problems) -> dict[str, Any] | None:
    """``config["presence"]``: ``{"sources": {id: source}}``, or None when nothing is configured."""
    if not isinstance(raw, dict):
        return None
    sources_raw = raw.get("sources") if isinstance(raw.get("sources"), dict) else {}
    if len(sources_raw) > MAX_SOURCES:
        p.add("presence.sources", f"At most {MAX_SOURCES} presence sources")
    sources = {}
    for source_id in sorted(sources_raw)[:MAX_SOURCES]:
        if not isinstance(source_id, str) or not SOURCE_ID.match(source_id):
            p.add(f"presence.sources.{source_id}", "Invalid source id")
            continue
        sources[source_id] = validate_source(source_id, sources_raw[source_id], f"presence.sources.{source_id}", p)
    return {"sources": sources} if sources else None


def missing(source: dict[str, Any], state: str) -> str | None:
    """What is not configured yet for this source in this state (None: complete)."""
    name = source["name"]
    if source["kind"] == "position":
        pos = source["position"] or {}
        shape = pos.get("shape") or {}
        points = shape.get("points") or []
        if not points or (shape.get("type") == "rect" and len(points) != 2) or (shape.get("type") == "polygon" and len(points) < 3):
            return f"the shape of the zone “{name}” is not complete yet (a rectangle needs two corners, a polygon three points)"
        if not pos.get("targets") or any(not t["x"]["entity_id"] or not t["y"]["entity_id"] for t in pos["targets"]):
            return f"the position zone “{name}” has no complete x/y source"
        return None
    if state == "possible":
        possible = source.get("possible") or {}
        if not possible.get("entity_id") or not possible.get("state"):
            return f"“{name}” has no source for “possibly occupied”"
        return None
    if not source["entity_id"]:
        return f"“{name}” has no entity yet"
    if source["kind"] == "person" and not source["active_state"]:
        return f"the person source “{name}” has no zone chosen (for example home)"
    return None


def entities(source: dict[str, Any]) -> list[str]:
    out = [source.get("entity_id"), (source.get("possible") or {}).get("entity_id")]
    for target in ((source.get("position") or {}).get("targets") or []):
        out += [target["x"]["entity_id"], target["y"]["entity_id"]]
    return [e for e in out if e]


# ------------------------------------------------------------------ position zones
def _coordinate(ref: dict[str, Any], refs: C.Refs) -> str:
    if ref["attribute"]:
        return f"(state_attr({refs.ref(ref['entity_id'])}, {C.quote(ref['attribute'])}) | float(none))"
    return f"(states({refs.ref(ref['entity_id'])}) | float(none))"


def _shape_points(shape: dict[str, Any]) -> list[list[float]]:
    points = [list(map(float, plain(pt))) for pt in shape["points"]]
    if shape["type"] == "rect":
        (x1, y1), (x2, y2) = points
        return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    return points


def _fmt(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def position_template(source: dict[str, Any], refs: C.Refs, which: str) -> str:
    """``which``: "inside" (a target is in the shape), "outside" (readings exist, none is in the shape), or
    "not_inside" (the zone is not occupied: no target in it, or no readings)."""
    pos = source["position"]
    poly = "[" + ", ".join(f"[{_fmt(x)}, {_fmt(y)}]" for x, y in _shape_points(pos["shape"])) + "]"
    parts = []
    for target in pos["targets"]:
        fresh = "true"
        if pos["max_age_seconds"]:
            fresh = (f"(as_timestamp(now()) - as_timestamp(states[{refs.ref(target['x']['entity_id'])}].last_updated, 0)"
                     f" < {pos['max_age_seconds']})")
        parts.append(f"[{_coordinate(target['x'], refs)}, {_coordinate(target['y'], refs)}, {fresh}]")
    origin = " and not (t[0] == 0 and t[1] == 0)" if pos["ignore_origin"] else ""
    result = {"inside": "ns.inside", "outside": "ns.seen and not ns.inside", "not_inside": "not ns.inside"}[which]
    return ("{% set poly = " + poly + " %}{% set ns = namespace(inside=false, seen=false) %}"
            "{% for t in [" + ", ".join(parts) + "] %}"
            "{% if t[0] is number and t[1] is number and t[2]" + origin + " %}{% set ns.seen = true %}"
            "{% set c = namespace(v=false) %}{% for i in range(poly | length) %}{% set a = poly[i] %}{% set b = poly[i - 1] %}"
            "{% if ((a[1] > t[1]) != (b[1] > t[1])) and (t[0] < (b[0] - a[0]) * (t[1] - a[1]) / (b[1] - a[1]) + a[0]) %}"
            "{% set c.v = not c.v %}{% endif %}{% endfor %}{% if c.v %}{% set ns.inside = true %}{% endif %}"
            "{% endif %}{% endfor %}{{ " + result + " }}")


def inside(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    """Ray casting, exactly as the template does it."""
    x, y = point
    result = False
    for i in range(len(polygon)):
        a, b = polygon[i], polygon[i - 1]
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            result = not result
    return result


def _reading(states: dict[str, Any], ref: dict[str, Any]) -> float | None:
    state = states.get(ref["entity_id"]) or {}
    value = (state.get("attributes") or {}).get(ref["attribute"]) if ref["attribute"] else state.get("state")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def evaluate_position(source: dict[str, Any], states: dict[str, Any], which: str) -> bool:
    """The Python twin of :func:`position_template` (simulation)."""
    pos = source["position"]
    polygon = _shape_points(pos["shape"])
    seen = found = False
    for target in pos["targets"]:
        x, y = _reading(states, target["x"]), _reading(states, target["y"])
        age = (states.get(target["x"]["entity_id"]) or {}).get("age")
        fresh = not pos["max_age_seconds"] or (age is not None and age < pos["max_age_seconds"])
        if x is None or y is None or not fresh or (pos["ignore_origin"] and x == 0 and y == 0):
            continue
        seen = True
        found = found or inside((x, y), polygon)
    return {"inside": found, "outside": seen and not found, "not_inside": not found}[which]


# ------------------------------------------------------------------ native triggers and conditions
# ``x`` is the expansion (``designers.Expansion``): every entity and delay passes through it, so that a blueprint
# can make it an input.
def _entity(source: dict[str, Any], x: Any) -> Any:
    return x.e(source["entity_id"], f"Presence source “{source['name']}”", group="Trigger entities",
               name=f"{source['name']}: entity")


def _delay(source: dict[str, Any], which: str, x: Any) -> Any:
    value = source["entry_seconds"] if which == "entry" else source["exit_seconds"]
    words = ("confirmation before it counts as entered" if which == "entry"
             else "delay before it counts as left (vacant)")
    return x.seconds(f"{source['id']}_{which}", value, f"{source['name']}: {which} delay", f"The {words}.")


def _state(source: dict[str, Any], x: Any) -> Any:
    return x.text(f"{source['id']}_state", source["active_state"], f"{source['name']}: zone",
                  "The state of the person or tracker that counts as present (home or the name of a zone).")


def _possible(source: dict[str, Any], x: Any) -> tuple[Any, Any]:
    possible = source["possible"]
    return (x.e(possible["entity_id"], f"“Possibly occupied” source of {source['name']}", group="Trigger entities"),
            x.text(f"{source['id']}_possible", possible["state"], f"{source['name']}: possibly occupied state",
                   "The state that means “possibly occupied”."))


def _position_source(source: dict[str, Any], x: Any) -> dict[str, Any]:
    """The source with its coordinate entities passed through the expansion (the shape stays as drawn)."""
    pos = source["position"]
    targets = [{axis: {**t[axis], "entity_id": x.e(t[axis]["entity_id"], f"{source['name']}: {axis} coordinate",
                                                    group="Sensor entities")} for axis in ("x", "y")}
               for t in pos["targets"]]
    return {**source, "position": {**pos, "targets": targets}}


def _fresh(source: dict[str, Any], entity_id: Any, x: Any) -> list[dict[str, Any]]:
    if not source["respect_fresh"] or source["kind"] not in ("room", "zone"):
        return []
    text = f"{{{{ state_attr({x.refs.ref(entity_id)}, 'fresh') != false }}}}"
    return [x.refs.template(text, lambda states, e=plain(entity_id): ((states.get(e) or {}).get("attributes") or {}).get("fresh") is not False)]


def active(source: dict[str, Any], state: str, x: Any) -> list[dict[str, Any]]:
    """The conditions that hold while the source is in this state (all of them must hold)."""
    kind = source["kind"]
    if kind == "position":
        which = "inside" if state == "occupied" else "outside"
        src = _position_source(source, x)
        text = position_template(src, x.refs, which)
        return [x.refs.template(text, lambda states, s=source, w=which: evaluate_position(s, states, w))]
    if state == "possible":
        entity_id, value = _possible(source, x)
        return [C.c_state(entity_id, value, for_seconds=_delay(source, "entry", x))]
    entity_id = _entity(source, x)
    if kind == "person":
        if state == "occupied":
            return [C.c_state(entity_id, _state(source, x), for_seconds=_delay(source, "entry", x))]
        return [C.c_state(entity_id, _state(source, x), negate=True),
                C.c_state(entity_id, "unavailable", negate=True),
                C.c_state(entity_id, "unknown", negate=True)]
    value, delay = ("on", _delay(source, "entry", x)) if state == "occupied" else ("off", _delay(source, "exit", x))
    return [C.c_state(entity_id, value, for_seconds=delay), *_fresh(source, entity_id, x)]


def entered(source: dict[str, Any], state: str, x: Any, *, occupancy_type: bool = False) -> dict[str, Any]:
    """The trigger "the source has reached this state (and held it for its delay)"."""
    kind = source["kind"]
    if kind == "position":
        which = "inside" if state == "occupied" else "outside"
        delay = _delay(source, "entry" if state == "occupied" else "exit", x)
        return C.t_template(position_template(_position_source(source, x), x.refs, which), delay)
    if state == "possible":
        entity_id, value = _possible(source, x)
        return C.t_state(entity_id, to=value, for_seconds=_delay(source, "entry", x))
    entity_id = _entity(source, x)
    if kind == "person":
        if state == "occupied":
            return C.t_state(entity_id, to=_state(source, x), for_seconds=_delay(source, "entry", x))
        return C.t_state(entity_id, frm=_state(source, x), for_seconds=_delay(source, "exit", x))
    if occupancy_type:
        return C.t_occupancy(entity_id, "occupied" if state == "occupied" else "vacant")
    if state == "occupied":
        return C.t_state(entity_id, to="on", for_seconds=_delay(source, "entry", x))
    return C.t_state(entity_id, to="off", for_seconds=_delay(source, "exit", x))


def ended(source: dict[str, Any], state: str, x: Any) -> dict[str, Any]:
    """The trigger "the source has left this state" (so that a situation it dominated applies again)."""
    kind = source["kind"]
    if kind == "position":
        src = _position_source(source, x)
        return C.t_template(position_template(src, x.refs, "not_inside" if state == "occupied" else "inside"))
    if state == "possible":
        entity_id, value = _possible(source, x)
        return C.t_state(entity_id, frm=value)
    entity_id = _entity(source, x)
    if kind == "person":
        return C.t_state(entity_id, frm=_state(source, x)) if state == "occupied" else C.t_state(entity_id, to=_state(source, x))
    return C.t_state(entity_id, frm="on" if state == "occupied" else "off")


def recheckable(source: dict[str, Any], state: str) -> bool:
    """Can this situation use the occupancy trigger with its automatic rechecks (a binary sensor, no delays)?"""
    return (source["kind"] in ("room", "zone") and state in ("occupied", "vacant")
            and str(plain(source["entity_id"] or "")).startswith("binary_sensor.")
            and not source["entry_seconds"] and not source["exit_seconds"])


def label(source: dict[str, Any], state: str) -> str:
    if source["kind"] == "person":
        where = source["active_state"] or "…"
        return f"{source['name']} at {where}" if state == "occupied" else f"{source['name']} not at {where}"
    if source["kind"] == "room":
        return f"Room {STATE_WORDS[state]}" if source["name"].lower() in ("room", "") else f"{source['name']} {STATE_WORDS[state]}"
    return f"{source['name']} {STATE_WORDS[state]}"
