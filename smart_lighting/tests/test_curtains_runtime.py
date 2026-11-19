"""Phase A5: the App's curtain runtime against a simulated integration bridge (tests/fake_curtains.py).

* Shadow: a fresh manager from the integration's copy; it never sends anything; the comparison runs.
* Handover needs readiness; then the App's manager moves a cover only through ``execute`` (with its own
  context) and publishes its configuration and state.
* Losing control (the integration takes it back, or refuses with a "not the owner" code) stops the App's
  manager at once; release and return give control back with the final state.
* A reconnect resumes with the lease token and restarts the manager from the App's own store.
* The interface's curtain commands go to the integration in shadow mode and run in the App after the handover.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from smart_lighting.ha import CURTAIN_BRIDGE_COMMANDS, ReadOnlyViolation
from smart_occupancy import curtain_sync as S

from .conftest import ADMIN_HEADERS, FakeHomeAssistant, wait_for
from .fake_curtains import AREA, COVER, LAYER, WINDOW, FakeCurtains, curtain_doc
from .fake_engine import FakeEngine
from .test_engine import start_app, two_sensor_room

GUEST = {"X-Remote-User-Id": "user-plain-2", "X-Smart-Lighting": "1"}
FAST = {"SL_CURTAINS_MIN_SHADOW_SECONDS": "0", "SL_CURTAINS_MIN_DECISIONS": "0"}


def shadow_running(client: Any) -> bool:
    runtime = client.core.curtains
    return runtime.mode == "shadow" and runtime.instance is not None and runtime.instance.started


@pytest.fixture
async def curtain_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    two_sensor_room(fake_ha)
    fake_ha.curtains = FakeCurtains(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: shadow_running(client))
    yield client
    fake_ha.engine.stop()


def rt(client: Any):
    return client.core.curtains


async def handover(client: Any) -> dict[str, Any]:
    resp = await client.post("/api/curtains/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200, body
    return body["result"]


async def command(client: Any, headers: dict[str, str] = ADMIN_HEADERS, **msg: Any) -> tuple[int, Any]:
    resp = await client.post("/api/curtains/command", json=msg, headers=headers)
    return resp.status, await resp.json()


def executes(fake: FakeHomeAssistant) -> list[dict[str, Any]]:
    return [c for c in fake.commands if c["type"].endswith("/execute") or c["type"] == "call_service"]


def record(position: int, purpose: str = "automatic", context_id: str = "01KV5B0000000000000000CTX1") -> dict[str, Any]:
    return {"domain": "cover", "service": "set_cover_position", "data": {"entity_id": COVER, "position": position},
            "context_id": context_id, "entity_id": COVER, "target": position, "layer_id": LAYER, "purpose": purpose,
            "window_id": WINDOW}


# ----------------------------------------------------------------------------------------------------- shadow
async def test_shadow_follows_the_integration_and_never_sends(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    assert runtime.mode == "shadow" and runtime.comparator.running
    assert runtime.config_doc() == fake_ha.curtains.config
    assert runtime.instance.role == "shadow" and runtime.manager.fenced is False
    # The integration enables Automatic for the window and maps its presets: the shadow's copy follows.
    changed = curtain_doc("automatic", conditions={"day": "p_open", "night": "p_closed"})
    await fake_ha.curtains.set_config(changed)
    await wait_for(lambda: runtime.config_doc() == changed)
    assert runtime.manager.config.windows[WINDOW].mode == "automatic"
    assert runtime.integration_revision == S.config_revision(changed)
    # Events the rules react to: a cover report, an occupancy change, the sun.
    await fake_ha.emit_live_context(COVER, "open", {"friendly_name": "Curtain Kamar", "supported_features": 15,
                                                     "current_position": 60}, "01KV5B0000000000000000HAND")
    await fake_ha.emit_live("sun.sun", "below_horizon")
    await fake_ha.emit_live("binary_sensor.kamar_pir", "on")
    await asyncio.sleep(1.0)
    assert fake_ha.curtains.executed == [] and executes(fake_ha) == []
    assert runtime.comparator.running
    status = await (await curtain_app.get("/api/curtains")).json()
    assert status["mode"] == "shadow" and status["commands_go_to"] == "integration" and status["moving"] == []
    checks = {c["key"]: c["status"] for c in status["readiness"]["checks"]}
    assert checks["fingerprint"] == "ok" and checks["core"] == "ok" and checks["config"] == "ok"
    assert status["acknowledged_owner"] == S.OWNER_INTEGRATION and status["integration"]["moving"] == []
    assert curtain_app.core.safety()["device_control"] is False
    assert curtain_app.core.safety()["curtain_commands_sent"] == 0


async def test_comparator_matches_commands_and_decisions(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    comparator = runtime.comparator
    now = datetime.now(UTC)
    # The integration's command arrives over the bridge; the shadow's identical one is recorded where it is made.
    await fake_ha.curtains.broadcast({"type": "command", "command": record(0)})
    await wait_for(lambda: comparator.report()["commands"]["pending"]["integration"] == 1)
    comparator.command("app", record(0, context_id="01KV5B0000000000000000CTX2"), now)
    report = comparator.report(now)
    assert report["commands"]["matched"] == 1 and report["commands"]["mismatches"] == 0
    assert report["commands"]["by_purpose"] == {"automatic": 1}
    # Decisions: the same window status on both sides matches; a different one is a difference after the window.
    decision = {"kind": "status", "action": "would_move", "code": "observing", "condition": "night",
                "preset_id": "p_closed", "preset_name": "Closed", "text": "Observing"}
    await fake_ha.curtains.broadcast({"type": "decision", "window_id": WINDOW, "decision": decision})
    await wait_for(lambda: comparator._decisions["integration"].pending)  # noqa: SLF001
    comparator.decision("app", WINDOW, decision, now)
    comparator.decision("app", WINDOW, {**decision, "preset_id": "p_half"}, now)
    assert comparator.report(now)["decisions"] == {"matched": 1, "differences": 0}
    later = now + timedelta(seconds=11)
    assert comparator.report(later)["decisions"] == {"matched": 1, "differences": 1}
    assert comparator.report(later)["decision_differences"][0]["only"] == "app"
    # A command without a partner within the window is a mismatch, kept with the other side's recent commands.
    comparator.command("app", record(50, purpose="manual_confirmed"), later)
    assert runtime.readiness(later)["ready"] is True  # still pending
    sweep_at = later + timedelta(seconds=4)
    report = comparator.report(sweep_at)
    assert report["commands"]["mismatches"] == 1 and report["commands"]["mismatch_by_purpose"] == {"manual_confirmed": 1}
    assert report["discrepancies"][0]["only"] == "app" and report["discrepancies"][0]["command"]["position"] == 50
    assert report["discrepancies"][0]["other_side"][-1]["position"] == 0
    readiness = runtime.readiness(sweep_at)
    assert readiness["ready"] is False
    assert {c["key"]: c["status"] for c in readiness["checks"]}["mismatches"] == "error"
    assert readiness["summary"]["mismatches"] == 1 and readiness["summary"]["decision_differences"] == 1


# --------------------------------------------------------------------------------------------------- handover
async def test_handover_then_a_confirmed_preset_moves_only_through_execute(curtain_app: Any,
                                                                          fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    result = await handover(curtain_app)
    assert result["copy"]["identical"] and result["copy"]["windows"] == 1
    assert runtime.mode == "owner" and fake_ha.curtains.owner == S.OWNER_APP
    assert runtime.instance.role == "owner" and runtime.epoch == fake_ha.curtains.epoch
    await wait_for(lambda: {k for p in fake_ha.curtains.published for k in p} >= {"config", "state"})
    assert runtime.config_doc() == fake_ha.curtains.config
    assert curtain_app.core.safety()["device_control"] is True
    assert curtain_app.core.safety()["curtains_owner"] == "app"
    # The person reviews the preset and confirms exactly those targets.
    status, body = await command(curtain_app, type="smart_occupancy/curtains/preview", window_id=WINDOW,
                                 preset_id="p_closed")
    assert status == 200 and body["handled_by"] == "app", body
    preview = body["result"]
    assert preview["moves"] == 1 and preview["items"][0]["decision"] == "move"
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_closed", confirm=True, expected={LAYER: 0})
    assert status == 200 and body["handled_by"] == "app", body
    op = body["result"]
    assert op["origin"] == "manual_confirmed" and op["user_id"] == ADMIN_HEADERS["X-Remote-User-Id"]
    assert fake_ha.curtains.executed, "the cover was not commanded through the bridge"
    cmd = fake_ha.curtains.executed[-1]
    assert (cmd["domain"], cmd["service"], cmd["data"]) == ("cover", "set_cover_position",
                                                            {"entity_id": COVER, "position": 0})
    assert cmd["epoch"] == fake_ha.curtains.epoch and len(cmd["context_id"]) == 26 and cmd["context_id"] == op["context_id"]
    assert executes(fake_ha) == [c for c in fake_ha.commands if c["type"] == "smart_occupancy/curtains/bridge/execute"]
    # The cover's report (with the App's context) completes the operation and is not a manual change.
    await wait_for(lambda: runtime.manager.last[WINDOW]["status"] != "running")
    last = runtime.manager.last[WINDOW]
    assert last["items"][0]["status"] == "reached" and last["items"][0]["final_position"] == 0
    assert runtime.manager.manual == {} and runtime.moving_windows() == []
    assert runtime.counters["commands"] == 1 and curtain_app.core.safety()["curtain_commands_sent"] == 1
    # The state (the operation) reaches the integration's mirror.
    await wait_for(lambda: any("state" in p and p["state"].get("last", {}).get(WINDOW, {}).get("status") == "succeeded"
                               for p in fake_ha.curtains.published), timeout=8)
    assert last["status"] == "succeeded" and fake_ha.curtains.state["last"][WINDOW]["op_id"] == op["op_id"]


async def test_handover_refused_when_not_ready_or_busy(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    two_sensor_room(fake_ha)
    fake_ha.curtains = FakeCurtains(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)  # the default thresholds: 2 h and 5 matching decisions
    await wait_for(lambda: shadow_running(client))
    resp = await client.post("/api/curtains/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "not_ready"
    assert fake_ha.curtains.owner == S.OWNER_INTEGRATION and client.core.curtains.mode == "shadow"
    resp = await client.post("/api/curtains/handover", json={}, headers=ADMIN_HEADERS)
    assert (await resp.json())["code"] == "confirmation_required"
    fake_ha.engine.stop()


async def test_handover_refused_while_the_integration_moves_a_cover(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    fake_ha.curtains.moving = [WINDOW]
    resp = await curtain_app.post("/api/curtains/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 409 and body["code"] == S.REJECT_BUSY == "busy", body
    assert runtime.mode == "shadow" and fake_ha.curtains.owner == S.OWNER_INTEGRATION
    assert runtime.instance is not None and runtime.instance.role == "shadow"
    fake_ha.curtains.moving = []
    assert (await handover(curtain_app))["copy"]["identical"]


# ------------------------------------------------------------------------------------------------ losing control
async def test_losing_control_stops_the_app_at_once(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    await handover(curtain_app)
    owner_instance = runtime.instance
    await fake_ha.curtains.take_back()
    await wait_for(lambda: runtime.mode == "shadow")
    assert runtime.ownership_log[0]["event"] == "lost"
    await wait_for(lambda: shadow_running(curtain_app))
    assert runtime.instance is not owner_instance and owner_instance.manager.fenced
    executed = len(fake_ha.curtains.executed)
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_closed", confirm=True, expected={LAYER: 0})
    assert status == 200 and body["handled_by"] == "integration"  # forwarded, not executed by the App
    await asyncio.sleep(0.5)
    assert len(fake_ha.curtains.executed) == executed and executes(fake_ha) == []


async def test_refused_command_with_a_lost_code_stops_the_app(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    await handover(curtain_app)
    fake_ha.curtains.refuse_execute = S.REJECT_STALE_EPOCH
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_closed", confirm=True, expected={LAYER: 0})
    assert status == 200, body
    await wait_for(lambda: runtime.mode == "shadow", timeout=8)
    assert runtime.ownership_log[0]["event"] == "lost" and S.REJECT_STALE_EPOCH in runtime.ownership_log[0]["reason"]
    assert runtime.last_error["code"] == S.REJECT_STALE_EPOCH
    assert fake_ha.curtains.executed == []  # refused before anything moved
    await wait_for(lambda: shadow_running(curtain_app))


# ------------------------------------------------------------------------------------------------ release, return
async def test_release_hands_back_the_final_state(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    await handover(curtain_app)
    resp = await curtain_app.post("/api/curtains/release", json={}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake_ha.curtains.released == 1 and fake_ha.curtains.owner == S.OWNER_INTEGRATION
    await wait_for(lambda: shadow_running(curtain_app))
    assert runtime.ownership_log[0]["event"] == "released"
    backups = runtime.backups()
    assert any("release" in b["id"] for b in backups) and any("handover" in b["id"] for b in backups)
    assert set(fake_ha.curtains.state) <= S.STATE_SECTIONS and fake_ha.curtains.config == runtime.config_doc()


async def test_return_as_owner_is_a_release(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    await handover(curtain_app)
    resp = await curtain_app.post("/api/curtains/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200, await resp.text()
    assert fake_ha.curtains.released == 1 and fake_ha.curtains.owner == S.OWNER_INTEGRATION
    await wait_for(lambda: shadow_running(curtain_app))
    assert [e["event"] for e in runtime.ownership_log].count("lost") == 0


async def test_return_control_from_shadow_mode(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await curtain_app.post("/api/curtains/release", json={"mode": "return"}, headers=ADMIN_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION
    assert rt(curtain_app).mode == "shadow" and fake_ha.curtains.released == 0


async def test_return_while_a_cover_moves_is_refused_unless_forced(curtain_app: Any, fake_ha: FakeHomeAssistant,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Review finding 3: the integration's mirror lags the App, so a return during a movement is refused (code
    ``moving``) unless the person forces it."""
    monkeypatch.setattr(S, "MOVING_WAIT_SECONDS", 0.3)
    runtime = rt(curtain_app)
    await handover(curtain_app)
    fake_ha.curtains.stall = True
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_closed", confirm=True, expected={LAYER: 0})
    assert status == 200 and body["handled_by"] == "app", body
    await wait_for(lambda: runtime.moving_windows() == [WINDOW])
    for payload in ({"mode": "return"}, {}):
        resp = await curtain_app.post("/api/curtains/release", json=payload, headers=ADMIN_HEADERS)
        body = await resp.json()
        assert resp.status == 409 and body["code"] == "moving", body
    assert runtime.mode == "owner" and fake_ha.curtains.owner == S.OWNER_APP and not runtime.manager.fenced
    assert runtime.moving_windows() == [WINDOW]
    resp = await curtain_app.post("/api/curtains/release", json={"mode": "return", "force": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200, body
    assert body["result"]["ownership"]["owner"] == S.OWNER_INTEGRATION and fake_ha.curtains.owner == S.OWNER_INTEGRATION
    await wait_for(lambda: shadow_running(curtain_app))
    assert "returned" in [e["event"] for e in runtime.ownership_log]


# ---------------------------------------------------------------------------------------------------- reconnect
async def test_reconnect_resumes_from_the_apps_own_store(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    await handover(curtain_app)
    epoch = fake_ha.curtains.epoch
    store = runtime.dir / "store"
    await wait_for(lambda: (store / "smart_occupancy.curtains").exists())
    await fake_ha.drop_connections()
    await wait_for(lambda: runtime.mode == "owner" and fake_ha.curtains.epoch == epoch + 1
                   and runtime.instance is not None and runtime.instance.started, timeout=10)
    assert runtime.ownership_log[0]["event"] == "resumed" and runtime.instance.role == "owner"
    assert runtime.instance.hass.storage_dir == str(store)
    assert runtime.config_doc() == fake_ha.curtains.config  # the resumed manager starts from the App's own store
    assert runtime.epoch == epoch + 1
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_half", confirm=True, expected={LAYER: 50})
    assert status == 200 and body["handled_by"] == "app", body
    await wait_for(lambda: fake_ha.curtains.executed and fake_ha.curtains.executed[-1]["epoch"] == epoch + 1)


# ----------------------------------------------------------------------------------------------------- commands
async def test_commands_follow_the_owner(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    status, body = await command(curtain_app, type="smart_occupancy/curtains/model")
    assert status == 200 and body["handled_by"] == "integration"
    assert fake_ha.curtains_editor_calls[-1]["type"] == "smart_occupancy/curtains/model"
    status, body = await command(curtain_app, type="smart_occupancy/curtains/bridge/execute", domain="cover",
                                 service="open_cover", data={"entity_id": COVER})
    assert status == 403 and body["code"] == "not_allowed"
    status, body = await command(curtain_app, type="call_service", domain="cover", service="open_cover")
    assert status == 403 and body["code"] == "not_allowed"
    # A non-administrator may read but not change.
    status, body = await command(curtain_app, GUEST, type="smart_occupancy/curtains/manual/clear", window_id=WINDOW)
    assert status == 403
    status, body = await command(curtain_app, GUEST, type="smart_occupancy/curtains/model")
    assert status == 200 and body["handled_by"] == "integration"
    # Forwarded runtime changes also run on the shadow's copy (SHADOW_MIRRORED); a configuration change does not.
    api = curtain_app.core.curtains_api
    mirrored: list[str] = []
    original = api._local  # noqa: SLF001

    async def spy(cmd: str, params: dict[str, Any], **kw: Any) -> Any:
        mirrored.append(cmd)
        return await original(cmd, params, **kw)

    api._local = spy  # noqa: SLF001
    runtime.manager.manual[WINDOW] = {"at": "2026-11-17T10:00:00+00:00", "last": "2026-11-17T10:00:00+00:00",
                                      "entity_id": COVER, "name": "Curtain Kamar", "from": 100, "to": 40,
                                      "source": "person"}
    for cmd, extra in (("manual/clear", {"window_id": WINDOW}), ("settings", {"tolerance": 5})):
        status, body = await command(curtain_app, type=f"smart_occupancy/curtains/{cmd}", **extra)
        assert status == 200 and body["handled_by"] == "integration", body
    assert mirrored == ["smart_occupancy/curtains/manual/clear"]
    assert runtime.manager.manual == {}  # the shadow's copy was cleared too
    assert [m["type"] for m in fake_ha.curtains_editor_calls][-2:] == [
        "smart_occupancy/curtains/manual/clear", "smart_occupancy/curtains/settings"]
    assert runtime.manager.config.settings.tolerance == 3  # configuration changes arrive as config events only
    api._local = original  # noqa: SLF001
    # After the handover the same commands run in the App against its own manager.
    await handover(curtain_app)
    assert (await (await curtain_app.get("/api/curtains")).json())["commands_go_to"] == "app"
    status, body = await command(curtain_app, type="smart_occupancy/curtains/model")
    assert status == 200 and body["handled_by"] == "app"
    model = body["result"]
    assert model["can_edit"] is True and [w["window_id"] for w in model["windows"]] == [WINDOW]
    status, body = await command(curtain_app, type="smart_occupancy/curtains/settings", tolerance=5)
    assert status == 200 and body["handled_by"] == "app", body
    assert runtime.manager.config.settings.tolerance == 5
    await wait_for(lambda: fake_ha.curtains.config["settings"]["tolerance"] == 5, timeout=8)
    status, body = await command(curtain_app, type="smart_occupancy/curtains/apply", window_id=WINDOW,
                                 preset_id="p_closed", confirm=True, expected={LAYER: 50})
    assert status == 400 and body["code"] == "preset_changed"
    assert fake_ha.curtains.executed == []
    assert len(fake_ha.curtains_editor_calls) == 4  # nothing more was forwarded (the guest's change never was)


# ------------------------------------------------------------------------------------------------ export, import
async def test_export_and_import(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    runtime = rt(curtain_app)
    resp = await curtain_app.get("/api/curtains/export", headers=GUEST)
    assert resp.status == 403
    resp = await curtain_app.get("/api/curtains/export", headers=ADMIN_HEADERS)
    assert resp.status == 200 and "attachment" in resp.headers["Content-Disposition"]
    export = await resp.json()
    assert export["schema"] == 1 and export["mode"] == "shadow" and export["config"] == fake_ha.curtains.config
    assert export["config_revision"] == S.config_revision(export["config"]) and isinstance(export["state"], dict)
    # A file with a window of a cover that no longer exists.
    file = dict(export)
    file["config"] = dict(export["config"])
    file["config"]["windows"] = [*export["config"]["windows"], {
        "window_id": "w_other", "name": "Other", "area_id": "stova", "mode": "observing",
        "layers": [{"layer_id": "l_other", "entity_id": "cover.missing_one", "kind": "sheer"}],
        "presets": [{"preset_id": "p_o", "name": "Open", "positions": {"l_other": 100}}]}]
    resp = await curtain_app.post("/api/curtains/import/review", json={"file": file}, headers=ADMIN_HEADERS)
    review = await resp.json()
    assert resp.status == 200, review
    assert review["changes"] == ["windows"] and review["missing_entities"] == ["cover.missing_one"]
    assert review["windows"] == 2 and review["writable"] is False and review["same"] is False
    resp = await curtain_app.post("/api/curtains/import/review", json={"file": {"schema": 2}}, headers=ADMIN_HEADERS)
    assert resp.status == 400 and (await resp.json())["code"] == "invalid_export"
    resp = await curtain_app.post("/api/curtains/import", json={"file": file, "revision": review["revision"]},
                                  headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "not_owner"
    await handover(curtain_app)
    resp = await curtain_app.post("/api/curtains/import", json={"file": file, "revision": "0000000000000000"},
                                  headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "stale_revision"
    review = await (await curtain_app.post("/api/curtains/import/review", json={"file": file},
                                           headers=ADMIN_HEADERS)).json()
    assert review["writable"] is True
    resp = await curtain_app.post("/api/curtains/import", json={"file": file, "revision": review["revision"]},
                                  headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200 and body["applied"] is True, body
    assert body["revision"] == review["incoming_revision"] == S.config_revision(runtime.config_doc())
    assert sorted(runtime.manager.config.windows) == [WINDOW, "w_other"]
    await wait_for(lambda: S.config_revision(fake_ha.curtains.config) == body["revision"], timeout=8)
    assert any("import" in b["id"] for b in runtime.backups())
    # Review finding 4: a file that switches a window to Automatic restores it as Observing unless confirmed.
    auto_file = dict(file)
    auto_file["config"] = dict(file["config"])
    auto_file["config"]["windows"] = [{**w, "mode": "automatic"} if w["window_id"] == WINDOW else w
                                      for w in file["config"]["windows"]]
    assert runtime.manager.config.windows[WINDOW].mode == "observing"
    review = await (await curtain_app.post("/api/curtains/import/review", json={"file": auto_file},
                                           headers=ADMIN_HEADERS)).json()
    assert [w["id"] for w in review["automatic_windows"]] == [WINDOW]
    resp = await curtain_app.post("/api/curtains/import", json={"file": auto_file, "revision": review["revision"]},
                                  headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200 and body["observing_instead"] == [WINDOW], body
    assert runtime.manager.config.windows[WINDOW].mode == "observing"
    review = await (await curtain_app.post("/api/curtains/import/review", json={"file": auto_file},
                                           headers=ADMIN_HEADERS)).json()
    resp = await curtain_app.post("/api/curtains/import", json={"file": auto_file, "revision": review["revision"],
                                                                "confirm_automatic": True}, headers=ADMIN_HEADERS)
    body = await resp.json()
    assert resp.status == 200 and body["observing_instead"] == [], body
    assert runtime.manager.config.windows[WINDOW].mode == "automatic"
    review = await (await curtain_app.post("/api/curtains/import/review", json={"file": auto_file},
                                           headers=ADMIN_HEADERS)).json()
    assert review["automatic_windows"] == [] and review["same"]


# -------------------------------------------------------------------------------------------------------- safety
async def test_the_client_has_no_other_way_to_a_cover(curtain_app: Any, fake_ha: FakeHomeAssistant) -> None:
    ha = curtain_app.core.ha
    with pytest.raises(ReadOnlyViolation):
        await ha.command("smart_occupancy/curtains/bridge/execute", domain="cover", service="open_cover",
                         data={"entity_id": COVER}, context_id="01KV5B0000000000000000CTX1")
    with pytest.raises(ReadOnlyViolation):
        await ha.command("call_service", domain="cover", service="open_cover", service_data={"entity_id": COVER})
    with pytest.raises(ReadOnlyViolation):
        await ha.curtains_command("smart_occupancy/curtains/model")
    with pytest.raises(ReadOnlyViolation):
        await ha.config_write("smart_occupancy/curtains/bridge/execute", domain="cover", service="open_cover",
                              data={"entity_id": COVER})
    assert CURTAIN_BRIDGE_COMMANDS == {f"smart_occupancy/curtains/bridge/{c}" for c in (
        "claim", "execute", "heartbeat", "publish", "release", "return")}
    assert executes(fake_ha) == [] and fake_ha.curtains.executed == []
    safety = curtain_app.core.safety()
    assert safety["curtains_mode"] == "shadow" and safety["curtain_commands_sent"] == 0
    assert safety["allowed_curtain_bridge_commands"] == sorted(CURTAIN_BRIDGE_COMMANDS)
    assert AREA == "kamar_nordur"


async def test_unchanged_reports_move_last_reported_on_the_host(fake_ha: FakeHomeAssistant, aiohttp_client,
                                                                monkeypatch) -> None:
    """Phase A5: Home Assistant delivers ``state_reported`` to filtered listeners only. The client re-reads the
    states periodically and hands a moved ``last_reported`` to the runtimes without a state change, so the vendored
    environment judge does not call a steady sensor stale."""
    from datetime import UTC, datetime, timedelta

    import smart_lighting.ha as ha_module

    monkeypatch.setattr(ha_module, "REPORTED_REFRESH_SECONDS", 0.2)
    fake_ha.curtains = FakeCurtains(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)
    runtime = client.core.curtains
    await wait_for(lambda: runtime.mode == "shadow" and runtime.instance is not None and runtime.instance.started)
    hass = runtime.instance.hass
    cover = next(s for s in fake_ha.states if s["entity_id"] == "cover.kamar_curtain")
    before = hass.states.get("cover.kamar_curtain")
    later = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    cover["last_reported"] = later  # the same state and attributes reported again
    changes: list[str] = []
    hass.bus.async_listen("state_changed", lambda e: changes.append(e.data["entity_id"]))
    await wait_for(lambda: hass.states.get("cover.kamar_curtain").last_reported.isoformat() == later, timeout=8)
    after = hass.states.get("cover.kamar_curtain")
    assert after.last_updated == before.last_updated and after.state == before.state
    assert "cover.kamar_curtain" not in changes  # a report is not a state change
    assert fake_ha.curtains.executed == []
    fake_ha.engine.stop()
