"""Occupancy forecast model (0.5.0, Phase 4): pure statistics, no Home Assistant dependency.

What is learned
---------------
Everything is learned from *daily summaries* (``HouseDay``): per room and
local hour, how long the room was observed vacant or occupied, how often it
was entered and left, at what visit / vacancy age, under which context
(sleep, workday, PC activity, a connected room occupied, ...), plus counts of
room-to-room movements with their delays. Summaries are aggregates: no
sequence of individual movements is ever stored.

The model
---------
A time-inhomogeneous two-state Markov chain per room (vacant <-> occupied),
with transparent rates:

``entry rate``  = lambda(weekday, day type, hour) x g_vacant(vacancy age) x contexts x movement
``exit rate``   = mu(weekday, day type, hour)     x g_occupied(visit age)  x contexts

* lambda and mu are hierarchical estimates: an hour's rate on one weekday is
  shrunk towards the same hour on that day type (workday / day off), which is
  shrunk towards the same hour on every day, which is shrunk towards
  neighbouring hours and the room's overall rate. Little data therefore means
  "like the room usually is", never an extreme probability.
* g are visit-age and vacancy-age factors (people who just left often come
  back; short visits end sooner than long ones). They and lambda / mu are fitted
  by alternating updates (backfitting), so they do not double count.
* Context factors are learned by indirect standardisation: observed entries
  (exits) while a context was on, divided by what the hour / day pattern alone
  would have expected. Shrunk towards 1.
* Movement: after an arrival in room A, the probability that room B is the
  next room entered within ``MAX_GAP`` seconds, and the delay distribution,
  by part of day and day type (shrunk towards the overall value).

A forecast steps the chain forward one minute at a time from the room's
current state and visit / vacancy age, and returns P(room occupied at now + H)
for each horizon H, plus, for a vacant room, P(entered within H).

Nothing here sends commands or touches Home Assistant.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
import math
from typing import Any

# Forecast horizons (minutes). The model works for any horizon; these are the ones published.
HORIZONS: tuple[int, ...] = (5, 15, 30, 60)
MAX_HORIZON = max(HORIZONS)
STEP_SECONDS = 60
# Prequential evaluation: a forecast is issued every this many minutes of each completed day.
EVAL_EVERY_MINUTES = 15
HOURS = 24

# Visit age buckets (minutes; lower edges) and vacancy age buckets.
OCC_AGE_EDGES: tuple[int, ...] = (0, 5, 15, 30, 60, 120)
VAC_AGE_EDGES: tuple[int, ...] = (0, 5, 15, 60, 240)
OCC_AGE_CAP = OCC_AGE_EDGES[-1]
VAC_AGE_CAP = VAC_AGE_EDGES[-1]

# Movement: an arrival in another room this soon after an arrival is a movement between them.
MAX_GAP_SECONDS = 180
# Delay buckets (upper edges, seconds).
DELAY_EDGES: tuple[int, ...] = (5, 15, 30, 60, 120, 180)
# Arrivals this close together in rooms that share a sensor (or are open plan) are one detection, not a move.
OVERLAP_SECONDS = 3
# Room activity that stops for less than this is the same activity episode.
EPISODE_GAP_SECONDS = 30
# Day parts used for movement statistics.
DAY_PARTS: tuple[tuple[int, int, str], ...] = ((0, 6, "night"), (6, 12, "morning"), (12, 18, "afternoon"),
                                              (18, 24, "evening"))

# Shrinkage strengths.
PRIOR_SECONDS = 7200.0        # rates and occupancy fractions: ~2 observed hours of a bin
RATIO_PRIOR = 5.0             # age and context factors: ~5 events
TRANSITION_PRIOR = 10.0       # movement probabilities: ~10 arrivals
FACTOR_MIN, FACTOR_MAX = 0.05, 20.0

# Protection against anomalies: a single day contributes at most this many events per hour / pair.
MAX_EVENTS_PER_HOUR = 12
MAX_ARRIVALS_PER_HOUR = 60
MAX_TRANSITIONS_PER_PAIR = 40

CAL_BINS = 10
# A summary made by an older version of the replay is processed again while the Recorder still has it.
SUMMARY_VERSION = 1

# Context key of "a connected room is occupied" (derived from the room relationships).
ADJACENT_CONTEXT = "~connected_occupied"

CONFIDENCE_LEVELS = ("insufficient_data", "low", "moderate", "high")


# --------------------------------------------------------------------- utils
def bucket(value: float, edges: tuple[int, ...]) -> int:
    """Index of the bucket whose lower edge is the largest one <= value."""
    return max(0, bisect_right(edges, value) - 1)


def day_part(hour: int) -> int:
    for i, (lo, hi, _) in enumerate(DAY_PARTS):
        if lo <= hour < hi:
            return i
    return len(DAY_PARTS) - 1


def _sparse(values: list[float], digits: int = 0) -> dict[str, float]:
    out: dict[str, float] = {}
    for i, v in enumerate(values):
        if v:
            out[str(i)] = round(v, digits) if digits else int(round(v))
    return out


def _dense(data: Any, size: int) -> list[float]:
    out = [0.0] * size
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if 0 <= i < size and isinstance(v, (int, float)):
                out[i] = float(v)
    return out


def num(value: float, digits: int = 2) -> float | int:
    """Compact JSON number: whole numbers without a fraction."""
    rounded = round(value, digits)
    return int(rounded) if float(rounded).is_integer() else rounded


def _joint_sparse(table: dict[tuple[int, int], float]) -> dict[str, float]:
    return {f"{h}:{k}": (int(round(v)) if v >= 1 else round(v, 2)) for (h, k), v in table.items() if v}


def _joint_dense(data: Any) -> dict[tuple[int, int], float]:
    out: dict[tuple[int, int], float] = {}
    if isinstance(data, dict):
        for key, v in data.items():
            try:
                h, k = (int(x) for x in str(key).split(":", 1))
            except ValueError:
                continue
            if isinstance(v, (int, float)) and v:
                out[(h, k)] = float(v)
    return out


def clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


# ----------------------------------------------------------- daily summaries
@dataclass(slots=True)
class ContextDay:
    """Exposure of one room while one context was on, by hour."""

    vac: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    ent: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    occ: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    ext: list[float] = field(default_factory=lambda: [0.0] * HOURS)

    def as_dict(self) -> dict[str, Any]:
        return {k: _sparse(getattr(self, k)) for k in ("vac", "ent", "occ", "ext") if any(getattr(self, k))}

    @classmethod
    def from_dict(cls, data: Any) -> ContextDay:
        data = data if isinstance(data, dict) else {}
        return cls(*(_dense(data.get(k), HOURS) for k in ("vac", "ent", "occ", "ext")))


@dataclass(slots=True)
class RoomDay:
    """One room on one local day. Seconds and counts; everything else is derived."""

    # (hour, visit age bucket) -> occupied seconds / exits; (hour, vacancy age bucket) -> vacant seconds / entries.
    oa: dict[tuple[int, int], float] = field(default_factory=dict)
    xa: dict[tuple[int, int], float] = field(default_factory=dict)
    va: dict[tuple[int, int], float] = field(default_factory=dict)
    ea: dict[tuple[int, int], float] = field(default_factory=dict)
    # Activity onsets that are arrivals (movement statistics denominators), by hour.
    arr: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    cx: dict[str, ContextDay] = field(default_factory=dict)
    # Prequential evaluation: horizon -> [n, brier model, brier schedule, brier persistence].
    ev: dict[int, list[float]] = field(default_factory=dict)
    # horizon -> calibration bin -> [n, sum p, sum outcome].
    cal: dict[int, dict[int, list[float]]] = field(default_factory=dict)
    skipped: int = 0  # forecasts not evaluated because the model of that day had too little data

    def hourly(self, table: dict[tuple[int, int], float]) -> list[float]:
        out = [0.0] * HOURS
        for (h, _k), v in table.items():
            if 0 <= h < HOURS:
                out[h] += v
        return out

    @property
    def occ(self) -> list[float]:
        return self.hourly(self.oa)

    @property
    def vac(self) -> list[float]:
        return self.hourly(self.va)

    @property
    def ent(self) -> list[float]:
        return self.hourly(self.ea)

    @property
    def ext(self) -> list[float]:
        return self.hourly(self.xa)

    @property
    def observed_seconds(self) -> float:
        return sum(self.oa.values()) + sum(self.va.values())

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"oa": _joint_sparse(self.oa), "xa": _joint_sparse(self.xa),
                               "va": _joint_sparse(self.va), "ea": _joint_sparse(self.ea), "arr": _sparse(self.arr)}
        cx = {k: c.as_dict() for k, c in self.cx.items()}
        if cx := {k: v for k, v in cx.items() if v}:
            out["cx"] = cx
        if self.ev:
            out["ev"] = {str(h): [num(x, 4) for x in v] for h, v in self.ev.items()}
        if self.cal:
            out["cal"] = {str(h): {str(b): [num(x) for x in v] for b, v in bins.items()}
                          for h, bins in self.cal.items()}
        if self.skipped:
            out["skipped"] = self.skipped
        return out

    @classmethod
    def from_dict(cls, data: Any) -> RoomDay:
        data = data if isinstance(data, dict) else {}
        ev: dict[int, list[float]] = {}
        for h, v in (data.get("ev") or {}).items():
            if isinstance(v, list) and len(v) == 4:
                ev[int(h)] = [float(x) for x in v]
        cal: dict[int, dict[int, list[float]]] = {}
        for h, bins in (data.get("cal") or {}).items():
            if isinstance(bins, dict):
                cal[int(h)] = {int(b): [float(x) for x in v] for b, v in bins.items()
                               if isinstance(v, list) and len(v) == 3}
        return cls(
            oa=_joint_dense(data.get("oa")), xa=_joint_dense(data.get("xa")),
            va=_joint_dense(data.get("va")), ea=_joint_dense(data.get("ea")),
            arr=_dense(data.get("arr"), HOURS),
            cx={str(k): ContextDay.from_dict(v) for k, v in (data.get("cx") or {}).items()},
            ev=ev, cal=cal, skipped=int(data.get("skipped") or 0),
        )


@dataclass(slots=True)
class TransitionDay:
    """Movements from one room to another on one day."""

    n: float = 0.0
    hours: list[float] = field(default_factory=lambda: [0.0] * HOURS)
    delays: list[float] = field(default_factory=lambda: [0.0] * len(DELAY_EDGES))

    def as_dict(self) -> dict[str, Any]:
        return {"n": int(self.n), "h": _sparse(self.hours), "d": [int(x) for x in self.delays]}

    @classmethod
    def from_dict(cls, data: Any) -> TransitionDay:
        data = data if isinstance(data, dict) else {}
        delays = data.get("d") if isinstance(data.get("d"), list) else []
        d = [float(x) for x in delays[: len(DELAY_EDGES)]] + [0.0] * (len(DELAY_EDGES) - len(delays))
        return cls(n=float(data.get("n") or 0), hours=_dense(data.get("h"), HOURS), delays=d)


@dataclass(slots=True)
class HouseDay:
    """Every room of the house on one local day (``date`` = ISO date)."""

    date: str
    dow: int
    workday: bool
    rooms: dict[str, RoomDay] = field(default_factory=dict)
    trans: dict[tuple[str, str], TransitionDay] = field(default_factory=dict)
    complete: bool = True
    version: int = SUMMARY_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {"date": self.date, "dow": self.dow, "wd": self.workday, "v": self.version,
                "rooms": {a: r.as_dict() for a, r in self.rooms.items()},
                "trans": {f"{a}>{b}": t.as_dict() for (a, b), t in self.trans.items() if t.n}}

    @classmethod
    def from_dict(cls, data: Any) -> HouseDay | None:
        if not isinstance(data, dict) or not isinstance(data.get("date"), str):
            return None
        try:
            trans: dict[tuple[str, str], TransitionDay] = {}
            for key, value in (data.get("trans") or {}).items():
                a, _, b = str(key).partition(">")
                if a and b:
                    trans[(a, b)] = TransitionDay.from_dict(value)
            return cls(
                date=data["date"], dow=int(data.get("dow", 0)) % 7, workday=bool(data.get("wd")),
                rooms={str(a): RoomDay.from_dict(r) for a, r in (data.get("rooms") or {}).items()},
                trans=trans, version=int(data.get("v") or 0),
            )
        except (TypeError, ValueError):
            return None


# ------------------------------------------------------------------ rates
@dataclass(slots=True)
class RateTable:
    """A hierarchical rate (or fraction) by weekday, day type and hour."""

    overall: float
    hour: list[float]
    daytype: dict[bool, list[float]]
    table: dict[tuple[int, bool], list[float]]

    def at(self, dow: int, workday: bool, hour: int) -> float:
        row = self.table.get((dow, workday))
        if row is not None:
            return row[hour]
        return self.daytype[workday][hour]


def build_rates(samples: Iterable[tuple[float, int, bool, list[float], list[float]]],
                prior: float = PRIOR_SECONDS, overall_prior: tuple[float, float] = (0.1, 3600.0)) -> RateTable:
    """``samples``: (weight, weekday, workday, events[24], exposure[24]) per day.

    Returns events per unit of exposure, shrunk level by level (see module docstring).
    """
    e_h = [0.0] * HOURS
    x_h = [0.0] * HOURS
    e_t = {True: [0.0] * HOURS, False: [0.0] * HOURS}
    x_t = {True: [0.0] * HOURS, False: [0.0] * HOURS}
    e_d: dict[tuple[int, bool], list[float]] = {}
    x_d: dict[tuple[int, bool], list[float]] = {}
    for w, dow, wd, events, exposure in samples:
        if w <= 0:
            continue
        ed = e_d.setdefault((dow, wd), [0.0] * HOURS)
        xd = x_d.setdefault((dow, wd), [0.0] * HOURS)
        for h in range(HOURS):
            e, x = events[h] * w, exposure[h] * w
            e_h[h] += e
            x_h[h] += x
            e_t[wd][h] += e
            x_t[wd][h] += x
            ed[h] += e
            xd[h] += x
    a0, b0 = overall_prior
    overall = (sum(e_h) + a0) / (sum(x_h) + b0)
    smooth = []
    for h in range(HOURS):
        ew = e_h[h - 1] + e_h[h] + e_h[(h + 1) % HOURS]
        xw = x_h[h - 1] + x_h[h] + x_h[(h + 1) % HOURS]
        smooth.append((ew + prior * overall) / (xw + prior))
    hour = [(e_h[h] + prior * smooth[h]) / (x_h[h] + prior) for h in range(HOURS)]
    daytype = {t: [(e_t[t][h] + prior * hour[h]) / (x_t[t][h] + prior) for h in range(HOURS)] for t in (True, False)}
    table = {key: [(e_d[key][h] + prior * daytype[key[1]][h]) / (x_d[key][h] + prior) for h in range(HOURS)]
             for key in e_d}
    return RateTable(overall=overall, hour=hour, daytype=daytype, table=table)


def _ratio(observed: float, expected: float, prior: float = RATIO_PRIOR) -> float:
    return clamp((observed + prior) / (expected + prior), FACTOR_MIN, FACTOR_MAX)


@dataclass(slots=True)
class ContextFactor:
    """How entries and exits change while a context is on / off (indirect standardisation)."""

    key: str
    entry_on: float = 1.0
    entry_off: float = 1.0
    exit_on: float = 1.0
    exit_off: float = 1.0
    entries_on: float = 0.0   # observed (weighted) entries while on, for explanations
    expected_on: float = 0.0
    exposure_on: float = 0.0

    def entry(self, value: bool | None) -> float:
        return 1.0 if value is None else self.entry_on if value else self.entry_off

    def exit(self, value: bool | None) -> float:
        return 1.0 if value is None else self.exit_on if value else self.exit_off

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "entry_on": round(self.entry_on, 3), "entry_off": round(self.entry_off, 3),
                "exit_on": round(self.exit_on, 3), "exit_off": round(self.exit_off, 3),
                "entries_on": round(self.entries_on, 1), "expected_on": round(self.expected_on, 1),
                "hours_on": round(self.exposure_on / 3600, 1)}


@dataclass(slots=True)
class RoomStats:
    """Evidence behind a room's model (for confidence and the UI)."""

    days: int = 0                 # days with at least OBSERVED_DAY_SECONDS observed
    weekdays: int = 0             # distinct weekdays among them
    observed_hours: float = 0.0
    entries: float = 0.0          # unweighted entries in the window
    exits: float = 0.0
    first_date: str | None = None
    last_date: str | None = None


OBSERVED_DAY_SECONDS = 6 * 3600


@dataclass(slots=True)
class RoomModel:
    area_id: str
    entry: RateTable
    exit: RateTable
    occupancy: RateTable          # fraction of observed time occupied (the schedule baseline)
    g_vacant: list[float]
    g_occupied: list[float]
    contexts: dict[str, ContextFactor]
    stats: RoomStats

    def entry_rate(self, dow: int, wd: bool, hour: int) -> float:
        return self.entry.at(dow, wd, hour)

    def exit_rate(self, dow: int, wd: bool, hour: int) -> float:
        return self.exit.at(dow, wd, hour)


@dataclass(slots=True)
class PairModel:
    """Movement from one room to another."""

    source: str
    target: str
    count: float                  # weighted
    probability: float            # overall P(next room = target | arrival in source)
    by_part: dict[tuple[int, bool], float]
    delay_cdf: list[float]        # at the DELAY_EDGES
    median_delay: float

    def p(self, part: int, workday: bool) -> float:
        return self.by_part.get((part, workday), self.probability)

    def cdf(self, seconds: float) -> float:
        """P(delay <= seconds), piecewise linear between the bucket edges."""
        if seconds <= 0:
            return 0.0
        lo_x, lo_y = 0.0, 0.0
        for x, y in zip(DELAY_EDGES, self.delay_cdf, strict=True):
            if seconds <= x:
                return lo_y + (y - lo_y) * (seconds - lo_x) / (x - lo_x)
            lo_x, lo_y = float(x), y
        return 1.0


@dataclass(slots=True)
class HouseModel:
    rooms: dict[str, RoomModel]
    pairs: dict[tuple[str, str], PairModel]
    arrivals: dict[str, float]    # weighted arrivals per room
    days: int
    dates: list[str]
    built_from: str | None = None  # newest date included

    def outgoing(self, source: str) -> list[PairModel]:
        return sorted((p for (a, _b), p in self.pairs.items() if a == source), key=lambda p: -p.probability)

    def incoming(self, target: str) -> list[tuple[PairModel, float]]:
        """Pairs into ``target`` with the share of its arrivals that came from each source."""
        items = [p for (_a, b), p in self.pairs.items() if b == target]
        total = sum(p.count for p in items)
        return sorted(((p, p.count / total if total else 0.0) for p in items), key=lambda x: -x[1])


def day_weight(age_days: int, half_life_days: float) -> float:
    if half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(age_days, 0) / half_life_days)


def _fit_age(days: list[tuple[float, HouseDay, RoomDay]], counts: str, exposure: str, edges: tuple[int, ...]
             ) -> tuple[RateTable, list[float], dict[tuple[str, int], list[float]]]:
    """Hour/day rates and age factors by backfitting (3 rounds). Also returns the age-adjusted hourly exposure."""
    k_n = len(edges)
    g = [1.0] * k_n
    rates: RateTable | None = None
    adjusted: dict[tuple[str, int], list[float]] = {}
    for _round in range(3):
        samples = []
        adjusted = {}
        for w, day, room in days:
            events = room.hourly(getattr(room, counts))
            x_adj = [0.0] * HOURS
            for (h, k), sec in getattr(room, exposure).items():
                if 0 <= h < HOURS and 0 <= k < k_n:
                    x_adj[h] += sec * g[k]
            adjusted[(day.date, id(room))] = x_adj
            samples.append((w, day.dow, day.workday, events, x_adj))
        rates = build_rates(samples)
        observed = [0.0] * k_n
        expected = [0.0] * k_n
        for w, day, room in days:
            for (h, k), n in getattr(room, counts).items():
                if 0 <= k < k_n:
                    observed[k] += w * n
            for (h, k), sec in getattr(room, exposure).items():
                if 0 <= h < HOURS and 0 <= k < k_n:
                    expected[k] += w * sec * rates.at(day.dow, day.workday, h)
        g = [_ratio(observed[k], expected[k]) for k in range(k_n)]
    assert rates is not None
    return rates, g, adjusted


def build_room_model(area_id: str, days: list[tuple[float, HouseDay]], contexts: Iterable[str]) -> RoomModel:
    """``days``: (weight, day) pairs, any order."""
    room_days = [(w, d, d.rooms[area_id]) for w, d in days if area_id in d.rooms]
    entry, g_vac, adj_vac = _fit_age(room_days, "ea", "va", VAC_AGE_EDGES)
    exit_, g_occ, adj_occ = _fit_age(room_days, "xa", "oa", OCC_AGE_EDGES)
    occupancy = build_rates(
        ((w, d.dow, d.workday, r.occ, [o + v for o, v in zip(r.occ, r.vac, strict=True)]) for w, d, r in room_days),
        overall_prior=(0.0, 3600.0),
    )
    factors: dict[str, ContextFactor] = {}
    for key in contexts:
        o_on = o_tot = e_on = e_tot = 0.0
        xo_on = xo_tot = xe_on = xe_tot = 0.0
        exposure_on = 0.0
        for w, d, r in room_days:
            c = r.cx.get(key)
            vac, ent, occ, ext = r.vac, r.ent, r.occ, r.ext
            xv = adj_vac[(d.date, id(r))]
            xo = adj_occ[(d.date, id(r))]
            for h in range(HOURS):
                lam = entry.at(d.dow, d.workday, h)
                mu = exit_.at(d.dow, d.workday, h)
                gv = xv[h] / vac[h] if vac[h] else 1.0
                go = xo[h] / occ[h] if occ[h] else 1.0
                o_tot += w * ent[h]
                e_tot += w * vac[h] * lam * gv
                xo_tot += w * ext[h]
                xe_tot += w * occ[h] * mu * go
                if c is not None:
                    o_on += w * c.ent[h]
                    e_on += w * c.vac[h] * lam * gv
                    xo_on += w * c.ext[h]
                    xe_on += w * c.occ[h] * mu * go
                    exposure_on += w * (c.vac[h] + c.occ[h])
        factors[key] = ContextFactor(
            key=key,
            entry_on=_ratio(o_on, e_on), entry_off=_ratio(o_tot - o_on, e_tot - e_on),
            exit_on=_ratio(xo_on, xe_on), exit_off=_ratio(xo_tot - xo_on, xe_tot - xe_on),
            entries_on=o_on, expected_on=e_on, exposure_on=exposure_on,
        )
    stats = RoomStats()
    observed_days = sorted(d.date for _w, d, r in room_days if r.observed_seconds >= OBSERVED_DAY_SECONDS)
    stats.days = len(observed_days)
    stats.weekdays = len({d.dow for _w, d, r in room_days if r.observed_seconds >= OBSERVED_DAY_SECONDS})
    stats.observed_hours = sum(r.observed_seconds for _w, _d, r in room_days) / 3600
    stats.entries = sum(sum(r.ea.values()) for _w, _d, r in room_days)
    stats.exits = sum(sum(r.xa.values()) for _w, _d, r in room_days)
    if observed_days:
        stats.first_date, stats.last_date = observed_days[0], observed_days[-1]
    return RoomModel(area_id=area_id, entry=entry, exit=exit_, occupancy=occupancy, g_vacant=g_vac,
                     g_occupied=g_occ, contexts=factors, stats=stats)


def build_house_model(days: list[HouseDay], areas: Iterable[str], contexts: Iterable[str], *, today: str,
                      half_life_days: float, ordinal: Callable[[str], int]) -> HouseModel:
    """Learn every room and the movements between them from ``days`` (window already applied)."""
    t = ordinal(today)
    weighted = [(day_weight(t - ordinal(d.date), half_life_days), d) for d in days]
    contexts = list(contexts)
    rooms = {a: build_room_model(a, weighted, contexts) for a in areas}
    # Movement: arrivals by (part, day type) and transitions by (part, day type).
    arrivals: dict[str, dict[tuple[int, bool], float]] = {}
    trans: dict[tuple[str, str], dict[tuple[int, bool], float]] = {}
    delays: dict[tuple[str, str], list[float]] = {}
    for w, d in weighted:
        for a, r in d.rooms.items():
            slot = arrivals.setdefault(a, {})
            for h in range(HOURS):
                if r.arr[h]:
                    key = (day_part(h), d.workday)
                    slot[key] = slot.get(key, 0.0) + w * r.arr[h]
        for pair, td in d.trans.items():
            slot = trans.setdefault(pair, {})
            for h in range(HOURS):
                if td.hours[h]:
                    key = (day_part(h), d.workday)
                    slot[key] = slot.get(key, 0.0) + w * td.hours[h]
            dl = delays.setdefault(pair, [0.0] * len(DELAY_EDGES))
            for i, v in enumerate(td.delays):
                dl[i] += w * v
    pairs: dict[tuple[str, str], PairModel] = {}
    total_arrivals = {a: sum(v.values()) for a, v in arrivals.items()}
    for (a, b), by_key in trans.items():
        n = sum(by_key.values())
        if n <= 0 or a not in total_arrivals:
            continue
        p_all = n / (total_arrivals[a] + 2.0)
        by_part: dict[tuple[int, bool], float] = {}
        for part in range(len(DAY_PARTS)):
            n_p = sum(v for (pp, _wd), v in by_key.items() if pp == part)
            a_p = sum(v for (pp, _wd), v in arrivals[a].items() if pp == part)
            p_part = (n_p + TRANSITION_PRIOR * p_all) / (a_p + TRANSITION_PRIOR)
            for wd in (True, False):
                n_pw = by_key.get((part, wd), 0.0)
                a_pw = arrivals[a].get((part, wd), 0.0)
                by_part[(part, wd)] = min((n_pw + TRANSITION_PRIOR * p_part) / (a_pw + TRANSITION_PRIOR), 0.99)
        dl = [x + 0.25 for x in delays[(a, b)]]  # light smoothing: no impossible delay
        total = sum(dl)
        cdf, run = [], 0.0
        for x in dl:
            run += x
            cdf.append(run / total)
        pairs[(a, b)] = PairModel(source=a, target=b, count=n, probability=min(p_all, 0.99), by_part=by_part,
                                  delay_cdf=cdf, median_delay=_median_from_cdf(cdf))
    observed = sorted({d.date for _w, d in weighted if any(r.observed_seconds for r in d.rooms.values())})
    return HouseModel(rooms=rooms, pairs=pairs, arrivals=total_arrivals, days=len(observed), dates=observed,
                      built_from=observed[-1] if observed else None)


def _median_from_cdf(cdf: list[float]) -> float:
    lo_x, lo_y = 0.0, 0.0
    for x, y in zip(DELAY_EDGES, cdf, strict=True):
        if y >= 0.5:
            return lo_x + (x - lo_x) * ((0.5 - lo_y) / (y - lo_y) if y > lo_y else 0.0)
        lo_x, lo_y = float(x), y
    return float(DELAY_EDGES[-1])


# ------------------------------------------------------------------ forecast
# Maps a timestamp to (weekday, workday, local hour). Supplied by the caller (time zone, workday sensor).
DayKey = Callable[[float], tuple[int, bool, int]]


@dataclass(slots=True)
class ForecastInput:
    now: float                                   # epoch seconds
    occupied: bool
    since: float | None                          # start of the current visit / vacancy
    contexts: dict[str, bool | None] = field(default_factory=dict)
    arrivals: list[tuple[str, float]] = field(default_factory=list)  # recent arrivals in other rooms (area, ts)
    since_exact: bool = True                     # False: ``since`` is only the start of what is known


@dataclass(slots=True)
class Factor:
    """One reason the forecast differs from the plain schedule."""

    kind: str          # schedule / current / movement / context / connected
    text: str
    impact: float      # change of the default-horizon probability, percentage points
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text, "impact": round(self.impact, 1), "details": self.details}


@dataclass(slots=True)
class Forecast:
    probabilities: dict[int, float]             # P(occupied at now + H)
    entry: dict[int, float] | None              # vacant room: P(entered within H)
    schedule: dict[int, float]                  # the time-of-week occupancy fraction at now + H (baseline)
    movement: dict[str, float] = field(default_factory=dict)   # source area -> P(movement into this room)
    entry_multiplier: float = 1.0
    exit_multiplier: float = 1.0


def _context_multipliers(model: RoomModel, contexts: dict[str, bool | None]) -> tuple[float, float]:
    ent = ext = 1.0
    for key, value in contexts.items():
        f = model.contexts.get(key)
        if f is None or value is None:
            continue
        ent *= f.entry(value)
        ext *= f.exit(value)
    return clamp(ent, FACTOR_MIN, FACTOR_MAX), clamp(ext, FACTOR_MIN, FACTOR_MAX)


def movement_hazards(house: HouseModel, target: str, inp: ForecastInput, daykey: DayKey, steps: int
                     ) -> tuple[list[float], dict[str, float]]:
    """Per step: P(someone moves into ``target`` in that minute | not yet), from recent arrivals elsewhere."""
    hazards = [0.0] * steps
    totals: dict[str, float] = {}
    for source, at in inp.arrivals:
        if source == target:
            continue
        pair = house.pairs.get((source, target))
        age = inp.now - at
        if pair is None or age < 0 or age > MAX_GAP_SECONDS:
            continue
        dow, wd, hour = daykey(at)
        p = pair.p(day_part(hour), wd)
        # Moves out of the source room that already happened (to any room) make this one less likely.
        done = sum(o.p(day_part(hour), wd) * o.cdf(age) for o in house.outgoing(source))
        remaining = max(1.0 - done, 1e-6)
        total = 0.0
        for i in range(steps):
            a0, a1 = age + i * STEP_SECONDS, age + (i + 1) * STEP_SECONDS
            if a0 >= MAX_GAP_SECONDS:
                break
            mass = p * (pair.cdf(a1) - pair.cdf(a0)) / remaining
            left = max(1.0 - total, 1e-9)
            h = clamp(mass / left, 0.0, 0.999)
            total += mass
            hazards[i] = 1.0 - (1.0 - hazards[i]) * (1.0 - h)
        totals[source] = min(total, 0.999)
    return hazards, totals


def forecast(house: HouseModel, area_id: str, inp: ForecastInput, daykey: DayKey, *,
             horizons: tuple[int, ...] = HORIZONS, use_movement: bool = True,
             use_contexts: bool = True) -> Forecast | None:
    """Step the room's chain forward from its current state. None when the room has no model."""
    model = house.rooms.get(area_id)
    if model is None:
        return None
    steps = max(horizons)
    want = set(horizons)
    ent_mult, ext_mult = _context_multipliers(model, inp.contexts) if use_contexts else (1.0, 1.0)
    hazards, movement = (movement_hazards(house, area_id, inp, daykey, steps) if use_movement
                         else ([0.0] * steps, {}))
    age0 = max(0.0, (inp.now - inp.since) / 60.0) if inp.since is not None else float(
        OCC_AGE_CAP if inp.occupied else VAC_AGE_CAP)
    # Cells: [age in minutes, probability mass, still in the initial vacancy].
    occ: list[list[float]] = [[min(age0, OCC_AGE_CAP), 1.0, 0.0]] if inp.occupied else []
    vac: list[list[float]] = [] if inp.occupied else [[min(age0, VAC_AGE_CAP), 1.0, 1.0]]
    entered = 0.0
    probabilities: dict[int, float] = {}
    entry: dict[int, float] = {}
    schedule: dict[int, float] = {}
    cache: dict[tuple[int, bool, int], tuple[float, float]] = {}
    for i in range(steps):
        mid = inp.now + (i + 0.5) * STEP_SECONDS
        key = daykey(mid)
        if key not in cache:
            dow, wd, hour = key
            cache[key] = (model.entry_rate(dow, wd, hour), model.exit_rate(dow, wd, hour))
        lam, mu = cache[key]
        lam *= ent_mult
        mu *= ext_mult
        move = hazards[i]
        new_occ = new_vac = 0.0
        for cell in vac:
            rate = lam * model.g_vacant[bucket(cell[0], VAC_AGE_EDGES)]
            p = 1.0 - (1.0 - (1.0 - math.exp(-rate * STEP_SECONDS))) * (1.0 - move)
            moved = cell[1] * p
            cell[1] -= moved
            new_occ += moved
            if cell[2]:
                entered += moved
            cell[0] = min(cell[0] + 1, VAC_AGE_CAP)
        for cell in occ:
            rate = mu * model.g_occupied[bucket(cell[0], OCC_AGE_EDGES)]
            moved = cell[1] * (1.0 - math.exp(-rate * STEP_SECONDS))
            cell[1] -= moved
            new_vac += moved
            cell[0] = min(cell[0] + 1, OCC_AGE_CAP)
        if new_occ:
            occ.append([0.0, new_occ, 0.0])
        if new_vac:
            vac.append([0.0, new_vac, 0.0])
        occ = _merge(occ, OCC_AGE_CAP)
        vac = _merge(vac, VAC_AGE_CAP)
        minute = i + 1
        if minute in want:
            probabilities[minute] = clamp(sum(c[1] for c in occ), 0.0, 1.0)
            entry[minute] = clamp(entered, 0.0, 1.0)
            dow, wd, hour = daykey(inp.now + minute * 60)
            schedule[minute] = model.occupancy.at(dow, wd, hour)
    return Forecast(probabilities=probabilities, entry=None if inp.occupied else entry, schedule=schedule,
                    movement=movement, entry_multiplier=ent_mult, exit_multiplier=ext_mult)


def _merge(cells: list[list[float]], cap: float) -> list[list[float]]:
    """Merge cells that reached the age cap (they behave the same from now on) and drop empty ones."""
    out: list[list[float]] = []
    capped: dict[float, list[float]] = {}
    for c in cells:
        if c[1] <= 1e-12:
            continue
        if c[0] >= cap:
            m = capped.get(c[2])
            if m is None:
                capped[c[2]] = [cap, c[1], c[2]]
            else:
                m[1] += c[1]
        else:
            out.append(c)
    out.extend(capped.values())
    return out


def next_rooms(house: HouseModel, source: str, age: float, daykey: DayKey, at: float) -> list[tuple[str, float]]:
    """Where someone who arrived in ``source`` ``age`` seconds ago goes next (within MAX_GAP), most likely first."""
    if age > MAX_GAP_SECONDS:
        return []
    _dow, wd, hour = daykey(at)
    part = day_part(hour)
    outgoing = house.outgoing(source)
    done = sum(p.p(part, wd) * p.cdf(age) for p in outgoing)
    remaining = max(1.0 - done, 1e-6)
    out = [(p.target, clamp(p.p(part, wd) * (1.0 - p.cdf(age)) / remaining, 0.0, 1.0)) for p in outgoing]
    return sorted((x for x in out if x[1] > 0.005), key=lambda x: -x[1])


# ---------------------------------------------------------------- confidence
@dataclass(slots=True)
class Accuracy:
    """Prequential evaluation totals of one room and horizon."""

    n: float = 0.0
    brier: float = 0.0
    brier_schedule: float = 0.0
    brier_persistence: float = 0.0
    bins: list[list[float]] = field(default_factory=lambda: [[0.0, 0.0, 0.0] for _ in range(CAL_BINS)])

    def add(self, ev: list[float], cal: dict[int, list[float]] | None) -> None:
        self.n += ev[0]
        self.brier += ev[1]
        self.brier_schedule += ev[2]
        self.brier_persistence += ev[3]
        for b, (n, sp, sy) in (cal or {}).items():
            if 0 <= b < CAL_BINS:
                self.bins[b][0] += n
                self.bins[b][1] += sp
                self.bins[b][2] += sy

    def mean(self, total: float) -> float | None:
        return total / self.n if self.n else None

    def skill(self, reference: float) -> float | None:
        """1 - Brier / reference Brier (positive = better than the reference)."""
        if not self.n or reference <= 0:
            return None
        return 1.0 - self.brier / reference

    def confusion(self, threshold: float) -> dict[str, float]:
        """Counts at a threshold (rounded to the calibration bins)."""
        first = int(round(clamp(threshold, 0.0, 1.0) * CAL_BINS))
        tp = fp = fn = tn = 0.0
        for b, (n, _sp, sy) in enumerate(self.bins):
            if b >= first:
                tp += sy
                fp += n - sy
            else:
                fn += sy
                tn += n - sy
        return {"threshold": first / CAL_BINS, "true_positive": round(tp), "false_positive": round(fp),
                "false_negative": round(fn), "true_negative": round(tn)}

    def as_dict(self, threshold: float) -> dict[str, Any]:
        def r(v: float | None, d: int = 4) -> float | None:
            return None if v is None else round(v, d)
        return {
            "forecasts": int(self.n),
            "brier": r(self.mean(self.brier)),
            "brier_schedule": r(self.mean(self.brier_schedule)),
            "brier_persistence": r(self.mean(self.brier_persistence)),
            "skill_vs_schedule": r(self.skill(self.brier_schedule), 3),
            "skill_vs_persistence": r(self.skill(self.brier_persistence), 3),
            "calibration": [{"bin": f"{b * 10}-{b * 10 + 10} %", "forecasts": int(n),
                             "mean_forecast": r(sp / n if n else None, 3), "observed": r(sy / n if n else None, 3)}
                            for b, (n, sp, sy) in enumerate(self.bins) if n],
            **self.confusion(threshold),
        }


def accuracy(days: Iterable[HouseDay], area_id: str | None, horizon: int) -> Accuracy:
    acc = Accuracy()
    for d in days:
        for a, r in d.rooms.items():
            if area_id is not None and a != area_id:
                continue
            if (ev := r.ev.get(horizon)) is not None:
                acc.add(ev, r.cal.get(horizon))
    return acc


@dataclass(slots=True)
class Confidence:
    level: str
    score: int
    reasons: list[str]
    components: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "score": self.score, "reasons": self.reasons, "components": self.components}


def confidence(stats: RoomStats, acc: Accuracy, *, min_days: int, min_entries: int,
               signals_known: float, usable: bool) -> Confidence:
    """Transparent reliability of a room's forecasts (see README: Confidence)."""
    reasons: list[str] = []
    skill = acc.skill(acc.brier_schedule) if acc.n >= 100 else None
    components = {
        "days_observed": stats.days, "weekdays_observed": stats.weekdays, "entries": int(stats.entries),
        "evaluated_forecasts": int(acc.n), "skill_vs_schedule": None if skill is None else round(skill, 3),
        "signals_known": round(signals_known, 2),
    }
    if not usable:
        return Confidence("insufficient_data", 0, ["Occupancy of this room is unreliable right now "
                                                   "(its occupancy sensors are unavailable)"], components)
    if stats.days < min_days:
        reasons.append(f"Only {stats.days} day{'s' if stats.days != 1 else ''} of history "
                       f"(at least {min_days} needed)")
    if stats.entries < min_entries:
        reasons.append(f"Only {int(stats.entries)} visits seen (at least {min_entries} needed)")
    if reasons:
        return Confidence("insufficient_data", 0, reasons, components)
    s_days = min(1.0, stats.days / 21)
    s_week = stats.weekdays / 7
    s_entries = min(1.0, stats.entries / 50)
    s_skill = clamp(skill / 0.2, 0.0, 1.0) if skill is not None else 0.0
    score = 100 * (0.45 * s_days + 0.15 * s_week + 0.2 * s_entries + 0.2 * s_skill) * clamp(signals_known, 0.0, 1.0)
    level = "high" if score >= 70 else "moderate" if score >= 40 else "low"
    if level == "high" and not (stats.days >= 14 and acc.n >= 200 and (skill or 0) > 0):
        level = "moderate"
        reasons.append("High confidence needs 14 days and forecasts that proved better than the schedule")
    if level == "moderate" and stats.days < 7:
        level = "low"
        reasons.append("Less than a week of history")
    if stats.weekdays < 7:
        reasons.append(f"{stats.weekdays} of 7 weekdays observed")
    if skill is not None and skill <= 0:
        reasons.append("Forecasts have not yet been better than the plain schedule")
    elif skill is None:
        reasons.append("Not enough evaluated forecasts yet to measure accuracy")
    if signals_known < 1:
        reasons.append("Some occupancy sensors of this room are unavailable")
    return Confidence(level, int(round(score)), reasons, components)


def confidence_rank(level: str) -> int:
    return CONFIDENCE_LEVELS.index(level) if level in CONFIDENCE_LEVELS else 0


# --------------------------------------------------------------- explanation
def _duration_text(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 90:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min" if rest and hours < 6 else f"{hours} h"


def _hour_range(hour: int) -> str:
    return f"{hour:02d}:00–{(hour + 1) % 24:02d}:00"


def explain(house: HouseModel, area_id: str, inp: ForecastInput, fc: Forecast, daykey: DayKey, horizon: int,
            name: Callable[[str], str]) -> tuple[str, list[Factor]]:
    """Plain-language reasons for a forecast, from the model's actual inputs (largest effect first)."""
    model = house.rooms[area_id]
    factors: list[Factor] = []
    p = fc.probabilities[horizon]
    target = inp.now + horizon * 60
    dow, wd, hour = daykey(target)
    day_word = "workdays" if wd else "days off"
    sched = model.occupancy.at(dow, wd, hour)
    part = DAY_PARTS[day_part(hour)][2]
    factors.append(Factor("schedule", f"On {day_word} at {_hour_range(hour)} this room is occupied "
                                      f"{round(sched * 100)} % of the time", round(sched * 100, 1),
                          {"hour": hour, "workday": wd, "occupancy_fraction": round(sched, 3)}))
    if inp.since is not None:
        age = inp.now - inp.since
        if inp.occupied:
            g = model.g_occupied[bucket(age / 60, OCC_AGE_EDGES)]
            text = f"Occupied for {'' if inp.since_exact else 'at least '}{_duration_text(age)}"
            if g >= 1.5:
                text += f"; visits this long usually end soon ({g:.1f}× the usual leaving rate)"
            elif g <= 0.67:
                text += f"; visits this long usually continue ({g:.1f}× the usual leaving rate)"
        else:
            g = model.g_vacant[bucket(age / 60, VAC_AGE_EDGES)]
            text = f"Vacant for {'' if inp.since_exact else 'at least '}{_duration_text(age)}"
            if g >= 1.5:
                text += f"; people often come back this soon ({g:.1f}× the usual entry rate)"
        factors.append(Factor("current", text, 0.0, {"occupied": inp.occupied, "seconds": round(age)}))
    if fc.movement:
        without = forecast(house, area_id, inp, daykey, horizons=(horizon,), use_movement=False)
        delta = (p - without.probabilities[horizon]) * 100 if without else 0.0
        for source, prob in sorted(fc.movement.items(), key=lambda x: -x[1]):
            if prob < 0.03:
                continue
            pair = house.pairs.get((source, area_id))
            age = next((inp.now - t for a, t in inp.arrivals if a == source), 0.0)
            factors.append(Factor(
                "movement", f"Movement in {name(source)} {_duration_text(age)} ago: "
                            f"{round(prob * 100)} % chance of continuing here"
                            + (f" (typically after {round(pair.median_delay)} s)" if pair else ""),
                delta, {"source": source, "probability": round(prob, 3)}))
            delta = 0.0  # the combined effect is shown once, on the strongest source
    if inp.contexts:
        without = forecast(house, area_id, inp, daykey, horizons=(horizon,), use_contexts=False)
        delta = (p - without.probabilities[horizon]) * 100 if without else 0.0
        items = []
        for key, value in inp.contexts.items():
            f = model.contexts.get(key)
            if f is None or value is None:
                continue
            m = f.exit(value) if inp.occupied else f.entry(value)
            if 0.8 <= m <= 1.25:
                continue
            what = "leaving" if inp.occupied else "entries"
            label = "A connected room is occupied" if key == ADJACENT_CONTEXT and value else (
                "No connected room is occupied" if key == ADJACENT_CONTEXT else f"{name(key)} is {'on' if value else 'off'}")
            items.append((abs(math.log(m)), Factor("context" if key != ADJACENT_CONTEXT else "connected",
                                                    f"{label}: {what} {m:.1f}× the usual rate", 0.0,
                                                    {"key": key, "value": value, "multiplier": round(m, 3)})))
        items.sort(key=lambda x: -x[0])
        for i, (_w, f) in enumerate(items):
            if i == 0:
                f.impact = delta
            factors.append(f)
    # Sentence: the pattern, then the strongest dynamic reasons.
    parts: list[str] = []
    if sched >= 0.25:
        parts.append(f"{'workday' if wd else 'day-off'} {part} activity pattern")
    elif sched <= 0.05:
        parts.append(f"rarely used on {day_word} in the {part}")
    else:
        parts.append(f"sometimes used on {day_word} in the {part}")
    moves = [f for f in factors if f.kind == "movement"]
    if moves and abs(moves[0].impact) >= 2:
        parts.append(f"recent movement in {name(moves[0].details['source'])}")
    ctx = [f for f in factors if f.kind in ("context", "connected")]
    if ctx and abs(ctx[0].impact) >= 2:
        parts.append(ctx[0].text.split(":")[0].lower() if ctx[0].kind == "connected" else
                     f"{name(ctx[0].details['key'])} {'on' if ctx[0].details['value'] else 'off'}")
    if inp.occupied:
        parts.insert(0, "occupied now")
    text = parts[0][:1].upper() + parts[0][1:]
    if len(parts) > 1:
        text += (", " + ", ".join(parts[1:-1]) if len(parts) > 2 else "") + " and " + parts[-1]
    return text + ".", factors
