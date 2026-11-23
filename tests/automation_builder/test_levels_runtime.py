"""The Light Level Manager (0.5) in Home Assistant's own engine: a room's light level in a real ``input_text`` helper,
kept by the room's generated automation (hysteresis, stabilisation, no reading, ambient estimates), and lights that
follow it through Light Level matrices (global default matrix → room override → light override)."""

from __future__ import annotations

import random
from typing import Any

import pytest

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component

from ab import designers, model
from ab.designers import levels as LV

from .test_lighting2_runtime import LightHouse, lights  # noqa: F401

LL_DEV, LIGHT_DEV = "d_00000011", "d_00000022"
L1, L2 = "light.l1", "light.l2"
LUX = "sensor.kamar_lux"
HELPER = LV.helper_ids(LL_DEV, "kamar")["band"]
SAMPLE = LV.helper_ids(LL_DEV, "kamar")["ambient"]


def config(*, ambient: dict[str, Any] | None = None, light_cells: dict[str, Any] | None = None, bright_off: bool = False,
           template: dict[str, Any] | None = None) -> dict[str, Any]:
    room = LV.blank_room("kamar", LL_DEV, None)
    room.update(template="t_00000001", source={"entity_id": LUX, "attribute": None},
                overrides={"bands": {"dark": {"below": 95}}}, matrices={"truth": "x_00000001", "brightness": "x_00000002"})
    if ambient:
        room["ambient"].update(ambient)
    truth = {"very_dark": {"kind": "on"}, "dark": {"kind": "on"}, "dim": {"kind": "on"}, "bright": {"kind": "off"}}
    bright = {"very_dark": {"kind": "set", "pct": 20}, "dark": {"kind": "set", "pct": 40}, "dim": {"kind": "set", "pct": 75},
              "bright": {"kind": "set", "pct": 100}}
    if bright_off:
        truth["dim"] = {"kind": "off"}
    lighting = {"id": "g_00000022", "kind": "lighting", "version": 2, "name": "Lighting", "lights": [L1, L2],
                "truth": {"matrices": [{"id": "m_0001", "name": "Light level", "level": {"room": "kamar"}, "cells": light_cells or {}}]},
                "brightness": {"zero": "off", "matrices": [{"id": "m_0002", "name": "Light level", "level": {"room": "kamar"}, "cells": {}}]}}
    raw = {"settings": {},
           "light_levels": {"templates": {"t_00000001": template or LV.preset_template("t_00000001")},
                            "matrices": {"x_00000001": {"name": "T", "layer": "truth", "template": "t_00000001", "cells": truth},
                                         "x_00000002": {"name": "B", "layer": "brightness", "template": "t_00000001", "cells": bright}},
                            "rooms": {"kamar": room}},
           "devices": {LL_DEV: {"name": "Light Level · Kamar", "targets": LV.device_targets(room), "rules": [],
                                "designers": [{"id": "g_00000011", "kind": "light_level", "version": 1, "name": "Light Level", "room": "kamar"}]},
                       LIGHT_DEV: {"name": "Ceiling", "targets": [L1, L2], "rules": [], "designers": [lighting]}}}
    return model.validate_config(raw)


def effective(cfg: dict[str, Any], device_id: str) -> dict[str, Any]:
    device = cfg["devices"][device_id]
    x = designers.expand(device, None, shared=cfg)
    blocking = [p for p in x.problems if p["level"] == "blocking"]
    assert not blocking, blocking
    return designers.effective(device, x)


async def start(hass, house: LightHouse, cfg: dict[str, Any], *, lux: float = 30, helper: str | None = None) -> None:
    for light in (L1, L2):
        hass.states.async_set(light, "off", {"supported_color_modes": ["brightness"]})
    hass.states.async_set(LUX, str(lux), {"unit_of_measurement": "lx", "device_class": "illuminance"})
    texts = {HELPER.split(".")[1]: {"name": "level", "max": 32, **({"initial": helper} if helper else {})}}
    assert await async_setup_component(hass, "input_text", {"input_text": texts})
    if cfg["light_levels"]["rooms"]["kamar"]["ambient"]["method"] == "off_sample":
        assert await async_setup_component(hass, "input_number", {"input_number": {SAMPLE.split(".")[1]: {
            "name": "sample", "min": -1, "max": 200000, "step": 1, "mode": "box", "initial": -1}}})   # as the App creates it
    await house.load(effective(cfg, LL_DEV), effective(cfg, LIGHT_DEV))
    await house.settle()


def level(hass) -> str:
    return hass.states.get(HELPER).state


def lux(hass, value: Any) -> None:
    hass.states.async_set(LUX, str(value), {"unit_of_measurement": "lx", "device_class": "illuminance"})


# ------------------------------------------------------------------ the band, hysteresis and stabilisation
async def test_the_level_starts_at_home_assistant_start_and_the_lights_follow_the_inherited_matrices(lights, hass):
    cfg = config(light_cells={L1: {"dim": {"kind": "off"}}})      # a light's own value overrides the room's
    await start(hass, lights, cfg, lux=30)
    assert level(hass) == "unknown"
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await lights.settle()
    assert level(hass) == "dark"                                  # 20 ≤ 30 < 95 (the room's own limit, not 80)
    assert lights.light(L1)[:2] == ("on", 102) and lights.light(L2)[:2] == ("on", 102)   # 40 % (global default)
    lights.taken()
    lux(hass, 150)                                                # brighter: dim, after the 180 s stabilisation
    await lights.advance(170, step=10)
    assert level(hass) == "dark" and lights.light_calls() == []
    await lights.advance(20, step=5)
    assert level(hass) == "dim"
    assert lights.light(L1)[0] == "off" and lights.light(L2)[:2] == ("on", 191)        # L1: its own OFF; L2: 75 %


async def test_hysteresis_and_a_flapping_reading_change_nothing(lights, hass):
    await start(hass, lights, config(), lux=150, helper="dim")
    lights.taken()
    for value in (205, 190, 209, 201, 196, 208):                 # around the 200 lx limit, within the 10 lx hysteresis
        lux(hass, value)
        await lights.advance(100, step=10)
    assert level(hass) == "dim"
    for value in (85, 100, 86, 104, 90):                          # around the room's 95 lx limit (dim → dark needs < 85)
        lux(hass, value)
        await lights.advance(50, step=10)
    assert level(hass) == "dim" and lights.light_calls() == []
    lux(hass, 84)                                                 # past the limit minus the hysteresis, held 60 s
    await lights.advance(55, step=5)
    assert level(hass) == "dim"
    await lights.advance(10, step=5)
    assert level(hass) == "dark"


async def test_a_short_dip_or_peak_does_not_change_the_level(lights, hass):
    await start(hass, lights, config(), lux=150, helper="dim")
    for value in (500, 150, 600, 150):                            # a lamp switched on for a moment, a reflection
        lux(hass, value)
        await lights.advance(120, step=10)
    assert level(hass) == "dim"


async def test_without_a_reading_the_level_becomes_no_reading_and_the_lights_are_left_alone(lights, hass):
    await start(hass, lights, config(), lux=30)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await lights.settle()
    assert level(hass) == "dark" and lights.light(L1)[0] == "on"
    lights.taken()
    hass.states.async_set(LUX, "unavailable")
    await lights.advance(590, step=10)
    assert level(hass) == "dark"
    await lights.advance(20, step=5)
    assert level(hass) == LV.NO_READING
    assert lights.light_calls() == [] and lights.light(L1)[0] == "on"    # nothing invented, nothing switched
    lux(hass, 250)                                                # a reading again: the level starts over (no hysteresis)
    await lights.settle()
    assert level(hass) == "bright"


# ------------------------------------------------------------------ lamps lighting their own sensor
class Room:
    """A room whose sensor sees daylight plus the lamps (L1 adds 150 lx at full brightness)."""

    def __init__(self, hass, daylight: float) -> None:
        self.hass, self.daylight = hass, daylight

    def report(self) -> None:
        state = self.hass.states.get(L1)
        lamp = 150 * (state.attributes.get("brightness") or 255) / 255 if state and state.state == "on" else 0
        lux(self.hass, round(self.daylight + lamp))


async def _run(lights_, hass, room: Room, minutes: int) -> list[str]:
    seen = []
    for _ in range(minutes * 2):
        room.report()
        await lights_.advance(30, step=10)
        seen.append(lights_.light(L1)[0])
    return seen


async def test_a_raw_reading_that_the_lamp_lifts_oscillates(lights, hass):
    raw = config(bright_off=True)          # dim and bright = OFF: the lamp (150 lx) lifts dark (80 lx) out of dark
    await start(hass, lights, raw, lux=80)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await lights.settle()
    room = Room(hass, 80)
    seen = await _run(lights, hass, room, 30)
    assert seen.count("on") and seen.count("off") and len([1 for a, b in zip(seen, seen[1:]) if a != b]) >= 4   # back and forth


async def test_calibrated_estimate_keeps_the_level_steady(lights, hass):
    steady = config(bright_off=True, ambient={"method": "calibrated", "lights": [L1], "contributions": {L1: 150}})
    await start(hass, lights, steady, lux=80)
    room = Room(hass, 80)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await lights.settle()
    seen = await _run(lights, hass, room, 30)
    assert set(seen[2:]) == {"on"} and level(hass) == "dark"      # the lamp's own light is taken out


async def test_off_sample_uses_the_reading_while_the_lights_are_off_and_expires(lights, hass):
    cfg = config(ambient={"method": "off_sample", "lights": [L1, L2], "settle_seconds": 30, "max_age_minutes": 20})
    await start(hass, lights, cfg, lux=150)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await lights.settle()
    assert level(hass) == "unknown"                              # no sample yet: no level (never a 0 lx reading)
    await lights.advance(300, step=10)                            # the regular check samples (all lights off)
    assert float(hass.states.get(SAMPLE).state) == 150 and level(hass) == "dim"
    hass.states.async_set(L1, "on", {"brightness": 255})           # a lamp on: its light never reaches the sample
    lux(hass, 400)
    await lights.advance(600, step=10)
    assert float(hass.states.get(SAMPLE).state) == 150 and level(hass) == "dim"
    await lights.advance(600, step=30)                            # 20 min old (no value) + 10 min without one
    assert level(hass) == "dim"
    await lights.advance(700, step=30)
    assert level(hass) == LV.NO_READING


# ------------------------------------------------------------------ the generated template against its Python twin
async def test_band_template_matches_the_python_twin(hass):
    cfg = config()
    room = cfg["light_levels"]["rooms"]["kamar"]
    eff = LV.effective(room, cfg["light_levels"]["templates"]["t_00000001"])
    prefix = LV.band_jinja(room, eff)
    ids = [b["id"] for b in eff["bands"]]
    limits = [b["below"] for b in eff["bands"][:-1]]
    rng = random.Random(5)
    values = [0, 19.9, 20, 29.9, 30, 85, 94.9, 95, 104.9, 105, 190, 199, 200, 209.9, 210, 5000, "unavailable", "abc"]
    values += [round(rng.uniform(0, 400), 1) for _ in range(40)]
    for value in values:
        for current in ["unknown", "no_reading", *ids]:
            hass.states.async_set(LUX, str(value))
            hass.states.async_set(HELPER, current)
            rendered = Template(prefix + "{{ new }}", hass).async_render(parse_result=False)
            twin = LV.band_index(LV._number(value), limits, eff["settings"]["hysteresis"]["value"], ids.index(current) if current in ids else -1)  # noqa: SLF001
            assert int(rendered) == twin, (value, current)


@pytest.mark.parametrize("brightness", [255, 128, None])
async def test_calibrated_value_template_matches_the_python_twin(hass, brightness):
    cfg = config(ambient={"method": "calibrated", "lights": [L1, L2], "contributions": {L1: 120, L2: 40.5}})
    room = cfg["light_levels"]["rooms"]["kamar"]
    for raw, s1, s2 in [(300, "on", "off"), (100, "on", "on"), (20, "on", "on"), (0, "off", "off"), ("unknown", "on", "on")]:
        hass.states.async_set(LUX, str(raw))
        hass.states.async_set(L1, s1, {"brightness": brightness} if brightness else {})
        hass.states.async_set(L2, s2, {"brightness": 255})
        rendered = Template(LV.value_jinja(room) + "{{ v }}", hass).async_render(parse_result=False)
        states = {e: {"state": s.state, "attributes": dict(s.attributes)} for e in (LUX, L1, L2) if (s := hass.states.get(e))}
        twin = LV.reading(room, states)["value"]
        assert (rendered == "None" and twin is None) or abs(float(rendered) - twin) < 1e-6, (raw, s1, s2, rendered, twin)
