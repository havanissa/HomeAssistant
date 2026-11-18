"""Phase A4 behavioural parity: the App's lighting runtime replays recorded integration scenarios.

Every fixture in ``lighting_scenarios/`` was recorded from the integration's own
lighting controller in a real (test) Home Assistant (``tests/test_lighting_parity.py``
of the integration). Here the vendored copy of the same controller runs on the
App's host interface: it starts from the recorded starting picture, receives the
recorded events at the recorded times (virtual time; timers fire exactly as in the
recording's harness), and its commands are answered like the recorded commands
(same outcome; the devices' reports made during a command are delivered inside
the App's matching command). It must send the same commands at the same times
and make the same decisions.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

from freezegun import freeze_time
import pytest

from homeassistant.core import Context, Event, State
from homeassistant.exceptions import HomeAssistantError
from occupancy_core.wire import spec_from_wire
from homeassistant.helpers import event as host_event

from smart_lighting.lighting_host import AppBackend, LightingInstance, room_result
from smart_lighting.lighting_scripts import AutomationEntity, ScriptEntity

SCENARIOS = sorted((Path(__file__).parent / "lighting_scenarios").glob("*.json.gz"))
FIRE_EARLY = 0.5  # the recording harness fires timers up to 0.5 s early (async_fire_time_changed)


class FakeHA:
    """The App's websocket client as the lighting host sees it (the recorded starting picture)."""

    def __init__(self, start: dict[str, Any]) -> None:
        self.config = start["ha_config"]
        self.states = {s["entity_id"]: s for s in start["states"]}
        self.entity_registry = start["entity_registry"]
        self.devices = start["device_registry"]
        self.areas = start["area_registry"]
        self.labels = start["label_registry"]
        self.config_entries_all = start["config_entries"]
        self.connected = True

    async def command(self, command_type: str, **params: Any) -> Any:
        raise HomeAssistantError(f"{command_type} is not part of the replay")

    async def unsubscribe(self, sub_id: int) -> None:
        return None


class FakeOccupancy:
    def __init__(self, start: dict[str, Any]) -> None:
        self.spec = spec_from_wire(start["spec"])
        self.published = dict(start["occupancy"])

    def published_wire(self, area_id: str) -> dict[str, Any] | None:
        return self.published.get(area_id)

    def room_spec(self, area_id: str) -> Any:
        return self.spec.room(area_id)


def _topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, part in enumerate(p):
        if part == "#":
            return True
        if i >= len(t) or (part != "+" and part != t[i]):
            return False
    return len(p) == len(t)


class ReplayBackend(AppBackend):
    """The App's backend in the replay: commands answered like the recording; MQTT from the recording."""

    def __init__(self, ha: Any, replay: Replay) -> None:
        super().__init__(ha, execute=replay.call_service)
        self.replay = replay

    async def async_mqtt_subscribe(self, topic: str, handler: Any) -> Any:
        item = (topic, handler)
        self.replay.mqtt.append(item)
        return lambda: self.replay.mqtt.remove(item) if item in self.replay.mqtt else None

    def mqtt_connected(self) -> bool:
        return True


def command_key(record: dict[str, Any]) -> str:
    data = dict(record.get("data") or {})
    raw = data.pop("entity_id", None)
    ids = sorted([raw] if isinstance(raw, str) else list(raw or []))
    return json.dumps([record.get("domain"), record.get("service"), ids, data], sort_keys=True)


class Replay:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.start = data["start"]
        self.events = data["events"]
        self.expected = data["expected"]["commands"]
        self.used: set[int] = set()
        self.consumed: set[int] = set()
        self.preset: dict[int, Any] = {}  # event index -> the state before the report (dispatched later)
        self.commands: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []
        self.unexpected: list[dict[str, Any]] = []
        self.inst: LightingInstance | None = None
        self.mqtt: list[tuple[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.occ = FakeOccupancy(self.start)

    # ------------------------------------------------------------- commands
    async def call_service(self, domain: str, service: str, data: dict[str, Any], context: Context) -> None:
        """The App's command: answered like the integration's matching one (outcome and the devices' reports)."""
        key = command_key({"domain": domain, "service": service, "data": data})
        match = None
        for i, record in enumerate(self.expected):
            if i not in self.used and command_key(record) == key:
                match = i
                break
        if match is None:
            self.unexpected.append({"domain": domain, "service": service, "data": data})
            return
        self.used.add(match)
        record = self.expected[match]
        for j, event in enumerate(self.events):
            if j in self.consumed or j in self.preset or event.get("during") != record["call"]:
                continue
            if event.get("dispatched") == "after":
                # The state machine has the report now; listeners get it at its place in the timeline.
                new = State.from_wire(event["state"]) if event["state"] else None
                self.preset[j] = self.inst.hass.states.set_state(event["entity_id"], new)
            else:
                self.consumed.add(j)
                self.apply(event)
        if record.get("error"):
            raise HomeAssistantError(record["error"])

    # --------------------------------------------------------------- events
    def apply(self, event: dict[str, Any]) -> None:
        inst = self.inst
        kind = event["type"]
        if kind == "state":
            inst.state_changed(event["entity_id"], event["state"])
        elif kind == "occupancy":
            self.occ.published[event["area_id"]] = event["result"]
            inst.occupancy_changed(event["area_id"])
        elif kind == "mqtt":
            for topic, handler in list(self.mqtt):
                if _topic_matches(topic, event["topic"]):
                    handler({"topic": event["topic"], "payload": event["payload"], "qos": event.get("qos", 0),
                             "retain": event.get("retain", False)})
        elif kind == "config":
            from smart_occupancy.lighting_config import LightingConfig  # noqa: PLC0415

            inst.controller.store.async_set(LightingConfig.from_dict(event["config"]))

    async def settle(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0)

    def fire_timers(self, frozen: Any, now: Any) -> None:
        loop = asyncio.get_running_loop()
        from datetime import timedelta  # noqa: PLC0415

        late = now + timedelta(seconds=FIRE_EARLY)
        for handle in list(loop._scheduled):  # noqa: SLF001 - the same as the recording harness
            if handle.cancelled():
                continue
            if handle.when() - loop.time() <= FIRE_EARLY:
                with patch.object(host_event, "time_tracker_utcnow", return_value=late):
                    handle._run()  # noqa: SLF001
                handle.cancel()

    async def run(self) -> None:
        from homeassistant.util import dt as dt_util  # noqa: PLC0415

        start = self.start
        with freeze_time(start["at"]) as frozen:
            ha = FakeHA(start)
            backend = ReplayBackend(ha, self)
            automations = [AutomationEntity(e, c) for e, c in start["automations"].items() if e.startswith("automation.")]
            scripts = [ScriptEntity(e, c) for e, c in start["automations"].items() if e.startswith("script.")]
            inst = LightingInstance(ha, role="shadow", options=start["options"], config=start["config"],
                                    state=start["state"], published=lambda a: room_result(self.occ, a),
                                    send_input=lambda *_: None, backend=backend, storage_dir=None,
                                    automations=automations, scripts=scripts)
            self.inst = inst
            await inst.async_start()
            inst.controller.note_recent_commands(start["recent"])
            inst.controller.command_observer = lambda record: self.commands.append(record)
            inst.controller.decisions.observer = lambda subject, d: self.decisions.append(
                {"t": dt_util.utcnow().isoformat(), "subject": subject, **d})
            await self.settle()
            deferred: list[int] = []
            for i, event in enumerate(self.events):
                if i in self.consumed:
                    continue
                t = dt_util.parse_datetime(event["t"])
                if t > dt_util.utcnow() or event["type"] == "tick":
                    # A device's answer to a command the App did not send arrives at the end of its instant.
                    for j in deferred:
                        if j not in self.consumed and j not in self.preset:
                            self.consumed.add(j)
                            self.apply(self.events[j])
                    deferred.clear()
                    await self.settle()
                if t > dt_util.utcnow():
                    frozen.move_to(t)
                if event.get("during") and i not in self.preset:
                    deferred.append(i)
                    continue
                if event["type"] == "tick":
                    self.fire_timers(frozen, dt_util.utcnow())
                elif i in self.preset:
                    new = self.inst.hass.states.get(event["entity_id"])
                    self.inst.hass.bus.async_fire_internal(Event("state_changed", {
                        "entity_id": event["entity_id"], "old_state": self.preset.pop(i), "new_state": new},
                        context=new.context if new else Context()))
                else:
                    self.apply(event)
                await self.settle()
            for j in deferred:
                if j not in self.consumed and j not in self.preset:
                    self.apply(self.events[j])
            await self.settle()
            await self.answer_models()
            await inst.async_stop(flush=False)

    async def answer_models(self) -> None:
        """The App's own handlers answer the same editor commands the integration answered at the end."""
        from types import SimpleNamespace  # noqa: PLC0415

        from smart_lighting.lighting_api import LightingApi, LightingApiError  # noqa: PLC0415

        api = LightingApi(None, SimpleNamespace(mode="owner", instance=self.inst, controller=self.inst.controller,
                                                _switching=False))
        self.models = []
        for item in self.data["expected"].get("models", []):
            request = item["request"]
            try:
                result = await api.call(dict(request), user_id="u", is_admin=True)
                reply = {"success": True, "result": result}
            except LightingApiError as err:
                reply = {"success": False, "error": {"code": err.code}}
            self.models.append({"request": request, "reply": json.loads(json.dumps(reply, default=str))})


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in ("context_id", "id", "context", "first_at", "call")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


# The editor model's parts that only the integration has (its own pages in the App show them): presence and
# forecasts, the room's occupancy health, and the problem list of the integration's full validation.
INTEGRATION_ONLY = ("presence", "forecast", "health", "problems")
UNORDERED = ("covered_by_group", "members", "opaque_groups")


def model_view(reply: dict[str, Any]) -> Any:
    if not reply.get("success"):
        return {"success": False, "code": (reply.get("error") or {}).get("code")}

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            out = {k: strip(v) for k, v in value.items() if k not in INTEGRATION_ONLY}
            if {"sensors", "state", "reason", "occupied"} <= set(out) and out.get("occupied") is False:
                # A vacant zone's text says which evaluation came last; a room timer (integration) and a zone timer
                # (App) due in the same step run in two processes. Its state is compared.
                out.pop("reason")
            for key in UNORDERED:
                if isinstance(out.get(key), list):  # built from sets: their order is the process's hash order
                    out[key] = sorted(out[key], key=lambda v: json.dumps(v, sort_keys=True))
            if isinstance(out.get("entries"), list) and all(isinstance(e, dict) and "kind" in e for e in out["entries"]):
                # A decision log: decisions of the same instant may be in either order (two processes' timers).
                out["entries"] = sorted(out["entries"], key=lambda e: (e.get("at") or "", json.dumps(e, sort_keys=True)))
                out.pop("latest", None)
            return out
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return _clean(strip(reply.get("result")))


def by_instant(items: list[dict[str, Any]], key: str) -> list[list[str]]:
    """Items grouped by their time, each group as a sorted set: the same things at the same instants.

    Within one instant the order is not compared: two timers due in the same harness step run in the integration
    in one process and partly in the App in another (occupancy timers stay in the integration's engine).
    """
    groups: dict[str, list[str]] = {}
    for item in _clean(items):
        groups.setdefault(item.get(key) or "", []).append(json.dumps(item, sort_keys=True))
    return [sorted(groups[t]) for t in sorted(groups)]


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.name.split(".")[0] for p in SCENARIOS])
async def test_replay_matches_the_integration(path: Path) -> None:
    data = json.loads(gzip.decompress(path.read_bytes()))
    replay = Replay(data)
    await replay.run()
    assert not replay.unexpected, f"commands the integration did not send: {replay.unexpected}"
    assert by_instant(replay.commands, "at") == by_instant(data["expected"]["commands"], "at"), \
        "the App's commands differ from the integration's"
    assert by_instant(replay.decisions, "t") == by_instant(data["expected"]["decisions"], "t"), \
        "the decisions differ"
    expected_models = data["expected"].get("models", [])
    assert len(replay.models) == len(expected_models) >= 1
    for theirs, ours in zip(expected_models, replay.models, strict=True):
        assert model_view(ours["reply"]) == model_view(theirs["reply"]), f"{theirs['request']}: the answers differ"


def test_there_are_scenarios() -> None:
    assert len(SCENARIOS) >= 21
