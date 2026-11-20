"""Climate (Phase A7.0): Smart Comfort's climate module. Configuration, discovery, live monitoring, history.

The module **observes only**. It reads Home Assistant (states, registries, the
Recorder's history through the allowlisted read commands of :mod:`ha`) and
writes nothing but the App's own storage. There is no path from here to a
thermostat: the App's Home Assistant client has no service call, and the
companion's fenced executor accepts light and cover commands only.

Storage (``/data/climate``)::

    config.json                         the configuration (``climate_model``; atomic writes, revision-checked)
    backups/climate-<stamp>-<rev>.json  earlier versions (the latest 30)
    config.corrupt-<stamp>.json         an unreadable config.json, moved aside when it was recovered from a backup
    observations/                       the observation store (``climate_observations``)

Every change goes through ``review`` (validation, the list of changes,
warnings) and ``save`` with the revision it was based on; a stale save from
another browser session is refused. Imports and restores are reviewed the
same way.

Observation: the entities a configuration names (room sensors, thermostats,
windows and doors, the outdoor source) plus each configured room's occupancy
entity, ``sun.sun`` and the room's curtain covers are recorded on every state
change, with an unchanged value re-confirmed at most every ``report_minutes``.
Nothing is polled: values arrive with Home Assistant's own state stream. When
the App starts, after a reconnect and for a newly configured entity, the gap
since the last observation is filled once from the Recorder in bounded
24-hour windows (``backfill_days`` at most); what the Recorder no longer has is
recorded as a gap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

from . import climate_model as cm
from .climate_learning import ThermalLearning
from .climate_observations import KINDS, MIN_INTERVAL, ObservationStore, history_rows, intervals, numeric_series, sample, signature
from .const import APP_VERSION, INTEGRATION_DOMAIN
from .ha import HomeAssistantClient, HomeAssistantError, utcnow_iso
from .rooms import RoomsService

_LOGGER = logging.getLogger(__name__)

BACKUP_KEEP = 30
BACKUP_COALESCE = 600.0
BACKUP_ID = re.compile(r"^climate-\d{8}T\d{9}Z-[0-9a-f]{16}(?:-\d+)?$")
FLUSH_SECONDS = 15.0
STATE_SECONDS = 60.0
PRUNE_SECONDS = 6 * 3600.0
BACKFILL_CHUNK = 24 * 3600.0
BACKFILL_BATCH = 20
BACKFILL_PAUSE = 0.25
MIN_GAP = 120.0
#: History windows of the room page (seconds).
WINDOWS = (6 * 3600, 24 * 3600, 3 * 86400, 7 * 86400, 30 * 86400)
OCCUPANCY_UID = re.compile(rf"^{INTEGRATION_DOMAIN}_room_(?P<area>.+)_occupied$")
OPENING_ROLE = {"window": "Window", "door": "Door"}
QUALITY_TEXT = {
    "ok": "OK", "stale": "Not updated recently", "unavailable": "Unavailable", "unknown": "No value",
    "missing": "Entity not found", "not_numeric": "Not a number", "wrong_unit": "Not a temperature unit",
    "implausible": "Implausible value", "none": "Not assigned",
}


class ClimateError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def _stamp() -> str:
    now = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}Z"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds")


class ClimateService:
    """Smart Comfort's climate module (observation only)."""

    def __init__(self, ha: HomeAssistantClient, rooms: RoomsService, data_dir: Path | None) -> None:
        self.ha = ha
        self.rooms = rooms
        self.dir = data_dir / "climate" if data_dir else None
        self.config: dict[str, Any] = cm.empty_config()
        self.revision = cm.revision_of(self.config)
        self.saved_at: str | None = None
        self.read_only: str | None = None if self.dir else "The App has no data folder, so the climate configuration " \
                                                           "cannot be saved."
        self.read_only_code: str | None = None if self.dir else "no_storage"
        self.notices: list[dict[str, str]] = []
        self.store = ObservationStore(self.dir / "observations" if self.dir else None)
        # Phase A7.1: thermal learning (models in /data/climate/models; reads the observations, writes no device).
        self.learning = ThermalLearning(self, data_dir)
        self.learning.add_listener(self._emit)
        # Other App subsystems, read for context (attached by the core; all optional).
        self.occupancy: Any = None
        self.lighting: Any = None
        self.curtains: Any = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[str], None]] = []
        self._last_auto_backup = 0.0
        self._tracked: dict[str, str] = {}
        self._last: dict[str, tuple[tuple[Any, ...], float, float]] = {}  # entity -> (signature, t, written mono)
        self._live_since: float | None = None
        self._since: dict[str, float] = {}  # entity -> when this App process began observing it
        self._pending: dict[str, tuple[float, float, bool]] = {}  # entity -> (from, to, new): Recorder reads queued
        self._tasks: list[asyncio.Task[Any]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._backfill_task: asyncio.Task[Any] | None = None
        self._backfill_wanted = False
        self.backfill: dict[str, Any] = {"state": "idle", "done_at": None, "error": None, "windows": 0, "records": 0,
                                         "entities": 0}
        self._states_handle: asyncio.TimerHandle | None = None
        self._last_prune = 0.0
        self._last_state_save = 0.0

    # ----------------------------------------------------------------- paths
    @property
    def config_path(self) -> Path:
        assert self.dir is not None
        return self.dir / "config.json"

    @property
    def backup_dir(self) -> Path:
        assert self.dir is not None
        return self.dir / "backups"

    def add_listener(self, listener: Callable[[str], None]) -> None:
        """``listener(what)``: "config" (saved, restored, imported or renamed) or "states" (a live value moved)."""
        self._listeners.append(listener)

    def _emit(self, what: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Climate listener failed")

    @property
    def ha_unit(self) -> str:
        return ((self.ha.config or {}).get("unit_system") or {}).get("temperature") or "°C"

    # --------------------------------------------------------------- loading
    def load(self) -> None:
        if self.dir is None:
            return
        try:
            for path in (self.dir, self.backup_dir):
                path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self.read_only, self.read_only_code = f"The climate folder cannot be created ({err}).", "no_storage"
            _LOGGER.error("Climate storage is not available: %s", err)
            return
        for leftover in [*self.dir.glob(".*.tmp"), *self.backup_dir.glob(".*.tmp")]:
            with contextlib.suppress(OSError):
                leftover.unlink()
        self.store.load()
        self.learning.load()
        if not self.config_path.exists():
            return
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            config = cm.validate(cm.migrate(raw)[0])
        except cm.ClimateConfigError as err:
            if any(e.get("code") == "newer_format" for e in err.errors):
                self.read_only, self.read_only_code = err.errors[0]["message"], "newer_format"
                _LOGGER.error("Climate configuration not loaded: %s", err)
                return
            self._recover(str(err))
            return
        except ValueError as err:
            self._recover(f"{type(err).__name__}: {err}")
            return
        except OSError as err:
            # Could not be read (not damaged): keep the file and refuse changes until the App restarts.
            self.read_only, self.read_only_code = f"The climate configuration could not be read ({err}).", "read_error"
            _LOGGER.error("Climate configuration not read: %s", err)
            return
        self.config, self.revision, self.saved_at = config, cm.revision_of(config), config.get("updated_at")

    def _recover(self, reason: str) -> None:
        aside = self.dir / f"config.corrupt-{_stamp()}.json"  # type: ignore[operator]
        with contextlib.suppress(OSError):
            os.replace(self.config_path, aside)
        _LOGGER.error("The climate configuration could not be read (%s); moved it to %s", reason, aside.name)
        for info in self.backups():
            try:
                config = cm.validate(cm.migrate(self._read_backup(info["id"])["config"])[0])
            except (cm.ClimateConfigError, ClimateError, OSError, ValueError, KeyError, TypeError):
                continue
            self.config, self.revision = config, cm.revision_of(config)
            try:
                self._store(config)
            except OSError as err:
                _LOGGER.error("The recovered climate configuration could not be stored: %s", err)
            self.notices.append({"code": "recovered", "message": f"The stored climate configuration was damaged. The "
                                 f"backup from {info['created_at']} was restored (changes saved after it are not in it; "
                                 f"backups are kept at most every 10 minutes while editing); the damaged file was kept "
                                 f"as {aside.name}."})
            return
        self.notices.append({"code": "recovered_empty", "message": "The stored climate configuration was damaged and "
                             f"no backup could be read, so it starts empty. The damaged file was kept as {aside.name}."})

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._unsubscribe = self.ha.add_listener(self._on_ha)
        self._retrack()
        if self.ha.connected:
            self._connected()
        self._tasks.append(asyncio.create_task(self._flush_loop(), name="climate-observations"))
        await self.learning.start()

    async def stop(self) -> None:
        await self.learning.stop()
        if self._unsubscribe:
            self._unsubscribe()
        if self._states_handle is not None:
            self._states_handle.cancel()
        for task in [*self._tasks, *([self._backfill_task] if self._backfill_task else [])]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        self._mark_live(time.time())
        await asyncio.to_thread(self._flush_all)

    def _flush_all(self) -> None:
        self.store.flush()
        self.store.save_state()

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                now = time.time()
                self._mark_live(now)
                written = await asyncio.to_thread(self.store.flush)
                # The coverage is stored with every flush that wrote records (so a crash cannot make the next gap
                # fill read records that were already stored), otherwise once a minute.
                if written or now - self._last_state_save >= STATE_SECONDS:
                    self._last_state_save = now
                    await asyncio.to_thread(self.store.save_state)
                if now - self._last_prune >= PRUNE_SECONDS:
                    self._last_prune = now
                    await asyncio.to_thread(self.store.prune, self.config["observation"]["retention_days"], now)
            except Exception:  # noqa: BLE001 - observation must never stop the App
                _LOGGER.exception("Climate observation flush failed")

    # ------------------------------------------------------------- tracking
    def occupancy_entities(self) -> dict[str, str]:
        """area_id -> its occupancy entity (``binary_sensor``), found by registry unique id (never by name)."""
        out: dict[str, str] = {}
        for entity_id, reg in self.rooms.index().registry.items():
            if reg.get("platform") != INTEGRATION_DOMAIN or reg.get("disabled_by"):
                continue
            if match := OCCUPANCY_UID.match(str(reg.get("unique_id") or "")):
                out[match["area"]] = entity_id
        return out

    def _covers(self, area_id: str) -> list[str]:
        try:
            mgr = self.curtains.manager if self.curtains is not None else None
            if mgr is None:
                return []
            return [layer.entity_id for w in mgr.config.windows.values() if w.area_id == area_id for layer in w.layers]
        except Exception:  # noqa: BLE001 - context only
            return []

    def tracked(self) -> dict[str, str]:
        """entity_id -> observation kind, for the current configuration."""
        config = self.config
        if not config["observation"]["enabled"] or not config["rooms"]:
            return {}
        out: dict[str, str] = {}
        occupancy = self.occupancy_entities()
        for area_id, room in config["rooms"].items():
            for t in room["thermostats"]:
                out[t["entity_id"]] = "thermostat"
            for ref in [*([room["primary_sensor"]] if room["primary_sensor"] else []), *room["secondary_sensors"]]:
                out[ref["entity_id"]] = "room_temp"
            for ref in room["openings"]:
                out[ref["entity_id"]] = "opening"
            if area_id in occupancy:
                out[occupancy[area_id]] = "occupancy"
            for cover in self._covers(area_id):
                out.setdefault(cover, "cover")
            for window in self.learning.windows(area_id):
                for sensor in window.sun_sensors:
                    out.setdefault(sensor, "illuminance")
        if config.get("outdoor"):
            out[config["outdoor"]["entity_id"]] = "outdoor"
        if "sun.sun" in self.ha.states:
            out["sun.sun"] = "sun"
        return out

    def _retrack(self) -> None:
        before = set(self._tracked)
        self._tracked = self.tracked()
        added = set(self._tracked) - before
        if added and self.ha.connected and self._live_since is not None:
            self._start_observing(added, time.time())

    def _history_available(self) -> bool:
        return "history" in set((self.ha.config or {}).get("components") or [])

    def _can_backfill(self) -> bool:
        return bool(self.config["observation"]["backfill_days"]) and self._history_available()

    def _start_observing(self, entity_ids: set[str], now: float) -> None:
        """Entities observed from now on (start-up, a reconnect, a newly configured entity).

        The time since an entity's last observation is recorded as a gap at once; with the Recorder that time is
        queued to be read from its history, and the gap is removed only for the windows the Recorder answered.
        The current value is remembered (with the Recorder) or stored now (without it), so an unchanged report is
        not stored twice.
        """
        backfill = self._can_backfill()
        days = self.config["observation"]["backfill_days"]
        earliest = now - days * 86400.0
        for entity_id in entity_ids:
            self._since[entity_id] = now
            span = self.store.coverage.get(entity_id)
            if span is None:
                if backfill and now - earliest > MIN_GAP:
                    self._pending[entity_id] = (earliest, now, True)
            elif now - span["to"] > MIN_GAP:
                self.store.add_gap(entity_id, span["to"], now)
                if backfill and now - max(span["to"], earliest) > MIN_GAP:
                    self._pending[entity_id] = (max(span["to"], earliest), now, False)
            state = self.ha.states.get(entity_id)
            if backfill:
                rec = sample(self._tracked[entity_id], entity_id, state, ha_unit=self.ha_unit, src="live") \
                    if state is not None else None
                if rec is not None:  # always the current value: a reconnect must not compare with an old one
                    self._last[entity_id] = (signature(rec), rec["t"], time.monotonic())
            else:
                self._record(entity_id, state, src="live")
        if backfill:
            self._want_backfill()  # with nothing pending it only reports "done"

    def _record(self, entity_id: str, state: dict[str, Any] | None, *, src: str, t: float | None = None) -> None:
        kind = self._tracked.get(entity_id)
        if kind is None or state is None:
            return
        rec = sample(kind, entity_id, state, ha_unit=self.ha_unit, src=src, t=t)
        if rec is None:
            return
        sig = signature(rec)
        mono = time.monotonic()
        last = self._last.get(entity_id)
        report_after = self.config["observation"]["report_minutes"] * 60.0
        if last is not None:
            same = last[0] == sig
            if same and (src != "report" or mono - last[2] < report_after):
                return
            if same:
                rec["t"] = round(time.time(), 3)
            elif kind in MIN_INTERVAL and rec["t"] - last[1] < MIN_INTERVAL[kind] and last[0][0] == sig[0]:
                return
            if not same and src == "report":
                rec["src"] = "live"
        elif src == "report":
            rec["src"] = "live"
        self._last[entity_id] = (sig, rec["t"], mono)
        self.store.append(rec)

    def _mark_live(self, now: float) -> None:
        if self._live_since is None or not self.ha.connected:
            return
        for entity_id in self._tracked:
            self.store.mark(entity_id, self._since.get(entity_id, self._live_since), now)

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            entity_id, state = payload
            if entity_id in self._tracked:
                self._record(entity_id, state, src="live")
                self._states_changed()
            elif entity_id == "sun.sun" and self.config["rooms"] and "sun.sun" not in self._tracked:
                self._retrack()
        elif kind == "reported":
            entity_id, state = payload
            if entity_id in self._tracked:
                self._record(entity_id, state, src="report")
        elif kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._mark_live(time.time())
                self._live_since = None
        elif kind == "registries":
            self._spawn(self.follow_renames())

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    def _states_changed(self) -> None:
        if self._states_handle is None:
            self._states_handle = asyncio.get_running_loop().call_later(1.0, self._emit_states)

    def _emit_states(self) -> None:
        self._states_handle = None
        self._emit("states")

    def _connected(self) -> None:
        if self._live_since is not None:
            return
        now = time.time()
        self._live_since = now
        self._tracked = self.tracked()
        self._start_observing(set(self._tracked), now)

    # ------------------------------------------------------------- backfill
    def _want_backfill(self) -> None:
        self._backfill_wanted = True
        if self._backfill_task is None or self._backfill_task.done():
            self._backfill_task = asyncio.get_running_loop().create_task(self._backfill_loop(), name="climate-backfill")

    async def _backfill_loop(self) -> None:
        while self._backfill_wanted:
            self._backfill_wanted = False
            try:
                await self._backfill_once()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.backfill.update(state="error", error=f"{type(err).__name__}: {err}")
                _LOGGER.warning("Climate history backfill failed: %s", err)

    async def _backfill_once(self) -> None:
        if not self.ha.connected or not self._tracked or not self._pending:
            if self.backfill["state"] in ("idle", "running"):
                self.backfill.update(state="done", done_at=utcnow_iso())
            return
        if not self._history_available():
            self.backfill.update(state="unavailable", error="The Recorder history is not available in this Home "
                                 "Assistant", done_at=utcnow_iso())
            self._pending.clear()
            return
        pending, self._pending = {e: g for e, g in self._pending.items() if e in self._tracked}, {}
        self.backfill.update(state="running", error=None, windows=0, records=0, entities=len(pending))
        groups: dict[tuple[float, float], list[str]] = {}
        for entity_id, (start, end, _new) in pending.items():
            groups.setdefault((start, end), []).append(entity_id)
        new = {e for e, g in pending.items() if g[2]}
        try:
            for (start, end), ids in groups.items():
                for i in range(0, len(ids), BACKFILL_BATCH):
                    await self._backfill_window(ids[i:i + BACKFILL_BATCH], start, end, new)
        except (ConnectionError, TimeoutError) as err:
            # The connection went away: what was not read stays a recorded gap; the reconnect queues it again.
            self.backfill.update(state="error", error=f"Interrupted ({type(err).__name__}); the missing time stays "
                                 "a gap until it is read")
            await asyncio.to_thread(self._flush_all)
            return
        await asyncio.to_thread(self._flush_all)
        self.backfill.update(state="done", done_at=utcnow_iso())
        _LOGGER.info("Climate history read from the Recorder: %d records for %d entities in %d windows",
                     self.backfill["records"], len(pending), self.backfill["windows"])
        self._emit("states")

    async def _backfill_window(self, ids: list[str], start: float, end: float, new: set[str]) -> None:
        seen: set[str] = set()
        t = float(math.floor(start))  # whole seconds: the window's start-time rows are matched exactly
        end = float(math.floor(end))
        while t < end:
            chunk_end = min(end, t + BACKFILL_CHUNK)
            try:
                raw = await self.ha.command(
                    "history/history_during_period", start_time=_iso(t), end_time=_iso(chunk_end), entity_ids=ids,
                    include_start_time_state=True, significant_changes_only=False, minimal_response=False,
                    no_attributes=False)
            except HomeAssistantError as err:
                # Not answered: an observed entity keeps its recorded gap for this window.
                self.backfill.update(error=f"The Recorder did not answer ({err.code})")
                t = chunk_end
                continue
            self.backfill["windows"] += 1
            data = raw if isinstance(raw, dict) else {}
            for entity_id in ids:
                kind = self._tracked.get(entity_id)
                rows = data.get(entity_id)
                if kind is None:
                    continue
                records = history_rows(kind, entity_id, rows, ha_unit=self.ha_unit, after=t, before=chunk_end) \
                    if rows else []
                if not records:
                    if entity_id in seen:
                        self.store.mark(entity_id, t, chunk_end)  # known, and unchanged through this window
                        self.store.fill_gap(entity_id, t, chunk_end)
                    # Otherwise: an observed entity keeps its gap (the Recorder no longer has this window); for a
                    # newly tracked entity nothing in the Recorder yet means its observation has not begun.
                    continue
                begins = t
                if entity_id in new and entity_id not in seen and records[0]["t"] > t + 1:
                    begins = records[0]["t"]  # the Recorder knows it only from here: observation starts here
                if records[0]["t"] <= t + 0.001 and (entity_id in seen or entity_id not in new):
                    # The Recorder's value at the window's start: already stored (observed live before the gap,
                    # or the last record of the previous window).
                    records = records[1:]
                for rec in records:
                    self.store.append(rec)
                self.backfill["records"] += len(records)
                seen.add(entity_id)
                self.store.mark(entity_id, begins, chunk_end)
                self.store.fill_gap(entity_id, begins, chunk_end)
            t = chunk_end
            await asyncio.to_thread(self.store.flush)
            await asyncio.sleep(BACKFILL_PAUSE)

    # --------------------------------------------------------------- renames
    def _apply_rename(self, old: str, new: str) -> None:
        self.store.rename(old, new)
        if old in self._last:
            self._last[new] = self._last.pop(old)

    async def rename(self, old: str, new: str) -> bool:
        """An entity id was renamed in Home Assistant (relayed by the companion): follow it."""
        async with self._lock:
            config = json.loads(json.dumps(self.config))
            if not cm.rename(config, old, new):
                return False
            try:
                config = cm.validate(config)
            except cm.ClimateConfigError as err:
                # For example the new id is already used elsewhere in the configuration: nothing is changed.
                text = f"{old} was renamed to {new} in Home Assistant, but the climate configuration cannot follow " \
                       f"automatically ({err.errors[0]['message']}). Check Climate → Settings."
                self.notices = [n for n in self.notices if n.get("message") != text] + [
                    {"code": "rename_conflict", "message": text}]
                _LOGGER.warning("%s", text)
                self._emit("config")
                return False
            self._enrich(config)
            self._apply_rename(old, new)
            await self._commit(config, self._write_backup(self.config, f"Before following a rename ({old} → {new})",
                                                          force=True), reason="rename")
        _LOGGER.info("Climate configuration follows the rename %s → %s", old, new)
        return True

    async def follow_renames(self) -> list[tuple[str, str]]:
        """References whose registry id now belongs to another entity id: renamed in Home Assistant."""
        registry = self.rooms.index().registry
        by_uid = {reg.get("id"): entity_id for entity_id, reg in registry.items() if isinstance(reg.get("id"), str)}
        renames = []
        for ref in cm.refs(self.config):
            uid = ref.get("uid")
            new = by_uid.get(uid) if uid else None
            if new and new != ref["entity_id"] and ref["entity_id"] not in registry:
                renames.append((ref["entity_id"], new))
        for old, new in renames:
            await self.rename(old, new)
        self._retrack()  # an occupancy entity may have appeared or gone
        return renames

    # ------------------------------------------------------------- config IO
    def status(self) -> dict[str, Any]:
        return {"writable": self.read_only is None, "read_only": self.read_only, "read_only_code": self.read_only_code,
                "schema_version": cm.SCHEMA_VERSION, "revision": self.revision, "saved_at": self.saved_at,
                "notices": list(self.notices), "mode": self.config["mode"], "rooms": len(self.config["rooms"]),
                "zones": len(self.config["zones"]), "observation": {**self.store.stats(), "tracked": len(self._tracked),
                                                                    "backfill": dict(self.backfill)},
                "learning": {**self.learning.models.stats(), "running": self.learning._running,
                             "notices": list(self.learning.models.notices)}}

    def document(self) -> dict[str, Any]:
        return {"config": self.config, "revision": self.revision, "status": self.status(),
                "unit": cm.display_unit(self.config, self.ha_unit), "ha_unit": self.ha_unit}

    def _store(self, config: dict[str, Any]) -> None:
        _atomic_write(self.config_path, json.dumps(config, ensure_ascii=False, indent=1).encode())

    def _check_writable(self, expected: Any) -> None:
        if self.read_only is not None:
            raise ClimateError(self.read_only_code or "read_only", self.read_only, 409)
        if not isinstance(expected, str) or not cm.REVISION.match(expected):
            raise ClimateError("invalid_revision", "A revision is required to save")
        if expected != self.revision:
            raise ClimateError("stale_revision", "The climate configuration was changed in another session after you "
                               "opened it. Reload to see the current version.", 409, revision=self.revision)

    def _clean(self, raw: Any) -> dict[str, Any]:
        try:
            config = cm.validate(raw)
        except cm.ClimateConfigError as err:
            raise ClimateError("invalid_config", "The climate configuration is not valid", 400,
                               errors=err.errors) from err
        self._enrich(config)
        return config

    def _enrich(self, config: dict[str, Any]) -> None:
        """Registry ids and thermostat capability snapshots come from Home Assistant, never from the browser or a
        file: an id Home Assistant does not know is dropped; a device without a state keeps the snapshot this App
        stored for it earlier, if any."""
        idx = self.rooms.index()
        stored = {t["entity_id"]: t.get("capabilities") for room in self.config["rooms"].values()
                  for t in room["thermostats"]}
        for ref in cm.refs(config):
            reg = idx.registry.get(ref["entity_id"])
            ref["uid"] = reg["id"] if reg is not None and isinstance(reg.get("id"), str) else None
        for room in config["rooms"].values():
            for t in room["thermostats"]:
                state = self.ha.states.get(t["entity_id"])
                t["capabilities"] = cm.clean_capabilities(self._capabilities(t["entity_id"], state) or {}) \
                    if state is not None else stored.get(t["entity_id"])

    def _capabilities(self, entity_id: str, state: dict[str, Any] | None) -> dict[str, Any] | None:
        idx = self.rooms.index()
        reg = idx.registry.get(entity_id) or {}
        device_entities = idx.device_entities.get(reg.get("device_id") or "", [])
        return cm.thermostat_capabilities(state, self.ha_unit,
                                          external_input=cm.external_input_of(device_entities, idx.registry))

    def warnings(self, config: dict[str, Any]) -> list[dict[str, str]]:
        """Problems that do not stop a save: unknown entities, other areas, unusual choices."""
        out: list[dict[str, str]] = []
        idx = self.rooms.index()
        states = self.ha.states

        def add(path: str, text: str, code: str = "warning") -> None:
            out.append({"path": path, "message": text, "code": code})

        for area_id, room in config["rooms"].items():
            name = (idx.areas.get(area_id) or {}).get("name") or area_id
            if area_id not in idx.areas:
                add(f"rooms.{area_id}", f"{area_id} is not a Home Assistant area (any more)", "unknown_area")
            if room["primary_sensor"] is None:
                add(f"rooms.{area_id}.primary_sensor", f"{name} has no primary temperature sensor: its temperature is "
                    "not measured (a thermostat's own reading is not used as the room temperature)", "no_primary")
            for ref in cm.refs({"rooms": {area_id: room}}):
                entity_id = ref["entity_id"]
                if entity_id not in states and entity_id not in idx.registry:
                    add(f"rooms.{area_id}", f"{entity_id} does not exist in Home Assistant", "unknown_entity")
                    continue
                reg = idx.registry.get(entity_id) or {}
                device = idx.devices.get(reg.get("device_id") or "") or {}
                where = reg.get("area_id") or device.get("area_id")
                if where and where != area_id and not entity_id.startswith("climate."):
                    other = (idx.areas.get(where) or {}).get("name") or where
                    add(f"rooms.{area_id}", f"{entity_id} is in the area {other} in Home Assistant; it is used for "
                        f"{name} as you chose", "other_area")
            primary = (room["primary_sensor"] or {}).get("entity_id")
            if primary:
                reg = idx.registry.get(primary) or {}
                thermostat_devices = {(idx.registry.get(t["entity_id"]) or {}).get("device_id")
                                      for t in room["thermostats"]} - {None}
                if reg.get("device_id") and reg.get("device_id") in thermostat_devices:
                    add(f"rooms.{area_id}.primary_sensor", f"{primary} belongs to a heating device: it measures at the "
                        "radiator, not the room", "sensor_on_thermostat")
                if primary in states and not cm.is_temperature_sensor(primary, states.get(primary), reg):
                    add(f"rooms.{area_id}.primary_sensor", f"{primary} does not report a temperature unit",
                        "not_temperature")
        if config["outdoor"] is None and config["rooms"]:
            add("outdoor", "No outdoor temperature source: indoor/outdoor differences are not shown and A7.1 cannot "
                "learn the outdoor influence", "no_outdoor")
        for zone in config["zones"]:
            if len(zone["rooms"]) > 1 and zone["layout"] == "connected" and not zone["shared_thermostats"]:
                add(f"zones.{zone['id']}", f"{zone['name']}: connected rooms without a shared heating device; each "
                    "room keeps its own readings and devices", "independent")
        return out

    async def review(self, raw: Any, expected: Any) -> dict[str, Any]:
        """Validate a draft and say what saving it would change (nothing is stored)."""
        try:
            config = cm.validate(raw)
        except cm.ClimateConfigError as err:
            return {"valid": False, "errors": err.errors, "warnings": [], "changes": [], "revision": self.revision,
                    "stale": expected != self.revision, "writable": self.read_only is None}
        self._enrich(config)
        changes = cm.changes(self.config, config)
        return {"valid": True, "errors": [], "warnings": self.warnings(config), "changes": changes,
                "identical": cm.revision_of(config) == self.revision, "revision": self.revision,
                "stale": expected != self.revision, "writable": self.read_only is None}

    async def save(self, raw: Any, expected: Any) -> dict[str, Any]:
        async with self._lock:
            self._check_writable(expected)
            config = self._clean(raw)
            if cm.revision_of(config) == self.revision:
                return {"saved": False, "revision": self.revision, "saved_at": self.saved_at, "backup": None}
            backup = self._auto_backup()
            result = await self._commit(config, backup, reason="save")
        _LOGGER.info("Climate configuration saved (%d rooms, %d thermal zones)", len(self.config["rooms"]),
                     len(self.config["zones"]))
        return result

    async def _commit(self, config: dict[str, Any], backup: str | None, *, reason: str) -> dict[str, Any]:
        config["updated_at"] = utcnow_iso()
        try:
            await asyncio.to_thread(self._store, config)
        except OSError as err:
            raise ClimateError("storage_error", f"The climate configuration could not be written ({err}). The stored "
                               "version is unchanged.", 500) from err
        self.config, self.revision, self.saved_at = config, cm.revision_of(config), config["updated_at"]
        self.notices = [n for n in self.notices if n["code"] not in ("recovered", "recovered_empty")]
        self._retrack()
        self._emit("config")
        return {"saved": True, "revision": self.revision, "saved_at": self.saved_at, "backup": backup,
                "reason": reason}

    # --------------------------------------------------------------- backups
    def _auto_backup(self) -> str | None:
        if not self.config["rooms"] and not self.config["zones"] and self.config["outdoor"] is None:
            return None
        now = time.monotonic()
        if self._last_auto_backup and now - self._last_auto_backup < BACKUP_COALESCE:
            return None
        self._last_auto_backup = now
        return self._write_backup(self.config, "Before saving")

    def _write_backup(self, config: Any, reason: str, *, force: bool = False) -> str | None:
        if self.dir is None or self.read_only_code == "no_storage":
            return None
        if not force and isinstance(config, dict) and not config.get("rooms"):
            return None
        revision = cm.revision_of(config) if isinstance(config, dict) else "0" * 16
        data = {"format": cm.BACKUP_FORMAT, "version": 1, "created_at": utcnow_iso(), "reason": reason,
                "app_version": APP_VERSION, "revision": revision, "config": config}
        name = f"climate-{_stamp()}-{revision}"
        path = self.backup_dir / f"{name}.json"
        n = 1
        while path.exists():
            n += 1
            path = self.backup_dir / f"{name}-{n}.json"
        try:
            _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1).encode())
            for old in sorted(self.backup_dir.glob("climate-*.json"), reverse=True)[BACKUP_KEEP:]:
                old.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not store a climate backup: %s", err)
            return None
        return path.stem

    def backups(self) -> list[dict[str, Any]]:
        if self.dir is None or not self.backup_dir.is_dir():
            return []
        out = []
        for path in sorted(self.backup_dir.glob("climate-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            config = data.get("config") if isinstance(data, dict) else None
            out.append({"id": path.stem, "created_at": data.get("created_at"), "reason": data.get("reason"),
                        "revision": data.get("revision"), "summary": self._summary(config)})
        return out

    @staticmethod
    def _summary(config: Any) -> dict[str, int] | None:
        if not isinstance(config, dict):
            return None
        rooms = config.get("rooms") if isinstance(config.get("rooms"), dict) else {}
        return {"rooms": len(rooms), "zones": len(config.get("zones") or []),
                "thermostats": sum(len(r.get("thermostats") or []) for r in rooms.values() if isinstance(r, dict))}

    def _read_backup(self, backup_id: Any) -> dict[str, Any]:
        if self.dir is None or not isinstance(backup_id, str) or not BACKUP_ID.match(backup_id):
            raise ClimateError("unknown_backup", "Unknown backup", 404)
        try:
            data = json.loads((self.backup_dir / f"{backup_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise ClimateError("unknown_backup", "Unknown backup", 404) from err
        if not isinstance(data, dict) or data.get("format") != cm.BACKUP_FORMAT:
            raise ClimateError("unknown_backup", "Unknown backup", 404)
        return data

    def backup(self, backup_id: str) -> dict[str, Any]:
        return self._read_backup(backup_id)

    # -------------------------------------------------------- export/import
    def export(self) -> dict[str, Any]:
        return {"format": cm.EXPORT_FORMAT, "version": 1, "exported_at": utcnow_iso(), "app_version": APP_VERSION,
                "revision": self.revision, "config": self.config}

    @staticmethod
    def _unpack(doc: Any) -> Any:
        if not isinstance(doc, dict):
            raise ClimateError("invalid_file", "This file is not a Smart Comfort climate configuration")
        if doc.get("format") in (cm.EXPORT_FORMAT, cm.BACKUP_FORMAT):
            return doc.get("config")
        if doc.get("format") == cm.FORMAT:
            return doc
        raise ClimateError("invalid_file", "This file is not a Smart Comfort climate configuration")

    def _incoming(self, doc: Any) -> dict[str, Any]:
        raw = self._unpack(doc)
        try:
            return cm.validate(cm.migrate(raw)[0])
        except cm.ClimateConfigError as err:
            raise ClimateError("invalid_config", f"The configuration in the file is not valid: {err}", 400,
                               errors=err.errors) from err

    async def import_review(self, doc: Any) -> dict[str, Any]:
        config = self._incoming(doc)
        self._enrich(config)
        return {"valid": True, "errors": [], "warnings": self.warnings(config), "changes": cm.changes(self.config, config),
                "identical": cm.revision_of(config) == self.revision, "revision": self.revision,
                "summary": self._summary(config), "current": self._summary(self.config),
                "writable": self.read_only is None}

    async def import_apply(self, doc: Any, expected: Any, *, reason: str = "import") -> dict[str, Any]:
        config = self._incoming(doc)
        async with self._lock:
            self._check_writable(expected)
            self._enrich(config)
            saved = self._write_backup(self.config, "Before restoring a backup" if reason == "restore"
                                       else "Before importing a file", force=True)
            result = await self._commit(config, saved, reason=reason)
        _LOGGER.info("Climate configuration %s (%s)", "restored" if reason == "restore" else "imported",
                     self._summary(config))
        return result

    async def restore_review(self, backup_id: Any) -> dict[str, Any]:
        return await self.import_review(self._read_backup(backup_id))

    async def restore(self, backup_id: Any, expected: Any) -> dict[str, Any]:
        return await self.import_apply(self._read_backup(backup_id), expected, reason="restore")

    # ------------------------------------------------------------- discovery
    def discovery(self) -> dict[str, Any]:
        """Climate entities, temperature sensors, windows/doors and outdoor sources, with suggestions (read-only)."""
        idx = self.rooms.index()
        states = self.ha.states
        names = {a: (v.get("name") or a) for a, v in idx.areas.items()}

        def where(entity_id: str) -> tuple[str | None, dict[str, Any], dict[str, Any]]:
            reg = idx.registry.get(entity_id) or {}
            device = idx.devices.get(reg.get("device_id") or "") or {}
            return reg.get("area_id") or device.get("area_id"), reg, device

        candidates = sorted(set(states) | set(idx.registry))
        thermostat_devices: set[str] = set()
        thermostats = []
        for entity_id in candidates:
            if not entity_id.startswith("climate."):
                continue
            area_id, reg, device = where(entity_id)
            if reg.get("disabled_by"):
                continue
            if reg.get("device_id"):
                thermostat_devices.add(reg["device_id"])
            state = states.get(entity_id)
            view = cm.thermostat_view(entity_id, state, now=time.time(), stale_minutes=1e9, ha_unit=self.ha_unit)
            thermostats.append({
                "entity_id": entity_id, "name": view["name"] or reg.get("name") or reg.get("original_name") or entity_id,
                "area_id": area_id, "area": names.get(area_id) if area_id else None,
                "device": device.get("name_by_user") or device.get("name"),
                "model": " ".join(str(x) for x in (device.get("manufacturer"), device.get("model")) if x) or None,
                "platform": reg.get("platform"), "kind": cm.thermostat_kind(entity_id, reg, device),
                "capabilities": self._capabilities(entity_id, state), "state": view})
        sensors, openings, outdoor = [], [], []
        for entity_id in candidates:
            state = states.get(entity_id)
            area_id, reg, device = where(entity_id)
            if reg.get("disabled_by"):
                continue
            base = {"entity_id": entity_id,
                    "name": ((state or {}).get("attributes") or {}).get("friendly_name") or reg.get("name")
                    or reg.get("original_name") or entity_id,
                    "area_id": area_id, "area": names.get(area_id) if area_id else None,
                    "device": device.get("name_by_user") or device.get("name")}
            if cm.is_temperature_sensor(entity_id, state, reg):
                hints = cm.sensor_hints(entity_id, state, reg, device, thermostat_devices=thermostat_devices)
                r = cm.reading(entity_id, state, now=time.time(), stale_minutes=1e9, ha_unit=self.ha_unit)
                item = {**base, **hints, "value_c": r["value_c"], "quality": r["quality"], "unit": r["unit"]}
                sensors.append(item)
                if hints["outdoor"]:
                    outdoor.append({**item, "source": "sensor"})
            elif entity_id.startswith("weather."):
                r = cm.reading(entity_id, state, now=time.time(), stale_minutes=1e9, ha_unit=self.ha_unit,
                               place="outdoor")
                outdoor.append({**base, "source": "weather", "value_c": r["value_c"], "quality": r["quality"],
                                "unit": r["unit"]})
            elif (kind := cm.opening_kind(entity_id, state, reg)) is not None:
                openings.append({**base, "kind": kind, "status": cm.opening_view(entity_id, state, kind=kind)["status"]})
        return {
            "areas": sorted(({"id": a, "name": n} for a, n in names.items()), key=lambda x: str(x["name"]).casefold()),
            "thermostats": thermostats, "sensors": sensors, "openings": openings, "outdoor": outdoor,
            "suggestions": self.suggestions(thermostats, sensors, openings, outdoor),
            "kinds": cm.THERMOSTAT_KINDS, "layouts": cm.ZONE_LAYOUTS,
            "unit": cm.display_unit(self.config, self.ha_unit), "ha_unit": self.ha_unit, "generated_at": utcnow_iso(),
        }

    def suggestions(self, thermostats: list[dict[str, Any]], sensors: list[dict[str, Any]],
                    openings: list[dict[str, Any]], outdoor: list[dict[str, Any]]) -> dict[str, Any]:
        """Guesses from Home Assistant's areas, device classes and units. Applied only when a person chooses them."""
        rooms: dict[str, dict[str, Any]] = {}
        for t in thermostats:
            if t["area_id"]:
                rooms.setdefault(t["area_id"], {"thermostats": [], "sensors": [], "openings": []})["thermostats"].append(
                    {"entity_id": t["entity_id"], "kind": t["kind"]})
        for s in sensors:
            if s["area_id"] and not s["outdoor"] and not s["on_thermostat"] and not s["not_room"]:
                rooms.setdefault(s["area_id"], {"thermostats": [], "sensors": [], "openings": []})["sensors"].append(
                    s["entity_id"])
        for o in openings:
            if o["area_id"] and o["area_id"] in rooms:
                rooms[o["area_id"]]["openings"].append(o["entity_id"])
        out_rooms = {}
        for area_id, s in rooms.items():
            ok = [e for e in s["sensors"] if next((x for x in sensors if x["entity_id"] == e), {}).get("quality") == "ok"]
            primary = (ok or s["sensors"] or [None])[0]
            out_rooms[area_id] = {"thermostats": s["thermostats"], "primary_sensor": primary,
                                  "secondary_sensors": [e for e in s["sensors"] if e != primary][:cm.LIMITS["secondary_sensors"]],
                                  "openings": s["openings"][:cm.LIMITS["openings"]],
                                  "reason": "From Home Assistant's areas: devices and sensors in this area"}
        zones = []
        configured = set(self.config["rooms"])
        in_zone = {r for z in self.config["zones"] for r in z["rooms"]}
        try:
            spaces = list(self.lighting.controller.store.config.shared_spaces) if self.lighting is not None \
                and self.lighting.controller is not None else []
        except Exception:  # noqa: BLE001 - context only
            spaces = []
        for space in spaces:
            rooms_of = [r for r in space.rooms]
            if len(rooms_of) > 1:
                zones.append({"origin": "open_plan", "name": space.name, "rooms": rooms_of, "layout": "open_plan",
                              "reason": "A shared space in Lighting: these rooms are one open space for lights. Heat "
                                        "moves freely between them, but confirm before grouping them.",
                              "configured": all(r in configured for r in rooms_of),
                              "grouped": any(r in in_zone for r in rooms_of)})
        model_rooms = (self.rooms.model or {}).get("rooms") or {}
        pairs = set()
        for area_id, room in model_rooms.items():
            for other in (room.get("config") or {}).get("adjacent_rooms") or []:
                pair = tuple(sorted((area_id, other)))
                if pair[0] != pair[1] and pair not in pairs:
                    pairs.add(pair)
        for a, b in sorted(pairs):
            if any(set((a, b)) <= set(z["rooms"]) for z in zones):
                continue
            zones.append({"origin": "adjacent", "name": None, "rooms": [a, b], "layout": "connected",
                          "reason": "Adjacent rooms in Presence (a door or opening between them). Adjacent is not the "
                                    "same as thermally connected: group them only if they share heating or are open "
                                    "to each other.",
                          "configured": a in configured and b in configured,
                          "grouped": a in in_zone or b in in_zone})
        best_outdoor = next((o["entity_id"] for o in outdoor if o["source"] == "sensor" and o["quality"] == "ok"), None) \
            or next((o["entity_id"] for o in outdoor if o["source"] == "weather"), None)
        return {"rooms": out_rooms, "zones": zones, "outdoor": best_outdoor}

    # ----------------------------------------------------------------- views
    def _occupancy(self, area_id: str, entity: str | None) -> dict[str, Any] | None:
        occ = self.occupancy
        try:
            result = occ.result(area_id) if occ is not None else None
        except Exception:  # noqa: BLE001
            result = None
        if result is not None:
            return {"occupied": result.occupied, "state": getattr(result.state, "value", str(result.state)),
                    "source": "engine"}
        state = self.ha.states.get(entity) if entity else None
        if state is None:
            return None
        raw = state.get("state")
        return {"occupied": True if raw == "on" else False if raw == "off" else None, "state": raw, "source": "entity"}

    def room_view(self, area_id: str, *, now: float | None = None) -> dict[str, Any]:
        now = time.time() if now is None else now
        room = self.config["rooms"].get(area_id)
        if room is None:
            raise ClimateError("not_configured", "This room is not configured for climate", 404)
        idx = self.rooms.index()
        states = self.ha.states
        targets = cm.effective_targets(self.config, area_id)
        stale = targets["stale_minutes"]
        primary = cm.reading((room["primary_sensor"] or {}).get("entity_id"),
                             states.get((room["primary_sensor"] or {}).get("entity_id") or ""), now=now,
                             stale_minutes=stale, ha_unit=self.ha_unit)
        secondary = [cm.reading(s["entity_id"], states.get(s["entity_id"]), now=now, stale_minutes=stale,
                                ha_unit=self.ha_unit) for s in room["secondary_sensors"]]
        thermostats = []
        for t in room["thermostats"]:
            state = states.get(t["entity_id"])
            view = cm.thermostat_view(t["entity_id"], state, now=now, stale_minutes=stale, ha_unit=self.ha_unit)
            live = self._capabilities(t["entity_id"], state) if state is not None else None
            stored = t.get("capabilities")
            changed = bool(live and stored and cm.clean_capabilities(live) != stored)
            thermostats.append({**view, "kind": t["kind"], "kind_label": cm.THERMOSTAT_KINDS[t["kind"]],
                                "capabilities": live or stored, "capabilities_changed": changed,
                                "shared": any(t["entity_id"] in z["shared_thermostats"] for z in self.config["zones"])})
        openings = []
        for o in room["openings"]:
            state = states.get(o["entity_id"])
            reg = idx.registry.get(o["entity_id"])
            openings.append(cm.opening_view(o["entity_id"], state, kind=cm.opening_kind(o["entity_id"], state, reg),
                                            configured=o.get("kind")))
        temperature = cm.room_temperature(primary, secondary)
        outdoor_ref = self.config.get("outdoor")
        outdoor = cm.reading(outdoor_ref["entity_id"] if outdoor_ref else None,
                             states.get(outdoor_ref["entity_id"]) if outdoor_ref else None, now=now,
                             stale_minutes=max(stale, 120), ha_unit=self.ha_unit, place="outdoor")
        diff = round(temperature["value_c"] - outdoor["value_c"], 2) \
            if temperature["value_c"] is not None and outdoor["quality"] == "ok" else None
        view = {"primary": primary, "secondary": secondary, "thermostats": thermostats}
        zone = cm.zone_of(self.config, area_id)
        occupancy_entity = self.occupancy_entities().get(area_id)
        heating_states = [t["heating"] for t in thermostats]
        return {
            "area_id": area_id, "name": (idx.areas.get(area_id) or {}).get("name") or area_id,
            "area_exists": area_id in idx.areas,
            "temperature": temperature, "primary": primary, "secondary": secondary, "thermostats": thermostats,
            "openings": openings, "any_open": any(o["status"] == "open" for o in openings),
            "heating": {"reported": sum(1 for h in heating_states if h["state"] == "heating"),
                        "not_reported": sum(1 for h in heating_states if h["basis"] != "reported"),
                        "devices": len(heating_states)},
            "demand": cm.demand_hint(temperature["value_c"], thermostats),
            "outdoor_difference_c": diff,
            "occupancy": self._occupancy(area_id, occupancy_entity), "occupancy_entity": occupancy_entity,
            "zone": {"id": zone["id"], "name": zone["name"], "layout": zone["layout"]} if zone else None,
            "targets": targets, "completeness": cm.completeness(room, view, outdoor=outdoor_ref is not None),
            "covers": self._covers(area_id),
        }

    def overview(self) -> dict[str, Any]:
        now = time.time()
        rooms = []
        problems = []
        for area_id in self.config["rooms"]:
            view = self.room_view(area_id, now=now)
            rooms.append(view)
            readings = [("Primary sensor", view["primary"]), *(("Secondary sensor", s) for s in view["secondary"]),
                        *(("Heating device", t["measured"]) for t in view["thermostats"] if not t["available"])]
            for role, r in readings:
                if r["entity_id"] and r["quality"] not in ("ok", "none"):
                    problems.append({"area_id": area_id, "room": view["name"], "role": role, "entity_id": r["entity_id"],
                                     "quality": r["quality"], "text": QUALITY_TEXT.get(r["quality"], r["quality"])})
            for o in view["openings"]:
                if o["status"] in ("unavailable", "missing"):
                    problems.append({"area_id": area_id, "room": view["name"],
                                     "role": OPENING_ROLE.get(o["kind"] or "", "Window or door (type not set)"),
                                     "entity_id": o["entity_id"], "quality": o["status"],
                                     "text": QUALITY_TEXT.get(o["status"], o["status"])})
        rooms.sort(key=lambda r: str(r["name"]).casefold())
        for r in rooms:
            r["learning"] = self.learning.summary(r["area_id"])
        outdoor_ref = self.config.get("outdoor")
        outdoor = cm.reading(outdoor_ref["entity_id"] if outdoor_ref else None,
                             self.ha.states.get(outdoor_ref["entity_id"]) if outdoor_ref else None, now=now,
                             stale_minutes=120, ha_unit=self.ha_unit, place="outdoor")
        if outdoor_ref and outdoor["quality"] != "ok":
            problems.append({"area_id": None, "room": None, "role": "Outdoor temperature",
                             "entity_id": outdoor_ref["entity_id"], "quality": outdoor["quality"],
                             "text": QUALITY_TEXT.get(outdoor["quality"], outdoor["quality"])})
        history_days = self.history_days()
        return {
            "mode": self.config["mode"], "unit": cm.display_unit(self.config, self.ha_unit), "ha_unit": self.ha_unit,
            "rooms": rooms, "outdoor": outdoor, "problems": problems,
            "zones": [{**z, "names": [next((r["name"] for r in rooms if r["area_id"] == a), a) for a in z["rooms"]]}
                      for z in self.config["zones"]],
            "completeness": {"rooms": len(rooms), "ready": sum(1 for r in rooms if r["completeness"]["ready"]),
                             "score": round(sum(r["completeness"]["score"] for r in rooms) / len(rooms)) if rooms else 0},
            "learning": {"history_days": history_days, "min_history_days": self.config["prediction"]["min_history_days"],
                         "available": True, **self.learning.overview(),
                         "text": "Smart Comfort learns how each room warms and cools from these observations. "
                                 "It only observes: no thermostat is changed."},
            "status": self.status(), "generated_at": utcnow_iso(),
        }

    def history_days(self) -> float:
        """How many days the App has observed the configured primary sensors (the shortest of them)."""
        spans = [self.store.coverage.get(r["primary_sensor"]["entity_id"]) for r in self.config["rooms"].values()
                 if r["primary_sensor"]]
        spans = [s for s in spans if s]
        if not spans:
            return 0.0
        return round(min(s["to"] - s["from"] for s in spans) / 86400.0, 1)

    async def history(self, area_id: str, window: Any) -> dict[str, Any]:
        """A room's observed measurements for a bounded window, shaped for a chart (from the App's own store)."""
        if area_id not in self.config["rooms"]:
            raise ClimateError("not_configured", "This room is not configured for climate", 404)
        try:
            seconds = int(window)
        except (TypeError, ValueError):
            seconds = WINDOWS[1]
        if seconds not in WINDOWS:
            raise ClimateError("invalid_window", f"The window must be one of {', '.join(map(str, WINDOWS))} seconds")
        room = self.config["rooms"][area_id]
        end = time.time()
        start = end - seconds
        occupancy = self.occupancy_entities().get(area_id)
        outdoor = (self.config.get("outdoor") or {}).get("entity_id")
        ids = {t["entity_id"] for t in room["thermostats"]} | {s["entity_id"] for s in room["secondary_sensors"]} \
            | {o["entity_id"] for o in room["openings"]}
        primary = (room["primary_sensor"] or {}).get("entity_id")
        for extra in (primary, occupancy, outdoor):
            if extra:
                ids.add(extra)
        data = await asyncio.to_thread(self.store.read, ids, start, end)
        gaps = {e: [g for g in self.store.gaps.get(e, []) if g[1] > start] for e in ids}
        names = {e: ((self.ha.states.get(e) or {}).get("attributes") or {}).get("friendly_name") or e for e in ids}
        series = []
        if primary:
            series.append({"key": "primary", "label": f"Room ({names[primary]})", "entity_id": primary, "kind": "room",
                           "points": numeric_series(data[primary], start=start, end=end, gaps=gaps.get(primary))})
        for s in room["secondary_sensors"]:
            series.append({"key": f"secondary:{s['entity_id']}", "label": names[s["entity_id"]], "kind": "secondary",
                           "entity_id": s["entity_id"],
                           "points": numeric_series(data[s["entity_id"]], start=start, end=end,
                                                     gaps=gaps.get(s["entity_id"]))})
        for t in room["thermostats"]:
            e = t["entity_id"]
            series.append({"key": f"measured:{e}", "label": f"{names[e]}: its own reading", "kind": "thermostat",
                           "entity_id": e, "points": numeric_series(data[e], start=start, end=end, field="cur",
                                                                    gaps=gaps.get(e))})
            series.append({"key": f"setpoint:{e}", "label": f"{names[e]}: setpoint", "kind": "setpoint",
                           "entity_id": e, "points": numeric_series(data[e], start=start, end=end, field="tgt",
                                                                    step=True, gaps=gaps.get(e))})
        if outdoor:
            series.append({"key": "outdoor", "label": f"Outdoor ({names[outdoor]})", "kind": "outdoor",
                           "entity_id": outdoor, "points": numeric_series(data[outdoor], start=start, end=end,
                                                             gaps=gaps.get(outdoor))})
        bands = []
        for t in room["thermostats"]:
            e = t["entity_id"]
            bands.append({"key": f"heating:{e}", "label": f"{names[e]} reports heating", "kind": "heating",
                          "entity_id": e, "intervals": intervals(
                              data[e], start=start, end=end,
                              active=lambda r: (r.get("a") or {}).get("action") in cm.HEATING_ACTIONS, gaps=gaps.get(e)),
                          "reported": any((r.get("a") or {}).get("action") is not None for r in data[e])})
        idx = self.rooms.index()
        for o in room["openings"]:
            e = o["entity_id"]
            what = cm.opening_view(e, self.ha.states.get(e), kind=cm.opening_kind(e, self.ha.states.get(e),
                                                                                  idx.registry.get(e)),
                                   configured=o.get("kind"))["kind"]
            label = f"{OPENING_ROLE.get(what or '', 'Window or door (type not set)')} open: {names[e]}"
            bands.append({"key": f"open:{e}", "label": label, "kind": "open", "entity_id": e, "opening": what,
                          "intervals": intervals(data[e], start=start, end=end, active=lambda r: r.get("v") == 1,
                                                gaps=gaps.get(e))})
        if occupancy:
            bands.append({"key": "occupied", "label": "Occupied", "kind": "occupied", "entity_id": occupancy,
                          "intervals": intervals(data[occupancy], start=start, end=end,
                                                   active=lambda r: r.get("v") == 1, gaps=gaps.get(occupancy))})
        coverage = {e: self.store.coverage.get(e) for e in ids}
        observed_from = min((c["from"] for c in coverage.values() if c), default=None)
        return {
            "area_id": area_id, "window": seconds, "windows": list(WINDOWS), "start": start, "end": end,
            "series": series, "bands": bands, "unit": cm.display_unit(self.config, self.ha_unit),
            "observed_from": observed_from, "gaps": {e: g for e, g in gaps.items() if g},
            "records": sum(len(v) for v in data.values()), "backfill": dict(self.backfill),
            "source": "The App's observations (live state changes, and the Recorder's history for the time before "
                      "the App observed them)",
            "time_zone": (self.ha.config or {}).get("time_zone"),
        }


__all__ = ["ClimateError", "ClimateService", "KINDS", "WINDOWS"]
