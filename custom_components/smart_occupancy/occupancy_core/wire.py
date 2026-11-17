"""JSON forms of the engine's configuration and results, with strict validation (shared by both sides).

Everything that crosses the bridge between the integration and the App goes
through these functions, so both sides read and write exactly the same shapes.
Times are ISO 8601 with microseconds and a time zone. Parsing never guesses:
anything unexpected raises ``WireError``.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any

from .engine import (
    MAX_REASON_LENGTH,
    Confidence,
    RoomResult,
    SignalStatus,
    SignalTrack,
    SignalView,
    Value,
)
from .house import HouseSpec, RoomSpec
from .types import TIMING_PARAMETERS, OccupancyState, OccupancyTiming, SensorAssignment, SensorKind, SensorRole

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
AREA_ID = re.compile(r"^[\w\-]{1,255}$")
MAX_ROOMS = 500
MAX_SENSORS = 100
MAX_TEXT = 1000


class WireError(ValueError):
    """A message does not have the expected shape."""


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def parse_time(value: Any, key: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise WireError(f"{key}: not a time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise WireError(f"{key}: not a time") from err
    if parsed.tzinfo is None:
        raise WireError(f"{key}: time without a time zone")
    return parsed


def _text(value: Any, key: str, limit: int = MAX_TEXT, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise WireError(f"{key}: not a short text")
    return value


def _bool(value: Any, key: str, *, optional: bool = False) -> bool | None:
    if value is None and optional:
        return None
    if not isinstance(value, bool):
        raise WireError(f"{key}: not a boolean")
    return value


def _number(value: Any, key: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise WireError(f"{key}: not a number between {low} and {high}")
    return value


def _enum(enum_cls: type, value: Any, key: str) -> Any:
    try:
        return enum_cls(value)
    except ValueError as err:
        raise WireError(f"{key}: not a valid value") from err


# ---------------------------------------------------------------- the spec
def spec_to_wire(spec: HouseSpec) -> dict[str, Any]:
    return {**spec.as_dict(), "revision": spec.revision()}


def spec_from_wire(data: Any) -> HouseSpec:
    """The engines' configuration as sent by the integration (its revision must match the content)."""
    if not isinstance(data, dict) or not isinstance(data.get("rooms"), dict):
        raise WireError("spec: rooms missing")
    rooms_in = data["rooms"]
    if len(rooms_in) > MAX_ROOMS:
        raise WireError("spec: too many rooms")
    rooms = []
    for area_id, raw in rooms_in.items():
        if not isinstance(area_id, str) or not AREA_ID.match(area_id) or not isinstance(raw, dict):
            raise WireError("spec: invalid room")
        sensors_in = raw.get("sensors")
        if not isinstance(sensors_in, list) or len(sensors_in) > MAX_SENSORS:
            raise WireError(f"spec {area_id}: invalid sensors")
        sensors = []
        seen = set()
        for s in sensors_in:
            if not isinstance(s, dict) or not isinstance(s.get("entity_id"), str) or not ENTITY_ID.match(s["entity_id"]):
                raise WireError(f"spec {area_id}: invalid sensor")
            if s["entity_id"] in seen:
                raise WireError(f"spec {area_id}: duplicate sensor")
            seen.add(s["entity_id"])
            sensors.append(SensorAssignment(
                entity_id=s["entity_id"], kind=_enum(SensorKind, s.get("kind"), "kind"),
                role=_enum(SensorRole, s.get("role"), "role"), inverted=bool(_bool(s.get("inverted"), "inverted")),
                enabled=bool(_bool(s.get("enabled"), "enabled")),
            ))
        timing_in = raw.get("timing")
        if not isinstance(timing_in, dict) or set(timing_in) != set(TIMING_PARAMETERS):
            raise WireError(f"spec {area_id}: invalid timing")
        timing = {}
        for key, (_default, low, high) in TIMING_PARAMETERS.items():
            value = timing_in[key]
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise WireError(f"spec {area_id}: invalid {key}")
            timing[key] = value
        adjacent = raw.get("adjacent")
        if not isinstance(adjacent, list) or len(adjacent) > MAX_ROOMS or not all(
            isinstance(a, str) and AREA_ID.match(a) and a != area_id for a in adjacent
        ):
            raise WireError(f"spec {area_id}: invalid adjacent rooms")
        rooms.append(RoomSpec(area_id, tuple(sensors), OccupancyTiming(**timing), tuple(adjacent)))
    spec = HouseSpec(tuple(rooms))
    if data.get("revision") is not None and data.get("revision") != spec.revision():
        raise WireError("spec: the revision does not match the content")
    return spec


# ------------------------------------------------------------------ results
def result_to_wire(result: RoomResult, visit_confirmed: bool) -> dict[str, Any]:
    """One room's evaluated result, complete (every field a consumer of ``RoomResult`` reads)."""
    c = result.confidence
    return {
        "state": result.state.value,
        "occupied": result.occupied,
        "reason": result.reason,
        "reason_code": result.reason_code,
        "confidence": {"score": c.score, "evidence": c.evidence, "coverage": c.coverage, "basis": c.basis},
        "usable": result.usable,
        "last_activity": iso(result.last_activity),
        "last_activity_source": result.last_activity_source,
        "quiet_since": iso(result.quiet_since),
        "vacancy_at": iso(result.vacancy_at),
        "required_quiet": result.required_quiet,
        "vacancy_evidence": list(result.vacancy_evidence),
        "starting_up": result.starting_up,
        "state_since": iso(result.state_since),
        "visit_confirmed": visit_confirmed,
        "signals": [
            {
                "entity_id": v.track.entity_id,
                "name": v.track.name,
                "value": v.track.value.value,
                "status": v.status.value,
                "since": iso(v.track.since),
                "active_start": iso(v.track.active_start),
                "active_end": iso(v.track.active_end),
                "last_transition": iso(v.track.last_transition),
                "lost_while_active": v.track.lost_while_active,
            }
            for v in result.signals
        ],
    }


def result_from_wire(data: Any, room: RoomSpec) -> RoomResult:
    """Rebuild a ``RoomResult`` from its JSON form; the signals must be exactly the room's configured signals."""
    if not isinstance(data, dict):
        raise WireError("result: not an object")
    signals_in = data.get("signals")
    if not isinstance(signals_in, list) or [s.get("entity_id") if isinstance(s, dict) else None
                                            for s in signals_in] != [s.entity_id for s in room.sensors]:
        raise WireError("result: the signals differ from the room's configuration")
    views = []
    for assignment, s in zip(room.sensors, signals_in, strict=True):
        track = SignalTrack(assignment)
        track.name = _text(s.get("name"), "signal name", MAX_REASON_LENGTH)
        track.value = _enum(Value, s.get("value"), "signal value")
        track.since = parse_time(s.get("since"), "since")
        track.active_start = parse_time(s.get("active_start"), "active_start")
        track.active_end = parse_time(s.get("active_end"), "active_end")
        track.last_transition = parse_time(s.get("last_transition"), "last_transition")
        track.lost_while_active = bool(_bool(s.get("lost_while_active"), "lost_while_active"))
        views.append(SignalView(track, _enum(SignalStatus, s.get("status"), "signal status")))
    conf = data.get("confidence")
    if not isinstance(conf, dict):
        raise WireError("result: confidence missing")
    evidence_in = data.get("vacancy_evidence")
    if not isinstance(evidence_in, list) or len(evidence_in) > 10:
        raise WireError("result: invalid vacancy evidence")
    required = data.get("required_quiet")
    if required is not None:
        required = int(_number(required, "required_quiet", 0, 100_000))
    state = _enum(OccupancyState, data.get("state"), "state")
    occupied = _bool(data.get("occupied"), "occupied", optional=True)
    _bool(data.get("visit_confirmed"), "visit_confirmed")
    return RoomResult(
        state=state,
        occupied=occupied,
        reason=_text(data.get("reason"), "reason", MAX_REASON_LENGTH, optional=False) or "",
        reason_code=_text(data.get("reason_code"), "reason_code", 64, optional=False) or "",
        confidence=Confidence(
            score=int(_number(conf.get("score"), "score", 0, 100)),
            evidence=float(_number(conf.get("evidence"), "evidence", 0, 1)),
            coverage=float(_number(conf.get("coverage"), "coverage", 0, 1)),
            basis=_text(conf.get("basis"), "basis", MAX_REASON_LENGTH, optional=False) or "",
        ),
        signals=tuple(views),
        usable=bool(_bool(data.get("usable"), "usable")),
        last_activity=parse_time(data.get("last_activity"), "last_activity"),
        last_activity_source=_text(data.get("last_activity_source"), "last_activity_source", MAX_REASON_LENGTH),
        quiet_since=parse_time(data.get("quiet_since"), "quiet_since"),
        vacancy_at=parse_time(data.get("vacancy_at"), "vacancy_at"),
        required_quiet=required,
        vacancy_evidence=tuple(_text(e, "vacancy evidence", MAX_REASON_LENGTH, optional=False) or ""
                               for e in evidence_in),
        starting_up=bool(_bool(data.get("starting_up"), "starting_up")),
        state_since=parse_time(data.get("state_since"), "state_since"),
    )


def decision_of(data: dict[str, Any]) -> dict[str, Any]:
    """The decision part of a result's JSON form (what another engine may adopt)."""
    return {k: data.get(k) for k in ("state", "occupied", "visit_confirmed", "state_since", "last_activity",
                                     "last_activity_source")}
