"""The App's real companion link (``spc/publisher.py``) against the real companion, over Home Assistant's websocket."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from spc import publisher as PUB  # noqa: E402 - the App's backend, put on the path by conftest
from spc.ha import HomeAssistantError

from .conftest import CONNECTED, FORECAST, HEAT, KAMAR, STOVA, Ws, room, setup_spc, temperature

DOMAIN = "smart_predictive_climate"


class Bridge:
    """What the companion link uses of the App's Home Assistant client, over a test websocket connection."""

    def __init__(self, ws: Ws) -> None:
        self.ws = ws
        self.connected = True
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    async def command(self, command_type: str, **params: Any) -> Any:
        msg = await self.ws.raw(command_type, **params)
        if not msg["success"]:
            raise HomeAssistantError(msg["error"]["code"], msg["error"]["message"])
        return msg["result"]

    async def subscribe(self, command_type: str, params: dict[str, Any], handler):
        msg_id = next(self.ws._ids)
        self.ws.handlers[msg_id] = handler
        future = asyncio.get_running_loop().create_future()
        self.ws.pending[msg_id] = future
        await self.ws.client.send_json({"id": msg_id, "type": command_type, **params})
        msg = await asyncio.wait_for(future, 10)
        if not msg["success"]:
            raise HomeAssistantError(msg["error"]["code"], msg["error"]["message"])

        async def unsubscribe() -> None:
            if self.ws.handlers.pop(msg_id, None) is not None:
                await self.ws.raw("unsubscribe_events", subscription=msg_id)

        return unsubscribe


async def wait_for(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


def state(hass, entity_id):
    found = hass.states.get(entity_id)
    return found.state if found else None


async def test_the_app_publishes_through_the_real_companion(hass: HomeAssistant, ws, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(PUB, "PUBLISH_DELAY", 0.01)
    await setup_spc(hass)
    bridge = Bridge(await ws())
    link = PUB.CompanionLink(bridge, tmp_path, app_version="0.1.0")
    link.publish({"rooms": {KAMAR: room(), STOVA: room("Stova")}})  # before anything is connected: kept
    await link.start()
    await link._open()
    await wait_for(lambda: link.state == "not_paired")
    assert hass.states.get(FORECAST) is None
    assert await link.pair(replace=False, requested_by=None) == {"paired": True, "state": "publishing"}
    await wait_for(lambda: state(hass, FORECAST) == "21.4")
    assert state(hass, HEAT) == "on" and state(hass, CONNECTED) == "on"
    assert link.status()["publishing"] and link.status()["counters"]["publishes"] == 1
    # A new snapshot replaces the old one; the same snapshot is not sent twice.
    link.publish({"rooms": {KAMAR: room(forecast_1h=temperature(value=19.5)), STOVA: room("Stova")}})
    await wait_for(lambda: state(hass, FORECAST) == "19.5")
    publishes = link.counters["publishes"]
    link.publish({"rooms": {KAMAR: room(forecast_1h=temperature(value=19.5)), STOVA: room("Stova")}})
    await asyncio.sleep(0.1)
    assert link.counters["publishes"] == publishes
    # An invalid snapshot is never sent (the companion's sequence does not move); the entities keep the last one.
    seq = (await bridge.command(f"{DOMAIN}/status"))["seq"]
    with caplog.at_level("CRITICAL", logger="spc.publisher"):  # it is logged as an error (a bug in the App)
        link.publish({"rooms": {KAMAR: room(forecast_1h=temperature(value="warm"))}})
        await asyncio.sleep(0.1)
    status = await bridge.command(f"{DOMAIN}/status")
    assert status["seq"] == seq and status["refused"] == {} and state(hass, FORECAST) == "19.5"
    assert "forecast_1h" in link.status()["last_error"]
    # A room deleted in the App loses its entities; one that is merely missing would keep them.
    link.mark_deleted(STOVA)
    link.publish({"rooms": {KAMAR: room()}})
    await wait_for(lambda: state(hass, "sensor.spc_stova_forecast_1h") is None and state(hass, FORECAST) == "21.4")
    await wait_for(lambda: link.deleted == set())  # forgotten once the companion accepted it
    await hass.async_block_till_done()
    assert not [e for e in er.async_get(hass).entities if "stova" in e]
    # A restarted App process (new instance, same /data) resumes without a human.
    await link.stop()
    await bridge.ws.close()
    await hass.async_block_till_done()
    assert state(hass, CONNECTED) == "off" and state(hass, FORECAST) == "unavailable"
    bridge2 = Bridge(await ws())
    link2 = PUB.CompanionLink(bridge2, tmp_path, app_version="0.1.0")
    await link2.start()
    await link2._open()
    await wait_for(lambda: link2.state == "publishing")
    assert state(hass, FORECAST) == "unavailable"  # a lease without a snapshot of this run shows nothing
    link2.publish({"rooms": {KAMAR: room(forecast_1h=temperature(value=20.1))}})
    await wait_for(lambda: state(hass, FORECAST) == "20.1")
    status = await bridge2.command(f"{DOMAIN}/status")
    assert status["epoch"] == 2 and status["fresh"] and status["refused"] == {}
    # The integration is reloaded: the App opens a new session, claims again and sends its snapshot by itself.
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await wait_for(lambda: link2.state == "publishing" and (link2.epoch or 0) >= 3, 10)
    await wait_for(lambda: state(hass, FORECAST) == "20.1" and state(hass, CONNECTED) == "on")
    # Unpairing from the App: everything unavailable, the token is forgotten.
    assert await link2.unpair(requested_by=None) == {"paired": False}
    await hass.async_block_till_done()
    assert state(hass, FORECAST) == "unavailable" and state(hass, CONNECTED) == "off"
    assert link2.state == "not_paired" and not (tmp_path / "companion.json").exists()
    await link2.stop()
    assert hass.services.async_services().get(DOMAIN) is None
