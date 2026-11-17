"""Test fixtures: a fake Home Assistant websocket API and Supervisor.

The fake records every command the App sends, so tests can prove that only
read-only commands are used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import sys
from typing import Any

from aiohttp import WSMsgType, web
import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR / "backend"))

from smart_lighting.core import AppCore  # noqa: E402
from smart_lighting.settings import load_settings  # noqa: E402
from smart_lighting.web import build_app  # noqa: E402

TOKEN = "supervisor-token-abcdef0123456789"
HA_VERSION = "2026.11.1"


def state(entity_id: str, value: str, name: str, unit: str | None = None) -> dict[str, Any]:
    attributes: dict[str, Any] = {"friendly_name": name}
    if unit:
        attributes["unit_of_measurement"] = unit
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": attributes,
        "last_changed": "2026-11-17T10:00:00+00:00",
        "last_updated": "2026-11-17T10:00:00+00:00",
        "context": {"id": "x", "parent_id": None, "user_id": None},
    }


class FakeHomeAssistant:
    """Speaks enough of the Home Assistant websocket protocol for the App."""

    def __init__(self) -> None:
        self.token = TOKEN
        self.commands: list[dict[str, Any]] = []
        self.connections: list[web.WebSocketResponse] = []
        self.subscriptions: dict[web.WebSocketResponse, list[int]] = {}
        self.auth_attempts = 0
        self.integration_installed = True
        self.states = [
            state("binary_sensor.kamar_nordur_occupancy", "off", "Kamar Norður occupancy"),
            state("sensor.kamar_nordur_status", "vacant", "Kamar Norður status"),
            state("light.kamar_nordur_loft", "on", "Loft ljós"),
            state("sensor.outdoor_lux", "1200", "Outdoor lux", "lx"),
        ]
        self.supervisor_requests: list[str] = []

    # -- data ---------------------------------------------------------------
    def config(self) -> dict[str, Any]:
        return {
            "location_name": "Heima",
            "version": HA_VERSION,
            "state": "RUNNING",
            "time_zone": "Atlantic/Faroe",
            "country": "FO",
            "language": "en",
            "currency": "DKK",
            "unit_system": {"length": "km", "temperature": "°C"},
            "components": ["http", "websocket_api", "smart_occupancy", "light"],
            "safe_mode": False,
            "recovery_mode": False,
        }

    def entries(self, domain: str | None) -> list[dict[str, Any]]:
        if not self.integration_installed:
            return []
        entry = {"entry_id": "abc", "domain": "smart_occupancy", "title": "Smart Occupancy & Lighting", "state": "loaded",
                 "disabled_by": None, "reason": None, "source": "user"}
        return [entry] if domain in (None, "smart_occupancy") else []

    def registry(self) -> list[dict[str, Any]]:
        items = [
            {"entity_id": "light.kamar_nordur_loft", "platform": "hue", "disabled_by": None,
             "device_id": "dev-loft", "area_id": None, "name": None, "original_name": "Loft"},
            {"entity_id": "sensor.outdoor_lux", "platform": "zha", "disabled_by": None,
             "device_id": "dev-lux", "area_id": "uti", "name": None, "original_name": "Illuminance"},
            {"entity_id": "switch.hidden_registry_only", "platform": "zha", "disabled_by": None,
             "device_id": None, "area_id": None, "name": "Registry only", "original_name": None},
            {"entity_id": "switch.disabled_switch", "platform": "zha", "disabled_by": "user",
             "device_id": "dev-lux", "area_id": None, "name": None, "original_name": "Off"},
        ]
        if self.integration_installed:
            items += [
                {"entity_id": "binary_sensor.kamar_nordur_occupancy", "platform": "smart_occupancy", "disabled_by": None},
                {"entity_id": "sensor.kamar_nordur_status", "platform": "smart_occupancy", "disabled_by": None},
                {"entity_id": "sensor.disabled_one", "platform": "smart_occupancy", "disabled_by": "user"},
            ]
        return items

    def devices(self) -> list[dict[str, Any]]:
        return [
            {"id": "dev-loft", "name": "Hue loft", "name_by_user": "Loft ljós", "manufacturer": "Signify",
             "model": "LCT015", "area_id": "kamar_nordur", "disabled_by": None},
            {"id": "dev-lux", "name": "Outdoor sensor", "name_by_user": None, "manufacturer": "Aqara",
             "model": "GZCGQ01LM", "area_id": None, "disabled_by": None},
            {"id": "dev-off", "name": "Disabled device", "name_by_user": None, "manufacturer": None,
             "model": None, "area_id": None, "disabled_by": "user"},
        ]

    # -- protocol -----------------------------------------------------------
    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": HA_VERSION})
        auth = await ws.receive_json()
        self.auth_attempts += 1
        if auth.get("type") != "auth" or auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token or password"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": HA_VERSION})
        self.connections.append(ws)
        self.subscriptions[ws] = []
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = msg.json()
                self.commands.append(data)
                await self._answer(ws, data)
        finally:
            self.connections.remove(ws)
            self.subscriptions.pop(ws, None)
        return ws

    async def _answer(self, ws: web.WebSocketResponse, msg: dict[str, Any]) -> None:
        mid, kind = msg["id"], msg["type"]

        async def result(value: Any) -> None:
            await ws.send_json({"id": mid, "type": "result", "success": True, "result": value})

        async def error(code: str, message: str) -> None:
            await ws.send_json({"id": mid, "type": "result", "success": False, "error": {"code": code, "message": message}})

        if kind == "ping":
            await ws.send_json({"id": mid, "type": "pong"})
        elif kind == "subscribe_events":
            self.subscriptions[ws].append(mid)
            await result(None)
        elif kind == "get_config":
            await result(self.config())
        elif kind == "get_states":
            await result(self.states)
        elif kind == "config_entries/get":
            await result(self.entries(msg.get("domain")))
        elif kind == "manifest/get":
            if self.integration_installed and msg["integration"] == "smart_occupancy":
                await result({"domain": "smart_occupancy", "name": "Smart Occupancy & Lighting", "version": "0.6.8"})
            else:
                await error("not_found", "Integration not found")
        elif kind == "config/entity_registry/list":
            await result(self.registry())
        elif kind == "config/area_registry/list":
            await result([{"area_id": "kamar_nordur", "name": "Kamar Norður"}, {"area_id": "stova", "name": "Stova"},
                          {"area_id": "uti", "name": "Úti"}])
        elif kind == "config/device_registry/list":
            await result(self.devices())
        else:
            await error("unknown_command", f"Unknown command {kind}")

    async def emit_state(self, entity_id: str, value: str, name: str) -> None:
        new = state(entity_id, value, name)
        for ws, ids in list(self.subscriptions.items()):
            for sub_id in ids:
                await ws.send_json(
                    {"id": sub_id, "type": "event", "event": {"event_type": "state_changed",
                     "data": {"entity_id": entity_id, "old_state": None, "new_state": new}}}
                )

    async def drop_connections(self) -> None:
        for ws in list(self.connections):
            await ws.close()

    # -- supervisor ---------------------------------------------------------
    async def supervisor(self, request: web.Request) -> web.Response:
        self.supervisor_requests.append(request.path)
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({"result": "error", "message": "Unauthorized"}, status=401)
        if request.path == "/addons/self/info":
            data = {"name": "Smart Occupancy & Lighting", "slug": "smart_lighting", "version": "0.1.1",
                    "version_latest": "0.1.1", "update_available": False, "state": "started", "arch": "amd64",
                    "options": {"log_level": "info"}, "ingress_url": "/api/hassio_ingress/xyz/"}
        else:
            data = {"supervisor": "2026.11.2", "homeassistant": HA_VERSION, "hassos": "17.1",
                    "operating_system": "Home Assistant OS 17.1", "machine": "generic-x86-64", "arch": "amd64",
                    "channel": "stable", "supported": True, "healthy": True}
        return web.json_response({"result": "ok", "data": data})


@pytest.fixture
async def fake_ha(aiohttp_server: Callable[..., Awaitable[Any]]) -> Any:
    fake = FakeHomeAssistant()
    app = web.Application()
    app.router.add_get("/core/websocket", fake.websocket)
    app.router.add_get("/addons/self/info", fake.supervisor)
    app.router.add_get("/info", fake.supervisor)
    server = await aiohttp_server(app)
    fake.base_url = str(server.make_url("")).rstrip("/")
    fake.ws_url = fake.base_url.replace("http://", "ws://") + "/core/websocket"
    return fake


def make_settings(fake: Any, **env: str) -> Any:
    values = {
        "SUPERVISOR_TOKEN": TOKEN,
        "SL_HA_WS_URL": fake.ws_url,
        "SL_SUPERVISOR_URL": fake.base_url,
        "SL_ALLOWED_CLIENTS": "127.0.0.1,::1",
        "SL_OPTIONS": "/nonexistent/options.json",
    }
    values.update(env)
    return load_settings(values)


async def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)


@pytest.fixture
async def app_client(fake_ha: FakeHomeAssistant, aiohttp_client: Callable[..., Awaitable[Any]]) -> Any:
    """The App, started against the fake Home Assistant and connected."""
    settings = make_settings(fake_ha)
    core = AppCore(settings)
    app = build_app(settings, core)

    async def start(_: web.Application) -> None:
        await core.start()

    async def stop(_: web.Application) -> None:
        await core.stop()

    app.on_startup.insert(0, start)
    app.on_cleanup.append(stop)
    client = await aiohttp_client(app)
    await wait_for(lambda: core.ha is not None and core.ha.connected)
    await wait_for(lambda: core.supervisor is not None and core.supervisor.checked_at is not None)
    client.core = core
    return client
