"""The publishing bridge's security: who may publish, fencing, freshness of the lease, strict validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from homeassistant.core import HomeAssistant

from custom_components.smart_predictive_climate import protocol as P
from custom_components.smart_predictive_climate.const import DOMAIN

from .conftest import CONNECTED, FORECAST, HEAT, KAMAR, STOVA, WS_LOGGER, App, binary, room, sensor, setup_spc, temperature


async def test_protocol_copy_matches_the_app():
    root = Path(__file__).resolve().parents[2]
    assert (root / "custom_components" / DOMAIN / "protocol.py").read_bytes() == \
        (root / "smart_predictive_climate" / "backend" / "spc" / "protocol.py").read_bytes()


async def test_only_administrators_may_use_the_bridge(hass: HomeAssistant, ws, hass_read_only_access_token, caplog):
    await setup_spc(hass)
    caplog.set_level("CRITICAL", logger=WS_LOGGER)
    reader = await ws(access_token=hass_read_only_access_token)
    session = "a" * 32
    fence = {"session_id": session, "epoch": 1, "lease_token": "b" * 32, "seq": 1}
    for command, params in (("session", {"protocol": 1, "app_version": "0.1.0", "instance_id": session}),
                            ("status", {}), ("pair", {"session_id": session}), ("unpair", {"session_id": session}),
                            ("claim", {"session_id": session}), ("heartbeat", fence), ("release", fence),
                            ("publish", {**fence, "rooms": {}})):
        msg = await reader.raw(f"{DOMAIN}/{command}", **params)
        assert not msg["success"] and msg["error"]["code"] == "unauthorized", command


async def test_the_integration_registers_no_service_and_only_its_commands(hass, app):
    await app.publish({KAMAR: room()})
    await hass.async_block_till_done()
    assert hass.services.async_services().get(DOMAIN) is None
    from custom_components.smart_predictive_climate.api import COMMANDS

    names = {c._ws_command for c in COMMANDS}
    assert names == {f"{DOMAIN}/{c}" for c in ("session", "pair", "unpair", "claim", "heartbeat", "release",
                                               "publish", "status")}
    # Every state in Home Assistant that this integration wrote is one of its own entities.
    ours = {s.entity_id for s in hass.states.async_all() if "spc_" in s.entity_id}
    assert ours == {FORECAST, HEAT, CONNECTED}


async def test_a_session_needs_pairing_before_it_can_publish(hass, ws):
    await setup_spc(hass)
    a = App(await ws())
    hello = await a.open()
    assert hello["paired"] is False and hello["trusted"] is False
    assert await a.ws.refused("claim", session_id=a.session_id) == P.REJECT_NOT_PAIRED
    a.epoch, a.lease = 1, "f" * 32
    assert await a.publish_refused({KAMAR: room()}) == P.REJECT_NOT_OWNER
    assert hass.states.get(FORECAST) is None


async def test_wrong_protocol_or_ids_are_refused(hass, ws):
    await setup_spc(hass)
    client = await ws()
    msg = await client.raw(f"{DOMAIN}/session", protocol=2, app_version="0.1.0", instance_id="a" * 32)
    assert msg["error"]["code"] == P.REJECT_VERSION
    msg = await client.raw(f"{DOMAIN}/session", protocol=1, app_version="0.1.0", instance_id="not-hex")
    assert msg["error"]["code"] == P.REJECT_INVALID


async def test_fencing(hass, app, ws):
    await app.claim()  # the same session may claim again: a new epoch (2)
    await app.publish({KAMAR: room(forecast_1h=temperature(value=20.0))})
    await app.publish({KAMAR: room()})
    assert hass.states.get(FORECAST).state == "21.4"
    good = app.seq
    other = {KAMAR: room(forecast_1h=temperature(value=5.0))}
    # Duplicate and out-of-order sequence numbers.
    assert await app.publish_refused(other, seq=good) == P.REJECT_SEQUENCE
    assert await app.publish_refused(other, seq=good - 1) == P.REJECT_SEQUENCE
    # A wrong lease token, an old epoch (stale), an unknown session.
    assert await app.publish_refused(other, lease_token="0" * 32) == P.REJECT_LEASE
    assert await app.publish_refused(other, epoch=app.epoch - 1) == P.REJECT_STALE_EPOCH
    assert await app.publish_refused(other, session_id="b" * 32) == P.REJECT_UNKNOWN_SESSION
    # The right session id on another connection (a stolen id) is refused too.
    thief = App(await ws())
    assert await thief.ws.refused("publish", session_id=app.session_id, epoch=app.epoch, lease_token=app.lease,
                                  seq=app.seq + 100, rooms=other) == P.REJECT_UNKNOWN_SESSION
    assert hass.states.get(FORECAST).state == "21.4"  # nothing of that was applied
    status = await app.ws.call("status")
    assert status["refused"][P.REJECT_SEQUENCE] == 2 and status["fresh"]
    assert status["rooms"] == 1 and status["entities"] == 2


BAD_ENTITIES = [
    temperature(value="warm"),
    temperature(value=True),
    temperature(unit=None),
    temperature(unit="m"),
    temperature(brightness=50),
    temperature(name=""),
    temperature(name="x" * 81),
    temperature(icon="home"),
    temperature(category="config"),
    temperature(state_class="total"),
    sensor("Speed", 3, device_class="speed", unit="m/s"),
    sensor("Text with a unit", "abc", unit="x"),
    sensor("Long", "x" * 256),
    sensor("Nested", "a", attributes={"deep": {"er": 1}}),
    sensor("Reserved", "a", attributes={"friendly_name": "Other"}),
    sensor("Big", "a", attributes={f"k{i}": "x" * 250 for i in range(20)}),
    sensor("Many", "a", attributes={f"k{i}": i for i in range(31)}),
    sensor("When", "yesterday", device_class="timestamp"),
    sensor("When", "2026-11-22T12:00:00", device_class="timestamp"),  # no time zone
    sensor("Mode", "heat", device_class="enum"),  # no options
    sensor("Mode", "heat", device_class="enum", attributes={"options": ["idle", "cool"]}),
    sensor("Mode", "heat", device_class="enum", attributes={"options": ["heat", "heat"]}),
    binary(value="on"),
    binary(value=1),
    binary(device_class="occupancy"),
    binary(unit="°C"),
    binary(state_class="measurement"),
    {"platform": "switch", "name": "Heater", "value": True},
    {"platform": "climate", "name": "Thermostat", "value": 21},
    {"platform": "sensor", "name": "No value"},
    "not an object",
]


@pytest.mark.parametrize("bad", [
    {"Bad Room": room()},
    {"kamar-sudur": room()},
    {KAMAR: {"name": "Kamar"}},
    {KAMAR: {"name": "", "entities": {}}},
    {KAMAR: {"name": "Kamar", "entities": {}, "extra": 1}},
    {KAMAR: room(**{"Bad Key": temperature()})},
    {KAMAR: room(entities={f"k{i}": temperature() for i in range(31)})},
    # Two (room, key) pairs that would be the same entity.
    {"stova": room("Stova", {"a_b": temperature()}), "stova_a": room("Stova A", {"b": temperature()})},
    *({KAMAR: room(extra=bad)} for bad in BAD_ENTITIES),
], ids=lambda bad: str(bad)[:60])
async def test_invalid_snapshots_are_refused_and_change_nothing(hass, app, bad):
    await app.publish({KAMAR: room()})
    seq = app.seq
    assert await app.publish_refused(bad) == P.REJECT_INVALID
    await hass.async_block_till_done()
    assert hass.states.get(FORECAST).state == "21.4" and hass.states.get("sensor.spc_kamar_sudur_extra") is None
    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert hub.seq == seq  # the refused message did not move the companion's sequence
    app.seq = seq
    await app.publish({KAMAR: room(forecast_1h=temperature(value=19.0))})
    assert hass.states.get(FORECAST).state == "19.0"


async def test_an_invalid_deleted_list_is_refused(hass, app, caplog):
    caplog.set_level("CRITICAL", logger=WS_LOGGER)  # Home Assistant logs the unknown field below as an error
    await app.publish({KAMAR: room()})
    seq = app.seq
    for deleted in ([KAMAR], ["Not A Room"], [1], ["x"] * 101):
        assert await app.publish_refused({KAMAR: room()}, deleted=deleted) == P.REJECT_INVALID
        app.seq = seq
    assert await app.ws.refused("publish", **app.fence(), rooms={KAMAR: room()}, disabled={}) == "invalid_format"
    assert hass.states.get(FORECAST).state == "21.4"


async def test_a_second_session_cannot_take_a_valid_lease(hass, app, ws):
    other = App(await ws(), token=app.token)  # another process of the same paired App
    assert (await other.open())["trusted"]
    assert await other.ws.refused("claim", session_id=other.session_id) == P.REJECT_BUSY


async def test_pairing_again_fences_the_previous_app(hass, app, ws):
    await app.publish({KAMAR: room()})
    new = App(await ws())
    hello = await new.open()
    assert hello["paired"] and not hello["trusted"]
    assert await new.ws.refused("pair", session_id=new.session_id) == P.REJECT_ALREADY_PAIRED
    await new.pair(replace=True)
    await hass.async_block_till_done()
    assert any(e.get("type") == "fenced" for e in app.ws.events)
    assert hass.states.get(FORECAST).state == "unavailable"  # nobody publishes right now
    assert await app.publish_refused({KAMAR: room(forecast_1h=temperature(value=5.0))}) == P.REJECT_NOT_OWNER
    await new.claim()
    await new.publish({KAMAR: room(forecast_1h=temperature(value=18.5))})
    assert hass.states.get(FORECAST).state == "18.5"
    # The old token is worthless now.
    old = App(await ws(), token=app.token)
    assert (await old.open())["trusted"] is False
    assert await old.ws.refused("claim", session_id=old.session_id) == P.REJECT_NOT_PAIRED


async def test_unpair(hass, app):
    await app.publish({KAMAR: room()})
    assert await app.ws.call("unpair", session_id=app.session_id) == {"paired": False}
    assert await app.publish_refused({KAMAR: room()}) == P.REJECT_NOT_OWNER
    assert hass.states.get(FORECAST).state == "unavailable" and hass.states.get(HEAT).state == "unavailable"
    connected = hass.states.get(CONNECTED)
    assert connected.state == "off" and connected.attributes["paired"] is False


async def test_release(hass, app):
    await app.publish({KAMAR: room()})
    await app.ws.call("release", **app.fence())
    assert hass.states.get(CONNECTED).state == "off"
    assert hass.states.get(FORECAST).state == "unavailable"


async def test_the_store_tells_whether_it_was_ever_paired(hass, ws):
    """A fresh store (the integration added again) lets a previously paired App pair by itself; an unpair is
    remembered, so it never does after that."""
    await setup_spc(hass)
    a = App(await ws())
    assert (await a.open())["fresh_store"] is True
    await a.ws.call("pair", session_id=a.session_id, automatic=True)
    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert hub.pairing["automatic"] is True
    await a.ws.call("unpair", session_id=a.session_id)
    b = App(await ws())
    assert (await b.open())["fresh_store"] is False
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    c = App(await ws())
    assert (await c.open())["fresh_store"] is False  # remembered across reloads


def test_the_protocol_module_is_strict_about_limits():
    rooms, deleted = P.check_snapshot({"rooms": {KAMAR: room()}, "deleted": [STOVA, STOVA]})
    assert deleted == [STOVA]
    entity = rooms[KAMAR]["entities"]["forecast_1h"]
    assert set(entity) == set(P.ENTITY_FIELDS) and entity["attributes"] == {} and entity["icon"] is None
    assert P.check_snapshot({"rooms": {}}) == ({}, [])
    for payload in (None, [], {"rooms": []}, {"rooms": {}, "disabled": {}},
                    {"rooms": {f"r{i}": room() for i in range(P.MAX_ROOMS + 1)}}):
        with pytest.raises(P.ProtocolError):
            P.check_snapshot(payload)
    for value in (float("inf"), float("nan"), -float("inf")):  # not JSON: cannot even arrive over the websocket
        with pytest.raises(P.ProtocolError):
            P.check_entity("forecast_1h", temperature(value=value))
    assert P.check_entity("x", sensor("Count", 3))["value"] == 3
    assert P.check_entity("x", sensor("Text", ""))["value"] == ""
    with pytest.raises(P.ProtocolError):
        P.check_entity("x", sensor("Flag", True))  # true/false belongs to a binary sensor
    assert P.ROOM_ID.pattern == r"^[a-z0-9_]{1,100}$" and P.ENTITY_KEY.pattern == r"^[a-z0-9_]{1,40}$"
    assert (P.MAX_ROOMS, P.MAX_ENTITIES_PER_ROOM, P.PROTOCOL_VERSION) == (100, 30, 1)
