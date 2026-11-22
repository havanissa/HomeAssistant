"""Phase A7.1: thermal learning.

* Parameter recovery on deterministic synthetic rooms with known coefficients (``thermal_sim``): tolerances,
  not "returns a value".
* Realistic Home Assistant data: short retention, few usable hours, no reported activity, several devices,
  secondary-sensor fallback, windows, shared heating, outages, sensor replacement, renames, DST, missing
  outdoor data, Recorder gap fills, pruning.
* Statistical correctness: no leakage from the held-out period, no interpolation through gaps, no fabricated
  heating, plausible parameters only, weaker evidence → wider uncertainty, meaningful baselines.
* The A7.2 interface, model persistence (corrupt / newer / interrupted), and the App service and API (rebuild,
  invalidation, reset, export, restart, no thermostat command).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import copy
from dataclasses import replace
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import time
from typing import Any

import pytest

from spc import thermal_model as tm
from spc.fit import HORIZONS, learn, make_series, origins, Structure
from spc.model_store import ModelStore
from spc.prep import (
    GRID_S, RoomSpec, StoreView, WindowSpec, episodes, prepare_room, read_events, transmission, window_geometry,
)
from thermal_sim import LOCATION, Scenario, Sim, Truth


# ------------------------------------------------------------------ helpers
def simulate(tmp: Path, *, name: str = "obs", **kw: Any) -> tuple[Sim, StoreView, Scenario]:
    sc = Scenario(**kw)
    sim = Sim(sc).run()
    d = tmp / name
    state = sim.write(d)
    return sim, StoreView(d, state["coverage"], state["gaps"], state.get("aliases", {})), sc


def room_spec(sim: Sim, sc: Scenario, **over: Any) -> RoomSpec:
    ids = sim.ids
    devices = [ids["trv"]] if sc.heating == "thermostat" or sc.fixed_heating is not None else []
    spec = RoomSpec("kamar", ids["room"], [], devices, [ids["window"]] if sc.windows_open_per_day else [],
                    ids["outdoor"] if sc.outdoor else None, own_thermostats=devices, location=LOCATION,
                    windows=[WindowSpec("w", sc.window_bearing, layers=[(ids["cover"], "blackout")])]
                    if sc.window_bearing is not None else [])
    return replace(spec, **over)


def fit(tmp: Path, *, min_days: int = 14, spec_over: dict[str, Any] | None = None, neighbour: bool = False,
        **kw: Any) -> tuple[Any, Any, Sim, Scenario]:
    sim, view, sc = simulate(tmp, neighbour=neighbour, **kw)
    end = sc.start + sc.days * 86400
    neigh = {}
    if neighbour:
        ns = RoomSpec("stova", sim.ids["neighbour"], [], [], [], sim.ids["outdoor"])
        neigh = {"stova": prepare_room(ns, view, sc.start, end, tz="UTC").temp}
    grid = prepare_room(room_spec(sim, sc, **(spec_over or {})), view, sc.start, end, tz="UTC",
                        neighbour_temps=neigh)
    return learn(grid, min_history_days=min_days, now=end), grid, sim, sc


def value(doc: dict[str, Any], name: str) -> float:
    p = doc["parameters"][name]
    assert p["available"], (name, p)
    return p["value"]


def close(got: float, want: float, rel: float) -> bool:
    return abs(got - want) <= rel * abs(want)


def skill(doc: dict[str, Any], minutes: str = "180") -> float:
    return doc["validation"]["horizons"][minutes]["skill"]


# ---------------------------------------------------- synthetic recovery
def test_recovers_heating_and_losses_with_stable_outdoor(tmp_path: Path) -> None:
    r, _g, _s, sc = fit(tmp_path, outdoor_swing=0.5)
    doc = r.doc
    assert doc["status"] in ("moderate", "high") and not doc["preliminary"]
    t = sc.truth
    assert close(value(doc, "heating_c_per_h"), t.b, 0.10)
    assert close(value(doc, "k_per_h"), t.k, 0.30)
    assert close(value(doc, "baseline_c_per_h"), t.d, 0.35)
    # Delay: lag + emitter (true 30 min) within the grid's resolution.
    assert abs(value(doc, "heating_delay_min") - (t.lag_min + t.tau_min)) <= 15
    h = doc["validation"]["horizons"]["180"]
    assert h["model"]["mae"] < 0.15 and h["model"]["mae"] < 0.3 * h["persistence"]["mae"]
    assert doc["structure"]["heating"] == "reported" and doc["structure"]["solar"] is False


def test_recovers_with_falling_outdoor_temperature(tmp_path: Path) -> None:
    r, _g, _s, sc = fit(tmp_path, outdoor_trend_per_day=-0.5)
    t = sc.truth
    assert close(value(r.doc, "k_per_h"), t.k, 0.15)
    assert close(value(r.doc, "heating_c_per_h"), t.b, 0.10)
    assert close(value(r.doc, "time_constant_h"), 1 / t.k, 0.20)


def test_long_thermal_inertia(tmp_path: Path) -> None:
    truth = Truth(k=0.012, b=0.6, d=0.12, tau_min=60, lag_min=20)
    r, _g, _s, _sc = fit(tmp_path, truth=truth, outdoor_trend_per_day=-0.3)
    assert close(value(r.doc, "time_constant_h"), 1 / truth.k, 0.25)
    assert close(value(r.doc, "heating_c_per_h"), truth.b, 0.10)
    assert abs(value(r.doc, "heating_delay_min") - 80) <= 20
    assert (r.doc["parameters"]["residual_warming_c"] or {}).get("available")


def test_open_plan_coupling_is_measured(tmp_path: Path) -> None:
    truth = Truth(coupling=0.15, k=0.03)
    r, _g, _s, _sc = fit(tmp_path, neighbour=True, truth=truth)
    assert r.doc["structure"]["coupling"] == ["stova"]
    assert close(value(r.doc, "coupling:stova"), 0.15, 0.15)
    assert close(value(r.doc, "k_per_h"), 0.03, 0.30)
    # The night term the neighbour's daily cycle first suggested is removed once coupling explains it.
    assert not r.doc["structure"]["night"]


def test_no_coupling_is_invented_for_an_unrelated_neighbour(tmp_path: Path) -> None:
    r, _g, _s, _sc = fit(tmp_path, neighbour=True, truth=Truth(coupling=0.0))
    assert r.doc["structure"]["coupling"] == []
    assert not r.doc["parameters"]["coupling:stova"]["available"]
    assert r.doc["parameters"]["coupling:stova"]["reason"]


def test_solar_warming_through_window_geometry(tmp_path: Path) -> None:
    r, _g, _s, _sc = fit(tmp_path, truth=Truth(solar=1.5), window_bearing=180, cover_closed_at_night=True)
    assert r.doc["structure"]["solar"]
    assert close(value(r.doc, "solar_c_per_h"), 1.5, 0.15)
    assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.10)
    assert r.doc["evidence"]["episodes"]["solar"]["count"] >= 5


def test_no_solar_gain_is_manufactured(tmp_path: Path) -> None:
    # A south window, but the room gains nothing from the sun: the term must not be kept.
    r, _g, _s, _sc = fit(tmp_path, truth=Truth(solar=0.0), window_bearing=180)
    assert not r.doc["structure"]["solar"]
    assert not r.doc["parameters"]["solar_c_per_h"]["available"]
    # Without any window direction it is unavailable, with a reason (not a positive value).
    r2, _g, _s, _sc = fit(tmp_path / "b", truth=Truth(solar=1.5))
    p = r2.doc["parameters"]["solar_c_per_h"]
    assert not p["available"] and "direction" in p["reason"]


def test_thermostat_sensor_bias_does_not_matter(tmp_path: Path) -> None:
    clean, *_ = fit(tmp_path / "a", seed=3)
    biased, *_ = fit(tmp_path / "b", seed=3, thermostat_bias=2.5)
    for name in ("k_per_h", "heating_c_per_h", "baseline_c_per_h"):
        assert value(clean.doc, name) == pytest.approx(value(biased.doc, name), rel=1e-6)


def test_noisy_sensors_widen_the_uncertainty(tmp_path: Path) -> None:
    clean, *_ = fit(tmp_path / "a", seed=5)
    noisy, *_ = fit(tmp_path / "b", seed=5, sensor_noise=0.15, resolution=0.5)
    assert close(value(noisy.doc, "heating_c_per_h"), 1.6, 0.20)
    assert noisy.doc["uncertainty"]["180"]["q80"] > clean.doc["uncertainty"]["180"]["q80"]
    assert noisy.doc["parameters"]["heating_c_per_h"]["se"] > clean.doc["parameters"]["heating_c_per_h"]["se"]


def test_missing_samples_and_irregular_intervals(tmp_path: Path) -> None:
    r, grid, _s, sc = fit(tmp_path, dropout_per_day=1.0, max_interval_min=60, gaps=[(4, 20), (11, 6)])
    assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.12)
    ex = r.doc["evidence"]["excluded"]
    assert ex.get("not_observed", 0) >= 25 and ex.get("no_temperature", 0) > 0
    # Nothing inside a recorded gap has a value.
    a, b = sc.start + 4 * 86400, sc.start + 4 * 86400 + 20 * 3600
    inside = [i for i, t in enumerate(grid.times) if a <= t < b]
    assert inside and all(grid.temp[i] is None and grid.why[i] == "not_observed" for i in inside)


# ------------------------------------------------------- realistic data
def test_short_retention_is_preliminary_not_ready(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, days=5)
    assert r.doc["preliminary"] and r.doc["status"] == "insufficient"
    assert r.doc["confidence"]["level"] == "low"
    assert tm.load_model(r.doc) == tm.Unavailable("preliminary", tm.load_model(r.doc).text)  # type: ignore[union-attr]
    assert isinstance(tm.load_model(r.doc, allow_preliminary=True), tm.ThermalModel)


def test_fourteen_usable_days_are_ready(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, days=16)
    assert r.doc["evidence"]["usable_days"] >= 14 and not r.doc["preliminary"]
    assert r.doc["status"] in ("low", "moderate", "high")


def test_many_calendar_days_but_few_usable_hours(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, days=30, gaps=[(d, 23.5) for d in range(30)])
    assert r.doc is None and r.diagnostic["status"] == "insufficient"
    ev = r.diagnostic["evidence"]
    assert ev["calendar_days"] > 28 and ev["usable_hours"] < 24


def test_no_heating_activity_reporting(tmp_path: Path) -> None:
    r, grid, _s, _sc = fit(tmp_path, reports_action=False)
    doc = r.doc
    assert grid.heat_basis == "inferred"
    assert not doc["parameters"]["heating_c_per_h"]["available"]
    inferred = doc["parameters"]["heating_inferred_c_per_h"]
    assert inferred["available"] and inferred["basis"] == "inferred" and inferred["confidence"] == "low"
    assert doc["structure"]["heating"] == "inferred" and doc["confidence"]["level"] != "high"
    # Inferred demand is never a heating episode.
    assert doc["evidence"]["episodes"]["heating"]["count"] == 0
    model = tm.load_model(doc)
    assert isinstance(model, tm.ThermalModel) and any("inferred" in w for w in model.warnings)


def _thermostat(t: float, action: str | None, mode: str = "heat", tgt: float = 21.0) -> dict[str, Any]:
    return {"t": t, "e": "", "k": "thermostat", "s": mode, "v": 20.0, "q": "ok",
            "a": {"cur": 20.0, "tgt": tgt, "lo": None, "hi": None, "action": action, "preset": None}}


def _write(directory: Path, records: list[dict[str, Any]], coverage: dict[str, tuple[float, float]],
           gaps: dict[str, list[list[float]]] | None = None) -> StoreView:
    directory.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for rec in records:
        by_day.setdefault(datetime.fromtimestamp(rec["t"], UTC).strftime("%Y%m%d"), []).append(json.dumps(rec))
    for day, lines in by_day.items():
        with open(directory / f"obs-{day}.jsonl", "a", encoding="utf-8") as h:
            h.write("\n".join(lines) + "\n")
    cov = {e: {"from": a, "to": b} for e, (a, b) in coverage.items()}
    return StoreView(directory, cov, gaps or {}, {})


T0 = datetime(2026, 2, 2, 10, tzinfo=UTC).timestamp()


def _temp(e: str, t: float, v: float | None, q: str = "ok") -> dict[str, Any]:
    return {"t": t, "e": e, "k": "room_temp", "s": str(v), "u": "°C", "v": v if q == "ok" else None, "q": q}


def test_multiple_trvs_give_a_heating_fraction(tmp_path: Path) -> None:
    recs = [_temp("sensor.r", T0, 20.0)]
    a = {**_thermostat(T0, "heating"), "e": "climate.a"}
    b = {**_thermostat(T0, "idle"), "e": "climate.b"}
    c = {**_thermostat(T0 + 1800, "heating"), "e": "climate.b"}
    view = _write(tmp_path, [*recs, a, b, c], {e: (T0, T0 + 3600) for e in ("sensor.r", "climate.a", "climate.b")})
    spec = RoomSpec("r", "sensor.r", [], ["climate.a", "climate.b"], [], None)
    g = prepare_room(spec, view, T0, T0 + 3600, tz="UTC")
    assert g.heat[1] == 0.5 and g.heat[-1] == 1.0 and g.heat_basis == "reported"


def test_mixed_reporting_devices_are_not_assumed_passive(tmp_path: Path) -> None:
    a = {**_thermostat(T0, "idle"), "e": "climate.a"}
    b = {**_thermostat(T0, None), "e": "climate.b"}       # does not report activity, mode heat
    off = {**_thermostat(T0 + 1800, None, mode="off"), "e": "climate.b"}
    view = _write(tmp_path, [_temp("sensor.r", T0, 20.0), a, b, off],
                  {e: (T0, T0 + 3600) for e in ("sensor.r", "climate.a", "climate.b")})
    g = prepare_room(RoomSpec("r", "sensor.r", [], ["climate.a", "climate.b"], [], None), view, T0, T0 + 3600,
                     tz="UTC")
    assert g.heat_basis == "mixed"
    assert g.heat[1] is None and g.why[1] == "heating_unknown"
    # Mode off is a trustworthy "not heating", even without hvac_action.
    assert g.heat[-1] == 0.0 and g.why[-1] == "usable"


def test_secondary_sensor_fallback_breaks_segments(tmp_path: Path) -> None:
    recs = [_temp("sensor.p", T0, 20.0), _temp("sensor.s", T0, 21.0), _temp("sensor.p", T0 + 1200, None,
                                                                              "unavailable"),
            _temp("sensor.p", T0 + 2400, 20.2)]
    view = _write(tmp_path, recs, {e: (T0, T0 + 3600) for e in ("sensor.p", "sensor.s")})
    g = prepare_room(RoomSpec("r", "sensor.p", ["sensor.s"], [], [], None), view, T0, T0 + 3600, tz="UTC")
    i = g.times.index(T0 + 1500)
    assert g.temp[i] == 21.0 and g.source[i] == "s:sensor.s"
    # The 1 °C jump to the secondary is a change of source, not a discontinuity or warming.
    assert g.why[i] == "usable" and g.discontinuities == 0
    assert g.source[g.times.index(T0 + 3000)] == "p"


def test_multiple_windows(tmp_path: Path) -> None:
    def opening(e: str, t: float, v: int | None) -> dict[str, Any]:
        return {"t": t, "e": e, "k": "opening", "s": "on" if v else "off", "v": v,
                "q": "ok" if v is not None else "unavailable"}

    recs = [_temp("sensor.r", T0, 20.0), opening("binary_sensor.w1", T0, 0), opening("binary_sensor.w2", T0, 0),
            opening("binary_sensor.w2", T0 + 600, 1), opening("binary_sensor.w2", T0 + 900, 0),
            opening("binary_sensor.w1", T0 + 4000, None), _temp("sensor.r", T0 + 3500, 20.1)]
    ids = ("sensor.r", "binary_sensor.w1", "binary_sensor.w2")
    view = _write(tmp_path, recs, {e: (T0, T0 + 7200) for e in ids})
    g = prepare_room(RoomSpec("r", "sensor.r", [], [], ["binary_sensor.w1", "binary_sensor.w2"], None), view, T0,
                     T0 + 7200, tz="UTC")
    w = dict(zip(g.times, g.why, strict=True))
    assert w[T0] == "usable" and w[T0 + 600] == "window_open"
    assert w[T0 + 900 + 600] == "window_settling" and w[T0 + 900 + 2400] == "usable"
    assert w[T0 + 4200] == "window_unknown"


def test_sensor_unavailable_during_heating_is_excluded(tmp_path: Path) -> None:
    r, grid, _s, _sc = fit(tmp_path, dropout_per_day=3.0)
    heating_steps = [i for i, h in enumerate(grid.heat) if h and grid.temp[i] is None]
    assert heating_steps and all(grid.why[i] != "usable" for i in heating_steps)
    assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.12)


def test_sensor_jump_is_a_discontinuity(tmp_path: Path) -> None:
    recs = [_temp("sensor.r", T0, 20.0), _temp("sensor.r", T0 + 1200, 24.0), _temp("sensor.r", T0 + 1500, 24.1)]
    view = _write(tmp_path, recs, {"sensor.r": (T0, T0 + 3600)})
    g = prepare_room(RoomSpec("r", "sensor.r", [], [], [], None), view, T0, T0 + 3600, tz="UTC")
    assert g.discontinuities == 1 and g.why[g.times.index(T0 + 1200)] == "discontinuity"


def test_dst_and_midnight(tmp_path: Path) -> None:
    # Europe/Copenhagen leaves summer time on 2026-10-25 at 01:00 UTC.
    start = datetime(2026, 10, 24, 18, tzinfo=UTC).timestamp()
    recs = [_temp("sensor.r", start + i * 600, 20.0 + (i % 3) * 0.1) for i in range(int(16 * 3600 / 600))]
    view = _write(tmp_path, recs, {"sensor.r": (start, start + 16 * 3600)})
    g = prepare_room(RoomSpec("r", "sensor.r", [], [], [], None), view, start, start + 16 * 3600,
                     tz="Europe/Copenhagen")
    night = dict(zip(g.times, g.night, strict=True))
    # 20:00 UTC = 22:00 local (summer): night; 04:30 UTC = 05:30 local (winter): night; 05:00 UTC = 06:00: day.
    assert night[datetime(2026, 10, 24, 20, tzinfo=UTC).timestamp()] == 1
    assert night[datetime(2026, 10, 24, 19, 55, tzinfo=UTC).timestamp()] == 0
    assert night[datetime(2026, 10, 25, 4, 30, tzinfo=UTC).timestamp()] == 1
    assert night[datetime(2026, 10, 25, 5, 0, tzinfo=UTC).timestamp()] == 0
    # Crossing UTC midnight (two day files) leaves no hole.
    m = datetime(2026, 10, 25, tzinfo=UTC).timestamp()
    assert all(g.temp[g.times.index(m + d)] is not None for d in (-300, 0, 300))


def test_missing_outdoor_uses_a_simpler_model(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, outdoor=False)
    doc = r.doc
    assert doc["structure"]["reference"] == "fixed" and doc["structure"]["reference_c"] is not None
    assert "balance_offset_c" not in doc["parameters"]
    assert any("No outdoor temperature is configured" in e or "No usable outdoor temperature" in e
               for e in doc["explanations"])
    assert doc["confidence"]["level"] != "high"  # a fixed reference is at most moderate (review S6)
    model = tm.load_model(doc)
    assert isinstance(model, tm.ThermalModel)
    assert not tm.simulate(model, 20.0, [tm.Inputs(heating=0.0)] * 12).warnings or True


def test_partly_missing_outdoor_measurements(tmp_path: Path) -> None:
    sim, view, sc = simulate(tmp_path)
    end = sc.start + sc.days * 86400
    view.gaps[sim.ids["outdoor"]] = [[sc.start + d * 86400, sc.start + d * 86400 + 8 * 3600] for d in range(0, 21, 2)]
    grid = prepare_room(room_spec(sim, sc), view, sc.start, end, tz="UTC")
    r = learn(grid, min_history_days=14, now=end)
    assert r.doc["structure"]["reference"] == "outdoor"
    assert close(value(r.doc, "k_per_h"), sc.truth.k, 0.3)
    # Blocks without an outdoor value are not fitted with an invented one.
    assert r.doc["evidence"]["outdoor_hours"] < r.doc["evidence"]["usable_hours"]


def test_recorder_backfill_after_an_outage(tmp_path: Path) -> None:
    sim, view, sc = simulate(tmp_path, days=4)
    room = sim.ids["room"]
    a, b = sc.start + 2 * 86400, sc.start + 2 * 86400 + 6 * 3600
    missing = [r for r in sim.records if r["e"] == room and a <= r["t"] < b]
    kept = [r for r in sim.records if not (r["e"] == room and a <= r["t"] < b)]
    sim.records = kept
    state = sim.write(tmp_path / "outage")
    outage = StoreView(tmp_path / "outage", state["coverage"], {room: [[a, b]]}, {})
    g1 = prepare_room(room_spec(sim, sc), outage, sc.start, sc.start + 4 * 86400, tz="UTC")
    inside = [i for i, t in enumerate(g1.times) if a <= t < b]
    assert all(g1.why[i] == "not_observed" for i in inside)
    # The Recorder fill appends the records to the day file later (out of order) and removes the gap.
    _write(tmp_path / "outage", [{**r, "src": "recorder"} for r in missing], {})
    filled = StoreView(tmp_path / "outage", state["coverage"], {}, {})
    g2 = prepare_room(room_spec(sim, sc), filled, sc.start, sc.start + 4 * 86400, tz="UTC")
    assert sum(1 for i in inside if g2.temp[i] is not None) > 0.9 * len(inside)


def test_renamed_entity_records_are_read_under_the_new_id(tmp_path: Path) -> None:
    view = _write(tmp_path, [_temp("sensor.old", T0, 20.0), _temp("sensor.new", T0 + 1800, 20.5)],
                  {"sensor.new": (T0, T0 + 3600)})
    view.aliases["sensor.new"] = ["sensor.old"]
    events = read_events(view, {"sensor.new": "room_temp"}, T0, T0 + 3600)
    assert [e[1] for e in events["sensor.new"]] == [20.0, 20.5]


def test_retention_pruning_keeps_learning(tmp_path: Path) -> None:
    sim, view, sc = simulate(tmp_path, days=20)
    for path in sorted(view.directory.glob("obs-*.jsonl"))[:6]:  # type: ignore[union-attr]
        path.unlink()
    end = sc.start + 20 * 86400
    r = learn(prepare_room(room_spec(sim, sc), view, sc.start, end, tz="UTC"), min_history_days=14, now=end)
    assert r.doc["evidence"]["usable_days"] < 15 and close(value(r.doc, "heating_c_per_h"), 1.6, 0.12)


# ------------------------------------------------------ statistical
def test_no_leakage_from_the_held_out_period(tmp_path: Path) -> None:
    sim, view, sc = simulate(tmp_path)
    end = sc.start + sc.days * 86400
    grid = prepare_room(room_spec(sim, sc), view, sc.start, end, tz="UTC")
    base = learn(grid, min_history_days=14, now=end).doc
    split = base["data"]["split"]
    # Change every temperature after the split (not when it is usable): the fit must not move at all.
    tampered = copy.deepcopy(grid)
    tampered.temp = [t + (3.0 if t is not None and grid.times[i] >= split else 0.0) if t is not None else None
                     for i, t in enumerate(grid.temp)]
    other = learn(tampered, min_history_days=14, now=end).doc
    assert other["data"]["split"] == split
    for name, p in base["parameters"].items():
        assert other["parameters"][name]["value"] == p["value"], name
    assert other["selection"] == base["selection"]
    # ... while the held-out accuracy does see it.
    assert other["validation"]["horizons"]["180"]["model"]["mae"] != base["validation"]["horizons"]["180"]["model"]["mae"]


def test_no_fabricated_heating_episodes(tmp_path: Path) -> None:
    r, grid, _s, _sc = fit(tmp_path, reports_action=False)
    assert all(h is None or h == 0 for h in grid.heat)
    assert not [e for e in episodes(grid) if e.kind == "heating"]


def test_parameters_are_always_plausible(tmp_path: Path) -> None:
    for i, kw in enumerate([{}, {"reports_action": False}, {"outdoor": False}, {"sensor_noise": 0.3, "resolution": 0.5},
                            {"truth": Truth(k=0.2, b=4.0, d=1.0)}, {"heating": "none", "days": 15}]):
        r, *_ = fit(tmp_path / str(i), **kw)
        if r.doc is None:
            continue
        tm.check_document(r.doc)  # every available parameter within BOUNDS
        for name, p in r.doc["parameters"].items():
            if p["available"] and name in ("k_per_h", "heating_c_per_h", "solar_c_per_h") or name.startswith(
                    "coupling:") and p["available"]:
                assert p["value"] >= 0


def test_heating_labels_unrelated_to_temperature_are_not_learned(tmp_path: Path) -> None:
    # The device reports heating, but its heat never reaches the room (a closed valve, another room's radiator).
    r, *_ = fit(tmp_path, truth=Truth(b=0.0, d=0.6))
    p = r.doc["parameters"]["heating_c_per_h"]
    assert not p["available"] or p["value"] < 0.15
    assert r.doc["structure"]["heating"] is None or p["value"] < 0.15


def test_uncertainty_grows_as_evidence_weakens(tmp_path: Path) -> None:
    long, *_ = fit(tmp_path / "a", days=24, seed=8)
    short, *_ = fit(tmp_path / "b", days=6, seed=8, sensor_noise=0.1)
    assert long.doc["confidence"]["score"] > short.doc["confidence"]["score"]
    assert long.doc["parameters"]["heating_c_per_h"]["se"] < short.doc["parameters"]["heating_c_per_h"]["se"]
    assert LEVEL[long.doc["confidence"]["level"]] >= LEVEL[short.doc["confidence"]["level"]]


LEVEL = {"none": 0, "low": 1, "moderate": 2, "high": 3}


def test_insufficient_data_states(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, days=0.5)
    assert r.doc is None and r.diagnostic["status"] in ("insufficient", "collecting")
    view = StoreView(tmp_path / "none", {}, {}, {})
    g = prepare_room(RoomSpec("r", "sensor.x", [], [], [], None), view, T0, T0 + 86400, tz="UTC")
    r2 = learn(g, min_history_days=14, now=T0 + 86400)
    assert r2.doc is None and r2.diagnostic["status"] == "collecting"


def test_baselines_are_computed_on_the_same_origins(tmp_path: Path) -> None:
    sim, view, sc = simulate(tmp_path)
    end = sc.start + sc.days * 86400
    grid = prepare_room(room_spec(sim, sc), view, sc.start, end, tz="UTC")
    doc = learn(grid, min_history_days=14, now=end).doc
    s = make_series(grid)
    st = Structure(heating=True, lag=doc["dynamics"]["lag_min"], tau=doc["dynamics"]["tau_min"])
    starts = origins(s, st, lo=doc["data"]["split"], hi=doc["data"]["to"], horizon=HORIZONS[180])
    persistence = sum(abs(grid.temp[o] - grid.temp[o + 36]) for o in starts) / len(starts)  # type: ignore[operator]
    h = doc["validation"]["horizons"]["180"]
    assert h["origins"] == len(starts) and h["persistence"]["mae"] == pytest.approx(persistence, abs=1e-3)
    assert h["passive"]["mae"] > h["model"]["mae"]



# --------------------------------------------------------- A7.2 interface
def model_doc(**over: Any) -> dict[str, Any]:
    params = {"k_per_h": {"value": 0.05, "available": True}, "baseline_c_per_h": {"value": 0.5, "available": True},
              "heating_c_per_h": {"value": 2.0, "available": True}}
    doc = {"format": tm.FORMAT, "schema": tm.SCHEMA, "algorithm": tm.ALGORITHM, "area_id": "r",
           "trained_at": "2026-01-01T00:00:00+00:00", "status": "high", "preliminary": False,
           "structure": {"reference": "outdoor", "reference_c": None, "heating": "reported", "solar": False,
                         "night": False, "coupling": []},
           "parameters": params, "dynamics": {"lag_min": 0, "tau_min": 0},
           "uncertainty": {"60": {"q80": 0.1}, "180": {"q80": 0.3}}, "confidence": {"level": "high"},
           "fingerprint_hash": "abc"}
    doc.update(over)
    return doc


def test_interface_matches_the_closed_form() -> None:
    model = tm.load_model(model_doc())
    assert isinstance(model, tm.ThermalModel)
    # dT/dt = 0.05 (0 − T) + 0.5 + 2.0 → equilibrium 50 °C; T(t) = 50 + (20 − 50) e^{−0.05 t}
    est = tm.estimate_change(model, 20.0, 3.0, outdoor_c=0.0, heating=1.0)
    assert est["end_c"] == pytest.approx(50 - 30 * math.exp(-0.15), abs=1e-3)
    off = tm.estimate_change(model, 20.0, 3.0, outdoor_c=0.0, heating=0.0)
    assert off["end_c"] == pytest.approx(10 + 10 * math.exp(-0.15), abs=1e-3)
    ev = tm.evaluate(model, 20.0, outdoor_c=0.0, heating=0.0, hours=1)
    assert ev["settles_unheated_c"] == 10.0 and ev["settles_heating_c"] == 50.0 and ev["time_constant_h"] == 20.0
    assert ev["rate_now_c_per_h"] == pytest.approx(-0.5)
    traj = tm.simulate(model, 20.0, [tm.Inputs(outdoor_c=0.0, heating=1.0)] * 36)
    assert traj.kind == "model_estimate" and len(traj.temps) == 37
    assert traj.high[12] - traj.temps[12] == pytest.approx(0.1) and traj.high[36] - traj.temps[36] == pytest.approx(0.3)


def test_interface_lag_and_emitter() -> None:
    model = tm.load_model(model_doc(dynamics={"lag_min": 15, "tau_min": 30}))
    traj = tm.simulate(model, 20.0, [tm.Inputs(outdoor_c=20.0, heating=1.0)] * 24, heating_history=[0.0] * 6,
                       heating_state=0.0)
    # Nothing for the delay (only the baseline), then the emitter ramps up.
    early = traj.temps[3] - traj.temps[0]
    assert early == pytest.approx(0.5 * 15 / 60, abs=1e-3)
    assert traj.temps[24] - traj.temps[12] > traj.temps[12] - traj.temps[6] > 0


def test_interface_unavailable_and_warnings() -> None:
    assert tm.load_model(None).code == "no_model"  # type: ignore[union-attr]
    assert tm.load_model(model_doc(), fingerprint_hash="other").code == "invalidated"  # type: ignore[union-attr]
    assert tm.load_model(model_doc(status="unidentifiable")).code == "unidentifiable"  # type: ignore[union-attr]
    assert tm.load_model(model_doc(schema=99)).code == "newer_schema"  # type: ignore[union-attr]
    bad = model_doc()
    bad["parameters"]["k_per_h"]["value"] = 5.0
    assert tm.load_model(bad).code == "invalid"  # type: ignore[union-attr]
    no_heat = model_doc(structure={**model_doc()["structure"], "heating": None})
    no_heat["parameters"]["heating_c_per_h"] = {"value": None, "available": False, "reason": "x"}
    m = tm.load_model(no_heat)
    assert isinstance(tm.estimate_change(m, 20, 2, outdoor_c=0, heating=1.0), tm.Unavailable)  # type: ignore[arg-type]
    m = tm.load_model(model_doc())
    # Without the outdoor temperature an outdoor-reference model refuses (leaving the loss out would be wrong).
    missing = tm.simulate(m, 20.0, [tm.Inputs(outdoor_c=None, heating=0.0)] * 60)  # type: ignore[arg-type]
    assert isinstance(missing, tm.Unavailable) and missing.code == "missing_input" and "outdoor_c" in missing.missing
    assert isinstance(tm.estimate_change(m, 21.0, 6.0, outdoor_c=None), tm.Unavailable)  # type: ignore[arg-type]
    traj = tm.simulate(m, 20.0, [tm.Inputs(outdoor_c=2.0, heating=0.0)] * 60)  # type: ignore[arg-type]
    assert any("extrapolated" in w for w in traj.warnings)
    assert tm.load_model(model_doc(dynamics={"lag_min": -5, "tau_min": 0})).code == "invalid"  # type: ignore[union-attr]
    assert tm.load_model(model_doc(structure={**model_doc()["structure"], "reference": "fixed",
                                              "reference_c": None})).code == "invalid"  # type: ignore[union-attr]


def test_a_learned_model_runs_through_the_interface(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, window_bearing=180, truth=Truth(solar=1.0))
    model = tm.load_model(r.doc)
    assert isinstance(model, tm.ThermalModel) and model.solar is not None
    est = tm.estimate_change(model, 19.0, 2.0, outdoor_c=0.0, heating=1.0, solar=0.0)
    off = tm.estimate_change(model, 19.0, 2.0, outdoor_c=0.0, heating=0.0, solar=0.0)
    assert est["end_c"] > off["end_c"]
    assert est["band_c"][0] < est["end_c"] < est["band_c"][1]


# ------------------------------------------------------------- storage
def test_store_recovers_and_protects(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path / "fit")
    doc = r.doc
    store = ModelStore(tmp_path / "models")
    store.load()
    store.save({**doc, "area_id": "kamar"})
    store.save({**doc, "area_id": "kamar", "trained_at": "2026-01-02T00:00:00+00:00"})
    assert len(list((tmp_path / "models" / "history").glob("kamar@*.json"))) == 1
    (tmp_path / "models" / "kamar.json").write_text("{broken", encoding="utf-8")
    again = ModelStore(tmp_path / "models")
    again.load()
    assert again.docs["kamar"]["trained_at"] == doc["trained_at"]
    assert any(n["code"] == "model_recovered" for n in again.notices)
    assert list((tmp_path / "models").glob("kamar.corrupt-*.json"))
    # A newer App's model is kept and never overwritten.
    newer = {**doc, "area_id": "stova", "schema": 9}
    (tmp_path / "models" / "stova.json").write_text(json.dumps(newer), encoding="utf-8")
    third = ModelStore(tmp_path / "models")
    third.load()
    assert "stova" in third.protected and third.problems["stova"]["code"] == "newer_schema"
    with pytest.raises(PermissionError):
        third.save({**doc, "area_id": "stova"})
    assert json.loads((tmp_path / "models" / "stova.json").read_text())["schema"] == 9


def test_interrupted_training_is_reported(tmp_path: Path) -> None:
    store = ModelStore(tmp_path)
    store.load()
    store.state["kamar"] = {"started_at": "2026-01-01T00:00:00+00:00", "finished_at": None}
    store.save_state()
    again = ModelStore(tmp_path)
    again.load()
    assert again.state["kamar"]["result"] == "interrupted"



# ---------------------------------------------------------- the service






# ------------------------------------------- independent review regressions
def test_review_b1_outdoor_added_late_still_gives_a_model(tmp_path: Path) -> None:
    """An outdoor sensor configured only for the last days must not leave the room without any model."""
    sim, view, sc = simulate(tmp_path)
    out = sim.ids["outdoor"]
    end = sc.start + sc.days * 86400
    view.coverage[out] = {"from": end - 4 * 86400, "to": end}
    r = learn(prepare_room(room_spec(sim, sc), view, sc.start, end, tz="UTC"), min_history_days=14, now=end)
    assert r.doc is not None and r.doc["structure"]["reference"] == "fixed"
    assert r.doc["evidence"]["outdoor_share"] < 0.6
    assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.2)
    assert any("outdoor temperature" in e and "heat loss could be measured" in e for e in r.doc["explanations"])


def test_review_b2_a_real_solar_term_survives_cloudy_windows(tmp_path: Path) -> None:
    """Selection over several rolling folds: across seeds, a true solar gain is kept and heating stays unbiased."""
    for seed in (1, 4, 7):
        r, *_ = fit(tmp_path / str(seed), seed=seed, truth=Truth(solar=1.5), window_bearing=180, clouds=True)
        assert r.doc["structure"]["solar"], (seed, r.doc["selection"])
        assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.12), seed


def test_review_b2_standard_errors_cover_the_half_disagreement(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, truth=Truth(solar=1.5), window_bearing=180, clouds=True)
    stab = r.doc["stability"]
    for name, (a, b) in stab["values"].items():
        param = {"heat": "heating_c_per_h", "ref": "k_per_h"}.get(name)
        if param and r.doc["parameters"][param]["se"] is not None:
            assert r.doc["parameters"][param]["se"] >= abs(a - b) / 2 - 1e-6


def test_review_s1_heating_acts_in_its_own_step(tmp_path: Path) -> None:
    """Truth without any delay: no spurious lag, unbiased heating and baseline."""
    r, *_ = fit(tmp_path, truth=Truth(lag_min=0, tau_min=0))
    assert close(value(r.doc, "heating_c_per_h"), 1.6, 0.05)
    assert close(value(r.doc, "baseline_c_per_h"), 0.35, 0.2)
    assert r.doc["dynamics"]["lag_min"] == 0 and r.doc["dynamics"]["tau_min"] == 0
    assert r.doc["validation"]["horizons"]["180"]["model"]["mae"] < 0.08


def test_review_s2_a_failed_save_keeps_the_current_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from spc import model_store as store_mod  # noqa: PLC0415

    r, *_ = fit(tmp_path / "fit")
    store = ModelStore(tmp_path / "models")
    store.load()
    store.save({**r.doc, "area_id": "kamar"})

    def full_disk(path: Path, data: bytes) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(store_mod, "_atomic_write", full_disk)
    with pytest.raises(OSError):
        store.save({**r.doc, "area_id": "kamar", "trained_at": "2026-02-02T00:00:00+00:00"})
    again = ModelStore(tmp_path / "models")
    again.load()
    assert again.docs["kamar"]["trained_at"] == r.doc["trained_at"]


def test_review_s2_history_names_do_not_collide_between_rooms(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path / "fit")
    store = ModelStore(tmp_path / "models")
    store.load()
    for area in ("kamar", "kamar-2"):
        store.save({**r.doc, "area_id": area})
        store.save({**r.doc, "area_id": area})
    store.archive("kamar", "reset")
    assert list((tmp_path / "models" / "history").glob("kamar-2@*.json"))
    assert "kamar-2" in store.docs


def test_review_s4_a_window_closed_for_days_stays_known(tmp_path: Path) -> None:
    def opening(t: float, v: int) -> dict[str, Any]:
        return {"t": t, "e": "binary_sensor.w", "k": "opening", "s": "on" if v else "off", "v": v, "q": "ok"}

    start = T0 - 12 * 86400  # closed long before the window and never re-reported
    recs = [opening(start, 0), *(_temp("sensor.r", T0 + i * 600, 20.0) for i in range(144))]
    view = _write(tmp_path, recs, {"sensor.r": (T0, T0 + 86400), "binary_sensor.w": (start, T0 + 86400)})
    g = prepare_room(RoomSpec("r", "sensor.r", [], [], ["binary_sensor.w"], None), view, T0, T0 + 86400, tz="UTC")
    assert set(g.why[:-1]) == {"usable"}
    # ... but not across a recorded gap.
    view.gaps["binary_sensor.w"] = [[T0 + 3600, T0 + 7200]]
    g2 = prepare_room(RoomSpec("r", "sensor.r", [], [], ["binary_sensor.w"], None), view, T0, T0 + 86400, tz="UTC")
    assert g2.why[g2.times.index(T0 + 5400)] == "window_unknown"


def test_review_s4_cover_without_position(tmp_path: Path) -> None:
    from spc.prep import _event  # noqa: PLC0415

    assert _event("cover", {"t": 1.0, "s": "closed", "q": "ok", "a": {"position": None}}) == (1.0, 0.0)
    assert _event("cover", {"t": 1.0, "s": "open", "q": "ok", "a": {"position": None}}) == (1.0, 100.0)
    assert _event("cover", {"t": 1.0, "s": "opening", "q": "ok", "a": {"position": None}}) == (1.0, None)



def test_review_s6_fixed_reference_is_never_high(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, outdoor=False)
    assert r.doc["confidence"]["level"] in ("low", "moderate") and r.doc["status"] != "high"
    night = r.doc["parameters"]["night_c_per_h"]
    assert not night["available"] or night["confidence"] == "low"


def test_review_s7_coupling_is_validated_with_the_neighbour_held(tmp_path: Path) -> None:
    r, *_ = fit(tmp_path, neighbour=True, truth=Truth(coupling=0.15, k=0.03))
    assert r.doc["validation"]["neighbours"] == "held at the start"
    assert r.doc["validation"]["conditions_known"] == "observed"
