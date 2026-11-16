"""Brightness rules (0.6.7, Phase 4.7): one brightness matrix per enrolled light.

Pure logic. *Occupancy chooses the scope, ambient light chooses the
brightness, and the mood chooses the appearance.*

Matrix
------
An enrolled light has a value per ambient category for the **Room occupied**
column and for each zone it is explicitly associated with (the zone lists it
as a target). Cells are keyed by the category's stable id (0.6.8: the room's
effective categories, ``ambient.AmbientSettings.levels``), so a renamed
category keeps its values and a new one starts as *Not configured* (no value
is invented). Values of a category that no longer exists are kept in storage
until the reviewed removal reassigns or deletes them (``ambient_levels.py``).
A value is a brightness percentage:

* ``0`` is **Off** (``light.turn_off``, never ``turn_on`` with 0 %),
* ``1``-``100`` is On at that brightness (On for an on/off-only light, a relay
  or a Dumb Light; their editor offers On / Off and stores On as 100),
* ``None`` is *Not configured*: a zone cell does not claim the brightness
  (another active zone with a value, else the Room occupied value of the same
  category applies); a Room occupied cell (with no zone value either) leaves
  the light to its previous rules (mood profile, zone target) for that
  category.

Which column decides
--------------------
Only while the room is occupied (its vacancy behaviour is unchanged). Among
the active zones that list the light and have a value for the current
category, the existing zone arbitration picks one winner (higher priority,
then the zone listed first); values are never combined or averaged and only
one command is sent. Without such a zone the Room occupied value applies.

Ownership
---------
For an enrolled light the matrix owns on/off and brightness; the room's mood
(Occupied profile, global adjustment included) owns colour, white colour
temperature and effect. A legacy brightness from the mood, a zone target or
the global brightness adjustment is not used for it. A light that is not
enrolled (or whose matrix has no value for the situation) keeps every
previous behaviour exactly.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
import math
from typing import TYPE_CHECKING, Any

from .ambient import _ID as LEVEL_ID, DEFAULT_LEVELS, LEVEL_KEYS, MAX_LEVELS_LIMIT, AmbientLevel
from .const import AdjustMode, DaylightCondition, LightAction

if TYPE_CHECKING:  # (profiles imports the lighting configuration, which imports this module)
    from .profiles import ResolvedSetting

MAX_TEMPLATES = 24
APPEARANCE_FIELDS = ("color_temp_kelvin", "color_temp_mode", "color_temp_offset", "rgb_color", "hs_color", "effect")
ROOM = "room"


def _cell(value: Any) -> int | None:
    """0-100 (0 = Off) or None (not configured)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number):
        return None
    return int(round(min(max(number, 0), 100)))


def cells_from(data: Any, keys: Iterable[str] | None = None) -> dict[str, int | None]:
    """Cells by category id. ``keys``: exactly these categories; None: every valid category id given."""
    data = data if isinstance(data, dict) else {}
    if keys is not None:
        return {k: _cell(data.get(k)) for k in keys}
    out = {k: _cell(v) for k, v in data.items() if isinstance(k, str) and LEVEL_ID.match(k)}
    return dict(list(out.items())[:MAX_LEVELS_LIMIT * 2])


def cells_as_dict(cells: Mapping[str, int | None], keys: Iterable[str] | None = None) -> dict[str, int | None]:
    if keys is None:
        return dict(cells)
    return {k: cells.get(k) for k in keys}


def describe_value(value: int | None, *, onoff: bool = False) -> str:
    if value is None:
        return "Not configured"
    if value == 0:
        return "Off"
    return "On" if onoff else f"{value} %"


# ------------------------------------------------------------ configuration
@dataclass(frozen=True, slots=True)
class LightMatrix:
    """The brightness matrix of one enrolled light (see the module docstring)."""

    entity_id: str
    enabled: bool = True
    room: dict[str, int | None] = field(default_factory=dict)
    zones: dict[str, dict[str, int | None]] = field(default_factory=dict)
    template: str | None = None      # the template it was created from ("Restore defaults")
    enrolled_at: str | None = None
    enrolled_by: str | None = None

    @classmethod
    def from_dict(cls, entity_id: str, data: Any, zone_ids: Iterable[str] | None = None) -> LightMatrix:
        data = data if isinstance(data, dict) else {}
        known = set(zone_ids) if zone_ids is not None else None
        zones = {str(z): cells_from(c) for z, c in (data.get("zones") or {}).items()
                 if isinstance(c, dict) and (known is None or z in known)}
        return cls(
            entity_id=entity_id, enabled=data.get("enabled", True) is not False, room=cells_from(data.get("room")),
            zones=zones, template=data.get("template") if isinstance(data.get("template"), str) else None,
            enrolled_at=data.get("enrolled_at") if isinstance(data.get("enrolled_at"), str) else None,
            enrolled_by=data.get("enrolled_by") if isinstance(data.get("enrolled_by"), str) else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "room": cells_as_dict(self.room),
                "zones": {z: cells_as_dict(c) for z, c in self.zones.items()}, "template": self.template,
                "enrolled_at": self.enrolled_at, "enrolled_by": self.enrolled_by}

    def value(self, column: str, level: str) -> int | None:
        cells = self.room if column == ROOM else self.zones.get(column) or {}
        return cells.get(level)

    def columns(self) -> dict[str, dict[str, int | None]]:
        return {ROOM: self.room, **self.zones}

    def configured_cells(self, level: str) -> dict[str, int]:
        """Column -> value of every configured cell of one category."""
        return {c: v for c, cells in self.columns().items() if (v := cells.get(level)) is not None}

    def remapped(self, moves: Mapping[str, str | None]) -> LightMatrix:
        """Categories removed in a reviewed change: ``{removed: target}`` moves a value (it replaces the target's
        value), ``{removed: None}`` deletes it."""
        def move(cells: dict[str, int | None]) -> dict[str, int | None]:
            out = {k: v for k, v in cells.items() if k not in moves}
            for old, new in moves.items():
                if new is not None and cells.get(old) is not None:
                    out[new] = cells[old]
            return out
        return replace(self, room=move(self.room), zones={z: move(c) for z, c in self.zones.items()})

    @property
    def configured(self) -> bool:
        return any(v is not None for v in self.room.values()) or any(
            v is not None for c in self.zones.values() for v in c.values())


def matrices_from(data: Any, zone_ids: Iterable[str]) -> dict[str, LightMatrix]:
    zone_ids = list(zone_ids)
    out: dict[str, LightMatrix] = {}
    for entity_id, item in (data.items() if isinstance(data, dict) else []):
        if isinstance(entity_id, str) and "." in entity_id and isinstance(item, dict):
            out[entity_id] = LightMatrix.from_dict(entity_id, item, zone_ids)
    return out


def _levels_from(data: Any) -> tuple[AmbientLevel, ...]:
    out: list[AmbientLevel] = []
    for item in data if isinstance(data, (list, tuple)) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and LEVEL_ID.match(item["id"]) \
                and isinstance(item.get("start"), (int, float)) and not isinstance(item.get("start"), bool) \
                and all(lv.id != item["id"] for lv in out) and (not out or item["start"] > out[-1].start):
            out.append(AmbientLevel(item["id"], str(item.get("name") or item["id"])[:40], int(item["start"])))
    return tuple(out) if len(out) >= 2 and out[0].start == 0 else DEFAULT_LEVELS


def map_cells(cells: Mapping[str, int | None], source: tuple[AmbientLevel, ...],
              target: tuple[AmbientLevel, ...]) -> dict[str, int | None]:
    """Values of ``source`` categories for ``target`` categories (0.6.8): the same id takes its own value;
    another category takes the value of the source category its start lies in."""
    out: dict[str, int | None] = {}
    for level in target:
        if level.id in cells and any(s.id == level.id for s in source):
            out[level.id] = cells.get(level.id)
            continue
        containing = next((s for s in reversed(source) if level.start >= s.start), source[0])
        out[level.id] = cells.get(containing.id)
    return out


@dataclass(frozen=True, slots=True)
class Template:
    """A matrix for a kind of fixture: the Room occupied column and one column applied to every zone.

    0.6.8: a template keeps the categories it was made for (``levels``; the built-ins: the default ones) and
    is mapped onto a room's own categories when it is applied (``for_levels``).
    """

    id: str
    name: str
    room: dict[str, int | None]
    zone: dict[str, int | None]
    builtin: bool = False
    levels: tuple[AmbientLevel, ...] = DEFAULT_LEVELS

    @classmethod
    def from_dict(cls, data: Any) -> Template | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            return None
        name = str(data.get("name") or data["id"]).strip()[:60] or data["id"]
        levels = _levels_from(data.get("levels"))
        keys = [lv.id for lv in levels]
        return cls(id=data["id"], name=name, room=cells_from(data.get("room"), keys),
                   zone=cells_from(data.get("zone"), keys), levels=levels)

    def as_dict(self) -> dict[str, Any]:
        keys = [lv.id for lv in self.levels]
        return {"id": self.id, "name": self.name, "room": cells_as_dict(self.room, keys),
                "zone": cells_as_dict(self.zone, keys), "builtin": self.builtin,
                "levels": [lv.as_dict() for lv in self.levels]}

    def for_levels(self, levels: tuple[AmbientLevel, ...]) -> Template:
        """This template for other categories (values mapped by id, else by lux range)."""
        if tuple(lv.id for lv in levels) == tuple(lv.id for lv in self.levels):
            return self
        return replace(self, room=map_cells(self.room, self.levels, levels),
                       zone=map_cells(self.zone, self.levels, levels), levels=levels)

    def matrix(self, entity_id: str, zone_ids: Iterable[str], levels: tuple[AmbientLevel, ...] | None = None,
               **kw: Any) -> LightMatrix:
        t = self.for_levels(levels) if levels else self
        return LightMatrix(entity_id=entity_id, room=dict(t.room), zones={z: dict(t.zone) for z in zone_ids},
                           template=self.id, **kw)


def _t(id_: str, name: str, room: tuple[int | None, ...], zone: tuple[int | None, ...]) -> Template:
    return Template(id_, name, dict(zip(LEVEL_KEYS, room, strict=True)), dict(zip(LEVEL_KEYS, zone, strict=True)),
                    builtin=True)


# Starting points only: every value is editable. Order: No light, Low light, Bright.
BUILTIN_TEMPLATES: tuple[Template, ...] = (
    _t("general", "General light", (40, 25, 0), (70, 50, 0)),
    _t("floor", "Floor light", (15, 15, 0), (65, 45, 80)),
    _t("task", "Task / desk lamp", (0, 0, 0), (80, 70, 50)),
    _t("ceiling", "Ceiling light", (60, 40, 0), (90, 70, 0)),
    _t("accent", "Accent / LED strip", (30, 20, 0), (30, 20, 0)),
)
DEFAULT_TEMPLATE = "general"


def templates_from(data: Any) -> tuple[Template, ...]:
    out: list[Template] = []
    builtin = {t.id for t in BUILTIN_TEMPLATES}
    for item in data if isinstance(data, (list, tuple)) else []:
        template = Template.from_dict(item)
        if template is not None and template.id not in builtin and all(t.id != template.id for t in out):
            out.append(template)
    return tuple(out[:MAX_TEMPLATES])


def find_template(user: Iterable[Template], template_id: str | None) -> Template | None:
    return next((t for t in (*BUILTIN_TEMPLATES, *user) if t.id == template_id), None)


# ------------------------------------------------------------------ decision
@dataclass(slots=True, frozen=True)
class ColumnClaim:
    """An active zone that lists the light (from the zone arbitration, the winner first)."""

    zone_id: str
    name: str
    priority: int
    order: int


@dataclass(slots=True)
class MatrixDecision:
    """Which matrix cell decides an enrolled light now, and what it superseded."""

    entity_id: str
    column: str                 # "room" or a zone id
    column_name: str            # "Room occupied" or "Desk zone"
    level: str
    value: int                  # 0 = Off
    superseded: list[str] = field(default_factory=list)
    explanation: str = ""
    level_name: str = ""
    onoff: bool = False         # an on/off-only target (relay, Dumb Light): any value above 0 is On

    @property
    def off(self) -> bool:
        return self.value == 0

    @property
    def zone(self) -> str | None:
        return None if self.column == ROOM else self.column

    def as_dict(self) -> dict[str, Any]:
        return {"column": self.column, "column_name": self.column_name, "level": self.level,
                "level_name": self.level_name or self.level, "value": self.value,
                "text": describe_value(self.value, onoff=self.onoff), "onoff": self.onoff,
                "superseded": list(self.superseded), "explanation": self.explanation}


def _why_wins(winner: ColumnClaim, other: ColumnClaim) -> str:
    if winner.priority > other.priority:
        return f"{winner.name} zone wins over {other.name} zone due to higher priority"
    return f"{winner.name} zone wins over {other.name} zone (same priority; listed first)"


def decide(matrix: LightMatrix, level: str, claims: list[ColumnClaim], level_name: str | None = None,
           ) -> MatrixDecision | None:
    """The cell that decides the light now (the room is occupied); None leaves it to its previous rules.

    ``claims`` are the active zones that list the light, ordered by the zone arbitration (winner first).
    """
    if not matrix.enabled:
        return None
    name = level_name or level
    # A zone without a value for this category does not claim the brightness.
    candidates = [(c, v) for c in claims if (v := matrix.value(c.zone_id, level)) is not None]
    unset = [c for c in claims if matrix.value(c.zone_id, level) is None]
    if candidates:
        (winner, value), others = candidates[0], candidates[1:]
        superseded = [f"{c.name} zone" for c, _v in others] + ["Room occupied"]
        reasons = [_why_wins(winner, c) for c, _v in others]
        reasons += [f"{c.name} zone has no value for {name}" for c in unset]
        column = f"{winner.name} zone"
        text = "; ".join(reasons) if reasons else f"{column} is active"
        return MatrixDecision(matrix.entity_id, winner.zone_id, column, level, value, superseded, text, name)
    value = matrix.room.get(level)
    if value is None:
        return None
    return MatrixDecision(matrix.entity_id, ROOM, "Room occupied", level, value,
                          explanation="no active zone of this light" if not claims else
                          f"{', '.join(c.name for c in claims)} zone has no value for {name}: "
                          "the Room occupied value applies", level_name=name)


# ------------------------------------------------------------------- setting
def appearance_of(mood: ResolvedSetting | None) -> dict[str, Any]:
    """The mood's colour, white colour temperature and effect (no brightness, no action)."""
    if mood is None or mood.action != LightAction.ON:
        return {}
    return {f: getattr(mood, f) for f in APPEARANCE_FIELDS if getattr(mood, f) is not None}


def merged_setting(decision: MatrixDecision, mood: ResolvedSetting | None, *, transition: float | None,
                   ) -> ResolvedSetting:
    """The one effective setting: on/off and brightness from the matrix, appearance from the mood.

    ``mood`` is the light's resolved Occupied profile with the global values filled in and its action
    taken as On (so an Off or Ignore mood still lends its colour, when it has one).
    """
    from .profiles import ResolvedSetting

    source = {"level": "brightness", "rule": decision.column, "ambient": decision.level}
    if decision.off:
        return ResolvedSetting(action=LightAction.OFF, transition=transition, sources={"action": source})
    look = appearance_of(mood)
    out = ResolvedSetting(action=LightAction.ON, brightness_pct=decision.value, brightness_mode=AdjustMode.FIXED,
                          transition=transition if transition is not None else (mood.transition if mood else None),
                          # Switching on is decided by the ambient category itself (not the daylight gate).
                          daylight=DaylightCondition.ALWAYS, **look)
    out.sources = {"action": source, "brightness_pct": source}
    for f in look:
        if mood is not None and f in mood.sources:
            out.sources[f] = dict(mood.sources[f])
    if mood is not None and mood.adaptive:
        # The global colour temperature still follows the mood; the global brightness is not used.
        kept = {k: dict(v) for k, v in mood.adaptive.items() if k != "brightness"}
        out.adaptive = kept or None
    if out.adaptive is None:
        out.adaptive = {"brightness": {"mode": "fixed", "status": "fixed", "value": decision.value}}
    else:
        out.adaptive["brightness"] = {"mode": "fixed", "status": "fixed", "value": decision.value}
    return out


def describe_appearance(mood: ResolvedSetting | None, mood_name: str | None) -> str:
    look = appearance_of(mood)
    if not look:
        return "colour left as it is" + (f" ({mood_name} sets no colour for it)" if mood_name else "")
    parts = []
    if look.get("rgb_color"):
        parts.append("RGB " + ",".join(str(c) for c in look["rgb_color"]))
    elif look.get("hs_color"):
        parts.append("HS " + ",".join(f"{c:g}" for c in look["hs_color"]))
    elif look.get("color_temp_kelvin"):
        parts.append(f"{look['color_temp_kelvin']} K")
    if look.get("effect"):
        parts.append(f"effect {look['effect']}")
    detail = ", ".join(parts)
    if not mood_name:
        return detail
    return f"colour from {mood_name}" + (f" ({detail})" if detail else "")


def conflicts(*, mood_name: str | None, mood: ResolvedSetting | None, zone_targets: list[tuple[str, Any]],
              adaptive_brightness: bool) -> list[str]:
    """What changes for a light when it is enrolled (shown before enrolling)."""
    out: list[str] = []
    if mood_name and mood is not None:
        if mood.action == LightAction.OFF:
            out.append(f"{mood_name} switches this light off; with brightness rules it follows the matrix while the "
                       "room is occupied (On or Off), and has no colour from that mood.")
        elif mood.action == LightAction.IGNORE:
            out.append(f"{mood_name} does not control this light; with brightness rules it is switched on and off by "
                       "the matrix while the room is occupied.")
        elif mood.brightness_pct is not None and mood.brightness_mode not in (AdjustMode.GLOBAL, AdjustMode.OFFSET):
            out.append(f"{mood_name} sets {mood.brightness_pct} % brightness; the matrix brightness replaces it "
                       "(the colour stays the mood's).")
        if mood.is_persistent:
            out.append(f"In {mood_name} this light is ambient (on regardless of occupancy): while the room is "
                       "occupied the matrix decides it; while it is vacant it stays ambient.")
    if adaptive_brightness:
        out.append("The global brightness adjustment no longer changes this light's brightness (its colour "
                   "temperature still follows it when the mood says so).")
    for zone_name, target in zone_targets:
        what = getattr(target, "describe", lambda: "")()
        out.append(f"The {zone_name} zone sets it {what}; with brightness rules the zone column of the matrix is "
                   "used instead (the zone still chooses when it applies).")
    return out


def with_matrix(matrices: Mapping[str, LightMatrix], matrix: LightMatrix | None, entity_id: str,
                ) -> dict[str, LightMatrix]:
    out = dict(matrices)
    if matrix is None:
        out.pop(entity_id, None)
    else:
        out[entity_id] = replace(matrix, entity_id=entity_id)
    return out
