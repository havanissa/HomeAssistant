"""Phase 0.2 occupancy engine inside Home Assistant (pilot: Kamar Norður).

Runs the real Home Assistant core (state machine, registries, config
entries, storage, event helpers and timers); time is advanced with the
freezer so timeouts are exercised exactly.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, entity_registry as er

from custom_components.smart_occupancy.const import DOMAIN, OCCUPANCY_STORE_KEY
from custom_components.smart_occupancy.models import migrate_options
from custom_components.smart_occupancy.sensor import RoomStateSensor

from .conftest import Pilot, pilot_options, setup_entry

ROOM = "kamar_nordur"
OCCUPIED = f"binary_sensor.smart_occupancy_{ROOM}_occupied"
STATE = f"sensor.smart_occupancy_{ROOM}_state"
CONFIDENCE = f"sensor.smart_occupancy_{ROOM}_confidence"
LAST_ACTIVITY = f"sensor.smart_occupancy_{ROOM}_last_activity"
REASON = f"sensor.smart_occupancy_{ROOM}_reason"
COMPARISON = f"sensor.smart_occupancy_{ROOM}_helper_comparison"


async def tick(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def set_state(hass: HomeAssistant, entity_id: str, value: str, **attrs: Any) -> None:
    old = hass.states.get(entity_id)
    attributes = {**(old.attributes if old else {}), **attrs}
    hass.states.async_set(entity_id, value, attributes)
    await hass.async_block_till_done()


def room_state(hass: HomeAssistant) -> tuple[str, str]:
    return hass.states.get(STATE).state, hass.states.get(OCCUPIED).state


def attrs(hass: HomeAssistant) -> dict[str, Any]:
    return dict(hass.states.get(STATE).attributes)


async def start(hass: HomeAssistant, pilot: Pilot, freezer, **kw) -> MockConfigEntry:
    entry = await setup_entry(hass, pilot_options(pilot, **kw))
    await tick(hass, freezer, 121)  # past the start-up hold
    return entry


# ----------------------------------------------------- install / upgrade
async def test_upgrade_from_0_1_1_creates_occupancy_entities(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    v1 = pilot_options(pilot)
    entry = await setup_entry(hass, v1)
    assert (entry.version, entry.minor_version) == (1, 2)
    assert entry.options == migrate_options(v1)
    assert entry.options["rooms"][pilot.area]["reference_occupancy_entity"] == pilot.helper
    # Phase 0.1.1 entities keep their unique ids; the new ones have the suggested ids.
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_room_{pilot.area}_observation")
    for entity_id in (OCCUPIED, STATE, CONFIDENCE, LAST_ACTIVITY, REASON, COMPARISON):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get("binary_sensor.smart_occupancy_gongd_occupied").state == "off"
    assert hass.states.get("sensor.smart_occupancy_gongd_helper_comparison") is None  # no helper there
    assert room_state(hass) == ("vacant", "off")
    assert hass.states.get(STATE).attributes["observe_only"] is True


async def test_new_installation_flow_creates_v2_entry(hass: HomeAssistant, pilot: Pilot) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "auto_mood_entity": "input_select.auto_mood", "manual_mood_entity": "input_select.mood",
        "main_mood_entity": "input_select.main_mood", "auto_option": "Auto", "mismatch_grace_seconds": 30,
    })
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"create_missing_labels": False})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"rooms": [pilot.area]})
    assert result["step_id"] == "room_assign"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "pir": [pilot.pir], "radar": [pilot.radar], "pc_activity": [pilot.pc], "door": [pilot.door],
        "reference_occupancy_entity": pilot.helper,
    })
    assert result["step_id"] == "room_roles"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        pilot.pir: "trigger", pilot.radar: "maintain", pilot.pc: "maintain", pilot.door: "trigger",
    })
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert (entry.version, entry.minor_version) == (1, 2)
    assert entry.options["config_version"] == 2
    assert entry.options["occupancy"]["inactivity_timeout"] == 600
    assert hass.states.get(OCCUPIED).state == "off"


# --------------------------------------------------------- pilot rules
async def test_pir_radar_pc_and_vacancy(hass: HomeAssistant, pilot: Pilot, freezer, service_calls) -> None:
    await start(hass, pilot, freezer)

    # 1. PIR -> immediate possible occupancy.
    await set_state(hass, pilot.pir, "on")
    assert room_state(hass) == ("possible_occupancy", "on")
    assert attrs(hass)["active_sensors"] == ["PIR Kamar Norður"]
    assert hass.states.get(LAST_ACTIVITY).attributes["source"] == pilot.pir

    # 2. Radar maintains (and corroborates).
    await set_state(hass, pilot.radar, "on")
    assert room_state(hass) == ("confirmed_occupancy", "on")
    assert hass.states.get(CONFIDENCE).state == "100"
    await set_state(hass, pilot.pc, "on")
    await set_state(hass, pilot.pir, "off")

    # 3/4. Radar clears while the PC is active: no vacancy.
    await set_state(hass, pilot.radar, "off")
    await tick(hass, freezer, 3600)
    assert room_state(hass) == ("confirmed_occupancy", "on")
    assert attrs(hass)["active_sensors"] == ["Nobara PC active"]
    assert "Nobara PC active" in hass.states.get(REASON).state

    # 5. PC stops: vacancy confirmation begins, respecting the inactivity timeout.
    await set_state(hass, pilot.pc, "off")
    await tick(hass, freezer, 31)  # past the dropout grace
    assert room_state(hass) == ("possible_vacancy", "on")
    a = attrs(hass)
    assert a["pending_vacancy"] is True and a["required_quiet_seconds"] == 600 and a["vacancy_confirm_at"]
    await tick(hass, freezer, 500)
    assert room_state(hass) == ("possible_vacancy", "on")

    # 6. Confirmed only after the required delay.
    await tick(hass, freezer, 80)
    assert room_state(hass) == ("vacant", "off")
    assert attrs(hass)["vacancy_confirm_at"] is None

    # Observe only: nothing was called, the helper was not touched.
    assert service_calls == []
    assert hass.states.get(pilot.helper).state == "off"


async def test_radar_dropout_and_simultaneous_sensors(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    await start(hass, pilot, freezer)
    for entity_id in (pilot.pir, pilot.radar, pilot.pc):
        await set_state(hass, entity_id, "on")
    assert room_state(hass) == ("confirmed_occupancy", "on")
    assert sorted(attrs(hass)["active_sensors"]) == ["Nobara PC active", "PIR Kamar Norður", "Radar Kamar Norður"]
    await set_state(hass, pilot.pc, "off")
    await set_state(hass, pilot.pir, "off")
    events: list = []
    hass.bus.async_listen(EVENT_STATE_CHANGED, lambda e: events.append(e) if e.data["entity_id"] == OCCUPIED else None)
    for _ in range(5):  # brief radar dropouts
        await set_state(hass, pilot.radar, "off")
        assert "Radar Kamar Norður (dropout grace)" in attrs(hass)["active_sensors"]
        await tick(hass, freezer, 10)
        await set_state(hass, pilot.radar, "on")
        await tick(hass, freezer, 30)
    assert room_state(hass) == ("confirmed_occupancy", "on")
    assert events == []  # the occupied output never toggled


async def test_pir_stuck_on(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    await start(hass, pilot, freezer)
    await set_state(hass, pilot.pir, "on")
    await tick(hass, freezer, 1801)
    assert attrs(hass)["stuck_sensors"] == ["PIR Kamar Norður"]
    assert room_state(hass) == ("possible_vacancy", "on")
    health = attrs(hass)["health"]["occupancy"]
    assert health["status"] == "warning" and "stuck" in health["reasons"][0]
    await tick(hass, freezer, 600)
    assert room_state(hass) == ("vacant", "off")
    assert hass.states.get(pilot.pir).state == "on"  # still stuck, but no longer holds the room


async def test_unknown_and_unavailable_are_not_vacancy(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    await start(hass, pilot, freezer)
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 15)
    await set_state(hass, pilot.radar, STATE_UNAVAILABLE)
    await set_state(hass, pilot.pc, STATE_UNKNOWN)
    await tick(hass, freezer, 900)
    assert room_state(hass) == ("possible_vacancy", "on")
    a = attrs(hass)
    assert {s["entity_id"] for s in a["unavailable_sensors"]} == {pilot.radar, pilot.pc}
    assert "became unavailable while reporting presence" in a["reason"]
    assert a["health"]["occupancy"]["status"] == "warning" and a["occupancy_usable"] is True

    # Everything unavailable: uncertain, last state kept.
    await set_state(hass, pilot.pir, STATE_UNAVAILABLE)
    await tick(hass, freezer, 7200)
    assert room_state(hass) == ("uncertain", "on")
    a = attrs(hass)
    assert a["health"]["occupancy"]["status"] == "unavailable" and a["occupancy_usable"] is False
    assert hass.states.get(CONFIDENCE).state == "0"

    # Reconnect: quiet period restarts from the reconnect.
    for entity_id in (pilot.pir, pilot.radar, pilot.pc):
        await set_state(hass, entity_id, "off")
    assert room_state(hass)[1] == "on"
    await tick(hass, freezer, 31)
    assert room_state(hass) == ("possible_vacancy", "on")
    await tick(hass, freezer, 568)
    assert room_state(hass)[1] == "on"
    await tick(hass, freezer, 2)
    assert room_state(hass) == ("vacant", "off")


async def test_offline_light_is_lighting_warning_only(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    await start(hass, pilot, freezer)
    await set_state(hass, pilot.light, STATE_UNAVAILABLE)
    await tick(hass, freezer, 3)  # diagnostics debounce
    health = attrs(hass)["health"]
    assert health["occupancy"]["status"] == "healthy" and health["occupancy"]["usable"] is True
    assert health["lighting"]["status"] == "warning"
    assert health["lighting"]["unavailable_lights"][0]["name"] == "Spots Kamar Norður"
    assert "Spots Kamar Norður (light.spt_kno_group)" in health["lighting"]["reasons"][0]
    assert health["configuration"]["status"] == "healthy"


async def test_configuration_health_separate(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    options = pilot_options(pilot)
    options["rooms"][pilot.area]["sensors"].append(
        {"entity_id": "binary_sensor.deleted_sensor", "kind": "other", "role": "maintain"}
    )
    await setup_entry(hass, options)
    await tick(hass, freezer, 121)
    health = attrs(hass)["health"]
    assert health["configuration"]["status"] == "config_error"
    assert "binary_sensor.deleted_sensor" in health["configuration"]["reasons"][0]
    assert health["occupancy"]["usable"] is True  # the other sensors still work


async def test_door_and_adjacent_room(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    options = pilot_options(pilot)
    options["rooms"][pilot.area]["adjacent_rooms"] = [pilot.hall]
    await setup_entry(hass, options)
    await tick(hass, freezer, 121)
    # The door alone does not make the room occupied.
    await set_state(hass, pilot.door, "on")
    assert room_state(hass) == ("possible_occupancy", "off")
    await tick(hass, freezer, 11)
    assert room_state(hass) == ("vacant", "off")
    # Someone inside, then leaves through the door into the hallway.
    await set_state(hass, pilot.pir, "on")
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 60)
    await set_state(hass, pilot.door, "off")
    await set_state(hass, pilot.hall_pir, "on")
    await set_state(hass, pilot.radar, "off")
    await set_state(hass, pilot.pir, "off")
    await tick(hass, freezer, 31)
    a = attrs(hass)
    assert room_state(hass) == ("possible_vacancy", "on")
    assert a["required_quiet_seconds"] == 300 and "adjacent room" in a["vacancy_evidence"][0]
    assert hass.states.get("binary_sensor.smart_occupancy_gongd_occupied").state == "on"
    await tick(hass, freezer, 300)
    assert room_state(hass) == ("vacant", "off")


# ------------------------------------------------------ helper comparison
async def test_helper_comparison(hass: HomeAssistant, pilot: Pilot, freezer, service_calls) -> None:
    await start(hass, pilot, freezer)
    assert hass.states.get(COMPARISON).state == "agree"
    await set_state(hass, pilot.radar, "on")
    await set_state(hass, pilot.helper, "on")   # the existing automation follows the radar
    await tick(hass, freezer, 20)
    await set_state(hass, pilot.radar, "off")
    await set_state(hass, pilot.pc, "on")       # PC keeps the room occupied
    await set_state(hass, pilot.helper, "off")  # old automation clears with the radar
    # Within the grace (correlation window) a disagreement is not reported.
    assert hass.states.get(COMPARISON).state == "agree"
    await tick(hass, freezer, 120)
    comparison = hass.states.get(COMPARISON)
    assert comparison.state == "disagree"
    assert comparison.attributes["disagreements"] == 1 and comparison.attributes["episode_open_since"]
    await set_state(hass, pilot.radar, "on")    # still there: the helper cleared prematurely
    assert hass.states.get(COMPARISON).attributes["helper_cleared_prematurely"] == 1
    await set_state(hass, pilot.helper, "on")
    a = hass.states.get(COMPARISON).attributes
    assert hass.states.get(COMPARISON).state == "agree"
    assert a["total_disagreement_seconds"] >= 120
    assert attrs(hass)["comparison"]["helper"] == pilot.helper
    assert service_calls == []


# ---------------------------------------------- reload, restart, options
async def test_reload_keeps_occupancy_and_cleans_up(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    entry = await start(hass, pilot, freezer)
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 15)
    await tick(hass, freezer, 11)  # the visit's decision log entry is saved (delayed write), not a subscription
    listeners_before = hass.bus.async_listeners()
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.bus.async_listeners() == listeners_before  # no duplicated subscriptions
    assert room_state(hass) == ("confirmed_occupancy", "on")
    manager = entry.runtime_data.occupancy
    assert len(manager._timers) <= len(manager.engines)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert manager._timers == {} and manager._unsub is None
    assert entry.state is ConfigEntryState.NOT_LOADED
    # Events after unload do nothing (and raise nothing).
    await set_state(hass, pilot.radar, "off")
    await tick(hass, freezer, 1000)


async def test_restart_is_conservative(hass: HomeAssistant, pilot: Pilot, freezer, hass_storage) -> None:
    entry = await start(hass, pilot, freezer)
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 20)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass_storage[OCCUPANCY_STORE_KEY]["data"]["rooms"][pilot.area]["engine"]
    assert stored["occupied"] is True and stored["state"] == "confirmed_occupancy"

    # "Restart": Home Assistant starting, sensors not loaded yet.
    hass.set_state(CoreState.starting)
    for entity_id in (pilot.pir, pilot.radar, pilot.pc, pilot.door):
        hass.states.async_set(entity_id, STATE_UNAVAILABLE, {"restored": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert room_state(hass) == ("uncertain", "on")
    await hass.async_start()
    await hass.async_block_till_done()
    for entity_id in (pilot.pir, pilot.radar, pilot.pc, pilot.door):
        hass.states.async_set(entity_id, "off", {"friendly_name": entity_id})
    await hass.async_block_till_done()
    assert room_state(hass) == ("possible_vacancy", "on")
    assert attrs(hass)["starting_up"] is True
    await tick(hass, freezer, 599)
    assert room_state(hass)[1] == "on"  # no false vacancy after the restart
    await tick(hass, freezer, 2)
    assert room_state(hass) == ("vacant", "off")


async def test_timing_change_applies_without_restart(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    entry = await start(hass, pilot, freezer)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "occupancy"})
    assert result["step_id"] == "occupancy"
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {**entry.options["occupancy"], "trigger_stuck_after": 5}
    )
    assert bad["errors"] == {"trigger_stuck_after": "stuck_too_short"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**entry.options["occupancy"], "inactivity_timeout": 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert attrs(hass)["timing"]["inactivity_timeout"] == 120

    # Per-room override and adjacency through the room editor.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rooms"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": pilot.area})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "enabled": True, "pir": [pilot.pir], "radar": [pilot.radar], "pc_activity": [pilot.pc],
        "door": [pilot.door], "lights": [pilot.light], "reference_occupancy_entity": pilot.helper, "reset": False,
    })
    assert result["step_id"] == "room_roles"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        pilot.pir: "trigger", pilot.radar: "confirm_vacancy", pilot.pc: "maintain", pilot.door: "trigger",
        "inverted_signals": [],
    })
    assert result["step_id"] == "room_occupancy"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"exit_confirmation": 60, "adjacent_rooms": [pilot.hall]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    room = entry.options["rooms"][pilot.area]
    assert room["timing"] == {"exit_confirmation": 60} and room["adjacent_rooms"] == [pilot.hall]
    assert room["reference_occupancy_entity"] == pilot.helper and room["lights"] == [pilot.light]
    assert attrs(hass)["timing"]["exit_confirmation"] == 60
    assert attrs(hass)["timing"]["inactivity_timeout"] == 120  # still inherited from global

    # Radar as Confirm vacancy now gives the fast path.
    await tick(hass, freezer, 121)
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 15)
    await set_state(hass, pilot.radar, "off")
    await tick(hass, freezer, 31)
    assert attrs(hass)["required_quiet_seconds"] == 60
    await tick(hass, freezer, 30)
    assert room_state(hass) == ("vacant", "off")


async def test_entity_ids_stable_after_area_rename(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    await start(hass, pilot, freezer)
    ar.async_get(hass).async_update(pilot.area, name="Norðurkamar")
    await hass.async_block_till_done()
    await tick(hass, freezer, 3)
    assert hass.states.get(OCCUPIED) is not None
    assert hass.states.get(STATE).attributes["room"] == "Norðurkamar"
    assert "Norðurkamar" in hass.states.get(OCCUPIED).name


# --------------------------------------------------- recorder / efficiency
def test_high_frequency_attributes_are_not_recorded() -> None:
    unrecorded = RoomStateSensor._unrecorded_attributes
    for key in ("signals", "health", "active_sensors", "unavailable_sensors", "vacancy_confirm_at", "reason"):
        assert key in unrecorded


async def test_no_writes_while_waiting(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    """A pending vacancy is one timer, not a countdown: nothing is written while waiting."""
    await start(hass, pilot, freezer)
    await set_state(hass, pilot.radar, "on")
    await tick(hass, freezer, 15)
    await set_state(hass, pilot.radar, "off")
    await tick(hass, freezer, 31)
    assert room_state(hass) == ("possible_vacancy", "on")
    written: list[str] = []
    hass.bus.async_listen(EVENT_STATE_CHANGED, lambda e: written.append(e.data["entity_id"]))
    for _ in range(9):
        await tick(hass, freezer, 60)
    assert written == []
    await tick(hass, freezer, 60)
    assert OCCUPIED in written and room_state(hass) == ("vacant", "off")


async def test_motion_does_not_rebuild_diagnostics(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    entry = await start(hass, pilot, freezer)
    coordinator = entry.runtime_data
    before = coordinator.data.evaluated_at
    for value in ("on", "off", "on", "off"):
        await set_state(hass, pilot.pir, value)
        await tick(hass, freezer, 2)
    assert coordinator.data.evaluated_at == before
    await set_state(hass, pilot.pir, STATE_UNAVAILABLE)  # availability matters
    await tick(hass, freezer, 2)
    assert coordinator.data.evaluated_at > before
