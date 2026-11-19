"""App 0.8.0 (Phase A6.1): multi-sensor sunlight for curtain windows.

Pure tests of the ANY-activate / ALL-release rules (``curtains/multisun.py``), then the runtime in a real Home
Assistant: a south window with two of its own sunlight sensors next to the house's outdoor light sensor, the
existing curtain rules (sleep, holds, movement safety) and a restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.curtains import multisun as M, rules
from custom_components.smart_occupancy.curtains.config import Window

from .test_curtains import (  # noqa: F401  (fixtures)
    KAMAR,
    STOVA_BLACKOUT,
    STOVA_SHEER,
    covers,
    house,
    set_cover,
)
from .test_curtains_auto import LUX, admin_ws, begin, enable, faroe, kamar, stova  # noqa: F401

EAST, WEST, ROOF = "sensor.sun_east", "sensor.sun_west", "sensor.sun_roof"
T0 = datetime(2026, 6, 15, 12, 0, tzinfo=dt_util.UTC)
LEVELS = {"on_lux": 30000.0, "off_lux": 15000.0, "release_minutes": 15.0}


def reading(entity_id: str, value: float | None, *, ok: bool = True, enabled: bool = True) -> M.SensorReading:
    healthy = ok and value is not None
    return M.SensorReading(entity_id, enabled, healthy, value if healthy else None, "ok" if healthy else "unavailable")


def run(state: M.SunState, minutes: float, **values: float | None) -> M.SunResult:
    readings = [reading(e, v) for e, v in values.items()]
    return M.evaluate(state, readings, now=T0 + timedelta(minutes=minutes), **LEVELS)


# ======================================================================== pure rules
def test_one_of_several_sensors_activates_and_the_others_need_not_agree() -> None:
    state = M.SunState()
    result = run(state, 0, east=40000, west=2000, roof=8000)
    assert result.active and result.changed == "activated" and result.triggered_by == "east"
    assert result.above_on == ["east"] and state.since == T0
    # The others stay below activation: protection stays on, nothing about them is averaged in.
    result = run(state, 1, east=31000, west=1000, roof=1000)
    assert result.active and result.changed is None and result.countdown_started is None


def test_release_needs_every_sensor_below_for_the_whole_delay() -> None:
    state = M.SunState()
    run(state, 0, east=40000, west=2000)
    result = run(state, 1, east=14000, west=2000)
    assert result.active and result.countdown_started == T0 + timedelta(minutes=1)
    assert result.remaining_seconds == 15 * 60
    result = run(state, 15.9, east=14000, west=2000)
    assert result.active and 0 < result.remaining_seconds <= 6
    result = run(state, 16, east=14000, west=2000)
    assert not result.active and result.changed == "released" and state.triggered_by is None


def test_a_sensor_rising_during_the_countdown_restarts_it() -> None:
    state = M.SunState()
    run(state, 0, east=40000, west=2000)
    run(state, 1, east=10000, west=2000)
    result = run(state, 10, east=10000, west=16000)        # west rises to the release level or above
    assert result.active and result.blocking == ["west"] and result.countdown_started is None
    assert "west" in result.reason
    result = run(state, 11, east=10000, west=2000)
    assert result.countdown_started == T0 + timedelta(minutes=11)     # a fresh countdown
    result = run(state, 20, east=35000, west=2000)          # above activation: still active, reset again
    assert result.active and result.above_on == ["east"] and result.countdown_started is None
    assert state.countdown is None


def test_threshold_boundaries() -> None:
    state = M.SunState()
    assert not run(state, 0, east=30000).active                       # equal to "activate above": not above
    assert run(state, 1, east=30000.5).active                         # strictly above
    result = run(state, 2, east=15000)                                # equal to "release below": not below
    assert result.active and result.blocking == ["east"] and result.countdown_started is None
    result = run(state, 3, east=14999.9)
    assert result.countdown_started is not None


def test_invalid_threshold_ordering_is_refused() -> None:
    assert M.check_levels(30000, 15000) is None
    assert M.check_levels(30000, 30000) is not None
    assert M.check_levels(10000, 20000) is not None
    assert M.check_levels("a", 1) is not None
    problems = rules.consistency({**rules.defaults(), "multisun_on_lux": 10000, "multisun_off_lux": 10000})
    assert any("Multi-sensor" in p for p in problems)
    assert not any("Multi-sensor" in p for p in rules.consistency(rules.defaults()))


def test_an_invalid_sensor_is_never_sunshine_and_never_confirms_a_release() -> None:
    state = M.SunState()
    readings = [reading("east", None, ok=False), reading("west", 2000)]
    result = M.evaluate(state, readings, now=T0, **LEVELS)
    assert not result.active and result.invalid == ["east"]
    run(state, 1, east=40000, west=2000)
    run(state, 2, east=10000, west=2000)                              # the countdown runs
    readings = [reading("east", None, ok=False), reading("west", 2000)]
    result = M.evaluate(state, readings, now=T0 + timedelta(minutes=30), **LEVELS)
    # 28 minutes later, but east is unavailable: release cannot be confirmed; protection is kept.
    assert result.active and not result.release_confirmable and result.countdown_started is None
    assert "cannot be confirmed" in result.reason and "east" in result.reason
    # Recovery: the countdown starts again from the recovery, never from the earlier readings.
    result = run(state, 31, east=10000, west=2000)
    assert result.active and result.countdown_started == T0 + timedelta(minutes=31)
    assert not run(state, 46, east=10000, west=2000).active


def test_a_disabled_sensor_is_ignored_and_disabling_every_sensor_releases() -> None:
    state = M.SunState()
    readings = [reading("east", 40000, enabled=False), reading("west", 2000)]
    assert not M.evaluate(state, readings, now=T0, **LEVELS).active
    run(state, 1, east=40000, west=2000)
    failed = [reading("east", None, ok=False), reading("west", 2000)]
    assert M.evaluate(state, failed, now=T0 + timedelta(minutes=2), **LEVELS).active
    # The administrator disables the failed sensor (a reviewed change): the remaining one decides.
    without = [reading("east", None, ok=False, enabled=False), reading("west", 2000)]
    result = M.evaluate(state, without, now=T0 + timedelta(minutes=3), **LEVELS)
    assert result.active and result.countdown_started == T0 + timedelta(minutes=3)
    assert not M.evaluate(state, without, now=T0 + timedelta(minutes=18), **LEVELS).active


def test_restart_keeps_protection_and_restarts_the_countdown() -> None:
    state = M.SunState()
    run(state, 0, east=40000, west=2000)
    run(state, 1, east=10000, west=2000)
    assert state.countdown is not None
    restored = M.SunState.restore(state.export())
    assert restored.active and restored.triggered_by == "east" and restored.countdown is None
    # 14 minutes after the countdown started (before the restart): not released, it starts again.
    result = run(restored, 15, east=10000, west=2000)
    assert result.active and result.countdown_started == T0 + timedelta(minutes=15)
    assert M.SunState.restore({"active": False}).active is False
    assert M.SunState.restore("garbage").active is False


def test_simulation_is_read_only() -> None:
    state = M.SunState(True, "east", T0)
    sensors = (M.SunSensor("east"), M.SunSensor("west"))
    steps = [(0, {"east": 9000, "west": 1000}), (16, {"east": 9000, "west": 1000})]
    out = M.simulate(state, steps, sensors, start=T0, **LEVELS)
    assert out[-1]["active"] is False and out[-1]["changed"] == "released"
    assert state.active is True and state.countdown is None          # the real state is untouched
    out = M.simulate(state, [(0, {"east": "unavailable", "west": 1000}), (60, {"east": "unavailable", "west": 1000})],
                     sensors, start=T0, **LEVELS)
    assert out[-1]["active"] is True and out[-1]["release_confirmable"] is False


def test_old_windows_serialize_exactly_as_before() -> None:
    window = Window.from_dict({"window_id": "w1", "name": "Old", "layers": [{"layer_id": "l1", "entity_id": "cover.x",
                                                                           "kind": "blackout"}]})
    assert window is not None and not window.multi_sun and window.sun_sensors == ()
    assert "multi_sun" not in window.as_dict() and "sun_sensors" not in window.as_dict()
    configured = Window.from_dict({**window.as_dict(), "multi_sun": True,
                                   "sun_sensors": ["sensor.a", {"entity_id": "sensor.b", "enabled": False}, "light.x",
                                                   "sensor.a"]})
    assert configured.multi_sun and [s.as_dict() for s in configured.sun_sensors] == [
        {"entity_id": "sensor.a", "enabled": True}, {"entity_id": "sensor.b", "enabled": False}]
    assert configured.multi_sun_active and Window.from_dict(configured.as_dict()) == configured


# ======================================================================== runtime
class Sensors:
    """The window's own sunlight sensors: they report every minute (with a 1 lx jitter, as real sensors do)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.values: dict[str, Any] = {}
        self.tick = 0

    def set(self, **values: Any) -> None:
        for key, value in values.items():
            self.values[{"east": EAST, "west": WEST, "roof": ROOF}[key]] = value
        self.report()

    def report(self) -> None:
        self.tick += 1
        for entity_id, value in self.values.items():
            shown = value + self.tick % 2 if isinstance(value, (int, float)) and value >= 100 else value
            self.hass.states.async_set(entity_id, str(shown), {"unit_of_measurement": "lx",
                                                               "device_class": "illuminance",
                                                               "friendly_name": entity_id.split(".")[1].title()})


async def walk(r, sensors: Sensors, minutes: float) -> None:
    for _ in range(max(1, round(minutes))):
        r.freezer.tick(timedelta(minutes=1))
        r.hass.states.async_set(LUX, str(3000 + sensors.tick % 2), {"unit_of_measurement": "lx",
                                                                    "device_class": "illuminance"})
        sensors.report()
        async_fire_time_changed(r.hass)
        await r.hass.async_block_till_done()


async def multisun_window(hass, pilot, house, covers, admin_ws, freezer, **fields: Any):
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    # Midday, the sun high but *behind* the window (north): the geometry alone would never shade it, and the
    # house's light sensor reads ordinary daylight. Only the window's own sensors can see the sunlight.
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-06-15T12:05:00+00:00", lux=3000,
                        elevation=40, azimuth=0)
    sensors = Sensors(hass)
    sensors.set(east=2000, west=2000)
    await hass.async_block_till_done()
    s = await stova(r.api, house, solar_protection=True, multi_sun=True,
                    sun_sensors=[{"entity_id": EAST}, {"entity_id": WEST}], **fields)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"min_interval": 0})
    await walk(r, sensors, 3)
    await r.settle()
    await walk(r, sensors, 11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    return r, sensors, s


async def test_any_sensor_starts_sun_protection_and_all_must_stay_below_to_release(hass, pilot, house, covers,
                                                                                   admin_ws, freezer, faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    assert r.condition(s) == "day" and covers.calls == []
    env = r.auto(s)["environment"]["multi_sun"]
    assert env["active_source"] and [x["status"] for x in env["sensors"]] == ["ok", "ok"]
    # East sees direct sun: immediately (no "strong for 10 minutes" of the single-sensor evidence).
    sensors.set(east=42000)
    await walk(r, sensors, 1)
    assert r.condition(s) == "sun" and len(covers.calls) == 1
    result = r.auto(s)["environment"]["multi_sun"]["result"]
    assert result["active"] and result["triggered_by"] == EAST
    assert r.auto(s)["environment"]["direct_sun"]["multi_sun"] is True
    # A cloud: every sensor below 15000 lx, but west comes back to 16000 after 10 minutes: the countdown restarts.
    sensors.set(east=8000, west=2000)
    await walk(r, sensors, 10)
    assert r.condition(s) == "sun"
    sensors.set(west=16000)
    await walk(r, sensors, 1)
    result = r.auto(s)["environment"]["multi_sun"]["result"]
    assert result["blocking"] == [WEST] and result["countdown_started"] is None
    sensors.set(west=2000)
    await walk(r, sensors, 14)
    assert r.condition(s) == "sun" and len(covers.calls) == 1           # 14 of 15 minutes
    await walk(r, sensors, 2)
    # Released: the complete decision is evaluated again (here: Day), through the normal movement rules.
    assert r.condition(s) == "day" and len(covers.calls) == 2 and r.pos(STOVA_SHEER) == 100
    log = [x for x in r.auto(s)["log"] if x["action"] == "sun_sensors"]
    assert [x["text"][:9] for x in log[:2]] == ["Released:", "Direct su"]


async def test_a_failed_sensor_keeps_protection_until_it_is_disabled(hass, pilot, house, covers, admin_ws, freezer,
                                                                     faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    sensors.set(west=45000)
    await walk(r, sensors, 1)
    assert r.condition(s) == "sun"
    moves = len(covers.calls)
    # Every value drops, but east goes unavailable: release cannot be confirmed, for as long as it lasts.
    sensors.set(east="unavailable", west=1000)
    await walk(r, sensors, 40)
    assert r.condition(s) == "sun" and len(covers.calls) == moves
    result = r.auto(s)["environment"]["multi_sun"]["result"]
    assert result["release_confirmable"] is False and EAST in result["invalid"]
    model = await r.api.ok("model")
    window = next(w for w in model["windows"] if w["window_id"] == s)
    assert any("Sunlight sensor" in p["text"] and "prevent the release" in p["text"] for p in window["problems"])
    # Reviewed configuration: the failed sensor is disabled; the countdown starts, then releases.
    await r.api.ok("window/update", window_id=s, sun_sensors=[{"entity_id": EAST, "enabled": False},
                                                             {"entity_id": WEST}])
    await walk(r, sensors, 14)
    assert r.condition(s) == "sun"
    await walk(r, sensors, 2)
    assert r.condition(s) == "day" and len(covers.calls) == moves + 1


async def test_sensor_recovery_and_no_invalid_reading_counts_as_sunshine(hass, pilot, house, covers, admin_ws,
                                                                         freezer, faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    sensors.set(east="unavailable", west="not a number")
    await walk(r, sensors, 5)
    assert r.condition(s) == "day" and covers.calls == []
    direct = r.auto(s)["environment"]["direct_sun"]
    assert direct["status"] == "uncertain"
    sensors.set(east=2000, west=2000)
    await walk(r, sensors, 2)
    assert r.condition(s) == "day" and covers.calls == []
    sensors.set(east=50000)                           # recovered and bright: protection, once
    await walk(r, sensors, 2)
    assert r.condition(s) == "sun" and len(covers.calls) == 1


async def test_sleep_and_manual_holds_still_win(hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    # A person moves the sheer, with "keep a manual move until I resume": the hold wins over a new sunlight
    # trigger. (With the default "until the next automatic phase" a sun trigger is such a phase, as before.)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"manual_policy": "until_released"})
    set_cover(hass, STOVA_SHEER, 40, context=Context())
    await walk(r, sensors, 1)
    assert r.condition(s) == "manual_override"
    sensors.set(east=60000)
    await walk(r, sensors, 2)
    assert r.condition(s) == "manual_override" and r.pos(STOVA_SHEER) == 40 and covers.calls == []
    # Kamar: sleep protection keeps it closed while someone sleeps, whatever the window's sensors say.
    set_cover(hass, KAMAR, 0)
    r.sleep(True)
    k = await kamar(r.api, house, solar_protection=True, multi_sun=True, sun_sensors=[EAST])
    await r.api.ok("auto/settings", scope="global", values={"sleep_entity": "input_boolean.sleep"})
    await walk(r, sensors, 2)
    await enable(r.api, k)
    await walk(r, sensors, 2)
    assert r.condition(k) == "sleep" and r.pos(KAMAR) == 0


async def test_restart_during_the_countdown_never_releases_early(hass, pilot, house, covers, admin_ws, freezer,
                                                                 faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    sensors.set(east=40000)
    await walk(r, sensors, 1)
    assert r.condition(s) == "sun"
    sensors.set(east=5000)
    await walk(r, sensors, 12)                         # 12 of 15 minutes, then Home Assistant restarts
    entry = hass.config_entries.async_entries("smart_occupancy")[0]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    r.mgr = entry.runtime_data.curtains
    restarted = dt_util.utcnow()
    sun = r.mgr.auto.runtimes[s].sun
    # Still active after the restart; the countdown starts again from the restart, never from before it.
    assert sun.active and (sun.countdown is None or sun.countdown >= restarted)
    calls = len(covers.calls)
    # The start-up settle and the light history starting again: the window waits and keeps its sun position
    # (as after every restart); the multi-sensor state stays active underneath.
    await r.settle()
    assert r.condition(s) in ("waiting", "sun") and len(covers.calls) == calls
    assert r.mgr.auto.runtimes[s].sun.active
    await walk(r, sensors, 10)                         # 13 minutes after the restart: not released
    assert r.condition(s) == "sun" and len(covers.calls) == calls
    await walk(r, sensors, 7)                          # 20 minutes: the full delay counted from the restart
    assert r.condition(s) == "day" and len(covers.calls) == calls + 1


async def test_settings_inheritance_validation_and_the_read_only_simulation(hass, pilot, house, covers, admin_ws,
                                                                           freezer, faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    # Global levels, a room override, a window override: the usual scopes.
    await r.api.ok("auto/settings", scope="global", values={"multisun_on_lux": 25000})
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"multisun_release": 5})
    auto = r.auto(s)
    assert (auto["values"]["multisun_on_lux"], auto["sources"]["multisun_on_lux"]) == (25000, "global")
    assert (auto["values"]["multisun_release"], auto["sources"]["multisun_release"]) == (5, "window")
    # Release must stay below activation.
    bad = await r.api.error("auto/settings", scope="window", window_id=s, values={"multisun_off_lux": 26000})
    assert bad["code"]
    # Unknown or non-sensor entities are refused.
    bad = await r.api.error("window/update", window_id=s, sun_sensors=[{"entity_id": "sensor.does_not_exist"}])
    assert bad["code"] == "unknown_entity"
    bad = await r.api.error("window/update", window_id=s, sun_sensors=["light.kitchen"])
    assert bad["code"] == "invalid_sun_sensors"
    # The simulation: east 30000 now, then everything at 1000 lx for 6 minutes; nothing is saved or moved.
    before = (r.auto(s)["environment"]["multi_sun"]["result"], len(covers.calls))
    sim = await r.api.ok("sun_sensors/simulate", window_id=s, readings={EAST: 30000, WEST: 1000},
                         then={EAST: 1000, WEST: 1000}, elapsed_minutes=6)
    assert sim["steps"][0]["active"] is True and sim["steps"][0]["triggered_by"] == EAST
    assert sim["result"]["active"] is False and sim["result"]["changed"] == "released" and sim["saved"] is False
    sim = await r.api.ok("sun_sensors/simulate", window_id=s, readings={EAST: 30000, WEST: None},
                         then={EAST: 1000, WEST: None}, elapsed_minutes=60)
    assert sim["result"]["active"] is True and sim["result"]["release_confirmable"] is False
    assert (r.auto(s)["environment"]["multi_sun"]["result"], len(covers.calls)) == before
    bad = await r.api.error("sun_sensors/simulate", window_id=s, readings={}, levels={"on_lux": 100, "off_lux": 200})
    assert bad["code"] == "invalid_levels"


async def test_renamed_sensor_follows_and_old_windows_keep_their_sun_evidence(hass, pilot, house, covers, admin_ws,
                                                                              freezer, faroe) -> None:
    r, sensors, s = await multisun_window(hass, pilot, house, covers, admin_ws, freezer)
    assert r.mgr.rename_entity(EAST, "sensor.sun_east_renamed")
    assert [x.entity_id for x in r.mgr.config.windows[s].sun_sensors] == ["sensor.sun_east_renamed", WEST]
    # Switching the feature off returns the window to its normal (geometry + house sensor) evidence.
    await r.api.ok("window/update", window_id=s, multi_sun=False)
    await walk(r, sensors, 2)
    assert r.auto(s)["environment"]["direct_sun"]["status"] == "not_exposed"
    assert r.auto(s)["environment"]["multi_sun"]["active_source"] is False


@pytest.fixture(autouse=True)
def _no_sun_sensor_entities_left(hass: HomeAssistant):
    yield
