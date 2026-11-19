"""Lighting parity scenarios (Phase A4): record what the integration's lighting controller does.

A scenario runs in a real (test) Home Assistant with simulated lights and relays
(service calls change their state, with the caller's context, like a local
device). The :class:`Recorder` captures, from the moment it starts:

* the starting picture the App's shadow would get: the lighting configuration,
  the controller's runtime state and recent commands, the options, every state,
  the registries and Home Assistant's configuration (through the real websocket
  API, so the formats are exactly what the App receives), the occupancy engine's
  specification and results;
* every ``state_changed`` event (with its context), in order, before any other
  listener sees it, and every change of a room's published occupancy;
* every command the controller sends (with its outcome) and every decision.

``smart_lighting/tests/test_lighting_parity.py`` replays each recorded scenario
through the App's host and requires the same commands at the same times and the
same decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
from datetime import timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, ServiceCall, StateMachine, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, \
    label_registry as lr
from homeassistant.helpers.json import json_bytes
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.occupancy_core.wire import result_to_wire, spec_to_wire

from .test_zones import Devices as ZoneDevices

REPO = Path(__file__).resolve().parents[2]
SCENARIO_DIR = REPO / "smart_lighting/tests/lighting_scenarios"

BRIGHTNESS_KEYS = ("brightness", "brightness_pct")


def _json(value: Any) -> Any:
    return json.loads(json_bytes(value))


def _label(label) -> dict[str, Any]:
    from homeassistant.components.config.label_registry import _entry_dict  # noqa: PLC0415

    return _json(_entry_dict(label))


def _iso(value) -> str:
    return dt_util.as_utc(value).isoformat()


class Devices(ZoneDevices):
    """The zone tests' simulated lights and relays (Zigbee2MQTT-like: reports with a new context, optional delay).

    Reports made while a command is being executed are marked with that command's context (``during``), so a
    replay can deliver them inside the App's corresponding command, exactly where the integration saw them.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass)
        hass.services.async_register("light", "toggle", self._handle)
        hass.services.async_register("switch", "toggle", self._handle)
        self.during: str | None = None

    async def _handle(self, call: ServiceCall) -> None:
        if call.service == "toggle":
            ids = self.ids(call)
            state = self.hass.states.get(ids[0])
            call = ServiceCall(self.hass, call.domain, "turn_off" if state and state.state == "on" else "turn_on",
                               dict(call.data), call.context)
        self.during = call.context.id
        try:
            await super()._handle(call)
        finally:
            self.during = None


@dataclass
class Recorder:
    """Records one scenario from :meth:`async_begin` to :meth:`finish`."""

    hass: HomeAssistant
    entry: Any
    ws: Any
    name: str
    description: str
    devices: Any = None
    _current: str | None = None  # the service call being sent (context id # call number)
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
    _msg_id: int = 1000

    async def _ws(self, command: str, **params: Any) -> Any:
        self._msg_id += 1
        await self.ws.send_json({"id": self._msg_id, "type": command, **params})
        reply = await self.ws.receive_json()
        assert reply["success"], reply
        return reply["result"]

    @property
    def ctrl(self):
        return self.entry.runtime_data.lighting

    async def async_begin(self) -> None:
        """Restart the controller from its stores (as the App's starts), snapshot that picture, start recording.

        Both controllers then start at the same instant from the same stored configuration and state, so start-up
        behaviour (nothing applied on start, the supervisor's settle delay) is the same on both sides. The restart
        is the one the lighting bridge uses when it takes control back.
        """
        from custom_components.smart_occupancy.controller import LightingController  # noqa: PLC0415

        hass, coordinator = self.hass, self.entry.runtime_data
        old = coordinator.lighting
        await old.async_flush()
        coordinator.lighting_bridge.detach()
        old.async_stop()
        ctrl = LightingController(hass, coordinator)
        await ctrl.async_load()
        coordinator.lighting = ctrl
        ctrl.async_start()
        coordinator.lighting_bridge.attach(ctrl)
        await hass.async_block_till_done()
        manager = self.entry.runtime_data.occupancy
        automations = {}
        for state in hass.states.async_all(("automation", "script")):
            try:
                reply = await self._ws(f"{state.domain}/config", entity_id=state.entity_id)
            except AssertionError:
                reply = None
            automations[state.entity_id] = (reply or {}).get("config")
        self.start = _json({
            "at": _iso(dt_util.utcnow()),
            "config": ctrl.lighting.as_dict(),
            "state": ctrl.export_state(),
            "recent": ctrl.recent_commands(),
            "options": dict(self.entry.options),
            "ha_config": await self._ws("get_config"),
            "states": [s.as_dict() for s in hass.states.async_all()],
            # What the config component's list commands send (the same serializations).
            # The list plus the extended entries (config/entity_registry/get_entries), as the App reads them.
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
        # Move the recorder to the front: it sees every change before any controller reacts to it.
        mine = [item for item in bus_listeners if item not in before]
        bus_listeners[:] = mine + before
        for area_id in manager.engines:
            listener = self._occupancy_listener(area_id)
            self._unsubs.append(manager.async_add_listener(area_id, listener))
            listeners = manager._listeners[area_id]  # noqa: SLF001 - first, before the controller reacts
            listeners.insert(0, listeners.pop(listeners.index(listener)))
        if "mqtt" in hass.config.components:
            from homeassistant.components import mqtt  # noqa: PLC0415

            self._unsubs.append(await mqtt.async_subscribe(hass, "#", self._on_mqtt, qos=0, encoding=None))
        ctrl.command_observer = self._on_command
        ctrl.decisions.observer = self._on_decision
        # Which state changes are a device's answer to a command (whatever simulates the devices).
        send = ctrl.async_call_service

        async def call_service(domain, service, data, context, **kw):
            # One id per service call (several batches of one command share a context).
            self._calls += 1
            previous, self._current = self._current, f"{context.id}#{self._calls}"
            try:
                return await send(domain, service, data, context, **kw)
            finally:
                self._current = previous

        ctrl.async_call_service = call_service
        original = StateMachine.async_set
        recorder = self

        def async_set(machine, entity_id, new_state, attributes=None, force_update=False, context=None, *a, **k):
            if recorder._current is not None and context is not None:
                recorder._reports[context.id] = recorder._current
            return original(machine, entity_id, new_state, attributes, force_update, context, *a, **k)

        self._patch = patch.object(StateMachine, "async_set", async_set)
        self._patch.start()
        self._unsubs.append(ctrl.store.async_add_listener(self._on_config))

    @callback
    def _on_state(self, event: Event) -> None:
        new = event.data["new_state"]
        item = {"t": _iso(event.time_fired), "type": "state", "entity_id": event.data["entity_id"],
                "state": _json(new.as_dict()) if new is not None else None}
        if new is not None and (during := self._reports.get(new.context.id)):
            # A device's report to a command: the state machine had it inside the command; listeners got it
            # then too, or (when the command ran inside another event's dispatch) right after that dispatch.
            item["during"] = during
            item["dispatched"] = "inside" if self._current == during else "after"
        self.events.append(item)

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
    def _on_mqtt(self, msg) -> None:
        payload = msg.payload.decode(errors="replace") if isinstance(msg.payload, bytes) else str(msg.payload)
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "mqtt", "topic": msg.topic, "payload": payload,
                            "qos": msg.qos, "retain": msg.retain})

    @callback
    def _on_command(self, record: dict[str, Any]) -> None:
        self.commands.append({**_json(record), "call": self._current})

    @callback
    def _on_decision(self, subject: str, decision: dict[str, Any]) -> None:
        self.decisions.append({"t": _iso(dt_util.utcnow()), "subject": subject, **_json(decision)})

    @callback
    def _on_config(self, _old, new) -> None:
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "config", "config": _json(new.as_dict())})

    def mark(self, text: str) -> None:
        """A note in the timeline (shown when a replay differs)."""
        self.events.append({"t": _iso(dt_util.utcnow()), "type": "note", "text": text})

    async def async_capture_models(self) -> None:
        """The integration's answers to the read-only editor commands (the App's handlers must give the same)."""
        rooms = sorted(self.entry.runtime_data.config.enabled_rooms)
        requests: list[dict[str, Any]] = [{"type": "smart_occupancy/lighting/editor"}]
        for area_id in rooms:
            requests += [{"type": "smart_occupancy/lighting/editor", "area_id": area_id},
                         {"type": "smart_occupancy/lighting/editor", "area_id": area_id, "profile": "vacant"},
                         {"type": "smart_occupancy/lighting/room/activation", "area_id": area_id},
                         {"type": "smart_occupancy/lighting/supervisor/room", "area_id": area_id},
                         {"type": "smart_occupancy/lighting/preview", "area_id": area_id}]
        self.models = []
        for request in requests:
            self._msg_id += 1
            await self.ws.send_json({"id": self._msg_id, **request})
            reply = await self.ws.receive_json()
            self.models.append({"request": request, "reply": _json(reply)})

    def finish(self) -> dict[str, Any]:
        if self._patch is not None:
            self._patch.stop()
            self._patch = None
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        ctrl = self.ctrl
        ctrl.command_observer = None
        ctrl.decisions.observer = None
        return {"schema": 1, "name": self.name, "description": self.description, "start": self.start,
                "events": self.events, "end": _iso(dt_util.utcnow()), "expected": {
                    "commands": self.commands, "decisions": self.decisions, "models": self.models}}


async def advance(hass: HomeAssistant, freezer, seconds: float, rec: Recorder | None = None,
                  step: float = 1.0) -> None:
    """Move the clock forward in small steps (timers fire close to their time, as in a running Home Assistant).

    Each step is recorded: a replay fires the App's timers at exactly the same steps.
    """
    left = seconds
    while left > 0:
        tick = min(step, left)
        freezer.tick(timedelta(seconds=tick))
        if rec is not None:
            rec.events.append({"t": _iso(dt_util.utcnow()), "type": "tick"})
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        left -= tick


def path_of(name: str) -> Path:
    return SCENARIO_DIR / f"{name}.json.gz"


def write(data: dict[str, Any]) -> None:
    SCENARIO_DIR.mkdir(parents=True, exist_ok=True)
    # (Key order kept: the bridge's JSON keeps it too, and the order of e.g. a switch's buttons matters.)
    text = json.dumps(data, separators=(",", ":"))
    path_of(data["name"]).write_bytes(gzip.compress(text.encode(), mtime=0))


def read(name: str) -> dict[str, Any]:
    return json.loads(gzip.decompress(path_of(name).read_bytes()))


def comparable(data: dict[str, Any]) -> Any:
    """What must stay the same between two recordings of a scenario (context ids and decision ids are random)."""
    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            # (lights_off_since can come from a state some fixtures set before the clock was frozen: wall-clock.)
            out = {k: clean(v) for k, v in value.items() if k not in ("context_id", "id", "context", "first_at",
                                                                       "call", "lights_off_since")}
            for key in ("covered_by_group", "members", "opaque_groups"):  # built from sets (hash order)
                if isinstance(out.get(key), list):
                    out[key] = sorted(out[key], key=lambda v: json.dumps(v, sort_keys=True))
            return out
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    # The editor models carry fixture details (registry ids, times of states set before the clock was frozen); the
    # App's replay compares them exactly with the same recording. Between two recordings: the behaviour.
    return clean({k: v for k, v in data["expected"].items() if k != "models"})
