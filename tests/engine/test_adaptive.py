"""Global light adjustment (0.3.2), pure logic: source normalisation, modes, offsets, gradual updates."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import State

from custom_components.smart_occupancy.adaptive import (
    AdaptiveSent,
    GlobalSource,
    GlobalValues,
    adaptive_update,
    apply_global,
    read_brightness,
    read_color_temp,
)
from custom_components.smart_occupancy.const import (
    ALL_MOODS,
    AdjustMode,
    LightAction,
    SourceScale,
)
from custom_components.smart_occupancy.lighting import Capabilities
from custom_components.smart_occupancy.lighting_config import (
    AdaptiveConfig,
    LightingConfig,
    RoomLighting,
)
from custom_components.smart_occupancy.models import ProfileSetting
from custom_components.smart_occupancy.planner import Target, build_command, build_plan
from custom_components.smart_occupancy.profiles import resolve, validate_setting


def st(entity_id: str, state: Any, **attrs: Any) -> State:
    return State(entity_id, str(state), attrs)


# ------------------------------------------------------------- normalisation
@pytest.mark.parametrize(("state", "scale", "expected", "unit"), [
    (st("sensor.global_brightness", 62, unit_of_measurement="%"), SourceScale.AUTO, 62, "%"),
    (st("input_number.brightness", 128, max=255), SourceScale.AUTO, 128 / 255 * 100, "0-255"),
    (st("input_number.brightness", 80, max=255), SourceScale.AUTO, 80 / 255 * 100, "0-255"),
    (st("sensor.raw", 200), SourceScale.AUTO, 200 / 255 * 100, "0-255"),
    (st("sensor.raw", 80), SourceScale.BYTE, 80 / 255 * 100, "0-255"),
    (st("sensor.raw", 80), SourceScale.AUTO, 80, "%"),
    (st("light.reference", "on", brightness=255), SourceScale.AUTO, 100, "0-255"),
    (st("switch.adaptive_lighting_stova", "on", brightness_pct=45, color_temp_kelvin=2900), SourceScale.AUTO, 45, "%"),
])
def test_brightness_sources(state: State, scale: SourceScale, expected: float, unit: str) -> None:
    r = read_brightness(state, state.entity_id, scale=scale)
    assert r.ok and r.value == pytest.approx(expected) and r.unit == unit


@pytest.mark.parametrize("state", [
    None,
    st("sensor.b", "unavailable"),
    st("sensor.b", "unknown"),
    st("sensor.b", "high"),
    st("sensor.b", 300),
    st("sensor.b", -5, unit_of_measurement="%"),
    st("light.reference", "off"),
])
def test_brightness_unusable_is_never_zero(state: State | None) -> None:
    r = read_brightness(state, "sensor.b")
    assert not r.ok and r.value is None and r.detail


@pytest.mark.parametrize(("state", "attribute", "expected"), [
    (st("sensor.global_ct", 3200, unit_of_measurement="K"), None, 3200),
    (st("sensor.global_ct", 3200), None, 3200),
    (st("sensor.global_ct", 370, unit_of_measurement="mired"), None, 1_000_000 / 370),
    (st("sensor.global_ct", 250), None, 4000),  # no unit, mired range
    (st("input_number.ct", 2700, unit_of_measurement="K"), None, 2700),
    (st("switch.adaptive_lighting_stova", "on", brightness_pct=45, color_temp_kelvin=2900), None, 2900),
    (st("switch.al", "on", color_temp_mired=400), None, 2500),
    (st("sensor.al_attrs", "ok", ct=3000), "ct", 3000),
    (st("light.reference", "on", color_mode="color_temp", color_temp_kelvin=3100), None, 3100),
])
def test_color_temp_sources(state: State, attribute: str | None, expected: float) -> None:
    r = read_color_temp(state, state.entity_id, attribute)
    assert r.ok and r.value == pytest.approx(expected)


@pytest.mark.parametrize("state", [
    st("sensor.ct", "unavailable"), st("sensor.ct", 20), st("sensor.ct", 20000, unit_of_measurement="K"),
    st("light.reference", "on", color_mode="hs", color_temp_kelvin=None), st("sensor.ct", "warm"),
])
def test_color_temp_unusable(state: State) -> None:
    assert not read_color_temp(state, state.entity_id).ok


# --------------------------------------------------------------- resolution
def cfg(global_profiles=None, **room) -> LightingConfig:
    return LightingConfig(global_profiles=global_profiles or {}, rooms={"kno": RoomLighting(**room)})


def ps(**kw: Any) -> ProfileSetting:
    return ProfileSetting.from_dict(kw)


VALUES = GlobalValues(
    brightness=GlobalSource("sensor.b", 62.4, "connected", ""),
    color_temp=GlobalSource("sensor.k", 3200, "connected", ""),
)


def eff(config: LightingConfig, mood: str, light: str | None = None, targets=("lamps",), values=VALUES):
    return apply_global(resolve(config, mood, "kno", light, list(targets)), values)


def test_fixed_profiles_stay_fixed_over_every_mood_entries() -> None:
    """Upgrade rule: a value without a mode is fixed and beats a less specific "follow global"."""
    config = cfg({
        ALL_MOODS: {"lamps": ps(brightness_mode="global", color_temp_mode="global")},
        "Hygge": {"lamps": ps(action="on", brightness_pct=30, color_temp_kelvin=2200)},
        "Dagur": {"lamps": ps(action="on")},
    })
    hygge = eff(config, "Hygge")
    assert (hygge.brightness_pct, hygge.color_temp_kelvin) == (30, 2200)
    assert hygge.adaptive["brightness"]["status"] == "fixed"
    dagur = eff(config, "Dagur")
    assert (dagur.brightness_pct, dagur.color_temp_kelvin) == (62, 3200)
    assert dagur.adaptive["brightness"] == {"mode": "global", "offset": 0, "global": 62.4, "source_status": "connected",
                                            "value": 62, "status": "applied", "clamped": False}
    # "*" alone never switches a light on: no action -> Ignore.
    assert eff(cfg({ALL_MOODS: {"lamps": ps(brightness_mode="global")}}), "Dagur").action == LightAction.IGNORE


def test_independent_modes_offsets_and_clamping() -> None:
    config = cfg({"Kvøld": {"lamps": ps(action="on", brightness_mode="offset", brightness_offset=-20,
                                        color_temp_mode="offset", color_temp_offset=-300)}})
    r = eff(config, "Kvøld")
    assert (r.brightness_pct, r.color_temp_kelvin) == (42, 2900)
    low = GlobalValues(GlobalSource("sensor.b", 10, "connected", ""), GlobalSource("sensor.k", 1100, "connected", ""))
    r = eff(config, "Kvøld", values=low)
    assert (r.brightness_pct, r.color_temp_kelvin) == (1, 1000)  # never 0 (no switch-off), never < 1000 K
    assert r.adaptive["brightness"]["clamped"] and r.adaptive["color_temp"]["clamped"]
    # Follow global brightness + fixed 2700 K; fixed 60 % + follow global temperature.
    mixed = cfg({"A": {"lamps": ps(action="on", brightness_mode="global", color_temp_kelvin=2700)},
                 "B": {"lamps": ps(action="on", brightness_pct=60, color_temp_mode="global")}})
    a, b = eff(mixed, "A"), eff(mixed, "B")
    assert (a.brightness_pct, a.color_temp_kelvin) == (62, 2700)
    assert (b.brightness_pct, b.color_temp_kelvin) == (60, 3200)
    # Ignore leaves the property unchanged; offsets of other modes are dropped.
    ign = eff(cfg({"A": {"lamps": ps(action="on", brightness_mode="ignore", brightness_offset=5,
                                     color_temp_mode="ignore", color_temp_kelvin=2700)}}), "A")
    assert ign.brightness_pct is None and ign.color_temp_kelvin is None and ign.brightness_offset is None


def test_levels_room_and_light_every_mood_entries() -> None:
    config = cfg(
        {"Dagur": {"lamps": ps(action="on", brightness_pct=80, color_temp_kelvin=4000)}},
        mood_overrides={ALL_MOODS: {"lamps": ps(color_temp_mode="global")}},
        light_overrides={ALL_MOODS: {"light.desk": ps(brightness_mode="offset", brightness_offset=-20)}},
    )
    lamp = eff(config, "Dagur", "light.lamp")
    assert (lamp.brightness_pct, lamp.color_temp_kelvin) == (80, 3200)  # room "*" beats the global mood entry
    desk = eff(config, "Dagur", "light.desk")
    assert (desk.brightness_pct, desk.color_temp_kelvin) == (42, 3200)
    assert desk.sources["brightness_mode"]["level"] == "light" and desk.sources["brightness_mode"]["mood"] == ALL_MOODS
    # A mood-specific room entry beats the room "*" entry.
    config2 = cfg(mood_overrides={ALL_MOODS: {"lamps": ps(color_temp_mode="global")},
                                  "Hygge": {"lamps": ps(action="on", color_temp_kelvin=2200)}})
    assert eff(config2, "Hygge").color_temp_kelvin == 2200


def test_rgb_and_effects_keep_their_colour() -> None:
    config = cfg({
        ALL_MOODS: {"uplight": ps(color_temp_mode="global")},
        "Veistla": {"uplight": ps(action="on", rgb_color=[255, 150, 40], brightness_mode="offset", brightness_offset=-20)},
        "Party": {"uplight": ps(action="on", effect="rainbow", brightness_mode="global")},
    })
    r = eff(config, "Veistla", targets=("uplight",))
    assert r.rgb_color == (255, 150, 40) and r.color_temp_kelvin is None and r.color_temp_mode is None
    assert r.brightness_pct == 42
    p = eff(config, "Party", targets=("uplight",))
    assert p.color_temp_kelvin is None and p.adaptive["color_temp"]["status"] == "suppressed"
    assert p.brightness_pct == 62 and p.effect == "rainbow"
    _, data, _, _ = build_command(r, Capabilities(known=True, brightness=True, color_temp=True, color=True))
    assert data == {"brightness_pct": 42, "rgb_color": [255, 150, 40]}  # never RGB and a temperature together
    # A white mode at a more specific level replaces an inherited colour.
    w = eff(cfg({"Veistla": {"uplight": ps(action="on", rgb_color=[255, 0, 0])}},
                mood_overrides={"Veistla": {"uplight": ps(color_temp_mode="global")}}), "Veistla", targets=("uplight",))
    assert w.rgb_color is None and w.color_temp_kelvin == 3200


def test_unavailable_source_skips_property() -> None:
    values = GlobalValues(GlobalSource("sensor.b", None, "unavailable", "sensor.b is unavailable"),
                          GlobalSource("sensor.k", 3000, "stale", "last value"))
    r = eff(cfg({"Dagur": {"lamps": ps(action="on", brightness_mode="global", color_temp_mode="global")}}),
            "Dagur", values=values)
    assert r.brightness_pct is None and r.adaptive["brightness"]["status"] == "unavailable"
    assert r.color_temp_kelvin == 3000 and r.adaptive["color_temp"]["status"] == "stale"
    service, data, _, notes = build_command(r, Capabilities(known=True, brightness=True, color_temp=True))
    assert service == "turn_on" and data == {"color_temp_kelvin": 3000}
    assert any(n["field"] == "brightness_pct" and n["level"] == "info" for n in notes)


def test_capabilities_brightness_only_and_no_native_white() -> None:
    r = eff(cfg({"Dagur": {"strips": ps(action="on", brightness_mode="global", color_temp_mode="global")}}),
            "Dagur", targets=("strips",))
    _, data, _, notes = build_command(r, Capabilities(known=True, brightness=True))  # brightness-only strip
    assert data == {"brightness_pct": 62}
    assert [n["level"] for n in notes] == ["info"]
    _, data, _, _ = build_command(r, Capabilities(known=True, brightness=True, color=True))  # RGB only, no CT mode
    assert data == {"brightness_pct": 62}
    _, data, _, notes = build_command(r, Capabilities(known=True, brightness=True, color_temp=True,
                                                      min_kelvin=3500, max_kelvin=6500))
    assert data == {"brightness_pct": 62, "color_temp_kelvin": 3500}  # clamped to the light's range
    _, data, _, notes = build_command(r, Capabilities(known=True))  # on/off only
    assert data == {} and all(n["level"] == "info" for n in notes)


def test_validation_of_new_fields() -> None:
    assert validate_setting(ps(brightness_offset=150)) == {"brightness_offset": "invalid_brightness_offset"}
    assert validate_setting(ps(color_temp_offset=-6000)) == {"color_temp_offset": "invalid_kelvin_offset"}
    assert validate_setting(ps(action="on"), ALL_MOODS) == {"action": "all_moods_action"}
    assert validate_setting(ps(effect="rainbow"), ALL_MOODS) == {"effect": "all_moods_field"}
    assert validate_setting(ps(brightness_mode="global", color_temp_mode="offset", color_temp_offset=-300), ALL_MOODS) == {}
    assert validate_setting(ps(color_temp_mode="global", rgb_color=[1, 2, 3])) == {"rgb_color": "several_colors"}
    # Round trip and pre-0.3.2 entries.
    s = ps(action="on", brightness_mode="offset", brightness_offset=-20)
    assert ProfileSetting.from_dict(s.as_dict()) == s
    assert ps(action="on", brightness_pct=40).brightness_mode is None
    assert ps(brightness_mode="bogus").brightness_mode is None


def test_adaptive_config_bounds() -> None:
    c = AdaptiveConfig.from_dict({"brightness_entity": "sensor.b", "min_interval": -5, "brightness_step": 0,
                                  "kelvin_step": 99999, "transition": "x", "brightness_scale": "byte"})
    assert (c.min_interval, c.brightness_step, c.kelvin_step, c.transition) == (0, 1, 2000, 10.0)
    assert c.brightness_scale == SourceScale.BYTE and c.entities == ("sensor.b",)
    assert AdaptiveConfig.from_dict(None) == AdaptiveConfig()
    assert AdaptiveConfig.from_dict(c.as_dict()) == c


# ------------------------------------------------------------- gradual updates
CT_CAPS = Capabilities(known=True, brightness=True, color_temp=True, min_kelvin=2200, max_kelvin=6500, transition=True)


def target(state="on", caps=CT_CAPS, **attrs) -> Target:
    base = {"brightness": 158, "color_mode": "color_temp", "color_temp_kelvin": 3200}  # 62 %
    return Target("light.lamp", "Lamp", "lamps", "Lamps", state, {**base, **attrs} if state == "on" else attrs, caps,
                  targets=("lamps",))


def item_for(t: Target, values=VALUES, setting=None):
    config = cfg({"Dagur": {"lamps": setting or ps(action="on", brightness_mode="global", color_temp_mode="global")}})
    plan = build_plan(area_id="kno", mood="Dagur", targets=[t],
                      resolve_fn=lambda x: eff(config, "Dagur", values=values),
                      occupancy_state="confirmed_occupancy", occupied=True, trigger="adaptive", control_enabled=True)
    return plan.items[0]


def gv(b: float, k: float) -> GlobalValues:
    return GlobalValues(GlobalSource("sensor.b", b, "connected", ""), GlobalSource("sensor.k", k, "connected", ""))


def test_update_only_meaningful_changes_with_transition() -> None:
    config = AdaptiveConfig()
    t = target()
    assert adaptive_update(item_for(t), t, None, 0, config).decision == "unchanged"
    d = adaptive_update(item_for(t, gv(64, 3250)), t, None, 0, config)  # +2 %, +50 K: below the thresholds
    assert d.decision == "unchanged" and d.code == "below_threshold"
    d = adaptive_update(item_for(t, gv(70, 3250)), t, None, 0, config)
    assert d.decision == "send" and d.data == {"brightness_pct": 70, "transition": 10.0}
    d = adaptive_update(item_for(t, gv(70, 2900)), t, None, 0, AdaptiveConfig(transition=0))
    assert d.data == {"brightness_pct": 70, "color_temp_kelvin": 2900}


def test_update_throttled_newest_wins_and_force() -> None:
    config = AdaptiveConfig(min_interval=60)
    t = target()
    last = AdaptiveSent(100.0, {"brightness_pct": 62, "color_temp_kelvin": 3200})
    d = adaptive_update(item_for(t, gv(70, 3200)), t, last, 130.0, config)
    assert d.decision == "deferred" and d.due == 160.0
    d = adaptive_update(item_for(t, gv(80, 3200)), t, last, 161.0, config)
    assert d.decision == "send" and d.data["brightness_pct"] == 80
    assert adaptive_update(item_for(t, gv(80, 3200)), t, last, 130.0, config, force=True).decision == "send"
    # The reference is what was last sent (the light may still be in its transition).
    sent = AdaptiveSent(0.0, {"brightness_pct": 80})
    assert adaptive_update(item_for(t, gv(81, 3200)), t, sent, 500.0, config).decision == "unchanged"


def test_update_never_switches_a_light_on_and_respects_plan() -> None:
    config = AdaptiveConfig()
    off = target("off")
    d = adaptive_update(item_for(off, gv(80, 3000)), off, None, 0, config)
    assert d.decision == "skipped" and d.code == "light_off"
    blocked = target()
    blocked.override = {"source": "ui"}
    d = adaptive_update(item_for(blocked, gv(80, 3000)), blocked, None, 0, config)
    assert d.decision == "blocked" and d.code == "manual_override"
    t = target()
    d = adaptive_update(item_for(t, gv(80, 3000)), t, None, 0, config,
                        competing=[{"entity_id": "automation.adaptive", "name": "Adaptive"}])
    assert d.decision == "blocked" and d.code == "competing_controller"
    fixed = adaptive_update(item_for(t, gv(80, 3000), ps(action="on", brightness_pct=40)), t, None, 0, config)
    assert fixed.decision == "skipped" and fixed.code == "not_following"
    gone = GlobalValues(GlobalSource("sensor.b", None, "unavailable", ""), GlobalSource("sensor.k", None, "unavailable", ""))
    d = adaptive_update(item_for(t, gone), t, None, 0, config)
    assert d.decision == "blocked" and d.code == "source_unavailable"


def test_update_leaves_a_current_colour_alone() -> None:
    t = target(color_mode="hs", hs_color=[30, 80])
    d = adaptive_update(item_for(t, gv(80, 2700)), t, None, 0, AdaptiveConfig(transition=0))
    assert d.decision == "send" and d.data == {"brightness_pct": 80}
    t = target(effect="candle")
    assert "color_temp_kelvin" not in adaptive_update(item_for(t, gv(80, 2700)), t, None, 0, AdaptiveConfig()).data


def test_modes_enum_values() -> None:
    assert {m.value for m in AdjustMode} == {"fixed", "global", "offset", "ignore"}


def test_update_on_off_light_says_unsupported() -> None:
    caps = Capabilities(known=True)
    t = target(caps=caps)
    d = adaptive_update(item_for(t, gv(80, 3000)), t, None, 0, AdaptiveConfig())
    assert d.decision == "skipped" and d.code == "unsupported"
