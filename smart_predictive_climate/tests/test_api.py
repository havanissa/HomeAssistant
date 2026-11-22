"""The JSON API through the real web frame (security checks) with the real core."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spc.core import AppCore
from spc.publisher import CompanionLink
from spc.settings import load_settings
from spc.web import build_app

from conftest import KAMAR

HEADERS = {"X-SPC": "1", "X-Remote-User-Id": "admin"}


@pytest.fixture
async def client(aiohttp_client, tmp_path, ha):
    settings = load_settings({"SPC_DATA_DIR": str(tmp_path), "SPC_ALLOWED_CLIENTS": "127.0.0.1",
                              "SPC_OPTIONS": str(tmp_path / "o.json")})
    core = AppCore(settings)
    core.ha = ha
    core.link = CompanionLink(ha, settings.data_dir, app_version="0.1.0")
    await core.on_start()
    at = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)
    ha.devices.append({"id": "dev-trv", "name": "Kamar TRV", "area_id": KAMAR, "manufacturer": "Acme", "model": "T1"})
    ha.devices.append({"id": "dev-sensor", "name": "Kamar sensor", "area_id": KAMAR})
    for entity, state, attrs, device in (
            ("sensor.kamar_temperature", "20.4", {"unit_of_measurement": "°C", "device_class": "temperature"}, "dev-sensor"),
            ("sensor.kamar_humidity", "45", {"unit_of_measurement": "%", "device_class": "humidity"}, "dev-sensor"),
            ("climate.kamar_trv", "heat", {"current_temperature": 22.0, "temperature": 21, "hvac_action": "idle"}, "dev-trv"),
            ("sensor.kamar_trv_temperature", "22.0", {"unit_of_measurement": "°C", "device_class": "temperature"}, "dev-trv"),
            ("binary_sensor.kamar_window", "off", {"device_class": "window"}, "dev-sensor"),
            ("sensor.outdoor_temperature", "4", {"unit_of_measurement": "°C", "device_class": "temperature"}, None),
            ("weather.home", "cloudy", {"temperature": 4, "cloud_coverage": 80, "temperature_unit": "°C"}, None)):
        ha.set(entity, state, at, attributes=attrs)
        ha.entity_registry.append({"entity_id": entity, "device_id": device, "area_id": None})
    ha.connect()
    test_client = await aiohttp_client(build_app(settings, core))
    test_client.core = core
    yield test_client
    await core.on_stop()


async def test_room_first_discovery_suggests_and_chooses_nothing(client):
    areas = (await (await client.get("/api/areas", headers=HEADERS)).json())["areas"]
    kamar = next(a for a in areas if a["area_id"] == KAMAR)
    assert kamar["temperature_sensors"] == 2 and kamar["thermostats"] == 1 and not kamar["configured"]
    area = await (await client.get(f"/api/areas/{KAMAR}", headers=HEADERS)).json()
    assert [d["name"] for d in area["devices"]] == ["Kamar sensor", "Kamar TRV"]
    temps = area["suggestions"]["temperature"]
    # the independent sensor first; the TRV's own sensor carries a caution
    assert temps[0]["entity_id"] == "sensor.kamar_temperature" and temps[0]["caution"] is None
    assert "thermostat" in temps[1]["caution"].lower()
    assert [e["entity_id"] for e in area["suggestions"]["openings"]] == ["binary_sensor.kamar_window"]
    assert area["configured"] is None
    outdoor = (await (await client.get("/api/outdoor-candidates", headers=HEADERS)).json())["results"]
    assert {e["entity_id"] for e in outdoor} == {"sensor.outdoor_temperature", "weather.home"}


async def test_save_room_validate_and_security(client):
    overview = await (await client.get("/api/overview", headers=HEADERS)).json()
    revision = overview["revision"]
    room = {"name": "Kamar", "temperature": "sensor.kamar_temperature", "thermostats": ["climate.kamar_trv"],
            "windows": [{"id": "w_0a0b0c", "name": "South", "orientation": 0, "area_m2": 2}]}
    refused = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room, "expected_revision": revision},
                                headers={"X-Remote-User-Id": "admin"})
    assert refused.status == 403                                   # no CSRF header
    guest = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room, "expected_revision": revision},
                              headers={**HEADERS, "X-Remote-User-Id": "guest"})
    assert guest.status == 403
    bad = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": {**room, "thermostats": ["sensor.kamar_temperature"]},
                                                     "expected_revision": revision}, headers=HEADERS)
    assert bad.status == 400 and "cannot be used here" in (await bad.json())["problems"][0]["message"]
    ok = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room, "expected_revision": revision}, headers=HEADERS)
    assert ok.status == 200
    stale = await client.post("/api/rooms/save", json={"area_id": KAMAR, "room": room, "expected_revision": revision}, headers=HEADERS)
    assert stale.status == 409
    view = await (await client.get(f"/api/rooms/{KAMAR}", headers=HEADERS)).json()
    assert view["room"]["windows"][0]["orientation"] == 0 and view["status"]["state"] in ("waiting", "collecting", "insufficient")
    levels = {m["level"] for m in view["missing"]}
    assert "required" not in levels and "suggested" in levels
    sim = await (await client.post("/api/rooms/simulate", json={"area_id": KAMAR, "scenario": {"heating": "on"}},
                                   headers=HEADERS)).json()
    assert sim["available"] is False                               # no model yet: says so, invents nothing
    unknown = await client.get("/api/rooms/nope", headers=HEADERS)
    assert unknown.status == 404


async def test_zone_and_settings_endpoints(client):
    rev = (await (await client.get("/api/overview", headers=HEADERS)).json())["revision"]
    for area_id, temp in ((KAMAR, "sensor.kamar_temperature"), ("stova", "sensor.outdoor_temperature")):
        r = await client.post("/api/rooms/save", json={"area_id": area_id, "room": {"name": area_id, "temperature": temp},
                                                       "expected_revision": rev}, headers=HEADERS)
        rev = (await r.json())["revision"]
    r = await client.post("/api/zones/save", json={"zone_id": "new", "zone": {"name": "Open plan", "rooms": [KAMAR, "stova"]},
                                                   "expected_revision": rev}, headers=HEADERS)
    data = await r.json()
    assert r.status == 200 and data["zone_id"].startswith("z_")
    settings = await (await client.get("/api/settings", headers=HEADERS)).json()
    zone = settings["zones"][data["zone_id"]]
    assert zone["connections"] == [{"a": KAMAR, "b": "stova", "strength": "normal", "door": None}]
    r = await client.post("/api/settings/save", json={"settings": {**settings["settings"], "weather": "weather.home"},
                                                      "expected_revision": data["revision"]}, headers=HEADERS)
    assert r.status == 200
    diag = await (await client.get("/api/diagnostics", headers=HEADERS)).json()
    import re
    words = set(re.findall(r"[a-z_]*token[a-z_]*", str(diag).lower()))
    assert diag["configuration"]["rooms"] == 2, diag["configuration"]
    assert words <= {"has_token"}, words      # no token value, only the fact that one exists
