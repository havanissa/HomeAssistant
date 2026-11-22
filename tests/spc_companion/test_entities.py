"""The native entities: generic creation, identity, freshness, outages, restarts, renames, deletion, coexistence."""

from __future__ import annotations

from datetime import timedelta
import logging

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.smart_predictive_climate.const import DOMAIN, NAME

from .conftest import CONNECTED, FORECAST, HEAT, KAMAR, STOVA, App, binary, room, sensor, setup_spc, temperature

FULL = {
    "forecast_1h": temperature(attributes={"confidence": 0.8, "curve": [21.0, 21.2, 21.4], "note": None}),
    "heat": binary(icon="mdi:radiator"),
    "comfort_at": sensor("Comfortable at", "2026-11-22T14:30:00+00:00", device_class="timestamp"),
    "advice": sensor("Advice", "preheat", device_class="enum",
                     attributes={"options": ["none", "preheat", "ventilate"], "reason": "Cold front at 15:00"}),
    "time_to_target": sensor("Time to target", 45, device_class="duration", unit="min"),
    "summary": sensor("Summary", "Warming slowly", icon="mdi:text"),
    "model_ok": binary("Model problem", False, device_class="problem", category="diagnostic"),
    "samples": sensor("Samples", 1234, category="diagnostic"),
}


def _device(hass, room_id):
    found = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, f"room_{room_id}")})
    return found[0] if found else None


def _ours(hass):
    registry = er.async_get(hass)
    return {e.entity_id: e for e in registry.entities.values() if e.platform == DOMAIN}


def _room_states(hass):
    return [hass.states.get(e) for e in _ours(hass) if e != CONNECTED]


async def test_generic_entities(hass: HomeAssistant, app):
    await app.publish({KAMAR: room(entities=FULL)})
    await hass.async_block_till_done()
    ours = _ours(hass)
    assert {e: r.unique_id for e, r in ours.items()} == {
        "sensor.spc_kamar_sudur_forecast_1h": f"{KAMAR}_forecast_1h",
        "binary_sensor.spc_kamar_sudur_heat": f"{KAMAR}_heat",
        "sensor.spc_kamar_sudur_comfort_at": f"{KAMAR}_comfort_at",
        "sensor.spc_kamar_sudur_advice": f"{KAMAR}_advice",
        "sensor.spc_kamar_sudur_time_to_target": f"{KAMAR}_time_to_target",
        "sensor.spc_kamar_sudur_summary": f"{KAMAR}_summary",
        "binary_sensor.spc_kamar_sudur_model_ok": f"{KAMAR}_model_ok",
        "sensor.spc_kamar_sudur_samples": f"{KAMAR}_samples",
        CONNECTED: "app-connected",
    }
    forecast = hass.states.get(FORECAST)
    assert forecast.state == "21.4"
    assert forecast.attributes == {
        "device_class": "temperature", "unit_of_measurement": "°C", "state_class": "measurement",
        "friendly_name": "Kamar Suður Forecast in 1 hour", "confidence": 0.8, "curve": [21.0, 21.2, 21.4],
        "note": None}
    heat = hass.states.get(HEAT)
    assert heat.state == "on" and heat.attributes == {
        "device_class": "heat", "icon": "mdi:radiator", "friendly_name": "Kamar Suður Heating recommended"}
    # A timestamp is a real time for Home Assistant; an enum has its options (once, not as an extra attribute).
    when = hass.states.get("sensor.spc_kamar_sudur_comfort_at")
    assert when.state == "2026-11-22T14:30:00+00:00" and when.attributes["device_class"] == "timestamp"
    advice = hass.states.get("sensor.spc_kamar_sudur_advice")
    assert advice.state == "preheat" and advice.attributes["options"] == ["none", "preheat", "ventilate"]
    assert advice.attributes["reason"] == "Cold front at 15:00" and advice.attributes["device_class"] == "enum"
    duration = hass.states.get("sensor.spc_kamar_sudur_time_to_target")
    assert duration.state == "45" and duration.attributes["unit_of_measurement"] == "min"
    assert "state_class" not in duration.attributes
    summary = hass.states.get("sensor.spc_kamar_sudur_summary")
    assert summary.state == "Warming slowly" and summary.attributes["icon"] == "mdi:text"
    assert "device_class" not in summary.attributes and "unit_of_measurement" not in summary.attributes
    assert hass.states.get("binary_sensor.spc_kamar_sudur_model_ok").state == "off"
    assert ours["binary_sensor.spc_kamar_sudur_model_ok"].entity_category == "diagnostic"
    assert ours["sensor.spc_kamar_sudur_samples"].entity_category == "diagnostic"
    assert ours[FORECAST].entity_category is None
    # One device per room; the App has its own.
    device = _device(hass, KAMAR)
    assert device.name == "Kamar Suður" and device.model == "Room climate forecast" and device.manufacturer == NAME
    assert device.entry_type is dr.DeviceEntryType.SERVICE and device.area_id is None
    assert all(r.device_id == device.id for e, r in ours.items() if e != CONNECTED)
    connected = hass.states.get(CONNECTED)
    assert connected.state == "on" and connected.attributes["device_class"] == "connectivity"
    assert connected.attributes["paired"] is True and connected.attributes["rooms"] == 1
    assert connected.attributes["app_version"] == "0.1.0"
    assert ours[CONNECTED].entity_category == "diagnostic"
    app_device = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, "app")})[0]
    assert ours[CONNECTED].device_id == app_device.id and app_device.name == NAME


async def test_a_value_of_none_is_unknown(hass, app):
    entities = {k: {**v, "value": None} for k, v in FULL.items()}
    await app.publish({KAMAR: room(entities=entities)})
    await hass.async_block_till_done()
    states = _room_states(hass)
    assert len(states) == len(FULL) and all(s.state == "unknown" for s in states)
    await app.publish({KAMAR: room(entities=FULL)})
    assert hass.states.get(FORECAST).state == "21.4" and hass.states.get(HEAT).state == "on"


async def test_states_are_written_only_on_change(hass, app):
    await app.publish({KAMAR: room(entities=FULL)})
    await hass.async_block_till_done()
    before = {s.entity_id: s.last_updated for s in _room_states(hass)}
    await app.publish({KAMAR: room(entities=FULL)})
    await hass.async_block_till_done()
    assert {s.entity_id: s.last_updated for s in _room_states(hass)} == before
    # Only the entity whose attributes changed is written again.
    changed = {**FULL, "forecast_1h": temperature(attributes={"confidence": 0.9})}
    await app.publish({KAMAR: room(entities=changed)})
    await hass.async_block_till_done()
    after = {s.entity_id: s.last_updated for s in _room_states(hass)}
    assert {e for e in after if after[e] != before[e]} == {FORECAST}
    assert hass.states.get(FORECAST).attributes["confidence"] == 0.9


async def test_freshness_when_the_lease_expires(hass, app):
    await app.publish({KAMAR: room(entities=FULL)})
    # The App stops sending (crash, update): the lease runs out.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=32))
    await hass.async_block_till_done()
    states = _room_states(hass)
    assert len(states) == len(FULL) and all(s.state == "unavailable" for s in states)
    # No stale value anywhere, not in the attributes either.
    forecast = hass.states.get(FORECAST)
    assert "confidence" not in forecast.attributes and "curve" not in forecast.attributes
    assert "reason" not in hass.states.get("sensor.spc_kamar_sudur_advice").attributes
    assert "21.4" not in str([s.as_dict() for s in states])
    assert hass.states.get(CONNECTED).state == "off"
    # Its next message is refused: it must claim again (a new epoch), and only a publish makes them available.
    assert await app.publish_refused({KAMAR: room()}) == "not_owner"
    epoch = app.epoch
    await app.claim()
    assert app.epoch == epoch + 1
    assert hass.states.get(FORECAST).state == "unavailable" and hass.states.get(CONNECTED).state == "off"
    await app.ws.call("heartbeat", **app.fence())
    assert hass.states.get(FORECAST).state == "unavailable"  # a lease alone is not a result
    await app.publish({KAMAR: room(entities=FULL)})
    assert hass.states.get(FORECAST).state == "21.4" and hass.states.get(CONNECTED).state == "on"


async def test_heartbeats_keep_the_entities_available(hass, app):
    await app.publish({KAMAR: room()})
    for _ in range(4):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=20))
        await hass.async_block_till_done()
        await app.ws.call("heartbeat", **app.fence())
    assert hass.states.get(FORECAST).state == "21.4" and hass.states.get(CONNECTED).state == "on"


async def test_freshness_when_the_app_session_closes(hass, app):
    await app.publish({KAMAR: room()})
    await app.ws.close()
    await hass.async_block_till_done()
    assert hass.states.get(FORECAST).state == "unavailable" and hass.states.get(HEAT).state == "unavailable"
    assert hass.states.get(CONNECTED).state == "off"


async def test_restart_keeps_the_entities_unavailable_until_the_app_publishes(hass, app, ws):
    await app.publish({KAMAR: room(entities=FULL)})
    await hass.async_block_till_done()
    before = {e: r.unique_id for e, r in _ours(hass).items()}
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    # The stored snapshot only tells which entities exist: the same ones, all unavailable.
    assert {e: r.unique_id for e, r in _ours(hass).items()} == before
    states = _room_states(hass)
    assert len(states) == len(FULL) and all(s.state == "unavailable" for s in states)
    assert "21.4" not in str([s.as_dict() for s in states])
    assert hass.states.get(CONNECTED).state == "off"
    # A replay of a message from before the restart is refused (the session and the lease are gone).
    assert await app.publish_refused({KAMAR: room()}) == "unknown_session"
    again = App(await ws(), token=app.token)
    assert (await again.open())["trusted"]
    result = await again.claim()
    assert result["epoch"] == app.epoch + 1  # the epoch survived the restart
    assert hass.states.get(FORECAST).state == "unavailable"
    await again.publish({KAMAR: room(entities=FULL)})
    assert hass.states.get(FORECAST).state == "21.4"
    assert {e: r.unique_id for e, r in _ours(hass).items()} == before


async def test_a_stored_snapshot_from_before_a_restart(hass, ws, hass_storage):
    """Home Assistant starts with the store of an earlier run: entities exist, unavailable; the pairing works."""
    import secrets

    from custom_components.smart_predictive_climate import protocol as P
    from custom_components.smart_predictive_climate.const import STORE_KEY

    token = secrets.token_hex(16)
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {
        "pairing": {"hash": P.token_hash(token), "paired_at": "2026-11-22T10:00:00+00:00", "by": "u",
                    "instance": "abcd1234"},
        "ever_paired": True, "epoch": 7, "views": {KAMAR: room()}, "orphaned": {STOVA: room("Stova")},
        "published_at": "2026-11-22T10:00:00+00:00", "app_version": "0.1.0", "history": []}}
    await setup_spc(hass)
    assert hass.states.get(FORECAST).state == "unavailable"
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "unavailable"
    a = App(await ws(), token=token)
    assert (await a.open())["trusted"]  # no new pairing needed
    assert (await a.claim())["epoch"] == 8
    await a.publish({KAMAR: room()})
    assert hass.states.get(FORECAST).state == "21.4"
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "unavailable"  # still orphaned


async def test_an_unusable_store_is_ignored(hass, ws, hass_storage):
    from custom_components.smart_predictive_climate.const import STORE_KEY

    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": {
        "epoch": 3, "views": {KAMAR: {"name": "Kamar", "entities": {"x": {"platform": "switch"}}}}}}
    await setup_spc(hass)
    assert _room_states(hass) == []
    hub = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert hub.epoch == 3 and hub.views == {}


async def test_rename_keeps_unique_ids_and_entity_ids(hass, app):
    await app.publish({KAMAR: room()})
    await hass.async_block_till_done()
    before = {e: r.unique_id for e, r in _ours(hass).items()}
    await app.publish({KAMAR: room("Kamar Norður", forecast_1h=temperature("Forecast (1 h)"))})
    await hass.async_block_till_done()
    assert _device(hass, KAMAR).name == "Kamar Norður"
    assert {e: r.unique_id for e, r in _ours(hass).items()} == before
    # The room's and the entity's new names show; the ids stay.
    assert hass.states.get(FORECAST).attributes["friendly_name"] == "Kamar Norður Forecast (1 h)"
    assert hass.states.get(HEAT).attributes["friendly_name"] == "Kamar Norður Heating recommended"


async def test_a_removed_key_removes_its_entity(hass, app):
    await app.publish({KAMAR: room(), STOVA: room("Stova")})
    await hass.async_block_till_done()
    await app.publish({KAMAR: room(entities={"forecast_1h": temperature()}), STOVA: room("Stova")})
    await hass.async_block_till_done()
    assert HEAT not in _ours(hass) and hass.states.get(HEAT) is None
    assert hass.states.get(FORECAST).state == "21.4"
    assert hass.states.get("binary_sensor.spc_stova_heat").state == "on"  # the other room keeps its own
    # It comes back when the App publishes the key again, and a key may change its platform.
    await app.publish({KAMAR: room(), STOVA: room("Stova", heat=sensor("Heating", "soon"))})
    await hass.async_block_till_done()
    assert hass.states.get(HEAT).state == "on"
    assert "binary_sensor.spc_stova_heat" not in _ours(hass)
    assert hass.states.get("sensor.spc_stova_heat").state == "soon"


async def test_orphaned_deleted_and_empty(hass, app):
    await app.publish({KAMAR: room(), STOVA: room("Stova")})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "21.4"
    # Missing from a snapshot (a mistake, a reset App): kept, unavailable ("orphaned").
    await app.publish({KAMAR: room()})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "unavailable"
    assert hass.states.get("binary_sensor.spc_stova_heat").state == "unavailable"
    assert (await app.ws.call("status"))["orphaned"] == 1 and _device(hass, STOVA) is not None
    assert hass.states.get(FORECAST).state == "21.4"
    # Published again: available again, the same entities.
    await app.publish({KAMAR: room(), STOVA: room("Stova")})
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "21.4"
    # Deleted by an administrator in the App: its entities and its device go.
    await app.publish({KAMAR: room()}, deleted=[STOVA])
    await hass.async_block_till_done()
    assert not [e for e in _ours(hass) if "stova" in e] and _device(hass, STOVA) is None
    assert hass.states.get("sensor.spc_stova_forecast_1h") is None
    # An App without any room (fresh or reset) never deletes entities: they stay, unavailable.
    await app.publish({})
    await hass.async_block_till_done()
    assert FORECAST in _ours(hass) and hass.states.get(FORECAST).state == "unavailable"
    assert hass.states.get(CONNECTED).state == "on" and hass.states.get(CONNECTED).attributes["rooms"] == 0
    await app.publish({KAMAR: room()})
    await hass.async_block_till_done()
    assert hass.states.get(FORECAST).state == "21.4"
    # Deleting the last room works too, also when it was orphaned first.
    await app.publish({})
    await app.publish({}, deleted=[KAMAR])
    await hass.async_block_till_done()
    assert set(_ours(hass)) == {CONNECTED} and _device(hass, KAMAR) is None


async def test_a_changed_description_is_applied_with_one_warning(hass, app, caplog):
    await app.publish({KAMAR: room()})
    await hass.async_block_till_done()
    caplog.clear()
    fahrenheit = temperature(value=70.5, unit="°F", state_class=None)
    for _ in range(2):
        await app.publish({KAMAR: room(forecast_1h=fahrenheit)})
        fahrenheit = {**fahrenheit, "unit": "K", "value": 294.0}
    await hass.async_block_till_done()
    state = hass.states.get(FORECAST)
    assert state.attributes["unit_of_measurement"] == "K" and "state_class" not in state.attributes
    assert float(state.state) == 294.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "changed device class" in r.getMessage()]
    assert len(warnings) == 1 and FORECAST in warnings[0].getMessage()


async def test_a_deleted_orphan_device_gets_entities_again(hass, app):
    await app.publish({KAMAR: room(), STOVA: room("Stova")})
    await app.publish({KAMAR: room()})  # Stova is now an orphan
    await hass.async_block_till_done()
    device = _device(hass, STOVA)
    from custom_components.smart_predictive_climate import async_remove_config_entry_device

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    # What Home Assistant does when an administrator deletes the device: ask the integration, then remove it.
    assert await async_remove_config_entry_device(hass, entry, device)
    dr.async_get(hass).async_remove_device(device.id)
    await hass.async_block_till_done()
    assert _device(hass, STOVA) is None
    await app.publish({KAMAR: room(), STOVA: room("Stova")})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.spc_stova_forecast_1h").state == "21.4"
    app_device = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, "app")})[0]
    assert not await async_remove_config_entry_device(hass, entry, app_device)  # the App device stays
    assert not await async_remove_config_entry_device(hass, entry, _device(hass, KAMAR))  # a live room too


async def test_a_room_called_app_does_not_collide(hass, app):
    await app.publish({"app": room("App", {"connected": binary("Connected", False)})})
    await hass.async_block_till_done()
    assert _device(hass, "app").name == "App"
    assert dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, "app")})[0].name == NAME
    ours = _ours(hass)
    assert ours[CONNECTED].unique_id == "app-connected" and hass.states.get(CONNECTED).state == "on"
    assert ours["binary_sensor.spc_app_connected_2"].unique_id == "app_connected"
    assert hass.states.get("binary_sensor.spc_app_connected_2").state == "off"


async def test_coexists_with_the_occupancy_detection_companion(hass, ws):
    """Smart Occupancy Detection's integration and this one in one Home Assistant: no shared identity."""
    from custom_components.smart_occupancy_detection.const import DOMAIN as SOD_DOMAIN

    sod_entry = MockConfigEntry(domain=SOD_DOMAIN, title="Smart Occupancy Detection", data={}, unique_id=SOD_DOMAIN)
    sod_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sod_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    # An adversarial case: an entity of the other integration already uses the id this one would suggest.
    taken = registry.async_get_or_create("sensor", SOD_DOMAIN, f"{KAMAR}_forecast_1h",
                                         suggested_object_id="spc_kamar_sudur_forecast_1h", config_entry=sod_entry)
    theirs_before = {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == SOD_DOMAIN}
    assert "binary_sensor.sod_app_connected" in theirs_before
    await setup_spc(hass)
    a = App(await ws())
    await a.open()
    await a.pair()
    await a.claim()
    await a.publish({KAMAR: room()})
    await hass.async_block_till_done()
    ours = {e: r.unique_id for e, r in _ours(hass).items()}
    assert taken.entity_id == FORECAST and registry.async_get(FORECAST).platform == SOD_DOMAIN
    assert ours["sensor.spc_kamar_sudur_forecast_1h_2"] == f"{KAMAR}_forecast_1h"  # ours got another id
    assert not set(ours) & set(theirs_before)
    assert {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == SOD_DOMAIN} == theirs_before
    assert hass.config_entries.async_get_entry(sod_entry.entry_id).state.value == "loaded"
    # Each integration answers only its own commands, with its own pairing and epoch.
    theirs = await a.ws.raw(f"{SOD_DOMAIN}/status")
    assert theirs["success"] and theirs["result"]["paired"] is False and theirs["result"]["epoch"] == 0
    mine = await a.ws.call("status")
    assert mine["paired"] is True and mine["epoch"] == 1
    assert hass.states.get("binary_sensor.sod_app_connected").state == "off"
    assert hass.states.get(CONNECTED).state == "on"
    # Deleting a room here never touches the other integration's entities.
    await a.publish({}, deleted=[KAMAR])
    await hass.async_block_till_done()
    assert {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == SOD_DOMAIN} == theirs_before
    assert set(_ours(hass)) == {CONNECTED}


async def test_diagnostics_are_redacted(hass, app):
    from custom_components.smart_predictive_climate.diagnostics import async_get_config_entry_diagnostics

    await app.publish({KAMAR: room()})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    data = await async_get_config_entry_diagnostics(hass, entry)
    text = str(data)
    assert app.token not in text and app.lease not in text and app.instance_id[:8] not in text
    assert app.session_id[:8] not in text
    assert data["status"]["paired_by"] == "**REDACTED**"
    assert data["status"]["sessions"][0]["instance"] == "**REDACTED**"
    assert data["views"][KAMAR]["entities"]["forecast_1h"]["value"] == 21.4


async def test_config_flow_allows_one_instance(hass):
    from homeassistant.config_entries import SOURCE_USER

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "form"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry" and result["title"] == NAME
    await hass.async_block_till_done()
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == "abort"
