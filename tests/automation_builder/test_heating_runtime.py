"""The Heating Designer (0.5) in Home Assistant's own engine: schedules (weekdays and weekend), presence setback,
windows (frost, OFF, unavailable), away/home with return, manual override by context, resume, the priority of the
layers, no repeated or out-of-range setpoints, the Python twin against the engine, and the exported blueprint.

No other App is involved: the thermostat is a climate entity that follows its commands, the helpers are Home
Assistant's own input_boolean and timer.
"""

from __future__ import annotations

from datetime import datetime
import itertools
from typing import Any

import pytest

from homeassistant.core import Context, ServiceCall
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from ab import blueprint, designers, model, simulate
from ab.designers import heating as H

from .conftest import House

DEV = "d_00000033"
T = "climate.kamar"
OCC, WIN, FAMILY, RESUME = "binary_sensor.kamar_occ", "binary_sensor.kamar_window", "group.family", "input_button.resume"
FLAG, TIMER = H.helper_ids(DEV, T)["flag"], H.helper_ids(DEV, T)["timer"]
SRC = "z_00000001"
ATTRS = {"min_temp": 7, "max_temp": 30, "target_temp_step": 0.5, "hvac_modes": ["off", "heat"], "supported_features": 401,
         "preset_modes": ["eco", "comfort"], "preset_mode": None, "current_temperature": 19.5, "temperature": 18}


class ClimateHouse(House):
    """A thermostat that follows its commands (setpoint, HVAC mode, preset), with the command's context."""

    SERVICES = {**House.SERVICES, "climate": ("set_temperature", "set_hvac_mode", "set_preset_mode")}

    async def _record(self, call: ServiceCall) -> None:
        if call.domain != "climate":
            await super()._record(call)
            return
        self.calls.append(call)
        ids = call.data.get("entity_id") or []
        for entity_id in [ids] if isinstance(ids, str) else ids:
            current = self.hass.states.get(entity_id)
            attrs, state = dict(current.attributes), current.state
            if call.service == "set_temperature":
                for key in ("temperature", "target_temp_low", "target_temp_high"):
                    if key in call.data:
                        attrs[key] = call.data[key]
                state = call.data.get("hvac_mode", state)
            elif call.service == "set_hvac_mode":
                state = call.data["hvac_mode"]
            elif call.service == "set_preset_mode":
                attrs["preset_mode"] = call.data["preset_mode"]
            self.hass.states.async_set(entity_id, state, attrs, context=call.context)

    def climate_calls(self) -> list[tuple[str, tuple[str, ...], dict[str, Any]]]:
        return [c for c in self.taken() if c[0].startswith("climate.")]

    def target(self) -> Any:
        return self.hass.states.get(T).attributes.get("temperature")


@pytest.fixture
async def home(hass, freezer):
    await hass.config.async_set_time_zone("UTC")
    house = ClimateHouse(hass, freezer)
    yield house
    entities = [s.entity_id for s in hass.states.async_all("automation")]
    if entities:
        await hass.services.async_call("automation", "turn_off", {"entity_id": entities}, blocking=True)
        await hass.async_block_till_done()


def designer(**extra: Any) -> dict[str, Any]:
    d = {"id": "g_00000033", "kind": "heating", "version": 1, "name": "Heating", "thermostats": [T], "profile": {"room": "kamar"},
         "cells": {T: {"night": {"kind": "set", "temp": 16.5}}},
         "modes": {"source": "schedule", "schedule": {"split": "weekdays",
                   "weekdays": [{"at": "06:00", "mode": "comfort"}, {"at": "09:00", "mode": "setback"},
                                {"at": "17:00", "mode": "comfort"}, {"at": "23:00", "mode": "night"}],
                   "weekend": [{"at": "08:00", "mode": "comfort"}, {"at": "23:30", "temp": 17.5}]}}}
    d.update(extra)
    return d


def configure(d: dict[str, Any], **template: Any) -> dict[str, Any]:
    tpl = {**H.preset_template("h_00000001"), **template}
    return model.validate_config({
        "settings": {}, "presence": {"sources": {SRC: {"name": "Kamar", "kind": "room", "entity_id": OCC}}},
        "heating": {"templates": {"h_00000001": tpl}, "rooms": {"kamar": {"template": "h_00000001",
                                                                          "overrides": {"comfort": {"kind": "set", "temp": 22}}}}},
        "devices": {DEV: {"name": "Kamar radiator", "targets": [T], "rules": [], "designers": [d]}}})


def expanded(cfg: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    device = cfg["devices"][DEV]
    x = designers.expand(device, cfg.get("presence"), shared=cfg)
    blocking = [p for p in x.problems if p["level"] == "blocking"]
    assert not blocking, blocking
    return designers.effective(device, x), x


async def start(hass, home: ClimateHouse, d: dict[str, Any], at: str, *, temp: float = 18, state: str = "heat",
                attrs: dict[str, Any] | None = None, **template: Any) -> Any:
    home.freezer.move_to(datetime.fromisoformat(at).replace(tzinfo=dt_util.UTC))
    hass.states.async_set(T, state, {**ATTRS, "temperature": temp, **(attrs or {})})
    for entity, value in ((OCC, "on"), (WIN, "off"), (FAMILY, "home"), (RESUME, "2026-11-01T00:00:00+00:00")):
        hass.states.async_set(entity, value)
    if d.get("override", {}).get("manual"):
        assert await async_setup_component(hass, "input_boolean", {"input_boolean": {FLAG.split(".")[1]: {"name": "o"}}})
        assert await async_setup_component(hass, "timer", {"timer": {TIMER.split(".")[1]: {"restore": True}}})
    effective, x = expanded(configure(d, **template))
    await home.load(effective)
    await home.settle()
    home.taken()
    return x


async def hold(home: ClimateHouse, hass, entity: str, state: str) -> None:
    """A state change, then a fraction of a second: the harness fires ``for:`` timers up to 0.5 s early, so a trigger
    "held for N" and the condition "held for N" would otherwise meet exactly (real Home Assistant never fires early)."""
    hass.states.async_set(entity, state)
    await home.settle()
    await home.advance(0.3, step=0.3)


def poke(hass) -> None:
    """Ask the thermostat's automation to evaluate now (what any input change does)."""
    hass.bus.async_fire(H.EVENT, {"climate": T})


# ------------------------------------------------------------------ schedules
async def test_weekday_schedule_with_room_and_thermostat_values(home, hass):
    await start(hass, home, designer(), "2026-11-23T05:59:00")         # Monday
    await home.advance(65, step=5)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]   # comfort: the room's 22
    for at, expected in (("09:00", 18), ("17:00", 22), ("23:00", 16.5)):  # setback (global), comfort, night (thermostat)
        home.freezer.move_to(datetime.fromisoformat(f"2026-11-23T{at}:00").replace(tzinfo=dt_util.UTC))
        await home.advance(2)
        assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": expected})], at


async def test_weekend_differs_and_the_night_carries_over_midnight(home, hass):
    await start(hass, home, designer(), "2026-11-28T07:00:00", temp=22)  # Saturday 07:00: Friday's 23:00 night applies
    poke(hass)
    await home.settle()
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 16.5})]
    home.freezer.move_to(datetime.fromisoformat("2026-11-28T08:00:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]
    home.freezer.move_to(datetime.fromisoformat("2026-11-28T23:30:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 17.5})]   # a temperature entry
    home.freezer.move_to(datetime.fromisoformat("2026-11-28T09:00:00").replace(tzinfo=dt_util.UTC))


async def test_day_night_and_no_repeated_or_out_of_range_setpoints(home, hass):
    d = designer(modes={"source": "schedule", "schedule": {"split": "all", "all": [{"at": "06:30", "temp": 35}, {"at": "22:30", "temp": 20.3}]}},
                 reconcile_minutes=5)
    await start(hass, home, d, "2026-11-23T12:00:00")
    poke(hass)
    await home.settle()
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 30})]   # clamped to max_temp
    await home.advance(600, step=30)                                    # the reconciliation runs: nothing to change
    poke(hass)
    await home.settle()
    assert home.climate_calls() == []
    home.freezer.move_to(datetime.fromisoformat("2026-11-23T22:30:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 20.5})]  # rounded to the 0.5 step


# ------------------------------------------------------------------ presence, windows, away
async def test_presence_setback_and_return_to_the_current_schedule(home, hass):
    d = designer(presence={"enabled": True, "source": SRC, "vacant_minutes": 30, "mode": "setback"})
    await start(hass, home, d, "2026-11-23T16:00:00", temp=18)
    hass.states.async_set(OCC, "off")
    await home.advance(1790, step=30)
    assert home.climate_calls() == []                                   # 18 is setback already (and not 30 min yet)
    home.freezer.move_to(datetime.fromisoformat("2026-11-23T17:00:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    assert home.climate_calls() == []                                   # 17:00 comfort, but the room is vacant: setback
    hass.states.async_set(OCC, "on")
    await home.settle()
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]   # the schedule as it is NOW


async def test_window_open_frost_then_back_to_the_schedule_of_now(home, hass):
    d = designer(window={"enabled": True, "sensors": [WIN], "open_seconds": 60, "close_seconds": 30, "action": "frost", "unavailable": "ignore"})
    await start(hass, home, d, "2026-11-23T16:58:00", temp=18)
    await hold(home, hass, WIN, "on")
    await home.advance(65, step=5)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 7})]    # frost protection
    await home.advance(60)                                              # 17:00 passes while the window is open
    assert home.climate_calls() == []
    await hold(home, hass, WIN, "off")
    await home.advance(35, step=5)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]   # comfort (17:00), not 18


@pytest.mark.parametrize("policy,expected", [("ignore", []), ("open", [("climate.set_temperature", (T,), {"temperature": 7})]),
                                             ("keep", [])])
async def test_window_sensor_unavailable_follows_the_configured_fallback(home, hass, policy, expected):
    d = designer(window={"enabled": True, "sensors": [WIN], "open_seconds": 60, "close_seconds": 0, "action": "frost", "unavailable": policy})
    await start(hass, home, d, "2026-11-23T12:00:00", temp=18)
    hass.states.async_set(WIN, "unavailable")
    await home.advance(65, step=5)
    poke(hass)
    await home.settle()
    assert home.climate_calls() == expected


async def test_window_off_and_back_on_with_the_hvac_mode(home, hass):
    d = designer(window={"enabled": True, "sensors": [WIN], "open_seconds": 10, "close_seconds": 10, "action": "off", "unavailable": "ignore"},
                 when_off="heat", hvac_mode="heat")
    await start(hass, home, d, "2026-11-23T12:00:00", temp=18)
    await hold(home, hass, WIN, "on")
    await home.advance(12, step=2)
    assert home.climate_calls() == [("climate.set_hvac_mode", (T,), {"hvac_mode": "off"})]
    await hold(home, hass, WIN, "off")
    await home.advance(12, step=2)
    # the thermostat is off and "switch it on with the setpoint" is chosen: one command does both
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 18, "hvac_mode": "heat"})]
    assert hass.states.get(T).state == "heat"
    poke(hass)
    await home.settle()
    assert home.climate_calls() == []


async def test_away_then_return_home_then_schedule(home, hass):
    d = designer(away={"enabled": True, "entity_id": FAMILY, "home_state": "home", "away_minutes": 30, "mode": "away",
                       "return_mode": "comfort", "return_minutes": 60},
                 presence={"enabled": True, "source": SRC, "vacant_minutes": 30, "mode": "setback"})
    await start(hass, home, d, "2026-11-23T10:00:00", temp=18)
    hass.states.async_set(OCC, "off")
    await hold(home, hass, FAMILY, "not_home")
    await home.advance(1830, step=30)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 16})]   # away outranks presence
    await hold(home, hass, FAMILY, "home")
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]   # return: comfort for 60 min
    await home.advance(3610, step=60)
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 18})]   # then setback (10:00–17:00)


# ------------------------------------------------------------------ manual override
async def test_manual_change_suspends_until_the_next_scheduled_change(home, hass):
    d = designer(override={"manual": True, "policy": "until_next"})
    await start(hass, home, d, "2026-11-23T12:00:00", temp=18)
    hass.states.async_set(T, "heat", {**ATTRS, "temperature": 23.5})    # by hand (no automation context)
    await home.settle()
    assert hass.states.get(FLAG).state == "on" and home.climate_calls() == []      # not undone
    poke(hass)
    await home.settle()
    assert home.climate_calls() == [] and home.target() == 23.5
    home.freezer.move_to(datetime.fromisoformat("2026-11-23T17:00:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    assert hass.states.get(FLAG).state == "off"
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 22})]


async def test_an_automation_s_change_and_a_reported_echo_are_not_manual(home, hass):
    d = designer(override={"manual": True, "policy": "until_resumed", "resume": [{"type": "state", "entity_id": RESUME}]})
    await start(hass, home, d, "2026-11-23T12:00:00", temp=20)
    hass.states.async_set(T, "heat", {**ATTRS, "temperature": 21}, context=Context(parent_id="01OTHER"))   # another automation
    await home.settle()
    assert hass.states.get(FLAG).state == "off"
    hass.states.async_set(T, "heat", {**ATTRS, "temperature": 18})      # by hand, but exactly what applies now: no override
    await home.settle()
    assert hass.states.get(FLAG).state == "off"
    hass.states.async_set(T, "heat", {**ATTRS, "temperature": 24})      # by hand
    await home.settle()
    assert hass.states.get(FLAG).state == "on"
    home.taken()
    hass.states.async_set(RESUME, "2026-11-23T12:05:00+00:00")          # resume automatic
    await home.settle()
    assert hass.states.get(FLAG).state == "off"
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 18})]


async def test_override_for_a_duration(home, hass):
    d = designer(override={"manual": True, "policy": "duration", "minutes": 2})
    await start(hass, home, d, "2026-11-23T12:00:00", temp=18)
    hass.states.async_set(T, "heat", {**ATTRS, "temperature": 25})
    await home.settle()
    assert hass.states.get(FLAG).state == "on" and hass.states.get(TIMER).state == "active"
    await home.advance(125, step=5)
    assert hass.states.get(FLAG).state == "off"
    assert home.climate_calls() == [("climate.set_temperature", (T,), {"temperature": 18})]


# ------------------------------------------------------------------ the Python twin against the engine
@pytest.mark.parametrize("window,vacant,away,clock", list(itertools.product([False, True], [False, True], [False, True],
                                                                            ["07:00", "12:00", "23:45"])))
async def test_preview_predicts_what_home_assistant_does(home, hass, window, vacant, away, clock):
    d = designer(window={"enabled": True, "sensors": [WIN], "open_seconds": 0, "close_seconds": 0, "action": "frost", "unavailable": "ignore"},
                 presence={"enabled": True, "source": SRC, "vacant_minutes": 0, "mode": "setback"},
                 away={"enabled": True, "entity_id": FAMILY, "home_state": "home", "away_minutes": 0, "mode": "away"})
    x = await start(hass, home, d, f"2026-11-24T{clock}:00", temp=19)  # Tuesday
    hass.states.async_set(WIN, "on" if window else "off")
    hass.states.async_set(OCC, "off" if vacant else "on")
    hass.states.async_set(FAMILY, "not_home" if away else "home")
    await home.settle()
    home.taken()
    states = {e: {"state": s.state, "attributes": dict(s.attributes), "held": 3600} for e in (T, WIN, OCC, FAMILY)
              if (s := hass.states.get(e))}
    gen = x.generators["g_00000033"]
    ev = simulate.Evaluator(states, {"time": f"{clock}:00", "weekday": "tue"}, x.refs.evaluators, lambda e: e)
    predicted = H.evaluate(gen, T, ev)["command"]
    poke(hass)
    await home.settle()
    sent = home.climate_calls()
    if predicted["service"] is None:
        assert sent == []
    else:
        assert sent == [(f"climate.{predicted['service']}", (T,), predicted["data"])]


# ------------------------------------------------------------------ blueprint
async def test_heating_blueprint_runs_with_other_inputs(home, hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    cfg = configure(designer(presence={"enabled": True, "source": SRC, "vacant_minutes": 30, "mode": "setback"}))
    device = cfg["devices"][DEV]
    found = blueprint.candidates(device, cfg["settings"], None, presence=cfg.get("presence"), shared=cfg)
    keys = [i["key"] for i in found["inputs"]]
    assert {"controlled_kamar", "binary_sensor_kamar_occ", "kamar_comfort", "kamar_setback", "at_0600", "vacant_for"} <= set(keys)
    device = {**device, "blueprint": {"name": "Heating", "description": "", "automation": None,
                                      "inputs": {k: {"name": "", "description": ""} for k in keys}}}
    built = blueprint.build(device, cfg["settings"], presence=cfg.get("presence"), shared=cfg)
    assert built["problems"] == [], built["problems"]
    path = tmp_path / "blueprints" / "automation" / built["path"]
    path.parent.mkdir(parents=True)
    path.write_text(built["yaml"], encoding="utf-8")
    home.freezer.move_to(datetime.fromisoformat("2026-11-23T12:00:00").replace(tzinfo=dt_util.UTC))
    other = "climate.stova"
    for entity in (T, other):
        hass.states.async_set(entity, "heat", {**ATTRS, "temperature": 15})
    hass.states.async_set(OCC, "on")
    for domain, services in home.SERVICES.items():
        for service in services:
            hass.services.async_register(domain, service, home._record)  # noqa: SLF001
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "from_blueprint_0", "alias": "As designed", "use_blueprint": {"path": built["path"], "input": {}}},
        {"id": "from_blueprint_1", "alias": "Another room", "use_blueprint": {"path": built["path"], "input": {
            "controlled_kamar": other, "kamar_setback": 16, "kamar_comfort": 20.5}}}]})
    await hass.async_block_till_done()
    hass.bus.async_fire(H.EVENT, {"climate": T})
    hass.bus.async_fire(H.EVENT, {"climate": T})
    await home.settle()
    sent = sorted(home.climate_calls(), key=str)
    assert ("climate.set_temperature", (T,), {"temperature": 18}) in sent           # setback 12:00 (global)
    home.freezer.move_to(datetime.fromisoformat("2026-11-23T17:00:00").replace(tzinfo=dt_util.UTC))
    await home.advance(2)
    sent = sorted(home.climate_calls(), key=str)
    assert sent == sorted([("climate.set_temperature", (T,), {"temperature": 22}), ("climate.set_temperature", (other,), {"temperature": 20.5})], key=str)
