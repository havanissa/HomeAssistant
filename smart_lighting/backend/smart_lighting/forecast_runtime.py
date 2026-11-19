"""The App's forecast runtime (Phase A5): the integration's forecast manager, in shadow or as the producer.

Forecasts are observation only on both sides: nothing here, and nothing the
vendored forecast code does, commands a device.

* **Shadow** (the integration produces the forecasts, the default): every bridge
  session starts a fresh manager from the integration's learned data (settings
  and daily summaries from ``hello``). It learns alongside the integration (today
  and newly completed days, through the websocket history API), follows its
  settings changes, and its predictions are compared with the integration's
  published view (``forecast_compare``). Nothing it computes is published.
* **Owner** (after a reviewed handover): the App's manager starts from the data
  the integration handed over (validated against the same model code), learns,
  and publishes its view (every room's forecast, the predicted next room, the
  learning status) and its learned data to the integration over the fenced
  bridge; the integration's entities show the published view, and the published
  learned data is the rollback copy, so a return of control never loses a learned
  day. Its own copy lives in ``/data/predictions/store``.
* **Restarts and reconnects**: the owner resumes with its lease token and
  restarts its manager from its own stores; a new App process is a new instance
  and the integration produces the forecasts again, from the App's last learned
  data.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
from typing import Any

from occupancy_core import fingerprint as core_fingerprint
from smart_occupancy import forecast_sync as S
from smart_occupancy.runtime_manifest import RUNTIME_VERSION, fingerprint as runtime_fingerprint

from .forecast_compare import ForecastComparator, iso
from .forecast_host import DAYS_STORE_KEY, ForecastInstance
from .ha import HomeAssistantClient, HomeAssistantError
from .lighting_host import room_result

_LOGGER = logging.getLogger(__name__)

MIN_SHADOW_SECONDS = 3600
MIN_MATCHES = 10
VIEW_DELAY = 2.0
EXPORT_DELAY = 60.0
MODELS_DELAY = 0.5
RESUBSCRIBE_DELAYS = (2, 5, 15, 30, 60)
OWNERSHIP_LOG = 50
BACKUPS = 20


class ForecastError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> datetime:
    return datetime.now(UTC)


def _detail(err: Exception) -> str:
    return str(err).split(": ", 1)[-1]


class ForecastRuntime:
    def __init__(self, ha: HomeAssistantClient, occupancy: Any, data_dir: Path | None, *, app_version: str,
                 min_shadow_seconds: int = MIN_SHADOW_SECONDS, min_matches: int = MIN_MATCHES) -> None:
        self.ha = ha
        self.occupancy = occupancy
        self.dir = data_dir / "predictions" if data_dir else None
        self.app_version = app_version
        self.min_shadow_seconds = min_shadow_seconds
        self.min_matches = min_matches
        self.instance_id = secrets.token_hex(16)
        self.fingerprint: str | None = None
        self.core_fingerprint: str | None = None
        self.mode = "starting"
        self.bridge = "disconnected"
        self.bridge_error: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.integration: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.integration_settings_revision: str | None = None
        self.integration_view: dict[str, Any] | None = None
        self.integration_view_at: str | None = None
        self.recorder_oldest: float | None = None
        self.instance: ForecastInstance | None = None
        self.epoch: int | None = None
        self._token: str | None = None
        self.seq = 0
        self.lease_until: str | None = None
        self.comparator = ForecastComparator()
        self.counters: Counter[str] = Counter()
        self.last_error: dict[str, Any] | None = None
        self.notices: list[dict[str, str]] = []
        self.ownership_log: deque[dict[str, Any]] = deque(maxlen=OWNERSHIP_LOG)
        self._listeners: list[Callable[[str], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._heartbeat: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None
        self._view_handle: asyncio.TimerHandle | None = None
        self._export_handle: asyncio.TimerHandle | None = None
        self._models_handle: asyncio.TimerHandle | None = None
        self._backlog: list[dict[str, Any]] | None = None
        self._switching = False
        self._stop_lock = asyncio.Lock()
        self._attempt = 0
        self._stopping = False
        self.last_copy: dict[str, Any] | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self.fingerprint = await asyncio.to_thread(runtime_fingerprint)
        self.core_fingerprint = await asyncio.to_thread(core_fingerprint)
        self.mode = "standalone"
        self._unsubs.append(self.ha.add_listener(self._on_ha))
        self._unsubs.append(self.occupancy.add_result_listener(self._on_occupancy))
        if self.ha.connected:
            self._connected()

    async def stop(self) -> None:
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for handle in (self._view_handle, self._export_handle, self._models_handle):
            if handle:
                handle.cancel()
        for task in (self._heartbeat, self._subscribe_task, *self._tasks):
            if task:
                task.cancel()
        await self._stop_instance(flush=True)

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self, what: str = "predictions") -> None:
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Predictions listener failed")

    def _models_changed(self) -> None:
        if self._models_handle is None:
            self._models_handle = asyncio.get_running_loop().call_later(MODELS_DELAY, self._models_fire)

    def _models_fire(self) -> None:
        self._models_handle = None
        self._changed("models")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _log(self, event: str, **data: Any) -> None:
        self.ownership_log.appendleft({"at": iso(utcnow()), "event": event, **data})

    def _notice(self, code: str, text: str) -> None:
        self.notices = [n for n in self.notices if n["code"] != code][-9:] + [{"code": code, "text": text}]

    # --------------------------------------------------------------- events
    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._disconnected(str((payload or {}).get("last_error") or "disconnected"))
        elif kind == "state":
            entity_id, new_state = payload
            if self.instance is not None:
                self.instance.state_changed(entity_id, new_state)
        elif kind == "reported":
            if self.instance is not None:
                self.instance.state_reported(*payload)
        elif kind == "registries":
            if self.instance is not None:
                self.instance.registries_changed()
        elif kind == "event":
            if (payload or {}).get("event_type") == "homeassistant_started" and self.instance is not None:
                self.instance.ha_started()

    def _on_occupancy(self, area_id: str) -> None:
        if self.instance is not None:
            fired = getattr(self.occupancy, "result_fired", {}).get(area_id)
            self.instance.occupancy_changed(area_id, fired)

    def _connected(self) -> None:
        if self._subscribe_task is None or self._subscribe_task.done():
            self._attempt = 0
            self._subscribe_task = asyncio.get_running_loop().create_task(self._subscribe_loop())

    def _disconnected(self, reason: str) -> None:
        self.session_id = None
        self.sub_id = None
        self.bridge, self.bridge_error = "disconnected", reason
        self.comparator.stop()
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        self._spawn(self._stop_instance(flush=self.mode == "owner"))
        self._changed()

    async def _subscribe_loop(self) -> None:
        while not self._stopping and self.ha.connected and self.session_id is None:
            self.bridge = "subscribing"
            try:
                self.sub_id = await self.ha.subscribe(
                    "smart_occupancy/forecast/bridge/subscribe", self._on_bridge,
                    instance_id=self.instance_id, protocol=S.PROTOCOL_VERSION, app_version=self.app_version,
                    runtime_version=RUNTIME_VERSION, fingerprint=self.fingerprint,
                    core_fingerprint=self.core_fingerprint, resume_token=self._token)
                return
            except HomeAssistantError as err:
                if err.code in ("unknown_command", "not_found"):
                    self.bridge, self.bridge_error = "unsupported", (
                        "The integration does not offer the forecast bridge (it needs version 0.9.0 or later)")
                elif err.code == S.REJECT_VERSION:
                    self.bridge, self.bridge_error = "incompatible", _detail(err)
                else:
                    self.bridge, self.bridge_error = "error", f"{err.code}: {_detail(err)}"
            except (ConnectionError, TimeoutError) as err:
                self.bridge, self.bridge_error = "disconnected", type(err).__name__
            self._changed()
            await asyncio.sleep(RESUBSCRIBE_DELAYS[min(self._attempt, len(RESUBSCRIBE_DELAYS) - 1)])
            self._attempt += 1

    async def _resubscribe(self) -> None:
        if self.sub_id is not None:
            await self.ha.unsubscribe(self.sub_id)
        self.sub_id = None
        self.session_id = None
        self._connected()

    def _on_bridge(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "hello":
            self._backlog = []
            self._spawn(self._on_hello(event))
            return
        if self._backlog is not None:
            self._backlog.append(event)
            return
        self._dispatch(event)

    def _dispatch(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        try:
            if kind == "view":
                self.integration_view = event.get("view") if isinstance(event.get("view"), dict) else None
                self.integration_view_at = event.get("fired")
                if self.integration_view is not None:
                    self.comparator.view("integration", self.integration_view)
                self._models_changed()
            elif kind == "settings":
                self._on_settings(event)
            elif kind == "ownership":
                self._on_ownership(event.get("ownership") or {})
            elif kind == "closed":
                self.session_id = None
                self.sub_id = None
                self.bridge, self.bridge_error = "disconnected", "the integration closed the session"
                self.comparator.stop()
                self._spawn(self._stop_instance(flush=self.mode == "owner"))
                self._connected()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Forecast bridge message %s not usable: %s", kind, err)
        self._changed()

    async def _on_hello(self, hello: dict[str, Any]) -> None:
        try:
            await self._hello(hello)
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Forecast bridge session could not start")
            self.bridge, self.bridge_error = "error", f"invalid hello: {err}"
        finally:
            backlog, self._backlog = self._backlog or [], None
            for event in backlog:
                self._dispatch(event)
            self._changed()

    async def _hello(self, hello: dict[str, Any]) -> None:
        self.session_id = hello.get("session_id")
        self.integration = dict(hello.get("ownership") or {})
        self.options = dict(hello.get("options") or {})
        self.integration_settings_revision = hello.get("settings_revision")
        oldest = hello.get("recorder_oldest")
        self.recorder_oldest = float(oldest) if isinstance(oldest, (int, float)) and not isinstance(oldest, bool) \
            else None
        self.bridge, self.bridge_error = "connected", None
        self.counters["sessions"] += 1
        lease = hello.get("lease_token")
        await self._stop_instance(flush=self.mode == "owner")
        if lease and S.valid_id(lease):
            self._token, self.epoch, self.seq = lease, self.integration.get("epoch"), 0
            self.mode = "owner"
            self._log("resumed", epoch=self.epoch)
            await self._start_owner(export=None)
        else:
            if self.mode == "owner":
                self._log("lost", reason="the integration did not keep the App's control")
                self._notice("lost", "Forecast production returned to the integration while the App was away.")
            self._token, self.epoch = None, None
            self.mode = "shadow"
            await self._start_shadow(hello)
        self._changed()

    def _on_settings(self, event: dict[str, Any]) -> None:
        self.integration_settings_revision = event.get("settings_revision")
        inst = self.instance
        if self.mode != "shadow" or inst is None or inst.manager is None:
            return
        from smart_occupancy.forecast_settings import ForecastSettings  # noqa: PLC0415

        self._spawn(inst.manager.async_set_settings(ForecastSettings.from_dict(event.get("settings") or {})))

    def _on_ownership(self, ownership: dict[str, Any]) -> None:
        self.integration = dict(ownership)
        if self.mode == "owner" and (ownership.get("owner") != S.OWNER_APP or ownership.get("epoch") != self.epoch):
            self._spawn(self._lose("the integration changed the owner"))

    # ------------------------------------------------------------ instances
    def _published(self, area_id: str) -> Any:
        return room_result(self.occupancy, area_id)

    async def _start_shadow(self, hello: dict[str, Any]) -> None:
        export = hello.get("export") if isinstance(hello.get("export"), dict) else None
        self.integration_view = hello.get("view") if isinstance(hello.get("view"), dict) else None
        inst = ForecastInstance(self.ha, role="shadow", options=self.options, export=export,
                                published=self._published, storage_dir=None, recorder_oldest=lambda: self.recorder_oldest)
        await self._run_instance(inst)
        inst.manager.view_observer = lambda view: self._on_own_view(view)
        self.comparator.start(utcnow())
        if self.integration_view is not None:
            self.comparator.view("integration", self.integration_view)

    async def _start_owner(self, *, export: dict[str, Any] | None) -> None:
        store = self.dir / "store" if self.dir else None
        inst = ForecastInstance(self.ha, role="owner", options=self.options, export=export,
                                published=self._published, storage_dir=store, recorder_oldest=lambda: self.recorder_oldest)
        await self._run_instance(inst)
        self.comparator.stop()
        inst.manager.view_observer = lambda view: self._on_own_view(view)
        inst.backend._store_saved = self._store_saved  # noqa: SLF001 - the owner's saved days are mirrored
        self._schedule_view(0)
        self._schedule_export(5)
        self._start_heartbeat()

    def _on_own_view(self, view: dict[str, Any]) -> None:
        if self.mode == "shadow":
            self.comparator.view("app", view)
        elif self.mode == "owner":
            self._schedule_view(VIEW_DELAY)
        self._models_changed()

    async def _run_instance(self, inst: ForecastInstance) -> None:
        self.instance = inst
        await inst.async_start()

    async def _stop_instance(self, *, flush: bool) -> None:
        # Serialized: a stop spawned by a lost connection may still be flushing the stores when the next hello
        # starts an instance on the same files; the newer caller waits for the older stop first.
        async with self._stop_lock:
            inst, self.instance = self.instance, None
            if inst is not None:
                await inst.async_stop(flush=flush)

    # ---------------------------------------------------------------- owner
    def _owner_params(self) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session_id, "epoch": self.epoch, "lease_token": self._token, "seq": self.seq}

    async def _owner_command(self, command: str, **params: Any) -> Any:
        if self.mode != "owner" or self.session_id is None or self._token is None:
            raise ForecastError(S.REJECT_NOT_OWNER, "The App does not produce the forecasts")
        try:
            return await self.ha.forecast_command(command, **self._owner_params(), **params)
        except HomeAssistantError as err:
            if err.code in S.LOST_CODES:
                self._spawn(self._lose(f"{command.rsplit('/', 1)[-1]} refused: {err.code}"))
            raise

    def _store_saved(self, key: str, data: Any) -> None:
        if key == DAYS_STORE_KEY and self.mode == "owner":
            self._schedule_export(EXPORT_DELAY)

    def _schedule_view(self, delay: float) -> None:
        if self._view_handle is None:
            self._view_handle = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self._publish_view()))

    def _schedule_export(self, delay: float) -> None:
        if self._export_handle is None:
            self._export_handle = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self._publish_export()))

    async def _publish_view(self) -> None:
        self._view_handle = None
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.manager is None:
            return
        try:
            await self._owner_command("smart_occupancy/forecast/bridge/publish", view=inst.manager.view())
            self.counters["views_published"] += 1
        except (ForecastError, HomeAssistantError, ConnectionError, TimeoutError) as err:
            self.last_error = {"at": iso(utcnow()), "code": getattr(err, "code", type(err).__name__), "detail": str(err)}
            if self.mode == "owner":
                self._schedule_view(10)

    async def _publish_export(self) -> None:
        self._export_handle = None
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.manager is None:
            return
        try:
            await self._owner_command("smart_occupancy/forecast/bridge/publish", export=inst.manager.export_learning())
            self.counters["exports_published"] += 1
        except (ForecastError, HomeAssistantError, ConnectionError, TimeoutError) as err:
            self.last_error = {"at": iso(utcnow()), "code": getattr(err, "code", type(err).__name__), "detail": str(err)}
            if self.mode == "owner":
                self._schedule_export(60)

    def _start_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while self.mode == "owner" and not self._stopping:
            await asyncio.sleep(S.HEARTBEAT_SECONDS)
            if self.mode != "owner":
                return
            try:
                reply = await self._owner_command("smart_occupancy/forecast/bridge/heartbeat")
                self.lease_until = (reply or {}).get("lease_until")
            except (ForecastError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug("forecast heartbeat failed: %s", err)

    async def _lose(self, why: str) -> None:
        if self.mode != "owner" or self._switching:
            return
        _LOGGER.warning("The App no longer produces the forecasts (%s); back to shadow mode", why)
        self._log("lost", reason=why)
        self.mode = "shadow"
        self._token, self.epoch = None, None
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        await self._stop_instance(flush=True)
        if self.ha.connected:
            await self._resubscribe()
        self._changed()

    # -------------------------------------------------------- handover, return
    def _own_revisions(self) -> tuple[str | None, str | None, int]:
        inst = self.instance
        mgr = inst.manager if inst else None
        if mgr is None:
            return None, None, 0
        settings = S.revision(mgr.settings.as_dict())
        learned = S.learned_revision({"days": {k: d.as_dict() for k, d in mgr.days.items()},
                                      "no_data": sorted(mgr.no_data)})
        return settings, learned, len(mgr.days)

    def readiness(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        report = self.comparator.report(now)
        checks: list[dict[str, Any]] = []

        def check(key: str, ok: bool, title: str, detail: str, warning: bool = False) -> None:
            checks.append({"key": key, "status": "ok" if ok else "warning" if warning else "error", "title": title,
                           "detail": detail})

        integ = self.integration
        settings, learned, days = self._own_revisions()
        check("bridge", self.bridge == "connected", "Forecast bridge connected", self.bridge_error or self.bridge)
        check("fingerprint", bool(self.fingerprint) and self.fingerprint == integ.get("fingerprint"),
              "Same forecast code", "The App and the integration run identical forecast modules"
              if self.fingerprint == integ.get("fingerprint") else "Update the App and the integration together")
        check("settings", settings is not None and settings == self.integration_settings_revision,
              "Settings in sync", f"App {settings or '-'}, integration {self.integration_settings_revision or '-'}")
        check("learned", learned is not None and learned == integ.get("learned_revision"),
              "Learned days identical", f"{days} day(s) learned on both sides" if learned == integ.get("learned_revision")
              else f"App {days} day(s), integration {integ.get('days_learned', '-')}; they re-align after the next refresh")
        check("owner", integ.get("owner") == S.OWNER_INTEGRATION and integ.get("state") == S.OWNER_INTEGRATION,
              "The integration produces the forecasts", f"owner {integ.get('owner') or '-'}")
        check("duration", report["seconds"] >= self.min_shadow_seconds, "Compared long enough",
              f"{report['seconds'] // 60} of {self.min_shadow_seconds // 60} min")
        check("matches", report["agreements"] >= self.min_matches, "Enough matching predictions",
              f"{report['agreements']} of {self.min_matches} room forecasts agreed")
        check("disagreements", report["disagreements"] == 0, "Predictions agree",
              f"{report['disagreements']} disagreement(s); review them below", warning=True)
        ready = all(c["status"] == "ok" for c in checks if c["status"] != "warning") and self.mode == "shadow"
        return {"ready": ready, "checks": checks, "summary": {
            "seconds": report["seconds"], "matched": report["agreements"], "mismatches": report["disagreements"]},
            "thresholds": {"min_shadow_seconds": self.min_shadow_seconds, "min_matches": self.min_matches}}

    async def handover(self, user_id: str | None) -> dict[str, Any]:
        if self.mode == "owner":
            raise ForecastError("already_owner", "The App already produces the forecasts")
        if self._switching:
            raise ForecastError("busy", "A change of control is in progress")
        ready = self.readiness()
        if not ready["ready"]:
            failed = [c["title"] for c in ready["checks"] if c["status"] == "error"]
            raise ForecastError("not_ready", "Not ready for a handover: " + ", ".join(failed))
        self._switching = True
        try:
            try:
                grant = await self.ha.forecast_command(
                    "smart_occupancy/forecast/bridge/claim", session_id=self.session_id,
                    expected_epoch=self.integration.get("epoch"), settings_revision=self.integration_settings_revision,
                    readiness={k: ready[k] for k in ("ready", "checks", "summary")}, requested_by=user_id)
            except HomeAssistantError as err:
                raise ForecastError(err.code, _detail(err)) from err
            except (ConnectionError, TimeoutError) as err:
                raise ForecastError("not_connected", "Home Assistant is not connected", 503) from err
            export = S.check_export(grant["export"])
            self._backup(export, "handover")
            await self._stop_instance(flush=False)
            self._token, self.epoch, self.seq = grant["lease_token"], grant["epoch"], 0
            self.lease_until = grant.get("lease_until")
            self.mode = "owner"
            self.integration.update(owner=S.OWNER_APP, state=S.OWNER_APP, epoch=self.epoch)
            self._log("handover", epoch=self.epoch, by=user_id)
            store = self.dir / "store" if self.dir else None
            if store is not None and store.exists():
                await asyncio.to_thread(shutil.rmtree, store)
            await self._start_owner(export=export)
            check = self.verify_copy(export, grant.get("learned_revision"))
            self.last_copy = check
        finally:
            self._switching = False
        _LOGGER.warning("Forecast handover: the App now produces the forecasts (epoch %s)", self.epoch)
        self._changed()
        return {"epoch": self.epoch, "copy": check}

    def verify_copy(self, export: dict[str, Any], learned_revision: Any) -> dict[str, Any]:
        """Source and destination learned data: every exported day loaded, the same revision (same model input)."""
        settings, learned, days = self._own_revisions()
        wanted = sorted(export.get("days") or {})
        differences = []
        if settings != S.revision(export.get("settings") or {}):
            differences.append("settings")
        if learned != (learned_revision or S.learned_revision(export)):
            differences.append("days")
        return {"source_days": len(wanted), "destination_days": days, "source_revision": learned_revision,
                "destination_revision": learned, "identical": not differences, "differences": differences}

    def _backup(self, data: dict[str, Any], reason: str) -> None:
        if self.dir is None:
            return
        folder = self.dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        payload = {"schema": S.EXPORT_SCHEMA, "reason": reason, "at": iso(utcnow()), "export": data}
        path = folder / f"predictions-{stamp}-{reason}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1))
        os.replace(tmp, path)
        for old in sorted(folder.glob("predictions-*.json"))[:-BACKUPS]:
            old.unlink(missing_ok=True)

    def backups(self) -> list[dict[str, Any]]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not folder.is_dir():
            return []
        return [{"id": p.stem, "size": p.stat().st_size} for p in sorted(folder.glob("predictions-*.json"), reverse=True)]

    def export(self) -> dict[str, Any]:
        inst = self.instance
        mgr = inst.manager if inst else None
        return {"schema": S.EXPORT_SCHEMA, "at": iso(utcnow()), "mode": self.mode,
                "export": mgr.export_learning() if mgr else None, "view": mgr.view() if mgr else None}

    def import_review(self, doc: Any) -> dict[str, Any]:
        export = doc.get("export") if isinstance(doc, dict) and isinstance(doc.get("export"), dict) else doc
        try:
            export = S.check_export(export)
        except S.ProtocolError as err:
            raise ForecastError("invalid_export", f"This is not a Smart Lighting predictions export: {err}", 400) from err
        from smart_occupancy.forecast_model import HouseDay  # noqa: PLC0415

        days = {k: d for k, v in export["days"].items() if (d := HouseDay.from_dict(v)) is not None}
        _settings, learned, own_days = self._own_revisions()
        return {"writable": self.mode == "owner", "days": len(days), "invalid_days": len(export["days"]) - len(days),
                "current_days": own_days, "current_revision": learned, "incoming_revision": S.learned_revision(export),
                "exported_at": export.get("exported_at"), "same": learned == S.learned_revision(export)}

    async def import_apply(self, doc: Any, expected_revision: Any) -> dict[str, Any]:
        review = self.import_review(doc)
        inst = self.instance
        if self.mode != "owner" or inst is None or inst.manager is None:
            raise ForecastError("not_owner", "The App does not produce the forecasts: hand them over first")
        if expected_revision != review["current_revision"]:
            raise ForecastError("stale_revision", "The learned data changed meanwhile; review again")
        export = doc.get("export") if isinstance(doc, dict) and isinstance(doc.get("export"), dict) else doc
        self._backup(inst.manager.export_learning(), "import")
        taken = await inst.manager.async_import_learning(export, save=True)
        await inst.manager._async_build_model(log_changes=False)  # noqa: SLF001
        self._schedule_export(0)
        self._schedule_view(0)
        return {"applied": True, "days": taken}

    async def release(self, user_id: str | None) -> dict[str, Any]:
        if self.mode != "owner" or self.instance is None:
            return await self.return_control(user_id)
        if self._switching:
            raise ForecastError("busy", "A change of control is in progress")
        self._switching = True
        lost = False
        try:
            inst = self.instance
            mgr = inst.manager
            final = mgr.export_learning()
            self._backup(final, "release")
            try:
                reply = await self._owner_command("smart_occupancy/forecast/bridge/release", view=mgr.view(),
                                                  export=final, detail="returned from the App")
            except (ForecastError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                lost = getattr(err, "code", None) in S.LOST_CODES
                raise ForecastError(getattr(err, "code", None) or "not_connected", _detail(err)) from err
            self.mode = "shadow"
            self._token, self.epoch = None, None
            if self._heartbeat:
                self._heartbeat.cancel()
                self._heartbeat = None
            await self._stop_instance(flush=True)
            self.integration = dict((reply or {}).get("ownership") or {})
            self._log("released", by=user_id)
        finally:
            self._switching = False
            if lost:
                self._spawn(self._lose("release refused"))
        await self._resubscribe()
        self._changed()
        return {"ownership": self.integration}

    async def return_control(self, user_id: str | None) -> dict[str, Any]:
        if self.mode == "owner" and self.instance is not None and not self._switching:
            try:
                return await self.release(user_id)
            except ForecastError as err:
                _LOGGER.warning("Release refused (%s); returning control without the final data", err)
        try:
            reply = await self.ha.forecast_command("smart_occupancy/forecast/bridge/return",
                                                   reason="returned from the App")
        except HomeAssistantError as err:
            raise ForecastError(err.code, _detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            raise ForecastError("not_connected", "Home Assistant is not connected", 503) from err
        self._log("returned", by=user_id)
        if self.mode == "owner":
            await self._lose("returned to the integration")
        self.integration = dict((reply or {}).get("ownership") or self.integration)
        self._changed()
        return {"ownership": self.integration}

    # ---------------------------------------------------------------- views
    def authoritative_view(self) -> dict[str, Any] | None:
        """The forecasts that count: the integration's published view in shadow mode, the App's own as owner."""
        inst = self.instance
        if self.mode == "owner" and inst is not None and inst.manager is not None:
            return inst.manager.view()
        return self.integration_view

    def _authoritative_room(self, area_id: str) -> dict[str, Any] | None:
        view = self.authoritative_view() or {}
        room = (view.get("rooms") or {}).get(area_id)
        return room if isinstance(room, dict) else None

    def room(self, area_id: str) -> dict[str, Any]:
        """Everything the Predictions room page shows (the integration's ``forecast/room`` model from the App's own
        instance: patterns, movement, accuracy, settings), with the forecast that counts from the authoritative side."""
        from smart_occupancy.forecast_websocket import context_candidates, room_detail  # noqa: PLC0415

        inst = self.instance
        mgr = inst.manager if inst else None
        if mgr is None:
            raise ForecastError("not_running", "The forecast runtime is not running (no bridge session)", 503)
        if area_id not in mgr.coordinator.config.enabled_rooms:
            raise ForecastError("not_found", f"Room {area_id} is not enabled", 404)
        detail = room_detail(inst.hass, mgr, area_id)
        detail["context_candidates"] = context_candidates(inst.hass)
        detail["source"] = "app" if self.mode == "owner" else "integration"
        detail["shadow_now"] = detail["now"] if self.mode != "owner" else None
        authoritative = self._authoritative_room(area_id)
        if self.mode != "owner" and authoritative is not None:
            detail["now"] = authoritative
        detail["comparison"] = self.comparator.rooms.get(area_id)
        return detail

    def house(self) -> dict[str, Any]:
        """The house overview: learning progress and history coverage, every room's forecast that counts, the
        predicted next room, house-wide accuracy and the comparison."""
        from smart_occupancy.forecast_model import HORIZONS, accuracy  # noqa: PLC0415

        inst = self.instance
        mgr = inst.manager if inst else None
        view = self.authoritative_view() or {}
        rooms_out = []
        learning: dict[str, Any] = dict(view.get("status") or {})
        house_accuracy: dict[str, Any] = {}
        coverage: dict[str, Any] = {}
        if mgr is not None:
            learning = {**mgr.status.as_dict(), **(view.get("status") or {} if self.mode != "owner" else {})}
            days = sorted(mgr.days)
            coverage = {"days_learned": len(mgr.days), "first_date": days[0] if days else None,
                        "last_date": days[-1] if days else None, "window_days": mgr.settings.defaults.window_days,
                        "days_without_data": len(mgr.no_data), "today_included": mgr.today is not None,
                        "recorder_oldest": mgr.status.as_dict().get("recorder_oldest"),
                        "model_days": mgr.model.days if mgr.model else 0}
            threshold = mgr.settings.defaults.probability_threshold / 100
            house_accuracy = {str(h): accuracy(mgr.days.values(), None, h).as_dict(threshold) for h in HORIZONS}
            for area_id in sorted(mgr.coordinator.config.enabled_rooms, key=mgr.name):
                eff = mgr.settings.effective(area_id)
                model = mgr.model.rooms.get(area_id) if mgr.model else None
                stats = model.stats if model else None
                own = mgr.rooms.get(area_id)
                rooms_out.append({
                    "area_id": area_id, "name": mgr.name(area_id),
                    "now": self._authoritative_room(area_id) if self.mode != "owner" else (own.as_dict(mgr.name) if own else None),
                    "shadow_now": own.as_dict(mgr.name) if own and self.mode != "owner" else None,
                    "enabled": eff.enabled, "horizon_minutes": eff.horizon_minutes,
                    "probability_threshold": eff.probability_threshold, "confidence_threshold": eff.confidence_threshold,
                    "days": stats.days if stats else 0, "entries": int(stats.entries) if stats else 0,
                    "observed_hours": round(stats.observed_hours, 1) if stats else 0,
                    "comparison": self.comparator.rooms.get(area_id),
                })
        return {"mode": self.mode, "source": "app" if self.mode == "owner" else "integration",
                "view_at": self.integration_view_at if self.mode != "owner" else iso(utcnow()),
                "learning": learning, "coverage": coverage, "rooms": rooms_out,
                "next_room": view.get("next_room"), "accuracy": house_accuracy,
                "settings": mgr.settings.as_dict() if mgr else view.get("settings"),
                "workday_entity": mgr.workday_entity() if mgr else None,
                "contexts": mgr.contexts() if mgr else [],
                "house_log": inst.decisions.model("forecast:house") if inst and inst.decisions else None}

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or utcnow()
        inst = self.instance
        integ = self.integration
        settings, learned, days = self._own_revisions()
        return {
            "mode": self.mode, "bridge": self.bridge, "bridge_error": self.bridge_error,
            "instance_id": self.instance_id[:8], "session_id": (self.session_id or "")[:8] or None,
            "app_version": self.app_version, "runtime_version": RUNTIME_VERSION,
            "fingerprint": self.fingerprint, "core_fingerprint": self.core_fingerprint,
            "integration": {k: integ.get(k) for k in (
                "owner", "state", "epoch", "integration_version", "fingerprint", "lease_until", "changed_at",
                "changed_by", "settings_revision", "days_learned", "learned_revision", "recorder_available",
                "recorder_oldest", "learning", "manager", "published", "rejections", "history")},
            "epoch": self.epoch, "lease_until": self.lease_until,
            "settings_revision": settings, "integration_settings_revision": self.integration_settings_revision,
            "learned_revision": learned, "days_learned": days,
            "history_calls": inst.history_calls if inst else 0,
            "running": bool(inst and inst.started), "counters": dict(self.counters), "last_error": self.last_error,
            "notices": self.notices, "ownership_log": list(self.ownership_log), "last_copy": self.last_copy,
            "comparison": self.comparator.report(now), "readiness": self.readiness(now),
        }

    @property
    def manager(self) -> Any:
        inst = self.instance
        return inst.manager if inst is not None else None
