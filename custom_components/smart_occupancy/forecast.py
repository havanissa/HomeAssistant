"""Occupancy forecast runtime (0.5.0, Phase 4): learning, live forecasts, evaluation and logging.

Observation only. This module never calls a service, never changes an
occupancy state, a helper or a light, and nothing in the integration acts on
a forecast. Forecasts are published as entities, shown in the room card,
explained, logged and scored; other modules (and later phases) can read them
through ``ForecastManager.room()``.

Learning (``async_learn``)
--------------------------
* The Recorder is asked only for the configured room signals and context
  entities, one local day at a time (plus a warm-up before and a look-ahead
  after the day), through ``recorder.history.get_significant_states`` in the
  Recorder's own executor. The database is never written.
* Days that are already summarised are not queried again: after the first
  backfill only today (partial) and newly completed days are read. The
  oldest state the Recorder still has bounds the backfill, so a 90-day
  window with 10 days of history simply learns 10 days, and keeps every
  later day in its own store (the Recorder purges states; the summaries stay
  for the configured learning window).
* Each completed day is scored before it is learned from: forecasts are
  issued with the model of the earlier days only (prequential evaluation).

Live
----
The live occupancy engines report every evaluation; arrivals (new activity
in a room) and occupancy changes recalculate the forecasts at once, and a
timer every ``FORECAST_TICK_MINUTES`` keeps them current. Entities write a
new state only when the rounded forecast changes.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    FORECAST_DAYS_STORE_KEY,
    FORECAST_DAYS_STORE_VERSION,
    FORECAST_SAVE_DELAY_SECONDS,
    FORECAST_SETTINGS_STORE_KEY,
    FORECAST_SETTINGS_STORE_VERSION,
    FORECAST_START_DELAY_SECONDS,
    FORECAST_TICK_MINUTES,
    SIGNAL_FORECAST_UPDATED,
    SIGNAL_LIGHTING_UPDATED,
    OccupancyState,
    SensorKind,
)
from .decisions import NOTED, OBSERVED
from .discovery import area_name
from .engine import RoomResult, SignalStatus, Value, interpret
from .forecast_history import (
    EvalSettings,
    History,
    Replay,
    ReplaySpec,
    RoomSpec,
    lookahead_seconds,
    replay,
    summarize,
    warmup_seconds,
    workday_of,
)
from .forecast_model import (
    ADJACENT_CONTEXT,
    HORIZONS,
    MAX_GAP_SECONDS,
    OVERLAP_SECONDS,
    SUMMARY_VERSION,
    Accuracy,
    Confidence,
    Factor,
    ForecastInput,
    HouseDay,
    HouseModel,
    accuracy,
    build_house_model,
    confidence,
    confidence_rank,
    explain,
    forecast,
    next_rooms,
)
from .forecast_settings import Connection, ForecastSettings
from .occupancy import friendly_name

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

_LOGGER = logging.getLogger(__name__)

MODULE_FORECAST = "forecast"
HOUSE_SUBJECT = "forecast:house"
# The predicted next room is shown when at least this likely.
NEXT_ROOM_MIN_PROBABILITY = 0.25
# A learned hourly occupancy that moves this much after a new day is logged as a changed pattern.
PATTERN_CHANGE = 0.15
SCHEDULE_SCAN_HOURS = 12

# Fetches a history: (entity ids, start, end) -> History (a coroutine function).
Fetcher = Callable[[list[str], datetime, datetime], Any]

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DISABLED = "disabled"
STATUS_LEARNING = "learning"


def subject(area_id: str) -> str:
    return f"forecast:{area_id}"


@dataclass(slots=True)
class LiveRoom:
    occupied: bool | None = None
    since: float | None = None
    active: bool = False
    last_inactive: float | None = None
    since_exact: bool = True


@dataclass(slots=True)
class RoomForecast:
    """The published forecast of one room (also the API for other modules)."""

    area_id: str
    status: str
    horizon: int
    threshold: int
    confidence_threshold: str
    occupied: bool | None = None
    state: str | None = None
    probabilities: dict[int, float] | None = None
    entry: dict[int, float] | None = None
    schedule: dict[int, float] | None = None
    predicted: bool | None = None
    confidence: Confidence | None = None
    explanation: str = ""
    factors: list[Factor] = field(default_factory=list)
    window: dict[str, Any] | None = None
    next_rooms: list[tuple[str, float]] = field(default_factory=list)
    updated: str | None = None

    @property
    def probability(self) -> float | None:
        return self.probabilities.get(self.horizon) if self.probabilities else None

    def fingerprint(self) -> tuple:
        rounded = tuple(sorted((h, round(p * 20)) for h, p in (self.probabilities or {}).items()))
        p = self.probability
        return (self.status, None if p is None else round(p * 100), self.predicted,
                self.confidence.level if self.confidence else None, rounded)

    def as_dict(self, name: Callable[[str], str] | None = None) -> dict[str, Any]:
        def pct(d: dict[int, float] | None) -> dict[str, int] | None:
            return None if d is None else {str(h): round(v * 100) for h, v in sorted(d.items())}
        return {
            "area_id": self.area_id, "status": self.status, "horizon": self.horizon, "threshold": self.threshold,
            "confidence_threshold": self.confidence_threshold, "occupied": self.occupied, "state": self.state,
            "probability": None if self.probability is None else round(self.probability * 100),
            "probabilities": pct(self.probabilities), "entry": pct(self.entry), "schedule": pct(self.schedule),
            "predicted": self.predicted,
            "confidence": self.confidence.as_dict() if self.confidence else None,
            "explanation": self.explanation, "factors": [f.as_dict() for f in self.factors],
            "window": self.window,
            "next_rooms": [{"area_id": a, "name": name(a) if name else a, "probability": round(p * 100)}
                           for a, p in self.next_rooms],
            "updated": self.updated,
        }


@dataclass(slots=True)
class LearningStatus:
    state: str = "waiting"          # waiting / learning / ready / disabled / no_recorder / error
    detail: str = ""
    progress: tuple[int, int] | None = None
    last_update: str | None = None
    last_error: str | None = None
    recorder_oldest: float | None = None
    queries: int = 0
    last_duration: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail,
                "progress": list(self.progress) if self.progress else None,
                "last_update": self.last_update, "last_error": self.last_error,
                "recorder_oldest": dt_util.utc_from_timestamp(self.recorder_oldest).isoformat(timespec="seconds")
                if self.recorder_oldest else None,
                "queries": self.queries,
                "last_duration_seconds": None if self.last_duration is None else round(self.last_duration, 1)}


class ForecastManager:
    """Learns from the Recorder, publishes live forecasts and scores them."""

    def __init__(self, hass: HomeAssistant, coordinator: SmartOccupancyCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.settings = ForecastSettings()
        self.days: dict[str, HouseDay] = {}
        self.no_data: set[str] = set()
        self.today: HouseDay | None = None
        self.today_replay: Replay | None = None
        self.model: HouseModel | None = None
        self.status = LearningStatus()
        self.rooms: dict[str, RoomForecast] = {}
        self.live: dict[str, LiveRoom] = {}
        self.arrivals: deque[tuple[float, str]] = deque(maxlen=200)
        self.next_room: dict[str, Any] | None = None
        self.fetch_history: Fetcher = self._recorder_fetch
        # The oldest state the Recorder still has (bounds the backfill); None = unknown.
        self.oldest_history: Callable[[], float | None] = self._recorder_oldest
        self._settings_store: Store[dict[str, Any]] = Store(
            hass, FORECAST_SETTINGS_STORE_VERSION, FORECAST_SETTINGS_STORE_KEY)
        self._days_store: Store[dict[str, Any]] = Store(hass, FORECAST_DAYS_STORE_VERSION, FORECAST_DAYS_STORE_KEY)
        self._lock = asyncio.Lock()
        self._unsubs: list[CALLBACK_TYPE] = []
        self._timers: list[CALLBACK_TYPE] = []
        self._learn_unsub: CALLBACK_TYPE | None = None
        self._recompute_timer: CALLBACK_TYPE | None = None
        self._ctx_unsub: CALLBACK_TYPE | None = None
        self._running = False
        self._accuracy_cache: dict[tuple[str | None, int], Accuracy] = {}
        self._published: tuple | None = None
        self._pending: list[dict[str, Any]] = []   # predictions waiting for their outcome (memory only)
        self._task: asyncio.Task | None = None
        self._spec: ReplaySpec | None = None
        self._days_dirty = False
        self._started_unsub: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------ lifecycle
    async def async_load(self) -> None:
        self.settings = ForecastSettings.from_dict(await self._settings_store.async_load())
        data = await self._days_store.async_load() or {}
        for item in (data.get("days") or {}).values():
            if (day := HouseDay.from_dict(item)) is not None:
                self.days[day.date] = day
        self.no_data = {d for d in data.get("no_data") or [] if isinstance(d, str)}
        oldest = data.get("recorder_oldest")
        self.status.recorder_oldest = oldest if isinstance(oldest, (int, float)) else None
        self.status.last_update = data.get("last_update") if isinstance(data.get("last_update"), str) else None

    @callback
    def async_start(self) -> None:
        self._running = True
        for area_id in self.coordinator.config.enabled_rooms:
            self.live[area_id] = LiveRoom()
            self._unsubs.append(self.coordinator.occupancy.async_add_listener(area_id, self._listener(area_id)))
            self._observe_room(area_id, initial=True)
        self._track_contexts()
        self._unsubs.append(async_track_time_interval(
            self.hass, self._on_tick, timedelta(minutes=FORECAST_TICK_MINUTES), cancel_on_shutdown=True))
        self._schedule_learning_interval()
        if self.days:
            # A model from the stored days is available at once (today is added by the first learning run).
            self.hass.async_create_task(self._async_build_model(log_changes=False), eager_start=True)
        if self.hass.state is CoreState.running:
            self._timers.append(async_call_later(self.hass, FORECAST_START_DELAY_SECONDS, self._on_learn_timer))
        else:
            self._started_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, self._on_started)
        self._recompute()

    @callback
    def async_stop(self) -> None:
        self._running = False
        for unsub in (*self._unsubs, *self._timers):
            unsub()
        self._unsubs.clear()
        self._timers.clear()
        for attr in ("_learn_unsub", "_recompute_timer", "_ctx_unsub", "_started_unsub"):
            if unsub := getattr(self, attr):
                unsub()
                setattr(self, attr, None)
        if self._task and not self._task.done():
            self._task.cancel()

    async def async_flush(self) -> None:
        if self._days_dirty:
            await self._days_store.async_save(self._days_data())

    async def async_remove(self) -> None:
        await self._settings_store.async_remove()
        await self._days_store.async_remove()

    @callback
    def _on_started(self, _event: Event) -> None:
        self._started_unsub = None  # one-shot listener is already removed
        self._timers.append(async_call_later(self.hass, FORECAST_START_DELAY_SECONDS, self._on_learn_timer))

    @callback
    def _on_learn_timer(self, _now: datetime) -> None:
        if self._running:
            self._task = self.hass.async_create_background_task(self.async_learn(), "smart_occupancy forecast learning")

    @callback
    def _schedule_learning_interval(self) -> None:
        if self._learn_unsub:
            self._learn_unsub()
        self._learn_unsub = async_track_time_interval(
            self.hass, self._on_learn_timer, timedelta(minutes=self.settings.defaults.refresh_minutes),
            cancel_on_shutdown=True)

    # ------------------------------------------------------------- settings
    async def async_set_settings(self, settings: ForecastSettings) -> None:
        """Save changed settings and apply them (no reload; learned days are kept)."""
        old = self.settings
        self.settings = settings
        self._spec = None
        await self._settings_store.async_save(settings.as_dict())
        if settings.defaults.refresh_minutes != old.defaults.refresh_minutes:
            self._schedule_learning_interval()
        if (settings.defaults.contexts, settings.defaults.workday_entity) != (
                old.defaults.contexts, old.defaults.workday_entity):
            self._track_contexts()
        await self._async_build_model(log_changes=False)
        self._recompute()

    async def async_rebuild(self) -> None:
        """Forget what was learned and learn again from the Recorder's history."""
        async with self._lock:
            self.days.clear()
            self.no_data.clear()
            self.today = None
            self.model = None
            self._accuracy_cache.clear()
            self._save_days(0)
        self._recompute()
        await self.async_learn()

    # -------------------------------------------------------------- helpers
    def name(self, key: str) -> str:
        if "." not in key:
            return area_name(self.hass, key) or key
        return friendly_name(self.hass, key) or key

    def _tz(self):
        return dt_util.get_default_time_zone()

    def _day_bounds(self, d: date) -> tuple[float, float]:
        tz = self._tz()
        start = datetime.combine(d, time(0), tz)
        end = datetime.combine(d + timedelta(days=1), time(0), tz)
        return start.timestamp(), end.timestamp()

    def _daykey(self, workdays: dict[str, bool] | None = None) -> Callable[[float], tuple[int, bool, int]]:
        tz = self._tz()
        workdays = workdays or {}
        cache: dict[int, tuple[int, bool, int]] = {}

        def key(ts: float) -> tuple[int, bool, int]:
            slot = int(ts // 900)
            if (hit := cache.get(slot)) is not None:
                return hit
            local = datetime.fromtimestamp(ts, tz)
            dow = local.weekday()
            wd = workdays.get(local.date().isoformat(), dow < 5)
            cache[slot] = result = (dow, wd, local.hour)
            return result

        return key

    def workday_entity(self) -> str | None:
        configured = self.settings.defaults.workday_entity
        if configured is not None:
            return configured or None
        ent_reg = er.async_get(self.hass)
        found = sorted(e.entity_id for e in ent_reg.entities.values()
                       if e.platform == "workday" and e.domain == "binary_sensor" and not e.disabled)
        return found[0] if found else None

    def automatic_contexts(self) -> list[str]:
        out: dict[str, None] = {}
        for room in self.coordinator.config.enabled_rooms.values():
            for s in room.sensors:
                if s.participates and s.kind in (SensorKind.BED, SensorKind.PC_ACTIVITY):
                    out[s.entity_id] = None
        return list(out)

    def contexts(self) -> list[str]:
        configured = self.settings.defaults.contexts
        return list(configured) if configured is not None else self.automatic_contexts()

    def suggested_connections(self) -> list[Connection]:
        """From the configuration: adjacent rooms (a door) and rooms whose signals share a device (open plan)."""
        rooms = self.coordinator.config.enabled_rooms
        out: dict[frozenset[str], Connection] = {}
        for a, room in rooms.items():
            for b in room.adjacent_rooms:
                if b in rooms and b != a:
                    c = Connection.make(a, b, "door")
                    out.setdefault(c.pair, c)
        devices = self._room_devices()
        names = sorted(devices)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if devices[a] & devices[b]:
                    out[frozenset((a, b))] = Connection.make(a, b, "open")
        return list(out.values())

    def connections(self) -> list[Connection]:
        if self.settings.connections is not None:
            return list(self.settings.connections)
        return self.suggested_connections()

    def _room_devices(self) -> dict[str, set[str]]:
        ent_reg = er.async_get(self.hass)
        out: dict[str, set[str]] = {}
        for a, room in self.coordinator.config.enabled_rooms.items():
            ids = set()
            for s in room.sensors:
                if s.participates and s.kind != SensorKind.DOOR:
                    entry = ent_reg.async_get(s.entity_id)
                    if entry and entry.device_id:
                        ids.add(entry.device_id)
            out[a] = ids
        return out

    def replay_spec(self) -> ReplaySpec:
        config = self.coordinator.config
        rooms = {a: RoomSpec(a, r.sensors, config.timing_for(a), r.adjacent_rooms)
                 for a, r in config.enabled_rooms.items() if self.settings.effective(a).enabled}
        connections = [c for c in self.connections() if c.a in rooms and c.b in rooms]
        connected: dict[str, set[str]] = {a: set() for a in rooms}
        for c in connections:
            connected[c.a].add(c.b)
            connected[c.b].add(c.a)
        overlaps = {c.pair for c in connections if c.kind in ("open", "passage")}
        devices = self._room_devices()
        names = sorted(r for r in devices if r in rooms)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if devices[a] & devices[b]:
                    overlaps.add(frozenset((a, b)))
        return ReplaySpec(rooms=rooms, contexts=tuple(self.contexts()), workday_entity=self.workday_entity(),
                          connected={a: frozenset(v) for a, v in connected.items()}, overlaps=frozenset(overlaps))

    # ----------------------------------------------------------- recorder
    def recorder_available(self) -> bool:
        return "recorder" in self.hass.config.components

    def _recorder_oldest(self) -> float | None:
        try:
            from homeassistant.components.recorder import get_instance

            oldest = get_instance(self.hass).states_manager.oldest_ts
        except Exception:  # noqa: BLE001 - the attribute is not part of a stable API; fall back to probing days
            return None
        return float(oldest) if oldest else None

    async def _recorder_fetch(self, entity_ids: list[str], start: datetime, end: datetime) -> History:
        from homeassistant.components.recorder import get_instance, history

        def job() -> History:
            raw = history.get_significant_states(
                self.hass, start, end, entity_ids, None, True, True, True, True, True)
            out: History = {}
            for entity_id, items in raw.items():
                rows = []
                for item in items:
                    if isinstance(item, dict):
                        ts = item.get("lc") or item.get("lu")
                        state = item.get("s")
                    else:  # a State object (first row of some domains)
                        ts = item.last_changed.timestamp()
                        state = item.state
                    if ts is not None and state is not None:
                        rows.append((float(ts), str(state)))
                out[entity_id] = rows
            return out

        return await get_instance(self.hass).async_add_executor_job(job)

    async def _fetch(self, entity_ids: list[str], start: float, end: float) -> History:
        self.status.queries += 1
        return await self.fetch_history(entity_ids, dt_util.utc_from_timestamp(start), dt_util.utc_from_timestamp(end))

    # ------------------------------------------------------------ learning
    async def async_learn(self) -> None:
        """Bring the learned days up to date, re-read today and rebuild the model (see module docstring)."""
        if self._lock.locked():
            return
        async with self._lock:
            started = dt_util.utcnow()
            if not self.settings.defaults.enabled:
                self.status.state, self.status.detail = "disabled", "Forecasts are switched off"
                self._recompute()
                return
            if not self.recorder_available() and self.fetch_history == self._recorder_fetch:
                self.status.state = "no_recorder"
                self.status.detail = "The Recorder is not running: there is no history to learn from"
                self._recompute()
                return
            spec = self.replay_spec()
            if not spec.rooms:
                self.status.state, self.status.detail = "ready", "No room has forecasts enabled"
                self._recompute()
                return
            try:
                await self._learn(spec)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - learning must never break the integration
                _LOGGER.warning("forecast_learning_failed: %s", err, exc_info=True)
                self.status.state, self.status.last_error = "error", f"{type(err).__name__}: {err}"
                self.status.detail = "Learning failed; the previous model is kept"
            else:
                self.status.state, self.status.last_error = "ready", None
            self.status.progress = None
            self.status.last_duration = (dt_util.utcnow() - started).total_seconds()
            self._recompute()

    async def _learn(self, spec: ReplaySpec) -> None:
        tz = self._tz()
        now = dt_util.utcnow()
        today = now.astimezone(tz).date()
        window = self.settings.defaults.window_days
        first = today - timedelta(days=window)
        if oldest := self.oldest_history():
            self.status.recorder_oldest = oldest
        if self.status.recorder_oldest:
            first = max(first, datetime.fromtimestamp(self.status.recorder_oldest, tz).date())
        entity_ids = spec.entity_ids()
        todo = []
        d = first
        while d < today:
            key = d.isoformat()
            _ds, de = self._day_bounds(d)
            existing = self.days.get(key)
            if (existing is None or existing.version < SUMMARY_VERSION) and key not in self.no_data \
                    and now.timestamp() >= de + lookahead_seconds():
                todo.append(d)
            d += timedelta(days=1)
        self.status.state = "learning"
        changed = False
        for i, d in enumerate(todo):
            self.status.progress = (i, len(todo))
            self.status.detail = f"Learning {d.isoformat()} ({i + 1} of {len(todo)})"
            async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)
            ds, de = self._day_bounds(d)
            history = await self._fetch(entity_ids, ds - warmup_seconds(), de + lookahead_seconds())
            if not any(rows for rows in history.values()):
                self.no_data.add(d.isoformat())
                continue
            prior = [x for k, x in self.days.items() if k < d.isoformat()]
            day = await self.hass.async_add_executor_job(self._process_day, spec, history, d, ds, de, prior)
            if day is None:
                self.no_data.add(d.isoformat())
                continue
            self.days[day.date] = day
            changed = True
            await asyncio.sleep(0)
        # Today, from midnight until now (not stored: read again at every refresh).
        ds, _de = self._day_bounds(today)
        history = await self._fetch(entity_ids, ds - warmup_seconds(), now.timestamp())
        self.today_replay, self.today = await self.hass.async_add_executor_job(
            self._process_today, spec, history, today, ds, now.timestamp())
        self._seed_live()
        # Window: forget older days.
        cutoff = (today - timedelta(days=window)).isoformat()
        for key in [k for k in self.days if k < cutoff]:
            del self.days[key]
            changed = True
        self.no_data = {k for k in self.no_data if k >= cutoff}
        self.status.last_update = now.isoformat(timespec="seconds")
        self.status.detail = f"{len(self.days)} days learned"
        self._accuracy_cache.clear()
        await self._async_build_model(log_changes=changed)
        self._save_days()
        _LOGGER.debug("forecast_learned days=%d new=%d queries=%d", len(self.days), len(todo), self.status.queries)

    def _settings_eval(self) -> EvalSettings:
        g = self.settings.defaults
        return EvalSettings(min_days=g.min_days, min_entries=g.min_entries)

    def _model_from(self, days: list[HouseDay], areas, today: str) -> HouseModel:
        cutoff = (date.fromisoformat(today) - timedelta(days=self.settings.defaults.window_days)).isoformat()
        return build_house_model([d for d in days if d.date >= cutoff], areas, self._context_keys(),
                                 today=today, half_life_days=self.settings.defaults.half_life_days,
                                 ordinal=lambda s: date.fromisoformat(s).toordinal())

    def _context_keys(self) -> list[str]:
        return [*self.contexts(), ADJACENT_CONTEXT]

    def _process_day(self, spec: ReplaySpec, history: History, d: date, ds: float, de: float,
                     prior: list[HouseDay]) -> HouseDay | None:
        """Executor: replay one completed day, score it with the model of the earlier days, summarise it."""
        rp = replay(spec, history, ds - warmup_seconds(), de + lookahead_seconds())
        if rp.first_seen is None:
            return None
        workday = workday_of(rp, ds, d.weekday())
        model = self._model_from(prior, spec.rooms, d.isoformat()) if prior else None
        daykey = self._daykey({d.isoformat(): workday})
        return summarize(spec, rp, date=d.isoformat(), dow=d.weekday(), workday=workday, day_start=ds, day_end=de,
                         daykey=daykey, model=model, settings=self._settings_eval())

    def _process_today(self, spec: ReplaySpec, history: History, d: date, ds: float, now: float
                       ) -> tuple[Replay, HouseDay]:
        rp = replay(spec, history, ds - warmup_seconds(), now)
        workday = workday_of(rp, ds, d.weekday())
        day = summarize(spec, rp, date=d.isoformat(), dow=d.weekday(), workday=workday, day_start=ds, day_end=now,
                        daykey=self._daykey({d.isoformat(): workday}))
        day.complete = False
        return rp, day

    async def _async_build_model(self, *, log_changes: bool) -> None:
        areas = list(self.replay_spec().rooms)
        days = sorted(self.days.values(), key=lambda d: d.date)
        if self.today is not None:
            days.append(self.today)
        if not days:
            self.model = None
            return
        today = dt_util.now().date().isoformat()
        old = self.model
        self.model = await self.hass.async_add_executor_job(self._model_from, days, areas, today)
        self._accuracy_cache.clear()
        if log_changes and old is not None:
            self._log_pattern_changes(old, self.model)
        self._recompute()

    def _save_days(self, delay: float = FORECAST_SAVE_DELAY_SECONDS) -> None:
        self._days_dirty = True
        self._days_store.async_delay_save(self._days_data, delay)

    def _days_data(self) -> dict[str, Any]:
        self._days_dirty = False
        return {"days": {k: d.as_dict() for k, d in sorted(self.days.items())}, "no_data": sorted(self.no_data),
                "recorder_oldest": self.status.recorder_oldest, "last_update": self.status.last_update}

    # ---------------------------------------------------------------- live
    @callback
    def _listener(self, area_id: str) -> Callable[[], None]:
        @callback
        def update() -> None:
            self._observe_room(area_id)

        return update

    @callback
    def _observe_room(self, area_id: str, *, initial: bool = False) -> None:
        live = self.live.get(area_id)
        result = self.coordinator.occupancy.result(area_id)
        if live is None or result is None:
            return
        now = dt_util.utcnow().timestamp()
        occupied = None if result.state == OccupancyState.UNCERTAIN else result.occupied
        changed = False
        if occupied != live.occupied:
            if occupied is not None and live.occupied is not None:
                live.since, live.since_exact = now, True
            elif occupied is not None and not initial:
                live.since = None  # known again after an outage: the age is unknown
            if occupied and live.occupied is False:
                self._on_entered(area_id, now)
            live.occupied = occupied
            changed = True
        active = any(v.status == SignalStatus.ACTIVE and v.track.capable for v in result.signals)
        if active and not live.active:
            if not initial and (live.last_inactive is None or now - live.last_inactive >= 30):
                self._on_arrival(area_id, now)
                changed = True
        elif not active and live.active:
            live.last_inactive = now
        live.active = active
        if changed and not initial:
            self._schedule_recompute()

    @callback
    def _on_arrival(self, area_id: str, now: float) -> None:
        if self._spec is None:
            self._spec = self.replay_spec()
        overlaps = self._spec.overlaps
        for t, other in reversed(self.arrivals):
            if now - t > OVERLAP_SECONDS:
                break
            if other != area_id and frozenset((area_id, other)) in overlaps:
                return  # the same detection, seen by a sensor shared with another room
        self._check_next_room(area_id, now)
        self.arrivals.append((now, area_id))

    def _seed_live(self) -> None:
        """After reading today's history: visit / vacancy ages and recent arrivals the live view does not know yet."""
        rp = self.today_replay
        if rp is None:
            return
        now = dt_util.utcnow().timestamp()
        for area_id, live in self.live.items():
            tl = rp.occupancy.get(area_id)
            if tl is None or live.since is not None:
                continue
            value = tl.at(now)
            if value is not None and value == live.occupied:
                live.since = tl.run_start(now)
                live.since_exact = live.since is not None and live.since > rp.start + 1
        known = {(round(t), a) for t, a in self.arrivals}
        for t, a in rp.arrivals:
            if now - t <= MAX_GAP_SECONDS and (round(t), a) not in known:
                self.arrivals.append((t, a))
        self.arrivals = deque(sorted(self.arrivals), maxlen=200)

    @callback
    def _track_contexts(self) -> None:
        if self._ctx_unsub:
            self._ctx_unsub()
            self._ctx_unsub = None
        ids = [*self.contexts(), *([w] if (w := self.workday_entity()) else [])]
        if ids and self._running:
            self._ctx_unsub = async_track_state_change_event(self.hass, ids, self._on_context)

    @callback
    def _on_context(self, _event: Event) -> None:
        self._schedule_recompute()

    @callback
    def _schedule_recompute(self) -> None:
        if self._recompute_timer is None and self._running:
            self._recompute_timer = async_call_later(self.hass, 1, self._on_recompute_timer)

    @callback
    def _on_recompute_timer(self, _now: datetime) -> None:
        self._recompute_timer = None
        self._recompute()

    @callback
    def _on_tick(self, _now: datetime) -> None:
        self._recompute()

    def _context_values(self) -> dict[str, bool | None]:
        out: dict[str, bool | None] = {}
        for key in self.contexts():
            state = self.hass.states.get(key)
            value = interpret(state.state if state else None)
            out[key] = None if value == Value.UNKNOWN else value == Value.ACTIVE
        return out

    def _today_workday(self) -> bool:
        today = dt_util.now()
        if entity := self.workday_entity():
            state = self.hass.states.get(entity)
            value = interpret(state.state if state else None)
            if value != Value.UNKNOWN:
                return value == Value.ACTIVE
        return today.weekday() < 5

    def accuracy(self, area_id: str | None, horizon: int) -> Accuracy:
        key = (area_id, horizon)
        if (hit := self._accuracy_cache.get(key)) is None:
            hit = self._accuracy_cache[key] = accuracy(self.days.values(), area_id, horizon)
        return hit

    @callback
    def _recompute(self) -> None:
        """Calculate every room's forecast from the live state; publish and log what changed."""
        if self.hass.is_stopping:
            return
        now_dt = dt_util.utcnow()
        now = now_dt.timestamp()
        daykey = self._daykey({dt_util.now().date().isoformat(): self._today_workday()})
        contexts = self._context_values()
        g = self.settings.defaults
        previous = self.rooms
        rooms: dict[str, RoomForecast] = {}
        for area_id in self.coordinator.config.enabled_rooms:
            eff = self.settings.effective(area_id)
            rf = RoomForecast(area_id=area_id, status=STATUS_OK, horizon=eff.horizon_minutes,
                              threshold=eff.probability_threshold, confidence_threshold=eff.confidence_threshold,
                              updated=now_dt.isoformat(timespec="seconds"))
            result = self.coordinator.occupancy.result(area_id)
            rf.state = result.state.value if result else None
            rf.occupied = result.occupied if result else None
            rooms[area_id] = rf
            if not eff.enabled:
                rf.status, rf.explanation = STATUS_DISABLED, "Forecasts are switched off for this room."
                continue
            usable, known = _usable(result)
            if not usable:
                rf.status = STATUS_UNAVAILABLE
                rf.explanation = "Forecast not available because the room's occupancy data is unreliable " \
                                 "(its occupancy sensors are unavailable)."
                rf.confidence = Confidence("insufficient_data", 0, [rf.explanation], {})
                continue
            model = self.model.rooms.get(area_id) if self.model else None
            if model is None:
                rf.status = STATUS_LEARNING if self.status.state in ("waiting", "learning") else STATUS_INSUFFICIENT
                rf.explanation = "Learning from the Recorder's history has not finished yet." \
                    if rf.status == STATUS_LEARNING else "There is no history for this room yet."
                rf.confidence = Confidence("insufficient_data", 0, [rf.explanation], {})
                continue
            acc = self.accuracy(area_id, eff.horizon_minutes)
            conf = confidence(model.stats, acc, min_days=g.min_days, min_entries=g.min_entries,
                              signals_known=known, usable=True)
            rf.confidence = conf
            if conf.level == "insufficient_data":
                rf.status = STATUS_INSUFFICIENT
                rf.explanation = "Not enough history for a reliable forecast: " + "; ".join(conf.reasons) + "."
                continue
            live = self.live.get(area_id) or LiveRoom()
            inp = ForecastInput(
                now=now, occupied=bool(rf.occupied), since=live.since,
                contexts={**contexts, ADJACENT_CONTEXT: self._connected_occupied(area_id)},
                arrivals=[(a, t) for t, a in self.arrivals if a != area_id and now - t <= MAX_GAP_SECONDS],
                since_exact=live.since_exact,
            )
            minutes = tuple(range(1, max(HORIZONS) + 1))
            fc = forecast(self.model, area_id, inp, daykey, horizons=minutes)
            if fc is None:
                rf.status = STATUS_INSUFFICIENT
                continue
            rf.probabilities = {h: fc.probabilities[h] for h in HORIZONS}
            rf.entry = {h: fc.entry[h] for h in HORIZONS} if fc.entry else None
            rf.schedule = {h: fc.schedule[h] for h in HORIZONS}
            rf.predicted = (rf.probability or 0) >= eff.probability_threshold / 100 and \
                confidence_rank(conf.level) >= confidence_rank(eff.confidence_threshold)
            rf.explanation, rf.factors = explain(self.model, area_id, inp, fc, daykey, eff.horizon_minutes, self.name)
            rf.window = self._window(model, inp, fc, daykey, eff.probability_threshold / 100)
            if live.active or any(a == area_id and now - t <= MAX_GAP_SECONDS for t, a in self.arrivals):
                last = max((t for t, a in self.arrivals if a == area_id), default=now)
                rf.next_rooms = next_rooms(self.model, area_id, now - last, daykey, now)[:3]
        self.rooms = rooms
        self._update_next_room(now, daykey)
        self._log_changes(previous, rooms, now)
        self._check_pending(now)
        fingerprint = tuple((a, r.fingerprint()) for a, r in sorted(rooms.items())) + (
            (self.next_room or {}).get("area_id"), self.status.state)
        async_dispatcher_send(self.hass, SIGNAL_FORECAST_UPDATED)
        if fingerprint != self._published:
            self._published = fingerprint
            async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)

    def _connected_occupied(self, area_id: str) -> bool | None:
        others = [c.b if c.a == area_id else c.a for c in self.connections() if area_id in (c.a, c.b)]
        values = []
        for other in others:
            r = self.coordinator.occupancy.result(other)
            if r is not None and r.state != OccupancyState.UNCERTAIN:
                values.append(bool(r.occupied))
        return any(values) if values else None

    def _window(self, model, inp: ForecastInput, fc, daykey, threshold: float) -> dict[str, Any] | None:
        """When the room is likely occupied next (or likely vacant), from the forecast, else the schedule."""
        def iso(ts: float) -> str:
            return dt_util.utc_from_timestamp(ts).isoformat(timespec="minutes")
        probs = fc.probabilities
        if inp.occupied:
            below = next((m for m in sorted(probs) if probs[m] < 0.5), None)
            if below is None:
                return {"kind": "stays", "source": "forecast", "start": iso(inp.now), "end": None,
                        "text": f"Likely to stay occupied for at least {max(probs)} min"}
            return {"kind": "vacates", "source": "forecast", "start": iso(inp.now), "end": iso(inp.now + below * 60),
                    "text": "Likely vacant within about " + f"{below} min"}
        above = next((m for m in sorted(probs) if probs[m] >= threshold), None)
        if above is not None:
            return {"kind": "occupied", "source": "forecast", "start": iso(inp.now + above * 60), "end": None,
                    "text": f"Likely occupied within {above} min"}
        # Schedule: the next hours whose usual occupancy reaches the threshold.
        start_hour = datetime.fromtimestamp(inp.now, self._tz()).replace(minute=0, second=0, microsecond=0)
        span: list[float] = []
        for i in range(1, SCHEDULE_SCAN_HOURS + 1):
            t = (start_hour + timedelta(hours=i)).timestamp()
            dow, wd, hour = daykey(t)
            if model.occupancy.at(dow, wd, hour) >= threshold:
                span.append(t)
            elif span:
                break
        if not span:
            return {"kind": "none", "source": "schedule", "start": None, "end": None,
                    "text": f"Not usually occupied in the next {SCHEDULE_SCAN_HOURS} h"}
        tz = self._tz()
        fmt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%H:%M")  # noqa: E731
        return {"kind": "occupied", "source": "schedule", "start": iso(span[0]), "end": iso(span[-1] + 3600),
                "text": f"Usually occupied {fmt(span[0])}–{fmt(span[-1] + 3600)}"}

    @callback
    def _update_next_room(self, now: float, daykey) -> None:
        """The room most likely entered next, after the latest arrival (house wide)."""
        if self.model is None or not self.arrivals:
            self.next_room = None
            return
        t, source = self.arrivals[-1]
        if now - t > MAX_GAP_SECONDS:
            self.next_room = None
            return
        candidates = [(a, p) for a, p in next_rooms(self.model, source, now - t, daykey, now)
                      if a in self.rooms and self.rooms[a].status != STATUS_DISABLED]
        best = candidates[0] if candidates and candidates[0][1] >= NEXT_ROOM_MIN_PROBABILITY else None
        new = {"area_id": best[0], "probability": best[1], "source": source, "since": t,
               "candidates": candidates[:4]} if best else None
        old = self.next_room
        self.next_room = new
        if new and (old is None or (old["area_id"], old["source"], old["since"]) != (
                new["area_id"], new["source"], new["since"])):
            self._decision(HOUSE_SUBJECT, "movement_predicted", f"Next room: {self.name(new['area_id'])}",
                           f"Predicted movement from {self.name(source)} to {self.name(new['area_id'])} "
                           f"({round(new['probability'] * 100)} %).",
                           key=("movement", source, new["area_id"]), status=OBSERVED,
                           details={"source": source, "target": new["area_id"],
                                    "probability": round(new["probability"], 3),
                                    "candidates": {a: round(p, 3) for a, p in candidates[:4]}})

    @callback
    def _check_next_room(self, area_id: str, now: float) -> None:
        """An arrival: was it the predicted next room?"""
        nr = self.next_room
        if not nr or now - nr["since"] > MAX_GAP_SECONDS or area_id == nr["source"]:
            return
        hit = area_id == nr["area_id"]
        self._decision(
            HOUSE_SUBJECT, "movement_outcome", "Confirmed" if hit else "Not confirmed",
            f"Predicted movement from {self.name(nr['source'])} to {self.name(nr['area_id'])}; "
            + ("movement subsequently confirmed." if hit else f"the next room was {self.name(area_id)} instead."),
            key=("movement_outcome", nr["source"], nr["area_id"], hit), status=NOTED,
            details={"source": nr["source"], "predicted": nr["area_id"], "actual": area_id, "confirmed": hit})
        self.next_room = None

    # ------------------------------------------------------------- logging
    def _decision(self, subj: str, kind: str, action: str, explanation: str, *, key: tuple, status: str = NOTED,
                  details: dict[str, Any] | None = None) -> None:
        log = self.coordinator.lighting.decisions
        log.record(subj, module=MODULE_FORECAST, trigger="forecast", kind=kind, action=action,
                   explanation=explanation, mode="observing", status=status, details=details, key=key)

    @callback
    def _log_changes(self, previous: dict[str, RoomForecast], rooms: dict[str, RoomForecast], now: float) -> None:
        for area_id, rf in rooms.items():
            old = previous.get(area_id)
            name = self.name(area_id)
            if old is None:
                continue
            if rf.status == STATUS_UNAVAILABLE and old.status != STATUS_UNAVAILABLE:
                self._decision(subject(area_id), "forecast_unavailable", "Forecast unavailable",
                               f"Forecast for {name} not available because its occupancy data is unreliable.",
                               key=("unavailable",))
            elif old.status == STATUS_UNAVAILABLE and rf.status not in (STATUS_UNAVAILABLE, STATUS_DISABLED):
                self._decision(subject(area_id), "forecast_available", "Forecast available again",
                               f"Occupancy data of {name} is usable again; forecasts resume.", key=("available",))
            if rf.confidence and old.confidence and rf.status != STATUS_UNAVAILABLE \
                    and old.status != STATUS_UNAVAILABLE:
                a, b = confidence_rank(old.confidence.level), confidence_rank(rf.confidence.level)
                if a != b:
                    word = "reduced" if b < a else "increased"
                    why = "; ".join(rf.confidence.reasons[:2]) if b < a else ""
                    self._decision(
                        subject(area_id), "confidence", f"Confidence {rf.confidence.level.replace('_', ' ')}",
                        f"Prediction confidence for {name} {word} ({old.confidence.level.replace('_', ' ')} → "
                        f"{rf.confidence.level.replace('_', ' ')})" + (f" because: {why}." if why else "."),
                        key=("confidence", rf.confidence.level),
                        details=rf.confidence.components)
            if rf.predicted and not old.predicted and rf.occupied is False:
                p = rf.probability or 0
                self._decision(
                    subject(area_id), "predicted", f"Predicted within {rf.horizon} min",
                    f"Predicted {name} occupancy within {rf.horizon} minutes ({round(p * 100)} %): {rf.explanation}",
                    key=("predicted", rf.horizon), status=OBSERVED,
                    details={"probability": round(p, 3), "threshold": rf.threshold / 100,
                             "confidence": rf.confidence.level if rf.confidence else None,
                             "factors": [f.text for f in rf.factors[:4]]})
                self._pending.append({"area_id": area_id, "at": now, "until": now + rf.horizon * 60,
                                      "horizon": rf.horizon, "probability": p})

    @callback
    def _on_entered(self, area_id: str, now: float) -> None:
        for item in [p for p in self._pending if p["area_id"] == area_id and p["at"] <= now <= p["until"]]:
            self._pending.remove(item)
            self._decision(subject(area_id), "prediction_confirmed", "Prediction confirmed",
                           f"{self.name(area_id)} became occupied {round((now - item['at']) / 60)} min after the "
                           f"prediction ({round(item['probability'] * 100)} % within {item['horizon']} min).",
                           key=("outcome", True), details={"minutes_after": round((now - item["at"]) / 60, 1)})

    @callback
    def _check_pending(self, now: float) -> None:
        for item in [p for p in self._pending if now > p["until"]]:
            self._pending.remove(item)
            self._decision(subject(item["area_id"]), "prediction_missed", "Prediction did not materialise",
                           f"{self.name(item['area_id'])} was predicted occupied within {item['horizon']} min "
                           f"({round(item['probability'] * 100)} %), but nobody came.",
                           key=("outcome", False))

    def _log_pattern_changes(self, old: HouseModel, new: HouseModel) -> None:
        for area_id, room in new.rooms.items():
            before = old.rooms.get(area_id)
            if before is None or room.stats.days < self.settings.defaults.min_days:
                continue
            diffs = [(abs(room.occupancy.hour[h] - before.occupancy.hour[h]), h) for h in range(24)]
            change, hour = max(diffs)
            if change >= PATTERN_CHANGE:
                was, now = before.occupancy.hour[hour], room.occupancy.hour[hour]
                self._decision(
                    subject(area_id), "pattern_changed", "Learned pattern changed",
                    f"Learned pattern of {self.name(area_id)} changed: around {hour:02d}:00 it is now usually "
                    f"occupied {round(now * 100)} % of the time (was {round(was * 100)} %).",
                    key=("pattern", hour), details={"hour": hour, "before": round(was, 3), "after": round(now, 3)})

    # ----------------------------------------------------------------- read
    def room(self, area_id: str) -> RoomForecast | None:
        """The current forecast of a room (API for other modules; observation only)."""
        return self.rooms.get(area_id)

    def summary(self, area_id: str) -> dict[str, Any] | None:
        rf = self.rooms.get(area_id)
        return rf.as_dict(self.name) if rf else None

    def diagnostics(self) -> dict[str, Any]:
        return {
            "settings": self.settings.as_dict(),
            "status": self.status.as_dict(),
            "days_learned": sorted(self.days),
            "days_without_data": sorted(self.no_data),
            "contexts": self.contexts(),
            "workday_entity": self.workday_entity(),
            "connections": [c.as_dict() for c in self.connections()],
            "rooms": {a: r.as_dict() for a, r in self.rooms.items()},
            "next_room": self.next_room,
        }


def _usable(result: RoomResult | None) -> tuple[bool, float]:
    """Is the room's live occupancy trustworthy, and what share of its occupancy signals is known."""
    if result is None or result.state == OccupancyState.UNCERTAIN or result.occupied is None:
        return False, 0.0
    capable = [v for v in result.signals if v.track.capable]
    if not capable:
        return False, 0.0
    known = sum(1 for v in capable if v.status != SignalStatus.UNKNOWN)
    return known > 0, known / len(capable)

