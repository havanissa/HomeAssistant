"""What the native entities show (Phase A6): the App builds every entity's value and attributes.

Until Phase A6 the integration's entity platforms computed these from the
engines; the companion integration now publishes exactly the same values and
attributes from the views built here, so automations and dashboards see no
difference. One function per view domain:

* :func:`occupancy_room` - the room occupancy entities (state, occupied, confidence, last activity, reason, the
  helper comparison) from a ``RoomResult``;
* :func:`lighting_room` - the mood lighting and daylight sensors from the lighting controller;
* :func:`forecast_view` - every room's forecast entities, the predicted next room and the learning status;
* :func:`system_view` - the global diagnostic sensors, every room's observation sensor, the mood mismatch and
  the built-in mood selector, from the diagnostics snapshot.
"""

from __future__ import annotations

from typing import Any

from smart_occupancy.const import PHASE, Classification, OccupancyState, OperatingMode, ProblemCategory, Severity
from smart_occupancy.engine import RoomResult, SignalStatus
from smart_occupancy.health import room_health
from smart_occupancy.occupancy import AVAILABLE, CONFIGURATION_INVALID, OPERATIONAL_UNAVAILABLE, PENDING
from smart_occupancy.snapshot import Snapshot


def view(value: Any, attributes: dict[str, Any] | None = None, *, available: bool = True) -> dict[str, Any]:
    return {"value": value, "attributes": _jsonable(attributes or {}), "available": available}


def _iso(value: Any) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None and hasattr(value, "isoformat") else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):  # enums
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat(timespec="seconds")
    return str(value)


# ------------------------------------------------------------------ occupancy
def occupancy_room(area_id: str, result: RoomResult | None, *, room_name: str, config: Any, snapshot: Snapshot | None,
                   lighting_active: bool, manual_mode: bool, comparison: Any, source_name: str | None,
                   mode: OperatingMode) -> dict[str, Any] | None:
    """The six room occupancy entities (``None`` when the engine has no result yet)."""
    if result is None:
        return None
    room_config = config.room(area_id)
    health = room_health(snapshot, area_id, result)

    def labels(*statuses: SignalStatus) -> list[str]:
        return result.names(*statuses)

    active = [
        v.track.label() + (" (dropout grace)" if v.status == SignalStatus.GRACE else "")
        for v in result.views(SignalStatus.ACTIVE, SignalStatus.GRACE)
    ]
    state_attributes = {
        "integration": "smart_occupancy",
        "area_id": area_id,
        "room": room_name,
        "occupied": result.occupied,
        "state_since": _iso(result.state_since),
        "confidence": result.confidence.score,
        "reason": result.reason,
        "reason_code": result.reason_code,
        "active_sensors": active,
        "recent_door_movement": labels(SignalStatus.RECENT),
        "stuck_sensors": labels(SignalStatus.STUCK),
        "unavailable_sensors": health["occupancy"]["unavailable_sensors"],
        "pending_vacancy": result.state == OccupancyState.POSSIBLE_VACANCY,
        "vacancy_confirm_at": _iso(result.vacancy_at),
        "quiet_since": _iso(result.quiet_since),
        "required_quiet_seconds": result.required_quiet,
        "vacancy_evidence": list(result.vacancy_evidence),
        "starting_up": result.starting_up,
        "last_activity": _iso(result.last_activity),
        "last_activity_source": result.last_activity_source,
        "signals": [v.as_dict() for v in result.signals],
        "health": health,
        "occupancy_usable": health["occupancy"]["usable"],
        "operating_mode": mode.value,
        "observe_only": not lighting_active,
        "lighting_control": lighting_active,
        "lighting_paused": lighting_active and manual_mode,
        "comparison": ({"helper": room_config.reference_occupancy_entity, **comparison.as_dict()}
                       if comparison else None),
        "timing": config.timing_for(area_id).as_dict(),
        "adjacent_rooms": list(room_config.adjacent_rooms),
    }
    out = {
        "occupied": view(result.occupied),
        "state": view(result.state.value, state_attributes),
        "confidence": view(result.confidence.score, {**result.confidence.as_dict(),
                                                     "meaning": "evidence quality, not predictive accuracy"}),
        "last_activity": view(result.last_activity.isoformat() if result.last_activity else None,
                              {"source": result.last_activity_source, "source_name": source_name}
                              if result.last_activity_source else {}),
        "reason": view(result.reason, {"reason_code": result.reason_code}),
    }
    if room_config.reference_occupancy_entity:
        agreement = comparison.reported_agreement if comparison else None
        value = "unknown" if agreement is None else ("agree" if agreement else "disagree")
        out["helper_comparison"] = view(value, {"helper": room_config.reference_occupancy_entity,
                                                **(comparison.as_dict() if comparison else {})})
    return out


# ------------------------------------------------------------------- lighting
def lighting_room(ctrl: Any, area_id: str) -> dict[str, Any]:
    """The mood lighting and daylight sensors of one room (the controller's own diagnostics)."""
    diag = dict(ctrl.room_diagnostics(area_id))
    zones = diag.pop("zones", None) or {}
    supervisor = diag.pop("supervisor", None) or {}
    diag["active_zones"] = sorted(z["name"] for z in zones.get("status", []) if z.get("active"))
    diag["supervisor"] = supervisor.get("status")
    diag["supervisor_discrepancies"] = supervisor.get("discrepancies")
    diag.pop("last_external_changes", None)
    mode = diag.get("control_mode")
    rt = ctrl.runtime.get(area_id)
    activation = rt.activation if rt else None
    daylight = dict(ctrl.daylight.status(area_id).summary())
    daylight_attributes = {**daylight, "last_activation_at": rt.last_activation_at if rt else None,
                           "activation": activation.get("text") if activation else None}
    return {
        "mood_lighting": view(mode, {"integration": "smart_occupancy", **diag}),
        "daylight": view(daylight.get("decision"), daylight_attributes),
    }


def lighting_view(ctrl: Any, area_ids: list[str]) -> dict[str, Any]:
    rooms = {a: lighting_room(ctrl, a) for a in area_ids}
    overrides = sum(len(getattr(rt, "overrides", {}) or {}) for rt in ctrl.runtime.values())
    return {"rooms": rooms, "manual_mode": bool(ctrl.manual_mode),
            "summary": {"active_rooms": sorted(a for a in area_ids if ctrl.room_active(a)), "overrides": overrides}}


# ------------------------------------------------------------------- forecast
def forecast_view(mgr: Any, area_ids: list[str]) -> dict[str, Any]:
    from smart_occupancy.forecast import STATUS_DISABLED, STATUS_OK, STATUS_UNAVAILABLE  # noqa: PLC0415
    from smart_occupancy.forecast_model import HORIZONS  # noqa: PLC0415

    rooms: dict[str, Any] = {}
    for area_id in area_ids:
        rf = mgr.room(area_id)
        if rf is None:
            rooms[area_id] = {"forecast": None, "forecast_confidence": None, "predicted": None}
            continue
        available = rf.status not in (STATUS_DISABLED, STATUS_UNAVAILABLE)
        common = {"status": rf.status, "horizon_minutes": rf.horizon, "observation_only": True}
        attributes = dict(common)
        for h in HORIZONS:
            p = rf.probabilities.get(h) if rf.probabilities else None
            attributes[f"probability_{h}_min"] = None if p is None else round(p * 100)
        attributes["confidence"] = rf.confidence.level if rf.confidence else None
        attributes["predicted"] = rf.predicted
        attributes["threshold"] = rf.threshold
        attributes["explanation"] = rf.explanation
        attributes["window"] = rf.window.get("text") if rf.window else None
        attributes["schedule"] = {f"{h}_min": round(v * 100) for h, v in (rf.schedule or {}).items()} or None
        attributes["entry_probability"] = {f"{h}_min": round(v * 100) for h, v in (rf.entry or {}).items()} or None
        attributes["factors"] = [f.text for f in rf.factors] or None
        attributes["updated"] = rf.updated
        p = rf.probability
        rooms[area_id] = {
            "forecast": view(round(p * 100) if rf.status == STATUS_OK and p is not None else None, attributes,
                             available=available),
            "forecast_confidence": view(rf.confidence.level if rf.confidence else None,
                                        {**common, "score": rf.confidence.score, "reasons": rf.confidence.reasons,
                                         "components": rf.confidence.components} if rf.confidence else common,
                                        available=available),
            "predicted": view(rf.predicted if rf.status == STATUS_OK else None,
                              {**common, "probability": None if p is None else round(p * 100),
                               "threshold": rf.threshold, "confidence": rf.confidence.level if rf.confidence else None,
                               "minimum_confidence": rf.confidence_threshold, "explanation": rf.explanation},
                              available=available),
        }
    nr = mgr.next_room
    name = mgr.name
    if nr:
        next_room = view(name(nr["area_id"]), {
            "area_id": nr["area_id"], "probability": round(nr["probability"] * 100), "from_area_id": nr["source"],
            "from": name(nr["source"]), "observation_only": True,
            "candidates": [{"area_id": a, "name": name(a), "probability": round(p * 100)} for a, p in nr["candidates"]]})
    else:
        next_room = view("none", {"area_id": None, "observation_only": True})
    st = mgr.status.as_dict()
    learning = view(mgr.status.state, {
        "detail": st["detail"], "days_learned": len(mgr.days), "history_from": st["recorder_oldest"],
        "window_days": mgr.settings.defaults.window_days, "last_update": st["last_update"], "queries": st["queries"],
        "last_error": st["last_error"], "observation_only": True})
    return {"rooms": rooms, "predicted_next_room": next_room, "forecast_learning": learning}


# --------------------------------------------------------------------- system
def _issues(s: Snapshot, healths: frozenset[str]) -> dict[str, Any]:
    entities = s.entity_issues(healths)
    return {"items": [Snapshot.issue_line(i) for i in entities], "entities": entities, "last_checked": s.last_checked}


def _config_problems(s: Snapshot) -> dict[str, Any]:
    config = s.problems_where(category=ProblemCategory.CONFIGURATION, include_info=True)
    return {
        "errors": sum(1 for p in config if p.severity == Severity.ERROR),
        "warnings": sum(1 for p in config if p.severity == Severity.WARNING),
        "problems": [s.problem_dict(p) for p in config if p.severity != Severity.INFO],
        "notes": [s.problem_dict(p) for p in config if p.severity == Severity.INFO],
        "last_checked": s.last_checked,
    }


def system_view(s: Snapshot, config: Any, *, room_names: dict[str, str], manual_option: str | None) -> dict[str, Any]:
    """The global sensors, every room's observation sensor, the mood mismatch and the mood selector."""
    sensors = {
        "status": view(s.status.value, {
            "summary": s.summary(), "phase": PHASE, "effective_mode": s.effective_mode.value,
            "config_errors": s.count(Classification.CONFIG_ERROR), "unavailable": s.count(Classification.UNAVAILABLE),
            "warnings": s.count(Classification.WARNING), "problems": [s.problem_dict(p) for p in s.problems_where()],
            "last_checked": s.last_checked}),
        "operating_mode": view(s.effective_mode.value, {
            "configured_mode": s.configured_mode.value, "lighting_control": bool(s.mood_lighting_rooms),
            "mood_lighting_rooms": list(s.mood_lighting_rooms)}),
        "effective_mood": view(s.mood.main_mood, {
            "auto_mood": s.mood.auto_mood, "manual_mood": s.mood.manual_mood,
            "expected_main_mood": s.mood.expected_main_mood, "follows": s.mood.source, "consistent": s.mood.consistent}),
        "configured_rooms": view(len(s.rooms), {"rooms": {r.name: r.status.value for r in s.rooms}}),
        "light_categories": view(len(s.categories), {"categories": {
            c.name: {"label": c.label_name or c.label_id, "lights": len(c.lights)} for c in s.categories}}),
        "discovered_sensors": view(len(s.discovered_signals), {"sensors": [
            {"area": a, "entity_id": e, "suggested_kind": k} for a, e, k in s.discovered_signals]}),
        "missing_entities": view(len(s.entity_issues(CONFIGURATION_INVALID)), _issues(s, CONFIGURATION_INVALID)),
        "unavailable_entities": view(len(s.entity_issues(OPERATIONAL_UNAVAILABLE)), _issues(s, OPERATIONAL_UNAVAILABLE)),
        "config_problems": view(len(s.problems_where(category=ProblemCategory.CONFIGURATION)), _config_problems(s)),
    }
    observation: dict[str, Any] = {}
    for room in s.rooms:
        def split(items: Any) -> tuple[list[Any], list[Any]]:
            ok = [i.as_dict() for i in items if i.health == AVAILABLE]
            bad = [i.as_dict() for i in items if i.health not in (AVAILABLE, PENDING)]
            return ok, bad

        sensors_ok, sensors_bad = split(room.sensors)
        lights_ok, lights_bad = split(room.lights)
        observation[room.area_id] = view(room.status.value, {
            "area_id": room.area_id, "operating_mode": s.effective_mode.value,
            "configuration_valid": room.configuration_valid, "usable": room.usable, "reasons": list(room.reasons),
            "sensors_available": sensors_ok, "sensors_unavailable": sensors_bad, "lights_available": lights_ok,
            "lights_unavailable": lights_bad,
            "pending": [i.entity_id for i in (*room.sensors, *room.lights) if i.health == PENDING],
            "reference_occupancy": room.reference_occupancy.as_dict() if room.reference_occupancy else None,
            "problems": [s.problem_dict(p) for p in s.problems_where(area_id=room.area_id)],
            "last_checked": s.last_checked})
    mood = config.mood
    moods = list(mood.builtin or ())
    if mood.builtin and manual_option and manual_option.casefold() not in {m.casefold() for m in moods}:
        moods.append(manual_option)
    rooms = {a: {"name": room_names.get(a) or a, "reference": r.reference_occupancy_entity or None}
             for a, r in config.enabled_rooms.items()}
    return {
        "rooms": rooms,
        "sensors": sensors,
        "room_observation": observation,
        "mood_mismatch": view(None if s.mood.consistent is None else s.mood_mismatch, {
            "main_mood": s.mood.main_mood, "expected_main_mood": s.mood.expected_main_mood,
            "grace_seconds": mood.mismatch_grace_seconds}),
        "mood_select": {"enabled": bool(mood.builtin), "options": moods},
    }


# ------------------------------------------------------------------- curtains
def curtains_view(hass: Any, mgr: Any) -> dict[str, Any]:
    """Every window as the curtain editor's model shows it (the cards have no entity for a window)."""
    from smart_occupancy.curtains.websocket import window_model  # noqa: PLC0415

    conflicts: dict[str, Any] = {}
    windows: dict[str, Any] = {}
    moving = 0
    for window_id, window in mgr.config.windows.items():
        try:
            model = window_model(hass, mgr, window, conflicts)
        except Exception:  # noqa: BLE001 - one window's view must not hide the others
            model = {"window_id": window_id, "name": window.name, "area_id": window.area_id, "mode": window.mode}
        running = mgr.running(window_id)
        if running:
            moving += 1
        windows[window_id] = _jsonable({**model, "running": running})
    return {"windows": windows, "summary": {
        "windows": len(windows), "automatic": sum(1 for w in mgr.config.windows.values() if w.mode == "automatic"),
        "moving": moving}}
