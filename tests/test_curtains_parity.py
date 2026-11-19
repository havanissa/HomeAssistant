"""Phase A5 parity scenarios: the integration's curtain manager, recorded for the App's replay.

Each test runs one scenario in a real (test) Home Assistant with the simulated
covers, light sensor, sun and sleep switch of ``test_curtains_auto`` and records
it (``curtains_parity.CurtainRecorder``). The recording is compared with the
committed fixture in ``smart_lighting/tests/curtain_scenarios/`` (the commands,
their outcomes and the decisions), so the fixture always describes what the
integration really does; ``CURTAINS_RECORD=1`` writes the fixtures instead. The
App's suite (``smart_lighting/tests/test_curtains_parity.py``) replays every
fixture through the App's host and requires the same commands, decisions and
model answers.

Covered: night → dawn → Morning with a slow cover verified to its target;
dusk → Evening privacy → Night; a manual move during the day (hold started,
kept, released by the next phase); strong sunshine in front of a window (Sun
protection and its release); a cover that fails twice and is retried, Resume
with late device reports, an unavailable cover and its catch-up.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from homeassistant.core import Context, HomeAssistant

from . import curtains_parity as cp
from .conftest import pilot_options, setup_entry
from .test_curtains import KAMAR, STOVA_BLACKOUT, STOVA_SHEER, Api, house, set_cover  # noqa: F401
from .test_curtains_auto import LUX, Run, admin_ws, enable, faroe, kamar, stova  # noqa: F401

RECORD = os.environ.get("CURTAINS_RECORD") == "1"


@pytest.fixture
def covers(hass: HomeAssistant) -> cp.Covers:
    return cp.Covers(hass)


class Recording(Run):
    """The scenario driver of ``test_curtains_auto``, recording every clock step once a recorder is attached."""

    rec: cp.CurtainRecorder | None = None

    async def walk(self, minutes: float, step: float = 60) -> None:
        from datetime import timedelta  # noqa: PLC0415

        from pytest_homeassistant_custom_component.common import async_fire_time_changed  # noqa: PLC0415

        # What the last change started (a command's tasks) runs now, before the clock moves - as in a running
        # Home Assistant, where nothing waits for the next report.
        await self.hass.async_block_till_done()
        steps = max(1, round(minutes * 60 / step))
        for _ in range(steps):
            self.freezer.tick(timedelta(seconds=step))
            if self.reporting and self.lux_value is not None:
                self._ticks += 1
                value = self.lux_value
                if self.jitter and isinstance(value, (int, float)) and value >= 100:
                    value = value + self._ticks % 2
                self.hass.states.async_set(LUX, str(value), {"unit_of_measurement": "lx",
                                                             "device_class": "illuminance",
                                                             "friendly_name": "Terrace light"})
            if self.rec is not None:
                self.rec.tick()
            async_fire_time_changed(self.hass)
            await self.hass.async_block_till_done()


async def begin(hass, pilot, freezer, covers, admin_ws, when: str, *, lux: Any = 0, elevation: float = -10.0,
                azimuth: float = 120.0, sleep: bool | None = None) -> tuple[Any, Recording]:
    """As ``test_curtains_auto.begin``, with the recording driver."""
    freezer.move_to(cp.dt_util.parse_datetime(when))
    entry = await setup_entry(hass, pilot_options(pilot))
    api = Api(await admin_ws(), admin_ws)
    run = Recording(hass, freezer, api, entry.runtime_data.curtains, covers)
    if lux is not None:
        run.lux(lux)
    run.sun(elevation, azimuth)
    if sleep is not None:
        run.sleep(sleep)
    await hass.async_block_till_done()
    await api.ok("auto/settings", scope="global", values={"lux_entity": LUX})
    return entry, run


async def record(entry, r: Recording, covers: cp.Covers, name: str, description: str) -> cp.CurtainRecorder:
    rec = cp.CurtainRecorder(r.hass, entry, r.api, name, description, covers=covers)
    await rec.async_begin()
    r.mgr = entry.runtime_data.curtains
    r.rec = rec
    return rec


async def finish(rec: cp.CurtainRecorder) -> dict[str, Any]:
    await rec.async_capture_model()
    data = rec.finish()
    path = cp.path_of(data["name"])
    if RECORD or not path.exists():
        cp.write(data)
        return data
    stored = cp.read(data["name"])
    if cp.comparable(data) != cp.comparable(stored) and os.environ.get("CURTAINS_DIFF_DIR"):
        import gzip, json  # noqa: E401, PLC0415

        (Path(os.environ["CURTAINS_DIFF_DIR"]) / f"{data['name']}.json.gz").write_bytes(
            gzip.compress(json.dumps(data).encode()))
    assert cp.comparable(data) == cp.comparable(stored), (
        f"{data['name']}: the integration no longer does what the fixture records; if intended, re-record with "
        "CURTAINS_RECORD=1 and review the difference")
    return data


# ------------------------------------------------------------------ scenarios
async def test_morning_open(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """Thursday 19 Nov: Kamar Erva closed for the night; the night ends at 06:30, daylight is measured, Morning
    applies Half open; the cover travels 20 s (opening, half way, reached)."""
    set_cover(hass, KAMAR, 0)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T06:00:00+00:00", lux=0,
                           elevation=-12.0, azimuth=110, sleep=False)
    k = await kamar(r.api, house)
    await r.settle()
    await enable(r.api, k)
    await hass.async_block_till_done()
    assert covers.calls == [] and r.condition(k) == "night"
    rec = await record(entry, r, covers, "morning_open",
                       "Night ends, dawn light rises to daylight; Morning applies Half open to a slow cover")
    covers.travel = {KAMAR: 20}
    await r.walk(26)
    await r.walk(2, step=2)                 # 06:30: the night ends (timer)
    assert r.condition(k) == "waiting"
    await rec.async_capture_model()
    r.sun(-5.0, 118)
    r.lux(1)
    await r.walk(40)
    r.sun(-1.5, 126)
    r.lux(160)
    await r.walk(9)
    assert covers.calls == []
    await r.walk(3, step=2)                 # daylight stable for 10 min: Morning, the cover moves
    assert r.condition(k) == "morning" and len(covers.calls) == 1
    assert r.pos(KAMAR) == 50 and r.mgr.last[k]["status"] == "succeeded"
    await rec.async_capture_model()
    r.lux(2400)
    r.sun(3.0, 140)
    await r.walk(10)
    await finish(rec)


async def test_evening_privacy_night(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """Stova Suður open in the afternoon; dusk closes it for privacy (sheer 0, blackout 60); the night (moved to
    18:00 for this household) applies Full blackout."""
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T15:15:00+00:00", lux=3000,
                           elevation=4.0, azimuth=215)
    s = await stova(r.api, house)
    await r.api.ok("auto/settings", scope="global", values={"night_start_workday": "18:00"})
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert covers.calls == [] and r.condition(s) == "day"
    rec = await record(entry, r, covers, "evening_privacy_night",
                       "Dusk: Evening privacy closes the sheer and lowers the blackout; Night applies Full blackout")
    covers.travel = {STOVA_BLACKOUT: 12}
    await r.walk(5)
    r.lux(30)
    r.sun(-4.5, 230)
    await r.walk(9)
    await r.walk(3, step=2)                 # dusk stable for 10 min: Evening privacy
    assert r.condition(s) == "evening_privacy" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    await rec.async_capture_model()
    r.lux(0)
    r.sun(-30, 300)
    await r.until("17:58", step=300)
    await r.walk(5, step=2)                 # 18:00: Night
    assert r.condition(s) == "night" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0)
    await r.walk(5)
    await finish(rec)


async def test_manual_hold(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """A wall button closes the sheer during the day: a manual hold is started and kept until the next automatic
    phase (dusk), which releases it and applies Evening privacy."""
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                           elevation=6.0, azimuth=170)
    s = await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    await r.walk(3)                         # (reports within 90 s of our own command count as ours)
    rec = await record(entry, r, covers, "manual_hold",
                       "A manual move holds the window through the day; dusk releases the hold (Evening privacy)")
    await r.walk(13)                        # the restarted manager measures daylight again: Normal daytime
    assert r.condition(s) == "day" and len(covers.calls) == 2
    set_cover(hass, STOVA_SHEER, 30, context=Context())
    await r.walk(1, step=2)
    assert r.condition(s) == "manual_override" and r.auto(s)["hold"]["policy"] == "next_phase"
    await rec.async_capture_model()
    r.lux(2800)
    await r.walk(30)
    assert r.pos(STOVA_SHEER) == 30 and len(covers.calls) == 2
    r.lux(30)
    r.sun(-3.0, 225)
    await r.walk(9)
    await r.walk(3, step=2)
    assert r.condition(s) == "evening_privacy" and r.auto(s)["hold"] is None
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    await r.walk(3)
    await finish(rec)


async def test_sun_protection(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """Strong sunshine in front of the south window: Sun protection (Full shade) after the stable period; the sun
    leaving the window's sector releases it (Normal daytime)."""
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:05:00+00:00", lux=3000,
                           elevation=9.0, azimuth=170)
    s = await stova(r.api, house, solar_protection=True)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"min_interval": 0})
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert covers.calls == [] and r.condition(s) == "day"
    rec = await record(entry, r, covers, "sun_protection",
                       "Strong sunshine in front of the window: Sun protection applies Full shade; released when the "
                       "sun leaves the sector")
    covers.travel = {STOVA_SHEER: 8}
    await r.walk(4)
    r.lux(25000)
    await r.walk(9)
    assert r.condition(s) == "day"
    await r.walk(3, step=2)
    assert r.condition(s) == "sun" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100)
    await rec.async_capture_model()
    r.lux(5000)                             # a short cloud does not release it
    await r.walk(6)
    r.lux(20000)
    await r.walk(6)
    assert r.condition(s) == "sun" and len(covers.calls) == 1
    r.sun(12.0, 252)
    await r.walk(1, step=2)
    assert r.condition(s) == "sun"
    r.sun(12.0, 254)                        # out of the sector (± 70° and the 3° margin)
    await r.walk(1, step=2)
    assert r.condition(s) == "day" and r.pos(STOVA_SHEER) == 100
    await r.walk(4)
    await finish(rec)


async def test_device_failure_retry(hass: HomeAssistant, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """Kamar Erva's motor refuses the command: retried after 60 s and 5 min, then given up; the person resumes and
    the (now working) cover reports late; it drops off the network, sleep cannot close it, and it catches up when
    it returns."""
    set_cover(hass, KAMAR, 0)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                           elevation=6.0, azimuth=170, sleep=False)
    k = await kamar(r.api, house)
    covers.fail = {KAMAR}
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)                  # the first attempt fails just before the recording starts
    await hass.async_block_till_done()
    assert len(covers.calls) == 1 and r.mgr.last[k]["status"] == "failed"
    rec = await record(entry, r, covers, "device_failure_retry",
                       "A failing cover is retried with growing delays and given up; Resume with late reports; an "
                       "unavailable cover catches up when it returns")
    await r.walk(2, step=2)                 # the start-up settle period passes: daylight is measured again first
    assert len(covers.calls) == 1 and r.condition(k) == "waiting"
    await r.walk(9)
    await r.walk(2, step=2)                 # Normal daytime again: sent (the restored attempt key does not match,
    assert len(covers.calls) == 3, (r.auto(k)["status"], r.auto(k)["applied"])  # so it counts as a first attempt
    await r.walk(4)                         # and fails; retried after 60 s (attempt 2, fails)
    await r.walk(2, step=2)                 # the next retry, 5 minutes later (attempt 3, fails): given up
    assert len(covers.calls) == 4
    await r.walk(10)
    assert r.auto(k)["status"]["code"] == "gave_up"
    await rec.async_capture_model()
    covers.fail = set()
    covers.travel = {KAMAR: 40}
    await rec.async_api("resume", window_id=k)
    assert len(covers.calls) == 5 and r.auto(k)["status"]["action"] in ("moved", "moving")
    await r.walk(0.5, step=5)
    assert r.auto(k)["status"]["action"] == "moving"
    set_cover(hass, KAMAR, 55, state="opening", context=Context())   # a late in-between report is ours
    await r.walk(1, step=5)
    assert r.mgr.last[k]["status"] == "succeeded" and r.auto(k)["hold"] is None and r.pos(KAMAR) == 100
    await r.walk(2)
    set_cover(hass, KAMAR, None, state="unavailable")
    r.sleep(True)
    await r.walk(1, step=2)
    assert r.auto(k)["status"]["code"] == "unavailable" and len(covers.calls) == 5
    await rec.async_capture_model()
    covers.travel = {}
    set_cover(hass, KAMAR, 100)
    await r.walk(1, step=2)
    assert r.pos(KAMAR) == 0 and len(covers.calls) == 6
    await r.walk(2)
    await finish(rec)
