"""The web API: Ingress only, administrator and CSRF checks, reviewed saves with revisions, pages."""

from __future__ import annotations

from pathlib import Path

import pytest

from sod import config as C
from sod.core import AppCore
from sod.publisher import CompanionLink
from sod.runtime import OccupancyRuntime
from sod.settings import load_settings
from sod.web import build_app
from tests.conftest import KAMAR, STOVA, room

PIR, RADAR = "binary_sensor.kamar_pir", "binary_sensor.kamar_radar"
HEADERS = {"X-SOD": "1", "X-Remote-User-Id": "admin"}


@pytest.fixture
async def client(aiohttp_client, tmp_path, ha, clock):
    settings = load_settings({"SOD_DATA_DIR": str(tmp_path), "SOD_ALLOWED_CLIENTS": "127.0.0.1",
                              "SOD_OPTIONS": str(tmp_path / "none.json")})
    core = AppCore(settings)
    core.ha = ha
    core.runtime = OccupancyRuntime(ha, core.store, tmp_path)
    core.link = CompanionLink(ha, core.runtime.published_rooms, tmp_path, app_version="0.1.0",
                              can_publish=core._can_publish)
    await core.runtime.start()
    ha.set(PIR, "off", clock.now, "Kamar PIR", {"device_class": "motion"})
    ha.set(RADAR, "on", clock.now, "Kamar radar", {"device_class": "occupancy"})
    ha.set("binary_sensor.kamar_window", "off", clock.now, "Window", {"device_class": "window"})
    ha.entity_registry = [
        {"entity_id": PIR, "area_id": KAMAR, "platform": "zha"},
        {"entity_id": RADAR, "device_id": "dev1", "platform": "esphome"},
        {"entity_id": "binary_sensor.kamar_window", "area_id": KAMAR, "platform": "zha"},
        {"entity_id": "binary_sensor.sod_kamar_sudur_occupied", "area_id": KAMAR, "platform": "smart_occupancy_detection"},
        {"entity_id": "binary_sensor.kamar_nordur_occupied", "area_id": KAMAR, "platform": "smart_occupancy"},
    ]
    ha.devices = [{"id": "dev1", "area_id": KAMAR}]
    ha.connect()
    app = build_app(settings, core)
    test_client = await aiohttp_client(app)
    test_client.core = core
    yield test_client
    await core.runtime.stop()


async def test_requests_from_outside_ingress_are_refused(aiohttp_client, tmp_path):
    settings = load_settings({"SOD_DATA_DIR": str(tmp_path), "SOD_ALLOWED_CLIENTS": "172.30.32.2",
                              "SOD_OPTIONS": str(tmp_path / "none.json")})
    test_client = await aiohttp_client(build_app(settings, AppCore(settings)))
    response = await test_client.get("/api/health")
    assert response.status == 403


async def test_index_gets_the_ingress_base(client):
    response = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    text = await response.text()
    assert '<base href="/api/hassio_ingress/abc/">' in text and "__VERSION__" not in text
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    for asset in ("app.js", "app.css", "icon.svg"):
        assert (await client.get(f"/assets/{asset}")).status == 200
    assert (await client.get("/assets/../config.py")).status == 404


async def test_discovery_suggests_and_never_offers_occupancy_outputs(client):
    data = await (await client.get(f"/api/discover/{KAMAR}")).json()
    by_id = {c["entity_id"]: c for c in data["candidates"]}
    assert by_id[PIR]["suggested_kind"] == "pir" and by_id[PIR]["suggested_role"] == "trigger"
    assert by_id[RADAR]["suggested_kind"] == "radar"  # through its device's area
    assert by_id["binary_sensor.kamar_window"]["informational"]
    assert "binary_sensor.sod_kamar_sudur_occupied" not in by_id
    assert "binary_sensor.kamar_nordur_occupied" not in by_id
    found = await (await client.get("/api/entities?q=kamar")).json()
    assert {r["entity_id"] for r in found["results"]} >= {PIR, RADAR}


async def test_changes_need_csrf_header_and_an_administrator(client):
    body = {"area_id": KAMAR, "room": room()}
    assert (await client.post("/api/rooms/review", json=body, headers={"X-Remote-User-Id": "admin"})).status == 403
    assert (await client.post("/api/rooms/review", json=body, headers={**HEADERS, "X-Remote-User-Id": "guest"})).status == 403
    assert (await client.post("/api/rooms/review", json=body, headers={"X-SOD": "1"})).status == 403
    cross = {**HEADERS, "Origin": "https://evil.example", "Host": "ha.local:8123"}
    assert (await client.post("/api/rooms/review", json=body, headers=cross)).status == 403
    same = {**HEADERS, "Origin": "http://ha.local:8123", "Host": "ha.local:8123"}
    assert (await client.post("/api/rooms/review", json=body, headers=same)).status == 200


async def test_add_review_save_and_delete_a_room(client):
    overview = await (await client.get("/api/overview", headers=HEADERS)).json()
    assert overview["rooms"] == [] and overview["user"]["is_admin"]
    review = await (await client.post("/api/rooms/review", json={"area_id": KAMAR, "room": room()},
                                      headers=HEADERS)).json()
    assert review["valid"] and review["changes"][0]["type"] == "room_added"
    assert review["impact"][KAMAR]["after"]["status"] == "occupied"  # the radar is on
    saved = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room(),
                                                       "expected_revision": review["revision"]}, headers=HEADERS)
    assert saved.status == 200
    stale = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room(timing={"inactivity_timeout": 900}),
                                                       "expected_revision": review["revision"]}, headers=HEADERS)
    assert stale.status == 409 and (await stale.json())["error"] == "stale"
    detail = await (await client.get(f"/api/rooms/{KAMAR}")).json()
    assert detail["room"]["status"] == "occupied" and detail["config"]["name"] == "Kamar Suður"
    assert {s["entity_id"] for s in detail["room"]["signals"]} == {PIR, RADAR}
    # An occupancy output is refused as a sensor.
    bad = room([("binary_sensor.kamar_nordur_occupied", "radar", "maintain")])
    review = await (await client.post("/api/rooms/review", json={"area_id": KAMAR, "room": bad}, headers=HEADERS)).json()
    assert not review["valid"] and "feed occupancy back" in review["problems"][0]["message"]
    # Invalid input is listed, not saved.
    response = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room([]),
                                                          "expected_revision": detail["revision"]}, headers=HEADERS)
    assert response.status == 400 and (await response.json())["problems"]
    # An unknown area cannot become a room.
    response = await client.post("/api/rooms/review", json={"area_id": "nowhere", "room": room()}, headers=HEADERS)
    assert response.status == 404
    # Delete: reviewed, removes the neighbour reference too.
    review = await (await client.post("/api/rooms/delete/review", json={"area_id": KAMAR}, headers=HEADERS)).json()
    assert review["changes"] == [{"type": "room_removed", "area_id": KAMAR, "name": "Kamar Suður"}]
    response = await client.post("/api/rooms/delete", json={"area_id": KAMAR, "expected_revision": review["revision"]},
                                 headers=HEADERS)
    assert response.status == 200 and client.core.store.config["rooms"] == {}


async def test_global_timing_export_import_and_backups(client):
    core = client.core
    core.store.save({"timing": C.empty_config()["timing"], "rooms": {KAMAR: room()}},
                    expected_revision=core.store.revision, reason="seed")
    settings = await (await client.get("/api/settings")).json()
    timing = {**settings["timing"], "inactivity_timeout": 1200}
    review = await (await client.post("/api/timing/review", json={"timing": timing}, headers=HEADERS)).json()
    assert review["valid"] and review["changes"] == [{"type": "global_timing", "keys": ["inactivity_timeout"]}]
    assert KAMAR in review["impact"]
    await client.post("/api/timing/save", json={"timing": timing, "expected_revision": review["revision"]},
                      headers=HEADERS)
    assert core.store.config["timing"]["inactivity_timeout"] == 1200
    exported = await (await client.get("/api/export")).json()
    review = await (await client.post("/api/import/review", json={"document": exported}, headers=HEADERS)).json()
    assert review["valid"] and review["changes"] == []
    backups = (await (await client.get("/api/settings")).json())["backups"]
    review = await (await client.post("/api/backups/review", json={"name": backups[0]["name"]}, headers=HEADERS)).json()
    assert review["changes"] == [{"type": "global_timing", "keys": ["inactivity_timeout"]}]
    response = await client.post("/api/backups/restore", json={"name": backups[0]["name"],
                                                               "expected_revision": review["revision"]}, headers=HEADERS)
    assert response.status == 200 and core.store.config["timing"]["inactivity_timeout"] == 600
    diagnostics = await client.get("/api/diagnostics")
    assert "attachment" in diagnostics.headers["Content-Disposition"]


async def test_smart_comfort_import_flow(client):
    document = {"format": "smart_lighting.core_config", "version": 1, "options": {
        "rooms": {KAMAR: {"enabled": True, "sensors": [{"entity_id": RADAR, "kind": "radar", "role": "maintain"}],
                          "lights": ["light.x"], "adjacent_rooms": [STOVA]},
                  STOVA: {"enabled": True, "sensors": [{"entity_id": "binary_sensor.stova_pir", "kind": "pir",
                                                        "role": "trigger"}]}},
        "occupancy": {"inactivity_timeout": 700}}}
    review = await (await client.post("/api/smart-comfort/review", json={"document": document}, headers=HEADERS)).json()
    assert [r["area_id"] for r in review["rooms"]] == [KAMAR, STOVA]
    preview = await (await client.post("/api/smart-comfort/preview", json={"document": document, "rooms": [KAMAR],
                                                                            "include_timing": True}, headers=HEADERS)).json()
    assert preview["valid"] and any("adjacent" in n for n in preview["notes"])
    response = await client.post("/api/smart-comfort/apply", json={"document": document, "rooms": [KAMAR],
                                                                    "include_timing": True,
                                                                    "expected_revision": preview["revision"]},
                                 headers=HEADERS)
    assert response.status == 200
    assert client.core.store.config["rooms"][KAMAR]["sensors"][0]["entity_id"] == RADAR
    assert client.core.store.config["timing"]["inactivity_timeout"] == 700


async def test_pairing_goes_through_the_link(client):
    await __import__("asyncio").sleep(0)
    await client.core.link.start()
    for _ in range(50):
        if client.core.link.state == "not_paired":
            break
        await __import__("asyncio").sleep(0.01)
    response = await client.post("/api/companion/pair", json={}, headers={**HEADERS, "X-Remote-User-Id": "guest"})
    assert response.status == 403
    response = await client.post("/api/companion/pair", json={}, headers=HEADERS)
    assert response.status == 200 and (await response.json())["paired"]
    await client.core.link.stop()


def test_static_files_exist():
    static = Path(__file__).resolve().parents[1] / "backend" / "sod" / "static"
    assert {p.name for p in static.iterdir()} >= {"index.html", "app.js", "app.css", "icon.svg"}


async def test_no_changes_while_home_assistant_is_away(client, ha):
    review = await (await client.post("/api/rooms/review", json={"area_id": KAMAR, "room": room()},
                                      headers=HEADERS)).json()
    ha.connected = False
    client.core.runtime.suspended = True
    response = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room(),
                                                           "expected_revision": review["revision"]}, headers=HEADERS)
    assert response.status in (403, 503)
    assert client.core.store.config["rooms"] == {}
