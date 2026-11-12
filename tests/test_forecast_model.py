"""Occupancy forecast (0.5.0): learning, prediction, confidence and evaluation on SIMULATED histories.

Every history here is synthetic (tests/forecast_helpers.py). Nothing is a real
Home Assistant observation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json

import pytest

from custom_components.smart_occupancy import forecast_history
from custom_components.smart_occupancy.forecast_history import (
    lookahead_seconds,
    replay,
    summarize,
    warmup_seconds,
)
from custom_components.smart_occupancy.forecast_model import (
    ADJACENT_CONTEXT,
    HORIZONS,
    MAX_EVENTS_PER_HOUR,
    Accuracy,
    ForecastInput,
    HouseDay,
    RoomStats,
    accuracy,
    confidence,
    explain,
    forecast,
    next_rooms,
)

from .forecast_helpers import DAY0, UTC, Sim, day_start, daykey, learn, model_of, pir, radar, spec

HALL, OFFICE = "binary_sensor.hall_pir", "binary_sensor.office_pir"


def _office_sim(n_days: int, *, weekends: bool = True, hour: int = 16) -> Sim:
    """Every day at about 16:00 someone walks through the hall into the office and stays until 18:00."""
    sim = Sim([HALL, OFFICE], day_start(DAY0) - 86400)
    for i in range(n_days):
        d = DAY0 + timedelta(days=i)
        if not weekends and d.weekday() >= 5:
            continue
        t = day_start(d) + hour * 3600 + (i % 3) * 300
        sim.pulse(HALL, t)
        sim.stay(OFFICE, t + 20, day_start(d) + (hour + 2) * 3600)
    return sim


def _office_spec(**kwargs):
    return spec(("hall", (pir(HALL),)), ("office", (pir(OFFICE),)), **kwargs)


def _at(d, hour: int, minute: int = 0) -> float:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC).timestamp()


# ------------------------------------------------------------------ history
def test_replay_uses_the_live_engine_rules() -> None:
    """Occupied time, entries and exits come from the same engine as live occupancy (vacancy confirmation)."""
    sp = _office_spec()
    days = learn(sp, _office_sim(1).history(), 1, evaluate=False)
    office = days[0].rooms["office"]
    assert sum(office.ent) == 1 and sum(office.ext) == 1
    # Motion until ~17:56 plus the engine's exit confirmation (5 min, an unconfirmed PIR visit).
    minutes = sum(office.occ) / 60
    assert 120 <= minutes <= 135
    assert office.observed_seconds == pytest.approx(86400)
    assert days[0].trans[("hall", "office")].n == 1


def test_short_coverage_and_no_invented_history() -> None:
    """Before the first recorded state nothing is observed: no occupied or vacant time is invented."""
    sp = _office_spec()
    sim = Sim([HALL, OFFICE], day_start(DAY0) + 12 * 3600)  # history starts at noon of the first day
    sim.pulse(HALL, day_start(DAY0) + 16 * 3600)
    days = learn(sp, sim.history(), 1, evaluate=False)
    assert days[0].rooms["hall"].observed_seconds == pytest.approx(12 * 3600)


def test_unavailable_sensor_is_not_observed() -> None:
    sp = _office_spec()
    sim = _office_sim(1)
    sim.set(OFFICE, day_start(DAY0) + 2 * 3600, "unavailable")
    sim.set(OFFICE, day_start(DAY0) + 5 * 3600, "off")
    days = learn(sp, sim.history(), 1, evaluate=False)
    office = days[0].rooms["office"]
    assert office.observed_seconds == pytest.approx(86400 - 3 * 3600, abs=60)
    assert sum(office.occ[2:5]) == 0 and sum(office.vac[2:5]) == 0


def test_summary_round_trip_and_size() -> None:
    sp = _office_spec()
    days = learn(sp, _office_sim(3).history(), 3)
    for d in days:
        data = json.loads(json.dumps(d.as_dict()))
        again = HouseDay.from_dict(data)
        assert again.as_dict() == d.as_dict()
    # Aggregates only: a day of two rooms is a few kB, not a list of movements.
    assert len(json.dumps(days[-1].as_dict())) < 6000


def test_anomalous_hour_is_capped() -> None:
    """A party (or a sensor gone wild) in one hour cannot dominate what is learned."""
    from custom_components.smart_occupancy.forecast_history import ReplaySpec, RoomSpec
    from custom_components.smart_occupancy.models import OccupancyTiming

    fast = OccupancyTiming(exit_confirmation=0, inactivity_timeout=30, startup_hold=0)
    sp = ReplaySpec(rooms={"hall": RoomSpec("hall", (pir(HALL),), fast)})
    sim = Sim([HALL], day_start(DAY0) - 86400)
    for k in range(60):  # a separate short visit every minute for an hour
        sim.pulse(HALL, day_start(DAY0) + 20 * 3600 + k * 60, hold=5)
    sim.pulse(HALL, day_start(DAY0) + 9 * 3600, hold=5)
    days = learn(sp, sim.history(), 1, evaluate=False)
    hall = days[0].rooms["hall"]
    assert hall.ent[9] == 1
    assert hall.ent[20] == pytest.approx(MAX_EVENTS_PER_HOUR)


# ------------------------------------------------------------------ prediction
def test_time_of_day_probability() -> None:
    sp = _office_spec()
    days = learn(sp, _office_sim(10).history(), 10)
    today = DAY0 + timedelta(days=10)
    model = model_of(sp, days, today)
    before = forecast(model, "office", ForecastInput(now=_at(today, 15, 50), occupied=False,
                                                     since=_at(today, 15, 50) - 6 * 3600), daykey)
    morning = forecast(model, "office", ForecastInput(now=_at(today, 10), occupied=False,
                                                      since=_at(today, 10) - 6 * 3600), daykey)
    assert before.probabilities[60] > 0.6
    assert morning.probabilities[60] < 0.05
    assert model.rooms["office"].occupancy.at(today.weekday(), True, 17) > 0.8


def test_horizons_differ_and_entry_is_monotonic() -> None:
    sp = _office_spec()
    days = learn(sp, _office_sim(10).history(), 10)
    today = DAY0 + timedelta(days=10)
    model = model_of(sp, days, today)
    fc = forecast(model, "office", ForecastInput(now=_at(today, 15, 45), occupied=False,
                                                 since=_at(today, 15, 45) - 6 * 3600), daykey)
    probs = [fc.probabilities[h] for h in HORIZONS]
    entry = [fc.entry[h] for h in HORIZONS]
    assert len(set(round(p, 3) for p in probs)) == len(HORIZONS)
    assert entry == sorted(entry)
    # A forecast is about the future, not the current state: an occupied room late in its usual visit
    # is forecast to empty.
    late = forecast(model, "office", ForecastInput(now=_at(today, 17, 55), occupied=True,
                                                   since=_at(today, 16, 0)), daykey)
    assert late.probabilities[5] > late.probabilities[60]
    assert late.probabilities[60] < 0.3


def test_weekday_pattern_with_shrinkage() -> None:
    """Workdays only: a Monday 15:50 is forecast busy, a Saturday less so; one sample never gives certainty."""
    sp = _office_spec()
    days = learn(sp, _office_sim(21, weekends=False).history(), 21)
    today = DAY0 + timedelta(days=21)  # Monday
    model = model_of(sp, days, today)
    saturday = today + timedelta(days=5)
    occ = model.rooms["office"].occupancy
    assert occ.at(today.weekday(), True, 17) > 0.8
    assert occ.at(saturday.weekday(), False, 17) < 0.5
    # Only two days of history: shrunk towards the general pattern, not 0 % / 100 %.
    few = learn(sp, _office_sim(2).history(), 2)
    weak = model_of(sp, few, DAY0 + timedelta(days=2)).rooms["office"].occupancy.at(2, True, 17)
    assert 0.2 < weak < 0.95


def test_movement_transitions_and_delay() -> None:
    sp = _office_spec()
    days = learn(sp, _office_sim(10).history(), 10)
    model = model_of(sp, days, DAY0 + timedelta(days=10))
    pair = model.pairs[("hall", "office")]
    assert pair.probability > 0.6
    assert 10 <= pair.median_delay <= 30
    now = _at(DAY0 + timedelta(days=10), 11)
    nxt = next_rooms(model, "hall", 5, daykey, now)
    assert nxt[0][0] == "office" and nxt[0][1] > 0.5
    # A recent hall arrival raises the office's short-term forecast far above its 11:00 schedule.
    base = forecast(model, "office", ForecastInput(now=now, occupied=False, since=now - 3600), daykey)
    moved = forecast(model, "office", ForecastInput(now=now, occupied=False, since=now - 3600,
                                                    arrivals=[("hall", now - 5)]), daykey)
    assert moved.probabilities[5] > 0.5 > base.probabilities[5]
    assert moved.movement["hall"] > 0.5
    # Explanations name the evidence.
    text, factors = explain(model, "office", ForecastInput(now=now, occupied=False, since=now - 3600,
                                                           arrivals=[("hall", now - 5)]), moved, daykey, 5, str)
    assert "movement in hall" in text
    assert any(f.kind == "movement" for f in factors)


def test_open_plan_overlap_is_not_movement() -> None:
    """Two rooms seen by one radar report the same person at the same moment: that is not a movement."""
    kitchen, living = "binary_sensor.fp2_kitchen_zone", "binary_sensor.fp2_living_zone"
    sim = Sim([kitchen, living], day_start(DAY0) - 86400)
    for k in range(10):
        t = day_start(DAY0) + 9 * 3600 + k * 900
        sim.pulse(living, t)
        sim.pulse(kitchen, t + 1)  # one second later, same device
    rooms = (("kitchen", (radar(kitchen),)), ("living", (radar(living),)))
    plain = learn(spec(*rooms), sim.history(), 1, evaluate=False)
    overlapping = learn(spec(*rooms, overlaps=frozenset({frozenset(("kitchen", "living"))})), sim.history(), 1,
                        evaluate=False)
    assert plain[0].trans[("living", "kitchen")].n == 10
    assert not overlapping[0].trans
    assert sum(overlapping[0].rooms["kitchen"].arr) == 0


def test_connected_room_context() -> None:
    """'A connected room is occupied' is learned from the room relationships, not assumed."""
    sp = _office_spec(connected={"office": frozenset({"hall"}), "hall": frozenset({"office"})})
    days = learn(sp, _office_sim(10).history(), 10)
    factor = model_of(sp, days, DAY0 + timedelta(days=10)).rooms["office"].contexts[ADJACENT_CONTEXT]
    assert factor.entry_on > 2  # the office is entered right after the hall is used


def test_multiple_occupants() -> None:
    """Two people moving at once: both arrivals count and probabilities stay valid."""
    sp = _office_spec()
    sim = _office_sim(10)
    for i in range(10):  # a second person uses the hall alone at 08:00
        sim.pulse(HALL, day_start(DAY0 + timedelta(days=i)) + 8 * 3600)
    days = learn(sp, sim.history(), 10)
    model = model_of(sp, days, DAY0 + timedelta(days=10))
    pair = model.pairs[("hall", "office")]
    assert 0.3 < pair.probability < 0.7  # half of the hall arrivals continue to the office
    now = _at(DAY0 + timedelta(days=10), 16)
    fc = forecast(model, "office", ForecastInput(now=now, occupied=False, since=now - 3600,
                                                 arrivals=[("hall", now - 5), ("hall", now - 60)]), daykey)
    assert all(0.0 <= p <= 1.0 for p in fc.probabilities.values())


# ------------------------------------------------------------------ confidence
def test_insufficient_data_and_confidence_levels() -> None:
    acc = Accuracy()
    few = confidence(RoomStats(days=1, weekdays=1, entries=2), acc, min_days=3, min_entries=10, signals_known=1,
                     usable=True)
    assert few.level == "insufficient_data" and few.score == 0 and len(few.reasons) == 2
    week = confidence(RoomStats(days=8, weekdays=7, entries=40), acc, min_days=3, min_entries=10, signals_known=1,
                      usable=True)
    assert week.level in ("low", "moderate")
    # High needs two weeks AND forecasts that proved better than the schedule.
    good = Accuracy(n=500, brier=0.05, brier_schedule=0.10, brier_persistence=0.2)
    high = confidence(RoomStats(days=21, weekdays=7, entries=80), good, min_days=3, min_entries=10,
                      signals_known=1, usable=True)
    assert high.level == "high"
    bad = Accuracy(n=500, brier=0.12, brier_schedule=0.10, brier_persistence=0.2)
    capped = confidence(RoomStats(days=60, weekdays=7, entries=400), bad, min_days=3, min_entries=10,
                        signals_known=1, usable=True)
    assert capped.level != "high"
    down = confidence(RoomStats(days=21, weekdays=7, entries=80), good, min_days=3, min_entries=10,
                      signals_known=0.5, usable=True)
    assert down.score < high.score
    off = confidence(RoomStats(days=21, weekdays=7, entries=80), good, min_days=3, min_entries=10,
                     signals_known=0, usable=False)
    assert off.level == "insufficient_data"


def test_confidence_grows_with_history() -> None:
    sp = _office_spec()
    sim = _office_sim(21).history()
    levels = []
    for n in (2, 8, 21):
        days = learn(sp, sim, n)
        model = model_of(sp, days, DAY0 + timedelta(days=n))
        acc = accuracy(days, "office", 15)
        levels.append(confidence(model.rooms["office"].stats, acc, min_days=3, min_entries=5, signals_known=1,
                                 usable=True).score)
    assert levels[0] == 0 and levels[0] < levels[1] < levels[2]


# ------------------------------------------------------------------ evaluation
def test_prequential_evaluation_beats_schedule() -> None:
    sp = _office_spec()
    days = learn(sp, _office_sim(14).history(), 14)
    acc = accuracy(days, "office", 5)
    assert acc.n > 500
    assert acc.mean(acc.brier) < acc.mean(acc.brier_schedule)
    assert acc.skill(acc.brier_schedule) > 0
    report = acc.as_dict(0.5)
    assert report["true_positive"] + report["false_negative"] > 0
    assert sum(b["forecasts"] for b in report["calibration"]) == report["forecasts"]
    # Days whose model had too little history are not scored (no fake accuracy from the first days).
    assert days[0].rooms["office"].ev == {} and days[0].rooms["office"].skipped == 0
    assert days[1].rooms["office"].skipped > 0


def test_no_look_ahead_in_evaluation(monkeypatch) -> None:
    """A forecast issued at t is identical whatever happens after t on that day (and the day is not in its model)."""
    sp = _office_spec()
    base = _office_sim(8)
    other = _office_sim(8)
    d = DAY0 + timedelta(days=7)
    change = day_start(d) + 12 * 3600
    other.stay(OFFICE, change + 60, change + 3 * 3600)  # an extra afternoon visit on the evaluated day
    seen: dict[str, dict[float, dict[int, float]]] = {"base": {}, "other": {}}
    real = forecast_history.forecast

    def run(label: str, sim: Sim) -> None:
        def spy(model, area, inp, dk, **kw):
            fc = real(model, area, inp, dk, **kw)
            if area == "office":
                seen[label][inp.now] = dict(fc.probabilities)
            return fc
        monkeypatch.setattr(forecast_history, "forecast", spy)
        days = learn(sp, sim.history(), 7)  # the 7 earlier days only
        model = model_of(sp, days, d)
        ds = day_start(d)
        rp = replay(sp, sim.history(), ds - warmup_seconds(), ds + 86400 + lookahead_seconds())
        seen[label].clear()
        summarize(sp, rp, date=d.isoformat(), dow=d.weekday(), workday=True, day_start=ds, day_end=ds + 86400,
                  daykey=daykey, model=model)

    run("base", base)
    run("other", other)
    issued_before = [t for t in seen["base"] if t <= change]
    assert issued_before
    for t in issued_before:
        assert seen["base"][t] == seen["other"][t]
    assert any(seen["base"][t] != seen["other"][t] for t in seen["base"] if t > change + 120)
