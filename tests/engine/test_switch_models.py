"""Wall switch model, parsing, validation, deduplication and migration (pure logic, Phase 0.3.5)."""

from __future__ import annotations

import json

import pytest

from custom_components.smart_occupancy.const import (
    ButtonAction,
    OnValues,
    SwitchMode,
    SwitchSource,
)
from custom_components.smart_occupancy.lighting_config import (
    LightingConfig,
    migrate_store,
    rename_entity,
)
from custom_components.smart_occupancy.switch_discovery import (
    light_bindings,
    parse_bridge_devices,
    resolve_source,
)
from custom_components.smart_occupancy.switches import (
    ButtonEvent,
    ButtonMapping,
    Deduplicator,
    DimmingConfig,
    SwitchConfig,
    action_label,
    event_entity_action,
    parse_action,
    parse_mqtt_payload,
    suggest_mapping,
    validate_base_topic,
    validate_topic,
)


@pytest.mark.parametrize(("raw", "button", "press"), [
    ("on_left", "on_left", "single"),
    ("off_right", "off_right", "single"),
    ("brightness_move_up_left", "brightness_up_left", "hold"),
    ("brightness_stop_right", "brightness_right", "release"),
    ("1_double", "1", "double"),
    ("button_2_hold", "button_2", "hold"),
    ("up_hold_release", "up", "release"),
    ("single", "single", "single"),
    ("toggle", "toggle", "single"),
])
def test_parse_action_never_invents_press_types(raw: str, button: str, press: str) -> None:
    assert parse_action(raw) == (button, press)


def test_action_labels_and_suggestions() -> None:
    assert action_label("on_right") == "ON right" and action_label("brightness_move_up_left") == "brightness move up left"
    assert suggest_mapping("on_right").action == ButtonAction.ON
    assert suggest_mapping("off_left").action == ButtonAction.OFF
    assert suggest_mapping("toggle").action == ButtonAction.TOGGLE
    assert suggest_mapping("brightness_move_up_left").action == ButtonAction.DIM_UP
    assert suggest_mapping("brightness_move_down_right").action == ButtonAction.DIM_DOWN
    assert suggest_mapping("brightness_stop_left").action == ButtonAction.DIM_STOP
    assert suggest_mapping("brightness_step_up").action == ButtonAction.BRIGHTNESS_UP
    # Unknown device words get no default: they stay unassigned.
    for raw in ("1_single", "arrow_left_click", "on_long_press_extra_words", "scene_3"):
        assert suggest_mapping(raw) is None, raw


@pytest.mark.parametrize(("payload", "expected"), [
    ('{"action": "on_right", "battery": 100, "linkquality": 120}', ("on_right", "ok")),
    (b'{"action": "off_left"}', ("off_left", "ok")),
    ('{"battery": 97, "linkquality": 80}', (None, "no_action")),   # battery / link quality update
    ('{"action": "", "linkquality": 80}', (None, "no_action")),    # cleared action
    ('{"action": null}', (None, "no_action")),
    ("{not json", (None, "malformed")),
    ("[1, 2]", (None, "malformed")),
    ('{"action": 5}', (None, "no_action")),
    ('{"action": "rm -rf /"}', (None, "invalid_action")),
    ('"on_right"', ("on_right", "ok")),
    ("on_right", ("on_right", "ok")),                                # <topic>/action payload
    ("", (None, "no_action")),
    (b"\xff\xfe", (None, "malformed")),
    ("x" * 70000, (None, "payload_too_large")),
])
def test_mqtt_payloads(payload, expected) -> None:
    assert parse_mqtt_payload(payload) == expected


def test_event_entity_actions() -> None:
    assert event_entity_action({"event_type": "on_right"}) == "on_right"
    assert event_entity_action({"event_type": "press", "button": "left"}) == "left_press"
    assert event_entity_action({"event_type": "left_press", "button": "left"}) == "left_press"
    assert event_entity_action({"event_type": "double", "button": 2}) == "2_double"
    assert event_entity_action({}) is None and event_entity_action({"event_type": "a b c ;"}) is None


@pytest.mark.parametrize(("topic", "error"), [
    ("zigbee2mqtt/avb_ksu", None),
    ("zigbee2mqtt/living room/switch", None),
    ("zigbee2mqtt/#", "topic_wildcard"),
    ("zigbee2mqtt/+/action", "topic_wildcard"),
    ("#", "topic_wildcard"),
    ("zigbee2mqtt/bridge/devices", "topic_reserved"),
    ("homeassistant/device_automation/x/config", "topic_reserved"),
    ("avb_ksu", "topic_invalid"),
    ("$SYS/broker", "topic_invalid"),
    ("/zigbee2mqtt/x", "topic_invalid"),
    ("zigbee2mqtt//x", "topic_invalid"),
    ("", "topic_empty"),
    (None, "topic_empty"),
])
def test_topics_are_validated(topic, error) -> None:
    assert validate_topic(topic) == error


def test_base_topic() -> None:
    assert validate_base_topic("zigbee2mqtt") is None and validate_base_topic("home/z2m") is None
    assert validate_base_topic("z2m/#") == "topic_wildcard" and validate_base_topic("homeassistant") == "topic_invalid"


def ev(raw: str, source: str, at: float, ref: str | None = None) -> ButtonEvent:
    return ButtonEvent.create("kamar", "sw1", source, raw, at, "2026-11-11T20:00:00+00:00", ref)


def test_deduplication_across_paths_keeps_deliberate_repeats() -> None:
    dedupe = Deduplicator()
    assert dedupe.accept(ev("on_right", "zigbee2mqtt", 10.0), 100) == (True, None)
    # The same press through the device trigger and the event entity: duplicates.
    assert dedupe.accept(ev("on_right", "device_trigger", 10.05), 100) == (False, "duplicate")
    assert dedupe.accept(ev("on_right", "event_entity", 10.2, "e@1"), 100) == (False, "duplicate")
    # A radio retransmission within the debounce time.
    assert dedupe.accept(ev("on_right", "zigbee2mqtt", 10.08), 100) == (False, "debounced")
    # A second deliberate press (same path, after the debounce time) counts.
    assert dedupe.accept(ev("on_right", "zigbee2mqtt", 10.6), 100) == (True, None)
    # Another button at the same moment is a different press.
    assert dedupe.accept(ev("off_right", "device_trigger", 10.6), 100) == (True, None)
    # Debounce 0 keeps even fast repeats on one path.
    assert dedupe.accept(ev("on_right", "zigbee2mqtt", 10.62), 0) == (True, None)
    # An event entity reference is used once.
    assert dedupe.accept(ev("on_left", "event_entity", 20.0, "e@2"), 100) == (True, None)
    assert dedupe.accept(ev("on_left", "event_entity", 25.0, "e@2"), 100) == (False, "duplicate")
    dedupe.forget("sw1")
    assert dedupe.accept(ev("on_right", "device_trigger", 25.1), 100) == (True, None)


def test_switch_round_trip_and_bounds() -> None:
    data = {
        "id": "sw1", "name": "avb_ksu", "device_id": "dev", "ieee": "0x000b57fffe8a1b2c", "z2m_name": "avb_ksu",
        "topic": "zigbee2mqtt/#",  # never accepted, also not from storage
        "source": "nope", "mode": "control",
        "buttons": {"on_right": {"source": "zigbee2mqtt"}, "bad action!": {}},
        "mappings": {"on_right": {"action": "on", "on_values": "fixed", "brightness": 400, "color_temp": 20},
                     "off_right": {"action": "nope"}},
        "dimming": {"step": 0, "interval": 10, "min_brightness": 90, "max_brightness": 10, "timeout": 999},
        "debounce_ms": -5, "override_minutes": 99999,
    }
    switch = SwitchConfig.from_dict(data)
    assert switch.topic is None and switch.source == SwitchSource.AUTO and switch.mode == SwitchMode.CONTROL
    assert list(switch.buttons) == ["on_right"]
    m = switch.mappings["on_right"]
    assert (m.action, m.on_values, m.brightness, m.color_temp) == (ButtonAction.ON, OnValues.FIXED, 100, 1000)
    assert switch.mappings["off_right"].action == ButtonAction.NONE and switch.mapping("off_right") is None
    assert switch.dimming == DimmingConfig(step=1, interval=2.0, min_brightness=10, max_brightness=90, timeout=60)
    assert switch.debounce_ms == 0 and switch.override_minutes == 1440
    assert SwitchConfig.from_dict(switch.as_dict()) == switch
    assert SwitchConfig.from_dict({"name": "no id"}) is None
    # Adding a learned button suggests a default only once and never overwrites a choice.
    switch = switch.with_button("off_left", "zigbee2mqtt", None)
    assert switch.mappings["off_left"].action == ButtonAction.OFF
    assert switch.with_button("off_left", "event_entity", None) is switch
    assert ButtonMapping(ButtonAction.MOOD, mood="Hygge").describe() == "Select Hygge"
    assert ButtonMapping(ButtonAction.MOOD_AUTO, scope="global").describe() == "Return to Auto mood (all rooms)"


def test_store_migration_keeps_existing_settings_and_legacy_switches() -> None:
    """1.2 -> 1.3: every room starts without switches; wall switch entities stay as they are (listen only)."""
    stored = {
        "global_profiles": {"Veitsla": {"lamps": {"action": "on", "rgb_color": [255, 0, 0]}}},
        "manual_override_minutes": 45,
        "rooms": {"kamar_sudur": {"control_enabled": True, "manual_switches": ["event.old_switch"],
                                  "manual_override_minutes": 20, "mood_override": "Hygge",
                                  "daylight": {"on_first_motion": False, "auto_on": "dark"}}},
        "adaptive": {"brightness_entity": "sensor.adaptive_brightness"},
    }
    migrated = migrate_store(1, 2, json.loads(json.dumps(stored)))
    assert migrated["rooms"]["kamar_sudur"]["switches"] == []
    config = LightingConfig.from_dict(migrated)
    room = config.room("kamar_sudur")
    assert room.manual_switches == ("event.old_switch",) and room.switches == ()
    assert room.control_enabled and room.mood_override == "Hygge" and room.manual_override_minutes == 20
    assert room.daylight.on_first_motion is False
    assert "Veitsla" in config.global_profiles  # option values are never renamed
    assert config.adaptive.brightness_entity == "sensor.adaptive_brightness" and config.manual_override_minutes == 45
    assert config.switch_settings.mqtt_base_topic == "zigbee2mqtt"
    # From 1.1 the 0.3.3 step still runs first.
    older = migrate_store(1, 1, {"rooms": {"r": {"control_enabled": True}}})
    assert older["rooms"]["r"]["daylight"] == {"on_first_motion": False} and older["rooms"]["r"]["switches"] == []
    assert LightingConfig.from_dict(config.as_dict()) == config


def test_entity_renames_reach_switch_settings() -> None:
    switch = SwitchConfig.from_dict({
        "id": "sw1", "name": "avb_ksu", "event_entity": "event.avb_ksu_action",
        "mappings": {"on_right": {"action": "on", "target": "light.kamar_spots"},
                     "on_left": {"action": "scene", "entity": "scene.kvold"}},
    })
    config = LightingConfig.from_dict({"rooms": {"kamar": {"switches": [switch.as_dict()]}}})
    renamed = rename_entity(config, "event.avb_ksu_action", "event.kamar_switch_action")
    assert renamed.room("kamar").switches[0].event_entity == "event.kamar_switch_action"
    renamed = rename_entity(renamed, "light.kamar_spots", "light.kamar_downlights")
    assert renamed.room("kamar").switches[0].mappings["on_right"].target == "light.kamar_downlights"
    renamed = rename_entity(renamed, "scene.kvold", "scene.kvoldseta")
    assert renamed.room("kamar").switches[0].mappings["on_left"].entity == "scene.kvoldseta"
    assert rename_entity(renamed, "light.unrelated", "light.other") is None


def test_bridge_devices_and_bindings() -> None:
    payload = json.dumps([
        {"ieee_address": "0x00124B0000000001", "type": "Coordinator", "friendly_name": "Coordinator"},
        {"ieee_address": "0x000B57FFFE8A1B2C", "type": "EndDevice", "friendly_name": "avb_ksu",
         "definition": {"model": "WDE002906", "vendor": "Schneider Electric", "exposes": [
             {"type": "composite", "features": [{"name": "action", "values": ["on_left", "off_left", "bad value!"]}]}]},
         "endpoints": {"21": {"bindings": [
             {"cluster": "genOnOff", "target": {"type": "endpoint", "ieee_address": "0x00124b0000000001"}},
             {"cluster": "genLevelCtrl", "target": {"type": "group", "id": 3}},
             {"cluster": "genPowerCfg", "target": {"type": "group", "id": 3}}]}}},
        {"no": "ieee"}, "garbage",
    ])
    devices, coordinator = parse_bridge_devices(payload)
    assert coordinator == "0x00124b0000000001" and list(devices) == ["0x000b57fffe8a1b2c"]
    wiser = devices["0x000b57fffe8a1b2c"]
    assert wiser.actions == ("on_left", "off_left") and wiser.vendor == "Schneider Electric"
    assert light_bindings(wiser, coordinator) == [{"cluster": "genLevelCtrl", "endpoint": "21", "target": "group 3"}]
    assert parse_bridge_devices("{bad") == ({}, None) and parse_bridge_devices('{"a": 1}') == ({}, None)


def test_authoritative_source() -> None:
    auto = SwitchConfig(id="a", name="a")
    assert resolve_source(auto, ["event_entity", "device_trigger", "zigbee2mqtt"]) == "zigbee2mqtt"
    assert resolve_source(auto, ["event_entity", "device_trigger"]) == "device_trigger"
    assert resolve_source(auto, []) is None
    chosen = SwitchConfig(id="a", name="a", source=SwitchSource.EVENT_ENTITY)
    assert resolve_source(chosen, ["zigbee2mqtt", "event_entity"]) == "event_entity"
    assert resolve_source(chosen, ["zigbee2mqtt"]) is None
