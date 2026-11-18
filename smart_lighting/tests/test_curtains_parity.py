"""Phase A5 behavioural parity: the App's curtain runtime replays recorded integration scenarios.

Every fixture in ``curtain_scenarios/`` was recorded from the integration's own
curtain manager in a real (test) Home Assistant (``tests/test_curtains_parity.py``
of the integration). Here the vendored copy of the same manager runs on the
App's host interface (``CurtainInstance``): it starts from the recorded starting
picture, receives the recorded events at the recorded times (virtual time;
timers fire exactly as in the recording's harness), its cover commands are
answered like the recorded commands (the same error, or the device's reports
delivered inside the App's matching command), the editor commands a person sent
(``resume`` ...) run through the App's own handlers, and the model command is
answered at the recorded checkpoints. It must send the same commands at the same
instants, make the same decisions and give the same model answers.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from freezegun import freeze_time
import pytest

from homeassistant.core import Context, Event, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import event as host_event
from homeassistant.util import dt as dt_util
from occupancy_core.wire import spec_from_wire

from smart_lighting.curtains_api import CurtainsApi, CurtainsApiError
from smart_lighting.curtains_compare import command_key, decision_key
from smart_lighting.curtains_host import CurtainInstance
from smart_lighting.lighting_host import AppBackend, room_result
from smart_lighting.lighting_scripts import AutomationEntity, ScriptEntity

SCENARIOS = sorted((Path(__file__).parent / "curtain_scenarios").glob("*.json.gz"))
FIRE_EARLY = 0.5       # the recording harness fires timers up to 0.5 s early (async_fire_time_changed)
TOLERANCE = 2.0        # seconds: a command or decision counts as "the same instant" within this


class FakeHA:
    """The App's websocket client as the curtain host sees it (the recorded starting picture)."""

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


class ReplayBackend(AppBackend):
    """The shadow's backend (``record`` captures every command); the command is answered like the recording."""

    def __init__(self, ha: Any, replay: Replay) -> None:
        super().__init__(ha, execute=None, record=replay.recorded.append)
        self.replay = replay

    async def async_call_service(self, domain: str, service: str, data: dict[str, Any], context: Context,
                                 blocking: bool) -> Any:
        await super().async_call_service(domain, service, data, context, blocking)
        return await self.replay.call_service(domain, service, data, context)


class Replay:
    """``feed_reported``: deliver the recorded ``state_reported`` events (unchanged sensor reports) to the host's
    states as ``last_reported`` updates, as a host with a complete feed would."""

    def __init__(self, data: dict[str, Any], *, feed_reported: bool = True) -> None:
        self.data = data
        self.feed_reported = feed_reported
        self.start = data["start"]
        self.events = data["events"]
        self.expected = data["expected"]["commands"]
        self.used: set[int] = set()
        self.consumed: set[int] = set()
        self.preset: dict[int, Any] = {}
        self.recorded: list[dict[str, Any]] = []     # the backend's record (what the shadow would compare)
        self.commands: list[dict[str, Any]] = []     # the manager's command observer (as the recording)
        self.decisions: list[dict[str, Any]] = []
        self.unexpected: list[dict[str, Any]] = []
        self.models: list[dict[str, Any]] = []
        self.api_replies: list[dict[str, Any]] = []
        self.inst: CurtainInstance | None = None
        self.occ = FakeOccupancy(self.start)

    # ------------------------------------------------------------- commands
    async def call_service(self, domain: str, service: str, data: dict[str, Any], context: Context) -> None:
        """The App's command: answered like the integration's matching one (error, or the device's reports)."""
        key = command_key({"domain": domain, "service": service, "data": data})
        match = None
        for i, record in enumerate(self.expected):
            if i not in self.used and command_key(record) == key:
                match = i
                break
        if match is None:
            self.unexpected.append({"domain": domain, "service": service, "data": data,
                                    "at": dt_util.utcnow().isoformat()})
            return
        self.used.add(match)
        record = self.expected[match]
        for j, event in enumerate(self.events):
            if j in self.consumed or j in self.preset or event.get("during") != record["call"]:
                continue
            if event.get("dispatched") == "after":
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
        elif kind == "reported":
            if not self.feed_reported:
                return
            old = inst.hass.states.get(event["entity_id"])
            if old is not None:
                inst.hass.states.set_state(event["entity_id"], State(
                    old.entity_id, old.state, old.attributes, last_changed=old.last_changed,
                    last_updated=old.last_updated, context=old.context,
                    last_reported=dt_util.parse_datetime(event["last_reported"])))
        elif kind == "occupancy":
            self.occ.published[event["area_id"]] = event["result"]
            inst.occupancy_changed(event["area_id"])
        elif kind == "config":
            from smart_occupancy.curtains.config import CurtainConfig  # noqa: PLC0415

            inst.config_changed(CurtainConfig.from_dict(event["config"]))

    async def settle(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0)

    def fire_timers(self, now: datetime) -> None:
        loop = asyncio.get_running_loop()
        late = now + timedelta(seconds=FIRE_EARLY)
        for handle in list(loop._scheduled):  # noqa: SLF001 - the same as the recording harness
            if handle.cancelled():
                continue
            if handle.when() - loop.time() <= FIRE_EARLY:
                with patch.object(host_event, "time_tracker_utcnow", return_value=late):
                    handle._run()  # noqa: SLF001
                handle.cancel()

    def api(self) -> CurtainsApi:
        inst = self.inst
        return CurtainsApi(None, SimpleNamespace(mode="owner", instance=inst, manager=inst.manager,
                                                _switching=False))

    async def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        """The App's own handler answers a curtain editor command (as the owner's interface and the shadow do)."""
        try:
            result = await self.api().call(dict(request), user_id="u", is_admin=True)
            reply = {"success": True, "result": result}
        except CurtainsApiError as err:
            reply = {"success": False, "error": {"code": err.code, "message": str(err)}}
        return json.loads(json.dumps(reply, default=_default))

    async def run(self) -> None:
        from smart_occupancy.curtains.manager import CurtainManager  # noqa: PLC0415

        start = self.start
        original_start = CurtainManager.async_start
        replay = self

        def observed_start(mgr: Any) -> None:
            # Observers before the start, as the recorder's: the evaluations the start makes are compared too.
            mgr.command_observer = lambda record: replay.commands.append(record)
            mgr.decision_observer = lambda window_id, d: replay.decisions.append(
                {"t": dt_util.utcnow().isoformat(), "window_id": window_id, **d})
            return original_start(mgr)

        with freeze_time(start["at"]) as frozen:
            ha = FakeHA(start)
            backend = ReplayBackend(ha, self)
            automations = [AutomationEntity(e, c) for e, c in start["automations"].items() if e.startswith("automation.")]
            scripts = [ScriptEntity(e, c) for e, c in start["automations"].items() if e.startswith("script.")]
            inst = CurtainInstance(ha, role="shadow", options=start["options"], config=start["config"],
                                   state=start["state"], published=lambda a: room_result(self.occ, a),
                                   backend=backend, storage_dir=None, automations=automations, scripts=scripts)
            self.inst = inst
            try:
                with patch.object(CurtainManager, "async_start", observed_start):
                    await inst.async_start()
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
                    kind = event["type"]
                    if kind == "tick":
                        self.fire_timers(dt_util.utcnow())
                    elif i in self.preset:
                        new = inst.hass.states.get(event["entity_id"])
                        inst.hass.bus.async_fire_internal(Event("state_changed", {
                            "entity_id": event["entity_id"], "old_state": self.preset.pop(i), "new_state": new},
                            context=new.context if new else Context()))
                    elif kind == "model":
                        expected = self.data["expected"]["models"][event["index"]]
                        self.models.append({"request": expected["request"],
                                            "reply": await self.answer(expected["request"])})
                    elif kind == "api":
                        self.api_replies.append(await self.answer(event["request"]))
                    else:
                        self.apply(event)
                    await self.settle()
                for j in deferred:
                    if j not in self.consumed and j not in self.preset:
                        self.apply(self.events[j])
                await self.settle()
            finally:
                await inst.async_stop(flush=False)
                dt_util.set_default_time_zone(UTC)  # the host set the recording's time zone


def _default(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items() if k not in ("context_id", "id", "context", "call", "op_id")}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


# Model fields that are not behaviour: the release string and the next scheduled evaluation (an internal deadline
# the card shows).
MODEL_VOLATILE = ("version", "next_evaluation")


def model_view(reply: dict[str, Any]) -> Any:
    if not reply.get("success"):
        return {"success": False, "code": (reply.get("error") or {}).get("code")}

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in MODEL_VOLATILE}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return _clean(strip(reply.get("result")))


def _seconds(value: str | None) -> float:
    return datetime.fromisoformat(value).timestamp() if value else 0.0


def pair(ours: list[dict[str, Any]], theirs: list[dict[str, Any]], key: Any, when: str,
         tolerance: float = TOLERANCE) -> tuple[list[Any], list[Any]]:
    """Match items with the same key within ``tolerance`` seconds; returns what is left unmatched on each side."""
    left = list(enumerate(theirs))
    unmatched_ours = []
    for mine in ours:
        found = None
        for n, (i, other) in enumerate(left):
            if key(other) == key(mine) and abs(_seconds(other.get(when)) - _seconds(mine.get(when))) <= tolerance:
                found = n
                break
        if found is None:
            unmatched_ours.append(_clean(mine))
        else:
            left.pop(found)
    return unmatched_ours, [_clean(other) for _, other in left]


def _decision_view(d: dict[str, Any]) -> str:
    return decision_key(d["window_id"], d) + " " + json.dumps(d.get("text"))


def per_window(decisions: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in decisions:
        out.setdefault(d["window_id"], []).append(_decision_view(d))
    return out


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.name.split(".")[0] for p in SCENARIOS])
async def test_replay_matches_the_integration(path: Path) -> None:
    data = json.loads(gzip.decompress(path.read_bytes()))
    replay = Replay(data)
    await replay.run()
    expected = data["expected"]
    assert not replay.unexpected, f"commands the integration did not send: {replay.unexpected}"
    # The same cover commands (service, cover, position) at the same instants; the backend's record saw them all.
    ours, theirs = pair(replay.commands, expected["commands"], command_key, "at")
    assert (ours, theirs) == ([], []), f"the App's commands differ from the integration's: only App {ours}; " \
                                        f"only integration {theirs}"
    assert [command_key(r) for r in replay.recorded] == [command_key(r) for r in replay.commands]
    # The same decision stream per window (status actions / codes / conditions / presets, log entries, texts) ...
    assert per_window(replay.decisions) == per_window(expected["decisions"]), "the decisions differ"
    # ... at the same instants.
    ours, theirs = pair(replay.decisions, expected["decisions"], _decision_view, "t")
    assert (ours, theirs) == ([], []), f"decisions at other instants: only App {ours}; only integration {theirs}"
    # The same answers to the model command at every checkpoint, and the same answers to the person's commands.
    assert len(replay.models) == len(expected["models"]) >= 1
    for theirs_, ours_ in zip(expected["models"], replay.models, strict=True):
        assert model_view(ours_["reply"]) == model_view(theirs_["reply"]), f"{theirs_['request']}: the answers differ"
    assert all(reply["success"] for reply in replay.api_replies), replay.api_replies


def test_there_are_scenarios() -> None:
    assert len(SCENARIOS) >= 5


@pytest.mark.xfail(strict=True, reason="A host fed with state_changed only: an unchanged sensor report "
                   "(state_reported, which Home Assistant delivers to filtered bus listeners only) does not move "
                   "last_reported, so the vendored environment judge calls a steady light sensor stale after "
                   "lux_stale minutes. The App's client therefore re-reads the states every 60 s and delivers moved "
                   "last_reported values (ha.REPORTED_REFRESH_SECONDS; test_curtains_runtime proves the path); this "
                   "replay pins what the feed alone gives. Decisions and commands still match; the readings do not.")
async def test_unchanged_reports_reach_the_host() -> None:
    """The replay with the websocket feed alone (no ``state_reported``): the model answers must still agree."""
    path = next(p for p in SCENARIOS if p.name.startswith("evening_privacy_night"))
    data = json.loads(gzip.decompress(path.read_bytes()))
    replay = Replay(data, feed_reported=False)
    await replay.run()
    for theirs_, ours_ in zip(data["expected"]["models"], replay.models, strict=True):
        assert model_view(ours_["reply"]) == model_view(theirs_["reply"]), f"{theirs_['request']}: the answers differ"
