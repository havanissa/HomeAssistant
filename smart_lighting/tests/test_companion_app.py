"""Phase A6: the App with the companion integration - setup, pairing, claims, the fenced executor, the outage policy,
the migration wizard, the configuration authority and the Settings → System endpoints (against tests/fake_companion.py)."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from smart_lighting import companion_protocol as P
from smart_lighting.companion_client import CompanionError
from smart_lighting.core_config import CoreConfigStore, default_options

from .conftest import ADMIN_HEADERS, FakeHomeAssistant, wait_for
from .fake_companion import FakeCompanion
from .fake_engine import FakeEngine
from .fake_lighting import FakeLighting
from .test_engine import start_app, two_sensor_room

AREA = "kamar_nordur"
LIGHT = "light.kamar_nordur_loft"
FAST = {"SL_ENGINE_MIN_SHADOW_SECONDS": "0", "SL_ENGINE_MIN_TRANSITIONS": "0", "SL_LIGHTING_MIN_SHADOW_SECONDS": "0",
        "SL_LIGHTING_MIN_COMMANDS": "0"}
DOMAINS = ("occupancy", "lighting", "curtains", "forecast")


def room_options() -> dict[str, Any]:
    """A core configuration with one room (a PIR, a radar, one light): the App's own document (v2)."""
    options = default_options()
    options["rooms"] = {AREA: {
        "enabled": True, "sensors": [
            {"entity_id": "binary_sensor.kamar_pir", "kind": "pir", "role": "trigger", "inverted": False},
            {"entity_id": "binary_sensor.kamar_radar", "kind": "radar", "role": "confirm_vacancy", "inverted": False}],
        "lights": [LIGHT], "reference_occupancy_entity": None, "mood_overrides": {}, "light_overrides": {},
        "timing": {}, "adjacent_rooms": []}}
    return options


def owners(client: Any) -> dict[str, bool]:
    return {d: bool(getattr(client.core, "forecast" if d == "forecast" else d).cm.owner) for d in DOMAINS}


async def all_claimed(client: Any) -> None:
    await wait_for(lambda: all(owners(client).values()), timeout=8)


@pytest.fixture
async def companion_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    """A fresh installation: the companion is installed (not paired), the App has no configuration yet."""
    two_sensor_room(fake_ha)
    fake_ha.companion = FakeCompanion(fake_ha)
    fake_ha.rooms_api = fake_ha.presence_api = False  # the companion has no room API: the App's own handlers serve it
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: client.core.companion.state == "present")
    yield client


# ------------------------------------------------------------------ setup and pairing
async def test_fresh_install_setup_pair_and_claim(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    status = (await companion_app.get("/api/system")).status
    assert status == 200
    system = await (await companion_app.get("/api/system")).json()
    assert system["companion"]["state"] == "present" and system["companion"]["trusted"] is False
    assert system["config_authority"] == "integration" and all(v == "none" for v in system["owners"].values())
    # Nothing is claimed without a configuration and without pairing: the runtimes wait.
    assert core.lighting.cm.state in ("waiting", "unpaired", "no_config") and not core.lighting.cm.owner
    assert core.lighting.mode == "standalone" and core.lighting.bridge != "owner"
    setup = await (await companion_app.get("/api/setup")).json()
    assert setup["legacy_installed"] is False and setup["config"]["authoritative"] is False
    assert [s["key"] for s in setup["steps"]][:3] == ["app", "companion", "config"]
    # Step: create the configuration (safe defaults: no rooms, nothing automatic).
    r = await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    assert core.core_config.authoritative and core.core_config.options["rooms"] == {}
    # Still not claimed: not paired.
    await asyncio.sleep(0.2)
    assert not any(owners(companion_app).values())
    assert core.lighting.cm.state == "unpaired"
    # Step: pair. The companion issues a token, the App stores it, every domain is claimed with a lease.
    r = await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    assert fake_ha.companion.paired and core.companion.trusted and core.companion.pairing_token is not None
    await all_claimed(companion_app)
    records = fake_ha.companion.records
    assert all(r.owner == "app" and r.epoch == 1 for r in records.values())
    assert core.occupancy.mode == "authoritative" and core.lighting.mode == "owner" and core.lighting.bridge == "companion"
    system = await (await companion_app.get("/api/system")).json()
    assert all(v == "app" for v in system["owners"].values()) and system["config_authority"] == "app"
    # The token is on disk (the App's own data), never in a configuration export.
    token_file = json.loads((fake_ha.data_dir / "companion" / "pairing.json").read_text())
    assert P.valid_id(token_file["pairing_token"])
    export = await (await companion_app.get("/api/config/export", headers=ADMIN_HEADERS)).text()
    assert token_file["pairing_token"] not in export and "pairing" not in export
    # Restart of the App: a new process never reuses a lease; it claims again (new epoch, new session).
    await core.stop()
    await wait_for(lambda: not fake_ha.companion.sessions)
    assert all(r.owner == "app" and r.session is None for r in records.values())  # away, owner kept
    assert all(r.state(fake_ha.companion.sessions) == "app_away" for r in records.values())


async def test_published_views_and_the_fenced_executor(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    # A room appears in the configuration: the occupancy engine installs it, the targets follow and views publish.
    await core.core_config.save(room_options(), expected_revision=core.core_config.revision, reason="test", source="test")
    await wait_for(lambda: AREA in (fake_ha.companion.views.get("occupancy") or {}).get("rooms", {}), timeout=8)
    await wait_for(lambda: LIGHT in fake_ha.companion.records["lighting"].targets.get("lights", []), timeout=8)
    occ = fake_ha.companion.views["occupancy"]["rooms"][AREA]
    assert {"occupied", "state", "confidence", "last_activity", "reason"} <= set(occ) <= set(P.OCCUPANCY_ENTITY_KEYS)
    assert occ["state"]["value"] in ("vacant", "uncertain")
    await wait_for(lambda: "system" in fake_ha.companion.views, timeout=8)
    assert fake_ha.companion.views["system"]["rooms"][AREA]["name"] == "Kamar Norður"
    # A motion event drives the engine: the published occupancy follows (through the companion, no entity of the App).
    await fake_ha.emit_live("binary_sensor.kamar_pir", "on")
    await wait_for(lambda: fake_ha.companion.views["occupancy"]["rooms"][AREA]["state"]["value"] in
                   ("possible_occupancy", "confirmed_occupancy"), timeout=8)
    # The executor: a configured light passes, with the App's context; anything else is refused by the allowlist.
    cm = core.lighting.cm
    await cm.execute("light", "turn_on", {"entity_id": LIGHT, "brightness": 120}, "ctxlighting000001", purpose="test",
                     area_id=AREA)
    assert fake_ha.service_calls[-1] == {"domain": "light", "service": "turn_on", "data": {"entity_id": LIGHT, "brightness": 120},
                                         "context_id": "ctxlighting000001"}
    with pytest.raises(CompanionError) as refused:
        await cm.execute("light", "turn_on", {"entity_id": "light.kamar_lamp"}, "ctxlighting000002", purpose=None, area_id=None)
    assert refused.value.code == P.REJECT_NOT_ALLOWED
    with pytest.raises(CompanionError) as refused:
        await cm.execute("switch", "turn_on", {"entity_id": LIGHT}, "ctxlighting000003", purpose=None, area_id=None)
    assert refused.value.code == P.REJECT_NOT_ALLOWED
    with pytest.raises(CompanionError):
        await cm.execute("homeassistant", "restart", {}, "ctxlighting000004", purpose=None, area_id=None)
    assert len(fake_ha.service_calls) == 1
    record = fake_ha.companion.records["lighting"]
    assert record.executed == 1 and record.execute_errors == 3
    # Occupancy commands nothing at all.
    with pytest.raises(CompanionError) as refused:
        await core.occupancy.cm.command("execute", service_domain="light", service="turn_on", data={"entity_id": LIGHT},
                                        context_id="ctxoccupancy00001")
    assert refused.value.code == P.REJECT_NOT_ALLOWED
    # The dashboard has the room from the App's own runtimes.
    dash = await (await companion_app.get("/api/dashboard")).json()
    assert dash["house"]["rooms"] == 1 and dash["rooms"][0]["area_id"] == AREA
    assert dash["engines"]["occupancy"] == {"owner": "app", "fresh": True}


# ------------------------------------------------------------------ outage policy
async def test_losing_the_lease_stops_the_engine_and_a_new_epoch_is_claimed(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    await core.core_config.save(room_options(), expected_revision=core.core_config.revision, reason="test", source="test")
    await wait_for(lambda: LIGHT in fake_ha.companion.records["lighting"].targets.get("lights", []), timeout=8)
    before = fake_ha.companion.records["lighting"].epoch
    # The companion revokes (service smart_occupancy.revoke_app_control): the App stops at once.
    await fake_ha.companion.revoke("lighting")
    await wait_for(lambda: not core.lighting.cm.owner and core.lighting.mode == "standalone")
    assert core.lighting.cm.state == "lost"
    # A stale command (the old lease) never reaches a device.
    with pytest.raises(CompanionError) as refused:
        await core.lighting.cm.execute("light", "turn_off", {"entity_id": LIGHT}, "ctxstale0000000001", purpose=None, area_id=None)
    assert refused.value.code == P.REJECT_NOT_OWNER and not fake_ha.service_calls
    # ... and the App claims again after the reclaim delay, with a new epoch (never the old lease).
    await wait_for(lambda: core.lighting.cm.owner, timeout=8)
    assert fake_ha.companion.records["lighting"].epoch == before + 2
    # A refused command with a lost code (the companion says the epoch is stale) also stops the engine.
    fake_ha.companion.fail_execute = P.REJECT_STALE_EPOCH
    with pytest.raises(CompanionError):
        await core.lighting.cm.execute("light", "turn_on", {"entity_id": LIGHT}, "ctxlost00000000001", purpose=None, area_id=None)
    await wait_for(lambda: not core.lighting.cm.owner)
    await wait_for(lambda: core.lighting.cm.owner, timeout=8)


async def test_app_outage_keeps_the_last_state_and_a_restart_is_a_fresh_session(companion_app: Any, fake_ha: FakeHomeAssistant,
                                                                               aiohttp_client) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    await core.core_config.save(room_options(), expected_revision=core.core_config.revision, reason="test", source="test")
    await wait_for(lambda: AREA in (fake_ha.companion.views.get("occupancy") or {}).get("rooms", {}), timeout=8)
    published = copy.deepcopy(fake_ha.companion.views["occupancy"])
    epochs = {d: r.epoch for d, r in fake_ha.companion.records.items()}
    # The App goes away: the companion keeps the owner (away), the views and marks nothing vacant.
    await core.stop()
    await wait_for(lambda: not fake_ha.companion.sessions)
    assert fake_ha.companion.views["occupancy"] == published
    assert all(r.state(fake_ha.companion.sessions) == "app_away" for r in fake_ha.companion.records.values())
    assert not fake_ha.service_calls  # nothing was switched because the App left
    # A new App process (same data folder: same pairing token and configuration) claims every domain again.
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: client.core.companion.state == "present" and client.core.companion.trusted)
    await all_claimed(client)
    assert {d: r.epoch for d, r in fake_ha.companion.records.items()} == {d: e + 1 for d, e in epochs.items()}
    assert client.core.core_config.authoritative and AREA in client.core.core_config.options["rooms"]
    # The engines start as after a restart: nothing in flight is replayed.
    assert not fake_ha.service_calls


async def test_release_claim_and_revoke_endpoints(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    r = await companion_app.post("/api/system/release", json={"domain": "curtains"}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    await wait_for(lambda: fake_ha.companion.records["curtains"].owner == "none")
    assert not core.curtains.cm.owner and core.curtains.cm.auto_claim is False
    await asyncio.sleep(0.3)
    assert not core.curtains.cm.owner  # released: no automatic reclaim
    r = await companion_app.post("/api/system/claim", json={"domain": "curtains"}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    await wait_for(lambda: core.curtains.cm.owner)
    # An invalid domain and a non-administrator are refused.
    assert (await companion_app.post("/api/system/claim", json={"domain": "x"}, headers=ADMIN_HEADERS)).status == 400
    assert (await companion_app.post("/api/system/revoke", json={}, headers={"X-Smart-Lighting": "1"})).status in (401, 403)
    # Revoke everything from the App's own page: every lease is gone; nothing is switched.
    r = await companion_app.post("/api/system/revoke", json={"domain": None}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    await wait_for(lambda: not any(owners(companion_app).values()))
    assert not fake_ha.service_calls


# ------------------------------------------------------------------ relayed actions, renames
async def test_card_actions_and_renames_are_handled_by_the_app(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    await core.core_config.save(room_options(), expected_revision=core.core_config.revision, reason="test", source="test")
    await wait_for(lambda: LIGHT in fake_ha.companion.records["lighting"].targets.get("lights", []), timeout=8)
    action_id = await fake_ha.companion.card_action("room/resume", {"area_id": AREA})
    await wait_for(lambda: fake_ha.companion.actions[action_id] is not None)
    answer = fake_ha.companion.actions[action_id]
    assert answer["action_id"] == action_id and "ok" in answer
    action_id = await fake_ha.companion.card_action("bogus/kind", {})
    await wait_for(lambda: fake_ha.companion.actions[action_id] is not None)
    assert fake_ha.companion.actions[action_id]["ok"] is False
    # A rename in Home Assistant: the configuration follows and the companion is told.
    await fake_ha.companion.rename(LIGHT, "light.kamar_ceiling")
    await wait_for(lambda: fake_ha.companion.renames_applied == 1)
    assert core.core_config.options["rooms"][AREA]["lights"] == ["light.kamar_ceiling"]


# ------------------------------------------------------------------ configuration authority
async def test_core_config_store_versions_backups_and_imports(tmp_path) -> None:
    store = CoreConfigStore(tmp_path)
    await store.load()
    assert store.options is None and not store.authoritative
    status = await store.save(default_options(), expected_revision=None, reason="setup", source="test", authority="app")
    assert status["authoritative"] and store.revision
    with pytest.raises(Exception) as stale:
        await store.save(room_options(), expected_revision="0" * 16, reason="x", source="test")
    assert getattr(stale.value, "code", "") == "stale_revision"
    await store.save(room_options(), expected_revision=store.revision, reason="room", source="test")
    assert len(store.backups()) >= 1 and (tmp_path / "config" / "core.json").exists()
    # Export carries no credentials and imports review before they apply.
    export = store.export()
    assert export["format"] == "smart_lighting.core_config" and "token" not in json.dumps(export).lower()
    changed = copy.deepcopy(export)
    changed["options"]["rooms"][AREA]["lights"] = ["light.other"]
    review = store.import_review(changed, entity_exists=lambda e: e != "light.other")
    assert review["changes"] and "light.other" in review["missing_entities"]
    assert store.options["rooms"][AREA]["lights"] == [LIGHT]  # a review changes nothing
    applied = await store.import_apply(changed, expected_revision=store.revision, source="import", reason="import")
    assert applied["status"]["revision"] == store.revision and store.options["rooms"][AREA]["lights"] == ["light.other"]
    with pytest.raises(Exception):
        store.import_review({"format": "something_else"})
    # Stale detection across processes: another instance wrote the file.
    other = CoreConfigStore(tmp_path)
    await other.load()
    await other.save(default_options(), expected_revision=other.revision, reason="other", source="test")
    with pytest.raises(Exception) as stale:
        await store.save(room_options(), expected_revision=store.revision, reason="x", source="test")
    assert getattr(stale.value, "code", "") == "stale_revision"
    # A rename rewrites every reference atomically.
    await store.load()
    await store.save(room_options(), expected_revision=store.revision, reason="room", source="test")
    assert await store.rename_entity(LIGHT, "light.renamed") is True
    assert store.options["rooms"][AREA]["lights"] == ["light.renamed"]
    assert await store.rename_entity("light.nothing", "light.x") is False
    # Promote / demote (staged copy during the migration).
    await store.demote("test")
    assert store.staged and not store.authoritative
    await store.promote("test")
    assert store.authoritative


async def test_config_endpoints(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    r = await companion_app.get("/api/config/export", headers=ADMIN_HEADERS)
    assert r.status == 200 and r.headers["Content-Type"].startswith("application/json")
    export = await r.json()
    changed = copy.deepcopy(export)
    changed["options"] = room_options()
    r = await companion_app.post("/api/config/import/review", json={"file": changed}, headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    assert (await r.json())["changes"]
    r = await companion_app.post("/api/config/import", json={"file": changed, "revision": core.core_config.revision},
                                 headers=ADMIN_HEADERS)
    assert r.status == 200, await r.text()
    assert core.core_config.options["rooms"][AREA]["lights"] == [LIGHT]
    backups = await (await companion_app.get("/api/config/backups", headers=ADMIN_HEADERS)).json()
    assert backups["backups"]
    one = await companion_app.get(f"/api/config/backups/{backups['backups'][0]['id']}", headers=ADMIN_HEADERS)
    assert one.status == 200


# ------------------------------------------------------------------ the migration wizard
@pytest.fixture
async def legacy_app(fake_ha: FakeHomeAssistant, aiohttp_client) -> Any:
    """An installation on the monolithic integration 0.9.0: its bridges are present, no companion."""
    two_sensor_room(fake_ha)
    fake_ha.lighting = FakeLighting(fake_ha)
    fake_ha.engine = FakeEngine(fake_ha)
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: client.core.lighting.mode == "shadow" and client.core.occupancy.mode == "shadow", timeout=8)
    yield client
    if fake_ha.engine is not None:
        fake_ha.engine.stop()


async def step(client: Any, name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await client.post(f"/api/migration/{name}", json=body or {}, headers=ADMIN_HEADERS)
    data = await r.json()
    assert r.status == 200, data
    return data["result"]


async def test_migration_wizard_from_legacy_to_companion(legacy_app: Any, fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    core = legacy_app.core
    status = await (await legacy_app.get("/api/migration")).json()
    assert status["mode"] == "legacy" and status["started_at"] is None
    assert [s["key"] for s in status["steps"]] == ["inspect", "backup", "validate", "transfer_config", "transfer_control",
                                                   "activate", "verify", "retire"]
    # 1 inspect: changes nothing, reports who runs what.
    inspect = await step(legacy_app, "inspect")
    assert inspect["mode"] == "legacy" and inspect["versions"]["integration"] == "0.6.8"
    assert inspect["owners"]["lighting"]["source"] == "legacy" and inspect["legacy"]["rooms"] == 1
    # 2 backup: a file in the App's storage, without credentials.
    backup = await step(legacy_app, "backup")
    backup_path = fake_ha.data_dir / "migration" / "backups" / f"{backup['id']}.json"
    assert backup["size"] > 0 and backup_path.exists()
    text = backup_path.read_text()
    assert fake_ha.token not in text
    backups = await (await legacy_app.get("/api/migration/backups", headers=ADMIN_HEADERS)).json()
    assert backups["backups"][0]["id"] == backup["id"]
    # 3 validate
    validate = await step(legacy_app, "validate")
    assert {c["key"] for c in validate["checks"]} >= {"storage", "config"}
    # 4 transfer_config: reviewed, then applied as a staged copy (the integration's options still count).
    review = await step(legacy_app, "transfer_config", {"apply": False})
    assert review["applied"] is False and "lighting bridge" in review["source"] and not core.core_config.options
    applied = await step(legacy_app, "transfer_config", {"apply": True})
    assert applied["applied"] is True and applied["authority"] == "staged"
    assert core.core_config.staged and not core.core_config.authoritative
    assert AREA in core.core_config.options["rooms"]
    # 5 transfer_control: the safe order is enforced.
    r = await legacy_app.post("/api/migration/transfer_control", json={"domain": "lighting"}, headers=ADMIN_HEADERS)
    assert r.status == 409 and (await r.json())["code"] == "order"
    handed = await step(legacy_app, "transfer_control", {"domain": "occupancy"})
    assert handed["domains"]["occupancy"] and core.occupancy.mode == "authoritative"
    # 6 activate needs the companion: refused on 0.9.0 with a clear message.
    r = await legacy_app.post("/api/migration/activate", json={}, headers=ADMIN_HEADERS)
    assert r.status == 409 and (await r.json())["code"] == "companion_missing"
    # Abort hands control back and demotes nothing destructive.
    aborted = await step(legacy_app, "abort", {"restore_legacy": False})
    assert any("occupancy" in a for a in aborted["actions"])
    await wait_for(lambda: core.occupancy.mode == "shadow", timeout=8)
    assert core.core_config.staged  # the staged copy stays (it is not the authority)
    # --- The person updates the integration to the companion (1.0.0) and restarts Home Assistant.
    legacy_options = fake_ha.lighting.options()
    fake_ha.lighting = None
    fake_ha.engine.stop()
    fake_ha.engine = None
    fake_ha.rooms_api = fake_ha.presence_api = False
    fake_ha.legacy_options = legacy_options
    fake_ha.companion = FakeCompanion(fake_ha)
    await fake_ha.drop_connections()
    await wait_for(lambda: core.ha.connected and core.companion.state == "present", timeout=10)
    status = await (await legacy_app.get("/api/migration")).json()
    assert status["mode"] == "companion"
    inspect = await step(legacy_app, "inspect")
    assert inspect["mode"] == "companion" and inspect["versions"]["companion"] == "1.0.0"
    # 6 activate: pairs, compares the staged copy with the legacy options, promotes, claims.
    activated = await step(legacy_app, "activate")
    assert activated["activated"] is True, activated
    assert core.core_config.authoritative and core.companion.trusted
    await all_claimed(legacy_app)
    assert set(fake_ha.companion.records["lighting"].targets["lights"]) >= {LIGHT}
    # 7 verify (once the system view, and with it the room entities, exist in the companion)
    await wait_for(lambda: AREA in ((fake_ha.companion.views.get("system") or {}).get("rooms") or {}), timeout=8)
    verify = await step(legacy_app, "verify")
    assert verify["ok"] is True, verify
    # 8 retire: the companion backs up and removes the legacy data; the App's backups stay.
    retired = await step(legacy_app, "retire", {"confirm": True})
    assert retired["outcome"]["stores_removed"] and fake_ha.companion.legacy_options is None
    assert backup_path.exists()
    status = await (await legacy_app.get("/api/migration")).json()
    assert status["completed_at"]
    # Rollback path: the legacy data can be restored (for a downgrade to 0.9.0); nothing of the App is deleted.
    restored = await step(legacy_app, "abort", {"restore_legacy": True})
    assert any("restored" in a for a in restored["actions"]) and fake_ha.companion.legacy_options is not None
    assert core.core_config.authoritative


async def test_activate_refuses_a_stale_staged_copy_until_reimported(legacy_app: Any, fake_ha: FakeHomeAssistant) -> None:
    core = legacy_app.core
    await step(legacy_app, "inspect")
    await step(legacy_app, "transfer_config", {"apply": True})
    # The configuration changes in the integration after the transfer (someone removed a light in 0.9.0).
    assert len(fake_ha.rooms_options[AREA]["lights"]) == 2
    fake_ha.rooms_options[AREA]["lights"] = [LIGHT]
    legacy_options = fake_ha.lighting.options()
    fake_ha.lighting = None
    fake_ha.engine.stop()
    fake_ha.engine = None
    fake_ha.rooms_api = fake_ha.presence_api = False
    fake_ha.legacy_options = legacy_options
    fake_ha.companion = FakeCompanion(fake_ha)
    await fake_ha.drop_connections()
    await wait_for(lambda: core.ha.connected and core.companion.state == "present", timeout=10)
    blocked = await step(legacy_app, "activate")
    assert blocked["activated"] is False and blocked["needs_reimport"] is True
    assert not core.core_config.authoritative
    activated = await step(legacy_app, "activate", {"reimport": True})
    assert activated["activated"] is True
    assert core.core_config.options["rooms"][AREA]["lights"] == [LIGHT]
    await all_claimed(legacy_app)


async def test_setup_refuses_to_start_fresh_while_the_legacy_integration_is_installed(legacy_app: Any) -> None:
    r = await legacy_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    assert r.status == 409 and (await r.json())["code"] == "legacy_installed"
    setup = await (await legacy_app.get("/api/setup")).json()
    assert setup["legacy_installed"] is True


async def test_activate_again_never_overwrites_an_edited_configuration(legacy_app: Any, fake_ha: FakeHomeAssistant) -> None:
    """Review finding: a retry of Activate (legacy data still present) must not replace the App's edited configuration."""
    core = legacy_app.core
    await step(legacy_app, "inspect")
    await step(legacy_app, "transfer_config", {"apply": True})
    legacy_options = fake_ha.lighting.options()
    fake_ha.lighting = None
    fake_ha.engine.stop()
    fake_ha.engine = None
    fake_ha.rooms_api = fake_ha.presence_api = False
    fake_ha.legacy_options = legacy_options
    fake_ha.companion = FakeCompanion(fake_ha)
    await fake_ha.drop_connections()
    await wait_for(lambda: core.ha.connected and core.companion.state == "present", timeout=10)
    assert (await step(legacy_app, "activate"))["activated"] is True
    # The administrator edits the configuration in the App (a light removed) but never retires the legacy data.
    edited = copy.deepcopy(core.core_config.options)
    edited["rooms"][AREA]["lights"] = [LIGHT]
    await core.core_config.save(edited, expected_revision=core.core_config.revision, reason="edit", source="app")
    revision = core.core_config.revision
    again = await step(legacy_app, "activate")
    assert again["activated"] is True and core.core_config.revision == revision
    assert core.core_config.options["rooms"][AREA]["lights"] == [LIGHT]
    # Only an explicit re-import replaces it (after the review the step shows).
    reimported = await step(legacy_app, "activate", {"reimport": True})
    assert reimported["activated"] is True and len(core.core_config.options["rooms"][AREA]["lights"]) == 2


async def test_setup_refuses_to_start_fresh_while_the_companion_holds_legacy_data(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    two_sensor_room(fake_ha)
    fake_ha.legacy_options = {"config_version": 2, "operating_mode": "observe", "mood": {}, "categories": [],
                              "rooms": copy.deepcopy(fake_ha.rooms_options), "global_profiles": {}, "occupancy": {}}
    fake_ha.companion = FakeCompanion(fake_ha)
    fake_ha.rooms_api = fake_ha.presence_api = False
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    await wait_for(lambda: client.core.companion.state == "present")
    r = await client.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    assert r.status == 409 and (await r.json())["code"] == "legacy_present", await r.text()
    assert client.core.core_config.options is None
    assert not any(owners(client).values())  # nothing claimed, so the legacy room entities stay


async def test_a_lapsed_0_9_0_lease_never_demotes_the_companion_owned_engine(companion_app: Any, fake_ha: FakeHomeAssistant) -> None:
    """E2E finding: the lease-lapse timer of the 0.9.0 bridge fired after the companion had claimed occupancy."""
    core = companion_app.core
    await companion_app.post("/api/setup/start", json={}, headers=ADMIN_HEADERS)
    await companion_app.post("/api/system/pair", json={}, headers=ADMIN_HEADERS)
    await all_claimed(companion_app)
    assert core.occupancy.mode == "authoritative"
    core.occupancy._lease_lapsed()  # noqa: SLF001 - the timer the old bridge session scheduled
    core.occupancy._lose("the integration changed the owner", "revoked")  # noqa: SLF001 - a stale bridge event
    assert core.occupancy.mode == "authoritative" and core.occupancy.cm.owner


async def test_a_fresh_app_next_to_the_companion_imports_the_legacy_stores(fake_ha: FakeHomeAssistant, aiohttp_client) -> None:
    """Production finding: an App installed fresh next to companion 1.0.0 has no configuration and no engine stores;
    Activate imports the options *and* the lighting / curtain / forecast stores from the companion's legacy export."""
    two_sensor_room(fake_ha)
    fake_ha.legacy_options = room_options() | {"operating_mode": "observe"}
    fake_ha.companion = FakeCompanion(fake_ha)
    fake_ha.rooms_api = fake_ha.presence_api = False
    client = await start_app(fake_ha, aiohttp_client, **FAST)
    core = client.core
    await wait_for(lambda: core.companion.state == "present")
    assert core.lighting.stored_config_doc() is None and core.curtains.stored_config_doc() is None
    # Production finding (0.7.1): Validate found "No configuration" here and blocked the wizard at step 3.
    validate = await step(client, "validate")
    checks = {c["key"]: c for c in validate["checks"]}
    assert validate["ok"] is True, [c for c in validate["checks"] if c["status"] != "ok"]
    assert checks["config"]["status"] == "ok" and "companion" in checks["config"]["detail"]
    review = await step(client, "transfer_config", {"apply": False})
    assert {s["key"]: s["import"] for s in review["stores"]} == {"smart_occupancy.lighting": True, "smart_occupancy.curtains": True,
                                                                "smart_occupancy.forecast_days": True}
    assert core.lighting.stored_config_doc() is None  # a review writes nothing
    activated = await step(client, "activate")
    assert activated["activated"] is True
    await all_claimed(client)
    lighting = core.lighting.stored_config_doc()
    assert lighting is not None and lighting["rooms"][AREA]["control_enabled"] is True
    curtains = core.curtains.stored_config_doc()
    assert curtains is not None and curtains["windows"]
    assert (fake_ha.data_dir / "predictions" / "store" / "smart_occupancy.forecast_days").is_file()
    assert json.loads((fake_ha.data_dir / "lighting" / "store" / "smart_occupancy.lighting").read_text())["minor_version"] == 9
    # The running controller took the imported configuration: the room is in Automatic.
    await wait_for(lambda: core.lighting.controller is not None and core.lighting.controller.room_active(AREA), timeout=8)
    # A second transfer never replaces the App's copies.
    again = await step(client, "transfer_config", {"apply": True})
    assert all(s["import"] is False for s in again["stores"]) and again["stores_imported"] == []
