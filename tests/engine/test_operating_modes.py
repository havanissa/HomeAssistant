"""Operating modes on the main screens (0.3.6).

* the room's automatic lighting: Observing <-> Automatic (``room/activate`` /
  ``room/deactivate``), the real backend state, persisted over restarts;
* each wall switch: Observing <-> Active (``switch/mode``), independent of the
  room's automatic lighting mode; mappings are never touched by a mode change;
* what happens to a press (outcomes), also when targets are unavailable or a
  service call fails.

Switches, broker and lights are simulated (MQTT messages through Home
Assistant's MQTT integration, mocked light services).
"""

from __future__ import annotations

import logging

import pytest

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError

from custom_components.smart_occupancy.supervision import ControlNotPermittedError
from custom_components.smart_occupancy.websocket import _THROTTLE_SECONDS

from .conftest import setup_entry
from .test_lighting_runtime import WS_LOGGER, Calls, Lit, advance, occupy, options, set_moods
from .test_lighting_runtime import calls, lit  # noqa: F401  (fixtures)
from .test_switches import WS, controlled, ctrl_of, mqtt, press, room_switches, settle  # noqa: F401

ROOM = "smart_occupancy/lighting/room"
MODE = "smart_occupancy/lighting/switch/mode"


async def test_room_mode_observing_automatic_round_trip(hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client,
                                                        hass_read_only_access_token, hass_storage, freezer,
                                                        caplog) -> None:
    """1-2, 10-11: Observing -> Automatic -> Observing changes the real state, persists, keeps the room's settings."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # the refused requests below are expected
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    # Room settings made while Observing must survive every mode change.
    assert (await ws(type=f"{ROOM}/set", area_id=lit.area, manual_override_minutes=25, mood_override="Kvøld"))["success"]
    await advance(hass, freezer)
    assert ctrl.room_diagnostics(lit.area)["control_mode"] == "observe" and calls.all == []
    # A second browser session watches for changes.
    other = await hass_ws_client(hass)
    await other.send_json_auto_id({"type": "smart_occupancy/lighting/subscribe"})
    assert (await other.receive_json())["success"]

    # Without confirmation, and for read-only users, nothing changes.
    assert not (await ws(type=f"{ROOM}/activate", area_id=lit.area))["success"]
    ro = WS(await hass_ws_client(hass, hass_read_only_access_token))
    assert (await ro(type=f"{ROOM}/activate", area_id=lit.area, confirm=True))["error"]["code"] == "unauthorized"
    assert not ctrl.room_active(lit.area)

    # Observing -> Automatic (what the status double-click and the selector send).
    reply = await ws(type=f"{ROOM}/activate", area_id=lit.area, confirm=True)
    assert reply["success"] and reply["result"]["control_enabled"] is True
    await advance(hass, freezer)
    assert ctrl.room_active(lit.area) and ctrl.room_diagnostics(lit.area)["control_mode"] == "active"
    assert calls.all, "Automatic applies the room's mood at once"
    await advance(hass, freezer, _THROTTLE_SECONDS + 0.5)
    event = await other.receive_json()
    assert event["type"] == "event" and event["event"] == {"changed": True}  # the other session refetches
    model = (await ws(type="smart_occupancy/lighting/editor", area_id=lit.area))["result"]["room"]
    assert model["settings"]["control_enabled"] is True and model["diagnostics"]["control_mode"] == "active"

    # Persisted over a restart (reload), settings intact.
    await ctrl.store.async_flush()
    assert hass_storage["smart_occupancy.lighting"]["data"]["rooms"][lit.area]["control_enabled"] is True
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = ctrl_of(entry)
    room = ctrl.lighting.room(lit.area)
    assert room.control_enabled and room.manual_override_minutes == 25 and room.mood_override == "Kvøld"

    # Automatic -> Observing: nothing is sent any more, settings intact, persisted.
    assert (await ws(type=f"{ROOM}/deactivate", area_id=lit.area))["success"]
    await advance(hass, freezer)
    assert not ctrl.room_active(lit.area) and ctrl.room_diagnostics(lit.area)["control_mode"] == "observe"
    calls.clear()
    set_moods(hass, "Nátt", "Auto", "Nátt")
    assert (await ws(type=f"{ROOM}/set", area_id=lit.area, mood_override=None))["success"]
    await advance(hass, freezer, 3)
    assert calls.all == []
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    room = ctrl_of(entry).lighting.room(lit.area)
    assert not room.control_enabled and room.manual_override_minutes == 25


async def test_switch_mode_round_trip_keeps_mappings(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                     hass_ws_client, freezer, caplog) -> None:
    """5-8, 10-11: Observe <-> Active per switch; mappings and buttons untouched; persisted; Active executes."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)  # the unconfirmed request below is expected to fail
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry,
                                 on_right={"action": "set_brightness", "brightness": 40, "target": lit.lamp})
    ctrl = ctrl_of(entry)
    before = ctrl.lighting.room(lit.area).switch(switch_id)
    assert before.controls and before.mappings["on_right"].brightness == 40

    # Active -> Observing: the press is registered and logged, nothing is executed.
    assert (await ws(type=MODE, area_id=lit.area, switch_id=switch_id, mode="listen"))["success"]
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.all == []
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert status.last_outcome == "observed" and "not executed" in status.last_result
    after = ctrl.lighting.room(lit.area).switch(switch_id)
    assert after.mappings == before.mappings and after.buttons == before.buttons
    assert room_switches(ctrl, lit.area)[0]["operating"] == {
        "mode": "observe", "state": "observing", "blocked_by": None,
        "text": "Observing: presses are registered and logged; no action is executed."}

    # Persisted over a restart, still Observing with every mapping.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass, freezer, 3)
    ctrl = ctrl_of(entry)
    switch = ctrl.lighting.room(lit.area).switch(switch_id)
    assert not switch.controls and switch.mappings == before.mappings

    # Observing -> Active (confirmed, as the status double-click and the selector send it).
    assert not (await ws(type=MODE, area_id=lit.area, switch_id=switch_id, mode="control"))["success"]
    assert (await ws(type=MODE, area_id=lit.area, switch_id=switch_id, mode="control", confirm=True))["success"]
    switch = ctrl.lighting.room(lit.area).switch(switch_id)
    assert switch.controls and switch.mappings == before.mappings
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.by_entity() == {lit.lamp: ("turn_on", {"brightness_pct": 40})} and len(calls.all) == 1
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert status.last_outcome == "succeeded" and status.history[-1]["outcome"] == "succeeded"
    assert room_switches(ctrl, lit.area)[0]["operating"]["state"] == "active"
    # The last outcome survives a restart (runtime state store).
    await ctrl.async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass, freezer, 3)
    assert ctrl_of(entry).switch_inputs.status[(lit.area, switch_id)].last_outcome == "succeeded"


async def test_room_mode_and_switch_mode_precedence(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                    hass_ws_client, freezer) -> None:
    """9: automatic lighting never blocks an Active switch; a disabled room does, and the switch says so."""
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry)
    ctrl = ctrl_of(entry)
    for automatic in (False, True):
        if automatic:
            assert (await ws(type=f"{ROOM}/activate", area_id=lit.area, confirm=True))["success"]
            await advance(hass, freezer)
        assert ctrl.room_active(lit.area) is automatic
        assert ctrl.switch_active(lit.area, switch_id)
        calls.clear()
        press(hass, "off_right")
        await settle(hass, freezer, 1)
        assert calls.by_entity()[lit.group] == ("turn_off", {}), automatic
        assert len(calls.off) == 1  # one service call per press, no duplicate execution path
    # The room is disabled in the integration options: the Active switch is shown as blocked, and cannot send.
    hass.config_entries.async_update_entry(entry, options=options(lit, enabled=False))
    await hass.async_block_till_done()
    await settle(hass, freezer, 3)
    ctrl = ctrl_of(entry)
    assert not ctrl.switch_active(lit.area, switch_id)
    operating = room_switches(ctrl, lit.area)[0]["operating"]
    assert operating["mode"] == "active" and operating["state"] == "blocked" and operating["blocked_by"] == "room_disabled"
    with pytest.raises(ControlNotPermittedError):
        await ctrl.async_switch_lights(lit.area, "turn_off", {lit.group: {}}, Context(), switch_id=switch_id)
    # The switch's own mode was not changed by any of this.
    assert ctrl.lighting.room(lit.area).switch(switch_id).controls


async def test_unavailable_targets_and_failed_service_calls(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                            hass_ws_client, freezer) -> None:
    """12: an unavailable target or a failing service is a failed outcome; the switch keeps working afterwards."""
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry)
    ctrl = ctrl_of(entry)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    controlled_lights = list(ctrl.managed_room(lit.area).controlled)
    saved = {e: hass.states.get(e) for e in controlled_lights}
    for e in controlled_lights:
        hass.states.async_set(e, "unavailable")
    await hass.async_block_till_done()
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.all == []
    assert status.last_outcome == "failed" and status.last_result.startswith("No light to control")
    assert "offline" in status.last_result
    for e, state in saved.items():
        hass.states.async_set(e, state.state, state.attributes)
    await hass.async_block_till_done()

    # Home Assistant rejects the call: failed, with the reason; nothing crashes.
    async def broken(call: ServiceCall) -> None:
        raise HomeAssistantError("Zigbee coordinator offline")

    hass.services.async_register("light", "turn_off", broken)
    await advance(hass, freezer, 1)
    press(hass, "off_right")
    await settle(hass, freezer)
    assert status.last_outcome == "failed" and "Zigbee coordinator offline" in status.last_result
    assert not status.override  # nothing was switched, so no manual override
    # The next press executes normally.
    calls.clear()
    await advance(hass, freezer, 1)
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.on and status.last_outcome == "succeeded"
    assert [h["outcome"] for h in status.history][-3:] == ["failed", "failed", "succeeded"]
