"""Phase A4 parity scenarios (2): switches, possible occupancy, daylight, Vacant profiles, Manual mood, conflicts.

See ``test_lighting_parity.py``: each scenario is recorded and compared with its fixture; the App replays it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch
import uuid

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_mqtt_message

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import VacancyAction
from custom_components.smart_occupancy.daylight import IndoorSensor, RoomDaylight
from custom_components.smart_occupancy.preliminary import PreliminaryTarget

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_daylight_runtime import LUX, set_lux, start as daylight_start
from .test_lighting_parity import START, TIMING, begin, configure, devices, finish, user_context, ws_admin  # noqa: F401
from .test_lighting_runtime import MAIN_OPTS, lit, options as lit_options, set_moods  # noqa: F401
from .test_preliminary import prelim
from .test_switches import IEEE, NAME, TOPIC, WS, bridge, mqtt, wiser_device  # noqa: F401
from .test_vacant_profiles import VACANT


async def _switch(hass, ws: WS, area: str, freezer, mappings: dict[str, Any], **add: Any) -> str:
    # A fixed switch id: the recording is the same on every run.
    with patch("custom_components.smart_occupancy.switch_websocket.uuid.uuid4",
               return_value=uuid.UUID("0123456789abcdef0123456789abcdef")):
        reply = await ws(type="smart_occupancy/lighting/switch/add", area_id=area, **add)
    assert reply["success"], reply
    switch_id = reply["result"]["switch_id"]
    for raw, mapping in mappings.items():
        reply = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=area, switch_id=switch_id, raw=raw,
                         mapping=mapping)
        assert reply["success"], (raw, reply)
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=area, switch_id=switch_id, mode="control",
                     confirm=True))["success"]
    await lp.advance(hass, freezer, 3)
    return switch_id


async def test_switch_event_entity(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                   hass_admin_user) -> None:
    freezer.move_to(START)
    source = MockConfigEntry(domain="test_remote")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("hue", "dimmer")},
                                                    name="Hue dimmer", manufacturer="Signify", model="RWL022")
    entity = er.async_get(hass).async_get_or_create("event", "test_remote", "dimmer_button", device_id=device.id,
                                                    suggested_object_id="hue_dimmer_action").entity_id
    types = ["on_press", "off_press", "up_press", "down_press"]
    hass.states.async_set(entity, "unknown", {"event_types": types, "device_class": "button"})
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15)
    await _switch(hass, WS(ws), lit.area, freezer, {
        "off_press": {"action": "off"}, "on_press": {"action": "on"},
        "up_press": {"action": "brightness_up"}, "down_press": {"action": "brightness_down"}}, device_id=device.id)
    rec = await begin(hass, freezer, entry, ws, devices, "switch_event_entity",
                      "A remote (event entity, no MQTT): off, on, brighter, darker; the lights it set are overrides")
    for action in ("off_press", "on_press", "up_press", "up_press", "down_press"):
        hass.states.async_set(entity, dt_util.utcnow().isoformat(), {"event_type": action, "event_types": types})
        await lp.advance(hass, freezer, 3, rec)
    set_moods(hass, "Nátt", "Auto", "Nátt")
    await lp.advance(hass, freezer, 5, rec)
    await finish(rec)


async def test_switch_zigbee2mqtt(hass: HomeAssistant, mqtt, lit, devices, freezer, hass_ws_client,
                                  hass_admin_user) -> None:
    freezer.move_to(START)
    device_id = wiser_device(hass, lit.area)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    assert (await WS(ws)(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area))["success"]
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge(), retain=True)
    await hass.async_block_till_done()
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    await _switch(hass, WS(ws), lit.area, freezer, {"on_left": {"action": "on"}, "off_left": {"action": "off"},
                                                    "on_right": {"action": "toggle", "target": lit.lamp}},
                  device_id=device_id)
    rec = await begin(hass, freezer, entry, ws, devices, "switch_zigbee2mqtt",
                      "A Zigbee2MQTT wall switch through Home Assistant's MQTT: on, toggle one lamp, off")
    for action in ("on_left", "on_right", "on_right", "off_left"):
        async_fire_mqtt_message(hass, TOPIC, f'{{"action": "{action}", "battery": 100}}')
        await lp.advance(hass, freezer, 3, rec)
    await finish(rec)


async def test_possible_occupancy(hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    entry, ctrl = await daylight_start(hass, lit, freezer, lux=10)
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    configure(ctrl, lit.area, preliminary=prelim(PreliminaryTarget(lit.lamp, 10, 2200, 0.5)))
    await lp.advance(hass, freezer, 3)
    rec = await begin(hass, freezer, entry, ws, devices, "possible_occupancy",
                      "Motion alone: the lamp dimly on, unconfirmed it goes off; then a confirmed visit takes over")
    hass.states.async_set(lit.pir, "on")
    await lp.advance(hass, freezer, 4, rec)
    hass.states.async_set(lit.pir, "off")
    await lp.advance(hass, freezer, 90, rec)
    hass.states.async_set(lit.pir, "on")
    await lp.advance(hass, freezer, 3, rec)
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 25, rec)
    await finish(rec)


async def test_daylight_became_dark(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                    hass_admin_user) -> None:
    entry, ctrl = await daylight_start(hass, lit, freezer, lux=900,
                                       daylight=RoomDaylight(sensors=(IndoorSensor(LUX),), stable_seconds=5))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    rec = await begin(hass, freezer, entry, ws, devices, "daylight_became_dark",
                      "Bright room: arrival switches nothing on; when it becomes dark the mood is applied")
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 20, rec)
    for lux in (400, 120, 40, 8, 5):
        set_lux(hass, lux)
        await lp.advance(hass, freezer, 15, rec)
    await lp.advance(hass, freezer, 30, rec)
    await finish(rec)


async def test_vacant_profile(hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    from custom_components.smart_occupancy.lighting_config import RoomVacancy as Vacancy  # noqa: PLC0415

    freezer.move_to(START)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True, daylight=RoomDaylight.from_dict({"auto_on": "always"}),
              vacancy=Vacancy(VacancyAction.VACANT_MOOD), vacant_overrides=VACANT)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15)
    rec = await begin(hass, freezer, entry, ws, devices, "vacant_profile",
                      "Vacancy action Vacant mood; a mood change while vacant uses the Vacant profile; a new visit")
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await lp.advance(hass, freezer, 120, rec, step=2)
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await lp.advance(hass, freezer, 5, rec)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15, rec)
    await finish(rec)


async def test_manual_mood_pauses(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                  hass_admin_user) -> None:
    freezer.move_to(START)
    opts = [*MAIN_OPTS, "Manual"]
    set_moods(hass, "Dagur", "Auto", "Dagur", main_opts=opts, manual_opts=opts)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    rec = await begin(hass, freezer, entry, ws, devices, "manual_mood",
                      "Main Mood Manual pauses automatic lighting (occupancy and mood); leaving Manual applies again")
    set_moods(hass, "Dagur", "Manual", "Manual", main_opts=opts, manual_opts=opts)
    await lp.advance(hass, freezer, 3, rec)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15, rec)
    set_moods(hass, "Dagur", "Hygge", "Hygge", main_opts=opts, manual_opts=opts)
    await lp.advance(hass, freezer, 8, rec)
    await finish(rec)


async def test_automation_conflict(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                   hass_admin_user) -> None:
    freezer.move_to(START)
    hass.states.async_set("input_boolean.evening", "off")
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "lamp_evening", "alias": "Lamp in the evening",
         "triggers": [{"trigger": "state", "entity_id": "input_boolean.evening", "to": "on"}],
         "actions": [{"action": "light.turn_on", "target": {"entity_id": lit.lamp}, "data": {"brightness_pct": 40}}]},
        {"id": "desk_scene", "alias": "Desk scene", "triggers": [{"trigger": "event", "event_type": "nothing"}],
         "actions": [{"choose": [{"conditions": [{"condition": "state", "entity_id": "input_boolean.evening",
                                                  "state": "on"}],
                                  "sequence": [{"action": "light.turn_off", "target": {"entity_id": lit.desk}}]}]}]},
    ]})
    await hass.async_block_till_done()
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    rec = await begin(hass, freezer, entry, ws, devices, "automation_conflict",
                      "Lights that enabled automations control are left alone (also inside choose)")
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15, rec)
    set_moods(hass, "Hygge", "Auto", "Hygge")
    await lp.advance(hass, freezer, 6, rec)
    await finish(rec)
