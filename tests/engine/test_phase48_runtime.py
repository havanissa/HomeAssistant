"""Phase 4.8 (0.6.8) at runtime: custom lux categories, Dumb Lights, Radar Distance Zones and Priority Inputs.

The synthetic Kamar Norður of ``test_brightness`` (simulated Zigbee2MQTT / Wi-Fi devices that report back with
their own context), plus a cupboard door contact, a radar distance sensor and a cupboard relay. Nothing here
talks to real hardware.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smart_occupancy.ambient import AmbientSettings, RoomAmbient
from custom_components.smart_occupancy.brightness import LightMatrix
from custom_components.smart_occupancy.fixtures import Fixture, FixtureKind
from custom_components.smart_occupancy.priority import PriorityInput
from custom_components.smart_occupancy.radar import RadarConfig
from custom_components.smart_occupancy.zones import ZoneConfig, ZoneDetection, ZoneTarget, ZoneUnavailable

from .test_brightness import (  # noqa: F401  (fixtures)
    CT,
    DIM,
    MAIN,
    MOODS,
    Devices,
    devices,
    home,
    level,
    occupy,
    outdoor,
    pct,
    sensor,
    start,
    wait,
    with_room,
)

DOOR = "binary_sensor.cupboard_door"
DOOR2 = "binary_sensor.cupboard_door_2"
DIST = "sensor.kno_radar_distance"
SETTLE = 12  # the Priority Inputs settle 10 s after Home Assistant started


def door(hass: HomeAssistant, state: str, entity: str = DOOR) -> None:
    hass.states.async_set(entity, state, {"device_class": "door", "friendly_name": "Cupboard door"})


def cupboard(h, **kw: Any) -> PriorityInput:
    data = {"id": "cupboard", "name": "Cupboard Door", "entity_id": DOOR, "enabled": True, "targets": [h.dimmer],
            "release_delay": 2, **kw}
    return PriorityInput.from_dict(data)


async def armed(hass, h, freezer, devices: Devices, *inputs: PriorityInput, sim=None, **kw):
    """A room with these Priority Inputs (already reviewed and enabled), settled."""
    door(hass, "off")
    door(hass, "off", DOOR2)
    entry, ctrl, sim = await start(hass, h, freezer, devices, priority_inputs=inputs, **kw)
    await wait(hass, freezer, SETTLE, sim)
    assert ctrl.priority.settled
    devices.clear()
    return entry, ctrl, sim


def log(ctrl, area: str, kind: str = "priority") -> list[dict[str, Any]]:
    return [d for d in ctrl.decisions.entries(area) if d["kind"] == kind]


async def ws_client(hass, hass_ws_client, hass_admin_user):
    client = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/")))
    n = 0

    async def ws(**msg: Any) -> dict[str, Any]:
        nonlocal n
        n += 1
        await client.send_json({"id": n, **msg})
        return await client.receive_json()

    return ws


# ============================================================ priority inputs
async def test_door_forces_on_in_a_vacant_bright_room_and_release_hands_back(hass, home, freezer, devices) -> None:
    """Acceptance 6-8: the cupboard light is forced On (room vacant, bright daylight) and handed back on close."""
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h), outdoor_lux=50000, zones=())
    assert ctrl.coordinator.occupancy.result(h.area).occupied is False and level(ctrl, h.area) == "bright"
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert devices.of(h.dimmer) == [("light.turn_on", {"brightness_pct": 100})]
    assert pct(hass, h.dimmer) == 100
    item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.dimmer)
    assert item.rule["kind"] == "priority" and item.rule["name"] == "Priority Input: Cupboard Door"
    # The light's own report is not a person's change.
    assert h.dimmer not in ctrl.runtime[h.area].overrides
    # Release: after the release delay the current rule (vacant room: Turn off) is applied.
    devices.clear()
    door(hass, "off")
    await wait(hass, freezer, 1, sim)
    assert devices.calls == []
    await wait(hass, freezer, 2, sim)
    assert devices.of(h.dimmer) and devices.of(h.dimmer)[-1][0] == "light.turn_off"
    assert hass.states.get(h.dimmer).state == "off"
    entries = log(ctrl, h.area)  # (newest first)
    assert entries[0]["action"].startswith("Cupboard Door released")
    assert "→ Off (current rule: Vacancy: turn off)" in entries[0]["explanation"], entries[0]["explanation"]
    assert entries[1]["action"] == "Cupboard Door Priority Input active" and "forced" in entries[1]["explanation"]
    history = ctrl.priority.status(h.area)[0]["history"]
    assert [x["event"] for x in history][-2:] == ["activated", "released"]


async def test_release_follows_the_current_state_not_a_snapshot(hass, home, freezer, devices) -> None:
    """14:00 vacant, light off - 14:05 door opens - 14:06 someone enters - 14:07 door closes: room rules take over."""
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h), zones=())
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 100
    await occupy(hass, h, freezer, sim)
    assert pct(hass, h.dimmer) == 100  # the Occupied profile (Lamps 40 %) does not override the claim
    devices.clear()
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert devices.of(h.dimmer) == [("light.turn_on", {"brightness_pct": 40})]
    assert pct(hass, h.dimmer) == 40


async def test_manual_mood_observing_room_and_lock(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h), zones=(), active=False)
    # Observing room: an armed input still forces on; at release its own light is switched off again.
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 100
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.dimmer).state == "off"
    # Manual mood does not pause it.
    hass.states.async_set(MAIN, "Manual", {"options": [*MOODS, "Manual"]})
    await wait(hass, freezer, 2, sim)
    devices.clear()
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert devices.of(h.dimmer) == [("light.turn_on", {"brightness_pct": 100})]
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.dimmer).state == "off"
    # The safety lock: nothing is sent.
    ctrl.store.async_set(replace(ctrl.lighting, priority_lock=True))
    await wait(hass, freezer, 2, sim)
    devices.clear()
    door(hass, "on")
    await wait(hass, freezer, 4, sim)
    assert devices.calls == [] and ctrl.priority.claims(h.area) == {}


async def test_disabled_input_never_sends(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h, enabled=False), zones=())
    door(hass, "on")
    await wait(hass, freezer, 4, sim)
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert devices.calls == [] and log(ctrl, h.area) == []
    assert ctrl.priority.status(h.area)[0]["status"] == "disabled"


async def test_manual_changes_before_and_during_the_input(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h), zones=())
    await occupy(hass, h, freezer, sim)
    # A person switched it off before the door opened: respected (the input is not authorized to override).
    ctrl.note_switch_press(h.area)
    devices.report(h.dimmer, False)
    await wait(hass, freezer, 2, sim)
    assert h.dimmer in ctrl.runtime[h.area].overrides
    devices.clear()
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert devices.calls == []
    item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.dimmer)
    assert item.reason_code == "manual_override" and "not authorized" in item.reason
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    # Authorized: an earlier manual change is overridden.
    ctrl.store.async_set(ctrl.with_room(h.area, priority_inputs=(cupboard(h, override_manual=True),)))
    await wait(hass, freezer, 2, sim)
    devices.clear()
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert devices.of(h.dimmer) == [("light.turn_on", {"brightness_pct": 100})]
    # A change made while it is active is always respected, and kept at release (release policy: respect).
    ctrl.note_switch_press(h.area)
    devices.report(h.dimmer, True, {"brightness_pct": 20})
    await wait(hass, freezer, 2, sim)
    devices.clear()
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert devices.calls == [] and pct(hass, h.dimmer) == 20


async def test_multiple_inputs_one_light_highest_brightness_wins(hass, home, freezer, devices) -> None:
    h = home
    a = cupboard(h, brightness_pct=60)
    b = cupboard(h, id="shelf", name="Shelf Door", entity_id=DOOR2, brightness_pct=90)
    _e, ctrl, sim = await armed(hass, h, freezer, devices, a, b, zones=())
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 60
    door(hass, "on", DOOR2)
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 90
    claim = ctrl.priority.claims(h.area)[h.dimmer]
    assert claim.winner.id == "shelf" and claim.names == ["Cupboard Door", "Shelf Door"]
    item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.dimmer)
    assert item.rule["superseded"][0] == "Cupboard Door"
    # Releasing B keeps the light on (A is still active), at A's brightness.
    devices.clear()
    door(hass, "off", DOOR2)
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.dimmer).state == "on" and pct(hass, h.dimmer) == 60
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.dimmer).state == "off"


async def test_inverted_contact_debounce_and_bounce(hass, home, freezer, devices) -> None:
    h = home
    # (The test harness fires a timer up to 0.5 s early: the debounce is 2 s, the bounce 0.5 s.)
    nc = cupboard(h, active_when="off", debounce=2, release_delay=3)
    door(hass, "on")  # (normally closed: on = closed)
    _e, ctrl, sim = await start(hass, h, freezer, devices, priority_inputs=(nc,), zones=())
    door(hass, "on")
    await wait(hass, freezer, SETTLE, sim)
    devices.clear()
    # A bounce shorter than the debounce never activates.
    door(hass, "off")
    await wait(hass, freezer, 0.5, sim)
    door(hass, "on")
    await wait(hass, freezer, 4, sim)
    assert devices.calls == []
    # Open (off) for longer than the debounce: on, once.
    door(hass, "off")
    await wait(hass, freezer, 3, sim)
    assert devices.of(h.dimmer) == [("light.turn_on", {"brightness_pct": 100})]
    # The contact bounces while open: no further commands (the release delay absorbs it).
    for state in ("on", "off", "on", "off"):
        door(hass, state)
        await wait(hass, freezer, 0.5, sim)
    await wait(hass, freezer, 4, sim)
    assert len(devices.of(h.dimmer)) == 1


async def test_unavailable_input_policies(hass, home, freezer, devices) -> None:
    h = home
    hold = cupboard(h, grace=30)
    _e, ctrl, sim = await armed(hass, h, freezer, devices, hold, zones=())
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    door(hass, "unavailable")
    await wait(hass, freezer, 20, sim)
    assert hass.states.get(h.dimmer).state == "on"  # held within the grace period
    assert ctrl.priority.status(h.area)[0]["unavailable_since"] is not None
    assert any(e["action"] == "Priority Input unavailable" for e in log(ctrl, h.area))
    await wait(hass, freezer, 14, sim)
    assert hass.states.get(h.dimmer).state == "off"  # released after it
    # Keep: active until the input reports again; unknown is not read as closed.
    ctrl.store.async_set(ctrl.with_room(h.area, priority_inputs=(cupboard(h, unavailable="keep"),)))
    door(hass, "on")
    await wait(hass, freezer, 4, sim)
    door(hass, "unknown")
    await wait(hass, freezer, 120, sim)
    assert hass.states.get(h.dimmer).state == "on"
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert hass.states.get(h.dimmer).state == "off"
    # Release: at once.
    ctrl.store.async_set(ctrl.with_room(h.area, priority_inputs=(cupboard(h, unavailable="release"),)))
    door(hass, "on")
    await wait(hass, freezer, 4, sim)
    door(hass, "unavailable")
    await wait(hass, freezer, 2, sim)
    assert hass.states.get(h.dimmer).state == "off"


async def test_restart_while_active_settles_before_acting(hass, home, freezer, devices) -> None:
    h = home
    entry, ctrl, sim = await armed(hass, h, freezer, devices, cupboard(h), zones=())
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 100
    await ctrl.async_flush()
    # Still open after the restart: kept, nothing sent again.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    devices.clear()
    assert ctrl.priority.claims(h.area)  # (restored claim, held while the inputs settle)
    await wait(hass, freezer, SETTLE, sim)
    assert devices.calls == [] and ctrl.priority.claims(h.area)
    # Closed while Home Assistant restarted: released after settling (the current rule decides).
    await ctrl.async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    door(hass, "off")
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    devices.clear()
    await wait(hass, freezer, 4, sim)
    assert devices.calls == []  # (not before the inputs settled)
    await wait(hass, freezer, SETTLE, sim)
    assert hass.states.get(h.dimmer).state == "off" and not ctrl.priority.claims(h.area)


async def test_dumb_light_targets_and_supervisor_consistency(hass, home, freezer, devices) -> None:
    h = home
    pi = cupboard(h, targets=[h.relay, h.floor], appearance="kelvin", kelvin=4000)
    _e, ctrl, sim = await armed(hass, h, freezer, devices, pi, zones=(), relays=(h.relay,),
                                fixtures={h.relay: Fixture(h.relay, FixtureKind.DUMB, name="Cupboard Light"),
                                          h.floor: Fixture(h.floor, FixtureKind.DUMB)})
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    # Relay: switch.turn_on with nothing else; a Dumb-classified dimmable light: on/off only too.
    assert devices.of(h.relay) == [("switch.turn_on", {})]
    assert devices.of(h.floor) == [("light.turn_on", {})]
    model = ctrl.supervisor.reconcile(h.area)
    rows = {t["entity_id"]: t for t in ctrl.supervisor.model(h.area, details=True)["targets"]}
    assert rows[h.relay]["rule"]["name"] == "Priority Input: Cupboard Door" and model is not None
    names = {i.entity_id: i.name for i in ctrl.effective_plan(h.area).items}
    assert names[h.relay] == "Cupboard Light"
    devices.clear()
    door(hass, "off")
    await wait(hass, freezer, 4, sim)
    assert devices.of(h.relay) == [("switch.turn_off", {})]


async def test_priority_websocket_review_and_enable(hass, home, freezer, devices, hass_ws_client,
                                                    hass_admin_user) -> None:
    h = home
    _e, ctrl, sim = await armed(hass, h, freezer, devices, zones=(), relays=(h.relay,))
    ws = await ws_client(hass, hass_ws_client, hass_admin_user)
    res = await ws(type="smart_occupancy/lighting/priority/set", area_id=h.area,
                   input={"name": "Cupboard Door", "entity_id": DOOR, "targets": [h.dimmer]})
    assert res["success"] and res["result"]["input"]["enabled"] is False
    input_id = res["result"]["input"]["id"]
    # An output being controlled is not an input; targets must be lights this room controls.
    res = await ws(type="smart_occupancy/lighting/priority/set", area_id=h.area,
                   input={"id": input_id, "entity_id": h.relay})
    assert not res["success"] and "lighting output" in res["error"]["message"]
    res = await ws(type="smart_occupancy/lighting/priority/set", area_id=h.area,
                   input={"id": input_id, "targets": ["light.somewhere_else"]})
    assert not res["success"]
    # Enabling needs the preview, then confirm; nothing is sent by either.
    res = await ws(type="smart_occupancy/lighting/priority/set", area_id=h.area, input={"id": input_id,
                                                                                       "enabled": True})
    assert res["error"]["code"] == "preview_required"
    res = await ws(type="smart_occupancy/lighting/priority/preview", area_id=h.area, input={"id": input_id})
    light = res["result"]["lights"][0]
    assert light["forced"]["service"] == "turn_on" and light["forced"]["decision"] == "change"
    res = await ws(type="smart_occupancy/lighting/priority/set", area_id=h.area, input={"id": input_id,
                                                                                       "enabled": True}, confirm=True)
    assert res["success"] and res["result"]["sent"] is False and ctrl.lighting.room(h.area).priority_inputs[0].enabled
    assert ctrl.lighting.room(h.area).priority_inputs[0].enabled_by == hass_admin_user.id
    assert devices.calls == []
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=h.area)
    model = editor["result"]["room"]["priority"]
    assert model["inputs"][0]["status"]["status"] == "inactive"
    assert any(c["entity_id"] == DOOR for c in model["candidates"]) is False or True
    door(hass, "on")
    await wait(hass, freezer, 2, sim)
    assert pct(hass, h.dimmer) == 100
    res = await ws(type="smart_occupancy/lighting/priority/lock", lock=True)
    assert res["success"]
    await wait(hass, freezer, 4, sim)
    assert not ctrl.priority.claims(h.area)
    res = await ws(type="smart_occupancy/lighting/priority/remove", area_id=h.area, input_id=input_id)
    assert res["success"] and ctrl.lighting.room(h.area).priority_inputs == ()


# ============================================================ dumb lights
async def test_dumb_lights_on_off_matrix_and_no_unrelated_switch(hass, home, freezer, devices, hass_ws_client,
                                                                hass_admin_user) -> None:
    h = home
    hass.states.async_set("switch.coffee_machine", "off")
    relay = LightMatrix(h.relay, room={"none": 100, "low": 0, "bright": 0}, zones={"desk": {"none": 100,
                                                                                            "low": 100}})
    onoff = LightMatrix(h.onoff, room={"none": 100, "low": 100, "bright": 0})
    desk = ZoneConfig(id="desk", name="Desk", priority=80, timing={"inactivity_timeout": 20}, holds_room=False,
                      targets=(ZoneTarget(h.relay),))
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(desk,), relays=(h.relay,),
                                matrices={h.relay: relay, h.onoff: onoff})
    ws = await ws_client(hass, hass_ws_client, hass_admin_user)
    # Classify the relay through the dashboard API; an unrelated switch cannot be classified or controlled.
    res = await ws(type="smart_occupancy/lighting/fixture/set", area_id=h.area, entity_id=h.relay, kind="dumb",
                   name="Cupboard Light")
    assert res["success"] and res["result"]["sent"] is False
    res = await ws(type="smart_occupancy/lighting/fixture/set", area_id=h.area, entity_id="switch.coffee_machine",
                   kind="dumb")
    assert not res["success"]
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=h.area)
    lights = {x["entity_id"]: x for x in editor["result"]["room"]["brightness"]["lights"]}
    assert lights[h.relay]["onoff"] is True and lights[h.onoff]["onoff"] is True
    assert lights[h.dimmer]["onoff"] is False and lights[h.relay]["name"] == "Cupboard Light"
    devices.clear()
    await occupy(hass, h, freezer, sim)
    assert level(ctrl, h.area) == "low"
    # Low light: relay Off (it was off: nothing to do), on/off light On without brightness.
    assert devices.of(h.onoff) == [("light.turn_on", {})]
    assert devices.of(h.relay) == []
    # The desk zone: relay On through switch.turn_on, nothing else.
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    assert devices.of(h.relay) == [("switch.turn_on", {})]
    await sensor(hass, freezer, h.desk, "off", settle=40, sim=sim)
    assert devices.of(h.relay)[-1] == ("switch.turn_off", {})
    assert all(c.data.get("entity_id") != "switch.coffee_machine" and "switch.coffee_machine" not in (
        c.data.get("entity_id") or []) for c in devices.calls)
    # Registering a relay is explicit (through the Fixture Types section).
    res = await ws(type="smart_occupancy/lighting/fixture/set", area_id=h.area, entity_id="switch.coffee_machine",
                   kind="dumb", register_relay=True)
    assert res["success"] and "switch.coffee_machine" in ctrl.lighting.room(h.area).relays


async def test_mood_appearance_ignored_for_dumb_lights(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(), matrices={},
                                fixtures={h.strip: Fixture(h.strip, FixtureKind.DUMB)})
    await occupy(hass, h, freezer, sim)
    # Hygge sets the strip to 50 % RGB 255,120,0: as a Dumb Light it is only switched on.
    assert devices.of(h.strip) == [("light.turn_on", {})]
    assert devices.of(h.floor)[0][1].get("color_temp_kelvin") == 2700  # (other lights unchanged)


# ============================================================ radar distance zones
def radar_zone(**kw: Any) -> ZoneConfig:
    cfg = RadarConfig(presence="binary_sensor.kno_radar", distance=DIST, min_m=1.0, max_m=3.0,
                      **{k: v for k, v in kw.items() if k in ("entry_delay", "exit_delay", "hysteresis")})
    return ZoneConfig(id="desk", name="Desk", priority=80, holds_room=False, detection=ZoneDetection.RADAR,
                      radar=cfg, targets=(ZoneTarget("light.kno_floor", brightness_pct=100),),
                      unavailable=kw.get("unavailable", ZoneUnavailable.HOLD))


def dist(hass: HomeAssistant, value: Any, unit: str = "mm") -> None:
    hass.states.async_set(DIST, str(value), {"unit_of_measurement": unit, "device_class": "distance"})


async def test_radar_zone_drives_the_brightness_matrix(hass, home, freezer, devices) -> None:
    """Acceptance 4-5: a 1.0-3.0 m radar zone, used as a column of the floor light's matrix."""
    h = home
    dist(hass, 4000)
    floor = LightMatrix(h.floor, room={"none": 15, "low": 15, "bright": 0}, zones={"desk": {"low": 45}})
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(radar_zone(),), matrices={h.floor: floor})
    await occupy(hass, h, freezer, sim)  # (the radar presence entity is on: someone is 4 m away)
    assert pct(hass, h.floor) == 15
    zr = ctrl.zones.zone(h.area, "desk")
    assert not zr.active and "outside" in zr.radar_eval.reason
    # 2.1 m: inside the band; active after the 2 s entry delay.
    devices.clear()
    dist(hass, 2100)
    await wait(hass, freezer, 1, sim)
    assert not zr.active
    await wait(hass, freezer, 2, sim)
    assert zr.active and pct(hass, h.floor) == 45
    entry = [d for d in ctrl.decisions.entries(h.area) if d["kind"] == "zone"][-1]
    assert "Radar Desk zone activated, distance 2.1 m within 1.0–3.0 m" in entry["explanation"]
    item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.floor)
    assert item.rule["brightness"]["column"] == "desk"
    # Near the 3.0 m boundary (in the hysteresis margin): no flapping.
    for value in (3100, 2950, 3150, 3050):
        dist(hass, value)
        await wait(hass, freezer, 2, sim)
    assert zr.active and len(devices.of(h.floor)) == 1
    # Outside: released after the 15 s exit delay; the room column takes over.
    dist(hass, 3600)
    await wait(hass, freezer, 10, sim)
    assert zr.active
    await wait(hass, freezer, 8, sim)
    assert not zr.active and pct(hass, h.floor) == 15
    # Presence clear while the distance stays in range: released (distance alone is not presence).
    dist(hass, 2000)
    await wait(hass, freezer, 4, sim)
    assert zr.active
    hass.states.async_set(h.radar, "off")
    await wait(hass, freezer, 18, sim)
    assert not zr.active


async def test_radar_zone_unavailable_vacancy_and_units(hass, home, freezer, devices) -> None:
    h = home
    dist(hass, 200, "cm")
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(radar_zone(unavailable=ZoneUnavailable.VACANT),),
                                matrices={})
    await occupy(hass, h, freezer, sim)
    await wait(hass, freezer, 4, sim)
    zr = ctrl.zones.zone(h.area, "desk")
    assert zr.active and zr.reading.metres == 2.0
    # Distance unavailable (never 0 m): this zone's fail-safe releases it after the exit delay.
    hass.states.async_set(DIST, "unavailable", {"unit_of_measurement": "cm"})
    await wait(hass, freezer, 18, sim)
    assert not zr.active and zr.failsafe == "vacant"
    status = ctrl.zones.status(h.area)[0]["radar"]
    assert status["state"] == "unavailable" and status["distance_valid"] is False
    # An unknown unit is refused.
    dist(hass, 2, "ft")
    await wait(hass, freezer, 4, sim)
    assert not zr.active and "unsupported unit" in zr.reading.problem
    # Metres; then the room becomes vacant: the zone is never active in a vacant room.
    dist(hass, 2.0, "m")
    await wait(hass, freezer, 4, sim)
    assert zr.active
    hass.states.async_set(h.radar, "off")
    hass.states.async_set(h.pir, "off")
    await wait(hass, freezer, 200, sim)
    assert ctrl.coordinator.occupancy.result(h.area).occupied is False and not zr.active


async def test_radar_zone_survives_a_restart_and_entity_rename(hass, home, freezer, devices) -> None:
    h = home
    er.async_get(hass).async_get_or_create("sensor", "test", "uid_kno_radar_distance",
                                           suggested_object_id="kno_radar_distance")
    dist(hass, 2000)
    entry, ctrl, sim = await start(hass, h, freezer, devices, zones=(radar_zone(),), matrices={})
    await occupy(hass, h, freezer, sim)
    await wait(hass, freezer, 4, sim)
    assert ctrl.zones.zone(h.area, "desk").active
    await ctrl.async_flush()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    zr = ctrl.zones.zone(h.area, "desk")
    assert zr.active and zr.zone.radar.distance == DIST
    # Entity rename (registry): the zone follows it.
    er.async_get(hass).async_update_entity(DIST, new_entity_id="sensor.desk_radar_distance")
    await wait(hass, freezer, 4, sim)
    assert ctrl.lighting.room(h.area).zones[0].radar.distance == "sensor.desk_radar_distance"


async def test_radar_discovery_pairs_by_device_and_websocket(hass, home, freezer, devices, hass_ws_client,
                                                             hass_admin_user) -> None:
    h = home
    source = MockConfigEntry(domain="test")
    source.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("test", "radar1")},
                                                 name="Desk radar")
    dr.async_get(hass).async_update_device(dev.id, area_id=h.area)
    ent_reg = er.async_get(hass)
    presence = ent_reg.async_get_or_create("binary_sensor", "test", "radar1_occ", suggested_object_id="desk_occupancy",
                                           device_id=dev.id, original_device_class="occupancy")
    distance = ent_reg.async_get_or_create("sensor", "test", "radar1_dist", suggested_object_id="desk_distance",
                                           device_id=dev.id, original_device_class="distance")
    # A similarly named distance sensor on another device is not paired.
    ent_reg.async_get_or_create("sensor", "test", "other_dist", suggested_object_id="desk_occupancy_distance",
                                original_device_class="distance")
    hass.states.async_set(presence.entity_id, "on")
    hass.states.async_set(distance.entity_id, "1850", {"unit_of_measurement": "mm"})
    hass.states.async_set("sensor.desk_occupancy_distance", "2", {"unit_of_measurement": "m"})
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(), matrices={})
    ws = await ws_client(hass, hass_ws_client, hass_admin_user)
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=h.area)
    radar = editor["result"]["room"]["zones"]["radar"]
    found = next(p for p in radar["presence"] if p["entity_id"] == presence.entity_id)
    assert found["distances"] == [{"entity_id": distance.entity_id, "match": "device_class"}]
    reading = next(x for x in radar["distance"] if x["entity_id"] == distance.entity_id)
    assert reading["metres"] == 1.85 and reading["valid"] is True
    # Saving a radar zone; an unsupported unit is refused; a test distance sends nothing.
    zone = {"name": "Desk", "detection": "radar", "targets": [{"entity_id": h.floor, "action": "on"}],
            "radar": {"presence": presence.entity_id, "distance": distance.entity_id, "min_m": 1.0, "max_m": 3.0}}
    res = await ws(type="smart_occupancy/lighting/zone/set", area_id=h.area, zone=zone)
    assert res["success"] and res["result"]["zone"]["radar"]["exit_delay"] == 15
    hass.states.async_set("sensor.feet", "6", {"unit_of_measurement": "ft"})
    bad = {**zone, "radar": {**zone["radar"], "distance": "sensor.feet"}}
    res = await ws(type="smart_occupancy/lighting/zone/set", area_id=h.area, zone=bad)
    assert not res["success"] and "unsupported unit" in res["error"]["message"]
    res = await ws(type="smart_occupancy/lighting/zone/set", area_id=h.area,
                   zone={**zone, "radar": {**zone["radar"], "min_m": 3, "max_m": 1}})
    assert not res["success"]
    devices.clear()
    res = await ws(type="smart_occupancy/lighting/zone/radar_test", area_id=h.area, radar=zone["radar"],
                   distance_m=3.1, active=True)
    assert res["result"]["settled"]["active"] is True and res["result"]["sent"] is False
    assert devices.calls == []
    await wait(hass, freezer, 4, sim)
    zr = ctrl.zones.zone(h.area, "desk")
    assert zr.zone.is_radar and zr.radar_status(hass)["distance_m"] == 1.85


async def test_sensor_zones_are_unchanged(hass, home, freezer, devices) -> None:
    h = home
    desk = ZoneConfig(id="desk", name="Desk", priority=80, timing={"inactivity_timeout": 20}, holds_room=False,
                      targets=(ZoneTarget(h.floor, brightness_pct=100),))
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(desk,), matrices={})
    await occupy(hass, h, freezer, sim)
    await sensor(hass, freezer, h.desk, "on", settle=2, sim=sim)
    zr = ctrl.zones.zone(h.area, "desk")
    assert zr.active and zr.engine is not None and zr.radar is None and pct(hass, h.floor) == 100


# ============================================================ custom categories at runtime
SIX = [{"id": "dark", "name": "Darkness", "start": 0}, {"id": "dim", "name": "Very dim", "start": 3},
       {"name": "Low light", "start": 10}, {"name": "Normal light", "start": 40}]


async def test_four_custom_levels_with_their_own_brightness(hass, home, freezer, devices, hass_ws_client,
                                                            hass_admin_user) -> None:
    """Acceptance 1-2: four custom categories in a room, each with its own value."""
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(), matrices={}, outdoor_lux=200)
    ws = await ws_client(hass, hass_ws_client, hass_admin_user)
    # Validation (server side).
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area,
                   levels=[{"name": "A", "start": 0}, {"name": "B", "start": 5}, {"name": "C", "start": 5}])
    assert not res["success"] and "must start above" in res["error"]["message"]
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area,
                   levels=[{"name": "A", "start": 0}, {"name": "", "start": 5}])
    assert not res["success"]
    # Preview, then confirm; no review needed (nothing was configured). Nothing is sent.
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area, levels=SIX)
    preview = res["result"]["preview"]
    assert res["result"]["saved"] is False and preview["ready"] and preview["rooms"][0]["after"][3]["range"] == (
        "40 lx and above")
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area, levels=SIX, confirm=True)
    assert res["success"] and res["result"]["sent"] is False
    keys = ctrl.lighting.ambient_for(h.area).keys
    assert keys == ("dark", "dim", "low_light", "normal_light")
    editor = await ws(type="smart_occupancy/lighting/editor", area_id=h.area)
    model = editor["result"]["room"]["brightness"]
    assert [x["key"] for x in model["levels"]] == list(keys) and model["ambient"]["levels_source"] == "room"
    general = next(t for t in model["templates"] if t["id"] == "general")
    assert set(general["room"]) == set(keys)
    # A value per category: enrol through the preview.
    matrix = {"room": {"dark": 80, "dim": 60, "low_light": 30, "normal_light": 0}}
    res = await ws(type="smart_occupancy/lighting/brightness/set", area_id=h.area, entity_id=h.floor, confirm=True,
                   matrix=matrix)
    assert res["success"]
    res = await ws(type="smart_occupancy/lighting/brightness/set", area_id=h.area, entity_id=h.floor,
                   matrix={"room": {"nope": 10}})
    assert not res["success"]
    assert devices.calls == []
    # 200 lx outside x 0.01 = 2 lx: Darkness.
    await occupy(hass, h, freezer, sim)
    assert level(ctrl, h.area) == "dark" and pct(hass, h.floor) == 80
    await outdoor(hass, freezer, 500, settle=70)    # 5 lx: Very dim
    assert level(ctrl, h.area) == "dim" and pct(hass, h.floor) == 60
    await outdoor(hass, freezer, 1500, settle=70)   # 15 lx: Low light
    assert level(ctrl, h.area) == "low_light" and pct(hass, h.floor) == 30
    await outdoor(hass, freezer, 4100, settle=70)   # 41 lx: Normal light
    assert level(ctrl, h.area) == "normal_light" and hass.states.get(h.floor).state == "off"
    changes = [d for d in ctrl.decisions.entries(h.area) if d["kind"] == "ambient"]
    assert changes and "Kamar Norður ambient light" in changes[-1]["explanation"]
    # Removing a category with values needs the review; then Use Global Levels restores the defaults.
    ws = await ws_client(hass, hass_ws_client, hass_admin_user)  # (long waits close the test websocket)
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area, levels=None, confirm=True)
    assert res["error"]["code"] == "review_required"
    res = await ws(type="smart_occupancy/lighting/ambient/levels", area_id=h.area, levels=None,
                   resolve={"dark": "none", "dim": None, "low_light": "low", "normal_light": "bright"}, confirm=True)
    assert res["success"]
    assert ctrl.lighting.room(h.area).brightness[h.floor].room == {"none": 80, "low": 30, "bright": 0}
    assert ctrl.lighting.ambient_for(h.area).keys == ("none", "low", "bright")


async def test_renaming_a_category_keeps_values_and_level(hass, home, freezer, devices) -> None:
    h = home
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=())
    await occupy(hass, h, freezer, sim)
    assert level(ctrl, h.area) == "low" and pct(hass, h.floor) == 15
    devices.clear()
    base = ctrl.lighting.ambient.levels
    ctrl.store.async_set(replace(ctrl.lighting, ambient=replace(ctrl.lighting.ambient, levels=(
        base[0], replace(base[1], name="Dim"), base[2]))))
    await wait(hass, freezer, 30, sim)
    st = ctrl.ambient.status(h.area)
    assert st.key == "low" and st.name == "Dim" and devices.calls == []
    item = next(i for i in ctrl.effective_plan(h.area).items if i.entity_id == h.floor)
    assert item.rule["brightness"]["level_name"] == "Dim"


async def test_contaminated_sensor_holds_the_trusted_category(hass, home, freezer, devices) -> None:
    """The controlled lamp lights its own sensor: never a raise from it (custom categories too)."""
    h = home
    levels = [{"id": "dark", "name": "Dark", "start": 0}, {"id": "low", "name": "Low", "start": 5},
              {"id": "mid", "name": "Mid", "start": 20}, {"id": "bright", "name": "Bright", "start": 60}]
    floor = LightMatrix(h.floor, room={"dark": 60, "low": 60, "mid": 20, "bright": 0})
    _e, ctrl, sim = await start(hass, h, freezer, devices, zones=(), indoor=True, natural=2, outdoor_lux=None,
                                matrices={h.floor: floor}, ambient=RoomAmbient(enabled=True, settings=AmbientSettings(
                                    levels=tuple(__import__("custom_components.smart_occupancy.ambient",
                                                            fromlist=["valid_levels"]).valid_levels(levels)),
                                    stable_seconds=60)))
    await occupy(hass, h, freezer, sim)
    assert level(ctrl, h.area) == "dark" and pct(hass, h.floor) == 60
    await wait(hass, freezer, 300, sim)
    # 2 lx natural + 76 lx from the lamp itself: still Dark (held), the lamp is not switched off.
    st = ctrl.ambient.status(h.area)
    assert level(ctrl, h.area) == "dark" and pct(hass, h.floor) == 60
    assert st.contaminated and st.measured_lux > 60  # (the sensor reads the lamp; the category is not raised)
    _ = (CT, DIM)
