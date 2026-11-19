"""Curtain parity scenarios (Phase A5): record what the integration's curtain manager does.

A scenario runs in a real (test) Home Assistant with the simulated Zigbee2MQTT
covers of ``test_curtains`` (a command is accepted, the device reports with a
context of its own, possibly later, possibly failing). The :class:`CurtainRecorder`
captures, from the moment it starts:

* the starting picture the App's shadow would get: the curtain configuration,
  the manager's runtime state (holds, applied presets, budgets, logs, history),
  the options, every state, the registries and Home Assistant's configuration
  (through the real websocket API, so the formats are exactly what the App
  receives), the occupancy engine's specification and published results;
* every ``state_changed`` event (with its context and Home Assistant's times),
  in order, before any other listener sees it, and every ``state_reported``
  (an unchanged report moves ``last_reported``, which the light sensor's
  staleness is judged by); every change of a room's published occupancy; every
  configuration change;
* every cover command the manager sends (``command_observer``) with its
  outcome (an error, or the device's reports made inside the command), every
  automatic decision (``decision_observer``), the read-only editor commands
  the App mirrors on its shadow (``resume`` ...) and the integration's answers
  to ``smart_occupancy/curtains/model`` at checkpoints.

``smart_lighting/tests/test_curtains_parity.py`` replays each recorded scenario
through the App's host (``CurtainInstance``) and requires the same commands at
the same instants, the same decisions and the same model answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import gzip
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp.client_exceptions import WSMessageTypeError

from homeassistant.const import EVENT_STATE_CHANGED, EVENT_STATE_REPORTED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, StateMachine, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, \
    label_registry as lr
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.occupancy_core.wire import result_to_wire, spec_to_wire

from .test_curtains import Api, Covers as BaseCovers

REPO = Path(__file__).resolve().parents[1]
SCENARIO_DIR = REPO / "smart_lighting/tests/curtain_scenarios"
MODEL = "smart_occupancy/curtains/model"
#: Random or per-process values that differ between two runs of the same scenario.
VOLATILE = ("context_id", "id", "context", "call", "op_id")


def _json(value: Any) -> Any:
    return json.loads(json_bytes(value))


def _label(label) -> dict[str, Any]:
    from homeassistant.components.config.label_registry import _entry_dict  # noqa: PLC0415

    return _json(_entry_dict(label))


def _iso(value) -> str:
    return dt_util.as_utc(value).isoformat()


class Covers(BaseCovers):
    """The curtain tests' simulated covers, telling the recorder which reports belong to which command.

    Reports made while a cover command is being executed are marked with that command (``during``), so a replay
    can deliver them inside the App's corresponding command, exactly where the integration saw them; a command the
    device refuses is recorded with its error.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        self.recorder: CurtainRecorder | None = None

    async def _handle(self, call: ServiceCall) -> None:
        rec = self.recorder
        if rec is None:
            return await super()._handle(call)
        rec._calls += 1  # noqa: SLF001
        previous, rec._current = rec._current, f"{call.context.id}#{rec._calls}"  # noqa: SLF001
        record = rec.bind(call)
        try:
            return await super()._handle(call)
        except Exception as err:
            if record is not None:
                record["error"] = str(err)
            raise
        finally:
            rec._current = previous  # noqa: SLF001


async def ws_call(api: Api, command: str, **params: Any) -> dict[str, Any]:
    """Any websocket command through the test's (reconnecting) connection; the whole reply."""
    api.n += 1
    payload = {"id": api.n, "type": command, **params}
    for attempt in (1, 2):
        try:
            if api.client.closed:
                raise ConnectionResetError
            await api.client.send_json(payload)
            while True:
                msg = await api.client.receive_json()
                if msg.get("id") == api.n and msg.get("type") == "result":
                    return msg
        except (ConnectionResetError, WSMessageTypeError):
            if api.connect is None or attempt == 2:
                raise
            api.client = await api.connect()
    raise AssertionError("unreachable")


@dataclass
class CurtainRecorder:
    """Records one scenario from :meth:`async_begin` to :meth:`finish`."""

    hass: HomeAssistant
    entry: Any
    api: Api
    name: str
    description: str
    covers: Covers | None = None
    _current: str | None = None  # the cover command being executed (context id # call number)
    _calls: int = 0
    _reports: dict[str, str] = field(default_factory=dict)  # context of a state set during it -> that command
    _patch: Any = None
    models: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    start: dict[str, Any] = field(default_factory=dict)
    _unsubs: list[CALLBACK_TYPE] = field(default_factory=list)
    _results: dict[str, Any] = field(default_factory=dict)

    async def _ws(self, command: str, **params: Any) -> Any:
        reply = await ws_call(self.api, command, **params)
        assert reply["success"], reply
        return reply["result"]

    @property
    def mgr(self):
        return self.entry.runtime_data.curtains

    async def async_begin(self) -> None:
        """Restart the manager from its stores (as the App's starts), snapshot that picture, start recording.

        Both managers then start at the same instant from the same stored configuration and runtime state, so
        start-up behaviour (nothing applied on start, the start-up settle period, the light history starting again)
        is the same on both sides. The restart is the one the curtain bridge uses when it takes control back. The
        observers are attached before the start, so the evaluations the start makes are recorded too.
        """
        from custom_components.smart_occupancy.curtains.manager import CurtainManager  # noqa: PLC0415

        hass, coordinator = self.hass, self.entry.runtime_data
        old = coordinator.curtains
        assert not old.busy_covers(), "begin the recording while no cover is being moved"
        await old.async_flush()
        coordinator.curtain_bridge.detach()
        old.async_stop()
        await hass.async_block_till_done()
        mgr = CurtainManager(hass, coordinator.occupancy)
        await mgr.async_load()
        coordinator.curtains = mgr
        manager = coordinator.occupancy
        automations = {}
        for state in hass.states.async_all(("automation", "script")):
            try:
                reply = await self._ws(f"{state.domain}/config", entity_id=state.entity_id)
            except AssertionError:
                reply = None
            automations[state.entity_id] = (reply or {}).get("config")
        self.start = _json({
            "at": _iso(dt_util.utcnow()),
            "config": mgr.config.as_dict(),
            "state": mgr.export_state(),
            "options": dict(self.entry.options),
            "ha_config": await self._ws("get_config"),
            "states": [s.as_dict() for s in hass.states.async_all()],
            # What the config component's list commands send (the same serializations), as the App reads them.
            "entity_registry": [_json(e.extended_dict) for e in er.async_get(hass).entities.values()
                                if e.partial_json_repr is not None],
            "device_registry": [json.loads(d.json_repr) for d in dr.async_get(hass).devices
                                if d.json_repr is not None],
            "area_registry": [_json(a.json_fragment) for a in ar.async_get(hass).async_list_areas()],
            "label_registry": [_label(label) for label in lr.async_get(hass).async_list_labels()],
            "config_entries": [{"entry_id": e.entry_id, "domain": e.domain, "title": e.title, "state": e.state.value}
                               for e in hass.config_entries.async_entries()],
            "automations": automations,
            "spec": spec_to_wire(manager.house.spec),
            "occupancy": {a: result_to_wire(e.result, e.visit_confirmed) for a, e in manager.engines.items()
                          if e.result is not None},
        })
        self._results = dict(self.start["occupancy"])
        bus_listeners = hass.bus._listeners.setdefault(EVENT_STATE_CHANGED, [])  # noqa: SLF001
        before = list(bus_listeners)
        self._unsubs.append(hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state))
        # Move the recorder to the front: it sees every change before the manager reacts to it.
        mine = [item for item in bus_listeners if item not in before]
        bus_listeners[:] = mine + before
        self._unsubs.append(hass.bus.async_listen(EVENT_STATE_REPORTED, self._on_reported,
                                                  event_filter=callback(lambda _data: True)))
        for area_id in manager.engines:
            listener = self._occupancy_listener(area_id)
            self._unsubs.append(manager.async_add_listener(area_id, listener))
            listeners = manager._listeners[area_id]  # noqa: SLF001 - first, before the manager reacts
            listeners.insert(0, listeners.pop(listeners.index(listener)))
        original = StateMachine.async_set
        recorder = self

        def async_set(machine, entity_id, new_state, attributes=None, force_update=False, context=None, *a, **k):
            if recorder._current is not None and context is not None:
                recorder._reports[context.id] = recorder._current
            return original(machine, entity_id, new_state, attributes, force_update, context, *a, **k)

        self._patch = patch.object(StateMachine, "async_set", async_set)
        self._patch.start()
        if self.covers is not None:
            self.covers.recorder = self
        coordinator.curtain_bridge.attach(mgr)
        mgr.command_observer = self._on_command
        mgr.decision_observer = self._on_decision
        mgr.config_observer = self._on_config
        mgr.async_start()
        await hass.async_block_till_done()

    def bind(self, call: ServiceCall) -> dict[str, Any] | None:
        """The command record of the cover command being executed (the observer saw it just before)."""
        for record in reversed(self.commands):
            if record.get("call") is None and record["context_id"] == call.context.id \
                    and record["entity_id"] == call.data.get("entity_id"):
                record["call"] = self._current
                return record
        return None

    @callback
    def _on_state(self, event: Event) -> None:
        new = event.data["new_state"]
        item = {"t": _iso(event.time_fired), "type": "state", "entity_id": event.data["entity_id"],
                "state": _json(new.as_dict()) if new is not None else None}
        if new is not None and (during := self._reports.get(new.context.id)):
            # A device's report to a command: the state machine had it inside the command and so did the listeners
            # (Home Assistant dispatches nested events at once), or - if it was fired later - after it.
            item["during"] = during
            item["dispatched"] = "inside" if self._current == during else "after"
        self.events.append(item)

    @callback
    def _on_reported(self, event: Event) -> None:
        """An unchanged report (same state and attributes): only ``last_reported`` moves; no ``state_changed``."""
        new = event.data["new_state"]
        self.events.append({"t": _iso(event.time_fired), "type": "reported", "entity_id": event.data["entity_id"],
                            "last_reported": _iso(new.last_reported)})

    def _occupancy_listener(self, area_id: str):
        manager = self.entry.runtime_data.occupancy

        @callback
        def update() -> None:
            engine = manager.engines[area_id]
            if engine.result is None:
                return
            wire = _json(result_to_wire(engine.result, engine.visit_confirmed))
            if wire != self._results.get(area_id):
                self._results[area_id] = wire
                self.events.append({"t": _iso(dt_util.utcnow()), "type": "occupancy", "area_id": area_id,
                                    "result": wire})

        return update

    @callback
    def _on_command(self, record: dict[str, Any]) -> None:
        self.commands.append({**_json(record), "call": None})

    @callback
    def _on_decision(self, window_id: str, decision: dict[str, Any]) -> None:
        self.decisions.append({"t": _iso(dt_util.utcnow()), "window_id": window_id, **_json(decision)})

    @callback
    def _on_config(self, config) -> None:
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "config", "config": _json(config.as_dict())})

    def tick(self) -> None:
        """The clock moved and the harness fires the timers due: a replay fires the App's at the same step."""
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "tick"})

    def mark(self, text: str) -> None:
        """A note in the timeline (shown when a replay differs)."""
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "note", "text": text})

    async def async_capture_model(self) -> dict[str, Any]:
        """A checkpoint: the integration's answer to the read-only model command (the App's handler must agree)."""
        request = {"type": MODEL}
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "model", "index": len(self.models)})
        reply = await ws_call(self.api, MODEL)
        self.models.append({"request": request, "reply": _json({k: v for k, v in reply.items()
                                                                if k in ("success", "result", "error")})})
        return reply["result"]

    async def async_api(self, command: str, **params: Any) -> dict[str, Any]:
        """A curtain editor command a person sends while the scenario runs (the App's shadow mirrors the same
        runtime commands on its copy: ``resume``, ``apply``, ``manual/clear`` ...)."""
        request = {"type": f"smart_occupancy/curtains/{command}", **params}
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "api", "request": _json(request)})
        reply = await self.api(command, **params)
        assert reply["success"], reply
        await self.hass.async_block_till_done()
        return reply["result"]

    def finish(self) -> dict[str, Any]:
        if self._patch is not None:
            self._patch.stop()
            self._patch = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self.covers is not None:
            self.covers.recorder = None
        mgr = self.mgr
        mgr.command_observer = None
        mgr.decision_observer = None
        mgr.config_observer = None
        return {"schema": 1, "name": self.name, "description": self.description, "start": self.start,
                "events": self.events, "end": _iso(dt_util.utcnow()), "expected": {
                    "commands": self.commands, "decisions": self.decisions, "models": self.models}}


async def advance(hass: HomeAssistant, freezer, seconds: float, rec: CurtainRecorder | None = None,
                  step: float = 1.0) -> None:
    """Move the clock forward in small steps (timers fire close to their time, as in a running Home Assistant)."""
    from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

    left = seconds
    while left > 0:
        tick = min(step, left)
        freezer.tick(timedelta(seconds=tick))
        if rec is not None:
            rec.tick()
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        left -= tick


def path_of(name: str) -> Path:
    return SCENARIO_DIR / f"{name}.json.gz"


def write(data: dict[str, Any]) -> None:
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, separators=(",", ":"))  # key order kept (the bridge's JSON keeps it too)
    path_of(data["name"]).write_bytes(gzip.compress(text.encode(), mtime=0))


def read(name: str) -> dict[str, Any]:
    return json.loads(gzip.decompress(path_of(name).read_bytes()))


def comparable(data: dict[str, Any]) -> Any:
    """What must stay the same between two recordings of a scenario (context and operation ids are random, and so
    are the window and layer ids a test run creates: they are compared by their place in the configuration)."""
    names: dict[str, str] = {}
    for i, window in enumerate(data["start"]["config"].get("windows") or []):
        names[window["window_id"]] = f"window#{i}"
        for j, layer in enumerate(window.get("layers") or []):
            names[layer["layer_id"]] = f"layer#{i}.{j}"

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in VOLATILE}
        if isinstance(value, list):
            return [clean(v) for v in value]
        if isinstance(value, str):
            return names.get(value, value)
        return value
    # The model answers carry fixture details (registry ids, states set before the clock was frozen); the App's
    # replay compares them exactly with the same recording. Between two recordings: the behaviour.
    return clean({k: v for k, v in data["expected"].items() if k != "models"})
