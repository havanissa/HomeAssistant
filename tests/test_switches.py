"""Wall switches and remotes (Phase 0.3.5) in a running Home Assistant with the real MQTT integration.

Simulated real-world sequences of a Schneider Electric Wiser wireless switch
(``avb_ksu``) paired with Zigbee2MQTT. The switch, the broker and the lights
are simulated (MQTT messages through Home Assistant's MQTT integration, mocked
light services): no physical Wiser hardware was used.
"""

from __future__ import annotations

from datetime import timedelta
import json
import logging
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import SwitchMode

from .conftest import setup_entry
from .test_lighting_runtime import CT, MAIN, MANUAL, Calls, Lit, activate, advance, occupy, options, set_moods
from .test_lighting_runtime import calls, lit  # noqa: F401  (fixtures)

WS_LOGGER = "homeassistant.components.websocket_api.http.connection"
IEEE = "0x000b57fffe8a1b2c"
NAME = "avb_ksu"
TOPIC = f"zigbee2mqtt/{NAME}"
WISER_ACTIONS = ["on_left", "off_left", "on_right", "off_right", "brightness_move_up_left", "brightness_move_down_left",
                 "brightness_stop_left", "brightness_move_up_right", "brightness_move_down_right",
                 "brightness_stop_right"]


def bridge(name: str = NAME, *, bindings: list[dict[str, Any]] | None = None, include: bool = True) -> str:
    devices: list[dict[str, Any]] = [{"ieee_address": "0x00124b0000000001", "type": "Coordinator",
                                      "friendly_name": "Coordinator"}]
    if include:
        devices.append({
            "ieee_address": IEEE, "type": "EndDevice", "friendly_name": name, "power_source": "Battery",
            "manufacturer": "Schneider Electric", "model_id": "FLS/AIRLINK/4",
            "definition": {"model": "WDE002906", "vendor": "Schneider Electric",
                           "description": "Wiser wireless switch 2-gang",
                           "exposes": [{"type": "enum", "name": "action", "property": "action", "values": WISER_ACTIONS},
                                       {"type": "numeric", "name": "battery", "property": "battery"}]},
            "endpoints": {"21": {"bindings": [{"cluster": "genOnOff", "target": {
                "type": "endpoint", "ieee_address": "0x00124b0000000001", "endpoint": 1}}, *(bindings or [])]}},
        })
    return json.dumps(devices)


def press(hass: HomeAssistant, action: str, *, topic: str = TOPIC, retain: bool = False, **extra: Any) -> None:
    async_fire_mqtt_message(hass, topic, json.dumps({"action": action, "battery": 100, "linkquality": 120, **extra}),
                            retain=retain)


async def settle(hass: HomeAssistant, freezer, seconds: float = 0.1) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)
    await advance(hass, freezer, seconds)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.fixture
async def mqtt(hass: HomeAssistant, mqtt_mock):
    """Home Assistant's MQTT integration (mocked broker)."""
    return mqtt_mock


def wiser_device(hass: HomeAssistant, area_id: str | None = None) -> str:
    """The Zigbee2MQTT device as Home Assistant's MQTT discovery registers it."""
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=mqtt_entry.entry_id, identifiers={("mqtt", f"zigbee2mqtt_{IEEE}")}, name=NAME,
        manufacturer="Schneider Electric", model="Wiser wireless switch 2-gang (WDE002906)")
    if area_id:
        dr.async_get(hass).async_update_device(device.id, area_id=area_id)
    return device.id


class WS:
    def __init__(self, client) -> None:
        self.client = client

    async def __call__(self, **msg: Any) -> dict[str, Any]:
        await self.client.send_json_auto_id(msg)
        reply = await self.client.receive_json()
        while reply.get("type") == "event":
            reply = await self.client.receive_json()
        return reply


def room_switches(ctrl, area: str) -> list[dict[str, Any]]:
    from custom_components.smart_occupancy.switch_websocket import switches_model

    return switches_model(ctrl.hass, ctrl, area)["items"]


async def assign(hass: HomeAssistant, ws: WS, lit: Lit, freezer, **msg: Any) -> str:
    reply = await ws(type="smart_occupancy/lighting/switch/add", area_id=lit.area, **msg)
    assert reply["success"], reply
    await settle(hass, freezer, 3)  # subscriptions attached and settled
    return reply["result"]["switch_id"]


def ctrl_of(entry):
    return entry.runtime_data.lighting


# --------------------------------------------------------------- discovery
async def test_discover_select_and_assign_wiser_switch(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                       hass_ws_client, freezer) -> None:
    """1-2: the Zigbee2MQTT Wiser switch is offered as a device; assigning it is Listen only with its buttons."""
    device_id = wiser_device(hass)
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    reply = await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area)
    assert reply["success"]
    await hass.async_block_till_done()
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge(), retain=True)
    await hass.async_block_till_done()
    found = (await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area))["result"]
    wiser = next(c for c in found["candidates"] if c["device_id"] == device_id)
    assert wiser["name"] == NAME and wiser["manufacturer"] == "Schneider Electric"
    assert wiser["sources"] == ["zigbee2mqtt"] and wiser["actions"] == WISER_ACTIONS
    assert wiser["bindings"] == 0 and wiser["assigned_room"] is None  # binding to the coordinator is normal

    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)
    switch = ctrl_of(entry).lighting.room(lit.area).switch(switch_id)
    assert switch.mode == SwitchMode.LISTEN and switch.ieee == IEEE and switch.z2m_name == NAME
    assert list(switch.buttons) == WISER_ACTIONS  # declared by the device, not hardcoded
    # Default suggestions for recognisable buttons; nothing is controlled yet.
    assert switch.mappings["on_right"].action == "on" and switch.mappings["off_left"].action == "off"
    assert switch.mappings["brightness_move_up_left"].action == "dim_up"
    assert switch.mappings["brightness_stop_right"].action == "dim_stop"
    model = room_switches(ctrl_of(entry), lit.area)[0]
    assert model["connection"] == "Zigbee2MQTT (MQTT)" and model["topic_now"] == TOPIC
    assert model["status"]["sources"] == ["zigbee2mqtt"] and model["mode"] == "listen"
    # Assigning the same device again (any room) is refused.
    reply = await ws(type="smart_occupancy/lighting/switch/add", area_id=lit.area, device_id=device_id)
    assert reply["error"]["code"] == "already_assigned"
    candidate = next(c for c in (await ws(type="smart_occupancy/lighting/switch/candidates",
                                          area_id=lit.area))["result"]["candidates"] if c["device_id"] == device_id)
    assert candidate["assigned_room"] == lit.area
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.all == []  # Listen only
    status = ctrl_of(entry).switch_inputs.status[(lit.area, switch_id)]
    assert status.last_event["raw"] == "on_right" and status.last_result.startswith("Listen only")


async def test_learn_buttons_persist_and_never_command(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                       hass_ws_client, freezer, hass_storage) -> None:
    """3-4, 16-18: learning captures real presses only, never sends a command, and the result persists."""
    device_id = wiser_device(hass)
    entry = await setup_entry(hass, options(lit))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    calls.clear()
    ws = WS(await hass_ws_client(hass))
    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)  # no bridge list: nothing declared
    ctrl = ctrl_of(entry)
    assert ctrl.lighting.room(lit.area).switch(switch_id).buttons == {}
    # Even in Smart Occupancy control, learning executes nothing.
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True))["success"]
    reply = await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id,
                     action="start", seconds=60)
    assert reply["success"] and reply["result"]["active"]
    await hass.async_block_till_done()
    press(hass, "on_right", retain=True)  # a retained message is not a press
    async_fire_mqtt_message(hass, TOPIC, json.dumps({"battery": 97, "linkquality": 80}))  # status update
    async_fire_mqtt_message(hass, TOPIC, json.dumps({"action": "", "linkquality": 80}))   # cleared action
    async_fire_mqtt_message(hass, TOPIC, "{not json")                                    # malformed
    await settle(hass, freezer)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert ctrl.lighting.room(lit.area).switch(switch_id).buttons == {}
    assert status.ignored_updates == 2 and status.last_ignored["reason"] == "malformed"
    press(hass, "on_right")
    await settle(hass, freezer, 0.5)
    press(hass, "off_right")
    await settle(hass, freezer, 0.5)
    session = ctrl.switch_inputs.learning[(lit.area, switch_id)]
    assert [c["raw"] for c in session.captured] == ["on_right", "off_right"]
    room = room_switches(ctrl, lit.area)[0]
    assert room["learning"]["captured"][-1]["label"] == "OFF right"
    assert calls.all == []
    switch = ctrl.lighting.room(lit.area).switch(switch_id)
    assert list(switch.buttons) == ["on_right", "off_right"] and switch.buttons["on_right"].source == "zigbee2mqtt"
    # Cancel: the switch listens on its authoritative source only again.
    reply = await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="stop")
    assert reply["success"] and len(reply["result"]["captured"]) == 2
    assert (lit.area, switch_id) not in ctrl.switch_inputs.learning
    # Learned actions survive a restart (reload).
    await ctrl.store.async_flush()
    assert set(hass_storage["smart_occupancy.lighting"]["data"]["rooms"][lit.area]["switches"][0]["buttons"]) == {
        "on_right", "off_right"}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    switch = ctrl_of(entry).lighting.room(lit.area).switch(switch_id)
    assert list(switch.buttons) == ["on_right", "off_right"] and switch.mode == SwitchMode.CONTROL
    # Learning times out by itself.
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="start",
             seconds=5)
    await advance(hass, freezer, 6)
    assert ctrl_of(entry).switch_inputs.learning == {}


async def controlled(hass: HomeAssistant, ws: WS, lit: Lit, freezer, entry, *, bridge_list: bool = True,
                     **mappings: dict[str, Any]) -> str:
    """The Wiser switch assigned to the pilot room, buttons declared, mappings set, control activated."""
    device_id = wiser_device(hass)
    if bridge_list:
        await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area)
        await hass.async_block_till_done()
        async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge(), retain=True)
        await hass.async_block_till_done()
    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)
    for raw, mapping in mappings.items():
        reply = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                         raw=raw, mapping=mapping)
        assert reply["success"], reply
    reply = await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True)
    assert reply["success"], reply
    await settle(hass, freezer)
    return switch_id


# ----------------------------------------------------------------- control
async def test_wiser_controls_room_group_with_manual_override(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                              hass_ws_client, freezer) -> None:
    """5-8, 33: ON/OFF go to the managed group (not its members), count as manual and win over automation."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer, manual_override_minutes=30)  # Smart Occupancy switched the lights on
    switch_id = await controlled(hass, ws, lit, freezer, entry)
    calls.clear()
    # The person presses OFF right while the room is occupied and lit.
    press(hass, "off_right")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert sent[lit.group] == ("turn_off", {}) and lit.spot_1 not in sent and lit.spot_2 not in sent  # via the group
    assert set(sent) == set(ctrl.managed_room(lit.area).controlled)
    rt = ctrl.runtime[lit.area]
    override = rt.overrides[lit.group]
    assert override["source"] == "wall_switch" and override["switch"] == NAME and override["state"] == "off"
    assert override["reason"] == "Physical switch OFF requested" and override["action"] == "off_right"
    until = dt_util.parse_datetime(override["until"])
    assert timedelta(minutes=29) < until - dt_util.utcnow() <= timedelta(minutes=30)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert status.last_result.startswith("Room lights") and "switched off" in status.last_result and status.override
    # The lights report back (MQTT, new context): still the person's change, not a new automation change.
    for e in sent:
        attrs = {**CT, "entity_id": [lit.spot_1, lit.spot_2]} if e == lit.group else CT
        hass.states.async_set(e, "off", attrs, context=Context())
    await hass.async_block_till_done()
    assert rt.overrides[lit.group]["switch"] == NAME
    # Occupancy and moods continue, but do not switch the lights back on.
    calls.clear()
    hass.states.async_set(lit.pir, "off")
    await hass.async_block_till_done()
    hass.states.async_set(lit.pir, "on")
    await settle(hass, freezer, 2)
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await settle(hass, freezer, 2)
    assert all(lit_entity not in calls.by_entity() for lit_entity in sent)
    plan = ctrl.plan(lit.area)
    assert {i.entity_id: i.reason_code for i in plan.items}[lit.group] == "manual_override"
    diag = ctrl.room_diagnostics(lit.area)
    assert any(o["switch"] == NAME for o in diag["manual_overrides"])
    # ON is an explicit request: it is executed even where automatic switch-on is not allowed (daylight gate).
    ctrl.store.async_set(ctrl.with_room(lit.area, daylight=ctrl.lighting.room(lit.area).daylight.__class__.from_dict(
        {"auto_on": "never"})))
    await settle(hass, freezer)
    assert ctrl.daylight_gate(lit.area, fresh=True).blocks() is not None  # occupancy would not switch on
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    sent = calls.by_entity()
    # Current mood (Kvøld) values for lights the mood switches on; the strip is left as the mood says (ignore).
    assert sent[lit.group] == ("turn_on", {"brightness_pct": 50, "color_temp_kelvin": 3000})
    assert sent[lit.lamp] == ("turn_on", {"brightness_pct": 75, "color_temp_kelvin": 2700})
    assert rt.overrides[lit.group]["state"] == "on"
    # Overrides expire as before.
    await advance(hass, freezer, 31 * 60)
    await advance(hass, freezer, 1)  # the next expiry, scheduled by the first one
    assert rt.overrides == {}


async def test_switches_work_in_manual_mood_and_respect_observe(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                                hass_ws_client, freezer) -> None:
    """9-10: Main Mood = Manual pauses automatic lighting, never the wall switches; Observe never sends."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    # Observe: the switch is in control, the room was never activated.
    switch_id = await controlled(hass, ws, lit, freezer, entry)
    calls.clear()
    press(hass, "off_right")
    await settle(hass, freezer)
    assert calls.all == []
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert "Observe" in status.last_result
    assert any(w["code"] == "observe" for w in room_switches(ctrl, lit.area)[0]["warnings"])
    # Activated, then Main Mood = Manual: automatic lighting stops, the wall switch keeps working.
    await activate(hass, entry, lit, freezer)
    set_moods(hass, "Dagur", "Manual", "Manual", main_opts=["Morgun", "Dagur", "Kvøld", "Nátt", "Hygge", "Manual"])
    await settle(hass, freezer, 2)
    assert ctrl.manual_mode
    calls.clear()
    set_moods(hass, "Kvøld", "Manual", "Manual", main_opts=["Morgun", "Dagur", "Kvøld", "Nátt", "Hygge", "Manual"])
    await settle(hass, freezer, 2)
    assert calls.all == []  # nothing automatic
    press(hass, "off_right")
    await settle(hass, freezer)
    assert calls.by_entity()[lit.group] == ("turn_off", {})
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.by_entity()[lit.group] == ("turn_on", {})  # no mood in Manual: the lights' own last values


async def test_listen_only_recognises_direct_binding(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                     hass_ws_client, freezer) -> None:
    """22: a switch bound directly to its lights switches them itself; Smart Occupancy only recognises it."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    device_id = wiser_device(hass)
    await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area)
    await hass.async_block_till_done()
    group_binding = [{"cluster": "genOnOff", "target": {"type": "group", "id": 7}}]
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge(bindings=group_binding), retain=True)
    await hass.async_block_till_done()
    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)
    model = room_switches(ctrl, lit.area)[0]
    assert model["bindings"] == [{"cluster": "genOnOff", "endpoint": "21", "target": "group 7"}]
    assert not model["can_control"] and any(w["code"] == "binding" for w in model["warnings"])
    # Control is refused while the binding exists: both would switch the lights.
    reply = await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True)
    assert reply["error"]["code"] == "binding"
    calls.clear()
    # Listen only: the press, then the bound group reports off (no Home Assistant context).
    press(hass, "off_right")
    await hass.async_block_till_done()
    hass.states.async_set(lit.group, "off", {**CT, "entity_id": [lit.spot_1, lit.spot_2]}, context=Context())
    await settle(hass, freezer)
    assert calls.all == []
    override = ctrl.runtime[lit.area].overrides[lit.group]
    assert override["source"] == "wall_switch" and override["state"] == "off"


# ------------------------------------------------------------ input paths
async def discover_device_triggers(hass: HomeAssistant, actions: list[str]) -> str:
    """Zigbee2MQTT's Home Assistant discovery of device triggers (``<topic>/action``)."""
    for action in actions:
        async_fire_mqtt_message(hass, f"homeassistant/device_automation/{IEEE}/action_{action}/config", json.dumps({
            "automation_type": "trigger", "type": "action", "subtype": action, "payload": action,
            "topic": f"{TOPIC}/action",
            "device": {"identifiers": [f"zigbee2mqtt_{IEEE}"], "name": NAME, "manufacturer": "Schneider Electric",
                       "model": "Wiser wireless switch 2-gang (WDE002906)"},
        }))
    await hass.async_block_till_done()
    from custom_components.smart_occupancy.switch_discovery import all_devices

    device = next(d for d in all_devices(hass) if ("mqtt", f"zigbee2mqtt_{IEEE}") in d.identifiers)
    return device.id


async def test_mqtt_device_triggers(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls, hass_ws_client,
                                    freezer) -> None:
    """12: a device that only exposes MQTT device triggers is discovered and its triggers are the buttons."""
    device_id = await discover_device_triggers(hass, ["on_right", "off_right"])
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    found = (await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area))["result"]["candidates"]
    wiser = next(c for c in found if c["device_id"] == device_id)
    assert "device_trigger" in wiser["sources"] and set(wiser["actions"]) == {"on_right", "off_right"}
    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)
    assert set(ctrl.lighting.room(lit.area).switch(switch_id).buttons) == {"on_right", "off_right"}
    assert (await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
                     source="device_trigger"))["success"]
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True))["success"]
    await settle(hass, freezer, 3)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert status.sources == ["device_trigger"]
    calls.clear()
    async_fire_mqtt_message(hass, f"{TOPIC}/action", "off_right")
    await settle(hass, freezer)
    assert calls.by_entity()[lit.group] == ("turn_off", {})
    assert status.last_event["source"] == "device_trigger" and status.last_event["raw"] == "off_right"
    # The raw device topic is not listened to: a press there is not seen twice.
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer)
    assert calls.all == []


async def test_event_entities(hass: HomeAssistant, lit: Lit, calls: Calls, hass_ws_client, freezer) -> None:
    """13: event entities (no MQTT needed). The state is a timestamp; the action is the event type."""
    source = MockConfigEntry(domain="test_remote")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(config_entry_id=source.entry_id, identifiers={("hue", "dimmer")},
                                                    name="Hue dimmer", manufacturer="Signify", model="RWL022")
    entity = er.async_get(hass).async_get_or_create("event", "test_remote", "dimmer_button", device_id=device.id,
                                                    suggested_object_id="hue_dimmer_action").entity_id
    hass.states.async_set(entity, "unknown", {"event_types": ["press", "double_press", "long_press"],
                                              "device_class": "button"})
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    found = (await ws(type="smart_occupancy/lighting/switch/candidates", area_id=lit.area))["result"]["candidates"]
    dimmer = next(c for c in found if c["device_id"] == device.id)
    assert dimmer["sources"] == ["event_entity"] and dimmer["event_entities"] == [entity]
    switch_id = await assign(hass, ws, lit, freezer, device_id=device.id)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert status.sources == ["event_entity"]
    assert (await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id,
                     action="start"))["success"]
    # An event split into button + event type (the state is the time of the event, not the action).
    hass.states.async_set(entity, dt_util.utcnow().isoformat(), {"event_type": "press", "button": "off",
                                                                 "event_types": ["press"]})
    await settle(hass, freezer, 1)
    hass.states.async_set(entity, dt_util.utcnow().isoformat(), {"event_type": "on_press", "event_types": ["on_press"]})
    await settle(hass, freezer, 1)
    switch = ctrl.lighting.room(lit.area).switch(switch_id)
    assert {"off_press", "on_press"} <= set(switch.buttons)
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="stop")
    assert (await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                     raw="off_press", mapping={"action": "off"}))["success"]
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True))["success"]
    await settle(hass, freezer)
    calls.clear()
    # Restored after unavailable, and a stale event: not presses.
    old = (dt_util.utcnow() - timedelta(minutes=5)).isoformat()
    hass.states.async_set(entity, "unavailable", {})
    hass.states.async_set(entity, old, {"event_type": "press", "button": "off"})
    hass.states.async_set(entity, (dt_util.utcnow() - timedelta(minutes=1)).isoformat(),
                          {"event_type": "press", "button": "off"})
    await settle(hass, freezer)
    assert calls.all == [] and status.last_ignored["reason"] == "stale"
    hass.states.async_set(entity, dt_util.utcnow().isoformat(), {"event_type": "press", "button": "off"})
    await settle(hass, freezer)
    assert calls.by_entity()[lit.group] == ("turn_off", {})


async def test_one_press_through_several_paths_counts_once(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                           hass_ws_client, freezer) -> None:
    """14-15: raw MQTT + device trigger + event entity of one press = one command; deliberate repeats are kept."""
    device_id = await discover_device_triggers(hass, ["on_right", "off_right"])
    event = er.async_get(hass).async_get_or_create("event", "mqtt", f"{IEEE}_action", device_id=device_id,
                                                   suggested_object_id=f"{NAME}_action").entity_id
    hass.states.async_set(event, "unknown", {"event_types": ["on_right", "off_right"]})
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    ws = WS(await hass_ws_client(hass))
    switch_id = await assign(hass, ws, lit, freezer, device_id=device_id)
    assert (await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                     raw="on_right", mapping={"action": "toggle"}))["success"]
    assert (await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True))["success"]
    # Learning listens on every path: the same press arrives three times and is captured once.
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="start")
    await settle(hass, freezer, 3)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert set(status.sources) == {"zigbee2mqtt", "device_trigger", "event_entity"}
    press(hass, "off_left")
    async_fire_mqtt_message(hass, f"{TOPIC}/action", "off_left")
    hass.states.async_set(event, dt_util.utcnow().isoformat(), {"event_type": "off_left"})
    await settle(hass, freezer)
    session = ctrl.switch_inputs.learning[(lit.area, switch_id)]
    assert [c["raw"] for c in session.captured] == ["off_left"]
    assert status.last_ignored["reason"] == "duplicate"
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="stop")
    await settle(hass, freezer, 3)
    assert status.sources == ["zigbee2mqtt"]  # authoritative path only
    # Two deliberate presses (toggle) a second apart: both executed.
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer, 1)
    first = calls.by_entity()[lit.group][0]
    hass.states.async_set(lit.group, "off" if first == "turn_off" else "on",
                          {**CT, "entity_id": [lit.spot_1, lit.spot_2]})
    for e in (lit.lamp, lit.desk, lit.strip, lit.plug):
        hass.states.async_set(e, "off" if first == "turn_off" else "on", CT)
    await hass.async_block_till_done()
    calls.clear()
    press(hass, "on_right")
    await settle(hass, freezer, 1)
    assert calls.by_entity()[lit.group][0] != first
    # A radio retransmission within the debounce time counts once.
    calls.clear()
    press(hass, "off_right")
    press(hass, "off_right")
    await settle(hass, freezer, 1)
    assert len([c for c in calls.off if lit.group in c.data["entity_id"]]) == 1


async def test_availability_reconnect_rename_and_removal(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                         hass_ws_client, freezer) -> None:
    """19-21: offline switch, MQTT reconnect, Zigbee2MQTT rename (followed by IEEE address), device and switch removal."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry)
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    async_fire_mqtt_message(hass, f"{TOPIC}/availability", json.dumps({"state": "offline"}), retain=True)
    await hass.async_block_till_done()
    assert status.availability == "offline"
    assert "offline" in str(room_switches(ctrl, lit.area)[0]["status"]["availability"])
    async_fire_mqtt_message(hass, f"{TOPIC}/availability", "online")
    await hass.async_block_till_done()
    assert status.availability == "online"
    # Broker connection lost and back: subscribed again, still one command per press.
    from homeassistant.components.mqtt.const import MQTT_CONNECTION_STATE

    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, False)
    await hass.async_block_till_done()
    assert ctrl.switch_inputs.mqtt_state == "disconnected"
    async_dispatcher_send(hass, MQTT_CONNECTION_STATE, True)
    await settle(hass, freezer, 3)
    assert ctrl.switch_inputs.mqtt_state == "connected"
    press(hass, "off_right", retain=True)  # retained replay after the resubscription
    await settle(hass, freezer)
    assert calls.off == []
    calls.clear()
    press(hass, "off_right")
    await settle(hass, freezer)
    assert len([c for c in calls.off if lit.group in c.data["entity_id"]]) == 1
    # Renamed in Zigbee2MQTT: the same IEEE address, a new topic.
    # (Updates of the retained device list reach a subscriber live, without the retain flag.)
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge("kamar_sudur_switch"))
    await settle(hass, freezer, 3)
    switch = ctrl.lighting.room(lit.area).switch(switch_id)
    assert switch.z2m_name == "kamar_sudur_switch" and status.topic == "zigbee2mqtt/kamar_sudur_switch"
    calls.clear()
    press(hass, "off_right")  # the old topic
    await settle(hass, freezer)
    assert calls.all == []
    press(hass, "off_right", topic="zigbee2mqtt/kamar_sudur_switch")
    await settle(hass, freezer)
    assert calls.off
    # Removed from Zigbee2MQTT: no longer listened to (and reported).
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge(include=False))
    await settle(hass, freezer, 3)
    assert status.sources == [] and "no longer in Zigbee2MQTT" in status.problem
    # Removed from the room: unsubscribed, its status gone.
    async_fire_mqtt_message(hass, "zigbee2mqtt/bridge/devices", bridge("kamar_sudur_switch"))
    await settle(hass, freezer, 3)
    assert (await ws(type="smart_occupancy/lighting/switch/remove", area_id=lit.area, switch_id=switch_id))["success"]
    await settle(hass, freezer, 3)
    assert ctrl.lighting.room(lit.area).switches == () and (lit.area, switch_id) not in ctrl.switch_inputs.status
    calls.clear()
    press(hass, "off_right", topic="zigbee2mqtt/kamar_sudur_switch")
    await settle(hass, freezer)
    assert calls.all == []


# ----------------------------------------------------- competing controls
async def test_competing_automation_must_be_resolved_before_control(hass: HomeAssistant, mqtt, lit: Lit,
                                                                    calls: Calls, hass_ws_client, freezer) -> None:
    """23: an existing automation that reacts to the switch is reported; activation needs it resolved or acknowledged."""
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "wiser_off", "alias": "Wiser OFF right",
         "triggers": [{"trigger": "mqtt", "topic": f"{TOPIC}/action", "payload": "off_right"}],
         "actions": [{"action": "light.turn_off", "target": {"entity_id": lit.group}}]},
    ]})
    await hass.async_block_till_done()
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    switch_id = await assign(hass, ws, lit, freezer, device_id=wiser_device(hass))
    model = room_switches(ctrl, lit.area)[0]
    assert model["competing"] == [{"entity_id": "automation.wiser_off_right", "name": "Wiser OFF right",
                                   "enabled": True, "via": "MQTT topic", "buttons": ["off_right"]}]
    assert any(w["code"] == "competing_automation" for w in model["warnings"])
    reply = await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control")
    assert reply["error"]["code"] == "confirmation_required"
    reply = await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True)
    assert reply["error"]["code"] == "competing_automation" and "Wiser OFF right" in reply["error"]["message"]
    assert ctrl.lighting.room(lit.area).switch(switch_id).mode == SwitchMode.LISTEN
    # Disabled automations do not compete; acknowledging allows both.
    reply = await ws(type="smart_occupancy/lighting/switch/mode", area_id=lit.area, switch_id=switch_id,
                     mode="control", confirm=True, acknowledge=["automation.wiser_off_right"])
    assert reply["success"]
    assert ctrl.lighting.room(lit.area).switch(switch_id).acknowledged == ("automation.wiser_off_right",)
    assert hass.states.get("automation.wiser_off_right").state == "on"  # never changed by Smart Occupancy


# ----------------------------------------------------------------- dimming
async def test_hold_to_dim_with_and_without_stop(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls, hass_ws_client,
                                                 freezer) -> None:
    """24-28: bounded, paced dimming of the group; STOP ends it, the timeout ends it, limits are respected."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    up = {"action": "dim_up", "target": lit.group}
    down = {"action": "dim_down", "target": lit.group}
    switch_id = await controlled(hass, ws, lit, freezer, entry, brightness_move_up_left=up,
                                 brightness_move_down_left=down)
    assert (await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
                     dimming={"hold_step": 10, "interval": 0.5, "min_brightness": 20, "max_brightness": 80,
                              "timeout": 3}))["success"]
    # The group is at 50 %.
    hass.states.async_set(lit.group, "on", {**CT, "entity_id": [lit.spot_1, lit.spot_2], "brightness": 128,
                                            "color_mode": "color_temp", "color_temp_kelvin": 3000})
    await settle(hass, freezer)
    calls.clear()
    press(hass, "brightness_move_up_left")
    await hass.async_block_till_done()
    levels = [c.data["brightness_pct"] for c in calls.on]
    assert levels == [60]  # the first step at once
    for _ in range(2):
        await advance(hass, freezer, 0.5)
    levels = [c.data["brightness_pct"] for c in calls.on]
    assert levels == [60, 70, 80]
    assert all(c.data["entity_id"] == [lit.group] for c in calls.on)  # the group, never each member
    await advance(hass, freezer, 0.5)
    assert len(calls.on) == 3  # the maximum stops it
    assert ctrl.lighting.room(lit.area).switch(switch_id).dimming.max_brightness == 80
    status = ctrl.switch_inputs.status[(lit.area, switch_id)]
    assert "reached the maximum" in status.last_result and ctrl.runtime[lit.area].overrides[lit.group]["state"] == "on"
    # The dimmed brightness is kept: automatic and global-adjustment updates leave the group alone.
    item = next(i for i in ctrl.plan(lit.area, "adaptive").items if i.entity_id == lit.group)
    assert item.decision == "blocked" and item.reason_code == "manual_override"
    # Dimming down; STOP / release ends it at once.
    calls.clear()
    press(hass, "brightness_move_down_left")
    await hass.async_block_till_done()
    await advance(hass, freezer, 0.5)
    press(hass, "brightness_stop_left")
    await settle(hass, freezer)
    await advance(hass, freezer, 2)
    assert [c.data["brightness_pct"] for c in calls.on] == [70, 60]
    assert status.history[-1]["result"].startswith("Dimmed down to 60 %")
    # STOP never arrives: the safety timeout ends it; at most one command per interval (no flooding).
    calls.clear()
    press(hass, "brightness_move_down_left")
    await hass.async_block_till_done()
    for _ in range(12):
        await advance(hass, freezer, 0.5)
    sent = [c.data["brightness_pct"] for c in calls.on]
    assert sent == [50, 40, 30, 20]  # one command per interval, never below the minimum
    assert not ctrl.switch_control.dimming(lit.area, switch_id)
    assert "reached the minimum" in status.last_result
    # A slow dimmer: the safety timeout stops it when STOP never arrives.
    await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
             dimming={"hold_step": 1, "interval": 1, "timeout": 2})
    await settle(hass, freezer)
    calls.clear()
    press(hass, "brightness_move_up_left")
    await hass.async_block_till_done()
    for _ in range(6):
        await advance(hass, freezer, 1)
    assert len(calls.on) == 2  # 2 s at one command per second, then it stops
    assert not ctrl.switch_control.dimming(lit.area, switch_id)
    assert "no STOP within 2 s (safety timeout)" in status.last_result


async def test_brightness_steps_respect_limits_and_capabilities(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                                hass_ws_client, freezer) -> None:
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    switch_id = await controlled(hass, ws, lit, freezer, entry, on_left={"action": "brightness_up"},
                                 off_left={"action": "set_color_temp", "color_temp": 2000})
    await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
             dimming={"step": 30, "max_brightness": 90})
    hass.states.async_set(lit.plug, "on", {"supported_color_modes": ["onoff"]})
    hass.states.async_set(lit.lamp, "on", {**CT, "brightness": 204})  # 80 %
    await settle(hass, freezer)
    calls.clear()
    press(hass, "on_left")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert sent[lit.lamp] == ("turn_on", {"brightness_pct": 90})  # capped at the maximum
    assert lit.plug not in sent  # on/off only: no brightness command
    calls.clear()
    press(hass, "off_left")
    await settle(hass, freezer)
    sent = calls.by_entity()
    assert sent[lit.lamp] == ("turn_on", {"color_temp_kelvin": 2200})  # clamped to the lamp's range
    assert lit.plug not in sent  # no colour temperature: not commanded


# ------------------------------------------------------------------- moods
async def test_mood_buttons(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls, hass_ws_client, freezer) -> None:
    """29-31: select a mood for the room or all rooms, return to Auto; Main Mood is never written."""
    select = async_mock_service(hass, "input_select", "select_option")
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    switch_id = await controlled(
        hass, ws, lit, freezer, entry,
        on_left={"action": "mood", "mood": "Hygge"}, off_left={"action": "mood_auto"},
        on_right={"action": "mood", "mood": "Kvøld", "scope": "global"},
        off_right={"action": "mood_auto", "scope": "global"},
        brightness_move_up_right={"action": "mood", "mood": "Nátt", "minutes": 10})
    calls.clear()
    press(hass, "on_left")
    await settle(hass, freezer, 2)
    assert ctrl.lighting.room(lit.area).mood_override == "Hygge"
    assert calls.by_entity()[lit.lamp] == ("turn_on", {"brightness_pct": 70, "color_temp_kelvin": 2700,
                                                        "transition": 2})  # applied by mood lighting
    press(hass, "off_left")
    await settle(hass, freezer, 2)
    assert ctrl.lighting.room(lit.area).mood_override is None
    # Global: the user's mood selector (Manual Mood) is set; the existing automation updates Main Mood.
    press(hass, "on_right")
    await settle(hass, freezer)
    assert [(c.data["entity_id"], c.data["option"]) for c in select] == [(MANUAL, "Kvøld")]
    set_moods(hass, "Dagur", "Kvøld", "Dagur")  # Manual Mood is Kvøld, the automation has not run yet
    await hass.async_block_till_done()
    press(hass, "off_right")
    await settle(hass, freezer)
    assert [(c.data["entity_id"], c.data["option"]) for c in select][-1] == (MANUAL, "Auto")
    assert all(c.data["entity_id"] != MAIN for c in select)
    # A temporary room mood returns to automatic selection by itself.
    press(hass, "brightness_move_up_right")
    await settle(hass, freezer, 2)
    assert ctrl.lighting.room(lit.area).mood_override == "Nátt"
    await advance(hass, freezer, 10 * 60 + 1)
    assert ctrl.lighting.room(lit.area).mood_override is None
    # Moods are discovered (no fixed list); a mood that does not exist cannot be assigned.
    reply = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                     raw="on_left", mapping={"action": "mood", "mood": "Veistla"})
    assert reply["error"]["code"] == "invalid_mapping"


# --------------------------------------------------------- configuration
async def test_websocket_validation_permissions_and_persistence(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                                hass_ws_client, hass_read_only_access_token,
                                                                freezer, caplog) -> None:
    """36: settings are validated, admin only and persist; no wildcard topic; safe actions only."""
    caplog.set_level(logging.CRITICAL, logger=WS_LOGGER)
    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    for topic in ("zigbee2mqtt/#", "zigbee2mqtt/+/action", "zigbee2mqtt/bridge/devices", "avb_ksu", "$SYS/x"):
        reply = await ws(type="smart_occupancy/lighting/switch/add", area_id=lit.area, topic=topic)
        assert not reply["success"], topic
    switch_id = await assign(hass, ws, lit, freezer, topic="zigbee2mqtt/avb_ksu")
    async_fire_mqtt_message(hass, TOPIC, json.dumps({"action": "on_right"}))
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="start")
    await settle(hass, freezer, 3)
    press(hass, "on_right")
    await settle(hass, freezer)
    await ws(type="smart_occupancy/lighting/switch/learn", area_id=lit.area, switch_id=switch_id, action="stop")
    bad = [
        {"action": "scene", "entity": "scene.nope"},
        {"action": "script", "entity": "light.kno_lamp"},
        {"action": "wled_preset", "entity": "select.kno_preset", "option": "Party"},
        {"action": "on", "target": "switch.kettle"},
        {"action": "set_brightness"},
        {"action": "on", "on_values": "fixed"},
    ]
    for mapping in bad:
        reply = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                         raw="on_right", mapping=mapping)
        assert reply["error"]["code"] == "invalid_mapping", mapping
    reply = await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                     raw="on_right", mapping={"action": "service", "service": "shell_command.x"})
    assert not reply["success"]  # no arbitrary service calls
    hass.states.async_set("scene.kvoldseta", "scening", {"friendly_name": "Kvøldseta"})
    assert (await ws(type="smart_occupancy/lighting/switch/mapping", area_id=lit.area, switch_id=switch_id,
                     raw="on_right", mapping={"action": "scene", "entity": "scene.kvoldseta"}))["success"]
    assert (await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
                     name="Wiser Kamar Suður", debounce_ms=250, activity_evidence=True, override_minutes=15,
                     dimming={"step": 20}))["success"]
    reply = await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id,
                     dimming={"min_brightness": 90, "max_brightness": 10})
    assert not reply["success"]
    reply = await ws(type="smart_occupancy/lighting/switch/settings", mqtt_base_topic="z2m/#")
    assert not reply["success"]
    assert (await ws(type="smart_occupancy/lighting/switch/settings", mqtt_base_topic="z2m"))["success"]
    # Read-only users can see the Switches tab, not change it.
    ro = WS(await hass_ws_client(hass, hass_read_only_access_token))
    model = (await ro(type="smart_occupancy/lighting/editor", area_id=lit.area))["result"]
    assert model["room"]["switches"]["items"][0]["name"] == "Wiser Kamar Suður"
    for msg in ({"type": "smart_occupancy/lighting/switch/mode", "area_id": lit.area, "switch_id": switch_id,
                 "mode": "control", "confirm": True},
                {"type": "smart_occupancy/lighting/switch/learn", "area_id": lit.area, "switch_id": switch_id,
                 "action": "start"},
                {"type": "smart_occupancy/lighting/switch/candidates", "area_id": lit.area}):
        reply = await ro(**msg)
        assert reply["error"]["code"] == "unauthorized", msg
    # Everything persists over a reload.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    switch = ctrl_of(entry).lighting.room(lit.area).switch(switch_id)
    assert (switch.name, switch.debounce_ms, switch.activity_evidence, switch.override_minutes, switch.dimming.step,
            switch.topic) == ("Wiser Kamar Suður", 250, True, 15, 20, "zigbee2mqtt/avb_ksu")
    assert switch.mappings["on_right"].entity == "scene.kvoldseta"
    assert ctrl_of(entry).lighting.switch_settings.mqtt_base_topic == "z2m"
    assert calls.all == []


async def test_reload_and_unload_clean_up_listeners(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                    hass_ws_client, freezer) -> None:
    """35: no duplicate listeners after reloads; nothing is handled after unload."""
    from homeassistant.components.mqtt.models import DATA_MQTT

    entry = await setup_entry(hass, options(lit))
    ws = WS(await hass_ws_client(hass))
    await occupy(hass, lit, freezer)
    await activate(hass, entry, lit, freezer)
    await controlled(hass, ws, lit, freezer, entry)

    def ours() -> int:
        subscriptions = hass.data[DATA_MQTT].client._simple_subscriptions  # the real client behind the mock
        return sum(len(subs) for topic, subs in subscriptions.items() if topic.startswith("zigbee2mqtt/"))

    count = ours()
    assert count == 3  # the device topic, its availability and the bridge device list
    for _ in range(2):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await settle(hass, freezer, 3)
    assert ours() == count
    calls.clear()
    press(hass, "off_right")
    await settle(hass, freezer)
    assert len([c for c in calls.off if lit.group in c.data["entity_id"]]) == 1  # once, not once per reload
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ours() == 0
    calls.clear()
    press(hass, "off_right")
    await settle(hass, freezer)
    assert calls.all == []


async def test_activity_evidence_refreshes_an_occupied_room_only(hass: HomeAssistant, mqtt, lit: Lit, calls: Calls,
                                                                 hass_ws_client, freezer) -> None:
    """Optional: a press keeps an occupied room occupied a little longer; it never occupies an empty room."""
    entry = await setup_entry(hass, options(lit))
    ctrl = ctrl_of(entry)
    ws = WS(await hass_ws_client(hass))
    switch_id = await assign(hass, ws, lit, freezer, device_id=wiser_device(hass))
    assert ctrl.lighting.room(lit.area).switch(switch_id).activity_evidence is False  # off by default
    await ws(type="smart_occupancy/lighting/switch/set", area_id=lit.area, switch_id=switch_id, activity_evidence=True)
    await settle(hass, freezer)
    occupancy = ctrl.coordinator.occupancy
    assert occupancy.result(lit.area).occupied is not True
    press(hass, "on_right")
    await settle(hass, freezer)
    assert occupancy.result(lit.area).occupied is not True  # not occupied by a press
    await occupy(hass, lit, freezer)
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await settle(hass, freezer, 500)
    press(hass, "on_right")
    await settle(hass, freezer)
    assert occupancy.result(lit.area).last_activity_source == f"wall switch {NAME}"
    await advance(hass, freezer, 500)
    assert occupancy.result(lit.area).occupied is True  # still occupied (refreshed)
    await advance(hass, freezer, 200)
    await advance(hass, freezer, 5)
    assert occupancy.result(lit.area).occupied is False  # the press does not hold it
