"""Setup, entry migration and entity identity of the companion."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smart_occupancy.const import DOMAIN, ENTRY_MINOR_VERSION
from custom_components.smart_occupancy.entity import expected_unique_ids

from .conftest import observation, KAMAR, STOVA, FakeApp, legacy_options, occupancy_view, setup_companion, system_view

# Every entity the monolithic 0.9.0 integration created for the legacy_options() home (platform, unique id,
# entity id) - the identities the companion must keep.
LEGACY_ENTITIES = [
    ("sensor", "smart_occupancy_status", "sensor.smart_occupancy_status"),
    ("sensor", "smart_occupancy_operating_mode", "sensor.smart_occupancy_operating_mode"),
    ("sensor", "smart_occupancy_effective_mood", "sensor.smart_occupancy_effective_mood"),
    ("sensor", "smart_occupancy_configured_rooms", "sensor.smart_occupancy_configured_rooms"),
    ("sensor", "smart_occupancy_light_categories", "sensor.smart_occupancy_light_categories"),
    ("sensor", "smart_occupancy_discovered_sensors", "sensor.smart_occupancy_discovered_sensors"),
    ("sensor", "smart_occupancy_missing_entities", "sensor.smart_occupancy_missing_entities"),
    ("sensor", "smart_occupancy_unavailable_entities", "sensor.smart_occupancy_unavailable_entities"),
    ("sensor", "smart_occupancy_config_problems", "sensor.smart_occupancy_config_problems"),
    ("sensor", "smart_occupancy_predicted_next_room", "sensor.smart_occupancy_predicted_next_room"),
    ("sensor", "smart_occupancy_forecast_learning", "sensor.smart_occupancy_forecast_learning"),
    ("binary_sensor", "smart_occupancy_mood_mismatch", "binary_sensor.smart_occupancy_mood_mismatch"),
    ("button", "smart_occupancy_recheck", "button.smart_occupancy_re_check_diagnostics"),
    ("select", "smart_occupancy_mood", "select.smart_lighting_mood"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_observation", "sensor.smart_occupancy_kamar_nordur_observation"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_state", "sensor.smart_occupancy_kamar_nordur_state"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_confidence", "sensor.smart_occupancy_kamar_nordur_confidence"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_last_activity", "sensor.smart_occupancy_kamar_nordur_last_activity"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_reason", "sensor.smart_occupancy_kamar_nordur_reason"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_helper_comparison", "sensor.smart_occupancy_kamar_nordur_helper_comparison"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_mood_lighting", "sensor.smart_occupancy_kamar_nordur_mood_lighting"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_daylight", "sensor.smart_occupancy_kamar_nordur_daylight"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_forecast", "sensor.smart_occupancy_kamar_nordur_forecast"),
    ("sensor", f"smart_occupancy_room_{KAMAR}_forecast_confidence", "sensor.smart_occupancy_kamar_nordur_forecast_confidence"),
    ("binary_sensor", f"smart_occupancy_room_{KAMAR}_occupied", "binary_sensor.smart_occupancy_kamar_nordur_occupied"),
    ("binary_sensor", f"smart_occupancy_room_{KAMAR}_predicted", "binary_sensor.smart_occupancy_kamar_nordur_predicted"),
]


def _register_legacy(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    reg = er.async_get(hass)
    for platform, unique_id, eid in LEGACY_ENTITIES:
        created = reg.async_get_or_create(platform, DOMAIN, unique_id, config_entry=entry,
                                          suggested_object_id=eid.split(".", 1)[1])
        assert created.entity_id == eid, (created.entity_id, eid)


async def test_fresh_setup_creates_the_global_entities_only(hass: HomeAssistant) -> None:
    entry = await setup_companion(hass)
    assert entry.minor_version == ENTRY_MINOR_VERSION
    reg = er.async_get(hass)
    ours = {e.unique_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    assert ours == expected_unique_ids({}, False)
    assert hass.states.get("sensor.smart_occupancy_lighting_status").state == "unavailable"  # nothing published yet
    assert hass.states.get("button.smart_occupancy_lighting_re_check_diagnostics") is not None
    assert "select.smart_lighting_mood" not in ours and hass.states.get("select.smart_lighting_mood") is None


async def test_legacy_entry_migrates_and_keeps_its_options_read_only(hass: HomeAssistant, areas) -> None:
    options = legacy_options()
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", data={}, options=options, version=1,
                            minor_version=2, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    _register_legacy(hass, entry)
    await setup_companion(hass, entry=entry)
    assert entry.minor_version == ENTRY_MINOR_VERSION
    assert dict(entry.options) == options  # untouched until the App's migration retires them
    reg = er.async_get(hass)
    # Nothing was published yet: every registered entity is kept (an upgrade never drops entities).
    for platform, unique_id, eid in LEGACY_ENTITIES:
        assert reg.async_get_entity_id(platform, DOMAIN, unique_id) == eid
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state") is None  # not created until the App publishes


async def test_entities_keep_their_identity_after_the_upgrade(hass: HomeAssistant, areas, hass_ws_client) -> None:
    """The App publishes the rooms: every room entity reappears on exactly its 0.9.0 entity id and unique id."""
    entry = MockConfigEntry(domain=DOMAIN, title="Smart Occupancy & Lighting", data={}, options=legacy_options(),
                            version=1, minor_version=2, unique_id=DOMAIN)
    entry.add_to_hass(hass)
    _register_legacy(hass, entry)
    await setup_companion(hass, entry=entry)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy", "lighting", "forecast")
    assert (await app.publish("system", system_view()))["success"]
    assert (await app.publish("occupancy", occupancy_view()))["success"]
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    for platform, unique_id, eid in LEGACY_ENTITIES:
        assert reg.async_get_entity_id(platform, DOMAIN, unique_id) == eid, (platform, unique_id)
        state = hass.states.get(eid)
        assert state is not None, eid
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_state").state == "confirmed_occupancy"
    assert hass.states.get("binary_sensor.smart_occupancy_kamar_nordur_occupied").state == "on"
    assert hass.states.get("select.smart_lighting_mood").attributes["options"] == ["Dagur", "Kvøld", "Manual"]
    ours = {e.unique_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)}
    assert ours == {u for _p, u, _e in LEGACY_ENTITIES}
    device = reg.async_get("sensor.smart_occupancy_status").device_id
    assert device and all(reg.async_get(e).device_id == device for _p, _u, e in LEGACY_ENTITIES)


async def test_rooms_follow_the_published_system_view(hass: HomeAssistant, areas, hass_ws_client) -> None:
    entry = await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("occupancy")
    assert (await app.publish("system", system_view({KAMAR: {"name": "Kamar Norður", "reference": None},
                                                       STOVA: {"name": "Stova", "reference": None}})))["success"]
    await hass.async_block_till_done()
    assert observation(hass, STOVA).state == "healthy"
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_helper_comparison") is None  # no reference entity
    # The reference helper appears: so does its comparison sensor. Stova disappears: its entities are removed.
    assert (await app.publish("system", system_view({KAMAR: {"name": "Kamar Norður",
                                                              "reference": "input_boolean.kamar_helper"}})))["success"]
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smart_occupancy_kamar_nordur_helper_comparison") is not None
    assert observation(hass, STOVA) is None
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"smart_occupancy_room_{STOVA}_state") is None
    # The selector follows the mood source.
    assert hass.states.get("select.smart_lighting_mood") is not None
    assert (await app.publish("system", system_view(mood_select=False)))["success"]
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert hass.states.get("select.smart_lighting_mood") is None
    # A restart recreates the published rooms before the App is back (from the companion's store).
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert observation(hass, KAMAR).state == "healthy"
    assert hass.states.get("sensor.smart_occupancy_lighting_status").state == "healthy"


async def test_the_mood_selector_keeps_its_choice_across_restarts(hass: HomeAssistant, areas, hass_ws_client) -> None:
    entry = await setup_companion(hass)
    app = FakeApp(hass, await hass_ws_client(hass))
    await app.start("lighting")
    assert (await app.publish("system", system_view()))["success"]
    await hass.async_block_till_done()
    await hass.services.async_call("select", "select_option", {"entity_id": "select.smart_lighting_mood",
                                                               "option": "Kvøld"}, blocking=True)
    assert hass.states.get("select.smart_lighting_mood").state == "Kvøld"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("select.smart_lighting_mood").state == "Kvøld"
