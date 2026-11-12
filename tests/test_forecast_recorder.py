"""Occupancy forecast (0.5.0): the history query against the REAL Recorder (in-memory database).

States are written with past timestamps by the test itself (simulated values),
then read back through the forecast's own query and conversion.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import Pilot, pilot_options, setup_entry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The Recorder must be set up before Home Assistant (overrides the conftest fixture)."""
    yield

async def test_real_recorder_query(recorder_mock, hass: HomeAssistant, pilot: Pilot) -> None:
    """The real Recorder: states written at past times come back through the forecast's query."""
    from homeassistant.components.recorder import get_instance
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    p = pilot
    entry = await setup_entry(hass, pilot_options(p))
    m = entry.runtime_data.forecast
    base = dt_util.utcnow() - timedelta(hours=3)
    for i, value in enumerate(["on", "off", "on", "off"]):
        hass.states.async_set(p.pir, value, timestamp=(base + timedelta(minutes=10 * i)).timestamp())
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    history = await m._recorder_fetch([p.pir, p.radar], base - timedelta(minutes=5), dt_util.utcnow())
    states = [s for _ts, s in history[p.pir]]
    assert states[-4:] == ["on", "off", "on", "off"]
    times = [ts for ts, _s in history[p.pir]][-4:]
    assert times == sorted(times) and abs(times[0] - base.timestamp()) < 1
    assert m.recorder_available()
    assert get_instance(hass).states_manager.oldest_ts is not None or m._recorder_oldest() is None

