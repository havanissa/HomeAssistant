"""Pairing, claims, fencing, leases, sessions, revocation and restarts of the companion's ownership records."""

from __future__ import annotations

import asyncio

import logging

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.smart_occupancy import protocol as P
from custom_components.smart_occupancy.const import DOMAIN

from .conftest import WS_LOGGER, FakeApp, occupancy_view, setup_companion, system_view, tick, user_ws


async def status(app: FakeApp) -> dict:
    reply = await app.send(type="smart_occupancy/companion/status")
    assert reply["success"], reply
    return reply["result"]


async def test_hello_and_an_unpaired_app_cannot_claim(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    hello = await app.subscribe(token=None)
    assert hello["protocol"] == P.PROTOCOL_VERSION and hello["paired"] is False and hello["trusted"] is False
    assert set(hello["ownership"]) == set(P.DOMAINS)
    assert all(r["owner"] == P.OWNER_NONE and r["state"] == P.STATE_NONE for r in hello["ownership"].values())
    assert hello["legacy"]["present"] is False
    reply = await app.claim("occupancy")
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_PAIRED
    # A wrong protocol is refused before a session exists.
    await app.client.send_json({"id": 999, "type": "smart_occupancy/companion/subscribe", "instance_id": "a" * 32,
                               "protocol": 42, "app_version": "x"})
    reply = await app.client.receive_json()
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_VERSION


async def test_pair_claim_and_fence(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.subscribe(token=None)
    token = await app.pair()
    assert P.valid_id(token)
    grant = await app.claim("lighting")
    assert grant["success"] and grant["result"]["epoch"] == 1 and P.valid_id(grant["result"]["lease_token"])
    own = (await status(app))["ownership"]["lighting"]
    assert own["owner"] == P.OWNER_APP and own["state"] == P.STATE_APP and own["connected"] and own["lease_valid"]
    assert own["targets"] == {"lights": 1, "relays": 0, "actions": 0} and own["config_revision"] == "abc123"
    # The claim is compare-and-swap on the epoch.
    reply = await app.claim("occupancy", expected_epoch=7)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_BUSY
    # Fencing: a stale epoch, a wrong lease, a repeated sequence and a foreign session are refused.
    assert (await app.heartbeat("lighting"))["success"]
    seq = app.seq["lighting"]
    bad = dict(app.owner_params("lighting"))
    for change, code in ((("epoch", 99), P.REJECT_STALE_EPOCH), (("lease_token", "f" * 32), P.REJECT_LEASE),
                         (("seq", seq), P.REJECT_SEQUENCE), (("session_id", "e" * 32), P.REJECT_UNKNOWN_SESSION)):
        params = {**bad, change[0]: change[1]}
        reply = await app.send(type="smart_occupancy/companion/heartbeat", **params)
        assert not reply["success"] and reply["error"]["code"] == code, (change, reply)
    assert (await app.heartbeat("lighting"))["success"]  # the owner itself is unaffected
    assert (await status(app))["ownership"]["lighting"]["rejections"] == {
        P.REJECT_STALE_EPOCH: 1, P.REJECT_LEASE: 1, P.REJECT_SEQUENCE: 1, P.REJECT_UNKNOWN_SESSION: 1}
    # A command domain needs its targets.
    reply = await app.claim("curtains", targets={"covers": ["light.not_a_cover"]})
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_INVALID


async def test_lease_expiry_keeps_the_state_and_refuses_the_old_lease(hass: HomeAssistant, hass_ws_client,
                                                                     freezer, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "lighting")
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    state = hass.states.get("sensor.smart_occupancy_kamar_nordur_state")
    assert state.state == "confirmed_occupancy" and state.attributes["fresh"] is True
    await tick(hass, freezer, P.LEASE_SECONDS + 5)
    own = (await status(app))["ownership"]
    assert own["occupancy"]["state"] == P.STATE_APP_AWAY and own["occupancy"]["owner"] == P.OWNER_APP
    assert own["occupancy"]["history"][0]["reason"] == P.REASON_LEASE_EXPIRED
    # Nothing changed for the home: the last decision stands, marked as not fresh; no vacancy was inferred.
    state = hass.states.get("sensor.smart_occupancy_kamar_nordur_state")
    assert state.state == "confirmed_occupancy" and state.attributes["fresh"] is False
    assert hass.states.get("binary_sensor.smart_occupancy_kamar_nordur_occupied").state == "on"
    reply = await app.publish("occupancy", occupancy_view(state="vacant", occupied=False))
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_LEASE
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state").state == "confirmed_occupancy"
    # The same App claims again: a new epoch, the old lease is history.
    reply = await app.claim("occupancy")
    assert reply["success"] and reply["result"]["epoch"] == 2
    assert (await app.publish("occupancy", occupancy_view(state="vacant", occupied=False)))["success"]
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state").state == "vacant"


async def test_a_new_app_process_claims_with_a_new_epoch_and_the_old_one_is_fenced(hass: HomeAssistant, hass_ws_client,
                                                                                    freezer, caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_companion(hass)
    first = FakeApp(hass, await hass_ws_client(hass))
    await first.start("lighting")
    second = FakeApp(hass, await hass_ws_client(hass))
    second.pairing_token = first.pairing_token  # the same installation: the token is in the App's storage
    await second.subscribe()
    assert second.hello["trusted"] is True
    # While the first session holds a valid lease, the second may not take the domain.
    reply = await second.claim("lighting")
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_BUSY
    # The first process dies: its connection closes; the record is away at once.
    await first.client.close()
    await hass.async_block_till_done()
    own = (await status(second))["ownership"]["lighting"]
    assert own["state"] == P.STATE_APP_AWAY and own["history"][0]["reason"] == P.REASON_SESSION_CLOSED
    reply = await second.claim("lighting")
    assert reply["success"] and reply["result"]["epoch"] == 2
    assert (await second.heartbeat("lighting"))["success"]
    # A late message of the old process (replayed with its old lease) is refused: nothing is executed.
    calls = async_mock_service(hass, "light", "turn_on")
    late = FakeApp(hass, await hass_ws_client(hass), instance=first.instance)
    late.pairing_token = first.pairing_token
    await late.subscribe()
    late.grants["lighting"] = first.grants["lighting"]
    reply = await late.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp"})
    assert not reply["success"] and reply["error"]["code"] in P.LOST_CODES
    assert calls == []


async def test_release_and_revoke(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "lighting", "curtains", "forecast")
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    reply = await app.owner("release", "forecast", detail="test")
    assert reply["success"] and reply["result"]["ownership"]["forecast"]["owner"] == P.OWNER_NONE
    assert reply["result"]["ownership"]["forecast"]["history"][0]["reason"] == P.REASON_RELEASE
    assert (await app.heartbeat("lighting"))["success"]
    # The service fences every domain: the App's commands are refused, the published state stays.
    await hass.services.async_call(DOMAIN, "revoke_app_control", {}, blocking=True)
    await hass.async_block_till_done()
    own = (await status(app))["ownership"]
    assert all(r["owner"] == P.OWNER_NONE for r in own.values())
    assert own["lighting"]["history"][0]["reason"] == P.REASON_REVOKED and own["lighting"]["epoch"] == 2
    reply = await app.heartbeat("lighting")
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_OWNER
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state").state == "confirmed_occupancy"
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state").attributes["fresh"] is False
    events = await app.drain()
    assert any(e.get("type") == "ownership" for e in events)
    # Still paired: the App claims again on its own (a revoke of one domain only).
    assert (await app.claim("lighting"))["success"]
    reply = await app.send(type="smart_occupancy/companion/revoke", domain="lighting")
    assert reply["success"] and reply["result"]["revoked"] == ["lighting"]
    # Unpair: no claim until an administrator pairs again.
    reply = await app.send(type="smart_occupancy/companion/unpair")
    assert reply["success"]
    reply = await app.claim("occupancy")
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_PAIRED


async def test_restart_keeps_the_designation_but_never_a_lease(hass: HomeAssistant, hass_ws_client, areas) -> None:
    entry = await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "lighting")
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The entities show the last published decision, not fresh; the record says the App is away.
    state = hass.states.get("sensor.smart_occupancy_kamar_nordur_state")
    assert state.state == "confirmed_occupancy" and state.attributes["fresh"] is False
    again = FakeApp(hass, await hass_ws_client(hass), instance=app.instance)
    again.pairing_token = app.pairing_token
    hello = await again.subscribe()
    assert hello["trusted"] is True and hello["ownership"]["lighting"]["state"] == P.STATE_APP_AWAY
    assert hello["ownership"]["lighting"]["history"][0]["reason"] == P.REASON_RESTART
    again.grants = app.grants  # the old lease tokens
    reply = await again.heartbeat("lighting")
    assert not reply["success"] and reply["error"]["code"] in P.LOST_CODES
    reply = await again.claim("lighting")
    assert reply["success"] and reply["result"]["epoch"] == 2


async def test_non_admin_cannot_use_the_bridge(hass: HomeAssistant, hass_ws_client, hass_read_only_user,
                                               caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    await setup_companion(hass)
    app = FakeApp(hass, await user_ws(hass, hass_ws_client, hass_read_only_user))
    for msg in ({"type": "smart_occupancy/companion/status"},
                {"type": "smart_occupancy/companion/subscribe", "instance_id": "a" * 32, "protocol": 1,
                 "app_version": "x"},
                {"type": "smart_occupancy/companion/revoke"},
                {"type": "smart_occupancy/companion/legacy/export"},
                {"type": "smart_occupancy/companion/action", "kind": "room/resume", "params": {}}):
        reply = await app.send(**msg)
        assert not reply["success"] and reply["error"]["code"] == "unauthorized", msg
    # The read-only view is for everyone (the dashboard cards).
    reply = await app.send(type="smart_occupancy/companion/view")
    assert reply["success"] and reply["result"]["owners"]["lighting"] == P.STATE_NONE


async def test_pairing_again_revokes_the_previous_apps_leases(hass: HomeAssistant, hass_ws_client, areas) -> None:
    """A new App process pairs (new token): the App paired with the old token loses every lease at once."""
    await setup_companion(hass)
    old = FakeApp(hass, await hass_ws_client(hass))
    await old.start("lighting", "occupancy")
    new = FakeApp(hass, await hass_ws_client(hass))
    await new.subscribe(token=None)
    assert (await new.claim("lighting", targets={"lights": ["light.a"]}))["success"] is False  # not paired yet
    await new.pair()
    ownership = (await new.send(type="smart_occupancy/companion/status"))["result"]["ownership"]
    assert all(ownership[d]["owner"] == P.OWNER_NONE for d in ("lighting", "occupancy"))
    assert ownership["lighting"]["history"][0]["reason"] == P.REASON_PAIRED
    # The old App's lease is dead: its commands are refused; the new App claims with the next epoch.
    refused = await old.execute("lighting", "light", "turn_on", {"entity_id": "light.a"})
    assert not refused["success"] and refused["error"]["code"] in (P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH)
    claimed = await new.claim("lighting", targets={"lights": ["light.a"]})
    assert claimed["success"] and claimed["result"]["epoch"] == ownership["lighting"]["epoch"] + 1


async def test_only_the_session_an_action_was_sent_to_may_answer_it(hass: HomeAssistant, hass_ws_client, areas) -> None:
    await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    other = FakeApp(hass, await hass_ws_client(hass))
    await other.subscribe()
    card = FakeApp(hass, await hass_ws_client(hass))
    task = asyncio.ensure_future(card.send(type="smart_occupancy/companion/action", kind="room/resume",
                                           params={"area_id": "kamar_nordur"}))
    action = await app.event("action")
    stolen = await other.send(type="smart_occupancy/companion/action_result", session_id=other.session,
                              action_id=action["action_id"], ok=True, result={"stolen": True})
    assert stolen["success"] and stolen["result"]["accepted"] is False
    answered = await app.send(type="smart_occupancy/companion/action_result", session_id=app.session,
                              action_id=action["action_id"], ok=True, result={"resumed": True})
    assert answered["result"]["accepted"] is True
    assert (await task)["result"] == {"resumed": True}
