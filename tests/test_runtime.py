"""Runtime (Observe mode): entities, mood observation, missing entities, restart."""

from __future__ import annotations

from datetime import timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er, label_registry as lr
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import DOMAIN
from custom_components.smart_occupancy.diagnostics import async_get_config_entry_diagnostics

from .conftest import Home, base_options, set_moods, setup_entry


def eid(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id, unique_id
    return entity_id


def state(hass: HomeAssistant, platform: str, key: str):
    return hass.states.get(eid(hass, platform, f"{DOMAIN}_{key}"))


async def test_entities_created_with_stable_unique_ids(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))
    ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert ids == {
        f"{DOMAIN}_{k}" for k in (
            "status", "operating_mode", "effective_mood", "configured_rooms", "light_categories",
            "discovered_sensors", "missing_entities", "unavailable_entities", "config_problems",
            "mood_mismatch",
        )
    } | {f"{DOMAIN}_room_{home.pilot_area}_observation"}
    assert state(hass, "sensor", "operating_mode").state == "observe"
    room = hass.states.get(eid(hass, "sensor", f"{DOMAIN}_room_{home.pilot_area}_observation"))
    assert room.state == "ok"
    assert room.name.endswith("Kamar Norður observation")


async def test_mood_follows_auto(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    mood = state(hass, "sensor", "effective_mood")
    assert mood.state == "Dagur"
    assert mood.attributes["follows"] == "auto"
    assert mood.attributes["consistent"] is True
    assert state(hass, "binary_sensor", "mood_mismatch").state == "off"


async def test_mood_manual_override(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    set_moods(hass, "Dagur", "Nátt", "Nátt")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    mood = state(hass, "sensor", "effective_mood")
    assert mood.state == "Nátt"
    assert mood.attributes["follows"] == "manual"
    assert mood.attributes["expected_main_mood"] == "Nátt"
    assert mood.attributes["consistent"] is True


async def test_mood_mismatch_after_grace_period(hass: HomeAssistant, home: Home, freezer) -> None:
    await setup_entry(hass, base_options(home))
    # Auto mood changes; existing automation has not copied it to Main Mood yet.
    set_moods(hass, "Kvøld", "Auto", "Dagur")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state(hass, "sensor", "effective_mood").attributes["consistent"] is False
    # Within the grace period no problem is reported.
    assert state(hass, "binary_sensor", "mood_mismatch").state == "off"

    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", "mood_mismatch").state == "on"
    assert state(hass, "sensor", "status").state == "warning"

    # Existing automation catches up -> mismatch clears.
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=2))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert state(hass, "binary_sensor", "mood_mismatch").state == "off"


async def test_mood_helper_unavailable(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    hass.states.async_set("input_select.mood", STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    # Cannot judge -> unknown, never a false "problem".
    assert state(hass, "binary_sensor", "mood_mismatch").state == "unknown"
    assert "input_select.mood" in state(hass, "sensor", "unavailable_entities").attributes["entities"]


async def test_light_categories_from_labels(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    cats = state(hass, "sensor", "light_categories")
    assert cats.state == "3"
    # Entity label (group) and device label (lamp) both classify.
    assert cats.attributes["categories"]["Downlights"] == {"label": "Downlights", "lights": 1}
    assert cats.attributes["categories"]["Lamps"] == {"label": "Lamps", "lights": 1}
    assert cats.attributes["categories"]["LED strips"] == {"label": None, "lights": 0}


async def test_label_change_reclassifies(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    er.async_get(hass).async_update_entity(home.member_a, labels={home.lamp_label})
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert state(hass, "sensor", "light_categories").attributes["categories"]["Lamps"]["lights"] == 2


async def test_deleted_label_reported(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home))
    lr.async_get(hass).async_delete(home.lamp_label)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    codes = [p["code"] for p in state(hass, "sensor", "config_problems").attributes["problems"]]
    assert "category_label_missing" in codes


async def test_group_and_member_conflict(hass: HomeAssistant, home: Home) -> None:
    await setup_entry(hass, base_options(home, lights=[home.group, home.member_a]))
    problems = state(hass, "sensor", "config_problems").attributes["problems"]
    conflict = [p for p in problems if p["code"] == "group_member_conflict"]
    assert len(conflict) == 1 and conflict[0]["entity_id"] == home.member_a
    assert state(hass, "sensor", "status").state == "warning"


async def test_missing_and_unavailable_entities(hass: HomeAssistant, home: Home, service_calls) -> None:
    opts = base_options(home)
    opts["rooms"][home.pilot_area]["sensors"].append(
        {"entity_id": "binary_sensor.removed_sensor", "kind": "pir", "role": "trigger"}
    )
    await setup_entry(hass, opts)
    room_id = eid(hass, "sensor", f"{DOMAIN}_room_{home.pilot_area}_observation")
    assert state(hass, "sensor", "missing_entities").attributes["entities"] == ["binary_sensor.removed_sensor"]
    assert hass.states.get(room_id).state == "degraded"

    # A configured sensor going unavailable is handled safely.
    hass.states.async_set(home.radar, STATE_UNAVAILABLE)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    assert home.radar in state(hass, "sensor", "unavailable_entities").attributes["entities"]
    signals = {s["entity_id"]: s["health"] for s in hass.states.get(room_id).attributes["signals"]}
    assert signals[home.radar] == "unavailable"
    assert signals["binary_sensor.removed_sensor"] == "missing"
    assert service_calls == []


async def test_registered_but_not_loaded_is_not_missing(hass: HomeAssistant, home: Home) -> None:
    hass.states.async_remove(home.pir)  # integration not loaded yet
    await setup_entry(hass, base_options(home))
    assert state(hass, "sensor", "missing_entities").state == "0"
    assert home.pir in state(hass, "sensor", "unavailable_entities").attributes["entities"]


async def test_entity_rename_updates_configuration(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))
    er.async_get(hass).async_update_entity(home.radar, new_entity_id="binary_sensor.renamed_radar")
    await hass.async_block_till_done()
    sensors = [s["entity_id"] for s in entry.options["rooms"][home.pilot_area]["sensors"]]
    assert "binary_sensor.renamed_radar" in sensors and home.radar not in sensors
    assert entry.state is ConfigEntryState.LOADED


async def test_persistence_across_restart(hass: HomeAssistant, home: Home) -> None:
    opts = base_options(home)
    opts["global_profiles"] = {"Kvøld": {"downlights": {"action": "on", "brightness_pct": 75}}}
    entry = await setup_entry(hass, opts)
    before = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.options == opts
    after = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert after == before
    assert state(hass, "sensor", "effective_mood").state == "Dagur"


async def test_quiet_during_startup(hass: HomeAssistant, home: Home, caplog) -> None:
    """While HA starts, missing entities are not logged as warnings until started."""
    opts = base_options(home)
    opts["rooms"][home.pilot_area]["lights"] = ["light.not_there_yet"]
    hass.set_state(CoreState.starting)
    caplog.set_level(logging.INFO, logger="custom_components.smart_occupancy")
    await setup_entry(hass, opts)
    assert "problem_detected" not in caplog.text

    await hass.async_start()
    await hass.async_block_till_done()
    assert "observe_summary" in caplog.text
    assert "problem_detected severity=warning code=missing_entity" in caplog.text


async def test_unload_before_and_after_startup(hass: HomeAssistant, home: Home) -> None:
    """Unloading must be clean whether or not HA finished starting."""
    hass.set_state(CoreState.starting)
    entry = await setup_entry(hass, base_options(home))
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_start()
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_disabled_room_entity_removed_only_for_own_entities(hass: HomeAssistant, home: Home) -> None:
    entry = await setup_entry(hass, base_options(home))
    ent_reg = er.async_get(hass)
    before_foreign = {e.entity_id for e in ent_reg.entities.values() if e.platform != DOMAIN}

    opts = base_options(home, enabled=False)
    hass.config_entries.async_update_entry(entry, options=opts)
    await hass.async_block_till_done()
    assert ent_reg.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{home.pilot_area}_observation") is None
    assert {e.entity_id for e in ent_reg.entities.values() if e.platform != DOMAIN} == before_foreign
    assert entry.options["rooms"][home.pilot_area]["sensors"]  # config kept


async def test_area_missing(hass: HomeAssistant, home: Home) -> None:
    opts = base_options(home)
    opts["rooms"]["deleted_area"] = {"enabled": True, "sensors": [], "lights": []}
    await setup_entry(hass, opts)
    room = hass.states.get(eid(hass, "sensor", f"{DOMAIN}_room_deleted_area_observation"))
    assert room.state == "area_missing"


async def test_mode_forced_to_observe(hass: HomeAssistant, home: Home) -> None:
    """Even a hand-edited 'full_control' runs as Observe and is flagged."""
    opts = base_options(home)
    opts["operating_mode"] = "full_control"
    await setup_entry(hass, opts)
    mode = state(hass, "sensor", "operating_mode")
    assert mode.state == "observe"
    assert mode.attributes["configured_mode"] == "full_control"
    assert state(hass, "sensor", "status").state == "error"


async def test_no_service_calls_while_observing(hass: HomeAssistant, home: Home, service_calls) -> None:
    await setup_entry(hass, base_options(home))
    for s in ("on", "off", "on"):
        hass.states.async_set(home.pir, s)
        hass.states.async_set(home.member_a, s)
        set_moods(hass, "Nátt", "Auto", "Dagur")
        await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert service_calls == []


async def test_diagnostics(hass: HomeAssistant, home: Home) -> None:
    opts = base_options(home)
    opts["global_profiles"] = {"Dagur": {"downlights": {"action": "on", "brightness_pct": 100, "color_temp_kelvin": 3500}}}
    opts["rooms"][home.pilot_area]["mood_overrides"] = {"Dagur": {"downlights": {"action": "inherit", "brightness_pct": 60}}}
    entry = await setup_entry(hass, opts)
    hass.states.async_set(home.radar, "on")
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["phase"] == "0.1"
    room = diag["rooms"][home.pilot_area]
    assert room["active_signals_now"] == {"maintain": [home.radar]}
    assert sorted(room["group_members"][home.group]) == sorted([home.member_a, home.member_b])
    preview = room["resolved_profiles_preview"][home.group]
    assert preview["action"] == "on" and preview["brightness_pct"] == 60
    assert preview["sources"] == {"action": "global", "brightness_pct": "room", "color_temp_kelvin": "global"}
    assert any(a["name"] == "Kamar Norður" for a in diag["areas"])


def test_list_attributes_excluded_from_recorder() -> None:
    """Check the set Home Assistant itself hands to the Recorder."""
    from custom_components.smart_occupancy.sensor import SENSORS, RoomObservationSensor, SmartSensor

    combined = SmartSensor._Entity__combined_unrecorded_attributes
    assert {"rooms", "categories", "sensors", "entities", "problems"} <= combined
    assert {"signals", "lights"} <= RoomObservationSensor._Entity__combined_unrecorded_attributes
    # No long-term statistics are generated.
    assert all(d.state_class is None for d in SENSORS)
