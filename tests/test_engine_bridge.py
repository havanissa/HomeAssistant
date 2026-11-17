"""Phase A3.2: occupancy ownership, the App bridge, fencing, fallback and rollback (integration side).

A simulated App (``FakeApp``) speaks the bridge protocol over a real websocket
connection and computes its results with the shared engine (``House``), as the
App does. The tests prove:

* the integration is the owner by default (also after a restart) and the App
  is shadowed with the integration engine's exact state;
* a reviewed handover needs a matching engine, configuration and readiness,
  is compare-and-swap on the epoch, and changes no published decision;
* the App's accepted results are what every consumer reads: entities,
  ``result()``, lighting (a light is switched on from an App result);
* stale epochs, wrong leases, duplicate or reordered sequences, a wrong
  configuration revision, malformed results and foreign sessions are refused
  and change nothing; exactly one engine is authoritative at any time;
* lease expiry, an App restart, release and the admin service fall back to the
  integration engine without vacancy, without duplicate transitions and
  without a light command - during occupancy, possible occupancy and a vacancy
  countdown; a recovered App does not take ownership back;
* Home Assistant restarts and integration reloads keep a reviewed handover only
  for the same App process (resume), and fall back otherwise;
* non-administrators cannot use any bridge command or the service.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import secrets
from typing import Any

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.smart_occupancy.const import DOMAIN, ENGINE_STORE_KEY
from custom_components.smart_occupancy.occupancy_core import CORE_VERSION, fingerprint
from custom_components.smart_occupancy.occupancy_core import protocol as P
from custom_components.smart_occupancy.occupancy_core.house import House, Observation
from custom_components.smart_occupancy.occupancy_core.wire import result_to_wire, spec_from_wire
from custom_components.smart_occupancy.presence_log import presence_log

from .conftest import Pilot, pilot_options, setup_entry
from .test_lighting_runtime import WS_LOGGER, Calls, Lit, activate, calls, ctrl_of, lit, options  # noqa: F401

SUB = "smart_occupancy/engine/subscribe"


async def tick(hass: HomeAssistant, freezer, seconds: float, step: float = 1.0) -> None:
    remaining = seconds
    while remaining > 0:
        delta = min(step, remaining)
        freezer.tick(timedelta(seconds=delta))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        remaining -= delta


async def alive(hass: HomeAssistant, freezer, fake: FakeApp, seconds: float) -> None:
    """Let time pass while the App keeps its lease (a heartbeat every 10 s)."""
    while seconds > 0:
        step = min(P.HEARTBEAT_SECONDS, seconds)
        await tick(hass, freezer, step)
        assert (await fake.heartbeat())["success"]
        seconds -= step


def readiness(ready: bool = True) -> dict[str, Any]:
    return {"ready": ready, "checks": [{"key": "parity", "status": "ok" if ready else "error"}],
            "shadow_seconds": 7200, "transitions": 25, "disagreements": 0}


class FakeApp:
    """The App's side of the bridge: one websocket connection, one shared-engine House."""

    def __init__(self, hass: HomeAssistant, client: Any, instance: str | None = None) -> None:
        self.hass = hass
        self.client = client
        self.instance = instance or secrets.token_hex(16)
        self._id = 100
        self.sub_id: int | None = None
        self.events: list[dict[str, Any]] = []
        self.session: str | None = None
        self.house: House | None = None
        self.revision: str | None = None
        self.epoch: int | None = None
        self.token: str | None = None
        self.seq = 0
        self.hello: dict[str, Any] | None = None

    async def _send(self, **msg: Any) -> dict[str, Any]:
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
        """Read every event sent so far: a ping is answered after them (messages arrive in order)."""
        await self.hass.async_block_till_done()
        await self._send(type="ping")
        return self.events

    async def connect(self, resume_token: str | None = None, *, fp: str | None = None, protocol: int = 1,
                      expect_ok: bool = True) -> dict[str, Any]:
        self._id += 1
        self.sub_id = self._id
        await self.client.send_json({"id": self._id, "type": SUB, "instance_id": self.instance, "protocol": protocol,
                                     "app_version": "0.4.0", "core_version": CORE_VERSION,
                                     "fingerprint": fp or fingerprint(), "resume_token": resume_token})
        reply = await self.client.receive_json()
        if not expect_ok:
            assert not reply["success"]
            return reply
        assert reply["success"], reply
        hello = (await self.client.receive_json())["event"]
        assert hello["type"] == "hello"
        self.hello = hello
        self.session = hello["session_id"]
        self.revision = hello["spec"]["revision"]
        self.house = House(spec_from_wire(hello["spec"]))
        for area_id, engine in self.house.engines.items():
            engine.import_state(hello["state"][area_id])
        if hello.get("lease_token"):
            self.token, self.epoch, self.seq = hello["lease_token"], hello["ownership"]["epoch"], 0
        return hello

    async def claim(self, *, expected_epoch: int | None = None, ready: bool = True, revision: str | None = None):
        status = await self.status()
        reply = await self._send(type="smart_occupancy/engine/claim", session_id=self.session,
                                 expected_epoch=status["epoch"] if expected_epoch is None else expected_epoch,
                                 config_revision=revision or self.revision, readiness=readiness(ready))
        if reply["success"]:
            grant = reply["result"]
            self.epoch, self.token, self.seq = grant["epoch"], grant["lease_token"], 0
            for area_id, engine in self.house.engines.items():
                engine.import_state(grant["state"][area_id])
        return reply

    async def status(self) -> dict[str, Any]:
        reply = await self._send(type="smart_occupancy/engine/status")
        assert reply["success"], reply
        return reply["result"]

    def observe(self, entity_id: str, state: str) -> list[str]:
        st = self.hass.states.get(entity_id)
        now = st.last_changed
        return self.house.observe(entity_id, Observation(state, now, entity_id), now)

    def results(self, now=None) -> dict[str, Any]:
        from homeassistant.util import dt as dt_util
        now = now or dt_util.utcnow()
        out = {}
        for area_id, engine in self.house.engines.items():
            result = engine.evaluate(now)
            out[area_id] = result_to_wire(result, engine.visit_confirmed)
        return out

    async def publish(self, results: dict[str, Any] | None = None, **override: Any) -> dict[str, Any]:
        self.seq += 1
        if "results" in override:
            results = override.pop("results")
        msg = {"type": "smart_occupancy/engine/publish", "session_id": self.session, "epoch": self.epoch,
               "lease_token": self.token, "seq": self.seq, "config_revision": self.revision,
               "results": self.results() if results is None else results}
        msg.update(override)
        return await self._send(**msg)

    async def heartbeat(self, **override: Any) -> dict[str, Any]:
        self.seq += 1
        msg = {"type": "smart_occupancy/engine/heartbeat", "session_id": self.session, "epoch": self.epoch,
               "lease_token": self.token, "seq": self.seq, "config_revision": self.revision}
        msg.update(override)
        return await self._send(**msg)

    async def release(self, **extra: Any) -> dict[str, Any]:
        return await self._send(type="smart_occupancy/engine/release", **extra)


async def app(hass: HomeAssistant, hass_ws_client, **kw) -> FakeApp:
    return FakeApp(hass, await hass_ws_client(hass), **kw)


def room_state(hass: HomeAssistant, area: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{area}_state")
    return hass.states.get(entity_id).state


def reason_attrs(hass: HomeAssistant, area: str) -> dict[str, Any]:
    entity_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{area}_reason")
    return dict(hass.states.get(entity_id).attributes)


async def occupied_room(hass, pilot: Pilot, freezer) -> None:
    hass.states.async_set(pilot.pir, "on")
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await tick(hass, freezer, 2)


async def handover(hass, hass_ws_client, entry, freezer) -> FakeApp:
    fake = await app(hass, hass_ws_client)
    await fake.connect()
    reply = await fake.claim()
    assert reply["success"], reply
    reply = await fake.publish()
    assert reply["success"], reply
    await hass.async_block_till_done()
    return fake


# ------------------------------------------------------------------ default
async def test_integration_owns_by_default_and_shadow_gets_exact_state(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    await occupied_room(hass, pilot, freezer)
    fake = await app(hass, hass_ws_client)
    hello = await fake.connect()
    own = hello["ownership"]
    assert own["owner"] == "integration" and own["epoch"] == 0 and own["fingerprint"] == fingerprint()
    assert own["core_version"] == CORE_VERSION and own["protocol"] == P.PROTOCOL_VERSION
    assert hello["spec"]["revision"] == manager.spec_revision and hello["lease_token"] is None
    # The shadow engine starts as an exact copy: it evaluates exactly like the integration's.
    from homeassistant.util import dt as dt_util
    now = dt_util.utcnow()
    for area_id, engine in fake.house.engines.items():
        mine = manager.engines[area_id]
        assert engine.export_state() == mine.export_state()
        assert result_to_wire(engine.evaluate(now), engine.visit_confirmed) == \
            result_to_wire(mine.evaluate(now), mine.visit_confirmed)
    # Live results and inputs are streamed.
    hass.states.async_set(pilot.radar, "off")
    await hass.async_block_till_done()
    manager.async_set_holds(pilot.area, frozenset({"Desk"}))
    await fake.drain()
    kinds = [(e["type"], e.get("kind")) for e in fake.events]
    assert ("result", None) in kinds and ("input", "hold") in kinds
    assert reason_attrs(hass, pilot.area)["decided_by"] == "integration"
    # Shadow sessions never publish: a publish without ownership is refused.
    fake.epoch, fake.token = 0, secrets.token_hex(16)
    reply = await fake.publish()
    assert reply["error"]["code"] == P.REJECT_NOT_OWNER


# ----------------------------------------------------------------- handover
async def test_reviewed_handover_and_app_results_are_published(
    hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, options(lit))
    await activate(hass, entry, lit, freezer)  # Automatic lighting; vacant: nothing sent
    calls.clear()
    manager = entry.runtime_data.occupancy
    fake = await app(hass, hass_ws_client)
    await fake.connect()
    # Not ready, wrong revision, wrong epoch: refused, nothing changes.
    assert (await fake.claim(ready=False))["error"]["code"] == P.REJECT_NOT_READY
    assert (await fake.claim(revision="0" * 16))["error"]["code"] == P.REJECT_CONFIG
    assert (await fake.claim(expected_epoch=7))["error"]["code"] == P.REJECT_BUSY
    assert manager.bridge.owner == "integration" and manager.bridge.epoch == 0
    reply = await fake.claim()
    assert reply["success"], reply
    assert manager.bridge.owner == "app" and manager.bridge.epoch == 1
    assert (await fake.claim())["error"]["code"] == P.REJECT_BUSY  # already owned
    reply = await fake.publish()
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert calls.all == []  # the handover itself changes no decision and sends nothing
    assert reason_attrs(hass, lit.area)["decided_by"] == "app"
    history = manager.bridge.history
    assert history[-1]["reason"] == P.REASON_HANDOVER and history[-1]["readiness"]["ready"] is True
    assert any(e["kind"] == "engine" for e in presence_log(hass).entries)

    # Motion: the App decides; the integration publishes the App's result, and lighting reacts to it.
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await hass.async_block_till_done()
    local = manager.engines[lit.area].result
    fake.observe(lit.pir, "on")
    fake.observe(lit.radar, "on")
    reply = await fake.publish()
    assert reply["success"], reply
    await hass.async_block_till_done()
    assert manager.result(lit.area) is not local and manager.published_by(lit.area) == "app"
    assert manager.result(lit.area).occupied is True
    assert room_state(hass, lit.area) in ("possible_occupancy", "confirmed_occupancy")
    await tick(hass, freezer, 2)
    assert calls.on, "lighting reacts to the App's published occupancy"


async def test_fencing_refuses_stale_duplicate_and_malformed(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer, caplog
) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    accepted = manager.bridge.accepted_total
    good = fake.results()
    room = good[pilot.area]
    bad_cases = {
        P.REJECT_STALE_EPOCH: {"epoch": fake.epoch - 1},
        P.REJECT_LEASE: {"lease_token": secrets.token_hex(16)},
        P.REJECT_SEQUENCE: {"seq": 1},  # a duplicate (already used)
        P.REJECT_CONFIG: {"config_revision": "f" * 16},
        P.REJECT_UNKNOWN_SESSION: {"session_id": secrets.token_hex(16)},
    }
    for code, override in bad_cases.items():
        before = manager.result(pilot.area)
        reply = await fake.publish(override.pop("results", good), **override)
        assert not reply["success"] and reply["error"]["code"] == code, (code, reply)
        assert manager.result(pilot.area) is before
    assert manager.bridge.accepted_total == accepted
    # Out of order: a lower sequence after a higher one.
    assert (await fake.publish(good, seq=50))["success"]
    assert (await fake.publish(good, seq=49))["error"]["code"] == P.REJECT_SEQUENCE
    # Another connection cannot use the owner's session, even with every secret.
    other = await app(hass, hass_ws_client, instance=fake.instance)
    other.session, other.epoch, other.token, other.revision, other.seq = (
        fake.session, fake.epoch, fake.token, fake.revision, 1000)
    other.sub_id = -1
    assert (await other.publish(good))["error"]["code"] == P.REJECT_UNKNOWN_SESSION
    assert manager.bridge.owner == "app"
    assert manager.bridge.rejections[P.REJECT_SEQUENCE] >= 2


async def test_invalid_results_from_the_owner_fall_back(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer, caplog
) -> None:
    """Results the integration cannot use would freeze the rooms while heartbeats keep the lease: take over."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    await occupied_room(hass, pilot, freezer)
    fake = await handover(hass, hass_ws_client, entry, freezer)
    good = fake.results()
    reply = await fake.publish({pilot.area: {**good[pilot.area], "state": "party"}})
    assert reply["error"]["code"] == P.REJECT_INVALID
    assert manager.bridge.owner == "integration" and manager.bridge.history[-1]["reason"] == P.REASON_REVOKED
    assert manager.result(pilot.area).occupied is True
    assert (await fake.heartbeat())["error"]["code"] in (P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH)
    # Unknown rooms are invalid too (and a non-owner's invalid message changes nothing).
    other = await handover(hass, hass_ws_client, entry, freezer)
    assert (await other.publish({"unknown_room": good[pilot.area]}))["error"]["code"] == P.REJECT_INVALID
    assert manager.bridge.owner == "integration"


async def test_resume_keeps_what_the_app_published(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                   freezer) -> None:
    """The copy a resuming App starts from is reconciled with its accepted decisions (no vacancy by resuming)."""
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    from homeassistant.util import dt as dt_util
    now = dt_util.utcnow()
    fake.house.observe(pilot.radar, Observation("on", now, pilot.radar), now)
    fake.house.observe(pilot.pir, Observation("on", now, pilot.pir), now)
    assert (await fake.publish())["success"]
    assert manager.engines[pilot.area].occupied is False  # the standby did not see it
    token, instance = fake.token, fake.instance
    await fake.client.close()
    await hass.async_block_till_done()
    back = await app(hass, hass_ws_client, instance=instance)
    hello = await back.connect(resume_token=token)
    assert hello["ownership"]["owner"] == "app"
    assert hello["state"][pilot.area]["occupied"] is True
    assert back.house.engines[pilot.area].evaluate(dt_util.utcnow()).occupied is True
    assert manager.result(pilot.area).occupied is True


async def test_decisions_of_an_earlier_owner_are_dropped(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                         freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    from homeassistant.util import dt as dt_util
    now = dt_util.utcnow()
    fake.house.observe(pilot.radar, Observation("on", now, pilot.radar), now)
    assert (await fake.publish())["success"]
    assert manager.bridge.decisions
    assert (await fake.release())["result"]["released"]
    assert manager.bridge.decisions == {}


async def test_exactly_one_authoritative_engine(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    """Whatever the App sends and whenever the ownership changes, result() comes from exactly the owner."""
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    assert manager.published_by(pilot.area) == "app"
    epoch = fake.epoch
    assert (await fake.release())["result"]["released"] is True
    assert manager.published_by(pilot.area) == "integration" and manager.bridge.epoch == epoch + 1
    # The previous owner's in-flight messages are fenced out.
    assert (await fake.publish())["error"]["code"] == P.REJECT_NOT_OWNER
    assert (await fake.heartbeat())["error"]["code"] == P.REJECT_NOT_OWNER
    assert manager.result(pilot.area) is manager.engines[pilot.area].result


async def test_concurrent_claims_only_one_wins(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    first, second = await app(hass, hass_ws_client), await app(hass, hass_ws_client)
    await first.connect()
    await second.connect()
    a, b = await asyncio.gather(first.claim(expected_epoch=0), second.claim(expected_epoch=0))
    assert sorted([a["success"], b["success"]]) == [False, True]
    loser = a if not a["success"] else b
    assert loser["error"]["code"] == P.REJECT_BUSY
    assert entry.runtime_data.occupancy.bridge.epoch == 1


async def test_version_and_protocol_mismatch_refused(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                     caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_entry(hass, pilot_options(pilot))
    fake = await app(hass, hass_ws_client)
    reply = await fake.connect(protocol=2, expect_ok=False)
    assert reply["error"]["code"] == P.REJECT_VERSION
    fake = await app(hass, hass_ws_client)
    await fake.connect(fp="0" * 64)
    assert (await fake.claim())["error"]["code"] == P.REJECT_VERSION


# ----------------------------------------------------------------- fallback
@pytest.mark.parametrize("phase", ["occupied", "possible", "countdown"])
async def test_lease_expiry_falls_back_without_vacancy_or_commands(
    hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client, freezer, phase: str
) -> None:
    entry = await setup_entry(hass, options(lit))
    await activate(hass, entry, lit, freezer)
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    # The App sees a visit; the integration's standby engine misses it (it never saw these events, e.g.
    # the App had an event the integration did not): the published result is the App's.
    from homeassistant.util import dt as dt_util
    now = dt_util.utcnow()
    pir = Observation("on", now, lit.pir)
    fake.house.observe(lit.pir, pir, now)
    if phase != "possible":
        fake.house.observe(lit.radar, Observation("on", now, lit.radar), now)
    assert (await fake.publish())["success"]
    await tick(hass, freezer, 2)
    if phase == "countdown":
        later = dt_util.utcnow()
        for e in (lit.pir, lit.radar):
            fake.house.observe(e, Observation("off", later, e), later)
        await alive(hass, freezer, fake, 40)  # past the dropout grace
        assert (await fake.publish())["success"]
        assert manager.result(lit.area).state.value == "possible_vacancy"
    published = manager.result(lit.area)
    assert published.occupied is True and manager.engines[lit.area].occupied is False
    calls.clear()
    transitions_before = sum(1 for e in presence_log(hass).entries if e["kind"] == "transition")
    # The App stops answering: the lease expires, the integration takes over.
    await tick(hass, freezer, P.LEASE_SECONDS + 2)
    assert manager.bridge.owner == "integration"
    assert manager.bridge.history[-1]["reason"] == P.REASON_LEASE_EXPIRED
    result = manager.result(lit.area)
    assert result is manager.engines[lit.area].result
    assert result.occupied is True, "communication loss never causes vacancy"
    assert result.vacancy_at is None or result.vacancy_at > dt_util.utcnow() + timedelta(seconds=250)
    assert calls.off == [], "no light command caused by the ownership change"
    # No duplicate transition: the published state stays occupied across the fallback.
    new_transitions = [e for e in presence_log(hass).entries if e["kind"] == "transition"][transitions_before:]
    assert all(e.get("occupied") is not False for e in new_transitions)
    # The recovered App does not take ownership back: its old session is fenced out.
    assert (await fake.heartbeat())["error"]["code"] in (P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH)
    assert manager.bridge.owner == "integration"


async def test_heartbeats_keep_the_lease(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    for _ in range(6):
        await tick(hass, freezer, P.HEARTBEAT_SECONDS)
        assert (await fake.heartbeat())["success"]
    assert manager.bridge.owner == "app"  # 60 s, longer than one lease


async def test_app_restart_falls_back_and_new_instance_stays_shadow(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    await occupied_room(hass, pilot, freezer)
    old = await handover(hass, hass_ws_client, entry, freezer)
    epoch = manager.bridge.epoch
    restarted = await app(hass, hass_ws_client)  # a new App process: a new instance id, no lease token
    hello = await restarted.connect()
    assert manager.bridge.owner == "integration" and manager.bridge.epoch == epoch + 1
    assert manager.bridge.history[-1]["reason"] == P.REASON_APP_RESTARTED
    assert hello["ownership"]["owner"] == "integration" and hello["lease_token"] is None
    assert manager.result(pilot.area).occupied is True
    assert (await old.publish())["error"]["code"] in (P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH)


async def test_same_process_reconnect_resumes_within_lease(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    token, instance = fake.token, fake.instance
    await fake.client.close()  # a network interruption
    await hass.async_block_till_done()
    assert manager.bridge.owner == "app" and not manager.bridge.status()["owner_connected"]
    back = await app(hass, hass_ws_client, instance=instance)
    hello = await back.connect(resume_token=token)
    assert hello["lease_token"] and hello["ownership"]["owner"] == "app"
    assert manager.bridge.history[-1]["reason"] == P.REASON_RESUMED
    assert (await back.publish())["success"]
    # A wrong token from the same instance cannot resume: the integration takes over instead.
    await back.client.close()
    await hass.async_block_till_done()
    thief = await app(hass, hass_ws_client, instance=instance)
    await thief.connect(resume_token=secrets.token_hex(16))
    assert manager.bridge.owner == "integration" and manager.bridge.history[-1]["reason"] == P.REASON_REVOKED


async def test_disconnect_without_resume_expires(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    await fake.client.close()
    await tick(hass, freezer, P.LEASE_SECONDS + 2)
    assert manager.bridge.owner == "integration" and manager.bridge.history[-1]["reason"] == P.REASON_LEASE_EXPIRED


# -------------------------------------------------------- restart / reload
async def test_reload_resumes_same_process_or_falls_back(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    await occupied_room(hass, pilot, freezer)
    fake = await handover(hass, hass_ws_client, entry, freezer)
    token = fake.token
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await fake.drain()
    assert any(e["type"] == "closed" for e in fake.events)
    manager = entry.runtime_data.occupancy
    assert manager.bridge.owner == "app" and manager.bridge.state() == "resume_pending"
    assert manager.published_by(pilot.area) == "integration"  # nothing accepted from the App yet
    assert manager.result(pilot.area).occupied is True
    again = await app(hass, hass_ws_client, instance=fake.instance)
    hello = await again.connect(resume_token=token)
    assert hello["ownership"]["owner"] == "app" and hello["ownership"]["state"] == "app"
    assert (await again.publish())["success"]
    assert manager.published_by(pilot.area) == "app"

    # Reload again, and this time the App does not come back: fallback after the grace period.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data.occupancy
    await tick(hass, freezer, P.RESUME_GRACE_SECONDS + 2, step=5)
    assert manager.bridge.owner == "integration"
    assert manager.bridge.history[-1]["reason"] == P.REASON_NOT_RESUMED
    assert manager.result(pilot.area).occupied is True


async def test_restart_keeps_app_occupied_decision(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer,
                                                   hass_storage) -> None:
    """The App had a room occupied that the integration engine did not: after a restart it stays occupied."""
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    fake = await handover(hass, hass_ws_client, entry, freezer)
    from homeassistant.util import dt as dt_util
    now = dt_util.utcnow()
    fake.house.observe(pilot.radar, Observation("on", now, pilot.radar), now)
    fake.house.observe(pilot.pir, Observation("on", now, pilot.pir), now)
    assert (await fake.publish())["success"]
    assert manager.result(pilot.area).occupied is True and manager.engines[pilot.area].occupied is False
    assert await hass.config_entries.async_unload(entry.entry_id)  # flushes both stores
    await hass.async_block_till_done()
    stored = hass_storage[ENGINE_STORE_KEY]["data"]
    assert stored["owner"] == "app" and stored["decisions"][pilot.area]["occupied"] is True
    assert "lease_token" not in str(stored) and stored["lease_hash"] != fake.token
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = entry.runtime_data.occupancy
    assert manager.bridge.state() == "resume_pending"
    assert manager.result(pilot.area).occupied is True


async def test_damaged_ownership_record_falls_back_safely(hass: HomeAssistant, pilot: Pilot, hass_storage) -> None:
    hass_storage[ENGINE_STORE_KEY] = {"version": 1, "minor_version": 1, "key": ENGINE_STORE_KEY,
                                      "data": {"owner": "app", "epoch": "seven", "owner_instance": "x"}}
    entry = await setup_entry(hass, pilot_options(pilot))
    bridge = entry.runtime_data.occupancy.bridge
    assert bridge.owner == "integration" and bridge.state() == "integration"
    hass_storage[ENGINE_STORE_KEY] = {"version": 1, "minor_version": 1, "key": ENGINE_STORE_KEY,
                                      "data": {"owner": "app", "epoch": 4, "owner_instance": "nothex",
                                               "lease_hash": "z"}}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.occupancy.bridge.owner == "integration"


# ------------------------------------------------------------------ rollback
async def test_service_returns_control_without_the_app(hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer,
                                                       hass_admin_user, hass_read_only_user, caplog) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    await occupied_room(hass, pilot, freezer)
    await handover(hass, hass_ws_client, entry, freezer)
    from homeassistant.core import Context
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "engine_return_to_integration", {}, blocking=True,
                                       context=Context(user_id=hass_read_only_user.id))
    assert manager.bridge.owner == "app"
    await hass.services.async_call(DOMAIN, "engine_return_to_integration", {}, blocking=True,
                                   context=Context(user_id=hass_admin_user.id))
    assert manager.bridge.owner == "integration"
    assert manager.bridge.history[-1]["reason"] == P.REASON_RELEASE
    assert manager.bridge.history[-1]["by"] == hass_admin_user.id
    assert manager.result(pilot.area).occupied is True
    # Releasing again is harmless.
    await hass.services.async_call(DOMAIN, "engine_return_to_integration", {}, blocking=True,
                                   context=Context(user_id=hass_admin_user.id))


async def test_release_during_countdown_keeps_deadline_when_engines_agree(
    hass: HomeAssistant, pilot: Pilot, hass_ws_client, freezer
) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    manager = entry.runtime_data.occupancy
    await occupied_room(hass, pilot, freezer)
    fake = await handover(hass, hass_ws_client, entry, freezer)
    for e in (pilot.pir, pilot.radar):
        hass.states.async_set(e, "off")
    await hass.async_block_till_done()
    for e in (pilot.pir, pilot.radar):
        fake.observe(e, "off")
    await alive(hass, freezer, fake, 40)
    assert (await fake.publish())["success"]
    app_result = manager.result(pilot.area)
    assert app_result.state.value == "possible_vacancy"
    deadline = app_result.vacancy_at
    assert (await fake.release())["result"]["released"]
    local = manager.result(pilot.area)
    assert local.state.value == "possible_vacancy"
    assert abs((local.vacancy_at - deadline).total_seconds()) < 2  # the same countdown continues


async def test_non_admin_cannot_use_the_bridge(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                               hass_read_only_user, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_entry(hass, pilot_options(pilot))
    refresh = await hass.auth.async_create_refresh_token(hass_read_only_user, "https://example.com/app")
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    fake = FakeApp(hass, client)
    for msg in ({"type": "smart_occupancy/engine/status"},
                {"type": SUB, "instance_id": fake.instance, "protocol": 1, "app_version": "x", "core_version": "x",
                 "fingerprint": "0" * 64},
                {"type": "smart_occupancy/engine/release"}):
        reply = await fake._send(**msg)  # noqa: SLF001
        assert not reply["success"] and reply["error"]["code"] == "unauthorized"
