"""Possible occupancy lighting (0.3.9): a dim, temporary light before occupancy is confirmed.

Pure logic, no Home Assistant runtime: settings (global defaults and room
overrides), which occupancy evidence may start the preliminary stage, the
daylight decision for it, and the plan of what would be sent to each
preliminary light. The lighting controller runs it.

The state comes from the occupancy engine, which already has a separate
``possible_occupancy`` state (new, uncorroborated evidence: one trigger event,
a presence signal that has just turned on, or a door movement alone) next to
``confirmed_occupancy`` (corroborated evidence). Nothing here adds a second
occupancy engine; this module only decides what the lights do in the
possible stage:

* A room's preliminary lighting is **active** when it is enabled (room value,
  or the global default) and at least one preliminary light is chosen. Without
  a light nothing changes, so the global default cannot alter a room that
  has none.
* It starts once per visit, when the engine reports possible occupancy and at
  least one of the room's selected signals is part of that evidence (default:
  the room's Trigger sensors, i.e. PIR motion; radar, PC activity and doors
  only when selected).
* Only lights that are **off** are switched on (brightness, optional colour
  temperature, transition). A light that is already on is left alone, so its
  state is never lost and removing the preliminary light restores the room.
* The room's daylight rules decide whether it may switch on: the same
  automatic switch-on policy, sensors, fallback for uncertain readings and,
  unless a preliminary threshold is set, the same threshold. The decision is
  taken when the stage starts (and again if the room becomes dark during it);
  nothing is switched off because of daylight, so it cannot oscillate.
* While a room with active preliminary lighting is in possible occupancy, the
  normal (mood) lighting does not switch further lights on: it waits for
  confirmation. A room that confirms occupancy at once skips the stage.

On confirmation each preliminary light either is **handed over** to the normal
lighting (the occupied scene decides its final state; a light the scene does
not control stays on and is handled by the vacancy action) or is **switched
off**. The occupied scene has precedence: a preliminary light that the scene
switches on stays on with the scene's settings, never off-then-on. If the
normal lighting would not switch the light on (daylight is enough at the
normal threshold), it is switched off in both cases.

Without confirmation the stage ends after the preliminary timeout (counted
from its start; later sensor events do not extend it) or when the room
becomes vacant, whichever is first. Then only the lights this stage switched
on, and that nobody changed since, are switched off.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


from .const import LUX_MAX_THRESHOLD, LUX_MIN, TRANSITION_MAX, AutoOn, SensorKind, SensorRole
from .daylight import BRIGHT, DARK, HIGH, MEDIUM, UNCERTAIN, DaylightGate, Estimate
from .engine import RoomResult, SignalStatus
from .models import SensorAssignment, _enum_or

DEFAULT_TIMEOUT = 30
TIMEOUT_MIN = 5
TIMEOUT_MAX = 600
DEFAULT_BRIGHTNESS = 10
DEFAULT_TRANSITION = 0.5
KELVIN_MIN = 1500
KELVIN_MAX = 9000

TRIGGER = "possible_occupancy"


class ConfirmAction(StrEnum):
    """What happens to a preliminary light when occupancy is confirmed."""

    HANDOVER = "handover"   # the normal occupied lighting decides
    TURN_OFF = "turn_off"   # switched off (unless the occupied scene switches it on)


DEFAULT_CONFIRM = ConfirmAction.HANDOVER


def _int(value: Any, low: int, high: int) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(min(max(float(value), low), high))
    except (TypeError, ValueError):
        return None


def _float(value: Any, low: float, high: float) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return round(min(max(float(value), low), high), 2)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ configuration
@dataclass(frozen=True, slots=True)
class PreliminaryDefaults:
    """Global defaults; a room that does not set a value follows them."""

    enabled: bool = False
    timeout: int = DEFAULT_TIMEOUT
    brightness_pct: int = DEFAULT_BRIGHTNESS
    transition: float = DEFAULT_TRANSITION
    # None: the room's normal darkness threshold.
    on_below: int | None = None
    on_confirm: ConfirmAction = DEFAULT_CONFIRM

    @classmethod
    def from_dict(cls, data: Any) -> PreliminaryDefaults:
        data = data if isinstance(data, dict) else {}
        enabled = data.get("enabled", False)
        timeout = _int(data.get("timeout"), TIMEOUT_MIN, TIMEOUT_MAX)
        brightness = _int(data.get("brightness_pct"), 1, 100)
        transition = _float(data.get("transition"), 0, TRANSITION_MAX)
        return cls(
            enabled=enabled if isinstance(enabled, bool) else False,
            timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
            brightness_pct=DEFAULT_BRIGHTNESS if brightness is None else brightness,
            transition=DEFAULT_TRANSITION if transition is None else transition,
            on_below=_int(data.get("on_below"), LUX_MIN, LUX_MAX_THRESHOLD),
            on_confirm=_enum_or(ConfirmAction, data.get("on_confirm"), DEFAULT_CONFIRM),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "timeout": self.timeout, "brightness_pct": self.brightness_pct,
                "transition": self.transition, "on_below": self.on_below, "on_confirm": self.on_confirm.value}


@dataclass(frozen=True, slots=True)
class PreliminaryTarget:
    """One preliminary light. ``None`` follows the global default (no colour temperature by default)."""

    entity_id: str
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    transition: float | None = None
    on_confirm: ConfirmAction | None = None

    @classmethod
    def from_dict(cls, data: Any) -> PreliminaryTarget | None:
        if isinstance(data, str):
            data = {"entity_id": data}
        if not isinstance(data, dict):
            return None
        entity_id = data.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            return None
        return cls(
            entity_id=entity_id.strip(),
            brightness_pct=_int(data.get("brightness_pct"), 1, 100),
            color_temp_kelvin=_int(data.get("color_temp_kelvin"), KELVIN_MIN, KELVIN_MAX),
            transition=_float(data.get("transition"), 0, TRANSITION_MAX),
            on_confirm=_enum_or(ConfirmAction, data.get("on_confirm"), None),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "brightness_pct": self.brightness_pct,
                "color_temp_kelvin": self.color_temp_kelvin, "transition": self.transition,
                "on_confirm": self.on_confirm.value if self.on_confirm else None}


@dataclass(frozen=True, slots=True)
class RoomPreliminary:
    """A room's possible occupancy lighting. ``None`` follows the global default."""

    enabled: bool | None = None
    timeout: int | None = None
    on_below: int | None = None
    # Signals whose evidence may start the stage; None: the room's Trigger sensors (PIR motion).
    signals: tuple[str, ...] | None = None
    targets: tuple[PreliminaryTarget, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> RoomPreliminary:
        data = data if isinstance(data, dict) else {}
        enabled = data.get("enabled")
        signals = data.get("signals")
        targets = tuple(t for t in (PreliminaryTarget.from_dict(x) for x in data.get("targets") or []) if t)
        return cls(
            enabled=enabled if isinstance(enabled, bool) else None,
            timeout=_int(data.get("timeout"), TIMEOUT_MIN, TIMEOUT_MAX),
            on_below=_int(data.get("on_below"), LUX_MIN, LUX_MAX_THRESHOLD),
            signals=tuple(dict.fromkeys(s for s in signals if isinstance(s, str) and "." in s))
            if isinstance(signals, (list, tuple)) else None,
            targets=tuple({t.entity_id: t for t in targets}.values()),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "timeout": self.timeout, "on_below": self.on_below,
                "signals": list(self.signals) if self.signals is not None else None,
                "targets": [t.as_dict() for t in self.targets]}

    def target(self, entity_id: str) -> PreliminaryTarget | None:
        return next((t for t in self.targets if t.entity_id == entity_id), None)


def default_signals(sensors: Iterable[SensorAssignment]) -> tuple[str, ...]:
    """The room's Trigger sensors (PIR / motion): the classic first-motion evidence."""
    return tuple(s.entity_id for s in sensors if s.kind != SensorKind.DOOR and s.role == SensorRole.TRIGGER)


@dataclass(frozen=True, slots=True)
class EffectiveTarget:
    entity_id: str
    brightness_pct: int
    color_temp_kelvin: int | None
    transition: float
    on_confirm: ConfirmAction
    sources: dict[str, str]  # field -> "room" | "global"

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "brightness_pct": self.brightness_pct,
                "color_temp_kelvin": self.color_temp_kelvin, "transition": self.transition,
                "on_confirm": self.on_confirm.value, "sources": dict(self.sources)}


@dataclass(frozen=True, slots=True)
class EffectivePreliminary:
    """A room's settings with the global defaults filled in, and where each value comes from."""

    enabled: bool
    timeout: int
    on_below: int | None       # None: the room's normal threshold
    signals: tuple[str, ...]
    signals_default: bool
    targets: tuple[EffectiveTarget, ...]
    sources: dict[str, str] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        """Changes anything: enabled with at least one light."""
        return self.enabled and bool(self.targets)

    def target(self, entity_id: str) -> EffectiveTarget | None:
        return next((t for t in self.targets if t.entity_id == entity_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "active": self.active, "timeout": self.timeout,
                "on_below": self.on_below, "signals": list(self.signals), "signals_default": self.signals_default,
                "targets": [t.as_dict() for t in self.targets], "sources": dict(self.sources)}


def effective(room: RoomPreliminary, defaults: PreliminaryDefaults,
              sensors: Iterable[SensorAssignment]) -> EffectivePreliminary:
    sensors = tuple(sensors)
    assigned = {s.entity_id for s in sensors}

    def pick(value: Any, default: Any) -> tuple[Any, str]:
        return (value, "room") if value is not None else (default, "global")

    enabled, s_enabled = pick(room.enabled, defaults.enabled)
    timeout, s_timeout = pick(room.timeout, defaults.timeout)
    on_below, s_lux = pick(room.on_below, defaults.on_below)
    if room.signals is not None:
        signals = tuple(s for s in room.signals if s in assigned)
    else:
        signals = default_signals(sensors)
    targets = []
    for t in room.targets:
        brightness, s_b = pick(t.brightness_pct, defaults.brightness_pct)
        transition, s_t = pick(t.transition, defaults.transition)
        confirm, s_c = pick(t.on_confirm, defaults.on_confirm)
        targets.append(EffectiveTarget(t.entity_id, brightness, t.color_temp_kelvin, transition, confirm,
                                       {"brightness_pct": s_b, "transition": s_t, "on_confirm": s_c}))
    return EffectivePreliminary(
        enabled=enabled, timeout=timeout, on_below=on_below, signals=signals, signals_default=room.signals is None,
        targets=tuple(targets), sources={"enabled": s_enabled, "timeout": s_timeout, "on_below": s_lux},
    )


# ----------------------------------------------------------------- evidence
def evidence(result: RoomResult | None, signals: Iterable[str]) -> list[str]:
    """Selected signals that are part of the engine's possible occupancy now (entity ids)."""
    if result is None:
        return []
    wanted = set(signals)
    out: list[str] = []
    for v in result.signals:
        if v.track.entity_id not in wanted or not v.track.used:
            continue
        if v.track.capable and v.status in (SignalStatus.ACTIVE, SignalStatus.GRACE):
            out.append(v.track.entity_id)
        elif v.track.is_door and v.status == SignalStatus.RECENT:
            out.append(v.track.entity_id)
    return out


# ------------------------------------------------------------------ daylight
def preliminary_gate(gate: DaylightGate, estimate: Estimate, on_below: int | None) -> DaylightGate:
    """The room's daylight decision, at the preliminary threshold when one is set.

    Same policy (Never / Always / when dark), same evidence and the same
    fallback for uncertain readings: an unknown, stale or unavailable reading
    is not evidence of darkness. Without its own threshold the room's normal
    decision is used unchanged (including its hysteresis).
    """
    if on_below is None or gate.policy != AutoOn.DARK:
        return gate
    if estimate.lux is None or estimate.confidence not in (HIGH, MEDIUM):
        decision = UNCERTAIN
    elif estimate.lux < on_below:
        decision = DARK
    else:
        decision = BRIGHT
    return DaylightGate(gate.policy, decision, gate.uncertain, estimate.text, on_below)


def describe_setting(eff: EffectiveTarget) -> str:
    """"10 %, 2200 K"."""
    parts = [f"{eff.brightness_pct} %"]
    if eff.color_temp_kelvin:
        parts.append(f"{eff.color_temp_kelvin} K")
    return ", ".join(parts)
