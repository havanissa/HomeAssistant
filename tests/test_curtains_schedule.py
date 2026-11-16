"""Phase 4.6.4 (0.6.5): the Global Curtain Schedule.

One schedule evaluation (``curtains/schedule.py``) makes Morning, Daytime, Evening and Night eligible; every
window still decides on its own (sleep, daylight, sun, lamp, manual holds) and resolves its preset through
Global Modes. Covered: every activation rule, Morning in daylight / at 0 lx / asleep / after sleep, Daytime
rules, Evening at a time, sunset and measured dusk with first / last and passing clouds, Night before normal and
free days, Friday / Saturday late nights, Sunday to Monday, midnight, custom days, per-day times, time zones and
daylight saving, global / room / window inheritance, unavailable sensors, sun and lamp precedence, manual
holds, windows without assignments, layered presets, a restart in the night, the reviewed save with its
policies, the 0.6.4 migration, 24-hour times. Simulated Zigbee2MQTT covers only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import re
from typing import Any

import pytest

from homeassistant.core import Context
from homeassistant.util import dt as dt_util

from custom_components.smart_occupancy.curtains import logic, rules, schedule
from custom_components.smart_occupancy.curtains.config import CurtainConfig, CurtainConfigError, Window

from .test_curtains import (  # noqa: F401  (fixtures)
    KAMAR,
    KOKUR_BLACKOUT,
    KOKUR_SHEER,
    STOVA_BLACKOUT,
    STOVA_SHEER,
    covers,
    house,
    set_cover,
)
from .test_curtains_auto import (  # noqa: F401  (fixtures)
    FAROE,
    LAYERED_MODES,
    LUX,
    admin_ws,
    begin,
    enable,
    faroe,
    global_modes,
    kamar,
    stova,
)

TZ = dt_util.get_time_zone(FAROE)
# 2026-11-19 is a Thursday.
THU, FRI, SAT, SUN, MON = (date(2026, 11, d) for d in (19, 20, 21, 22, 23))
ACCEPTANCE = {"night_end_workday": "07:00", "night_end_free": "08:00", "morning_mode": "until_time",
              "day_from_workday": "12:00", "day_from_free": "12:00", "evening_rule": "first",
              "evening_time_workday": "18:00", "evening_time_free": "18:00", "evening_event": "dusk",
              "night_start_workday": "21:30", "night_start_free": "23:55"}


def values(**changes: Any) -> dict[str, Any]:
    return {**rules.defaults(), **changes}


def at(day: date, hhmm: str, tz=TZ) -> datetime:
    return schedule.at(day, hhmm, tz)


def ev(when: datetime, **changes: Any) -> logic.Evidence:
    e = logic.Evidence(now=when, lux=600.0, lux_status="ok", lux_entity=LUX, daylight=True, elevation=6.0,
                       exposure_text="x")
    for key, value in changes.items():
        setattr(e, key, value)
    return e


DARK = {"lux": 0.0, "daylight": False, "dusk_measured": True, "elevation": -12.0}
CONDS = {"morning": "p_half_shade", "day": "p_open", "sun": "p_full_shade", "evening_privacy": "p_evening_privacy",
         "night": "p_blackout", "sleep": "p_blackout", "lamp": "p_half_shade"}


def decide(e: logic.Evidence, v: dict[str, Any], *, sleep=False, privacy=True, solar=False, conds=None, hold=None,
           morning_since=None, evening_since=None) -> logic.Decision:
    return logic.decide(logic.Flags(sleep, privacy, solar, CONDS if conds is None else conds), v, e, hold=hold, active_since={},
                        morning_since=morning_since, evening_since=evening_since)


# ============================================================================ Night
def test_night_normal_late_and_custom_evenings() -> None:
    v = values(**ACCEPTANCE)   # free days Sat, Sun; late evenings = evenings before a free day (Fri, Sat)

    def night(day: date, hhmm: str, vv=v) -> bool:
        return schedule.night_now(at(day, hhmm), vv)[0]

    assert not night(THU, "21:29") and night(THU, "21:31")       # Thursday: normal evening 21:30
    assert night(FRI, "06:59") and not night(FRI, "07:01")       # Friday morning (normal day) 07:00
    assert not night(FRI, "23:50") and night(FRI, "23:56")       # Friday evening: late, 23:55
    assert night(SAT, "07:59") and not night(SAT, "08:01")       # Saturday (free) Morning 08:00
    assert not night(SAT, "23:50") and night(SAT, "23:56")       # Saturday evening: late
    assert night(SUN, "07:59") and not night(SUN, "08:01")
    assert not night(SUN, "21:29") and night(SUN, "21:31")       # Sunday evening before Monday: normal
    assert night(MON, "06:59") and not night(MON, "07:01")
    start, end = schedule.night_bounds(FRI, v, TZ)
    assert (start, end) == (at(FRI, "23:55"), at(SAT, "08:00"))
    assert schedule.night_start(FRI, v)[1] == "the late evening schedule"
    assert schedule.night_start(THU, v)[1] == "the normal evening schedule"
    # Chosen late evenings instead of "before a free day": only Friday is late, Saturday is normal.
    custom = {**v, "late_evenings": "custom", "late_days": (4,)}
    assert night(FRI, "23:56", custom) and not night(FRI, "23:00", custom)
    assert night(SAT, "21:31", custom)
    # Custom free days: a Friday off (free Fri, Sat, Sun) makes Thursday evening late and Friday morning later.
    off = {**v, "free_days": (4, 5, 6)}
    assert not night(THU, "21:31", off) and night(THU, "23:56", off) and night(FRI, "07:30", off)


def test_night_across_midnight_and_per_day_times() -> None:
    v = values(**ACCEPTANCE, day_overrides=rules.validate("day_overrides", {"4": {"night": "00:30"},
                                                                            "5": {"morning": "09:15"}}, "global"))
    assert schedule.night_start(FRI, v) == ("00:30", "the Friday schedule")
    start, end = schedule.night_bounds(FRI, v, TZ)
    assert (start, end) == (at(SAT, "00:30"), at(SAT, "09:15"))    # after midnight, still Friday's evening
    assert not schedule.night_now(at(SAT, "00:20"), v)[0] and schedule.night_now(at(SAT, "00:40"), v)[0]
    assert schedule.night_now(at(SAT, "09:10"), v)[0] and not schedule.night_now(at(SAT, "09:20"), v)[0]
    # A Thursday night that starts after midnight and the Friday Morning time.
    late = values(night_start_workday="00:30")
    assert schedule.night_now(at(FRI, "00:40"), late)[0] and not schedule.night_now(at(THU, "23:00"), late)[0]
    # Per-day times are validated as 24-hour HH:mm.
    for bad in ({"7": {"night": "22:00"}}, {"1": {"lunch": "12:00"}}, {"1": {"night": "9:5x"}},
                {"1": {"night": "10:30 PM"}}):
        with pytest.raises(CurtainConfigError):
            rules.validate("day_overrides", bad, "global")
    with pytest.raises(CurtainConfigError):
        rules.validate("day_overrides", {"1": {"night": "22:00"}}, "window")
    assert rules.dump({"d": rules.validate("day_overrides", {"4": {"night": "23:55"}}, "global")}) == {
        "d": {"4": {"night": "23:55"}}}


@pytest.mark.parametrize("zone", ["Europe/Copenhagen", "America/New_York", FAROE])
def test_schedule_across_daylight_saving(zone: str) -> None:
    tz = dt_util.get_time_zone(zone)
    v = values(**ACCEPTANCE)
    for day in (date(2026, 3, 28), date(2026, 3, 7), date(2026, 10, 24), date(2026, 10, 31)):
        start, end = schedule.night_bounds(day, v, tz)
        # Wall-clock times stay what was configured, whatever the UTC offset does in between.
        assert start.astimezone(tz).strftime("%H:%M") == "23:55" and end.astimezone(tz).strftime("%H:%M") == "08:00"
        assert schedule.night_now(end - timedelta(minutes=1), v)[0] and not schedule.night_now(end, v)[0]
        evening = schedule.at(day, schedule.evening_time(day, v), tz)
        assert evening.astimezone(tz).strftime("%H:%M") == "18:00"


# ===================================================================== Evening rules
def test_evening_activation_rules_and_latch() -> None:
    v = values(**ACCEPTANCE)
    dusk_1635 = ev(at(THU, "16:35"), lux=12.0, daylight=False, dusk_measured=True, elevation=-2.5)
    light_1635 = ev(at(THU, "16:35"), elevation=-2.5)
    light_1805 = ev(at(THU, "18:05"), elevation=-2.5)
    # first: dusk before 18:00 starts it; without dusk the time does
    s = schedule.evening(dusk_1635.now, v, dusk_1635, None)
    assert s.active and s.start["reason"] == "dusk was confirmed (12 lx) before 18:00"
    assert s.text == "Evening started at 16:35 because dusk was confirmed (12 lx) before 18:00"
    assert not schedule.evening(light_1635.now, v, light_1635, None).active
    assert schedule.evening(light_1635.now, v, light_1635, None).deadline == at(THU, "18:00")
    s = schedule.evening(light_1805.now, v, light_1805, None)
    assert s.active and s.start["reason"] == "it is 18:00, before dusk"
    # last: both needed
    last = {**v, "evening_rule": "last"}
    assert not schedule.evening(dusk_1635.now, last, dusk_1635, None).active
    dusk_1805 = ev(at(THU, "18:05"), lux=8.0, daylight=False, dusk_measured=True)
    assert schedule.evening(dusk_1805.now, last, dusk_1805, None).start["reason"] == \
        "dusk was confirmed (8 lx) after 18:00"
    waiting = schedule.evening(light_1805.now, last, light_1805, None)
    assert not waiting.active and waiting.waiting == "Evening waits for dusk (after 18:00)"
    # time / event (sunset)
    assert schedule.evening(light_1805.now, {**v, "evening_rule": "time"}, light_1805, None).active
    sunset = {**v, "evening_rule": "event", "evening_event": "sunset"}
    assert schedule.evening(light_1635.now, sunset, ev(light_1635.now, elevation=-1.0), None).active
    assert not schedule.evening(light_1635.now, sunset, ev(light_1635.now, elevation=0.5), None).active
    # Latched: a passing cloud ended (it is light again) - Evening stays until Night.
    latch = {"at": at(THU, "16:35"), "reason": "dusk was confirmed (12 lx) before 18:00"}
    bright = ev(at(THU, "17:10"), lux=900.0, elevation=-3.0)
    s = schedule.evening(bright.now, v, bright, latch)
    assert s.active and s.start is None and s.text.startswith("Evening since 16:35")
    # Never before 12:00; the latch is per day.
    assert not schedule.evening(at(FRI, "11:00"), {**v, "evening_rule": "time", "evening_time_workday": "12:00"},
                                ev(at(FRI, "11:00")), latch).active
    assert not schedule.evening(at(FRI, "16:00"), v, ev(at(FRI, "16:00")), latch).active


def test_evening_without_light_or_sun_data() -> None:
    v = values(**ACCEPTANCE)
    # The light sensor is unavailable: the sun below the dark elevation is dusk (as in 0.6.2-0.6.4).
    no_lux = ev(at(THU, "16:40"), lux=None, lux_status="unavailable", daylight=None, elevation=-4.5)
    s = schedule.evening(no_lux.now, v, no_lux, None)
    assert s.active and "the sun at -4.5°" in s.start["reason"]
    nothing = ev(at(THU, "16:40"), lux=None, lux_status="unavailable", daylight=None, elevation=None)
    event = {**v, "evening_rule": "event"}
    s = schedule.evening(nothing.now, event, nothing, None)
    assert not s.active and s.waiting == "Evening waits for dusk: neither outdoor light nor the sun position is " \
        "available"
    late = ev(at(THU, "18:10"), lux=None, lux_status="unavailable", daylight=None, elevation=None)
    assert schedule.evening(late.now, v, late, None).start["reason"].startswith("it is 18:00 (dusk cannot be")
    assert "the configured time is the fallback" in schedule.evening(late.now, {**v, "evening_rule": "last"}, late,
                                                                      None).start["reason"]


def test_legacy_evening_is_kept_until_adopted() -> None:
    v = values()
    assert v["evening_rule"] == "legacy"
    dark = ev(at(THU, "15:30"), lux=20.0, daylight=False, dusk_measured=True, elevation=-1.0)
    d = decide(dark, v)
    assert d.condition == "evening_privacy" and d.text == "Dark outside in the evening (20 lx)" and \
        d.evening_start is None
    # Not latched: lighter again = no longer Evening (0.6.4).
    assert decide(ev(at(THU, "15:50"), elevation=-1.0), v).condition == "day"


# ===================================================================== Morning and Daytime
def test_morning_eligible_but_dark_asleep_or_after_sleep() -> None:
    v = values(**ACCEPTANCE)
    # 07:00 at 0 lx: Morning is eligible, nothing opens.
    d = decide(ev(at(THU, "07:00"), **{**DARK, "dusk_measured": False}), v)
    assert d.condition == "waiting" and d.preset_id is None
    assert d.note == "Morning is eligible, but daylight has not been confirmed. Holding current positions."
    # Sleep on in a sleep-protected room: Sleep, never Morning.
    asleep = decide(ev(at(THU, "08:30"), sleep="on"), v, sleep=True)
    assert asleep.condition == "sleep" and asleep.note.startswith("Morning is eligible, but Sleep Mode is on")
    # Daylight confirmed and sleep off: Morning applies its preset.
    m = decide(ev(at(THU, "08:25"), sleep="off"), v, sleep=True)
    assert (m.condition, m.preset_id, m.morning_start) == ("morning", "p_half_shade", at(THU, "08:25"))
    # Before the Morning time (Night off): no Morning even in daylight.
    off = {**v, "night_enabled": False}
    early = decide(ev(at(THU, "06:30")), off)
    assert early.condition == "day" and early.morning_start is None
    # Sleep released while it is still dark, with "opening after sleep needs daylight" off: Morning at once.
    woke = decide(ev(at(THU, "07:20"), **{**DARK, "dusk_measured": False}, sleep="off", sleep_ended=at(THU, "07:15")),
                  {**v, "sleep_require_daylight": False}, sleep=True)
    assert woke.condition == "morning"
    # Morning off: Daytime directly.
    assert decide(ev(at(THU, "08:25")), {**v, "morning_enabled": False}).condition == "day"


def test_daytime_rules_and_no_late_morning() -> None:
    since = at(THU, "08:25")
    base = values(**ACCEPTANCE)
    # At a time: Morning until 12:00.
    assert decide(ev(at(THU, "11:59")), base, morning_since=since).condition == "morning"
    assert decide(ev(at(THU, "12:00")), base, morning_since=since).condition == "day"
    # A Morning that could not begin before 12:00 (dark, asleep) is skipped, never started late.
    late = decide(ev(at(THU, "12:30")), base)
    assert late.condition == "day"
    # Duration: 120 min after this window's Morning began.
    dur = {**base, "morning_mode": "duration", "morning_minutes": 90}
    assert decide(ev(at(THU, "09:54")), dur, morning_since=since).condition == "morning"
    assert decide(ev(at(THU, "09:55")), dur, morning_since=since).condition == "day"
    # Not before 10:00, and not before Morning has lasted 90 min.
    nb = {**base, "morning_mode": "not_before", "day_from_workday": "10:00", "morning_minutes": 90}
    assert decide(ev(at(THU, "10:30")), nb, morning_since=at(THU, "09:20")).condition == "morning"
    assert decide(ev(at(THU, "10:50")), nb, morning_since=at(THU, "09:20")).condition == "day"
    assert decide(ev(at(THU, "10:00")), nb, morning_since=at(THU, "08:00")).condition == "day"
    # A finished Morning is not started again the same day.
    assert decide(ev(at(THU, "11:00")), dur, morning_since=at(THU, "07:30")).condition == "day"


def test_sun_and_lamp_still_win_over_the_schedule() -> None:
    v = values(**ACCEPTANCE)
    sunny = ev(at(THU, "12:30"), lux=25000.0, exposed=True, strong=True, elevation=9.0)
    assert decide(sunny, v, solar=True).condition == "sun"           # Sun Protection over Daytime
    assert decide(sunny, v, solar=False).condition == "day"          # the window next to it: Daytime
    assert decide(ev(at(THU, "12:30"), lamp="on"), v).condition == "lamp"
    evening = ev(at(THU, "18:30"), lamp="on", elevation=-8.0)
    d = decide(evening, v)
    assert d.condition == "evening_privacy" and d.lamp_bypassed       # Evening privacy wins over the lamp (default)
    d = decide(evening, {**v, "lamp_bypass": ("sleep", "night")})
    assert d.condition == "lamp" and d.base == "evening_privacy"
    # After a hold ends, the window returns to the current phase, not the one when the hold began.
    hold = {"source": "manual", "created": at(THU, "11:00"), "policy": "next_phase", "phase": "morning"}
    d = decide(ev(at(THU, "12:10")), v, hold=hold, morning_since=at(THU, "08:25"))
    assert d.hold_release and d.base == "day"
    held = decide(ev(at(THU, "11:30")), v, hold=hold, morning_since=at(THU, "08:25"))
    assert held.condition == "manual_override"


def test_window_without_assignment_keeps_its_position_in_every_phase() -> None:
    v = values(**ACCEPTANCE)
    for when, kwargs in ((at(THU, "08:30"), {}), (at(THU, "13:00"), {}),
                         (at(THU, "18:30"), {"elevation": -8.0}), (at(THU, "22:00"), DARK)):
        d = decide(ev(when, **kwargs), v, conds={})
        assert d.preset_id is None and "the covers are kept as they are" in d.text, (when, d.text)


# ========================================================================= household
def test_household_phase_reasons_and_next_transitions() -> None:
    v = {**values(**ACCEPTANCE), "lux_entity": LUX}
    state: dict[str, Any] = {}

    def house(day: date, hhmm: str, **kw: Any) -> dict[str, Any]:
        e = schedule.HouseEvidence(now=at(day, hhmm), elevation=kw.pop("elevation", 5.0), lux=kw.pop("lux", 600.0),
                                   lux_status="ok", daylight=kw.pop("daylight", True), **kw)
        return schedule.household(v, e, state)

    r = house(THU, "06:00", daylight=False, lux=0.0, elevation=-14.0)
    assert r["phase"] == "night" and r["reason"] == "Night started according to the normal evening schedule"
    r = house(THU, "07:00", daylight=False, lux=0.0, elevation=-10.0)
    assert (r["phase"], r["waiting"]) == ("morning", "daylight has not been confirmed: holding current positions")
    r = house(THU, "08:25")
    assert r["phase"] == "morning" and r["reason"] == "Morning began at 08:25: daylight confirmed (earliest 07:00)"
    assert house(THU, "12:00")["phase"] == "day"
    r = house(THU, "16:35", daylight=False, dusk_measured=True, lux=12.0, elevation=-2.0)
    assert r["phase"] == "evening" and r["reason"] == "Evening started because dusk was confirmed (12 lx) before 18:00"
    assert house(THU, "17:00", lux=900.0, elevation=-3.0)["phase"] == "evening"     # a cloud passed: kept
    assert house(FRI, "23:56", daylight=False)["reason"] == "Night started according to the late evening schedule"
    plan = {**v, "day_overrides": rules.validate("day_overrides", {"4": {"night": "23:55"}}, "global"),
            "late_evenings": "custom", "late_days": ()}
    e = schedule.HouseEvidence(now=at(FRI, "23:56"), daylight=False)
    assert schedule.household(plan, e, {})["reason"] == "Night started according to the Friday schedule"
    nxt = schedule.upcoming(at(THU, "13:00"), v, {"dusk": at(THU, "16:30")})
    assert [(x["phase"], x["time"]) for x in nxt[:3]] == [("evening", "16:30"), ("night", "21:30"),
                                                           ("morning", "07:00")]
    assert nxt[0]["approx"] and nxt[0]["reason"] == "At 18:00 or dusk, whichever comes first"
    assert all(re.fullmatch(r"\d{2}:\d{2}", x["time"]) for x in nxt)
    bar = schedule.timeline(THU, v, TZ, {"dusk": at(THU, "16:30")})
    assert [(s["phase"], s["from"], s["to"]) for s in bar] == [
        ("night", 0, 420), ("morning", 420, 720), ("day", 720, 990), ("evening", 990, 1290), ("night", 1290, 1440)]


def test_inheritance_only_before_the_migration() -> None:
    """Room / window schedule values from 0.6.5 still count until the migration is applied (unchanged behaviour);
    a centralized configuration has one schedule for the whole house."""
    w = Window("w1", "Kamar", area_id="kamar", auto=rules.load({"night_start_workday": "20:45"}, "window",
                                                               legacy=True))
    other = Window("w2", "Stova", area_id="stova")
    cfg = CurtainConfig(windows={"w1": w, "w2": other}, centralized=False,
                        defaults=rules.load({"night_start_workday": "21:30", "evening_rule": "first"}, "global"),
                        rooms={"kamar": rules.load({"night_start_workday": "21:00", "evening_time_workday": "17:30"},
                                                   "room", legacy=True)})
    assert rules.load({"night_start_workday": "20:45"}, "window") == {}       # new data: refused
    central = CurtainConfig(windows=cfg.windows, defaults=cfg.defaults, rooms=cfg.rooms)
    assert central.rules_for(w)[0]["night_start_workday"] == "21:30"          # one schedule for every window
    assert central.rules_for(w)[1]["evening_time_workday"] == "default"
    v1, s1 = cfg.rules_for(w)
    v2, s2 = cfg.rules_for(other)
    assert (v1["night_start_workday"], s1["night_start_workday"]) == ("20:45", "window")
    assert (v1["evening_time_workday"], s1["evening_time_workday"]) == ("17:30", "room")
    assert (v1["evening_rule"], s1["evening_rule"]) == ("first", "global")
    assert (v2["night_start_workday"], s2["night_start_workday"]) == ("21:30", "global")
    assert s2["evening_time_workday"] == "default"


# ======================================================================= the house, simulated
async def test_acceptance_day_with_sleep_sun_dusk_clouds_and_night(hass, pilot, house, covers, admin_ws, freezer,
                                                                   faroe) -> None:
    """The user's acceptance test, Thursday 19 Nov in the Faroes: Morning 07:00 with daylight, Daytime 12:00,
    Evening 18:00 or dusk (first), Night 21:30 / 23:55. The sleeping room does not open at 07:00; the south
    window with sun protection shades while the others use Daytime; dusk before 18:00 starts Evening, a cloud
    passing does not end it; Night at 21:30."""
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    set_cover(hass, KOKUR_SHEER, 100)
    set_cover(hass, KOKUR_BLACKOUT, 0)
    set_cover(hass, KAMAR, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T06:30:00+00:00", lux=0,
                        elevation=-12.0, sleep=True)
    api = r.api
    saved = await api.ok("schedule/change", scope="global", values=ACCEPTANCE)
    assert saved["saved"] and "Evening begins (all windows): default (When it is dark after 12:00 (0.6.4 behaviour, " \
        "not kept)) → At a time or at sunset / dusk, whichever comes first" in saved["changes"]
    k = await kamar(api, house)
    s = await stova(api, house)
    kokur = (await api.ok("window/create", name="Køkur Suður", area_id=house.kokur, direction="S",
                          solar_protection=True, layers=[{"entity_id": KOKUR_SHEER, "kind": "sheer"},
                                                         {"entity_id": KOKUR_BLACKOUT, "kind": "blackout"}]))
    kokur = kokur["window_id"]
    await global_modes(api, kokur, LAYERED_MODES)
    await r.settle()
    for wid in (k, s, kokur):
        await enable(api, wid)
    assert covers.calls == [] and r.condition(s) == "night"
    # 07:00: Morning is eligible; 0 lx and sleep on: nothing opens.
    r.sun(-10.0, 115)
    await r.until("07:05")
    assert covers.calls == []
    assert r.condition(k) == "sleep" and r.condition(s) == "waiting"
    assert r.auto(s)["log"][0]["summary"] == ("Stova Suður: Morning is eligible, but daylight has not been "
                                              "confirmed. Holding current positions.")
    model = await api.ok("model")
    h = model["schedule"]["house"]
    assert h["phase"] == "morning" and h["waiting"] == "daylight has not been confirmed: holding current positions"
    assert h["next"][0]["word"] == "Daytime" and h["next"][0]["time"] == "12:00"
    # 08:20 daylight: Morning for the awake rooms (Half shade); the sleeping room stays closed.
    await r.until("08:20")
    r.sun(-1.0, 130)
    r.lux(400)
    await r.walk(11)
    assert r.condition(s) == "morning" and r.condition(kokur) == "morning" and r.condition(k) == "sleep"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KAMAR)) == (50, 100, 0)
    # Sleep ends at 09:00: that room's Morning starts then (its own Morning; the house phase is a reference).
    await r.until("09:00")
    r.sleep(False)
    await r.walk(2)
    assert r.condition(k) == "morning" and r.pos(KAMAR) == 50
    # 12:00 Daytime; strong sun from the south: Køkur (sun protection) shades, Stova uses Daytime.
    r.lux(25000)
    r.sun(9.0, 178)
    await r.until("12:15", step=120)
    assert r.condition(s) == "day" and r.condition(kokur) == "sun" and r.condition(k) == "day"
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 100)
    assert (r.pos(KOKUR_SHEER), r.pos(KOKUR_BLACKOUT)) == (0, 100)
    # 16:25: dusk measured (10 lx for 10 minutes, the sun still above the dark elevation) before 18:00.
    r.lux(3000)
    r.sun(2.0, 220)
    await r.until("16:25", step=300)
    r.lux(10)
    r.sun(-2.0, 228)
    await r.walk(11)
    assert r.condition(s) == "evening_privacy" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    moved = next(x for x in r.auto(s)["log"] if x["action"] == "moved")
    assert moved["summary"].startswith("Evening started because dusk was confirmed (10 lx) before 18:00. Evening. "
                                       "Stova Suður → Evening privacy (Global Mode).")
    assert r.condition(k) == "waiting"              # no evening privacy for Kamar: kept as it is
    # A cloud passes / it gets lighter again: Evening stays.
    calls = len(covers.calls)
    r.lux(900)
    await r.walk(25)
    assert r.condition(s) == "evening_privacy" and len(covers.calls) == calls
    # 21:30 Thursday (Friday is a normal day): Night for every window.
    r.lux(0)
    r.sun(-30.0, 300)
    await r.until("21:20", step=300)
    await r.until("21:31")
    assert {r.condition(x) for x in (k, s, kokur)} == {"night"}
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT), r.pos(KAMAR)) == (100, 0, 0)
    model = await api.ok("model")
    log = model["schedule"]["house"]["log"]
    assert log[0]["text"] == "Night started according to the normal evening schedule."
    assert any(x["text"].startswith("Evening started because dusk was confirmed") for x in log)
    assert all(re.fullmatch(r"\d{2}:\d{2}", x["time"]) for x in model["schedule"]["house"]["next"])


async def test_preview_policies_and_next_transition(hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    api = r.api
    s = await stova(api, house)
    await r.settle()
    await r.walk(11)
    await enable(api, s)
    await r.walk(2)
    assert r.condition(s) == "day" and covers.calls == []
    change = {"evening_rule": "time", "evening_time_workday": "13:00"}
    preview = await api.ok("schedule/change", scope="global", values=change, dry_run=True)
    assert preview["needs_confirm"] and not preview["saved"]
    entry = preview["impact"][0]
    assert (entry["name"], entry["condition"], entry["predicted"], entry["moves"]) == (
        "Stova Suður", "day", "evening_privacy", 2)
    assert entry["before"] == "Fully open" and entry["after"] == "Evening privacy"
    assert [t["word"] for t in preview["transitions"]["after"]][:2] == ["Night", "Morning"]
    assert preview["schedule"]["values"]["evening_rule"] == "time"            # the draft page
    assert r.mgr.config.defaults.get("evening_rule") is None and covers.calls == []
    assert (await api.error("schedule/change", scope="global", values=change))["code"] == "confirm_required"
    assert (await api.error("schedule/change", scope="global", values={"evening_time_workday": "09:00",
                                                                        "evening_rule": "time"},
                            dry_run=True))["code"] == "inconsistent"
    assert (await api.error("schedule/change", scope="global", values={"lux_entity": LUX}))["code"] == \
        "unknown_setting"
    # Apply now: through the normal safety gates (evening privacy is not delayed by the minimum interval).
    await api.ok("schedule/change", scope="global", values=change, confirm=True, policy="now")
    await hass.async_block_till_done()
    assert r.condition(s) == "evening_privacy" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60)
    # Back to the 0.6.4 Evening, applied at the next natural transition: nothing moves now ...
    preview = await api.ok("schedule/change", scope="global", values={"evening_rule": "legacy"}, dry_run=True)
    assert preview["needs_confirm"] and preview["impact"][0]["predicted"] == "day"
    await api.ok("schedule/change", scope="global", values={"evening_rule": "legacy"}, confirm=True,
                 policy="next_change")
    await r.walk(30, step=120)
    assert (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (0, 60) and r.auto(s)["status"]["code"] == "deferred"
    # ... and at the next transition (Night 22:30) the window follows its schedule again.
    r.lux(0)
    r.sun(-30.0, 300)
    await r.until("22:20", step=600)
    await r.until("22:31")
    assert r.condition(s) == "night" and (r.pos(STOVA_SHEER), r.pos(STOVA_BLACKOUT)) == (100, 0)
    assert r.auto(s)["defer"] is None


async def test_no_room_or_window_schedules(hass, pilot, house, covers, admin_ws, freezer, faroe) -> None:
    """0.6.6: the Global Schedule is the only clock. Room and window schedules are refused; the window settings
    keep their own protections (e.g. sleep behaviour)."""
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    api = r.api
    k = await kamar(api, house)
    s = await stova(api, house)
    await api.ok("schedule/change", scope="global", values={"night_start_workday": "21:30"})
    err = await api.error("schedule/change", scope="room", area_id=house.kamar, values={"night_start_workday": "21:00"})
    assert err["code"] == "wrong_scope" and "Global Schedule" in err["message"]
    err = await api.error("auto/settings", scope="window", window_id=s, values={"evening_rule": "time"})
    assert err["code"] == "wrong_scope"
    await api.ok("schedule/change", scope="window", window_id=k, values={"sleep_end": "hold"})   # a protection
    model = await api.ok("model")
    assert model["centralized"] and not model["migration"]["pending"]
    schema = {x["key"]: x for x in model["rules"]["schema"]}
    assert all(schema[key]["scopes"] == ["global"] for key in ("night_start_workday", "night_end_workday",
                                                               "morning_mode", "evening_rule", "free_days"))
    win = {w["window_id"]: w for w in model["windows"]}
    assert win[k]["auto"]["sources"]["night_start_workday"] == "global"
    assert win[k]["auto"]["sources"]["sleep_end"] == "window"
    assert covers.calls == []


async def test_restart_in_the_night_replays_nothing(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                    service_calls) -> None:
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    entry, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T23:00:00+00:00", lux=0,
                           elevation=-40.0)
    s = await stova(r.api, house)
    await r.settle()
    await enable(r.api, s)
    await r.walk(2)
    assert r.condition(s) == "night" and covers.calls == []
    house_before = r.mgr.auto.house["key"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    r.mgr = entry.runtime_data.curtains
    await r.walk(5)
    assert r.condition(s) == "night" and covers.calls == []
    assert r.mgr.auto.house["key"] == house_before
    model = await r.api.ok("model")
    h = model["schedule"]["house"]
    assert h["phase"] == "night" and h["reason"] == "Night started according to the normal evening schedule"
    assert h["next"][0]["word"] == "Morning" and h["next"][0]["time"] == "06:30"
    assert h["until"].startswith("2026-11-20T06:30")
    # The night ends at 06:30, but it is dark: nothing opens and nothing is replayed.
    await r.until("07:00", step=600)
    assert r.condition(s) == "waiting" and covers.calls == []
    assert all(c.domain == "cover" for c in service_calls)


async def test_upgrade_from_0_6_4_keeps_the_schedule(hass, pilot, house, covers, admin_ws, freezer, faroe,
                                                     hass_storage) -> None:
    """A 0.6.4 store: every stored schedule value, mode, mapping and hold stays; Evening keeps the 0.6.4 rule
    until it is changed on purpose; the page says which implementation is active."""
    window = Window("w_stova", "Stova Suður", area_id=house.stova, mode="automatic", privacy=True).as_dict()
    window["layers"] = [{"layer_id": "l_s", "entity_id": STOVA_SHEER, "kind": "sheer"},
                        {"layer_id": "l_b", "entity_id": STOVA_BLACKOUT, "kind": "blackout"}]
    window["presets"] = [{"preset_id": "p_open", "name": "Fully open", "positions": {"l_s": 100, "l_b": 100}}]
    stored = {"windows": [window], "defaults": {"lux_entity": LUX, "night_start_workday": "21:15",
                                                "morning_mode": "until_time", "day_from_workday": "09:30",
                                                "free_days": [6]},
              "modes": {"day": [{"window_id": "w_stova", "layer_id": None, "preset_id": "p_open"}]}}
    hass_storage["smart_occupancy.curtains"] = {"version": 1, "minor_version": 4, "key": "smart_occupancy.curtains",
                                                "data": stored}
    hass_storage["smart_occupancy.curtains_state"] = {
        "version": 1, "minor_version": 1, "key": "smart_occupancy.curtains_state",
        "data": {"auto": {"w_stova": {"hold": {"source": "manual", "created": "2026-11-19T12:50:00+00:00",
                                               "policy": "until_released", "phase": "day"}}}}}
    set_cover(hass, STOVA_SHEER, 30)
    set_cover(hass, STOVA_BLACKOUT, 100)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    cfg = r.mgr.config
    values_, sources = cfg.rules_for(cfg.windows["w_stova"])
    assert (values_["night_start_workday"], values_["day_from_workday"], values_["free_days"]) == ("21:15", "09:30",
                                                                                                 (6,))
    assert values_["evening_rule"] == "legacy" and sources["evening_rule"] == "default"
    assert cfg.windows["w_stova"].mode == "automatic" and cfg.modes["day"][0].preset_id == "p_open"
    model = await r.api.ok("model")
    sched = model["schedule"]
    assert sched["implementation"] == "Global Curtain Schedule (0.6.5); Evening: 0.6.4 behaviour"
    assert any(w.startswith("Evening uses the 0.6.4 behaviour") for w in sched["warnings"])
    assert sched["values"]["night_start_workday"] == "21:15" and sched["sources"]["night_start_workday"] == "global"
    await r.settle()
    await r.walk(15)
    assert r.condition("w_stova") == "manual_override" and covers.calls == [] and r.pos(STOVA_SHEER) == 30
    # Saturday is a normal day now (free days: Sunday only): Friday evening follows the normal 21:15.
    assert schedule.night_now(at(FRI, "21:20"), values_)[0]


async def test_historical_early_morning_failure_with_the_schedule(hass, pilot, house, covers, admin_ws, freezer,
                                                                  faroe) -> None:
    """11-12 / 11-13 again, with the Global Schedule's Morning at 07:00: at 07:00:14, −10.2°, 0 lx and sleep on,
    nothing opens in any room."""
    set_cover(hass, KAMAR, 0)
    set_cover(hass, STOVA_SHEER, 100)
    set_cover(hass, STOVA_BLACKOUT, 0)
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T06:40:00+00:00", lux=0, elevation=-12.0,
                        sleep=True)
    await r.api.ok("schedule/change", scope="global", values=ACCEPTANCE)
    k, s = await kamar(r.api, house), await stova(r.api, house)
    await r.settle()
    await enable(r.api, k)
    await enable(r.api, s)
    r.sun(-10.2, 115)
    await r.until("07:00")
    await r.walk(0.25, step=14)
    assert covers.calls == [] and r.pos(KAMAR) == 0 and r.pos(STOVA_BLACKOUT) == 0
    assert r.condition(k) == "sleep" and r.condition(s) == "waiting"


async def test_schedule_leaves_occupancy_lighting_and_forecasts_alone(hass, pilot, house, covers, admin_ws, freezer,
                                                                      faroe, service_calls) -> None:
    _e, r = await begin(hass, pilot, freezer, covers, admin_ws, "2026-11-19T13:00:00+00:00", lux=3000, elevation=5)
    await stova(r.api, house)
    await r.api.ok("schedule/change", scope="global", values=ACCEPTANCE)
    await r.walk(30, step=300)
    assert all(c.domain == "cover" for c in service_calls)
    text = open(schedule.__file__, encoding="utf-8").read().lower()
    assert "mood" not in text and "light." not in text
    _ = Context
