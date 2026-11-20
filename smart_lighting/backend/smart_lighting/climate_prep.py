"""Climate thermal learning (Phase A7.1): preparing the observations for fitting.

Pure functions plus one streaming reader of the A7.0 observation store
(``climate_observations``). Nothing here writes anything: the stored records
are never rewritten, interpolated or re-saved; preparation produces an
in-memory grid that a fit reads and then discards.

The grid
--------
Home Assistant sensors report irregularly (on change, every few minutes, or
once an hour when nothing moves). The learner therefore aligns every input on
a fixed **5-minute grid** by *bounded holding*: the value at a grid time is the
entity's last record at or before it, **only** when

* the time is inside the entity's observed coverage and not inside a recorded
  gap (``state.json`` of the store): the App (or the Recorder, for a gap fill)
  was listening, so the absence of a new record means *unchanged*;
* the record is younger than the kind's freshness limit (``HOLD``; for room
  temperatures the room's ``stale_minutes``); and
* the record's quality is ``ok``.

Anything else is *unknown* (with its reason), never a guess: there is no
interpolation, and a value never continues across a gap or a stale stretch.

Per grid step the room gets:

``temp``        the room temperature (°C) by the A7.0 policy: the primary sensor,
                else the mean of the usable secondary sensors; never a thermostat's
                own reading. ``source`` records which sensors produced it; a change
                of source breaks a fitting segment (two sensors disagree by a bias).
``heat``        the reported heating fraction 0..1 (devices reporting ``heating`` /
                all heating devices), or None when it is not known. A device in mode
                ``off`` is known not to heat. A setpoint alone is **never** heating.
``heat_inf``    for rooms whose devices do not report activity: an *inferred* demand
                fraction (mode on and setpoint above the room temperature). Kept apart
                from ``heat`` everywhere; a model built on it says so.
``window``      closed / open / unknown / none (no window or door assigned).
``outdoor``     the outdoor temperature (°C) or None.
``solar``       the direct-sun index (see ``solar_index``) or None.
``neighbours``  the temperatures of the other rooms of the room's thermal zone.
``night``       1 between 22:00 and 06:00 local time (Home Assistant's time zone).

and a classification ``why`` (``usable`` or the first reason it is excluded).
"""

from __future__ import annotations

from collections.abc import Iterable
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import math
from pathlib import Path
from typing import Any

from .climate_model import HEATING_ACTIONS

GRID_S = 300.0
#: How long a record may be held on the grid (seconds), per kind. Room temperatures use the room's stale_minutes.
#: States (a thermostat's mode and action, a window, a cover) are held without an age limit *inside observed
#: coverage and outside recorded gaps*: Home Assistant reports every change, and many contact sensors never
#: re-report an unchanged state. Numeric sensors keep an age limit (a silent sensor must not look current).
HOLD = {"outdoor": 7200.0, "thermostat": math.inf, "opening": math.inf, "cover": math.inf,
        "sun": 1800.0, "illuminance": 3 * 3600.0}
STATE_KINDS = ("thermostat", "opening", "cover")
#: For a state entity without a record shortly before the window, older day files are searched this far back.
STATE_LOOKBACK_DAYS = 30
#: A room-temperature jump larger than this between two grid steps is a discontinuity (a reset, a relocated or
#: replaced sensor, a spike), not a thermal change: it breaks the segment.
JUMP_C = 1.5
#: After a window or door closes, the room is not a closed room yet (the air is being replaced).
WINDOW_SETTLE_S = 1800.0
#: An inferred demand needs the setpoint at least this much above the room temperature.
DEMAND_MARGIN_C = 0.3
#: Sheer layers let part of the sun through even when closed.
SHEER_TRANSMISSION = 0.6
NIGHT_HOURS = (22, 6)
NOT_HEATING_ACTIONS = {"off", "idle", "fan", "drying"}
#: Actions under which the room is neither heated (as modelled) nor passive.
EXCLUDED_ACTIONS = {"cooling", "defrosting"}

REASONS = {
    "usable": "Usable",
    "not_observed": "Not observed (the App and the Recorder have no data)",
    "no_temperature": "No current, plausible room temperature",
    "discontinuity": "Sudden jump (sensor reset, replacement or spike)",
    "window_open": "A window or door was open",
    "window_settling": "Within 30 minutes after a window or door closed",
    "window_unknown": "Window or door state unknown",
    "heating_unknown": "Heating activity not known",
    "active_cooling": "A device reported cooling or defrosting",
}


# ------------------------------------------------------------------ inputs
@dataclass
class WindowSpec:
    """A curtain window of the room (from the Curtains configuration): geometry for the direct-sun index."""

    window_id: str
    bearing: float | None
    exposure_width: float = 70.0
    min_elevation: float = 3.0
    max_elevation: float | None = None
    obstructions: list[tuple[float, float, float]] = field(default_factory=list)
    layers: list[tuple[str, str]] = field(default_factory=list)  # (cover entity, kind)
    sun_sensors: list[str] = field(default_factory=list)


@dataclass
class RoomSpec:
    area_id: str
    primary: str | None
    secondary: list[str]
    thermostats: list[str]           # the room's own heating devices plus its zone's shared devices
    openings: list[str]
    outdoor: str | None
    stale_s: float = 3600.0
    windows: list[WindowSpec] = field(default_factory=list)
    neighbours: list[str] = field(default_factory=list)  # area ids of the other rooms of its thermal zone
    zone: dict[str, Any] | None = None
    own_thermostats: list[str] = field(default_factory=list)

    def entities(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for e in [*([self.primary] if self.primary else []), *self.secondary]:
            out[e] = "room_temp"
        for e in self.thermostats:
            out[e] = "thermostat"
        for e in self.openings:
            out[e] = "opening"
        if self.outdoor:
            out[self.outdoor] = "outdoor"
        for w in self.windows:
            for cover, _kind in w.layers:
                out.setdefault(cover, "cover")
            for sensor in w.sun_sensors:
                out.setdefault(sensor, "illuminance")
        if any(w.bearing is not None for w in self.windows):
            out["sun.sun"] = "sun"
        return out


@dataclass
class StoreView:
    """What the learner needs from the observation store: its folder and a copy of its coverage bookkeeping."""

    directory: Path | None
    coverage: dict[str, dict[str, float]]
    gaps: dict[str, list[list[float]]]
    aliases: dict[str, list[str]]


# -------------------------------------------------------------- the reader
def _day(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y%m%d")


def _event(kind: str, rec: dict[str, Any]) -> tuple[Any, ...] | None:
    """A compact (t, value) for one record; value None when the record is not usable."""
    t = rec.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return None
    ok = rec.get("q") == "ok"
    a = rec.get("a") if isinstance(rec.get("a"), dict) else {}
    if kind == "thermostat":
        state = rec.get("s")
        if state in (None, "unavailable", "unknown", ""):
            return (float(t), None)
        return (float(t), (str(state), a.get("action"), _f(a.get("tgt"))))
    if kind == "sun":
        elevation, azimuth = _f(a.get("elevation")), _f(a.get("azimuth"))
        return (float(t), (elevation, azimuth) if elevation is not None and azimuth is not None else None)
    if kind == "cover":
        if rec.get("q") != "ok":
            return (float(t), None)
        position = _f(a.get("position"))
        if position is None:  # a cover without a position: its open / closed state
            position = {"open": 100.0, "closed": 0.0}.get(str(rec.get("s")))
        return (float(t), position)
    value = _f(rec.get("v"))
    return (float(t), value if ok else None)


def _f(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def read_events(view: StoreView, kinds: dict[str, str], start: float, end: float) -> dict[str, list[tuple[Any, ...]]]:
    """Every wanted entity's compact events in [start - 2 days, end), sorted (records of renamed ids included).

    Reads one day file at a time and keeps only the compact events, so memory grows with the number of
    records of the room's own entities, not with the size of the store.
    """
    out: dict[str, list[tuple[Any, ...]]] = {e: [] for e in kinds}
    if view.directory is None or not view.directory.is_dir():
        return out
    first = datetime.fromtimestamp(start, UTC).date() - timedelta(days=2)
    last = datetime.fromtimestamp(end, UTC).date()
    days = {(first + timedelta(days=i)).strftime("%Y%m%d") for i in range((last - first).days + 1)}
    files = sorted(view.directory.glob("obs-*.jsonl"))
    _scan([p for p in files if p.stem[4:] in days], view, kinds, out, end)
    # A state that did not change for days has no record in the lookback: find its last one further back.
    missing = {e: k for e, k in kinds.items() if k in STATE_KINDS and not any(ev[0] <= start for ev in out[e])}
    older = [p for p in reversed(files) if p.stem[4:] < first.strftime("%Y%m%d")
             and p.stem[4:] >= (first - timedelta(days=STATE_LOOKBACK_DAYS)).strftime("%Y%m%d")]
    for path in older:
        if not missing:
            break
        found: dict[str, list[tuple[Any, ...]]] = {e: [] for e in missing}
        _scan([path], view, missing, found, end)
        for e, events in found.items():
            if events:
                out[e].append(max(events, key=lambda ev: ev[0]))
                del missing[e]
    for events in out.values():
        events.sort(key=lambda ev: ev[0])
    return out


def _scan(paths: list[Path], view: StoreView, kinds: dict[str, str], out: dict[str, list[tuple[Any, ...]]],
          end: float) -> None:
    alias = {old: e for e in kinds for old in view.aliases.get(e, []) if old not in kinds}
    marker = '"e":'
    for path in paths:
        try:
            handle = open(path, encoding="utf-8")  # noqa: SIM115 - closed below
        except OSError:
            continue
        with contextlib.closing(handle):
            for line in handle:
                # Skip other entities without parsing the line (the store writes compact JSON).
                i = line.find(marker)
                if i < 0:
                    continue
                i = line.find('"', i + 4)
                j = line.find('"', i + 1)
                if i < 0 or j < 0:
                    continue
                entity_id = line[i + 1:j]
                entity_id = alias.get(entity_id, entity_id)
                kind = kinds.get(entity_id)
                if kind is None:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # a line cut short by a power loss
                ev = _event(kind, rec)
                if ev is not None and ev[0] < end:
                    out[entity_id].append(ev)


# --------------------------------------------------------------- the grid
def grid_times(start: float, end: float) -> list[float]:
    first = math.ceil(start / GRID_S) * GRID_S
    n = max(0, int((end - first) // GRID_S) + 1)
    return [first + i * GRID_S for i in range(n)]


def _observed_mask(times: list[float], coverage: dict[str, float] | None, gaps: list[list[float]]) -> list[bool]:
    if not coverage:
        return [False] * len(times)
    lo, hi = coverage["from"], coverage["to"]
    spans = sorted((a, b) for a, b in gaps)
    out = []
    k = 0
    for t in times:
        while k < len(spans) and spans[k][1] <= t:
            k += 1
        in_gap = k < len(spans) and spans[k][0] <= t < spans[k][1]
        out.append(lo <= t <= hi and not in_gap)
    return out


def hold(times: list[float], events: list[tuple[Any, ...]], *, limit: float, observed: list[bool]
         ) -> tuple[list[Any], list[str | None]]:
    """(values, reasons): the bounded-hold value of the events at every grid time, or None with its reason
    (``not_observed``, ``stale`` or ``bad``)."""
    values: list[Any] = []
    reasons: list[str | None] = []
    k = 0
    last: tuple[Any, ...] | None = None
    n = len(events)
    for t, seen in zip(times, observed, strict=True):
        while k < n and events[k][0] <= t:
            last = events[k]
            k += 1
        if not seen or last is None:
            values.append(None)
            reasons.append("not_observed")
        elif t - last[0] > limit:
            values.append(None)
            reasons.append("stale")
        elif last[1] is None:
            values.append(None)
            reasons.append("bad")
        else:
            values.append(last[1])
            reasons.append(None)
    return values, reasons


# ---------------------------------------------------------------- the sun
def _angle_diff(a: float, b: float) -> float:
    d = (a - b) % 360.0
    return d - 360.0 if d > 180.0 else d


def _in_arc(value: float, start: float, end: float) -> bool:
    value, start, end = value % 360, start % 360, end % 360
    return start <= value <= end if start <= end else value >= start or value <= end


def window_geometry(w: WindowSpec, elevation: float, azimuth: float) -> float:
    """How directly the sun shines on the window's glass: cos(incidence) while it is *exposed* by the Curtains
    module's own rules (sector width, minimum and maximum elevation, obstructions), else 0. Geometry only:
    whether it actually shines is the weather's business (clouds), which this does not know."""
    if w.bearing is None or elevation <= 0:
        return 0.0
    offset = _angle_diff(azimuth, w.bearing)
    if abs(offset) > w.exposure_width or abs(offset) >= 90 or elevation < w.min_elevation:
        return 0.0
    if w.max_elevation is not None and elevation > w.max_elevation:
        return 0.0
    if any(_in_arc(azimuth, a, b) and elevation < m for a, b, m in w.obstructions):
        return 0.0
    return max(0.0, math.cos(math.radians(elevation)) * math.cos(math.radians(offset)))


def transmission(layers: list[tuple[str, str]], positions: dict[str, float | None]) -> float | None:
    """The share of sun the window's covers let through (1 = no cover or all open), None when a position is
    not known. A closed sheer still lets ``SHEER_TRANSMISSION`` through."""
    out = 1.0
    for entity_id, kind in layers:
        pos = positions.get(entity_id)
        if pos is None:
            return None
        closed = 1.0 - max(0.0, min(100.0, pos)) / 100.0
        out *= 1.0 - closed * (1.0 - SHEER_TRANSMISSION if kind == "sheer" else 1.0)
    return out


# ------------------------------------------------------------ preparation
@dataclass
class RoomGrid:
    area_id: str
    times: list[float]
    temp: list[float | None]
    source: list[str | None]
    temp_reason: list[str | None]
    heat: list[float | None]
    heat_inf: list[float | None]
    heat_basis: str                      # reported | inferred | mixed | none
    window: list[str]                    # closed | open | unknown | none | settling
    outdoor: list[float | None]
    solar: list[float | None] | None     # None: no window geometry (direct sun cannot be estimated)
    solar_basis: str                     # geometry+illuminance | geometry | none
    night: list[int]
    setpoint: list[float | None]
    neighbours: dict[str, list[float | None]] = field(default_factory=dict)
    why: list[str] = field(default_factory=list)
    discontinuities: int = 0
    notes: list[str] = field(default_factory=list)
    devices: list[str] = field(default_factory=list)  # the heating devices whose reported share is ``heat``


def _night_flags(times: list[float], tz_name: str | None) -> list[int]:
    tz: Any = UTC
    if tz_name:
        try:
            from zoneinfo import ZoneInfo  # noqa: PLC0415

            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 - an unknown zone falls back to UTC
            tz = UTC
    lo, hi = NIGHT_HOURS
    out = []
    for t in times:
        hour = datetime.fromtimestamp(t, tz).hour
        out.append(1 if hour >= lo or hour < hi else 0)
    return out


def _room_temperature(spec: RoomSpec, times: list[float], held: dict[str, tuple[list[Any], list[str | None]]]
                      ) -> tuple[list[float | None], list[str | None], list[str | None]]:
    temps: list[float | None] = []
    sources: list[str | None] = []
    reasons: list[str | None] = []
    for i in range(len(times)):
        if spec.primary and held[spec.primary][0][i] is not None:
            temps.append(held[spec.primary][0][i])
            sources.append("p")
            reasons.append(None)
            continue
        usable = [(e, held[e][0][i]) for e in spec.secondary if held[e][0][i] is not None]
        if usable:
            temps.append(sum(v for _e, v in usable) / len(usable))
            sources.append("s:" + ",".join(e for e, _v in usable))
            reasons.append(None)
            continue
        temps.append(None)
        sources.append(None)
        names = ([spec.primary] if spec.primary else []) + spec.secondary
        unobserved = all(held[e][1][i] == "not_observed" for e in names) if names else True
        reasons.append("not_observed" if unobserved else "no_temperature")
    return temps, sources, reasons


def _heating(spec: RoomSpec, temps: list[float | None], held: dict[str, tuple[list[Any], list[str | None]]]
             ) -> tuple[list[float | None], list[float | None], list[bool], str]:
    """(reported fraction, inferred fraction, active-cooling flags, basis) per step."""
    n = len(temps)
    devices = spec.thermostats
    if not devices:
        return [0.0] * n, [None] * n, [False] * n, "none"
    reports_any = {e: any(isinstance(v, tuple) and v[1] is not None for v in held[e][0]) for e in devices}
    heat: list[float | None] = []
    inferred: list[float | None] = []
    cooling: list[bool] = []
    for i in range(n):
        on = known = 0
        demand = 0
        demand_known = True
        cool = False
        for e in devices:
            v = held[e][0][i]
            if v is None:
                demand_known = False
                continue
            mode, action, target = v
            if action in EXCLUDED_ACTIONS:
                cool = True
            if mode == "off":
                known += 1
                continue
            if action is not None:
                if action in HEATING_ACTIONS:
                    on += 1
                    known += 1
                    demand += 1
                elif action in NOT_HEATING_ACTIONS:
                    known += 1
                # Other actions (cooling, defrosting, unknown values): not known as heating or passive.
                continue
            # The device does not report its activity: only a setpoint above the room temperature hints at demand.
            room = temps[i]
            if target is None or room is None:
                demand_known = False
            elif target > room + DEMAND_MARGIN_C:
                demand += 1
        heat.append(on / len(devices) if known == len(devices) else None)
        inferred.append(demand / len(devices) if demand_known else None)
        cooling.append(cool)
    reporting = [e for e in devices if reports_any[e]]
    basis = "reported" if len(reporting) == len(devices) else "inferred" if not reporting else "mixed"
    return heat, inferred, cooling, basis


def prepare_room(spec: RoomSpec, view: StoreView, start: float, end: float, *, tz: str | None,
                 events: dict[str, list[tuple[Any, ...]]] | None = None,
                 neighbour_temps: dict[str, list[float | None]] | None = None) -> RoomGrid:
    """The room's aligned grid for [start, end) with every step classified."""
    kinds = spec.entities()
    times = grid_times(start, end)
    if events is None:
        events = read_events(view, kinds, start, end)
    held: dict[str, tuple[list[Any], list[str | None]]] = {}
    for entity_id, kind in kinds.items():
        limit = spec.stale_s if kind == "room_temp" else HOLD.get(kind, 3600.0)
        observed = _observed_mask(times, view.coverage.get(entity_id), view.gaps.get(entity_id, []))
        held[entity_id] = hold(times, events.get(entity_id, []), limit=limit, observed=observed)
    temps, sources, temp_reasons = _room_temperature(spec, times, held)
    heat, heat_inf, cooling, basis = _heating(spec, temps, held)
    n = len(times)

    # Windows and doors: any open = open; any unknown = unknown; then a settling time after closing.
    window: list[str] = []
    last_open = -1e18
    for i in range(n):
        if not spec.openings:
            state = "none"
        else:
            values = [held[e][0][i] for e in spec.openings]
            state = "open" if any(v == 1 for v in values) else "unknown" if any(v is None for v in values) \
                else "closed"
        if state == "open":
            last_open = times[i]
        elif state in ("closed", "none") and times[i] - last_open < WINDOW_SETTLE_S:
            state = "settling"
        window.append(state)

    outdoor = held[spec.outdoor][0] if spec.outdoor else [None] * n

    # Direct sun on the room's windows (geometry x what the covers let through x how bright it is).
    geo_windows = [w for w in spec.windows if w.bearing is not None]
    solar: list[float | None] | None = None
    solar_basis = "none"
    if geo_windows and "sun.sun" in kinds:
        sensors = [s for w in geo_windows for s in w.sun_sensors]
        lux_scale = _lux_scale([v for s in sensors for v in held[s][0] if isinstance(v, float)]) if sensors else None
        solar_basis = "geometry+illuminance" if lux_scale else "geometry"
        solar = []
        sun = held["sun.sun"][0]
        for i in range(n):
            pos = sun[i]
            if pos is None:
                solar.append(None)
                continue
            elevation, azimuth = pos
            total: float | None = 0.0
            for w in geo_windows:
                g = window_geometry(w, elevation, azimuth)
                if g <= 0:
                    continue
                tr = transmission(w.layers, {c: held[c][0][i] for c, _k in w.layers})
                if tr is None:
                    total = None
                    break
                bright = 1.0
                if lux_scale and w.sun_sensors:
                    readings = [held[s][0][i] for s in w.sun_sensors if held[s][0][i] is not None]
                    if not readings:
                        total = None
                        break
                    bright = min(1.0, max(readings) / lux_scale)
                total += g * tr * bright
            solar.append(None if total is None else round(total, 4))

    setpoint: list[float | None] = []
    for i in range(n):
        targets = [held[e][0][i][2] for e in spec.own_thermostats or spec.thermostats
                   if held[e][0][i] is not None and held[e][0][i][2] is not None and held[e][0][i][0] != "off"]
        setpoint.append(sum(targets) / len(targets) if targets else None)

    grid = RoomGrid(area_id=spec.area_id, times=times, temp=temps, source=sources, temp_reason=temp_reasons,
                    heat=heat, heat_inf=heat_inf, heat_basis=basis, window=window, outdoor=outdoor, solar=solar,
                    solar_basis=solar_basis, night=_night_flags(times, tz), setpoint=setpoint,
                    neighbours=dict(neighbour_temps or {}), devices=list(spec.thermostats))
    _classify(grid, cooling)
    return grid


def _lux_scale(values: list[float]) -> float | None:
    """The 95th percentile of the daytime readings: "as bright as it gets here" (sensors differ widely)."""
    positive = sorted(v for v in values if v > 0)
    if len(positive) < 50:
        return None
    return max(1.0, positive[int(0.95 * (len(positive) - 1))])


def _classify(grid: RoomGrid, cooling: list[bool]) -> None:
    n = len(grid.times)
    why: list[str] = []
    discontinuities = 0
    for i in range(n):
        t = grid.temp[i]
        if t is None:
            why.append(grid.temp_reason[i] or "no_temperature")
            continue
        prev = grid.temp[i - 1] if i else None
        if prev is not None and grid.source[i] == grid.source[i - 1] and abs(t - prev) > JUMP_C:
            discontinuities += 1
            why.append("discontinuity")
            continue
        w = grid.window[i]
        if w == "open":
            why.append("window_open")
        elif w == "settling":
            why.append("window_settling")
        elif w == "unknown":
            why.append("window_unknown")
        elif cooling[i]:
            why.append("active_cooling")
        elif grid.heat[i] is None and (grid.heat_basis != "inferred" or grid.heat_inf[i] is None):
            why.append("heating_unknown")
        else:
            why.append("usable")
    grid.why = why
    grid.discontinuities = discontinuities


def heating_of(grid: RoomGrid) -> list[float | None]:
    """The heating input the fit uses: reported where known; for an inferred room, the inferred demand."""
    if grid.heat_basis == "inferred":
        return [h if h is not None else hi for h, hi in zip(grid.heat, grid.heat_inf, strict=True)]
    return grid.heat


# ----------------------------------------------------------- the evidence
@dataclass
class Episode:
    kind: str           # heating | passive | solar
    start: float
    end: float
    temp_start: float
    temp_end: float
    outdoor: float | None

    @property
    def hours(self) -> float:
        return (self.end - self.start) / 3600.0

    @property
    def rate(self) -> float:
        return (self.temp_end - self.temp_start) / max(self.hours, 1e-9)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "start": round(self.start), "end": round(self.end), "hours": round(self.hours, 2),
                "temp_start": round(self.temp_start, 2), "temp_end": round(self.temp_end, 2),
                "outdoor": None if self.outdoor is None else round(self.outdoor, 1), "rate": round(self.rate, 3)}


MIN_EPISODE_STEPS = {"heating": 3, "passive": 12, "solar": 6}


def episodes(grid: RoomGrid) -> list[Episode]:
    """Runs of usable steps with one source: confirmed heating (reported heating > 0), passive (confirmed not
    heating) and sunny passive (direct-sun index above 0.2). Inferred demand never makes a heating episode."""
    out: list[Episode] = []
    n = len(grid.times)

    def label(i: int) -> str | None:
        if grid.why[i] != "usable":
            return None
        h = grid.heat[i]
        if h is None:
            return None
        if h > 0:
            return "heating"
        if grid.solar is not None and (grid.solar[i] or 0) > 0.2:
            return "solar"
        return "passive"

    i = 0
    while i < n:
        kind = label(i)
        if kind is None:
            i += 1
            continue
        j = i
        while j + 1 < n and label(j + 1) == kind and grid.source[j + 1] == grid.source[i]:
            j += 1
        if j - i >= MIN_EPISODE_STEPS[kind]:
            outs = [o for o in grid.outdoor[i:j + 1] if o is not None]
            out.append(Episode(kind, grid.times[i], grid.times[j], grid.temp[i], grid.temp[j],  # type: ignore[arg-type]
                               sum(outs) / len(outs) if outs else None))
        i = j + 1
    return out


def exclusions(grid: RoomGrid) -> dict[str, float]:
    """Hours per classification."""
    out: dict[str, float] = {}
    for reason in grid.why:
        out[reason] = out.get(reason, 0.0) + GRID_S / 3600.0
    return {k: round(v, 2) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def secondary_hours(grid: RoomGrid) -> float:
    return round(sum(GRID_S for s, w in zip(grid.source, grid.why, strict=True)
                     if s and s.startswith("s:") and w == "usable") / 3600.0, 2)


def runs(flags: Iterable[bool]) -> int:
    count, prev = 0, False
    for f in flags:
        if f and not prev:
            count += 1
        prev = f
    return count
