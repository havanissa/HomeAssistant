"""Forecast ownership (Phase A5): which side produces the occupancy forecasts.

Forecasts are observation only, on both sides: nothing here can reach a device.
What the record decides is who *learns and publishes*:

* **Integration owns** (the default, also after every install or upgrade): the
  integration's :class:`ForecastManager` learns from the Recorder and publishes
  the forecast entities. App bridge sessions get a copy of everything it learned
  (settings, daily summaries) when they open and every recomputed view, so the
  App's copy of the same model can learn alongside and compare its predictions.
* **App owns**: the integration's manager is *dormant* - stopped, it learns
  nothing and recomputes nothing, keeps the learned days for its read-only views
  and shows on the forecast entities exactly what the App published
  (:meth:`ForecastManager.apply_published`). The App publishes its view (every
  room's forecast, the predicted next room, the learning status) and its learned
  data; the latter is the rollback copy, so a return of control never loses a
  learned day.

Ownership record (``.storage/smart_occupancy.forecast_ownership``); the App's
published view and learned data in ``.storage/smart_occupancy.forecast_mirror``.

Handover (``claim``, reviewed in the App): the integration exports its learned
data (validated by the App against the same model code: same days, same
predictions), stops its manager and hands over. Fallback (release, ``return``,
the service, lease expiry, a new App instance, no resume after a restart): the
epoch increases, the App's last learned data is written into the integration's
stores and its manager starts again from them (it keeps learning where the App
stopped; nothing is retrained from scratch). Only the same App process may resume
after a Home Assistant restart.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import secrets
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import forecast_sync as S
from .const import DOMAIN, VERSION
from .occupancy_core import fingerprint as core_fingerprint
from .runtime_manifest import RUNTIME_VERSION, fingerprint

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator
    from .forecast import ForecastManager

_LOGGER = logging.getLogger(__name__)

OWNERSHIP_STORE_KEY = f"{DOMAIN}.forecast_ownership"
MIRROR_STORE_KEY = f"{DOMAIN}.forecast_mirror"
STORE_VERSION = 1
SAVE_DELAY_SECONDS = 5
MAX_SESSIONS = 4
#: Forecast websocket commands that keep answering while the App produces the forecasts (reads).
READ_ONLY_COMMANDS = frozenset({"smart_occupancy/forecast/room"})
MANAGED_BY_APP = "managed_by_app"
MANAGED_TEXT = ("Predictions are managed by the Smart Lighting App: change them there (Predictions). This editor is "
                "read-only until control is returned to the integration.")


class BridgeError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class Session:
    session_id: str
    instance_id: str
    connection: Any
    send: Callable[[dict[str, Any]], None]
    app_version: str
    runtime_version: str
    fingerprint: str
    core_fingerprint: str
    protocol: int
    opened_at: datetime
    last_seen: datetime
    messages: int = 0

    def as_dict(self, owner_session: str | None) -> dict[str, Any]:
        return {
            "session_id": self.session_id[:8], "instance_id": self.instance_id[:8], "app_version": self.app_version,
            "runtime_version": self.runtime_version, "fingerprint": self.fingerprint, "protocol": self.protocol,
            "opened_at": _iso(self.opened_at), "last_seen": _iso(self.last_seen), "messages": self.messages,
            "owner": self.session_id == owner_session,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _readiness(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"ready": False, "checks": []}
    checks = [{"key": c["key"][:64], "status": str(c.get("status"))[:16]} for c in value.get("checks") or []
              if isinstance(c, dict) and isinstance(c.get("key"), str)]
    return {"ready": value.get("ready") is True, "checks": checks[:30],
            "summary": value.get("summary") if isinstance(value.get("summary"), dict) else None}


class ForecastBridge:
    def __init__(self, hass: HomeAssistant, coordinator: SmartOccupancyCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.boot_id = secrets.token_hex(16)
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, OWNERSHIP_STORE_KEY)
        self._mirror_store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, MIRROR_STORE_KEY)
        self.fingerprint: str | None = None
        self.core_fingerprint: str | None = None
        self.owner = S.OWNER_INTEGRATION
        self.epoch = 0
        self.owner_instance: str | None = None
        self.owner_session: str | None = None
        self.owner_versions: dict[str, Any] = {}
        self._lease_hash: str | None = None
        self.lease_until: datetime | None = None
        self.last_seq = 0
        self.changed_at: datetime | None = None
        self.changed_by: str | None = None
        self.history: list[dict[str, Any]] = []
        self.resume_pending = False
        self.sessions: dict[str, Session] = {}
        self.rejections: Counter[str] = Counter()
        self.last_rejection: dict[str, Any] | None = None
        self.published = 0
        self.mirror: dict[str, Any] = {}
        self._busy = False
        self._timer: CALLBACK_TYPE | None = None
        self._running = False
        self._stopped = False
        self.fallback_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def mgr(self) -> ForecastManager:
        return self.coordinator.forecast

    async def async_load(self) -> None:
        self.fingerprint = await self.hass.async_add_executor_job(fingerprint)
        self.core_fingerprint = await self.hass.async_add_executor_job(core_fingerprint)
        mirror = await self._mirror_store.async_load()
        self.mirror = mirror if isinstance(mirror, dict) else {}
        data = await self._store.async_load()
        if data is None:
            return
        try:
            self._restore(data)
        except (TypeError, ValueError, KeyError) as err:
            _LOGGER.warning("forecast_ownership_invalid error=%s; the integration keeps the forecasts", err)
            epoch = data.get("epoch") if isinstance(data, dict) else None
            self.owner, self.epoch = S.OWNER_INTEGRATION, (epoch + 1 if isinstance(epoch, int) and epoch >= 0 else 1)
            self.owner_instance = self._lease_hash = None
            self.resume_pending = False

    def _restore(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            raise ValueError("not an object")
        epoch = data["epoch"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise ValueError("invalid epoch")
        self.epoch = epoch
        history = data.get("history") or []
        self.history = [h for h in history if isinstance(h, dict)][-S.HISTORY_SIZE:] if isinstance(history, list) else []
        self.changed_at = dt_util.parse_datetime(data["changed_at"]) if isinstance(data.get("changed_at"), str) else None
        self.changed_by = data.get("changed_by") if isinstance(data.get("changed_by"), str) else None
        owner = data.get("owner")
        instance, lease = data.get("owner_instance"), data.get("lease_hash")
        if owner == S.OWNER_APP and S.valid_id(instance) and isinstance(lease, str) and len(lease) == 64:
            self.owner = S.OWNER_APP
            self.owner_instance, self._lease_hash = instance, lease
            versions = data.get("owner_versions")
            self.owner_versions = versions if isinstance(versions, dict) else {}
            self.resume_pending = True
            self.lease_until = dt_util.utcnow() + timedelta(seconds=S.RESUME_GRACE_SECONDS)
        elif owner not in (S.OWNER_INTEGRATION, S.OWNER_APP, None):
            raise ValueError("invalid owner")

    @property
    def app_owns(self) -> bool:
        return self.owner == S.OWNER_APP

    @property
    def busy(self) -> bool:
        return self._busy

    @callback
    def async_prepare_manager(self) -> None:
        """Before the manager starts: dormant when the App owned the forecasts (it is never started); it shows the
        App's last published view meanwhile."""
        if self.app_owns:
            mgr = self.mgr
            mgr.dormant = True
            view = self.mirror.get("view")
            if isinstance(view, dict):
                mgr.apply_published(view)

    @callback
    def async_start(self) -> None:
        self._running = True
        if not self.app_owns:
            self.attach(self.mgr)
        self._schedule_lease()
        if self.resume_pending:
            _LOGGER.info("forecast_resume_pending epoch=%d grace=%ds", self.epoch, S.RESUME_GRACE_SECONDS)

    @callback
    def async_stop(self) -> None:
        self._running = False
        self._stopped = True
        if self._timer:
            self._timer()
            self._timer = None
        self.detach()
        self._broadcast({"type": "closed", "reason": "integration_stopping"})
        self.sessions.clear()

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())
        if self.mirror:
            await self._mirror_store.async_save(self.mirror)

    async def async_remove(self) -> None:
        await self._store.async_remove()
        await self._mirror_store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {
            "owner": self.owner, "epoch": self.epoch, "owner_instance": self.owner_instance,
            "lease_hash": self._lease_hash, "owner_versions": self.owner_versions,
            "changed_at": _iso(self.changed_at), "changed_by": self.changed_by, "history": self.history,
        }

    @callback
    def _save(self) -> None:
        if not self._stopped:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    @callback
    def _save_mirror(self) -> None:
        if not self._stopped:
            self._mirror_store.async_delay_save(lambda: self.mirror, SAVE_DELAY_SECONDS)

    # ------------------------------------------------- the running manager
    @callback
    def attach(self, mgr: ForecastManager) -> None:
        mgr.view_observer = self._on_view
        mgr.settings_observer = self.settings_changed

    @callback
    def detach(self) -> None:
        if self.mgr.view_observer == self._on_view:
            self.mgr.view_observer = None
        if self.mgr.settings_observer == self.settings_changed:
            self.mgr.settings_observer = None

    @callback
    def _on_view(self, view: dict[str, Any]) -> None:
        if self.sessions:
            self._broadcast({"type": "view", "view": view, "fired": dt_util.utcnow().isoformat()})

    @callback
    def settings_changed(self) -> None:
        """The integration's forecast settings were saved (its editors): the App's shadow follows."""
        if self.sessions and not self.app_owns:
            settings = self.mgr.settings.as_dict()
            self._broadcast({"type": "settings", "settings": settings, "settings_revision": S.revision(settings)})

    # ------------------------------------------------------------- reading
    def settings_revision(self) -> str:
        return S.revision(self.mgr.settings.as_dict())

    def state(self) -> str:
        if self.owner == S.OWNER_APP:
            return "resume_pending" if self.resume_pending else S.OWNER_APP
        return S.OWNER_INTEGRATION

    def recorder_oldest(self) -> float | None:
        mgr = self.mgr
        oldest = mgr.oldest_history() if not mgr.dormant else None
        return oldest or mgr.status.recorder_oldest

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
        mgr = self.mgr
        return {
            "protocol": S.PROTOCOL_VERSION, "integration_version": VERSION, "runtime_version": RUNTIME_VERSION,
            "fingerprint": self.fingerprint, "core_fingerprint": self.core_fingerprint, "boot_id": self.boot_id,
            "now": _iso(now), "owner": self.owner, "state": self.state(), "epoch": self.epoch,
            "owner_instance": self.owner_instance[:8] if self.owner_instance else None,
            "owner_connected": self.owner_session in self.sessions if self.owner_session else False,
            "owner_versions": self.owner_versions, "lease_until": _iso(self.lease_until),
            "lease_seconds": S.LEASE_SECONDS, "heartbeat_seconds": S.HEARTBEAT_SECONDS,
            "resume_grace_seconds": S.RESUME_GRACE_SECONDS, "changed_at": _iso(self.changed_at),
            "changed_by": self.changed_by, "last_seq": self.last_seq,
            "settings_revision": self.settings_revision(), "days_learned": len(mgr.days),
            "learned_revision": S.learned_revision({"days": {k: d.as_dict() for k, d in mgr.days.items()},
                                                    "no_data": sorted(mgr.no_data)}),
            "recorder_available": mgr.recorder_available(), "recorder_oldest": self.recorder_oldest(),
            "learning": mgr.status.as_dict(),
            "manager": "failed" if self.fallback_error else "dormant" if mgr.dormant else "running",
            "error": self.fallback_error,
            "sessions": [s.as_dict(self.owner_session) for s in self.sessions.values()],
            "published": self.published, "mirror_at": self.mirror.get("view_at"),
            "learned_at": self.mirror.get("export_at"), "rejections": dict(self.rejections),
            "last_rejection": self.last_rejection, "history": list(reversed(self.history)),
        }

    # ------------------------------------------------------------ sessions
    def _hello(self, session: Session, now: datetime, lease_token: str | None = None) -> dict[str, Any]:
        mgr = self.mgr
        hello: dict[str, Any] = {
            "type": "hello", "session_id": session.session_id, "ownership": self.status(now),
            "options": dict(self.coordinator.config_entry.options), "lease_token": lease_token,
            "settings": mgr.settings.as_dict(), "settings_revision": self.settings_revision(),
            "recorder_oldest": self.recorder_oldest(),
        }
        if not self.app_owns:
            hello["export"] = mgr.export_learning()
            hello["view"] = mgr.view()
        return hello

    async def async_open_session(self, connection: Any, send: Callable[[dict[str, Any]], None],
                                 params: dict[str, Any]) -> tuple[Session, dict[str, Any]]:
        instance = params["instance_id"]
        if not S.valid_id(instance):
            raise BridgeError(S.REJECT_INVALID, "invalid instance id")
        if params["protocol"] != S.PROTOCOL_VERSION:
            raise BridgeError(S.REJECT_VERSION, f"protocol {params['protocol']} is not supported "
                                                f"(this integration speaks {S.PROTOCOL_VERSION})")
        if len(self.sessions) >= MAX_SESSIONS:
            raise BridgeError(S.REJECT_BUSY, "too many bridge sessions")
        now = dt_util.utcnow()
        session = Session(
            session_id=secrets.token_hex(16), instance_id=instance, connection=connection, send=send,
            app_version=params["app_version"], runtime_version=params["runtime_version"],
            fingerprint=params["fingerprint"], core_fingerprint=params["core_fingerprint"],
            protocol=params["protocol"], opened_at=now, last_seen=now,
        )
        lease_token: str | None = None
        if self.owner == S.OWNER_APP and instance != self.owner_instance:
            await self.async_fallback(S.REASON_APP_RESTARTED, detail="a new App instance connected")
        elif self.owner == S.OWNER_APP:
            token = params.get("resume_token")
            lease_valid = self.resume_pending or (self.lease_until is not None and now < self.lease_until)
            if (isinstance(token, str) and S.valid_id(token) and S.token_hash(token) == self._lease_hash
                    and lease_valid and session.fingerprint == self.fingerprint
                    and session.core_fingerprint == self.core_fingerprint and not self._busy):
                lease_token = secrets.token_hex(16)
                self._change_owner(S.OWNER_APP, S.REASON_RESUMED, now, session=session, token=lease_token,
                                   by=self.changed_by)
            else:
                await self.async_fallback(S.REASON_REVOKED, detail="the App could not resume its lease")
        if len(self.sessions) >= MAX_SESSIONS:
            raise BridgeError(S.REJECT_BUSY, "too many bridge sessions")
        self.sessions[session.session_id] = session
        _LOGGER.info("forecast_session_open session=%s instance=%s app=%s owner=%s epoch=%d",
                     session.session_id[:8], instance[:8], session.app_version, self.owner, self.epoch)
        return session, self._hello(session, now, lease_token)

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        if session_id == self.owner_session:
            self.owner_session = None

    def _session(self, session_id: Any, connection: Any) -> Session:
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.connection is not connection:
            raise BridgeError(S.REJECT_UNKNOWN_SESSION, "unknown bridge session")
        session.last_seen = dt_util.utcnow()
        session.messages += 1
        return session

    def _check_owner(self, session: Session, params: dict[str, Any], now: datetime) -> None:
        if self.owner != S.OWNER_APP or session.session_id != self.owner_session or self._busy:
            raise BridgeError(S.REJECT_NOT_OWNER, "this session does not produce the forecasts")
        if params["epoch"] != self.epoch:
            raise BridgeError(S.REJECT_STALE_EPOCH, f"epoch {params['epoch']} is not the current one ({self.epoch})")
        token = params["lease_token"]
        if not S.valid_id(token) or S.token_hash(token) != self._lease_hash:
            raise BridgeError(S.REJECT_LEASE, "invalid lease")
        if self.lease_until is None or now >= self.lease_until:
            raise BridgeError(S.REJECT_LEASE, "the lease has expired")
        if params["seq"] <= self.last_seq:
            raise BridgeError(S.REJECT_SEQUENCE, f"sequence {params['seq']} is not newer than {self.last_seq}")

    def _accept(self, connection: Any, params: dict[str, Any], what: str) -> Session:
        now = dt_util.utcnow()
        try:
            session = self._session(params["session_id"], connection)
            self._check_owner(session, params, now)
        except BridgeError as err:
            self._rejected(err, what)
            raise
        self.last_seq = params["seq"]
        self.lease_until = now + timedelta(seconds=S.LEASE_SECONDS)
        self._schedule_lease()
        return session

    def _rejected(self, err: BridgeError, what: str) -> None:
        self.rejections[err.code] += 1
        self.last_rejection = {"at": _iso(dt_util.utcnow()), "message": what, "code": err.code, "detail": str(err)}

    # ------------------------------------------------------------ handover
    async def async_claim(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """A reviewed handover: export the learned data, stop the integration's manager, hand over."""
        session = self._session(params["session_id"], connection)
        if self.owner != S.OWNER_INTEGRATION or self._busy:
            raise BridgeError(S.REJECT_BUSY, "the App already produces the forecasts (or a change is in progress)")
        if self.fallback_error:
            raise BridgeError(S.REJECT_BUSY, self.fallback_error)
        if params["expected_epoch"] != self.epoch:
            raise BridgeError(S.REJECT_BUSY, f"the ownership changed (epoch {self.epoch})")
        if session.fingerprint != self.fingerprint or session.core_fingerprint != self.core_fingerprint:
            raise BridgeError(S.REJECT_VERSION, "the App runs different forecast code; update the App and the "
                                                "integration to matching versions")
        if params["settings_revision"] != self.settings_revision():
            raise BridgeError(S.REJECT_CONFIG, "the App's copy of the forecast settings is not the current one")
        readiness = _readiness(params.get("readiness"))
        if not readiness["ready"]:
            raise BridgeError(S.REJECT_NOT_READY, "the App reports that it is not ready for a handover")
        mgr = self.mgr
        self._busy = True
        try:
            # A learning run in progress finishes first (bounded by the lock); it never touches a device.
            async with mgr._lock:  # noqa: SLF001
                pass
            await mgr.async_flush()
            if self.sessions.get(session.session_id) is not session or not self._running:
                raise BridgeError(S.REJECT_UNKNOWN_SESSION, "the bridge session ended during the handover")
            export = mgr.export_learning()
            view = mgr.view()
            self.detach()
            mgr.async_stop()
            mgr.dormant = True
            now = dt_util.utcnow()
            token = secrets.token_hex(16)
            by = params.get("requested_by") if isinstance(params.get("requested_by"), str) else user_id
            self.mirror = {"export": export, "export_at": _iso(now), "view": view, "view_at": _iso(now),
                           "epoch": self.epoch + 1}
            self._save_mirror()
            self._change_owner(S.OWNER_APP, S.REASON_HANDOVER, now, session=session, token=token, by=by,
                               readiness=readiness)
        finally:
            self._busy = False
        _LOGGER.info("forecast_handover epoch=%d by=%s days=%d", self.epoch, by, len(export["days"]))
        return {
            "epoch": self.epoch, "lease_token": token, "lease_until": _iso(self.lease_until),
            "lease_seconds": S.LEASE_SECONDS, "heartbeat_seconds": S.HEARTBEAT_SECONDS,
            "export": export, "learned_revision": S.learned_revision(export), "view": view,
        }

    # ------------------------------------------------------------ the owner
    @callback
    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._accept(connection, params, "heartbeat")
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until), "epoch": self.epoch}

    def _validate_publish(self, params: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        view = export = None
        try:
            if params.get("view") is not None:
                view = S.check_view(params["view"])
            if params.get("export") is not None:
                export = S.check_export(params["export"])
        except S.ProtocolError as err:
            raise BridgeError(err.code, str(err)) from err
        return view, export

    def _apply_publish(self, view: dict[str, Any] | None, export: dict[str, Any] | None) -> None:
        now = dt_util.utcnow()
        mgr = self.mgr
        if view is not None:
            self.mirror = {**self.mirror, "view": view, "view_at": _iso(now), "epoch": self.epoch}
            mgr.apply_published(view)
            self.published += 1
        if export is not None:
            self.mirror = {**self.mirror, "export": export, "export_at": _iso(now)}
            # The dormant manager keeps the App's learned days (its read-only views and the rollback copy).
            self.hass.async_create_task(self._async_adopt_export(mgr, export), "smart_occupancy forecast mirror")
        self._save_mirror()

    async def _async_adopt_export(self, mgr: ForecastManager, export: dict[str, Any]) -> None:
        if not mgr.dormant:
            return
        try:
            await mgr.async_import_learning(export, save=True)
            await mgr._async_build_model(log_changes=False)  # noqa: SLF001 - the read-only patterns follow
        except Exception:  # noqa: BLE001 - the mirror is diagnostics and a rollback copy; never raise
            _LOGGER.debug("forecast_mirror_adopt_failed", exc_info=True)

    @callback
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "publish")
            raise
        self._accept(connection, params, "publish")
        self._apply_publish(*parsed)
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until)}

    async def async_release(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "release")
            raise
        self._accept(connection, params, "release")
        view, export = parsed
        if view is not None:
            self.mirror = {**self.mirror, "view": view, "view_at": _iso(dt_util.utcnow())}
        if export is not None:
            self.mirror = {**self.mirror, "export": export, "export_at": _iso(dt_util.utcnow())}
        await self.async_fallback(S.REASON_RELEASE, by=user_id, detail=params.get("detail"))
        return {"ownership": self.status()}

    # -------------------------------------------------------- owner change
    @callback
    def _schedule_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> None:
        if self.owner != S.OWNER_APP:
            return
        self._change_owner(S.OWNER_INTEGRATION, reason, dt_util.utcnow(), by=by, detail=detail)
        self.hass.async_create_task(self._async_start_manager(reason), "smart_occupancy forecast fallback")

    async def async_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> bool:
        if self.owner != S.OWNER_APP:
            return False
        self._change_owner(S.OWNER_INTEGRATION, reason, dt_util.utcnow(), by=by, detail=detail)
        await self._async_start_manager(reason)
        return True

    async def _async_start_manager(self, reason: str) -> None:
        """The integration's manager learns and publishes again, from the App's last learned data."""
        self._busy = True
        try:
            mgr = self.mgr
            export = self.mirror.get("export")
            try:
                if isinstance(export, dict):
                    await mgr.async_import_learning(S.check_export(export), save=True)
            except Exception:  # noqa: BLE001 - the integration's own stored days are the fallback
                _LOGGER.exception("forecast_fallback_import_failed (the integration starts from its stored days)")
            mgr.dormant = False
            try:
                mgr.async_start()
            except Exception:
                _LOGGER.exception("forecast_fallback_start_failed: forecasts do not run; reload the integration")
                self.fallback_error = "the forecast manager could not be started; reload the integration"
                return
            self.attach(mgr)
            self.mirror = {**self.mirror, "returned_at": _iso(dt_util.utcnow())}
            self._save_mirror()
        finally:
            self._busy = False
        level = logging.INFO if reason in (S.REASON_RELEASE, S.REASON_SERVICE) else logging.WARNING
        _LOGGER.log(level, "forecast_fallback reason=%s epoch=%d", reason, self.epoch)

    def _change_owner(self, owner: str, reason: str, now: datetime, *, session: Session | None = None,
                      token: str | None = None, by: str | None = None, detail: str | None = None,
                      readiness: dict[str, Any] | None = None) -> None:
        self.epoch += 1
        self.owner = owner
        self.last_seq = 0
        self.resume_pending = False
        self.changed_at = now
        self.changed_by = by
        if owner == S.OWNER_APP:
            assert session is not None and token is not None
            self.owner_instance = session.instance_id
            self.owner_session = session.session_id
            self._lease_hash = S.token_hash(token)
            self.lease_until = now + timedelta(seconds=S.LEASE_SECONDS)
            self.owner_versions = {"app": session.app_version, "runtime": session.runtime_version,
                                   "fingerprint": session.fingerprint}
        else:
            self.owner_instance = self.owner_session = self._lease_hash = None
            self.lease_until = None
            self.owner_versions = {}
        entry: dict[str, Any] = {"epoch": self.epoch, "owner": owner, "reason": reason, "at": _iso(now), "by": by,
                                 "instance": session.instance_id[:8] if session else None}
        if detail:
            entry["detail"] = str(detail)[:200]
        if readiness is not None:
            entry["readiness"] = readiness
        self.history = [*self.history, entry][-S.HISTORY_SIZE:]
        self._schedule_lease()
        self._save()
        self._broadcast({"type": "ownership", "ownership": self.status(now)})

    def _schedule_lease(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        if self._running and self.owner == S.OWNER_APP and self.lease_until is not None:
            self._timer = async_track_point_in_utc_time(self.hass, self._lease_check, self.lease_until)

    @callback
    def _lease_check(self, _now: datetime) -> None:
        self._timer = None
        if self.owner != S.OWNER_APP or self.lease_until is None:
            return
        if dt_util.utcnow() < self.lease_until:
            self._schedule_lease()
            return
        if self.resume_pending:
            self._schedule_fallback(S.REASON_NOT_RESUMED, detail="the App did not reconnect after the restart")
        else:
            self._schedule_fallback(S.REASON_LEASE_EXPIRED, detail="no heartbeat from the App within the lease")

    def _broadcast(self, message: dict[str, Any]) -> None:
        for session in list(self.sessions.values()):
            try:
                session.send(message)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("forecast_send_failed session=%s", session.session_id[:8])
