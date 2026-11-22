"""Held-out validation of learning + prediction on synthetic rooms with a known truth.

Each scenario simulates a room minute by minute (``thermal_sim``), stores what Home Assistant would have
reported, lets the App learn from the first ``train_days`` only, and then, on the following ``test_days``, which
the learner never saw:

* forecasts the temperature every 3 hours for 1, 3, 6 and 24 hours ahead, with the heating that really happened
  and (a) the true outdoor temperature (a perfect forecast) and (b) an outdoor forecast 2 °C too warm;
* compares the error with persistence ("it stays as it is") and checks how often the truth lies inside the
  published 80 % band;
* asks for the preheat start for 21 °C at 07:00 from the state at 03:00, follows that recommendation in the
  *true* room and records whether 07:00 was reached, and how far the start was from the true latest start.

``python validation.py`` prints the table that is in the documentation; ``test_validation.py`` checks the
properties that must hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
from typing import Any

HERE = Path(__file__).resolve().parent
for path in (HERE.parent / "backend", HERE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from spc import forecast as F, thermal_model as tm  # noqa: E402
from spc.fit import learn  # noqa: E402
from spc.prep import RoomSpec, StoreView, WindowSpec, prepare_room, window_index  # noqa: E402
from spc.solar import clear_fraction, position  # noqa: E402
from thermal_sim import LOCATION, Scenario, Sim, Truth, truth_run  # noqa: E402

HORIZONS_H = (1, 3, 6, 24)


def scenarios() -> dict[str, Scenario]:
    start = datetime(2026, 1, 5, tzinfo=UTC).timestamp()
    return {
        "first-order room (model matches the truth)": Scenario(start=start, days=35, seed=11),
        "south window, covers closed at night": Scenario(start=start, days=35, seed=12, window_bearing=180.0,
                                                         cover_closed_at_night=True, truth=Truth(solar=2.0)),
        "south window, clouds reported by a weather entity": Scenario(
            start=start, days=35, seed=15, window_bearing=180.0, clouds=True, report_clouds=True, truth=Truth(solar=2.5)),
        "two thermal masses, moderate (model simpler than the truth)": Scenario(
            start=start, days=35, seed=13, truth=Truth(mass_c=0.15, mass_ratio=5.0)),
        "two thermal masses, strong (model simpler than the truth)": Scenario(
            start=start, days=35, seed=16, truth=Truth(mass_c=0.4, mass_ratio=10.0)),
        "open-plan neighbour": Scenario(start=start, days=35, seed=14, neighbour=True, truth=Truth(coupling=0.25)),
    }


def spec_for(sim: Sim, sc: Scenario) -> RoomSpec:
    ids = sim.ids
    weather = ids["weather"] if sc.report_clouds else None
    return RoomSpec("kamar", ids["room"], [], [ids["trv"]], [], ids["outdoor"] if sc.outdoor else None,
                    own_thermostats=[ids["trv"]], location=LOCATION, weather=weather, stale_s=3 * 3600.0,
                    windows=[WindowSpec("w", sc.window_bearing, layers=[(ids["cover"], "blackout")])]
                    if sc.window_bearing is not None else [])


def steps_inputs(sim: Sim, sc: Scenario, model: tm.ThermalModel, start_min: int, hours: int, *,
                 outdoor_bias: float = 0.0, solar_basis: str = "geometry") -> tuple[list[tm.Inputs], list[float]]:
    """Per 5-minute step: the conditions that really happened (with an optional outdoor forecast error)."""
    inputs, heat = [], []
    window = [WindowSpec("w", sc.window_bearing)] if sc.window_bearing is not None else []
    for k in range(hours * 12):
        a = start_min + k * 5
        minutes = range(a, a + 5)
        heat.append(sum(sim.heat_series[m] for m in minutes) / 5)
        t = sc.start + (a + 2.5) * 60
        out = sum(sim.outdoor_series[m] for m in minutes) / 5 + outdoor_bias
        hour = ((t / 3600.0) + sc.tz_offset_h) % 24
        el, az = position(t, *LOCATION)
        s = 0.0
        if window and model.solar is not None:
            cover = 0.0 if sc.cover_closed_at_night and (hour >= 21 or hour < 9) else 100.0
            s = window_index([WindowSpec("w", sc.window_bearing, layers=[("c", "blackout")])], el, az, {"c": cover}) or 0.0
            if solar_basis == "geometry+cloud":
                # what the weather entity reported (the same cloud coverage the sim reported), as a forecast would
                reported = round(100 * max(0.0, (1 - sim.cloud_series[a]) / 0.75) ** (1 / 3.4)) if sc.clouds else 0
                s *= clear_fraction(reported)
        neighbours = None
        if model.coupling:
            neighbours = {"stova": sim.neigh_series[a]}
        inputs.append(tm.Inputs(out if model.reference == "outdoor" else None, heat[-1],
                                s if model.solar is not None else None,
                                (hour >= 22 or hour < 6) if model.night is not None else None, neighbours))
    return inputs, heat


def evaluate(name: str, sc: Scenario, *, train_days: int = 28) -> dict[str, Any]:
    sim = Sim(sc).run()
    with tempfile.TemporaryDirectory() as tmp:
        state = sim.write(Path(tmp))
        view = StoreView(Path(tmp), state["coverage"], state["gaps"], state.get("aliases", {}))
        split = sc.start + train_days * 86400
        grid = prepare_room(spec_for(sim, sc), view, sc.start, split, tz="UTC")
        if sc.neighbour:
            neigh = RoomSpec("stova", sim.ids["neighbour"], [], [], [], sim.ids["outdoor"])
            grid.neighbours = {"stova": prepare_room(neigh, view, sc.start, split, tz="UTC").temp}
        learned = learn(grid, min_history_days=14, now=split)
    if learned.doc is None:
        return {"scenario": name, "model": None, "reason": learned.diagnostic.get("reason")}
    doc = learned.doc
    model = tm.load_model(doc, allow_preliminary=True)
    if not isinstance(model, tm.ThermalModel):
        return {"scenario": name, "model": {"status": doc["status"], "confidence": doc["confidence"]["level"]},
                "unusable": model.text, "explanations": doc.get("explanations", [])[:4]}
    solar_basis = grid.solar_basis
    errors: dict[str, dict[int, list[float]]] = {"perfect": {h: [] for h in HORIZONS_H},
                                                 "forecast_+2C": {h: [] for h in HORIZONS_H},
                                                 "persistence": {h: [] for h in HORIZONS_H}}
    inside = {h: 0 for h in HORIZONS_H}
    count = {h: 0 for h in HORIZONS_H}
    test_start = train_days * 1440
    total = len(sim.state_series)
    origin = test_start
    while origin + 24 * 60 < total:
        start_temp = sim.state_series[origin][0]
        past = sim.heat_series[max(0, origin - 120):origin:5]
        for label, bias in (("perfect", 0.0), ("forecast_+2C", 2.0)):
            inputs, _heat = steps_inputs(sim, sc, model, origin, 24, outdoor_bias=bias, solar_basis=solar_basis)
            traj = tm.simulate(model, start_temp, inputs, heating_history=past)
            if isinstance(traj, tm.Unavailable):
                continue
            for h in HORIZONS_H:
                truth = sim.state_series[origin + h * 60][0]
                errors[label][h].append(traj.temps[h * 12] - truth)
                if label == "perfect":
                    errors["persistence"][h].append(start_temp - truth)
                    lo, hi = traj.low[h * 12], traj.high[h * 12]
                    if lo == lo:
                        count[h] += 1
                        inside[h] += 1 if lo <= truth <= hi else 0
        origin += 180
    def mae(xs: list[float]) -> float | None:
        return round(statistics.fmean(abs(x) for x in xs), 3) if xs else None

    table = {label: {h: {"mae": mae(v), "bias": round(statistics.fmean(v), 3) if v else None} for h, v in per.items()}
             for label, per in errors.items()}
    band = {h: round(inside[h] / count[h], 2) if count[h] else None for h in HORIZONS_H}
    preheat = _preheat(sim, sc, model, doc, test_start, solar_basis)
    return {"scenario": name, "model": {"status": doc["status"], "confidence": doc["confidence"]["level"],
                                        "parameters": _params(model, sc)},
            "errors": table, "band80_coverage": band, "preheat": preheat,
            "learner_validation": {str(k): v for k, v in (doc.get("validation") or {}).get("horizons", {}).items()}
            if isinstance((doc.get("validation") or {}).get("horizons"), dict) else None}


def _params(model: tm.ThermalModel, sc: Scenario) -> dict[str, Any]:
    tr = sc.truth
    return {"k": [round(model.k, 3), tr.k], "heating": [None if model.heating is None else round(model.heating, 2), tr.b],
            "solar": [None if model.solar is None else round(model.solar, 2), tr.solar],
            "coupling": [round(sum(model.coupling.values()), 3), tr.coupling if sc.neighbour else 0.0],
            "lag_tau_min": [[model.lag_min, model.tau_min], [tr.lag_min, tr.tau_min]]}


def _preheat(sim: Sim, sc: Scenario, model: tm.ThermalModel, doc: dict[str, Any], test_start: int,
             solar_basis: str = "geometry") -> dict[str, Any]:
    """From 03:00 on each test day: the recommended start for 21 °C at 07:00, followed in the true room."""
    if model.heating is None:
        return {"available": False}
    results = []
    tz = UTC
    day = test_start // 1440
    while (day + 1) * 1440 < len(sim.state_series):
        origin = day * 1440 + 3 * 60
        day += 1
        need = 4 * 60
        start_temp = sim.state_series[origin][0]
        t0 = sc.start + origin * 60
        forecast = [(t0 + h * 3600, sim.outdoor_series[min(origin + h * 60, len(sim.outdoor_series) - 1)], None)
                    for h in range(0, 30)]
        targets = [None] * 48 + [21.0] * (24 * 12 - 48 + 1)
        ctx = F.Context(now=t0, tz=tz, temp_c=start_temp, outdoor_c=sim.outdoor_series[origin], outdoor_forecast=forecast,
                        heating_now=sim.heat_series[origin], heating_history=sim.heat_series[origin - 120:origin:5],
                        location=LOCATION, windows=[WindowSpec("w", sc.window_bearing)] if sc.window_bearing is not None else [],
                        targets=targets)
        if model.coupling:
            ctx.neighbours = {"stova": sim.neigh_series[origin]}
        if sc.report_clouds:
            ctx.outdoor_forecast = [(t, temp, round(100 * max(0.0, (1 - sim.cloud_series[min(origin + h * 60, len(sim.cloud_series) - 1)]) / 0.75) ** (1 / 3.4)))
                                    for h, (t, temp, _c) in enumerate(forecast)]
        out = F.predict(model, ctx, horizon_h=24, solar_basis=solar_basis)
        if not out.get("available") or out.get("state") in ("not_needed", None):
            # The truth: does the room reach 21 °C at 07:00 without heating?
            free = truth_run(sim, origin, [0.0] * need)[-1]
            results.append({"recommended": None, "reached_without": free >= 20.9, "free_c": round(free, 2)})
            continue
        rec = (datetime.fromisoformat(out["preheat_start"]).timestamp() - t0) / 60
        expected = (datetime.fromisoformat(out["preheat_start_expected"]).timestamp() - t0) / 60
        def at_seven(start_min: float) -> float:
            s = int(round(start_min))
            return truth_run(sim, origin, [0.0] * s + [1.0] * (need - s))[-1]
        lo, hi = 0, need
        if at_seven(0) < 20.9:
            true_latest = None
        else:
            while lo < hi:
                mid = (lo + hi + 1) // 2
                lo, hi = (mid, hi) if at_seven(mid) >= 20.9 else (lo, mid - 1)
            true_latest = lo
        followed = at_seven(rec)
        results.append({"recommended": round(rec), "expected": round(expected), "true_latest": true_latest,
                        "reached": followed >= 20.9 - 0.2, "temp_at_target": round(followed, 2),
                        "error_min": None if true_latest is None else round(expected - true_latest)})
    usable = [r for r in results if r.get("recommended") is not None]
    errs = [r["error_min"] for r in usable if r.get("error_min") is not None]
    return {"days": len(results), "recommended_days": len(usable),
            "reached_share": round(sum(r["reached"] for r in usable) / len(usable), 2) if usable else None,
            "mean_abs_start_error_min": round(statistics.fmean(abs(e) for e in errs)) if errs else None,
            "mean_start_error_min": round(statistics.fmean(errs)) if errs else None,
            "no_preheat_days": [r for r in results if r.get("recommended") is None]}


def main() -> None:
    only = sys.argv[1:]
    rows = []
    for name, sc in scenarios().items():
        if only and not any(o in name for o in only):
            continue
        rows.append(evaluate(name, sc))
        print(json.dumps(rows[-1], default=str), file=sys.stderr, flush=True)
    print(json.dumps(rows, indent=1, default=str))


if __name__ == "__main__":
    main()
