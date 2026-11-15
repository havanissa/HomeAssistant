"""Phase 4.6.1 (0.6.2): autonomous curtain control per window.

The house is simulated in the Faroes (Atlantic/Faroe, UTC+0 in November) with
the covers of ``test_curtains`` (Zigbee2MQTT-like: own report context, delayed
reports, failures, drop-outs), an outdoor light sensor that reports every
minute, ``sun.sun``, ``input_boolean.sleep`` and a window lamp. The clock only
moves forward (fixtures create states at the real time).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.const import OccupancyState
from custom_components.smart_occupancy.curtains import auto as auto_mod, logic, rules
from custom_components.smart_occupancy.curtains.config import CurtainConfigError

from .conftest import pilot_options, setup_entry
from .test_curtains import (  # noqa: F401  (fixtures)
    KAMAR,
    STOVA_BLACKOUT,
    STOVA_SHEER,
    Api,
    Covers,
    House,
    covers,
    house,
    layer_ids,
    set_cover,
)

LUX = "sensor.pir_tfr_illuminance"
SLEEP = "input_boolean.sleep"
LAMP = "light.kamar_erva_vindeyga"
FAROE = "Atlantic/Faroe"


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text)


class Run:
    """Drives one scenario: sensors, sun, sleep, lamp and the clock (in steps, like real reports)."""

    def __init__(self, hass: HomeAssistant, freezer, api: Api, mgr, covers: Covers) -> None:
        self.hass, self.freezer, self.api, self.mgr, self.covers = hass, freezer, api, mgr, covers
        self.lux_value: Any = None
        self.reporting = True

    def lux(self, value: Any) -> None:
        self.lux_value = value
        self.hass.states.async_set(LUX, str(value), {"unit_of_measurement": "lx", "device_class": "illuminance",
                                                     "friendly_name": "Terrace light"})

    def sun(self, elevation: float, azimuth: float = 140.0) -> None:
        self.hass.states.async_set("sun.sun", "above_horizon" if elevation > 0 else "below_horizon",
                                   {"elevation": elevation, "azimuth": azimuth})

    def sleep(self, on: bool | None) -> None:
        self.hass.states.async_set(SLEEP, {True: "on", False: "off", None: "unavailable"}[on])

    def lamp(self, on: bool) -> None:
        self.hass.states.async_set(LAMP, "on" if on else "off", {"friendly_name": "Window lamp"})

    async def walk(self, minutes: float, step: float = 60) -> None:
        """Let time pass; the light sensor reports (unchanged) after every step."""
        steps = max(1, round(minutes * 60 / step))
        for _ in range(steps):
            self.freezer.tick(timedelta(seconds=step))
            if self.reporting and self.lux_value is not None:
                self.lux(self.lux_value)
            async_fire_time_changed(self.hass)
            await self.hass.async_block_till_done()

    async def until(self, hhmm: str, step: float = 60) -> None:
        now = dt_util.now()
        hours, minutes = (int(x) for x in hhmm.split(":"))
        target = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await self.walk((target - now).total_seconds() / 60, step)

    async def settle(self) -> None:
        await self.walk(auto_mod.STARTUP_SETTLE_SECONDS / 60 + 1)

    def pos(self, entity_id: str) -> int | None:
        return self.hass.states.get(entity_id).attributes.get("current_position")

    def auto(self, window_id: str) -> dict[str, Any]:
        return self.mgr.auto.model(self.mgr.config.windows[window_id])

    def condition(self, window_id: str) -> str | None:
        decision = self.auto(window_id)["decision"]
        return decision["condition"] if decision else None


@pytest.fixture
async def faroe(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone(FAROE)


@pytest.fixture
def admin_ws(hass: HomeAssistant, hass_ws_client, hass_admin_user):
    """Connections with a fresh admin token each time: tokens of the moved clock expire after 30 minutes."""

    async def connect():
        refresh = await hass.auth.async_create_refresh_token(hass_admin_user, "https://example.com/app")
        return await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))

    return connect


async def begin(hass, pilot, freezer, covers, hass_ws_client, when: str, *, lux: Any = 0, elevation: float = -10.0,
                azimuth: float = 120.0, sleep: bool | None = None, entry_options=None) -> tuple[Any, Run]:
    freezer.move_to(utc(when))
    entry = await setup_entry(hass, entry_options or pilot_options(pilot))
    api = Api(await hass_ws_client(), hass_ws_client)
    run = Run(hass, freezer, api, entry.runtime_data.curtains, covers)
    if lux is not None:
        run.lux(lux)
    run.sun(elevation, azimuth)
    if sleep is not None:
        run.sleep(sleep)
    await hass.async_block_till_done()
    await api.ok("auto/settings", scope="global", values={"lux_entity": LUX})
    return entry, run


async def kamar(api: Api, house: House, **fields: Any) -> str:
    """Kamar Erva: one blackout cover, sleep protected, facing east (Fully open / Half open / Fully closed)."""
    created = await api.ok("window/create", name="Kamar Erva", area_id=house.kamar, direction="E",
                           layers=[{"entity_id": KAMAR, "kind": "blackout"}], sleep_protection=True, **fields)
    return created["window_id"]


async def stova(api: Api, house: House, **fields: Any) -> str:
    """Stova Suður: sheer + blackout, facing south, evening privacy."""
    created = await api.ok("window/create", name="Stova Suður", area_id=house.stova, direction="S", privacy=True,
                           layers=[{"entity_id": STOVA_SHEER, "kind": "sheer"},
                                   {"entity_id": STOVA_BLACKOUT, "kind": "blackout"}], **fields)
    return created["window_id"]


async def enable(api: Api, window_id: str) -> dict[str, Any]:
    review = await api.ok("auto/preview", window_id=window_id)
    assert not review["blockers"], review["blockers"]
    await api.ok("auto/enable", window_id=window_id, confirm=True)
    return review


# ======================================================================== pure logic
def values(**changes: Any) -> dict[str, Any]:
    return {**rules.defaults(), **changes}


def test_night_routines_workdays_and_free_days() -> None:
    tz = dt_util.get_time_zone(FAROE)
    v = values()

    def night(text: str) -> bool:
        return logic.night_now(datetime.fromisoformat(text).replace(tzinfo=tz), v)[0]

    assert night("2026-11-19T22:31") and not night("2026-11-19T22:29")   # Thu -> workday Fri: 22:30
    assert night("2026-11-20T06:29") and not night("2026-11-20T06:31")   # Fri morning (workday): 06:30
    assert not night("2026-11-20T23:00") and night("2026-11-20T23:31")   # Fri -> free Sat: 23:30
    assert night("2026-11-21T07:59") and not night("2026-11-21T08:01")   # Sat morning (free): 08:00
    assert night("2026-11-22T23:00")                                     # Sun -> workday Mon: 22:30
    assert not night("2026-11-23T06:45")
    # A night start after midnight belongs to the evening before; disabled nights never apply.
    late = values(night_start_workday="00:30")
    assert logic.night_now(datetime(2026, 11, 20, 0, 40, tzinfo=tz), late)[0]
    assert not logic.night_now(datetime(2026, 11, 19, 23, 0, tzinfo=tz), late)[0]
    assert not logic.night_now(datetime(2026, 11, 19, 23, 0, tzinfo=tz), values(night_enabled=False))[0]
    # Other free days (e.g. a Friday off).
    assert not logic.night_now(datetime(2026, 11, 19, 23, 0, tzinfo=tz), values(free_days=(4, 5, 6)))[0]


def test_precedence_is_explicit_and_follows_the_exceptions() -> None:
    keys = [r["key"] for r in logic.precedence(values())]
    assert keys == ["safety", "manual_override", "sleep", "night", "evening_privacy", "lamp", "sun", "morning",
                    "day", "waiting"]
    keys = [r["key"] for r in logic.precedence(values(lamp_bypass=("sleep", "sun")))]
    assert keys == ["safety", "manual_override", "sleep", "sun", "lamp", "night", "evening_privacy", "morning",
                    "day", "waiting"]
    rows = logic.precedence(values(manual_bypass=("sleep",)))
    assert "ended by Sleep" in rows[1]["word"]


def test_hysteresis_needs_stable_readings_and_keeps_its_state_between_thresholds() -> None:
    t0 = utc("2026-11-19T08:00:00+00:00")
    history, state = logic.History(), logic.Hysteresis()

    def at(minute: int, value: float | None) -> tuple[bool | None, datetime | None]:
        now = t0 + timedelta(minutes=minute)
        history.add(now, value)
        return state.evaluate(history, now, lambda v: v >= 150, timedelta(minutes=10), lambda v: v <= 50,
                              timedelta(minutes=10))

    assert at(0, 0) == (None, t0 + timedelta(minutes=10))
    assert at(10, 0)[0] is False                      # dark for 10 minutes
    assert at(11, 160)[0] is False and at(15, 140)[0] is False  # a dip breaks the run
    assert at(16, 170) == (False, t0 + timedelta(minutes=26))
    assert at(26, 170)[0] is True
    assert at(30, 90)[0] is True and at(45, 120)[0] is True     # between the thresholds: still daylight
    assert at(46, None)[0] is True and history.since(lambda v: v >= 0) is None  # unavailable breaks every run


def test_rule_settings_validation_and_resolution() -> None:
    assert rules.validate("daylight_lux", 200, "window") == 200
    assert rules.validate("free_days", [6, 5], "global") == (5, 6)
    assert rules.validate("lamp_bypass", ["sun", "sleep"], "room") == ("sleep", "sun")
    assert rules.validate("night_start_free", " 23:45 ", "global") == "23:45"
    assert rules.validate("daylight_lux", None, "window") is None
    for key, value, code in (("daylight_lux", 0, "invalid_daylight_lux"), ("daylight_lux", 2.5, "invalid_daylight_lux"),
                             ("night_end_free", "25:00", "invalid_night_end_free"),
                             ("sleep_entity", "light.x", "invalid_sleep_entity"), ("nope", 1, "unknown_setting")):
        with pytest.raises(CurtainConfigError) as err:
            rules.validate(key, value, "window")
        assert err.value.code == code
    with pytest.raises(CurtainConfigError) as err:
        rules.validate("lamp_entity", LAMP, "global")
    assert err.value.code == "wrong_scope"
    resolved, sources = rules.resolve({"daylight_lux": 200, "lux_entity": LUX}, {"daylight_lux": 300, "lux_entity": ""},
                                      {"daylight_lux": 400})
    assert resolved["daylight_lux"] == 400 and sources["daylight_lux"] == "window"
    assert resolved["lux_entity"] is None and sources["lux_entity"] == "room"   # the room has no sensor
    assert sources["dusk_lux"] == "default"
    assert rules.consistency(values(dusk_lux=200)) == ["“Dark below” must be lower than “Daylight from”"]
    # Damaged stored values are dropped, never guessed.
    assert rules.load({"daylight_lux": "lots", "dusk_lux": 40, "x": 1}, "global") == {"dusk_lux": 40}


# ====================================================================== the incident
async def test_replay_of_the_november_12_13_early_morning_opening(hass, pilot, house, covers, admin_ws,
                                                                 freezer, faroe) -> None:
    """11-12 and 11-13: the legacy chain opened the curtains at 07:00:14 at −10.2°, 0 lx, sleep on, 85 minutes
    before sunrise. Replayed with the same evidence on a Thursday and the Friday after: nothing opens before
    sleep has ended *and* daylight is measured; then Morning, then Day, then privacy and night again."""
    set_cover(hass, KAMAR, 0)
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T06:40:00+00:00", lux=0,
                        elevation=-12.0, sleep=True)
    api = r.api
    k, s = await kamar(api, house), await stova(api, house)
    await r.settle()
    await enable(api, k)
    await enable(api, s)
    assert r.condition(k) == "sleep" and r.condition(s) == "waiting"
    r.sun(-10.2, 115)
    await r.until("07:05")   # the legacy clock opening at 07:00 does not exist here
    assert covers.calls == []
    assert r.pos(KAMAR) == 0 and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0)
    assert r.auto(k)["decision"]["text"] == "Sleep mode is on"
    assert r.auto(s)["decision"]["text"].startswith("Waiting for daylight. Outdoor light is 0 lx")
    # 07:40: the user wakes up; it is still dark (1 lx, −5.1°).
    r.sun(-5.1, 125)
    r.lux(1)
    await r.until("07:40")
    r.sleep(False)
    await r.walk(1)
    assert r.condition(k) == "waiting"
    text = r.auto(k)["decision"]["text"]
    assert text == ("Waiting for daylight. Sleep mode ended, but outdoor light is 1 lx (daylight needs 150 lx for "
                    "10 min). Morning preset not yet applied")
    assert r.auto(k)["status"]["action"] == "keep" and covers.calls == []
    # 08:20: daylight, measured and stable for 10 minutes -> Morning (soft: Half open / Half shade).
    await r.until("08:20")
    r.sun(-1.5, 128)
    r.lux(160)
    await r.walk(9)
    assert covers.calls == []                      # not yet stable
    await r.walk(2)
    assert r.condition(k) == "morning" and r.condition(s) == "morning"
    assert r.pos(KAMAR) == 50
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (50, 100)
    assert len(covers.calls) == 3                  # one command per cover, together
    assert r.auto(k)["hold"] is None and r.auto(s)["hold"] is None  # our own reports are not manual changes
    # Morning lasts 120 minutes, then Normal daytime (Fully open).
    r.lux(2400)
    r.sun(5.0, 150)
    await r.walk(110, step=120)
    assert r.condition(k) == "morning"
    await r.walk(12, step=120)
    assert r.condition(k) == "day" and r.pos(KAMAR) == 100
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    # Dusk: Stova closes for privacy even without a usable light sensor (sun below −4°).
    await r.until("16:00", step=300)
    r.lux("unavailable")
    r.sun(-4.5, 230)
    await r.walk(2)
    assert r.condition(s) == "evening_privacy"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    assert r.condition(k) == "waiting" and r.pos(KAMAR) == 100   # Kamar has no privacy: kept
    # 22:30 workday night: both close; the user goes to bed at 23:10.
    r.lux(0)
    r.sun(-40, 330)
    await r.until("22:31", step=300)
    assert r.condition(k) == "night" and r.condition(s) == "night"
    assert r.pos(KAMAR) == 0 and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0)
    await r.until("23:10", step=300)
    r.sleep(True)
    calls = len(covers.calls)
    # Friday 07:00:14, exactly the failing moment: sleep on, 0 lx, −10.2°. Nothing opens.
    await r.until("07:00", step=300)
    r.sun(-10.2, 115)
    await r.walk(1)
    assert r.condition(k) == "sleep" and r.condition(s) == "waiting"
    assert len(covers.calls) == calls and r.pos(KAMAR) == 0 and r.pos(STOVA_BLACKOUT) == 0


# ======================================================================== modes
async def test_off_observing_and_automatic_per_window(hass, pilot, house, covers, admin_ws, freezer,
                                                      faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:10:00+00:00", lux=3000,
                        elevation=8)
    k, s = await kamar(r.api, house), await stova(r.api, house)
    await r.api.ok("window/update", window_id=k, mode="off")
    await r.settle()
    await r.walk(11)
    # Observing (the default for every new window) decides and explains, but sends nothing.
    assert r.condition(s) == "day"
    status = r.auto(s)["status"]
    assert status["action"] == "would_move" and "would move 2 covers to Fully open" in status["text"]
    assert r.auto(k)["status"]["action"] == "off" and r.auto(k)["decision"] is None
    assert covers.calls == []
    # Enabling one window shows the intended preset, its plan and warnings first, and moves only that window.
    review = await r.api.ok("auto/preview", window_id=s)
    assert review["preset_name"] == "Fully open" and review["moves"] == 2 and review["decision"]["condition"] == "day"
    assert [p["key"] for p in review["precedence"]][:3] == ["safety", "manual_override", "sleep"]
    assert (await r.api.error("auto/enable", window_id=s, confirm=False))["code"] == "confirm_required"
    await r.api.ok("auto/enable", window_id=s, confirm=True)
    await hass.async_block_till_done()
    assert sorted(covers.services()) == [("set_cover_position", STOVA_SHEER, 100),
                                         ("set_cover_position", STOVA_BLACKOUT, 100)]
    assert r.mgr.config.windows[k].mode == "off" and r.mgr.config.windows[s].mode == "automatic"
    log = r.auto(s)["log"]
    assert [x["action"] for x in log[:2]] == ["moved", "enabled"]
    assert r.mgr.last[s]["origin"] == "automatic" and r.mgr.last[s]["status"] == "succeeded"
    # Automatic -> Observing: nothing is sent any more.
    await r.api.ok("window/update", window_id=s, mode="observing")
    set_cover(hass, STOVA_SHEER, 20, context=Context())
    await r.walk(30)
    assert len(covers.calls) == 2


async def test_enable_is_blocked_by_a_competing_automation_until_resolved(hass, pilot, house, covers,
                                                                          admin_ws, freezer, faroe) -> None:
    assert await async_setup_component(hass, "script", {"script": {"stova_close": {"sequence": [
        {"action": "cover.close_cover", "target": {"entity_id": STOVA_BLACKOUT}}]}}})
    assert await async_setup_component(hass, "automation", {"automation": [
        {"id": "legacy", "alias": "cover_stova_sudur", "triggers": [{"trigger": "event", "event_type": "legacy_go"}],
         "actions": [{"action": "script.stova_close"}]},
        {"id": "later", "alias": "sol_stova", "initial_state": False,
         "triggers": [{"trigger": "state", "entity_id": "input_boolean.sol"}],
         "actions": [{"action": "cover.set_cover_position", "target": {"entity_id": STOVA_SHEER},
                      "data": {"position": 0}}]},
    ]})
    await hass.async_block_till_done()
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:10:00+00:00", lux=3000,
                        elevation=8)
    s = await stova(r.api, house)
    await r.settle()
    review = await r.api.ok("auto/preview", window_id=s)
    assert len(review["blockers"]) == 1 and "cover_stova_sudur" in review["blockers"][0]
    assert review["conflicts"][0]["via"] == "script"   # indirectly, through a script
    err = await r.api.error("auto/enable", window_id=s, confirm=True)
    assert err["code"] == "blocked" and "cover_stova_sudur" in err["message"]
    assert r.mgr.config.windows[s].mode == "observing" and covers.calls == []
    # The user reviews it and allows it to coexist (or disables it); the automation itself is never touched.
    await r.api.ok("automation/allow", window_id=s, automation="automation.cover_stova_sudur", allowed=True)
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert len(covers.calls) == 2, (r.auto(s)["status"], r.auto(s)["decision"])
    # Another legacy executor is switched on later: automatic commands stop and say why.
    await hass.services.async_call("automation", "turn_on", {"entity_id": "automation.sol_stova"}, blocking=True)
    await r.walk(2)
    set_cover(hass, STOVA_SHEER, 30, context=Context())   # a manual move, then Resume
    await r.api.ok("resume", window_id=s)
    status = r.auto(s)["status"]
    assert status["action"] == "blocked" and status["code"] == "conflict" and "sol_stova" in status["text"]
    assert len(covers.calls) == 2
    assert hass.states.get("automation.cover_stova_sudur").state == "on"


# ================================================================ sleep and lamp
async def test_sleep_on_off_and_a_missing_sleep_source(hass, pilot, house, covers, admin_ws, freezer,
                                                       faroe) -> None:
    set_cover(hass, KAMAR, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-21T13:00:00+00:00", lux=3000,
                        elevation=6, sleep=False)
    k = await kamar(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    await hass.async_block_till_done()
    assert r.condition(k) == "day" and covers.calls == []         # already Fully open
    r.sleep(True)                                                 # a nap: sleep protection closes at once
    await r.walk(1)
    assert r.condition(k) == "sleep" and r.pos(KAMAR) == 0
    r.sleep(False)
    await r.walk(1)
    assert r.condition(k) == "day" and r.pos(KAMAR) == 100       # the morning already happened today
    # The sleep source disappears: no automatic opening (and no closing because of it).
    hass.states.async_remove(SLEEP)
    await r.walk(2)
    set_cover(hass, KAMAR, 0, context=Context(user_id="u"))
    await r.api.ok("resume", window_id=k)
    assert r.condition(k) == "waiting" and "sleep source input_boolean.sleep is unavailable" in \
        r.auto(k)["decision"]["text"]
    assert r.pos(KAMAR) == 0
    review = await r.api.ok("auto/preview", window_id=k)
    assert any("sleep source" in w for w in review["warnings"])
    # Sleep "keeps its position until Resume" when it ends.
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"sleep_end": "hold"})
    r.sleep(True)
    await r.walk(1)
    r.sleep(False)
    await r.walk(5)
    assert r.condition(k) == "manual_override" and r.auto(k)["hold"]["source"] == "sleep_end" and r.pos(KAMAR) == 0
    await r.api.ok("resume", window_id=k)
    await hass.async_block_till_done()
    assert r.condition(k) == "day" and r.pos(KAMAR) == 100


async def test_window_lamp_override_release_delay_and_bypass(hass, pilot, house, covers, admin_ws, freezer,
                                                             faroe) -> None:
    set_cover(hass, KAMAR, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T14:00:00+00:00", lux=3000,
                        elevation=4, sleep=False)
    r.lamp(False)
    k = await kamar(r.api, house)
    await r.api.ok("auto/settings", scope="window", window_id=k,
                   values={"lamp_enabled": True, "lamp_entity": LAMP, "lamp_release": 120})
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    assert r.condition(k) == "day"
    r.lamp(True)
    await r.walk(1)
    assert r.condition(k) == "lamp" and r.pos(KAMAR) == 50       # Lamp On preset: Half open
    assert r.auto(k)["decision"]["text"].startswith("Lamp override active. Sleep, Night, Evening privacy are set to")
    r.lamp(False)
    await r.walk(1)
    assert r.condition(k) == "lamp" and r.pos(KAMAR) == 50       # release delay
    await r.walk(2)
    assert r.condition(k) == "day" and r.pos(KAMAR) == 100       # the CURRENT condition
    # Night bypasses the lamp (default): the lamp at night keeps the window closed.
    r.lux(0)
    r.sun(-30)
    await r.until("22:31", step=300)
    assert r.condition(k) == "night" and r.pos(KAMAR) == 0
    r.lamp(True)
    await r.walk(1)
    assert r.condition(k) == "night" and r.pos(KAMAR) == 0
    assert "set to win over the lamp" in r.auto(k)["decision"]["text"]
    # Without the night bypass the lamp wins at night; after it switches off the window returns to Night,
    # not to the day position from before the lamp.
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"lamp_bypass": ["sleep"]})
    await r.walk(1)
    assert r.condition(k) == "lamp" and r.pos(KAMAR) == 50
    r.sleep(True)                                                 # sleep still wins over the lamp
    await r.walk(1)
    assert r.condition(k) == "sleep" and r.pos(KAMAR) == 0
    r.sleep(False)
    await r.walk(1)
    assert r.condition(k) == "lamp" and r.pos(KAMAR) == 50
    r.lamp(False)
    await r.walk(3)
    assert r.condition(k) == "night" and r.pos(KAMAR) == 0


# ============================================================== manual changes
async def test_manual_hold_is_kept_released_by_the_next_phase_and_resumed(hass, pilot, house, covers,
                                                                         admin_ws, freezer, faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6)
    s = await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    await r.walk(2)   # (reports within 90 s of our own command count as ours)
    # The NSPanel / a wall button closes the sheer (the device's own context, no user).
    set_cover(hass, STOVA_SHEER, 30, context=Context())
    await r.walk(1)
    assert r.condition(s) == "manual_override"
    hold = r.auto(s)["hold"]
    assert hold["source"] == "manual" and hold["policy"] == "next_phase" and hold["phase"] == "day"
    r.lux(2800)
    await r.walk(60, step=120)
    assert r.pos(STOVA_SHEER) == 30 and len(covers.calls) == 2   # never undone
    assert "Automatic would use Normal daytime" in r.auto(s)["decision"]["text"]
    # Dusk = the next automatic phase: the hold ends and Evening privacy applies.
    r.lux(30)
    r.sun(-3)
    await r.walk(11)
    assert r.condition(s) == "evening_privacy" and r.auto(s)["hold"] is None
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    assert any(x["action"] == "released" for x in r.auto(s)["log"])
    # "Until I resume": only Resume Automatic Curtains releases it; the next manual move makes a new hold.
    await r.api.ok("auto/settings", scope="room", area_id=house.stova, values={"manual_policy": "until_released"})
    await r.walk(2)
    set_cover(hass, STOVA_BLACKOUT, 100, context=Context(user_id="person"))
    await r.walk(1)
    first = r.auto(s)["hold"]
    assert first["policy"] == "until_released" and first["by"] == "person"
    r.sun(-40)
    await r.until("22:31", step=300)
    assert r.condition(s) == "manual_override" and r.pos(STOVA_BLACKOUT) == 100
    await r.api.ok("resume", window_id=s)
    await hass.async_block_till_done()
    assert r.condition(s) == "night" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0)
    await r.walk(2)
    set_cover(hass, STOVA_SHEER, 70, context=Context())
    await r.walk(1)
    assert r.auto(s)["hold"] is not None and r.auto(s)["hold"]["created"] != first["created"]


async def test_manual_hold_duration_vacancy_and_bypass(hass, pilot, house, covers, admin_ws, freezer,
                                                       faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6, sleep=False)
    k = await kamar(r.api, house)
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"manual_policy": "duration",
                                                                          "manual_minutes": 30})
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    await hass.async_block_till_done()
    assert r.pos(KAMAR) == 100
    await r.walk(2)
    set_cover(hass, KAMAR, 20, context=Context())
    await r.walk(29)
    assert r.pos(KAMAR) == 20
    await r.walk(2)
    assert r.pos(KAMAR) == 100 and r.auto(k)["hold"] is None
    # Until the room has been empty (occupancy of the window's room).
    room: dict[str, Any] = {"state": OccupancyState.CONFIRMED_OCCUPANCY, "since": dt_util.utcnow()}

    class Occupancy:
        def result(self, area_id):
            return type("R", (), {"state": room["state"], "state_since": room["since"]})()

        def async_add_listener(self, area_id, update):
            room["update"] = update
            return lambda: None

    r.mgr.auto.occupancy = Occupancy()
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"manual_policy": "vacant",
                                                                          "vacant_minutes": 15})
    await r.walk(2)
    set_cover(hass, KAMAR, 40, context=Context())
    await r.walk(30)
    assert r.pos(KAMAR) == 40                                     # occupied: kept
    room.update(state=OccupancyState.VACANT, since=dt_util.utcnow())
    room["update"]()
    await r.walk(14)
    assert r.pos(KAMAR) == 40
    await r.walk(2)
    assert r.pos(KAMAR) == 100
    # Sleep set to end manual holds: only when sleep starts after the manual move.
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"manual_policy": "until_released",
                                                                          "manual_bypass": ["sleep"]})
    await r.walk(2)
    set_cover(hass, KAMAR, 60, context=Context())
    await r.walk(1)
    r.sleep(True)
    await r.walk(1)
    assert r.condition(k) == "sleep" and r.pos(KAMAR) == 0
    await r.walk(2)
    set_cover(hass, KAMAR, 10, context=Context(user_id="u"))     # a move during sleep is respected
    await r.walk(5)
    assert r.condition(k) == "manual_override" and r.pos(KAMAR) == 10


async def test_a_confirmed_manual_preset_is_kept_in_automatic(hass, pilot, house, covers, admin_ws, freezer,
                                                              faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6)
    s = await stova(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    plan = await r.api.ok("preview", window_id=s, preset_id="p_full_shade")
    await r.api.ok("apply", window_id=s, preset_id="p_full_shade", confirm=True, expected=plan["positions"])
    await r.walk(30)
    assert r.condition(s) == "manual_override" and r.auto(s)["hold"]["source"] == "preset"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100) and len(covers.calls) == 3


# ======================================================================= daylight
async def test_stale_or_unavailable_light_never_opens_but_dusk_still_closes(hass, pilot, house, covers,
                                                                           admin_ws, freezer, faroe) -> None:
    set_cover(hass, STOVA_SHEER, 0)
    set_cover(hass, STOVA_BLACKOUT, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T09:00:00+00:00", lux=2000,
                        elevation=3)
    r.reporting = False   # the sensor reported once and then stopped, before daylight was confirmed
    s = await stova(r.api, house)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"lux_stale": 5})
    await r.settle()
    await enable(r.api, s)
    await r.walk(40)
    assert r.condition(s) == "waiting" and covers.calls == []
    assert "has not reported for too long" in r.auto(s)["decision"]["text"]
    # Unavailable: still no opening.
    r.lux("unavailable")
    r.reporting = True
    await r.walk(15)
    assert r.condition(s) == "waiting" and covers.calls == [] and "unavailable" in r.auto(s)["decision"]["text"]
    # Reports again: daylight needs its stable period again before anything opens.
    r.lux(2100)
    await r.walk(9)
    assert covers.calls == []
    await r.walk(2)
    assert r.condition(s) == "morning" and len(covers.calls) == 2
    # Without a sensor and without the fallback nothing ever opens; the conservative sun fallback opens
    # once the sun is clearly up.
    await r.api.ok("auto/settings", scope="global", values={"lux_entity": None})
    await r.walk(2)
    set_cover(hass, STOVA_SHEER, 0, context=Context())
    await r.api.ok("resume", window_id=s)
    assert r.condition(s) == "waiting" and "no outdoor light sensor and no fallback" in r.auto(s)["decision"]["text"], \
        (r.auto(s)["decision"], r.auto(s)["values"]["lux_entity"], r.mgr.config.defaults)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"fallback": "sun"})
    r.sun(-5.0)
    await r.walk(1)
    r.sun(2.0)                                                    # (hysteresis: up from 3°, down below −4°)
    await r.walk(1)
    assert r.condition(s) == "waiting"
    r.sun(3.5)
    await r.walk(1)
    assert r.condition(s) == "morning" and r.pos(STOVA_SHEER) == 50


async def test_repeated_sensor_changes_do_not_flap(hass, pilot, house, covers, admin_ws, freezer,
                                                   faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T08:30:00+00:00", lux=0,
                        elevation=-1)
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    s = await stova(r.api, house, solar_protection=True)
    await r.settle()
    await enable(r.api, s)
    for minute in range(40):                                      # around the daylight threshold
        r.lux(140 if minute % 2 else 160)
        await r.walk(1)
    assert covers.calls == []
    r.lux(400)
    await r.walk(11)
    assert r.condition(s) == "morning" and len(covers.calls) == 2
    for minute in range(60):                                      # between dusk and daylight: still daylight
        r.lux(60 if minute % 2 else 140)
        await r.walk(1)
    assert r.condition(s) == "morning" and len(covers.calls) == 2
    # Around the strong-sunshine threshold with the sun in front of the window: no shading, no flapping.
    r.sun(10, 180)
    for minute in range(40):
        r.lux(11900 if minute % 2 else 12100)
        await r.walk(1)
    assert r.condition(s) == "morning" and len(covers.calls) == 2


# ================================================================== sun protection
async def test_sun_protection_enters_and_leaves(hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:05:00+00:00", lux=3000,
                        elevation=9, azimuth=170)
    s = await stova(r.api, house, solar_protection=True)
    await r.api.ok("auto/settings", scope="window", window_id=s, values={"min_interval": 0})
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    assert r.condition(s) == "day" and covers.calls == []
    # The sun is in front of the window, but the light is ordinary: geometry alone is not sunshine.
    await r.walk(20)
    assert r.condition(s) == "day"
    r.lux(25000)
    await r.walk(9)
    assert r.condition(s) == "day"
    await r.walk(2)
    assert r.condition(s) == "sun" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 100)
    # A short cloud does not release it (needs 20 min below 8000 lx).
    r.lux(5000)
    await r.walk(10)
    r.lux(20000)
    await r.walk(15)
    assert r.condition(s) == "sun" and len(covers.calls) == 1
    # The sun leaves the window's sector (± 70° and a 3° margin): released.
    r.sun(12, 180 + 72)
    await r.walk(1)
    assert r.condition(s) == "sun"
    r.sun(12, 180 + 74)
    await r.walk(1)
    assert r.condition(s) == "day" and r.pos(STOVA_SHEER) == 100
    # Back in front with strong light; then weaker for 20 minutes: released by light.
    r.sun(15, 185)
    await r.walk(11)
    assert r.condition(s) == "sun"
    r.lux(3000)
    await r.walk(19)
    assert r.condition(s) == "sun"
    await r.walk(2)
    assert r.condition(s) == "day"


# ======================================================== movement safety
async def test_device_failure_delayed_reports_and_unavailable_covers(hass, pilot, house, covers, admin_ws,
                                                                    freezer, faroe) -> None:
    set_cover(hass, KAMAR, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6, sleep=False)
    k = await kamar(r.api, house)
    covers.fail = {KAMAR}
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    await hass.async_block_till_done()
    assert len(covers.calls) == 1 and r.mgr.last[k]["status"] == "failed"
    await r.walk(1.5, step=10)
    assert len(covers.calls) == 2, (r.auto(k)["status"], r.auto(k)["applied"])  # first retry after 60 s
    await r.walk(6)
    assert len(covers.calls) == 3                                 # second after 5 min
    await r.walk(60, step=300)
    assert len(covers.calls) == 3                                 # max_retries = 2: given up, reported
    status = r.auto(k)["status"]
    assert status["code"] == "gave_up" and "failed 3 times" in status["text"]
    # The device works again but reports late (40 s travel, intermediate positions).
    covers.fail = set()
    covers.travel = {KAMAR: 40}
    await r.api.ok("resume", window_id=k)
    await hass.async_block_till_done()
    assert len(covers.calls) == 4 and r.auto(k)["status"]["action"] in ("moved", "moving")
    await r.walk(0.5, step=5)
    assert r.auto(k)["status"]["action"] == "moving"
    set_cover(hass, KAMAR, 55, state="opening", context=Context())  # a late in-between report is ours
    await r.walk(1, step=5)
    assert r.mgr.last[k]["status"] == "succeeded" and r.auto(k)["hold"] is None and r.pos(KAMAR) == 100
    # Unavailable: nothing is sent; when it returns, the window catches up.
    set_cover(hass, KAMAR, None, state="unavailable")
    r.sleep(True)
    await r.walk(1)
    status = r.auto(k)["status"]
    assert status["code"] == "unavailable" and len(covers.calls) == 4
    covers.travel = {}
    set_cover(hass, KAMAR, 100)
    await r.walk(1)
    assert r.pos(KAMAR) == 0 and len(covers.calls) == 5


async def test_budget_and_minimum_interval(hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    set_cover(hass, KAMAR, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6, sleep=False)
    r.lamp(False)
    k = await kamar(r.api, house)
    await r.api.ok("auto/settings", scope="window", window_id=k, values={
        "lamp_enabled": True, "lamp_entity": LAMP, "lamp_release": 0, "max_moves": 3, "min_interval": 10,
        "manual_policy": "duration", "manual_minutes": 5})
    await r.settle()
    await r.walk(11)
    await enable(r.api, k)
    assert covers.calls == []                                     # already Fully open
    r.lamp(True)
    await r.walk(1)
    assert r.pos(KAMAR) == 50                                     # move 1: the lamp is not delayed
    r.lamp(False)
    await r.walk(1)
    assert r.pos(KAMAR) == 100                                    # move 2: leaving the lamp is not delayed
    await r.walk(2)
    set_cover(hass, KAMAR, 20, context=Context())                # a manual move, kept for 5 minutes
    await r.walk(5)
    assert r.pos(KAMAR) == 20 and r.auto(k)["status"]["code"] == "dwell"   # then Day waits for the interval
    assert "minimum interval" in r.auto(k)["status"]["text"]
    await r.walk(2)
    assert r.pos(KAMAR) == 100 and r.auto(k)["moves_today"] == 3  # move 3, 10 minutes after move 2
    r.lamp(True)
    await r.walk(1)
    assert r.pos(KAMAR) == 100 and r.auto(k)["status"]["code"] == "budget"
    r.sleep(True)                                                 # sleep is exempt from the budget
    await r.walk(1)
    assert r.pos(KAMAR) == 0 and r.auto(k)["moves_today"] == 4


async def test_restart_keeps_holds_waits_for_sensors_and_never_replays(hass, pilot, house, covers, admin_ws,
                                                                      freezer, faroe, service_calls) -> None:
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                           elevation=6, sleep=False)
    set_cover(hass, KAMAR, 0)
    s = await stova(r.api, house)
    k = await kamar(r.api, house)
    await r.settle()
    await r.walk(11)
    await enable(r.api, s)
    await hass.async_block_till_done()
    await r.walk(2)
    set_cover(hass, STOVA_SHEER, 30, context=Context())          # a manual hold on Stova
    covers.ignore = {KAMAR}                                      # accepted; the device reports later
    await enable(r.api, k)                                       # Kamar is moving when Home Assistant stops
    await hass.async_block_till_done()
    calls = len(covers.calls)
    assert r.mgr.running(k)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    mgr = entry.runtime_data.curtains
    r.mgr = mgr
    assert mgr.config.windows[s].mode == "automatic" and mgr.auto.runtimes[s].hold["source"] == "manual"
    assert mgr.last[k]["status"] == "interrupted"
    # The device reports the rest of the movement after the restart (Zigbee2MQTT keeps moving).
    await r.walk(0.5, step=10)
    set_cover(hass, KAMAR, 40, state="opening", context=Context())
    await r.walk(0.5, step=10)
    set_cover(hass, KAMAR, 100, context=Context())
    await r.walk(1)
    # Light history starts again: no daylight until it has been stable again, and nothing is sent.
    assert r.condition(k) == "waiting" and len(covers.calls) == calls
    assert r.auto(k)["hold"] is None   # the rest of the interrupted movement is not a manual change
    await r.walk(12)
    # The interrupted movement is not replayed: the cover is already there.
    assert r.condition(k) == "day" and r.auto(k)["status"]["action"] == "at_target", r.auto(k)["status"]
    assert r.pos(KAMAR) == 100 and len(covers.calls) == calls
    assert r.condition(s) == "manual_override" and r.pos(STOVA_SHEER) == 30
    assert all(c.data["domain"] == "cover" for c in service_calls)


async def test_automatic_curtains_leave_occupancy_lighting_and_forecasts_alone(hass, pilot, house, covers,
                                                                               admin_ws, freezer, faroe,
                                                                               service_calls) -> None:
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T08:00:00+00:00", lux=0,
                           elevation=-3, sleep=True)
    coordinator = entry.runtime_data
    lighting = coordinator.lighting.config
    forecast = coordinator.forecast.settings
    before = coordinator.occupancy.result(pilot.area)
    occupancy = (before.state, before.occupied)
    k, s = await kamar(r.api, house), await stova(r.api, house, solar_protection=True)
    await r.settle()
    await enable(r.api, k)
    await enable(r.api, s)
    r.lux(500)
    r.sleep(False)
    await r.walk(15)
    assert covers.calls
    assert coordinator.lighting.config == lighting
    after = coordinator.occupancy.result(pilot.area)
    assert (after.state, after.occupied) == occupancy
    assert coordinator.forecast.settings == forecast
    assert {c.data["domain"] for c in service_calls} == {"cover"}
    assert hass.states.get(pilot.light).state == "off"


async def test_settings_scopes_through_the_card_api(hass, pilot, house, covers, admin_ws, freezer,
                                                    faroe) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T12:00:00+00:00", lux=3000,
                        elevation=6)
    k = await kamar(r.api, house)
    await r.api.ok("auto/settings", scope="global", values={"daylight_lux": 200})
    await r.api.ok("auto/settings", scope="room", area_id=house.kamar, values={"daylight_lux": 300,
                                                                                 "sleep_entity": "binary_sensor.bed"})
    await r.api.ok("auto/settings", scope="window", window_id=k, values={"lamp_entity": LAMP})
    model = await r.api.ok("model")
    w = next(x for x in model["windows"] if x["window_id"] == k)
    assert w["auto"]["values"]["daylight_lux"] == 300 and w["auto"]["sources"]["daylight_lux"] == "room"
    assert w["auto"]["values"]["sleep_entity"] == "binary_sensor.bed"
    assert model["rules"]["global"] == {"daylight_lux": 200, "lux_entity": LUX}
    assert model["rules"]["rooms"][house.kamar]["daylight_lux"] == 300
    assert {g[0] for g in model["rules"]["groups"]} == {"sleep", "lamp", "daylight", "schedule", "sun", "manual",
                                                        "movement"}
    await r.api.ok("auto/settings", scope="room", area_id=house.kamar, values={"daylight_lux": None})
    assert r.auto(k)["sources"]["daylight_lux"] == "global"
    err = await r.api.error("auto/settings", scope="global", values={"dusk_lux": 500})
    assert err["code"] == "inconsistent"
    assert (await r.api.error("auto/settings", scope="global", values={"lamp_entity": LAMP}))["code"] == "wrong_scope"
    assert (await r.api.error("auto/settings", scope="room", area_id="nowhere", values={}))["code"] == "unknown_area"
    # Stored and reloaded with the curtain store (minor version 2).
    await r.mgr.async_flush()
    assert hass.data  # nothing else to check here: the restart test covers persistence
    assert covers.calls == []
