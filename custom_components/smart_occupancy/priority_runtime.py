"""Priority Inputs at runtime (0.6.8): which inputs are active, and when their claims start and end.

Not a second executor. This module watches the input entities (one state
subscription), applies each input's entry debounce, release delay and
unavailable policy (one timer per input at most), and keeps the **claims**
the lighting controller's arbitration reads (``claims``). When a claim starts
or ends it asks the controller to apply the affected lights
(``LightingController.async_priority``), which builds the normal effective
plan - where an active claim is the winning rule of its lights - and sends
through the normal executor.

Start-up: nothing is replayed. Claims that were active when Home Assistant
stopped are kept (so no other rule takes the lights meanwhile) until the
inputs have settled (``SETTLE_SECONDS`` after Home Assistant started); then
every input is evaluated from its *current* state: still active - kept (no
command when the light is already on); inactive - released (the current rule
takes over); unavailable - its unavailable policy.

Every activation, release, unavailable / recovered input and blocked command
is kept in a short history per input (diagnostics and the card) and in the
room's decision log.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .priority import PriorityClaim, PriorityInput, Unavailable, claims_for

if TYPE_CHECKING:
    from .controller import LightingController

_LOGGER = logging.getLogger(__name__)

SETTLE_SECONDS = 10.0
HISTORY = 20

ENTRY = "entry"
RELEASE = "release"
GRACE = "grace"


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


@dataclass(slots=True)
class InputState:
    """The runtime state of one Priority Input."""

    reading: bool | None = None          # True active, False inactive, None unknown / unavailable
    active: bool = False                 # its claim is in force
    since: datetime | None = None        # when the claim started
    timer: CALLBACK_TYPE | None = None
    timer_kind: str | None = None        # entry | release | grace
    timer_due: datetime | None = None
    unavailable_since: datetime | None = None
    last_change: datetime | None = None
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=HISTORY))

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer()
        self.timer = self.timer_kind = self.timer_due = None


class PriorityInputs:
    """Owns the Priority Inputs' subscriptions, timers and claims."""

    def __init__(self, hass: HomeAssistant, ctrl: LightingController) -> None:
        self.hass = hass
        self.ctrl = ctrl
        self.states: dict[tuple[str, str], InputState] = {}
        # Lights a Priority Input switched on from off: (area, light) -> input id (switched off at release when
        # no automatic rule takes them over). Lights a person changed while claimed: (area, light).
        self.owned: dict[tuple[str, str], str] = {}
        self.touched: set[tuple[str, str]] = set()
        self._entities: dict[str, list[tuple[str, str]]] = {}
        self._unsub: CALLBACK_TYPE | None = None
        self._settle: CALLBACK_TYPE | None = None
        self._running = False
        self.settled = False

    # ------------------------------------------------------------ lifecycle
    def restore(self, data: Any) -> None:
        data = data if isinstance(data, dict) else {}
        for key, item in (data.get("inputs") or {}).items():
            if not isinstance(item, dict) or "/" not in key:
                continue
            area_id, input_id = key.split("/", 1)
            st = InputState(active=bool(item.get("active")),
                            since=dt_util.parse_datetime(item.get("since") or "") if item.get("active") else None)
            for h in item.get("history") or []:
                if isinstance(h, dict):
                    st.history.append(h)
            self.states[(area_id, input_id)] = st
        for key, input_id in (data.get("owned") or {}).items():
            if isinstance(input_id, str) and "/" in key:
                area_id, entity_id = key.split("/", 1)
                self.owned[(area_id, entity_id)] = input_id

    def persist(self) -> dict[str, Any]:
        return {
            "inputs": {f"{a}/{i}": {"active": st.active, "since": _iso(st.since), "history": list(st.history)}
                       for (a, i), st in self.states.items() if st.active or st.history},
            "owned": {f"{a}/{e}": i for (a, e), i in self.owned.items()},
        }

    @callback
    def async_start(self) -> None:
        self._running = True
        self._subscribe()

    @callback
    def async_started(self) -> None:
        """Home Assistant is running: the inputs are evaluated once they had time to settle."""
        if self._settle is None and not self.settled:
            self._settle = async_call_later(self.hass, SETTLE_SECONDS, self._on_settled)

    @callback
    def async_stop(self) -> None:
        self._running = False
        if self._unsub:
            self._unsub()
            self._unsub = None
        if self._settle:
            self._settle()
            self._settle = None
        for st in self.states.values():
            st.cancel()

    @callback
    def _on_settled(self, _now: datetime) -> None:
        self._settle = None
        self.settled = True
        if not self._running:
            return
        for (area_id, input_id) in list(self._configured()):
            self._evaluate(area_id, input_id, startup=True)
        # Restored claims of inputs that no longer exist (removed while Home Assistant was stopped) end.
        for key in [k for k, st in self.states.items() if st.active and k not in self._configured()]:
            self._release(*key, "removed", startup=True)

    def _configured(self) -> dict[tuple[str, str], PriorityInput]:
        out: dict[tuple[str, str], PriorityInput] = {}
        for area_id in self.ctrl.config.enabled_rooms:
            for pi in self.ctrl.lighting.room(area_id).priority_inputs:
                out[(area_id, pi.id)] = pi
        return out

    def _subscribe(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        self._entities = {}
        for (area_id, input_id), pi in self._configured().items():
            if pi.entity_id:
                self._entities.setdefault(pi.entity_id, []).append((area_id, input_id))
        if self._entities and self._running:
            self._unsub = async_track_state_change_event(self.hass, list(self._entities), self._on_state)

    @callback
    def async_config_changed(self) -> None:
        """Inputs added, removed, enabled, disabled or changed; the safety lock changed."""
        self._subscribe()
        if not self.settled:
            return
        configured = self._configured()
        for key, st in list(self.states.items()):
            if st.active and (key not in configured or not self._usable(*key)):
                self._release(*key, "disabled" if key in configured else "removed")
        for key in configured:
            self._evaluate(*key)

    # ------------------------------------------------------------------ read
    def _usable(self, area_id: str, input_id: str) -> bool:
        pi = self.ctrl.lighting.room(area_id).priority_input(input_id)
        return (pi is not None and pi.enabled and not self.ctrl.lighting.priority_lock
                and area_id in self.ctrl.config.enabled_rooms)

    def state(self, area_id: str, input_id: str) -> InputState:
        return self.states.setdefault((area_id, input_id), InputState())

    def claims(self, area_id: str) -> dict[str, PriorityClaim]:
        """entity -> the claim of every active, armed input of the room (empty when none is active)."""
        if self.ctrl.lighting.priority_lock or area_id not in self.ctrl.config.enabled_rooms:
            return {}
        room = self.ctrl.lighting.room(area_id)
        active = [pi for pi in room.priority_inputs
                  if pi.enabled and (st := self.states.get((area_id, pi.id))) is not None and st.active]
        if not active:
            return {}
        return claims_for(active, {pi.id: _iso(self.states[(area_id, pi.id)].since) for pi in active})

    def active(self, area_id: str) -> list[PriorityInput]:
        room = self.ctrl.lighting.room(area_id)
        return [pi for pi in room.priority_inputs if (st := self.states.get((area_id, pi.id))) and st.active]

    # ---------------------------------------------------------------- events
    @callback
    def _on_state(self, event: Event[EventStateChangedData]) -> None:
        for area_id, input_id in self._entities.get(event.data["entity_id"], ()):
            self._evaluate(area_id, input_id)

    @callback
    def note_change(self, area_id: str, entity_id: str, *, manual: bool, off: bool) -> None:
        """A light changed by something other than Smart Occupancy (from the controller's light events)."""
        key = (area_id, entity_id)
        if off:
            self.owned.pop(key, None)  # (off: nothing left for a release to switch off)
        if manual and any(entity_id in pi.targets for pi in self.active(area_id)):
            self.touched.add(key)

    def _timer(self, area_id: str, input_id: str, kind: str, seconds: float) -> None:
        st = self.state(area_id, input_id)
        st.cancel()

        @callback
        def fire(_now: datetime) -> None:
            st.timer = st.timer_kind = st.timer_due = None
            if self._running:
                self._evaluate(area_id, input_id, fired=kind)

        st.timer, st.timer_kind = async_call_later(self.hass, seconds, fire), kind
        st.timer_due = dt_util.utcnow() + timedelta(seconds=seconds)

    def _evaluate(self, area_id: str, input_id: str, *, fired: str | None = None, startup: bool = False) -> None:
        pi = self.ctrl.lighting.room(area_id).priority_input(input_id)
        st = self.state(area_id, input_id)
        if pi is None:
            return
        state = self.hass.states.get(pi.entity_id) if pi.entity_id else None
        reading = pi.reading(state.state if state else None)
        now = dt_util.utcnow()
        if reading != st.reading:
            st.last_change = now
        st.reading = reading
        if not self.settled:
            return  # (evaluated when the inputs have settled after a start)
        if not self._usable(area_id, input_id):
            st.cancel()
            if st.active:
                self._release(area_id, input_id, "disabled")
            return
        if reading is None:
            if st.unavailable_since is None:
                st.unavailable_since = now
                self._history(st, "unavailable", f"{pi.name}: input {state.state if state else 'missing'}",
                              policy=pi.unavailable.value)
                self.ctrl.explain_priority_input(area_id, pi, "unavailable", state.state if state else "missing")
            if st.timer_kind == ENTRY:
                st.cancel()
            if not st.active:
                return
            if pi.unavailable == Unavailable.RELEASE:
                st.cancel()
                self._release(area_id, input_id, "unavailable")
            elif pi.unavailable == Unavailable.HOLD:
                if fired == GRACE:
                    self._release(area_id, input_id, "unavailable_grace")
                elif st.timer_kind != GRACE:
                    self._timer(area_id, input_id, GRACE, pi.grace)
            return  # (KEEP: active until the input reports again)
        if st.unavailable_since is not None:
            st.unavailable_since = None
            self._history(st, "recovered", f"{pi.name}: input reports again ({state.state if state else ''})")
        if reading:
            if st.active:
                if st.timer_kind in (RELEASE, GRACE):
                    st.cancel()  # (back before the release: the claim continues, nothing is sent)
                if startup:
                    self._history(st, "kept", f"{pi.name} still active after the restart")
                    self.ctrl.create_task(self.ctrl.async_priority(area_id, input_id, "kept"))
                return
            if fired == ENTRY or pi.debounce <= 0:
                self._activate(area_id, input_id, now)
            elif st.timer_kind != ENTRY:
                self._timer(area_id, input_id, ENTRY, pi.debounce)
            return
        # Inactive.
        if st.timer_kind == ENTRY:
            st.cancel()  # (a bounce shorter than the debounce: never activated)
            self._history(st, "debounced", f"{pi.name}: active for less than {pi.debounce:g} s; ignored")
            return
        if not st.active:
            return
        if fired == RELEASE or pi.release_delay <= 0 or startup:
            st.cancel()
            self._release(area_id, input_id, "inactive", startup=startup)
        elif st.timer_kind != RELEASE:
            self._timer(area_id, input_id, RELEASE, pi.release_delay)

    def _activate(self, area_id: str, input_id: str, now: datetime) -> None:
        pi = self.ctrl.lighting.room(area_id).priority_input(input_id)
        st = self.state(area_id, input_id)
        st.cancel()
        st.active, st.since = True, now
        if pi is not None:
            self._history(st, "activated", f"{pi.name} active: {', '.join(pi.targets)} forced On")
        _LOGGER.info("priority_input_active area=%s input=%s", area_id, input_id)
        self.ctrl.mark_dirty()
        self.ctrl.create_task(self.ctrl.async_priority(area_id, input_id, "activated"))

    def _release(self, area_id: str, input_id: str, reason: str, *, startup: bool = False) -> None:
        st = self.state(area_id, input_id)
        st.cancel()
        if not st.active:
            return
        since = st.since
        st.active, st.since = False, None
        pi = self.ctrl.lighting.room(area_id).priority_input(input_id)
        words = {"inactive": "input inactive", "unavailable": "input unavailable", "disabled": "disabled or locked",
                 "unavailable_grace": "input unavailable for longer than its grace period", "removed": "removed"}
        self._history(st, "released", f"{pi.name if pi else input_id} released ({words.get(reason, reason)})"
                      + (" after the restart" if startup else ""))
        _LOGGER.info("priority_input_released area=%s input=%s reason=%s", area_id, input_id, reason)
        self.ctrl.mark_dirty()
        self.ctrl.create_task(self.ctrl.async_priority(area_id, input_id, "released", reason=reason,
                                                       since=since, config=pi))

    @staticmethod
    def _history(st: InputState, event: str, text: str, **data: Any) -> None:
        st.history.append({"at": _iso(dt_util.utcnow()), "event": event, "text": text,
                           **{k: v for k, v in data.items() if v is not None}})

    def note(self, area_id: str, input_id: str, event: str, text: str, **data: Any) -> None:
        """A command result (sent, blocked, failed) for an input's history."""
        self._history(self.state(area_id, input_id), event, text, **data)

    # ------------------------------------------------------------- display
    def status(self, area_id: str) -> list[dict[str, Any]]:
        out = []
        room = self.ctrl.lighting.room(area_id)
        locked = self.ctrl.lighting.priority_lock
        for pi in room.priority_inputs:
            st = self.states.get((area_id, pi.id)) or InputState()
            state = self.hass.states.get(pi.entity_id) if pi.entity_id else None
            word = ("locked" if locked and pi.enabled else "disabled" if not pi.enabled else
                    "active" if st.active else "unavailable" if st.reading is None and state is not None
                    and self.settled else "missing" if pi.entity_id and state is None else
                    "settling" if not self.settled else "inactive")
            out.append({
                "id": pi.id, "name": pi.name, "status": word, "active": st.active, "since": _iso(st.since),
                "input_state": state.state if state else None, "reading": st.reading,
                "timer": st.timer_kind, "timer_due": _iso(st.timer_due),
                "unavailable_since": _iso(st.unavailable_since), "history": list(st.history)[-10:],
            })
        return out

    def diagnostics(self) -> dict[str, Any]:
        return {"settled": self.settled, "lock": self.ctrl.lighting.priority_lock,
                "rooms": {a: self.status(a) for a in sorted(self.ctrl.config.enabled_rooms)
                          if self.ctrl.lighting.room(a).priority_inputs},
                "owned": {f"{a}/{e}": i for (a, e), i in self.owned.items()}}
