"""Changing ambient light categories (0.6.8, Phase 4.8): preview, review and apply.

Pure logic. A category change is one of:

* the **global** categories (``area_id`` None): every room that has no
  categories of its own follows them; rooms with their own categories are
  listed as unaffected (they are never overwritten silently);
* a **room's own** categories (a completely different structure is allowed);
* **Use Global Levels** for a room (``levels`` None): its own categories are
  dropped and it follows the global ones again.

What happens to the brightness matrices (keyed by category id):

* a **renamed** category keeps its id, so every value stays;
* an **added** category starts *Not configured* in every matrix (nothing is
  invented, no light is switched on by the change itself);
* a **removed** category whose values are configured anywhere needs a
  **resolution** before the change can be saved:
  ``{removed_id: target_id}`` moves its values to the target category
  (replacing the target's value in that cell) or ``{removed_id: None}``
  deletes them. The preview lists every affected cell first. A removed
  fallback category is replaced by the resolution's target, else by the
  second category (shown in the preview).

Saving never sends a light command: the committed category is classified
again from the current evidence (``brightness_runtime.py``) and the new
values are used at the next trigger.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from .ambient import DEFAULT_LEVELS, AmbientLevel, AmbientSettings
from .brightness import ROOM, describe_value

if TYPE_CHECKING:
    from .lighting_config import LightingConfig


def fmt_range(levels: tuple[AmbientLevel, ...], i: int) -> str:
    """"5 to below 20 lx", "below 5 lx", "20 lx and above"."""
    lv = levels[i]
    if i == len(levels) - 1:
        return f"{lv.start} lx and above"
    upper = levels[i + 1].start
    return f"below {upper} lx" if lv.start == 0 else f"{lv.start} to below {upper} lx"


def ranges(levels: tuple[AmbientLevel, ...]) -> list[dict[str, Any]]:
    return [{"id": lv.id, "name": lv.name, "start": lv.start,
             "below": levels[i + 1].start if i < len(levels) - 1 else None, "range": fmt_range(levels, i)}
            for i, lv in enumerate(levels)]


@dataclass(slots=True)
class RoomImpact:
    """What a category change means for one room."""

    area_id: str
    old: tuple[AmbientLevel, ...]
    new: tuple[AmbientLevel, ...]
    source: str                                   # "global" | "room" (after the change)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str, str]] = field(default_factory=list)   # (id, old name, new name)
    moved: list[tuple[str, int, int]] = field(default_factory=list)     # (id, old start, new start)
    cells: list[dict[str, Any]] = field(default_factory=list)          # configured cells of removed categories
    fallback: dict[str, Any] | None = None        # the room's fallback category was removed: what replaces it
    unresolved: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed or self.renamed or self.moved)


@dataclass(slots=True)
class LevelChange:
    """A planned category change (see the module docstring)."""

    scope: str                                    # "global" | room area id
    levels: tuple[AmbientLevel, ...] | None       # the new definitions (None: Use Global Levels)
    resolve: dict[str, str | None]
    rooms: list[RoomImpact] = field(default_factory=list)
    unaffected: list[str] = field(default_factory=list)   # rooms that keep their own categories
    errors: list[str] = field(default_factory=list)

    @property
    def unresolved(self) -> list[str]:
        return sorted({r for room in self.rooms for r in room.unresolved})

    @property
    def ready(self) -> bool:
        return not self.errors and not self.unresolved

    def as_dict(self, names: Callable[[str], str] | None = None,
                light_names: Callable[[str], str] | None = None) -> dict[str, Any]:
        names = names or (lambda a: a)
        light_names = light_names or (lambda e: e)
        rooms = []
        for r in self.rooms:
            old = {lv.id: lv for lv in r.old}
            new = {lv.id: lv for lv in r.new}
            rooms.append({
                "area_id": r.area_id, "name": names(r.area_id), "source": r.source, "changed": r.changed,
                "before": ranges(r.old), "after": ranges(r.new),
                "added": [new[i].name for i in r.added],
                "removed": [{"id": i, "name": old[i].name, "to": self.resolve.get(i, "unset"),
                             "to_name": new[t].name if (t := self.resolve.get(i)) in new else None}
                            for i in r.removed],
                "renamed": [{"id": i, "from": a, "to": b} for i, a, b in r.renamed],
                "moved": [{"id": i, "name": new[i].name, "from": a, "to": b} for i, a, b in r.moved],
                "cells": [{**c, "light": light_names(c["entity_id"]), "level_name": old[c["level"]].name,
                           "text": describe_value(c["value"])} for c in r.cells],
                "fallback": r.fallback, "unresolved": [old[i].name for i in r.unresolved],
            })
        return {"scope": self.scope, "levels": [lv.as_dict() for lv in self.levels] if self.levels else None,
                "resolve": dict(self.resolve), "rooms": rooms, "unaffected": [names(a) for a in self.unaffected],
                "unresolved": self.unresolved, "errors": list(self.errors), "ready": self.ready}


def _impact(area_id: str, lighting: LightingConfig, old: tuple[AmbientLevel, ...], new: tuple[AmbientLevel, ...],
            source: str, resolve: Mapping[str, str | None], fallback_level: str | None) -> RoomImpact:
    old_by = {lv.id: lv for lv in old}
    new_by = {lv.id: lv for lv in new}
    impact = RoomImpact(area_id, old, new, source)
    impact.added = [lv.id for lv in new if lv.id not in old_by]
    impact.removed = [lv.id for lv in old if lv.id not in new_by]
    impact.renamed = [(i, old_by[i].name, new_by[i].name) for i in old_by if i in new_by
                      and old_by[i].name != new_by[i].name]
    impact.moved = [(i, old_by[i].start, new_by[i].start) for i in old_by if i in new_by
                    and old_by[i].start != new_by[i].start]
    room = lighting.room(area_id)
    for entity_id, matrix in sorted(room.brightness.items()):
        for level in impact.removed:
            for column, value in matrix.configured_cells(level).items():
                zone = room.zone(column) if column != ROOM else None
                impact.cells.append({"entity_id": entity_id, "column": column, "level": level, "value": value,
                                     "column_name": "Room occupied" if column == ROOM else
                                     f"{zone.name if zone else column} zone"})
    used = {c["level"] for c in impact.cells}
    if fallback_level in impact.removed:
        # (Shown in the preview; replaced by the resolution's target, else the second category. No review needed.)
        to = resolve.get(fallback_level)
        keys = [lv.id for lv in new]
        replacement = to if to in new_by else keys[min(1, len(keys) - 1)]
        impact.fallback = {"from": old_by[fallback_level].name, "to": new_by[replacement].name, "id": replacement}
    impact.unresolved = [i for i in impact.removed if i in used and i not in resolve]
    return impact


def plan(lighting: LightingConfig, area_ids: Iterable[str], area_id: str | None,
         levels: tuple[AmbientLevel, ...] | None, resolve: Mapping[str, str | None] | None = None) -> LevelChange:
    """Plan a category change (global with ``area_id`` None; a room's own; ``levels`` None: Use Global Levels)."""
    resolve = dict(resolve or {})
    change = LevelChange(scope=area_id or "global", levels=levels, resolve=resolve)
    glob = lighting.ambient
    if area_id is None and levels is None:
        change.errors.append("The global categories need a definition")
        return change
    rooms = sorted({*area_ids, *lighting.rooms})
    if area_id is None:
        old_global = glob.levels or DEFAULT_LEVELS
        for a in rooms:
            own = lighting.room(a).ambient.settings
            if own.levels is not None:
                change.unaffected.append(a)
                continue
            fallback = own.fallback_level or glob.fallback_level
            change.rooms.append(_impact(a, lighting, old_global, levels, "global", resolve, fallback))
    else:
        own = lighting.room(area_id).ambient.settings
        old = lighting.ambient_for(area_id).levels or DEFAULT_LEVELS
        new = levels if levels is not None else glob.levels or DEFAULT_LEVELS
        fallback = own.fallback_level or glob.fallback_level
        change.rooms.append(_impact(area_id, lighting, old, new, "room" if levels is not None else "global",
                                    resolve, fallback))
    for removed, target in resolve.items():
        for r in change.rooms:
            if removed in r.removed and target is not None and target not in {lv.id for lv in r.new}:
                change.errors.append(f"{removed} cannot move to {target}: no such category after the change")
    return change


def apply(lighting: LightingConfig, change: LevelChange) -> LightingConfig:
    """The configuration after a ready change (matrices remapped, fallbacks replaced)."""
    if not change.ready:
        raise ValueError("The change needs a resolution for every removed category first")
    out = lighting
    if change.scope == "global":
        fallback = lighting.ambient.fallback_level
        if fallback not in {lv.id for lv in change.levels or ()}:
            keys = [lv.id for lv in change.levels or DEFAULT_LEVELS]
            to = change.resolve.get(fallback or "")
            fallback = to if to in keys else keys[min(1, len(keys) - 1)]
        out = replace(out, ambient=replace(out.ambient, levels=change.levels, fallback_level=fallback))
    for impact in change.rooms:
        room = out.room(impact.area_id)
        settings: AmbientSettings = room.ambient.settings
        if change.scope == impact.area_id:
            settings = replace(settings, levels=change.levels)
        if impact.fallback and settings.fallback_level is not None:
            settings = replace(settings, fallback_level=impact.fallback["id"])
        moves = {r: change.resolve.get(r) for r in impact.removed}
        matrices = {e: m.remapped(moves) for e, m in room.brightness.items()} if moves else room.brightness
        updated = replace(room, ambient=replace(room.ambient, settings=settings), brightness=matrices)
        if updated != room:
            out = out.with_room(impact.area_id, updated)
    return out
