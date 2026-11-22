"""Predictions from a learned room model: trajectories, warm-up times, preheat start and their explanation.

Pure code. ``Context`` is what is known *now* (measurements, weather forecast, covers, neighbours); ``predict``
returns everything the App publishes and shows for a room. Every number says what kind of number it is:

* ``measured``      a reading from a sensor;
* ``forecast``      a value from a weather forecast (taken as given);
* ``assumed``       held constant or a documented default, because nothing better is known;
* ``model``         computed by the room's learned model (with its validated uncertainty);
* ``hypothetical``  a "what if" of the simulator.

Nothing here can reach a device: the result is a dictionary.

The preheat search. For the next time the comfort target rises (the start of a comfort period), the room is
simulated with its heating off until a candidate start and fully on afterwards (the heating fraction the model
learned as "full", i.e. what its thermostats did when they heated). The latest start at which the predicted
temperature reaches the target is the *expected* start; the latest start at which even the lower edge of the
80 % uncertainty band reaches it is the *cautious* start, and that is the one recommended (never more than
``MAX_CAUTION_MIN`` earlier than the expected one). When the room reaches the target without heating - sunshine,
a warm neighbour, stored heat - no preheat is recommended, and the explanation says why.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import math
from typing import Any

from . import thermal_model as tm
from .prep import NIGHT_HOURS, WindowSpec, window_index
from .solar import clear_fraction, position as sun_position

STEP_MIN = 5.0
REACHED_C = 0.1              # "reached" = within this of the target
MAX_CAUTION_MIN = 90         # the cautious start is at most this much earlier than the expected one
#: When nothing is known about the sky but the model's solar coefficient was learned with measured brightness,
#: this share of clear-sky sunshine is assumed (and flagged).
ASSUMED_CLEAR = 0.5
#: Extra loss with a window open (1/h towards outdoors) when the room's own data cannot tell: a documented
#: assumption (a tilted window in still air), used only by the simulator and always labelled.
ASSUMED_WINDOW_LOSS = 0.3
LEVELS = {"none": 0, "low": 1, "moderate": 2, "high": 3}


@dataclass
class Context:
    """What is known about a room now. Times are UTC timestamps; temperatures °C."""

    now: float
    tz: Any                                     # a tzinfo for the home's local time
    temp_c: float | None                        # the room temperature now (measured)
    temp_source: str | None = None
    outdoor_c: float | None = None              # measured now
    outdoor_forecast: list[tuple[float, float | None, float | None]] = field(default_factory=list)
    #                                             [(t, temperature, cloud coverage %)], hourly, from a weather entity
    cloud_now: float | None = None              # measured or reported now (weather entity), %
    heating_now: float | None = None            # reported heating fraction 0..1
    setpoint_c: float | None = None             # the thermostats' setpoint now (highest), None = not known
    heating_history: list[float] = field(default_factory=list)  # 5-min steps before now, newest last
    location: tuple[float, float] | None = None
    windows: list[WindowSpec] = field(default_factory=list)
    covers: dict[str, float | None] = field(default_factory=dict)
    window_open: bool | None = None
    neighbours: dict[str, float | None] = field(default_factory=dict)
    barriers: dict[str, bool] = field(default_factory=dict)  # neighbour -> a closed door separates it now
    targets: list[float | None] = field(default_factory=list)  # the comfort target for every step from now
    target_reasons: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ targets
def comfort_targets(comfort: dict[str, Any], times: Sequence[float], tz: Any, *, away: bool | None = None,
                    occupancy: dict[str, list[float]] | None = None) -> tuple[list[float | None], list[str]]:
    """The comfort target for each time, and why. ``None`` = no requirement at that time.

    ``occupancy`` (optional, learned): {"weekday": [24 shares], "weekend": [24 shares]}; with
    ``comfort.follow_occupancy`` the day target applies only in hours when the room is usually occupied.
    """
    out: list[float | None] = []
    why: list[str] = []
    for t in times:
        local = datetime.fromtimestamp(t, UTC).astimezone(tz)
        weekend = local.weekday() >= 5
        period = comfort["weekend" if weekend else "weekday"]
        minute = local.hour * 60 + local.minute
        start = int(period["start"][:2]) * 60 + int(period["start"][3:])
        end = int(period["end"][:2]) * 60 + int(period["end"][3:])
        inside = start <= minute < end if start < end else (minute >= start or minute < end)
        if inside and comfort.get("follow_occupancy") and occupancy:
            shares = occupancy.get("weekend" if weekend else "weekday") or []
            if len(shares) == 24 and shares[local.hour] < 0.3:
                inside = False
                reason = "usually not occupied at this hour (learned)"
            else:
                reason = "comfort period"
        else:
            reason = "comfort period" if inside else "outside the comfort period"
        if away:
            out.append(comfort.get("away_c"))
            why.append("away")
        elif inside:
            out.append(comfort["day_c"])
            why.append(reason)
        else:
            out.append(comfort.get("night_c"))
            why.append(reason)
    return out, why


def night_flag(t: float, tz: Any) -> bool:
    hour = datetime.fromtimestamp(t, UTC).astimezone(tz).hour
    start, end = NIGHT_HOURS
    return hour >= start or hour < end


# ------------------------------------------------------------------ inputs
def _interp(series: list[tuple[float, float | None]], t: float) -> float | None:
    points = [(a, b) for a, b in series if b is not None]
    if not points or t < points[0][0] - 3600 or t > points[-1][0] + 3600:
        return None
    for (t0, v0), (t1, v1) in zip(points, points[1:], strict=False):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * (t - t0) / max(t1 - t0, 1.0)
    return points[0][1] if t < points[0][0] else points[-1][1]


@dataclass
class Plan:
    """The per-step conditions of a forecast, with where each one comes from."""

    times: list[float]
    inputs: list[tm.Inputs]
    outdoor_basis: str                       # forecast | measured_held | none
    sky_basis: str                           # forecast | measured_held | assumed | not_needed
    solar: list[float]                       # the direct-sun index per step (before the model's coefficient)


def plan(model: tm.ThermalModel, ctx: Context, steps: int, *, solar_basis: str,
         outdoor_offset: float = 0.0, outdoor_override: float | None = None,
         sky_override: float | None = None) -> Plan:
    """The conditions for ``steps`` 5-minute steps from now.

    Outdoor temperature: the weather forecast where it reaches, else the measured value held (``assumed``).
    Sun: computed position x window geometry x covers as they are now x the sky, consistent with how the
    model's solar coefficient was learned (``solar_basis``): a coefficient learned from geometry alone is an
    average over all weather and is used as it is; one learned with cloud coverage or measured brightness is
    combined with the forecast cloud coverage (an estimate).
    """
    times = [ctx.now + i * STEP_MIN * 60.0 for i in range(steps)]
    forecast_t = [(t, temp) for t, temp, _c in ctx.outdoor_forecast]
    forecast_c = [(t, cloud) for t, _temp, cloud in ctx.outdoor_forecast]
    outdoor_basis = "forecast" if any(v is not None for _t, v in forecast_t) else \
        "measured_held" if ctx.outdoor_c is not None else "none"
    sky_basis = "not_needed"
    inputs, solar = [], []
    used_sky: set[str] = set()
    for t in times:
        if outdoor_override is not None:
            outdoor = outdoor_override
        else:
            outdoor = _interp(forecast_t, t + STEP_MIN * 30.0) if forecast_t else None
            if outdoor is None:
                outdoor = ctx.outdoor_c
            outdoor = None if outdoor is None else outdoor + outdoor_offset
        index = 0.0
        if model.solar is not None and ctx.location is not None:
            elevation, azimuth = sun_position(t + STEP_MIN * 30.0, ctx.location[0], ctx.location[1])
            geometry = window_index(ctx.windows, elevation, azimuth, ctx.covers) if elevation > 0 else 0.0
            index = geometry or 0.0
            if index > 0 and solar_basis != "geometry":
                if sky_override is not None:
                    clear, basis = clear_fraction(sky_override), "hypothetical"
                else:
                    cloud = _interp(forecast_c, t) if forecast_c else None
                    basis = "forecast"
                    if cloud is None and ctx.cloud_now is not None:
                        cloud, basis = ctx.cloud_now, "measured_held"
                    clear = clear_fraction(cloud)
                    if clear is None:
                        clear, basis = ASSUMED_CLEAR, "assumed"
                used_sky.add(basis)
                index *= clear
        solar.append(index)
        neighbours = {}
        for area in model.coupling:
            value = ctx.neighbours.get(area)
            neighbours[area] = ctx.temp_c if ctx.barriers.get(area) else value  # a closed door: no exchange
        inputs.append(tm.Inputs(outdoor_c=outdoor, heating=0.0,
                                solar=index if model.solar is not None else None,
                                night=night_flag(t, ctx.tz) if model.night is not None else None,
                                neighbours={a: v for a, v in neighbours.items() if v is not None} or None))
    if used_sky:
        sky_basis = "assumed" if "assumed" in used_sky else "measured_held" if "measured_held" in used_sky else \
            sorted(used_sky)[0]
    return Plan(times, inputs, outdoor_basis, sky_basis, solar)


def with_heating(p: Plan, schedule: Sequence[float]) -> list[tm.Inputs]:
    return [tm.Inputs(i.outdoor_c, h, i.solar, i.night, i.neighbours) for i, h in zip(p.inputs, schedule, strict=True)]


def run(model: tm.ThermalModel, ctx: Context, p: Plan, schedule: Sequence[float], *,
        extra_loss: Sequence[float] | None = None, start_c: float | None = None) -> tm.Trajectory | tm.Unavailable:
    """Simulate a heating schedule (fractions per step). ``extra_loss``: an additional loss rate towards
    outdoors per step (1/h), for "a window is open" scenarios."""
    start = ctx.temp_c if start_c is None else start_c
    if start is None:
        return tm.Unavailable("no_temperature", "The room temperature is not known", ("temperature",))
    inputs = with_heating(p, schedule)
    if extra_loss is None or not any(extra_loss):
        return tm.simulate(model, start, inputs, step_min=STEP_MIN, heating_history=ctx.heating_history or None)
    # An open window: the model's loss towards outdoors is temporarily larger. Simulated by shifting the
    # reference model's equilibrium step by step (same exact step, larger B), never stored as a learned term.
    temps, low, high = [start], [start], [start]
    u_f = ctx.heating_history[-1] if ctx.heating_history else (schedule[0] if schedule else 0.0)
    lag = int(round(model.lag_min / STEP_MIN))
    past = list(ctx.heating_history)
    temp = start
    dt_h = STEP_MIN / 60.0
    for i, inp in enumerate(inputs):
        j = i - lag
        delayed = schedule[j] if j >= 0 else (past[len(past) + j] if len(past) + j >= 0 else (schedule[0] if schedule else 0.0))
        w = extra_loss[i] if i < len(extra_loss) else 0.0
        if w and inp.outdoor_c is not None:
            A, B, _m = tm.rates(model, tm.Inputs(inp.outdoor_c, None, inp.solar, inp.night, inp.neighbours),
                                tm.emitter_mean(delayed, u_f, model.tau_min, dt_h))
            A, B = A + w * inp.outdoor_c, B + w
            eq = A / B if B > 1e-9 else temp
            temp = eq + (temp - eq) * math.exp(-B * dt_h)
            u_f = delayed + (u_f - delayed) * math.exp(-dt_h * 60.0 / model.tau_min) if model.tau_min > 0 else delayed
        else:
            temp, u_f, _m = tm.step(model, temp, u_f, tm.Inputs(inp.outdoor_c, delayed, inp.solar, inp.night,
                                                                inp.neighbours), dt_h)
        width, _beyond = tm._band(model, (i + 1) * STEP_MIN)  # noqa: SLF001 - the same band as the simulator
        temps.append(temp)
        low.append(temp - width)
        high.append(temp + width)
    return tm.Trajectory(start, STEP_MIN, tuple(temps), tuple(low), tuple(high),
                         (*model.warnings, "Hypothetical open window: modelled as extra loss towards outdoors"),
                         kind="hypothetical")


def thermostat_schedule(model: tm.ThermalModel, ctx: Context, p: Plan, heat: float, setpoint: float) -> list[float]:
    """The heating "as now", as a thermostat runs it: ``heat`` while the room (as the model predicts it) is below
    the setpoint, off above it. An approximation: the thermostat regulates on its own measurement, which can
    differ from the room sensor's."""
    schedule: list[float] = []
    lag = int(round(model.lag_min / STEP_MIN))
    past = list(ctx.heating_history)
    u_f = past[-1] if past else heat
    temp = ctx.temp_c if ctx.temp_c is not None else setpoint
    dt_h = STEP_MIN / 60.0
    for i, inp in enumerate(p.inputs):
        schedule.append(heat if temp < setpoint else 0.0)
        j = i - lag
        delayed = schedule[j] if j >= 0 else (past[len(past) + j] if len(past) + j >= 0 else schedule[0])
        temp, u_f, _m = tm.step(model, temp, u_f, tm.Inputs(inp.outdoor_c, delayed, inp.solar, inp.night,
                                                            inp.neighbours), dt_h)
    return schedule


# ------------------------------------------------------------- the decisions
def _reach_index(traj: tm.Trajectory, target: float, *, lower: bool = False) -> int | None:
    series = traj.low if lower else traj.temps
    for i, v in enumerate(series):
        if v == v and v >= target - REACHED_C:
            return i
    return None


def next_requirement(targets: Sequence[float | None], temps: Sequence[float]) -> tuple[int, float] | None:
    """The next step at which the room has to be warm: the target rises above what it would be without heating.

    Returns ``(step index, target)`` - index 0 when the room is below its target already now.
    """
    for i, target in enumerate(targets):
        if target is None or i >= len(temps):
            continue
        rising = i == 0 or targets[i - 1] is None or (targets[i - 1] or 0) < target
        if (rising or i == 0) and temps[i] < target - REACHED_C:
            return i, target
        if i > 0 and rising and temps[i] >= target - REACHED_C:
            continue
    return None


def latest_start(model: tm.ThermalModel, ctx: Context, p: Plan, index: int, target: float, *, lower: bool
                 ) -> int | None:
    """The latest step to switch the heating fully on so that the room is at ``target`` at step ``index``
    (``lower``: by the lower edge of the uncertainty band). None when even starting now is not enough."""
    def ok(start: int) -> bool:
        schedule = [0.0] * start + [1.0] * (len(p.inputs) - start)
        traj = run(model, ctx, p, schedule)
        if isinstance(traj, tm.Unavailable):
            return False
        series = traj.low if lower else traj.temps
        value = series[index]
        return value == value and value >= target - REACHED_C

    if not ok(0):
        return None
    lo, hi = 0, index  # ok(lo) is True; find the largest start that is still ok
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None or value != value else round(value, digits)


def rates_now(model: tm.ThermalModel, ctx: Context, p: Plan) -> dict[str, float | None]:
    """Heating and cooling rates at the room's present temperature and conditions (°C/h)."""
    if ctx.temp_c is None or not p.inputs:
        return {"heating_rate": None, "cooling_rate": None}
    first = p.inputs[0]
    passive_a, passive_b, _m = tm.rates(model, first, 0.0)
    cooling = passive_a - passive_b * ctx.temp_c
    heating = None
    if model.heating is not None:
        heat_a, heat_b, _m = tm.rates(model, first, 1.0)
        heating = heat_a - heat_b * ctx.temp_c
    return {"heating_rate": _r(heating), "cooling_rate": _r(cooling)}


def predict(model: tm.ThermalModel, ctx: Context, *, horizon_h: int, solar_basis: str,
            recommend_from: str = "moderate") -> dict[str, Any]:
    """Everything a room publishes and shows. Never raises for missing inputs: it says what is missing."""
    steps = int(horizon_h * 60 / STEP_MIN)
    out: dict[str, Any] = {"available": False, "computed_at": _iso(ctx.now), "horizon_h": horizon_h,
                           "confidence": model.confidence, "reliable": LEVELS.get(model.confidence, 0) >= LEVELS[recommend_from],
                           "warnings": list(model.warnings), "explanation": []}
    if ctx.temp_c is None:
        out.update(reason="The room temperature is not known right now (sensor unavailable or stale)",
                   code="no_temperature")
        return out
    p = plan(model, ctx, steps, solar_basis=solar_basis)
    if model.reference == "outdoor" and any(i.outdoor_c is None for i in p.inputs):
        out.update(reason="The outdoor temperature is not known (no measurement and no forecast)", code="no_outdoor")
        return out
    targets = list(ctx.targets[: steps + 1]) + [ctx.targets[-1] if ctx.targets else None] * max(0, steps + 1 - len(ctx.targets))
    now_heat = ctx.heating_now if ctx.heating_now is not None else 0.0
    passive = run(model, ctx, p, [0.0] * steps)
    as_now = [now_heat] * steps
    course = "off" if now_heat <= 0 else "held"
    if now_heat > 0 and ctx.setpoint_c is not None:
        as_now = thermostat_schedule(model, ctx, p, now_heat, ctx.setpoint_c)
        course = "until_setpoint"
    expected = run(model, ctx, p, as_now)
    if isinstance(passive, tm.Unavailable) or isinstance(expected, tm.Unavailable):
        bad = passive if isinstance(passive, tm.Unavailable) else expected
        out.update(reason=bad.text, code=bad.code)
        return out
    no_sun = None
    if model.solar is not None and any(p.solar):
        no_sun_plan = Plan(p.times, [tm.Inputs(i.outdoor_c, 0.0, 0.0, i.night, i.neighbours) for i in p.inputs],
                           p.outdoor_basis, p.sky_basis, [0.0] * steps)
        no_sun = run(model, ctx, no_sun_plan, [0.0] * steps)
    per_hour = int(60 / STEP_MIN)
    hourly = [{"t": _iso(ctx.now + h * 3600), "expected": _r(expected.temps[h * per_hour]),
               "passive": _r(passive.temps[h * per_hour]), "low": _r(expected.low[h * per_hour]),
               "high": _r(expected.high[h * per_hour]), "target": targets[h * per_hour]}
              for h in range(horizon_h + 1)]
    solar_gain = None
    if no_sun is not None and not isinstance(no_sun, tm.Unavailable):
        solar_gain = max(a - b for a, b in zip(passive.temps, no_sun.temps, strict=True))
    rates = rates_now(model, ctx, p)
    out.update(available=True, code="ok", hourly=hourly, steps={
        "times": [_iso(t) for t in [*p.times, ctx.now + steps * STEP_MIN * 60]][:: 3],
        "expected": [_r(v) for v in expected.temps][:: 3], "passive": [_r(v) for v in passive.temps][:: 3],
        "low": [_r(v) for v in expected.low][:: 3], "high": [_r(v) for v in expected.high][:: 3],
        "target": targets[:: 3]},
        predicted_1h=_r(expected.temps[per_hour]), predicted_1h_band=[_r(expected.low[per_hour]), _r(expected.high[per_hour])],
        passive_end=_r(passive.temps[-1]), solar_gain_c=_r(solar_gain, 1), **rates,
        inputs={"temperature": "measured", "outdoor": p.outdoor_basis, "sky": p.sky_basis,
                "covers": "measured_held" if ctx.covers else "none", "heating_now": "measured" if ctx.heating_now is not None else "assumed_off",
                "neighbours": "measured_held" if model.coupling else "none", "heating_course": course,
                "setpoint_c": ctx.setpoint_c if course == "until_setpoint" else None},
        warnings=list(dict.fromkeys([*model.warnings, *expected.warnings, *(
            ["The thermostats report no setpoint: the heating is assumed to continue as now for the whole horizon"]
            if course == "held" else [])])))
    if p.outdoor_basis == "measured_held":
        out["warnings"].append("No outdoor temperature forecast: the present outdoor temperature is assumed to hold")
    if p.sky_basis == "assumed":
        out["warnings"].append("Nothing is known about clouds: half of the clear-sky sunshine is assumed")

    # --- target, requirement, warm-up and preheat
    target_now = targets[0]
    requirement = next_requirement(targets, passive.temps)
    out["target_now"] = target_now
    explanation = out["explanation"]
    if requirement is None:
        upcoming = [(i, t) for i, t in enumerate(targets) if t is not None]
        out.update(preheat_needed=False, preheat_start=None, preheat_start_expected=None, requirement=None,
                   recommended_target=target_now, warmup_min=0 if target_now is not None and ctx.temp_c >= target_now - REACHED_C else None,
                   state="not_needed")
        if upcoming:
            explanation.append(f"Without heating the room is predicted to stay at or above its comfort target for the "
                               f"next {horizon_h} hours.")
            if solar_gain and solar_gain >= 0.3:
                explanation.append(f"The sun is expected to add up to {solar_gain:.1f} °C (an estimate).")
        else:
            explanation.append(f"No comfort target applies in the next {horizon_h} hours.")
        return out
    index, target = requirement
    out["requirement"] = {"t": _iso(p.times[index] if index < len(p.times) else ctx.now + index * STEP_MIN * 60),
                          "target_c": target, "in_min": int(index * STEP_MIN),
                          "passive_c": _r(passive.temps[index])}
    out["recommended_target"] = target
    if model.heating is None:
        out.update(preheat_needed=None, preheat_start=None, preheat_start_expected=None, warmup_min=None,
                   state="unknown_heating")
        explanation.append("The room's heating response has not been learned, so no warm-up time can be computed.")
        return out
    full = run(model, ctx, p, [1.0] * steps)
    reached = _reach_index(full, target) if not isinstance(full, tm.Unavailable) else None
    out["warmup_min"] = None if reached is None else int(reached * STEP_MIN)
    expected_start = latest_start(model, ctx, p, index, target, lower=False)
    cautious = latest_start(model, ctx, p, index, target, lower=True) if any(v == v for v in passive.low[1:]) else None
    if expected_start is None:
        out.update(preheat_needed=True, preheat_start=_iso(ctx.now), preheat_start_expected=_iso(ctx.now), state="late")
        late = f" it is expected to get there after about {reached * STEP_MIN / 60:.1f} h" if reached else \
            f" it is not expected to get there within {horizon_h} h"
        explanation.append(f"Even with full heating from now the room does not reach {target:g} °C in time;{late}.")
        return out
    start = expected_start
    if cautious is not None:
        start = max(cautious, expected_start - int(MAX_CAUTION_MIN / STEP_MIN))
    start_t = ctx.now + start * STEP_MIN * 60
    out.update(preheat_needed=True, preheat_start=_iso(start_t),
               preheat_start_expected=_iso(ctx.now + expected_start * STEP_MIN * 60),
               state="preheat_now" if start == 0 else "preheat_later")
    if index == 0:
        out["state"] = "below_target"
        explanation.append(f"The room is below its target of {target:g} °C now.")
    else:
        explanation.append(
            f"To be at {target:g} °C at {datetime.fromtimestamp(p.times[index] if index < len(p.times) else start_t, UTC).astimezone(ctx.tz):%H:%M}, "
            f"heating should start about {int((index - start) * STEP_MIN)} minutes before"
            + (f" (the expected need is {int((index - expected_start) * STEP_MIN)} minutes; the rest is margin for the "
               "model's uncertainty)" if start != expected_start else "") + ".")
    if solar_gain and solar_gain >= 0.3:
        explanation.append(f"Sunshine is expected to add up to {solar_gain:.1f} °C (an estimate); this already "
                           "shortens the heating time.")
    if rates.get("heating_rate") is not None:
        explanation.append(f"Learned: with full heating the room now warms by about {rates['heating_rate']:.1f} °C/h, "
                           f"without it it changes by {rates['cooling_rate']:+.1f} °C/h.")
    return out


# ------------------------------------------------------------ derived traits
def characteristics(model: tm.ThermalModel, *, location: tuple[float, float] | None = None) -> dict[str, Any]:
    """What the model says about the room in plain numbers (all ``model`` values, under stated conditions)."""
    out: dict[str, Any] = {"time_constant_h": _r(model.time_constant_h, 1), "lag_min": model.lag_min,
                           "emitter_min": model.tau_min}
    if model.heating is not None:
        warm = []
        for outdoor in (-5.0, 5.0, 15.0):
            for start_c in (16.0, 18.0, 20.0):
                change = tm.estimate_change(model, start_c, 1.0, outdoor_c=outdoor, heating=1.0,
                                            solar=0.0 if model.solar is not None else None, night=False)
                if isinstance(change, dict):
                    warm.append({"outdoor_c": outdoor, "start_c": start_c, "rise_per_h": change["change_c"]})
        out["warming"] = warm
        # Stored heat: how much the room keeps rising after the heating stops (the emitter's own inertia).
        out["afterheat_c"] = _r(model.heating * model.tau_min / 60.0, 2) if model.tau_min else 0.0
    cool = []
    for outdoor in (-5.0, 5.0, 15.0):
        change = tm.estimate_change(model, 20.0, 1.0, outdoor_c=outdoor, heating=0.0,
                                    solar=0.0 if model.solar is not None else None, night=False)
        if isinstance(change, dict):
            cool.append({"outdoor_c": outdoor, "start_c": 20.0, "change_per_h": change["change_c"]})
    out["cooling"] = cool
    if model.solar is not None:
        out["solar_c_per_h_full_sun"] = _r(model.solar, 2)
    if model.night is not None:
        out["night_c_per_h"] = _r(model.night, 2)
    out["coupling"] = {a: _r(c, 3) for a, c in model.coupling.items()}
    return out


def window_loss(model: tm.ThermalModel, grid: Any) -> dict[str, Any]:
    """The extra loss rate (1/h towards outdoors) while a window of the room is open, from the room's own data.

    Training leaves open-window time out; here the model's prediction for those 30-minute blocks (heating off
    for the two hours before, outdoor temperature known) is compared with what happened. ``available`` only with
    at least three openings and two hours of such time; otherwise the simulator uses ``ASSUMED_WINDOW_LOSS``.
    """
    if model.reference != "outdoor":
        return {"available": False, "reason": "Needs an outdoor temperature source"}
    times, temps, window = grid.times, grid.temp, grid.window
    heat = grid.heat
    num = den = 0.0
    blocks = 0
    openings = 0
    previous_open = False
    block = 6
    i = 0
    while i + block < len(times):
        is_open = window[i] == "open"
        if is_open and not previous_open:
            openings += 1
        previous_open = is_open
        span = range(i, i + block + 1)
        if all(window[j] == "open" for j in span) and all(temps[j] is not None for j in span) \
                and all((heat[j] or 0.0) == 0.0 and heat[j] is not None for j in range(max(0, i - 24), i + block)) \
                and all(grid.outdoor[j] is not None for j in span):
            mean_t = sum(temps[j] for j in span) / len(span)
            outdoor = sum(grid.outdoor[j] for j in span) / len(span)
            solar = None
            if grid.solar is not None:
                values = [grid.solar[j] for j in span]
                solar = None if any(v is None for v in values) else sum(values) / len(values)
            inp = tm.Inputs(outdoor, 0.0, solar if model.solar is not None else None,
                            bool(grid.night[i]) if model.night is not None else None,
                            {a: v[i] for a, v in grid.neighbours.items() if v[i] is not None} or None)
            A, B, missing = tm.rates(model, inp, 0.0)
            if not missing:
                observed = (temps[i + block] - temps[i]) / (block * STEP_MIN / 60.0)
                residual = observed - (A - B * mean_t)
                diff = mean_t - outdoor
                if diff > 2.0:
                    num += -residual * diff
                    den += diff * diff
                    blocks += 1
            i += block
            continue
        i += 1
    hours = blocks * block * STEP_MIN / 60.0
    if openings < 3 or hours < 2.0 or den <= 0:
        return {"available": False, "hours": round(hours, 1), "openings": openings,
                "reason": f"{hours:.1f} h of usable open-window time in {openings} openings; needs 2 h in 3"}
    value = max(0.0, num / den)
    return {"available": True, "value_per_h": round(value, 3), "hours": round(hours, 1), "openings": openings}


def horizon_steps(hours: float) -> int:
    return int(round(hours * 60 / STEP_MIN))


def local_day_start(t: float, tz: Any) -> float:
    local = datetime.fromtimestamp(t, UTC).astimezone(tz)
    return (local - timedelta(hours=local.hour, minutes=local.minute, seconds=local.second,
                              microseconds=local.microsecond)).timestamp()
