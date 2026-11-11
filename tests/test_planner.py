"""Pure unit tests: command building per capability and the plan decisions."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.smart_occupancy.const import LightAction
from custom_components.smart_occupancy.lighting import Capabilities
from custom_components.smart_occupancy.planner import Target, build_command, build_plan, matches
from custom_components.smart_occupancy.profiles import ResolvedSetting

FULL = Capabilities(known=True, brightness=True, color_temp=True, color=True, min_kelvin=2200, max_kelvin=6500,
                    effects=("candle",), effect=True, transition=True)
WHITE = Capabilities(known=True, brightness=True, color_temp=True, min_kelvin=2700, max_kelvin=6500)
COLOUR_ONLY = Capabilities(known=True, brightness=True, color=True)
ONOFF = Capabilities(known=True)


def on(**kw: Any) -> ResolvedSetting:
    return ResolvedSetting(action=LightAction.ON, sources={"action": {"level": "global", "mood": "Hygge"}}, **kw)


def test_full_capabilities() -> None:
    service, data, requested, notes = build_command(
        on(brightness_pct=70, color_temp_kelvin=2700, effect="candle", transition=2), FULL)
    assert service == "turn_on" and notes == []
    assert data == {"brightness_pct": 70, "color_temp_kelvin": 2700, "effect": "candle", "transition": 2}
    assert requested["state"] == "on"


def test_unsupported_settings_are_omitted_and_reported() -> None:
    _, data, _, notes = build_command(on(brightness_pct=40, color_temp_kelvin=2700, transition=1), ONOFF)
    assert data == {}
    assert {n["field"] for n in notes} == {"brightness_pct", "color_temp_kelvin", "transition"}
    _, data, _, notes = build_command(on(rgb_color=(255, 0, 0), effect="rainbow"), WHITE)
    assert data == {} and {n["field"] for n in notes} == {"rgb_color", "effect"}
    # Kelvin outside the range is clamped; on colour-only lights Home Assistant approximates it.
    _, data, _, notes = build_command(on(color_temp_kelvin=2000), WHITE)
    assert data == {"color_temp_kelvin": 2700} and "outside" in notes[0]["message"]
    _, data, _, notes = build_command(on(color_temp_kelvin=2700), COLOUR_ONLY)
    assert data == {"color_temp_kelvin": 2700} and "approximates" in notes[0]["message"]
    service, data, _, _ = build_command(ResolvedSetting(action=LightAction.OFF, transition=3), ONOFF)
    assert (service, data) == ("turn_off", {})


def test_ignore_and_inherit_send_nothing() -> None:
    assert build_command(ResolvedSetting(action=LightAction.IGNORE), FULL)[0] is None


def _target(entity_id="light.l", state="off", caps=FULL, **kw) -> Target:
    return Target(entity_id=entity_id, name=entity_id, category="lamps", category_name="Lamps", state=state,
                  attributes=kw.pop("attributes", {}), capabilities=caps, **kw)


def _plan(targets, resolved, occupancy="confirmed_occupancy", occupied=True, mood="Hygge", control=True):
    by_id = resolved if isinstance(resolved, dict) else {t.entity_id: resolved for t in targets}
    return build_plan(area_id="r", mood=mood, targets=targets, resolve_fn=lambda t: by_id[t.entity_id],
                      occupancy_state=occupancy, occupied=occupied, trigger="mood_change", control_enabled=control)


@pytest.mark.parametrize(
    ("target_kw", "occupancy", "occupied", "decision", "code"),
    [
        ({}, "confirmed_occupancy", True, "change", "change"),
        ({"excluded": "exclusion label"}, "confirmed_occupancy", True, "skipped", "excluded"),
        ({"covered_by": "light.group"}, "confirmed_occupancy", True, "skipped", "covered_by_group"),
        ({"state": "unavailable"}, "confirmed_occupancy", True, "blocked", "offline"),
        ({"override": {"source": "ui", "until": None}}, "confirmed_occupancy", True, "blocked", "manual_override"),
        ({"conflicts": ({"automation": "automation.old", "name": "Old"},)}, "confirmed_occupancy", True,
         "blocked", "conflict"),
        ({}, "vacant", False, "blocked", "vacant"),
        ({}, "uncertain", None, "blocked", "occupancy_uncertain"),
        ({}, "possible_occupancy", True, "change", "change"),
    ],
)
def test_plan_decisions(target_kw, occupancy, occupied, decision, code) -> None:
    plan = _plan([_target(**target_kw)], on(brightness_pct=50), occupancy, occupied)
    item = plan.items[0]
    assert (item.decision, item.reason_code) == (decision, code), item.reason


def test_no_shutoff_on_unconfirmed_occupancy() -> None:
    off = ResolvedSetting(action=LightAction.OFF, sources={"action": {"level": "global", "mood": "Hygge"}})
    plan = _plan([_target(state="on")], off, "possible_vacancy", True)
    assert plan.items[0].reason_code == "off_needs_confirmation"
    plan = _plan([_target(state="on")], off, "confirmed_occupancy", True)
    assert plan.items[0].decision == "change" and plan.items[0].service == "turn_off"


def test_persistent_ambient_applies_while_vacant() -> None:
    plan = _plan([_target()], on(brightness_pct=20, persistent=True), "vacant", False)
    assert plan.items[0].decision == "change" and "Ambient" in plan.items[0].reason


def test_unchanged_when_already_in_state_or_already_sent() -> None:
    attrs = {"brightness": 128, "color_mode": "color_temp", "color_temp_kelvin": 2710}
    target = _target(state="on", attributes=attrs)
    assert matches(target, "turn_on", {"brightness_pct": 50, "color_temp_kelvin": 2700})
    assert not matches(target, "turn_on", {"brightness_pct": 80})
    plan = _plan([target], on(brightness_pct=50, color_temp_kelvin=2700))
    assert plan.items[0].reason_code == "already"
    sent = _target(caps=COLOUR_ONLY, state="on", attributes={"brightness": 128, "color_mode": "hs"},
                   last_command=("turn_on", {"brightness_pct": 50, "color_temp_kelvin": 2700}))
    plan = _plan([sent], on(brightness_pct=50, color_temp_kelvin=2700))
    assert plan.items[0].reason_code == "already_sent"


def test_observe_plans_but_says_would() -> None:
    plan = _plan([_target()], on(brightness_pct=50), control=False)
    assert plan.items[0].decision == "change" and plan.items[0].reason.startswith("Would")
    assert plan.as_dict()["will_execute"] is False


def test_no_mood_changes_nothing() -> None:
    plan = _plan([_target()], on(brightness_pct=50), mood=None)
    assert plan.items[0].reason_code == "no_mood" and not plan.changes


def test_reason_explains_sources() -> None:
    resolved = ResolvedSetting(action=LightAction.ON, brightness_pct=50, color_temp_kelvin=2700, sources={
        "action": {"level": "global", "mood": "Hygge"}, "brightness_pct": {"level": "room", "mood": "Hygge"},
        "color_temp_kelvin": {"level": "global", "mood": "Kvøld"}})
    reason = _plan([_target()], resolved).items[0].reason
    assert "50 %" in reason and "2700 K" in reason
    assert "brightness from room override" in reason and "global Kvøld (inherited)" in reason
