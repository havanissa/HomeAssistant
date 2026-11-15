"""Zones, shared spaces, relay lighting outputs and Room Supervisor settings (0.6.0, Phase 4.5).

Pure configuration models (no Home Assistant dependency), stored in the
lighting store (``lighting_config.py``) and edited from the room card.

Zones
-----
A **zone** is a logical part of a room (the sink, the shower, the kitchen
island), defined here and never in Home Assistant's area registry. It has:

* presence inputs: a subset of the room's own signals (their kind, role and
  "inverted" flag come from the room; a signal excluded in the Presence tab is
  excluded in its zones too),
* detection rules: ``activation`` (``possible``: the first evidence activates
  the zone; ``confirmed``: it waits for corroborated presence) and optional
  timing overrides (confirmation window, hold after the last detection, exit
  confirmation, dropout grace) on top of the room's timing,
* an availability fail-safe (``unavailable``): while every zone signal is
  unavailable the zone ``hold``\\ s its last state (default), is treated as
  ``vacant``, or as ``occupied`` while the room itself is occupied,
* lighting: targets (lights or relay outputs the room controls) with an action
  (On with brightness / colour temperature / transition, Off, or Unchanged),
  a ``priority`` (higher wins), the manual-override policy and a daylight rule,
* ``holds_room``: while the zone is occupied the room is not declared vacant.

A zone is never active while its room is vacant, and predictions never make a
zone active: zone occupancy comes from sensor states only.

Manual overrides (``manual``)
-----------------------------
``respect`` (default): a person's manual change of a target is respected by
the zone, like by every other automatic rule. ``override``: the zone takes
control of a target that has a manual override made *before* the zone became
active (e.g. the shower always gets full light); a change a person makes while
the zone is active is respected.

Shared spaces
-------------
A **shared space** groups rooms that form one open-plan area (Køkur / Stova).
Each room keeps its own occupancy, zones, lights and daylight rules. The space
adds:

* a shared mood: an optional mood for the space (rooms without their own room
  mood use it instead of the global mood),
* shared targets: a light managed by two member rooms is never switched off by
  one room while another member room is occupied, and is commanded by one room
  at a time (the first occupied member room in the space's order),
* ``vacancy``: ``independent`` (default; each room runs its own vacancy
  action) or ``together`` (a member room that becomes vacant keeps its lights
  until every member room is vacant).

Relay lighting outputs
----------------------
A ``switch.*`` entity is a lighting output only when the user explicitly adds
it to a room's ``relays``. It is then managed like an on/off light (profiles
per single light, possible occupancy, vacancy, zones, manual overrides, Room
Supervisor) and commanded with ``switch.turn_on`` / ``switch.turn_off``.
Switches are never reinterpreted as lights automatically.

Room Supervisor
---------------
Global defaults with per-room overrides (``None``: inherit). ``mode``:
``observing`` (default: compares expected and actual states and reports,
never sends), ``automatic`` (also corrects genuine discrepancies, within the
retry and rate limits) or ``off``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .const import (
    LUX_MAX_THRESHOLD,
    LUX_MIN,
    SUPERVISOR_LIMITS,
    TIMING_PARAMETERS,
    TRANSITION_MAX,
    ZONE_PRIORITY_DEFAULT,
    ZONE_PRIORITY_MAX,
    ZONE_TIMING_KEYS,
)

KELVIN_MIN = 1000
KELVIN_MAX = 10000
MAX_ZONES = 8
MAX_ZONE_TARGETS = 24
MAX_SPACES = 16


class ZoneActivation(StrEnum):
    POSSIBLE = "possible"      # first evidence (one PIR movement) activates the zone
    CONFIRMED = "confirmed"    # corroborated presence


class ZoneUnavailable(StrEnum):
    HOLD = "hold"              # keep the last state (never activates the zone)
    VACANT = "vacant"          # release the zone
    OCCUPIED = "occupied"      # treat as occupied while the room is occupied


class ZoneManual(StrEnum):
    RESPECT = "respect"
    OVERRIDE = "override"


class ZoneDaylight(StrEnum):
    ROOM = "room"              # an off light is switched on only when the room's daylight rules allow it
    ALWAYS = "always"          # switched on whatever the daylight
    BELOW = "below"            # the zone's own darkness threshold (lx)


class ZoneRelease(StrEnum):
    RESTORE = "restore"        # lights the zone switched on that no remaining rule sets are switched off
    LEAVE = "leave"            # they stay as they are


class ZoneAction(StrEnum):
    ON = "on"
    OFF = "off"
    UNCHANGED = "unchanged"    # claimed by the zone and left as it is (lower rules do not change it)


class SpaceVacancy(StrEnum):
    INDEPENDENT = "independent"
    TOGETHER = "together"


class SupervisorMode(StrEnum):
    OFF = "off"
    OBSERVING = "observing"
    AUTOMATIC = "automatic"


def _enum(cls: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return cls(value)
    except ValueError:
        return default


def _int(value: Any, low: int, high: int, default: int | None = None) -> int | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(min(max(round(float(value)), low), high))
    except (TypeError, ValueError):
        return default


def _float(value: Any, low: float, high: float, default: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(min(max(float(value), low), high))
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(v for v in value if isinstance(v, str) and "." in v))


def slug(name: str, existing: set[str], fallback: str = "zone") -> str:
    base = "".join(ch if ch.isalnum() else "_" for ch in name.casefold()).strip("_")
    base = "_".join(filter(None, base.split("_"))) or fallback
    key, n = base, 2
    while key in existing:
        key, n = f"{base}_{n}", n + 1
    return key


# ------------------------------------------------------------------------ zones
@dataclass(frozen=True, slots=True)
class ZoneTarget:
    """What a zone does to one light or relay output while it is the winning rule."""

    entity_id: str
    action: ZoneAction = ZoneAction.ON
    brightness_pct: int | None = None
    color_temp_kelvin: int | None = None
    transition: float | None = None

    @classmethod
    def from_dict(cls, data: Any) -> ZoneTarget | None:
        if not isinstance(data, dict) or not isinstance(data.get("entity_id"), str) or "." not in data["entity_id"]:
            return None
        action = _enum(ZoneAction, data.get("action"), ZoneAction.ON)
        on = action == ZoneAction.ON
        return cls(
            entity_id=data["entity_id"], action=action,
            brightness_pct=_int(data.get("brightness_pct"), 1, 100) if on else None,
            color_temp_kelvin=_int(data.get("color_temp_kelvin"), KELVIN_MIN, KELVIN_MAX) if on else None,
            transition=_float(data.get("transition"), 0, TRANSITION_MAX) if action != ZoneAction.UNCHANGED else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {"entity_id": self.entity_id, "action": self.action.value, "brightness_pct": self.brightness_pct,
                "color_temp_kelvin": self.color_temp_kelvin, "transition": self.transition}

    def describe(self) -> str:
        if self.action == ZoneAction.OFF:
            return "off"
        if self.action == ZoneAction.UNCHANGED:
            return "unchanged"
        parts = [f"{self.brightness_pct} %" if self.brightness_pct is not None else "on"]
        if self.color_temp_kelvin:
            parts.append(f"{self.color_temp_kelvin} K")
        return ", ".join(parts)


def _timing(data: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(data, dict):
        return out
    for key in ZONE_TIMING_KEYS:
        _default, low, high = TIMING_PARAMETERS[key]
        if (value := _int(data.get(key), low, high)) is not None:
            out[key] = value
    return out


@dataclass(frozen=True, slots=True)
class ZoneConfig:
    """One logical zone of a room (see the module docstring)."""

    id: str
    name: str
    enabled: bool = True
    sensors: tuple[str, ...] = ()
    activation: ZoneActivation = ZoneActivation.POSSIBLE
    timing: dict[str, int] = field(default_factory=dict)
    unavailable: ZoneUnavailable = ZoneUnavailable.HOLD
    holds_room: bool = True
    priority: int = ZONE_PRIORITY_DEFAULT
    manual: ZoneManual = ZoneManual.RESPECT
    daylight: ZoneDaylight = ZoneDaylight.ROOM
    on_below: int | None = None
    release: ZoneRelease = ZoneRelease.RESTORE
    targets: tuple[ZoneTarget, ...] = ()

    @classmethod
    def from_dict(cls, data: Any) -> ZoneConfig | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            return None
        name = str(data.get("name") or data["id"]).strip()[:60] or data["id"]
        targets: list[ZoneTarget] = []
        for item in data.get("targets") or []:
            target = ZoneTarget.from_dict(item)
            if target is not None and all(t.entity_id != target.entity_id for t in targets):
                targets.append(target)
        return cls(
            id=data["id"], name=name,
            enabled=data.get("enabled", True) is not False,
            sensors=_ids(data.get("sensors")),
            activation=_enum(ZoneActivation, data.get("activation"), ZoneActivation.POSSIBLE),
            timing=_timing(data.get("timing")),
            unavailable=_enum(ZoneUnavailable, data.get("unavailable"), ZoneUnavailable.HOLD),
            holds_room=data.get("holds_room", True) is not False,
            priority=_int(data.get("priority"), 1, ZONE_PRIORITY_MAX, ZONE_PRIORITY_DEFAULT) or ZONE_PRIORITY_DEFAULT,
            manual=_enum(ZoneManual, data.get("manual"), ZoneManual.RESPECT),
            daylight=_enum(ZoneDaylight, data.get("daylight"), ZoneDaylight.ROOM),
            on_below=_int(data.get("on_below"), LUX_MIN, LUX_MAX_THRESHOLD),
            release=_enum(ZoneRelease, data.get("release"), ZoneRelease.RESTORE),
            targets=tuple(targets[:MAX_ZONE_TARGETS]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "enabled": self.enabled, "sensors": list(self.sensors),
            "activation": self.activation.value, "timing": dict(self.timing),
            "unavailable": self.unavailable.value, "holds_room": self.holds_room, "priority": self.priority,
            "manual": self.manual.value, "daylight": self.daylight.value, "on_below": self.on_below,
            "release": self.release.value, "targets": [t.as_dict() for t in self.targets],
        }

    def target(self, entity_id: str) -> ZoneTarget | None:
        return next((t for t in self.targets if t.entity_id == entity_id), None)


def zones_from(data: Any) -> tuple[ZoneConfig, ...]:
    out: list[ZoneConfig] = []
    for item in data if isinstance(data, (list, tuple)) else []:
        zone = ZoneConfig.from_dict(item)
        if zone is not None and all(z.id != zone.id for z in out):
            out.append(zone)
    return tuple(out[:MAX_ZONES])


# ---------------------------------------------------------------- shared spaces
@dataclass(frozen=True, slots=True)
class SharedSpace:
    """Rooms that form one open-plan space (see the module docstring)."""

    id: str
    name: str
    rooms: tuple[str, ...] = ()
    mood_override: str | None = None
    vacancy: SpaceVacancy = SpaceVacancy.INDEPENDENT

    @classmethod
    def from_dict(cls, data: Any) -> SharedSpace | None:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            return None
        rooms = tuple(dict.fromkeys(r for r in data.get("rooms") or [] if isinstance(r, str) and r))
        mood = data.get("mood_override")
        return cls(
            id=data["id"], name=str(data.get("name") or data["id"]).strip()[:60] or data["id"], rooms=rooms,
            mood_override=(mood.strip() or None) if isinstance(mood, str) else None,
            vacancy=_enum(SpaceVacancy, data.get("vacancy"), SpaceVacancy.INDEPENDENT),
        )

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "rooms": list(self.rooms), "mood_override": self.mood_override,
                "vacancy": self.vacancy.value}


def spaces_from(data: Any) -> tuple[SharedSpace, ...]:
    out: list[SharedSpace] = []
    for item in data if isinstance(data, (list, tuple)) else []:
        space = SharedSpace.from_dict(item)
        if space is not None and all(s.id != space.id for s in out):
            out.append(space)
    return tuple(out[:MAX_SPACES])


# ------------------------------------------------------------------- supervisor
@dataclass(frozen=True, slots=True)
class SupervisorSettings:
    """Room Supervisor settings. In a room, ``None`` means "use the global default"."""

    mode: SupervisorMode | None = None
    verify_delay: float | None = None
    reconcile_interval: float | None = None
    health_interval: float | None = None
    diagnostics_interval: float | None = None
    max_retries: float | None = None
    retry_delay: float | None = None
    max_per_hour: float | None = None
    brightness_tolerance: float | None = None
    kelvin_tolerance: float | None = None
    # Correct a state changed by something other than a person (an automation, an unattributed change) once.
    correct_external: bool | None = None

    @classmethod
    def from_dict(cls, data: Any, *, defaults: bool = False) -> SupervisorSettings:
        data = data if isinstance(data, dict) else {}
        values: dict[str, Any] = {}
        for key, (default, low, high) in SUPERVISOR_LIMITS.items():
            values[key] = _float(data.get(key), low, high, default if defaults else None)
        mode = _enum(SupervisorMode, data.get("mode"), None)
        external = data.get("correct_external")
        return cls(
            mode=mode or (SupervisorMode.OBSERVING if defaults else None),
            correct_external=external if isinstance(external, bool) else (False if defaults else None),
            **values,
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {key: getattr(self, key) for key in SUPERVISOR_LIMITS}
        out["mode"] = self.mode.value if self.mode else None
        out["correct_external"] = self.correct_external
        return out

    def merged(self, room: SupervisorSettings) -> SupervisorSettings:
        """These (global) settings with a room's own values applied."""
        changes = {k: v for k in (*SUPERVISOR_LIMITS, "mode", "correct_external")
                   if (v := getattr(room, k)) is not None}
        return replace(self, **changes)


GLOBAL_SUPERVISOR = SupervisorSettings.from_dict({}, defaults=True)
