"""Phase 0.2: compatibility, automatic re-pairing, communication diagnostics, hold reasons, data age, pilot,
redaction. The detection rules are not touched (test_engine_parity.py)."""

from __future__ import annotations

import asyncio
import json

import pytest

from sod import compat, config as C, pilot as PILOT, publisher as PUB
from sod.pilot import Pilot, explain
from sod.publisher import CompanionLink
from sod.redact import Redactor, personal_names
from sod.runtime import OccupancyRuntime
from tests.conftest import KAMAR, room

PIR, RADAR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar"
REF = "binary_sensor.smart_occupancy_lighting_kamar_sudur_occupied"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(PUB, "PUBLISH_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RECLAIM_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(PUB.P, "HEARTBEAT_SECONDS", 0.05)


async def setup(tmp_path, ha, clock, timing=None):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save({"timing": {**C.empty_config()["timing"], "startup_hold": 0, **(timing or {})},
                "rooms": {KAMAR: room()}}, expected_revision=store.revision, reason="test")
    ha.set(PIR, "off", clock.now)
    ha.set(RADAR, "off", clock.now)
    runtime = OccupancyRuntime(ha, store, tmp_path)
    link = CompanionLink(ha, runtime.published_rooms, tmp_path, app_version="0.2.0", can_publish=lambda: None)
    runtime.add_listener(lambda _a: link.schedule_publish())
    await runtime.start()
    await link.start()
    return runtime, link


async def wait_for(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# ------------------------------------------------------------------ compatibility
@pytest.mark.parametrize(("version", "protocol", "level", "words"), [
    ("0.2.0", 1, "ok", None),
    ("0.3.1", 1, "ok", None),
    ("0.1.0", 1, "warning", "update it in HACS"),
    ("0.0.9", 1, "incompatible", "too old"),
    ("0.1.0", 0, "incompatible", "update it in HACS"),
    ("1.0.0", 2, "incompatible", "update the App"),
    (None, 1, "warning", "unknown"),
])
def test_compatibility_levels(version, protocol, level, words):
    result = compat.check(version, protocol)
    assert result["level"] == level
    if words:
        assert words in result["message"]


async def test_an_incompatible_integration_is_reported_and_nothing_is_sent(tmp_path, ha, clock):
    ha.companion.protocol = 2
    ha.companion.version = "1.0.0"
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "incompatible")
    assert "update the App" in link.detail and link.errors[0]["code"] == "incompatible"
    assert ha.commands_sent[f"{PUB.P.DOMAIN}/session"] == 0
    await link.stop()
    await runtime.stop()


async def test_an_older_integration_still_works_with_a_warning(tmp_path, ha, clock):
    ha.companion.version = "0.1.0"
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, user_id="admin")
    await wait_for(lambda: link.state == "publishing")
    status = link.status()
    assert status["compatibility"]["level"] == "warning" and status["companion_version"] == "0.1.0"
    assert status["last_ok_at"] is not None
    await link.stop()
    await runtime.stop()


# ------------------------------------------------------------------ pairing survives re-adding the integration
async def test_the_app_pairs_again_when_the_integration_lost_its_pairing(tmp_path, ha, clock):
    runtime, link = await setup(tmp_path, ha, clock)
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, user_id="admin")
    await wait_for(lambda: link.state == "publishing")
    # The integration is removed and added again: its pairing is gone, the App still holds its token.
    ha.companion.pairing_hash = None
    ha.disconnect()
    ha.connect()
    await wait_for(lambda: link.state == "publishing")
    assert link.counters["auto_pairs"] == 1
    # After an explicit unpair the App never pairs by itself.
    await link.unpair(user_id="admin")
    ha.disconnect()
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await asyncio.sleep(0.1)
    assert link.state == "not_paired" and link.counters["auto_pairs"] == 1
    await link.stop()
    await runtime.stop()


# ------------------------------------------------------------------ hold reasons, data age
async def test_hold_reasons_and_data_age(tmp_path, ha, clock):
    store = C.ConfigStore(tmp_path)
    store.load()
    store.save({"timing": {**C.empty_config()["timing"], "startup_hold": 120}, "rooms": {KAMAR: room()}},
               expected_revision=store.revision, reason="test")
    ha.set(PIR, "on", clock.now)
    ha.set(RADAR, "on", clock.now)
    runtime = OccupancyRuntime(ha, store, tmp_path)
    await runtime.start()
    ha.connect()
    view = runtime.room_view(KAMAR, detail=True)
    assert view["hold"]["reason"] == "start"
    assert "Start-up hold (the App started)" in next(p["text"] for p in view["problems"] if p["code"] == "startup_hold")
    assert view["signals"][0]["last_changed"] == clock.now.isoformat()
    clock.advance(200)
    ha.disconnect()
    ha.connect()
    assert runtime.room_view(KAMAR)["hold"]["reason"] == "reconnect"
    clock.advance(200)
    new = json.loads(json.dumps(store.config))
    new["rooms"][KAMAR]["sensors"] = new["rooms"][KAMAR]["sensors"][1:]
    store.save(new, expected_revision=store.revision, reason="sensor removed")
    assert runtime.room_view(KAMAR)["hold"]["reason"] == "sensors_changed"
    await runtime.stop()


# ------------------------------------------------------------------ pilot
async def test_pilot_logs_differences_with_evidence_and_never_changes_a_decision(tmp_path, ha, clock, monkeypatch):
    monkeypatch.setattr(PILOT, "utcnow", clock)
    monkeypatch.setattr(PILOT, "GRACE_SECONDS", 0.05)
    ha.entity_registry = [{"entity_id": REF, "platform": "smart_occupancy",
                           "unique_id": f"smart_occupancy_room_{KAMAR}_occupied"}]
    runtime, link = await setup(tmp_path, ha, clock)
    pilot = Pilot(ha, runtime, tmp_path)
    await pilot.start()
    ha.set(REF, "off", clock.now, attributes={"reason": "Vacant: no activity for 5 min."})
    ha.connect()
    assert pilot.suggestion(KAMAR) == REF
    with pytest.raises(ValueError):
        ha.entity_registry.append({"entity_id": "binary_sensor.sod_kamar_occupied",
                                   "platform": "smart_occupancy_detection"})
        pilot.set_reference(KAMAR, "binary_sensor.sod_kamar_occupied")
    pilot.set_reference(KAMAR, REF)
    assert pilot.room(KAMAR)["now"] == "agree"
    decision_before = runtime.result(KAMAR)
    clock.advance(5)
    ha.set(RADAR, "on", clock.now)  # this App: occupied; the reference still vacant
    assert pilot.room(KAMAR)["now"] == "differ" and pilot.room(KAMAR)["pending_since"]
    clock.advance(1)
    await asyncio.sleep(0.8)  # the grace time (and its 0.5 s margin) passes
    differences = pilot.report()
    assert len(differences) == 1 and differences[0]["ended_at"] is None
    d = differences[0]
    assert d["ours"]["occupied"] is True and d["reference"]["occupied"] is False
    assert any(s["entity_id"] == RADAR and s["status"] == "active" for s in d["ours"]["signals"])
    assert "Smart Comfort says: Vacant: no activity for 5 min." in d["explanation"]
    clock.advance(30)
    ha.set(REF, "on", clock.now)
    assert d["ended_at"] is not None and d["seconds"] == 31
    summary = pilot.room(KAMAR)
    assert summary["differences"] == 1 and 0 < summary["agreement"] < 100
    csv_text = pilot.report_csv()
    assert "Kamar Suður" in csv_text and "occupied,possible_occupancy" in csv_text  # the evidence at the start
    # Observation only: the decision went its own way, nothing in the engine changed because of the pilot.
    assert runtime.result(KAMAR).occupied is True and decision_before.state.value == "vacant"
    assert not hasattr(runtime, "pilot")  # the runtime does not know the pilot: it cannot influence a decision
    await pilot.stop()
    saved = json.loads((tmp_path / "pilot" / "settings.json").read_text())
    assert saved["references"] == {KAMAR: REF}
    assert "rooms" in json.loads((tmp_path / "config" / "config.json").read_text())["config"]  # config untouched
    assert "reference" not in json.dumps(json.loads((tmp_path / "config" / "config.json").read_text()))
    await link.stop()
    await runtime.stop()


def test_explanations():
    ours = {"occupied": True, "state": "possible_vacancy", "signals": []}
    ref = {"occupied": False, "attributes": {}}
    assert "vacancy timeout" in explain(ours, ref)
    ours = {"occupied": False, "state": "vacant", "signals": []}
    assert "other sensors" in explain(ours, {"occupied": True, "attributes": {"reason": "PIR"}})
    ours = {"occupied": False, "state": "possible_occupancy", "signals": []}
    assert "door" in explain(ours, {"occupied": True, "attributes": {}})


# ------------------------------------------------------------------ redaction
def test_diagnostics_redaction():
    redact = Redactor(personal_names({"person.ragnar": {"attributes": {"friendly_name": "Ragnar"}},
                                      "binary_sensor.pir": {"attributes": {"friendly_name": "PIR"}}}))
    data = redact({"by": "1c5f0b2e", "token": "abc", "log": [
        "Paired (requested by 1c5f0b2e) from 192.168.1.50 via http://homeassistant.local:8123/api",
        "person.ragnar is home; Ragnar left; eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlX3h4 used",
        "binary_sensor.pir active in Kamar Suður"]})
    text = json.dumps(data, ensure_ascii=False)
    for secret in ("192.168.1.50", "homeassistant.local", "person.ragnar", "Ragnar", "eyJhbGci", '"abc"'):
        assert secret not in text, secret
    assert data["by"].startswith("user-") and data["token"] == "**REDACTED**"
    assert "binary_sensor.pir active in Kamar Suður" in text  # what a diagnosis needs stays
