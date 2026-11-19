"""Pure unit tests: lighting store model, mood inheritance, field-level resolution."""

from __future__ import annotations

import pytest

from custom_components.smart_occupancy.const import LightAction, UnattributedPolicy, UnrepresentableMode
from custom_components.smart_occupancy.lighting_config import (
    LightingConfig,
    RoomLighting,
    UnrepresentableHandling,
    parent_cycle,
    rename_entity,
    seed_from_options,
    set_profile,
    table_get,
    without_categories,
)
from custom_components.smart_occupancy.models import IntegrationConfig, ProfileSetting
from custom_components.smart_occupancy.profiles import mood_chain, resolve

ON = LightAction.ON


def _cfg(**kw) -> LightingConfig:
    return LightingConfig(**kw)


def _levels(r) -> dict[str, str]:
    return {k: f"{v['level']}:{v['mood']}" for k, v in r.sources.items()}


def test_spec_example_field_level_inheritance() -> None:
    """Global Hygge lamps 70 %/2700 K; room 50 %; desk lamp 80 % -> kelvin inherited at every level."""
    cfg = _cfg(
        global_profiles={"Hygge": {"lamps": ProfileSetting(ON, brightness_pct=70, color_temp_kelvin=2700)}},
        rooms={"kamar": RoomLighting(
            mood_overrides={"Hygge": {"lamps": ProfileSetting(brightness_pct=50)}},
            light_overrides={"Hygge": {"light.desk": ProfileSetting(brightness_pct=80)}},
        )},
    )
    room_lamp = resolve(cfg, "Hygge", "kamar", "light.floor", "lamps")
    assert (room_lamp.action, room_lamp.brightness_pct, room_lamp.color_temp_kelvin) == (ON, 50, 2700)
    assert _levels(room_lamp) == {"action": "global:Hygge", "brightness_pct": "room:Hygge",
                                  "color_temp_kelvin": "global:Hygge"}
    desk = resolve(cfg, "Hygge", "kamar", "light.desk", "lamps")
    assert (desk.brightness_pct, desk.color_temp_kelvin) == (80, 2700)
    assert _levels(desk)["brightness_pct"] == "light:Hygge"
    assert _levels(desk)["color_temp_kelvin"] == "global:Hygge"
    # Another room without overrides gets the global values.
    other = resolve(cfg, "Hygge", "stova", "light.x", "lamps")
    assert (other.brightness_pct, other.color_temp_kelvin) == (70, 2700)


def test_parent_and_fallback_moods() -> None:
    cfg = _cfg(
        global_profiles={
            "Default": {"downlights": ProfileSetting(ON, brightness_pct=100, color_temp_kelvin=4000, transition=2)},
            "Kvøld": {"downlights": ProfileSetting(ON, brightness_pct=50, color_temp_kelvin=3000)},
            "Hygge": {"downlights": ProfileSetting(color_temp_kelvin=2400)},
        },
        parents={"Hygge": "Kvøld"},
        fallback_mood="Default",
    )
    assert mood_chain(cfg, "Hygge") == ["Hygge", "Kvøld", "Default"]
    assert mood_chain(cfg, "Default") == ["Default"]
    r = resolve(cfg, "Hygge", None, None, "downlights")
    assert (r.action, r.brightness_pct, r.color_temp_kelvin, r.transition) == (ON, 50, 2400, 2)
    assert _levels(r) == {"action": "global:Kvøld", "brightness_pct": "global:Kvøld",
                          "color_temp_kelvin": "global:Hygge", "transition": "global:Default"}
    # A mood without any entry inherits everything from the fallback.
    assert resolve(cfg, "Morgun", None, None, "downlights").brightness_pct == 100


def test_inheritance_loops_are_refused_and_cut_off() -> None:
    cfg = _cfg(parents={"A": "B", "B": "C"})
    assert parent_cycle(cfg, "C", "A") == ["C", "A", "B", "C"]
    assert parent_cycle(cfg, "A", "A") == ["A", "A"]
    assert parent_cycle(cfg, "C", "D") is None
    assert parent_cycle(cfg, "A", None) is None
    looped = _cfg(parents={"A": "B", "B": "A"}, fallback_mood="A")
    assert mood_chain(looped, "A") == ["A", "B"]  # terminates


def test_ignore_inherit_and_off() -> None:
    cfg = _cfg(global_profiles={
        "Nátt": {"downlights": ProfileSetting(LightAction.OFF, transition=3),
                 "lamps": ProfileSetting(LightAction.IGNORE)},
    }, rooms={"r": RoomLighting(mood_overrides={"Nátt": {"downlights": ProfileSetting(brightness_pct=10)}})})
    off = resolve(cfg, "Nátt", "r", "light.d", "downlights")
    assert off.action is LightAction.OFF and off.brightness_pct is None and off.transition == 3
    assert resolve(cfg, "Nátt", "r", "light.l", "lamps").action is LightAction.IGNORE
    # Nothing configured at all: ignore (no command).
    assert resolve(cfg, "Dagur", "r", "light.l", "lamps").action is LightAction.IGNORE
    # A light without a category only follows its own override.
    assert resolve(cfg, "Nátt", "r", "light.none", None).action is LightAction.IGNORE


def test_colour_settings_replace_each_other() -> None:
    cfg = _cfg(
        global_profiles={"Kvøld": {"led": ProfileSetting(ON, color_temp_kelvin=2400)}},
        rooms={"r": RoomLighting(light_overrides={"Kvøld": {"light.s": ProfileSetting(hs_color=(30, 90))}})},
    )
    r = resolve(cfg, "Kvøld", "r", "light.s", "led")
    assert r.hs_color == (30, 90) and r.color_temp_kelvin is None and "color_temp_kelvin" not in r.sources


def test_resolution_levels_give_inherited_values() -> None:
    cfg = _cfg(
        global_profiles={"Hygge": {"lamps": ProfileSetting(ON, brightness_pct=70)}},
        rooms={"r": RoomLighting(mood_overrides={"Hygge": {"lamps": ProfileSetting(brightness_pct=50)}})},
    )
    assert resolve(cfg, "Hygge", "r", "light.x", "lamps", levels=("global",)).brightness_pct == 70
    assert resolve(cfg, "Hygge", "r", "light.x", "lamps").brightness_pct == 50


def test_mood_names_match_case_insensitively() -> None:
    cfg = _cfg(global_profiles={"Hygge": {"lamps": ProfileSetting(ON)}}, parents={"hygge": "Kvøld"})
    assert table_get(cfg.global_profiles, "HYGGE") and cfg.parent_of("Hygge") == "Kvøld"
    assert resolve(cfg, "hygge", None, None, "lamps").action is ON


def test_set_profile_and_remove() -> None:
    cfg = set_profile(LightingConfig(), "global", "Hygge", "lamps", ProfileSetting(ON, brightness_pct=70))
    cfg = set_profile(cfg, "room", "Hygge", "lamps", ProfileSetting(brightness_pct=40), "r")
    cfg = set_profile(cfg, "light", "Hygge", "light.d", ProfileSetting(brightness_pct=80), "r")
    assert cfg.rooms["r"].light_overrides["Hygge"]["light.d"].brightness_pct == 80
    cfg = set_profile(cfg, "light", "hygge", "light.d", None, "r")  # stored spelling is reused
    assert cfg.rooms["r"].light_overrides == {}
    cfg = set_profile(cfg, "global", "Hygge", "lamps", ProfileSetting())  # empty entry = remove
    assert cfg.global_profiles == {}
    with pytest.raises(ValueError):
        set_profile(cfg, "room", "Hygge", "lamps", ProfileSetting(ON))


def test_round_trip_and_defaults() -> None:
    cfg = LightingConfig(
        global_profiles={"Hygge": {"lamps": ProfileSetting(ON, brightness_pct=70, hs_color=(30.0, 80.0),
                                                            persistent=True)}},
        parents={"Hygge": "Kvøld"}, fallback_mood="Default",
        unrepresentable={"Onki": UnrepresentableHandling(UnrepresentableMode.NO_MOOD)},
        exclude_label="no_auto", manual_override_minutes=30, unattributed=UnattributedPolicy.MANUAL,
        rooms={"r": RoomLighting(control_enabled=True, acknowledged=("automation.a",), mood_override="Hygge",
                                 excluded=("light.x",), manual_switches=("event.sw",), manual_override_minutes=0)},
    )
    assert LightingConfig.from_dict(cfg.as_dict()) == cfg
    empty = LightingConfig.from_dict({"rooms": {"r": {}}, "manual_override_minutes": "junk"})
    assert empty.room("r").control_enabled is False and empty.room("r").auto_discover is True
    assert empty.manual_override_minutes == 60
    assert empty.handling_for("Onki").mode is UnrepresentableMode.FOLLOW_MAIN
    assert cfg.override_minutes("r") == 0 and cfg.override_minutes("other") == 30
    assert cfg.active_rooms == ["r"]


def test_seed_from_phase_02_options_keeps_profiles_and_starts_in_observe() -> None:
    options = IntegrationConfig.from_dict({
        "global_profiles": {"Kvøld": {"lamps": {"action": "on", "brightness_pct": 60}}},
        "rooms": {
            "a": {"enabled": True, "mood_overrides": {"Kvøld": {"lamps": {"brightness_pct": 40}}},
                  "light_overrides": {"Kvøld": {"light.l": {"action": "off"}}}},
            "b": {"enabled": True},
        },
    })
    cfg = seed_from_options(options)
    assert cfg.global_profiles["Kvøld"]["lamps"].brightness_pct == 60
    assert cfg.rooms["a"].mood_overrides["Kvøld"]["lamps"].brightness_pct == 40
    assert cfg.rooms["a"].light_overrides["Kvøld"]["light.l"].action is LightAction.OFF
    assert "b" not in cfg.rooms
    assert cfg.active_rooms == []


def test_rename_and_category_removal() -> None:
    cfg = LightingConfig(
        global_profiles={"Hygge": {"lamps": ProfileSetting(ON), "led": ProfileSetting(ON)}},
        rooms={"r": RoomLighting(excluded=("light.old",), acknowledged=("automation.old",),
                                 light_overrides={"Hygge": {"light.old": ProfileSetting(ON)}},
                                 mood_overrides={"Hygge": {"led": ProfileSetting(brightness_pct=5)}})},
    )
    renamed = rename_entity(cfg, "light.old", "light.new")
    assert renamed.rooms["r"].excluded == ("light.new",)
    assert "light.new" in renamed.rooms["r"].light_overrides["Hygge"]
    assert rename_entity(cfg, "light.unused", "light.z") is None
    assert rename_entity(cfg, "automation.old", "automation.new").rooms["r"].acknowledged == ("automation.new",)
    stripped = without_categories(cfg, {"led"})
    assert set(stripped.global_profiles["Hygge"]) == {"lamps"}
    assert stripped.rooms["r"].mood_overrides["Hygge"] == {}
