"""The fenced executor: allowlisted services of configured targets, with the App's context; nothing else."""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.smart_occupancy import protocol as P

from .conftest import FakeApp, setup_companion

CTX = "01KV5B0000000000000000ABCD"


async def test_light_commands_run_with_the_apps_context(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    calls = async_mock_service(hass, "light", "turn_on")
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    reply = await app.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp", "brightness_pct": 60},
                              purpose="lighting", area_id="kamar_nordur")
    assert reply["success"], reply
    assert len(calls) == 1 and calls[0].data == {"entity_id": "light.kamar_lamp", "brightness_pct": 60}
    assert calls[0].context.id == CTX
    own = (await app.send(type="smart_occupancy/companion/status"))["result"]["ownership"]["lighting"]
    assert own["executed"] == 1 and own["last_execute"]["purpose"] == "lighting"


async def test_the_policy_refuses_everything_else(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    scene = async_mock_service(hass, "scene", "turn_on")
    cover = async_mock_service(hass, "cover", "set_cover_position")
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting", "curtains", "forecast")
    cases = [
        ("lighting", "light", "turn_on", {"entity_id": "light.other"}, P.REJECT_NOT_ALLOWED),
        ("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp", "foo": 1}, P.REJECT_NOT_ALLOWED),
        ("lighting", "light", "turn_off", {"entity_id": "light.kamar_lamp", "brightness_pct": 5}, P.REJECT_NOT_ALLOWED),
        ("lighting", "light", "set_color", {"entity_id": "light.kamar_lamp"}, P.REJECT_NOT_ALLOWED),
        ("lighting", "switch", "turn_off", {"entity_id": "switch.relay"}, P.REJECT_NOT_ALLOWED),
        ("lighting", "scene", "turn_on", {"entity_id": "scene.movie"}, P.REJECT_NOT_ALLOWED),
        ("lighting", "cover", "open_cover", {"entity_id": "cover.kamar"}, P.REJECT_NOT_ALLOWED),
        ("lighting", "light", "turn_on", {"entity_id": "not an id"}, P.REJECT_INVALID),
        ("lighting", "homeassistant", "restart", {"entity_id": "light.kamar_lamp"}, P.REJECT_NOT_ALLOWED),
        ("curtains", "cover", "set_cover_position", {"entity_id": "cover.kamar", "position": 150}, P.REJECT_NOT_ALLOWED),
        ("curtains", "cover", "set_cover_position", {"entity_id": "cover.kamar", "position": 50.5}, P.REJECT_NOT_ALLOWED),
        ("curtains", "cover", "set_cover_position", {"entity_id": "cover.other", "position": 50}, P.REJECT_NOT_ALLOWED),
        ("curtains", "cover", "open_cover", {"entity_id": ["cover.kamar", "cover.kamar"]}, P.REJECT_NOT_ALLOWED),
        ("curtains", "cover", "stop_cover", {"entity_id": "cover.kamar"}, P.REJECT_NOT_ALLOWED),
        ("curtains", "light", "turn_on", {"entity_id": "light.kamar_lamp"}, P.REJECT_NOT_ALLOWED),
        ("forecast", "light", "turn_on", {"entity_id": "light.kamar_lamp"}, P.REJECT_NOT_ALLOWED),
    ]
    for domain, sd, service, data, code in cases:
        reply = await app.execute(domain, sd, service, data)
        assert not reply["success"] and reply["error"]["code"] == code, (domain, sd, service, data, reply)
    assert on == [] and off == [] and scene == [] and cover == []
    # A refused command consumes no sequence number: the next good one is accepted.
    reply = await app.execute("curtains", "cover", "set_cover_position", {"entity_id": "cover.kamar", "position": 40})
    assert reply["success"], reply
    assert len(cover) == 1 and cover[0].data["position"] == 40
    # A bad context id is refused before anything runs.
    reply = await app.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp"}, context_id="x" * 26)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_INVALID and on == []


async def test_targets_follow_the_published_configuration(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    on = async_mock_service(hass, "light", "turn_on")
    select = async_mock_service(hass, "select", "select_option")
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    reply = await app.execute("lighting", "light", "turn_on", {"entity_id": "light.new_lamp"})
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_NOT_ALLOWED
    from .conftest import lighting_view

    reply = await app.publish("lighting", lighting_view(), targets={"lights": ["light.kamar_lamp", "light.new_lamp"],
                                                                     "relays": [], "actions": ["select.smart_lighting_mood"]},
                              config_revision="def456")
    assert reply["success"], reply
    assert (await app.execute("lighting", "light", "turn_on", {"entity_id": "light.new_lamp"}))["success"]
    assert (await app.execute("lighting", "select", "select_option",
                              {"entity_id": "select.smart_lighting_mood", "option": "Kvøld"}))["success"]
    assert len(on) == 1 and len(select) == 1
    own = (await app.send(type="smart_occupancy/companion/status"))["result"]["ownership"]["lighting"]
    assert own["config_revision"] == "def456" and own["targets"]["lights"] == 2


async def test_a_failing_or_slow_service_is_reported(hass: HomeAssistant, hass_ws_client) -> None:
    await setup_companion(hass)
    from homeassistant.exceptions import HomeAssistantError

    async def failing(call: ServiceCall) -> None:
        raise HomeAssistantError("device unreachable")

    hass.services.async_register("light", "turn_on", failing)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    reply = await app.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp"})
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_FAILED
    assert "unreachable" in reply["error"]["message"]
    own = (await app.send(type="smart_occupancy/companion/status"))["result"]["ownership"]["lighting"]
    assert own["execute_errors"] == 1 and own["last_execute"]["error"].startswith("device")
    # The sequence advanced (the command was accepted and attempted): a replay with the same seq is refused.
    seq = app.seq["lighting"]
    reply = await app.send(type="smart_occupancy/companion/execute", **{**app.owner_params("lighting"), "seq": seq},
                           service_domain="light", service="turn_on", data={"entity_id": "light.kamar_lamp"},
                           context_id=CTX)
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_SEQUENCE


async def test_no_command_without_a_live_session(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    await setup_companion(hass)
    on = async_mock_service(hass, "light", "turn_on")
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    from .conftest import tick

    await tick(hass, freezer, P.LEASE_SECONDS + 5)
    reply = await app.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp"})
    assert not reply["success"] and reply["error"]["code"] == P.REJECT_LEASE and on == []
    assert (await app.claim("lighting"))["success"]
    assert (await app.execute("lighting", "light", "turn_on", {"entity_id": "light.kamar_lamp"}))["success"]
    assert len(on) == 1
    await asyncio.sleep(0)
