"""Presence API for the Smart Lighting App (0.6.10, App Phase A3.1).

The App shows and configures this integration's occupancy engine; the engine
itself stays here and is the only one. The commands read and change the
existing configuration (config entry options):

* sensors of a room: ``rooms.<area>.sensors`` - kind, role, inverted, enabled
  (validated exactly like the Rooms API, ``rooms_api``);
* timing: ``occupancy`` (global defaults) and ``rooms.<area>.timing`` (room
  overrides; a missing key inherits the global default). The parameters and
  their ranges are the engine's own (``const.TIMING_PARAMETERS``).

Commands (administrator only):

* ``smart_occupancy/presence/get`` - every room's live engine view (state,
  evidence, deadlines, confidence, per-signal status), timing with its
  inheritance, diagnostics (measured vs heuristic) and the bounded presence
  log (``presence_log``). Reads only.
* ``smart_occupancy/presence/review`` - dry run of one change: one room
  (sensors and/or timing overrides) or the global defaults (``area_id`` null).
  Lists the changes, blocking errors, the problems afterwards, which rooms are
  affected, and the consequences - including the engine's decision right
  after saving, computed by evaluating a *copy* of the room's engine with the
  new settings (``predict``).
* ``smart_occupancy/presence/set`` - apply a reviewed change. Refused when the
  revision differs (a room: the room revision, as in the Rooms API; global
  defaults: the revision of ``occupancy``), on errors, or when a consequence
  that needs confirmation is not confirmed (any affected room with Automatic
  lighting, and any decision that would change at once).

Applying: timing, role, inverted and include/exclude changes are applied live
(``coordinator.async_apply_presence``): every engine keeps its signal values
and is evaluated again - no reload, no start-up hold, no reset of occupancy,
no command sent by the change itself. Adding or removing a sensor or changing
its kind reloads the integration, as the Rooms API and the options flow do.
Every other option key and every unknown key inside a room are kept.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .config_flow import MIN_STUCK_SECONDS
from .const import DOMAIN, TIMING_PARAMETERS, VERSION, OccupancyState, SensorKind, SensorRole
from .discovery import area_name
from .engine import ACTIVE_STATES, INACTIVE_STATES, RoomEngine, RoomResult, SignalStatus, Value, interpret
from .entity import room_entity_unique_id
from .health import room_health
from .models import IntegrationConfig, RoomConfig
from .presence import BASIC_TIMING, TIMING_INFO, _KIND_WORDS, _ROLE_WORDS, presence_model, timing_rows
from .presence_log import presence_log
from .validation import validate
from .rooms_api import (
    CONFIRM,
    INFO,
    SENSOR_INPUT,
    RoomsApiError,
    _check_write,
    _coordinator,
    _entry,
    _loaded_entry,
    _public,
    _room_problems,
    build_room,
    diff_room,
    impacts as room_impacts,
    revision_of,
    room_errors,
    room_revision,
    rooms_section,
    schema_info,
)

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

API_VERSION = 1
MAX_SENSORS = 100
# Sensor changes the engine can apply without a reload (it keeps every signal's value).
LIVE_TYPES = frozenset({"timing", "sensor_enabled", "sensor_role", "sensor_inverted"})
# The integration's room-level health checks that concern occupancy (validation.py).
OCCUPANCY_PROBLEMS = frozenset({"area_missing", "room_without_signals", "room_only_doors", "adjacent_room_not_enabled",
                                "room_signals_unavailable"})
ROOM_ENTITY_KEYS = ("state", "occupied", "reason", "confidence", "last_activity")

# What 0 means for a parameter (None: 0 is below the minimum).
ZERO_MEANS = {
    "exit_confirmation": "vacant at once when there is exit evidence",
    "dropout_grace": "no bridging: a clear counts at once",
    "trigger_stuck_after": "off (a Trigger is never ignored as stuck)",
    "presence_stuck_after": "off (Maintain / Confirm-vacancy signals are never ignored as stuck)",
    "startup_hold": "no hold after a restart",
}
# Which signals a parameter applies to (for the inspector).
APPLIES_TO = {
    "correlation_window": "all signals",
    "exit_confirmation": "the room (vacancy with exit evidence)",
    "inactivity_timeout": "the room (vacancy without exit evidence)",
    "dropout_grace": "Maintain and Confirm-vacancy signals",
    "trigger_stuck_after": "Trigger signals",
    "presence_stuck_after": "Maintain and Confirm-vacancy signals",
    "startup_hold": "the room (after a restart or reload)",
}
STATE_WORDS = {
    OccupancyState.VACANT: "Vacant",
    OccupancyState.POSSIBLE_OCCUPANCY: "Possible occupancy",
    OccupancyState.CONFIRMED_OCCUPANCY: "Occupied",
    OccupancyState.POSSIBLE_VACANCY: "Vacancy pending",
    OccupancyState.UNCERTAIN: "Uncertain",
}
# The engine's transitions (engine.py), for the state machine diagram. (from, to, condition)
TRANSITIONS = [
    ("vacant", "possible_occupancy", "A Trigger or presence signal becomes active, or a door moves"),
    ("possible_occupancy", "confirmed_occupancy",
     "A second signal within the correlation window, or one signal active for the whole window"),
    ("possible_occupancy", "possible_vacancy", "The activity ends before it is confirmed (fast vacancy path)"),
    ("possible_occupancy", "vacant", "A door movement alone ends without presence"),
    ("confirmed_occupancy", "possible_vacancy", "No signal is active any more (dropouts bridged first)"),
    ("possible_vacancy", "confirmed_occupancy", "New activity before the vacancy timeout"),
    ("possible_vacancy", "vacant", "Quiet for the vacancy timeout (or the fast one with exit evidence)"),
    ("*", "uncertain", "Every occupancy signal is unavailable; the last occupied value is kept"),
    ("uncertain", "*", "A signal reports a value again"),
]


def timing_schema() -> list[dict[str, Any]]:
    out = []
    for key, (label, help_text) in TIMING_INFO.items():
        default, low, high = TIMING_PARAMETERS[key]
        out.append({
            "key": key, "label": label, "help": help_text, "unit": "s", "min": low, "max": high,
            "default": default, "basic": key in BASIC_TIMING, "zero": ZERO_MEANS.get(key) if low == 0 else None,
            "min_nonzero": MIN_STUCK_SECONDS if key in ("trigger_stuck_after", "presence_stuck_after") else None,
            "applies_to": APPLIES_TO.get(key),
        })
    return out


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _after(value: datetime | None, seconds: int) -> datetime | None:
    return value + timedelta(seconds=seconds) if value is not None else None


def global_revision(options: dict[str, Any]) -> str:
    return revision_of(options.get("occupancy") if isinstance(options, dict) else None)


# ------------------------------------------------------------- engine views
def signal_details(engine: RoomEngine, result: RoomResult | None, now: datetime) -> dict[str, dict[str, Any]]:
    """Per-signal engine facts and the moments its contribution changes by itself (from the engine's rules)."""
    timing = engine.timing
    views = {v.track.entity_id: v for v in (result.signals if result else ())}
    out: dict[str, dict[str, Any]] = {}
    for entity_id, t in engine.tracks.items():
        view = views.get(entity_id)
        status = view.status if view else None
        limit = timing.trigger_stuck_after if t.is_trigger else timing.presence_stuck_after
        deadlines: dict[str, str | None] = {}
        if t.used and t.since is not None:
            if t.is_door:
                if status == SignalStatus.RECENT and t.last_transition:
                    deadlines["recent_until"] = _iso(_after(t.last_transition, timing.correlation_window))
            elif t.value == Value.ACTIVE:
                if limit:
                    deadlines["stuck_at"] = _iso(_after(t.since, limit))
                if entity_id not in engine.preliminary:
                    deadlines["sustained_at"] = _iso(_after(t.since, timing.correlation_window))
            elif status == SignalStatus.GRACE and t.active_end is not None:
                deadlines["grace_until"] = _iso(_after(t.active_end, timing.dropout_grace))
        if t.is_door:
            effect = "door"
        elif not t.used:
            effect = "none"
        elif t.is_presence:
            effect = "presence"
        else:
            effect = "trigger"
        out[entity_id] = {
            "status": status.value if status else None,
            "value": t.value.value,
            "since": _iso(t.since),
            "active_start": _iso(t.active_start),
            "active_end": _iso(t.active_end),
            "last_transition": _iso(t.last_transition),
            "lost_while_active": t.lost_while_active,
            "preliminary": entity_id in engine.preliminary,
            "effect": effect,
            "stuck_limit": limit or None,
            "deadlines": deadlines,
        }
    return out


def explain(engine: RoomEngine, result: RoomResult, now: datetime) -> dict[str, Any]:
    """What the engine waits for next, from its own rules and current values (never estimated).

    ``next`` describes the condition, ``at`` the moment it happens by itself
    when nothing else changes (None when no time is known).
    """
    timing = engine.timing
    state = result.state
    nxt: list[dict[str, Any]] = []
    if state == OccupancyState.UNCERTAIN:
        nxt.append({"code": "await_signal", "text": "Waiting for an occupancy sensor to report a value. The last "
                    "occupied value is kept meanwhile; an outage never causes vacancy.", "at": None})
    elif state == OccupancyState.VACANT:
        nxt.append({"code": "await_activity", "text": "Becomes possibly occupied when a Trigger or presence signal "
                    "becomes active (a door movement alone only shows possible occupancy).", "at": None})
    elif state == OccupancyState.POSSIBLE_OCCUPANCY:
        sustained = []
        for v in result.views(SignalStatus.ACTIVE):
            t = v.track
            if t.since is not None and t.entity_id not in engine.preliminary:
                sustained.append((t.since + timedelta(seconds=timing.correlation_window), t.label()))
        text = (f"Confirmed when a second signal agrees within {timing.correlation_window} s"
                + (" or a signal stays active for the whole window." if sustained else "."))
        if engine.preliminary:
            text += " Signals that start possible occupancy lighting cannot confirm occupancy by themselves."
        if sustained:
            at, label = min(sustained)
            nxt.append({"code": "await_confirmation", "text": text + f" {label} confirms it at the time shown if "
                        "it stays active.", "at": _iso(at)})
        else:
            nxt.append({"code": "await_confirmation", "text": text, "at": None})
    elif state == OccupancyState.CONFIRMED_OCCUPANCY:
        holding = [v.track.label() for v in result.views(SignalStatus.ACTIVE, SignalStatus.GRACE)]
        if holding:
            nxt.append({"code": "held", "text": f"Stays occupied while {', '.join(holding)} "
                        f"{'is' if len(holding) == 1 else 'are'} active. When every signal has cleared, "
                        "the vacancy timer starts.", "at": None})
        elif engine.external_hold:
            nxt.append({"code": "zone_hold", "text": "Held occupied by the zones "
                        f"{', '.join(sorted(engine.external_hold))}; the quiet period starts when they end.",
                        "at": None})
    elif state == OccupancyState.POSSIBLE_VACANCY and result.vacancy_at is not None:
        fast = bool(result.vacancy_evidence)
        nxt.append({"code": "await_vacancy",
                    "text": f"Vacant after {result.required_quiet} s without activity "
                            + (f"(fast vacancy: {', '.join(result.vacancy_evidence)})." if fast
                               else "(the vacancy timeout: no exit evidence).")
                            + " Any new activity returns the room to occupied.",
                    "at": _iso(result.vacancy_at)})
    grace = [(v.track.active_end + timedelta(seconds=timing.dropout_grace), v.track.label())
             for v in result.views(SignalStatus.GRACE) if v.track.active_end is not None]
    if grace:
        at, label = min(grace)
        nxt.append({"code": "dropout_grace", "text": f"{label} cleared moments ago; it is bridged as a radar "
                    "dropout until the time shown.", "at": _iso(at)})
    if result.starting_up and engine._startup_until is not None:  # noqa: SLF001 - same package, read only
        nxt.append({"code": "startup_hold", "text": "Home Assistant (or the integration) started recently: the "
                    "previous state is held until the time shown.", "at": _iso(engine._startup_until)})  # noqa: SLF001
    deadline = engine.next_deadline(now)
    return {
        "state_label": STATE_WORDS.get(state, state.value),
        "next": nxt,
        "next_evaluation": _iso(deadline),
        "vacancy_path": None if result.required_quiet is None else (
            "fast" if result.vacancy_evidence else "normal"),
        "visit_confirmed": engine.visit_confirmed,
    }


def predict(hass: HomeAssistant, engine: RoomEngine, room: RoomConfig, config: IntegrationConfig,
            now: datetime) -> RoomResult:
    """The engine's decision right after applying ``room``'s sensors and timing - on a copy of the engine.

    The same steps as the live path (``OccupancyManager.async_reconfigure``):
    reconfigure, read every signal again (an inverted flag changes its value),
    evaluate. The running engine is not touched.
    """
    sim = copy.deepcopy(engine)
    sim.reconfigure(room.sensors, config.timing_for(room.area_id))
    for entity_id, track in sim.tracks.items():
        state = hass.states.get(entity_id)
        value = interpret(state.state if state else None, inverted=track.assignment.inverted)
        if value == track.value:
            continue
        at = min(state.last_changed, now) if state else now
        if track.since is not None and at < track.since:
            at = now
        sim.observe(entity_id, value, at)
    return sim.evaluate(now)


# ------------------------------------------------------------- diagnostics
def _diag(code: str, level: str, basis: str, title: str, detail: str, action: str, *, target: str,
          entity_id: str | None = None, setting: str | None = None) -> dict[str, Any]:
    return {"code": code, "level": level, "basis": basis, "title": title, "detail": detail, "action": action,
            "target": target, "entity_id": entity_id, "setting": setting}


def diagnostics(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator, area_id: str,
                problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Why occupancy may not work as expected. ``basis``: measured (engine / states now), integration (its own
    health checks), heuristic (a suggestion from the configuration; may be intended)."""
    config = coordinator.config
    room = config.room(area_id)
    engine = coordinator.occupancy.engines.get(area_id)
    result = engine.result if engine else None
    timing = config.timing_for(area_id)
    out: list[dict[str, Any]] = []
    add = out.append

    for p in problems:
        level = {"error": "error", "warning": "warning"}.get(p.get("severity"), "info")
        add(_diag(f"integration:{p.get('type') or p.get('code')}", level, "integration", p.get("title") or "",
                  p.get("explanation") or "", p.get("action") or "", target="sensors" if p.get("entity_id") else "room",
                  entity_id=p.get("entity_id")))

    if not room.enabled:
        add(_diag("room_disabled", "info", "measured", "Room not observed",
                  "This room is not enabled, so the engine does not run for it.",
                  "Enable it in the Rooms editor.", target="rooms_editor"))
        return out
    if engine is None or result is None:
        add(_diag("engine_missing", "error", "measured", "No occupancy engine",
                  "The integration is not running this room's engine.", "Check that the integration is running.",
                  target="room"))
        return out

    # Measured: the engine's current view.
    if result.reason_code == "no_signals":
        add(_diag("no_signals", "error", "measured", "No occupancy signal",
                  "No Trigger, Maintain or Confirm-vacancy sensor is used (door contacts cannot detect presence).",
                  "Add a motion or presence sensor.", target="sensors"))
    elif result.reason_code == "all_signals_unknown":
        add(_diag("all_unknown", "error", "measured", "Every occupancy sensor is unavailable",
                  "The room is Uncertain: the last occupied value is kept until a sensor reports again.",
                  "Check the devices and their integration.", target="sensors"))
    for v in result.signals:
        t = v.track
        if not t.used:
            continue
        if v.status == SignalStatus.STUCK:
            limit = timing.trigger_stuck_after if t.is_trigger else timing.presence_stuck_after
            key = "trigger_stuck_after" if t.is_trigger else "presence_stuck_after"
            add(_diag("signal_stuck", "warning", "measured", f"{t.label()} is ignored as stuck",
                      f"Active since {_iso(t.since)}, longer than {limit} s; it is not used as evidence until it "
                      "changes.", "Check the device, or raise the stuck limit if this is normal.", target="sensors",
                      entity_id=t.entity_id, setting=key))
        elif v.status == SignalStatus.UNKNOWN:
            state = hass.states.get(t.entity_id)
            if state is not None and state.state not in ("unavailable", "unknown"):
                add(_diag("state_not_understood", "warning", "measured", f"{t.label()}: state not understood",
                          f"It reports “{state.state}”, which is neither an active nor an inactive state for the "
                          "engine; it is not used as evidence.",
                          "Use a sensor with on/off states, or another kind.", target="sensors",
                          entity_id=t.entity_id))
            if t.lost_while_active:
                add(_diag("lost_while_active", "info", "measured", f"{t.label()} dropped out while active",
                          "It became unavailable while reporting presence, so occupancy is held one extra vacancy "
                          "timeout before the quiet period starts.", "Check the device's connection.",
                          target="sensors", entity_id=t.entity_id))
    presence = [v for v in result.signals if v.track.is_presence and v.track.used]
    active = [v for v in presence if v.status == SignalStatus.ACTIVE]
    cleared = [v for v in presence if v.status == SignalStatus.CLEARED]
    if active and cleared:
        add(_diag("presence_disagrees", "info", "measured", "Presence sensors disagree",
                  f"{', '.join(v.track.label() for v in active)} report presence while "
                  f"{', '.join(v.track.label() for v in cleared)} report none. The room stays occupied while any of "
                  "them is active.", "Often normal (different coverage); check if it persists.", target="sensors"))
    capable = [t for t in engine.tracks.values() if t.capable]
    if capable and engine.preliminary and all(t.entity_id in engine.preliminary for t in capable):
        add(_diag("preliminary_only", "warning", "measured", "Occupancy cannot be confirmed",
                  "Every occupancy signal of this room starts possible occupancy lighting, and those signals cannot "
                  "confirm occupancy by themselves. The room stays Possible occupancy.",
                  "Add a radar, PC or other presence signal.", target="sensors"))
    comparison = coordinator.occupancy.comparison(area_id)
    if comparison is not None and comparison.reported_agreement is False:
        add(_diag("reference_disagrees", "info", "measured", "Reference helper disagrees",
                  f"The reference entity {room.reference_occupancy_entity} differs from the engine's decision "
                  f"(since {_iso(comparison.disagreement_since)}).",
                  "Compare the timeline; the reference is only read.", target="room"))
    excluded = [s for s in room.sensors if not s.enabled]
    if excluded:
        add(_diag("excluded", "info", "measured", f"{len(excluded)} sensor(s) excluded",
                  "Excluded sensors keep their assignment but are not used for occupancy.",
                  "Include them again in the sensor inspector if this is not intended.", target="sensors"))

    # Heuristic: the configuration.
    used = [s for s in room.sensors if s.participates]
    non_door = [s for s in used if s.kind != SensorKind.DOOR and s.role != SensorRole.IGNORE]
    if non_door and not any(s.role in (SensorRole.MAINTAIN, SensorRole.CONFIRM_VACANCY) for s in non_door):
        add(_diag("no_presence_sensor", "info", "heuristic", "Only motion triggers",
                  "No Maintain or Confirm-vacancy sensor: people sitting still are not detected, so the room becomes "
                  f"vacant {timing.inactivity_timeout} s after the last motion.",
                  "Add a radar / presence sensor (Maintain) if people sit still here.", target="sensors"))
    if len(non_door) == 1:
        add(_diag("single_signal", "info", "heuristic", "One occupancy signal",
                  "Occupancy is confirmed only when this signal stays active for the correlation window; there is "
                  "no second signal to corroborate it.", "A second sensor makes confirmation faster and safer.",
                  target="sensors"))
    for s in used:
        name = coordinator.occupancy.engines[area_id].tracks[s.entity_id].label() if s.entity_id in engine.tracks \
            else s.entity_id
        if s.kind in (SensorKind.RADAR, SensorKind.BED, SensorKind.PC_ACTIVITY) and s.role == SensorRole.TRIGGER:
            add(_diag("presence_as_trigger", "warning", "heuristic", f"{name}: presence sensor used as Trigger",
                      f"As a Trigger it is ignored as stuck after {timing.trigger_stuck_after} s of continuous "
                      "presence and its clearing is never vacancy evidence."
                      if timing.trigger_stuck_after else "As a Trigger, its clearing is never vacancy evidence.",
                      "Use Maintain (or Confirm vacancy).", target="sensors", entity_id=s.entity_id))
        if s.kind == SensorKind.PIR and s.role in (SensorRole.MAINTAIN, SensorRole.CONFIRM_VACANCY):
            add(_diag("pir_as_presence", "info", "heuristic", f"{name}: PIR used as {s.role.value.replace('_', ' ')}",
                      "A PIR only sees movement; as a presence signal it does not start occupancy after a quiet "
                      "period differently, but its short clears are bridged as dropouts"
                      + ("" if timing.presence_stuck_after else " and a stuck PIR is never ignored") + ".",
                      "Trigger is the usual role for a PIR.", target="sensors", entity_id=s.entity_id))
        if s.kind == SensorKind.DOOR and s.role in (SensorRole.MAINTAIN, SensorRole.CONFIRM_VACANCY):
            add(_diag("door_role", "info", "heuristic", f"{name}: the role has no effect on a door",
                      "Door contacts count only their movements (activity and exit evidence), whatever the role.",
                      "Leave the default role.", target="sensors", entity_id=s.entity_id))
        state = hass.states.get(s.entity_id)
        dclass = state.attributes.get("device_class") if state else None
        if s.inverted and dclass in ("motion", "occupancy", "presence", "moving"):
            add(_diag("inverted_presence", "warning", "heuristic", f"{name} is inverted",
                      f"Its device class is {dclass}, so “on” already means detected; inverted, it counts as active "
                      "when nothing is detected.", "Clear Inverted unless this is intended.", target="sensors",
                      entity_id=s.entity_id))
    for s in room.sensors:
        if s.role == SensorRole.IGNORE and s.enabled:
            add(_diag("ignored_role", "info", "heuristic", f"{s.entity_id} has the role Ignore",
                      "It is assigned but not used for occupancy.", "Choose a role, or remove it.",
                      target="sensors", entity_id=s.entity_id))
    if timing.exit_confirmation > timing.inactivity_timeout:
        add(_diag("fast_slower", "warning", "heuristic", "Fast vacancy is slower than the vacancy timeout",
                  f"Fast vacancy timeout {timing.exit_confirmation} s > vacancy timeout {timing.inactivity_timeout} s:"
                  " exit evidence makes vacancy slower, not faster.", "Make the fast timeout shorter.",
                  target="timing", setting="exit_confirmation"))
    if timing.correlation_window >= 60:
        add(_diag("long_confirmation", "info", "heuristic", "Long occupancy confirmation",
                  f"Signals are correlated over {timing.correlation_window} s; confirmation and door exits are judged "
                  "over that long a period.", "10–30 s is usual.", target="timing", setting="correlation_window"))
    if timing.dropout_grace >= timing.inactivity_timeout:
        add(_diag("grace_long", "warning", "heuristic", "Dropout bridge as long as the vacancy timeout",
                  f"Radar dropouts are bridged for {timing.dropout_grace} s, as long as the vacancy timeout.",
                  "Keep the bridge short (for example 30 s).", target="timing", setting="dropout_grace"))
    if timing.inactivity_timeout >= 7200:
        add(_diag("long_vacancy", "info", "heuristic", "Long vacancy timeout",
                  f"The room stays occupied {timing.inactivity_timeout // 60} min after the last activity "
                  "without exit evidence.", "Shorter timeouts need a presence sensor.", target="timing",
                  setting="inactivity_timeout"))
    if not timing.trigger_stuck_after and any(s.role == SensorRole.TRIGGER and s.kind != SensorKind.DOOR
                                              for s in used):
        add(_diag("trigger_stuck_off", "info", "heuristic", "PIR stuck detection is off",
                  "A Trigger that stays active (a stuck motion sensor) holds the room occupied indefinitely.",
                  "Set “PIR stuck after” (default 1800 s).", target="timing", setting="trigger_stuck_after"))
    if timing.presence_stuck_after and timing.presence_stuck_after < 3600 and any(
            s.role in (SensorRole.MAINTAIN, SensorRole.CONFIRM_VACANCY) for s in used):
        add(_diag("presence_stuck_short", "info", "heuristic", "Presence stuck limit is short",
                  f"Presence signals active longer than {timing.presence_stuck_after} s are ignored: a person "
                  "sitting still that long is then not seen.", "0 (off) is the default.", target="timing",
                  setting="presence_stuck_after"))
    return out


# ------------------------------------------------------------------ model
def _entities(hass: HomeAssistant, area_id: str) -> dict[str, str | None]:
    reg = er.async_get(hass)
    platform = {"occupied": "binary_sensor"}
    return {key: reg.async_get_entity_id(platform.get(key, "sensor"), DOMAIN, room_entity_unique_id(area_id, key))
            for key in ROOM_ENTITY_KEYS}


def _automation(coordinator: SmartOccupancyCoordinator, area_id: str) -> dict[str, Any]:
    ctrl = coordinator.lighting
    room = ctrl.lighting.room(area_id)
    windows = [{"window_id": w.window_id, "name": w.name, "mode": w.mode}
               for w in coordinator.curtains.config.windows.values() if w.area_id == area_id]
    return {
        "lighting_automatic": ctrl.room_active(area_id),
        "lighting_paused": ctrl.room_active(area_id) and ctrl.manual_mode,
        "vacancy_action": room.vacancy.action.value,
        "curtains": windows,
        "curtains_automatic": [w["name"] for w in windows if w["mode"] == "automatic"],
    }


def _zones(coordinator: SmartOccupancyCoordinator, hass: HomeAssistant, area_id: str) -> list[dict[str, Any]]:
    ctrl = coordinator.lighting
    room = ctrl.lighting.room(area_id)
    try:
        status = {z["id"]: z for z in ctrl.zones.status(area_id)}
    except Exception:  # noqa: BLE001 - zones are context only
        status = {}
    out = []
    for z in room.zones:
        live = status.get(z.id) or {}
        out.append({
            "id": z.id, "name": z.name, "enabled": z.enabled, "detection": z.detection.value,
            "holds_room": z.holds_room, "sensors": list(z.sensors),
            "radar": z.radar.as_dict() if z.is_radar and z.radar else None,
            "band": z.radar.band() if z.is_radar and z.radar else None,
            "active": live.get("active"), "state": live.get("state"), "since": live.get("since"),
            "reason": live.get("reason"), "available": live.get("available"),
            "live_radar": live.get("radar"),
        })
    return out


def _problems_by_room(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator,
                      config: IntegrationConfig) -> dict[str, list[dict[str, Any]]]:
    """The integration's validation, run once for every room (same selection as ``rooms_api._room_problems``)."""
    found = validate(hass, config, settled=coordinator.settled, index=coordinator.lighting.index)
    out: dict[str, list[dict[str, Any]]] = {}
    for area_id, room in config.rooms.items():
        refs = set(room.referenced_entities)
        out[area_id] = [{**p.as_dict(), "key": p.key} for p in found
                        if p.area_id == area_id or (p.area_id is None and p.entity_id in refs)]
    return out


def config_view(hass: HomeAssistant, area_id: str, raw: dict[str, Any] | None,
                stored: IntegrationConfig) -> dict[str, Any]:
    """The editable part of a room, from the stored options (the same source as its revision)."""
    room = stored.room(area_id)
    return {
        "area_id": area_id,
        "name": area_name(hass, area_id) or area_id,
        "configured": raw is not None,
        "enabled": area_id in stored.enabled_rooms,
        "revision": revision_of(raw),
        "sensors": [s.as_dict() | {"enabled": s.enabled} for s in room.sensors],
        "timing": timing_rows(stored, area_id),
        "timing_overrides": dict(room.timing),
        "adjacent_rooms": list(room.adjacent_rooms),
        "reference_occupancy_entity": room.reference_occupancy_entity,
        "entities": _entities(hass, area_id),
        "problems": [], "diagnostics": [], "zones": [],
        "automation": {"lighting_automatic": False, "lighting_paused": False, "vacancy_action": None,
                       "curtains": [], "curtains_automatic": []},
        "occupancy": None, "signals": [], "explain": None, "health": None,
    }


def room_view(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator, area_id: str,
              now: datetime, raw: dict[str, Any] | None,
              all_problems: dict[str, list[dict[str, Any]]] | None = None,
              stored: IntegrationConfig | None = None) -> dict[str, Any]:
    config = coordinator.config
    room = config.room(area_id)
    enabled = area_id in config.enabled_rooms
    engine = coordinator.occupancy.engines.get(area_id)
    result = engine.result if engine else None
    # The room's occupancy problems from the integration's own checks: about its sensors or reference helper, or
    # about the room's occupancy as a whole (lighting problems belong to the lighting views).
    signals = {s.entity_id for s in room.sensors} | ({room.reference_occupancy_entity}
                                                     if room.reference_occupancy_entity else set())
    if all_problems is None:
        all_problems = {area_id: _room_problems(hass, coordinator, config, area_id)}
    problems = [p for p in (all_problems.get(area_id, []) if raw is not None else [])
                if p.get("aspect") != "lighting" and (
                    p.get("entity_id") in signals or (not p.get("entity_id") and p.get("type") in OCCUPANCY_PROBLEMS))]
    view: dict[str, Any] = {
        **config_view(hass, area_id, raw, stored or config),
        "problems": problems,
        "diagnostics": diagnostics(hass, coordinator, area_id, problems) if raw is not None else [],
        "automation": _automation(coordinator, area_id),
        "zones": _zones(coordinator, hass, area_id),
        "occupancy": None,
        "signals": [],
        "explain": None,
        "health": None,
    }
    if not enabled or engine is None or result is None:
        return view
    model = presence_model(hass, coordinator, area_id)
    details = signal_details(engine, result, now)
    view["signals"] = [{**s, **details.get(s["entity_id"], {})} for s in model["sensors"]]
    source = result.last_activity_source
    view["occupancy"] = {
        "state": result.state.value,
        "state_label": STATE_WORDS.get(result.state, result.state.value),
        "occupied": result.occupied,
        "state_since": _iso(result.state_since),
        "reason": result.reason,
        "reason_code": result.reason_code,
        "confidence": {"score": result.confidence.score, **result.confidence.as_dict()},
        "usable": result.usable,
        "last_activity": _iso(result.last_activity),
        "last_activity_source": source,
        "last_activity_name": engine.tracks[source].label() if source in engine.tracks else source,
        "quiet_since": _iso(result.quiet_since),
        "vacancy_at": _iso(result.vacancy_at),
        "required_quiet": result.required_quiet,
        "vacancy_evidence": list(result.vacancy_evidence),
        "starting_up": result.starting_up,
        "preliminary_signals": sorted(engine.preliminary),
        "zone_holds": sorted(engine.external_hold),
    }
    view["explain"] = explain(engine, result, now)
    view["health"] = room_health(coordinator.data, area_id, result)
    comparison = coordinator.occupancy.comparison(area_id)
    view["comparison"] = comparison.as_dict() if comparison else None
    return view


def presence_get(hass: HomeAssistant, area_id: str | None = None) -> dict[str, Any]:
    entry = _entry(hass)
    options = dict(entry.options) if entry else {}
    coordinator = _coordinator(entry)
    config = IntegrationConfig.from_dict(options)
    now = dt_util.utcnow()
    rooms: dict[str, Any] = {}
    section = rooms_section(options)
    wanted = [a for a in ([area_id] if area_id else list(section)) if a in section]
    if coordinator is not None:
        problems = _problems_by_room(hass, coordinator, coordinator.config) if wanted else {}
        for a in wanted:
            rooms[a] = room_view(hass, coordinator, a, now, section.get(a), problems, config)
    else:
        # Not running: the stored configuration only (no engine, so no occupancy, nothing invented).
        for a in wanted:
            rooms[a] = config_view(hass, a, section.get(a), config)
    log = presence_log(hass)
    return {
        "api_version": API_VERSION,
        "integration_version": VERSION,
        "now": now.isoformat(timespec="milliseconds"),
        "loaded": coordinator is not None,
        "writable": coordinator is not None,
        "state": entry.state.value if entry else "not_set_up",
        "operating_mode": config.operating_mode.value,
        "global": {"timing": config.occupancy.as_dict(), "revision": global_revision(options)},
        "timing_schema": timing_schema(),
        # Kinds with their allowed domains and default roles, roles: the Rooms API's editor schema.
        "schema": schema_info(),
        "kinds": [{"value": k.value, "label": _KIND_WORDS.get(k, k.value)} for k in SensorKind],
        "roles": [{"value": r.value, "label": _ROLE_WORDS.get(r, r.value)} for r in SensorRole],
        "states": [{"value": s.value, "label": STATE_WORDS[s]} for s in OccupancyState],
        "transitions": [{"from": a, "to": b, "when": c} for a, b, c in TRANSITIONS],
        # The engine's own reading of state strings (anything else is "unknown"), for displaying history.
        "state_values": {"active": sorted(ACTIVE_STATES), "inactive": sorted(INACTIVE_STATES)},
        "rooms": rooms,
        "log": log.view(area_id, limit=400 if area_id else 150),
        "log_seq": log.seq,
    }


# ----------------------------------------------------------------- planning
TIMING_VALUE = vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=0, max=10_000_000)))
CHANGE_SCHEMA = vol.Schema({
    vol.Optional("sensors"): vol.Any(None, vol.All([SENSOR_INPUT], vol.Length(max=MAX_SENSORS))),
    vol.Optional("timing"): vol.Any(None, {vol.In(list(TIMING_PARAMETERS)): TIMING_VALUE}),
    vol.Optional("reset_timing", default=False): bool,
})


def _timing_errors(values: dict[str, int | None]) -> list[dict[str, Any]]:
    errors = []
    for key, value in values.items():
        if value is None:
            continue
        _default, low, high = TIMING_PARAMETERS[key]
        label = TIMING_INFO[key][0]
        if not low <= value <= high:
            errors.append({"code": "timing_range", "setting": key,
                           "message": f"{label} must be from {low} to {high} seconds"})
        elif key in ("trigger_stuck_after", "presence_stuck_after") and value and value < MIN_STUCK_SECONDS:
            errors.append({"code": "timing_stuck", "setting": key,
                           "message": f"{label} must be 0 (off) or at least {MIN_STUCK_SECONDS} seconds"})
    return errors


def _rooms_using_global(config: IntegrationConfig, keys: set[str]) -> list[str]:
    return [a for a, r in config.enabled_rooms.items() if keys - set(r.timing)]


def plan_presence(hass: HomeAssistant, area_id: str | None, change: Any,
                  expected_revision: str | None) -> dict[str, Any]:
    entry = _entry(hass)
    if entry is None:
        raise RoomsApiError("not_set_up", "Smart Occupancy & Lighting is not set up")
    coordinator = _coordinator(entry)
    try:
        change = CHANGE_SCHEMA(change or {})
    except vol.Invalid as err:
        raise RoomsApiError("invalid_change", f"Invalid presence change: {err}") from err
    options = dict(entry.options)
    old_config = IntegrationConfig.from_dict(options)
    timing_in: dict[str, int | None] = dict(change.get("timing") or {})
    errors = _timing_errors(timing_in)
    changes: list[dict[str, Any]] = []
    now = dt_util.utcnow()

    if area_id is None:
        if change.get("sensors") is not None or change["reset_timing"]:
            raise RoomsApiError("invalid_change", "Global defaults have timing only")
        current = global_revision(options)
        raw_occ = dict(options.get("occupancy") or {}) if isinstance(options.get("occupancy"), dict) else {}
        values = old_config.occupancy.as_dict()
        for key, value in timing_in.items():
            values[key] = TIMING_PARAMETERS[key][0] if value is None else value
        for key, value in values.items():
            if value != old_config.occupancy.as_dict()[key]:
                changes.append({"type": "timing", "scope": "global", "key": key,
                                "old": old_config.occupancy.as_dict()[key], "new": value})
        new_options = {**options, "occupancy": {**raw_occ, **values}} if not errors else options
    else:
        section = rooms_section(options)
        old_raw = section.get(area_id)
        current = room_revision(options, area_id)
        if old_raw is None:
            raise RoomsApiError("not_configured", "This room is not configured; set it up in the Rooms editor first")
        old_room = RoomConfig.from_dict(area_id, old_raw)
        sensors = change.get("sensors")
        edit = {
            "enabled": old_room.enabled,
            "sensors": sensors if sensors is not None else [s.as_dict() | {"enabled": s.enabled}
                                                           for s in old_room.sensors],
            "lights": list(old_room.lights),
            "reference_occupancy_entity": old_room.reference_occupancy_entity,
            "adjacent_rooms": list(old_room.adjacent_rooms),
        }
        errors += room_errors(hass, area_id, old_raw, edit)
        new_raw = old_raw
        if not errors:
            new_raw = build_room(area_id, old_raw, edit)
            changes += diff_room(area_id, old_raw, new_raw)
            raw_timing = old_raw.get("timing") if isinstance(old_raw.get("timing"), dict) else {}
            timing = {} if change["reset_timing"] else dict(raw_timing)
            for key, value in timing_in.items():
                if value is None:
                    timing.pop(key, None)
                else:
                    timing[key] = value
            new_raw = {**new_raw, "timing": timing}
            glob = old_config.occupancy.as_dict()
            for key in TIMING_PARAMETERS:
                before, after = raw_timing.get(key), timing.get(key)
                if before != after:
                    changes.append({
                        "type": "timing", "scope": "room", "key": key, "old": before, "new": after,
                        "old_effective": before if before is not None else glob[key],
                        "new_effective": after if after is not None else glob[key],
                    })
        new_options = {**options, "rooms": {**section, area_id: new_raw}}
    new_config = IntegrationConfig.from_dict(new_options)
    types = {c["type"] for c in changes}
    live = bool(changes) and coordinator is not None and types <= LIVE_TYPES and \
        coordinator.occupancy.same_signals(new_config)

    # Rooms whose engine settings change.
    affected: list[str] = []
    for a in new_config.enabled_rooms:
        if old_config.timing_for(a) != new_config.timing_for(a) or (
                a == area_id and types - {"timing"}):
            affected.append(a)
    found = _impacts(hass, coordinator, area_id, old_config, new_config, changes, affected, live, now,
                     options, new_options)
    before = {p["key"]: p for p in (_room_problems(hass, coordinator, old_config, area_id) if area_id else [])}
    after = {p["key"]: p for p in (_room_problems(hass, coordinator, new_config, area_id) if area_id else [])}
    return {
        "scope": "global" if area_id is None else "room",
        "area_id": area_id,
        "revision": current,
        "stale": expected_revision is not None and expected_revision != current,
        "errors": errors,
        "changes": changes if not errors else [],
        "affected_rooms": [{"area_id": a, "name": area_name(hass, a) or a} for a in affected] if not errors else [],
        "impacts": found if not errors else [],
        "requires_confirmation": sorted({i["code"] for i in found if i["level"] == CONFIRM}) if not errors else [],
        "problems_new": [p for k, p in after.items() if k not in before],
        "problems_resolved": [p for k, p in before.items() if k not in after],
        "apply": "none" if not changes or errors else "live" if live else "reload",
        "writable": coordinator is not None,
        "_new_options": new_options,
        "_new_config": new_config,
    }


def _impacts(hass: HomeAssistant, coordinator: SmartOccupancyCoordinator | None, area_id: str | None,
             old: IntegrationConfig, new: IntegrationConfig, changes: list[dict[str, Any]], affected: list[str],
             live: bool, now: datetime, options: dict[str, Any], new_options: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not changes:
        return out

    def add(code: str, level: str, title: str, detail: str, **extra: Any) -> None:
        out.append({"code": code, "level": level, "title": title, "detail": detail, **extra})

    if live:
        add("live_apply", INFO, "Applied at once, without a reload",
            "The occupancy engine keeps every sensor's value and evaluates the affected rooms again with the new "
            "settings. No restart hold, no reset of occupancy, and nothing is switched by the change itself.")
    else:
        add("reload", INFO, "The integration reloads",
            "Adding or removing a sensor or changing its type reloads Smart Occupancy & Lighting (about a second). "
            "Occupancy is restored from its saved state and held for the start-up hold; the reload switches no "
            "light and moves no cover.")
    if area_id is not None:
        sensor_changes = [c for c in changes if c["type"] != "timing"]
        if sensor_changes:
            section_old = rooms_section(options).get(area_id)
            section_new = rooms_section(new_options).get(area_id)
            for i in room_impacts(hass, coordinator, area_id, section_old, section_new, sensor_changes, live=live):
                if i["code"] not in ("live_apply", "reload"):
                    out.append(i)
    if coordinator is None:
        return out
    ctrl = coordinator.lighting
    automatic = [a for a in affected if ctrl.room_active(a)]
    if automatic and not any(i["code"] == "automatic_lighting" for i in out):
        names = ", ".join(area_name(hass, a) or a for a in automatic)
        add("automatic_rooms", CONFIRM, "Rooms with Automatic lighting are affected",
            f"Automatic lighting in {names} follows the new occupancy settings: lights switch on or off at "
            "different times when occupancy changes.", rooms=automatic)
    curtains = [a for a in affected if any(w.area_id == a and w.mode == "automatic"
                                           for w in coordinator.curtains.config.windows.values())]
    if curtains:
        add("automatic_curtains", INFO, "Automatic curtains use these rooms' occupancy",
            f"Automatic curtains in {', '.join(area_name(hass, a) or a for a in curtains)} follow the changed "
            "occupancy decisions.", rooms=curtains)
    if not live:
        return out
    predictions = []
    for a in affected:
        engine = coordinator.occupancy.engines.get(a)
        if engine is None or engine.result is None:
            continue
        current = engine.result
        predicted = predict(hass, engine, new.room(a), new, now)
        item = {
            "area_id": a, "name": area_name(hass, a) or a,
            "state": current.state.value, "occupied": current.occupied,
            "new_state": predicted.state.value, "new_occupied": predicted.occupied,
            "vacancy_at": _iso(current.vacancy_at), "new_vacancy_at": _iso(predicted.vacancy_at),
            "reason": predicted.reason,
        }
        predictions.append(item)
        if (predicted.state, predicted.occupied) != (current.state, current.occupied):
            automatic_room = ctrl.room_active(a)
            words = f"{STATE_WORDS[current.state]} → {STATE_WORDS[predicted.state]}"
            add("decision_changes_now", CONFIRM if automatic_room or predicted.occupied != current.occupied else INFO,
                f"{item['name']}: the decision changes at once ({words})",
                f"Evaluated with the new settings, the engine decides “{predicted.reason}”"
                + (" Automatic lighting reacts to it." if automatic_room else ""), rooms=[a])
        elif current.vacancy_at != predicted.vacancy_at and predicted.vacancy_at is not None:
            add("vacancy_moves", INFO, f"{item['name']}: vacancy moves",
                f"The pending vacancy is confirmed at {_iso(predicted.vacancy_at)} instead of "
                f"{_iso(current.vacancy_at)}.", rooms=[a])
    if predictions:
        out.append({"code": "prediction", "level": INFO, "title": "The engine right after saving",
                    "detail": "Computed by evaluating a copy of each affected room's engine with the new settings "
                              "and the current sensor values (nothing is changed by the review).",
                    "predictions": predictions})
    return out


# ------------------------------------------------------------------ commands
def _send_error(connection: websocket_api.ActiveConnection, msg: dict, err: RoomsApiError) -> None:
    connection.send_error(msg["id"], err.code, str(err))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/presence/get",
    vol.Optional("area_id"): vol.Any(None, vol.All(str, vol.Length(min=1, max=255))),
})
@websocket_api.require_admin
@callback
def ws_presence_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Live engine view, timing, diagnostics and the presence log. Reads only."""
    connection.send_result(msg["id"], presence_get(hass, msg.get("area_id")))


_CHANGE_MSG = {
    vol.Optional("area_id"): vol.Any(None, vol.All(str, vol.Length(min=1, max=255))),
    vol.Required("change"): dict,
}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/presence/review", **_CHANGE_MSG,
    vol.Optional("expected_revision"): vol.Any(None, str),
})
@websocket_api.require_admin
@callback
def ws_presence_review(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Dry run of one presence change. Changes nothing."""
    try:
        plan = plan_presence(hass, msg.get("area_id"), msg["change"], msg.get("expected_revision"))
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    connection.send_result(msg["id"], _public(plan))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/presence/set", **_CHANGE_MSG,
    vol.Required("expected_revision"): str,
    vol.Optional("confirmed", default=list): [str],
})
@websocket_api.require_admin
@callback
def ws_presence_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Apply one reviewed presence change (see the module docstring)."""
    try:
        entry = _loaded_entry(hass)
        plan = plan_presence(hass, msg.get("area_id"), msg["change"], msg["expected_revision"])
        _check_write(plan, msg["confirmed"])
    except RoomsApiError as err:
        _send_error(connection, msg, err)
        return
    coordinator: SmartOccupancyCoordinator = entry.runtime_data
    if plan["apply"] != "none":
        presence_log(hass).expect_source("app")  # this API is the Smart Lighting App's
        if not (plan["apply"] == "live"
                and coordinator.async_apply_presence(plan["_new_config"], options=plan["_new_options"])):
            hass.config_entries.async_update_entry(entry, options=plan["_new_options"])
    options = dict(entry.options)
    area_id = msg.get("area_id")
    connection.send_result(msg["id"], {
        "applied": plan["apply"],
        "changes": plan["changes"],
        "revision": global_revision(options) if area_id is None else room_revision(options, area_id),
        "rooms_revision": revision_of(rooms_section(options)),
    })


PRESENCE_API_HANDLERS = (ws_presence_get, ws_presence_review, ws_presence_set)
