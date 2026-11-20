"""Phase A7.0: Smart Comfort's climate module (observation only).

Model rules, discovery against Home Assistant's registries, the reviewed configuration workflow, live readings
and their quality, the observation store with the Recorder gap fill, restarts, backups, renames and the proof that
nothing can command a thermostat.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from typing import Any

from aiohttp import web
import pytest

from smart_lighting import climate_model as cm
from smart_lighting.climate_observations import ObservationStore, history_rows, intervals, numeric_series, sample
from smart_lighting.companion_protocol import ProtocolError, check_command
from smart_lighting.core import AppCore
from smart_lighting.ha import ALLOWED_COMMANDS, READ_ONLY_COMMANDS
from smart_lighting.web import build_app

from .conftest import ADMIN_HEADERS, USER_ID, FakeHomeAssistant, make_settings, wait_for

NOW = datetime.now(UTC)


def _iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(t if t is not None else time.time(), UTC).isoformat()


def st(entity_id: str, value: str, **attrs: Any) -> dict[str, Any]:
    now = _iso()
    return {"entity_id": entity_id, "state": value, "attributes": attrs, "last_changed": now, "last_updated": now,
            "last_reported": now, "context": {"id": "c", "parent_id": None, "user_id": None}}


def add_climate_home(fake: FakeHomeAssistant) -> None:
    """A home with a TRV, a generic thermostat, a second device in one room, room and outdoor sensors."""
    fake.extra_areas = [{"area_id": "eldhus", "name": "Eldhús"}]
    fake.states += [
        st("climate.kamar_trv", "heat", friendly_name="TRV Kamar", current_temperature=20.5, temperature=21.0,
           hvac_action="heating", hvac_modes=["off", "heat"], supported_features=1 | 128 | 256, min_temp=5,
           max_temp=30, target_temp_step=0.5),
        st("number.kamar_trv_external_temperature_input", "0", friendly_name="TRV external temperature input"),
        st("sensor.kamar_trv_local_temperature", "22.5", friendly_name="TRV local temperature",
           unit_of_measurement="°C", device_class="temperature"),
        st("sensor.kamar_temperature", "20.1", friendly_name="Kamar temperature", unit_of_measurement="°C",
           device_class="temperature"),
        st("climate.stova_thermostat", "heat", friendly_name="Stova thermostat", current_temperature=19.0,
           temperature=22.0, hvac_modes=["off", "heat"], supported_features=1),
        st("climate.stova_heater", "heat", friendly_name="Stova heater", current_temperature=19.5, temperature=18.0,
           hvac_action="idle", hvac_modes=["heat", "off"], supported_features=1),
        st("sensor.stova_temperature", "68.0", friendly_name="Stova temperature", unit_of_measurement="°F",
           device_class="temperature"),
        st("binary_sensor.stova_window", "off", friendly_name="Stova window", device_class="window"),
        st("sensor.outdoor_temperature", "3.5", friendly_name="Outdoor temperature", unit_of_measurement="°C",
           device_class="temperature"),
        st("weather.home", "cloudy", friendly_name="Home", temperature=4.0, temperature_unit="°C"),
        st("sensor.nas_cpu_temperature", "48", friendly_name="NAS CPU temperature", unit_of_measurement="°C",
           device_class="temperature"),
        st("sun.sun", "above_horizon", friendly_name="Sun", elevation=12.3, azimuth=180.0),
    ]
    fake.extra_devices = [
        {"id": "dev-trv", "name": "TRVZB", "name_by_user": None, "manufacturer": "SONOFF", "model": "TRVZB",
         "area_id": "kamar_nordur", "disabled_by": None},
        {"id": "dev-kt", "name": "Kamar sensor", "name_by_user": None, "manufacturer": "Aqara", "model": "WSDCGQ11LM",
         "area_id": "kamar_nordur", "disabled_by": None},
        {"id": "dev-st", "name": "Stova thermostat", "name_by_user": None, "manufacturer": None, "model": None,
         "area_id": "stova", "disabled_by": None},
        {"id": "dev-sh", "name": "Panel heater", "name_by_user": None, "manufacturer": "Adax", "model": "Neo",
         "area_id": "stova", "disabled_by": None},
        {"id": "dev-out", "name": "Outdoor", "name_by_user": None, "manufacturer": "Aqara", "model": "WSDCGQ11LM",
         "area_id": "uti", "disabled_by": None},
    ]
    fake.extra_registry = [
        {"entity_id": "climate.kamar_trv", "id": "reg-trv", "platform": "mqtt", "device_id": "dev-trv",
         "area_id": None, "disabled_by": None, "original_name": None},
        {"entity_id": "number.kamar_trv_external_temperature_input", "id": "reg-trv-ext", "platform": "mqtt",
         "device_id": "dev-trv", "area_id": None, "disabled_by": None,
         "original_name": "External temperature input"},
        {"entity_id": "sensor.kamar_trv_local_temperature", "id": "reg-trv-local", "platform": "mqtt",
         "device_id": "dev-trv", "area_id": None, "disabled_by": None, "original_name": "Local temperature"},
        {"entity_id": "sensor.kamar_temperature", "id": "reg-kt", "platform": "zha", "device_id": "dev-kt",
         "area_id": None, "disabled_by": None, "original_name": "Temperature"},
        {"entity_id": "climate.stova_thermostat", "id": "reg-st", "platform": "generic_thermostat",
         "device_id": "dev-st", "area_id": None, "disabled_by": None, "original_name": None},
        {"entity_id": "climate.stova_heater", "id": "reg-sh", "platform": "adax", "device_id": "dev-sh",
         "area_id": None, "disabled_by": None, "original_name": None},
        {"entity_id": "sensor.stova_temperature", "id": "reg-stt", "platform": "zha", "device_id": None,
         "area_id": "stova", "disabled_by": None, "original_name": "Temperature"},
        {"entity_id": "binary_sensor.stova_window", "id": "reg-sw", "platform": "zha", "device_id": None,
         "area_id": "stova", "disabled_by": None, "original_name": "Window"},
        {"entity_id": "sensor.outdoor_temperature", "id": "reg-out", "platform": "zha", "device_id": "dev-out",
         "area_id": None, "disabled_by": None, "original_name": "Temperature"},
        {"entity_id": "sensor.nas_cpu_temperature", "id": "reg-cpu", "platform": "synology", "device_id": None,
         "area_id": "stova", "disabled_by": None, "original_name": "CPU temperature", "entity_category": "diagnostic"},
    ]


async def start_app(fake: FakeHomeAssistant, aiohttp_client: Callable[..., Awaitable[Any]], **env: str) -> Any:
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
    await wait_for(lambda: core.rooms is not None and core.rooms.api_state == "ok")
    client.core = core
    return client


@pytest.fixture
async def climate_client(fake_ha: FakeHomeAssistant, aiohttp_client: Callable[..., Awaitable[Any]]) -> Any:
    add_climate_home(fake_ha)
    return await start_app(fake_ha, aiohttp_client)


def two_rooms() -> dict[str, Any]:
    config = cm.empty_config()
    config["outdoor"] = {"entity_id": "sensor.outdoor_temperature"}
    config["rooms"] = {
        "kamar_nordur": {**cm.empty_room(), "thermostats": [{"entity_id": "climate.kamar_trv", "kind": "trv"}],
                         "primary_sensor": {"entity_id": "sensor.kamar_temperature"}},
        "stova": {**cm.empty_room(),
                  "thermostats": [{"entity_id": "climate.stova_thermostat", "kind": "generic"},
                                  {"entity_id": "climate.stova_heater", "kind": "other"}],
                  "primary_sensor": {"entity_id": "sensor.stova_temperature"},
                  "openings": [{"entity_id": "binary_sensor.stova_window"}, {"entity_id": "binary_sensor.stova_door"}]},
    }
    return config


async def save(client: Any, config: dict[str, Any], *, headers: dict[str, str] = ADMIN_HEADERS) -> Any:
    doc = await (await client.get("/api/climate")).json()
    return await client.post("/api/climate/save", json={"config": config, "revision": doc["revision"]},
                             headers=headers)


# ------------------------------------------------------------------ the model
def test_units_and_conversion() -> None:
    assert cm.to_celsius("68", "°F") == pytest.approx(20.0)
    assert cm.to_celsius(293.15, "K") == pytest.approx(20.0)
    assert cm.to_celsius("21.5", "°C") == 21.5
    assert cm.to_celsius("21.5", "%") is None and cm.to_celsius("abc", "°C") is None
    assert cm.from_celsius(20.0, "°F") == pytest.approx(68.0)
    assert cm.display_unit({"units": {"display": "auto"}}, "°F") == "°F"
    assert cm.display_unit({"units": {"display": "celsius"}}, "°F") == "°C"


def test_validation_rules() -> None:
    clean = cm.validate(cm.empty_config())
    assert clean["mode"] == "observing" and clean["defaults"]["comfort_c"] == 21.0
    with pytest.raises(cm.ClimateConfigError) as err:
        cm.validate({**cm.empty_config(), "mode": "automatic"})
    assert err.value.errors[0]["path"] == "mode"
    bad = cm.empty_config()
    bad["defaults"] = {**bad["defaults"], "setback_c": 23.0, "comfort_c": 20.0, "max_target_c": 40}
    paths = {e["path"] for e in pytest.raises(cm.ClimateConfigError, cm.validate, bad).value.errors}
    assert "defaults.max_target_c" in paths  # outside the hard safety limits
    bad["defaults"]["max_target_c"] = 24
    paths = {e["path"] for e in pytest.raises(cm.ClimateConfigError, cm.validate, bad).value.errors}
    assert paths == {"defaults.setback_c"}
    # A thermostat belongs to one room; a room to one zone; a shared device must be in the zone.
    config = two_rooms()
    config["rooms"]["eldhus"] = {**cm.empty_room(), "thermostats": [{"entity_id": "climate.kamar_trv"}]}
    config["zones"] = [{"id": "a", "name": "A", "rooms": ["stova"], "shared_thermostats": ["climate.kamar_trv"]},
                       {"id": "b", "name": "B", "rooms": ["stova", "nowhere"]}]
    errors = pytest.raises(cm.ClimateConfigError, cm.validate, config).value.errors
    text = " ".join(e["message"] for e in errors)
    assert "climate.kamar_trv is already assigned to the room" in text
    assert "not a heating device of one of the zone's rooms" in text
    assert "already in the thermal zone a" in text and "nowhere is not a Smart Comfort climate room" in text
    # Wrong types are refused, not coerced.
    assert pytest.raises(cm.ClimateConfigError, cm.validate,
                         {**cm.empty_config(), "observation": {"enabled": "false"}}).value.errors[0]["path"] == \
        "observation.enabled"
    assert pytest.raises(cm.ClimateConfigError, cm.validate, {**cm.empty_config(), "rooms": {"stova": "x"}}
                         ).value.errors[0]["path"] == "rooms.stova"
    # Entity domains are checked: a binary sensor is no temperature sensor.
    config = two_rooms()
    config["rooms"]["stova"]["primary_sensor"] = {"entity_id": "binary_sensor.stova_window"}
    assert pytest.raises(cm.ClimateConfigError, cm.validate, config).value.errors[0]["path"] == \
        "rooms.stova.primary_sensor"


def test_readings_and_quality() -> None:
    now = time.time()
    ok = cm.reading("sensor.t", st("sensor.t", "20.4", unit_of_measurement="°C"), now=now, stale_minutes=60,
                    ha_unit="°C")
    assert (ok["quality"], ok["value_c"], ok["raw"], ok["unit"]) == ("ok", 20.4, "20.4", "°C")
    old = st("sensor.t", "20.4", unit_of_measurement="°C")
    old["last_reported"] = old["last_updated"] = old["last_changed"] = _iso(now - 7200)
    assert cm.reading("sensor.t", old, now=now, stale_minutes=60, ha_unit="°C")["quality"] == "stale"
    for value, unit, quality in (("unavailable", "°C", "unavailable"), ("unknown", "°C", "unknown"),
                                 ("abc", "°C", "not_numeric"), ("20", "%", "wrong_unit"), ("95", "°C", "implausible")):
        r = cm.reading("sensor.t", st("sensor.t", value, unit_of_measurement=unit), now=now, stale_minutes=60,
                       ha_unit="°C")
        assert r["quality"] == quality, value
    assert cm.reading("sensor.t", None, now=now, stale_minutes=60, ha_unit="°C")["quality"] == "missing"
    assert cm.reading(None, None, now=now, stale_minutes=60, ha_unit="°C")["quality"] == "none"
    # The room temperature is never a thermostat's own reading.
    primary = cm.reading("sensor.t", None, now=now, stale_minutes=60, ha_unit="°C")
    assert cm.room_temperature(primary, [])["source"] == "none"
    second = cm.reading("sensor.u", st("sensor.u", "19", unit_of_measurement="°C"), now=now, stale_minutes=60,
                        ha_unit="°C")
    assert cm.room_temperature(primary, [second]) == {"value_c": 19.0, "source": "secondary",
                                                      "entity_ids": ["sensor.u"]}


def test_heating_activity_is_reported_never_inferred() -> None:
    now = time.time()
    reported = cm.thermostat_view("climate.a", st("climate.a", "heat", hvac_action="heating", temperature=22,
                                                  current_temperature=19), now=now, stale_minutes=60, ha_unit="°C")
    assert reported["heating"] == {"state": "heating", "basis": "reported"}
    silent = cm.thermostat_view("climate.b", st("climate.b", "heat", temperature=25, current_temperature=19),
                                now=now, stale_minutes=60, ha_unit="°C")
    assert silent["heating"] == {"state": "unknown", "basis": "not_reported"}  # setpoint above: still not "heating"
    hint = cm.demand_hint(19.0, [silent])
    assert hint["basis"] == "inferred" and "not a report" in hint["text"]
    off = cm.thermostat_view("climate.c", st("climate.c", "off", temperature=25), now=now, stale_minutes=60,
                             ha_unit="°C")
    assert cm.demand_hint(19.0, [off]) is None
    gone = cm.thermostat_view("climate.d", st("climate.d", "unavailable"), now=now, stale_minutes=60, ha_unit="°C")
    assert gone["heating"]["basis"] == "unavailable" and not gone["available"]
    # Fahrenheit thermostats are converted from Home Assistant's unit system.
    f = cm.thermostat_view("climate.e", st("climate.e", "heat", temperature=70, current_temperature=68), now=now,
                           stale_minutes=60, ha_unit="°F")
    assert f["target_c"] == pytest.approx(21.11, abs=0.01) and f["measured"]["value_c"] == pytest.approx(20.0)
    # A climate entity's own temperature_unit attribute wins over the system unit (Home Assistant 2026.11).
    own = cm.thermostat_view("climate.g", st("climate.g", "heat", temperature=70, current_temperature=68,
                                             temperature_unit="°F"), now=now, stale_minutes=60, ha_unit="°C")
    assert own["target_c"] == pytest.approx(21.11, abs=0.01) and own["measured"]["value_c"] == pytest.approx(20.0)


def test_changes_and_rename() -> None:
    before = cm.validate(two_rooms())
    after = json.loads(json.dumps(before))
    del after["rooms"]["kamar_nordur"]
    after["defaults"]["comfort_c"] = 21.5
    after["zones"] = [{"id": "stofa", "name": "Stofa", "rooms": ["stova"]}]
    after = cm.validate(after)
    kinds = [(c["kind"], c["path"]) for c in cm.changes(before, after)]
    assert ("room_removed", "rooms.kamar_nordur") in kinds and ("zone_added", "zones.stofa") in kinds
    assert ("changed", "defaults.comfort_c") in kinds
    assert not any(p.startswith("rooms.kamar_nordur.") for _, p in kinds)
    assert cm.rename(after, "binary_sensor.stova_window", "binary_sensor.stova_window_2")
    assert after["rooms"]["stova"]["openings"][0]["entity_id"] == "binary_sensor.stova_window_2"


def test_observation_records_and_series(tmp_path: Path) -> None:
    t0 = 1_795_000_000.0
    trv = sample("thermostat", "climate.a", st("climate.a", "heat", current_temperature=20, temperature=21),
                 ha_unit="°C", src="live", t=t0)
    assert trv["a"]["action"] is None and trv["a"]["tgt"] == 21.0 and trv["v"] == 20.0  # no hvac_action: null
    rows = [{"s": "heat", "a": {"current_temperature": 19, "temperature": 21, "hvac_action": "idle"}, "lu": t0},
            {"s": "heat", "a": {"current_temperature": 19, "temperature": 21, "hvac_action": "idle",
                                "battery": 80}, "lu": t0 + 30},
            {"s": "heat", "a": {"current_temperature": 19.5, "temperature": 21, "hvac_action": "heating"}, "lu": t0 + 60},
            {"s": "heat", "a": {"current_temperature": 20, "temperature": 21, "hvac_action": "idle"}, "lu": t0 + 600}]
    records = history_rows("thermostat", "climate.a", rows, ha_unit="°C", after=t0, before=t0 + 3600)
    assert len(records) == 3 and all(r["src"] == "recorder" for r in records)  # the battery-only row is not a change
    assert intervals(records, start=t0, end=t0 + 3600,
                     active=lambda r: r["a"]["action"] == "heating") == [[t0 + 60, t0 + 600]]
    assert numeric_series(records, start=t0, end=t0 + 3600, field="tgt", step=True) == [[t0, 21.0]]
    sensor = [sample("room_temp", "sensor.t", st("sensor.t", v, unit_of_measurement="°F"), ha_unit="°C", src="live",
                     t=t0 + i * 60) for i, v in enumerate(("68", "unavailable", "70"))]
    assert [r["q"] for r in sensor] == ["ok", "unavailable", "ok"]
    assert [p[1] for p in numeric_series(sensor, start=t0, end=t0 + 180, buckets=3)] == [20.0, None, 21.11]
    # An outage right after a usable reading (same bucket), then silence: the line must not bridge the outage.
    outage = [sample("room_temp", "sensor.t", st("sensor.t", v, unit_of_measurement="°C"), ha_unit="°C", src="live",
                     t=t0 + dt) for dt, v in ((0, "20"), (5, "unavailable"), (3600, "21"))]
    pts = numeric_series(outage, start=t0, end=t0 + 7200, buckets=10)
    assert [p[1] for p in pts] == [20.0, None, 21.0] and pts[1][0] == t0 + 5
    store = ObservationStore(tmp_path / "obs")
    store.load()
    for rec in sensor:
        store.append(rec)
    assert store.flush() == 3
    store.mark("sensor.t", t0, t0 + 180)
    store.save_state()
    again = ObservationStore(tmp_path / "obs")
    again.load()
    assert again.coverage == {"sensor.t": {"from": t0, "to": t0 + 180}}
    got = again.read({"sensor.t"}, t0 + 30, t0 + 600)["sensor.t"]
    assert [r["s"] for r in got] == ["68", "unavailable", "70"]  # the value at the start, then the window
    # A line cut short by a power loss is skipped, and the next record is not glued onto it.
    path = next((tmp_path / "obs").glob("obs-*.jsonl"))
    path.write_text(path.read_text() + '{"t": 17950')
    assert len(again.read({"sensor.t"}, t0, t0 + 600)["sensor.t"]) == 3
    again.append(sample("room_temp", "sensor.t", st("sensor.t", "21", unit_of_measurement="°C"), ha_unit="°C",
                        src="live", t=t0 + 240))
    assert again.flush() == 1
    assert [r["s"] for r in again.read({"sensor.t"}, t0, t0 + 600)["sensor.t"]] == ["68", "unavailable", "70", "21"]
    # A renamed entity keeps its records; a filled gap is removed, the rest of it stays.
    again.rename("sensor.t", "sensor.u")
    assert len(again.read({"sensor.u"}, t0, t0 + 600)["sensor.u"]) == 4
    again.add_gap("sensor.u", t0, t0 + 3600)
    again.fill_gap("sensor.u", t0, t0 + 1800)
    assert again.gaps["sensor.u"] == [[t0 + 1800, t0 + 3600]]
    # A recorded gap breaks lines and bands.
    flat = [sample("room_temp", "sensor.t", st("sensor.t", "20", unit_of_measurement="°C"), ha_unit="°C",
                   src="live", t=t0)]
    assert [p[1] for p in numeric_series(flat, start=t0, end=t0 + 7200, gaps=[[t0 + 600, t0 + 1200]])] == [20.0, None]
    on = [{**r, "v": 1} for r in flat]
    assert intervals(on, start=t0, end=t0 + 7200, active=lambda r: r["v"] == 1, gaps=[[t0 + 600, t0 + 1200]]) == [
        [t0, t0 + 600], [t0 + 1200, t0 + 7200]]


# -------------------------------------------------------------- the service
async def test_fresh_installation_without_climate_devices(app_client: Any) -> None:
    overview = await (await app_client.get("/api/climate/overview")).json()
    assert overview["mode"] == "observing" and overview["rooms"] == [] and overview["problems"] == []
    assert overview["learning"]["available"] is False
    discovery = await (await app_client.get("/api/climate/discovery")).json()
    assert discovery["thermostats"] == [] and discovery["suggestions"]["rooms"] == {}
    doc = await (await app_client.get("/api/climate")).json()
    assert doc["status"]["writable"] and doc["config"]["rooms"] == {} and doc["unit"] == "°C"
    # A climate room without any heating device: observed only.
    config = cm.empty_config()
    config["rooms"]["kamar_nordur"] = {**cm.empty_room(), "primary_sensor": None}
    resp = await save(app_client, config)
    assert resp.status == 200, await resp.text()
    room = await (await app_client.get("/api/climate/rooms/kamar_nordur")).json()
    assert room["thermostats"] == [] and room["temperature"]["source"] == "none"
    assert not room["completeness"]["ready"]
    health = await (await app_client.get("/api/health")).json()
    assert any(c["key"] == "climate" for c in health["checks"])


async def test_discovery_inspects_capabilities(climate_client: Any) -> None:
    d = await (await climate_client.get("/api/climate/discovery")).json()
    by = {t["entity_id"]: t for t in d["thermostats"]}
    trv = by["climate.kamar_trv"]
    assert trv["kind"] == "trv" and trv["area_id"] == "kamar_nordur"
    assert trv["capabilities"]["reports_action"] and trv["capabilities"]["turn_off"]
    assert trv["capabilities"]["external_input"] == "number.kamar_trv_external_temperature_input"
    generic = by["climate.stova_thermostat"]
    assert generic["kind"] == "generic" and generic["capabilities"]["reports_action"] is False
    assert generic["capabilities"]["external_input"] is None
    sensors = {s["entity_id"]: s for s in d["sensors"]}
    assert sensors["sensor.kamar_trv_local_temperature"]["on_thermostat"]
    assert sensors["sensor.outdoor_temperature"]["outdoor"] and sensors["sensor.nas_cpu_temperature"]["not_room"]
    assert sensors["sensor.stova_temperature"]["value_c"] == pytest.approx(20.0)
    assert {o["entity_id"]: o["kind"] for o in d["openings"]} == {"binary_sensor.stova_door": "door",
                                                                   "binary_sensor.stova_window": "window"}
    rooms = d["suggestions"]["rooms"]
    assert rooms["kamar_nordur"]["primary_sensor"] == "sensor.kamar_temperature"  # not the TRV's own sensor
    assert {t["entity_id"] for t in rooms["stova"]["thermostats"]} == {"climate.stova_thermostat",
                                                                        "climate.stova_heater"}
    assert rooms["stova"]["primary_sensor"] == "sensor.stova_temperature"  # the CPU sensor is not suggested
    assert d["suggestions"]["outdoor"] == "sensor.outdoor_temperature"
    # Nothing is configured by discovering.
    assert (await (await climate_client.get("/api/climate")).json())["config"]["rooms"] == {}


async def test_reviewed_save_and_live_readings(climate_client: Any, fake_ha: FakeHomeAssistant) -> None:
    config = two_rooms()
    doc = await (await climate_client.get("/api/climate")).json()
    review = await (await climate_client.post("/api/climate/review", json={"config": config, "revision": doc["revision"]},
                                              headers=ADMIN_HEADERS)).json()
    assert review["valid"] and not review["identical"]
    assert [c["kind"] for c in review["changes"]][:2] == ["room_added", "room_added"]
    assert not [w for w in review["warnings"] if w["code"] in ("unknown_entity", "other_area", "no_primary")]
    # Not an administrator: refused before anything is read.
    resp = await save(climate_client, config, headers={"X-Remote-User-Id": USER_ID, "X-Smart-Lighting": "1"})
    assert resp.status == 403
    resp = await save(climate_client, config)
    assert resp.status == 200, await resp.text()
    saved = (await resp.json())["document"]
    trv = saved["config"]["rooms"]["kamar_nordur"]["thermostats"][0]
    assert trv["uid"] == "reg-trv" and trv["capabilities"]["reports_action"] is True  # snapshot from Home Assistant
    # A stale revision is refused.
    resp = await climate_client.post("/api/climate/save", json={"config": config, "revision": doc["revision"]},
                                     headers=ADMIN_HEADERS)
    assert resp.status == 409 and (await resp.json())["code"] == "stale_revision"
    # Invalid: refused with every problem.
    bad = json.loads(json.dumps(config))
    bad["mode"] = "automatic"
    resp = await save(climate_client, bad)
    body = await resp.json()
    assert resp.status == 400 and body["code"] == "invalid_config" and body["errors"][0]["path"] == "mode"

    overview = await (await climate_client.get("/api/climate/overview")).json()
    rooms = {r["area_id"]: r for r in overview["rooms"]}
    kamar, stova = rooms["kamar_nordur"], rooms["stova"]
    assert kamar["temperature"] == {"value_c": 20.1, "source": "primary", "entity_ids": ["sensor.kamar_temperature"]}
    assert kamar["thermostats"][0]["measured"]["value_c"] == 20.5  # kept separate from the room's sensor
    assert kamar["heating"] == {"reported": 1, "not_reported": 0, "devices": 1}
    assert kamar["outdoor_difference_c"] == pytest.approx(16.6)
    assert stova["temperature"]["value_c"] == pytest.approx(20.0)  # 68 °F
    assert stova["heating"]["not_reported"] == 1  # the generic thermostat reports no action
    assert stova["demand"]["entity_ids"] == ["climate.stova_thermostat"]  # 22 °C requested: possible demand only
    assert {o["entity_id"]: o["status"] for o in stova["openings"]} == {"binary_sensor.stova_window": "closed",
                                                                         "binary_sensor.stova_door": "unavailable"}
    assert any(p["entity_id"] == "binary_sensor.stova_door" for p in overview["problems"])
    assert kamar["completeness"]["ready"] and overview["completeness"]["rooms"] == 2
    # A sensor goes unavailable: the room has no temperature, and the thermostat's reading is not used instead.
    await fake_ha.emit_live("sensor.kamar_temperature", "unavailable")
    await wait_for(lambda: climate_client.core.ha.states["sensor.kamar_temperature"]["state"] == "unavailable")
    room = await (await climate_client.get("/api/climate/rooms/kamar_nordur")).json()
    assert room["primary"]["quality"] == "unavailable" and room["temperature"]["source"] == "none"
    assert any(p["entity_id"] == "sensor.kamar_temperature"
               for p in (await (await climate_client.get("/api/climate/overview")).json())["problems"])


async def test_history_from_the_recorder_and_live(fake_ha: FakeHomeAssistant,
                                                  aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    t0 = time.time() - 3 * 3600
    fake_ha.history["sensor.kamar_temperature"] = [
        {"s": "19.0", "a": {"unit_of_measurement": "°C"}, "lu": t0},
        {"s": "19.6", "a": {"unit_of_measurement": "°C"}, "lu": t0 + 1800},
        {"s": "unavailable", "a": {}, "lu": t0 + 3600},
        {"s": "20.1", "a": {"unit_of_measurement": "°C"}, "lu": t0 + 4000}]
    fake_ha.history["climate.kamar_trv"] = [
        {"s": "heat", "a": {"current_temperature": 19, "temperature": 21, "hvac_action": "heating"}, "lu": t0},
        {"s": "heat", "a": {"current_temperature": 20.5, "temperature": 21, "hvac_action": "idle"}, "lu": t0 + 5400}]
    client = await start_app(fake_ha, aiohttp_client)
    config = cm.empty_config()
    config["rooms"]["kamar_nordur"] = two_rooms()["rooms"]["kamar_nordur"]
    assert (await save(client, config)).status == 200
    service = client.core.climate
    await wait_for(lambda: service.backfill["state"] == "done", timeout=10)
    calls = [c for c in fake_ha.history_calls if "climate.kamar_trv" in c["entity_ids"]]
    assert calls and calls[0]["minimal_response"] is False and calls[0]["end_time"]
    span = service.store.coverage["sensor.kamar_temperature"]
    # Observation starts at the Recorder's first value (not 7 days back), and the days before are no gap.
    assert span["from"] == pytest.approx(t0, abs=1) and span["to"] >= time.time() - 60
    assert "sensor.kamar_temperature" not in service.store.gaps
    assert len(fake_ha.history_calls) >= 7  # bounded 24-hour windows over backfill_days
    # Live changes are recorded on top.
    await fake_ha.emit_live_context("climate.kamar_trv", "heat", {"friendly_name": "TRV Kamar",
                                    "current_temperature": 20.9, "temperature": 22, "hvac_action": "heating"}, "x")
    await wait_for(lambda: any(r["e"] == "climate.kamar_trv" and r["src"] == "live" for r in service.store._buffer))
    history = await (await client.get("/api/climate/rooms/kamar_nordur/history?window=86400")).json()
    series = {s["key"]: s for s in history["series"]}
    assert [p[1] for p in series["primary"]["points"] if p[1] is not None][:2] == [19.0, 19.6]
    assert None in [p[1] for p in series["primary"]["points"]]  # the unavailable reading stays a gap
    assert series["setpoint:climate.kamar_trv"]["points"][-1][1] == 22.0
    heating = next(b for b in history["bands"] if b["kind"] == "heating")
    assert heating["reported"] and heating["intervals"][0][0] == pytest.approx(t0, abs=1)
    assert heating["intervals"][-1][1] == pytest.approx(history["end"], abs=5)  # heating again, now
    resp = await client.get("/api/climate/rooms/kamar_nordur/history?window=12345")
    assert resp.status == 400


async def test_restart_keeps_configuration_and_fills_the_gap(fake_ha: FakeHomeAssistant,
                                                             aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)
    assert (await save(client, two_rooms())).status == 200
    service = client.core.climate
    await wait_for(lambda: service.backfill["state"] == "done", timeout=10)
    revision = service.revision
    await client.core.stop()  # the App stops (coverage and buffered records are stored)
    to = service.store.coverage["sensor.kamar_temperature"]["to"]
    for span in service.store.coverage.values():
        span["to"] -= 1800  # pretend the App was away for half an hour
    service.store.save_state()
    fake_ha.history_calls.clear()
    again = await start_app(fake_ha, aiohttp_client)
    assert again.core.climate.revision == revision
    assert again.core.climate.backfill["state"] in ("idle", "running", "done")
    await wait_for(lambda: again.core.climate.backfill["state"] == "done", timeout=10)
    gap_calls = [c for c in fake_ha.history_calls if "sensor.kamar_temperature" in c["entity_ids"]]
    assert gap_calls
    start = datetime.fromisoformat(gap_calls[0]["start_time"]).timestamp()
    assert start == pytest.approx(to - 1800, abs=2)


async def test_restart_without_a_gap_reports_done(fake_ha: FakeHomeAssistant,
                                                   aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)
    assert (await save(client, two_rooms())).status == 200
    await wait_for(lambda: client.core.climate.backfill["state"] == "done", timeout=10)
    client.core.climate._mark_live(time.time())
    await client.core.stop()  # a short restart: nothing to read from the Recorder
    again = await start_app(fake_ha, aiohttp_client)
    await wait_for(lambda: again.core.climate.backfill["state"] == "done", timeout=10)
    assert again.core.climate.backfill["windows"] == 0


async def test_backups_restore_export_import(climate_client: Any) -> None:
    assert (await save(climate_client, two_rooms())).status == 200
    changed = two_rooms()
    changed["defaults"]["comfort_c"] = 22.0
    resp = await save(climate_client, changed)
    assert (await resp.json())["result"]["backup"]
    assert (await climate_client.get("/api/climate/backups")).status == 403  # administrators only
    backups = (await (await climate_client.get("/api/climate/backups", headers=ADMIN_HEADERS)).json())["backups"]
    assert backups and backups[0]["summary"] == {"rooms": 2, "zones": 0, "thermostats": 3}
    review = await (await climate_client.post("/api/climate/restore/review", json={"backup": backups[0]["id"]},
                                              headers=ADMIN_HEADERS)).json()
    assert [c["path"] for c in review["changes"]] == ["defaults.comfort_c"]
    doc = await (await climate_client.get("/api/climate")).json()
    resp = await climate_client.post("/api/climate/restore", json={"backup": backups[0]["id"],
                                                                   "revision": doc["revision"]}, headers=ADMIN_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["document"]["config"]["defaults"]["comfort_c"] == 21.0
    assert (await climate_client.get("/api/climate/export")).status == 403
    export = await (await climate_client.get("/api/climate/export", headers=ADMIN_HEADERS)).json()
    review = await (await climate_client.post("/api/climate/import/review", json={"file": export},
                                              headers=ADMIN_HEADERS)).json()
    assert review["identical"]
    resp = await climate_client.post("/api/climate/import/review", json={"file": {"format": "other"}},
                                     headers=ADMIN_HEADERS)
    assert resp.status == 400
    newer = {**export, "config": {**export["config"], "schema": 99}}
    resp = await climate_client.post("/api/climate/import/review", json={"file": newer}, headers=ADMIN_HEADERS)
    assert resp.status == 400 and "newer App" in (await resp.json())["error"]
    resp = await climate_client.get("/api/climate/backups/../../etc", headers=ADMIN_HEADERS)
    assert resp.status == 404


async def test_damaged_configuration_is_recovered(fake_ha: FakeHomeAssistant,
                                                  aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    client = await start_app(fake_ha, aiohttp_client)
    assert (await save(client, two_rooms())).status == 200
    changed = two_rooms()
    changed["defaults"]["comfort_c"] = 22.0
    assert (await save(client, changed)).status == 200  # the first version is now a backup
    await client.core.stop()
    path = Path(fake_ha.data_dir) / "climate" / "config.json"
    path.write_text("{ damaged")
    again = await start_app(fake_ha, aiohttp_client)
    status = again.core.climate.status()
    assert status["notices"][0]["code"] == "recovered" and len(again.core.climate.config["rooms"]) == 2
    assert list(path.parent.glob("config.corrupt-*.json"))


async def test_renamed_entities_are_followed(climate_client: Any, fake_ha: FakeHomeAssistant) -> None:
    assert (await save(climate_client, two_rooms())).status == 200
    reg = next(r for r in fake_ha.extra_registry if r["entity_id"] == "sensor.kamar_temperature")
    reg["entity_id"] = "sensor.kamar_temperature_2"
    fake_ha.states = [s for s in fake_ha.states if s["entity_id"] != "sensor.kamar_temperature"] + [
        st("sensor.kamar_temperature_2", "20.3", unit_of_measurement="°C", device_class="temperature")]
    await fake_ha.emit_event("entity_registry_updated", {"action": "update", "entity_id": "sensor.kamar_temperature_2",
                                                         "old_entity_id": "sensor.kamar_temperature"})
    service = climate_client.core.climate
    await wait_for(lambda: service.config["rooms"]["kamar_nordur"]["primary_sensor"]["entity_id"]
                   == "sensor.kamar_temperature_2", timeout=10)
    assert any(b["reason"].startswith("Before following a rename") for b in service.backups())
    # The companion's rename relay is followed as well.
    assert await service.rename("binary_sensor.stova_window", "binary_sensor.stova_window_2")
    assert service.config["rooms"]["stova"]["openings"][0]["entity_id"] == "binary_sensor.stova_window_2"


async def test_fahrenheit_home(fake_ha: FakeHomeAssistant, aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    fake_ha.unit_temperature = "°F"
    fake_ha.states = [s for s in fake_ha.states if s["entity_id"] != "climate.kamar_trv"] + [
        st("climate.kamar_trv", "heat", current_temperature=68, temperature=70, hvac_action="idle",
           supported_features=1, min_temp=41, max_temp=86)]
    client = await start_app(fake_ha, aiohttp_client)
    assert (await save(client, two_rooms())).status == 200
    room = await (await client.get("/api/climate/rooms/kamar_nordur")).json()
    trv = room["thermostats"][0]
    assert trv["measured"]["value_c"] == pytest.approx(20.0) and trv["target_c"] == pytest.approx(21.11, abs=0.01)
    assert trv["capabilities"]["min_temp_c"] == pytest.approx(5.0)
    assert (await (await client.get("/api/climate")).json())["unit"] == "°F"


async def test_no_heating_command_can_be_sent(climate_client: Any, fake_ha: FakeHomeAssistant) -> None:
    assert (await save(climate_client, two_rooms())).status == 200
    await (await climate_client.get("/api/climate/overview")).json()
    await (await climate_client.get("/api/climate/rooms/kamar_nordur/history?window=21600")).json()
    sent = {c["type"] for c in fake_ha.commands}
    assert sent <= ALLOWED_COMMANDS
    assert not any(c.startswith(("climate", "call_service", "execute_script")) for c in sent)
    assert not fake_ha.service_calls
    assert not any(c.startswith("climate") or "service" in c for c in ALLOWED_COMMANDS)
    assert "history/history_during_period" in READ_ONLY_COMMANDS
    # The companion's executor refuses a thermostat command for every owner.
    for domain in ("lighting", "curtains", "climate", "occupancy"):
        with pytest.raises(ProtocolError):
            check_command(domain, "climate", "set_temperature", {"entity_id": "climate.kamar_trv", "temperature": 30},
                          targets={"lights": ["climate.kamar_trv"], "covers": ["climate.kamar_trv"]})
    safety = (await (await climate_client.get("/api/overview")).json())["safety"]
    assert safety["heating_control"] is False and safety["service_calls_sent"] == 0


async def test_entity_added_later_is_read_not_assumed_observed(fake_ha: FakeHomeAssistant,
                                                              aiohttp_client: Callable[..., Awaitable[Any]]) -> None:
    add_climate_home(fake_ha)
    t0 = time.time() - 2 * 3600
    fake_ha.history["sensor.stova_temperature"] = [{"s": "68", "a": {"unit_of_measurement": "°F"}, "lu": t0}]
    client = await start_app(fake_ha, aiohttp_client)
    config = cm.empty_config()
    config["rooms"]["kamar_nordur"] = two_rooms()["rooms"]["kamar_nordur"]
    assert (await save(client, config)).status == 200
    service = client.core.climate
    await wait_for(lambda: service.backfill["state"] == "done", timeout=10)
    # The App has been connected for ten days when Stova is added.
    service._live_since -= 10 * 86400
    for entity_id in list(service._since):
        service._since[entity_id] -= 10 * 86400
    fake_ha.history_calls.clear()
    assert (await save(client, two_rooms())).status == 200
    await wait_for(lambda: any("sensor.stova_temperature" in c["entity_ids"] for c in fake_ha.history_calls), timeout=10)
    await wait_for(lambda: service.backfill["state"] == "done", timeout=10)
    service._mark_live(time.time())
    span = service.store.coverage["sensor.stova_temperature"]
    assert span["from"] == pytest.approx(t0, abs=1)  # from the Recorder's first value, not ten days back
    assert service.history_days() < 1


async def test_failed_gap_fill_keeps_the_gap(climate_client: Any) -> None:
    assert (await save(climate_client, two_rooms())).status == 200
    service = climate_client.core.climate
    await wait_for(lambda: service.backfill["state"] == "done", timeout=10)
    service._mark_live(time.time())
    now = time.time()
    service.store.coverage["sensor.kamar_temperature"]["to"] = now - 1800

    async def timeout(*_args: Any, **_kw: Any) -> Any:
        raise TimeoutError

    original = service.ha.command
    service.ha.command = timeout
    try:
        service._start_observing({"sensor.kamar_temperature"}, now)
        await wait_for(lambda: service.backfill["state"] == "error", timeout=10)
    finally:
        service.ha.command = original
    service._mark_live(time.time())  # live coverage moves on, the gap stays recorded
    assert service.store.gaps["sensor.kamar_temperature"][-1][0] == pytest.approx(now - 1800, abs=1)


async def test_conflicting_rename_is_not_followed(climate_client: Any) -> None:
    config = two_rooms()
    config["rooms"]["kamar_nordur"]["secondary_sensors"] = [{"entity_id": "sensor.kamar_trv_local_temperature"}]
    assert (await save(climate_client, config)).status == 200
    service = climate_client.core.climate
    revision = service.revision
    assert not await service.rename("sensor.kamar_trv_local_temperature", "sensor.kamar_temperature")
    assert service.revision == revision and service.status()["notices"][-1]["code"] == "rename_conflict"

