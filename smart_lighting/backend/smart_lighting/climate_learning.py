"""Climate thermal learning (Phase A7.1): the App's thermal-learning service.

Owns the learned models (``climate_model_store``), decides when a room is trained, runs one bounded fit at a
time in a worker thread (never on the event loop), and explains each room's learning status. It reads the
A7.0 observation store and the climate configuration and writes only ``/data/climate/models``: never the
configuration, never the observations, never a device.

When a room is trained
----------------------
* on an administrator's request (Climate → room → Thermal learning → Rebuild);
* automatically, at most every ``RETRAIN_S`` per room, once at least ``NEW_DATA_S`` of new observations exist
  since the last fit (a room with no model yet: once it has a day of observations, retried every
  ``RETRY_S``);
* after a configuration change that invalidates its model (a different sensor, thermostat, zone or outdoor
  source).

Invalidation
------------
Each model stores the fingerprint of what it was trained on (registry ids, so a renamed entity is still the
same entity). A *hard* change (room sensors, heating devices, thermal zone, outdoor source) invalidates the
model: it is shown with the reason and is no longer offered to A7.2. A *soft* change (windows and doors,
freshness limit, window geometry, minimum history) marks it outdated: still usable, rebuild suggested.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import copy
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import Any

from . import climate_model as cm
from . import climate_thermal_model as tm
from .climate_fit import Learned, learn
from .climate_model_store import ModelStore
from .climate_prep import GRID_S, RoomSpec, StoreView, WindowSpec, prepare_room, read_events
from .const import APP_VERSION
from .ha import utcnow_iso

_LOGGER = logging.getLogger(__name__)

TRAIN_DAYS = 60
CHECK_S = 1800.0
FIRST_CHECK_S = 120.0
RETRAIN_S = 24 * 3600.0
NEW_DATA_S = 12 * 3600.0
RETRY_S = 6 * 3600.0
MIN_COVERAGE_S = 86400.0
EXPORT_FORMAT = "smart_comfort.thermal_model_export"

STATUS_TEXT = {
    "no_data": "No data",
    "collecting": "Collecting observations",
    "insufficient": "Insufficient usable history",
    "learning": "Learning",
    "low": "Model available, low confidence",
    "moderate": "Model available, moderate confidence",
    "high": "Model available, high confidence",
    "unidentifiable": "Unable to identify a reliable model",
    "error": "Error",
    "invalidated": "Model invalidated (rebuild needed)",
}
HARD_TEXT = {
    "primary": "the primary temperature sensor changed",
    "secondary": "the secondary temperature sensors changed",
    "thermostats": "the heating devices changed",
    "zone": "the thermal zone changed",
    "neighbours": "the sensors of the zone's other rooms changed",
    "outdoor": "the outdoor temperature source changed",
}
SOFT_TEXT = {
    "openings": "the windows and doors changed",
    "stale_minutes": "the sensor freshness limit changed",
    "windows": "the curtain windows (direction, covers, sun sensors) changed",
    "min_history_days": "the minimum history changed",
}


def _ref(r: dict[str, Any] | None) -> str | None:
    if not r:
        return None
    return f"uid:{r['uid']}" if r.get("uid") else r["entity_id"]


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def fingerprint(config: dict[str, Any], area_id: str, windows: list[WindowSpec]) -> dict[str, Any]:
    """What a room's model depends on: ``hard`` (invalidates it) and ``soft`` (makes it outdated)."""
    room = config["rooms"][area_id]
    zone = cm.zone_of(config, area_id)
    by_entity = {t["entity_id"]: _ref(t) for r in config["rooms"].values() for t in r["thermostats"]}
    neighbours = {}
    if zone:
        for other in zone["rooms"]:
            if other != area_id and other in config["rooms"]:
                o = config["rooms"][other]
                neighbours[other] = {"primary": _ref(o["primary_sensor"]),
                                     "secondary": sorted(_ref(s) for s in o["secondary_sensors"])}
    hard = {
        "primary": _ref(room["primary_sensor"]),
        "secondary": sorted(_ref(s) for s in room["secondary_sensors"]),
        "thermostats": sorted(_ref(t) for t in room["thermostats"]),
        "zone": None if zone is None else {"id": zone["id"], "layout": zone["layout"], "rooms": sorted(zone["rooms"]),
                                           "shared": sorted(by_entity.get(s) or s for s in zone["shared_thermostats"])},
        "neighbours": neighbours,
        "outdoor": _ref(config.get("outdoor")),
    }
    soft = {
        "openings": sorted(_ref(o) for o in room["openings"]),
        "stale_minutes": cm.effective_targets(config, area_id)["stale_minutes"],
        "windows": sorted(json.dumps([w.bearing, w.exposure_width, w.min_elevation, w.max_elevation,
                                      w.obstructions, w.layers, w.sun_sensors]) for w in windows),
        "min_history_days": config["prediction"]["min_history_days"],
    }
    return {"hard": hard, "soft": soft}


def changed(before: dict[str, Any] | None, after: dict[str, Any], part: str) -> list[str]:
    if not before:
        return list((HARD_TEXT if part == "hard" else SOFT_TEXT))
    b, a = before.get(part) or {}, after.get(part) or {}
    return [k for k in a if b.get(k) != a.get(k)]


@dataclass
class Job:
    area_id: str
    reason: str
    requested_by: str | None = None


class ThermalLearning:
    """Smart Comfort's thermal learning (observation only)."""

    def __init__(self, climate: Any, data_dir: Path | None) -> None:
        self.climate = climate
        self.models = ModelStore(data_dir / "climate" / "models" if data_dir else None)
        self._queue: dict[str, Job] = {}
        self._running: str | None = None
        self._task: asyncio.Task[Any] | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._wake = asyncio.Event()
        self._listeners: list[Callable[[str], None]] = []
        self._stopping = False
        self.last_check: float | None = None
        self.trainings = 0
        # Bumped by a reset or a removal: a fit that started before it is discarded.
        self._generation: dict[str, int] = {}

    # -------------------------------------------------------------- lifecycle
    def load(self) -> None:
        self.models.load()

    async def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="climate-learning")
        self.climate.add_listener(self._on_climate)

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._task, self._worker):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await asyncio.to_thread(self.models.save_state)

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def _emit(self) -> None:
        for listener in list(self._listeners):
            try:
                listener("learning")
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Learning listener failed")

    def _on_climate(self, what: str) -> None:
        if what != "config":
            return
        # Rooms removed from the configuration: their models are archived (never deleted outright).
        removed = [a for a in list(self.models.docs) if a not in self.climate.config["rooms"]]
        for area in removed:
            self._generation[area] = self._generation.get(area, 0) + 1
            self.models.docs.pop(area, None)  # at once (views); the file is moved in a worker thread
        if removed:
            asyncio.get_running_loop().create_task(self._archive(removed, "removed"))
        self._wake.set()
        self._emit()

    async def _archive(self, areas: list[str], why: str) -> None:
        for area in areas:
            await asyncio.to_thread(self.models.archive, area, why)
            _LOGGER.info("Thermal model of %s archived: the room was removed from Climate", area)

    # --------------------------------------------------------------- context
    def windows(self, area_id: str) -> list[WindowSpec]:
        """The room's curtain windows with their geometry (Curtains configuration; context only)."""
        try:
            mgr = self.climate.curtains.manager if self.climate.curtains is not None else None
            if mgr is None:
                return []
            out = []
            for w in mgr.config.windows.values():
                if w.area_id != area_id:
                    continue
                sol = w.solar
                out.append(WindowSpec(
                    window_id=w.window_id, bearing=w.effective_bearing, exposure_width=float(sol.exposure_width),
                    min_elevation=float(sol.min_elevation), max_elevation=sol.max_elevation,
                    obstructions=[(o.from_azimuth, o.to_azimuth, o.min_elevation) for o in sol.obstructions],
                    layers=[(layer.entity_id, layer.kind) for layer in w.layers],
                    sun_sensors=[s.entity_id for s in w.sun_sensors if s.enabled]))
            return out
        except Exception:  # noqa: BLE001 - context only
            return []

    def spec(self, config: dict[str, Any], area_id: str) -> RoomSpec:
        room = config["rooms"][area_id]
        zone = cm.zone_of(config, area_id)
        own = [t["entity_id"] for t in room["thermostats"]]
        shared = [s for s in (zone or {}).get("shared_thermostats", []) if s not in own]
        neighbours = [r for r in (zone or {}).get("rooms", []) if r != area_id and r in config["rooms"]
                      and (config["rooms"][r]["primary_sensor"] or config["rooms"][r]["secondary_sensors"])]
        return RoomSpec(
            area_id=area_id, primary=(room["primary_sensor"] or {}).get("entity_id"),
            secondary=[s["entity_id"] for s in room["secondary_sensors"]], thermostats=[*own, *shared],
            own_thermostats=own, openings=[o["entity_id"] for o in room["openings"]],
            outdoor=(config.get("outdoor") or {}).get("entity_id"),
            stale_s=float(cm.effective_targets(config, area_id)["stale_minutes"]) * 60.0,
            windows=self.windows(area_id), neighbours=neighbours, zone=zone)

    def _view(self) -> StoreView:
        store = self.climate.store
        return StoreView(store.dir, copy.deepcopy(store.coverage), copy.deepcopy(store.gaps),
                         copy.deepcopy(store.aliases))

    def coverage_seconds(self, area_id: str) -> float:
        """Observed seconds of the room's temperature sensors (coverage minus recorded gaps; a cheap estimate)."""
        room = self.climate.config["rooms"].get(area_id)
        if not room:
            return 0.0
        best = 0.0
        for ref in [*([room["primary_sensor"]] if room["primary_sensor"] else []), *room["secondary_sensors"]]:
            span = self.climate.store.coverage.get(ref["entity_id"])
            if not span:
                continue
            gaps = sum(max(0.0, min(b, span["to"]) - max(a, span["from"]))
                       for a, b in self.climate.store.gaps.get(ref["entity_id"], []))
            best = max(best, span["to"] - span["from"] - gaps)
        return best

    # ------------------------------------------------------------- validity
    def validity(self, area_id: str, doc: dict[str, Any] | None = None) -> dict[str, Any]:
        doc = doc or self.models.docs.get(area_id)
        config = self.climate.config
        if doc is None or area_id not in config["rooms"]:
            return {"state": "none", "reasons": []}
        now_fp = fingerprint(config, area_id, self.windows(area_id))
        before = doc.get("fingerprint")
        hard = changed(before, now_fp, "hard")
        if hard:
            return {"state": "invalidated", "keys": hard,
                    "reasons": [f"Since this model was learned, {HARD_TEXT[k]}" for k in hard]}
        soft = changed(before, now_fp, "soft")
        if soft:
            return {"state": "outdated", "keys": soft,
                    "reasons": [f"Since this model was learned, {SOFT_TEXT[k]}" for k in soft]}
        return {"state": "valid", "reasons": []}

    def model_for(self, area_id: str, *, allow_preliminary: bool = False) -> tm.ThermalModel | tm.Unavailable:
        """The room's validated model for prediction (A7.2), or a structured Unavailable."""
        if area_id not in self.climate.config["rooms"]:
            return tm.Unavailable("not_configured", "This room is not configured for climate")
        doc = self.models.docs.get(area_id)
        if doc is None:
            problem = self.models.problems.get(area_id)
            return tm.Unavailable(problem["code"], problem["message"]) if problem else tm.load_model(None)
        fp = fingerprint(self.climate.config, area_id, self.windows(area_id))
        return tm.load_model(doc, fingerprint_hash=_hash(fp["hard"]), allow_preliminary=allow_preliminary)

    # ------------------------------------------------------------ scheduling
    def due(self, area_id: str, now: float) -> str | None:
        """Why a room should be trained now (None: not yet)."""
        if area_id in self.models.protected:
            return None
        room = self.climate.config["rooms"].get(area_id)
        if not room or not (room["primary_sensor"] or room["secondary_sensors"]):
            return None
        attempt = self.models.state.get(area_id) or {}
        doc = self.models.docs.get(area_id)
        hard_now = _hash(fingerprint(self.climate.config, area_id, self.windows(area_id))["hard"])
        last_try = _ts(attempt.get("finished_at")) or 0.0
        if attempt.get("hard_hash") and attempt["hard_hash"] != hard_now and (doc is None or doc.get(
                "fingerprint_hash") != hard_now):
            return "configuration changed"
        if doc is None:
            if self.coverage_seconds(area_id) < MIN_COVERAGE_S or now - last_try < RETRY_S:
                return None
            return "first model"
        if doc.get("fingerprint_hash") != hard_now:
            return None if now - last_try < RETRY_S else "configuration changed"
        trained = _ts(doc.get("trained_at")) or 0.0
        data_to = float((doc.get("data") or {}).get("to") or trained)
        if now - trained >= RETRAIN_S and now - data_to >= NEW_DATA_S and now - last_try >= RETRY_S:
            return "new observations"
        return None

    async def _loop(self) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), FIRST_CHECK_S)
        while not self._stopping:
            self._wake.clear()
            try:
                if self.climate.backfill.get("state") != "running":
                    self.check()
            except Exception:  # noqa: BLE001 - learning must never stop the App
                _LOGGER.exception("Thermal learning check failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CHECK_S)

    def check(self, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        self.last_check = now
        queued = []
        for area_id in sorted(self.climate.config["rooms"]):
            if area_id in self._queue or area_id == self._running:
                continue
            why = self.due(area_id, now)
            if why:
                self._queue[area_id] = Job(area_id, why)
                queued.append(area_id)
        if queued:
            self._kick()
        return queued

    def request(self, area_id: str | None, *, user: str | None = None) -> list[str]:
        """An administrator asked to rebuild one room (or every room)."""
        config = self.climate.config
        if area_id is not None and area_id not in config["rooms"]:
            raise KeyError(area_id)
        areas = [area_id] if area_id else sorted(config["rooms"])
        out = []
        for area in areas:
            if area in self.models.protected:
                continue
            self._queue[area] = Job(area, "requested", user)
            out.append(area)
        self._kick()
        self._emit()
        return out

    def _kick(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._work(), name="climate-learning-fit")

    async def _work(self) -> None:
        while self._queue and not self._stopping:
            area_id, job = next(iter(self._queue.items()))
            del self._queue[area_id]
            if area_id not in self.climate.config["rooms"]:
                continue
            self._running = area_id
            self._emit()
            try:
                await self.train(job)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Thermal learning of %s failed", area_id)
                self.models.state[area_id] = {**self.models.state.get(area_id, {}), "finished_at": utcnow_iso(),
                                              "result": "error", "error": f"{type(err).__name__}: {err}"}
                await asyncio.to_thread(self.models.save_state)
            finally:
                self._running = None
                self._emit()

    async def train(self, job: Job, *, now: float | None = None) -> Learned | None:
        """Train one room: snapshot the configuration, fit in a worker thread, save when still current."""
        area_id = job.area_id
        config = copy.deepcopy(self.climate.config)
        spec = self.spec(config, area_id)
        fp = fingerprint(config, area_id, spec.windows)
        hard_hash, soft_hash = _hash(fp["hard"]), _hash(fp["soft"])
        neighbour_specs = [self.spec(config, n) for n in spec.neighbours]
        view = self._view()
        now = time.time() if now is None else now
        tz = (self.climate.ha.config or {}).get("time_zone")
        min_days = int(config["prediction"]["min_history_days"])
        started = utcnow_iso()
        generation = self._generation.get(area_id, 0)
        before = self.models.state.get(area_id) or {}
        previous = {k: before.get(k) for k in ("finished_at", "result", "error")} if before.get("result") else None
        self.models.state[area_id] = {"started_at": started, "finished_at": None, "reason": job.reason,
                                      "requested_by": job.requested_by, "hard_hash": hard_hash, "previous": previous}
        await asyncio.to_thread(self.models.save_state)
        t0 = time.monotonic()
        learned = await asyncio.to_thread(train_room, spec, neighbour_specs, view, now, tz, min_days)
        elapsed = round(time.monotonic() - t0, 2)
        if self._stopping:
            return None
        if self._generation.get(area_id, 0) != generation:
            # The model was reset (or the room removed) while this fit ran: its result is not wanted.
            _LOGGER.info("Thermal model of %s: the fit was discarded (reset during training)", area_id)
            return learned
        attempt: dict[str, Any] = {"started_at": started, "finished_at": utcnow_iso(), "reason": job.reason,
                                   "requested_by": job.requested_by, "hard_hash": hard_hash, "seconds": elapsed,
                                   "previous": previous,
                                   "result": learned.diagnostic.get("status"),
                                   "diagnostic": _small(learned.diagnostic)}
        # The configuration may have changed while the fit ran: a model of the old configuration is not saved.
        current = self.climate.config["rooms"].get(area_id)
        if current is None or _hash(fingerprint(self.climate.config, area_id, self.windows(area_id))["hard"]) \
                != hard_hash:
            attempt.update(result="discarded", error="The configuration changed during training; the room is trained "
                                                     "again with the new configuration")
            self.models.state[area_id] = attempt
            await asyncio.to_thread(self.models.save_state)
            if current is not None:
                self._queue.setdefault(area_id, Job(area_id, "configuration changed"))
            return learned
        doc = learned.doc
        if doc is not None:
            doc.update(fingerprint=fp, fingerprint_hash=hard_hash, soft_hash=soft_hash,
                       config_revision=self.climate.revision, app_version=APP_VERSION, training_seconds=elapsed,
                       reason=doc.get("reason") or job.reason)
            old = self.models.docs.get(area_id)
            keep_old = (old is not None and old.get("fingerprint_hash") == hard_hash
                        and old.get("status") in ("low", "moderate", "high")
                        and doc["status"] in ("insufficient", "unidentifiable"))
            if keep_old:
                attempt["kept"] = "The previous model is kept: the new fit was " + (
                    "preliminary" if doc["status"] == "insufficient" else "not reliable")
            else:
                try:
                    await asyncio.to_thread(self.models.save, doc)
                    self.trainings += 1
                except (OSError, PermissionError, tm.ModelError) as err:
                    attempt.update(result="error", error=f"The model could not be stored ({err})")
        self.models.state[area_id] = attempt
        await asyncio.to_thread(self.models.save_state)
        _LOGGER.info("Thermal model of %s: %s (%.1f s)", area_id, attempt["result"], elapsed)
        return learned

    async def reset(self, area_id: str) -> bool:
        """Forget a room's learned model (archived, not deleted). Observations and configuration are kept."""
        if area_id not in self.climate.config["rooms"] and area_id not in self.models.docs:
            raise KeyError(area_id)
        self._queue.pop(area_id, None)
        self._generation[area_id] = self._generation.get(area_id, 0) + 1
        moved = await asyncio.to_thread(self.models.archive, area_id, "reset")
        # No automatic retraining before the retry interval: a reset is a deliberate "start again".
        self.models.state[area_id] = {"finished_at": utcnow_iso(), "result": "reset"}
        await asyncio.to_thread(self.models.save_state)
        self._emit()
        return moved

    # ----------------------------------------------------------------- views
    def status_of(self, area_id: str) -> str:
        if area_id == self._running:
            return "learning"
        if area_id in self.models.problems:
            return "error"
        doc = self.models.docs.get(area_id)
        if doc is not None:
            return doc["status"]
        attempt = self.models.state.get(area_id) or {}
        if attempt.get("result") == "error":
            return "error"
        if attempt.get("result") in ("insufficient", "collecting", "unidentifiable"):
            return attempt["result"]
        return "collecting" if self.coverage_seconds(area_id) > 0 else "no_data"

    def prerequisites(self, area_id: str) -> list[dict[str, Any]]:
        """What is missing for (better) learning, from the configuration and the devices."""
        config = self.climate.config
        room = config["rooms"][area_id]
        out = []

        def add(key: str, text: str, blocking: bool = False) -> None:
            out.append({"key": key, "text": text, "blocking": blocking})

        if not room["primary_sensor"] and not room["secondary_sensors"]:
            add("no_sensor", "No room temperature sensor: there is nothing to learn from", True)
        if not room["thermostats"]:
            add("no_heating", "No heating device: only the passive behaviour can be learned")
        for t in room["thermostats"]:
            caps = t.get("capabilities") or {}
            state = self.climate.ha.states.get(t["entity_id"])
            reports = "hvac_action" in ((state or {}).get("attributes") or {}) if state else caps.get("reports_action")
            if not reports:
                add("no_action", f"{t['entity_id']} does not report whether it is heating: its heating response can "
                                 "only be inferred from setpoints, at reduced confidence")
        if not config.get("outdoor"):
            add("no_outdoor", "No outdoor temperature source: the outdoor influence cannot be estimated")
        if not room["openings"]:
            add("no_openings", "No windows or doors are assigned: open-window periods cannot be left out of the "
                               "learning")
        if not any(w.bearing is not None for w in self.windows(area_id)):
            add("no_geometry", "No curtain window with a direction in this room: solar gain cannot be estimated")
        zone = cm.zone_of(config, area_id)
        if zone and len(zone["rooms"]) > 1 and not self.spec(config, area_id).neighbours:
            add("zone_unmeasured", "The other rooms of its thermal zone have no temperature sensor: their influence "
                                   "cannot be measured")
        return out

    def summary(self, area_id: str) -> dict[str, Any]:
        """The learning summary of a room (Climate → Overview)."""
        doc = self.models.docs.get(area_id)
        status = self.status_of(area_id)
        attempt = self.models.state.get(area_id) or {}
        min_days = self.climate.config["prediction"]["min_history_days"]
        ev = (doc or {}).get("evidence") or (attempt.get("diagnostic") or {}).get("evidence") or {}
        usable_days = ev.get("usable_days")
        observed_days = round(self.coverage_seconds(area_id) / 86400.0, 2)
        out: dict[str, Any] = {
            "area_id": area_id, "status": status, "status_text": STATUS_TEXT.get(status, status),
            "queued": area_id in self._queue, "running": area_id == self._running,
            "observed_days": observed_days, "usable_days": usable_days, "min_history_days": min_days,
            "ready": bool(usable_days is not None and usable_days >= min_days),
            "validity": self.validity(area_id), "prerequisites": self.prerequisites(area_id),
            "last_attempt": {k: attempt.get(k) for k in ("started_at", "finished_at", "result", "error", "reason",
                                                         "kept")} if attempt else None,
            "problem": self.models.problems.get(area_id),
            "trained_at": (doc or {}).get("trained_at"),
            "preliminary": (doc or {}).get("preliminary"),
        }
        if doc and doc.get("parameters"):
            p = doc["parameters"]
            heat = p.get("heating_c_per_h") or {}
            inferred = p.get("heating_inferred_c_per_h") or {}
            out["heating"] = {"value": heat.get("value") if heat.get("available") else inferred.get("value"),
                              "basis": "reported" if heat.get("available") else "inferred" if inferred.get("available")
                              else None, "confidence": (heat if heat.get("available") else inferred).get("confidence"),
                              "reason": heat.get("reason") if not heat.get("available") and not inferred.get(
                                  "available") else None}
            out["cooling"] = {"time_constant_h": (p.get("time_constant_h") or {}).get("value"),
                              "k_per_h": (p.get("k_per_h") or {}).get("value"),
                              "reference": doc["structure"]["reference"],
                              "confidence": (p.get("k_per_h") or {}).get("confidence")}
            out["confidence"] = {"level": doc["confidence"]["level"], "score": doc["confidence"]["score"]}
            val = doc.get("validation") or {}
            head = val.get("headline")
            if head:
                h = val["horizons"][head]
                out["accuracy"] = {"horizon_min": int(head), "mae": h["model"]["mae"], "rmse": h["model"]["rmse"],
                                   "persistence_mae": h["persistence"]["mae"], "origins": h["origins"]}
        return out

    def overview(self) -> dict[str, Any]:
        rooms = {a: self.summary(a) for a in self.climate.config["rooms"]}
        counts: dict[str, int] = {}
        for s in rooms.values():
            key = "invalidated" if s["validity"]["state"] == "invalidated" else s["status"]
            counts[key] = counts.get(key, 0) + 1
        return {"rooms": rooms, "counts": counts, "running": self._running, "queued": list(self._queue),
                "notices": list(self.models.notices), "store": self.models.stats(), "trainings": self.trainings,
                "last_check": self.last_check, "algorithm": tm.ALGORITHM, "schema": tm.SCHEMA,
                "statuses": STATUS_TEXT}

    def room(self, area_id: str) -> dict[str, Any]:
        """Everything about a room's learning (Climate → room → Thermal learning)."""
        if area_id not in self.climate.config["rooms"]:
            raise KeyError(area_id)
        doc = self.models.docs.get(area_id)
        attempt = self.models.state.get(area_id) or {}
        spec = self.spec(self.climate.config, area_id)
        areas = self.climate.rooms.index().areas
        store = self.climate.store
        ids = [e for e in spec.entities() if e != "sun.sun"]
        gaps = {e: [g for g in store.gaps.get(e, [])] for e in ids if store.gaps.get(e)}
        gap_hours = round(sum(b - a for g in gaps.values() for a, b in g) / 3600.0, 2)
        return {"summary": self.summary(area_id), "model": doc, "attempt": attempt,
                "entities": spec.entities(), "recorder_gaps": gaps, "recorder_gap_hours": gap_hours,
                "coverage": {e: store.coverage.get(e) for e in ids},
                "algorithm": tm.ALGORITHM, "schema": tm.SCHEMA, "statuses": STATUS_TEXT,
                "unit": cm.display_unit(self.climate.config, self.climate.ha_unit),
                "names": {a: (areas.get(a) or {}).get("name") or a for a in [area_id, *spec.neighbours]}}

    def export(self, area_id: str) -> dict[str, Any]:
        detail = self.room(area_id)
        return {"format": EXPORT_FORMAT, "version": 1, "exported_at": utcnow_iso(), "app_version": APP_VERSION,
                "area_id": area_id, "room_config": self.climate.config["rooms"][area_id], **detail}


def _small(diag: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in diag.items() if k not in ("selection",)}
    return json.loads(json.dumps(out, default=str))


def _ts(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def train_room(spec: RoomSpec, neighbour_specs: list[RoomSpec], view: StoreView, now: float, tz: str | None,
               min_history_days: int) -> Learned:
    """The blocking part of a training (a worker thread): read, prepare, fit."""
    temp_ids = [e for e in [spec.primary, *spec.secondary] if e]
    starts = [view.coverage[e]["from"] for e in temp_ids if e in view.coverage]
    start = max(now - TRAIN_DAYS * 86400.0, min(starts) if starts else now - GRID_S)
    start = min(start, now - GRID_S)
    only_temps = [RoomSpec(n.area_id, n.primary, n.secondary, [], [], None, stale_s=n.stale_s) for n in neighbour_specs]
    kinds = dict(spec.entities())
    for n in only_temps:
        kinds.update(n.entities())
    events = read_events(view, kinds, start, now)  # one pass over the day files for the room and its neighbours
    neighbour_temps = {n.area_id: prepare_room(n, view, start, now, tz=tz, events=events).temp for n in only_temps}
    grid = prepare_room(spec, view, start, now, tz=tz, neighbour_temps=neighbour_temps, events=events)
    return learn(grid, min_history_days=min_history_days, now=now)


__all__ = ["STATUS_TEXT", "ThermalLearning", "fingerprint", "train_room"]
