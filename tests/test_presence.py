"""Presence tab (0.3.8): sensors, include/exclude, global defaults and room overrides, Kamar Norður scenarios.

The pilot fixture is Kamar Norður with the entity ids of the live installation
(PIR, radar, door, Nobara PC activity) plus a hallway. Sensors and lights are
simulated; nothing here proves real-device behaviour.
"""

from __future__ import annotations

from dataclasses import replace
import logging

from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.smart_occupancy.engine import SignalStatus, Value, interpret
from custom_components.smart_occupancy.lighting_config import set_profile
from custom_components.smart_occupancy.models import IntegrationConfig, ProfileSetting, SensorAssignment
from custom_components.smart_occupancy.occupancy_manager import OccupancyManager
from custom_components.smart_occupancy.presence import display_state

from .conftest import Pilot, pilot_options, setup_entry
from .test_decision_log import log, obey
from .test_lighting_runtime import WS_LOGGER, advance
from .test_switches import WS
from .test_vacancy import ALWAYS, sent

EDITOR = "smart_occupancy/lighting/editor"
SENSOR = "smart_occupancy/lighting/presence/sensor"
TIMING = "smart_occupancy/lighting/presence/timing"


async def presence(ws: WS, area: str) -> dict:
    reply = await ws(type=EDITOR, area_id=area)
    assert reply["success"], reply
    return reply["result"]["room"]["presence"]


def by_id(model: dict) -> dict[str, dict]:
    return {s["entity_id"]: s for s in model["sensors"]}


# ------------------------------------------------------------- live states
def test_binary_sensor_labels_follow_the_device_class(hass: HomeAssistant) -> None:
    """Labels are for display only; the engine reads the raw state."""
    for cls, on, off in (("occupancy", "Detected", "Clear"), ("motion", "Detected", "Clear"),
                         ("door", "Open", "Closed"), (None, "On", "Off"), ("presence", "Home", "Away")):
        hass.states.async_set("binary_sensor.x", "on", {"device_class": cls} if cls else {})
        assert display_state(hass, "binary_sensor.x") == on
        hass.states.async_set("binary_sensor.x", "off", {"device_class": cls} if cls else {})
        assert display_state(hass, "binary_sensor.x") == off
    hass.states.async_set("binary_sensor.x", "unavailable", {"device_class": "occupancy"})
    assert display_state(hass, "binary_sensor.x") == "Unavailable"
    hass.states.async_set("binary_sensor.x", "unknown")
    assert display_state(hass, "binary_sensor.x") == "Unknown"
    assert display_state(hass, "binary_sensor.missing") == "Missing"
    # The underlying semantics: on/off are active/inactive, whatever the label; unknown/unavailable are neither.
    assert interpret("on") == Value.ACTIVE and interpret("off") == Value.INACTIVE
    assert interpret("unknown") == interpret("unavailable") == Value.UNKNOWN
    assert interpret("off", inverted=True) == Value.ACTIVE


async def test_presence_tab_lists_live_sensor_states(hass: HomeAssistant, pilot: Pilot, hass_ws_client) -> None:
    await setup_entry(hass, pilot_options(pilot))
    ws = WS(await hass_ws_client(hass))
    hass.states.async_set(pilot.radar, "on", {"friendly_name": "Radar Kamar Norður", "device_class": "occupancy"})
    hass.states.async_set(pilot.door, "on", {"friendly_name": "Door Kamar Norður", "device_class": "door"})
    hass.states.async_set(pilot.pc, "unavailable", {"friendly_name": "Nobara PC active"})
    await hass.async_block_till_done()
    model = await presence(ws, pilot.area)
    s = by_id(model)
    assert list(s) == [pilot.pir, pilot.radar, pilot.pc, pilot.door]  # every assigned signal, configured order
    radar = s[pilot.radar]
    assert (radar["name"], radar["kind"], radar["role"], radar["state"], radar["label"]) == (
        "Radar Kamar Norður", "radar", "maintain", "on", "Detected")
    assert (radar["value"], radar["condition"], radar["enabled"], radar["available"]) == ("active", "active", True, True)
    assert radar["last_changed"]
    assert (s[pilot.pir]["condition"], s[pilot.pir]["label"]) == ("clear", "Off")
    assert (s[pilot.door]["label"], s[pilot.door]["condition"]) == ("Open", "active")  # movement within the window
    assert "never as presence" in s[pilot.door]["role_name"]
    pc = s[pilot.pc]
    assert (pc["condition"], pc["label"], pc["available"], pc["health"]) == ("unavailable", "Unavailable", False,
                                                                            "unavailable")
    # A state that is not on/off/unknown/unavailable is shown as it is and flagged, never guessed.
    hass.states.async_set(pilot.pir, "garbage", {"friendly_name": "PIR Kamar Norður"})
    await hass.async_block_till_done()
    assert by_id(await presence(ws, pilot.area))[pilot.pir]["condition"] == "invalid"
    assert model["occupancy"]["state"] in ("possible_occupancy", "confirmed_occupancy")
    assert {r["key"] for r in model["timing"]} >= {"inactivity_timeout", "exit_confirmation", "correlation_window"}


async def test_signal_missed_during_start_is_read_again(hass: HomeAssistant, pilot: Pilot) -> None:
    """Root cause of "Nobara: state not understood until removed and re-added".

    The states are read when the engines load, but the state subscription
    starts later (after the lighting store and the first refresh). A PC
    activity sensor that turned on in between stayed "unknown" in the engine
    (while Home Assistant showed it on) until it changed again or the
    integration reloaded. 0.3.8 reads every signal again when the
    subscription starts.
    """
    config = IntegrationConfig.from_dict(pilot_options(pilot))
    hass.states.async_set(pilot.pc, "unavailable", {"friendly_name": "Nobara PC active"})
    await hass.async_block_till_done()

    def started(resync: bool) -> OccupancyManager:
        manager = OccupancyManager(hass, config)
        if not resync:
            manager._resync = lambda: None  # behaviour before 0.3.8
        return manager

    for resync, expected in ((False, Value.UNKNOWN), (True, Value.ACTIVE)):
        hass.states.async_set(pilot.pc, "unavailable", {"friendly_name": "Nobara PC active"})
        await hass.async_block_till_done()
        manager = started(resync)
        await manager.async_load()
        hass.states.async_set(pilot.pc, "on", {"friendly_name": "Nobara PC active"})  # the PC bridge connects
        await hass.async_block_till_done()
        manager.async_start()
        track = manager.engines[pilot.area].tracks[pilot.pc]
        assert track.value == expected, resync
        status = next(v.status for v in manager.result(pilot.area).signals if v.track.entity_id == pilot.pc)
        assert status == (SignalStatus.ACTIVE if resync else SignalStatus.UNKNOWN)
        manager.async_stop()


# --------------------------------------------------------- include / exclude
async def test_exclude_and_include_a_sensor_without_reload(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                           freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    coordinator = entry.runtime_data
    ws = WS(await hass_ws_client(hass))
    hass.states.async_set(pilot.pc, "on", {"friendly_name": "Nobara PC active"})
    await hass.async_block_till_done()
    await advance(hass, freezer, 15)
    result = coordinator.occupancy.result(pilot.area)
    assert result.occupied and result.state.value == "confirmed_occupancy"
    engine = coordinator.occupancy.engines[pilot.area]

    reply = await ws(type=SENSOR, area_id=pilot.area, entity_id=pilot.pc, enabled=False)
    assert reply["success"], reply
    await hass.async_block_till_done()
    # Applied at once, without a reload (same coordinator, same engine object, PC still tracked for display).
    assert entry.runtime_data is coordinator and coordinator.occupancy.engines[pilot.area] is engine
    stored = next(s for s in entry.options["rooms"][pilot.area]["sensors"] if s["entity_id"] == pilot.pc)
    assert stored == {"entity_id": pilot.pc, "kind": "pc_activity", "role": "maintain", "inverted": False,
                      "enabled": False}
    assert not engine.tracks[pilot.pc].used and engine.tracks[pilot.pc].value == Value.ACTIVE
    # The PC no longer holds the room: the vacancy timeout runs (it was the only active signal).
    assert coordinator.occupancy.result(pilot.area).state.value == "possible_vacancy"
    pc = by_id(await presence(ws, pilot.area))[pilot.pc]
    assert (pc["condition"], pc["enabled"], pc["label"]) == ("disabled", False, "On")  # its state is still shown
    # Other rooms and the entity itself are untouched; no health problem for an excluded sensor.
    assert hass.states.get(pilot.pc).state == "on"
    hall = entry.options["rooms"][pilot.hall]["sensors"]
    assert hall == [{"entity_id": pilot.hall_pir, "kind": "pir", "role": "trigger", "inverted": False}]
    assert not [p for p in coordinator.data.problems if p.entity_id == pilot.pc]

    # Included again: exactly the previous assignment, and it holds the room again.
    assert (await ws(type=SENSOR, area_id=pilot.area, entity_id=pilot.pc, enabled=True))["success"]
    await hass.async_block_till_done()
    stored = next(s for s in entry.options["rooms"][pilot.area]["sensors"] if s["entity_id"] == pilot.pc)
    assert stored == {"entity_id": pilot.pc, "kind": "pc_activity", "role": "maintain", "inverted": False}
    assert coordinator.occupancy.result(pilot.area).state.value == "confirmed_occupancy"
    # Validation.
    reply = await ws(type=SENSOR, area_id=pilot.area, entity_id="binary_sensor.nope", enabled=False)
    assert reply["error"]["code"] == "not_found"
    # Survives a restart (reload).
    assert (await ws(type=SENSOR, area_id=pilot.area, entity_id=pilot.door, enabled=False))["success"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    door = next(s for s in entry.runtime_data.config.room(pilot.area).sensors if s.entity_id == pilot.door)
    assert door.enabled is False and door.role.value == "trigger"


async def test_exclusion_is_kept_when_the_room_is_edited(hass: HomeAssistant, pilot: Pilot) -> None:
    opts = pilot_options(pilot, hall=False)
    opts["rooms"][pilot.area]["sensors"][2]["enabled"] = False  # the PC
    entry = await setup_entry(hass, opts)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rooms"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"area": pilot.area})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {
        "enabled": True, "pir": [pilot.pir], "radar": [pilot.radar], "pc_activity": [pilot.pc],
        "door": [pilot.door], "reset": False})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})  # roles unchanged
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})  # timing unchanged
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    pc = next(s for s in entry.options["rooms"][pilot.area]["sensors"] if s["entity_id"] == pilot.pc)
    assert pc["enabled"] is False and pc["role"] == "maintain"


def test_sensor_assignment_round_trip() -> None:
    s = SensorAssignment.from_dict({"entity_id": "binary_sensor.pc", "kind": "pc_activity", "role": "maintain"})
    assert s.enabled and s.participates and "enabled" not in s.as_dict()  # pre-0.3.8 shape unchanged
    off = replace(s, enabled=False)
    assert SensorAssignment.from_dict(off.as_dict()) == off and not off.participates
    assert SensorAssignment.from_dict({**off.as_dict(), "enabled": "yes"}).enabled  # invalid: included


# ------------------------------------------------ global defaults, overrides
async def test_global_defaults_and_room_overrides(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                  caplog) -> None:
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # rejected messages are logged as errors by HA
    entry = await setup_entry(hass, pilot_options(pilot))
    coordinator = entry.runtime_data
    ws = WS(await hass_ws_client(hass))

    def row(model, key="inactivity_timeout"):
        return next(r for r in model["timing"] if r["key"] == key)

    r = row(await presence(ws, pilot.area))
    assert (r["value"], r["global"], r["default"], r["overridden"]) == (600, 600, 600, False)
    # Global default: every room that inherits follows it.
    assert (await ws(type=TIMING, area_id=None, values={"inactivity_timeout": 900}))["success"]
    await hass.async_block_till_done()
    assert (row(await presence(ws, pilot.area))["value"], row(await presence(ws, pilot.hall))["value"]) == (900, 900)
    assert coordinator.occupancy.engines[pilot.area].timing.inactivity_timeout == 900  # live, no reload
    # Room override: kept when the global default changes again.
    reply = await ws(type=TIMING, area_id=pilot.area, values={"inactivity_timeout": 120})
    assert reply["success"] and row(reply["result"])["overridden"] is True
    assert (await ws(type=TIMING, area_id=None, values={"inactivity_timeout": 300}))["success"]
    await hass.async_block_till_done()
    r = row(await presence(ws, pilot.area))
    assert (r["value"], r["global"], r["overridden"]) == (120, 300, True)
    assert row(await presence(ws, pilot.hall))["value"] == 300
    assert coordinator.occupancy.engines[pilot.area].timing.inactivity_timeout == 120
    assert coordinator.occupancy.engines[pilot.hall].timing.inactivity_timeout == 300
    # Persisted in the existing options (one source of truth).
    assert entry.options["occupancy"]["inactivity_timeout"] == 300
    assert entry.options["rooms"][pilot.area]["timing"] == {"inactivity_timeout": 120}
    # Use global default (one setting), then restore all.
    assert (await ws(type=TIMING, area_id=pilot.area, values={"exit_confirmation": 60, "dropout_grace": 10}))["success"]
    assert (await ws(type=TIMING, area_id=pilot.area, values={"inactivity_timeout": None}))["success"]
    assert entry.options["rooms"][pilot.area]["timing"] == {"exit_confirmation": 60, "dropout_grace": 10}
    assert (await ws(type=TIMING, area_id=pilot.area, reset_all=True))["success"]
    assert entry.options["rooms"][pilot.area]["timing"] == {}
    # The global "use default" returns to the built-in value.
    assert (await ws(type=TIMING, area_id=None, values={"inactivity_timeout": None}))["success"]
    assert entry.options["occupancy"]["inactivity_timeout"] == 600
    # Validation before saving.
    reply = await ws(type=TIMING, area_id=pilot.area, values={"inactivity_timeout": 5})
    assert reply["error"]["message"] == "Vacancy timeout must be from 30 to 14400 seconds"
    reply = await ws(type=TIMING, area_id=pilot.area, values={"trigger_stuck_after": 10})
    assert "at least 60 seconds" in reply["error"]["message"]
    assert not (await ws(type=TIMING, area_id=pilot.area, values={"bogus": 1}))["success"]
    assert entry.options["rooms"][pilot.area]["timing"] == {}
    # Persist across a restart.
    assert (await ws(type=TIMING, area_id=pilot.area, values={"correlation_window": 20}))["success"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.config.timing_for(pilot.area).correlation_window == 20


async def test_shorter_timeout_applies_to_a_pending_vacancy(hass: HomeAssistant, pilot: Pilot, hass_ws_client,
                                                            freezer) -> None:
    entry = await setup_entry(hass, pilot_options(pilot))
    coordinator = entry.runtime_data
    hass.states.async_set(pilot.pir, "on")
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 130)  # past the start-up hold (no vacancy is confirmed during it)
    hass.states.async_set(pilot.pir, "off")
    hass.states.async_set(pilot.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 40)
    ws = WS(await hass_ws_client(hass))  # connected after the clock moved (frozen-time token expiry)
    result = coordinator.occupancy.result(pilot.area)
    assert result.state.value == "possible_vacancy" and result.required_quiet == 600
    assert (await ws(type=TIMING, area_id=pilot.area, values={"inactivity_timeout": 60}))["success"]
    await hass.async_block_till_done()
    assert coordinator.occupancy.result(pilot.area).required_quiet == 60  # not restarted from scratch
    await advance(hass, freezer, 30)
    assert coordinator.occupancy.result(pilot.area).state.value == "vacant"


# --------------------------------------------- Kamar Norður regression scenarios
async def pilot_ready(hass: HomeAssistant, pilot: Pilot, freezer):
    seen = obey(hass)
    entry = await setup_entry(hass, pilot_options(pilot))
    ctrl = entry.runtime_data.lighting
    config = set_profile(ctrl.lighting, "global", "Dagur", "downlights",
                         ProfileSetting.from_dict({"action": "on", "brightness_pct": 80}))
    ctrl.store.async_set(config)
    ctrl.store.async_set(ctrl.with_room(pilot.area, control_enabled=True, daylight=ALWAYS))
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    return entry, ctrl, seen


async def test_kamar_nordur_scenarios(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    """A enter, B working at Nobara, C leave, D return before the timeout (A-D in sequence)."""
    entry, ctrl, seen = await pilot_ready(hass, pilot, freezer)
    occ = entry.runtime_data.occupancy
    # A: PIR motion switches the spots on (first motion, prompt).
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert hass.states.is_state(pilot.light, "on") and sent(seen, "turn_on") == {pilot.light}
    hass.states.async_set(pilot.radar, "on")
    hass.states.async_set(pilot.pc, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 15)
    assert occ.result(pilot.area).state.value == "confirmed_occupancy"
    # B: sitting still at the PC: PIR and radar clear, the PC activity holds the room; lights stay.
    seen.clear()
    hass.states.async_set(pilot.pir, "off")
    hass.states.async_set(pilot.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 3600)
    assert occ.result(pilot.area).state.value == "confirmed_occupancy" and seen == []
    assert hass.states.is_state(pilot.light, "on")
    # D: the PC goes idle, presence returns before the timeout: nothing switches off and on.
    hass.states.async_set(pilot.pc, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    assert occ.result(pilot.area).state.value == "possible_vacancy"
    hass.states.async_set(pilot.radar, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert seen == [] and log(ctrl, pilot.area)[0]["action"] == "Vacancy cancelled"
    # C: everything clears (PIR, radar, PC): vacant after the timeout, the spots go off, the log explains.
    hass.states.async_set(pilot.radar, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 300)
    assert seen == []
    await advance(hass, freezer, 400)
    assert occ.result(pilot.area).state.value == "vacant"
    assert sent(seen, "turn_off") == {pilot.light} and hass.states.is_state(pilot.light, "off")
    d = log(ctrl, pilot.area)[0]
    assert (d["action"], d["status"]) == ("Lights off", "executed"), d
    assert "Spots Kamar Norður switched off" in d["explanation"]
    assert any(i.startswith("Nobara PC active: ") for i in d["details"]["occupancy_inputs"])


async def test_kamar_nordur_wall_switch_and_unavailable_pc(hass: HomeAssistant, pilot: Pilot, freezer) -> None:
    """E: a person's change is not undone; F: the PC bridge disconnects while active - not stuck occupied."""
    entry, ctrl, seen = await pilot_ready(hass, pilot, freezer)
    occ = entry.runtime_data.occupancy
    ctrl.store.async_set(ctrl.with_room(pilot.area, manual_switches=("event.swh_kno_action",)))
    await hass.async_block_till_done()
    hass.states.async_set(pilot.pir, "on")
    hass.states.async_set(pilot.pc, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 15)
    assert hass.states.is_state(pilot.light, "on")
    # E: the wall switch switches the spots off; the next mood change leaves them off.
    seen.clear()
    hass.states.async_set("event.swh_kno_action", "2026-11-12T15:00:00+00:00", {"event_type": "off"})
    hass.states.async_set(pilot.light, "off", hass.states.get(pilot.light).attributes, context=Context())
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert ctrl.runtime[pilot.area].overrides[pilot.light]["source"] == "wall_switch"
    from .test_lighting_runtime import set_moods

    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert seen == [] and hass.states.is_state(pilot.light, "off")
    # F: the PC is active and the bridge disconnects; PIR and radar are clear.
    hass.states.async_set(pilot.light, "on", hass.states.get(pilot.light).attributes, context=Context(user_id="p"))
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    await advance(hass, freezer, 60)
    hass.states.async_set(pilot.pc, "unavailable")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    # Other sensors still work: motion is seen while the PC is unavailable.
    hass.states.async_set(pilot.pir, "on")
    await hass.async_block_till_done()
    await advance(hass, freezer, 2)
    assert occ.result(pilot.area).occupied is True
    hass.states.async_set(pilot.pir, "off")
    await hass.async_block_till_done()
    seen.clear()
    # Held longer (the PC was reporting presence when it dropped out), but not forever.
    await advance(hass, freezer, 600)
    assert occ.result(pilot.area).occupied is True
    await advance(hass, freezer, 700)
    assert occ.result(pilot.area).state.value == "vacant"
    assert sent(seen, "turn_off") == {pilot.light}
