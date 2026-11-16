"""Autonomous curtain control (0.6.2, the user's Phase 4.6.1): evidence, holds, safety and execution.

0.6.3 (Phase 4.6.2) adds environmental evidence (``environment.py``): the
validity of the light sensor (also *stuck* and *implausible* readings), the
room and outdoor temperature with a hysteresis band, the weather / cloud cover
(supplementary, and only without a light sensor when allowed), glare, and a
side-effect-free ``simulate`` that predicts a window's decision under changed
settings before they are saved.

One ``WindowRuntime`` per window keeps what the decisions need between
evaluations (light hysteresis, the morning of the day, when protective
conditions started, the manual hold, what was applied, the daily move count
and the decision log). Holds, the applied preset, the move count and the log
are stored in ``.storage/smart_occupancy.curtains_state`` and survive a
restart; light history does not, so after a restart the sensors must be stable
again before daylight counts.

A window is evaluated when one of its inputs changes (its covers, the sun,
its light sensor, sleep source, window lamp or room occupancy) and at the next
time something can change by time alone (a stability period ending, the
night starting or ending, the morning ending, a hold expiring, the lamp's
release delay, a retry). Nothing polls; a 15-minute safety re-check is the
only periodic evaluation.

**Off** windows are not evaluated. **Observing** windows are evaluated and
explained ("would move ..."), but nothing is sent. **Automatic** windows send
the decided preset through the same executor as a confirmed manual preset,
after these safety limits, in this order:

1. Home Assistant started less than ``STARTUP_SETTLE_SECONDS`` ago;
2. a movement of this window is still running (it is never contradicted);
3. a cover is unavailable, missing or cannot reach its target;
4. a cover is being moved by another window's operation;
5. an enabled automation that may also move these covers (and that an
   administrator has not explicitly allowed);
6. the same targets were already applied (nothing is re-sent; a failed or
   timed-out operation is retried up to ``max_retries`` times, with growing
   delays, then reported and left);
7. the daily budget (sleep and night are exempt);
8. the minimum interval between moves (sleep, night, evening privacy, the lamp
   and the first move after enabling are exempt).

Covers that are already at their target get no command (the plan skips them).

0.6.4 (Global Curtain Modes): the target of a decision comes from the window's
Global Modes, room, window and cover settings (``modes.py``); a cover without
an assignment for the decided condition is left where it is. Every log entry
names where its preset came from. A confirmed mapping change can be set to
apply only at a window's next change of condition (``defer``).
"""

from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_ON
from homeassistant.core import CALLBACK_TYPE, Event, callback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from ..const import OccupancyState
from ..supervision import CurtainOrigin
from .config import CONDITION_WORDS, MODE_WORDS, CurtainConfig, Window
from .environment import (
    BAND_WORDS,
    Reading,
    band_text,
    clear_sky,
    comfort,
    judge_lux,
    judge_temperature,
    judge_weather,
    lux_value,
    stuck_deadline,
    temperature_of,
    thermostat_target,
    track_band,
)
from .executor import build_plan
from .logic import (
    BUDGET_EXEMPT,
    DWELL_EXEMPT,
    PHASE_EXEMPT,
    PROTECTIVE,
    SUN_BASES,
    Decision,
    Evidence,
    Flags,
    History,
    Hysteresis,
    decide,
    precedence,
)
from .modes import effective
from .rules import consistency
from .solar import exposure, window_angle_from_sun_angle

if TYPE_CHECKING:
    from .manager import CurtainManager

_LOGGER = logging.getLogger(__name__)

STARTUP_SETTLE_SECONDS = 120     # after a (re)start: let sensors report before anything is sent
RECHECK_SECONDS = 900            # safety re-check when nothing else is due
RETRY_DELAYS = (60, 300, 900, 1800, 3600)
SUN_MARGIN = 3.0                 # degrees of geometric hysteresis once sun protection is on
LOG_LENGTH = 40
NOTIFY_SECONDS = 60.0            # unchanged decisions are pushed to the card at most this often
SUCCESS = frozenset({"succeeded", "nothing_to_do"})
ENV_KEYS = ("lux_entity", "indoor_lux_entity", "room_temp_entity", "outdoor_temp_entity", "weather_entity",
            "cloud_entity", "thermostat_entity")
OCCUPIED = frozenset({OccupancyState.CONFIRMED_OCCUPANCY, OccupancyState.POSSIBLE_VACANCY})

ACTION_WORDS = {
    "moved": "Moved", "would_move": "Would move", "at_target": "Already there", "keep": "Keeping position",
    "hold": "Manual hold", "blocked": "Prevented", "failed": "Failed", "off": "Off", "released": "Hold released",
    "hold_started": "Manual hold started", "enabled": "Automatic enabled", "resumed": "Resumed",
    "mode": "Mode changed", "moving": "Moving", "config": "Mapping changed",
}
_NOT_LOGGED = frozenset({"moved", "moving"})  # "moved" is logged with its details when it is sent
POLICY_WORDS = {"next_phase": "Until the next automatic phase", "duration": "For a number of minutes",
                "vacant": "Until the room has been empty", "until_released": "Until you resume"}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _dt(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _key(preset_id: str, targets: Mapping[str, int | None]) -> list[Any]:
    return [preset_id, sorted((k, v) for k, v in targets.items() if v is not None)]


@dataclass(slots=True)
class WindowRuntime:
    daylight: Hysteresis = field(default_factory=Hysteresis)
    strong: Hysteresis = field(default_factory=Hysteresis)
    weather: Hysteresis = field(default_factory=Hysteresis)       # clear sky (weather evidence, opt-in)
    weather_history: History = field(default_factory=History)
    env: dict[str, Any] = field(default_factory=dict)              # the last readings (for the card)
    next_eval: datetime | None = None
    fallback: bool | None = None
    in_sun: bool = False
    lamp_on: bool = False
    lamp_off_at: datetime | None = None
    decision: Decision | None = None
    values: dict[str, Any] = field(default_factory=dict)
    timer: CALLBACK_TYPE | None = None
    notified: tuple[Any, ...] = ()
    notified_at: datetime | None = None
    skip_limits: bool = False   # the next move follows Enable or Resume: not delayed by the interval
    # stored
    hold: dict[str, Any] | None = None
    active_since: dict[str, datetime] = field(default_factory=dict)
    morning_since: datetime | None = None
    sleep_ended: datetime | None = None
    last_sleep: str | None = None
    last_base: str | None = None
    last_phase: str | None = None
    thermal: str | None = None          # the room's temperature band (kept for its hysteresis)
    applied: dict[str, Any] | None = None
    moves: dict[str, Any] = field(default_factory=dict)
    last_move_at: datetime | None = None
    last_condition: str | None = None   # the condition of the last automatic move
    status: dict[str, Any] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    # 0.6.4: a confirmed mapping change that applies at the next change of condition: {"condition", "at"}
    defer: dict[str, Any] | None = None

    def export(self) -> dict[str, Any]:
        hold = dict(self.hold, created=_iso(self.hold["created"])) if self.hold else None
        return {"hold": hold, "active_since": {k: _iso(v) for k, v in self.active_since.items()},
                "morning_since": _iso(self.morning_since), "sleep_ended": _iso(self.sleep_ended),
                "last_sleep": self.last_sleep, "last_base": self.last_base, "last_phase": self.last_phase,
                "thermal": self.thermal, "applied": self.applied,
                "moves": self.moves, "last_move_at": _iso(self.last_move_at),
                "last_condition": self.last_condition, "status": self.status,
                "log": self.log, "defer": self.defer}

    @classmethod
    def restore(cls, data: Any) -> WindowRuntime:
        rt = cls()
        if not isinstance(data, Mapping):
            return rt
        hold = data.get("hold")
        if isinstance(hold, Mapping) and (created := _dt(hold.get("created"))):
            rt.hold = {**hold, "created": created}
        rt.active_since = {k: d for k, v in (data.get("active_since") or {}).items() if (d := _dt(v))}
        rt.morning_since = _dt(data.get("morning_since"))
        rt.sleep_ended = _dt(data.get("sleep_ended"))
        rt.last_sleep = data.get("last_sleep") if data.get("last_sleep") in ("on", "off") else None
        rt.last_base = data.get("last_base") if isinstance(data.get("last_base"), str) else None
        rt.last_phase = data.get("last_phase") if isinstance(data.get("last_phase"), str) else rt.last_base
        rt.thermal = data.get("thermal") if data.get("thermal") in BAND_WORDS else None
        applied = data.get("applied")
        if isinstance(applied, Mapping):
            rt.applied = dict(applied)
            if rt.applied.get("status") == "running":
                rt.applied["status"] = "interrupted"  # never resumed: re-evaluated after the restart settles
        rt.moves = dict(data.get("moves")) if isinstance(data.get("moves"), Mapping) else {}
        rt.last_move_at = _dt(data.get("last_move_at"))
        rt.last_condition = data.get("last_condition") if isinstance(data.get("last_condition"), str) else None
        rt.status = dict(data.get("status")) if isinstance(data.get("status"), Mapping) else {}
        rt.log = [x for x in data.get("log") or () if isinstance(x, Mapping)][:LOG_LENGTH]
        defer = data.get("defer")
        rt.defer = dict(defer) if isinstance(defer, Mapping) and isinstance(defer.get("condition"), str) else None
        return rt


class AutoCurtains:
    def __init__(self, manager: CurtainManager, occupancy: Any | None = None) -> None:
        self.mgr = manager
        self.hass = manager.hass
        self.occupancy = occupancy
        self.runtimes: dict[str, WindowRuntime] = {}
        self.histories: dict[str, History] = {}
        self.temp_histories: dict[str, History] = {}   # °C per temperature entity
        # light sensor -> (value, since when it has had that value); survives restarts (stuck detection)
        self.value_since: dict[str, tuple[float | None, datetime]] = {}
        self.started_at: datetime | None = None
        self._tracking: CALLBACK_TYPE | None = None
        self._occupancy_unsubs: list[CALLBACK_TYPE] = []
        self._settle_timer: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------ storage
    def restore(self, data: Any) -> None:
        data = data if isinstance(data, Mapping) else {}
        self.runtimes = {k: WindowRuntime.restore(v) for k, v in data.items() if k in self.mgr.config.windows}

    def export(self) -> dict[str, Any]:
        return {k: rt.export() for k, rt in self.runtimes.items() if k in self.mgr.config.windows}

    def restore_sensors(self, data: Any) -> None:
        self.value_since = {}
        for entity_id, item in (data or {}).items() if isinstance(data, Mapping) else ():
            if isinstance(item, (list, tuple)) and len(item) == 2 and (since := _dt(item[1])):
                value = item[0] if isinstance(item[0], (int, float)) and not isinstance(item[0], bool) else None
                self.value_since[entity_id] = (value, since)

    def export_sensors(self) -> dict[str, Any]:
        return {k: [v, _iso(since)] for k, (v, since) in self.value_since.items()}

    def rt(self, window_id: str) -> WindowRuntime:
        return self.runtimes.setdefault(window_id, WindowRuntime())

    def forget(self, window_id: str) -> None:
        if (rt := self.runtimes.pop(window_id, None)) and rt.timer:
            rt.timer()

    # ---------------------------------------------------------- lifecycle
    @callback
    def start(self) -> None:
        self.started_at = dt_util.utcnow()
        self.reconfigure()
        self._settle_timer = async_track_point_in_utc_time(
            self.hass, self._settled, self.started_at + timedelta(seconds=STARTUP_SETTLE_SECONDS))

    @callback
    def _settled(self, _now: datetime) -> None:
        self._settle_timer = None
        self.evaluate_all()

    @callback
    def stop(self) -> None:
        if self._tracking:
            self._tracking()
            self._tracking = None
        for unsub in self._occupancy_unsubs:
            unsub()
        self._occupancy_unsubs.clear()
        if self._settle_timer:
            self._settle_timer()
            self._settle_timer = None
        for rt in self.runtimes.values():
            if rt.timer:
                rt.timer()
                rt.timer = None
        self.started_at = None

    @property
    def running(self) -> bool:
        return self.started_at is not None

    @callback
    def reconfigure(self) -> None:
        """Follow the inputs of every window again (after a configuration change) and evaluate."""
        if not self.running:
            return
        for window_id in [w for w in self.runtimes if w not in self.mgr.config.windows]:
            self.forget(window_id)
        if self._tracking:
            self._tracking()
            self._tracking = None
        ids: set[str] = set()
        areas: set[str] = set()
        for window in self.mgr.config.windows.values():
            values, _ = self.mgr.config.rules_for(window)
            if window.mode == "off":
                continue
            if window.sleep_protection and values["sleep_entity"]:
                ids.add(values["sleep_entity"])
            if values["lamp_enabled"] and values["lamp_entity"]:
                ids.add(values["lamp_entity"])
            if values["lux_entity"]:
                ids.add(values["lux_entity"])
                self._history(values["lux_entity"])
            for key in ENV_KEYS[1:]:
                if values[key]:
                    ids.add(values[key])
            if values["room_temp_entity"]:
                self._temp_history(values["room_temp_entity"])
            if window.area_id:
                areas.add(window.area_id)
        if ids:
            self._tracking = async_track_state_change_event(self.hass, sorted(ids), self._on_input)
        for unsub in self._occupancy_unsubs:
            unsub()
        self._occupancy_unsubs = []
        if self.occupancy is not None:
            for area_id in sorted(areas):
                self._occupancy_unsubs.append(self.occupancy.async_add_listener(
                    area_id, callback(lambda a=area_id: self._on_occupancy(a))))
        self.evaluate_all()

    # -------------------------------------------------------------- inputs
    def _history(self, entity_id: str) -> History:
        history = self.histories.get(entity_id)
        if history is None:
            history = self.histories[entity_id] = History()
            history.add(dt_util.utcnow(), self._lux_value(entity_id))
        return history

    def _lux_value(self, entity_id: str) -> float | None:
        return lux_value(self.hass.states.get(entity_id))

    def _temp_value(self, entity_id: str) -> float | None:
        return temperature_of(self.hass.states.get(entity_id), self.hass.config.units.temperature_unit)[0]

    def _temp_history(self, entity_id: str) -> History:
        """Room temperature in °C. Starts at the reading's last change: a temperature is slow and usually
        unchanged for a while, so its stable period does not restart at every reconfiguration."""
        history = self.temp_histories.get(entity_id)
        if history is None:
            history = self.temp_histories[entity_id] = History()
            state = self.hass.states.get(entity_id)
            start = min(state.last_changed, dt_util.utcnow()) if state is not None else dt_util.utcnow()
            history.add(start, self._temp_value(entity_id))
        return history

    def since_of(self, entity_id: str) -> datetime | None:
        """Since when a light sensor has reported its current value (for the stuck test)."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = lux_value(state)
        known = self.value_since.get(entity_id)
        if known is None or known[0] != value:
            # A restored record with the same value keeps its (older) start: a restart does not unstick it.
            self.value_since[entity_id] = (value, min(state.last_changed, dt_util.utcnow()))
            self.mgr.save_state()
        return self.value_since[entity_id][1]

    @callback
    def _on_input(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self.histories:
            self.histories[entity_id].add(dt_util.utcnow(), self._lux_value(entity_id))
            self.since_of(entity_id)
        if entity_id in self.temp_histories:
            self.temp_histories[entity_id].add(dt_util.utcnow(), self._temp_value(entity_id))
        for window in self.mgr.config.windows.values():
            values, _ = self.mgr.config.rules_for(window)
            if entity_id in (values["sleep_entity"], values["lamp_entity"], *(values[k] for k in ENV_KEYS)):
                self.evaluate(window.window_id)

    @callback
    def _on_occupancy(self, area_id: str) -> None:
        for window in self.mgr.config.windows.values():
            rt = self.runtimes.get(window.window_id)
            if window.area_id != area_id or rt is None:
                continue
            values = rt.values or self.mgr.config.rules_for(window)[0]
            if (rt.hold and rt.hold.get("policy") == "vacant") or (
                    values.get("glare_sensitive") and values.get("glare_when") == "occupied"):
                self.evaluate(window.window_id)

    @callback
    def on_sun(self) -> None:
        self.evaluate_all()

    @callback
    def on_cover(self, window_id: str) -> None:
        self.evaluate(window_id)

    @callback
    def evaluate_all(self) -> None:
        for window_id in list(self.mgr.config.windows):
            self.evaluate(window_id)

    # ------------------------------------------------------- manual events
    @callback
    def on_manual(self, window: Window, record: Mapping[str, Any]) -> None:
        """A movement Smart Occupancy did not command: hold the window (a new move starts a new hold)."""
        if window.mode == "off":
            return
        rt = self.rt(window.window_id)
        values, _ = self.mgr.config.rules_for(window)
        new = rt.hold is None or rt.hold.get("source") != "manual" or rt.hold.get("entity_id") != record.get(
            "entity_id") or rt.hold.get("at") != record.get("at")
        rt.hold = {"source": "manual", "created": dt_util.utcnow(), "policy": values["manual_policy"],
                   "phase": rt.last_phase or (rt.decision.phase if rt.decision else None),
                   "entity_id": record.get("entity_id"), "name": record.get("name"), "at": record.get("at"),
                   "from": record.get("from"), "to": record.get("to"), "by": record.get("source")}
        rt.applied = None  # the covers are no longer where automatic control put them
        if new:
            self._log(window, rt, "hold_started", f"{record.get('name')} moved by hand "
                      f"({record.get('from')} → {record.get('to')} %): kept "
                      f"{POLICY_WORDS[values['manual_policy']].lower()}")
        self.evaluate(window.window_id, force_notify=True)

    @callback
    def on_manual_preset(self, window: Window, preset, op: Mapping[str, Any]) -> None:
        """An administrator applied a preset: automatic control keeps it like a manual move."""
        if window.mode == "off":
            return
        rt = self.rt(window.window_id)
        values, _ = self.mgr.config.rules_for(window)
        rt.hold = {"source": "preset", "created": dt_util.utcnow(), "policy": values["manual_policy"],
                   "phase": rt.last_phase, "preset_id": preset.preset_id, "name": preset.name}
        rt.applied = None
        self._log(window, rt, "hold_started", f"{preset.name} applied by hand: kept "
                  f"{POLICY_WORDS[values['manual_policy']].lower()}")

    @callback
    def resume(self, window_id: str) -> None:
        """Resume Automatic Curtains: release the hold and apply the current condition at once."""
        window = self.mgr.config.windows[window_id]
        rt = self.rt(window_id)
        had = rt.hold is not None
        rt.hold = None
        rt.applied = None
        rt.status = {}
        rt.skip_limits = True
        rt.defer = None
        self._log(window, rt, "resumed", "Automatic control resumed" + ("" if had else " (there was no hold)"))
        self.evaluate(window_id, resume=True, force_notify=True)

    @callback
    def mode_changing(self, window: Window, new: str) -> None:
        """Called just before a window's mode changes (the configuration change then evaluates it)."""
        rt = self.rt(window.window_id)
        rt.applied = None
        rt.skip_limits = new == "automatic"
        rt.status = {}
        if new == "automatic":
            self._log(window, rt, "enabled", "Automatic enabled by an administrator")
        else:
            self._log(window, rt, "mode", f"Mode changed from {window.mode.capitalize()} to {new.capitalize()}")

    @callback
    def config_changed(self, window: Window, text: str, defer: Mapping[str, Any] | None) -> None:
        """A confirmed mapping change (Global Modes, 0.6.4): logged; with the "next change" policy the
        window keeps its current position until its condition changes. The configuration change itself then
        re-evaluates the window (nothing is sent here)."""
        rt = self.rt(window.window_id)
        if defer is not None and window.mode == "automatic":
            rt.defer = dict(defer)
        self._log(window, rt, "config", text)

    @callback
    def on_op_finished(self, op: Mapping[str, Any]) -> None:
        window_id = op["window_id"]
        rt = self.runtimes.get(window_id)
        window = self.mgr.config.windows.get(window_id)
        if rt is None or window is None:
            return
        if op.get("origin") == CurtainOrigin.AUTOMATIC.value and rt.applied and rt.applied.get("op_id") == \
                op["op_id"]:
            rt.applied["status"] = op["status"]
            if op["status"] not in SUCCESS:
                attempts = int(rt.applied.get("attempts", 1))
                delay = RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
                rt.applied["retry_at"] = _iso(dt_util.utcnow() + timedelta(seconds=delay))
                self._log(window, rt, "failed", op.get("explanation") or "The movement failed")
        self.evaluate(window_id, force_notify=True)

    # ------------------------------------------------------------ evidence
    def _evidence(self, window: Window, rt: WindowRuntime, values: Mapping[str, Any], now: datetime,
                  dry: bool = False) -> Evidence:
        hass = self.hass
        utc = dt_util.utcnow()
        sun = self.mgr.sun()
        ev = Evidence(now=now, elevation=sun.elevation)
        # sleep
        if window.sleep_protection:
            entity = values["sleep_entity"]
            state = hass.states.get(entity) if entity else None
            ev.sleep_entity = entity
            ev.sleep = state.state if state is not None and state.state in ("on", "off") else "unknown"
            if ev.sleep in ("on", "off"):
                if rt.last_sleep == "on" and ev.sleep == "off":
                    rt.sleep_ended = utc
                    if values["sleep_end"] == "hold" and rt.last_base == "sleep" and window.mode != "off":
                        rt.hold = {"source": "sleep_end", "created": utc, "policy": "until_released", "phase": "sleep"}
                        if not dry:
                            self._log(window, rt, "hold_started", "Sleep ended: the sleep position is kept until you "
                                      "resume")
                rt.last_sleep = ev.sleep
            ev.sleep_ended = dt_util.as_local(rt.sleep_ended) if rt.sleep_ended else None
        # window lamp
        if values["lamp_enabled"] and values["lamp_entity"]:
            on = hass.states.is_state(values["lamp_entity"], STATE_ON)
            if on:
                rt.lamp_on, rt.lamp_off_at, ev.lamp = True, None, "on"
            else:
                if rt.lamp_on:
                    rt.lamp_on, rt.lamp_off_at = False, utc
                release = rt.lamp_off_at + timedelta(seconds=values["lamp_release"]) if rt.lamp_off_at else None
                if release and utc < release:
                    ev.lamp, ev.lamp_release_at = "releasing", dt_util.as_local(release)
                else:
                    ev.lamp, rt.lamp_off_at = "off", None
        # outdoor light: missing, unavailable, stale, implausible or stuck readings are never evidence
        env: dict[str, Any] = {}
        entity = values["lux_entity"]
        if entity:
            ev.lux_entity = entity
            history = self._history(entity)
            state = hass.states.get(entity)
            reading = judge_lux(entity, state, now=utc, since=self.since_of(entity), elevation=sun.elevation,
                                stale_minutes=values["lux_stale"], stuck_minutes=values["lux_stuck"],
                                dark_lux=values["dusk_lux"])
            env["outdoor_lux"] = reading
            ev.lux_status, ev.lux, ev.lux_detail = reading.status, reading.value, reading.detail
            if reading.status in ("stuck", "implausible") and history.value is not None:
                # Untrustworthy readings are no evidence: once it reports real values again, daylight and
                # strong sunshine need their full stable periods again.
                history.add(utc, None)
            if reading.ok:
                ev.deadlines.append(reading.reported + timedelta(minutes=values["lux_stale"]))
                ev.deadlines.append(stuck_deadline(reading, values["lux_stuck"]))
            if ev.lux_status == "ok":
                stable = timedelta(minutes=values["daylight_stable"])
                ev.daylight, deadline = rt.daylight.evaluate(
                    history, utc, lambda v: v >= values["daylight_lux"], stable, lambda v: v <= values["dusk_lux"],
                    stable)
                ev.deadlines.append(deadline)
                ev.dusk_measured = ev.daylight is False
                strong, deadline = rt.strong.evaluate(
                    history, utc, lambda v: v >= values["sun_lux"], timedelta(minutes=values["sun_stable"]),
                    lambda v: v < values["sun_release_lux"], timedelta(minutes=values["sun_release"]))
                ev.strong = strong
                ev.deadlines.append(deadline)
            else:
                rt.daylight.reset()
                rt.strong.reset()
        else:
            el = sun.elevation
            if el is None:
                rt.fallback = None
            elif el >= values["fallback_elevation"]:
                rt.fallback = True
            elif el <= values["dusk_elevation"]:
                rt.fallback = False
            ev.fallback_daylight = rt.fallback
            env["outdoor_lux"] = Reading("outdoor_lux", None, unit="lx", detail="No light sensor chosen")
        # sun geometry (with a small hysteresis once the window is shading)
        solar = window.solar
        if rt.in_sun:
            solar = replace(solar, exposure_width=solar.exposure_width + SUN_MARGIN,
                            min_elevation=solar.min_elevation - 1.0,
                            max_elevation=None if solar.max_elevation is None else solar.max_elevation + 1.0)
        expo = exposure(window.effective_bearing, sun, solar)
        ev.exposed = expo["exposed"]
        ev.exposure_text = expo["text"]
        ev.in_sun = rt.in_sun
        self._environment(window, rt, values, ev, utc, sun, env)
        rt.env = env
        # occupancy (for the "until the room has been empty" hold)
        if self.occupancy is not None and window.area_id:
            result = self.occupancy.result(window.area_id)
            if result is not None:
                ev.occupancy_known = True
                if result.state == OccupancyState.VACANT and result.state_since is not None:
                    ev.vacant_since = dt_util.as_local(result.state_since)
        ev.deadlines = [dt_util.as_local(d) for d in ev.deadlines if d is not None]
        return ev

    def _environment(self, window: Window, rt: WindowRuntime, values: Mapping[str, Any], ev: Evidence,
                     utc: datetime, sun, env: dict[str, Any]) -> None:
        """Weather, temperatures and glare (0.6.3). Every reading is judged before it is used."""
        hass = self.hass
        unit = hass.config.units.temperature_unit

        def state_of(key: str):
            return hass.states.get(values[key]) if values[key] else None

        # weather and cloud cover: supplementary; evidence for sunshine only without a light sensor (opt-in)
        weather = judge_weather(values["weather_entity"], state_of("weather_entity"), values["cloud_entity"],
                                state_of("cloud_entity"), now=utc)
        env["weather"] = weather
        if weather.status != "none":
            ev.weather_text = weather.detail
        if values["sun_evidence"] == "weather" and ev.lux_status == "none":
            clear = clear_sky(weather, values["cloud_max"])
            if clear is None:
                rt.weather.reset()
                rt.weather_history = History()
            else:
                rt.weather_history.add(utc, 1.0 if clear else 0.0)
                ev.weather_sun, deadline = rt.weather.evaluate(
                    rt.weather_history, utc, lambda v: v >= 1, timedelta(minutes=values["sun_stable"]),
                    lambda v: v < 1, timedelta(minutes=values["sun_release"]))
                ev.deadlines.append(deadline)
                if weather.ok:
                    ev.weather_text = ("clear sky: " if clear else "not clear: ") + weather.detail
        else:
            rt.weather.reset()
        # outdoor temperature
        outdoor = judge_temperature("outdoor_temp", values["outdoor_temp_entity"], state_of("outdoor_temp_entity"),
                                    now=utc, hass_unit=unit, stale_minutes=values["temp_stale"])
        env["outdoor_temp"] = outdoor
        if outdoor.ok:
            ev.outdoor_warm = outdoor.value >= values["outdoor_heat_temp"]
            ev.outdoor_text = f"Outdoor temperature {outdoor.value:.1f} °C"
        elif outdoor.status != "none":
            ev.outdoor_text = f"Outdoor temperature unknown ({outdoor.detail})"
        # room temperature and its band (only used when temperature-aware sun is on)
        room = judge_temperature("room_temp", values["room_temp_entity"], state_of("room_temp_entity"), now=utc,
                                 hass_unit=unit, stale_minutes=values["temp_stale"])
        env["room_temp"] = room
        target = thermostat_target(state_of("thermostat_entity"), unit) if values["thermostat_entity"] else None
        c = comfort(values, target, values["thermostat_entity"])
        env["comfort"] = {"low": round(c.low, 1), "high": round(c.high, 1), "top": round(c.top, 1),
                          "preferred": round(c.preferred, 1), "source": c.source,
                          "thermostat": values["thermostat_entity"], "thermostat_target": target}
        if room.ok:
            history = self._temp_history(values["room_temp_entity"])
            band, deadline = track_band(history, utc, rt.thermal, c, timedelta(minutes=values["temp_stable"]))
            rt.thermal = band
            ev.deadlines.append(deadline)
            ev.deadlines.append(room.reported + timedelta(minutes=values["temp_stale"]))
            if values["thermal_enabled"]:
                ev.thermal, ev.thermal_text = band, band_text(band, room.value, c)
            env["band"] = band
        else:
            ev.thermal_text = (f"Room temperature unknown ({room.detail})" if room.status != "none"
                               else "No room temperature chosen")
            env["band"] = None
        # glare
        ev.glare = False
        if values["glare_sensitive"]:
            reasons: list[str] = []
            ok = True
            if values["glare_when"] == "occupied" and self.occupancy is not None and window.area_id:
                result = self.occupancy.result(window.area_id)
                if result is None:
                    reasons.append("the room has no occupancy, so glare protection applies always")
                elif result.state in OCCUPIED:
                    reasons.append("the room is occupied")
                else:
                    ok = False
                    reasons.append("the room is not occupied")
            indoor = None
            if values["indoor_lux_entity"]:
                indoor = judge_lux(values["indoor_lux_entity"], state_of("indoor_lux_entity"), now=utc,
                                   since=None, elevation=None, stale_minutes=values["lux_stale"], stuck_minutes=0,
                                   dark_lux=0, role="indoor_lux")
                env["indoor_lux"] = indoor
            if values["glare_indoor_lux"]:
                if indoor is not None and indoor.ok:
                    if indoor.value < values["glare_indoor_lux"]:
                        ok = False
                        reasons.append(f"indoor light {indoor.value:.0f} lx is below {values['glare_indoor_lux']} lx")
                    else:
                        reasons.append(f"indoor light {indoor.value:.0f} lx")
                else:
                    reasons.append("the indoor light sensor is not usable, so it is not required")
            ev.glare = ok
            ev.glare_text = "; ".join(reasons)
        elif values["indoor_lux_entity"]:
            env["indoor_lux"] = judge_lux(values["indoor_lux_entity"], state_of("indoor_lux_entity"), now=utc,
                                          since=None, elevation=None, stale_minutes=values["lux_stale"],
                                          stuck_minutes=0, dark_lux=0, role="indoor_lux")

    # ---------------------------------------------------------- evaluation
    @callback
    def evaluate(self, window_id: str, *, resume: bool = False, force_notify: bool = False) -> Decision | None:
        if not self.running:
            return None
        window = self.mgr.config.windows.get(window_id)
        if window is None:
            return None
        rt = self.rt(window_id)
        now = dt_util.now()
        values, _sources = self.mgr.config.rules_for(window)
        rt.values = values
        if window.mode == "off":
            rt.decision = None
            self._status(window, rt, "off", "Off: curtains are not evaluated", None, force_notify)
            self._schedule(window_id, rt, None)
            return None
        ev = self._evidence(window, rt, values, now)
        flags = self.flags(window)
        decision = decide(flags, values, ev, hold=rt.hold, active_since=rt.active_since,
                          morning_since=rt.morning_since)
        if decision.hold_release and rt.hold is not None:
            self._log(window, rt, "released", f"Manual hold released: {decision.hold_release}")
            rt.hold = None
            rt.applied = None
            decision = decide(flags, values, ev, hold=None, active_since=rt.active_since,
                              morning_since=rt.morning_since)
        if decision.morning_start is not None:
            rt.morning_since = decision.morning_start
        utc = dt_util.utcnow()
        for cond in PROTECTIVE:
            if decision.base == cond:
                rt.active_since.setdefault(cond, utc)
            else:
                rt.active_since.pop(cond, None)
        rt.last_base = decision.base
        rt.last_phase = decision.phase
        rt.in_sun = decision.base in SUN_BASES or decision.sun_hold
        rt.decision = decision
        deadline = self._act(window, rt, values, decision, utc, resume, force_notify)
        self._schedule(window_id, rt, min((d for d in (decision.next_check, deadline) if d is not None),
                                          default=None))
        return decision

    @staticmethod
    def flags_for(config: CurtainConfig, window: Window) -> Flags:
        """The window's switches and its Global Modes resolver (0.6.4) for one configuration."""
        return Flags(window.sleep_protection, window.privacy, window.solar_protection, window.conditions,
                     targets=lambda condition, phase: effective(config, window, condition, phase))

    def flags(self, window: Window) -> Flags:
        return self.flags_for(self.mgr.config, window)

    def simulate(self, window: Window, values: Mapping[str, Any], config: CurtainConfig | None = None) -> Decision:
        """What a window would decide now with these settings (and, 0.6.4, this curtain configuration).
        Changes nothing: it works on a copy of the window's runtime and never logs, schedules, notifies or
        sends."""
        source = self.rt(window.window_id)
        timer, source.timer = source.timer, None
        try:
            rt = copy.deepcopy(source)
        finally:
            source.timer = timer
        now = dt_util.now()
        ev = self._evidence(window, rt, values, now, dry=True)
        flags = self.flags_for(config or self.mgr.config, window)
        decision = decide(flags, values, ev, hold=rt.hold, active_since=rt.active_since,
                          morning_since=rt.morning_since)
        if decision.hold_release and rt.hold is not None:
            decision = decide(flags, values, ev, hold=None, active_since=rt.active_since,
                              morning_since=rt.morning_since)
        return decision

    def _schedule(self, window_id: str, rt: WindowRuntime, when: datetime | None) -> None:
        if rt.timer:
            rt.timer()
            rt.timer = None
        if not self.running:
            rt.next_eval = None
            return
        cap = dt_util.utcnow() + timedelta(seconds=RECHECK_SECONDS)
        when = min(when, cap) if when else cap
        rt.next_eval = dt_util.as_utc(when)
        rt.timer = async_track_point_in_utc_time(
            self.hass, callback(lambda _now: self._timer_fired(window_id)), dt_util.as_utc(when) + timedelta(
                milliseconds=50))

    @callback
    def _timer_fired(self, window_id: str) -> None:
        if (rt := self.runtimes.get(window_id)) is not None:
            rt.timer = None
        self.evaluate(window_id)

    def _act(self, window: Window, rt: WindowRuntime, values: Mapping[str, Any], decision: Decision,
             utc: datetime, resume: bool, force_notify: bool) -> datetime | None:
        """Send (Automatic) or describe (Observing) what the decision needs. Returns the next time to look again."""
        preset = decision.target.preset if decision.target.has_target else None
        if rt.defer and (resume or (decision.condition != rt.defer.get("condition")
                                    and decision.condition not in PHASE_EXEMPT)):
            # The condition changed (or Resume): the new mapping applies normally from now on. Waiting for
            # daylight (e.g. sensors settling after a restart) is not a change of condition.
            rt.defer = None
        if decision.condition == "manual_override":
            self._status(window, rt, "hold", decision.text, None, force_notify)
            return None
        if preset is None:
            self._status(window, rt, "keep", decision.text, None, force_notify)
            return None
        mgr = self.mgr
        plan = build_plan(self.hass, window, preset, mgr.config.settings.tolerance, mgr.busy_covers())
        moves = [p for p in plan if p.decision == "move"]
        bad = [p for p in plan if p.decision in ("unavailable", "missing", "unsupported")]
        name = preset.name
        cond = CONDITION_WORDS.get(decision.condition, decision.condition)
        source = decision.target.source_word
        if window.mode == "observing":
            if bad:
                text = f"Observing: {cond} → {name}, but {bad[0].reason}"
            elif moves:
                text = (f"Observing: would move {len(moves)} cover{'s' if len(moves) != 1 else ''} to {name} ({cond}, "
                        f"{source})")
            else:
                text = f"{name}: every cover is already there ({cond})"
            self._status(window, rt, "would_move" if moves and not bad else "at_target" if not bad else "blocked",
                         text, preset, force_notify, code="observing" if moves else None)
            return None

        # ------------------------------------------------------- Automatic
        def blocked(code: str, text: str, when: datetime | None = None) -> datetime | None:
            self._status(window, rt, "blocked", text, preset, force_notify, code=code)
            return when

        if self.started_at and utc < self.started_at + timedelta(seconds=STARTUP_SETTLE_SECONDS):
            until = self.started_at + timedelta(seconds=STARTUP_SETTLE_SECONDS)
            return blocked("startup", f"Home Assistant just started: waiting for sensors to settle before "
                           f"{name} is applied", until)
        if (op := mgr.running(window.window_id)) is not None:
            self._status(window, rt, "moving", f"Moving to {op['preset_name']}: waiting for every cover to report; "
                         "no other command is sent meanwhile", preset, force_notify)
            return None
        if bad:
            return blocked("unavailable", f"{cond} → {name}, but {bad[0].reason}: no command until it reports again")
        if busy := [p for p in plan if p.decision == "busy"]:
            return blocked("busy", busy[0].reason)
        key = _key(preset.preset_id, preset.positions)
        applied = rt.applied if rt.applied and rt.applied.get("key") == key else None
        if moves and rt.defer and not applied:
            since = _dt(rt.defer.get("at"))
            return blocked("deferred", f"The mapping for {cond} changed"
                           + (f" at {dt_util.as_local(since).strftime('%H:%M')}" if since else "")
                           + f": {name} applies at the next change of condition (as chosen when it was saved)")
        if not moves:
            rt.skip_limits = False
            if not applied or applied.get("status") not in SUCCESS:
                rt.applied = {"key": key, "preset_id": preset.preset_id, "condition": decision.condition,
                              "status": "nothing_to_do", "attempts": 0, "at": _iso(utc), "op_id": None}
            self._status(window, rt, "at_target", f"{name}: every cover is already there ({cond})", preset,
                         force_notify)
            return None
        if conflicts := mgr.blocking_automations(window):
            names = ", ".join(c["name"] or c["automation"] for c in conflicts[:3])
            return blocked("conflict", f"{len(conflicts)} enabled automation(s) may also move these covers ({names}). "
                           "Disable them or allow them in Diagnostics; nothing is sent until then")
        attempts = 0
        if applied:
            if applied.get("status") in SUCCESS:
                return blocked("not_corrected", f"{name} was applied; a cover reports a different position now and "
                               "is not corrected automatically")
            attempts = int(applied.get("attempts", 0))
            if attempts > values["max_retries"]:
                return blocked("gave_up", f"{name} failed {attempts} times; not retried until the condition changes "
                               "or you resume")
            retry_at = _dt(applied.get("retry_at"))
            if retry_at and utc < retry_at and not resume:
                return blocked("retry_wait", f"{name} did not complete; retrying at "
                               f"{dt_util.as_local(retry_at).strftime('%H:%M')}", retry_at)
        today = dt_util.now().date().isoformat()
        count = int(rt.moves.get("count", 0)) if rt.moves.get("date") == today else 0
        if count >= values["max_moves"] and decision.condition not in BUDGET_EXEMPT:
            return blocked("budget", f"{count} automatic moves today (limit {values['max_moves']}): {name} is not "
                           "applied until tomorrow; sleep and night still move")
        # Not delayed: the first move after enabling or resuming, a retry of the same targets, and leaving
        # sleep, night, evening privacy or the lamp.
        resume = resume or rt.skip_limits
        after_exempt = rt.last_condition in DWELL_EXEMPT
        if not resume and not attempts and not after_exempt and decision.condition not in DWELL_EXEMPT \
                and rt.last_move_at is not None \
                and values["min_interval"] and utc < rt.last_move_at + timedelta(minutes=values["min_interval"]):
            until = rt.last_move_at + timedelta(minutes=values["min_interval"])
            return blocked("dwell", f"{cond} → {name} waits for the minimum interval between moves (until "
                           f"{dt_util.as_local(until).strftime('%H:%M')})", until)
        # ------------------------------------------------------ send
        reason = decision.text
        op, sends, context = mgr.begin(window, preset, CurtainOrigin.AUTOMATIC, condition=decision.condition,
                                       reason=reason)
        rt.applied = {"key": key, "preset_id": preset.preset_id, "condition": decision.condition, "status": "running",
                      "attempts": attempts + 1, "at": _iso(utc), "op_id": op["op_id"]}
        rt.moves = {"date": today, "count": count + 1}
        rt.last_move_at = utc
        rt.last_condition = decision.condition
        rt.skip_limits = False
        text = (f"{reason}. Applying {name} ({cond}): moving {len(sends)} cover{'s' if len(sends) != 1 else ''}"
                + (f" (attempt {attempts + 1})" if attempts else "") + f". Preset from {source}")
        self._status(window, rt, "moved", text, preset, True)
        self._log(window, rt, "moved", text, preset)
        self.hass.async_create_task(mgr.async_run(op, sends, context), f"smart_occupancy curtain {op['op_id']}")
        return None

    # -------------------------------------------------------------- record
    def _status(self, window: Window, rt: WindowRuntime, action: str, text: str, preset, force: bool,
                code: str | None = None) -> None:
        decision = rt.decision
        status = {"action": action, "word": ACTION_WORDS.get(action, action), "code": code, "text": text,
                  "condition": decision.condition if decision else None,
                  "preset_id": preset.preset_id if preset else None, "preset_name": preset.name if preset else None}
        key = (action, code, status["condition"], status["preset_id"])
        old_key = (rt.status.get("action"), rt.status.get("code"), rt.status.get("condition"),
                   rt.status.get("preset_id"))
        utc = dt_util.utcnow()
        status["at"] = rt.status.get("at") if key == old_key and rt.status.get("at") else _iso(utc)
        changed = key != old_key
        rt.status = status
        just_moved = action == "at_target" and rt.log and rt.log[0]["action"] == "moved" and preset is not None \
            and rt.log[0]["preset_name"] == preset.name
        if changed and action not in _NOT_LOGGED and not just_moved:
            self._log(window, rt, action, text, preset)
        if changed or force or rt.notified != (key, text) and (
                rt.notified_at is None or (utc - rt.notified_at).total_seconds() >= NOTIFY_SECONDS):
            rt.notified, rt.notified_at = (key, text), utc
            self.mgr.notify()
            self.mgr.save_state()

    def _log(self, window: Window, rt: WindowRuntime, action: str, text: str, preset=None) -> None:
        decision = rt.decision
        entry = {"at": _iso(dt_util.utcnow()), "action": action, "word": ACTION_WORDS.get(action, action),
                 "condition": decision.condition if decision else None,
                 "preset_name": preset.name if preset else None, "text": text, "mode": window.mode,
                 "source": decision.target.source if decision and preset else None,
                 "source_word": decision.target.source_word if decision and preset else None,
                 "window": window.name, "summary": summary(window, decision, action, text, preset)}
        rt.log = [entry, *rt.log][:LOG_LENGTH]
        _LOGGER.info("curtain_decision window=%s action=%s condition=%s text=%s", window.window_id, action,
                     entry["condition"], text)
        self.mgr.save_state()

    # --------------------------------------------------------------- model
    def model(self, window: Window) -> dict[str, Any]:
        rt = self.rt(window.window_id)
        values, sources = self.mgr.config.rules_for(window)
        decision = rt.decision
        hold = None
        if rt.hold:
            hold = {k: v for k, v in rt.hold.items() if k != "created"}
            hold["created"] = _iso(rt.hold["created"])
            hold["policy_word"] = POLICY_WORDS.get(rt.hold.get("policy"), rt.hold.get("policy"))
        sleep_state = None
        if values["sleep_entity"]:
            state = self.hass.states.get(values["sleep_entity"])
            sleep_state = state.state if state is not None else "missing"
        lamp_state = None
        if values["lamp_entity"]:
            state = self.hass.states.get(values["lamp_entity"])
            lamp_state = state.state if state is not None else "missing"
        lux = values["lux_entity"]
        return {
            "environment": self._env_model(window, rt, values, decision),
            "next_evaluation": _iso(dt_util.as_local(rt.next_eval)) if rt.next_eval else None,
            "decision": decision.as_dict() if decision else None,
            "status": rt.status or None,
            "hold": hold,
            "sleep": {"enabled": window.sleep_protection, "entity": values["sleep_entity"], "state": sleep_state,
                      "active": window.sleep_protection and sleep_state == "on"},
            "lamp": {"enabled": bool(values["lamp_enabled"] and values["lamp_entity"]), "entity": values["lamp_entity"],
                     "state": lamp_state, "preset_id": effective(self.mgr.config, window, "lamp").preset_id,
                     "active": bool(decision and decision.condition == "lamp"),
                     "releasing_until": _iso(rt.lamp_off_at + timedelta(seconds=values["lamp_release"]))
                     if rt.lamp_off_at else None},
            "light": {"entity": lux, "value": self._lux_value(lux) if lux else None,
                      "daylight": rt.daylight.state if lux else rt.fallback, "strong": rt.strong.state if lux else None,
                      "fallback": values["fallback"]},
            "morning_since": _iso(rt.morning_since),
            "applied": rt.applied,
            "defer": rt.defer,
            "moves_today": int(rt.moves.get("count", 0)) if rt.moves.get("date") == dt_util.now().date().isoformat()
            else 0,
            "last_move_at": _iso(rt.last_move_at),
            "log": rt.log,
            "precedence": precedence(values),
            "values": {k: (list(v) if isinstance(v, tuple) else v) for k, v in values.items()},
            "sources": sources,
            "warnings": consistency(values),
        }

    def _env_model(self, window: Window, rt: WindowRuntime, values: Mapping[str, Any],
                   decision: Decision | None) -> dict[str, Any]:
        """Everything the card shows about the sun and the environment of one window (0.6.3)."""
        from .solar import calibrate

        sun = self.mgr.sun()
        bearing = window.effective_bearing
        expo = exposure(bearing, sun, window.solar)
        cal = calibrate(self.mgr.samples)
        readings = {role: r.as_dict() for role, r in rt.env.items() if isinstance(r, Reading)}
        band = rt.env.get("band")
        return {
            "sun": {**sun.as_dict(), "offset": expo["offset"], "incidence": expo["incidence"],
                    "status": expo["status"], "text": expo["text"], "exposed": expo["exposed"],
                    "offset_via_sun_angle": window_angle_from_sun_angle(sun.sun_angle, cal, bearing),
                    "calibration": cal["status"]},
            "readings": readings,
            "comfort": rt.env.get("comfort"),
            "band": band, "band_word": BAND_WORDS.get(band) if band else None,
            "direct_sun": decision.direct_sun if decision else None,
            "thermal": decision.thermal if decision else None,
            "thermal_enabled": values["thermal_enabled"],
            "glare": {"enabled": bool(values["glare_sensitive"]), "when": values["glare_when"],
                      "active": bool(decision and decision.condition == "glare")},
            "sun_evidence": values["sun_evidence"],
        }

    def diagnostics(self) -> dict[str, Any]:
        out = {}
        for window in self.mgr.config.windows.values():
            m = self.model(window)
            out[window.window_id] = {k: m[k] for k in ("decision", "status", "hold", "sleep", "lamp", "light",
                                                       "environment", "next_evaluation", "morning_since",
                                                       "applied", "moves_today", "precedence", "values", "sources",
                                                       "log")}
        return {"started_at": _iso(self.started_at), "windows": out, "sensors": self.export_sensors()}


def summary(window: Window, decision: Decision | None, action: str, text: str, preset) -> str:
    """One line for the decision log, e.g. "Morning. Stova Suður → Half shade (Global Mode)." (0.6.4)."""
    if decision is None or action in ("enabled", "resumed", "mode", "released", "hold_started", "config", "off"):
        return text
    word = MODE_WORDS.get(decision.condition, CONDITION_WORDS.get(decision.condition, decision.condition))
    lead = ""
    if decision.lamp_bypassed:
        base = MODE_WORDS.get(decision.base, decision.base)
        lead = f"Lamp override active. {base} is configured to bypass lamp control. "
    if decision.condition == "manual_override":
        return f"{lead}Manual hold. {window.name} keeps its position."
    if decision.condition == "waiting":
        return f"{lead}Waiting for daylight. {window.name} keeps its position."
    target = decision.target
    if preset is None or not target.has_target:
        if target.state == "no_action":
            return f"{lead}{word}. {window.name} is set to No action ({target.source_word}); leaving positions unchanged."
        return f"{lead}{word}. {window.name} has no assignment; leaving positions unchanged."
    unassigned = target.unassigned()
    tail = f" {', '.join(unassigned)} unchanged (no assignment)." if unassigned else ""
    verb = {"would_move": " would go", "blocked": " (prevented)", "failed": " (failed)"}.get(action, "")
    return f"{lead}{word}. {window.name}{verb} → {preset.name} ({target.source_word}).{tail}"
