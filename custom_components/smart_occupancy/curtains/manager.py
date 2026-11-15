"""Curtain runtime (0.6.1, automatic 0.6.2): configuration store, presets, verification and observation.

Cover commands are sent only by an operation created with ``begin``: for an
explicit, confirmed manual preset (``async_apply``) or for a window in
Automatic (``auto.py``), and only after ``assert_curtain_command_permitted``
accepted it. Loading, starting, migrating, discovering, editing, previewing and
stopping never send anything.

Applying a preset is one coordinated operation for the whole window: the
commands for its layers are sent together (one service call per cover), then
every cover's reported position is followed until it is at its target, the
verification timeout passes or the cover becomes unavailable. Each cover's
result is recorded (reached, already there, left unchanged, failed, timed
out, unavailable, unsupported), and so is the operation's (succeeded, partly
succeeded, timed out, failed). A cover in a running operation is busy: a
second preset for it is refused instead of sending a contradictory command.

A position change that Smart Occupancy did not command (wall panel, device
button, another automation, a person in Home Assistant) is recorded as the
window's manual change and becomes a manual hold for the automatic rules.
Our own commands are recognised by the covers being busy in an operation, plus
a settle window for late device reports, so they are never mistaken for
manual changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime, timedelta
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import CALLBACK_TYPE, Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..occupancy import friendly_name
from ..supervision import CurtainOrigin, assert_curtain_command_permitted
from . import conflicts as cover_conflicts
from .auto import AutoCurtains
from .config import CurtainConfig, CurtainConfigError, Window, new_id, valid_position
from .discovery import MOVING_STATES, available, capabilities, current_position, members
from .executor import PENDING, at_target, build_plan, explain, overall
from .solar import SunNow, add_sample, read_source

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "smart_occupancy.curtains"
STORE_VERSION = 1
STORE_MINOR_VERSION = 2   # 0.6.2: automatic rule settings (defaults, rooms, window auto); older data loads as is
STATE_STORE_KEY = "smart_occupancy.curtains_state"
STATE_STORE_VERSION = 1
SIGNAL_CURTAINS_UPDATED = "smart_occupancy_curtains_updated"

SAVE_DELAY_SECONDS = 2.0
SERVICE_CALL_TIMEOUT = 15.0     # seconds for Home Assistant to accept one cover command
SETTLE_SECONDS = 90.0           # late reports after an operation are still ours, not a manual change
MANUAL_MERGE_SECONDS = 120.0    # one manual movement reported in steps is one manual change
HISTORY_LENGTH = 10


def _now() -> datetime:
    return dt_util.utcnow()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class CurtainManager:
    def __init__(self, hass: HomeAssistant, occupancy: Any | None = None) -> None:
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY, minor_version=STORE_MINOR_VERSION)
        self._state_store: Store[dict[str, Any]] = Store(hass, STATE_STORE_VERSION, STATE_STORE_KEY)
        self.config = CurtainConfig()
        self.revision = 0
        self.last: dict[str, dict[str, Any]] = {}        # window -> last operation
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.manual: dict[str, dict[str, Any]] = {}      # window -> last manual change
        self.samples: list[tuple[float, float]] = []     # (azimuth, sun_angle) for the calibration
        self._running: dict[str, dict[str, Any]] = {}    # window -> running operation (same dict as last[window])
        self._busy: dict[str, str] = {}                  # physical cover -> operation id
        self._ours_until: dict[str, datetime] = {}       # cover -> end of its settle window
        self._timers: dict[str, CALLBACK_TYPE] = {}
        self._unsubs: list[CALLBACK_TYPE] = []
        self._tracking: CALLBACK_TYPE | None = None
        self._automations: list[cover_conflicts.CoverAutomation] | None = None
        self._dirty = False
        self._state_dirty = False
        self._started = False
        self.auto = AutoCurtains(self, occupancy)

    # -------------------------------------------------------------- storage
    async def async_load(self) -> None:
        """Load the configuration and the last results. Never moves a cover (also not after a restart)."""
        data = await self._store.async_load()
        self.config = CurtainConfig.from_dict(data) if data else CurtainConfig()
        state = await self._state_store.async_load() or {}
        windows = self.config.windows
        self.last = {k: v for k, v in (state.get("last") or {}).items() if k in windows and isinstance(v, dict)}
        self.history = {k: [x for x in v if isinstance(x, dict)][:HISTORY_LENGTH]
                        for k, v in (state.get("history") or {}).items() if k in windows and isinstance(v, list)}
        self.manual = {k: v for k, v in (state.get("manual") or {}).items() if k in windows and isinstance(v, dict)}
        self.samples = [(float(a), float(b)) for a, b in state.get("samples") or ()
                        if isinstance(a, (int, float)) and isinstance(b, (int, float))]
        self.auto.restore(state.get("auto"))
        # An operation that was running when Home Assistant stopped is not resumed and never re-sent. Its
        # covers may still be travelling and report the rest of that movement: those reports are ours, not
        # a manual change.
        for op in self.last.values():
            if op.get("status") == "running":
                until = _now() + timedelta(seconds=self.config.settings.verify_timeout + SETTLE_SECONDS)
                for item in op.get("items", ()):
                    if item.get("status") in PENDING and isinstance(item.get("entity_id"), str):
                        for e in members(self.hass, item["entity_id"]) | {item["entity_id"]}:
                            self._ours_until[e] = until
            self._interrupt(op)
        for ops in self.history.values():
            for op in ops:
                self._interrupt(op)

    @staticmethod
    def _interrupt(op: dict[str, Any]) -> None:
        if op.get("status") != "running":
            return
        for item in op.get("items", ()):
            if item.get("status") in PENDING:
                item["status"] = "interrupted"
                item["detail"] = "Home Assistant stopped before the position was confirmed"
        op["status"] = "interrupted"
        op["explanation"] = explain("interrupted", op.get("items", []), op.get("preset_name", "Preset"))

    def _config_data(self) -> dict[str, Any]:
        self._dirty = False
        return self.config.as_dict()

    def _state_data(self) -> dict[str, Any]:
        self._state_dirty = False
        return {"last": self.last, "history": self.history, "manual": self.manual,
                "samples": [list(s) for s in self.samples], "auto": self.auto.export()}

    def save_state(self) -> None:
        self._save_state()

    def _save_state(self) -> None:
        self._state_dirty = True
        self._state_store.async_delay_save(self._state_data, SAVE_DELAY_SECONDS)

    async def async_flush(self) -> None:
        if self._dirty:
            await self._store.async_save(self._config_data())
        if self._state_dirty:
            await self._state_store.async_save(self._state_data())

    async def async_remove(self) -> None:
        await self._store.async_remove()
        await self._state_store.async_remove()

    @callback
    def async_set(self, config: CurtainConfig) -> bool:
        """Replace the configuration (saved shortly after). Never moves a cover."""
        if config == self.config:
            return False
        self.config = config
        self.revision += 1
        self._dirty = True
        self._store.async_delay_save(self._config_data, SAVE_DELAY_SECONDS)
        for window_id in [w for w in {*self.last, *self.auto.runtimes} if w not in config.windows]:
            self.last.pop(window_id, None)
            self.history.pop(window_id, None)
            self.manual.pop(window_id, None)
            self.auto.forget(window_id)
            self._save_state()
        if self._started:
            self._track()
        self._automations = None
        self.auto.reconfigure()
        self.notify()
        return True

    @callback
    def rename_entity(self, old: str, new: str) -> bool:
        """Follow an entity id rename: the layer keeps its id, so every preset keeps its values."""
        from dataclasses import replace

        windows = {}
        changed = False
        for window_id, window in self.config.windows.items():
            if old in window.entity_ids:
                window = replace(window, layers=tuple(replace(x, entity_id=new) if x.entity_id == old else x
                                                      for x in window.layers))
                changed = True
            windows[window_id] = window
        if changed:
            _LOGGER.info("entity_renamed old=%s new=%s (curtain configuration updated)", old, new)
            self.async_set(replace(self.config, windows=windows))
        return changed

    # ------------------------------------------------------------ lifecycle
    @callback
    def async_start(self) -> None:
        """Follow the covers and the sun for display, the manual-change record and the sun-angle calibration."""
        self._started = True
        self._track()
        for event_type in ("automation_reloaded", "script_reloaded", er.EVENT_ENTITY_REGISTRY_UPDATED):
            self._unsubs.append(self.hass.bus.async_listen(event_type, self._on_automations_changed))
        self.auto.start()

    @callback
    def async_stop(self) -> None:
        self._started = False
        self.auto.stop()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._tracking:
            self._tracking()
            self._tracking = None
        for window_id in list(self._running):
            op = self._running.pop(window_id)
            self._interrupt(op)
            self._save_state()
        for cancel in self._timers.values():
            cancel()
        self._timers.clear()
        self._busy.clear()

    def _tracked(self) -> list[str]:
        ids: set[str] = set()
        for window in self.config.windows.values():
            for entity_id in window.entity_ids:
                ids |= members(self.hass, entity_id) | {entity_id}
        s = self.config.settings
        ids |= {e for e in (s.azimuth_entity, s.elevation_entity, s.sun_angle_entity) if e}
        return sorted(ids)

    @callback
    def _track(self) -> None:
        if self._tracking:
            self._tracking()
            self._tracking = None
        if ids := self._tracked():
            self._tracking = async_track_state_change_event(self.hass, ids, self._on_state)

    @callback
    def _on_automations_changed(self, _event: Event) -> None:
        self._automations = None
        self.notify()

    @callback
    def notify(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_CURTAINS_UPDATED)

    @callback
    def _on_state(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        s = self.config.settings
        if entity_id in (s.azimuth_entity, s.sun_angle_entity):
            self._sample()
        if entity_id.startswith("cover."):
            self._cover_changed(event)
            if window := self._window_of(entity_id):
                self.auto.on_cover(window.window_id)
        if entity_id in (s.azimuth_entity, s.elevation_entity):
            self.auto.on_sun()
        self.notify()

    def sun(self) -> SunNow:
        s = self.config.settings
        return SunNow(read_source(self.hass, s.azimuth_entity, "azimuth"),
                      read_source(self.hass, s.elevation_entity, "elevation"),
                      read_source(self.hass, s.sun_angle_entity, "sun_angle"))

    def _sample(self) -> None:
        sun = self.sun()
        if sun.azimuth is not None and sun.sun_angle is not None and add_sample(self.samples, sun.azimuth,
                                                                               sun.sun_angle):
            self._save_state()

    @callback
    def reset_calibration(self) -> None:
        self.samples.clear()
        self._save_state()
        self.notify()

    # ---------------------------------------------------- manual changes
    def _window_of(self, entity_id: str) -> Window | None:
        for window in self.config.windows.values():
            for layer_entity in window.entity_ids:
                if entity_id == layer_entity or entity_id in members(self.hass, layer_entity):
                    return window
        return None

    def _ours(self, entity_id: str) -> bool:
        ids = {entity_id} | members(self.hass, entity_id)
        if window := self._window_of(entity_id):
            ids |= set(window.entity_ids)  # a group's report and its members' reports belong together
        now = _now()
        return any(e in self._busy or (until := self._ours_until.get(e)) is not None and until > now for e in ids)

    def _cover_changed(self, event: Event) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or not available(old) or not available(new):
            return  # appearing, disappearing, restarts and reconnects are not movements
        entity_id = event.data["entity_id"]
        if window := self._window_of(entity_id):
            if (op := self._running.get(window.window_id)) is not None:
                self._verify(op)
            moved = current_position(old) != current_position(new) or (
                new.state in MOVING_STATES and old.state not in MOVING_STATES)
            if not moved or self._ours(entity_id):
                return
            now = _now()
            record = self.manual.get(window.window_id)
            by_person = new.context.user_id is not None
            if record and record.get("entity_id") == entity_id and (
                    at := dt_util.parse_datetime(record.get("last") or "")) and (now - at).total_seconds() < MANUAL_MERGE_SECONDS:
                record.update(to=current_position(new), last=_iso(now))
            else:
                record = self.manual[window.window_id] = {
                    "at": _iso(now), "last": _iso(now), "entity_id": entity_id,
                    "name": friendly_name(self.hass, entity_id) or entity_id,
                    "from": current_position(old), "to": current_position(new),
                    "source": "person" if by_person else "outside",
                }
            self._save_state()
            self.auto.on_manual(window, record)

    @callback
    def clear_manual(self, window_id: str) -> None:
        if self.manual.pop(window_id, None) is not None:
            self._save_state()
            self.notify()

    # ------------------------------------------------------- operations
    def busy_covers(self) -> Mapping[str, str]:
        return dict(self._busy)

    def preview(self, window_id: str, preset_id: str) -> dict[str, Any]:
        """What applying a preset would do now. Never sends anything."""
        window, preset = self._window_preset(window_id, preset_id)
        plan = build_plan(self.hass, window, preset, self.config.settings.tolerance, self._busy)
        return {"window_id": window_id, "preset_id": preset_id, "preset_name": preset.name,
                "positions": dict(preset.positions), "mode": window.mode,
                "items": [i.as_dict() for i in plan],
                "moves": sum(1 for i in plan if i.decision == "move"),
                "running": window_id in self._running}

    def _window_preset(self, window_id: str, preset_id: str):
        window = self.config.windows.get(window_id)
        if window is None:
            raise CurtainConfigError("not_found", "This window does not exist")
        preset = window.preset(preset_id)
        if preset is None:
            raise CurtainConfigError("not_found", "This preset does not exist")
        return window, preset

    async def async_apply(self, window_id: str, preset_id: str, *, origin: CurtainOrigin, confirmed: bool,
                          expected: Mapping[str, Any] | None, user_id: str | None = None) -> dict[str, Any]:
        """Apply a preset after an explicit confirmation. Returns once the commands are sent.

        In Observing and Automatic the applied preset becomes a manual hold, so automatic control does
        not undo the person's choice (see auto.py)."""
        window, preset = self._window_preset(window_id, preset_id)
        assert_curtain_command_permitted(origin, confirmed=confirmed, window_mode=window.mode, window_id=window_id)
        if expected is not None:
            confirmed_targets = {k: valid_position(v) for k, v in expected.items()}
            if confirmed_targets != dict(preset.positions):
                raise CurtainConfigError("preset_changed",
                                         "The preset changed after it was confirmed: review the targets and confirm again")
        if window_id in self._running:
            raise CurtainConfigError("busy", "A preset is still being applied to this window")
        self.manual.pop(window_id, None)  # the person chose this preset: a previous manual change is superseded
        op, moves, context = self.begin(window, preset, origin, user_id=user_id)
        if origin == CurtainOrigin.MANUAL_CONFIRMED:
            self.auto.on_manual_preset(window, preset, op)
        await self.async_run(op, moves, context)
        return op

    def begin(self, window: Window, preset, origin: CurtainOrigin, *, user_id: str | None = None,
              condition: str | None = None, reason: str | None = None):
        """Create a coordinated operation for every cover of a window (no await: callers can rely on the
        window being busy at once). The commands are sent by ``async_run``."""
        assert_curtain_command_permitted(origin, confirmed=origin == CurtainOrigin.MANUAL_CONFIRMED,
                                         window_mode=window.mode, window_id=window.window_id)
        window_id = window.window_id
        plan = build_plan(self.hass, window, preset, self.config.settings.tolerance, self._busy)
        op_id = new_id("op_")
        context = Context(user_id=user_id)
        started = _now()
        items: list[dict[str, Any]] = []
        for p in plan:
            status = {"move": "sent"}.get(p.decision, p.decision)
            items.append({"layer_id": p.layer_id, "entity_id": p.entity_id, "name": p.name, "kind": p.kind,
                          "target": p.target, "service": p.service, "status": status, "detail": p.reason,
                          "start_position": p.current, "final_position": p.current})
        op = {"op_id": op_id, "window_id": window_id, "preset_id": preset.preset_id, "preset_name": preset.name,
              "origin": origin.value, "user_id": user_id, "context_id": context.id, "started": _iso(started),
              "finished": None, "status": "running", "items": items, "explanation": "",
              "condition": condition, "reason": reason}
        moves = [i for i in items if i["status"] == "sent"]
        for item in moves:
            for e in members(self.hass, item["entity_id"]) | {item["entity_id"]}:
                self._busy[e] = op_id
        self._running[window_id] = op
        self.last[window_id] = op
        self.history[window_id] = [op, *self.history.get(window_id, [])][:HISTORY_LENGTH]
        op["explanation"] = explain("running", items, preset.name)
        self._save_state()
        _LOGGER.info("curtain_preset_apply window=%s preset=%s op=%s moves=%d origin=%s condition=%s", window_id,
                     preset.preset_id, op_id, len(moves), origin.value, condition)
        self.notify()
        return op, moves, context

    async def async_run(self, op: dict[str, Any], moves: list[dict[str, Any]], context: Context) -> None:
        window_id = op["window_id"]
        await asyncio.gather(*(self._send(item, context) for item in moves))
        if window_id in self._running and self._running[window_id] is op:
            self._verify(op)
        if op["status"] == "running":
            self._timers[op["op_id"]] = async_call_later(self.hass, self.config.settings.verify_timeout,
                                                         callback(lambda _now: self._timeout(op)))

    async def _send(self, item: dict[str, Any], context: Context) -> None:
        data: dict[str, Any] = {"entity_id": item["entity_id"]}
        if item["service"] == "set_cover_position":
            data["position"] = item["target"]
        try:
            async with asyncio.timeout(SERVICE_CALL_TIMEOUT):
                await self.hass.services.async_call("cover", item["service"], data, blocking=True, context=context)
        except TimeoutError:
            # It may still move: keep verifying, never send it again.
            if item["status"] in PENDING:
                item["detail"] = f"Home Assistant took longer than {SERVICE_CALL_TIMEOUT:.0f} s to accept the command"
            return
        except (HomeAssistantError, vol.Invalid, ValueError) as err:
            item["status"] = "failed"
            item["detail"] = f"cover.{item['service']} failed: {err}"
            self._release(item)
            _LOGGER.warning("curtain_command_failed entity=%s service=%s error=%s", item["entity_id"],
                            item["service"], err)
            return
        if item["status"] in PENDING:  # a device that reported during the call was already verified
            item["detail"] = f"cover.{item['service']} sent" + (f" ({item['target']} %)" if "position" in data else "")

    def _release(self, item: dict[str, Any]) -> None:
        until = _now() + timedelta(seconds=SETTLE_SECONDS)
        for e in members(self.hass, item["entity_id"]) | {item["entity_id"]}:
            self._busy.pop(e, None)
            self._ours_until[e] = until

    def _verify(self, op: dict[str, Any]) -> None:
        """Follow the reported positions of a running operation."""
        tolerance = self.config.settings.tolerance
        for item in op["items"]:
            if item["status"] not in PENDING:
                continue
            state = self.hass.states.get(item["entity_id"])
            position = current_position(state)
            if position is not None:
                item["final_position"] = position
            if state is not None and state.state in MOVING_STATES:
                item["status"] = "moving"
                item["detail"] = f"Moving ({position} %)" if position is not None else "Moving"
            elif at_target(state.state if state else None, position, item["target"], tolerance,
                           capabilities(state)["position"]):
                item["status"] = "reached"
                item["detail"] = f"Reported {position} % (target {item['target']} %)"
                self._release(item)
        if all(i["status"] not in PENDING for i in op["items"]):
            self._finish(op)
        else:
            op["explanation"] = explain("running", op["items"], op["preset_name"])

    def _timeout(self, op: dict[str, Any]) -> None:
        self._timers.pop(op["op_id"], None)
        if op["status"] != "running":
            return
        for item in op["items"]:
            if item["status"] not in PENDING:
                continue
            state = self.hass.states.get(item["entity_id"])
            if not available(state):
                item["status"] = "unavailable"
                item["detail"] = f"Became {state.state if state else 'missing'} before reaching {item['target']} %"
            else:
                item["status"] = "timeout"
                pos = current_position(state)
                item["detail"] = (f"Still at {pos} % after {self.config.settings.verify_timeout} s (target "
                                  f"{item['target']} %)" if pos is not None else
                                  f"No position reported within {self.config.settings.verify_timeout} s")
            self._release(item)
        self._finish(op)

    def _finish(self, op: dict[str, Any]) -> None:
        if (cancel := self._timers.pop(op["op_id"], None)) is not None:
            cancel()
        op["status"] = overall(op["items"])
        op["finished"] = _iso(_now())
        op["explanation"] = explain(op["status"], op["items"], op["preset_name"])
        self._running.pop(op["window_id"], None)
        for item in op["items"]:
            if self._busy.get(item["entity_id"]) == op["op_id"]:
                self._release(item)
        _LOGGER.info("curtain_preset_result window=%s preset=%s op=%s status=%s", op["window_id"],
                     op["preset_id"], op["op_id"], op["status"])
        self._save_state()
        self.auto.on_op_finished(op)
        self.notify()

    # ---------------------------------------------------------- conflicts
    def automations(self) -> list[cover_conflicts.CoverAutomation]:
        if self._automations is None:
            self._automations = cover_conflicts.collect(self.hass)
        return self._automations

    @callback
    def rescan(self) -> None:
        self._automations = None
        self.notify()

    def running(self, window_id: str) -> dict[str, Any] | None:
        return self._running.get(window_id)

    def conflicts(self, window: Window) -> dict[str, list[dict[str, Any]]]:
        return cover_conflicts.find(self.hass, window.entity_ids, self.automations())

    def blocking_automations(self, window: Window) -> list[dict[str, Any]]:
        """Enabled automations that may move this window's covers and that nobody explicitly allowed."""
        seen: dict[str, dict[str, Any]] = {}
        for found in self.conflicts(window).values():
            for c in found:
                if c["enabled"] and c["automation"] not in window.allowed_automations:
                    seen.setdefault(c["automation"], c)
        return list(seen.values())

    def diagnostics(self) -> dict[str, Any]:
        from .solar import calibrate

        return {
            "config": self.config.as_dict(),
            "last": self.last,
            "manual": self.manual,
            "running": sorted(self._running),
            "busy": dict(self._busy),
            "sun": self.sun().as_dict(),
            "sun_angle_calibration": calibrate(self.samples),
            "automatic": self.auto.diagnostics(),
        }
