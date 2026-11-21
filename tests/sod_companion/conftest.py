"""Fixtures of the Smart Occupancy Detection companion suite: a real Home Assistant and simulated App sessions.

Run in its own process: ``python scripts/run_tests.py tests/sod_companion``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import itertools
import logging
from pathlib import Path
import secrets
import sys
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from custom_components.smart_occupancy_detection.const import DOMAIN

# The App's own modules (its real companion link) for the protocol interplay tests.
APP_BACKEND = Path(__file__).resolve().parents[2] / "smart_occupancy_detection" / "backend"
if str(APP_BACKEND) not in sys.path:
    sys.path.insert(0, str(APP_BACKEND))

KAMAR = "kamar_sudur"
STOVA = "stova"
WS_LOGGER = "homeassistant.components.websocket_api.http.connection"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def no_errors_logged(caplog):
    yield
    errors = [r for r in caplog.get_records("call") + caplog.get_records("teardown") if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


async def setup_sod(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy Detection", data={}, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.fixture
async def areas(hass: HomeAssistant) -> dict[str, str]:
    registry = ar.async_get(hass)
    kamar = registry.async_create("Kamar Suður")
    stova = registry.async_create("Stova")
    return {"kamar": kamar.id, "stova": stova.id}


def view(name: str = "Kamar Suður", state: str = "confirmed_occupancy", occupied: bool | None = True,
         **extra: Any) -> dict[str, Any]:
    return {"name": name, "state": state, "occupied": occupied, "confidence": 80, "reason": "Occupied: PIR active.",
            "reason_code": "occupied", "last_activity": "2026-11-21T11:59:00+00:00",
            "state_since": "2026-11-21T11:58:00+00:00", "vacancy_at": None, "starting_up": False,
            "sensor_problems": 0, **extra}


class Ws:
    """A websocket client of the test Home Assistant with a reader that routes results and events."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self._ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[int, Callable[[Any], None]] = {}
        self.events: list[dict[str, Any]] = []
        self.task = asyncio.get_running_loop().create_task(self._read())

    async def _read(self) -> None:
        while True:
            try:
                msg = await self.client.receive_json()
            except Exception:  # noqa: BLE001 - the connection closed
                return
            if msg.get("type") == "event":
                self.events.append(msg["event"])
                if handler := self.handlers.get(msg["id"]):
                    handler(msg["event"])
            elif (future := self.pending.pop(msg.get("id"), None)) and not future.done():
                future.set_result(msg)

    async def raw(self, command: str, **params: Any) -> dict[str, Any]:
        msg_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self.pending[msg_id] = future
        await self.client.send_json({"id": msg_id, "type": command, **params})
        return await asyncio.wait_for(future, 10)

    async def call(self, command: str, **params: Any) -> Any:
        msg = await self.raw(f"{DOMAIN}/{command}", **params)
        assert msg["success"], msg
        return msg["result"]

    async def refused(self, command: str, **params: Any) -> str:
        msg = await self.raw(f"{DOMAIN}/{command}", **params)
        assert not msg["success"], msg
        return msg["error"]["code"]

    async def close(self) -> None:
        await self.client.close()
        self.task.cancel()


class App:
    """A simulated App: one session on its own websocket connection."""

    def __init__(self, ws: Ws, token: str | None = None) -> None:
        self.ws = ws
        self.token = token
        self.instance_id = secrets.token_hex(16)
        self.session_id: str | None = None
        self.hello: dict[str, Any] = {}
        self.epoch: int | None = None
        self.lease: str | None = None
        self.seq = 0

    async def open(self, **extra: Any) -> dict[str, Any]:
        params = {"protocol": 1, "app_version": "0.1.0", "instance_id": self.instance_id, **extra}
        if self.token:
            params["pairing_token"] = self.token
        msg = await self.ws.raw(f"{DOMAIN}/session", **params)
        assert msg["success"], msg
        for _ in range(100):
            hello = next((e for e in self.ws.events if e.get("type") == "hello"), None)
            if hello:
                break
            await asyncio.sleep(0.01)
        self.ws.events.remove(hello)
        self.hello, self.session_id = hello, hello["session_id"]
        return hello

    async def pair(self, replace: bool = False) -> None:
        self.token = (await self.ws.call("pair", session_id=self.session_id, replace=replace))["pairing_token"]

    async def claim(self) -> dict[str, Any]:
        result = await self.ws.call("claim", session_id=self.session_id)
        self.epoch, self.lease, self.seq = result["epoch"], result["lease_token"], 0
        return result

    def fence(self, **override: Any) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session_id, "epoch": self.epoch, "lease_token": self.lease, "seq": self.seq,
                **override}

    async def publish(self, rooms: dict[str, Any], disabled: dict[str, str] | None = None, **override: Any) -> Any:
        return await self.ws.call("publish", **self.fence(**override), rooms=rooms, disabled=disabled or {},
                                  app_version="0.1.0")

    async def publish_refused(self, rooms: dict[str, Any], **override: Any) -> str:
        return await self.ws.refused("publish", **self.fence(**override), rooms=rooms, disabled={})


@pytest.fixture
async def ws(hass_ws_client):
    clients: list[Ws] = []

    async def connect(**kwargs: Any) -> Ws:
        client = Ws(await hass_ws_client(**kwargs))
        clients.append(client)
        return client

    yield connect
    for client in clients:
        await client.close()


@pytest.fixture
async def app(hass, ws, areas):
    """A paired App that holds the lease."""
    await setup_sod(hass)
    a = App(await ws())
    await a.open()
    await a.pair()
    await a.claim()
    return a
