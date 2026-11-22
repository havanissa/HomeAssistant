"""Data acquisition: live observation, bounded Recorder backfill, long-term statistics and weather forecasts.

Everything the App learns from is kept in its own compact store (``observations.ObservationStore``: one JSON
line per state change, day files in UTC, with per-entity coverage and gaps), so learning goes on after Home
Assistant's Recorder has purged its detailed history. The Recorder's retention is never changed.

Backfill
    For every tracked entity the App keeps the span it has read (``progress.json``: ``read_from`` /
    ``read_to``, persisted after every window). On start and after every reconnect it reads, in this order:

    1. the time since it last observed live (the App or Home Assistant was down);
    2. holes inside the read span (windows the Recorder did not answer);
    3. older history, backwards, one window at a time, until ``history_days`` or until the Recorder has
       nothing more for that entity (``limit``: never asked again).

    A request covers ``WINDOW_HOURS`` for at most ``ENTITIES_PER_CALL`` entities, with a pause after each, so the
    Recorder is never asked for a large range at once. Every record keeps its original UTC timestamp; nothing
    is interpolated: what the Recorder does not have stays a gap.

Long-term statistics
    For temperature sensors that have them, the hourly mean/min/max before the start of the detailed history
    are kept separately (``statistics/<entity>.json``). They are *hourly aggregates*, shown as such in the
    history graph, and never used as state changes for learning (a 30-minute warming rate cannot be read from
    hourly means).

Forecast
    The weather entity's hourly forecast through ``weather/subscribe_forecast``: kept in memory and in
    ``forecast.json`` (with the time it was received), so a restart does not lose it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import json
import logging
import math
from pathlib import Path
import time
from typing import Any

from .observations import ObservationStore, history_rows, sample
from .store import _atomic_write
from .units import as_float, to_celsius

_LOGGER = logging.getLogger(__name__)

WINDOW_HOURS = 12
ENTITIES_PER_CALL = 10
PAUSE_S = 0.3
REPORT_S = 900.0          # an unchanged value is confirmed this often while it is observed live
MARK_S = 60.0             # live coverage is extended this often
FORECAST_MAX_AGE_S = 6 * 3600.0


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()


def _ts(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value < 1e11 else float(value) / 1000.0
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()
    return None


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Acquisition:
    """Owns the observation store and keeps it filled. ``ha`` is the App's websocket client."""

    def __init__(self, data_dir: Path | None, ha: Any, *, now: Callable[[], float] = time.time) -> None:
        self.dir = data_dir / "history" if data_dir else None
        self.store = ObservationStore(self.dir / "obs" if self.dir else None)
        self.ha = ha
        self.now = now
        self.tracked: dict[str, str] = {}
        self.history_days = 90
        self.progress: dict[str, dict[str, Any]] = {}
        self.statistics: dict[str, list[list[float | None]]] = {}
        self.forecast: dict[str, Any] = {}          # {"entity_id", "received_at", "hourly": [[t, temp, cloud]]}
        self.status: dict[str, Any] = {"state": "idle", "windows": 0, "records": 0, "error": None, "started_at": None,
                                       "finished_at": None, "pending": 0, "statistics": None, "recorder": None}
        self._last_live: dict[str, tuple[float, tuple[Any, ...]]] = {}
        self._marked_at: float | None = None
        self._task: asyncio.Task[Any] | None = None
        self._again = False
        self._forecast_unsub: Any = None
        self._forecast_entity: str | None = None
        self._listeners: list[Callable[[str], None]] = []
        self._ha_unit = "°C"

    # ---------------------------------------------------------------- set-up
    def load(self) -> None:
        self.store.load()
        if self.dir is None:
            return
        self.progress = {k: v for k, v in _read_json(self.dir / "progress.json", {}).items() if isinstance(v, dict)}
        self.forecast = _read_json(self.dir / "forecast.json", {}) or {}
        for path in (self.dir / "statistics").glob("*.json") if (self.dir / "statistics").is_dir() else []:
            rows = _read_json(path, [])
            if isinstance(rows, list):
                self.statistics[path.stem] = rows

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def _emit(self, kind: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Acquisition listener failed")

    def configure(self, tracked: dict[str, str], history_days: int) -> None:
        """The entities to observe changed: new ones are backfilled, removed ones stop being covered (their
        records stay until retention removes them, so learned models keep their evidence)."""
        removed = set(self.tracked) - set(tracked)
        self.store.forget(removed)
        added = set(tracked) - set(self.tracked)
        self.tracked = dict(tracked)
        self.history_days = history_days
        if added and self.ha is not None and getattr(self.ha, "connected", False):
            self.schedule_backfill()

    @property
    def ha_unit(self) -> str:
        unit = ((getattr(self.ha, "config", None) or {}).get("unit_system") or {}).get("temperature")
        return unit if unit in ("°C", "°F") else self._ha_unit

    # ------------------------------------------------------------- live data
    def on_state(self, entity_id: str, state: dict[str, Any] | None) -> None:
        kind = self.tracked.get(entity_id)
        if kind is None or state is None:
            return
        rec = sample(kind, entity_id, state, ha_unit=self.ha_unit, src="live")
        if rec is None:
            return
        sig = (rec.get("s"), rec.get("v"), rec.get("q"), json.dumps(rec.get("a"), sort_keys=True))
        last = self._last_live.get(entity_id)
        if last and last[1] == sig:
            return
        self._last_live[entity_id] = (rec["t"], sig)
        self.store.append(rec)

    def tick(self) -> None:
        """Every minute while connected: confirm unchanged values now and then, extend live coverage."""
        if self.ha is None or not getattr(self.ha, "connected", False):
            return
        now = self.now()
        for entity_id, kind in self.tracked.items():
            last = self._last_live.get(entity_id)
            if last is None or now - last[0] >= REPORT_S:
                state = self.ha.states.get(entity_id)
                rec = sample(kind, entity_id, state, ha_unit=self.ha_unit, src="report", t=now) if state else None
                if rec is not None:
                    self.store.append(rec)
                    self._last_live[entity_id] = (now, (rec.get("s"), rec.get("v"), rec.get("q"),
                                                        json.dumps(rec.get("a"), sort_keys=True)))
        if self._marked_at is not None:
            for entity_id in self.tracked:
                if entity_id in self.ha.states:
                    self.store.mark(entity_id, self._marked_at, now)
                    self._extend(entity_id, self._marked_at, now)
        self._marked_at = now

    def flush(self) -> None:
        self.store.flush()
        self.store.save_state()
        self._save_progress()

    def connected(self) -> None:
        """(Re)connected to Home Assistant: record what is there now, then fill what was missed."""
        now = self.now()
        for entity_id, kind in self.tracked.items():
            state = self.ha.states.get(entity_id)
            if state is not None:
                rec = sample(kind, entity_id, state, ha_unit=self.ha_unit, src="live", t=now)
                if rec is not None:
                    self.store.append(rec)
                    self._last_live[entity_id] = (now, (rec.get("s"), rec.get("v"), rec.get("q"),
                                                        json.dumps(rec.get("a"), sort_keys=True)))
        self._marked_at = now
        self.schedule_backfill()

    def disconnected(self) -> None:
        self._marked_at = None
        self._forecast_unsub = None
        self._forecast_entity = None

    def _extend(self, entity_id: str, start: float, end: float) -> None:
        prog = self.progress.setdefault(entity_id, {})
        if prog.get("read_to") is None or prog.get("read_from") is None:
            # Nothing read from the Recorder yet: the live span alone must not hide older history.
            prog.setdefault("live_from", start)
            return
        if start <= prog["read_to"] + 1:
            prog["read_to"] = max(prog["read_to"], end)

    # ------------------------------------------------------------- backfill
    def schedule_backfill(self) -> None:
        """Read what is missing. A request while a read is still running (even while it is only finishing, e.g.
        flushing after an interruption) is not dropped: one more pass follows."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._backfill_passes(), name="spc-backfill")
        else:
            self._again = True

    async def _backfill_passes(self) -> None:
        while True:
            self._again = False
            await self.backfill()
            if not self._again:
                return

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await asyncio.to_thread(self.flush)

    def plan(self, now: float) -> list[tuple[float, float, list[str], str]]:
        """The windows still to read: ``(start, end, entities, why)``, newest needs first."""
        horizon = now - self.history_days * 86400.0
        newer: dict[tuple[float, float], list[str]] = {}
        holes: dict[tuple[float, float], list[str]] = {}
        older: dict[tuple[float, float], list[str]] = {}
        step = WINDOW_HOURS * 3600.0

        def windows(start: float, end: float) -> list[tuple[float, float]]:
            out = []
            t = math.floor(start / step) * step
            while t < end:
                out.append((max(t, start), min(t + step, end)))
                t += step
            return out

        for entity_id in self.tracked:
            prog = self.progress.setdefault(entity_id, {})
            read_from, read_to = prog.get("read_from"), prog.get("read_to")
            if read_from is None or read_to is None:
                # Never read: from (the start of live observation, else now) backwards.
                first = prog.get("live_from") or now
                prog["read_from"] = prog["read_to"] = first
                read_from = read_to = first
            if read_to < now - 60:
                for w in windows(read_to, now):
                    newer.setdefault(w, []).append(entity_id)
            for a, b in self.store.gaps.get(entity_id, []):
                if b > horizon and a < now:
                    for w in windows(max(a, horizon), min(b, now)):
                        holes.setdefault(w, []).append(entity_id)
            limit = prog.get("limit")
            low = max(horizon, limit or horizon)
            if read_from > low + 60:
                for w in reversed(windows(low, read_from)):
                    older.setdefault(w, []).append(entity_id)
        out: list[tuple[float, float, list[str], str]] = []
        for why, group in (("downtime", sorted(newer.items())), ("hole", sorted(holes.items())),
                           ("older", sorted(older.items(), reverse=True))):
            for (a, b), ids in group:
                for i in range(0, len(ids), ENTITIES_PER_CALL):
                    out.append((a, b, sorted(ids[i:i + ENTITIES_PER_CALL]), why))
        return out

    async def backfill(self) -> None:
        if self.ha is None or not getattr(self.ha, "connected", False) or not self.tracked:
            return
        now = self.now()
        todo = self.plan(now)
        self.status.update(state="running", started_at=_iso(now), finished_at=None, pending=len(todo), error=None)
        self._emit("backfill")
        try:
            for start, end, ids, why in todo:
                await self._window(start, end, ids, why)
                self.status["pending"] -= 1
                if self.status["windows"] % 10 == 0:
                    await asyncio.to_thread(self.flush)
                    self._emit("backfill")
                await asyncio.sleep(PAUSE_S)
            await self._statistics(now)
        except asyncio.CancelledError:
            await asyncio.to_thread(self.flush)
            raise
        except (ConnectionError, TimeoutError) as err:
            await asyncio.to_thread(self.flush)
            self.status.update(state="interrupted", error=f"Interrupted ({type(err).__name__}); it continues where it "
                                                          "stopped when Home Assistant is back")
            self._emit("backfill")
            return
        await asyncio.to_thread(self.flush)
        self.status.update(state="done", finished_at=_iso(self.now()), pending=0)
        _LOGGER.info("History read: %d records in %d windows", self.status["records"], self.status["windows"])
        self._emit("backfill")

    async def _window(self, start: float, end: float, ids: list[str], why: str) -> None:
        try:
            raw = await self.ha.command(
                "history/history_during_period", start_time=_iso(start), end_time=_iso(end), entity_ids=ids,
                include_start_time_state=True, significant_changes_only=False, minimal_response=False,
                no_attributes=False)
        except (ConnectionError, TimeoutError):
            raise
        except Exception as err:  # noqa: BLE001 - the Recorder refused: the window stays a gap
            code = getattr(err, "code", type(err).__name__)
            self.status["recorder"] = f"The Recorder did not answer ({code})"
            for entity_id in ids:
                self.store.add_gap(entity_id, start, end)
            if code == "unknown_command":
                raise ConnectionError("The history component is not loaded") from err
            return
        self.status["windows"] += 1
        data = raw if isinstance(raw, dict) else {}
        for entity_id in ids:
            kind = self.tracked.get(entity_id)
            if kind is None:
                continue
            prog = self.progress.setdefault(entity_id, {})
            rows = data.get(entity_id) or []
            records = history_rows(kind, entity_id, rows, ha_unit=self.ha_unit, after=start - 0.001, before=end)
            for rec in records:
                if abs(rec["t"] - start) < 0.01:
                    rec["src"] = "recorder_start"   # the state *at* the window's start, not a change at that time
                self.store.append(rec)
            self.status["records"] += len(records)
            if records:
                begins = start
                if why == "older" and records[0]["t"] > start + 1 and not rows_before(rows, start):
                    begins = records[0]["t"]  # the entity's history begins here
                    prog["limit"] = begins
                self.store.mark(entity_id, begins, end)
                self.store.fill_gap(entity_id, begins, end)
                prog["read_from"] = min(prog.get("read_from", begins), begins)
                prog["read_to"] = max(prog.get("read_to", end), end)
            elif why == "older":
                prog["limit"] = end      # nothing that old in the Recorder: never asked again
                prog["read_from"] = min(prog.get("read_from", end), end)
            elif why == "downtime":
                # The entity existed but the Recorder has nothing for this window (it was not recorded): a gap.
                self.store.add_gap(entity_id, start, end)
                prog["read_to"] = max(prog.get("read_to", end), end)
            else:
                prog["read_to"] = max(prog.get("read_to", end), end)

    def _save_progress(self) -> None:
        if self.dir is None:
            return
        with contextlib.suppress(OSError):
            self.dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.dir / "progress.json", self.progress)

    # ---------------------------------------------------------- statistics
    async def _statistics(self, now: float) -> None:
        """Hourly long-term statistics of the temperature sensors, for the time before the detailed history."""
        temps = [e for e, k in self.tracked.items() if k in ("room_temp", "outdoor") and e.startswith("sensor.")]
        if not temps:
            return
        try:
            listed = await self.ha.command("recorder/list_statistic_ids", statistic_type="mean")
        except (ConnectionError, TimeoutError):
            raise
        except Exception as err:  # noqa: BLE001
            self.status["statistics"] = f"Long-term statistics are not available ({getattr(err, 'code', err)})"
            return
        have = {item.get("statistic_id"): item for item in listed or [] if isinstance(item, dict)}
        wanted = [e for e in temps if e in have]
        horizon = now - self.history_days * 86400.0
        read = 0
        for entity_id in wanted:
            detailed_from = (self.progress.get(entity_id) or {}).get("read_from") or now
            if detailed_from <= horizon + 3600:
                continue
            try:
                result = await self.ha.command(
                    "recorder/statistics_during_period", start_time=_iso(horizon), end_time=_iso(detailed_from),
                    statistic_ids=[entity_id], period="hour", types=["mean", "min", "max"])
            except (ConnectionError, TimeoutError):
                raise
            except Exception:  # noqa: BLE001
                continue
            unit = (have[entity_id].get("statistics_unit_of_measurement")
                    or have[entity_id].get("display_unit_of_measurement") or "°C")
            rows = []
            for row in (result or {}).get(entity_id) or []:
                t = _ts(row.get("start"))
                if t is None:
                    continue
                rows.append([t, *(None if to_celsius(row.get(k), unit) is None else round(to_celsius(row.get(k), unit), 2)
                                  for k in ("mean", "min", "max"))])
            self.statistics[entity_id] = rows
            read += len(rows)
            if self.dir is not None:
                with contextlib.suppress(OSError):
                    (self.dir / "statistics").mkdir(parents=True, exist_ok=True)
                    _atomic_write(self.dir / "statistics" / f"{entity_id}.json", rows)
            await asyncio.sleep(PAUSE_S)
        self.status["statistics"] = (f"{read} hourly values for {len(wanted)} sensors before the detailed history"
                                     if wanted else "No long-term statistics for the configured temperature sensors")

    # ------------------------------------------------------------- forecast
    async def follow_forecast(self, entity_id: str | None) -> None:
        """Keep the hourly forecast of this weather entity (``None``: no forecast is used)."""
        if entity_id == self._forecast_entity and self._forecast_unsub is not None:
            return
        if self._forecast_unsub is not None:
            with contextlib.suppress(Exception):
                await self._forecast_unsub()
            self._forecast_unsub = None
        self._forecast_entity = entity_id
        if entity_id is None or self.ha is None or not getattr(self.ha, "connected", False):
            return

        def handle(event: Any) -> None:
            forecast = (event or {}).get("forecast") if isinstance(event, dict) else None
            if not isinstance(forecast, list):
                return
            unit = ((self.ha.states.get(entity_id) or {}).get("attributes") or {}).get("temperature_unit") or self.ha_unit
            hourly = []
            for item in forecast[:96]:
                if not isinstance(item, dict):
                    continue
                t = _ts(item.get("datetime"))
                if t is None:
                    continue
                temp = to_celsius(item.get("temperature"), unit)
                cloud = as_float(item.get("cloud_coverage"))
                hourly.append([t, None if temp is None else round(temp, 2),
                               None if cloud is None or not 0 <= cloud <= 100 else round(cloud)])
            self.forecast = {"entity_id": entity_id, "received_at": self.now(), "hourly": hourly}
            if self.dir is not None:
                with contextlib.suppress(OSError):
                    self.dir.mkdir(parents=True, exist_ok=True)
                    _atomic_write(self.dir / "forecast.json", self.forecast)
            self._emit("forecast")

        try:
            self._forecast_unsub = await self.ha.subscribe("weather/subscribe_forecast",
                                                           {"entity_id": entity_id, "forecast_type": "hourly"}, handle)
        except Exception as err:  # noqa: BLE001 - a weather entity without an hourly forecast
            self.status["forecast"] = f"No hourly forecast from {entity_id} ({getattr(err, 'code', type(err).__name__)})"
            self._forecast_unsub = None

    def current_forecast(self) -> list[tuple[float, float | None, float | None]]:
        """The stored hourly forecast if it is recent enough, else nothing (an old forecast is not a forecast)."""
        received = self.forecast.get("received_at")
        if not isinstance(received, (int, float)) or self.now() - received > FORECAST_MAX_AGE_S:
            return []
        return [(row[0], row[1], row[2]) for row in self.forecast.get("hourly") or [] if row[0] >= self.now() - 3600]

    # ----------------------------------------------------------------- views
    def coverage(self) -> dict[str, dict[str, Any]]:
        """Per entity: observed span, gaps, Recorder limit and statistics, for the diagnostics and the UI."""
        out = {}
        for entity_id, kind in sorted(self.tracked.items()):
            span = self.store.coverage.get(entity_id) or {}
            gaps = self.store.gaps.get(entity_id, [])
            prog = self.progress.get(entity_id) or {}
            covered = (span.get("to", 0) - span.get("from", 0)) if span else 0.0
            missing = sum(b - a for a, b in gaps if a >= span.get("from", 0))
            out[entity_id] = {"kind": kind, "from": _iso(span["from"]) if span else None,
                              "to": _iso(span["to"]) if span else None,
                              "days": round(max(0.0, covered - missing) / 86400.0, 1),
                              "gaps": len(gaps), "gap_hours": round(missing / 3600.0, 1),
                              "recorder_limit": _iso(prog["limit"]) if prog.get("limit") else None,
                              "statistics_hours": len(self.statistics.get(entity_id, []))}
        return out


def rows_before(rows: list[Any], start: float) -> bool:
    """Did the Recorder return a state at (or before) the window's start: the entity existed then."""
    for row in rows:
        if isinstance(row, dict):
            t = _ts(row.get("lu", row.get("last_updated"))) or _ts(row.get("lc", row.get("last_changed")))
            if t is not None and t <= start + 0.01:
                return True
    return False
