"""The publishing bridge's security: who may publish, fencing, freshness of the lease, strict validation."""

from __future__ import annotations

import pytest

from homeassistant.core import HomeAssistant

from custom_components.smart_occupancy_detection import protocol as P
from custom_components.smart_occupancy_detection.const import DOMAIN

from .conftest import KAMAR, WS_LOGGER, App, setup_sod, view


async def test_protocol_copy_matches_the_app():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / "custom_components" / DOMAIN / "protocol.py").read_bytes() == \
        (root / "smart_occupancy_detection" / "backend" / "sod" / "protocol.py").read_bytes()


async def test_only_administrators_may_use_the_bridge(hass: HomeAssistant, ws, areas, hass_read_only_access_token, caplog):
    await setup_sod(hass)
    caplog.set_level("CRITICAL", logger=WS_LOGGER)
    reader = await ws(access_token=hass_read_only_access_token)
    for command, params in (("session", {"protocol": 1, "app_version": "0.1.0", "instance_id": "a" * 32}),
                            ("status", {}), ("claim", {"session_id": "a" * 32})):
        msg = await reader.raw(f"{DOMAIN}/{command}", **params)
        assert not msg["success"] and msg["error"]["code"] == "unauthorized"


async def test_a_session_needs_pairing_before_it_can_publish(hass, ws, areas):
    await setup_sod(hass)
    a = App(await ws())
    hello = await a.open()
    assert hello["paired"] is False and hello["trusted"] is False
    assert await a.ws.refused("claim", session_id=a.session_id) == P.REJECT_NOT_PAIRED
    a.epoch, a.lease = 1, "f" * 32
    assert await a.publish_refused({KAMAR: view()}) == P.REJECT_NOT_OWNER
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied") is None


async def test_wrong_protocol_or_ids_are_refused(hass, ws, areas):
    await setup_sod(hass)
    client = await ws()
    msg = await client.raw(f"{DOMAIN}/session", protocol=2, app_version="0.1.0", instance_id="a" * 32)
    assert msg["error"]["code"] == P.REJECT_VERSION
    msg = await client.raw(f"{DOMAIN}/session", protocol=1, app_version="0.1.0", instance_id="not-hex")
    assert msg["error"]["code"] == P.REJECT_INVALID


async def test_fencing(hass, app, ws):
    await app.claim()  # the same session may claim again: a new epoch (2)
    await app.publish({KAMAR: view(confidence=70)})
    await app.publish({KAMAR: view()})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"
    good = app.seq
    # Duplicate and out-of-order sequence numbers.
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}, seq=good) == P.REJECT_SEQUENCE
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}, seq=good - 1) == P.REJECT_SEQUENCE
    # A wrong lease token, an old epoch, an unknown session.
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}, lease_token="0" * 32) == P.REJECT_LEASE
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}, epoch=app.epoch - 1) == P.REJECT_STALE_EPOCH
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}, session_id="b" * 32) == P.REJECT_UNKNOWN_SESSION
    # The right session id on another connection (a stolen id) is refused too.
    thief = App(await ws())
    assert await thief.ws.refused("publish", session_id=app.session_id, epoch=app.epoch, lease_token=app.lease,
                                  seq=app.seq + 100, rooms={KAMAR: view(state="vacant", occupied=False)}) \
        == P.REJECT_UNKNOWN_SESSION
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"  # nothing of that was applied
    status = await app.ws.call("status")
    assert status["refused"][P.REJECT_SEQUENCE] == 2 and status["fresh"]


@pytest.mark.parametrize("bad", [
    {"bad room id!": view()},
    {KAMAR: view(state="occupied")},
    {KAMAR: view(state="vacant", occupied=True)},
    {KAMAR: view(state="possible_vacancy", occupied=False)},
    {KAMAR: view(confidence=101)},
    {KAMAR: view(confidence=True)},
    {KAMAR: view(reason="x" * 300)},
    {KAMAR: view(last_activity="yesterday")},
    {KAMAR: view(last_activity="2026-11-21T11:59:00")},
    {KAMAR: view(brightness=50)},
    {KAMAR: {"name": "Kamar"}},
    {KAMAR: view(name="")},
])
async def test_invalid_views_are_refused_and_change_nothing(hass, app, bad):
    await app.publish({KAMAR: view()})
    seq = app.seq
    assert await app.publish_refused(bad) == P.REJECT_INVALID
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"
    app.seq = seq  # the refused message did not move the companion's sequence
    await app.publish({KAMAR: view(state="vacant", occupied=False)})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"


async def test_a_second_session_cannot_take_a_valid_lease(hass, app, ws):
    other = App(await ws(), token=app.token)  # another process of the same paired App
    assert (await other.open())["trusted"]
    assert await other.ws.refused("claim", session_id=other.session_id) == P.REJECT_BUSY


async def test_pairing_again_fences_the_previous_app(hass, app, ws):
    await app.publish({KAMAR: view()})
    new = App(await ws())
    hello = await new.open()
    assert hello["paired"] and not hello["trusted"]
    assert await new.ws.refused("pair", session_id=new.session_id) == P.REJECT_ALREADY_PAIRED
    await new.pair(replace=True)
    await hass.async_block_till_done()
    assert any(e.get("type") == "fenced" for e in app.ws.events)
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}) == P.REJECT_NOT_OWNER
    await new.claim()
    await new.publish({KAMAR: view(state="vacant", occupied=False)})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"
    # The old token is worthless now.
    old = App(await ws(), token=app.token)
    assert (await old.open())["trusted"] is False
    assert await old.ws.refused("claim", session_id=old.session_id) == P.REJECT_NOT_PAIRED


async def test_unpair(hass, app):
    await app.publish({KAMAR: view()})
    assert await app.ws.call("unpair", session_id=app.session_id) == {"paired": False}
    assert await app.publish_refused({KAMAR: view()}) == P.REJECT_NOT_OWNER
    state = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert state.state == "on" and state.attributes["fresh"] is False  # kept, marked stale


async def test_release(hass, app):
    await app.publish({KAMAR: view()})
    await app.ws.call("release", **app.fence())
    assert hass.states.get("binary_sensor.sod_app_connected").state == "off"
