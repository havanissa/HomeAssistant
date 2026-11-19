"""App 0.9.0 (Phase A6.2): Delay On / Delay Off of brightness matrix cells.

The Phase 4.7 Kamar Norður of ``test_brightness.py`` (simulated Zigbee2MQTT-like devices that report with their
own context) with a floor lamp whose Room occupied cells are Off and whose Desk zone cell is 45 % at Low light:
the Desk zone switches it on and off, so its delays are observable. The clock is HA's frozen test clock, moved in
steps of at most 2 s.
"""

from __future__ import annotations

from dataclasses import replace
import json

import pytest

from homeassistant.core import Context

from custom_components.smart_occupancy.brightness import LightMatrix, Template
from custom_components.smart_occupancy.lighting_config import LightingConfig, migrate_store
from custom_components.smart_occupancy.priority import PriorityInput

from .test_brightness import (  # noqa: F401 - fixtures
    CT,
    DESK,
    SEAT,
    devices,
    home,
    occupy,
    outdoor,
    pct,
    sensor,
    start,
    wait,
    with_room,
)

DELAYED = {"light.kno_floor": LightMatrix(
    "light.kno_floor", room={"none": 0, "low": 0, "bright": 0},
    zones={"desk": {"none": 65, "low": 45, "bright": 80}, "seating": {"none": 30, "low": 20, "bright": None}},
    delays={"desk": {"low": (3, 15), "none": (3, 15)}})}


def floor_calls(devices, h) -> list[tuple[str, dict]]:
    return devices.of(h.floor)


def item(ctrl, h):
    return next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.floor)


async def zone_until(hass, freezer, ctrl, h, zone: str, active: bool, sim, limit: float = 90) -> float:
    """Advance until the zone reaches the state; returns the seconds it took."""
    waited = 0.0
    while ctrl.zones.zone(h.area, zone).active is not active:
        await wait(hass, freezer, 1, sim)
        waited += 1
        assert waited <= limit, f"{zone} never became {'active' if active else 'inactive'}"
    return waited


# ===================================================================== configuration
def test_cell_delays_round_trip_and_older_matrices_unchanged() -> None:
    old = LightMatrix.from_dict("light.a", {"room": {"low": 40}, "zones": {"desk": {"low": 70}}}, ["desk"])
    assert old.delays == {} and "delays" not in old.as_dict()  # serialised exactly as before
    m = LightMatrix.from_dict("light.a", {"room": {"low": 40}, "zones": {"desk": {"low": 70}},
                                          "delays": {"desk": {"low": {"on": 3, "off": 15}, "none": {"on": 0, "off": 0}},
                                                     "gone": {"low": {"on": 9}}, "room": {"low": {"on": 99999}}}},
                              ["desk"])
    assert m.delays == {"desk": {"low": (3, 15)}, "room": {"low": (3600, 0)}}  # bounded; unknown zone dropped
    assert m.delay("desk", "low") == (3, 15) and m.delay("room", "none") == (0, 0)
    assert LightMatrix.from_dict("light.a", m.as_dict(), ["desk"]) == m
    # A removed zone leaves no column and no delay behind.
    assert m.without_columns([]).delays == {"room": {"low": (3600, 0)}} and m.without_columns([]).zones == {}
    # Removed ambient categories move their delays like their values.
    assert m.remapped({"low": "none"}).delays == {"desk": {"none": (3, 15)}, "room": {"none": (3600, 0)}}


def test_templates_carry_delays_to_every_zone_column() -> None:
    t = Template.from_dict({"id": "desk", "name": "Desk", "room": {"none": 0, "low": 0, "bright": 0},
                            "zone": {"none": 70, "low": 50, "bright": 0},
                            "delays": {"zone": {"low": {"on": 2, "off": 30}}}})
    assert t.zone_delays == {"low": (2, 30)} and t.as_dict()["delays"] == {"room": {}, "zone": {"low": {"on": 2,
                                                                                                    "off": 30}}}
    m = t.matrix("light.a", ["desk", "sofa"])
    assert m.delays == {"desk": {"low": (2, 30)}, "sofa": {"low": (2, 30)}}
    plain = Template.from_dict({"id": "p", "name": "P", "room": {}, "zone": {}})
    assert "delays" not in plain.as_dict() and plain.matrix("light.a", ["desk"]).delays == {}


def test_store_migration_to_1_10_converts_nothing() -> None:
    data = {"rooms": {"kamar": {"zones": [{"id": "desk", "name": "Desk"}],
                                "brightness": {"light.a": {"room": {"low": 40}, "zones": {"desk": {"low": 70}}}}}}}
    migrated = migrate_store(1, 9, json.loads(json.dumps(data)))
    assert migrated["rooms"]["kamar"]["brightness"] == data["rooms"]["kamar"]["brightness"]
    cfg = LightingConfig.from_dict(migrated)
    assert cfg.room("kamar").brightness["light.a"].delays == {}
    assert cfg.room("kamar").zone("desk").id == "desk"  # stable zone ids, untouched


# ===================================================================== runtime
async def test_delay_on_then_delay_off(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    assert hass.states.get(h.floor).state == "off" and floor_calls(devices, h) == []
    hass.states.async_set(h.desk, "on")
    await hass.async_block_till_done()
    await wait(hass, freezer, 1, sim)
    pend = ctrl.delays.get(h.area, h.floor)
    assert pend is not None and pend.action == "on" and pend.seconds == 3 and pend.cell.column == "desk"
    assert floor_calls(devices, h) == []
    # Visible in every plan (inspector, supervisor) without being started there.
    assert item(ctrl, h).rule["delay"]["action"] == "on"
    await wait(hass, freezer, 4, sim)
    assert pct(hass, h.floor) == 45 and [c[0] for c in floor_calls(devices, h)] == ["light.turn_on"]
    assert ctrl.delays.get(h.area, h.floor) is None
    assert [e["event"] for e in ctrl.delays.status(h.area, h.floor)["history"]][:2] == ["executed", "started"]
    devices.clear()
    hass.states.async_set(h.desk, "off")
    await hass.async_block_till_done()
    await zone_until(hass, freezer, ctrl, h, "desk", False, sim)
    await wait(hass, freezer, 3, sim)
    pend = ctrl.delays.get(h.area, h.floor)
    assert pend is not None and pend.action == "off" and pend.seconds == 15
    assert pct(hass, h.floor) == 45 and floor_calls(devices, h) == []  # held for its Delay Off
    await wait(hass, freezer, 14, sim)
    assert hass.states.get(h.floor).state == "off"
    assert [c[0] for c in floor_calls(devices, h)] == ["light.turn_off"]  # exactly one command
    assert ctrl.runtime[h.area].overrides == {}


async def test_delay_on_cancelled_when_the_zone_ends_first(hass, home, freezer, devices) -> None:
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (120, 0)}})}  # (the zone ends ~30 s after)
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor).action == "on"
    await sensor(hass, freezer, h.desk, "off", settle=0, sim=sim)
    await zone_until(hass, freezer, ctrl, h, "desk", False, sim)
    await wait(hass, freezer, 4, sim)
    assert ctrl.delays.get(h.area, h.floor) is None
    await wait(hass, freezer, 120, sim)
    assert floor_calls(devices, h) == [] and hass.states.get(h.floor).state == "off"
    assert ctrl.delays.status(h.area, h.floor)["history"][0]["event"] == "cancelled"


async def test_reentry_during_delay_off_cancels_the_switch_off(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=6, sim=sim)
    assert pct(hass, h.floor) == 45
    devices.clear()
    await sensor(hass, freezer, h.desk, "off", settle=0, sim=sim)
    await zone_until(hass, freezer, ctrl, h, "desk", False, sim)
    await wait(hass, freezer, 2, sim)
    assert ctrl.delays.get(h.area, h.floor).action == "off"
    await sensor(hass, freezer, h.desk, "on", settle=4, sim=sim)  # back at the desk within 15 s
    assert ctrl.delays.get(h.area, h.floor) is None
    await wait(hass, freezer, 20, sim)
    assert floor_calls(devices, h) == [] and pct(hass, h.floor) == 45  # never switched off and on again


async def test_zero_delays_behave_exactly_as_before(hass, home, freezer, devices) -> None:
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert pct(hass, h.floor) == 45 and not ctrl.delays.pending
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert hass.states.get(h.floor).state == "off" and not ctrl.delays.pending


async def test_brightness_change_is_never_delayed_and_a_new_winning_cell_is_used(hass, home, freezer,
                                                                                  devices) -> None:
    """Seating (20 %, no delay) on; Desk (45 %, Delay On 3 s) then wins: a change between values, at once."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(DESK, SEAT), matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.seat, "on", settle=2, sim=sim)
    assert pct(hass, h.floor) == 20
    devices.clear()
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert pct(hass, h.floor) == 45 and not ctrl.delays.pending
    assert [c[1].get("brightness_pct") for c in floor_calls(devices, h)] == [45]


async def test_pending_on_follows_the_new_winning_cell(hass, home, freezer, devices) -> None:
    """While Desk's Low light Delay On (120 s) waits, the ambient category changes to No light, whose Desk cell (65 %)
    has no delay: the lamp switches on at once with the cell that wins now - never with the stale 45 %."""
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (120, 0)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(DESK, SEAT), matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor).cell.column == "desk"
    await outdoor(hass, freezer, 30, settle=0)  # 0.3 lx outside: No light once it is stable (60 s)
    await wait(hass, freezer, 70, sim)
    assert ctrl.coordinator.occupancy.result(h.area).occupied and ctrl.ambient.status(h.area).key == "none"
    assert pct(hass, h.floor) == 65
    assert [c[1].get("brightness_pct") for c in floor_calls(devices, h)] == [65]


async def test_manual_change_switch_and_vacancy_supersede_a_pending_delay(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=6, sim=sim)
    await sensor(hass, freezer, h.desk, "off", settle=0, sim=sim)
    await zone_until(hass, freezer, ctrl, h, "desk", False, sim)
    await wait(hass, freezer, 2, sim)
    assert ctrl.delays.get(h.area, h.floor).action == "off"
    devices.clear()
    # A person dims the lamp during the Delay Off: respected; no switch-off is sent over it.
    hass.states.async_set(h.floor, "on", {**hass.states.get(h.floor).attributes, "brightness": 200},
                          context=Context(user_id="person"))
    await wait(hass, freezer, 2, sim)
    assert ctrl.delays.get(h.area, h.floor) is None and h.floor in ctrl.runtime[h.area].overrides
    await wait(hass, freezer, 20, sim)
    assert floor_calls(devices, h) == []
    assert ctrl.delays.status(h.area, h.floor)["history"][0]["event"] == "superseded"
    # Resume: applied at once (never delayed), then a pending Delay On ends with the room's vacancy.
    ctrl.async_clear_override(h.area, h.floor)
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.floor).state == "off" and not ctrl.delays.pending
    with_room(ctrl, h.area, brightness={h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (60, 0)}})})
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor).action == "on"
    # A wall switch command for the lamp supersedes it.
    await ctrl._send_for_person(h.area, "turn_on", {h.floor: {"brightness_pct": 30}}, Context(), purpose="switch")
    await wait(hass, freezer, 2, sim)
    assert ctrl.delays.get(h.area, h.floor) is None


async def test_vacancy_and_manual_mood_cancel_pending_delays(hass, home, freezer, devices) -> None:
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (60, 0)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor) is not None
    hass.states.async_set("input_select.main_mood", "Manual", {"options": ["Hygge", "Arbeiði", "Dagur", "Manual"]})
    await wait(hass, freezer, 2, sim)
    assert not ctrl.delays.pending
    await wait(hass, freezer, 70, sim)
    assert floor_calls(devices, h) == []
    hass.states.async_set("input_select.main_mood", "Hygge", {"options": ["Hygge", "Arbeiði", "Dagur", "Manual"]})
    await wait(hass, freezer, 4, sim)
    # (Back from Manual: zones are applied again; the desk is still occupied: Delay On again.)
    for e in (h.desk, h.radar):
        hass.states.async_set(e, "off")
    await hass.async_block_till_done()
    await wait(hass, freezer, 150, sim)
    assert not ctrl.coordinator.occupancy.result(h.area).occupied
    assert not ctrl.delays.pending and hass.states.get(h.floor).state == "off"


async def test_priority_input_supersedes_a_pending_delay(hass, home, freezer, devices) -> None:
    h = home
    hass.states.async_set("binary_sensor.cupboard", "off")
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (60, 0)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices, priority_inputs=(PriorityInput(
        id="cupboard", name="Cupboard", entity_id="binary_sensor.cupboard", enabled=True, targets=(h.floor,),
        brightness_pct=100, debounce=0, release_delay=0),))
    await wait(hass, freezer, 12, sim)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor) is not None
    await sensor(hass, freezer, "binary_sensor.cupboard", "on", settle=4, sim=sim)
    assert pct(hass, h.floor) == 100 and ctrl.delays.get(h.area, h.floor) is None


async def test_supervisor_does_not_correct_a_pending_delay(hass, home, freezer, devices) -> None:
    from custom_components.smart_occupancy.zones import SupervisorSettings

    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (40, 0)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices,
                                supervisor=SupervisorSettings.from_dict({"mode": "automatic", "reconcile_interval": 5}))
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=20, sim=sim)
    model = ctrl.supervisor.reconcile(h.area)
    t = next(x for x in model["targets"] if x["entity_id"] == h.floor)
    assert t["result"] == "waiting" and floor_calls(devices, h) == []
    await wait(hass, freezer, 24, sim)
    assert pct(hass, h.floor) == 45 and len(floor_calls(devices, h)) == 1  # the delay's command only


async def test_offline_light_at_the_end_of_its_delay(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=1, sim=sim)
    hass.states.async_set(h.floor, "unavailable", CT)
    await wait(hass, freezer, 6, sim)
    assert ctrl.delays.get(h.area, h.floor) is None and floor_calls(devices, h) == []
    hass.states.async_set(h.floor, "off", CT)  # back: applied at once (its delay was served)
    await wait(hass, freezer, 4, sim)
    assert pct(hass, h.floor) == 45 and len(floor_calls(devices, h)) == 1


async def test_restart_drops_pending_delays_and_replays_nothing(hass, home, freezer, devices) -> None:
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (30, 15)}})}
    entry, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert ctrl.delays.get(h.area, h.floor) is not None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    await wait(hass, freezer, 60, sim)
    assert not ctrl.delays.pending and floor_calls(devices, h) == []  # nothing applied on start, nothing replayed
    assert ctrl.lighting.room(h.area).brightness[h.floor].delays == {"desk": {"low": (30, 15)}}


async def test_saving_a_matrix_with_delays_sends_nothing(hass, home, freezer, devices, hass_ws_client,
                                                         hass_admin_user) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=DELAYED)
    await occupy(hass, h, freezer, sim)
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await ws.send_json({"id": 1, "type": "smart_occupancy/lighting/brightness/set", "area_id": h.area,
                        "entity_id": h.floor, "matrix": {"room": {"low": 30}, "delays": {
                            "room": {"low": {"on": 5, "off": 20}, "none": {"on": 4}},
                            "desk": {"low": None}}}})
    res = await ws.receive_json()
    assert res["success"], res
    m = ctrl.lighting.room(h.area).brightness[h.floor]
    # (Room none is Off: a delay belongs to an On cell; Desk low was removed with null, Desk none is kept.)
    assert m.delays == {"room": {"low": (5, 20)}, "desk": {"none": (3, 15)}}
    assert res["result"]["matrix"]["delays"] == {"room": {"low": {"on": 5, "off": 20}}, "desk": {"none": {"on": 3,
                                                                                                       "off": 15}}}
    await ws.send_json({"id": 2, "type": "smart_occupancy/lighting/brightness/set", "area_id": h.area,
                        "entity_id": h.floor, "matrix": {"delays": {"nowhere": {"low": {"on": 1}}}}})
    assert not (await ws.receive_json())["success"]
    # Bounded (0-3600 s) by the command's schema (a rejected message is logged as an error by Home Assistant).
    from custom_components.smart_occupancy.brightness_websocket import _MATRIX  # noqa: PLC0415
    import voluptuous as vol  # noqa: PLC0415
    with pytest.raises(vol.Invalid):
        _MATRIX({"delays": {"room": {"low": {"on": 99999}}}})
    assert _MATRIX({"delays": {"room": {"low": {"on": 3600, "off": 0}}}})
    await ws.send_json({"id": 4, "type": "smart_occupancy/lighting/brightness/template", "op": "save",
                        "name": "Desk lamp", "room": {"none": 0, "low": 30, "bright": 0},
                        "zone": {"none": 60, "low": 40, "bright": 0},
                        "delays": {"zone": {"low": {"on": 2, "off": 30}}}})
    res = await ws.receive_json()
    assert res["success"], res
    assert res["result"]["template"]["delays"]["zone"] == {"low": {"on": 2, "off": 30}}
    await wait(hass, freezer, 10, sim)
    # (The room cell changed to 30 % at Low: still nothing is sent until the next trigger.)
    assert floor_calls(devices, h) == []


async def test_deleting_a_zone_removes_its_cells_and_delays(hass, home, freezer, devices, hass_ws_client,
                                                            hass_admin_user, hass_storage) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(DESK, SEAT), matrices=DELAYED)
    refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
    ws = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await ws.send_json({"id": 1, "type": "smart_occupancy/lighting/zone/remove", "area_id": h.area, "zone_id": "desk"})
    assert (await ws.receive_json())["success"]
    m = ctrl.lighting.room(h.area).brightness[h.floor]
    assert "desk" not in m.zones and "desk" not in m.delays and "seating" in m.zones
    # A new zone with the same id starts empty (no value comes back).
    await ws.send_json({"id": 2, "type": "smart_occupancy/lighting/zone/set", "area_id": h.area,
                        "zone": {"name": "Desk", "sensors": [h.desk],
                                 "targets": [{"entity_id": h.floor, "brightness_pct": 100}]}})
    res = await ws.receive_json()
    assert res["success"] and res["result"]["zone"]["id"] == "desk"
    m = ctrl.lighting.room(h.area).brightness[h.floor]
    assert "desk" not in m.zones and "desk" not in m.delays
    await ctrl.store.async_flush()
    await wait(hass, freezer, 12, sim)
    stored = hass_storage["smart_occupancy.lighting"]
    assert stored["minor_version"] == 10
    assert "desk" not in (stored["data"]["rooms"][h.area]["brightness"][h.floor].get("delays") or {})


async def test_deleting_the_lit_zone_hands_the_light_back_at_once(hass, home, freezer, devices) -> None:
    """The lit cell's Delay Off is looked up when the switch-off is decided: a deleted zone has none (review)."""
    h = home
    matrices = {h.floor: replace(DELAYED[h.floor], delays={"desk": {"low": (0, 600)}})}
    _e, ctrl, sim = await start(hass, h, freezer, devices, matrices=matrices)
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=4, sim=sim)
    assert pct(hass, h.floor) == 45
    room = ctrl.lighting.room(h.area)
    with_room(ctrl, h.area, zones=(), brightness={e: m.without_columns([]) for e, m in room.brightness.items()})
    await hass.async_block_till_done()
    await wait(hass, freezer, 6, sim)
    assert hass.states.get(h.floor).state == "off" and not ctrl.delays.pending
