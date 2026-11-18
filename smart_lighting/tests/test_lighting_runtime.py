"""Phase A4: the App's lighting runtime against a simulated integration bridge (tests/fake_lighting.py).

* Shadow: a fresh controller from the integration's copy; it never sends anything; the comparison runs.
* Handover needs readiness; then the App's controller commands only through ``execute`` (with its own
  context), publishes its configuration and state, and sends its occupancy inputs.
* Losing control (the integration takes it back, or refuses with a "not the owner" code) stops the App's
  controller at once; release and return give control back with the final state.
* A reconnect resumes with the lease token; a new App process never does.
* The interface's lighting commands go to the integration in shadow mode and run in the App after the handover.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from smart_occupancy import lighting_sync as S

from .conftest import ADMIN_HEADERS, ADMIN_ID, FakeHomeAssistant, make_settings, wait_for
from .fake_engine import FakeEngine
from .fake_lighting import AREA, LAMP, MAIN, FakeLighting
from .test_engine import PIR, RADAR, start_app, two_sensor_room


@pytest.fixture
async def lit_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    two_sensor_room(fake_ha)
    fake_ha.lighting = FakeLighting(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, SL_ENGINE_MIN_SHADOW_SECONDS="0", SL_ENGINE_MIN_TRANSITIONS="0",
                             SL_LIGHTING_MIN_SHADOW_SECONDS="0", SL_LIGHTING_MIN_COMMANDS="0")
    await wait_for(lambda: client.core.lighting.mode == "shadow" and client.core.lighting.instance is not None
                   and client.core.lighting.instance.started)
    yield client
    fake_ha.engine.stop()


def rt(client: Any):
    return client.core.lighting


async def mood(fake: FakeHomeAssistant, value: str) -> None:
    await fake.emit_live_context(MAIN, value, {"friendly_name": "Main Mood", "options": ["Dagur", "Hygge"]},
                                 "01KV5B0000000000000000MOOD")


async def occupy(client: Any, fake: FakeHomeAssistant) -> None:
    await fake.emit_live(PIR, "on")
    await fake.emit_live(RADAR, "on")
    occupancy = client.core.occupancy
    await wait_for(lambda: (occupancy.published_wire(AREA) or {}).get("occupied") is True, timeout=8)


async def handover(client: Any) -> dict[str, Any]:
    resp = await client.post("/api/lighting/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200, body
    return body["result"]


async def test_shadow_never_sends_and_compares(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(lit_app)
    assert runtime.mode == "shadow" and runtime.comparator.running
    assert runtime.config_doc() == fake_ha.lighting.config
    await mood(fake_ha, "Hygge")
    await asyncio.sleep(1.5)
    assert fake_ha.lighting.executed == []  # the shadow's commands go to the comparison only
    assert not [c for c in fake_ha.commands if c["type"].endswith("/execute")]
    status = await (await lit_app.get("/api/lighting")).json()
    assert status["mode"] == "shadow" and status["commands_go_to"] == "integration"
    assert {c["key"]: c["status"] for c in status["readiness"]["checks"]}["fingerprint"] == "ok"


async def test_handover_then_commands_only_through_execute(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(lit_app)
    result = await handover(lit_app)
    assert result["copy"]["identical"] and runtime.mode == "owner" and fake_ha.lighting.owner == S.OWNER_APP
    # Configuration and state mirrored at once (each on its own: a refused configuration never holds back the state)
    await wait_for(lambda: {k for p in fake_ha.lighting.published for k in p} >= {"config", "state"})
    first = next(p for p in fake_ha.lighting.published if "config" in p)
    assert first["config_revision"] == S.config_revision(first["config"]) and "state" not in first
    await occupy(lit_app, fake_ha)  # a visit: the App's controller switches the lamp on (Dagur, 60 %)
    await wait_for(lambda: fake_ha.lighting.executed, timeout=8)
    assert fake_ha.lighting.executed[-1]["data"]["brightness_pct"] == 60
    await mood(fake_ha, "Hygge")
    await wait_for(lambda: len(fake_ha.lighting.executed) >= 2, timeout=8)
    cmd = fake_ha.lighting.executed[-1]
    assert cmd["domain"] == "light" and cmd["data"]["entity_id"] == [LAMP] and cmd["data"]["brightness_pct"] == 25
    assert cmd["epoch"] == fake_ha.lighting.epoch and len(cmd["context_id"]) == 26
    # The report of its own command (with its context) is not a manual override.
    await asyncio.sleep(0.5)
    assert LAMP not in runtime.controller.runtime[AREA].overrides
    await wait_for(lambda: fake_ha.lighting.inputs)  # the occupancy inputs went to the integration
    assert {i["kind"] for i in fake_ha.lighting.inputs} >= {"preliminary"}


async def test_losing_control_stops_the_app_at_once(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(lit_app)
    await handover(lit_app)
    await fake_ha.lighting.take_back()
    await wait_for(lambda: runtime.mode == "shadow")
    executed = len(fake_ha.lighting.executed)
    await wait_for(lambda: runtime.instance is not None and runtime.instance.role == "shadow")
    await occupy(lit_app, fake_ha)
    await mood(fake_ha, "Hygge")
    await asyncio.sleep(1.5)
    assert len(fake_ha.lighting.executed) == executed  # nothing more is sent
    assert runtime.ownership_log[0]["event"] == "lost"


async def test_refused_command_with_a_lost_code_stops_the_app(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(lit_app)
    await handover(lit_app)
    fake_ha.lighting.refuse_execute = S.REJECT_STALE_EPOCH
    await occupy(lit_app, fake_ha)
    await wait_for(lambda: runtime.mode == "shadow", timeout=8)


async def test_release_hands_back_the_final_state(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(lit_app)
    await handover(lit_app)
    resp = await lit_app.post("/api/lighting/release", json={}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake_ha.lighting.released == 1 and fake_ha.lighting.owner == S.OWNER_INTEGRATION
    await wait_for(lambda: runtime.mode == "shadow" and runtime.instance is not None
                   and runtime.instance.role == "shadow")
    backups = runtime.backups()
    assert any("release" in b["id"] for b in backups) and any("handover" in b["id"] for b in backups)


async def test_return_as_owner_hands_back_the_final_state(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    """"Return" while the App controls lighting is a release: the last seconds of state are not lost."""
    runtime = rt(lit_app)
    await handover(lit_app)
    resp = await lit_app.post("/api/lighting/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake_ha.lighting.released == 1 and fake_ha.lighting.owner == S.OWNER_INTEGRATION
    await wait_for(lambda: runtime.mode == "shadow" and runtime.instance is not None
                   and runtime.instance.role == "shadow")
    assert [e["event"] for e in runtime.ownership_log].count("lost") == 0


async def test_return_control_from_shadow_mode(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await lit_app.post("/api/lighting/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION


async def test_reconnect_resumes_and_a_new_process_does_not(lit_app: Any, fake_ha: FakeHomeAssistant,
                                                            aiohttp_client) -> None:
    runtime = rt(lit_app)
    await handover(lit_app)
    epoch = fake_ha.lighting.epoch
    await fake_ha.drop_connections()
    await wait_for(lambda: runtime.mode == "owner" and fake_ha.lighting.epoch == epoch + 1
                   and runtime.instance is not None and runtime.instance.started, timeout=10)
    assert runtime.ownership_log[0]["event"] == "resumed"
    await occupy(lit_app, fake_ha)
    await wait_for(lambda: fake_ha.lighting.executed and fake_ha.lighting.executed[-1]["epoch"] == epoch + 1,
                   timeout=8)


async def test_handover_refused_when_not_ready(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    two_sensor_room(fake_ha)
    fake_ha.lighting = FakeLighting(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)  # the default thresholds: 2 h and 10 matching commands
    await wait_for(lambda: client.core.lighting.mode == "shadow")
    resp = await client.post("/api/lighting/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "not_ready"
    assert fake_ha.lighting.owner == S.OWNER_INTEGRATION
    resp = await client.post("/api/lighting/handover", json={}, headers=ADMIN_HEADERS)
    assert (await resp.json())["code"] == "confirmation_required"
    fake_ha.engine.stop()


async def test_commands_follow_the_owner(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    async def command(**msg: Any) -> Any:
        resp = await lit_app.post("/api/lighting/command", json=msg, headers=ADMIN_HEADERS)
        return resp.status, await resp.json()

    status, body = await command(type="smart_occupancy/lighting/editor", area_id=AREA)
    assert status == 200 and body["handled_by"] == "integration" and fake_ha.lighting_editor_calls
    status, body = await command(type="smart_occupancy/lighting/bridge/execute")
    assert status == 403 and body["code"] == "not_allowed"
    status, body = await command(type="call_service")
    assert status == 403
    # A non-administrator may read but not change.
    guest = {"X-Remote-User-Id": "user-plain-2", "X-Smart-Lighting": "1"}
    resp = await lit_app.post("/api/lighting/command", json={"type": "smart_occupancy/lighting/settings/set",
                                                              "manual_override_minutes": 5}, headers=guest)
    assert resp.status == 403
    await handover(lit_app)
    status, body = await command(type="smart_occupancy/lighting/editor", area_id=AREA)
    assert status == 200 and body["handled_by"] == "app"
    model = body["result"]
    assert model["room"]["area_id"] == AREA and model["can_edit"] is True
    status, body = await command(type="smart_occupancy/lighting/settings/set", manual_override_minutes=42)
    assert status == 200, body
    assert rt(lit_app).controller.lighting.manual_override_minutes == 42
    await wait_for(lambda: fake_ha.lighting.config.get("manual_override_minutes") == 42, timeout=8)
    status, body = await command(type="smart_occupancy/lighting/settings/set", manual_override_minutes=-1)
    assert status == 400 and body["code"] == "invalid_format"


async def test_no_lighting_bridge_is_reported(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    client = await start_app(fake_ha, aiohttp_client)
    await wait_for(lambda: client.core.lighting.bridge == "unsupported", timeout=8)
    health = await (await client.get("/api/health")).json()
    check = next(c for c in health["checks"] if c["key"] == "lighting_runtime")
    assert check["status"] == "info" and "0.8.0" in check["detail"]


def test_settings_thresholds(fake_ha: FakeHomeAssistant) -> None:
    settings = make_settings(fake_ha, SL_LIGHTING_MIN_SHADOW_SECONDS="60", SL_LIGHTING_MIN_COMMANDS="3")
    assert (settings.lighting_min_shadow_seconds, settings.lighting_min_commands) == (60, 3)
    assert ADMIN_ID


async def test_bridge_events_start_timers_from_when_they_fired(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    """A configuration change (or occupancy result) that reached the App late: its timers count from when it
    fired in Home Assistant, as state events do (otherwise the shadow plans after the integration's reaction)."""
    from datetime import datetime
    from smart_occupancy.lighting_config import LightingConfig

    runtime = rt(lit_app)
    await wait_for(lambda: runtime.mode == "shadow" and runtime.instance is not None and runtime.instance.started)
    inst = runtime.instance
    seen: list[Any] = []
    inst.controller.store.async_add_listener(lambda _o, _n: seen.append(inst.hass.event_time))
    doc = inst.controller.lighting.as_dict()
    doc["manual_override_minutes"] = (doc.get("manual_override_minutes") or 0) + 1
    fired = "2026-06-15T10:00:00.250000+00:00"
    inst.config_changed(LightingConfig.from_dict(doc), fired)
    assert seen == [datetime.fromisoformat(fired)] and inst.hass.event_time is None
    inst.config_changed(LightingConfig.from_dict(doc), "not a time")  # (unchanged: no listener; no crash)
    assert inst.hass.event_time is None
