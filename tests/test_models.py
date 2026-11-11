"""Pure unit tests: configuration model, profile inheritance, mood rule, mode gate."""

from __future__ import annotations

import copy

import pytest

from custom_components.smart_occupancy.const import CONFIG_VERSION, TIMING_PARAMETERS, LightAction, OperatingMode
from custom_components.smart_occupancy.models import (
    IntegrationConfig,
    ProfileSetting,
    migrate_options,
    rename_entity_references,
    unique_category_key,
)
from custom_components.smart_occupancy.lighting_config import LightingConfig, seed_from_options
from custom_components.smart_occupancy.mood import expected_main_mood
from custom_components.smart_occupancy.profiles import resolve, validate_setting
from custom_components.smart_occupancy.supervision import (
    ControlNotPermittedError,
    assert_control_permitted,
    control_permitted,
    effective_mode,
)

# A Phase 0.1.1 payload (CONFIG_VERSION 1), as stored by the previous release.
RAW_V1 = {
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

RAW = migrate_options(RAW_V1)


def test_round_trip() -> None:
    assert IntegrationConfig.from_dict(RAW).as_dict() == RAW
    full = copy.deepcopy(RAW)
    full["rooms"]["room1"]["timing"] = {"exit_confirmation": 120}
    full["rooms"]["room1"]["adjacent_rooms"] = ["hall"]
    full["rooms"]["room1"]["sensors"][0]["inverted"] = True
    full["occupancy"]["inactivity_timeout"] = 900
    assert IntegrationConfig.from_dict(full).as_dict() == full


def test_migration_v1_to_v2_keeps_everything() -> None:
    migrated = migrate_options(copy.deepcopy(RAW_V1))
    assert migrated["config_version"] == CONFIG_VERSION == 2
    # Every v1 value is carried over unchanged.
    for key in ("operating_mode", "mood", "global_profiles"):
        assert migrated[key] == RAW_V1[key]
    # Categories are kept; since 0.3.1 each also gets its kind (derived for old entries).
    assert [{k: v for k, v in c.items() if k != "kind"} for c in migrated["categories"]] == RAW_V1["categories"]
    assert [c["kind"] for c in migrated["categories"]] == ["source"]
    room, old = migrated["rooms"]["room1"], RAW_V1["rooms"]["room1"]
    for key in ("enabled", "lights", "reference_occupancy_entity", "mood_overrides", "light_overrides"):
        assert room[key] == old[key]
    assert room["sensors"] == [{**old["sensors"][0], "inverted": False}]
    # New keys get defaults.
    assert room["timing"] == {} and room["adjacent_rooms"] == []
    assert migrated["occupancy"] == {k: v[0] for k, v in TIMING_PARAMETERS.items()}
    # Idempotent.
    assert migrate_options(migrated) == migrated


def test_timing_overrides_and_bounds() -> None:
    raw = copy.deepcopy(RAW)
    raw["occupancy"]["exit_confirmation"] = 999999          # clamped to max
    raw["rooms"]["room1"]["timing"] = {"dropout_grace": 5, "bogus": 1, "inactivity_timeout": "x"}
    cfg = IntegrationConfig.from_dict(raw)
    assert cfg.occupancy.exit_confirmation == TIMING_PARAMETERS["exit_confirmation"][2]
    assert cfg.rooms["room1"].timing == {"dropout_grace": 5}
    timing = cfg.timing_for("room1")
    assert timing.dropout_grace == 5 and timing.inactivity_timeout == 600
    assert cfg.timing_for("unknown_room") == cfg.occupancy


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


def _lighting(raw: dict) -> LightingConfig:
    """Profiles of a Phase 0.2 options payload, as copied into the lighting store."""
    return seed_from_options(IntegrationConfig.from_dict(raw))


def test_inheritance_global_room_light() -> None:
    cfg = _lighting(RAW)
    r = resolve(cfg, "Kvøld", "room1", "light.l", "lamps")
    assert r.action is LightAction.ON
    assert r.brightness_pct == 50          # room overrides global
    assert r.rgb_color == (255, 0, 0)      # light override ...
    assert r.color_temp_kelvin is None     # ... clears the inherited colour temperature
    assert r.transition == 1.5             # inherited from global
    assert {k: v["level"] for k, v in r.sources.items()} == {
        "action": "light", "brightness_pct": "room", "rgb_color": "light", "transition": "global",
    }


def test_resolution_without_profiles_is_ignore() -> None:
    cfg = _lighting(RAW)
    assert resolve(cfg, "Dagur", "room1", "light.l", "lamps").action is LightAction.IGNORE
    assert resolve(cfg, "Kvøld", "room1", "light.other", None).action is LightAction.IGNORE


def test_off_clears_attributes() -> None:
    raw = {**RAW, "rooms": {"room1": {**RAW["rooms"]["room1"], "light_overrides": {"Kvøld": {"light.l": {"action": "off"}}}}}}
    r = resolve(_lighting(raw), "Kvøld", "room1", "light.l", "lamps")
    assert r.action is LightAction.OFF and r.brightness_pct is None and r.color_temp_kelvin is None
    assert r.transition == 1.5


@pytest.mark.parametrize(
    ("setting", "errors"),
    [
        (ProfileSetting(LightAction.ON, brightness_pct=101), {"brightness_pct": "invalid_brightness"}),
        (ProfileSetting(LightAction.ON, color_temp_kelvin=500), {"color_temp_kelvin": "invalid_kelvin"}),
        (ProfileSetting(LightAction.ON, rgb_color=(0, 0, 256)), {"rgb_color": "invalid_rgb"}),
        (ProfileSetting(LightAction.ON, color_temp_kelvin=3000, rgb_color=(1, 2, 3)), {"rgb_color": "several_colors"}),
        (ProfileSetting(LightAction.ON, rgb_color=(1, 2, 3), hs_color=(30, 50)), {"hs_color": "several_colors"}),
        (ProfileSetting(LightAction.ON, hs_color=(400, 50)), {"hs_color": "invalid_hs"}),
        (ProfileSetting(LightAction.ON, brightness_pct=0), {"brightness_pct": "invalid_brightness"}),
        (ProfileSetting(LightAction.ON, transition=-1), {"transition": "invalid_transition"}),
        (ProfileSetting(LightAction.OFF, brightness_pct=10), {"action": "off_with_attributes"}),
        (ProfileSetting(LightAction.ON, brightness_pct=1, color_temp_kelvin=2700, transition=0), {}),
        (ProfileSetting(LightAction.ON, hs_color=(30.5, 80), persistent=True), {}),
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
