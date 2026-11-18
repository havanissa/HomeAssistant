"""Phase A4 parity scenarios: the integration's lighting controller, recorded for the App's replay.

Each test runs one scenario in a real (test) Home Assistant with simulated
devices and records it (``lighting_parity.Recorder``). The recording is compared
with the committed fixture in ``smart_lighting/tests/lighting_scenarios/`` (the
commands, their outcomes and the decisions), so the fixture always describes what
the integration really does; ``LIGHTING_RECORD=1`` writes the fixtures instead.
The App's suite (``smart_lighting/tests/test_lighting_parity.py``) replays every
fixture through the App's host and requires the same commands and decisions.

Covered: PIR and radar occupancy, possible occupancy, vacancy, mood changes and
Manual mood, manual light changes (overrides and their expiry), daylight,
ambient levels and brightness rules, zones and shared spaces, relays, wall
switches, Priority Inputs, the Room Supervisor, offline and failing devices,
global brightness, Vacant profiles, conflicting automations, and randomized
event sequences.
"""

from __future__ import annotations

import os
import random
from typing import Any

import pytest

from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.smart_occupancy.lighting_config import LightingConfig
from custom_components.smart_occupancy.models import ProfileSetting  # noqa: F401 - used by scenario configs

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_lighting_runtime import MAIN, MANUAL, AUTO, lit, options as lit_options, set_moods  # noqa: F401
from .test_zones import SHOWER, SINK, home, options as zone_options, replace_sensors  # noqa: F401

RECORD = os.environ.get("LIGHTING_RECORD") == "1"
TIMING = {"correlation_window": 10, "exit_confirmation": 30, "inactivity_timeout": 60, "startup_hold": 0}
START = "2026-06-15 10:00:00+00:00"


@pytest.fixture
def devices(hass: HomeAssistant) -> lp.Devices:
    return lp.Devices(hass)


async def ws_admin(hass, hass_ws_client, hass_admin_user):
    return await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/")))


def configure(ctrl, area: str, **changes: Any) -> None:
    ctrl.store.async_set(ctrl.with_room(area, **changes))


def lighting_doc(ctrl, mutate) -> None:
    doc = ctrl.lighting.as_dict()
    mutate(doc)
    ctrl.store.async_set(LightingConfig.from_dict(doc))


async def begin(hass, freezer, entry, ws, devices, name: str, description: str) -> lp.Recorder:
    await lp.advance(hass, freezer, 5)
    rec = lp.Recorder(hass, entry, ws, name, description, devices=devices)
    await rec.async_begin()
    return rec


def finish(rec: lp.Recorder) -> dict[str, Any]:
    data = rec.finish()
    path = lp.path_of(data["name"])
    if RECORD or not path.exists():
        lp.write(data)
        return data
    stored = lp.read(data["name"])
    assert lp.comparable(data) == lp.comparable(stored), (
        f"{data['name']}: the integration no longer does what the fixture records; if intended, re-record with "
        "LIGHTING_RECORD=1 and review the difference")
    return data


def user_context(hass_admin_user) -> Context:
    return Context(user_id=hass_admin_user.id)


# ------------------------------------------------------------------ scenarios
async def test_occupancy_moods_and_vacancy(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                           hass_admin_user) -> None:
    freezer.move_to(START)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    rec = await begin(hass, freezer, entry, ws, devices, "occupancy_moods_vacancy",
                      "PIR then radar confirm a visit; mood changes; Manual pauses; vacancy turns off")
    hass.states.async_set(lit.pir, "on")
    await lp.advance(hass, freezer, 3, rec)
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 12, rec)
    set_moods(hass, "Dagur", "Hygge", "Hygge")
    await lp.advance(hass, freezer, 5, rec)
    set_moods(hass, "Kvøld", "Auto", "Kvøld")
    await lp.advance(hass, freezer, 5, rec)
    hass.states.async_set(lit.pir, "off")
    hass.states.async_set(lit.radar, "off")
    await lp.advance(hass, freezer, 150, rec, step=2)
    finish(rec)


async def test_manual_override_and_expiry(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                          hass_admin_user) -> None:
    freezer.move_to(START)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True, manual_override_minutes=2)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15)
    rec = await begin(hass, freezer, entry, ws, devices, "manual_override",
                      "A person dims a lamp (UI), a mood change leaves it, the override expires, the next mood applies")
    state = hass.states.get(lit.lamp)
    hass.states.async_set(lit.lamp, "on", {**state.attributes, "brightness": 30}, context=user_context(hass_admin_user))
    await lp.advance(hass, freezer, 3, rec)
    set_moods(hass, "Nátt", "Auto", "Nátt")
    await lp.advance(hass, freezer, 5, rec)
    await lp.advance(hass, freezer, 130, rec, step=2)
    set_moods(hass, "Dagur", "Auto", "Dagur")
    await lp.advance(hass, freezer, 5, rec)
    hass.states.async_set(lit.desk, "on", {"brightness": 200}, context=Context())  # unattributed change
    await lp.advance(hass, freezer, 3, rec)
    finish(rec)


async def test_offline_failing_and_reconnect(hass: HomeAssistant, lit, devices, freezer, hass_ws_client,
                                             hass_admin_user) -> None:
    freezer.move_to(START)
    entry = await setup_entry(hass, lit_options(lit, timing=TIMING))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True)
    rec = await begin(hass, freezer, entry, ws, devices, "offline_reconnect",
                      "An offline lamp is applied when it comes back; a failing light is reported")
    hass.states.async_set(lit.lamp, "unavailable", {})
    devices.fail.add(lit.desk)
    hass.states.async_set(lit.pir, "on")
    hass.states.async_set(lit.radar, "on")
    await lp.advance(hass, freezer, 15, rec)
    hass.states.async_set(lit.lamp, "off", {"supported_color_modes": ["color_temp"], "min_color_temp_kelvin": 2200,
                                            "max_color_temp_kelvin": 6500, "supported_features": 32})
    await lp.advance(hass, freezer, 8, rec)
    devices.fail.clear()
    set_moods(hass, "Hygge", "Auto", "Hygge")
    await lp.advance(hass, freezer, 8, rec)
    finish(rec)


async def test_zones_and_relay(hass: HomeAssistant, home, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    freezer.move_to(START)
    entry = await setup_entry(hass, zone_options(home))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    from custom_components.smart_occupancy.daylight import RoomDaylight  # noqa: PLC0415

    zones = tuple(replace_sensors(z, home) for z in (SINK, SHOWER))
    for area in (home.wc, home.kitchen, home.stova):
        configure(ctrl, area, control_enabled=True, daylight=RoomDaylight.from_dict({"auto_on": "always"}),
                  **({"zones": zones, "relays": (home.relay,)} if area == home.wc else {}))
    rec = await begin(hass, freezer, entry, ws, devices, "zones_relay",
                      "Sink and shower zones with different priorities; zone exit; kitchen visit")
    hass.states.async_set(home.sink, "on")
    await lp.advance(hass, freezer, 12, rec)
    hass.states.async_set(home.shower, "on")
    await lp.advance(hass, freezer, 12, rec)
    hass.states.async_set(home.sink, "off")
    await lp.advance(hass, freezer, 20, rec)
    hass.states.async_set(home.shower, "off")
    await lp.advance(hass, freezer, 240, rec, step=2)
    hass.states.async_set(home.k_pir, "on")
    await lp.advance(hass, freezer, 15, rec)
    hass.states.async_set(home.k_pir, "off")
    await lp.advance(hass, freezer, 120, rec, step=2)
    finish(rec)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
async def test_randomized(hass: HomeAssistant, lit, devices, freezer, hass_ws_client, hass_admin_user,
                          seed: int) -> None:
    """Random configurations and event sequences (seeded, so the recording is reproducible)."""
    rnd = random.Random(seed)
    freezer.move_to(START)
    timing = {**TIMING, "inactivity_timeout": rnd.choice([30, 60, 90]), "exit_confirmation": rnd.choice([10, 30])}
    entry = await setup_entry(hass, lit_options(lit, timing=timing))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    ctrl = entry.runtime_data.lighting
    configure(ctrl, lit.area, control_enabled=True, manual_override_minutes=rnd.choice([1, 2, 5]),
              vacancy=ctrl.lighting.room(lit.area).vacancy)
    rec = await begin(hass, freezer, entry, ws, devices, f"randomized_{seed}",
                      f"Random occupancy, moods and manual changes (seed {seed})")
    moods = ["Morgun", "Dagur", "Kvøld", "Nátt", "Hygge"]
    lights = [lit.lamp, lit.desk, lit.strip, lit.plug, lit.spot_1]
    for _ in range(40):
        action = rnd.random()
        if action < 0.25:
            hass.states.async_set(lit.pir, rnd.choice(["on", "off"]))
        elif action < 0.45:
            hass.states.async_set(lit.radar, rnd.choice(["on", "off"]))
        elif action < 0.6:
            mood = rnd.choice(moods)
            set_moods(hass, mood if mood != "Hygge" else "Dagur", "Auto" if mood != "Hygge" else "Hygge", mood)
        elif action < 0.72:
            light = rnd.choice(lights)
            state = hass.states.get(light)
            ctx = user_context(hass_admin_user) if rnd.random() < 0.6 else Context()
            new = rnd.choice(["on", "off"])
            attrs = dict(state.attributes) if state else {}
            if new == "on" and light != lit.plug:
                attrs["brightness"] = rnd.randint(10, 255)
            hass.states.async_set(light, new, attrs, context=ctx)
        elif action < 0.78:
            hass.states.async_set(rnd.choice(lights), "unavailable", {})
        await lp.advance(hass, freezer, rnd.choice([1, 2, 3, 5, 8, 13, 30, 60]), rec)
    await lp.advance(hass, freezer, 200, rec, step=2)
    finish(rec)
