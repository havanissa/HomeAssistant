"""Pure unit tests: configuration model, profile inheritance, mood rule, mode gate."""

from __future__ import annotations

import pytest

from custom_components.smart_occupancy.const import LightAction, OperatingMode
from custom_components.smart_occupancy.models import (
    IntegrationConfig,
    ProfileSetting,
    rename_entity_references,
    unique_category_key,
)
from custom_components.smart_occupancy.mood import expected_main_mood
from custom_components.smart_occupancy.profiles import resolve, validate_setting
from custom_components.smart_occupancy.supervision import (
    ControlNotPermittedError,
    assert_control_permitted,
    control_permitted,
    effective_mode,
)

RAW = {
    "config_version": 1,
    "operating_mode": "observe",
    "mood": {"auto_mood_entity": "input_select.a", "manual_mood_entity": "input_select.m",
             "main_mood_entity": "input_select.x", "auto_option": "Auto", "mismatch_grace_seconds": 30},
    "categories": [{"key": "lamps", "name": "Lamps", "label_id": "lamps"}],
    "rooms": {
        "room1": {
            "enabled": True,
            "sensors": [{"entity_id": "binary_sensor.r", "kind": "radar", "role": "maintain"}],
            "lights": ["light.l"],
            "reference_occupancy_entity": "input_boolean.occ",
            "mood_overrides": {"Kvøld": {"lamps": {"action": "inherit", "brightness_pct": 50}}},
            "light_overrides": {"Kvøld": {"light.l": {"action": "on", "rgb_color": [255, 0, 0]}}},
        }
    },
    "global_profiles": {"Kvøld": {"lamps": {"action": "on", "brightness_pct": 75,
                                            "color_temp_kelvin": 3000, "transition": 1.5}}},
}


def test_round_trip() -> None:
    assert IntegrationConfig.from_dict(RAW).as_dict() == RAW


def test_unknown_enum_values_fall_back_safely() -> None:
    raw = {**RAW, "operating_mode": "turbo"}
    raw["rooms"] = {"r": {"enabled": True, "sensors": [{"entity_id": "x.y", "kind": "laser", "role": "??"}]}}
    cfg = IntegrationConfig.from_dict(raw)
    assert cfg.operating_mode is OperatingMode.OBSERVE
    assert cfg.rooms["r"].sensors[0].kind.value == "other"
    assert cfg.rooms["r"].sensors[0].role.value == "ignore"


def test_rename_references() -> None:
    out = rename_entity_references(RAW, "light.l", "light.new")
    assert out["rooms"]["room1"]["lights"] == ["light.new"]
    assert "light.new" in out["rooms"]["room1"]["light_overrides"]["Kvøld"]
    assert rename_entity_references(RAW, "light.unused", "light.z") is None


def test_unique_category_key() -> None:
    assert unique_category_key("LED strips", set()) == "led_strips"
    assert unique_category_key("LED strips", {"led_strips"}) == "led_strips_2"
    assert unique_category_key("Ljós úti!", set()) == "ljós_úti"


def test_inheritance_global_room_light() -> None:
    cfg = IntegrationConfig.from_dict(RAW)
    r = resolve(cfg, "Kvøld", "room1", "light.l", "lamps")
    assert r.action is LightAction.ON
    assert r.brightness_pct == 50          # room overrides global
    assert r.rgb_color == (255, 0, 0)      # light override ...
    assert r.color_temp_kelvin is None     # ... clears the inherited colour temperature
    assert r.transition == 1.5             # inherited from global
    assert r.sources == {"action": "light", "brightness_pct": "room", "rgb_color": "light", "transition": "global"}


def test_resolution_without_profiles_is_ignore() -> None:
    cfg = IntegrationConfig.from_dict(RAW)
    assert resolve(cfg, "Dagur", "room1", "light.l", "lamps").action is LightAction.IGNORE
    assert resolve(cfg, "Kvøld", "room1", "light.other", None).action is LightAction.IGNORE


def test_off_clears_attributes() -> None:
    raw = {**RAW, "rooms": {"room1": {**RAW["rooms"]["room1"], "light_overrides": {"Kvøld": {"light.l": {"action": "off"}}}}}}
    r = resolve(IntegrationConfig.from_dict(raw), "Kvøld", "room1", "light.l", "lamps")
    assert r.action is LightAction.OFF and r.brightness_pct is None and r.color_temp_kelvin is None
    assert r.transition == 1.5


@pytest.mark.parametrize(
    ("setting", "errors"),
    [
        (ProfileSetting(LightAction.ON, brightness_pct=101), {"brightness_pct": "invalid_brightness"}),
        (ProfileSetting(LightAction.ON, color_temp_kelvin=500), {"color_temp_kelvin": "invalid_kelvin"}),
        (ProfileSetting(LightAction.ON, rgb_color=(0, 0, 256)), {"rgb_color": "invalid_rgb"}),
        (ProfileSetting(LightAction.ON, color_temp_kelvin=3000, rgb_color=(1, 2, 3)), {"rgb_color": "kelvin_and_rgb"}),
        (ProfileSetting(LightAction.ON, transition=-1), {"transition": "invalid_transition"}),
        (ProfileSetting(LightAction.OFF, brightness_pct=10), {"action": "off_with_attributes"}),
        (ProfileSetting(LightAction.ON, brightness_pct=0, color_temp_kelvin=2700, transition=0), {}),
    ],
)
def test_validate_setting(setting: ProfileSetting, errors: dict) -> None:
    assert validate_setting(setting) == errors


@pytest.mark.parametrize(
    ("auto", "manual", "expected"),
    [
        ("Dagur", "Auto", ("Dagur", "auto")),
        ("Dagur", "auto", ("Dagur", "auto")),   # case-insensitive
        ("Dagur", "Nátt", ("Nátt", "manual")),
        ("Dagur", None, (None, None)),
        (None, "Auto", (None, "auto")),
    ],
)
def test_existing_mood_rule(auto, manual, expected) -> None:
    assert expected_main_mood(auto, manual, "Auto") == expected


@pytest.mark.parametrize("mode", list(OperatingMode))
def test_lighting_control_never_permitted(mode: OperatingMode) -> None:
    assert effective_mode(mode) is OperatingMode.OBSERVE
    assert control_permitted(mode) is False
    with pytest.raises(ControlNotPermittedError):
        assert_control_permitted(mode)
