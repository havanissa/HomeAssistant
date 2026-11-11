"""Phase 0.1.1: detailed problem reporting and automatic recovery."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.smart_occupancy.const import DOMAIN, PROBLEM_STORE_KEY, STARTUP_GRACE_SECONDS

from .conftest import Home, base_options, setup_entry
from .test_runtime import eid, state


async def settle(hass: HomeAssistant, freezer, seconds: float = 3) -> None:
    """Let debouncers run (event-driven updates are coalesced for ~1-2 s)."""
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def room(hass: HomeAssistant, home: Home):
    return hass.states.get(eid(hass, "sensor", f"{DOMAIN}_room_{home.pilot_area}_observation"))


def summary(hass: HomeAssistant, home: Home) -> dict[str, Any]:
    return {
        "status": state(hass, "sensor", "status").state,
        "room": room(hass, home).state,
        "unavailable": int(state(hass, "sensor", "unavailable_entities").state),
        "missing": int(state(hass, "sensor", "missing_entities").state),
        "config": int(state(hass, "sensor", "config_problems").state),
    }


HEALTHY = {"status": "healthy", "room": "healthy", "unavailable": 0, "missing": 0, "config": 0}


def labelled_options(home: Home, **room: Any) -> dict[str, Any]:
    """Options in which every managed light has a category (no config warnings)."""
    return base_options(home, **room)


async def test_offline_light_detail_and_recovery(hass: HomeAssistant, home: Home, freezer, service_calls) -> None:
    await setup_entry(hass, labelled_options(home))
    assert summary(hass, home) == HEALTHY

    hass.states.async_set(home.group, STATE_UNAVAILABLE, {"friendly_name": "North spots"})
    await settle(hass, freezer)

    # An offline optional light is an operational warning, not a configuration error.
    assert summary(hass, home) == {**HEALTHY, "status": "warning", "room": "warning", "unavailable": 1}
    unavailable = state(hass, "sensor", "unavailable_entities").attributes
    assert unavailable["items"] == [f"{home.group} (North spots) — Kamar Norður — Light unavailable"]
    item = unavailable["entities"][0]
    assert item["entity_id"] == home.group
    assert item["name"] == "North spots"
    assert item["rooms"] == ["Kamar Norður"]
    assert item["type"] == "entity_unavailable"
    assert item["classification"] == "unavailable"
    assert item["category"] == "operational"
    assert item["severity"] == "warning"
    assert "remains usable" in item["explanation"]
    assert "power" in item["action"]
    assert item["first_detected"] and item["last_checked"]

    attrs = room(hass, home).attributes
    assert attrs["usable"] is True
    assert attrs["configuration_valid"] is True
    assert attrs["operating_mode"] == "observe"
    assert [l["entity_id"] for l in attrs["lights_unavailable"]] == [home.group]
    assert attrs["lights_available"] == []
    assert {s["entity_id"] for s in attrs["sensors_available"]} == {home.radar, home.pir, home.door}
    assert attrs["reasons"] == [f"Light unavailable: {home.group} (North spots)"]
    assert state(hass, "sensor", "status").attributes["summary"] == "1 unavailable."

    # Light comes back -> everything clears by itself.
    hass.states.async_set(home.group, "on", {"entity_id": [home.member_a, home.member_b]})
    await settle(hass, freezer)
    assert summary(hass, home) == HEALTHY
    assert state(hass, "sensor", "unavailable_entities").attributes["items"] == []
    assert room(hass, home).attributes["reasons"] == []
    assert service_calls == []


async def test_offline_sensor_recovery_and_unobservable_room(hass: HomeAssistant, home: Home, freezer) -> None:
    await setup_entry(hass, labelled_options(home))

    hass.states.async_set(home.radar, STATE_UNAVAILABLE)
    await settle(hass, freezer)
    assert summary(hass, home) == {**HEALTHY, "status": "warning", "room": "warning", "unavailable": 1}
    item = state(hass, "sensor", "unavailable_entities").attributes["entities"][0]
    assert item["title"] == "Sensor unavailable" and "battery" in item["action"]
    assert room(hass, home).attributes["usable"] is True

    # All Trigger/Maintain signals offline -> room cannot be observed.
    hass.states.async_set(home.pir, STATE_UNAVAILABLE)
    hass.states.async_set(home.door, STATE_UNAVAILABLE)
    await settle(hass, freezer)
    assert summary(hass, home) == {**HEALTHY, "status": "unavailable", "room": "unavailable", "unavailable": 3}
    attrs = room(hass, home).attributes
    assert attrs["usable"] is False
    assert "Room cannot be observed" in attrs["reasons"]

    # One sensor back -> usable again, still a warning for the others.
    hass.states.async_set(home.pir, "off")
    await settle(hass, freezer)
    assert summary(hass, home)["room"] == "warning"
    assert room(hass, home).attributes["usable"] is True

    hass.states.async_set(home.radar, "on")
    hass.states.async_set(home.door, "off")
    await settle(hass, freezer)
    assert summary(hass, home) == HEALTHY


async def test_deleted_entity_is_config_error_and_clears_when_removed(hass: HomeAssistant, home: Home, freezer) -> None:
    entry = await setup_entry(hass, labelled_options(home))
    er.async_get(hass).async_remove(home.door)
    hass.states.async_remove(home.door)
    await settle(hass, freezer)

    assert summary(hass, home) == {**HEALTHY, "status": "config_error", "room": "config_error", "missing": 1, "config": 1}
    missing = state(hass, "sensor", "missing_entities").attributes["entities"][0]
    assert missing["entity_id"] == home.door
    assert missing["classification"] == "config_error"
    assert "Configure → Rooms → Kamar Norður" in missing["action"]
    assert room(hass, home).attributes["configuration_valid"] is False
    # The room still observes with its remaining sensors.
    assert room(hass, home).attributes["usable"] is True

    # Fix the configuration (room configuration change) -> clears.
    opts = labelled_options(home)
    opts["rooms"][home.pilot_area]["sensors"] = [
        s for s in opts["rooms"][home.pilot_area]["sensors"] if s["entity_id"] != home.door
    ]
    hass.config_entries.async_update_entry(entry, options=opts)
    await hass.async_block_till_done()
    assert summary(hass, home) == HEALTHY


async def test_renamed_entity(hass: HomeAssistant, home: Home, freezer) -> None:
    entry = await setup_entry(hass, labelled_options(home))
    er.async_get(hass).async_update_entity(home.radar, new_entity_id="binary_sensor.radar_renamed")
    hass.states.async_set("binary_sensor.radar_renamed", "off")
    await settle(hass, freezer)
    assert "binary_sensor.radar_renamed" in [
        s["entity_id"] for s in entry.options["rooms"][home.pilot_area]["sensors"]
    ]
    assert summary(hass, home) == HEALTHY


async def test_renamed_while_not_running_is_reported_with_fix(hass: HomeAssistant, home: Home) -> None:
    opts = labelled_options(home)
    opts["rooms"][home.pilot_area]["sensors"][0]["entity_id"] = "binary_sensor.old_radar_name"
    await setup_entry(hass, opts)
    missing = state(hass, "sensor", "missing_entities").attributes["entities"]
    assert [m["entity_id"] for m in missing] == ["binary_sensor.old_radar_name"]
    assert "renamed" in missing[0]["explanation"]


async def test_disabled_and_orphaned_entities_are_explained(hass: HomeAssistant, home: Home) -> None:
    er.async_get(hass).async_update_entity(home.door, disabled_by=er.RegistryEntryDisabler.USER)
    hass.states.async_remove(home.door)
    hass.states.async_set(home.radar, STATE_UNAVAILABLE, {"restored": True})
    hass.states.async_set(home.pir, STATE_UNKNOWN)
    await setup_entry(hass, labelled_options(home))

    missing = {m["entity_id"]: m for m in state(hass, "sensor", "missing_entities").attributes["entities"]}
    assert missing[home.door]["type"] == "entity_disabled"
    unavailable = {u["entity_id"]: u for u in state(hass, "sensor", "unavailable_entities").attributes["entities"]}
    assert unavailable[home.radar]["type"] == "entity_not_provided"
    assert "does not clear by itself" in unavailable[home.radar]["explanation"]
    assert unavailable[home.pir]["type"] == "entity_unknown"


async def test_invalid_room_configuration(hass: HomeAssistant, home: Home) -> None:
    opts = labelled_options(home, lights=[home.group, "switch.not_a_light"])
    opts["rooms"][home.other_area] = {"enabled": True, "sensors": [], "lights": [home.lamp]}
    opts["rooms"]["deleted_area"] = {"enabled": True, "sensors": [], "lights": []}
    await setup_entry(hass, opts)

    problems = state(hass, "sensor", "config_problems").attributes["problems"]
    by_type = {(p["type"], p.get("area_id")) for p in problems}
    assert ("not_a_light", home.pilot_area) in by_type
    assert ("area_missing", "deleted_area") in by_type
    assert ("room_without_signals", home.other_area) in by_type
    for p in problems:
        assert p["explanation"] and p["action"] and p["first_detected"] and p["last_checked"]

    rooms = state(hass, "sensor", "configured_rooms").attributes["rooms"]
    assert rooms["Kamar Norður"] == "config_error"
    assert rooms["deleted_area"] == "config_error"
    # A room with nothing to observe is a warning, not a configuration error.
    assert rooms["Stova"] == "warning"
    assert state(hass, "sensor", "status").state == "config_error"


async def test_multiple_problems_clear_independently(hass: HomeAssistant, home: Home, freezer) -> None:
    opts = labelled_options(home, lights=[home.group, home.lamp])
    opts["rooms"][home.pilot_area]["sensors"].append(
        {"entity_id": "binary_sensor.gone", "kind": "pir", "role": "trigger"}
    )
    entry = await setup_entry(hass, opts)
    hass.states.async_set(home.group, STATE_UNAVAILABLE)
    hass.states.async_set(home.radar, STATE_UNAVAILABLE)
    await settle(hass, freezer)

    s = summary(hass, home)
    assert s == {"status": "config_error", "room": "config_error", "unavailable": 2, "missing": 1, "config": 1}
    assert state(hass, "sensor", "status").attributes["summary"] == "1 configuration error, 2 unavailable."

    hass.states.async_set(home.group, "off", {"entity_id": [home.member_a, home.member_b]})
    await settle(hass, freezer)
    assert summary(hass, home)["unavailable"] == 1
    assert [i["entity_id"] for i in state(hass, "sensor", "unavailable_entities").attributes["entities"]] == [home.radar]

    hass.states.async_set(home.radar, "off")
    await settle(hass, freezer)
    assert summary(hass, home) == {**HEALTHY, "status": "config_error", "room": "config_error", "missing": 1, "config": 1}

    fixed = copy.deepcopy(opts)  # entry.options references `opts`; change a copy
    fixed["rooms"][home.pilot_area]["sensors"].pop()
    hass.config_entries.async_update_entry(entry, options=fixed)
    await hass.async_block_till_done()
    assert summary(hass, home) == HEALTHY


async def test_reload_keeps_first_detected_and_drops_resolved(hass: HomeAssistant, home: Home, freezer) -> None:
    entry = await setup_entry(hass, labelled_options(home))
    hass.states.async_set(home.group, STATE_UNAVAILABLE)
    hass.states.async_set(home.radar, STATE_UNAVAILABLE)
    await settle(hass, freezer)
    first = {i["entity_id"]: i["first_detected"] for i in state(hass, "sensor", "unavailable_entities").attributes["entities"]}

    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(home.radar, "off")  # resolved while we reload
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    items = state(hass, "sensor", "unavailable_entities").attributes["entities"]
    assert [i["entity_id"] for i in items] == [home.group]
    assert items[0]["first_detected"] == first[home.group]
    assert items[0]["last_checked"] > items[0]["first_detected"]


async def test_restart_pending_grace_and_persisted_history(
    hass: HomeAssistant, home: Home, freezer, hass_storage
) -> None:
    # History written before the "restart".
    key = f"entity_unavailable|{home.pilot_area}|{home.group}|"
    hass_storage[PROBLEM_STORE_KEY] = {
        "version": 1, "minor_version": 1, "key": PROBLEM_STORE_KEY,
        "data": {"first_detected": {key: "2026-11-01T08:00:00+00:00", "stale|x||": "2026-10-01T00:00:00+00:00"}},
    }
    hass.set_state(CoreState.starting)
    hass.states.async_remove(home.pir)       # its integration has not loaded yet
    hass.states.async_set(home.group, STATE_UNAVAILABLE)
    await setup_entry(hass, labelled_options(home))

    # While starting, a not-yet-loaded sensor is pending, not a problem.
    assert room(hass, home).attributes["pending"] == [home.pir]
    assert room(hass, home).state == "warning"  # only the unavailable light
    item = state(hass, "sensor", "unavailable_entities").attributes["entities"][0]
    assert item["entity_id"] == home.group
    assert item["first_detected"] == "2026-11-01T08:00:00+00:00"  # survived the restart

    await hass.async_start()
    await hass.async_block_till_done()
    hass.states.async_set(home.pir, "off")    # loads within the grace period
    await settle(hass, freezer)
    freezer.tick(timedelta(seconds=STARTUP_GRACE_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert [i["entity_id"] for i in state(hass, "sensor", "unavailable_entities").attributes["entities"]] == [home.group]
    await settle(hass, freezer, 15)
    assert "stale|x||" not in hass_storage[PROBLEM_STORE_KEY]["data"]["first_detected"]

    hass.states.async_set(home.group, "on", {"entity_id": [home.member_a, home.member_b]})
    await settle(hass, freezer)
    assert summary(hass, home) == HEALTHY
    await settle(hass, freezer, 15)
    # Resolved problem is forgotten; only the informational note remains.
    assert list(hass_storage[PROBLEM_STORE_KEY]["data"]["first_detected"]) == [
        "category_without_label|||led_strips"
    ]


async def test_not_provided_after_grace(hass: HomeAssistant, home: Home, freezer) -> None:
    hass.set_state(CoreState.starting)
    hass.states.async_remove(home.pir)
    await setup_entry(hass, labelled_options(home))
    await hass.async_start()
    await hass.async_block_till_done()
    assert summary(hass, home) == HEALTHY  # still within the grace period
    freezer.tick(timedelta(seconds=STARTUP_GRACE_SECONDS + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    items = state(hass, "sensor", "unavailable_entities").attributes["entities"]
    assert [(i["entity_id"], i["type"]) for i in items] == [(home.pir, "entity_not_provided")]


async def test_area_rename_and_new_entity_reevaluate(hass: HomeAssistant, home: Home, freezer) -> None:
    opts = labelled_options(home)
    opts["rooms"][home.pilot_area]["lights"] = [home.group, "light.added_later"]
    await setup_entry(hass, opts)
    assert summary(hass, home)["missing"] == 1

    # The configured light is added to Home Assistant afterwards.
    ent = er.async_get(hass).async_get_or_create("light", "test", "added_later", suggested_object_id="added_later")
    er.async_get(hass).async_update_entity(ent.entity_id, labels={home.downlight_label})
    hass.states.async_set(ent.entity_id, "off")
    await settle(hass, freezer)
    assert summary(hass, home) == HEALTHY

    ar.async_get(hass).async_update(home.pilot_area, name="Kamar Norð")
    await hass.async_block_till_done()
    await settle(hass, freezer)
    assert state(hass, "sensor", "configured_rooms").attributes["rooms"] == {"Kamar Norð": "healthy"}


async def test_last_checked_refresh_is_bounded(hass: HomeAssistant, home: Home, freezer) -> None:
    freezer.move_to("2026-11-11T10:00:30+00:00")  # well inside a 15-minute window
    await setup_entry(hass, labelled_options(home))
    hass.states.async_set(home.group, STATE_UNAVAILABLE)
    await settle(hass, freezer)
    status_id = eid(hass, "sensor", f"{DOMAIN}_unavailable_entities")
    written = hass.states.get(status_id).last_updated

    # Re-evaluations without any change do not rewrite the state...
    for _ in range(3):
        hass.states.async_set(home.pir, "on")
        await settle(hass, freezer)
        hass.states.async_set(home.pir, "off")
        await settle(hass, freezer)
    assert hass.states.get(status_id).last_updated == written

    # ...but "last checked" moves on at most every 15 minutes.
    freezer.tick(timedelta(minutes=15))
    hass.states.async_set(home.pir, "on")
    await settle(hass, freezer)
    assert hass.states.get(status_id).last_updated > written


async def test_recheck_button(hass: HomeAssistant, home: Home, freezer, service_calls) -> None:
    await setup_entry(hass, labelled_options(home))
    hass.states.async_set(home.group, STATE_UNAVAILABLE)
    await settle(hass, freezer)
    before = state(hass, "sensor", "unavailable_entities").attributes["last_checked"]
    freezer.tick(timedelta(minutes=1))
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, "button", f"{DOMAIN}_recheck")}, blocking=True
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor", "unavailable_entities").attributes["last_checked"] > before
    # The only service call is the button press itself; no light is touched.
    assert [(c.data["domain"], c.data["service"]) for c in service_calls] == [("button", "press")]


async def test_existing_configuration_preserved(hass: HomeAssistant, home: Home) -> None:
    """Phase 0.1 options load unchanged; only entity states/attributes are new."""
    opts = labelled_options(home)
    entry = await setup_entry(hass, opts)
    assert entry.options == opts
    assert entry.version == 1
