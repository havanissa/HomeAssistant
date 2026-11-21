"""The App's real companion link (``sod/publisher.py``) against the real companion, over Home Assistant's websocket."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant

from sod import publisher as PUB  # noqa: E402 - the App's backend, put on the path by conftest
from sod.ha import HomeAssistantError

from .conftest import KAMAR, Ws, setup_sod, view


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

    async def unsubscribe(self, sub_id: int) -> None:
        self.ws.handlers.pop(sub_id, None)
        await self.ws.raw("unsubscribe_events", subscription=sub_id)

    async def subscribe(self, command_type: str, handler, **params: Any) -> int:
        msg_id = next(self.ws._ids)
        self.ws.handlers[msg_id] = handler
        future = asyncio.get_running_loop().create_future()
        self.ws.pending[msg_id] = future
        await self.ws.client.send_json({"id": msg_id, "type": command_type, **params})
        msg = await asyncio.wait_for(future, 10)
        if not msg["success"]:
            raise HomeAssistantError(msg["error"]["code"], msg["error"]["message"])
        return msg_id


async def wait_for(predicate, timeout: float = 5.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached")


async def test_the_app_publishes_through_the_real_companion(hass: HomeAssistant, ws, areas, tmp_path, monkeypatch):
    monkeypatch.setattr(PUB, "PUBLISH_DELAY", 0.01)
    await setup_sod(hass)
    rooms = {KAMAR: view(state="vacant", occupied=False, reason_code="vacant")}
    bridge = Bridge(await ws())
    link = PUB.CompanionLink(bridge, lambda: (rooms, {}), tmp_path, app_version="0.1.0", can_publish=lambda: None)
    await link.start()
    await link._open()
    await wait_for(lambda: link.state == "not_paired")
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied") is None
    await link.pair(replace=False, user_id=None)
    await wait_for(lambda: link.state == "publishing")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"
    rooms[KAMAR] = view()
    link.schedule_publish()
    await wait_for(lambda: (s := hass.states.get("binary_sensor.sod_kamar_sudur_occupied")) and s.state == "on")
    assert hass.states.get("binary_sensor.sod_app_connected").state == "on"
    # A restarted App process (new instance, same /data) resumes without a human.
    await link.stop()
    await bridge.ws.close()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_app_connected").state == "off"
    bridge2 = Bridge(await ws())
    link2 = PUB.CompanionLink(bridge2, lambda: (rooms, {}), tmp_path, app_version="0.1.0", can_publish=lambda: None)
    await link2.start()
    await link2._open()
    await wait_for(lambda: link2.state == "publishing")
    rooms[KAMAR] = view(state="possible_vacancy", reason_code="confirming_vacancy")
    link2.schedule_publish()
    await wait_for(lambda: hass.states.get("sensor.sod_kamar_sudur_state").state == "possible_vacancy")
    status = await bridge2.command("smart_occupancy_detection/status")
    assert status["epoch"] == 2 and status["fresh"] and status["refused"] == {}
    # The integration is reloaded: the App opens a new session and claims again by itself.
    entry = hass.config_entries.async_entries("smart_occupancy_detection")[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    rooms[KAMAR] = view()
    await wait_for(lambda: link2.state == "publishing" and (link2.epoch or 0) >= 3, 10)
    link2.schedule_publish()
    await wait_for(lambda: hass.states.get("binary_sensor.sod_app_connected").state == "on")
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"
    await link2.stop()
