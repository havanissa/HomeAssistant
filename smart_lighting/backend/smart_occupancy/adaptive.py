"""Global light adjustment: following the existing adaptive lighting system.

Smart Occupancy does **not** calculate brightness or colour temperature from
the time of day or the sun. The user's existing system does that and
publishes its targets in Home Assistant entities. This module

* reads the two optional source entities (brightness, colour temperature),
  normalises their units (% / 0-255, K / mired) and remembers the last good
  value, so an unavailable source is never read as 0;
* applies a profile's adjustment modes (follow, follow + offset, fixed,
  ignore) to a resolved setting (``apply_global``);
* decides the gradual "adaptive" update of a light that is already on
  (``adaptive_update``): only meaningful changes, at most one command per
  light per interval, never switching a light on;
* finds parts of the existing system that already command lights directly
  (an Adaptive Lighting switch, or automations/scripts that use a source
  entity and control lights), so both never adjust the same light.

Nothing in this module sends a command.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    BRIGHTNESS_MAX,
    BRIGHTNESS_MIN,
    KELVIN_MAX,
    KELVIN_MIN,
    AdjustMode,
    LightAction,
    SourceScale,
)
from .lighting_config import AdaptiveConfig

if TYPE_CHECKING:
    from .conflicts import AutomationInfo
    from .planner import PlanItem, Target
    from .profiles import ResolvedSetting

_LOGGER = logging.getLogger(__name__)

BRIGHTNESS = "brightness"
COLOR_TEMP = "color_temp"

# Source status.
CONNECTED = "connected"            # a current, valid value
STALE = "stale"                    # source unusable now; the last good value is used
UNAVAILABLE = "unavailable"        # source unusable and no earlier value known
NOT_CONFIGURED = "not_configured"

FOLLOWING = (AdjustMode.GLOBAL, AdjustMode.OFFSET)
_COLOUR_MODES = frozenset({"hs", "xy", "rgb", "rgbw", "rgbww"})
_NO_EFFECT = (None, "", "none", "off", "None")
_MIRED_UNITS = frozenset({"mired", "mireds", "mk⁻¹", "mk-1"})
_KELVIN_UNITS = frozenset({"k", "kelvin", "°k"})


# ---------------------------------------------------------------- reading
@dataclass(slots=True, frozen=True)
class SourceReading:
    """One reading of a source entity, normalised (brightness %, colour temperature K)."""

    value: float | None
    ok: bool
    detail: str
    unit: str | None = None  # what was read: "%", "0-255", "K", "mired"


def _float(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def _unusable(state: State | None, entity_id: str) -> SourceReading | None:
    if state is None:
        return SourceReading(None, False, f"{entity_id} does not exist")
    if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return SourceReading(None, False, f"{entity_id} is {state.state}")
    return None


def read_brightness(state: State | None, entity_id: str, attribute: str | None = None,
                    scale: SourceScale = SourceScale.AUTO) -> SourceReading:
    """Brightness in % (0-100) from a sensor, number, input_number, light or Adaptive Lighting switch."""
    if bad := _unusable(state, entity_id):
        return bad
    assert state is not None
    attrs = state.attributes
    domain = entity_id.split(".", 1)[0]
    implied: SourceScale | None = None
    if attribute:
        raw: Any = attrs.get(attribute)
        if raw is None:
            return SourceReading(None, False, f"{entity_id} has no attribute {attribute}")
        if attribute == "brightness":
            implied = SourceScale.BYTE
        elif attribute == "brightness_pct":
            implied = SourceScale.PERCENT
    elif domain == "light":
        if state.state != STATE_ON:
            return SourceReading(None, False, f"{entity_id} is {state.state}; a light reports brightness only while on")
        raw, implied = attrs.get("brightness"), SourceScale.BYTE
    elif attrs.get("brightness_pct") is not None:  # e.g. Adaptive Lighting switch
        raw, implied = attrs.get("brightness_pct"), SourceScale.PERCENT
    else:
        raw = state.state
    value = _float(raw)
    if value is None:
        return SourceReading(None, False, f"{entity_id}: '{raw}' is not a number")
    unit = str(attrs.get(ATTR_UNIT_OF_MEASUREMENT) or "").strip()
    chosen = scale if scale in (SourceScale.PERCENT, SourceScale.BYTE) else implied
    if chosen is None:
        if unit == "%":
            chosen = SourceScale.PERCENT
        elif _float(attrs.get("max")) in (254.0, 255.0):  # an input_number/number on the 0-255 scale
            chosen = SourceScale.BYTE
        elif value <= 100:
            chosen = SourceScale.PERCENT
        elif value <= 255:
            chosen = SourceScale.BYTE
        else:
            return SourceReading(None, False, f"{entity_id}: {value:g} is not a brightness (0-100 % or 0-255)")
    if chosen == SourceScale.BYTE:
        if not 0 <= value <= 255:
            return SourceReading(None, False, f"{entity_id}: {value:g} is outside 0-255")
        return SourceReading(value / 255 * 100, True, f"{entity_id}: {value:g} of 255", "0-255")
    if not 0 <= value <= 100:
        return SourceReading(None, False, f"{entity_id}: {value:g} % is outside 0-100 %")
    return SourceReading(value, True, f"{entity_id}: {value:g} %", "%")


def read_color_temp(state: State | None, entity_id: str, attribute: str | None = None,
                    scale: SourceScale = SourceScale.AUTO) -> SourceReading:
    """Colour temperature in K from a sensor, number, input_number, light or Adaptive Lighting switch."""
    if bad := _unusable(state, entity_id):
        return bad
    assert state is not None
    attrs = state.attributes
    domain = entity_id.split(".", 1)[0]
    implied: SourceScale | None = None
    if attribute:
        raw: Any = attrs.get(attribute)
        if raw is None:
            return SourceReading(None, False, f"{entity_id} has no attribute {attribute}")
        if "kelvin" in attribute:
            implied = SourceScale.KELVIN
        elif "mired" in attribute or attribute == "color_temp":
            implied = SourceScale.MIRED
    elif domain == "light":
        if state.state != STATE_ON or attrs.get("color_mode") != "color_temp":
            return SourceReading(None, False, f"{entity_id} is not on in white (colour temperature) mode")
        raw, implied = attrs.get("color_temp_kelvin"), SourceScale.KELVIN
    elif attrs.get("color_temp_kelvin") is not None:  # e.g. Adaptive Lighting switch
        raw, implied = attrs.get("color_temp_kelvin"), SourceScale.KELVIN
    elif attrs.get("color_temp_mired") is not None:
        raw, implied = attrs.get("color_temp_mired"), SourceScale.MIRED
    else:
        raw = state.state
    value = _float(raw)
    if value is None:
        return SourceReading(None, False, f"{entity_id}: '{raw}' is not a number")
    unit = str(attrs.get(ATTR_UNIT_OF_MEASUREMENT) or "").strip().casefold()
    chosen = scale if scale in (SourceScale.KELVIN, SourceScale.MIRED) else implied
    if chosen is None:
        if unit in _KELVIN_UNITS:
            chosen = SourceScale.KELVIN
        elif unit in _MIRED_UNITS:
            chosen = SourceScale.MIRED
        elif value >= KELVIN_MIN:
            chosen = SourceScale.KELVIN
        elif 50 <= value < KELVIN_MIN:
            chosen = SourceScale.MIRED
        else:
            return SourceReading(None, False, f"{entity_id}: {value:g} is not a colour temperature (K or mired)")
    if chosen == SourceScale.MIRED:
        if value <= 0:
            return SourceReading(None, False, f"{entity_id}: {value:g} mired is not valid")
        kelvin, unit_text, text = 1_000_000 / value, "mired", f"{value:g} mired"
    else:
        kelvin, unit_text, text = value, "K", f"{value:g} K"
    if not KELVIN_MIN <= kelvin <= KELVIN_MAX:
        return SourceReading(None, False, f"{entity_id}: {kelvin:.0f} K is outside {KELVIN_MIN}-{KELVIN_MAX} K")
    return SourceReading(kelvin, True, f"{entity_id}: {text}", unit_text)


# ----------------------------------------------------------------- values
@dataclass(slots=True, frozen=True)
class GlobalSource:
    """The value of one global source as profiles use it."""

    entity_id: str | None
    value: float | None
    status: str
    detail: str
    updated_at: str | None = None  # when the value was last read successfully
    unit: str | None = None

    @property
    def usable(self) -> bool:
        return self.value is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "value": None if self.value is None else round(self.value, 1),
            "status": self.status,
            "detail": self.detail,
            "updated_at": self.updated_at,
            "unit": self.unit,
        }


_NONE = GlobalSource(None, None, NOT_CONFIGURED, "Not configured")


@dataclass(slots=True, frozen=True)
class GlobalValues:
    brightness: GlobalSource = _NONE
    color_temp: GlobalSource = _NONE

    @property
    def status(self) -> str:
        configured = [s for s in (self.brightness, self.color_temp) if s.status != NOT_CONFIGURED]
        if not configured:
            return NOT_CONFIGURED
        return CONNECTED if all(s.status == CONNECTED for s in configured) else UNAVAILABLE

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, BRIGHTNESS: self.brightness.as_dict(), COLOR_TEMP: self.color_temp.as_dict()}


class GlobalAdjustment:
    """Live values of the configured global sources (state-change subscription, no polling)."""

    def __init__(self, hass: HomeAssistant, on_change: Callable[[GlobalValues, GlobalValues], None]) -> None:
        self.hass = hass
        self._on_change = on_change
        self.config = AdaptiveConfig()
        self.values = GlobalValues()
        # kind -> (entity_id, value, iso time) of the last good reading
        self._last: dict[str, tuple[str, float, str]] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._tracked: tuple[str, ...] = ()
        self._raw: tuple[tuple[str, str | None], ...] = ()
        self._started = False

    def restore(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        for kind in (BRIGHTNESS, COLOR_TEMP):
            item = data.get(kind)
            if isinstance(item, list) and len(item) == 3 and isinstance(item[0], str) and _float(item[1]) is not None:
                self._last[kind] = (item[0], float(item[1]), str(item[2]))

    def persist(self) -> dict[str, Any]:
        return {kind: list(v) for kind, v in self._last.items()}

    @callback
    def async_configure(self, config: AdaptiveConfig) -> None:
        self.config = config
        if self._started:
            self._subscribe()
        self._refresh(notify=self._started)

    @callback
    def async_start(self) -> None:
        self._started = True
        self._subscribe()
        self._refresh(notify=False)

    @callback
    def async_stop(self) -> None:
        self._started = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._tracked = ()

    def _subscribe(self) -> None:
        tracked = tuple(sorted(set(self.config.entities)))
        if tracked == self._tracked and (self._unsub or not tracked):
            return
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._tracked = tracked
        if tracked:
            self._unsub = async_track_state_change_event(self.hass, list(tracked), self._on_state)

    @callback
    def _on_state(self, _event: Event[EventStateChangedData]) -> None:
        self._refresh(notify=True)

    def _source(self, kind: str, entity_id: str | None, reading: SourceReading | None) -> GlobalSource:
        if not entity_id or reading is None:
            return _NONE
        if reading.ok and reading.value is not None:
            now = dt_util.utcnow().isoformat(timespec="seconds")
            self._last[kind] = (entity_id, reading.value, now)
            return GlobalSource(entity_id, reading.value, CONNECTED, reading.detail, now, reading.unit)
        last = self._last.get(kind)
        if last and last[0] == entity_id:
            return GlobalSource(entity_id, last[1], STALE,
                                f"{reading.detail}; using the last value from {last[2]}", last[2])
        return GlobalSource(entity_id, None, UNAVAILABLE, reading.detail)

    def read(self) -> GlobalValues:
        cfg = self.config
        states = self.hass.states
        b = cfg.brightness_entity
        k = cfg.color_temp_entity
        return GlobalValues(
            brightness=self._source(BRIGHTNESS, b, read_brightness(states.get(b), b, cfg.brightness_attribute,
                                                                   cfg.brightness_scale) if b else None),
            color_temp=self._source(COLOR_TEMP, k, read_color_temp(states.get(k), k, cfg.color_temp_attribute,
                                                                   cfg.color_temp_scale) if k else None),
        )

    def _refresh(self, *, notify: bool) -> None:
        old, self.values = self.values, self.read()
        # The sources' own on/off state counts too (an Adaptive Lighting switch turned off stops adjusting).
        raw = tuple((e, s.state if (s := self.hass.states.get(e)) else None) for e in self.config.entities)
        old_raw, self._raw = self._raw, raw
        changed = (_key(old.brightness), _key(old.color_temp)) != (_key(self.values.brightness),
                                                                   _key(self.values.color_temp)) or raw != old_raw
        if changed:
            _LOGGER.debug("global_adjustment brightness=%s (%s) color_temp=%s (%s)",
                          self.values.brightness.value, self.values.brightness.status,
                          self.values.color_temp.value, self.values.color_temp.status)
        if changed and notify:
            self._on_change(old, self.values)


def _key(source: GlobalSource) -> tuple[Any, ...]:
    return (source.entity_id, None if source.value is None else round(source.value, 2), source.status)


# ------------------------------------------------------------ resolution
def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def apply_global(resolved: ResolvedSetting, values: GlobalValues | None) -> ResolvedSetting:
    """Replace "follow global" modes by the current global values. Records how in ``resolved.adaptive``.

    * Brightness: global (+ offset), clamped to 1-100 % (a global value never switches a light off).
    * Colour temperature: global (+ offset), clamped to 1000-10000 K (and later to the light's range).
      While an RGB/HS colour or an effect controls the colour, the global white temperature is not used.
    * An unusable source leaves the property unchanged (it is never taken as 0) and is reported.
    """
    values = values or GlobalValues()
    if resolved.action != LightAction.ON:
        resolved.adaptive = None
        return resolved
    info: dict[str, Any] = {}

    mode = resolved.brightness_mode
    if mode in FOLLOWING:
        src = values.brightness
        offset = (resolved.brightness_offset or 0) if mode == AdjustMode.OFFSET else 0
        entry: dict[str, Any] = {"mode": mode.value, "offset": offset, "global": _r(src.value),
                                 "source_status": src.status}
        if src.value is None:
            resolved.brightness_pct = None
            entry |= {"value": None, "status": src.status,
                      "reason": "No global brightness value: brightness is left unchanged"}
        else:
            target = src.value + offset
            value = round(_clamp(target, BRIGHTNESS_MIN, BRIGHTNESS_MAX))
            resolved.brightness_pct = value
            entry |= {"value": value, "status": "applied" if src.status == CONNECTED else STALE,
                      "clamped": round(target) != value}
        info[BRIGHTNESS] = entry
    elif mode == AdjustMode.IGNORE:
        resolved.brightness_pct = None
        info[BRIGHTNESS] = {"mode": "ignore", "status": "ignored", "value": None}
    elif resolved.brightness_pct is not None:
        info[BRIGHTNESS] = {"mode": "fixed", "status": "fixed", "value": resolved.brightness_pct}

    mode = resolved.color_temp_mode
    if mode in FOLLOWING:
        src = values.color_temp
        offset = (resolved.color_temp_offset or 0) if mode == AdjustMode.OFFSET else 0
        entry = {"mode": mode.value, "offset": offset, "global": _r(src.value, 0), "source_status": src.status}
        controlled_by = ("RGB/HS colour" if resolved.rgb_color is not None or resolved.hs_color is not None
                         else f"effect {resolved.effect}" if resolved.effect else None)
        if controlled_by:
            resolved.color_temp_kelvin = None
            entry |= {"value": None, "status": "suppressed",
                      "reason": f"The {controlled_by} controls the colour; the global white temperature is not used"}
        elif src.value is None:
            resolved.color_temp_kelvin = None
            entry |= {"value": None, "status": src.status,
                      "reason": "No global colour temperature: colour temperature is left unchanged"}
        else:
            target = src.value + offset
            value = round(_clamp(target, KELVIN_MIN, KELVIN_MAX))
            resolved.color_temp_kelvin = value
            entry |= {"value": value, "status": "applied" if src.status == CONNECTED else STALE,
                      "clamped": round(target) != value}
        info[COLOR_TEMP] = entry
    elif mode == AdjustMode.IGNORE:
        resolved.color_temp_kelvin = None
        info[COLOR_TEMP] = {"mode": "ignore", "status": "ignored", "value": None}
    elif resolved.color_temp_kelvin is not None:
        info[COLOR_TEMP] = {"mode": "fixed", "status": "fixed", "value": resolved.color_temp_kelvin}
    resolved.adaptive = info or None
    return resolved


def _r(value: float | None, digits: int = 1) -> float | int | None:
    if value is None:
        return None
    return round(value) if digits == 0 else round(value, digits)


def follows_global(resolved: ResolvedSetting) -> bool:
    """Does the setting take brightness or colour temperature from the global adjustment?"""
    return resolved.brightness_mode in FOLLOWING or resolved.color_temp_mode in FOLLOWING


# ---------------------------------------------------------- gradual updates
SEND = "send"
UNCHANGED = "unchanged"
DEFERRED = "deferred"
BLOCKED = "blocked"
SKIPPED = "skipped"


@dataclass(slots=True)
class AdaptiveSent:
    """The last command sent to a light or group (monotonic time, adaptive fields)."""

    at: float
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AdaptiveDecision:
    decision: str
    code: str
    reason: str
    data: dict[str, Any] = field(default_factory=dict)
    due: float | None = None  # monotonic time a deferred update becomes due

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"decision": self.decision, "code": self.code, "reason": self.reason,
                               "data": dict(self.data), "would_send": self.decision == SEND}
        if self.due is not None and now is not None:
            out["due_in"] = max(0, round(self.due - now))
        return out


def _current(target: Target) -> dict[str, float]:
    a = target.attributes
    out: dict[str, float] = {}
    brightness = a.get("brightness")
    if isinstance(brightness, (int, float)) and not isinstance(brightness, bool):
        out["brightness_pct"] = brightness / 255 * 100
    kelvin = a.get("color_temp_kelvin")
    if a.get("color_mode") == "color_temp" and isinstance(kelvin, (int, float)):
        out["color_temp_kelvin"] = float(kelvin)
    return out


def adaptive_update(
    item: PlanItem,
    target: Target,
    last: AdaptiveSent | None,
    now: float,
    config: AdaptiveConfig,
    *,
    force: bool = False,
    competing: Iterable[dict[str, Any]] = (),
) -> AdaptiveDecision:
    """The gradual update a global value change causes for one light. Never switches a light on.

    ``item`` is the light's mood plan item (its decision covers exclusions, groups, manual
    overrides, conflicts, availability and occupancy). ``force`` skips the minimum interval
    (used when a light was just switched on by someone else).
    """
    info = item.resolved.adaptive or {}
    fields: dict[str, int] = {}
    unusable: list[str] = []
    unsupported: list[str] = []
    for kind, key, word in ((BRIGHTNESS, "brightness_pct", "brightness"),
                            (COLOR_TEMP, "color_temp_kelvin", "colour temperature")):
        entry = info.get(kind)
        if not entry or entry.get("mode") not in ("global", "offset"):
            continue
        if entry.get("status") in ("applied", STALE) and key in item.data:
            fields[key] = item.data[key]
        elif entry.get("status") in ("applied", STALE):
            unsupported.append(word)  # the light (or its current colour/effect) cannot take it
        elif entry.get("status") in (UNAVAILABLE, NOT_CONFIGURED):
            unusable.append(word)
    if not fields:
        if unusable:
            return AdaptiveDecision(BLOCKED, "source_unavailable",
                                    f"The global {' and '.join(unusable)} source has no value; nothing is adjusted")
        if unsupported and item.service == "turn_on":
            return AdaptiveDecision(SKIPPED, "unsupported",
                                    f"Follows the global {' and '.join(unsupported)}, but this light cannot use it")
        return AdaptiveDecision(SKIPPED, "not_following", "Does not follow the global adjustment in this mood")
    if item.decision == "skipped":
        return AdaptiveDecision(SKIPPED, item.reason_code, item.reason)
    if item.decision == "blocked":
        return AdaptiveDecision(BLOCKED, item.reason_code, item.reason)
    competing = list(competing)
    if competing:
        names = ", ".join(c.get("name") or c.get("entity_id", "?") for c in competing)
        return AdaptiveDecision(BLOCKED, "competing_controller",
                                f"Also adjusted by the existing adjustment system ({names}); global updates are "
                                "not sent so the two do not compete")
    if target.state != "on":
        return AdaptiveDecision(SKIPPED, "light_off",
                                "Light is off; a global change never switches a light on")
    notes: list[str] = []
    a = target.attributes
    if "color_temp_kelvin" in fields and (a.get("color_mode") in _COLOUR_MODES or a.get("effect") not in _NO_EFFECT):
        del fields["color_temp_kelvin"]
        notes.append("the light shows a colour or effect now, so its colour temperature is left alone")
    current = _current(target)
    steps = {"brightness_pct": config.brightness_step, "color_temp_kelvin": config.kelvin_step}
    for key in list(fields):
        reference = last.data.get(key) if last and key in last.data else current.get(key)
        if reference is not None and abs(float(fields[key]) - float(reference)) < steps[key]:
            del fields[key]
    if not fields:
        why = "; ".join(notes) or (f"change below the thresholds ({config.brightness_step} % / "
                                   f"{config.kelvin_step} K)")
        return AdaptiveDecision(UNCHANGED, "below_threshold", f"No update needed: {why}")
    if not force and last is not None and config.min_interval and now - last.at < config.min_interval:
        due = last.at + config.min_interval
        return AdaptiveDecision(DEFERRED, "throttled",
                                f"Updated {round(now - last.at)} s ago; the next update follows within "
                                f"{round(due - now)} s (newest value wins)", dict(fields), due)
    data: dict[str, Any] = dict(fields)
    if config.transition and target.capabilities.transition:
        data["transition"] = config.transition
    parts = []
    if "brightness_pct" in fields:
        parts.append(f"{fields['brightness_pct']} %")
    if "color_temp_kelvin" in fields:
        parts.append(f"{fields['color_temp_kelvin']} K")
    reason = f"Follows the global adjustment: {' · '.join(parts)}"
    if notes:
        reason += f" ({'; '.join(notes)})"
    return AdaptiveDecision(SEND, "adaptive", reason, data)


# --------------------------------------------------- competing controllers
@dataclass(slots=True, frozen=True)
class SourceController:
    """Part of the existing adjustment system that commands lights itself."""

    entity_id: str
    name: str | None
    kind: str           # "adaptive_lighting" | "automation"
    active: bool        # switch on / automation enabled
    lights: frozenset[str] = frozenset()  # lights named directly (Adaptive Lighting)

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "name": self.name, "kind": self.kind, "active": self.active,
                "lights": sorted(self.lights)}


def _adaptive_lighting_lights(state: State) -> set[str] | None:
    """Lights of an Adaptive Lighting style switch, or None if it isn't one."""
    attrs = state.attributes
    config = attrs.get("configuration")
    lights: Any = None
    if isinstance(config, dict):
        lights = config.get("lights")
    if lights is None and "manual_control" in attrs:
        lights = attrs.get("lights", [])
    if lights is None:
        return None
    return {x for x in lights if isinstance(x, str)} if isinstance(lights, (list, tuple)) else set()


def _raw_text(config: Any) -> str:
    try:
        return json.dumps(config, default=str)
    except (TypeError, ValueError):
        return ""


def find_source_controllers(
    hass: HomeAssistant, config: AdaptiveConfig, automations: list[AutomationInfo]
) -> tuple[list[SourceController], list[AutomationInfo]]:
    """The parts of the existing system that also command lights.

    * a configured source that is an Adaptive Lighting switch (it adjusts its own lights while on);
    * automations whose configuration uses a source entity (directly, or through a script that
      uses it) and that control lights.

    Returns (controllers for display, the matching automations for conflict matching).
    """
    sources = set(config.entities)
    if not sources:
        return [], []
    out: list[SourceController] = []
    ent_reg = er.async_get(hass)
    for entity_id in sorted(sources):
        state = hass.states.get(entity_id)
        if state is None or not entity_id.startswith("switch."):
            continue
        entry = ent_reg.async_get(entity_id)
        lights = _adaptive_lighting_lights(state)
        if lights is None and not (entry and entry.platform == "adaptive_lighting"):
            continue
        out.append(SourceController(entity_id, state.attributes.get("friendly_name"), "adaptive_lighting",
                                    state.state == STATE_ON, frozenset(lights or ())))

    def uses_source(raw: Any, extra: set[str]) -> bool:
        text = _raw_text(raw)
        return any(re.search(rf"(?<![\w.]){re.escape(e)}(?!\w)", text) for e in sources | extra)

    scripts: set[str] = set()
    if (component := hass.data.get("script")) is not None:
        for entity in getattr(component, "entities", ()):
            if uses_source(getattr(entity, "raw_config", None), set()):
                scripts.add(entity.entity_id)
    matching: list[AutomationInfo] = []
    component = hass.data.get("automation")
    raws = {e.entity_id: getattr(e, "raw_config", None) for e in getattr(component, "entities", ())} \
        if component is not None else {}
    for auto in automations:
        if auto.refs.controls_lights and uses_source(raws.get(auto.entity_id), scripts):
            matching.append(auto)
            out.append(SourceController(auto.entity_id, auto.name, "automation", auto.enabled))
    return out, matching
