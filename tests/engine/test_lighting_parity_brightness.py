"""Phase A4 parity scenarios (4): ambient light levels, brightness rules and Priority Inputs.

See ``test_lighting_parity.py``: each scenario is recorded and compared with its fixture; the App replays it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

import pytest

from . import lighting_parity as lp
from . import test_brightness
from .test_brightness import OUTDOOR, home, start  # noqa: F401
from .test_lighting_parity import PARITY_DAY, START, begin, devices, finish, ws_admin  # noqa: F401
from .test_phase48_runtime import SETTLE, cupboard, door


@pytest.fixture(autouse=True)
def _parity_day(monkeypatch):
    """The recorded day (``start`` moves the clock to ``test_brightness.DAY``, which depends on the day it runs)."""
    monkeypatch.setattr(test_brightness, "DAY", PARITY_DAY)


async def wait(hass, freezer, seconds: float, rec, sim) -> None:
    """Time passes in steps of 2 s; the indoor sensor reports every 20 s (as the brightness tests' wait)."""
    step = 0.0
    while seconds > 0:
        dt = min(seconds, 2)
        await lp.advance(hass, freezer, dt, rec, step=2)
        seconds -= dt
        step += dt
        if step >= 20:
            step = 0.0
            sim.report()
            await hass.async_block_till_done()


async def test_ambient_levels_and_matrix(hass: HomeAssistant, home, devices, freezer, hass_ws_client,
                                         hass_admin_user) -> None:
    freezer.move_to(START)  # (the sun: the recording must not depend on the wall clock)
    entry, ctrl, sim = await start(hass, home, freezer, devices, outdoor_lux=1200)
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    rec = await begin(hass, freezer, entry, ws, devices, "ambient_matrix",
                      "Ambient categories follow outdoor light; the floor lamp's matrix sets its brightness per "
                      "category and zone; the lamp's own light never raises the category")
    hass.states.async_set(home.pir, "on")
    hass.states.async_set(home.radar, "on")
    await wait(hass, freezer, 10, rec, sim)
    hass.states.async_set(home.desk, "on")
    await wait(hass, freezer, 30, rec, sim)
    for lux in (30000, 200, 20):
        hass.states.async_set(OUTDOOR, str(lux), {"unit_of_measurement": "lx", "device_class": "illuminance"})
        await wait(hass, freezer, 90, rec, sim)
    hass.states.async_set(home.desk, "off")
    await wait(hass, freezer, 40, rec, sim)
    await finish(rec)


async def test_priority_input(hass: HomeAssistant, home, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    freezer.move_to(START)  # (the sun: the recording must not depend on the wall clock)
    door(hass, "off")
    entry, ctrl, sim = await start(hass, home, freezer, devices, outdoor_lux=50000, zones=(),
                                   priority_inputs=(cupboard(home),))
    await wait(hass, freezer, SETTLE, None, sim)
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    rec = await begin(hass, freezer, entry, ws, devices, "priority_input",
                      "A cupboard door forces its light on in a vacant bright room; a visit; release hands back")
    door(hass, "on")
    await wait(hass, freezer, 4, rec, sim)
    hass.states.async_set(home.pir, "on")
    hass.states.async_set(home.radar, "on")
    await wait(hass, freezer, 6, rec, sim)
    door(hass, "off")
    await wait(hass, freezer, 6, rec, sim)
    hass.states.async_set(home.pir, "off")
    hass.states.async_set(home.radar, "off")
    await wait(hass, freezer, 90, rec, sim)
    door(hass, "on")
    await wait(hass, freezer, 4, rec, sim)
    door(hass, "off")
    await wait(hass, freezer, 6, rec, sim)
    await finish(rec)
