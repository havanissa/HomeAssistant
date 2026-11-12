"""Historical learning for the occupancy forecast (0.5.0): replay and daily summaries.

Pure functions, no Home Assistant objects: they run in an executor thread.

Replay
------
Recorded states of every room's configured occupancy signals are fed, in time
order, through the *same* occupancy engine (``engine.RoomEngine``) that runs
live, with the same timing and adjacency, evaluated at every event and at
every deadline the engine asks for. The result is the occupancy this
integration would have reported (vacant / occupied / unknown), so learning
uses exactly the live rules, including stuck-sensor detection, radar dropout
bridging and vacancy confirmation. ``uncertain`` (every signal unknown) is
"not observed": it is neither occupied nor vacant time.

Activity onsets (a room's sensors becoming active again after at least
``EPISODE_GAP_SECONDS`` of no activity) are *arrivals*. An arrival within
``OVERLAP_SECONDS`` of an arrival in a room that shares a sensor device with
it, or that is configured as open plan / transition area, is the same
detection seen twice (e.g. one radar covering two rooms) and is dropped. An
arrival in another room within ``MAX_GAP_SECONDS`` after an arrival is a
movement between the two rooms.

Summaries
---------
``summarize`` turns a replay into one ``HouseDay``: seconds and counts by
local hour (see ``forecast_model``). With a model trained on *earlier days
only* it also scores forecasts issued every ``EVAL_EVERY_MINUTES`` of that day
against what the replay shows actually happened (prequential evaluation: no
forecast is ever scored with data it was trained on).
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, datetime
import heapq
from typing import Any

from .const import OccupancyState
from .engine import RoomEngine, SignalStatus, interpret, Value
from .forecast_model import (
    ADJACENT_CONTEXT,
    CAL_BINS,
    DELAY_EDGES,
    EPISODE_GAP_SECONDS,
    EVAL_EVERY_MINUTES,
    HORIZONS,
    HOURS,
    MAX_ARRIVALS_PER_HOUR,
    MAX_EVENTS_PER_HOUR,
    MAX_GAP_SECONDS,
    MAX_HORIZON,
    MAX_TRANSITIONS_PER_PAIR,
    OCC_AGE_EDGES,
    OVERLAP_SECONDS,
    VAC_AGE_EDGES,
    ContextDay,
    DayKey,
    ForecastInput,
    HouseDay,
    HouseModel,
    RoomDay,
    TransitionDay,
    bucket,
    forecast,
)
from .models import OccupancyTiming, SensorAssignment

# A history: entity id -> [(epoch seconds, state string)], oldest first.
History = dict[str, list[tuple[float, str]]]


@dataclass(frozen=True, slots=True)
class RoomSpec:
    area_id: str
    sensors: tuple[SensorAssignment, ...]
    timing: OccupancyTiming
    adjacent: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplaySpec:
    """What to replay: the rooms (as configured), context entities and room relationships."""

    rooms: dict[str, RoomSpec]
    contexts: tuple[str, ...] = ()
    workday_entity: str | None = None
    # Room -> rooms connected to it (for "a connected room is occupied").
    connected: dict[str, frozenset[str]] = field(default_factory=dict)
    # Pairs of rooms whose sensors can see the same person at the same moment.
    overlaps: frozenset[frozenset[str]] = frozenset()

    def entity_ids(self) -> list[str]:
        ids: dict[str, None] = {}
        for room in self.rooms.values():
            for s in room.sensors:
                ids[s.entity_id] = None
        for c in self.contexts:
            ids[c] = None
        if self.workday_entity:
            ids[self.workday_entity] = None
        return list(ids)

    @property
    def context_keys(self) -> tuple[str, ...]:
        return (*self.contexts, ADJACENT_CONTEXT)


class Timeline:
    """A piecewise-constant value: ``values[i]`` holds from ``times[i]`` to the next change."""

    __slots__ = ("times", "values")

    def __init__(self) -> None:
        self.times: list[float] = []
        self.values: list[Any] = []

    def set(self, ts: float, value: Any) -> None:
        if self.values and self.values[-1] == value:
            return
        if self.times and ts <= self.times[-1]:
            # Same instant (several evaluations at one time): the latest value wins.
            self.times[-1] = max(self.times[-1], ts)
            self.values[-1] = value
            if len(self.values) > 1 and self.values[-2] == value:
                self.times.pop()
                self.values.pop()
            return
        self.times.append(ts)
        self.values.append(value)

    def index(self, ts: float) -> int:
        return bisect_right(self.times, ts) - 1

    def at(self, ts: float) -> Any:
        i = self.index(ts)
        return self.values[i] if i >= 0 else None

    def run_start(self, ts: float) -> float | None:
        i = self.index(ts)
        return self.times[i] if i >= 0 else None

    def changes(self, start: float, end: float) -> list[tuple[float, Any, Any]]:
        """(time, previous value, new value) for every change in [start, end)."""
        out = []
        for i in range(max(self.index(start), 1), len(self.times)):
            t = self.times[i]
            if t >= end:
                break
            if t >= start:
                out.append((t, self.values[i - 1], self.values[i]))
        return out


@dataclass(slots=True)
class Replay:
    start: float
    end: float
    occupancy: dict[str, Timeline]           # True / False / None (not observed)
    onsets: dict[str, list[float]]
    arrivals: list[tuple[float, str]]         # accepted arrivals, house wide, in time order
    transitions: list[tuple[float, str, str, float]]  # (from time, from, to, delay seconds)
    contexts: dict[str, Timeline]             # context key -> True / False / None
    entities_seen: set[str] = field(default_factory=set)
    first_seen: float | None = None           # earliest recorded state of any room signal


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def replay(spec: ReplaySpec, history: History, start: float, end: float) -> Replay:
    """Run the occupancy engines over ``history`` from ``start`` to ``end`` (see module docstring)."""
    engines = {a: RoomEngine(a, r.sensors, r.timing, adjacent=r.adjacent) for a, r in spec.rooms.items()}
    entity_rooms: dict[str, list[str]] = {}
    neighbours_of: dict[str, list[str]] = {}
    for a, r in spec.rooms.items():
        for s in r.sensors:
            entity_rooms.setdefault(s.entity_id, []).append(a)
        for n in r.adjacent:
            neighbours_of.setdefault(n, []).append(a)
    for engine in engines.values():
        engine.start(_dt(start))
    occupancy = {a: Timeline() for a in engines}
    onsets: dict[str, list[float]] = {a: [] for a in engines}
    active: dict[str, bool] = dict.fromkeys(engines, False)
    last_inactive: dict[str, float | None] = dict.fromkeys(engines)
    heap: list[tuple[float, int, str]] = []
    version: dict[str, int] = dict.fromkeys(engines, 0)
    seen: set[str] = set()
    first_seen: float | None = None

    def evaluate(area: str, ts: float) -> None:
        engine = engines[area]
        now = _dt(ts)
        res = engine.evaluate(now)
        occupancy[area].set(ts, None if res.state == OccupancyState.UNCERTAIN else res.occupied)
        is_active = any(v.status == SignalStatus.ACTIVE and v.track.capable for v in res.signals)
        if is_active and not active[area]:
            gap_from = last_inactive[area]
            if gap_from is None or ts - gap_from >= EPISODE_GAP_SECONDS:
                onsets[area].append(ts)
        elif not is_active and active[area]:
            last_inactive[area] = ts
        active[area] = is_active
        version[area] += 1
        if (deadline := engine.next_deadline(now)) is not None:
            d = deadline.timestamp()
            if d > ts:
                heapq.heappush(heap, (d, version[area], area))

    def run_deadlines(until: float) -> None:
        while heap and heap[0][0] <= until:
            d, v, area = heapq.heappop(heap)
            if v == version[area]:
                evaluate(area, d)

    events: list[tuple[float, str, str]] = []
    for entity_id, items in history.items():
        if entity_id not in entity_rooms:
            continue
        before = None  # only the state at the start counts of everything before it
        for ts, state in items:
            if ts >= end:
                break
            if first_seen is None or ts < first_seen:
                first_seen = ts
            seen.add(entity_id)
            if ts <= start:
                before = state
            else:
                events.append((ts, entity_id, state))
        if before is not None:
            events.append((start, entity_id, before))
    events.sort(key=lambda e: e[0])
    for area in engines:
        evaluate(area, start)
    for ts, entity_id, state in events:
        run_deadlines(ts)
        affected: dict[str, None] = {}
        for area in entity_rooms[entity_id]:
            engine = engines[area]
            track = engine.tracks.get(entity_id)
            if track is None:
                continue
            before = track.active_start
            if not engine.observe(entity_id, interpret(state, inverted=track.assignment.inverted), _dt(ts)):
                continue
            affected[area] = None
            if track.capable and track.active_start is not None and track.active_start != before:
                for other in neighbours_of.get(area, ()):
                    if other in engines:
                        engines[other].note_neighbour_motion(area, _dt(ts))
                        affected[other] = None
        for area in affected:
            evaluate(area, ts)
    run_deadlines(end)

    arrivals = _arrivals(onsets, spec.overlaps)
    transitions = _transitions(arrivals)
    contexts: dict[str, Timeline] = {}
    for key in spec.contexts:
        tl = Timeline()
        tl.set(start, None)
        _context_line(tl, history.get(key, ()), start, end)
        contexts[key] = tl
    if spec.workday_entity:
        tl = Timeline()
        _context_line(tl, history.get(spec.workday_entity, ()), start, end)
        contexts["~workday"] = tl
    return Replay(start=start, end=end, occupancy=occupancy, onsets=onsets, arrivals=arrivals,
                  transitions=transitions, contexts=contexts, entities_seen=seen, first_seen=first_seen)


def _context_line(tl: Timeline, items, start: float, end: float) -> None:
    before = None
    rows = []
    for ts, state in items:
        if ts >= end:
            break
        if ts <= start:
            before = state
        else:
            rows.append((ts, state))
    if before is not None:
        rows.insert(0, (start, before))
    for ts, state in rows:
        value = interpret(state)
        tl.set(ts, None if value == Value.UNKNOWN else value == Value.ACTIVE)


def _arrivals(onsets: dict[str, list[float]], overlaps: frozenset[frozenset[str]]) -> list[tuple[float, str]]:
    merged = sorted((t, a) for a, times in onsets.items() for t in times)
    accepted: list[tuple[float, str]] = []
    for t, a in merged:
        duplicate = False
        for t2, b in reversed(accepted):
            if t - t2 > OVERLAP_SECONDS:
                break
            if b != a and frozenset((a, b)) in overlaps:
                duplicate = True
                break
        if not duplicate:
            accepted.append((t, a))
    return accepted


def _transitions(arrivals: list[tuple[float, str]]) -> list[tuple[float, str, str, float]]:
    out = []
    for i, (t, a) in enumerate(arrivals):
        for t2, b in arrivals[i + 1:]:
            if t2 - t > MAX_GAP_SECONDS:
                break
            if b != a:
                out.append((t, a, b, t2 - t))
                break
    return out


def connected_timeline(rp: Replay, rooms: frozenset[str]) -> Timeline:
    """True while any of ``rooms`` is occupied (None when none of them is observed)."""
    tl = Timeline()
    lines = [rp.occupancy[r] for r in rooms if r in rp.occupancy]
    times = sorted({t for line in lines for t in line.times} | {rp.start})
    for t in times:
        values = [line.at(t) for line in lines]
        known = [v for v in values if v is not None]
        tl.set(t, any(known) if known else None)
    return tl


def hour_slots(start: float, end: float, daykey: DayKey) -> list[tuple[float, float, int]]:
    """Local hours between ``start`` and ``end`` (``start`` is a local midnight)."""
    slots = []
    t = start
    while t < end:
        t1 = min(t + 3600, end)
        slots.append((t, t1, daykey(t)[2]))
        t = t + 3600
    return slots


@dataclass(slots=True)
class EvalSettings:
    min_days: int = 3
    min_entries: int = 5


def workday_of(rp: Replay, day_start: float, dow: int) -> bool:
    tl = rp.contexts.get("~workday")
    if tl is not None:
        value = tl.at(day_start + 12 * 3600)
        if value is None:
            value = next((v for t, v in zip(tl.times, tl.values, strict=True) if v is not None and t >= day_start),
                         None)
        if value is not None:
            return bool(value)
    return dow < 5


def summarize(spec: ReplaySpec, rp: Replay, *, date: str, dow: int, workday: bool, day_start: float,
              day_end: float, daykey: DayKey, model: HouseModel | None = None,
              settings: EvalSettings | None = None) -> HouseDay:
    """One day's summary from a replay covering [day_start - warm-up, day_end (+ look-ahead))."""
    end = min(day_end, rp.end)
    slots = hour_slots(day_start, end, daykey)
    slot_starts = [s[0] for s in slots]
    day = HouseDay(date=date, dow=dow, workday=workday, complete=end >= day_end)
    connected = {a: connected_timeline(rp, spec.connected.get(a, frozenset())) for a in spec.rooms}
    for area in spec.rooms:
        day.rooms[area] = _room_day(spec, rp, area, connected[area], day_start, end, slots, slot_starts)
    # Movement (attributed to the day of the arrival it starts from).
    for t, a, b, delay in rp.transitions:
        if not day_start <= t < end:
            continue
        td = day.trans.setdefault((a, b), TransitionDay())
        if td.n >= MAX_TRANSITIONS_PER_PAIR:
            continue
        td.n += 1
        td.hours[_hour_at(t, slots, slot_starts)] += 1
        td.delays[min(bucket_delay(delay), len(DELAY_EDGES) - 1)] += 1
    if model is not None:
        _evaluate(spec, rp, day, model, connected, day_start, end, daykey, settings or EvalSettings())
    return day


def bucket_delay(seconds: float) -> int:
    for i, edge in enumerate(DELAY_EDGES):
        if seconds <= edge:
            return i
    return len(DELAY_EDGES) - 1


def _hour_at(ts: float, slots: list[tuple[float, float, int]], starts: list[float]) -> int:
    i = max(0, bisect_right(starts, ts) - 1)
    return slots[i][2] if slots else 0


def _room_day(spec: ReplaySpec, rp: Replay, area: str, connected: Timeline, start: float, end: float,
              slots: list[tuple[float, float, int]], slot_starts: list[float]) -> RoomDay:
    room = RoomDay()
    occ = rp.occupancy[area]
    ctx_lines = {**{k: rp.contexts[k] for k in spec.contexts if k in rp.contexts}, ADJACENT_CONTEXT: connected}
    points: set[float] = {start, end}
    points.update(s[0] for s in slots)
    for tl in (occ, *ctx_lines.values()):
        points.update(t for t in tl.times if start < t < end)
    # Age bucket edges of every visit / vacancy that overlaps the day.
    for i, t in enumerate(occ.times):
        t_next = occ.times[i + 1] if i + 1 < len(occ.times) else end
        if t_next <= start or t >= end or occ.values[i] is None:
            continue
        edges = OCC_AGE_EDGES if occ.values[i] else VAC_AGE_EDGES
        points.update(p for e in edges if start < (p := t + e * 60) < min(t_next, end))
    ordered = sorted(points)
    for b0, b1 in zip(ordered, ordered[1:], strict=False):
        dur = b1 - b0
        if dur <= 0:
            continue
        mid = (b0 + b1) / 2
        value = occ.at(mid)
        if value is None:
            continue
        hour = _hour_at(mid, slots, slot_starts)
        since = occ.run_start(mid) or b0
        age = (mid - since) / 60
        if value:
            k = bucket(age, OCC_AGE_EDGES)
            room.oa[(hour, k)] = room.oa.get((hour, k), 0.0) + dur
        else:
            k = bucket(age, VAC_AGE_EDGES)
            room.va[(hour, k)] = room.va.get((hour, k), 0.0) + dur
        for key, tl in ctx_lines.items():
            if tl.at(mid) is True:
                c = room.cx.setdefault(key, ContextDay())
                (c.occ if value else c.vac)[hour] += dur
    for t, before, after in occ.changes(start, end):
        if before is None or after is None or before == after:
            continue
        hour = _hour_at(t, slots, slot_starts)
        prev_start = occ.run_start(t - 1e-6) or t
        age = (t - prev_start) / 60
        if after:  # entry, after a vacancy of `age`
            k = bucket(age, VAC_AGE_EDGES)
            room.ea[(hour, k)] = room.ea.get((hour, k), 0.0) + 1
        else:      # exit, after a visit of `age`
            k = bucket(age, OCC_AGE_EDGES)
            room.xa[(hour, k)] = room.xa.get((hour, k), 0.0) + 1
        for key, tl in ctx_lines.items():
            if tl.at(t - 1e-6) is True:
                c = room.cx.setdefault(key, ContextDay())
                (c.ent if after else c.ext)[hour] += 1
    _cap_events(room.ea, room.cx, "ent")
    _cap_events(room.xa, room.cx, "ext")
    for t, a in rp.arrivals:
        if a == area and start <= t < end:
            h = _hour_at(t, slots, slot_starts)
            if room.arr[h] < MAX_ARRIVALS_PER_HOUR:
                room.arr[h] += 1
    return room


def _cap_events(table: dict[tuple[int, int], float], cx: dict[str, ContextDay], attr: str) -> None:
    """One unusual hour (a party, a sensor gone wild) counts at most MAX_EVENTS_PER_HOUR events."""
    per_hour = [0.0] * HOURS
    for (h, _k), n in table.items():
        per_hour[h] += n
    for h, total in enumerate(per_hour):
        if total > MAX_EVENTS_PER_HOUR:
            scale = MAX_EVENTS_PER_HOUR / total
            for key in [k for k in table if k[0] == h]:
                table[key] *= scale
            for c in cx.values():
                getattr(c, attr)[h] *= scale


def features_at(spec: ReplaySpec, rp: Replay, area: str, ts: float, connected: Timeline | None = None
                ) -> ForecastInput | None:
    """What a live forecast at ``ts`` would have known (nothing after ``ts``)."""
    occ = rp.occupancy.get(area)
    if occ is None:
        return None
    value = occ.at(ts)
    if value is None:
        return None
    contexts: dict[str, bool | None] = {k: rp.contexts[k].at(ts) for k in spec.contexts if k in rp.contexts}
    if connected is not None:
        contexts[ADJACENT_CONTEXT] = connected.at(ts)
    i = bisect_right(rp.arrivals, (ts, "￿"))
    arrivals = []
    for t, a in reversed(rp.arrivals[:i]):
        if ts - t > MAX_GAP_SECONDS:
            break
        if a != area:
            arrivals.append((a, t))
    since = occ.run_start(ts)
    return ForecastInput(now=ts, occupied=bool(value), since=since, contexts=contexts, arrivals=arrivals,
                         since_exact=since is not None and since > rp.start + 1)


def _evaluate(spec: ReplaySpec, rp: Replay, day: HouseDay, model: HouseModel, connected: dict[str, Timeline],
              start: float, end: float, daykey: DayKey, settings: EvalSettings) -> None:
    step = EVAL_EVERY_MINUTES * 60
    t = start
    while t < end:
        for area, room in day.rooms.items():
            m = model.rooms.get(area)
            if m is None or m.stats.days < settings.min_days or m.stats.entries < settings.min_entries:
                room.skipped += 1
                continue
            inp = features_at(spec, rp, area, t, connected.get(area))
            if inp is None:
                continue
            fc = forecast(model, area, inp, daykey)
            if fc is None:
                continue
            occ = rp.occupancy[area]
            for h in HORIZONS:
                target = t + h * 60
                if target >= rp.end:
                    continue
                y = occ.at(target)
                if y is None:
                    continue
                y = 1.0 if y else 0.0
                p = fc.probabilities[h]
                q = fc.schedule[h]
                pers = 1.0 if inp.occupied else 0.0
                ev = room.ev.setdefault(h, [0.0, 0.0, 0.0, 0.0])
                ev[0] += 1
                ev[1] += (p - y) ** 2
                ev[2] += (q - y) ** 2
                ev[3] += (pers - y) ** 2
                b = min(int(p * CAL_BINS), CAL_BINS - 1)
                cell = room.cal.setdefault(h, {}).setdefault(b, [0.0, 0.0, 0.0])
                cell[0] += 1
                cell[1] += p
                cell[2] += y
        t += step


def lookahead_seconds() -> int:
    """How far past a day's end the replay must reach so every forecast of that day can be scored."""
    return MAX_HORIZON * 60


def warmup_seconds() -> int:
    """Replay starts this long before a day so visits that began earlier have their real age."""
    return 3 * 3600


def coverage(rp: Replay) -> dict[str, Any]:
    return {"entities_with_history": len(rp.entities_seen), "first_state": rp.first_seen}

