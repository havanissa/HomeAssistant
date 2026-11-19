"""App 0.8.0 (Phase A6.1): a wall switch or remote controlling lights outside its own room.

The Wiser switch sits in Kamar Norður; Stova is an adjacent room with its own lamp and automatic lighting. A press
keeps belonging to the switch's room (its input, its activity), the command goes to the chosen lights through the
same controller (one executor), and the manual override is registered in the room that manages each light.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import Context, HomeAssistant

from .conftest import setup_entry
from .test_lighting_runtime import CT, Calls, Lit, activate, advance, occupy, options
from .test_lighting_runtime import calls, lit  # noqa: F401  (fixtures)
from .test_switches import NAME, WS, controlled, ctrl_of, mqtt, press, settle  # noqa: F401  (fixtures)

STOVA_PIR = "binary_sensor.stova_pir"


def two_rooms(lit: Lit, **kamar: Any) -> dict[str, Any]:
    """Kamar Norður (the switch's room, adjacent to Stova) and Stova (enabled, its own sensor and lamp)."""
    opts = options(lit, adjacent_rooms=[lit.other], **kamar)
    opts["rooms"][lit.other] = {"enabled": True, "sensors": [{"entity_id": STOVA_PIR, "kind": "pir",
                                                              "role": "trigger"}], "lights": [lit.other_lamp]}
    return opts


async def setup(hass: HomeAssistant, lit: Lit, freezer, **kamar: Any):
    hass.states.async_set(STOVA_PIR, "off", {"device_class": "motion"})
    hass.states.async_set(lit.other_lamp, "off", {**CT, "friendly_name": "Stova lamp"})
    entry = await setup_entry(hass, two_rooms(lit, **kamar))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer, manual_override_minutes=30)
    return entry, ctrl_of(entry)


async def test_a_button_controls_the_adjacent_room_and_the_override_belongs_there(
        hass: HomeAssistant, mqtt, lit: Lit, calls: Calls, hass_ws_client, freezer) -> None:
    entry, ctrl = await setup(hass, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    # Stova's automatic lighting is on too (it is vacant now), with its own override time.
    ctrl.store.async_set(ctrl.with_room(lit.other, control_enabled=True, manual_override_minutes=90))
    await advance(hass, freezer)
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=lit.area)
    options_ = editor["result"]["room"]["switches"]["target_options"]
    assert options_["adjacent"][0]["area_id"] == lit.other and options_["adjacent"][0]["name"] == "Stova"
    assert [x["entity_id"] for x in options_["adjacent"][0]["lights"]] == [lit.other_lamp]
    assert options_["this_room"]["area_id"] == lit.area
    switch_id = await controlled(hass, ws, lit, freezer, entry,
                                 on_left={"action": "on", "targets": [f"room:{lit.other}"]},
                                 off_left={"action": "off", "targets": [f"room:{lit.other}"]},
                                 on_right={"action": "toggle", "targets": [lit.other_lamp]})
    calls.clear()
    stova_before = ctrl.occupancy.result(lit.other) if hasattr(ctrl, "occupancy") else None
    press(hass, "on_left")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert list(sent) == [lit.other_lamp] and sent[lit.other_lamp][0] == "turn_on"   # nothing in Kamar
    # The override is Stova's, with Stova's time; Kamar gets none from this press.
    override = ctrl.runtime[lit.other].overrides[lit.other_lamp]
    assert override["source"] == "wall_switch" and override["switch_room"] == lit.area and override["switch"] == NAME
    from homeassistant.util import dt as dt_util  # noqa: PLC0415
    left = dt_util.parse_datetime(override["until"]) - dt_util.utcnow()
    assert 89 * 60 < left.total_seconds() <= 90 * 60
    assert lit.other_lamp not in ctrl.runtime[lit.area].overrides
    # Stova's occupancy is not changed by a light switched from Kamar.
    if stova_before is not None:
        assert ctrl.occupancy.result(lit.other) == stova_before
    # Stova is vacant: its automatic lighting keeps the lamp as the person left it.
    hass.states.async_set(lit.other_lamp, "on", {**CT, "brightness": 200}, context=Context())
    await settle(hass, freezer, 2)
    plan = {i.entity_id: i.reason_code for i in ctrl.plan(lit.other).items}
    assert plan.get(lit.other_lamp) == "manual_override"
    # Resume automatic lighting in Stova releases it; the next press establishes it again.
    reply = await ws(type="smart_occupancy/lighting/override/clear", area_id=lit.other)
    assert reply["success"], reply
    assert lit.other_lamp not in ctrl.runtime[lit.other].overrides
    calls.clear()
    press(hass, "on_right")                         # toggle: the lamp is on, so off
    await settle(hass, freezer)
    assert calls.by_entity() == {lit.other_lamp: ("turn_off", {})}
    assert ctrl.runtime[lit.other].overrides[lit.other_lamp]["state"] == "off"
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert "switched off" in status.last_result


async def test_several_targets_one_command_each_and_groups_never_twice(
        hass: HomeAssistant, mqtt, lit: Lit, calls: Calls, hass_ws_client, freezer) -> None:
    entry, ctrl = await setup(hass, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    hass.states.async_set(lit.group, "on", {**CT, "entity_id": [lit.spot_1, lit.spot_2]})
    await settle(hass, freezer)
    await controlled(hass, ws, lit, freezer, entry,
                     off_left={"action": "off", "targets": [lit.group, lit.spot_1, lit.other_lamp, lit.other_lamp]},
                     off_right={"action": "off", "targets": ["room", f"room:{lit.other}"]})
    calls.clear()
    press(hass, "off_left")
    await settle(hass, freezer)
    sent = calls.by_entity()
    # The group once (not its member again), the Stova lamp once.
    assert set(sent) == {lit.group, lit.other_lamp} and lit.spot_1 not in sent
    assert lit.group in ctrl.runtime[lit.area].overrides and lit.other_lamp in ctrl.runtime[lit.other].overrides
    calls.clear()
    press(hass, "off_right")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert set(sent) == set(ctrl.managed_room(lit.area).controlled) | {lit.other_lamp}
    assert all(len([c for c in calls.all if e in (c.data["entity_id"] if isinstance(c.data["entity_id"], list)
                                                  else [c.data["entity_id"]])]) == 1 for e in sent)


async def test_capabilities_offline_targets_and_the_preview(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                            hass_ws_client, freezer) -> None:
    entry, ctrl = await setup(hass, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    # The Stova lamp is a relay (on/off only): it never receives a brightness or colour temperature.
    hass.states.async_set(lit.other_lamp, "on", {"supported_color_modes": ["onoff"], "color_mode": "onoff",
                                                 "friendly_name": "Stova lamp"})
    await settle(hass, freezer)
    preview = await ws(type="smart_occupancy/lighting/switch/preview", area_id=lit.area,
                       mapping={"action": "set_brightness", "brightness": 40, "targets": [lit.lamp, lit.other_lamp]})
    assert preview["success"], preview
    result = preview["result"]
    assert result["error"] is None and result["cross_room"] is True
    rows = {t["entity_id"]: t for t in result["targets"]}
    assert rows[lit.other_lamp]["room_name"] == "Stova" and rows[lit.other_lamp]["on_off_only"] is True
    assert "(Stova)" in result["summary"] and rows[lit.other_lamp]["skipped"].startswith("on/off only")
    await controlled(hass, ws, lit, freezer, entry,
                     on_left={"action": "set_brightness", "brightness": 40, "targets": [lit.lamp, lit.other_lamp]},
                     on_right={"action": "set_color_temp", "color_temp": 3000, "targets": [lit.other_lamp]},
                     off_left={"action": "off", "targets": [lit.other_lamp]})
    calls.clear()
    press(hass, "on_left")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert sent[lit.lamp] == ("turn_on", {"brightness_pct": 40}) and sent[lit.other_lamp] == ("turn_on", {})
    calls.clear()
    press(hass, "on_right")                                   # colour temperature on a relay: nothing is sent
    await settle(hass, freezer)
    assert calls.all == []
    # Offline target: reported, nothing sent.
    hass.states.async_set(lit.other_lamp, "unavailable")
    await settle(hass, freezer)
    press(hass, "off_left")
    await settle(hass, freezer)
    assert calls.all == []
    status = next(iter(ctrl.switch_inputs.status.values()))
    assert "offline" in status.last_result
    # Unknown rooms and non-lights are refused when saving; a room that is not enabled resolves to nothing.
    switch_id = next(iter(ctrl.lighting.room(lit.area).switches)).id
    bad = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                   raw="on_left", mapping={"action": "on", "targets": ["room:nowhere"]})
    assert not bad["success"]
    bad = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                   raw="on_left", mapping={"action": "on", "targets": ["switch.kettle"]})
    assert not bad["success"]


async def test_hold_to_dim_across_rooms_stops_on_release(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                         hass_ws_client, freezer) -> None:
    entry, ctrl = await setup(hass, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    up = {"action": "dim_up", "targets": [lit.other_lamp]}
    switch_id = await controlled(hass, ws, lit, freezer, entry, brightness_move_up_left=up)
    assert (await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
                     dimming={"hold_step": 10, "interval": 0.5, "min_brightness": 10, "max_brightness": 90,
                              "timeout": 5}))["success"]
    hass.states.async_set(lit.other_lamp, "on", {**CT, "brightness": 128, "color_mode": "color_temp"})
    await settle(hass, freezer)
    calls.clear()
    press(hass, "brightness_move_up_left")
    await hass.async_block_till_done()
    await advance(hass, freezer, 0.5)
    press(hass, "brightness_stop_left")
    await settle(hass, freezer)
    await advance(hass, freezer, 2)
    levels = [c.data["brightness_pct"] for c in calls.on]
    assert levels == [60, 70] and all(c.data["entity_id"] == [lit.other_lamp] for c in calls.on)
    assert not ctrl.switch_control.dimming(lit.area, switch_id)
    assert ctrl.runtime[lit.other].overrides[lit.other_lamp]["state"] == "on"


async def test_mappings_survive_adjacency_changes_renames_and_restarts(hass: HomeAssistant, mqtt, lit: Lit,
                                                                       calls: Calls, hass_ws_client, freezer) -> None:
    entry, ctrl = await setup(hass, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    switch_id = await controlled(hass, ws, lit, freezer, entry,
                                 on_left={"action": "on", "targets": [lit.other_lamp]})
    # Stova is no longer adjacent: the explicit target is kept (adjacency only groups the picker).
    entry_options = dict(entry.options)
    rooms = dict(entry_options["rooms"])
    rooms[lit.area] = {**rooms[lit.area], "adjacent_rooms": []}
    hass.config_entries.async_update_entry(entry, options={**entry_options, "rooms": rooms})
    await hass.async_block_till_done()
    await settle(hass, freezer, 3)
    ctrl = ctrl_of(entry)
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=lit.area)
    assert editor["result"]["room"]["switches"]["target_options"]["adjacent"] == []
    assert any(r["area_id"] == lit.other for r in editor["result"]["room"]["switches"]["target_options"]["other"])
    calls.clear()
    press(hass, "on_left")
    await settle(hass, freezer)
    assert list(calls.by_entity()) == [lit.other_lamp]
    # The lamp is renamed in Home Assistant: the mapping follows.
    from custom_components.smart_occupancy.lighting_config import rename_entity  # noqa: PLC0415
    renamed = rename_entity(ctrl.store.config, lit.other_lamp, "light.stova_floor_lamp")
    assert renamed is not None
    mapping = renamed.room(lit.area).switch(switch_id).mappings["on_left"]
    assert mapping.targets == ("light.stova_floor_lamp",)
    # Stored and read back unchanged (and an old mapping without targets has no "targets" key).
    stored = ctrl.lighting.room(lit.area).switch(switch_id).mappings["on_left"].as_dict()
    assert stored["targets"] == [lit.other_lamp]
    from custom_components.smart_occupancy.switches import ButtonMapping  # noqa: PLC0415
    assert "targets" not in ButtonMapping.from_dict({"action": "on"}).as_dict()
    assert ButtonMapping.from_dict(stored).targets == (lit.other_lamp,)
