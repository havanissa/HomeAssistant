"""Phase A5: curtain ownership, the fenced executor, handover, mirror, fallback and rollback (integration side).

A simulated App (``FakeApp``) speaks the curtain bridge protocol over a real
websocket connection, against the simulated Zigbee2MQTT covers of
``test_curtains`` and the automatic-curtain scenario driver of
``test_curtains_auto``. The tests prove:

* the integration commands the covers by default (also after a restart); a
  session gets an exact copy (configuration, runtime state) and every cover
  command, automatic decision and configuration change of its manager;
* a reviewed handover needs the same curtain code, the current configuration,
  readiness, the expected epoch and no cover being moved; it stops the
  integration's manager (dormant, fenced, locked), sends no cover command and
  hands over configuration and state (manual holds, applied presets);
* while the App commands the covers the integration's curtain editors are
  read-only (reads, previews and simulations keep answering) and its manager
  cannot send even when asked directly;
* the App's commands are executed only from the owner's session with the current
  epoch, the lease token and an increasing sequence number, and only for an
  allowlisted cover service of a configured cover with allowed data; everything
  else is refused and moves nothing; the App's context is used;
* the mirror (configuration and state) reaches the integration;
* release, the ``return`` command, the admin service, lease expiry, a new App
  instance and a restart nobody resumes give control back: a new integration
  manager starts from the App's last configuration and state (holds and applied
  presets kept), sends nothing at the switch, marks an operation the App left
  running as interrupted (its covers' late reports are not manual changes),
  evaluates the windows only after the start-up settle, and the old App's
  commands are refused;
* Home Assistant restarts and integration reloads keep the manager dormant; only
  the same App process may resume;
* entity renames while the App is away reach the mirror and the fallback;
* non-administrators cannot use any bridge command or the service;
* the diagnostics download includes the ownership record.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from datetime import timedelta
import secrets
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy import curtain_sync as S
from custom_components.smart_occupancy.const import DOMAIN
from custom_components.smart_occupancy.curtains import auto as auto_mod
from custom_components.smart_occupancy.curtains.config import CurtainConfigError
from custom_components.smart_occupancy.diagnostics import async_get_config_entry_diagnostics
from custom_components.smart_occupancy.occupancy_core import fingerprint as core_fingerprint
from custom_components.smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint
from custom_components.smart_occupancy.supervision import CurtainOrigin

from .test_curtains import (  # noqa: F401  (fixtures)
    KOKUR_EAST,
    KAMAR,
    STOVA_BLACKOUT,
    STOVA_SHEER,
    Api,
    covers,
    house,
    set_cover,
)
from .test_curtains_auto import LUX, Run, admin_ws, begin, enable, faroe, kamar, stova  # noqa: F401  (fixtures)
from .test_lighting_runtime import WS_LOGGER

SUB = "smart_occupancy/curtains/bridge/subscribe"
MIRROR_KEY = "smart_occupancy.curtains_mirror"
NOON = "2026-11-19T12:00:00+00:00"


def readiness(ready: bool = True) -> dict[str, Any]:
    return {"ready": ready, "checks": [{"key": "commands", "status": "ok" if ready else "error"}],
            "summary": {"seconds": 3600, "matched": 4, "mismatches": 0}}


class FakeApp:
    """The App's side of the curtain bridge on one websocket connection."""

    def __init__(self, hass: HomeAssistant, client: Any, instance: str | None = None) -> None:
        self.hass = hass
        self.client = client
        self.instance = instance or secrets.token_hex(16)
        self._id = 100
        self.sub_id: int | None = None
        self.events: list[dict[str, Any]] = []
        self.session: str | None = None
        self.hello: dict[str, Any] | None = None
        self.epoch: int | None = None
        self.token: str | None = None
        self.seq = 0

    async def send(self, **msg: Any) -> dict[str, Any]:
        self._id += 1
        await self.client.send_json({"id": self._id, **msg})
        while True:
            reply = await self.client.receive_json()
            if reply.get("type") == "event" and reply.get("id") == self.sub_id:
                self.events.append(reply["event"])
                continue
            if reply.get("id") == self._id:
                return reply

    async def drain(self) -> list[dict[str, Any]]:
        await self.hass.async_block_till_done()
        await self.send(type="ping")
        return self.events

    async def connect(self, resume_token: str | None = None, *, fp: str | None = None,
                      expect_ok: bool = True) -> dict[str, Any]:
        self._id += 1
        self.sub_id = self._id
        await self.client.send_json({
            "id": self._id, "type": SUB, "instance_id": self.instance, "protocol": S.PROTOCOL_VERSION,
            "app_version": "0.6.0", "runtime_version": RUNTIME_VERSION, "fingerprint": fp or fingerprint(),
            "core_fingerprint": core_fingerprint(), "resume_token": resume_token})
        reply = await self.client.receive_json()
        if not expect_ok:
            return reply
        assert reply["success"], reply
        hello = (await self.client.receive_json())["event"]
        assert hello["type"] == "hello"
        self.hello, self.session = hello, hello["session_id"]
        if hello.get("lease_token"):
            self.token, self.epoch, self.seq = hello["lease_token"], hello["ownership"]["epoch"], 0
        return hello

    async def claim(self, *, ready: bool = True, epoch: int | None = None, revision: str | None = None) -> dict:
        reply = await self.send(type="smart_occupancy/curtains/bridge/claim", session_id=self.session,
                                expected_epoch=self.hello["ownership"]["epoch"] if epoch is None else epoch,
                                config_revision=revision or self.hello["config_revision"], readiness=readiness(ready))
        if reply["success"]:
            self.epoch, self.token, self.seq = reply["result"]["epoch"], reply["result"]["lease_token"], 0
        return reply

    def owner(self, **over: Any) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session, "epoch": self.epoch, "lease_token": self.token, "seq": self.seq, **over}

    async def execute(self, domain: str, service: str, data: dict[str, Any], *, context: str | None = None,
                      **over: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/curtains/bridge/execute", **self.owner(**over), domain=domain,
                               service=service, data=data, context_id=context or Context().id)

    async def heartbeat(self, **over: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/curtains/bridge/heartbeat", **self.owner(**over))

    async def publish(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/curtains/bridge/publish", **self.owner(), **params)

    async def release(self, **params: Any) -> dict[str, Any]:
        return await self.send(type="smart_occupancy/curtains/bridge/release", **self.owner(), **params)


def bridge_of(entry):
    return entry.runtime_data.curtain_bridge


def wire(value: Any) -> Any:
    """As the websocket delivers it (tuples become lists)."""
    return json.loads(json.dumps(value))


def mgr_of(entry):
    return entry.runtime_data.curtains


async def tick(hass: HomeAssistant, freezer, seconds: float, step: float = 1.0) -> None:
    """Move the clock forward in small steps (timers fire close to their time, as in a running Home Assistant)."""
    left = seconds
    while left > 0:
        freezer.tick(timedelta(seconds=min(step, left)))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        left -= step


async def alive(r: Run, app: FakeApp, seconds: float) -> None:
    """Time passes while the App keeps its lease (the light sensor keeps reporting, like the real one)."""
    left = seconds
    while left > 0:
        step = min(S.HEARTBEAT_SECONDS, left)
        r.freezer.tick(timedelta(seconds=step))
        if r.lux_value is not None:
            r.lux(r.lux_value)
        async_fire_time_changed(r.hass)
        await r.hass.async_block_till_done()
        assert (await app.heartbeat())["success"]
        left -= step


async def started(hass, pilot, freezer, covers, admin_ws, house) -> tuple[Any, Run, str, str]:
    """A November noon in the Faroes: Kamar Erva in Automatic (opened by it: an applied preset), Stova Suður in
    Observing with a confirmed preset applied by hand (a manual hold). Nothing moves after this."""
    set_cover(hass, KAMAR, 0)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, NOON, lux=3000, elevation=6, sleep=False)
    k, s = await kamar(r.api, house), await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    await hass.async_block_till_done()
    assert r.condition(k) == "day" and r.pos(KAMAR) == 100 and r.mgr.auto.rt(k).applied["status"] == "succeeded"
    plan = await r.api.ok("preview", window_id=s, preset_id="p_full_shade")
    await r.api.ok("apply", window_id=s, preset_id="p_full_shade", confirm=True, expected=plan["positions"])
    await hass.async_block_till_done()
    assert r.mgr.auto.rt(s).hold["source"] == "preset" and r.mgr.manual == {}
    await r.walk(2)
    assert len(covers.calls) == 2  # Kamar opened, the Stova blackout to 100
    covers.calls.clear()
    return entry, r, k, s


# ------------------------------------------------------------------ default and shadow
async def test_default_owner_and_shadow_copy(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer,
                                             faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge, mgr = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and bridge.state() == S.OWNER_INTEGRATION
    assert not mgr.dormant and not mgr.fenced and not mgr.locked and mgr.auto.running
    assert bridge.status()["manager"] == "running"
    app = FakeApp(hass, await admin_ws())
    hello = await app.connect()
    assert hello["config"] == mgr.config.as_dict()
    assert hello["config_revision"] == S.config_revision(mgr.config.as_dict())
    assert hello["state"] == wire(mgr.export_state()) and hello["running"] is True
    assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["lease_token"] is None
    assert hello["state"]["auto"][s]["hold"]["source"] == "preset"
    # Sleep starts: the Automatic window closes; the session sees the command and the decisions.
    r.sleep(True)
    await tick(hass, freezer, 3)
    assert r.pos(KAMAR) == 0
    events = await app.drain()
    commands = [e["command"] for e in events if e["type"] == "command"]
    assert len(commands) == 1
    command = commands[0]
    assert command["domain"] == "cover" and command["service"] == "set_cover_position"
    assert command["data"] == {"entity_id": KAMAR, "position": 0}
    assert command["context_id"] == mgr.last[k]["context_id"] == covers.calls[-1].context.id
    assert command["window_id"] == k and command["purpose"] == "automatic"
    decisions = [e for e in events if e["type"] == "decision"]
    assert {d["window_id"] for d in decisions} >= {k}
    assert {d["decision"]["kind"] for d in decisions} == {"status", "log"}
    assert any(d["decision"]["kind"] == "log" and d["decision"]["action"] == "moved" and d["window_id"] == k
               for d in decisions)
    # A configuration change is streamed with its revision.
    window = mgr.config.windows[s]
    mgr.async_set(mgr.config.with_window(replace(window, name="Stova Suður (renamed)")))
    events = await app.drain()
    config = [e for e in events if e["type"] == "config"][-1]
    assert config["config"] == mgr.config.as_dict()
    assert config["config_revision"] == S.config_revision(mgr.config.as_dict()) == bridge.config_revision()
    assert next(w for w in config["config"]["windows"] if w["window_id"] == s)["name"] == "Stova Suður (renamed)"


async def test_restart_without_a_record_keeps_the_integration_in_control(hass: HomeAssistant, pilot, house, covers,
                                                                        admin_ws, freezer, faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bridge, mgr = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and not bridge.resume_pending and bridge.epoch == 0
    assert not mgr.dormant and mgr._started and mgr.auto.running  # noqa: SLF001
    assert covers.calls == []  # a restart never moves a cover


# ------------------------------------------------------------------ handover
async def test_claim_refusals_and_a_moving_cover(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer,
                                                 faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge, mgr = bridge_of(entry), mgr_of(entry)
    other = FakeApp(hass, await admin_ws())
    await other.connect(fp="0" * 64)
    assert (await other.claim())["error"]["code"] == S.REJECT_VERSION
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim(revision="0" * 16))["error"]["code"] == S.REJECT_CONFIG
    assert (await app.claim(ready=False))["error"]["code"] == S.REJECT_NOT_READY
    assert (await app.claim(epoch=99))["error"]["code"] == S.REJECT_BUSY
    assert bridge.owner == S.OWNER_INTEGRATION and not mgr.dormant and not mgr.fenced
    # The Kamar cover is being moved (a slow motor): the claim waits a little, then is refused; the movement is
    # neither contradicted nor replayed and finishes normally.
    covers.travel = {KAMAR: 40}
    r.sleep(True)
    await hass.async_block_till_done()
    assert mgr.running(k) is not None and len(covers.calls) == 1
    task = asyncio.ensure_future(app.claim())  # (not one of Home Assistant's tasks: time must move meanwhile)
    for _ in range(int(S.MOVING_WAIT_SECONDS) + 10):  # (time is frozen: the wait's timeout fires when it moves)
        await tick(hass, freezer, 1)
        if task.done():
            break
    reply = await task
    assert not reply["success"] and reply["error"]["code"] == S.REJECT_BUSY, reply
    assert "Kamar Erva" in reply["error"]["message"]
    assert bridge.owner == S.OWNER_INTEGRATION and not mgr.fenced and not mgr.dormant
    assert mgr.running(k) is not None  # still the same operation
    await tick(hass, freezer, 35)
    assert mgr.running(k) is None and mgr.last[k]["status"] == "succeeded" and r.pos(KAMAR) == 0
    assert covers.services() == [("set_cover_position", KAMAR, 0)]  # one command, never repeated
    assert bridge.status()["moving"] == []
    # Once the cover rests the same App may take over.
    reply = await app.claim()
    assert reply["success"], reply
    assert bridge.owner == S.OWNER_APP and mgr.dormant and covers.services() == [("set_cover_position", KAMAR, 0)]


async def test_handover_stops_the_integration_manager_and_sends_nothing(hass: HomeAssistant, pilot, house, covers,
                                                                       admin_ws, freezer, faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge, old = bridge_of(entry), mgr_of(entry)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    reply = await app.claim()
    assert reply["success"], reply
    grant = reply["result"]
    assert grant["config"] == old.config.as_dict() and grant["config_revision"] == S.config_revision(grant["config"])
    assert grant["state"]["auto"][s]["hold"]["source"] == "preset"             # the manual hold travels
    assert grant["state"]["auto"][k]["applied"]["preset_id"] == "p_open"       # and the applied preset
    assert grant["state"]["auto"][k]["applied"]["status"] == "succeeded"
    assert grant["state"]["last"][k]["status"] == "succeeded" and grant["lease_token"] == app.token
    assert set(grant["state"]) == S.STATE_SECTIONS
    mgr = mgr_of(entry)
    assert mgr is old and mgr.dormant and mgr.fenced and mgr.locked
    assert not mgr._started and not mgr.auto.running  # noqa: SLF001
    assert mgr.command_observer is None and mgr.decision_observer is None and mgr.config_observer is None
    assert covers.calls == []  # the switch itself sends nothing
    status = bridge.status()
    assert status["owner"] == S.OWNER_APP and status["state"] == S.OWNER_APP and status["manager"] == "dormant"
    assert status["history"][0]["reason"] == S.REASON_HANDOVER and status["history"][0]["readiness"]["ready"]
    assert status["owner_connected"] and status["epoch"] == 1
    # Nothing happens on its own any more: sleep starts, the Automatic window stays open and is not even evaluated.
    before = copy.deepcopy(mgr.auto.rt(k).status)
    r.sleep(True)
    await alive(r, app, 30)
    assert covers.calls == [] and r.pos(KAMAR) == 100
    assert mgr.auto.rt(k).status == before and before["condition"] == "day"
    # The integration's manager cannot send even when asked directly.
    item = {"layer_id": "l_x", "entity_id": KAMAR, "name": "Kamar", "kind": "blackout", "target": 0,
            "service": "set_cover_position", "status": "sent", "detail": "", "start_position": 100,
            "final_position": 100}
    await mgr._send(item, Context())  # noqa: SLF001
    assert item["status"] == "failed" and "Not sent" in item["detail"] and covers.calls == []
    with pytest.raises(CurtainConfigError) as err:
        await mgr.async_apply(k, "p_closed", origin=CurtainOrigin.MANUAL_CONFIRMED, confirmed=True, expected=None)
    assert err.value.code == "busy" and covers.calls == []


# ------------------------------------------------------------------ the owner's commands
async def test_owner_commands_are_fenced_and_allowlisted(hass: HomeAssistant, pilot, house, covers, admin_ws,
                                                         freezer, faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge = bridge_of(entry)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim())["success"]
    context = Context().id
    reply = await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 40}, context=context,
                              window_id=k, purpose="automatic")
    assert reply["success"], reply
    assert reply["result"]["seq"] == app.seq
    assert r.pos(KAMAR) == 40 and covers.services() == [("set_cover_position", KAMAR, 40)]
    assert covers.calls[-1].context.id == context  # the App's own context (its reports are its own)
    assert bridge.executed == 1 and bridge.execute_errors == 0
    assert bridge.last_execute["entities"] == [KAMAR] and bridge.last_execute["position"] == 40
    assert bridge.last_execute["window_id"] == k and bridge.last_execute["purpose"] == "automatic"
    assert (await app.execute("cover", "open_cover", {"entity_id": STOVA_SHEER}))["success"]
    assert r.pos(STOVA_SHEER) == 100 and bridge.executed == 2
    before = len(covers.calls)
    refused = {
        "stop": (await app.execute("cover", "stop_cover", {"entity_id": KAMAR}), S.REJECT_NOT_ALLOWED),
        "domain": (await app.execute("light", "turn_on", {"entity_id": "light.kamar_erva_vindeyga"}),
                   S.REJECT_NOT_ALLOWED),
        "extra data": (await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 10,
                                                                         "transition": 1}), S.REJECT_INVALID),
        "open with data": (await app.execute("cover", "open_cover", {"entity_id": KAMAR, "position": 1}),
                           S.REJECT_NOT_ALLOWED),
        "range": (await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 101}),
                  S.REJECT_INVALID),
        "fraction": (await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 40.5}),
                     S.REJECT_INVALID),
        "text": (await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": "40"}),
                 S.REJECT_INVALID),
        "two covers": (await app.execute("cover", "set_cover_position", {"entity_id": [KAMAR, STOVA_SHEER],
                                                                         "position": 10}), S.REJECT_INVALID),
        "unconfigured": (await app.execute("cover", "set_cover_position", {"entity_id": KOKUR_EAST, "position": 10}),
                         S.REJECT_NOT_ALLOWED),
        "not a cover": (await app.execute("cover", "open_cover", {"entity_id": "switch.boiler"}), S.REJECT_NOT_ALLOWED),
        "context": (await app.execute("cover", "open_cover", {"entity_id": KAMAR}, context="x" * 26), S.REJECT_INVALID),
    }
    for what, (reply, code) in refused.items():
        assert not reply["success"] and reply["error"]["code"] == code, (what, reply)
    assert len(covers.calls) == before and r.pos(KAMAR) == 40
    # Fencing: a stale epoch, a wrong lease, a replayed sequence, a foreign connection.
    assert (await app.execute("cover", "open_cover", {"entity_id": KAMAR}, epoch=app.epoch - 1))["error"][
        "code"] == S.REJECT_STALE_EPOCH
    assert (await app.execute("cover", "open_cover", {"entity_id": KAMAR}, lease_token="f" * 32))["error"][
        "code"] == S.REJECT_LEASE
    assert (await app.execute("cover", "open_cover", {"entity_id": KAMAR}, seq=app.seq - 2))["error"][
        "code"] == S.REJECT_SEQUENCE
    other = FakeApp(hass, await admin_ws(), instance=app.instance)
    other.session, other.epoch, other.token, other.seq = app.session, app.epoch, app.token, app.seq + 100
    assert (await other.execute("cover", "open_cover", {"entity_id": KAMAR}))["error"][
        "code"] == S.REJECT_UNKNOWN_SESSION
    assert len(covers.calls) == before and r.pos(KAMAR) == 40
    assert bridge.rejections[S.REJECT_NOT_ALLOWED] == 5 and bridge.rejections[S.REJECT_INVALID] == 6
    assert bridge.last_rejection["code"] == S.REJECT_UNKNOWN_SESSION
    # A cover that refuses the command: reported, counted, nothing else changes.
    covers.fail = {KAMAR}
    reply = await app.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 70})
    assert reply["error"]["code"] == S.REJECT_FAILED and "zigbee2mqtt" in reply["error"]["message"]
    assert bridge.execute_errors == 1 and bridge.executed == 2 and bridge.last_execute["error"]
    assert r.pos(KAMAR) == 40
    covers.fail = set()
    # The lease expires without a heartbeat: the command is refused (the integration took the covers back).
    await tick(hass, freezer, S.LEASE_SECONDS + 2)
    reply = await app.execute("cover", "open_cover", {"entity_id": KAMAR})
    assert reply["error"]["code"] in S.LOST_CODES and bridge.owner == S.OWNER_INTEGRATION


def test_check_cover_command() -> None:
    covers = {KAMAR, STOVA_SHEER}
    assert S.check_cover_command("cover", "set_cover_position", {"entity_id": KAMAR, "position": 0}, covers=covers) == [KAMAR]
    assert S.check_cover_command("cover", "set_cover_position", {"entity_id": [KAMAR], "position": 100.0},
                                 covers=covers) == [KAMAR]
    assert S.check_cover_command("cover", "open_cover", {"entity_id": STOVA_SHEER}, covers=covers) == [STOVA_SHEER]
    assert S.check_cover_command("cover", "close_cover", {"entity_id": STOVA_SHEER}, covers=covers) == [STOVA_SHEER]
    cases = [
        (("cover", "stop_cover", {"entity_id": KAMAR}), S.REJECT_NOT_ALLOWED),
        (("cover", "toggle", {"entity_id": KAMAR}), S.REJECT_NOT_ALLOWED),
        (("light", "turn_on", {"entity_id": KAMAR}), S.REJECT_NOT_ALLOWED),
        (("cover", "open_cover", {"entity_id": "light.lamp"}), S.REJECT_NOT_ALLOWED),
        (("cover", "open_cover", {"entity_id": KOKUR_EAST}), S.REJECT_NOT_ALLOWED),
        (("cover", "open_cover", {"entity_id": KAMAR, "position": 50}), S.REJECT_NOT_ALLOWED),
        (("cover", "set_cover_position", {"entity_id": KAMAR}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": -1}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": 101}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": 12.5}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": True}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": "50"}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": KAMAR, "position": 50, "tilt_position": 1}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": [KAMAR, STOVA_SHEER], "position": 50}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": [], "position": 50}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {"entity_id": "not an id", "position": 50}), S.REJECT_INVALID),
        (("cover", "set_cover_position", {}), S.REJECT_INVALID),
        (("cover", "set_cover_position", [KAMAR]), S.REJECT_INVALID),
        ((None, "open_cover", {"entity_id": KAMAR}), S.REJECT_INVALID),
    ]
    for args, code in cases:
        with pytest.raises(S.ProtocolError) as err:
            S.check_cover_command(*args, covers=covers)
        assert err.value.code == code, args
    assert S.covers_of({"windows": [{"layers": [{"entity_id": KAMAR}, {"entity_id": "light.x"}, "junk"]}, 3]}) == {KAMAR}
    with pytest.raises(S.ProtocolError):
        S.check_state({"last": {}, "bogus": 1})
    with pytest.raises(S.ProtocolError):
        S.check_state({"samples": {}})
    assert S.running_operations({"last": {"w": {"status": "running"}, "v": {"status": "succeeded"}}}) == ["w"]


# ------------------------------------------------------------------ mirror and read-only
async def test_mirror_and_read_only_editors(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe,
                                            hass_storage) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge, mgr = bridge_of(entry), mgr_of(entry)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    grant = (await app.claim())["result"]
    doc = copy.deepcopy(grant["config"])
    next(w for w in doc["windows"] if w["window_id"] == k)["name"] = "Kamar Erva (App)"
    assert (await app.publish(config=doc, config_revision="0" * 16))["error"]["code"] == S.REJECT_CONFIG
    assert mgr.config.windows[k].name == "Kamar Erva"
    state = copy.deepcopy(grant["state"])
    state["auto"][k]["moves"] = {"date": dt_util.now().date().isoformat(), "count": 5}
    reply = await app.publish(config=doc, config_revision=S.config_revision(doc), state=state)
    assert reply["success"], reply
    assert mgr.config.windows[k].name == "Kamar Erva (App)" and mgr.locked and mgr.dormant
    assert bridge.mirror["state"] == state and bridge.status()["mirror_at"]
    assert (await app.publish(state={"bogus": 1}))["error"]["code"] == S.REJECT_INVALID
    assert (await app.publish(state={"last": []}))["error"]["code"] == S.REJECT_INVALID
    # Garbage in the configuration never reaches the store: what would be stored is not what the App claims.
    assert (await app.publish(config={"windows": "nope"}, config_revision="x"))["error"]["code"] == S.REJECT_CONFIG
    assert mgr.config.windows[k].name == "Kamar Erva (App)" and len(mgr.config.windows) == 2
    assert bridge.mirror["state"] == state  # a refused publish changes nothing
    await bridge.async_flush()
    assert hass_storage[MIRROR_KEY]["data"]["state"] == state
    await mgr.async_flush()
    assert next(w for w in hass_storage["smart_occupancy.curtains"]["data"]["windows"]
                if w["window_id"] == k)["name"] == "Kamar Erva (App)"
    # The store is locked: the integration's own editor cannot change it.
    with pytest.raises(CurtainConfigError) as err:
        mgr.async_set(mgr.config.with_window(replace(mgr.config.windows[k], name="Mine")))
    assert err.value.code == "managed_by_app"
    assert mgr.config.windows[k].name == "Kamar Erva (App)"
    # The card's editors refuse; reading, previews and simulations keep answering.
    editor = Api(await admin_ws(), admin_ws)
    writes = {
        "window/update": {"window_id": k, "name": "Mine"},
        "preset/save": {"window_id": k, "name": "New", "positions": {}},
        "apply": {"window_id": k, "preset_id": "p_closed", "confirm": True, "expected": {}},
        "auto/settings": {"scope": "global", "values": {"daylight_lux": 200}},
        "modes/change": {"ops": [{"op": "assign", "mode": "day", "window_id": k, "preset_id": "p_half"}]},
        "auto/enable": {"window_id": s, "confirm": True},
        "resume": {"window_id": k},
    }
    for command, params in writes.items():
        err = await editor.error(command, **params)
        assert err["code"] == "managed_by_app" and "Smart Lighting App" in err["message"], command
    model = await editor.ok("model")
    assert {w["window_id"] for w in model["windows"]} == {k, s}
    assert next(w for w in model["windows"] if w["window_id"] == k)["name"] == "Kamar Erva (App)"
    assert (await editor.ok("preview", window_id=k, preset_id="p_closed"))["moves"] == 1
    assert (await editor.ok("auto/preview", window_id=k))["window_id"] == k
    simulated = await editor.ok("auto/simulate", scope="global", values={"daylight_lux": 200})
    assert {w["window_id"] for w in simulated["windows"]} == {k, s}
    assert covers.calls == [] and mgr.config.windows[k].name == "Kamar Erva (App)"


# ------------------------------------------------------------------ giving control back
@pytest.mark.parametrize("how", ["release", "return", "service", "lease", "new_instance", "restart"])
async def test_control_returns_with_the_apps_state_and_nothing_is_switched(
        hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe, hass_admin_user, how: str) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    old = mgr_of(entry)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    grant = (await app.claim())["result"]
    # The App's manager changes the configuration and leaves an operation running (the blackout travelling to 60);
    # it mirrors both.
    doc = copy.deepcopy(grant["config"])
    next(w for w in doc["windows"] if w["window_id"] == s)["name"] = "Stova Suður (App)"
    state = copy.deepcopy(grant["state"])
    layer = next(x for x in old.config.windows[s].layers if x.entity_id == STOVA_BLACKOUT)
    state["last"][s] = {
        "op_id": "op_app", "window_id": s, "preset_id": "p_evening_privacy", "preset_name": "Evening privacy",
        "origin": "automatic", "user_id": None, "context_id": Context().id,
        "started": dt_util.utcnow().isoformat(timespec="seconds"), "finished": None, "status": "running",
        "items": [{"layer_id": layer.layer_id, "entity_id": STOVA_BLACKOUT, "name": "Blackout", "kind": "blackout",
                   "target": 60, "service": "set_cover_position", "status": "sent", "detail": "sent",
                   "start_position": 100, "final_position": 100}],
        "explanation": "", "condition": "evening_privacy", "reason": "",
    }
    assert (await app.publish(config=doc, config_revision=S.config_revision(doc), state=state))["success"]
    # Sleep starts while the App owns the curtains: the integration's dormant manager sends nothing.
    r.sleep(True)
    await alive(r, app, 20)
    assert covers.calls == [] and r.pos(KAMAR) == 100
    if how == "release":
        reply = await app.release(config=doc, config_revision=S.config_revision(doc), state=state, detail="test")
        assert reply["success"] and reply["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION
    elif how == "return":
        reply = await app.send(type="smart_occupancy/curtains/bridge/return", reason="test")
        assert reply["success"] and reply["result"]["returned"]
    elif how == "service":
        await hass.services.async_call(DOMAIN, "curtains_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_admin_user.id))
    elif how == "lease":
        await tick(hass, freezer, S.LEASE_SECONDS + 2)
    elif how == "new_instance":
        newcomer = FakeApp(hass, await admin_ws())
        hello = await newcomer.connect()
        assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION and hello["lease_token"] is None
        assert hello["state"]["auto"][s]["hold"]["source"] == "preset"  # its copy is the restarted manager's
        assert hello["ownership"]["history"][0]["reason"] == S.REASON_APP_RESTARTED
    else:
        await bridge_of(entry).async_flush()
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert bridge_of(entry).resume_pending and mgr_of(entry).dormant
        await tick(hass, freezer, S.RESUME_GRACE_SECONDS + 3, step=3)
        assert bridge_of(entry).history[-1]["reason"] == S.REASON_NOT_RESUMED
    await tick(hass, freezer, 2)
    bridge, new = bridge_of(entry), mgr_of(entry)
    r.mgr = new
    assert new is not old and bridge.owner == S.OWNER_INTEGRATION and bridge.history[-1]["owner"] == S.OWNER_INTEGRATION
    assert not new.dormant and not new.fenced and not new.locked and new._started and new.auto.running  # noqa: SLF001
    assert bridge.status()["manager"] == "running" and bridge.mirror.get("returned_at")
    assert new.config.windows[s].name == "Stova Suður (App)"                 # the App's configuration
    assert new.auto.rt(s).hold["source"] == "preset"                          # its manual hold
    assert new.auto.rt(k).applied["preset_id"] == "p_open"                    # and the applied preset
    assert new.last[s]["status"] == "interrupted" and new.running(s) is None  # the App's operation is not resumed
    assert new.last[s]["items"][0]["status"] == "interrupted"
    assert covers.calls == []                                                 # taking control back switches nothing
    # The rest of the App's movement is reported by the device: ours, not a manual change.
    set_cover(hass, STOVA_BLACKOUT, 80, state="closing", context=Context())
    set_cover(hass, STOVA_BLACKOUT, 60, context=Context())
    await hass.async_block_till_done()
    assert new.manual == {} and new.auto.rt(s).hold["source"] == "preset"
    # The App is fenced out (after the restart its socket is gone too: it comes back with its old credentials).
    if how == "restart":
        back = FakeApp(hass, await admin_ws(), instance=app.instance)
        back.session, back.epoch, back.token, back.seq = app.session, app.epoch, app.token, app.seq
        app = back
    assert (await app.execute("cover", "open_cover", {"entity_id": KAMAR}))["error"]["code"] in S.LOST_CODES
    assert (await app.heartbeat())["error"]["code"] in S.LOST_CODES
    # The windows are evaluated again only after the start-up settle: Kamar (sleep) waits, then closes.
    assert new.auto.rt(k).status["code"] == "startup" and r.condition(k) == "sleep"
    assert covers.calls == []
    await tick(hass, freezer, auto_mod.STARTUP_SETTLE_SECONDS + 3, step=5)
    assert covers.services() == [("set_cover_position", KAMAR, 0)] and r.pos(KAMAR) == 0
    assert new.last[k]["origin"] == "automatic" and new.auto.rt(k).status["action"] in ("moved", "at_target")
    assert r.condition(s) == "manual_override" and r.pos(STOVA_BLACKOUT) == 60   # the hold is kept


async def test_restart_keeps_the_manager_dormant_and_only_the_same_app_resumes(
        hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim())["success"]
    token, epoch = app.token, app.epoch
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    bridge, mgr = bridge_of(entry), mgr_of(entry)
    assert bridge.owner == S.OWNER_APP and bridge.resume_pending and bridge.state() == "resume_pending"
    assert bridge.epoch == epoch and mgr.dormant and mgr.fenced and mgr.locked and not mgr._started  # noqa: SLF001
    assert bridge.status()["manager"] == "dormant"
    r.sleep(True)
    await tick(hass, freezer, 10)
    assert covers.calls == []  # the restarted integration does not take the covers back on its own
    # A wrong token: revoked; the integration takes over.
    wrong = FakeApp(hass, await admin_ws(), instance=app.instance)
    hello = await wrong.connect(resume_token="a" * 32)
    assert hello["lease_token"] is None and hello["ownership"]["owner"] == S.OWNER_INTEGRATION
    assert bridge.history[-1]["reason"] == S.REASON_REVOKED and not mgr_of(entry).dormant
    assert covers.calls == [] and mgr_of(entry).auto.rt(k).status["code"] == "startup"
    # The App takes the curtains again (a reviewed handover), then Home Assistant restarts: the same process
    # resumes with its token.
    await wrong.client.close()
    await hass.async_block_till_done()
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim())["success"]
    token = app.token
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert bridge_of(entry).resume_pending
    again = FakeApp(hass, await admin_ws(), instance=app.instance)
    hello = await again.connect(resume_token=token)
    assert hello["lease_token"] and hello["ownership"]["owner"] == S.OWNER_APP and "state" not in hello
    assert hello["ownership"]["history"][0]["reason"] == S.REASON_RESUMED and hello["renames"] == []
    assert mgr_of(entry).dormant and not bridge_of(entry).resume_pending
    assert (await again.execute("cover", "set_cover_position", {"entity_id": KAMAR, "position": 0}))["success"]
    assert r.pos(KAMAR) == 0
    # A restart after which a different App instance connects: the integration takes over (app_restarted).
    await bridge_of(entry).async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    covers.calls.clear()
    stranger = FakeApp(hass, await admin_ws())
    hello = await stranger.connect(resume_token=token)
    assert hello["ownership"]["owner"] == S.OWNER_INTEGRATION
    assert bridge_of(entry).history[-1]["reason"] == S.REASON_APP_RESTARTED
    assert not mgr_of(entry).dormant and covers.calls == []


async def test_renames_while_the_app_is_away_reach_the_mirror_and_the_fallback(
        hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    bridge = bridge_of(entry)
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim())["success"]
    ent_reg = er.async_get(hass)
    # Connected: the App follows renames itself; the integration touches neither the mirror nor the configuration.
    ent_reg.async_update_entity(STOVA_SHEER, new_entity_id="cover.stova_sheer")
    await hass.async_block_till_done()
    assert not bridge.mirror.get("renames") and STOVA_SHEER in mgr_of(entry).config.windows[s].entity_ids
    bridge.note_rename("cover.a", "cover.b")
    assert not bridge.mirror.get("renames")
    # Away: kept for it.
    await app.client.close()
    await hass.async_block_till_done()
    assert bridge.owner_session is None
    ent_reg.async_update_entity(STOVA_BLACKOUT, new_entity_id="cover.stova_blackout")
    await hass.async_block_till_done()
    assert bridge.mirror["renames"] == [[STOVA_BLACKOUT, "cover.stova_blackout"]]
    assert STOVA_BLACKOUT in mgr_of(entry).config.windows[s].entity_ids  # the dormant configuration is not touched
    # The integration takes the curtains back (lease expiry): the rename is applied to the new manager.
    await tick(hass, freezer, S.LEASE_SECONDS + 3)
    new = mgr_of(entry)
    assert bridge.owner == S.OWNER_INTEGRATION and not new.dormant
    assert "cover.stova_blackout" in new.config.windows[s].entity_ids
    assert STOVA_BLACKOUT not in new.config.windows[s].entity_ids and STOVA_SHEER in new.config.windows[s].entity_ids
    assert bridge.mirror["renames"] == [] and covers.calls == []


async def test_non_admins_cannot_use_the_bridge(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe,
                                                hass_ws_client, hass_read_only_user, caplog) -> None:
    caplog.set_level("CRITICAL", logger=WS_LOGGER)
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/")))
    app = FakeApp(hass, client)
    reply = await app.connect(expect_ok=False)
    assert not reply["success"] and reply["error"]["code"] == "unauthorized"
    for kind in ("status", "return"):
        reply = await app.send(type=f"smart_occupancy/curtains/bridge/{kind}")
        assert reply["error"]["code"] == "unauthorized", kind
    app.session, app.epoch, app.token = "0" * 32, 0, "0" * 32
    reply = await app.execute("cover", "open_cover", {"entity_id": KAMAR})
    assert reply["error"]["code"] == "unauthorized"
    for kind in ("heartbeat", "publish", "release"):
        reply = await app.send(type=f"smart_occupancy/curtains/bridge/{kind}", **app.owner())
        assert reply["error"]["code"] == "unauthorized", kind
    reply = await app.send(type="smart_occupancy/curtains/bridge/claim", session_id="0" * 32, expected_epoch=0,
                           config_revision="x", readiness=readiness())
    assert reply["error"]["code"] == "unauthorized"
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "curtains_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert bridge_of(entry).owner == S.OWNER_INTEGRATION and bridge_of(entry).sessions == {}
    assert covers.calls == [] and r.pos(KAMAR) == 100


async def test_diagnostics_include_the_ownership(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer,
                                                 faroe) -> None:
    entry, r, k, s = await started(hass, pilot, freezer, covers, admin_ws, house)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["curtain_ownership"]["owner"] == S.OWNER_INTEGRATION and diag["curtain_ownership"]["manager"] == "running"
    assert diag["curtains"]["dormant"] is False and diag["curtains"]["fenced"] is False
    app = FakeApp(hass, await admin_ws())
    await app.connect()
    assert (await app.claim())["success"]
    diag = await async_get_config_entry_diagnostics(hass, entry)
    own = diag["curtain_ownership"]
    assert own["owner"] == S.OWNER_APP and own["manager"] == "dormant" and own["epoch"] == 1
    assert own["owner_instance"] == app.instance[:8] and own["sessions"][0]["owner"] is True
    assert own["history"][0]["reason"] == S.REASON_HANDOVER and own["windows"] == 2
    assert diag["curtains"]["dormant"] is True and diag["curtains"]["fenced"] is True
    assert covers.calls == []
