"""Occupancy forecast settings (0.5.0): global defaults, room overrides and room connections.

Kept in ``.storage/smart_occupancy.forecast_settings`` (small; written only
when a person changes a setting). Every setting here is used by the backend;
a room value of ``None`` means "use the global default".
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .forecast_model import HORIZONS

CONFIDENCE_THRESHOLDS = ("low", "moderate", "high")
CONNECTION_KINDS = ("door", "open", "passage")
CONNECTION_WORDS = {"door": "Through a door", "open": "Open plan (no door)",
                    "passage": "Shared transition area (hall, stairs)"}

# key -> (default, min, max)
GLOBAL_NUMBERS: dict[str, tuple[int, int, int]] = {
    "window_days": (90, 7, 365),
    "half_life_days": (30, 0, 365),
    "refresh_minutes": (60, 15, 1440),
    "probability_threshold": (60, 5, 99),
    "min_days": (3, 1, 30),
    "min_entries": (10, 1, 500),
}
MAX_CONTEXTS = 8


def _int(value: Any, default: int, low: int, high: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _horizon(value: Any, default: int | None) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return v if v in HORIZONS else default


@dataclass(frozen=True, slots=True)
class ForecastDefaults:
    enabled: bool = True
    window_days: int = 90
    half_life_days: int = 30
    refresh_minutes: int = 60
    horizon_minutes: int = 15
    probability_threshold: int = 60
    confidence_threshold: str = "moderate"
    min_days: int = 3
    min_entries: int = 10
    # None = automatic (bed and PC activity signals of the configured rooms).
    contexts: tuple[str, ...] | None = None
    # None = automatic (a Workday integration sensor if there is one), "" = none (Monday-Friday).
    workday_entity: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ForecastDefaults:
        data = data if isinstance(data, dict) else {}
        numbers = {k: _int(data.get(k), d, lo, hi) for k, (d, lo, hi) in GLOBAL_NUMBERS.items()}
        contexts = data.get("contexts")
        ctx = None if contexts is None else tuple(
            dict.fromkeys(c for c in contexts if isinstance(c, str) and "." in c))[:MAX_CONTEXTS]
        workday = data.get("workday_entity")
        conf = data.get("confidence_threshold")
        return cls(
            enabled=data.get("enabled", True) is not False,
            horizon_minutes=_horizon(data.get("horizon_minutes"), 15) or 15,
            confidence_threshold=conf if conf in CONFIDENCE_THRESHOLDS else "moderate",
            contexts=ctx,
            workday_entity=workday if isinstance(workday, str) else None,
            **numbers,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled, "window_days": self.window_days, "half_life_days": self.half_life_days,
            "refresh_minutes": self.refresh_minutes, "horizon_minutes": self.horizon_minutes,
            "probability_threshold": self.probability_threshold, "confidence_threshold": self.confidence_threshold,
            "min_days": self.min_days, "min_entries": self.min_entries,
            "contexts": list(self.contexts) if self.contexts is not None else None,
            "workday_entity": self.workday_entity,
        }


@dataclass(frozen=True, slots=True)
class RoomForecastSettings:
    enabled: bool | None = None
    horizon_minutes: int | None = None
    probability_threshold: int | None = None
    confidence_threshold: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> RoomForecastSettings:
        data = data if isinstance(data, dict) else {}
        enabled = data.get("enabled")
        thr = data.get("probability_threshold")
        conf = data.get("confidence_threshold")
        lo, hi = GLOBAL_NUMBERS["probability_threshold"][1:]
        return cls(
            enabled=enabled if isinstance(enabled, bool) else None,
            horizon_minutes=_horizon(data.get("horizon_minutes"), None),
            probability_threshold=None if thr is None else _int(thr, 60, lo, hi),
            confidence_threshold=conf if conf in CONFIDENCE_THRESHOLDS else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "horizon_minutes": self.horizon_minutes,
                "probability_threshold": self.probability_threshold,
                "confidence_threshold": self.confidence_threshold}

    @property
    def is_empty(self) -> bool:
        return self == RoomForecastSettings()


@dataclass(frozen=True, slots=True)
class Connection:
    """Two rooms a person can walk between directly."""

    a: str
    b: str
    kind: str = "door"

    @classmethod
    def make(cls, a: str, b: str, kind: str) -> Connection:
        a, b = sorted((a, b))
        return cls(a, b, kind if kind in CONNECTION_KINDS else "door")

    @property
    def pair(self) -> frozenset[str]:
        return frozenset((self.a, self.b))

    def as_dict(self) -> dict[str, Any]:
        return {"a": self.a, "b": self.b, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class EffectiveRoomSettings:
    enabled: bool
    horizon_minutes: int
    probability_threshold: int
    confidence_threshold: str
    overridden: frozenset[str]


@dataclass(frozen=True, slots=True)
class ForecastSettings:
    defaults: ForecastDefaults = field(default_factory=ForecastDefaults)
    rooms: dict[str, RoomForecastSettings] = field(default_factory=dict)
    # None until a person edits the connections: the suggested ones are used.
    connections: tuple[Connection, ...] | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ForecastSettings:
        data = data if isinstance(data, dict) else {}
        rooms = {str(a): RoomForecastSettings.from_dict(r) for a, r in (data.get("rooms") or {}).items()}
        conns = data.get("connections")
        connections = None
        if isinstance(conns, list):
            seen: dict[frozenset[str], Connection] = {}
            for c in conns:
                if isinstance(c, dict) and isinstance(c.get("a"), str) and isinstance(c.get("b"), str) \
                        and c["a"] != c["b"]:
                    conn = Connection.make(c["a"], c["b"], str(c.get("kind") or "door"))
                    seen[conn.pair] = conn
            connections = tuple(seen.values())
        return cls(defaults=ForecastDefaults.from_dict(data.get("global")),
                   rooms={a: r for a, r in rooms.items() if not r.is_empty}, connections=connections)

    def as_dict(self) -> dict[str, Any]:
        return {"global": self.defaults.as_dict(),
                "rooms": {a: r.as_dict() for a, r in self.rooms.items() if not r.is_empty},
                "connections": None if self.connections is None else [c.as_dict() for c in self.connections]}

    def room(self, area_id: str) -> RoomForecastSettings:
        return self.rooms.get(area_id) or RoomForecastSettings()

    def effective(self, area_id: str) -> EffectiveRoomSettings:
        g, r = self.defaults, self.room(area_id)
        overridden = frozenset(k for k, v in r.as_dict().items() if v is not None)
        return EffectiveRoomSettings(
            enabled=g.enabled and (r.enabled if r.enabled is not None else True),
            horizon_minutes=r.horizon_minutes or g.horizon_minutes,
            probability_threshold=r.probability_threshold or g.probability_threshold,
            confidence_threshold=r.confidence_threshold or g.confidence_threshold,
            overridden=overridden,
        )

    def with_room(self, area_id: str, room: RoomForecastSettings) -> ForecastSettings:
        rooms = {**self.rooms, area_id: room}
        return replace(self, rooms={a: v for a, v in rooms.items() if not v.is_empty})
