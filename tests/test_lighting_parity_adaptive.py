"""Phase A4 parity scenarios (5): global brightness and colour temperature from an existing adaptive system.

See ``test_lighting_parity.py``: each scenario is recorded and compared with its fixture; the App replays it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import lighting_parity as lp
from .conftest import setup_entry
from .test_adaptive_runtime import BRIGHT, CT, activate, configure_sources, home, options  # noqa: F401
from .test_lighting_parity import START, begin, devices, finish, ws_admin  # noqa: F401


async def test_global_adjustment(hass: HomeAssistant, home, devices, freezer, hass_ws_client, hass_admin_user) -> None:
    freezer.move_to(START)
    entry = await setup_entry(hass, options(home))
    ws = await ws_admin(hass, hass_ws_client, hass_admin_user)
    await configure_sources(hass, entry)
    await activate(hass, entry, home, freezer)
    hass.states.async_set(home.pir, "on")
    hass.states.async_set(home.radar, "on")
    await lp.advance(hass, freezer, 15)
    rec = await begin(hass, freezer, entry, ws, devices, "global_adjustment",
                      "Lights that follow the global brightness and colour temperature are adjusted gradually")
    for brightness, kelvin in ((60, 3500), (55, 3400), (40, 3000), (40, 2900), (80, 4500)):
        hass.states.async_set(BRIGHT, str(brightness), {"unit_of_measurement": "%"})
        hass.states.async_set(CT, str(kelvin), {"unit_of_measurement": "K"})
        await lp.advance(hass, freezer, 70, rec, step=2)
    finish(rec)
