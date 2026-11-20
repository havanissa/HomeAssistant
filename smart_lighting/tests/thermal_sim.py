"""Deterministic synthetic rooms for the thermal-learning tests (Phase A7.1).

A room with known coefficients is simulated on a 1-minute internal step with the same physics as
``climate_thermal_model`` (first-order, heating through a delayed emitter, sun through the window geometry and
covers, a neighbour, open windows with a large extra loss), and reported the way Home Assistant sensors report:
on a change of at least the sensor's resolution or after a maximum interval, rounded, with noise, optional
dropouts and recorded gaps. The records are written as the A7.0 observation store's day files, with its
coverage and gaps, so the learner reads exactly what it reads in the App.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import random
from typing import Any

from smart_lighting.climate_prep import WindowSpec, window_geometry


@dataclass
class Truth:
    k: float = 0.04            # 1/h towards outdoor
    d: float = 0.35            # °C/h baseline
    b: float = 1.6             # °C/h at full heating
    lag_min: float = 10.0
    tau_min: float = 20.0
    solar: float = 0.0         # °C/h at index 1
    night: float = 0.0
    coupling: float = 0.0      # 1/h to the neighbour
    window_loss: float = 1.2   # 1/h extra towards outdoor while a window is open


@dataclass
class Scenario:
    start: float = datetime(2026, 1, 5, tzinfo=UTC).timestamp()
    days: float = 21.0
    seed: int = 1
    truth: Truth = field(default_factory=Truth)
    outdoor_mean: float = 2.0
    outdoor_swing: float = 4.0
    outdoor_trend_per_day: float = 0.0
    heating: str = "thermostat"          # thermostat | none
    reports_action: bool = True
    comfort: float = 21.0
    setback: float = 17.5
    sensor_noise: float = 0.03
    resolution: float = 0.1
    max_interval_min: float = 30.0
    windows_open_per_day: float = 0.0
    window_bearing: float | None = None
    cover_closed_at_night: bool = False
    neighbour: bool = False
    outdoor: bool = True
    thermostat_bias: float = 0.0
    dropout_per_day: float = 0.0         # sensor unavailable for 1-3 h
    gaps: list[tuple[float, float]] = field(default_factory=list)  # (start_day, hours) not observed at all
    irregular: bool = True
    tz_offset_h: float = 0.0
    entity_prefix: str = "kamar"
    clouds: bool = False
    fixed_heating: float | None = None   # a constant heating fraction (no thermostat logic)


def sun_position(t: float, lat: float = 62.0) -> tuple[float, float]:
    """A simple sun model (declination for early January): elevation and azimuth in degrees."""
    day = datetime.fromtimestamp(t, UTC)
    hours = day.hour + day.minute / 60 + day.second / 3600
    decl = math.radians(-22.0)
    ha = math.radians(15 * (hours - 12.0))
    la = math.radians(lat)
    sin_el = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    el = math.asin(max(-1, min(1, sin_el)))
    cos_az = (math.sin(decl) - math.sin(el) * math.sin(la)) / max(1e-9, math.cos(el) * math.cos(la))
    az = math.degrees(math.acos(max(-1, min(1, cos_az))))
    if hours > 12:
        az = 360 - az
    return math.degrees(el), az


class Sim:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc
        self.rng = random.Random(sc.seed)
        p = sc.entity_prefix
        self.ids = {"room": f"sensor.{p}_temperature", "trv": f"climate.{p}_trv", "window": f"binary_sensor.{p}_window",
                    "outdoor": "sensor.outdoor_temperature", "cover": f"cover.{p}_curtain",
                    "neighbour": f"sensor.{p}_neighbour_temperature", "sun": "sun.sun"}
        self.records: list[dict[str, Any]] = []
        self.true_temp: list[tuple[float, float]] = []

    def outdoor_at(self, t: float) -> float:
        sc = self.sc
        day = (t - sc.start) / 86400.0
        hour = ((t / 3600.0) + sc.tz_offset_h) % 24
        return sc.outdoor_mean + sc.outdoor_trend_per_day * day + sc.outdoor_swing * math.sin(
            2 * math.pi * (hour - 9) / 24) + 1.5 * math.sin(2 * math.pi * day / 5.3)

    def schedule(self, t: float) -> float:
        hour = ((t / 3600.0) + self.sc.tz_offset_h) % 24
        return self.sc.comfort if 6.5 <= hour < 22.5 else self.sc.setback

    def run(self) -> Sim:
        sc, tr = self.sc, self.sc.truth
        dt = 60.0
        n = int(sc.days * 86400 / dt)
        temp = 19.0
        neigh = 20.0
        heat_hist: list[float] = []
        u_f = 0.0
        heating = 0
        window_until = -1.0
        dropout_until = -1.0
        window = WindowSpec("w", sc.window_bearing, layers=[(self.ids["cover"], "blackout")]) \
            if sc.window_bearing is not None else None
        last: dict[str, tuple[float, Any]] = {}
        cloud = 1.0
        for i in range(n):
            t = sc.start + i * dt
            out = self.outdoor_at(t)
            # Thermostat with hysteresis on the true temperature (what the TRV senses, minus its bias).
            sp = self.schedule(t)
            if sc.fixed_heating is not None:
                u = sc.fixed_heating
                heating = 1 if u > 0 else 0
            elif sc.heating == "thermostat":
                if temp < sp - 0.3:
                    heating = 1
                elif temp > sp + 0.3:
                    heating = 0
                u = float(heating)
            else:
                heating, u = 0, 0.0
            heat_hist.append(u)
            lag = int(tr.lag_min)
            u_lag = heat_hist[-1 - lag] if len(heat_hist) > lag else 0.0
            if tr.tau_min > 0:
                u_f += (u_lag - u_f) * (1 - math.exp(-1.0 / tr.tau_min))
            else:
                u_f = u_lag
            if self.rng.random() < sc.windows_open_per_day / 1440.0:
                window_until = t + self.rng.uniform(10, 45) * 60
            open_now = t < window_until
            el, az = sun_position(t)
            if sc.clouds and i % 60 == 0:
                cloud = self.rng.choice([0.2, 0.5, 1.0, 1.0])
            hour = ((t / 3600.0) + sc.tz_offset_h) % 24
            cover_pos = 0.0 if sc.cover_closed_at_night and (hour >= 21 or hour < 9) else 100.0
            s = 0.0
            if window is not None:
                s = window_geometry(window, el, az) * cover_pos / 100.0 * (cloud if sc.clouds else 1.0)
            night = 1 if hour >= 22 or hour < 6 else 0
            rate = tr.k * (out - temp) + tr.b * u_f + tr.solar * s + tr.night * night + tr.d
            if sc.neighbour:
                rate += tr.coupling * (neigh - temp)
                neigh_rate = 0.03 * (out - neigh) + 0.6 + 0.4 * math.sin(2 * math.pi * hour / 24) + 0.3 * (
                    1 if 7 <= hour < 9 or 17 <= hour < 20 else 0)
                neigh += neigh_rate * dt / 3600
            if open_now:
                rate += tr.window_loss * (out - temp)
            temp += rate * dt / 3600
            self.true_temp.append((t, temp))
            if any(sc.start + a * 86400 <= t < sc.start + a * 86400 + h * 3600 for a, h in sc.gaps):
                continue
            if self.rng.random() < sc.dropout_per_day / 1440.0:
                dropout_until = t + self.rng.uniform(1, 3) * 3600
            # Reports.
            measured = round(round((temp + self.rng.gauss(0, sc.sensor_noise)) / sc.resolution) * sc.resolution, 2)
            if t < dropout_until:
                self._report(last, t, self.ids["room"], "room_temp", "unavailable", None, None, force=True)
            else:
                self._report(last, t, self.ids["room"], "room_temp", str(measured), measured, None)
            if sc.outdoor:
                o = round(out + self.rng.gauss(0, 0.05), 1)
                self._report(last, t, self.ids["outdoor"], "outdoor", str(o), o, None)
            if sc.heating == "thermostat" or sc.fixed_heating is not None:
                attrs = {"cur": round(temp + sc.thermostat_bias, 1), "tgt": sp, "lo": None, "hi": None,
                         "action": ("heating" if heating else "idle") if sc.reports_action else None, "preset": None}
                self._report(last, t, self.ids["trv"], "thermostat", "heat", attrs["cur"], attrs)
            if sc.windows_open_per_day:
                self._report(last, t, self.ids["window"], "opening", "on" if open_now else "off",
                             1 if open_now else 0, None)
            if window is not None:
                if i % 10 == 0:
                    self._report(last, t, self.ids["sun"], "sun", "above_horizon" if el > 0 else "below_horizon",
                                 round(el, 1), {"elevation": round(el, 1), "azimuth": round(az, 1)}, force=True)
                self._report(last, t, self.ids["cover"], "cover", "open" if cover_pos else "closed", cover_pos,
                             {"position": cover_pos})
            if sc.neighbour:
                nv = round(neigh, 1)
                self._report(last, t, self.ids["neighbour"], "room_temp", str(nv), nv, None)
        return self

    def _report(self, last: dict[str, tuple[float, Any]], t: float, e: str, k: str, s: str, v: Any, a: Any, *,
                force: bool = False) -> None:
        prev = last.get(e)
        key = (s, json.dumps(a, sort_keys=True))
        max_gap = self.sc.max_interval_min * 60 * (self.rng.uniform(0.5, 1.5) if self.sc.irregular else 1.0)
        if prev is not None and prev[1] == key and t - prev[0] < max_gap and not force:
            return
        if prev is not None and prev[1] == key and force and t - prev[0] < 590:
            return
        last[e] = (t, key)
        q = "ok" if s not in ("unavailable", "unknown") else s
        rec: dict[str, Any] = {"t": round(t, 3), "e": e, "k": k, "s": s, "v": v if q == "ok" else None, "q": q,
                               "src": "live"}
        if k in ("room_temp", "outdoor", "thermostat"):
            rec["u"] = "°C"
        if a is not None:
            rec["a"] = a
        self.records.append(rec)

    def write(self, directory: Path) -> dict[str, Any]:
        """The records as the observation store's day files, with its state.json (coverage and gaps)."""
        directory.mkdir(parents=True, exist_ok=True)
        by_day: dict[str, list[str]] = {}
        for rec in self.records:
            day = datetime.fromtimestamp(rec["t"], UTC).strftime("%Y%m%d")
            by_day.setdefault(day, []).append(json.dumps(rec, separators=(",", ":"), ensure_ascii=False))
        for day, lines in by_day.items():
            # Shuffle a little within the file: a gap fill appends older records later.
            (directory / f"obs-{day}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        end = self.sc.start + self.sc.days * 86400
        entities = {r["e"] for r in self.records}
        gaps = [[self.sc.start + a * 86400, self.sc.start + a * 86400 + h * 3600] for a, h in self.sc.gaps]
        state = {"version": 1, "coverage": {e: {"from": self.sc.start, "to": end} for e in entities},
                 "gaps": {e: gaps for e in entities} if gaps else {}, "aliases": {}}
        (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
        return state
