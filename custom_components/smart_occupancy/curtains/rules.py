"""Settings of the automatic curtain rules (0.6.2, the user's Phase 4.6.1; environment, solar and
thermal settings 0.6.3, Phase 4.6.2).

Every setting is one entry in ``RULES``: its type, default, limits, group and
the scopes it can be set at. A value is resolved **window → room → all
windows → built-in default**: the first scope that sets it wins, so a room or
a single window overrides only what differs. Window-only settings (the
window lamp) exist only on the window.

The card renders its settings forms from this table (``schema()``), and the
websocket validates every change with ``validate()``; nothing is edited as
YAML.

0.6.3 adds the environment sensors (outdoor / indoor light, room and outdoor
temperature, weather and cloud cover), how their validity is judged, and the
optional temperature-aware and glare behaviour. Every new behaviour is **off**
by default (``thermal_enabled``, ``glare_sensitive``, ``sun_evidence``), so a
window configured in 0.6.2 decides exactly as before; the only new default that
is on (``lux_stuck``) can only keep a window where it is.

Times are always 24-hour ``HH:mm`` (00:00-23:59).
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
    kind: str                       # bool, int, float, time, choice, entity, days, set, dayplan
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
    step: float | None = None       # number fields (the card uses 1, or 0.5 for floats, when unset)
    # 0.6.3: switching this on (or choosing a value other than the default) shows the predicted result
    # (``auto/simulate``) before it is saved.
    confirm: bool = False
    entity_list: str | None = None  # which entity suggestions the card offers

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "default": _jsonable(self.default), "group": self.group,
                "label": self.label, "hint": self.hint, "low": self.low, "high": self.high, "unit": self.unit,
                "choices": [list(c) for c in self.choices], "domains": list(self.domains),
                "scopes": list(self.scopes), "step": self.step, "confirm": self.confirm,
                "entity_list": self.entity_list}


def _jsonable(value: Any) -> Any:
    if isinstance(value, tuple) and value and all(isinstance(x, tuple) and len(x) == 2 and isinstance(x[1], tuple)
                                                  for x in value):
        return {str(d): dict(t) for d, t in value}   # a day plan
    return list(value) if isinstance(value, (tuple, frozenset, set)) else value


GROUPS: tuple[tuple[str, str, str], ...] = (
    ("sleep", "Sleep mode", "Keeps sleep-protected windows closed while someone sleeps and decides what happens after."),
    ("lamp", "Window lamp", "A lamp in the window holds its own preset while it is on."),
    ("daylight", "Daylight", "Measured outdoor light decides when a window may open; the clock never does."),
    ("schedule", "Schedule", "When Morning, Daytime, Evening and Night begin. A phase only makes a mode eligible: "
     "sleep, missing daylight and every other protection still apply."),
    ("sun", "Strong sunshine", "Sun protection needs the sun in front of the window and measured strong light."),
    ("manual", "Manual changes", "How long a manual move is kept before automatic control takes over again."),
    ("movement", "Movement safety", "Limits that keep covers from moving too often."),
    # 0.6.3 (shown in the window's Environment tab)
    ("environment", "Environment sensors",
     "Measurements for daylight, sunshine and temperature. Doubtful readings never open a window."),
    ("thermal", "Temperature and glare",
     "Optional. Decides whether direct sunshine is welcome warmth, unwanted heat or glare. Never controls heating."),
)
ENVIRONMENT_GROUPS = frozenset({"environment", "thermal"})
TEMPERATURE_DOMAINS = ("sensor", "climate", "weather")

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
    Rule("daylight_lux", "int", 150, "daylight", "Daylight from", "Outdoor light for opening", 1, 20000, "lx"),
    Rule("dusk_lux", "int", 50, "daylight", "Dark below", "Outdoor light that counts as dusk", 0, 20000, "lx"),
    Rule("daylight_stable", "int", 10, "daylight", "Stable for", "Before daylight or dusk counts", 0, 120, "min"),
    Rule("daylight_elevation", "float", -2.0, "daylight", "Sun at least", "Daylight also needs this elevation",
         -12, 20, "°"),
    Rule("dusk_elevation", "float", -4.0, "daylight", "Dark when the sun is below",
         "Works without a light sensor too", -18, 10, "°"),
    Rule("fallback", "choice", "none", "daylight", "Without a light sensor",
         choices=(("none", "Never open automatically"), ("sun", "Open when the sun is clearly up"))),
    Rule("fallback_elevation", "float", 3.0, "daylight", "Sun clearly up at", "Only used by the fallback", -6, 30,
         "°"),
    # ------------------------------------------------------------- schedule
    # 0.6.5 (Global Curtain Schedule): the keys of 0.6.2 are kept (stored values carry over); their labels
    # now say what they are in the phase model Morning → Daytime → Evening → Night. ``night_end_*`` is the
    # earliest Morning of that day, which is also when the night before it ends.
    Rule("free_days", "days", (5, 6), "schedule", "Free days",
         "Their Morning, Daytime and Evening use the free-day times (Mon–Sun)"),
    Rule("morning_enabled", "bool", True, "schedule", "Morning phase",
         "Off: the day starts directly with Daytime once there is daylight"),
    Rule("night_end_workday", "time", "06:30", "schedule", "Morning from (normal days)",
         "The earliest Morning; Night ends here. Opening still needs confirmed daylight and no sleep"),
    Rule("night_end_free", "time", "08:00", "schedule", "Morning from (free days)"),
    Rule("morning_mode", "choice", "duration", "schedule", "Daytime begins",
         choices=(("duration", "A number of minutes after Morning begins"), ("until_time", "At a time of day"),
                  ("not_before", "At a time of day, but not before Morning has lasted its minutes"))),
    Rule("morning_minutes", "int", 120, "schedule", "Morning lasts", low=0, high=720, unit="min"),
    Rule("day_from_workday", "time", "10:00", "schedule", "Daytime at (normal days)",
         "For “at a time of day”"),
    Rule("day_from_free", "time", "11:00", "schedule", "Daytime at (free days)"),
    Rule("evening_rule", "choice", "legacy", "schedule", "Evening begins",
         "A started Evening lasts until Night (except the 0.6.4 behaviour)",
         choices=(("legacy", "When it is dark after 12:00 (0.6.4 behaviour, not kept)"),
                  ("time", "At a time"), ("event", "At sunset or dusk"),
                  ("first", "At a time or at sunset / dusk, whichever comes first"),
                  ("last", "At a time or at sunset / dusk, whichever comes last")), confirm=True),
    Rule("evening_event", "choice", "dusk", "schedule", "Sunset or dusk",
         choices=(("dusk", "Measured dusk (outdoor light; the sun below the dark elevation without a sensor)"),
                  ("sunset", "Sunset (the sun below the horizon)"))),
    Rule("evening_time_workday", "time", "18:00", "schedule", "Evening at (normal days)", "12:00–23:59"),
    Rule("evening_time_free", "time", "18:00", "schedule", "Evening at (free days)"),
    Rule("night_enabled", "bool", True, "schedule", "Night", "Closes for the household night"),
    Rule("night_start_workday", "time", "22:30", "schedule", "Night from (normal evenings)",
         "A time after midnight belongs to the evening before"),
    Rule("night_start_free", "time", "23:30", "schedule", "Night from (late evenings)"),
    Rule("late_evenings", "choice", "before_free", "schedule", "Late evenings",
         choices=(("before_free", "Evenings before a free day"), ("custom", "Chosen evenings"))),
    Rule("late_days", "days", (4, 5), "schedule", "Chosen late evenings",
         "For “chosen evenings”: the evening of these days (e.g. Fri, Sat)"),
    Rule("day_overrides", "dayplan", (), "schedule", "Per-day times",
         "Optional times for single weekdays (Global Schedule → Advanced)", scopes=(GLOBAL,)),
    # ------------------------------------------------------------------ sun
    Rule("sun_lux", "int", 12000, "sun", "Strong sunshine from", low=100, high=200000, unit="lx"),
    Rule("sun_release_lux", "int", 8000, "sun", "Released below", low=0, high=200000, unit="lx"),
    Rule("sun_stable", "int", 10, "sun", "Strong for", "Before sun protection starts", 0, 120, "min"),
    Rule("sun_release", "int", 20, "sun", "Weaker for", "Before sun protection ends", 0, 240, "min"),
    Rule("sun_evidence", "choice", "measured", "sun", "Direct sunshine is confirmed by",
         "A forecast never overrides a working light sensor",
         choices=(("measured", "Measured outdoor light only"),
                  ("weather", "Without a light sensor: also a clear sky from the weather entity")), confirm=True),
    Rule("cloud_max", "int", 30, "sun", "Clear sky up to", "Cloud cover for the weather evidence", 0, 100, "%"),
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
    # ---------------------------------------------------------- environment (0.6.3)
    Rule("lux_entity", "entity", None, "environment", "Outdoor light sensor", "An outdoor illuminance sensor (lx)",
         domains=("sensor",), entity_list="lux"),
    Rule("lux_stale", "int", 30, "environment", "Light sensor stale after", "No report for this long: no opening",
         5, 720, "min"),
    Rule("lux_stuck", "int", 120, "environment", "Light sensor stuck after",
         "The same reading for this long (not a dark night): not trusted, nothing opens or shades. 0 = never", 0,
         1440, "min"),
    Rule("indoor_lux_entity", "entity", None, "environment", "Indoor light sensor",
         "Optional; lamps make it unreliable for daylight, so it is only used to confirm glare", domains=("sensor",),
         scopes=(ROOM, WINDOW), entity_list="lux"),
    Rule("room_temp_entity", "entity", None, "environment", "Room temperature",
         "A temperature sensor, or a thermostat (its current temperature)", domains=("sensor", "climate"),
         scopes=(ROOM, WINDOW), entity_list="temperature"),
    Rule("outdoor_temp_entity", "entity", None, "environment", "Outdoor temperature",
         "A temperature sensor or the weather entity", domains=TEMPERATURE_DOMAINS, entity_list="temperature"),
    Rule("weather_entity", "entity", None, "environment", "Weather", "Condition and cloud cover (supplementary)",
         domains=("weather",), entity_list="weather"),
    Rule("cloud_entity", "entity", None, "environment", "Cloud cover sensor",
         "Optional (%); otherwise the weather entity's cloud cover", domains=("sensor",), entity_list="cloud"),
    Rule("temp_stale", "int", 180, "environment", "Temperature stale after",
         "No report for this long: temperature is not used", 10, 1440, "min"),
    # ---------------------------------------------------------- thermal and glare (0.6.3)
    Rule("thermal_enabled", "bool", False, "thermal", "Temperature-aware sun",
         "Direct sunshine shades only when the room is warm; a cold room keeps the sun's warmth", confirm=True),
    Rule("preferred_temp", "float", 21.0, "thermal", "Preferred room temperature", "", 5, 35, "°C", step=0.5),
    Rule("comfort_band", "float", 1.5, "thermal", "Comfortable within ±", "Around the preferred temperature",
         0.5, 10, "°C", step=0.5),
    Rule("max_temp", "float", 25.0, "thermal", "Maximum acceptable", "Above this the Excess solar heat preset applies",
         10, 40, "°C", step=0.5),
    Rule("thermostat_entity", "entity", None, "thermal", "Use a thermostat's target",
         "Optional: its target temperature replaces the preferred temperature (it is never changed)",
         domains=("climate", "input_number"), scopes=(ROOM, WINDOW), entity_list="thermostat"),
    Rule("heat_gain", "choice", "welcome", "thermal", "Solar heat gain",
         choices=(("welcome", "Welcome it when the room is cold"), ("ignore", "Do not keep covers open for warmth"))),
    Rule("outdoor_heat_temp", "float", 18.0, "thermal", "Warm outside from",
         "Outdoor temperature at or above this: solar heating is not needed", -20, 40, "°C", step=0.5),
    Rule("temp_hysteresis", "float", 0.5, "thermal", "Temperature hysteresis", "Before a temperature state ends",
         0.1, 5, "°C", step=0.1),
    Rule("temp_stable", "int", 10, "thermal", "Temperature stable for", "Before a new temperature state counts", 0, 120,
         "min"),
    Rule("glare_sensitive", "bool", False, "thermal", "Glare-sensitive window",
         "A screen or a workplace: direct sunshine shades even when its warmth would be welcome", scopes=(WINDOW,),
         confirm=True),
    Rule("glare_when", "choice", "always", "thermal", "Glare protection",
         choices=(("always", "Whenever there is direct sunshine"),
                  ("occupied", "Only while the room is occupied"))),
    Rule("glare_indoor_lux", "int", 0, "thermal", "Glare also needs indoor light from",
         "Uses the indoor light sensor; 0 = not used", 0, 100000, "lx"),
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
    if kind == "dayplan":
        return _dayplan(rule, value)
    raise _bad(rule, "unsupported")  # pragma: no cover


DAYPLAN_KEYS = ("morning", "day", "evening", "night")


def _dayplan(rule: Rule, value: Any) -> tuple[tuple[int, tuple[tuple[str, str], ...]], ...]:
    """Per-weekday times: {weekday 0-6: {morning|day|evening|night: "HH:mm"}}. "night" is the Night start on the
    evening of that weekday; "morning" is that day's earliest Morning (and the end of the night before)."""
    if isinstance(value, (list, tuple)):
        value = {str(d): dict(t) for d, t in value}
    if not isinstance(value, Mapping):
        raise _bad(rule, "must be times per weekday")
    out = []
    for day, times in value.items():
        try:
            weekday = int(day)
        except (TypeError, ValueError) as err:
            raise _bad(rule, "unknown weekday") from err
        if isinstance(day, bool) or weekday not in range(7) or not isinstance(times, Mapping):
            raise _bad(rule, "unknown weekday")
        clean = []
        for key, text in times.items():
            if key not in DAYPLAN_KEYS:
                raise _bad(rule, f"unknown phase {key}")
            if text in (None, ""):
                continue
            if not isinstance(text, str) or not _TIME.match(text.strip()):
                raise _bad(rule, "times must be like 07:00 or 23:55")
            clean.append((key, text.strip()))
        if clean:
            out.append((weekday, tuple(sorted(clean))))
    return tuple(sorted(out))


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
    if values["max_temp"] <= values["preferred_temp"] + values["comfort_band"]:
        problems.append("“Maximum acceptable” must be above the comfortable range")
    if values["evening_rule"] != "legacy":
        for key in ("evening_time_workday", "evening_time_free"):
            if values[key] < "12:00":
                problems.append("Evening times must be 12:00–23:59")
                break
    return problems
