"""Phase A3.2: the App's occupancy runtime, shadow comparison, handover, fencing and rollback (App side).

The fake Home Assistant runs the integration's side of the bridge with the
shared engine (``fake_engine.py``); the integration's real side is tested in
the integration's suite against real Home Assistant.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any

from aiohttp import web
import pytest

from occupancy_core import CORE_VERSION, fingerprint, golden
from occupancy_core import protocol as P
from occupancy_core.wire import spec_to_wire
from smart_lighting.core import AppCore
from smart_lighting.ha import ALLOWED_COMMANDS, ENGINE_COMMANDS
from smart_lighting.occupancy_compare import TOLERANCE_SECONDS, Comparator
from smart_lighting.web import build_app

from .conftest import ADMIN_HEADERS, USER_ID, FakeHomeAssistant, make_settings, wait_for
from .fake_engine import FakeEngine

REPO = Path(__file__).resolve().parents[2]
PIR, RADAR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar"
AREA = "kamar_nordur"


def two_sensor_room(fake: FakeHomeAssistant) -> None:
    fake.rooms_options[AREA]["sensors"] = [
        {"entity_id": PIR, "kind": "pir", "role": "trigger", "inverted": False},
        # Confirm vacancy: a cleared radar is exit evidence, so a visit ends after exit_confirmation (2 s).
        {"entity_id": RADAR, "kind": "radar", "role": "confirm_vacancy", "inverted": False},
    ]
    fake.rooms_options[AREA]["timing"] = {}
    for s in fake.states:
        if s["entity_id"] in (PIR, RADAR):
            s["state"] = "off"


async def start_app(fake: FakeHomeAssistant, aiohttp_client, **env: str) -> Any:
    settings = make_settings(fake, **env)
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
    client.core = core
    return client


@pytest.fixture
async def engine_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    two_sensor_room(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, SL_ENGINE_MIN_SHADOW_SECONDS="0",
                             SL_ENGINE_MIN_TRANSITIONS="2")
    await wait_for(lambda: client.core.occupancy.mode == "shadow")
    yield client
    fake_ha.engine.stop()


def runtime(client: Any):
    return client.core.occupancy


async def visit(fake: FakeHomeAssistant, rt) -> None:
    """Someone enters (PIR + radar), stays, leaves: both engines see the same events."""
    await fake.emit_live(PIR, "on")
    await fake.emit_live(RADAR, "on")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("state") == "confirmed_occupancy")
    await fake.emit_live(PIR, "off")
    await fake.emit_live(RADAR, "off")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("state") == "vacant", timeout=10)
    await wait_for(lambda: (rt.integration_results.get(AREA) or {}).get("state") == "vacant", timeout=10)


# ----------------------------------------------------------------- package
def test_vendored_engine_is_the_integrations() -> None:
    original = REPO / "custom_components/smart_occupancy/occupancy_core"
    copy = REPO / "smart_lighting/backend/occupancy_core"
    files = sorted(p.relative_to(original).as_posix() for p in original.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts)
    assert files and all((copy / f).read_bytes() == (original / f).read_bytes() for f in files)
    assert sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts) == files
    assert golden.self_test()["passed"]


def test_dockerfile_ships_the_engine() -> None:
    dockerfile = (REPO / "smart_lighting/Dockerfile").read_text(encoding="utf-8")
    assert "COPY backend/ /opt/smart_lighting/" in dockerfile  # occupancy_core sits in backend/
    assert "PYTHONPATH=/opt/smart_lighting" in dockerfile


# --------------------------------------------------------------- shadow
async def test_shadow_mode_compares_and_never_publishes(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    assert rt.self_test["passed"] and rt.bridge == "connected"
    assert rt.integration["owner"] == "integration"
    await visit(fake_ha, rt)
    summary = rt.comparator.summary(datetime.now(UTC))
    assert summary["running"] and summary["totals"]["transitions"] >= 3
    assert summary["totals"]["matched"] == summary["totals"]["transitions"]
    assert summary["totals"]["disagreements"] == 0
    assert fake_ha.engine.publishes == [] and fake_ha.engine.owner == "integration"
    types = {c["type"] for c in fake_ha.commands}
    assert not types & ENGINE_COMMANDS  # shadow mode: no claim, publish, heartbeat or release
    assert types <= ALLOWED_COMMANDS and not any("service" in t for t in types)
    data = await (await engine_app.get("/api/engine")).json()
    assert data["mode"] == "shadow" and data["acknowledged_owner"] == "integration"
    assert data["versions"]["core"] == CORE_VERSION and data["versions"]["fingerprint"] == fingerprint()
    assert data["config"]["in_sync"] is True and data["readiness"]["ready"] is True
    overview = await (await engine_app.get("/api/overview")).json()
    assert overview["safety"]["occupancy_engine"] == "integration" and overview["safety"]["device_control"] is False


async def test_inputs_from_the_integration_are_applied(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await fake_ha.emit_live(PIR, "on")
    await fake_ha.emit_live(RADAR, "on")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("state") == "confirmed_occupancy")
    fake_ha.engine.house.set_holds(AREA, frozenset({"Desk"}), datetime.now(UTC))
    await fake_ha.engine.send_input({"kind": "hold", "area_id": AREA, "zones": ["Desk"]})
    await fake_ha.engine.send_input({"kind": "preliminary", "signals": {AREA: [PIR]}})
    await wait_for(lambda: rt.house.engines[AREA].external_hold == frozenset({"Desk"}))
    await wait_for(lambda: rt.house.engines[AREA].preliminary == frozenset({PIR}))
    events = [e["kind"] for e in rt.comparator.rooms[AREA].events]
    assert "zone_hold" in events and "preliminary" in events and "state" in events


async def test_disagreement_is_recorded_with_its_events(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    # The integration's engine sees a motion the App never receives (a lost event): a real disagreement.
    await fake_ha.engine.on_state(PIR, "on", datetime.now(UTC))
    await wait_for(lambda: rt.integration_results[AREA]["state"] == "possible_occupancy")
    await asyncio.sleep(TOLERANCE_SECONDS + 0.5)
    summary = rt.comparator.summary(datetime.now(UTC))
    assert summary["open_disagreements"] == [AREA]
    ready = rt.readiness()
    assert not ready["ready"] and next(c for c in ready["checks"] if c["key"] == "agree_now")["status"] == "error"
    await fake_ha.emit_live(PIR, "on")  # the App catches up
    await wait_for(lambda: rt.comparator.rooms[AREA].disagreements == 1)
    record = rt.comparator.records()[0]
    assert record["kind"] == "disagreement" and record["duration_s"] > TOLERANCE_SECONDS
    assert record["integration"]["state"] in ("possible_occupancy", "confirmed_occupancy")
    assert record["app"]["state"] == "vacant"
    assert any(e["kind"] == "state" for e in record["events"])
    assert not rt.readiness()["ready"]  # a recent disagreement blocks the handover ...
    records = await (await engine_app.get("/api/engine/comparison")).json()
    assert records["records"][0]["kind"] == "disagreement"


# -------------------------------------------------------------- handover
async def test_handover_is_reviewed_admin_only_and_publishes(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    reply = await engine_app.post("/api/engine/handover", json={"confirm": True})  # no admin, no CSRF header
    assert reply.status == 403
    reply = await engine_app.post("/api/engine/handover", json={"confirm": True},
                                  headers={"X-Remote-User-Id": USER_ID, "X-Smart-Lighting": "1"})
    assert reply.status == 403  # not an administrator
    reply = await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert reply.status == 409 and (await reply.json())["code"] == "not_ready"  # no transitions compared yet
    await visit(fake_ha, rt)
    reply = await engine_app.post("/api/engine/handover", json={}, headers=ADMIN_HEADERS)
    assert reply.status == 400  # must be confirmed
    reply = await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert reply.status == 200, await reply.text()
    engine = fake_ha.engine
    assert engine.owner == "app" and rt.mode == "authoritative" and rt.epoch == engine.epoch
    assert engine.history[-1]["readiness"]["ready"] is True
    # The first publish is the integration engine's exact decision (no transition caused by the handover).
    assert engine.publishes and engine.publishes[0]["results"][AREA]["state"] == "vacant"
    first = len(engine.publishes)
    await fake_ha.emit_live(PIR, "on")
    await wait_for(lambda: len(engine.publishes) > first)
    seqs = [p["seq"] for p in engine.publishes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert engine.accepted[AREA]["state"] in ("possible_occupancy", "confirmed_occupancy")
    status = await (await engine_app.get("/api/engine")).json()
    assert status["acknowledged_owner"] == "app" and status["mode"] == "authoritative"
    overview = await (await engine_app.get("/api/overview")).json()
    presence = next(m for m in overview["modules"] if m["key"] == "presence")
    assert presence["automation_owner"] == "app" and overview["safety"]["occupancy_engine"] == "app"
    assert not any(c["type"].startswith(("call_service", "execute_script", "fire_event")) for c in fake_ha.commands)


async def test_heartbeat_keeps_the_lease(engine_app: Any, fake_ha: FakeHomeAssistant, monkeypatch) -> None:
    monkeypatch.setattr(P, "HEARTBEAT_SECONDS", 0.2)
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    await wait_for(lambda: fake_ha.engine.heartbeats >= 3)
    assert rt.mode == "authoritative"


async def test_fencing_returns_the_app_to_shadow_and_it_never_reclaims(engine_app: Any,
                                                                       fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    claims = sum(1 for c in fake_ha.commands if c["type"] == "smart_occupancy/engine/claim")
    await fake_ha.engine.fence()  # the integration takes over (e.g. the App was unresponsive)
    await wait_for(lambda: rt.mode == "shadow")
    assert rt.ownership_log[0]["event"] == "lost"
    await fake_ha.emit_live(PIR, "on")
    await asyncio.sleep(0.3)
    assert sum(1 for c in fake_ha.commands if c["type"] == "smart_occupancy/engine/claim") == claims
    assert fake_ha.engine.owner == "integration"


async def test_stale_publish_refusal_returns_to_shadow(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    fake_ha.engine.change_owner("integration", "lease_expired")  # fenced, but the App was not told
    await fake_ha.emit_live(PIR, "on")
    await wait_for(lambda: rt.mode == "shadow")
    assert rt.last_publish_error["code"] in (P.REJECT_NOT_OWNER, P.REJECT_STALE_EPOCH)


async def test_refused_results_return_control(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    """Results the integration cannot use: the App hands control back instead of freezing the rooms."""
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    fake_ha.engine.fail_publish = P.REJECT_INVALID
    await fake_ha.emit_live(PIR, "on")
    await wait_for(lambda: rt.mode == "shadow")
    await wait_for(lambda: fake_ha.engine.owner == "integration")
    assert fake_ha.engine.history[-1]["reason"] == P.REASON_RELEASE


async def test_config_refusal_is_sent_again(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    fake_ha.engine.fail_publish = P.REJECT_CONFIG
    count = len(fake_ha.engine.publishes)
    await fake_ha.emit_live(PIR, "on")
    await wait_for(lambda: rt.last_publish_error and rt.last_publish_error["code"] == P.REJECT_CONFIG)
    assert AREA in rt._pending  # noqa: SLF001 - kept to be sent again
    await fake_ha.engine.broadcast({"type": "spec", "spec": spec_to_wire(fake_ha.engine.house.spec)})
    await wait_for(lambda: len(fake_ha.engine.publishes) > count)
    assert rt.mode == "authoritative" and AREA in fake_ha.engine.publishes[-1]["results"]


async def test_return_control_to_integration(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    assert (await engine_app.post("/api/engine/release", json={})).status == 403
    reply = await engine_app.post("/api/engine/release", json={}, headers=ADMIN_HEADERS)
    assert reply.status == 200 and (await reply.json())["result"]["released"] is True
    assert fake_ha.engine.owner == "integration" and rt.mode == "shadow"
    # Again: harmless.
    reply = await engine_app.post("/api/engine/release", json={}, headers=ADMIN_HEADERS)
    assert reply.status == 200 and (await reply.json())["result"]["released"] is False


async def test_reconnect_resumes_with_a_new_session(engine_app: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(engine_app)
    await visit(fake_ha, rt)
    assert (await engine_app.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    session, epoch = rt.session_id, fake_ha.engine.epoch
    await fake_ha.drop_connections()  # a network interruption
    await wait_for(lambda: rt.session_id not in (None, session), timeout=10)
    subscribe = [c for c in fake_ha.commands if c["type"] == "smart_occupancy/engine/subscribe"][-1]
    assert subscribe["resume_token"] and subscribe["instance_id"] == rt.instance_id
    assert rt.mode == "authoritative" and fake_ha.engine.epoch == epoch + 1
    assert fake_ha.engine.history[-1]["reason"] == P.REASON_RESUMED


async def test_app_restart_is_a_new_instance_and_falls_back(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    two_sensor_room(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    env = {"SL_ENGINE_MIN_SHADOW_SECONDS": "0", "SL_ENGINE_MIN_TRANSITIONS": "2"}
    first = await start_app(fake_ha, aiohttp_client, **env)
    rt = runtime(first)
    await wait_for(lambda: rt.mode == "shadow")
    await visit(fake_ha, rt)
    assert (await first.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)).status == 200
    instance = rt.instance_id
    claims = sum(1 for c in fake_ha.commands if c["type"] == "smart_occupancy/engine/claim")
    await first.close()  # the App stops (state stored)
    stored = json.loads((Path(fake_ha.data_dir) / "occupancy/state.json").read_text())
    assert stored["schema"] == 1 and stored["core_version"] == CORE_VERSION and AREA in stored["rooms"]
    second = await start_app(fake_ha, aiohttp_client, **env)
    rt2 = runtime(second)
    await wait_for(lambda: rt2.mode == "shadow")
    assert rt2.instance_id != instance
    assert fake_ha.engine.owner == "integration" and fake_ha.engine.history[-1]["reason"] == P.REASON_APP_RESTARTED
    await asyncio.sleep(0.3)
    assert sum(1 for c in fake_ha.commands if c["type"] == "smart_occupancy/engine/claim") == claims
    await second.close()
    fake_ha.engine.stop()


# ----------------------------------------------------- without the bridge
async def test_older_integration_runs_standalone(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    rt = runtime(app_client)
    await wait_for(lambda: rt.bridge == "unsupported")
    assert rt.mode == "standalone" and not rt.readiness()["ready"]
    reply = await app_client.post("/api/engine/handover", json={"confirm": True}, headers=ADMIN_HEADERS)
    assert reply.status == 409
    assert not {c["type"] for c in fake_ha.commands} & ENGINE_COMMANDS


async def test_incompatible_stored_state_is_not_used(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    folder = Path(fake_ha.data_dir) / "occupancy"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"schema": 1, "core_version": "0.0.1", "spec": {}, "rooms": {}}))
    client = await start_app(fake_ha, aiohttp_client)
    rt = runtime(client)
    assert rt.house is None and any(n["code"] == "state_incompatible" for n in rt.notices)
    await client.close()


async def test_stored_state_restores_conservatively(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    """Without the integration's bridge, the App's engine restarts from its stored decisions (start-up hold)."""
    two_sensor_room(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)
    rt = runtime(client)
    await wait_for(lambda: rt.mode == "shadow")
    await fake_ha.emit_live(PIR, "on")
    await fake_ha.emit_live(RADAR, "on")
    await wait_for(lambda: (rt.results.get(AREA) or {}).get("occupied") is True)
    await client.close()
    fake_ha.engine.stop()
    fake_ha.engine = None  # the integration is not reachable now
    again = await start_app(fake_ha, aiohttp_client)
    rt2 = runtime(again)
    await wait_for(lambda: rt2.bridge == "unsupported")
    assert rt2.mode == "standalone" and rt2.house is not None
    assert rt2.house.engines[AREA].occupied is True
    await again.close()


# ---------------------------------------------------------- comparison
def test_comparator_rules() -> None:
    t0 = datetime(2026, 11, 17, 12, tzinfo=UTC)

    def w(state: str, occupied: bool | None, code: str, vacancy: datetime | None = None) -> dict[str, Any]:
        return {"state": state, "occupied": occupied, "reason_code": code,
                "vacancy_at": vacancy.isoformat() if vacancy else None}

    c = Comparator()
    c.start(t0, ["r"])
    vacant = w("vacant", False, "vacant")
    c.update("r", "integration", vacant, t0)
    c.update("r", "app", vacant, t0)
    # A transition the App follows 0.4 s later: a timing difference, matched.
    possible = w("possible_occupancy", True, "possible_occupancy")
    c.update("r", "integration", possible, t0 + timedelta(seconds=1))
    c.update("r", "app", possible, t0 + timedelta(seconds=1.4))
    totals = c.totals()
    assert (totals["transitions"], totals["matched"], totals["timing_differences"], totals["disagreements"]) == (
        1, 1, 1, 0)
    # A difference that lasts 10 s: a disagreement (and the transition is not matched).
    pv = w("possible_vacancy", True, "confirming_vacancy", t0 + timedelta(seconds=300))
    c.update("r", "integration", pv, t0 + timedelta(seconds=20))
    assert c.open_disagreements(t0 + timedelta(seconds=25)) == ["r"]
    c.update("r", "app", w("possible_vacancy", True, "confirming_vacancy", t0 + timedelta(seconds=310)),
             t0 + timedelta(seconds=30))
    totals = c.totals()
    assert totals["disagreements"] == 1 and totals["matched"] == 1 and totals["deadline_differences"] == 1
    assert c.last_disagreement(t0 + timedelta(seconds=31)) == t0 + timedelta(seconds=30)
    kinds = [r["kind"] for r in c.records()]
    assert kinds == ["deadline", "disagreement"]
    # The history survives a restart; counting starts again with a session.
    restored = Comparator()
    restored.restore(json.loads(json.dumps(c.persist())))
    assert [r["kind"] for r in restored.records()] == kinds
    assert restored.last_disagreement_at == t0 + timedelta(seconds=30)
    restored.start(t0 + timedelta(seconds=60), ["r"])
    assert restored.totals()["disagreements"] == 0 and restored.last_disagreement_at is not None


def test_comparator_history_is_bounded() -> None:
    t0 = datetime(2026, 11, 17, 12, tzinfo=UTC)
    c = Comparator()
    c.start(t0, ["r"])
    for i in range(500):
        at = t0 + timedelta(seconds=10 * i)
        c.update("r", "integration", {"state": "vacant", "occupied": False, "reason_code": str(i % 2)}, at)
        c.update("r", "app", {"state": "vacant", "occupied": False, "reason_code": str(i % 2)},
                 at + timedelta(seconds=5))
        c.note_event("r", at, "state", entity_id="binary_sensor.x", state="on")
    assert len(c.records()) == 200
    assert len(c.rooms["r"].events) == 30
