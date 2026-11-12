"""Simulated histories for the occupancy forecast tests (clearly synthetic, never real observations)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from custom_components.smart_occupancy.const import SensorKind, SensorRole
from custom_components.smart_occupancy.forecast_history import (
    ReplaySpec,
    RoomSpec,
    lookahead_seconds,
    replay,
    summarize,
    warmup_seconds,
)
from custom_components.smart_occupancy.forecast_model import HouseDay, build_house_model
from custom_components.smart_occupancy.models import OccupancyTiming, SensorAssignment

UTC = timezone.utc
DAY0 = date(2026, 10, 5)  # a Monday


def daykey(ts: float) -> tuple[int, bool, int]:
    d = datetime.fromtimestamp(ts, UTC)
    return d.weekday(), d.weekday() < 5, d.hour


def day_start(d: date) -> float:
    return datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp()


def pir(entity_id: str) -> SensorAssignment:
    return SensorAssignment(entity_id, SensorKind.PIR, SensorRole.TRIGGER)


def radar(entity_id: str) -> SensorAssignment:
    return SensorAssignment(entity_id, SensorKind.RADAR, SensorRole.MAINTAIN)


def spec(*rooms: tuple[str, tuple[SensorAssignment, ...]], **kwargs) -> ReplaySpec:
    return ReplaySpec(rooms={a: RoomSpec(a, s, OccupancyTiming()) for a, s in rooms}, **kwargs)


class Sim:
    """Builds a history: entity -> [(ts, state)]."""

    def __init__(self, entities: list[str], start: float) -> None:
        self.h: dict[str, list[tuple[float, str]]] = {e: [(start, "off")] for e in entities}

    def pulse(self, entity: str, at: float, hold: float = 60) -> None:
        self.h[entity] += [(at, "on"), (at + hold, "off")]

    def stay(self, entity: str, start: float, end: float, every: float = 240, hold: float = 60) -> None:
        t = start
        while t < end:
            self.pulse(entity, t, hold)
            t += every

    def set(self, entity: str, at: float, state: str) -> None:
        self.h[entity].append((at, state))

    def history(self) -> dict[str, list[tuple[float, str]]]:
        return {e: sorted(v) for e, v in self.h.items()}


def learn(sp: ReplaySpec, history, n_days: int, *, start: date = DAY0, evaluate: bool = True,
          half_life: float = 30, min_days: int = 3, min_entries: int = 5) -> list[HouseDay]:
    """Process ``n_days`` like the runtime: each day scored with a model of the earlier days only."""
    from custom_components.smart_occupancy.forecast_history import EvalSettings

    days: list[HouseDay] = []
    for i in range(n_days):
        d = start + timedelta(days=i)
        ds = day_start(d)
        rp = replay(sp, history, ds - warmup_seconds(), ds + 86400 + lookahead_seconds())
        model = model_of(sp, days, d, half_life) if (evaluate and days) else None
        days.append(summarize(sp, rp, date=d.isoformat(), dow=d.weekday(), workday=d.weekday() < 5, day_start=ds,
                              day_end=ds + 86400, daykey=daykey, model=model,
                              settings=EvalSettings(min_days=min_days, min_entries=min_entries)))
    return days


def model_of(sp: ReplaySpec, days: list[HouseDay], today: date, half_life: float = 30):
    return build_house_model(days, sp.rooms, sp.context_keys, today=today.isoformat(), half_life_days=half_life,
                             ordinal=lambda s: date.fromisoformat(s).toordinal())
