"""Per-room health, split into occupancy, lighting and configuration.

Built from the structured problems of the Phase 0.1.1 validation engine
(nothing is validated twice) plus the occupancy engine's live view of its
signals (unavailable or stuck sensors). The split keeps an offline light
from making occupancy detection look degraded:

* **occupancy** - can presence and vacancy be determined? Only signals.
* **lighting** - are the managed lights available, and can mood lighting
  work (automation conflicts, unsupported settings, failed commands)?
* **configuration** - are the room's assignments and settings valid?
* **daylight** (0.3.3) - are the light sensors usable? A light sensor problem
  never degrades occupancy or lighting health.
"""

from __future__ import annotations

from typing import Any

from .const import Classification, ProblemCategory, Severity
from .coordinator import RoomSnapshot, Snapshot
from .engine import RoomResult, SignalStatus
from .occupancy import AVAILABLE, PENDING
from .validation import ENTITY_PROBLEM_CODES

HEALTHY = Classification.HEALTHY.value
WARNING = Classification.WARNING.value
UNAVAILABLE = Classification.UNAVAILABLE.value
CONFIG_ERROR = Classification.CONFIG_ERROR.value

_HEALTH_WORDS = {
    "unavailable": "unavailable",
    "unknown": "has not reported a value",
    "not_provided": "not provided by its integration",
    "missing": "deleted",
    "disabled": "disabled",
    "pending": "still loading",
}


def _label(name: str | None, entity_id: str) -> str:
    return f"{name} ({entity_id})" if name and name != entity_id else entity_id


def _room(snapshot: Snapshot, area_id: str) -> RoomSnapshot | None:
    return next((r for r in snapshot.rooms if r.area_id == area_id), None)


def room_health(snapshot: Snapshot | None, area_id: str, result: RoomResult | None) -> dict[str, Any]:
    room = _room(snapshot, area_id) if snapshot else None
    problems = [
        p for p in (snapshot.problems if snapshot else ())
        if p.area_id == area_id and p.severity != Severity.INFO
    ]
    health_of = {s.entity_id: s.health for s in room.sensors} if room else {}

    # Occupancy -------------------------------------------------------------
    occ_reasons: list[str] = []
    unavailable_sensors: list[dict[str, Any]] = []
    if result is not None:
        for view in result.signals:
            t = view.track
            if view.status == SignalStatus.UNKNOWN:
                health = health_of.get(t.entity_id) or "unknown"
                if health == AVAILABLE:
                    health = "unrecognised_state"
                word = _HEALTH_WORDS.get(health, "reports a state that is not understood")
                unavailable_sensors.append({"entity_id": t.entity_id, "name": t.name, "health": health})
                if health != PENDING:
                    occ_reasons.append(f"{_label(t.name, t.entity_id)} {word}; not used as evidence")
            elif view.status == SignalStatus.STUCK:
                occ_reasons.append(
                    f"{_label(t.name, t.entity_id)} has been active too long; ignored as stuck"
                )
        if not result.usable:
            occ_status = UNAVAILABLE
            occ_reasons.insert(0, "No occupancy sensor is usable; the last state is kept")
        elif occ_reasons:
            occ_status = WARNING
        else:
            occ_status = HEALTHY
        usable = result.usable
    else:
        occ_status, usable = UNAVAILABLE, False
        occ_reasons.append("Occupancy engine not running")

    # Lighting --------------------------------------------------------------
    light_ids = {li.entity_id for li in room.lights} if room else set()
    unavailable_lights = [
        {"entity_id": li.entity_id, "name": li.name, "health": li.health}
        for li in (room.lights if room else ())
        if li.health not in (AVAILABLE, PENDING)
    ]
    light_reasons = [
        f"Light {_label(li['name'], li['entity_id'])} {_HEALTH_WORDS.get(li['health'], li['health'])}"
        for li in unavailable_lights
    ]
    # Mood lighting problems (conflicts, unsupported settings, failed commands):
    # one short line each by default; the full text is in ``details``.
    lighting_problems = [p for p in problems if p.aspect == "lighting" and p.code not in ENTITY_PROBLEM_CODES]
    daylight_problems = [p for p in problems if p.aspect == "daylight"]
    light_reasons += short_lighting_reasons(lighting_problems)
    light_details = [f"{p.title}: {p.explanation}" for p in lighting_problems]

    # Configuration ---------------------------------------------------------
    config_problems = [
        p for p in problems if p.category == ProblemCategory.CONFIGURATION and p.aspect not in ("lighting", "daylight")
    ]
    if any(p.severity == Severity.ERROR for p in config_problems):
        config_status = CONFIG_ERROR
    elif config_problems:
        config_status = WARNING
    else:
        config_status = HEALTHY
    config_reasons = [
        f"{p.title}: {p.name or p.entity_id}" if p.entity_id else p.title
        for p in config_problems
    ]
    config_details = [f"{p.title}: {p.explanation}" for p in config_problems]

    return {
        "occupancy": {
            "status": occ_status,
            "usable": usable,
            "reasons": occ_reasons,
            "unavailable_sensors": unavailable_sensors,
        },
        "lighting": {
            "status": WARNING if unavailable_lights or lighting_problems else HEALTHY,
            "reasons": light_reasons,
            "details": light_details,
            "unavailable_lights": unavailable_lights,
            "lights": len(light_ids),
        },
        "configuration": {
            "status": config_status,
            "reasons": config_reasons,
            "details": config_details,
        },
        "daylight": {
            "status": WARNING if daylight_problems else HEALTHY,
            "reasons": [f"{p.title}: {p.name or p.entity_id}" if p.entity_id else p.title for p in daylight_problems],
            "details": [f"{p.title}: {p.explanation}" for p in daylight_problems],
        },
    }


def short_lighting_reasons(problems) -> list[str]:
    """One concise line per kind of lighting problem (e.g. "2 lighting conflicts")."""
    out: list[str] = []
    conflicts = sorted({p.name or p.entity_id for p in problems if p.code == "automation_conflict"})
    if conflicts:
        n = len(conflicts)
        out.append(f"{n} lighting conflict{'s' if n != 1 else ''} ({', '.join(conflicts)})")
    for p in problems:
        name = p.name or p.entity_id or ""
        if p.code == "automation_conflict":
            continue
        if p.code == "lighting_unsupported":
            out.append(f"Some settings not supported by {name}")
        elif p.code == "group_overlap_unknown":
            out.append(f"Possible duplicate control: {name}")
        elif p.code == "group_member_conflict":
            out.append(f"Group and member in two rooms: {name}")
        elif p.code == "lighting_execution_failed":
            out.append("Last light command failed")
        elif p.code == "mood_unrepresentable":
            out.append(p.title)
        else:
            out.append(f"{p.title}: {name}" if name else p.title)
    return out
