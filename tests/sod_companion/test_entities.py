"""The native entities: identity, states, freshness, outages, restarts, renames, deletion, coexistence."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy_detection.const import DOMAIN

from .conftest import KAMAR, STOVA, App, setup_sod, view

def _device(hass, area_id):
    found = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, f"room_{area_id}")})
    return found[0] if found else None


IDS = {
    "binary_sensor.sod_kamar_sudur_occupied": f"{KAMAR}_occupied",
    "sensor.sod_kamar_sudur_state": f"{KAMAR}_state",
    "sensor.sod_kamar_sudur_confidence": f"{KAMAR}_confidence",
    "sensor.sod_kamar_sudur_reason": f"{KAMAR}_reason",
    "sensor.sod_kamar_sudur_last_activity": f"{KAMAR}_last_activity",
}


async def test_room_entities(hass: HomeAssistant, app):
    await app.publish({KAMAR: view()})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    for entity_id, unique_id in IDS.items():
        entry = registry.async_get(entity_id)
        assert entry is not None and entry.unique_id == unique_id and entry.platform == DOMAIN, entity_id
    occupied = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert occupied.state == "on" and occupied.attributes["device_class"] == "occupancy"
    assert set(occupied.attributes) == {"state", "fresh", "device_class", "friendly_name"}
    assert occupied.attributes["friendly_name"] == "Kamar Suður Occupancy"
    assert occupied.attributes["fresh"] is True and occupied.attributes["state"] == "confirmed_occupancy"
    assert hass.states.get("sensor.sod_kamar_sudur_state").state == "confirmed_occupancy"
    assert hass.states.get("sensor.sod_kamar_sudur_state").attributes["options"][0] == "vacant"
    confidence = hass.states.get("sensor.sod_kamar_sudur_confidence")
    assert confidence.state == "80" and confidence.attributes["unit_of_measurement"] == "%"
    assert "state_class" not in confidence.attributes  # no long-term statistics
    assert hass.states.get("sensor.sod_kamar_sudur_reason").state == "Occupied: PIR active."
    assert hass.states.get("sensor.sod_kamar_sudur_last_activity").state == "2026-11-21T11:59:00+00:00"
    assert hass.states.get("binary_sensor.sod_app_connected").state == "on"
    device = _device(hass, KAMAR)
    assert device.name == "Kamar Suður" and device.area_id is None  # never placed into the area automatically


async def test_states_are_written_only_on_change(hass, app):
    await app.publish({KAMAR: view()})
    await hass.async_block_till_done()
    before = hass.states.get("binary_sensor.sod_kamar_sudur_occupied").last_updated
    await app.publish({KAMAR: view()})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").last_updated == before


async def test_door_only_and_uncertain(hass, app):
    await app.publish({KAMAR: view(state="possible_occupancy", occupied=False, reason_code="door_only")})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"
    await app.publish({KAMAR: view(state="uncertain", occupied=None, reason_code="all_signals_unknown")})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "unknown"  # never "off"
    await app.publish({KAMAR: view(state="uncertain", occupied=True, reason_code="all_signals_unknown")})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"


async def test_an_absent_app_never_makes_a_room_vacant(hass, app):
    await app.publish({KAMAR: view()})
    # The App stops sending (crash, update): the lease runs out.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=32))
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert state.state == "on" and state.attributes["fresh"] is False
    assert hass.states.get("binary_sensor.sod_app_connected").state == "off"
    assert hass.states.get("sensor.sod_kamar_sudur_reason").state.startswith("The App is not connected")
    # Its next message is refused: it must claim again (a new epoch).
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}) == "not_owner"
    epoch = app.epoch
    await app.claim()
    assert app.epoch == epoch + 1
    await app.publish({KAMAR: view(state="vacant", occupied=False)})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"
    assert hass.states.get("binary_sensor.sod_app_connected").state == "on"


async def test_a_closed_connection_ends_the_session(hass, app):
    await app.publish({KAMAR: view()})
    await app.ws.close()
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert state.state == "on" and state.attributes["fresh"] is False


async def test_restart_keeps_the_last_decision_and_refuses_old_leases(hass, app, ws):
    await app.publish({KAMAR: view()})
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert state.state == "on" and state.attributes["fresh"] is False  # restored, not invented, not vacant
    # A replay of a message from before the restart is refused (the session and the lease are gone).
    assert await app.publish_refused({KAMAR: view(state="vacant", occupied=False)}) == "unknown_session"
    again = App(await ws(), token=app.token)
    assert (await again.open())["trusted"]
    result = await again.claim()
    assert result["epoch"] == app.epoch + 1  # the epoch survived the restart
    await again.publish({KAMAR: view(state="vacant", occupied=False)})
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "off"


async def test_rename_keeps_entity_ids(hass, app):
    await app.publish({KAMAR: view()})
    await app.publish({KAMAR: view(name="Kamar Norður")})
    await hass.async_block_till_done()
    device = _device(hass, KAMAR)
    assert device.name == "Kamar Norður"
    state = hass.states.get("binary_sensor.sod_kamar_sudur_occupied")
    assert state is not None and state.attributes["friendly_name"] == "Kamar Norður Occupancy"
    assert len([e for e in er.async_entries_for_config_entry(er.async_get(hass), device.config_entry_id)
                if e.unique_id.startswith(KAMAR)]) == 5


async def test_disabled_deleted_and_empty(hass, app):
    await app.publish({KAMAR: view(), STOVA: view(name="Stova")})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_stova_occupied").state == "on"
    # Detection turned off: the entities stay, unavailable.
    await app.publish({KAMAR: view()}, disabled={STOVA: "Stova"})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_stova_occupied").state == "unavailable"
    # Missing from a snapshot (a mistake, a reset App): kept, unavailable.
    await app.publish({KAMAR: view()})
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    assert hass.states.get("binary_sensor.sod_stova_occupied").state == "unavailable"
    # Deleted by an administrator in the App: its entities and device go.
    await app.ws.call("publish", **app.fence(), rooms={KAMAR: view()}, disabled={}, deleted=[STOVA])
    await hass.async_block_till_done()
    assert registry.async_get("binary_sensor.sod_stova_occupied") is None
    assert _device(hass, STOVA) is None
    # An App without any room (fresh or reset) never deletes entities: they stay, unavailable.
    await app.publish({})
    await hass.async_block_till_done()
    assert registry.async_get("binary_sensor.sod_kamar_sudur_occupied") is not None
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "unavailable"
    await app.publish({KAMAR: view()})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").state == "on"


async def test_recorder_excluded_attributes(hass, app):
    from custom_components.smart_occupancy_detection.sensor import RoomStateSensor

    assert {"vacancy_at", "sensor_problems", "reason_code", "starting_up"} <= RoomStateSensor._unrecorded_attributes
    await app.publish({KAMAR: view(vacancy_at="2026-11-21T12:10:00+00:00")})
    state = hass.states.get("sensor.sod_kamar_sudur_state")
    assert state.attributes["vacancy_at"] == "2026-11-21T12:10:00+00:00"


async def test_a_claim_is_not_fresh_until_it_publishes(hass, app):
    await app.publish({KAMAR: view()})
    await app.claim()  # a new epoch: the old views are not this epoch's
    assert hass.states.get("binary_sensor.sod_kamar_sudur_occupied").attributes["fresh"] is False
    assert hass.states.get("binary_sensor.sod_app_connected").state == "off"
    await app.publish({KAMAR: view()})
    assert hass.states.get("binary_sensor.sod_app_connected").state == "on"


async def test_a_deleted_orphan_device_gets_entities_again(hass, app):
    await app.publish({KAMAR: view(), STOVA: view(name="Stova")})
    await app.publish({KAMAR: view()})  # Stova is now an orphan
    await hass.async_block_till_done()
    device = _device(hass, STOVA)
    from custom_components.smart_occupancy_detection import async_remove_config_entry_device

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    # What Home Assistant does when an administrator deletes the device: ask the integration, then remove it.
    assert await async_remove_config_entry_device(hass, entry, device)
    dr.async_get(hass).async_remove_device(device.id)
    await hass.async_block_till_done()
    assert _device(hass, STOVA) is None
    await app.publish({KAMAR: view(), STOVA: view(name="Stova")})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.sod_stova_occupied").state == "on"
    app_device = dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, "app")})[0]
    assert not await async_remove_config_entry_device(hass, entry, app_device)  # the App device stays
    assert not await async_remove_config_entry_device(hass, entry, _device(hass, KAMAR))  # a live room too


async def test_an_area_called_app_does_not_collide(hass, app):
    await app.publish({"app": view(name="App")})
    await hass.async_block_till_done()
    assert _device(hass, "app").name == "App"
    assert dr.async_get(hass).async_get_devices(identifiers={(DOMAIN, "app")})[0].name == "Smart Occupancy Detection"


async def test_coexists_with_smart_comfort_without_taking_its_entities(hass, ws, areas):
    """Smart Comfort's integration (smart_occupancy) and this one in one Home Assistant: no shared identity."""
    from custom_components.smart_occupancy.const import DOMAIN as SC_DOMAIN

    sc_entry = MockConfigEntry(domain=SC_DOMAIN, title="Smart Occupancy & Lighting", data={}, options={},
                               version=1, minor_version=3, unique_id=SC_DOMAIN)
    sc_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(sc_entry.entry_id)
    registry = er.async_get(hass)
    # An adversarial case: a Smart Comfort entity already uses the id this integration would suggest.
    taken = registry.async_get_or_create("binary_sensor", SC_DOMAIN, f"smart_occupancy_room_{KAMAR}_occupied",
                                         suggested_object_id="sod_kamar_sudur_occupied", config_entry=sc_entry)
    sc_entities_before = {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == SC_DOMAIN}
    await setup_sod(hass)
    a = App(await ws())
    await a.open()
    await a.pair()
    await a.claim()
    await a.publish({KAMAR: view()})
    await hass.async_block_till_done()
    ours = {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == DOMAIN}
    assert taken.entity_id == "binary_sensor.sod_kamar_sudur_occupied" and taken.platform == SC_DOMAIN
    assert "binary_sensor.sod_kamar_sudur_occupied_2" in ours  # ours got another id; theirs is untouched
    assert not set(ours) & set(sc_entities_before)
    assert {e.entity_id: e.unique_id for e in registry.entities.values() if e.platform == SC_DOMAIN} == \
        sc_entities_before
    assert hass.config_entries.async_get_entry(sc_entry.entry_id).state.value == "loaded"
    # Smart Comfort's bridge commands stay its own; this integration answers only its domain.
    msg = await a.ws.raw("smart_occupancy/companion/status")
    assert msg["success"] and "occupancy" in str(msg["result"])
