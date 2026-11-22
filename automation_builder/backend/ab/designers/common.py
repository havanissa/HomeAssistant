"""What all designers share: cells and their defaults, building blocks of generated rules, references, problems.

Cells and their four meanings (a cell is one entry of a matrix: one light or cover, one mood/zone/band/mode)

    absent                  Unset: nothing is configured here. An explicitly configured default applies (row
                            default first, then the matrix default); without one, nothing happens.
    {"kind": "default"}     Use Default: the user chose the default on purpose. Without a configured default this
                            is an error that blocks deployment (it is never read as "nothing" or "off").
    {"kind": "none"}        No Action: the user decided that nothing happens. No default applies.
    {"kind": "off"}, ...    An explicit command (Off and Closed are commands like any other).

A default is itself a cell (never "default"). Nothing here creates a default: an absent default is unset.

Generated rules are ordinary rules of the rule model (``model.validate_rule`` accepts them unchanged; this is
checked for every one) with a ``generated`` entry the compiler ignores: which designer and which cell they come
from, and keys that make two rules of one designer mutually exclusive by construction (two values of one mood).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from .. import model
from ..compiler import Input, plain, quote
from ..model import Problems

DESIGNER_ID = re.compile(r"^g_[0-9a-f]{8}$")
SHORT_ID = re.compile(r"^[a-z]_[0-9a-f]{4,8}$")
MAX_GENERATED = 300


def generated_rule_id(*parts: Any) -> str:
    """A stable rule id from where the rule comes from (designer, layer, target, column, value)."""
    return "r_" + hashlib.sha256("|".join(str(plain(p)) for p in parts).encode()).hexdigest()[:8]


class Refs:
    """Entities and numbers inside generated templates: written out, or (in a blueprint) the variable of an input."""

    def __init__(self) -> None:
        self.variables: dict[str, Input] = {}
        self.evaluators: dict[str, Any] = {}  # template text -> Python function of the states (simulation)

    def ref(self, value: Any) -> str:
        if isinstance(value, Input):
            self.variables[value.variable] = value
            return value.variable
        return quote(value)

    def num(self, value: Any) -> str:
        if isinstance(value, Input):
            self.variables[value.variable] = value
            return value.variable
        return f"{value}"

    def template(self, text: str, evaluator: Any = None) -> dict[str, Any]:
        if evaluator is not None:
            self.evaluators[text] = evaluator
        return {"type": "template", "value_template": text}


# ------------------------------------------------------------------ canonical building blocks
def t_state(entity_id: Any, to: Any = None, frm: Any = None, for_seconds: Any = 0, attribute: str | None = None) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "to": to, "from": frm, "for_seconds": for_seconds or 0,
            "attribute": attribute}


def t_numeric(entity_id: Any, above: Any = None, below: Any = None, for_seconds: Any = 0, attribute: str | None = None) -> dict[str, Any]:
    return {"type": "numeric", "entity_id": entity_id, "above": above, "below": below, "for_seconds": for_seconds or 0,
            "attribute": attribute}


def t_occupancy(entity_id: Any, to: str) -> dict[str, Any]:
    return {"type": "occupancy", "entity_id": entity_id, "to": to}


def t_template(text: str, for_seconds: Any = 0) -> dict[str, Any]:
    return {"type": "template", "value_template": text, "for_seconds": for_seconds or 0}


def t_time(at: Any, weekdays: list[str] | None = None) -> dict[str, Any]:
    return {"type": "time", "at": at, "weekdays": list(weekdays or [])}


def c_state(entity_id: Any, state: Any, negate: bool = False, for_seconds: Any = 0, attribute: str | None = None) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "state": state, "negate": negate, "for_seconds": for_seconds or 0,
            "attribute": attribute}


def c_numeric(entity_id: Any, above: Any = None, below: Any = None, attribute: str | None = None) -> dict[str, Any]:
    return {"type": "numeric", "entity_id": entity_id, "above": above, "below": below, "attribute": attribute}


def c_time(after: Any = None, before: Any = None, weekdays: list[str] | None = None) -> dict[str, Any]:
    return {"type": "time", "after": after, "before": before, "weekdays": list(weekdays or [])}


def group(kind: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": kind, "items": list(items)}


def any_of(items: list[dict[str, Any]]) -> dict[str, Any]:
    return items[0] if len(items) == 1 else group("or", items)


def negation(items: list[dict[str, Any]]) -> dict[str, Any]:
    """NOT (all of these). A single state condition is negated in place (the form the conflict check knows)."""
    if len(items) == 1 and items[0]["type"] == "state":
        return {**items[0], "negate": not items[0]["negate"]}
    if len(items) == 1:
        return group("not", items)
    return group("not", [group("and", items)])


def call(action: str, targets: list[Any], all_targets: list[Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    chosen = [t for t in all_targets if plain(t) in [plain(x) for x in targets]]
    target: Any = "all" if len(chosen) == len(all_targets) else chosen
    data = data or {}
    return {"type": "service", "action": action, "target": target, "data": {k: data[k] for k in sorted(data)}}


def rule(rule_id: str, name: str, triggers: list[dict[str, Any]], conditions: list[dict[str, Any]],
         actions: list[dict[str, Any]], meta: dict[str, Any], *, recheck: bool = False,
         override_entity: Any = None) -> dict[str, Any]:
    unique: list[dict[str, Any]] = []
    for trigger in triggers:
        if trigger not in unique:
            unique.append(trigger)
    return {"id": rule_id, "name": name[: model.MAX_NAME].strip(), "enabled": True, "triggers": unique,
            "conditions": group("and", conditions), "actions": actions, "retrigger": "restart",
            "recheck": {"enabled": recheck, "first_seconds": None, "second_seconds": None, "periodic_minutes": None,
                        "override_entity": override_entity if recheck else None},
            "generated": meta}


def checked(raw: dict[str, Any], targets: list[str], problems: list[dict[str, Any]], designer_id: str) -> dict[str, Any] | None:
    """The rule as the model accepts it. A generated rule the model does not accept is a defect of this App: it is
    reported (blocking) and left out, never deployed half-built."""
    p = Problems()
    meta = raw["generated"]
    clean = model.validate_rule({k: v for k, v in raw.items() if k != "generated"}, "generated", p, targets, generated=True)
    if p.items:
        problems.append(problem("blocking", "internal", f"Internal error in the generated rule “{raw['name']}”: "
                                f"{p.items[0]['path']}: {p.items[0]['message']}. Please report this.", designer_id))
        return None
    return {**clean, "generated": meta}


def problem(level: str, code: str, message: str, designer_id: str | None = None, layer: str | None = None,
            **extra: Any) -> dict[str, Any]:
    return {"level": level, "code": code, "message": message, "designer_id": designer_id, "layer": layer, **extra}


# ------------------------------------------------------------------ validation helpers
def text(raw: Any, path: str, p: Problems, *, limit: int = model.MAX_NAME, required: bool = False) -> str:
    return model._text(raw, path, p, limit=limit, required=required)  # noqa: SLF001


def number(raw: Any, path: str, p: Problems, low: float | None = None, high: float | None = None,
           *, integer: bool = False) -> Any:
    if raw is None or raw == "":
        return None
    value = model._number(raw, path, p)  # noqa: SLF001
    if value is None:
        return None
    if integer and value != int(value):
        p.add(path, "Must be a whole number")
        return None
    if (low is not None and value < low) or (high is not None and value > high):
        p.add(path, f"Must be between {low:g} and {high:g}" if low is not None and high is not None else "Out of range")
        return None
    return int(value) if integer else value


def seconds(raw: Any, path: str, p: Problems, high: int = model.MAX_SECONDS) -> int:
    if raw is None or raw == "":
        return 0
    return model._seconds(raw, path, p, minimum=0, maximum=high)  # noqa: SLF001


def entity(raw: Any, path: str, p: Problems, domains: tuple[str, ...] | None = None, what: str = "an entity") -> str | None:
    """An entity that may be left unset (None). Never guessed: unset stays unset."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, str) or not model.ENTITY_ID.match(raw):
        p.add(path, f"Choose {what}")
        return None
    if domains and raw.split(".", 1)[0] not in domains:
        p.add(path, f"Choose {what}")
        return None
    return raw


def flag(raw: Any) -> bool:
    return raw is True


def short_id(raw: Any, prefix: str, path: str, p: Problems) -> str:
    if not isinstance(raw, str) or not SHORT_ID.match(raw) or not raw.startswith(prefix + "_"):
        p.add(path, "Invalid identifier")
        return f"{prefix}_0000"
    return raw


def weekdays(raw: Any, path: str, p: Problems) -> list[str]:
    return model._weekdays(raw, path, p)  # noqa: SLF001


def time_of_day(raw: Any, path: str, p: Problems, required: bool = False) -> str | None:
    return model._time(raw, path, p, required=required)  # noqa: SLF001


# ------------------------------------------------------------------ cells and defaults
def resolve(cell: dict[str, Any] | None, row_default: dict[str, Any] | None,
            matrix_default: dict[str, Any] | None) -> dict[str, Any]:
    """``{"state": "explicit"|"default"|"none"|"unset"|"missing_default", "cell": cell|None, "source": ...}``.

    The order is fixed: the cell itself, then the light's (or cover's) own default, then the matrix default.
    """
    if cell is not None and cell["kind"] not in ("default", "none"):
        return {"state": "explicit", "cell": cell, "source": "cell"}
    if cell is not None and cell["kind"] == "none":
        return {"state": "none", "cell": cell, "source": "cell"}
    for source, fallback in (("row default", row_default), ("matrix default", matrix_default)):
        if fallback is not None:
            if fallback["kind"] == "none":
                return {"state": "none", "cell": fallback, "source": source, "inherited": True}
            return {"state": "default", "cell": fallback, "source": source, "inherited": True,
                    "explicit_choice": cell is not None}
    if cell is not None:  # "Use default" without any default
        return {"state": "missing_default", "cell": None, "source": None}
    return {"state": "unset", "cell": None, "source": None}


def acting(resolved: dict[str, Any]) -> dict[str, Any] | None:
    """The cell that issues something, when the resolution has one."""
    return resolved["cell"] if resolved["state"] in ("explicit", "default") else None


def cell_map(raw: Any, path: str, p: Problems, rows: list[str], columns: list[str], validate_cell: Any,
             kinds: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """``{row: {column: cell}}`` with only configured cells (absent = unset) and only known rows and columns."""
    out: dict[str, dict[str, Any]] = {}
    raw = raw if isinstance(raw, dict) else {}
    for row in rows:
        cells = raw.get(row)
        if not isinstance(cells, dict):
            continue
        kept = {}
        for column in columns:
            if column in cells and cells[column] is not None:
                kept[column] = validate_cell(cells[column], f"{path}.{row}.{column}", p, kinds)
        if kept:
            out[row] = kept
    for row, cells in raw.items():
        if row not in rows and isinstance(cells, dict) and cells:
            p.add(f"{path}.{row}", f"{row} is not one of this designer's rows: remove its cells first")
        elif isinstance(cells, dict):
            for column in cells:
                if column not in columns and cells[column] is not None:
                    p.add(f"{path}.{row}.{column}", "This column no longer exists: clear its cells first")
    return out


def row_defaults(raw: Any, path: str, p: Problems, rows: list[str], validate_cell: Any, kinds: tuple[str, ...]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for row in rows:
        if raw.get(row) is not None:
            out[row] = validate_cell(raw[row], f"{path}.{row}", p, tuple(k for k in kinds if k != "default"))
    return out


def default_cell(raw: Any, path: str, p: Problems, validate_cell: Any, kinds: tuple[str, ...]) -> dict[str, Any] | None:
    if raw is None:
        return None
    return validate_cell(raw, path, p, tuple(k for k in kinds if k != "default"))


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(plain(value)).lower()).strip("_")[:30] or "x"
