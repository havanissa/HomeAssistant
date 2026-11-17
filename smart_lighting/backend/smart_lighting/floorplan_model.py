"""Floor plan data model (Phase A2.1): validation, schema migrations, revisions, image checks.

Pure functions only (no I/O), so they are easy to test. The storage service
is :mod:`smart_lighting.floorplan`.

Coordinate system
-----------------
Every floor has its own flat 2D Cartesian plane. **Units are metres.** The
origin is arbitrary (where the user started drawing). ``x`` grows to the right
and ``y`` grows *downwards* on the screen (the SVG convention), so a point
``(2, 3)`` is 2 m right of and 3 m below the origin. Angles are degrees,
clockwise, with 0 pointing to the top of the plan (``-y``).
``building.north_deg`` is the compass bearing of the plan's top edge (0: the
top of the plan is north; 90: it is east; ``None``: unknown). A floor's
``measured`` flag says whether its dimensions are real (calibrated or typed
in) or only a sketch; geometry is always stored in metres either way.

A background image is placed with its top-left pixel at ``(x, y)`` metres,
``scale`` metres per image pixel and ``rotation`` degrees clockwise about
that top-left corner.

Shape of a plan (version 1)::

    {"format": "smart_lighting.floorplan", "version": 1,
     "building": {"id", "name", "north_deg"},
     "floors": [{"id", "name", "level", "ha_floor_id", "measured",
                 "vertices": [{"id", "x", "y"}],           # shared corners
                 "rooms": [{"id", "name", "area_id", "area_name", "shares_area", "vertices": [vertex ids]}],
                 "walls": [{"id", "a", "b"}],               # room edges, a stable id per vertex pair
                 "openings": [{"id", "type", "wall_id", "offset", "width", "swing"}],
                 "placements": [{"id", "entity_id", "entity_uid", "device_id", "x", "y", "label"}],
                 "background": null | {"image_id", "width_px", "height_px", "x", "y", "scale",
                                       "rotation", "opacity", "visible", "locked"}}],
     "dismissed": [suggestion keys], "updated_at"}

Rooms reference Home Assistant areas by their stable ``area_id`` (``area_name``
is only the name seen when it was linked, to notice renames); placements
reference entities by ``entity_id`` plus the entity registry's stable ``id``
(``entity_uid``), which survives an entity id rename. Nothing of the
integration's configuration is copied here.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
import hashlib
import json
import math
import re
import struct
from typing import Any

FORMAT = "smart_lighting.floorplan"
EXPORT_FORMAT = "smart_lighting.floorplan_export"
SCHEMA_VERSION = 1

ID = re.compile(r"^[a-z]{1,4}_[A-Za-z0-9]{1,40}$")
IMAGE_ID = re.compile(r"^img_[0-9a-f]{16}$")
AREA_ID = re.compile(r"^[\w\-]{1,255}$")
FLOOR_ID = re.compile(r"^[\w\-]{1,255}$")
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
UID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SUGGESTION_KEY = re.compile(r"^[a-z_]{1,24}:[\w\-|.]{1,520}$")
OPENING_TYPES = ("door", "window", "opening")
SWINGS = (None, "left", "right")

LIMITS = {
    "floors": 20,
    "rooms": 200,  # per floor
    "vertices": 4000,  # per floor
    "room_vertices": 200,
    "walls": 8000,
    "openings": 1000,
    "placements": 2000,
    "dismissed": 500,
    "name": 100,
    "coordinate": 10_000.0,  # metres from the origin
}
MIN_ROOM_AREA = 0.01  # m²


class PlanError(ValueError):
    """The plan is not valid. ``code`` is stable, ``message`` is for people, ``path`` locates it."""

    def __init__(self, code: str, message: str, path: str = "") -> None:
        super().__init__(f"{path}: {message}" if path else message)
        self.code = code
        self.message = message
        self.path = path


# ----------------------------------------------------------------- revisions
def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def revision_of(plan: dict[str, Any] | None) -> str:
    """Revision of the stored plan (``updated_at`` excluded, so it reflects content only)."""
    if plan is None:
        return hashlib.sha256(b"null").hexdigest()[:16]
    content = {k: v for k, v in plan.items() if k != "updated_at"}
    return hashlib.sha256(canonical(content).encode()).hexdigest()[:16]


def empty_plan() -> dict[str, Any]:
    return {"format": FORMAT, "version": SCHEMA_VERSION, "building": {"id": "bld_home", "name": "", "north_deg": None},
            "floors": [], "dismissed": [], "updated_at": None}


# ---------------------------------------------------------------- migrations
#: version -> function turning a plan of that version into version + 1.
MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def migrate(doc: Any) -> tuple[dict[str, Any], list[int]]:
    """Bring a stored plan to :data:`SCHEMA_VERSION`. Returns the plan and the versions it was migrated from.

    Raises PlanError ``newer_format`` for a plan written by a newer App (it must
    not be overwritten by this version) and ``invalid_format`` for anything else.
    """
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise PlanError("invalid_format", "This is not a Smart Lighting floor plan")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise PlanError("invalid_format", "The floor plan has no valid format version")
    if version > SCHEMA_VERSION:
        raise PlanError("newer_format", f"The floor plan was saved by a newer version of the App (format {version}; "
                                        f"this version reads format {SCHEMA_VERSION}). Update the App to edit it.")
    steps: list[int] = []
    plan = copy.deepcopy(doc)
    while plan["version"] < SCHEMA_VERSION:
        step = MIGRATIONS.get(plan["version"])
        if step is None:
            raise PlanError("invalid_format", f"No migration from format {plan['version']}")
        steps.append(plan["version"])
        plan = step(plan)
        plan["version"] = steps[-1] + 1
    return plan, steps


# ---------------------------------------------------------------- validation
def _num(value: Any, path: str, *, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanError("invalid_number", "must be a finite number", path)
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise PlanError("out_of_range", f"must be between {lo} and {hi}", path)
    return float(value)


def _coord(value: Any, path: str) -> float:
    limit = LIMITS["coordinate"]
    return round(_num(value, path, lo=-limit, hi=limit), 4)


def _id(value: Any, path: str, seen: set[str]) -> str:
    if not isinstance(value, str) or not ID.match(value):
        raise PlanError("invalid_id", "is not a valid identifier", path)
    if value in seen:
        raise PlanError("duplicate_id", f"identifier {value} is used twice", path)
    seen.add(value)
    return value


def _name(value: Any, path: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise PlanError("invalid_name", "must be text", path)
    value = " ".join(value.split())
    if len(value) > LIMITS["name"]:
        raise PlanError("invalid_name", f"is longer than {LIMITS['name']} characters", path)
    if required and not value:
        raise PlanError("invalid_name", "must not be empty", path)
    return value


def _opt(value: Any, pattern: re.Pattern[str], path: str, code: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not pattern.match(value):
        raise PlanError(code, "is not valid", path)
    return value


def _list(value: Any, path: str, limit_key: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanError("invalid_list", "must be a list", path)
    if len(value) > LIMITS[limit_key]:
        raise PlanError("too_many", f"has more than {LIMITS[limit_key]} items", path)
    return value


def polygon_area(points: list[tuple[float, float]]) -> float:
    """Signed area (shoelace). Positive for clockwise order on screen (y down)."""
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return total / 2


def _segments_cross(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float],
                    p4: tuple[float, float]) -> bool:
    """Proper intersection or overlap of two segments (touching at shared endpoints excluded by the caller)."""

    def orient(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def on_segment(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> bool:
        return min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9 and \
            min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9

    eps = 1e-12
    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and ((d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)):
        return True
    return any(abs(d) <= eps and on_segment(a, b, c) for d, a, b, c in (
        (d1, p3, p4, p1), (d2, p3, p4, p2), (d3, p1, p2, p3), (d4, p1, p2, p4)))


def is_simple_polygon(points: list[tuple[float, float]]) -> bool:
    """No two non-adjacent edges touch, no repeated points."""
    n = len(points)
    if n < 3 or len(set(points)) != n:
        return False
    edges = [(points[i], points[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbours share a vertex
            if _segments_cross(*edges[i], *edges[j]):
                return False
    return True


def _background(value: Any, path: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanError("invalid_background", "must be an object", path)
    image_id = value.get("image_id")
    if not isinstance(image_id, str) or not IMAGE_ID.match(image_id):
        raise PlanError("invalid_image", "has no valid image id", f"{path}.image_id")
    return {
        "image_id": image_id,
        "width_px": int(_num(value.get("width_px"), f"{path}.width_px", lo=1, hi=100_000)),
        "height_px": int(_num(value.get("height_px"), f"{path}.height_px", lo=1, hi=100_000)),
        "x": _coord(value.get("x", 0), f"{path}.x"),
        "y": _coord(value.get("y", 0), f"{path}.y"),
        "scale": _num(value.get("scale", 0.01), f"{path}.scale", lo=1e-6, hi=10.0),
        "rotation": _num(value.get("rotation", 0), f"{path}.rotation", lo=-360, hi=360),
        "opacity": _num(value.get("opacity", 0.5), f"{path}.opacity", lo=0, hi=1),
        "visible": bool(value.get("visible", True)),
        "locked": bool(value.get("locked", False)),
    }


def _floor(value: Any, path: str, seen: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanError("invalid_floor", "must be an object", path)
    floor: dict[str, Any] = {
        "id": _id(value.get("id"), f"{path}.id", seen),
        "name": _name(value.get("name"), f"{path}.name", required=True),
        "level": int(_num(value.get("level", 0), f"{path}.level", lo=-20, hi=200)),
        "ha_floor_id": _opt(value.get("ha_floor_id"), FLOOR_ID, f"{path}.ha_floor_id", "invalid_floor_id"),
        "measured": bool(value.get("measured", False)),
    }
    vertices: dict[str, tuple[float, float]] = {}
    floor["vertices"] = []
    for i, v in enumerate(_list(value.get("vertices"), f"{path}.vertices", "vertices")):
        vp = f"{path}.vertices[{i}]"
        if not isinstance(v, dict):
            raise PlanError("invalid_vertex", "must be an object", vp)
        vid = _id(v.get("id"), f"{vp}.id", seen)
        point = (_coord(v.get("x"), f"{vp}.x"), _coord(v.get("y"), f"{vp}.y"))
        vertices[vid] = point
        floor["vertices"].append({"id": vid, "x": point[0], "y": point[1]})

    used: set[str] = set()
    floor["rooms"] = []
    for i, r in enumerate(_list(value.get("rooms"), f"{path}.rooms", "rooms")):
        rp = f"{path}.rooms[{i}]"
        if not isinstance(r, dict):
            raise PlanError("invalid_room", "must be an object", rp)
        ids = r.get("vertices")
        if not isinstance(ids, list) or not 3 <= len(ids) <= LIMITS["room_vertices"]:
            raise PlanError("invalid_polygon", f"needs 3 to {LIMITS['room_vertices']} corners", f"{rp}.vertices")
        for vid in ids:
            if vid not in vertices:
                raise PlanError("unknown_vertex", f"refers to the unknown corner {vid}", f"{rp}.vertices")
        if len(set(ids)) != len(ids):
            raise PlanError("invalid_polygon", "uses a corner twice", f"{rp}.vertices")
        points = [vertices[v] for v in ids]
        if not is_simple_polygon(points):
            raise PlanError("invalid_polygon", "has walls that cross each other", f"{rp}.vertices")
        if abs(polygon_area(points)) < MIN_ROOM_AREA:
            raise PlanError("invalid_polygon", "has no area", f"{rp}.vertices")
        used.update(ids)
        floor["rooms"].append({
            "id": _id(r.get("id"), f"{rp}.id", seen),
            "name": _name(r.get("name"), f"{rp}.name"),
            "area_id": _opt(r.get("area_id"), AREA_ID, f"{rp}.area_id", "invalid_area"),
            "area_name": _name(r.get("area_name"), f"{rp}.area_name") or None,
            "shares_area": bool(r.get("shares_area", False)),
            "vertices": list(ids),
        })
    # Unused corners are dropped (they would only be left over from edits).
    floor["vertices"] = [v for v in floor["vertices"] if v["id"] in used]

    walls: dict[str, tuple[str, str]] = {}
    pairs: set[frozenset[str]] = set()
    floor["walls"] = []
    for i, w in enumerate(_list(value.get("walls"), f"{path}.walls", "walls")):
        wp = f"{path}.walls[{i}]"
        if not isinstance(w, dict):
            raise PlanError("invalid_wall", "must be an object", wp)
        a, b = w.get("a"), w.get("b")
        if a not in used or b not in used or a == b:
            raise PlanError("invalid_wall", "must join two different corners of a room", wp)
        pair = frozenset((a, b))
        if pair in pairs:
            raise PlanError("duplicate_wall", "joins the same corners as another wall", wp)
        pairs.add(pair)
        wid = _id(w.get("id"), f"{wp}.id", seen)
        walls[wid] = (a, b)
        floor["walls"].append({"id": wid, "a": a, "b": b})

    floor["openings"] = []
    for i, o in enumerate(_list(value.get("openings"), f"{path}.openings", "openings")):
        op = f"{path}.openings[{i}]"
        if not isinstance(o, dict):
            raise PlanError("invalid_opening", "must be an object", op)
        if o.get("type") not in OPENING_TYPES:
            raise PlanError("invalid_opening", "has an unknown type", f"{op}.type")
        wall = walls.get(o.get("wall_id"))  # type: ignore[arg-type]
        if wall is None:
            raise PlanError("unknown_wall", "is not on a wall of this floor", f"{op}.wall_id")
        (ax, ay), (bx, by) = vertices[wall[0]], vertices[wall[1]]
        length = math.hypot(bx - ax, by - ay)
        width = _num(o.get("width"), f"{op}.width", lo=0.05, hi=50)
        offset = _num(o.get("offset"), f"{op}.offset", lo=0, hi=LIMITS["coordinate"])
        if o.get("swing") not in SWINGS:
            raise PlanError("invalid_opening", "has an unknown door swing", f"{op}.swing")
        # ``offset``: distance of the opening's centre from the wall's corner ``a``. Kept on the wall: a wall
        # edited shorter clamps its openings instead of losing them.
        half = min(width, length) / 2
        floor["openings"].append({
            "id": _id(o.get("id"), f"{op}.id", seen), "type": o["type"], "wall_id": o["wall_id"],
            "offset": round(min(max(offset, half), max(length - half, half)), 4),
            "width": round(width, 4), "swing": o.get("swing") if o["type"] == "door" else None,
        })

    floor["placements"] = []
    for i, p in enumerate(_list(value.get("placements"), f"{path}.placements", "placements")):
        pp = f"{path}.placements[{i}]"
        if not isinstance(p, dict):
            raise PlanError("invalid_placement", "must be an object", pp)
        entity_id = p.get("entity_id")
        if not isinstance(entity_id, str) or not ENTITY_ID.match(entity_id):
            raise PlanError("invalid_entity", "is not a valid entity id", f"{pp}.entity_id")
        floor["placements"].append({
            "id": _id(p.get("id"), f"{pp}.id", seen), "entity_id": entity_id,
            "entity_uid": _opt(p.get("entity_uid"), UID, f"{pp}.entity_uid", "invalid_entity"),
            "device_id": _opt(p.get("device_id"), UID, f"{pp}.device_id", "invalid_device"),
            "x": _coord(p.get("x"), f"{pp}.x"), "y": _coord(p.get("y"), f"{pp}.y"),
            "label": _name(p.get("label"), f"{pp}.label") or None,
        })
    floor["background"] = _background(value.get("background"), f"{path}.background")
    return floor


def validate(doc: Any) -> dict[str, Any]:
    """A clean copy of a current-version plan, or PlanError. Applies the limits and every reference check."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT or doc.get("version") != SCHEMA_VERSION:
        raise PlanError("invalid_format", "This is not a current Smart Lighting floor plan")
    building = doc.get("building") or {}
    if not isinstance(building, dict):
        raise PlanError("invalid_building", "must be an object", "building")
    north = building.get("north_deg")
    seen: set[str] = set()
    plan: dict[str, Any] = {
        "format": FORMAT,
        "version": SCHEMA_VERSION,
        "building": {
            "id": _id(building.get("id", "bld_home"), "building.id", seen),
            "name": _name(building.get("name"), "building.name"),
            "north_deg": None if north is None else round(_num(north, "building.north_deg", lo=-360, hi=360) % 360, 2),
        },
        "floors": [_floor(f, f"floors[{i}]", seen) for i, f in enumerate(_list(doc.get("floors"), "floors", "floors"))],
        "dismissed": [],
        "updated_at": doc.get("updated_at") if isinstance(doc.get("updated_at"), str) else None,
    }
    for i, key in enumerate(_list(doc.get("dismissed"), "dismissed", "dismissed")):
        if not isinstance(key, str) or not SUGGESTION_KEY.match(key):
            raise PlanError("invalid_dismissed", "is not a valid suggestion key", f"dismissed[{i}]")
        if key not in plan["dismissed"]:
            plan["dismissed"].append(key)
    _check_area_links(plan)
    return plan


def _check_area_links(plan: dict[str, Any]) -> None:
    """One Home Assistant area per drawn room, unless every room sharing it is marked as a shared space."""
    linked: dict[str, list[dict[str, Any]]] = {}
    for floor in plan["floors"]:
        for room in floor["rooms"]:
            if room["area_id"]:
                linked.setdefault(room["area_id"], []).append(room)
    for area_id, rooms in linked.items():
        if len(rooms) > 1 and not all(r["shares_area"] for r in rooms):
            names = ", ".join(r["name"] or r["id"] for r in rooms)
            raise PlanError("duplicate_area_link", f"The Home Assistant area {area_id} is linked to several rooms "
                                                   f"({names}). Mark them as one shared or open-plan space, or "
                                                   "link only one.", "floors")


def image_ids(plan: dict[str, Any]) -> set[str]:
    return {f["background"]["image_id"] for f in plan.get("floors") or [] if isinstance(f, dict)
            and isinstance(f.get("background"), dict) and isinstance(f["background"].get("image_id"), str)}


def summary(plan: dict[str, Any]) -> dict[str, Any]:
    floors = plan.get("floors") or []
    return {
        "floors": len(floors),
        "rooms": sum(len(f.get("rooms") or []) for f in floors),
        "linked_rooms": sum(1 for f in floors for r in f.get("rooms") or [] if r.get("area_id")),
        "openings": sum(len(f.get("openings") or []) for f in floors),
        "placements": sum(len(f.get("placements") or []) for f in floors),
        "backgrounds": sum(1 for f in floors if f.get("background")),
    }


# -------------------------------------------------------------------- images
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGE_SIDE = 16_384
MIN_IMAGE_SIDE = 8


class ImageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + length
    return None


def sniff_image(data: bytes) -> tuple[str, int, int]:
    """(kind, width, height) of a PNG, JPEG or WebP image, from its own bytes (never the file name or header)."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageError("too_large", f"The image is larger than {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    size: tuple[int, int] | None = None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR" and len(data) >= 24:
        kind, size = "png", struct.unpack(">II", data[16:24])
    elif data[:3] == b"\xff\xd8\xff":
        kind, size = "jpeg", _jpeg_size(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        kind, chunk = "webp", data[12:16]
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            w, h = struct.unpack("<HH", data[26:30])
            size = (w & 0x3FFF, h & 0x3FFF)
        elif chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            size = ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        elif chunk == b"VP8X":
            size = (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
    else:
        raise ImageError("unsupported_type", "Only PNG, JPEG and WebP images can be used")
    if size is None:
        raise ImageError("invalid_image", "The image file is damaged or incomplete")
    width, height = size
    if not (MIN_IMAGE_SIDE <= width <= MAX_IMAGE_SIDE and MIN_IMAGE_SIDE <= height <= MAX_IMAGE_SIDE):
        raise ImageError("invalid_size", f"The image must be between {MIN_IMAGE_SIDE} and {MAX_IMAGE_SIDE} pixels "
                                         "on each side")
    return kind, width, height


def image_id_for(data: bytes) -> str:
    """Content-addressed id: the same file always gets the same id."""
    return "img_" + hashlib.sha256(data).hexdigest()[:16]
