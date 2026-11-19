"""Curtain runtime (0.6.1, automatic 0.6.2, environment 0.6.3): store, presets, verification and observation.

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

Phase A5 (the Smart Lighting App): the manager can be *fenced* (no command
leaves it; an operation that would start is prevented and reported) and
*dormant* (stopped, keeping its configuration for read access while the App
commands the covers; its store takes only the App's published copy). Optional
observers see every command sent, every automatic decision, every configuration
change and every state save, so the App's shadow runtime can compare itself
with this manager and the owner can mirror its state. Without a bridge session
none of this changes anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable, Mapping
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
# 0.6.2: automatic rule settings (defaults, rooms, window auto); 0.6.3: environment / thermal settings, the
# Glare and Excess heat mappings and the overhang limit. Older data loads as it is (new keys are defaults).
# 0.6.4: Global Modes (modes, room_modes), cover presets per layer, cover overrides and explicit No action.
STORE_MINOR_VERSION = 4   # 0.6.5 adds settings keys only (defaults reproduce 0.6.4; nothing to migrate)
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
        # Phase A5: ownership (curtain_bridge.py). Fenced: no cover command leaves this manager. Dormant: stopped
        # and kept for read access while the Smart Lighting App commands the covers. Locked: the configuration
        # store takes only the App's published copy (async_set_mirrored).
        self.fenced = False
        self.dormant = False
        self.locked = False
        self.command_observer: Callable[[dict[str, Any]], None] | None = None
        self.decision_observer: Callable[[str, dict[str, Any]], None] | None = None
        self.config_observer: Callable[[CurtainConfig], None] | None = None
        self.state_observer: Callable[[], None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()
        self.auto = AutoCurtains(self, occupancy)

    # -------------------------------------------------------------- storage
    async def async_load(self) -> None:
        """Load the configuration and the last results. Never moves a cover (also not after a restart)."""
        data = await self._store.async_load()
        self.config = CurtainConfig.from_dict(data) if data else CurtainConfig()
        if not self.config.centralized:
            from dataclasses import replace

            from .migration import plan

            if not plan(self.config):
                # 0.6.6: nothing is configured outside Global Schedule / Global Modes, so centralizing changes
                # nothing; otherwise the old settings stay active until the reviewed migration is applied.
                self.config = replace(self.config, centralized=True)
                self._dirty = True
                self._store.async_delay_save(self._config_data, SAVE_DELAY_SECONDS)
        state = await self._state_store.async_load() or {}
        if not isinstance(state, Mapping):
            _LOGGER.warning("curtains_state_invalid: the stored runtime state is not an object; starting empty")
            state = {}
        windows = self.config.windows
        # Every section is restored on its own: a damaged section starts empty (with a warning) instead of keeping
        # the curtains from loading at all.
        self.last = self._section(state, "last", lambda v: {k: x for k, x in v.items() if k in windows
                                                             and isinstance(x, dict)}, {})
        self.history = self._section(state, "history", lambda v: {
            k: [x for x in items if isinstance(x, dict)][:HISTORY_LENGTH] for k, items in v.items()
            if k in windows and isinstance(items, list)}, {})
        self.manual = self._section(state, "manual", lambda v: {k: x for k, x in v.items() if k in windows
                                                                 and isinstance(x, dict)}, {})
        self.samples = self._section(state, "samples", lambda v: [
            (float(a), float(b)) for a, b in v if isinstance(a, (int, float)) and isinstance(b, (int, float))], [])
        for name, restore in (("auto", self.auto.restore), ("sensors", self.auto.restore_sensors),
                              ("house", self.auto.restore_house)):
            try:
                restore(state.get(name))
            except (TypeError, ValueError, KeyError, AttributeError) as err:
                _LOGGER.warning("curtains_state_section_invalid section=%s error=%s; starting it empty", name, err)
                restore(None)
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
    def _section(state: Mapping[str, Any], name: str, parse: Callable[[Any], Any], empty: Any) -> Any:
        value = state.get(name)
        if value is None:
            return empty
        try:
            return parse(value)
        except (TypeError, ValueError, KeyError, AttributeError) as err:
            _LOGGER.warning("curtains_state_section_invalid section=%s error=%s; starting it empty", name, err)
            return empty

    @callback
    def assume_moving(self, entity_ids: Iterable[str]) -> None:
        """Phase A5: after control changed owner, a movement the previous owner started may not be recorded: every
        report of these covers is ours for the verification and settle window (never a manual change)."""
        until = _now() + timedelta(seconds=self.config.settings.verify_timeout + SETTLE_SECONDS)
        for entity_id in entity_ids:
            for e in members(self.hass, entity_id) | {entity_id}:
                self._ours_until[e] = max(self._ours_until.get(e, until), until)

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
                "samples": [list(s) for s in self.samples], "auto": self.auto.export(),
                "sensors": self.auto.export_sensors(), "house": self.auto.export_house()}

    def save_state(self) -> None:
        self._save_state()

    def _save_state(self) -> None:
        self._state_dirty = True
        if self.dormant:
            return  # a dormant manager never saves: the store holds the App's published copy
        self._state_store.async_delay_save(self._state_data, SAVE_DELAY_SECONDS)
        if self.state_observer is not None:
            try:
                self.state_observer()
            except Exception:  # noqa: BLE001 - observation never affects curtains
                _LOGGER.debug("curtain_state_observer_failed", exc_info=True)

    def export_state(self) -> dict[str, Any]:
        """The runtime state as the state store holds it (an exact copy for the App; not a save)."""
        dirty = self._state_dirty
        try:
            return self._state_data()
        finally:
            self._state_dirty = dirty

    async def async_import_state(self, state: Mapping[str, Any]) -> None:
        """Replace the stored runtime state (before a load; used when control changes owner)."""
        if self._started and not self.dormant:
            raise RuntimeError("the runtime state of a running curtain manager cannot be replaced")
        await self._state_store.async_save(dict(state))
        self._state_dirty = False

    async def async_drain(self, timeout: float) -> bool:
        """Wait until no operation is running (no cover of a window is being moved). False on timeout."""
        try:
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        except TimeoutError:
            return False
        return not self._running

    @callback
    def async_set_mirrored(self, config: CurtainConfig) -> bool:
        """The App's published configuration (only while dormant / locked): stored, nothing evaluated."""
        if not self.locked:
            raise RuntimeError("a mirrored configuration is only accepted by a locked (dormant) curtain manager")
        if config == self.config:
            return False
        self.config = config
        self.revision += 1
        self._dirty = True
        self._store.async_delay_save(self._config_data, SAVE_DELAY_SECONDS)
        self._automations = None
        self.notify()
        return True

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
        if self.locked:
            raise CurtainConfigError("managed_by_app", "Curtains are managed by the Smart Lighting App: change them "
                                     "there. This editor is read-only until control is returned to the integration.")
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
        if self.config_observer is not None:
            try:
                self.config_observer(config)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("curtain_config_observer_failed", exc_info=True)
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
        self._idle.set()
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
        return self.plan(window, preset)

    def plan(self, window: Window, preset) -> dict[str, Any]:
        """What sending these targets (a preset, or a resolved per-cover target) would do now. Never sends."""
        window_id = window.window_id
        plan = build_plan(self.hass, window, preset, self.config.settings.tolerance, self._busy)
        return {"window_id": window_id, "preset_id": preset.preset_id, "preset_name": preset.name,
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
        if self.fenced:
            raise CurtainConfigError("busy", "Curtain control is changing owner; try again in a moment")
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
        self._idle.clear()
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
        await asyncio.gather(*(self._send(item, context, op) for item in moves))
        if window_id in self._running and self._running[window_id] is op:
            self._verify(op)
        if op["status"] == "running":
            self._timers[op["op_id"]] = async_call_later(self.hass, self.config.settings.verify_timeout,
                                                         callback(lambda _now: self._timeout(op)))

    async def _send(self, item: dict[str, Any], context: Context, op: Mapping[str, Any] | None = None) -> None:
        data: dict[str, Any] = {"entity_id": item["entity_id"]}
        if item["service"] == "set_cover_position":
            data["position"] = item["target"]
        if self.fenced:
            # Control is changing owner: nothing leaves this manager (the next owner re-evaluates the window).
            item["status"] = "failed"
            item["detail"] = "Not sent: curtain control was changing owner"
            self._release(item)
            return
        if self.command_observer is not None:
            try:
                self.command_observer({"domain": "cover", "service": item["service"], "data": dict(data),
                                       "context_id": context.id, "entity_id": item["entity_id"],
                                       "target": item["target"], "layer_id": item["layer_id"],
                                       "window_id": op.get("window_id") if op else None,
                                       "purpose": op.get("origin") if op else None,
                                       "at": _iso(_now())})
            except Exception:  # noqa: BLE001
                _LOGGER.debug("curtain_command_observer_failed", exc_info=True)
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
        if not self._running:
            self._idle.set()
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
            "fenced": self.fenced,
            "dormant": self.dormant,
        }
