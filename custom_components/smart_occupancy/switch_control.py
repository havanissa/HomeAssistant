"""What a wall switch press does (Phase 0.3.5).

Every press of a configured switch is first a *physical, manual* action:

* light changes during the next seconds are attributed to the switch (the
  existing wall switch attribution), so a directly bound switch or an existing
  automation still creates a manual override;
* optionally it refreshes the room's activity in the occupancy engine (never
  occupies a room by itself).

Then, only for a switch set to **Active**, the button's mapping is executed.
The switch's own mode is the only gate (0.3.6): the room's automatic lighting
mode (Observing / Automatic) does not block a deliberately activated switch:

* light commands go to the room's managed lights through the same group
  handling as mood lighting (a managed group is commanded, not its members),
  respecting each light's capabilities;
* every executed light command registers a manual override for those lights,
  so automatic occupancy, mood and global adjustment changes do not undo the
  person's choice until the override expires (or the room becomes vacant);
* an ON press is an explicit request: it is not gated by daylight or
  occupancy. Manual mood (``Main Mood = Manual``) pauses automatic lighting
  only: wall switches keep working.

Presses of one switch run one after another (at most ``MAX_QUEUED_PRESSES``
waiting); hold-to-dim sends at most one command per interval, waits for each
command to finish, stops on STOP/release or after the safety timeout, and
never goes beyond the configured minimum and maximum brightness.

A switch in **Observing** never sends anything; its presses are registered
and logged. Every press ends with one outcome (diagnostics):

* ``observed``   - the switch is Observing: registered, nothing executed;
* ``not_mapped`` - the button has no assigned action;
* ``blocked``    - the switch is Active, but something else prevents the
  action (the room is not enabled, too many presses queued, the switch was
  set to Observing before the press ran);
* ``succeeded`` / ``failed`` - the action was dispatched and Home Assistant
  accepted it or reported an error (an unavailable target is a failure).

Decision log (0.3.7): every press also becomes one decision record of the
switch (``decisions.py``): the button, the assigned action, the explanation,
the switch's mode and a status. A succeeded light command is *dispatched*
until the lights report the requested state (then *executed*); scenes and
scripts stay *dispatched* (their effect cannot be observed); presses that
changed nothing are *no change*. Repeats of the same button with the same
result within a minute are one entry.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import (
    COMMAND_TIMEOUT_SECONDS,
    DECISION_PRESS_MERGE_SECONDS,
    LEVEL_MEMORY_SECONDS,
    MAX_QUEUED_PRESSES,
    ButtonAction,
    OnValues,
)
from .decisions import (
    BLOCKED,
    DISPATCHED,
    EXECUTED,
    FAILED,
    MODULE_SWITCH,
    NO_ACTION,
    NO_CHANGE,
    OBSERVED,
    PARTIAL,
    Check,
    light_checks,
    minutes_text,
)
from .lighting import light_capabilities
from .supervision import assert_switch_control_permitted
from .planner import build_command
from .switches import (
    IGNORED_WORDS,
    LIGHT_ACTIONS,
    ROOM_TARGET,
    ButtonEvent,
    ButtonMapping,
    SwitchConfig,
    action_label,
)

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)
_OFFLINE = (STATE_UNAVAILABLE, STATE_UNKNOWN)

Key = tuple[str, str]


class _DimRun:
    """One hold-to-dim run: stopped by STOP/release (wakes at once) or between ticks of the scheduler."""

    def __init__(self) -> None:
        self.stopped = False
        self.wake = asyncio.Event()

    def stop(self) -> None:
        self.stopped = True
        self.wake.set()


# Outcomes of a press (see the module docstring).
OUTCOME_OBSERVED = "observed"
OUTCOME_NOT_MAPPED = "not_mapped"
OUTCOME_BLOCKED = "blocked"
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"


class _Result:
    def __init__(self, text: str, ok: bool | None, command: str | None = None, override: bool = False,
                 outcome: str | None = None, *, unchanged: bool = False, internal: bool = False,
                 partial: bool = False, checks: list[Check] | None = None) -> None:
        self.text, self.ok, self.command, self.override = text, ok, command, override
        self.outcome = outcome or (OUTCOME_SUCCEEDED if ok else OUTCOME_FAILED if ok is False else OUTCOME_OBSERVED)
        # For the decision log: nothing needed to change / done inside Smart Occupancy (a room mood) /
        # some targets failed / what the targets should report afterwards.
        self.unchanged, self.internal, self.partial, self.checks = unchanged, internal, partial, checks


def _plural(n: int, word: str = "light") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


class SwitchControl:
    """Executes button mappings for switches set to Active."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self._locks: dict[Key, asyncio.Lock] = {}
        self._queued: dict[Key, int] = {}
        self._dims: dict[Key, _DimRun] = {}
        self._levels: dict[str, tuple[int, float]] = {}  # entity -> (brightness %, monotonic)
        self._mood_timers: dict[str, CALLBACK_TYPE] = {}
        # What the light commands of the press being executed should leave behind (per switch).
        self._sent: dict[Key, dict[str, Check]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    # ------------------------------------------------------------- lifecycle
    @callback
    def async_start(self) -> None:
        for area_id, rt in self.ctrl.runtime.items():
            if rt.temp_mood:
                self._schedule_mood_expiry(area_id)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for run in self._dims.values():
            run.stop()
        for cancel in self._mood_timers.values():
            cancel()
        self._mood_timers.clear()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._dims.clear()

    def _create_task(self, coro) -> asyncio.Task | None:
        if self._stopped:
            coro.close()
            return None
        task = self.hass.async_create_background_task(coro, "smart_occupancy wall switch")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def dimming(self, area_id: str, switch_id: str) -> bool:
        return (area_id, switch_id) in self._dims

    # ----------------------------------------------------------------- input
    @callback
    def async_handle(self, event: ButtonEvent) -> None:
        ctrl = self.ctrl
        key = (event.area_id, event.switch_id)
        switch = ctrl.lighting.room(event.area_id).switch(event.switch_id)
        if switch is None or self._stopped:
            return
        if event.area_id not in ctrl.config.enabled_rooms:
            self._record(key, event, _Result(IGNORED_WORDS["disabled"], None, outcome=OUTCOME_BLOCKED))
            return
        # A person pressed a physical switch: light changes right after it are theirs.
        context = ctrl.note_switch_press(event.area_id)
        if switch.activity_evidence:
            ctrl.coordinator.occupancy.async_note_activity(event.area_id, f"wall switch {switch.name}")
        mapping = switch.mapping(event.raw)
        if not switch.controls:
            text = "Observing: registered as a manual action"
            if mapping:
                text += f" (assigned: {mapping.describe()}; not executed)"
            self._record(key, event, _Result(text, None, outcome=OUTCOME_OBSERVED))
            return
        if mapping is None:
            self._record(key, event, _Result("Registered as a manual action; " + IGNORED_WORDS["not_mapped"], None,
                                             outcome=OUTCOME_NOT_MAPPED))
            return
        if mapping.action == ButtonAction.DIM_STOP:
            stopped = self._stop_dim(key)
            self._record(key, event, _Result("Dimming stopped" if stopped else "Stop: nothing was dimming", True,
                                             internal=stopped, unchanged=not stopped))
            return
        self._stop_dim(key)  # any other press ends hold-to-dim
        if mapping.action in (ButtonAction.DIM_UP, ButtonAction.DIM_DOWN):
            run = _DimRun()
            if self._create_task(self._dim(key, event, switch, mapping, context, run)) is not None:
                self._dims[key] = run
            return
        if self._queued.get(key, 0) >= MAX_QUEUED_PRESSES:
            self._record(key, event, _Result(IGNORED_WORDS["busy"], False, outcome=OUTCOME_BLOCKED))
            return
        self._queued[key] = self._queued.get(key, 0) + 1
        self._create_task(self._run(key, event, switch, mapping, context))

    async def _run(self, key: Key, event: ButtonEvent, switch: SwitchConfig, mapping: ButtonMapping,
                   context: Context) -> None:
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                if self._stopped:
                    return
                if not self.ctrl.switch_active(event.area_id, event.switch_id):
                    result = _Result(IGNORED_WORDS["switched_to_observe"], None, outcome=OUTCOME_BLOCKED)
                else:
                    self._sent.pop(key, None)
                    try:
                        result = await self._execute(event, switch, mapping, context)
                    except Exception as err:  # noqa: BLE001 - one switch never stops the controller
                        _LOGGER.warning("Wall switch %s: %s failed: %s", switch.name, event.raw, err)
                        result = _Result(f"Failed: {err}", False)
                self._record(key, event, result)
        finally:
            self._queued[key] = max(self._queued.get(key, 1) - 1, 0)

    def _record(self, key: Key, event: ButtonEvent, result: _Result) -> None:
        status = self.ctrl.switch_inputs.status.get(key)
        # Only an executed command collects light checks (a STOP press must not take the dimming run's).
        sent = self._sent.pop(key, {}) if result.command is not None else {}
        if status is None:
            return
        self._decide(key, event, result, list(sent.values()))
        status.last_result, status.last_result_ok = result.text, result.ok
        status.last_outcome = result.outcome
        if result.command is not None:
            status.last_command = result.command
        status.override = result.override
        status.history.append({"at": event.when, "raw": event.raw, "label": action_label(event.raw),
                               "result": result.text, "ok": result.ok, "outcome": result.outcome,
                               "source": event.source})
        # Executed (or failed / blocked) actions at info; observed presses only at debug (no log noise).
        log = _LOGGER.info if result.outcome in (OUTCOME_SUCCEEDED, OUTCOME_FAILED, OUTCOME_BLOCKED) else _LOGGER.debug
        log("switch_press area=%s switch=%s action=%s source=%s outcome=%s result=%s", event.area_id,
            event.switch_id, event.raw, event.source, result.outcome, result.text)
        self.ctrl.mark_dirty()
        self.ctrl.notify()

    def _decide(self, key: Key, event: ButtonEvent, result: _Result, sent: list[Check]) -> None:
        """The press as a decision record of the switch (built from what was actually done)."""
        area_id, switch_id = key
        switch = self.ctrl.lighting.room(area_id).switch(switch_id)
        if switch is None:
            return
        mapping = switch.mapping(event.raw)
        label = action_label(event.raw)
        planned = mapping.describe() if mapping else "No action"
        outcome = result.outcome
        checks = result.checks or sent
        if outcome == OUTCOME_OBSERVED:
            status = OBSERVED
            text = (f"Button {label}: the switch is Observing, so "
                    + (f"{planned} is assigned but not executed." if mapping else "nothing is assigned to it.")
                    + " Registered as a manual action.")
        elif outcome == OUTCOME_NOT_MAPPED:
            status = NO_ACTION
            text = f"Button {label}: no action is assigned to this button (registered as a manual action)."
        elif outcome == OUTCOME_BLOCKED:
            status = BLOCKED
            text = f"Button {label}: not executed — {result.text}."
        elif outcome == OUTCOME_FAILED:
            status = PARTIAL if result.partial else FAILED
            text = f"Button {label}: {result.text}."
        elif result.unchanged:
            status = NO_CHANGE
            text = f"Button {label}: {result.text}."
        else:
            status = DISPATCHED if checks or not result.internal else EXECUTED
            text = f"Button {label}: {result.text}."
        if result.override and outcome in (OUTCOME_SUCCEEDED, OUTCOME_FAILED):
            minutes = switch.override_minutes if switch.override_minutes is not None else \
                self.ctrl.lighting.override_minutes(area_id)
            text += f" Automatic lighting leaves these lights as set {minutes_text(minutes)}."
        details: dict[str, Any] = {
            "trigger": "button press", "switch": switch.name, "button": event.raw, "press": event.press,
            "input": event.source, "trigger_entity": switch.event_entity if event.source == "event_entity" else None,
            "assigned_action": planned, "result": result.text,
        }
        if result.command:
            details["service"] = result.command
        if status in (FAILED, PARTIAL):
            details["errors"] = result.text
        if result.override:
            details["manual_override"] = True
        target = None
        if mapping is not None:
            if mapping.target and mapping.action in LIGHT_ACTIONS:
                target = "Room lights" if mapping.target == ROOM_TARGET else self._names([mapping.target])
            elif mapping.entity:
                target = self._names([mapping.entity])
            elif mapping.mood:
                target = mapping.mood
        self.ctrl.decisions.record(
            f"{area_id}/{switch_id}", module=MODULE_SWITCH, trigger="button", kind="press", action=planned,
            explanation=text, mode="active" if switch.controls else "observing", status=status, target=target,
            details=details, key=("press", event.raw, planned, status), merge_seconds=DECISION_PRESS_MERGE_SECONDS,
            checks=checks if status == DISPATCHED else None,
        )

    # --------------------------------------------------------------- targets
    def _targets(self, area_id: str, mapping: ButtonMapping) -> tuple[list[str], list[str]]:
        """(online targets, offline targets) of a light action."""
        if mapping.target == ROOM_TARGET:
            targets = list(self.ctrl.managed_room(area_id).controlled)
        else:
            targets = [mapping.target] if mapping.target.startswith("light.") else []
        online, offline = [], []
        for entity_id in targets:
            state = self.hass.states.get(entity_id)
            (offline if state is None or state.state in _OFFLINE else online).append(entity_id)
        return online, offline

    def _level(self, entity_id: str) -> int | None:
        remembered = self._levels.get(entity_id)
        if remembered and time.monotonic() - remembered[1] <= LEVEL_MEMORY_SECONDS:
            return remembered[0]
        state = self.hass.states.get(entity_id)
        if state is None or state.state != "on":
            return None
        brightness = state.attributes.get("brightness")
        if isinstance(brightness, (int, float)) and not isinstance(brightness, bool):
            return max(1, round(brightness / 255 * 100))
        return 100

    async def _lights(self, area_id: str, service: str, data_by_entity: dict[str, dict[str, Any]],
                      context: Context, switch: SwitchConfig, event: ButtonEvent, reason: str) -> dict[str, str]:
        """Send a light command and register the manual override for what was switched."""
        if not data_by_entity:
            return {}
        errors = await self.ctrl.async_switch_lights(area_id, service, data_by_entity, context, switch_id=switch.id)
        sent = [e for e in data_by_entity if e not in errors]
        expected = self._sent.setdefault((area_id, switch.id), {})
        for check in light_checks(service, {e: data_by_entity[e] for e in sent}):
            expected[check.entity_id] = check  # the last command per light (hold-to-dim sends several)
        now = time.monotonic()
        for entity_id in sent:
            data = data_by_entity[entity_id]
            if service == "turn_off":
                self._levels.pop(entity_id, None)
            elif "brightness_pct" in data:
                self._levels[entity_id] = (int(data["brightness_pct"]), now)
        if sent:
            self.ctrl.register_switch_override(
                area_id, sent, "off" if service == "turn_off" else "on", switch=switch, raw=event.raw, reason=reason)
        return errors

    # -------------------------------------------------------------- actions
    async def _execute(self, event: ButtonEvent, switch: SwitchConfig, mapping: ButtonMapping,
                       context: Context) -> _Result:
        area_id = event.area_id
        assert_switch_control_permitted(self.ctrl.switch_active(area_id, switch.id), area_id, switch.id)
        action = mapping.action
        _LOGGER.debug("switch_action_dispatched area=%s switch=%s action=%s mapping=%s", area_id, switch.id,
                      event.raw, mapping.describe())
        if action in LIGHT_ACTIONS:
            online, offline = self._targets(area_id, mapping)
            if not online:
                return _Result("No light to control" + (f" ({_plural(len(offline))} offline)" if offline else ""), False)
            note = f" · {_plural(len(offline))} offline" if offline else ""
            if action == ButtonAction.TOGGLE:
                any_on = any(self.hass.states.is_state(e, "on") for e in online)
                action = ButtonAction.OFF if any_on else ButtonAction.ON
            if action == ButtonAction.ON:
                return await self._on(area_id, online, switch, mapping, event, context, note)
            if action == ButtonAction.OFF:
                errors = await self._lights(area_id, "turn_off", {e: {} for e in online}, context, switch, event,
                                            "Physical switch OFF requested")
                return self._light_result("switched off", online, errors, note, "light.turn_off")
            if action in (ButtonAction.BRIGHTNESS_UP, ButtonAction.BRIGHTNESS_DOWN):
                direction = 1 if action == ButtonAction.BRIGHTNESS_UP else -1
                return await self._step(area_id, online, switch, direction, switch.dimming.step, event, context, note)
            if action == ButtonAction.SET_BRIGHTNESS:
                value = min(max(mapping.brightness or 100, switch.dimming.min_brightness), switch.dimming.max_brightness)
                data = {e: ({"brightness_pct": value} if light_capabilities(self.hass, e).brightness else {})
                        for e in online}
                errors = await self._lights(area_id, "turn_on", data, context, switch, event,
                                            f"Physical switch set brightness {value} %")
                return self._light_result(f"set to {value} %", online, errors, note, f"light.turn_on {value} %")
            if action == ButtonAction.SET_COLOR_TEMP:
                kelvin = mapping.color_temp or 3000
                data = {}
                for e in online:
                    caps = light_capabilities(self.hass, e)
                    if caps.color_temp:
                        low, high = caps.min_kelvin or kelvin, caps.max_kelvin or kelvin
                        data[e] = {"color_temp_kelvin": min(max(kelvin, low), high)}
                    elif caps.color:
                        data[e] = {"color_temp_kelvin": kelvin}
                if not data:
                    return _Result("No light supports colour temperature", False)
                errors = await self._lights(area_id, "turn_on", data, context, switch, event,
                                            f"Physical switch set {kelvin} K")
                skipped = len(online) - len(data)
                return self._light_result(f"set to {kelvin} K", list(data), errors,
                                          note + (f" · {skipped} without colour temperature" if skipped else ""),
                                          f"light.turn_on {kelvin} K")
            return _Result(f"{action.value} is not supported here", False)
        if action in (ButtonAction.MOOD, ButtonAction.MOOD_AUTO):
            return await self._mood(area_id, mapping, context)
        if action in (ButtonAction.SCENE, ButtonAction.SCRIPT, ButtonAction.WLED_PRESET):
            return await self._entity_action(mapping, context)
        return _Result(IGNORED_WORDS["not_mapped"], None)

    def _light_result(self, verb: str, entities: list[str], errors: dict[str, str], note: str,
                      command: str) -> _Result:
        ok = len(entities) - len(errors)
        if errors and not ok:
            return _Result(f"Failed: {next(iter(errors.values()))}", False, command)
        names = self._names(entities)
        text = f"{names} {verb}" + (f" · {len(errors)} failed" if errors else "") + note
        return _Result(text, not errors, command, override=ok > 0, partial=bool(errors))

    def _names(self, entities: list[str]) -> str:
        if len(entities) == 1:
            state = self.hass.states.get(entities[0])
            return (state.attributes.get("friendly_name") if state else None) or entities[0]
        return f"Room lights ({_plural(len(entities))})"

    async def _on(self, area_id: str, online: list[str], switch: SwitchConfig, mapping: ButtonMapping,
                  event: ButtonEvent, context: Context, note: str) -> _Result:
        data: dict[str, dict[str, Any]] = {}
        how = ""
        if mapping.on_values == OnValues.FIXED:
            for e in online:
                caps = light_capabilities(self.hass, e)
                values: dict[str, Any] = {}
                if mapping.brightness and caps.brightness:
                    values["brightness_pct"] = mapping.brightness
                if mapping.color_temp and caps.color_temp:
                    low, high = caps.min_kelvin or mapping.color_temp, caps.max_kelvin or mapping.color_temp
                    values["color_temp_kelvin"] = min(max(mapping.color_temp, low), high)
                data[e] = values
            how = " (fixed values)"
        elif mapping.on_values == OnValues.MOOD:
            mood, _source = self.ctrl.room_mood(area_id)
            if mood:
                for e in online:
                    resolved = self.ctrl.resolve(mood, area_id, e, self.ctrl.index.profile_targets(e))
                    service, values, _req, _notes = build_command(resolved, light_capabilities(self.hass, e))
                    if service == "turn_on":
                        data[e] = values
                how = f" ({mood})" if data else ""
            if not data:
                # No mood, Manual, or the mood switches none of them on: the person still wants light.
                data = {e: {} for e in online}
                how = " (last values)" if not mood else f" (last values; {mood} switches none of them on)"
        else:
            data = {e: {} for e in online}
            how = " (last values)"
        errors = await self._lights(area_id, "turn_on", data, context, switch, event, "Physical switch ON requested")
        left_off = len(online) - len(data)
        return self._light_result(f"switched on{how}", list(data), errors,
                                  note + (f" · {left_off} left off by the mood" if left_off else ""), "light.turn_on")

    async def _step(self, area_id: str, online: list[str], switch: SwitchConfig, direction: int, step: int,
                    event: ButtonEvent, context: Context, note: str) -> _Result:
        cfg = switch.dimming
        data: dict[str, dict[str, Any]] = {}
        for e in online:
            if not light_capabilities(self.hass, e).brightness:
                continue
            current = self._level(e)
            if current is None:
                if direction < 0:
                    continue  # an off light is not dimmed down (and never switched on by "down")
                new = max(cfg.min_brightness, min(step, cfg.max_brightness))
            else:
                new = min(max(current + direction * step, cfg.min_brightness), cfg.max_brightness)
                if new == current:
                    continue
            data[e] = {"brightness_pct": new}
        if not data:
            return _Result("Already at the " + ("maximum" if direction > 0 else "minimum") + " brightness" + note, True,
                           unchanged=True)
        errors = await self._lights(area_id, "turn_on", data, context, switch, event,
                                    f"Physical switch brightness {'up' if direction > 0 else 'down'}")
        values = sorted({d["brightness_pct"] for d in data.values()})
        return self._light_result(f"brightness {'/'.join(f'{v} %' for v in values)}", list(data), errors, note,
                                  f"light.turn_on brightness {'+' if direction > 0 else '−'}{step} %")

    async def _dim(self, key: Key, event: ButtonEvent, switch: SwitchConfig, mapping: ButtonMapping,
                   context: Context, run: _DimRun) -> None:
        """Hold-to-dim: one bounded command per interval until STOP, the limit or the safety timeout."""
        cfg = switch.dimming
        direction = 1 if mapping.action == ButtonAction.DIM_UP else -1
        area_id = event.area_id
        deadline = time.monotonic() + cfg.timeout
        why = "stopped"
        sent = 0
        failed: str | None = None
        final: list[int] = []
        try:
            async with self._locks.setdefault(key, asyncio.Lock()):
                self._sent.pop(key, None)
                online, _offline = self._targets(area_id, mapping)
                lights = [e for e in online if light_capabilities(self.hass, e).brightness]
                levels: dict[str, int | None] = {e: self._level(e) for e in lights}
                while not run.stopped and not self._stopped:
                    if time.monotonic() >= deadline:
                        why = f"no STOP within {cfg.timeout} s (safety timeout)"
                        break
                    data: dict[str, dict[str, Any]] = {}
                    for e, current in levels.items():
                        if current is None:
                            if direction < 0:
                                continue
                            new = cfg.min_brightness
                        else:
                            new = min(max(current + direction * cfg.hold_step, cfg.min_brightness),
                                      cfg.max_brightness)
                            if new == current:
                                continue
                        values: dict[str, Any] = {"brightness_pct": new}
                        if light_capabilities(self.hass, e).transition:
                            values["transition"] = cfg.interval
                        data[e] = values
                    if not data:
                        why = "reached the " + ("maximum" if direction > 0 else "minimum")
                        break
                    if not self.ctrl.switch_active(area_id, switch.id):
                        why = "the switch was set to Observing"
                        break
                    errors = await self._lights(area_id, "turn_on", data, context, switch, event,
                                                f"Physical switch dimming {'up' if direction > 0 else 'down'}")
                    for e, values in data.items():
                        if e not in errors:
                            levels[e] = values["brightness_pct"]
                    if errors:
                        failed = next(iter(errors.values()))
                        break
                    sent += 1
                    await self._tick(run, cfg.interval)
                final = sorted({v for v in levels.values() if v is not None})
        finally:
            if self._dims.get(key) is run:
                self._dims.pop(key, None)
        word = "up" if direction > 0 else "down"
        if failed:
            result = _Result(f"Dimming {word} failed: {failed}", False, "light.turn_on (dimming)", override=sent > 0)
        else:
            at = "/".join(f"{v} %" for v in final) if final else "—"
            result = _Result(f"Dimmed {word} to {at} ({why}; {_plural(sent, 'command')})", True,
                             f"light.turn_on brightness {'+' if direction > 0 else '−'}{cfg.hold_step} % every "
                             f"{cfg.interval:g} s", override=sent > 0)
        if not self._stopped:
            self._record(key, event, result)

    def _stop_dim(self, key: Key) -> bool:
        running = self._dims.pop(key, None)
        if running is None:
            return False
        running.stop()  # the loop ends after the command in flight (never cancelled mid-command)
        return True

    async def _tick(self, run: _DimRun, seconds: float) -> None:
        """Wait one hold-to-dim interval (Home Assistant's scheduler) or until STOP arrives."""
        run.wake.clear()
        if run.stopped:
            return
        @callback
        def wake(_now: datetime) -> None:
            run.wake.set()

        cancel = async_call_later(self.hass, seconds, wake)
        try:
            await run.wake.wait()
        finally:
            cancel()

    # --------------------------------------------------------------- moods
    async def _mood(self, area_id: str, mapping: ButtonMapping, context: Context) -> _Result:
        ctrl = self.ctrl
        auto = mapping.action == ButtonAction.MOOD_AUTO
        if mapping.scope == "global":
            helper = ctrl.config.mood.manual_mood_entity
            state = self.hass.states.get(helper) if helper else None
            if state is None:
                return _Result("The mood selector (Manual Mood) is not available", False)
            wanted = ctrl.config.mood.auto_option if auto else (mapping.mood or "")
            option = next((o for o in state.attributes.get("options") or [] if str(o).casefold() == wanted.casefold()),
                          None)
            if option is None:
                return _Result(f"'{wanted}' is not an option of {helper}", False)
            domain = helper.split(".", 1)[0]
            if domain not in ("input_select", "select"):
                return _Result(f"{helper} is not a selector", False)
            if state.state == option:
                return _Result(f"Mood is already {option}", True, unchanged=True)
            await self._call(domain, "select_option", {"entity_id": helper, "option": option}, context)
            return _Result(f"Mood selector set to {option} (all rooms follow through Main Mood)", True,
                           f"{domain}.select_option {option}", checks=[Check(helper, option)])
        room = ctrl.lighting.room(area_id)
        rt = ctrl.runtime_for(area_id)
        if auto:
            rt.temp_mood = None
            self._cancel_mood_timer(area_id)
            if room.mood_override is None:
                return _Result("The room already follows the automatic mood", True, unchanged=True)
            ctrl.store.async_set(ctrl.with_room(area_id, mood_override=None))
            return _Result("Room mood returned to Auto (follows the global mood)", True, "room mood: Auto",
                           internal=True)
        mood = next((m["name"] for m in ctrl.discovered_moods() if m["name"].casefold() == (mapping.mood or "").casefold()),
                    None)
        if mood is None:
            return _Result(f"Mood '{mapping.mood}' does not exist", False)
        if mapping.minutes:
            until = dt_util.utcnow() + timedelta(minutes=mapping.minutes)
            rt.temp_mood = {"mood": mood, "until": until.isoformat(timespec="seconds")}
            self._schedule_mood_expiry(area_id)
        else:
            rt.temp_mood = None
            self._cancel_mood_timer(area_id)
        ctrl.mark_dirty()
        if room.mood_override != mood:
            ctrl.store.async_set(ctrl.with_room(area_id, mood_override=mood))
        suffix = f" for {mapping.minutes} min" if mapping.minutes else ""
        return _Result(f"Room mood set to {mood}{suffix}", True, f"room mood: {mood}", internal=True)

    def _cancel_mood_timer(self, area_id: str) -> None:
        if cancel := self._mood_timers.pop(area_id, None):
            cancel()

    def _schedule_mood_expiry(self, area_id: str) -> None:
        self._cancel_mood_timer(area_id)
        rt = self.ctrl.runtime_for(area_id)
        until = dt_util.parse_datetime((rt.temp_mood or {}).get("until") or "")
        if until is None or self._stopped:
            rt.temp_mood = None
            return

        @callback
        def expire(_now: datetime) -> None:
            self._mood_timers.pop(area_id, None)
            temp = rt.temp_mood
            rt.temp_mood = None
            self.ctrl.mark_dirty()
            room = self.ctrl.lighting.room(area_id)
            if temp and room.mood_override and room.mood_override.casefold() == str(temp.get("mood")).casefold():
                _LOGGER.info("switch_room_mood_expired area=%s mood=%s", area_id, temp.get("mood"))
                self.ctrl.store.async_set(self.ctrl.with_room(area_id, mood_override=None))

        self._mood_timers[area_id] = async_track_point_in_utc_time(self.hass, expire, max(until, dt_util.utcnow()))

    # ------------------------------------------------------ scenes, scripts
    async def _entity_action(self, mapping: ButtonMapping, context: Context) -> _Result:
        entity = mapping.entity or ""
        state = self.hass.states.get(entity)
        if state is None:
            return _Result(f"{entity or 'The entity'} does not exist", False)
        if mapping.action == ButtonAction.SCENE and entity.startswith("scene."):
            await self._call("scene", "turn_on", {"entity_id": entity}, context)
            return _Result(f"Scene {state.name} activated", True, f"scene.turn_on {entity}")
        if mapping.action == ButtonAction.SCRIPT and entity.startswith("script."):
            await self._call("script", "turn_on", {"entity_id": entity}, context)
            return _Result(f"Script {state.name} started", True, f"script.turn_on {entity}")
        if mapping.action == ButtonAction.WLED_PRESET and entity.startswith("select."):
            option = mapping.option or ""
            if option not in (state.attributes.get("options") or []):
                return _Result(f"WLED preset '{option}' does not exist", False)
            await self._call("select", "select_option", {"entity_id": entity, "option": option}, context)
            return _Result(f"WLED preset {option} activated", True, f"select.select_option {option}",
                           checks=[Check(entity, option)])
        return _Result(f"{entity} cannot be used for {mapping.action.value}", False)

    async def _call(self, domain: str, service: str, data: dict[str, Any], context: Context) -> None:
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
                await self.hass.services.async_call(domain, service, data, blocking=True, context=context)
        except TimeoutError as err:
            raise HomeAssistantError("no response within timeout") from err
        _LOGGER.debug("switch_command service=%s.%s data=%s", domain, service, json.dumps(data))

