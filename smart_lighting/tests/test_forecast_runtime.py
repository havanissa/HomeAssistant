"""Phase A5: the App's forecast runtime against a simulated integration bridge (tests/fake_forecast.py).

* Shadow: the App's manager starts from the integration's learned days, learns today through the websocket
  history API only, compares its view with the integration's published one and publishes nothing.
* Handover needs readiness; then the App publishes its view and its learned data over the bridge.
* Losing control, release, return and reconnect behave as for lighting and curtains.
* Nothing here commands a device: the forecast bridge has no execute and the fake never sees a service call.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from smart_lighting.ha import FORECAST_BRIDGE_COMMANDS, ReadOnlyViolation
from smart_occupancy import forecast_sync as S

from .conftest import ADMIN_HEADERS, FakeHomeAssistant, wait_for
from .fake_engine import FakeEngine
from .fake_forecast import AREA, LEARNED_DAYS, FakeForecast
from .test_engine import start_app, two_sensor_room

GUEST = {"X-Remote-User-Id": "user-plain-2", "X-Smart-Lighting": "1"}
FAST = {"SL_FORECAST_MIN_SHADOW_SECONDS": "0", "SL_FORECAST_MIN_MATCHES": "0"}


def shadow_running(client: Any) -> bool:
    runtime = client.core.forecast
    return runtime.mode == "shadow" and runtime.instance is not None and runtime.instance.started


@pytest.fixture
async def forecast_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    two_sensor_room(fake_ha)
    fake_ha.forecast = FakeForecast(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: shadow_running(client))
    yield client
    fake_ha.engine.stop()


def rt(client: Any):
    return client.core.forecast


async def handover(client: Any) -> dict[str, Any]:
    resp = await client.post("/api/predictions/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200, body
    return body["result"]


async def command(client: Any, headers: dict[str, str] = ADMIN_HEADERS, **msg: Any) -> tuple[int, Any]:
    resp = await client.post("/api/predictions/command", json=msg, headers=headers)
    return resp.status, await resp.json()


def device_commands(fake: FakeHomeAssistant) -> list[dict[str, Any]]:
    return [c for c in fake.commands if c["type"] == "call_service" or "execute" in c["type"]]


# ----------------------------------------------------------------------------------------------------- shadow
async def test_shadow_learns_through_history_and_compares(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    fake = fake_ha.forecast
    assert runtime.mode == "shadow" and runtime.comparator.running and runtime.instance.role == "shadow"
    assert sorted(runtime.manager.days) == sorted(fake.days) and len(runtime.manager.days) == LEARNED_DAYS
    status = runtime.status()
    assert status["days_learned"] == LEARNED_DAYS and status["learned_revision"] == fake.learned_revision()
    assert status["settings_revision"] == S.revision(fake.settings) == status["integration_settings_revision"]
    assert runtime.manager.settings.as_dict() == fake.settings and runtime.recorder_oldest == fake.recorder_oldest
    checks = {c["key"]: c["status"] for c in status["readiness"]["checks"]}
    assert checks["fingerprint"] == "ok" and checks["learned"] == "ok" and checks["settings"] == "ok"
    # The integration published a view; the shadow learns today (history API only) and recomputes its own.
    await fake.send_view()
    await wait_for(lambda: runtime.integration_view is not None)
    before = len(fake_ha.history_calls)
    status_code, body = await command(forecast_app, type="smart_occupancy/forecast/learn")
    assert status_code == 200 and body["handled_by"] == "integration", body  # forwarded, and mirrored on the shadow
    assert fake_ha.forecast_editor_calls[-1]["type"] == "smart_occupancy/forecast/learn"
    calls = fake_ha.history_calls[before:]
    assert calls and runtime.instance.history_calls == len(calls) and runtime.status()["history_calls"] == len(calls)
    for call in calls:
        assert call["type"] == "history/history_during_period"
        assert call["minimal_response"] and call["no_attributes"] and call["significant_changes_only"]
        assert call["include_start_time_state"] is True
        start, end = datetime.fromisoformat(call["start_time"]), datetime.fromisoformat(call["end_time"])
        assert 0 < (end - start).total_seconds() <= 26 * 3600 + 60
        assert set(call["entity_ids"]) >= {"binary_sensor.kamar_pir", "binary_sensor.kamar_radar"}
    mgr = runtime.manager
    assert mgr.today is not None and mgr.status.state == "ready" and len(mgr.days) == LEARNED_DAYS  # today is not stored
    assert runtime.status()["learned_revision"] == fake.learned_revision()  # still identical to the integration's
    # Its own view went to the comparison together with the integration's.
    await wait_for(lambda: runtime.comparator.pairs >= 1)
    report = runtime.comparator.report()
    assert report["agreements"] + report["disagreements"] >= 1 and AREA in report["rooms"]
    assert report["rooms"][AREA]["integration"]["probability"] == 40
    assert fake.published == [] and device_commands(fake_ha) == []
    assert not [c for c in fake_ha.commands if c["type"].startswith("smart_occupancy/forecast/bridge/")
                and not c["type"].endswith(("/subscribe", "/status"))]
    # The pages: the forecasts that count come from the integration's view while it produces them.
    resp = await forecast_app.get("/api/predictions")
    body = await resp.json()
    assert resp.status == 200 and body["mode"] == "shadow" and body["commands_go_to"] == "integration"
    house = body["house"]
    assert house["source"] == "integration" and [r["area_id"] for r in house["rooms"]] == [AREA]
    assert house["rooms"][0]["now"] == fake.view["rooms"][AREA] and house["rooms"][0]["shadow_now"] is not None
    assert house["coverage"]["days_learned"] == LEARNED_DAYS and house["coverage"]["today_included"] is True
    assert body["acknowledged_owner"] == S.OWNER_INTEGRATION
    resp = await forecast_app.get(f"/api/predictions/rooms/{AREA}")
    room = await resp.json()
    assert resp.status == 200, room
    assert {"patterns", "accuracy", "settings", "movement", "learning", "log"} <= set(room)
    assert room["source"] == "integration" and room["now"] == fake.view["rooms"][AREA]
    assert room["shadow_now"] is not None and room["shadow_now"] != room["now"]
    assert room["learning"]["days_learned"] == LEARNED_DAYS
    resp = await forecast_app.get("/api/predictions/rooms/stova")
    assert resp.status == 404


async def test_settings_follow_the_integration(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    fake = fake_ha.forecast
    # A settings change in the integration arrives as an event.
    changed = {**fake.settings, "global": {**fake.settings["global"], "probability_threshold": 70}}
    await fake.set_settings(changed)
    await wait_for(lambda: runtime.manager.settings.defaults.probability_threshold == 70)
    assert runtime.integration_settings_revision == S.revision(fake.settings)
    assert runtime.status()["settings_revision"] == runtime.integration_settings_revision
    # A change through the App's interface is forwarded to the integration and mirrored on the shadow.
    status, body = await command(forecast_app, type="smart_occupancy/forecast/settings", area_id=AREA,
                                 values={"horizon_minutes": 30})
    assert status == 200 and body["handled_by"] == "integration", body
    assert fake_ha.forecast_editor_calls[-1]["values"] == {"horizon_minutes": 30}
    assert runtime.manager.settings.effective(AREA).horizon_minutes == 30
    status, body = await command(forecast_app, GUEST, type="smart_occupancy/forecast/settings", values={})
    assert status == 403
    status, body = await command(forecast_app, type="smart_occupancy/forecast/bridge/publish", view={})
    assert status == 403 and body["code"] == "not_allowed"
    status, body = await command(forecast_app, type="call_service", domain="light", service="turn_on")
    assert status == 403 and body["code"] == "not_allowed"
    assert fake.published == [] and device_commands(fake_ha) == []


# --------------------------------------------------------------------------------------------------- handover
async def test_handover_then_the_app_publishes(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    fake = fake_ha.forecast
    hello_days = sorted(fake.days)
    result = await handover(forecast_app)
    copy = result["copy"]
    assert copy["identical"] and copy["differences"] == [] and copy["source_days"] == copy["destination_days"] == LEARNED_DAYS
    assert copy["source_revision"] == copy["destination_revision"] == runtime.status()["learned_revision"]
    assert runtime.mode == "owner" and fake.owner == S.OWNER_APP and runtime.instance.role == "owner"
    assert runtime.epoch == fake.epoch and runtime.status()["last_copy"] == copy
    assert forecast_app.core.safety()["forecast_owner"] == "app"
    assert forecast_app.core.safety()["device_control"] is False  # forecasts never command anything
    await wait_for(lambda: {k for p in fake.published for k in ("view", "export") if p.get(k) is not None}
                   >= {"view", "export"}, timeout=8)
    export = next(p["export"] for p in fake.published if p.get("export") is not None)
    view = next(p["view"] for p in fake.published if p.get("view") is not None)
    assert sorted(export["days"]) == hello_days and export["schema"] == S.EXPORT_SCHEMA
    assert set(view) <= S.VIEW_SECTIONS and AREA in view["rooms"] and view["days_learned"] == LEARNED_DAYS
    assert runtime.counters["views_published"] >= 1 and runtime.counters["exports_published"] >= 1
    house = runtime.house()
    assert house["source"] == "app" and house["rooms"][0]["now"] is not None and house["rooms"][0]["shadow_now"] is None
    body = await (await forecast_app.get("/api/predictions")).json()
    assert body["commands_go_to"] == "app" and body["acknowledged_owner"] == S.OWNER_APP
    room = await (await forecast_app.get(f"/api/predictions/rooms/{AREA}")).json()
    assert room["source"] == "app" and room["shadow_now"] is None
    # Settings changes now run locally and reach the integration's mirror with the next view.
    forwarded = len(fake_ha.forecast_editor_calls)
    status, body = await command(forecast_app, type="smart_occupancy/forecast/settings",
                                 values={"probability_threshold": 55})
    assert status == 200 and body["handled_by"] == "app", body
    assert runtime.manager.settings.defaults.probability_threshold == 55
    assert len(fake_ha.forecast_editor_calls) == forwarded
    await wait_for(lambda: fake.view["settings"]["global"]["probability_threshold"] == 55, timeout=8)
    assert device_commands(fake_ha) == []
    assert any("handover" in b["id"] for b in runtime.backups())


async def test_handover_refused_when_not_ready(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    two_sensor_room(fake_ha)
    fake_ha.forecast = FakeForecast(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)  # the default thresholds: 1 h and 10 agreeing forecasts
    await wait_for(lambda: shadow_running(client))
    resp = await client.post("/api/predictions/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "not_ready"
    assert fake_ha.forecast.owner == S.OWNER_INTEGRATION and client.core.forecast.mode == "shadow"
    resp = await client.post("/api/predictions/handover", json={}, headers=ADMIN_HEADERS)
    assert (await resp.json())["code"] == "confirmation_required"
    resp = await client.post("/api/predictions/handover", json={"confirm": True}, headers=GUEST)
    assert resp.status == 403
    fake_ha.engine.stop()


# ------------------------------------------------------------------------------------------------ losing control
async def test_losing_control_stops_publishing(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    await handover(forecast_app)
    await wait_for(lambda: fake_ha.forecast.published)
    await fake_ha.forecast.take_back()
    await wait_for(lambda: runtime.mode == "shadow")
    assert runtime.ownership_log[0]["event"] == "lost"
    await wait_for(lambda: shadow_running(forecast_app))
    published = len(fake_ha.forecast.published)
    status, body = await command(forecast_app, type="smart_occupancy/forecast/settings", values={"probability_threshold": 45})
    assert status == 200 and body["handled_by"] == "integration"
    await asyncio.sleep(2.5)
    assert len(fake_ha.forecast.published) == published  # nothing more is published
    assert runtime.house()["source"] == "integration"


async def test_refused_publish_with_a_lost_code_stops_the_app(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    await handover(forecast_app)
    await wait_for(lambda: any(p.get("view") is not None for p in fake_ha.forecast.published))
    fake_ha.forecast.refuse_publish = S.REJECT_STALE_EPOCH
    status, body = await command(forecast_app, type="smart_occupancy/forecast/settings", values={"probability_threshold": 45})
    assert status == 200 and body["handled_by"] == "app"
    await wait_for(lambda: runtime.mode == "shadow", timeout=8)
    assert runtime.ownership_log[0]["event"] == "lost" and S.REJECT_STALE_EPOCH in runtime.ownership_log[0]["reason"]
    await wait_for(lambda: shadow_running(forecast_app))


# ------------------------------------------------------------------------------------------------ release, return
async def test_release_hands_back_the_learned_data(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    fake = fake_ha.forecast
    await handover(forecast_app)
    resp = await forecast_app.post("/api/predictions/release", json={}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake.released == 1 and fake.owner == S.OWNER_INTEGRATION
    release = next(c for c in fake_ha.commands if c["type"] == "smart_occupancy/forecast/bridge/release")
    assert S.check_export(release["export"])["days"].keys() == fake.days.keys() and S.check_view(release["view"])
    await wait_for(lambda: shadow_running(forecast_app))
    assert runtime.ownership_log[0]["event"] == "released"
    assert any("release" in b["id"] for b in runtime.backups()) and any("handover" in b["id"] for b in runtime.backups())
    assert runtime.status()["learned_revision"] == fake.learned_revision()


async def test_return_control(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    resp = await forecast_app.post("/api/predictions/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION and runtime.mode == "shadow"
    await handover(forecast_app)
    resp = await forecast_app.post("/api/predictions/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake_ha.forecast.released == 1 and fake_ha.forecast.owner == S.OWNER_INTEGRATION  # a release, not a loss
    await wait_for(lambda: shadow_running(forecast_app))
    assert [e["event"] for e in runtime.ownership_log].count("lost") == 0


# ---------------------------------------------------------------------------------------------------- reconnect
async def test_reconnect_resumes_from_the_apps_own_store(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    await handover(forecast_app)
    epoch = fake_ha.forecast.epoch
    store = runtime.dir / "store"
    await wait_for(lambda: (store / "smart_occupancy.forecast_days").exists()
                   and (store / "smart_occupancy.forecast_settings").exists())
    await fake_ha.drop_connections()
    await wait_for(lambda: runtime.mode == "owner" and fake_ha.forecast.epoch == epoch + 1
                   and runtime.instance is not None and runtime.instance.started, timeout=10)
    assert runtime.ownership_log[0]["event"] == "resumed" and runtime.instance.role == "owner"
    assert runtime.instance.hass.storage_dir == str(store)
    assert sorted(runtime.manager.days) == sorted(fake_ha.forecast.days)  # from the App's own store
    assert runtime.status()["learned_revision"] == fake_ha.forecast.learned_revision()
    await wait_for(lambda: fake_ha.forecast.published and fake_ha.forecast.published[-1]["epoch"] == epoch + 1,
                   timeout=8)


# ------------------------------------------------------------------------------------------------ export, import
async def test_export_and_import(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(forecast_app)
    resp = await forecast_app.get("/api/predictions/export", headers=GUEST)
    assert resp.status == 403
    resp = await forecast_app.get("/api/predictions/export", headers=ADMIN_HEADERS)
    assert resp.status == 200 and "attachment" in resp.headers["Content-Disposition"]
    file = await resp.json()
    assert file["schema"] == 1 and file["mode"] == "shadow"
    assert S.check_export(file["export"])["days"].keys() == fake_ha.forecast.days.keys() and S.check_view(file["view"])
    resp = await forecast_app.post("/api/predictions/import/review", json={"file": file}, headers=ADMIN_HEADERS)
    review = await resp.json()
    assert resp.status == 200, review
    assert review["days"] == LEARNED_DAYS and review["invalid_days"] == 0 and review["writable"] is False
    assert review["same"] is True and review["current_revision"] == review["incoming_revision"]
    resp = await forecast_app.post("/api/predictions/import/review", json={"file": {"schema": 7}}, headers=ADMIN_HEADERS)
    assert resp.status == 400 and (await resp.json())["code"] == "invalid_export"
    resp = await forecast_app.post("/api/predictions/import", json={"file": file, "revision": review["current_revision"]},
                                   headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "not_owner"
    await handover(forecast_app)
    # A file with one day fewer: applied as owner with the current revision, refused with a stale one.
    smaller = dict(file)
    smaller["export"] = {**file["export"], "days": dict(list(file["export"]["days"].items())[1:])}
    resp = await forecast_app.post("/api/predictions/import", json={"file": smaller, "revision": "0000000000000000"},
                                   headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "stale_revision"
    review = await (await forecast_app.post("/api/predictions/import/review", json={"file": smaller},
                                            headers=ADMIN_HEADERS)).json()
    assert review["writable"] is True and review["days"] == LEARNED_DAYS - 1 and review["same"] is False
    resp = await forecast_app.post("/api/predictions/import", json={"file": smaller, "revision": review["current_revision"]},
                                   headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200 and body == {"applied": True, "days": LEARNED_DAYS - 1}, body
    assert sorted(runtime.manager.days) == sorted(smaller["export"]["days"])
    assert runtime.status()["learned_revision"] == review["incoming_revision"]
    await wait_for(lambda: sorted(fake_ha.forecast.days) == sorted(smaller["export"]["days"]), timeout=8)
    assert any("import" in b["id"] for b in runtime.backups())


# -------------------------------------------------------------------------------------------------------- safety
async def test_no_device_control(forecast_app: Any, fake_ha: FakeHomeAssistant) -> None:
    ha = forecast_app.core.ha
    assert not any("execute" in c for c in FORECAST_BRIDGE_COMMANDS)
    assert FORECAST_BRIDGE_COMMANDS == {f"smart_occupancy/forecast/bridge/{c}" for c in (
        "claim", "heartbeat", "publish", "release", "return")}
    with pytest.raises(ReadOnlyViolation):
        await ha.command("smart_occupancy/forecast/bridge/publish", view={})
    with pytest.raises(ReadOnlyViolation):
        await ha.forecast_command("smart_occupancy/forecast/bridge/execute", domain="cover", service="open_cover")
    with pytest.raises(ReadOnlyViolation):
        await ha.forecast_command("call_service", domain="light", service="turn_on")
    safety = forecast_app.core.safety()
    assert safety["forecast_owner"] == "integration" and safety["forecast_mode"] == "shadow"
    assert safety["allowed_forecast_bridge_commands"] == sorted(FORECAST_BRIDGE_COMMANDS)
    await handover(forecast_app)
    await wait_for(lambda: fake_ha.forecast.published)
    safety = forecast_app.core.safety()
    assert safety["forecast_owner"] == "app" and safety["forecast_mode"] == "owner"
    assert safety["device_control"] is False and safety["service_calls_sent"] == 0
    assert device_commands(fake_ha) == []
