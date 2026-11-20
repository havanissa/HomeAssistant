"""Backend against a fake Home Assistant: startup, health, API, Ingress, safety."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import aiohttp
import pytest

from smart_lighting.const import APP_VERSION
from smart_lighting.core import AppCore
from smart_lighting.ha import (
    CONFIG_WRITE_COMMANDS,
    CURTAIN_BRIDGE_COMMANDS,
    CURTAIN_READ_COMMANDS,
    CURTAIN_WRITE_COMMANDS,
    ENGINE_COMMANDS,
    FORECAST_BRIDGE_COMMANDS,
    FORECAST_READ_COMMANDS,
    FORECAST_WRITE_COMMANDS,
    LIGHTING_BRIDGE_COMMANDS,
    LIGHTING_READ_COMMANDS,
    LIGHTING_WRITE_COMMANDS,
    READ_ONLY_COMMANDS,
    SUBSCRIPTION_COMMANDS,
    HomeAssistantClient,
    ReadOnlyViolation,
)
from smart_lighting.logbuffer import setup_logging
from smart_lighting.web import CORE_KEY, build_app

from .conftest import TOKEN, FakeHomeAssistant, make_settings, wait_for

INGRESS = "/api/hassio_ingress/AbC123_-xyz"


async def test_startup_connects_and_reports_healthy(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await app_client.get("/api/health")
    assert resp.status == 200
    health = await resp.json()
    assert health["status"] == "ok"
    assert health["version"] == APP_VERSION
    assert health["home_assistant"] == "connected"
    by_key = {c["key"]: c for c in health["checks"]}
    assert by_key["home_assistant"]["status"] == "ok"
    assert by_key["safety"]["status"] == "ok"
    assert by_key["rooms_api"]["status"] == "ok"
    assert by_key["supervisor"]["status"] == "ok"
    assert by_key["interface"]["status"] == "ok"
    assert by_key["live_updates"]["status"] == "ok"


async def test_overview_reads_home_assistant_and_detects_integration(app_client: Any) -> None:
    overview = await (await app_client.get("/api/overview")).json()
    info = overview["home_assistant"]["info"]
    assert info["location_name"] == "Heima"
    assert info["version"] == "2026.11.1"
    assert info["entity_count"] == 9
    assert info["area_count"] == 3
    integration = overview["integration"]
    assert integration["status"] == "loaded"
    assert integration["detected"] is True
    assert integration["version"] == "0.6.8"
    assert integration["entity_count"] == 2  # the disabled entity is not counted
    assert integration["entities_by_domain"] == {"binary_sensor": 1, "sensor": 1}
    assert [e["entity_id"] for e in overview["integration_entities"]] == [
        "binary_sensor.kamar_nordur_occupancy",
        "sensor.kamar_nordur_status",
    ]
    assert overview["supervisor"]["available"] is True
    assert "options" not in overview["supervisor"]["app"]  # only whitelisted fields
    assert overview["migration"]["modules_migrated"] == 0  # no automation runs in the App
    # The Rooms, Presence, Lighting, Moods, Switches, Curtains and Predictions interfaces (Phase A5).
    assert overview["migration"]["interfaces_migrated"] == 7
    assert overview["migration"]["modules_total"] == 7
    assert overview["safety"]["service_calls_sent"] == 0


async def test_integration_not_installed(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    fake_ha.integration_installed = False
    settings = make_settings(fake_ha)
    core = AppCore(settings)
    await core.start()
    try:
        await wait_for(lambda: core.ha.connected)
        report = core.ha.integration.as_dict()
        assert report["status"] == "not_installed"
        assert report["detected"] is False
        assert core.ha.integration_entities() == []
    finally:
        await core.stop()


async def test_only_read_only_commands_are_sent(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    await app_client.get("/api/overview")
    await app_client.get("/api/diagnostics")
    await app_client.get("/api/rooms")
    await app_client.get("/api/rooms/kamar_nordur")
    sent = {c["type"] for c in fake_ha.commands}
    # Reading never sends a configuration command (the occupancy bridge session is opened at start, Phase A3.2).
    assert sent <= READ_ONLY_COMMANDS | SUBSCRIPTION_COMMANDS
    assert "call_service" not in sent
    subscriptions = [c for c in fake_ha.commands if c["type"] == "subscribe_events"]
    assert [c["event_type"] for c in subscriptions] == [
        "state_changed", "area_registry_updated", "device_registry_updated", "entity_registry_updated",
        "floor_registry_updated", "label_registry_updated", "automation_reloaded", "script_reloaded",
        "homeassistant_started"]
    # The supervisor endpoints used are the two basic, read-only ones.
    assert set(fake_ha.supervisor_requests) == {"/addons/self/info", "/info"}


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("call_service", {"domain": "light", "service": "turn_on"}),
        ("fire_event", {"event_type": "x"}),
        ("config/entity_registry/update", {"entity_id": "light.x"}),
        ("config_entries/update", {"entry_id": "abc"}),
        ("subscribe_events", {"event_type": "call_service"}),
        ("subscribe_events", {}),
        ("execute_script", {"sequence": []}),
        ("smart_occupancy/rooms/set", {"area_id": "x", "room": None}),  # writes only through config_write()
        ("smart_occupancy/lighting/room/activate", {"area_id": "x"}),
        ("smart_occupancy/curtains/preset/apply", {}),
        ("config/auth/delete", {"user_id": "x"}),
        ("subscribe_events", {"event_type": "smart_occupancy_lighting_updated"}),
    ],
)
async def test_write_commands_are_refused_before_sending(
    app_client: Any, fake_ha: FakeHomeAssistant, command: str, params: dict[str, Any]
) -> None:
    before = len(fake_ha.commands)
    with pytest.raises(ReadOnlyViolation):
        await app_client.core.ha.command(command, **params)
    await asyncio.sleep(0.05)
    assert len(fake_ha.commands) == before


@pytest.mark.parametrize("command", ["call_service", "get_states", "smart_occupancy/rooms/get",
                                     "smart_occupancy/lighting/editor", "smart_occupancy/lighting/bridge/execute",
                                     "smart_occupancy/curtains/preset/apply", "config_entries/update"])
async def test_config_write_accepts_only_room_commands(app_client: Any, fake_ha: FakeHomeAssistant, command: str) -> None:
    before = len(fake_ha.commands)
    with pytest.raises(ReadOnlyViolation):
        await app_client.core.ha.config_write(command)
    await asyncio.sleep(0.05)
    assert len(fake_ha.commands) == before


def test_client_has_no_device_api() -> None:
    names = {n for n in dir(HomeAssistantClient) if not n.startswith("__")}
    assert not {n for n in names if re.search(r"service|turn_on|turn_off|set_state|fire|update_entry", n)}
    assert {n for n in names if "write" in n} == {"config_write", "config_writes_sent"}
    assert {n for n in names if "engine" in n or n == "subscribe"} == {"engine_command", "subscribe"}
    # Phase A4 / A5: the only ways towards a device are the integration's fenced lighting and curtain bridges.
    assert {n for n in names if "lighting" in n} == {"lighting_command"}
    assert {n for n in names if "curtain" in n} == {"curtains_command"}
    assert {n for n in names if "forecast" in n} == {"forecast_command"}
    # Phase A6: the companion integration's fenced executor and ownership commands (COMPANION_COMMANDS only).
    assert {n for n in names if "companion" in n} == {"companion_command"}
    assert READ_ONLY_COMMANDS == {
        "ping", "get_config", "get_states", "subscribe_events", "config_entries/get", "config_entries/subscribe",
        "manifest/get", "config/entity_registry/list", "config/area_registry/list", "config/device_registry/list",
        "config/floor_registry/list", "config/auth/list", "smart_occupancy/rooms/get", "smart_occupancy/rooms/review",
        "smart_occupancy/rooms/restore/review", "smart_occupancy/presence/get", "smart_occupancy/presence/review",
        "history/history_during_period", "smart_occupancy/engine/status",
        # Phase A4 (lighting runtime reads):
        "config/label_registry/list", "config/entity_registry/get_entries", "automation/config", "script/config",
        "device_automation/trigger/list",
        "validate_config", "unsubscribe_events", "smart_occupancy/lighting/bridge/status",
        # Phase A5: the curtain and forecast ownership records.
        "smart_occupancy/curtains/bridge/status", "smart_occupancy/forecast/bridge/status",
        # Phase A6: the companion's records and the cards' read-only view.
        "smart_occupancy/companion/status", "smart_occupancy/companion/view",
    }
    # Phase A3.2: the occupancy bridge (ownership and the App's published occupancy results; no device control).
    assert ENGINE_COMMANDS == {"smart_occupancy/engine/claim", "smart_occupancy/engine/publish",
                               "smart_occupancy/engine/heartbeat", "smart_occupancy/engine/release"}
    # Phase A4: the lighting bridge.
    assert LIGHTING_BRIDGE_COMMANDS == {f"smart_occupancy/lighting/bridge/{c}" for c in (
        "claim", "execute", "heartbeat", "publish", "inputs", "options", "release", "return")}
    assert SUBSCRIPTION_COMMANDS == {"smart_occupancy/engine/subscribe", "smart_occupancy/lighting/bridge/subscribe",
                                     "smart_occupancy/lighting/subscribe", "subscribe_trigger", "mqtt/subscribe",
                                     "smart_occupancy/curtains/bridge/subscribe",
                                     "smart_occupancy/forecast/bridge/subscribe", "smart_occupancy/curtains/subscribe",
                                     # Phase A6: the companion session.
                                     "smart_occupancy/companion/subscribe"}
    # Phase A5: the curtain bridge (its execute is fenced and allowlisted by the integration) and the forecast bridge
    # (no device command at all).
    assert CURTAIN_BRIDGE_COMMANDS == {f"smart_occupancy/curtains/bridge/{c}" for c in (
        "claim", "execute", "heartbeat", "publish", "release", "return")}
    assert FORECAST_BRIDGE_COMMANDS == {f"smart_occupancy/forecast/bridge/{c}" for c in (
        "claim", "heartbeat", "publish", "release", "return")}
    assert not any("execute" in c for c in FORECAST_BRIDGE_COMMANDS)
    # The commands that change configuration: the integration's validated room, presence, lighting, curtain and
    # forecast commands.
    assert CONFIG_WRITE_COMMANDS == {"smart_occupancy/rooms/set", "smart_occupancy/rooms/restore",
                                     "smart_occupancy/presence/set"} | LIGHTING_WRITE_COMMANDS | CURTAIN_WRITE_COMMANDS \
        | FORECAST_WRITE_COMMANDS
    assert all(c.startswith("smart_occupancy/lighting/") for c in LIGHTING_WRITE_COMMANDS | LIGHTING_READ_COMMANDS)
    assert not (LIGHTING_WRITE_COMMANDS | LIGHTING_READ_COMMANDS) & LIGHTING_BRIDGE_COMMANDS
    assert all(c.startswith("smart_occupancy/curtains/") and "/bridge/" not in c
               for c in CURTAIN_WRITE_COMMANDS | CURTAIN_READ_COMMANDS)
    assert all(c.startswith("smart_occupancy/forecast/") and "/bridge/" not in c
               for c in FORECAST_WRITE_COMMANDS | FORECAST_READ_COMMANDS)


async def test_token_never_reaches_the_browser(app_client: Any) -> None:
    for path in ("/", "/api/health", "/api/overview", "/api/diagnostics", "/api/diagnostics?download=1"):
        body = await (await app_client.get(path, headers={"X-Ingress-Path": INGRESS})).text()
        assert TOKEN not in body, path
    async with app_client.ws_connect("/api/live") as ws:
        first = await ws.receive_str(timeout=5)
        assert TOKEN not in first
    diagnostics = await (await app_client.get("/api/diagnostics")).json()
    assert diagnostics["settings"]["token"] == "present (hidden)"


async def test_log_redacts_token(fake_ha: FakeHomeAssistant) -> None:
    import logging

    buffer = setup_logging("debug", [TOKEN])
    logging.getLogger("smart_lighting.test").warning("token is %s", TOKEN)
    assert TOKEN not in json.dumps(buffer.entries())
    assert "**redacted**" in buffer.entries()[-1]["message"]
    logging.getLogger().handlers.clear()


async def test_index_gets_ingress_base(app_client: Any) -> None:
    resp = await app_client.get("/", headers={"X-Ingress-Path": INGRESS})
    assert resp.status == 200
    page = await resp.text()
    assert f'<base href="{INGRESS}/">' in page
    assert page.index("<base") < page.index("./assets/")
    # Assets are referenced relatively, so they resolve under the prefix.
    assets = re.findall(r'(?:src|href)="\./(assets/[^"]+)"', page)
    assert assets
    for asset in assets:
        r = await app_client.get("/" + asset)
        assert r.status == 200, asset
        assert "immutable" in r.headers["Cache-Control"]
    assert resp.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "header",
    ['/api/hassio_ingress/x"><script>alert(1)</script>', "javascript:alert(1)", "//evil.example", "/a b", ""],
)
async def test_invalid_ingress_header_is_ignored(app_client: Any, header: str) -> None:
    page = await (await app_client.get("/", headers={"X-Ingress-Path": header})).text()
    assert '<base href="/">' in page
    assert "<script>alert" not in page


async def test_trailing_slash_and_no_header(app_client: Any) -> None:
    page = await (await app_client.get("/index.html", headers={"X-Ingress-Path": INGRESS + "/"})).text()
    assert f'<base href="{INGRESS}/">' in page


async def test_security_headers(app_client: Any) -> None:
    resp = await app_client.get("/")
    csp = resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in csp
    assert "script-src 'self'" in csp
    assert "X-Frame-Options" not in resp.headers  # must stay embeddable in the HA sidebar panel
    assert resp.headers["X-Content-Type-Options"] == "nosniff"


async def test_unknown_api_and_methods(app_client: Any) -> None:
    resp = await app_client.get("/api/does-not-exist")
    assert resp.status == 404
    assert (await resp.json())["error"] == "Not found"
    resp = await app_client.post("/api/overview", json={})
    assert resp.status in (404, 405)
    assert (await app_client.get("/../../etc/passwd")).status == 404


async def test_diagnostics_download(app_client: Any) -> None:
    resp = await app_client.get("/api/diagnostics?download=1")
    assert resp.status == 200
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="smart-comfort-diagnostics-')
    data = await resp.json()
    assert data["runtime"]["aiohttp"] == aiohttp.__version__
    assert {"settings", "log", "checks", "safety"} <= set(data)


async def test_live_updates_forward_integration_entities(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    async with app_client.ws_connect("/api/live") as ws:
        first = await ws.receive_json(timeout=5)
        assert first["type"] == "overview"
        # A non-integration entity is not forwarded; an integration one is.
        await fake_ha.emit_state("sensor.outdoor_lux", "900", "Outdoor lux")
        await fake_ha.emit_state("binary_sensor.kamar_nordur_occupancy", "on", "Kamar Norður occupancy")
        async with asyncio.timeout(5):
            while True:
                msg = await ws.receive_json()
                if msg["type"] == "entities":
                    break
        assert [e["entity_id"] for e in msg["changed"]] == ["binary_sensor.kamar_nordur_occupancy"]
        assert msg["changed"][0]["state"] == "on"
        await ws.send_str('{"type":"ping"}')
        async with asyncio.timeout(5):
            while (await ws.receive_json())["type"] != "pong":
                pass
    assert app_client.core.ha.states["sensor.outdoor_lux"]["state"] == "900"
    assert app_client.core.ha.events_total == 2


async def test_live_rejects_cross_site_origin(app_client: Any) -> None:
    host = f"{app_client.host}:{app_client.port}"
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await app_client.ws_connect("/api/live", headers={"Origin": "https://evil.example"})
    assert err.value.status == 403
    # Through Ingress the browser's own host arrives as Host / X-Forwarded-Host.
    async with app_client.ws_connect(
        "/api/live", headers={"Origin": "http://homeassistant.local:8123", "X-Forwarded-Host": "homeassistant.local:8123"}
    ) as ws:
        assert (await ws.receive_json(timeout=5))["type"] == "overview"
    async with app_client.ws_connect("/api/live", headers={"Origin": f"http://{host}"}) as ws:
        assert (await ws.receive_json(timeout=5))["type"] == "overview"


async def test_reconnects_after_home_assistant_restart(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    ha = app_client.core.ha
    async with app_client.ws_connect("/api/live") as ws:
        await ws.receive_json(timeout=5)
        await fake_ha.drop_connections()
        await wait_for(lambda: not ha.connected)
        states = []
        async with asyncio.timeout(10):
            while "connected" not in states:
                msg = await ws.receive_json()
                if msg["type"] == "status":
                    states.append(msg["connection"]["state"])
        assert states[0] in ("disconnected", "connecting")
    await wait_for(lambda: ha.connected, timeout=10)
    assert ha.reconnects == 1
    assert ha.last_disconnect is not None
    health = await (await app_client.get("/api/health")).json()
    assert health["status"] == "ok"


async def test_rejected_token_is_reported(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    settings = make_settings(fake_ha, SUPERVISOR_TOKEN="wrong-token-0000000000")
    client = await aiohttp_client(build_app(settings))
    core = client.app[CORE_KEY]
    await wait_for(lambda: core.ha.state == "auth_failed")
    health = await (await client.get("/api/health")).json()
    assert health["status"] == "error"
    check = next(c for c in health["checks"] if c["key"] == "home_assistant")
    assert check["status"] == "error"
    assert fake_ha.auth_attempts == 1  # no rapid retry loop against a rejected token


async def test_unreachable_home_assistant(aiohttp_client: Any, unused_tcp_port: int) -> None:
    from smart_lighting.settings import load_settings

    settings = load_settings({
        "SUPERVISOR_TOKEN": TOKEN,
        "SL_HA_WS_URL": f"ws://127.0.0.1:{unused_tcp_port}/core/websocket",
        "SL_SUPERVISOR_URL": f"http://127.0.0.1:{unused_tcp_port}",
        "SL_ALLOWED_CLIENTS": "127.0.0.1,::1",
    })
    client = await aiohttp_client(build_app(settings))
    core = client.app[CORE_KEY]
    await wait_for(lambda: core.ha.state == "disconnected")
    health = await (await client.get("/api/health")).json()
    assert health["status"] == "warning"
    # The interface still loads and explains the problem.
    assert (await client.get("/")).status == 200


async def test_without_token(aiohttp_client: Any) -> None:
    from smart_lighting.settings import load_settings

    settings = load_settings({"SL_ALLOWED_CLIENTS": "127.0.0.1,::1", "SL_SUPERVISOR_URL": ""})
    client = await aiohttp_client(build_app(settings))
    health = await (await client.get("/api/health")).json()
    assert health["status"] == "error"
    assert health["home_assistant"] == "not_configured"


async def test_only_ingress_clients_are_accepted(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    # Production default: only the Supervisor's Ingress address (172.30.32.2).
    settings = make_settings(fake_ha, SL_ALLOWED_CLIENTS="172.30.32.2")
    client = await aiohttp_client(build_app(settings))
    for path in ("/", "/api/health", "/api/overview", "/api/live"):
        resp = await client.get(path)
        assert resp.status == 403, path
        assert "Home Assistant sidebar" in await resp.text()


async def test_lookup_for_searchable_selectors(app_client: Any) -> None:
    data = await (await app_client.get("/api/lookup")).json()
    assert [a["name"] for a in data["areas"]] == ["Kamar Norður", "Stova", "Úti"]
    kamar = data["areas"][0]
    assert kamar["device_count"] == 3 and kamar["entity_count"] == 5  # devices' entities count via the device
    devices = {d["id"]: d for d in data["devices"]}
    assert set(devices) == {"dev-loft", "dev-lux", "dev-pir", "dev-radar", "dev-door"}  # disabled ones left out
    assert devices["dev-loft"]["name"] == "Loft ljós"  # the user's own name wins
    assert devices["dev-loft"]["area"] == "Kamar Norður"
    assert devices["dev-lux"]["entity_count"] == 1
    entities = {e["entity_id"]: e for e in data["entities"]}
    assert "switch.disabled_switch" not in entities
    assert entities["switch.hidden_registry_only"]["name"] == "Registry only"
    assert entities["switch.hidden_registry_only"]["state"] is None
    assert entities["light.kamar_nordur_loft"]["area"] == "Kamar Norður"
    assert entities["light.kamar_nordur_loft"]["device"] == "Loft ljós"
    assert entities["sensor.outdoor_lux"]["area"] == "Úti"  # the entity's own area wins
    assert entities["sensor.outdoor_lux"]["unit"] == "lx"
    assert entities["binary_sensor.kamar_nordur_occupancy"]["platform"] == "smart_occupancy"


async def test_lookup_without_connection(aiohttp_client: Any) -> None:
    from smart_lighting.settings import load_settings

    client = await aiohttp_client(build_app(load_settings({"SL_ALLOWED_CLIENTS": "127.0.0.1,::1", "SL_SUPERVISOR_URL": ""})))
    resp = await client.get("/api/lookup")
    assert resp.status == 503


async def test_supervisor_error_page_is_reported_clearly(fake_ha: FakeHomeAssistant, aiohttp_server: Any) -> None:
    from aiohttp import web as aioweb

    from smart_lighting.supervisor import SupervisorClient

    async def broken(request: aioweb.Request) -> aioweb.Response:
        return aioweb.Response(status=500, text="500 Internal Server Error\n\nServer got itself in trouble")

    app = aioweb.Application()
    app.router.add_get("/addons/self/info", broken)
    app.router.add_get("/info", broken)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = SupervisorClient(session, str(server.make_url("")).rstrip("/"), TOKEN)
        await client.refresh()
    assert client.error == "/addons/self/info: HTTP 500 (unexpected response); /info: HTTP 500 (unexpected response)"
    assert not client.available
