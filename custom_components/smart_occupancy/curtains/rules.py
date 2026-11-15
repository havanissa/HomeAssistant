"""Settings of the automatic curtain rules (0.6.2, the user's Phase 4.6.1).

Every setting is one entry in ``RULES``: its type, default, limits, group and
the scopes it can be set at. A value is resolved **window → room → all
windows → built-in default**: the first scope that sets it wins, so a room or
a single window overrides only what differs. Window-only settings (the
window lamp) exist only on the window.

The card renders its settings forms from this table (``schema()``), and the
websocket validates every change with ``validate()``; nothing is edited as
YAML.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
import re
from typing import Any

from .config import CurtainConfigError

GLOBAL, ROOM, WINDOW = "global", "room", "window"
ALL_SCOPES = (GLOBAL, ROOM, WINDOW)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LAMP_BYPASS = ("sleep", "night", "evening_privacy", "sun")
MANUAL_BYPASS = ("sleep", "night", "evening_privacy")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class Rule:
    key: str
    kind: str                       # bool, int, float, time, choice, entity, days, set
    default: Any
    group: str
    label: str
    hint: str = ""
    low: float | None = None
    high: float | None = None
    unit: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    domains: tuple[str, ...] = ()   # entity: allowed domains
    scopes: tuple[str, ...] = ALL_SCOPES

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "default": _jsonable(self.default), "group": self.group,
                "label": self.label, "hint": self.hint, "low": self.low, "high": self.high, "unit": self.unit,
                "choices": [list(c) for c in self.choices], "domains": list(self.domains),
                "scopes": list(self.scopes)}


def _jsonable(value: Any) -> Any:
    return list(value) if isinstance(value, (tuple, frozenset, set)) else value


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("sleep", "Sleep mode", "Keeps sleep-protected windows closed while someone sleeps and decides what happens after."),
    ("lamp", "Window lamp", "A lamp in the window holds its own preset while it is on."),
    ("daylight", "Daylight", "Measured outdoor light decides when a window may open; the clock never does."),
    ("schedule", "Night and morning", "The household night, the soft morning and when the day position starts."),
    ("sun", "Strong sunshine", "Sun protection needs the sun in front of the window and measured strong light."),
    ("manual", "Manual changes", "How long a manual move is kept before automatic control takes over again."),
    ("movement", "Movement safety", "Limits that keep covers from moving too often."),
)

RULES: tuple[Rule, ...] = (
    # ---------------------------------------------------------------- sleep
    Rule("sleep_entity", "entity", "input_boolean.sleep", "sleep", "Sleep source",
         "An input boolean or binary sensor that is on while someone sleeps", domains=("input_boolean", "binary_sensor")),
    Rule("sleep_end", "choice", "morning", "sleep", "When sleep ends",
         choices=(("morning", "Start the morning once there is daylight"),
                  ("resume", "Go straight to the current condition"),
                  ("hold", "Keep the sleep position until Resume or a manual move"))),
    Rule("sleep_require_daylight", "bool", True, "sleep", "Opening after sleep needs daylight",
         "Off: after sleep the morning starts at once, also in the dark"),
    # ----------------------------------------------------------------- lamp
    Rule("lamp_enabled", "bool", False, "lamp", "Window lamp override", scopes=(WINDOW,)),
    Rule("lamp_entity", "entity", None, "lamp", "Lamp", "The light in or at this window", domains=("light",),
         scopes=(WINDOW,)),
    Rule("lamp_release", "int", 120, "lamp", "Release delay", "After the lamp switches off", 0, 3600, "s"),
    Rule("lamp_bypass", "set", ("sleep", "night", "evening_privacy"), "lamp", "These conditions win over the lamp",
         "Their own preset applies while the lamp stays on",
         choices=(("sleep", "Sleep"), ("night", "Night"), ("evening_privacy", "Evening privacy"),
                  ("sun", "Sun protection"))),
    # ------------------------------------------------------------- daylight
    Rule("lux_entity", "entity", None, "daylight", "Outdoor light sensor", "An outdoor illuminance sensor (lx)",
         domains=("sensor",)),
    Rule("daylight_lux", "int", 150, "daylight", "Daylight from", "Outdoor light for opening", 1, 20000, "lx"),
    Rule("dusk_lux", "int", 50, "daylight", "Dark below", "Outdoor light that counts as dusk", 0, 20000, "lx"),
    Rule("daylight_stable", "int", 10, "daylight", "Stable for", "Before daylight or dusk counts", 0, 120, "min"),
    Rule("daylight_elevation", "float", -2.0, "daylight", "Sun at least", "Daylight also needs this elevation",
         -12, 20, "°"),
    Rule("dusk_elevation", "float", -4.0, "daylight", "Dark when the sun is below",
         "Works without a light sensor too", -18, 10, "°"),
    Rule("lux_stale", "int", 30, "daylight", "Sensor stale after", "No report for this long: no opening", 5, 720,
         "min"),
    Rule("fallback", "choice", "none", "daylight", "Without a light sensor",
         choices=(("none", "Never open automatically"), ("sun", "Open when the sun is clearly up"))),
    Rule("fallback_elevation", "float", 3.0, "daylight", "Sun clearly up at", "Only used by the fallback", -6, 30,
         "°"),
    # ------------------------------------------------------------- schedule
    Rule("night_enabled", "bool", True, "schedule", "Night", "Closes for the household night"),
    Rule("night_start_workday", "time", "22:30", "schedule", "Night starts (before a workday)"),
    Rule("night_end_workday", "time", "06:30", "schedule", "Night ends (workday)",
         "The earliest morning; opening still needs daylight"),
    Rule("night_start_free", "time", "23:30", "schedule", "Night starts (before a free day)"),
    Rule("night_end_free", "time", "08:00", "schedule", "Night ends (free day)"),
    Rule("free_days", "days", (5, 6), "schedule", "Free days", "Weekend days"),
    Rule("morning_mode", "choice", "duration", "schedule", "Morning lasts",
         choices=(("duration", "A number of minutes"), ("until_time", "Until a time of day"))),
    Rule("morning_minutes", "int", 120, "schedule", "Morning length", low=0, high=720, unit="min"),
    Rule("day_from_workday", "time", "10:00", "schedule", "Day from (workday)", "When the morning lasts until a time"),
    Rule("day_from_free", "time", "11:00", "schedule", "Day from (free day)"),
    # ------------------------------------------------------------------ sun
    Rule("sun_lux", "int", 12000, "sun", "Strong sunshine from", low=100, high=200000, unit="lx"),
    Rule("sun_release_lux", "int", 8000, "sun", "Released below", low=0, high=200000, unit="lx"),
    Rule("sun_stable", "int", 10, "sun", "Strong for", "Before sun protection starts", 0, 120, "min"),
    Rule("sun_release", "int", 20, "sun", "Weaker for", "Before sun protection ends", 0, 240, "min"),
    # --------------------------------------------------------------- manual
    Rule("manual_policy", "choice", "next_phase", "manual", "Keep a manual move",
         choices=(("next_phase", "Until the next automatic phase"), ("duration", "For a number of minutes"),
                  ("vacant", "Until the room has been empty for a while"), ("until_released", "Until I resume"))),
    Rule("manual_minutes", "int", 120, "manual", "Minutes", "For “a number of minutes”", 5, 1440, "min"),
    Rule("vacant_minutes", "int", 30, "manual", "Room empty for", "For “until the room has been empty”", 1, 720,
         "min"),
    Rule("manual_bypass", "set", (), "manual", "These conditions end a manual hold",
         "Only when they start after the manual move",
         choices=(("sleep", "Sleep"), ("night", "Night"), ("evening_privacy", "Evening privacy"))),
    # ------------------------------------------------------------- movement
    Rule("min_interval", "int", 10, "movement", "Minimum time between moves",
         "Sleep, night, privacy and the lamp are not delayed", 0, 240, "min"),
    Rule("max_moves", "int", 12, "movement", "Automatic moves per day",
         "Then only sleep and night still move", 1, 100),
    Rule("max_retries", "int", 2, "movement", "Retries after a failed move", low=0, high=5),
)
BY_KEY: dict[str, Rule] = {r.key: r for r in RULES}


def defaults() -> dict[str, Any]:
    return {r.key: r.default for r in RULES}


def schema() -> list[dict[str, Any]]:
    return [r.as_dict() for r in RULES]


def _bad(rule: Rule, text: str) -> CurtainConfigError:
    return CurtainConfigError(f"invalid_{rule.key}", f"{rule.label}: {text}")


def validate(key: str, value: Any, scope: str) -> Any:
    """A stored value for ``key``; ``None`` removes the setting at that scope (it is inherited again)."""
    rule = BY_KEY.get(key)
    if rule is None:
        raise CurtainConfigError("unknown_setting", f"Unknown setting {key}")
    if scope not in rule.scopes:
        raise CurtainConfigError("wrong_scope", f"{rule.label} can only be set for a window")
    if value is None:
        return None
    kind = rule.kind
    if kind == "bool":
        if not isinstance(value, bool):
            raise _bad(rule, "must be on or off")
        return value
    if kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise _bad(rule, "must be a number")
        if kind == "int" and value != int(value):
            raise _bad(rule, "must be a whole number")
        if not rule.low <= value <= rule.high:
            raise _bad(rule, f"must be {rule.low:g} to {rule.high:g}{(' ' + rule.unit) if rule.unit else ''}")
        return int(value) if kind == "int" else round(float(value), 1)
    if kind == "time":
        if not isinstance(value, str) or not _TIME.match(value.strip()):
            raise _bad(rule, "must be a time like 22:30")
        return value.strip()
    if kind == "choice":
        if value not in {c for c, _ in rule.choices}:
            raise _bad(rule, "is not one of the choices")
        return value
    if kind == "entity":
        if value == "":
            return ""  # explicitly "none" (e.g. no light sensor for this room although all windows have one)
        if not isinstance(value, str) or "." not in value or value.split(".", 1)[0] not in rule.domains:
            raise _bad(rule, f"choose a {' or '.join(rule.domains)} entity")
        return value
    if kind == "days":
        if not isinstance(value, (list, tuple)) or any(isinstance(d, bool) or d not in range(7) for d in value):
            raise _bad(rule, "choose weekdays")
        return tuple(sorted(set(value)))
    if kind == "set":
        allowed = {c for c, _ in rule.choices}
        if not isinstance(value, (list, tuple)) or any(v not in allowed for v in value):
            raise _bad(rule, "is not one of the choices")
        return tuple(c for c, _ in rule.choices if c in value)
    raise _bad(rule, "unsupported")  # pragma: no cover


def load(raw: Any, scope: str) -> dict[str, Any]:
    """Stored overrides; anything unknown or invalid is dropped (it is inherited instead, never guessed)."""
    out: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return out
    for key, value in raw.items():
        try:
            clean = validate(key, value, scope)
        except CurtainConfigError:
            continue
        if clean is not None:
            out[key] = clean
    return out


def dump(values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in values.items()}


def resolve(global_values: Mapping[str, Any], room_values: Mapping[str, Any] | None,
            window_values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Effective settings of a window and where each one comes from (window, room, global or default)."""
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for rule in RULES:
        for scope, layer in ((WINDOW, window_values), (ROOM, room_values or {}), (GLOBAL, global_values)):
            if scope in rule.scopes and rule.key in layer:
                values[rule.key], sources[rule.key] = layer[rule.key], scope
                break
        else:
            values[rule.key], sources[rule.key] = rule.default, "default"
        if values[rule.key] == "":
            values[rule.key] = None  # an explicit "none" entity
    return values, sources


def consistency(values: Mapping[str, Any]) -> list[str]:
    """Combinations that do not make sense; shown as warnings and refused when saved."""
    problems = []
    if values["dusk_lux"] >= values["daylight_lux"]:
        problems.append("“Dark below” must be lower than “Daylight from”")
    if values["sun_release_lux"] >= values["sun_lux"]:
        problems.append("“Released below” must be lower than “Strong sunshine from”")
    if values["dusk_elevation"] > values["daylight_elevation"]:
        problems.append("The dark elevation must not be above the daylight elevation")
    return problems
