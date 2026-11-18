"""Types of the occupancy engine: signal kinds and roles, states, timing, sensor assignments.

Moved unchanged from the integration's ``const.py`` and ``models.py`` (0.6.10),
which re-export them, so every existing import keeps the same objects.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final


class SensorKind(StrEnum):
    """What a room signal physically is."""

    PIR = "pir"
    RADAR = "radar"
    DOOR = "door"
    PC_ACTIVITY = "pc_activity"
    BED = "bed"
    OTHER = "other"


class SensorRole(StrEnum):
    """How a room signal contributes to occupancy."""

    TRIGGER = "trigger"
    MAINTAIN = "maintain"
    CONFIRM_VACANCY = "confirm_vacancy"
    IGNORE = "ignore"


DEFAULT_ROLE_FOR_KIND: Final[dict[SensorKind, SensorRole]] = {
    SensorKind.PIR: SensorRole.TRIGGER,
    SensorKind.RADAR: SensorRole.MAINTAIN,
    SensorKind.DOOR: SensorRole.TRIGGER,
    SensorKind.PC_ACTIVITY: SensorRole.MAINTAIN,
    SensorKind.BED: SensorRole.MAINTAIN,
    SensorKind.OTHER: SensorRole.IGNORE,
}


class OccupancyState(StrEnum):
    """Internal occupancy state of a room (see engine.py for transitions)."""

    VACANT = "vacant"
    POSSIBLE_OCCUPANCY = "possible_occupancy"
    CONFIRMED_OCCUPANCY = "confirmed_occupancy"
    POSSIBLE_VACANCY = "possible_vacancy"
    UNCERTAIN = "uncertain"


# Occupancy timing parameters: key -> (default seconds, min, max).
# All are configurable globally (Configure -> Occupancy timing) and can be
# overridden per room. A value of 0 disables the two "stuck" detections.
TIMING_PARAMETERS: Final[dict[str, tuple[int, int, int]]] = {
    "correlation_window": (10, 1, 120),
    "exit_confirmation": (300, 0, 3600),
    "inactivity_timeout": (600, 30, 14400),
    "dropout_grace": (30, 0, 600),
    "trigger_stuck_after": (1800, 0, 86400),
    "presence_stuck_after": (0, 0, 172800),
    "startup_hold": (120, 0, 3600),
}


def enum_or(enum_cls: type, value: Any, default: Any) -> Any:
    try:
        return enum_cls(value)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class SensorAssignment:
    """A signal entity assigned to a room."""

    entity_id: str
    kind: SensorKind
    role: SensorRole
    # The signal means "active" when it is off (e.g. a "PC idle" sensor).
    inverted: bool = False
    # 0.3.8: False excludes the signal from this room's occupancy decisions (Presence tab). The assignment,
    # its kind and role are kept, so enabling it again restores it exactly; the entity itself is untouched.
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SensorAssignment:
        kind = enum_or(SensorKind, data.get("kind"), SensorKind.OTHER)
        role = enum_or(SensorRole, data.get("role"), DEFAULT_ROLE_FOR_KIND[kind])
        enabled = data.get("enabled", True)
        return cls(
            entity_id=data["entity_id"], kind=kind, role=role,
            inverted=bool(data.get("inverted", False)),
            enabled=enabled if isinstance(enabled, bool) else True,
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "entity_id": self.entity_id,
            "kind": self.kind.value,
            "role": self.role.value,
            "inverted": self.inverted,
        }
        if not self.enabled:
            # Only written when excluded: options saved before 0.3.8 keep their exact shape.
            out["enabled"] = False
        return out

    @property
    def participates(self) -> bool:
        """Takes part in occupancy decisions: enabled and not set to Ignore."""
        return self.enabled and self.role != SensorRole.IGNORE


def clean_timing(data: Any, *, fill_defaults: bool) -> dict[str, int]:
    """Valid timing values only (unknown keys and bad values are dropped)."""
    out: dict[str, int] = {}
    data = data if isinstance(data, dict) else {}
    for key, (default, low, high) in TIMING_PARAMETERS.items():
        value = data.get(key)
        if value is None or isinstance(value, bool):
            if fill_defaults:
                out[key] = default
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            if fill_defaults:
                out[key] = default
            continue
        out[key] = min(max(number, low), high)
    return out


@dataclass(frozen=True, slots=True)
class OccupancyTiming:
    """Occupancy timing parameters, in seconds (see TIMING_PARAMETERS)."""

    correlation_window: int = TIMING_PARAMETERS["correlation_window"][0]
    exit_confirmation: int = TIMING_PARAMETERS["exit_confirmation"][0]
    inactivity_timeout: int = TIMING_PARAMETERS["inactivity_timeout"][0]
    dropout_grace: int = TIMING_PARAMETERS["dropout_grace"][0]
    trigger_stuck_after: int = TIMING_PARAMETERS["trigger_stuck_after"][0]
    presence_stuck_after: int = TIMING_PARAMETERS["presence_stuck_after"][0]
    startup_hold: int = TIMING_PARAMETERS["startup_hold"][0]

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> OccupancyTiming:
        return cls(**clean_timing(data, fill_defaults=True))

    def as_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in TIMING_PARAMETERS}

    def with_overrides(self, overrides: dict[str, int]) -> OccupancyTiming:
        return replace(self, **clean_timing(overrides, fill_defaults=False))
