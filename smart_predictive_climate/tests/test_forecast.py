"""Predictions against closed-form answers: trajectories, warm-up, the preheat start, sun and uncertainty."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
from zoneinfo import ZoneInfo

import pytest

from spc import forecast as F, thermal_model as tm
from spc.prep import WindowSpec
from spc.solar import to_compass

TZ = ZoneInfo("Atlantic/Faroe")
LOCATION = (62.0, -6.8)


def model(**over) -> tm.ThermalModel:
    base = dict(area_id="kamar", trained_at="", status="moderate", preliminary=False, reference="outdoor",
                reference_c=None, k=0.1, d=0.0, heating=2.0, heating_basis="reported", lag_min=0.0, tau_min=0.0,
                solar=None, night=None, coupling={}, uncertainty={60: {"q80": 0.2}, 180: {"q80": 0.4}},
                confidence="moderate", fingerprint_hash=None)
    base.update(over)
    return tm.ThermalModel(**base)


def ctx(now: datetime, temp: float, *, outdoor: float = 5.0, targets=None, **over) -> F.Context:
    t = now.timestamp()
    c = F.Context(now=t, tz=TZ, temp_c=temp, outdoor_c=outdoor, location=LOCATION, heating_now=0.0)
    for k, v in over.items():
        setattr(c, k, v)
    c.targets = targets if targets is not None else []
    return c


def comfort(start="07:00", end="22:00", day=21.0, night=None):
    return {"day_c": day, "night_c": night, "weekday": {"start": start, "end": end}, "weekend": {"start": start, "end": end},
            "away_entity": None, "away_c": None, "follow_occupancy": False}


def schedule(now: datetime, c: dict, hours: int = 24, **kw):
    times = [now.timestamp() + i * 300 for i in range(hours * 12 + 1)]
    return F.comfort_targets(c, times, TZ, **kw)[0]


def test_passive_trajectory_is_the_closed_form():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    m = model()
    c = ctx(now, 20.0)
    p = F.plan(m, c, 72, solar_basis="geometry")
    traj = F.run(m, c, p, [0.0] * 72)
    for i in (12, 36, 72):
        hours = i * 5 / 60
        assert traj.temps[i] == pytest.approx(5 + 15 * math.exp(-0.1 * hours), abs=1e-6)


def test_preheat_start_matches_the_closed_form():
    """k = 0.1/h, b = 3 °C/h, outdoor 5 °C: heating settles at 35 °C. From 18 °C to 21 °C:
    t = -ln((25-21)/(25-T_s)) / k, with the room cooling passively until the start."""
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)  # 03:00 Faroese time (UTC+0 in winter); comfort from 07:00
    m = model(heating=3.0, uncertainty={})          # no band: the expected start is recommended
    c = ctx(now, 18.0, targets=schedule(now, comfort()))
    out = F.predict(m, c, horizon_h=24, solar_basis="geometry")
    assert out["available"] and out["state"] == "preheat_later" and out["requirement"]["target_c"] == 21.0
    start = datetime.fromisoformat(out["preheat_start"])
    # The analytic latest start: cooling from 18 °C for s hours, then heating for (4 - s) hours reaches 21 °C.
    def at_seven(s: float) -> float:
        cooled = 5 + 13 * math.exp(-0.1 * s)
        return 35 + (cooled - 35) * math.exp(-0.1 * (4 - s))
    lo, hi = 0.0, 4.0
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if at_seven(mid) >= 21.0 - F.REACHED_C else (lo, mid)
    exact = now + timedelta(hours=lo)
    assert abs((start - exact).total_seconds()) <= 300  # within one 5-minute step
    assert out["heating_rate"] == pytest.approx(3.0 - 0.1 * (18 - 5), abs=0.01)
    assert out["cooling_rate"] == pytest.approx(-0.1 * (18 - 5), abs=0.01)
    assert out["warmup_min"] == pytest.approx(60 * -math.log((35 - 21) / (35 - 18)) / 0.1, abs=5)


def test_uncertainty_makes_the_recommended_start_earlier_but_bounded():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    tight = F.predict(model(heating=3.0, uncertainty={}), ctx(now, 18.0, targets=schedule(now, comfort())), horizon_h=24, solar_basis="geometry")
    wide = F.predict(model(heating=3.0, uncertainty={60: {"q80": 0.5}, 180: {"q80": 1.2}}), ctx(now, 18.0, targets=schedule(now, comfort())),
                     horizon_h=24, solar_basis="geometry")
    expected = datetime.fromisoformat(wide["preheat_start_expected"])
    cautious = datetime.fromisoformat(wide["preheat_start"])
    assert wide["preheat_start_expected"] == tight["preheat_start"]
    assert timedelta(0) < expected - cautious <= timedelta(minutes=F.MAX_CAUTION_MIN)
    assert "margin for the model's uncertainty" in " ".join(wide["explanation"])


def test_sunshine_shortens_or_removes_the_preheat():
    """A south window in the morning sun: the room needs less heating, or none."""
    now = datetime(2026, 4, 15, 4, 0, tzinfo=UTC)   # 05:00 local summer time; comfort from 09:00
    windows = [WindowSpec("w", to_compass(0.0), area_m2=3.0)]  # 0° = South
    c_plain = ctx(now, 19.0, outdoor=8.0, targets=schedule(now, comfort(start="13:00")), windows=windows)
    no_sun = F.predict(model(), c_plain, horizon_h=24, solar_basis="geometry")
    sunny = F.predict(model(solar=3.0), ctx(now, 19.0, outdoor=8.0, targets=schedule(now, comfort(start="13:00")),
                                            windows=windows), horizon_h=24, solar_basis="geometry")
    assert sunny["solar_gain_c"] and sunny["solar_gain_c"] > 1.0
    if sunny["preheat_needed"]:
        assert datetime.fromisoformat(sunny["preheat_start"]) > datetime.fromisoformat(no_sun["preheat_start"])
    else:
        assert sunny["state"] == "not_needed" and "sun" in " ".join(sunny["explanation"]).lower()
    # The same window facing north gets no direct sun at that time of day
    north = F.predict(model(solar=3.0), ctx(now, 19.0, outdoor=8.0, targets=schedule(now, comfort(start="13:00")),
                                            windows=[WindowSpec("w", to_compass(180.0), area_m2=3.0)]),
                      horizon_h=24, solar_basis="geometry")
    assert (north["solar_gain_c"] or 0.0) < 0.2


def test_cloud_forecast_scales_the_sun_only_when_the_model_learned_with_clouds():
    now = datetime(2026, 4, 15, 4, 0, tzinfo=UTC)
    windows = [WindowSpec("w", to_compass(0.0), area_m2=3.0)]
    overcast = [(now.timestamp() + h * 3600, 8.0, 100.0) for h in range(30)]
    args = dict(outdoor=8.0, targets=schedule(now, comfort(start="13:00")), windows=windows, outdoor_forecast=overcast)
    with_clouds = F.predict(model(solar=3.0), ctx(now, 19.0, **args), horizon_h=24, solar_basis="geometry+cloud")
    average = F.predict(model(solar=3.0), ctx(now, 19.0, **args), horizon_h=24, solar_basis="geometry")
    assert with_clouds["solar_gain_c"] < average["solar_gain_c"]
    assert with_clouds["inputs"] == {**with_clouds["inputs"], "outdoor": "forecast", "sky": "forecast"}


def test_outdoor_forecast_is_used_and_missing_outdoor_is_unavailable():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    cold = [(now.timestamp() + h * 3600, -10.0, None) for h in range(30)]
    mild = F.predict(model(), ctx(now, 20.0), horizon_h=24, solar_basis="geometry")
    frost = F.predict(model(), ctx(now, 20.0, outdoor_forecast=cold), horizon_h=24, solar_basis="geometry")
    assert frost["passive_end"] < mild["passive_end"]
    assert mild["inputs"]["outdoor"] == "measured_held" and any("assumed to hold" in w for w in mild["warnings"])
    gone = F.predict(model(), ctx(now, 20.0, outdoor=None), horizon_h=24, solar_basis="geometry")
    assert gone["available"] is False and gone["code"] == "no_outdoor"
    unknown = F.predict(model(), ctx(now, None), horizon_h=24, solar_basis="geometry")
    assert unknown["available"] is False and unknown["code"] == "no_temperature"


def test_no_heating_response_gives_no_preheat_time_but_a_prediction():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    out = F.predict(model(heating=None), ctx(now, 18.0, targets=schedule(now, comfort())), horizon_h=24, solar_basis="geometry")
    assert out["available"] and out["predicted_1h"] is not None
    assert out["preheat_needed"] is None and out["state"] == "unknown_heating" and out["warmup_min"] is None


def test_too_cold_to_reach_in_time_says_so():
    now = datetime(2026, 1, 12, 6, 30, tzinfo=UTC)
    out = F.predict(model(), ctx(now, 12.0, targets=schedule(now, comfort())), horizon_h=24, solar_basis="geometry")
    assert out["state"] == "late" and out["preheat_start"] == out["computed_at"]
    assert "Even with full heating" in out["explanation"][0]


def test_already_warm_needs_nothing():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    out = F.predict(model(d=2.0), ctx(now, 23.0, targets=schedule(now, comfort(night=16.0))), horizon_h=24, solar_basis="geometry")
    assert out["state"] in ("not_needed", "preheat_later")


def test_comfort_targets_weekdays_weekend_away_and_dst():
    c = {"day_c": 21.0, "night_c": 17.0, "weekday": {"start": "06:30", "end": "22:00"},
         "weekend": {"start": "09:00", "end": "23:00"}, "away_entity": None, "away_c": 15.0, "follow_occupancy": False}
    monday_7 = datetime(2026, 3, 30, 7, 0, tzinfo=TZ).timestamp()   # the day after the switch to summer time
    saturday_7 = datetime(2026, 3, 28, 7, 0, tzinfo=TZ).timestamp()
    sunday_830 = datetime(2026, 3, 29, 8, 30, tzinfo=TZ).timestamp()  # summer time began at 01:00 UTC
    targets, why = F.comfort_targets(c, [monday_7, saturday_7, sunday_830], TZ)
    assert targets == [21.0, 17.0, 17.0] and why[0] == "comfort period"
    assert F.comfort_targets(c, [monday_7], TZ, away=True)[0] == [15.0]
    occupancy = {"weekday": [0.0] * 7 + [0.9] * 17, "weekend": [0.0] * 24}
    follow = {**c, "follow_occupancy": True}
    assert F.comfort_targets(follow, [monday_7, datetime(2026, 3, 30, 6, 45, tzinfo=TZ).timestamp()], TZ,
                             occupancy=occupancy)[0] == [21.0, 17.0]


def test_closed_door_removes_the_neighbour_influence():
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    m = model(coupling={"stova": 0.5})
    open_door = F.predict(m, ctx(now, 18.0, neighbours={"stova": 24.0}), horizon_h=6, solar_basis="geometry")
    closed = F.predict(m, ctx(now, 18.0, neighbours={"stova": 24.0}, barriers={"stova": True}), horizon_h=6,
                       solar_basis="geometry")
    assert open_door["predicted_1h"] > closed["predicted_1h"] + 0.5


def test_open_window_simulation_is_hypothetical_and_cooler():
    now = datetime(2026, 1, 12, 12, 0, tzinfo=UTC)
    m = model()
    c = ctx(now, 21.0)
    p = F.plan(m, c, 24, solar_basis="geometry")
    shut = F.run(m, c, p, [0.0] * 24)
    opened = F.run(m, c, p, [0.0] * 24, extra_loss=[F.ASSUMED_WINDOW_LOSS] * 12 + [0.0] * 12)
    assert opened.kind == "hypothetical" and opened.temps[12] < shut.temps[12] - 0.5
    assert opened.temps[24] < shut.temps[24]


def test_heating_as_now_stops_at_the_thermostat_setpoint():
    """A thermostat that reports "heating" heats until its setpoint, not for the whole horizon (k = 0.1/h,
    b = 2 °C/h, outdoor 5 °C: held full heating would settle at 25 °C)."""
    now = datetime(2026, 1, 12, 3, 0, tzinfo=UTC)
    m = model(heating=2.0)
    held = F.predict(m, ctx(now, 18.0, heating_now=1.0), horizon_h=24, solar_basis="geometry", recommend_from="moderate")
    assert held["inputs"]["heating_course"] == "held"
    assert held["hourly"][24]["expected"] > 24.0
    assert any("no setpoint" in w for w in held["warnings"])
    limited = F.predict(m, ctx(now, 18.0, heating_now=1.0, setpoint_c=20.0), horizon_h=24, solar_basis="geometry",
                        recommend_from="moderate")
    assert limited["inputs"]["heating_course"] == "until_setpoint" and limited["inputs"]["setpoint_c"] == 20.0
    temps = [h["expected"] for h in limited["hourly"]]
    assert temps[1] > 18.5 and max(temps) >= 19.8  # it warms while below the setpoint (0.7 °C/h at first)
    assert max(temps) <= 20.3                    # and stops there (one 5-minute step of overshoot at most)
    assert temps[-1] == pytest.approx(20.0, abs=0.3)
    # Above the setpoint: no heating while it cools down to it, then the thermostat holds it there.
    warm = F.predict(m, ctx(now, 22.0, heating_now=1.0, setpoint_c=20.0), horizon_h=6, solar_basis="geometry",
                     recommend_from="moderate")
    assert warm["hourly"][1]["expected"] == pytest.approx(warm["hourly"][1]["passive"], abs=0.01)
    assert warm["hourly"][3]["passive"] < 18.0 and 19.7 <= warm["hourly"][3]["expected"] <= 20.3
