"""Phase 0.3 in Home Assistant's own engine: the rules the Lighting and Cover Designers generate, compiled into the
device's one automation, parsed and run by Home Assistant - and the generated templates rendered by Home Assistant
and compared with the Python twins the simulation uses."""

from __future__ import annotations

import itertools

import pytest

from homeassistant.core import Context
from homeassistant.helpers.template import Template

from ab import compiler, designers, model, simulate
from ab.designers import presence as P, solar

SOFA, KITCHEN, ROOM, DESK = "z_00000001", "z_00000002", "z_00000003", "z_00000004"
LAMP, SPOTS, PENDANT = "light.sofa_lamp", "light.kitchen_spots", "light.dining_pendant"
MOOD, LUX = "input_select.main_mood", "sensor.stova_lux"
LIVING, KITCHEN_BLIND = "cover.living_blind", "cover.kitchen_blind"


def sources(**overrides):
    base = {
        SOFA: {"name": "Sofa", "kind": "zone", "entity_id": "binary_sensor.sofa_zone", "entry_seconds": 5},
        KITCHEN: {"name": "Kitchen", "kind": "zone", "entity_id": "binary_sensor.kitchen_zone"},
        ROOM: {"name": "Stova", "kind": "room", "entity_id": "binary_sensor.stova_occupied", "exit_seconds": 60, "respect_fresh": True},
        DESK: {"name": "Desk", "kind": "position", "position": {
            "targets": [{"x": {"entity_id": "sensor.radar_x1"}, "y": {"entity_id": "sensor.radar_y1"}},
                        {"x": {"entity_id": "sensor.radar_x2"}, "y": {"entity_id": "sensor.radar_y2"}}],
            "units": "mm", "ignore_origin": True, "shape": {"type": "polygon", "points": [[0, 500], [1500, 500], [1500, 2000], [0, 2000]]}}},
    }
    base.update(overrides)
    return {"sources": base}


def lighting(**over):
    d = {"id": "g_00000001", "kind": "lighting", "version": 1, "name": "Lighting", "lights": [LAMP, SPOTS, PENDANT],
         "primary": "presence",
         "mood": {"enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge"], "apply_on_change": True,
                  "cells": {LAMP: {"Hygge": {"kind": "on", "brightness_pct": 40, "color_temp_kelvin": 2700},
                                   "Dagur": {"kind": "on", "brightness_pct": 80}}}},
         "presence": {"enabled": True, "policy": "priority",
                      "columns": [{"source": SOFA, "state": "occupied"}, {"source": KITCHEN, "state": "occupied"},
                                  {"source": ROOM, "state": "vacant"}],
                      "cells": {LAMP: {f"{SOFA}:occupied": {"kind": "mood"}, f"{ROOM}:vacant": {"kind": "off"}},
                                SPOTS: {f"{KITCHEN}:occupied": {"kind": "on", "brightness_pct": 100}, f"{ROOM}:vacant": {"kind": "off"}},
                                PENDANT: {f"{SOFA}:occupied": {"kind": "on", "brightness_pct": 20},
                                          f"{KITCHEN}:occupied": {"kind": "on", "brightness_pct": 50}, f"{ROOM}:vacant": {"kind": "off"}}}},
         "lux": {"enabled": True, "entity_id": LUX, "hysteresis": 20,
                 "bands": [{"id": "b_0001", "name": "Dark", "below": 100}, {"id": "b_0002", "name": "Normal", "below": 1000},
                           {"id": "b_0003", "name": "Bright"}],
                 "gate": {LAMP: {"on": ["b_0001"]}}}}
    d.update(over)
    return d


def build(designer, presence=None, targets=(LAMP, SPOTS, PENDANT), rules=()):
    config = model.validate_config({"settings": {}, "presence": presence, "devices": {"d_00000001": {
        "name": "Stova", "targets": list(targets), "rules": list(rules), "designers": [designer]}}})
    device = config["devices"]["d_00000001"]
    x = designers.expand(device, config.get("presence"))
    assert not [p for p in x.problems if p["level"] == "blocking"], x.problems
    return designers.effective(device, x), x, config


def on(entity, **data):
    return ("light.turn_on", (entity,), data)


def off(entity):
    return ("light.turn_off", (entity,), {})


async def offset(house):
    """Called right after a state change that starts a ``for`` timer: moves the clock off the 1 s grid.

    A ``for: N`` trigger and a "held for N s" condition meet at last_changed + N. In Home Assistant the timer is
    scheduled when the change is handled, a moment after last_changed, so the condition holds when it fires. In
    the test harness the clock is frozen, and ``async_fire_time_changed`` fires timers up to 0.5 s early: on a
    1 s grid aligned with the change, the timer would run at exactly (or before) the boundary. 0.3 s later, it
    runs 0.3 s after it, as in reality."""
    await house.settle()  # the change is handled (and its timer scheduled) at the time it happened
    await house.advance(0.3, step=0.3)


async def test_combined_mood_presence_lux_runs_as_one_automation(house, hass):
    effective, x, _ = build(lighting(), sources())
    for entity, value in ((LAMP, "off"), (SPOTS, "off"), (PENDANT, "off"), ("binary_sensor.sofa_zone", "off"),
                          ("binary_sensor.kitchen_zone", "off"), (MOOD, "Hygge"), (LUX, "45")):
        house.set(entity, value)
    house.set("binary_sensor.stova_occupied", "on", fresh=True)
    automations = await house.load(effective)
    assert len(automations) == 1 and len(hass.states.async_all("automation")) == 1
    house.set("binary_sensor.sofa_zone", "on")
    await offset(house)
    await house.advance(3)
    assert house.taken() == []                               # the sofa zone needs 5 s of confirmation
    await house.advance(3)
    assert sorted(house.taken()) == sorted([on(LAMP, brightness_pct=40, color_temp_kelvin=2700), on(PENDANT, brightness_pct=20)])
    # the mood changes while the sofa is occupied: the lamp follows (re-apply is ticked), the pendant does not use the mood
    house.set(MOOD, "Dagur")
    await house.settle()
    assert house.taken() == [on(LAMP, brightness_pct=80)]
    # the kitchen becomes occupied too: priority sofa > kitchen; the spots (no sofa cell) turn on, the pendant keeps 20 %
    house.set("binary_sensor.kitchen_zone", "on")
    await house.settle()
    assert house.taken() == [on(SPOTS, brightness_pct=100)]
    # the sofa is left: the kitchen applies to the pendant now; the zone becoming vacant does not make the room vacant
    house.set("binary_sensor.sofa_zone", "off")
    await house.settle()
    assert house.taken() == [on(PENDANT, brightness_pct=50)]
    # bright: the light-level permission (ON only when dark) keeps the lamp off when the sofa is used again
    house.set(LUX, "500")
    house.set(LAMP, "off")
    house.set("binary_sensor.sofa_zone", "on")
    await offset(house)
    await house.advance(6)
    assert house.taken() == [on(PENDANT, brightness_pct=20)]
    # the room becomes vacant (stale information never counts): no action while fresh is false
    house.set("binary_sensor.sofa_zone", "off")
    house.set("binary_sensor.kitchen_zone", "off")
    await house.settle()
    house.taken()
    house.set("binary_sensor.stova_occupied", "off", fresh=False)
    await offset(house)
    await house.advance(70)
    assert house.taken() == []
    house.set("binary_sensor.stova_occupied", "on", fresh=True)
    await house.settle()
    house.set("binary_sensor.stova_occupied", "off", fresh=True)
    await offset(house)
    await house.advance(70)
    assert sorted(house.taken()) == sorted([off(LAMP), off(SPOTS), off(PENDANT)])


async def test_simulation_predicts_what_home_assistant_does(house, hass):
    """Differential: for every combination of zones, mood and light level, the simulation and the real engine agree."""
    effective, x, _ = build(lighting(), sources())
    for entity, value in ((LAMP, "off"), (SPOTS, "off"), (PENDANT, "off"), ("binary_sensor.sofa_zone", "off"),
                          ("binary_sensor.kitchen_zone", "off"), (MOOD, "Hygge"), (LUX, "45")):
        house.set(entity, value)
    house.set("binary_sensor.stova_occupied", "on", fresh=True)
    await house.load(effective)
    house.apply = False
    keys = {t["text"]: t["key"] for t in simulate.triggers_of(effective, str)}
    kitchen_on = keys["binary_sensor.kitchen_zone changes to “on”"]
    for sofa, mood, lux in itertools.product(("on", "off"), ("Hygge", "Dagur"), ("45", "500", "unavailable")):
        house.set("binary_sensor.kitchen_zone", "off")
        house.set("binary_sensor.sofa_zone", sofa)
        house.set(MOOD, mood)
        house.set(LUX, lux)
        await offset(house)
        await house.advance(6)
        house.taken()
        states = {e: {"state": hass.states.get(e).state, "attributes": dict(hass.states.get(e).attributes)}
                  for e in ("binary_sensor.sofa_zone", "binary_sensor.stova_occupied", MOOD, LUX)}
        states["binary_sensor.kitchen_zone"] = {"state": "on"}
        predicted = simulate.run(effective, {"states": states, "trigger": kitchen_on}, x.refs.evaluators, str)
        house.set("binary_sensor.kitchen_zone", "on")
        await house.settle()
        real = sorted((a, e, tuple(sorted(d.items()))) for a, e, d in house.taken())
        expected = sorted((c["action"], tuple(c["entities"]), tuple(sorted(c["data"].items())))
                          for r in predicted["rules"] for c in r["calls"])
        assert real == expected, (sofa, mood, lux, real, expected)


async def test_unset_cells_and_explicit_defaults(house, hass):
    """Unset does nothing; an unset cell takes an explicitly configured default; No Action stops the default."""
    designer = lighting(primary="mood", presence={"enabled": False}, lux={"enabled": False}, mood={
        "enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge", "Nátt"],
        "cells": {LAMP: {"Hygge": {"kind": "on", "brightness_pct": 40}, "Nátt": {"kind": "none"}},
                  SPOTS: {"Dagur": {"kind": "default"}}},
        "row_defaults": {SPOTS: {"kind": "on", "brightness_pct": 30}},
        "default": {"kind": "off"}})
    effective, x, _ = build(designer)
    for entity, value in ((LAMP, "on"), (SPOTS, "on"), (PENDANT, "on"), (MOOD, "Hygge")):
        house.set(entity, value)
    await house.load(effective)
    house.set(MOOD, "Dagur")
    await house.settle()
    # lamp: unset -> matrix default OFF; spots: "use default" -> row default 30 %; pendant: unset -> matrix default OFF
    assert sorted(house.taken()) == sorted([off(LAMP), on(SPOTS, brightness_pct=30), off(PENDANT)])
    house.set(MOOD, "Nátt")
    await house.settle()
    # lamp: NO ACTION (explicit, the default does not apply); spots: row default; pendant: matrix default
    assert sorted(house.taken()) == sorted([on(SPOTS, brightness_pct=30), off(PENDANT)])
    house.set(MOOD, "Hygge")
    await house.settle()
    assert sorted(house.taken()) == sorted([on(LAMP, brightness_pct=40), on(SPOTS, brightness_pct=30), off(PENDANT)])
    house.set(MOOD, "Unknown mood")                          # not a column: nothing at all
    await house.settle()
    assert house.taken() == []


async def test_exclusive_policy_does_nothing_while_ambiguous(house):
    designer = lighting(presence={**lighting()["presence"], "policy": "exclusive"}, mood={"enabled": False}, lux={"enabled": False})
    designer["presence"]["cells"][LAMP] = {f"{ROOM}:vacant": {"kind": "off"}}
    effective, _x, _ = build(designer, sources())
    for entity, value in ((LAMP, "off"), (SPOTS, "off"), (PENDANT, "off"), ("binary_sensor.sofa_zone", "off"),
                          ("binary_sensor.kitchen_zone", "off")):
        house.set(entity, value)
    house.set("binary_sensor.stova_occupied", "on", fresh=True)
    await house.load(effective)
    house.set("binary_sensor.kitchen_zone", "on")
    await house.settle()
    assert sorted(house.taken()) == sorted([on(SPOTS, brightness_pct=100), on(PENDANT, brightness_pct=50)])
    house.set("binary_sensor.sofa_zone", "on")
    await offset(house)
    await house.advance(6)
    assert house.taken() == []                              # both occupied: the pendant is ambiguous, nothing happens
    house.set("binary_sensor.kitchen_zone", "off")
    await house.settle()
    assert house.taken() == [on(PENDANT, brightness_pct=20)]  # only the sofa now: its cell applies


async def test_position_zone_template_matches_python_and_drives_lights(house, hass):
    zone = P.validate_source(DESK, sources()["sources"][DESK], "s", model.Problems())
    x = designers.Expansion({"targets": [], "rules": []}, None)
    for which in ("inside", "outside", "not_inside"):
        text = P.position_template(zone, x.refs, which)
        for readings in ([("100", "800"), ("0", "0")], [("2000", "800"), ("0", "0")], [("0", "0"), ("unavailable", "5")],
                         [("1499", "1999"), ("3000", "3000")], [("-1", "800"), ("700", "400")], [("700", "1200"), ("700", "1300")]):
            for (xv, yv), n in zip(readings, (1, 2)):
                hass.states.async_set(f"sensor.radar_x{n}", xv)
                hass.states.async_set(f"sensor.radar_y{n}", yv)
            real = Template(text, hass).async_render(parse_result=False) == "True"
            states = {s.entity_id: {"state": s.state, "attributes": dict(s.attributes)} for s in hass.states.async_all()}
            assert real == P.evaluate_position(zone, states, which), (which, readings)
    # a lamp that follows the desk zone (presence decides; the zone is anonymous: no person is assumed)
    designer = {"id": "g_00000002", "kind": "lighting", "version": 1, "lights": ["light.desk"], "primary": "presence",
                "presence": {"enabled": True, "columns": [{"source": DESK, "state": "occupied"}, {"source": DESK, "state": "vacant"}],
                             "cells": {"light.desk": {f"{DESK}:occupied": {"kind": "on", "brightness_pct": 70}, f"{DESK}:vacant": {"kind": "off"}}}}}
    # x and y arrive as two state changes: a delay keeps a half-updated reading from counting (the editor says so)
    desk = {**sources()["sources"][DESK], "entry_seconds": 1, "exit_seconds": 1}
    effective, x, _ = build(designer, sources(**{DESK: desk}), targets=("light.desk",))
    plain_config = model.validate_config({"settings": {}, "presence": sources(), "devices": {"d_00000001": {
        "name": "Desk", "targets": ["light.desk"], "rules": [], "designers": [designer]}}})
    zero = designers.expand(plain_config["devices"]["d_00000001"], plain_config["presence"])
    assert any(p["code"] == "position_delay" for p in zero.problems)
    for n in (1, 2):
        house.set(f"sensor.radar_x{n}", "0")
        house.set(f"sensor.radar_y{n}", "0")
    house.set("light.desk", "off")
    await house.load(effective)
    house.set("sensor.radar_x1", "700")
    house.set("sensor.radar_y1", "900")
    await offset(house)
    await house.advance(2)
    assert house.taken() == [on("light.desk", brightness_pct=70)]
    house.set("sensor.radar_x1", "3000")                     # the target walks out of the shape
    await offset(house)
    await house.advance(2)
    assert house.taken() == [off("light.desk")]
    house.set("sensor.radar_x1", "unavailable")              # no readings: neither occupied nor vacant
    house.set("sensor.radar_y1", "unavailable")
    await offset(house)
    await house.advance(3)
    assert house.taken() == []


async def test_manual_change_pauses_the_designer(house, hass):
    designer = lighting(primary="mood", presence={"enabled": False}, lux={"enabled": False},
                        mood={"enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge"],
                              "cells": {LAMP: {"Hygge": {"kind": "on", "brightness_pct": 40}, "Dagur": {"kind": "on", "brightness_pct": 90}}}},
                        override={"manual_minutes": 10}, lights=[LAMP])
    effective, x, _ = build(designer, targets=(LAMP,))
    house.set(LAMP, "off")
    house.set(MOOD, "Dagur")
    await house.load(effective)
    template = next(t for t in x.refs.evaluators if "parent_id" in t)
    # a state set without a parent context counts as a change by hand (so does every state after a restart)
    assert Template(template, hass).async_render(parse_result=False) == "False"
    await house.advance(601, step=60)
    house.set(MOOD, "Hygge")
    await house.settle()
    assert house.taken() == [on(LAMP, brightness_pct=40)]   # changed by the automation (parent context): no pause
    assert Template(template, hass).async_render(parse_result=False) == "True"
    hass.states.async_set(LAMP, "on", {"brightness": 200}, context=Context(user_id="someone"))  # by hand
    assert Template(template, hass).async_render(parse_result=False) == "False"
    house.set(MOOD, "Dagur")
    await house.settle()
    assert house.taken() == []                               # paused for 10 minutes
    await house.advance(601, step=10)
    assert Template(template, hass).async_render(parse_result=False) == "True"
    house.set(MOOD, "Hygge")
    await house.settle()
    assert house.taken() == [on(LAMP, brightness_pct=40)]


@pytest.mark.parametrize(("basis", "points"), [("elevation", [[10, 10], [50, 80]]), ("profile", [[5, 0], [60, 100]])])
async def test_sun_curve_template_matches_python(hass, basis, points):
    for center in (0, 90, -45, 170):
        text = solar.response_template(basis, points, center)
        for elevation, azimuth in itertools.product((-2, 3.5, 12, 33.3, 61, 88), (95, 170, 180, 205.5, 265, 300)):
            if abs(solar.relative(azimuth, center)) >= 89:
                continue  # behind the window plane: outside every sector, the rule never runs there
            hass.states.async_set("sun.sun", "above_horizon", {"elevation": elevation, "azimuth": azimuth})
            real = Template(text, hass).async_render()
            assert real == solar.response(basis, points, elevation, azimuth, center), (center, elevation, azimuth)


def test_orientation_convention():
    assert solar.to_ha(0) == 180 and solar.to_ha(90) == 270 and solar.to_ha(-90) == 90 and solar.to_ha(180) == 0
    for user in (-179.5, -90, -1, 0, 1, 45, 90, 179, 180):
        assert solar.from_ha(solar.to_ha(user)) == pytest.approx(solar.normalize(user))
    assert solar.sector(0, 60, 60) == (120, 240)          # a south window: sun from ESE to WSW
    assert solar.sector(180, 30, 30) == (330, 30)         # a north window: the sector contains North (wraps)
    assert solar.in_sector(350, 180, 30, 30) and solar.in_sector(10, 180, 30, 30) and not solar.in_sector(40, 180, 30, 30)


def test_sun_path_agrees_with_home_assistant(hass):
    """The previews' sun path (NOAA approximation) is within half a degree of astral, which sun.sun uses."""
    from datetime import UTC, datetime
    import astral
    import astral.sun
    observer = astral.Observer(latitude=62.0, longitude=-6.8)
    for month, hour in itertools.product((1, 4, 6, 9, 12), (8, 11, 14, 17)):
        when = datetime(2026, month, 15, hour, 20, tzinfo=UTC)
        elevation, azimuth = solar.sun_position(when, 62.0, -6.8)
        assert elevation == pytest.approx(astral.sun.elevation(observer, when), abs=0.5)
        if elevation > 1:
            assert azimuth == pytest.approx(astral.sun.azimuth(observer, when), abs=0.5)


def blinds(**sun_over):
    sun = {"enabled": True, "hysteresis": 3, "min_change": 5,
           "windows": {LIVING: {"orientation": 0, "left": 60, "right": 60, "elevation_min": 5, "elevation_max": 60,
                                "position": {"kind": "curve", "basis": "elevation", "points": [[10, 20], [50, 80]]},
                                "leave": {"kind": "open"}},
                       KITCHEN_BLIND: {"orientation": 90, "left": 45, "right": 45, "elevation_min": 0,
                                       "position": {"kind": "fixed", "value": 30}}}}
    sun.update(sun_over)
    return {"id": "g_00000003", "kind": "cover", "version": 1, "covers": [LIVING, KITCHEN_BLIND], "sun": sun,
            "modes": {"enabled": True, "names": ["Daylight", "Privacy", "Sleep"], "driver": "mood", "mood_entity": MOOD,
                      "mood_map": {"Dagur": "Daylight", "Hygge": "Privacy", "Nátt": "Sleep"},
                      "cells": {LIVING: {"Daylight": {"kind": "position", "position": 100}, "Privacy": {"kind": "position", "position": 20},
                                         "Sleep": {"kind": "close"}},
                                KITCHEN_BLIND: {"Privacy": {"kind": "position", "position": 40}}},
                      "sun_policy": {"Daylight": "sun_wins", "Privacy": "mode_wins", "Sleep": "mode_wins"}}}


class Blinds:
    def __init__(self, house, hass):
        self.house, self.hass = house, hass
        hass.services.async_register("cover", "set_cover_position", self.position)
        hass.services.async_register("cover", "set_cover_tilt_position", house._record)  # noqa: SLF001

    async def position(self, call):
        self.house.calls.append(call)
        for entity_id in call.data["entity_id"]:
            current = self.hass.states.get(entity_id)
            self.hass.states.async_set(entity_id, "open" if call.data["position"] else "closed",
                                       {**current.attributes, "current_position": call.data["position"]}, context=call.context)

    def sun(self, elevation, azimuth):
        self.hass.states.async_set("sun.sun", "above_horizon" if elevation > 0 else "below_horizon",
                                   {"elevation": elevation, "azimuth": azimuth})


def pos(entity, value):
    return ("cover.set_cover_position", (entity,), {"position": value})


async def test_cover_sun_tracking_modes_and_policy(house, hass):
    effective, _x, _ = build(blinds(), targets=(LIVING, KITCHEN_BLIND))
    blind = Blinds(house, hass)
    blind.sun(-5, 80)
    house.set(MOOD, "Dagur")
    for cover in (LIVING, KITCHEN_BLIND):
        hass.states.async_set(cover, "open", {"current_position": 100, "supported_features": 15})
    await house.load(effective)
    blind.sun(8, 112)                       # morning, still outside the south window's sector (120..240)
    await house.settle()
    assert house.taken() == []
    blind.sun(15, 125)                      # inside: the curve gives 20 + (15-10)*1.5 = 27.5 -> 28
    await house.settle()
    assert house.taken() == [pos(LIVING, 28)]
    blind.sun(16, 126)                      # 29.5 -> 30 (banker's rounding like Home Assistant), change of 2 < 5: no move
    await house.settle()
    assert house.taken() == []
    blind.sun(25, 150)                      # 42.5 -> 42: a real change
    await house.settle()
    assert house.taken() == [pos(LIVING, 42)]
    house.set(MOOD, "Hygge")                # Privacy: the mode wins over the sun for both covers
    await house.settle()
    assert sorted(house.taken()) == sorted([pos(LIVING, 20), pos(KITCHEN_BLIND, 40)])
    blind.sun(35, 175)
    await house.settle()
    assert house.taken() == []              # sun tracking suspended in Privacy
    house.set(MOOD, "Dagur")                # Daylight: the sun wins, so the curve applies again instead of 100 %
    await house.settle()
    assert house.taken() == [pos(LIVING, 58)]
    blind.sun(30, 250)                      # beyond the living room's sector + hysteresis (243): Daylight returns there;
    await house.settle()                    # meanwhile the sun entered the west kitchen window (225..315): fixed 30 %
    assert sorted(house.taken()) == sorted([pos(LIVING, 100), pos(KITCHEN_BLIND, 30)])
    blind.sun(28, 262)                      # still inside the kitchen sector: a fixed response moves only on entering
    await house.settle()
    assert house.taken() == []
    hass.states.async_set("sun.sun", "unavailable", {})   # no sun data and no fallback configured: nothing moves
    await house.settle()
    assert house.taken() == []


async def test_weather_sensitive_awning_needs_and_uses_protection(house, hass):
    awning = {"id": "g_00000004", "kind": "cover", "version": 1, "covers": ["cover.awning"], "sun": {
        "enabled": True, "windows": {"cover.awning": {"orientation": 0, "left": 80, "right": 80, "elevation_min": 10,
                                                      "position": {"kind": "fixed", "value": 100}, "weather_sensitive": True}}}}
    config = model.validate_config({"settings": {}, "devices": {"d_00000001": {"name": "Awning", "targets": ["cover.awning"],
                                                                                "rules": [], "designers": [awning]}}})
    x = designers.expand(config["devices"]["d_00000001"], None)
    assert [p["code"] for p in x.problems if p["level"] == "blocking"] == ["weather_safety"] and not x.rules
    awning["sun"]["safety"] = {"wind_entity": "sensor.wind", "wind_above": 10, "rain_entity": "binary_sensor.rain"}
    effective, _x, _ = build(awning, targets=("cover.awning",))
    blind = Blinds(house, hass)
    blind.sun(5, 180)
    hass.states.async_set("cover.awning", "closed", {"current_position": 0, "supported_features": 15})
    house.set("sensor.wind", "3")
    house.set("binary_sensor.rain", "off")
    await house.load(effective)
    house.set("sensor.wind", "14")          # wind first: it must not extend
    blind.sun(20, 180)
    await house.settle()
    assert house.taken() == [("cover.close_cover", ("cover.awning",), {})]
    house.set("sensor.wind", "4")
    blind.sun(22, 185)
    await house.settle()
    assert house.taken() == []              # the sun had already entered; no new entry while calm (fixed response)
    blind.sun(5, 190)
    await house.settle()
    blind.sun(15, 190)                      # enters again while calm: extended
    await house.settle()
    assert house.taken() == [pos("cover.awning", 100)]
    house.set("binary_sensor.rain", "on")   # rain: retracted
    await house.settle()
    assert house.taken() == [("cover.close_cover", ("cover.awning",), {})]


async def test_designer_blueprint_runs_in_home_assistant(house, hass, tmp_path):
    """A designer exported as a blueprint, instantiated with other entities, runs without Automation Builder."""
    from homeassistant.setup import async_setup_component

    from ab import blueprint
    designer = lighting(primary="mood", presence={"enabled": False}, lux={"enabled": False},
                        mood={"enabled": True, "entity_id": MOOD, "values": ["Dagur", "Hygge"],
                              "cells": {LAMP: {"Hygge": {"kind": "on", "brightness_pct": 40}, "Dagur": {"kind": "off"}}}},
                        lights=[LAMP])
    _effective, _x, config = build(designer, targets=(LAMP,))
    device = config["devices"]["d_00000001"]
    found = blueprint.candidates(device, config["settings"])
    keys = {i["key"] for i in found["inputs"]}
    chosen = [k for k in keys if k.startswith(("controlled_", "input_select_")) or k.endswith("brightness_pct")]
    assert len(chosen) == 3, keys
    device = {**device, "blueprint": {"name": "Mood lamp", "description": "", "automation": None,
                                      "inputs": {k: {"name": "", "description": ""} for k in chosen}}}
    built = blueprint.build(device, config["settings"])
    assert built["problems"] == [] and "!input" in built["yaml"], built
    hass.config.config_dir = str(tmp_path)
    path = tmp_path / "blueprints" / "automation" / built["path"]
    path.parent.mkdir(parents=True)
    path.write_text(built["yaml"], encoding="utf-8")
    for domain, services in house.SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, house._record)  # noqa: SLF001
    house.set("light.other", "off")
    house.set("input_select.other_mood", "Dagur")
    inputs = {next(k for k in chosen if k.startswith("controlled_")): "light.other",
              next(k for k in chosen if k.startswith("input_select_")): "input_select.other_mood",
              next(k for k in chosen if k.endswith("brightness_pct")): 65}
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "from_designer", "alias": "From the designer", "use_blueprint": {"path": built["path"], "input": inputs}}]})
    await hass.async_block_till_done()
    house.set("input_select.other_mood", "Hygge")
    await house.settle()
    assert house.taken() == [("light.turn_on", ("light.other",), {"brightness_pct": 65})]
    house.set("input_select.other_mood", "Dagur")
    await house.settle()
    assert house.taken() == [("light.turn_off", ("light.other",), {})]
    _ = compiler  # the compiler is what produced it; nothing of the App runs here
