"""Curtain ownership (Phase A5): the one authority over which curtain manager commands the covers.

The Smart Lighting App runs this integration's own curtain runtime (a
byte-identical copy of the ``curtains`` package, ``runtime_manifest.py``). This
module decides which of the two managers may send a cover command - every path:
automatic curtains and confirmed manual presets. Exactly one manager runs at a
time; curtains are a physical-device control domain of their own, with their
own ownership record, lease and sequence numbers (independent of lighting).

* **Integration owns** (the default, also after every install or upgrade): the
  integration's manager runs as before. App bridge sessions get a copy of its
  configuration and runtime state when they open and every cover command,
  automatic decision and configuration change it makes (the App's
  non-commanding shadow compares itself with them).
* **App owns**: the integration's manager is *dormant* - loaded, not running,
  fenced (it cannot send anything) and never saving. Its configuration store is a
  mirror of the App's (written only by the App's ``publish``) and the curtain
  editors refuse changes. The App's commands reach a cover only through
  :meth:`CurtainBridge.async_execute`, which accepts a command only from the
  owner's session on the same connection, with the current epoch, the lease
  token, a strictly increasing sequence number and an allowlisted cover service
  for a configured cover (``curtain_sync.check_cover_command``).

Ownership record (``.storage/smart_occupancy.curtains_ownership``): owner,
epoch (increases with every change), owner instance, lease token digest,
versions and a bounded history. The App's published state is kept in
``.storage/smart_occupancy.curtains_mirror``.

Handover (``claim``, reviewed in the App): the integration's manager is fenced;
if a cover of a configured window is still being moved the claim waits a little
and is otherwise refused (a movement is never contradicted or replayed); its
configuration and runtime state are exported and it stops. The App starts a new
manager from exactly that copy; its start applies nothing (the start-up settle
period, then the normal rules).

Fallback (the App releases, an administrator calls
``curtains_return_to_integration``, the lease expires, another App instance
connects, or the App does not resume after Home Assistant restarted): the epoch
increases first (every later App command is refused), in-flight App commands
finish (bounded), then a new integration manager starts from the mirror (the
App's configuration and last state: manual holds, applied presets, move budgets,
deferred changes, calibration and history), again applying nothing: an operation
the App left running is marked interrupted, its covers' reports are still "ours"
for a settle window, and the windows are evaluated again only after the start-up
settle period. A recovered App never takes ownership back by itself; only the
same App process may resume within the grace period after Home Assistant
restarted or the integration reloaded.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import curtain_sync as S
from .const import DOMAIN, VERSION
from .curtains.config import CurtainConfig
from .curtains.manager import SIGNAL_CURTAINS_UPDATED, CurtainManager
from .occupancy_core import fingerprint as core_fingerprint
from .runtime_manifest import RUNTIME_VERSION, fingerprint

if TYPE_CHECKING:
    from .coordinator import SmartOccupancyCoordinator

_LOGGER = logging.getLogger(__name__)

OWNERSHIP_STORE_KEY = f"{DOMAIN}.curtains_ownership"
MIRROR_STORE_KEY = f"{DOMAIN}.curtains_mirror"
STORE_VERSION = 1
SAVE_DELAY_SECONDS = 5
MAX_RENAMES = 50
MAX_SESSIONS = 4
RECENT_COMMANDS = 50
#: Curtain websocket commands that keep answering while the App owns curtains: they only read or simulate.
READ_ONLY_COMMANDS = frozenset({
    "smart_occupancy/curtains/model", "smart_occupancy/curtains/subscribe", "smart_occupancy/curtains/preview",
    "smart_occupancy/curtains/auto/preview", "smart_occupancy/curtains/auto/simulate",
})
MANAGED_BY_APP = "managed_by_app"
MANAGED_TEXT = ("Curtains are managed by the Smart Lighting App: change them there (Curtains). This editor is "
                "read-only until control is returned to the integration.")


class BridgeError(Exception):
    """A bridge request was refused; nothing changed."""

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
            "runtime_version": self.runtime_version, "fingerprint": self.fingerprint,
            "core_fingerprint": self.core_fingerprint, "protocol": self.protocol,
            "opened_at": _iso(self.opened_at), "last_seen": _iso(self.last_seen), "messages": self.messages,
            "owner": self.session_id == owner_session,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _readiness(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"ready": False, "checks": []}
    checks = []
    for item in value.get("checks") or []:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            checks.append({"key": item["key"][:64], "status": str(item.get("status"))[:16]})
    return {"ready": value.get("ready") is True, "checks": checks[:30],
            "summary": value.get("summary") if isinstance(value.get("summary"), dict) else None}


class CurtainBridge:
    """Ownership of curtain control, the App's bridge sessions and the mirror of the App's curtains."""

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
        self.executed = 0
        self.execute_errors = 0
        self.last_execute: dict[str, Any] | None = None
        self.mirror: dict[str, Any] = {}
        self._recent: deque[tuple[float, str]] = deque(maxlen=RECENT_COMMANDS)
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = False
        self._timer: CALLBACK_TYPE | None = None
        self._running = False
        self._stopped = False
        self.fallback_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def mgr(self) -> CurtainManager:
        return self.coordinator.curtains

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
            _LOGGER.warning("curtains_ownership_invalid error=%s; the integration keeps the curtains", err)
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
        """A handover or fallback is in progress (curtain editors wait)."""
        return self._busy

    @callback
    def async_prepare_manager(self) -> None:
        """Before the manager starts: a dormant one when the App owned curtains (it is never started)."""
        if self.app_owns:
            self._make_dormant(self.mgr)

    @callback
    def async_start(self) -> None:
        self._running = True
        if not self.app_owns:
            self.attach(self.mgr)
        self._schedule_lease()
        if self.resume_pending:
            _LOGGER.info("curtains_resume_pending epoch=%d grace=%ds (the App controlled the curtains before the "
                         "restart; the integration's curtains stay dormant meanwhile)", self.epoch,
                         S.RESUME_GRACE_SECONDS)

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
    def attach(self, mgr: CurtainManager) -> None:
        """Stream what the integration's manager does to the App's sessions (its shadow compares)."""
        self.detach()
        mgr.command_observer = self._on_command
        mgr.decision_observer = self._on_decision
        mgr.config_observer = self._on_config

    @callback
    def detach(self) -> None:
        mgr = self.coordinator.curtains
        if mgr.command_observer == self._on_command:
            mgr.command_observer = None
        if mgr.decision_observer == self._on_decision:
            mgr.decision_observer = None
        if mgr.config_observer == self._on_config:
            mgr.config_observer = None

    @staticmethod
    def _make_dormant(mgr: CurtainManager) -> None:
        mgr.fenced = True
        mgr.dormant = True
        mgr.locked = True
        mgr.command_observer = None
        mgr.decision_observer = None
        mgr.config_observer = None
        mgr.state_observer = None

    @callback
    def _on_command(self, record: dict[str, Any]) -> None:
        if self.sessions:
            self._broadcast({"type": "command", "command": record})

    @callback
    def _on_decision(self, window_id: str, decision: dict[str, Any]) -> None:
        if self.sessions:
            self._broadcast({"type": "decision", "window_id": window_id, "decision": decision})

    @callback
    def _on_config(self, new: CurtainConfig) -> None:
        if self.sessions:
            doc = new.as_dict()
            self._broadcast({"type": "config", "config": doc, "config_revision": S.config_revision(doc),
                             "fired": dt_util.utcnow().isoformat()})

    # ------------------------------------------------------------- reading
    def config_doc(self) -> dict[str, Any]:
        return self.mgr.config.as_dict()

    def config_revision(self) -> str:
        return S.config_revision(self.config_doc())

    def state(self) -> str:
        if self.owner == S.OWNER_APP:
            return "resume_pending" if self.resume_pending else S.OWNER_APP
        return S.OWNER_INTEGRATION

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
            "changed_by": self.changed_by, "last_seq": self.last_seq, "config_revision": self.config_revision(),
            "manager": "failed" if self.fallback_error else "dormant" if mgr.dormant else "running",
            "moving": sorted(w for w in mgr.config.windows if mgr.running(w) is not None),
            "windows": len(mgr.config.windows),
            "error": self.fallback_error,
            "sessions": [s.as_dict(self.owner_session) for s in self.sessions.values()],
            "executed": self.executed, "execute_errors": self.execute_errors, "last_execute": self.last_execute,
            "mirror_at": self.mirror.get("state_at"), "rejections": dict(self.rejections),
            "last_rejection": self.last_rejection, "history": list(reversed(self.history)),
        }

    # ------------------------------------------------------------ sessions
    def _hello(self, session: Session, now: datetime, lease_token: str | None = None) -> dict[str, Any]:
        mgr = self.mgr
        hello: dict[str, Any] = {
            "type": "hello", "session_id": session.session_id, "ownership": self.status(now),
            "config": self.config_doc(), "config_revision": self.config_revision(), "lease_token": lease_token,
        }
        if lease_token is not None:
            hello["renames"] = self._take_renames()
        if not self.app_owns:
            hello["state"] = mgr.export_state()
            hello["running"] = mgr._started  # noqa: SLF001
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
        _LOGGER.info("curtains_session_open session=%s instance=%s app=%s owner=%s epoch=%d",
                     session.session_id[:8], instance[:8], session.app_version, self.owner, self.epoch)
        return session, self._hello(session, now, lease_token)

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        _LOGGER.info("curtains_session_closed session=%s owner=%s", session_id[:8], session_id == self.owner_session)
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
            raise BridgeError(S.REJECT_NOT_OWNER, "this session does not control the curtains")
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
        _LOGGER.debug("curtains_rejected message=%s code=%s detail=%s", what, err.code, err)

    # ------------------------------------------------------------ handover
    async def async_claim(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """A reviewed handover to the App: fence, wait for movements to end, stop the manager; hand over its copy."""
        session = self._session(params["session_id"], connection)
        if self.owner != S.OWNER_INTEGRATION or self._busy:
            raise BridgeError(S.REJECT_BUSY, "the App already controls the curtains (or a change is in progress)")
        if self.fallback_error:
            raise BridgeError(S.REJECT_BUSY, self.fallback_error)
        if params["expected_epoch"] != self.epoch:
            raise BridgeError(S.REJECT_BUSY, f"the ownership changed (epoch {self.epoch})")
        if session.fingerprint != self.fingerprint or session.core_fingerprint != self.core_fingerprint:
            raise BridgeError(S.REJECT_VERSION, "the App runs different curtain code; update the App and the "
                                                "integration to matching versions")
        if params["config_revision"] != self.config_revision():
            raise BridgeError(S.REJECT_CONFIG, "the App's copy of the curtain configuration is not the current one")
        readiness = _readiness(params.get("readiness"))
        if not readiness["ready"]:
            raise BridgeError(S.REJECT_NOT_READY, "the App reports that it is not ready for a handover")
        mgr = self.mgr
        self._busy = True
        try:
            mgr.fenced = True
            try:
                if not await mgr.async_drain(S.MOVING_WAIT_SECONDS):
                    moving = sorted(w for w in mgr.config.windows if mgr.running(w) is not None)
                    raise BridgeError(S.REJECT_BUSY, "a cover is still being moved (" + ", ".join(
                        mgr.config.windows[w].name for w in moving) + "); wait until it has reached its position "
                        "and try again")
                await mgr.async_flush()
                if self.sessions.get(session.session_id) is not session or not self._running:
                    raise BridgeError(S.REJECT_UNKNOWN_SESSION, "the bridge session ended during the handover")
            except BaseException:
                mgr.fenced = False  # the integration keeps control
                raise
            # From here on nothing awaits: no edit or command can come between the copy and the switch.
            doc = mgr.config.as_dict()
            state = mgr.export_state()
            self.detach()
            mgr.async_stop()
            self._make_dormant(mgr)
            now = dt_util.utcnow()
            token = secrets.token_hex(16)
            by = params.get("requested_by") if isinstance(params.get("requested_by"), str) else user_id
            self.mirror = {"state": state, "state_at": _iso(now), "epoch": self.epoch + 1}
            self._save_mirror()
            self._change_owner(S.OWNER_APP, S.REASON_HANDOVER, now, session=session, token=token, by=by,
                               readiness=readiness)
        finally:
            self._busy = False
        mgr.notify()
        _LOGGER.info("curtains_handover epoch=%d by=%s", self.epoch, by)
        return {
            "epoch": self.epoch, "lease_token": token, "lease_until": _iso(self.lease_until),
            "lease_seconds": S.LEASE_SECONDS, "heartbeat_seconds": S.HEARTBEAT_SECONDS,
            "config": doc, "config_revision": S.config_revision(doc), "state": state,
        }

    # ------------------------------------------------------------ the owner
    async def async_execute(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """One cover command of the App's manager: fenced, allowlisted, sent with the App's context."""
        self._accept(connection, params, "execute")
        domain, service, data = params["domain"], params["service"], params["data"]
        try:
            ids = S.check_cover_command(domain, service, data, covers=S.covers_of(self.config_doc()))
        except S.ProtocolError as err:
            error = BridgeError(err.code, str(err))
            self._rejected(error, "execute")
            raise error from err
        context_id = params["context_id"]
        if not S.valid_context_id(context_id):
            error = BridgeError(S.REJECT_INVALID, "invalid context id")
            self._rejected(error, "execute")
            raise error
        self._recent.append((time.monotonic(), context_id))
        self._inflight += 1
        self._idle.clear()
        record = {"at": _iso(dt_util.utcnow()), "domain": domain, "service": service, "entities": ids,
                  "position": data.get("position"), "purpose": params.get("purpose"),
                  "window_id": params.get("window_id"), "seq": params["seq"]}
        try:
            async with asyncio.timeout(S.COMMAND_SECONDS):
                await self.hass.services.async_call(domain, service, dict(data), blocking=True,
                                                    context=Context(id=context_id))
        except TimeoutError as err:
            self.execute_errors += 1
            self.last_execute = {**record, "error": "timeout"}
            raise BridgeError(S.REJECT_FAILED, "no response within timeout") from err
        except (HomeAssistantError, ValueError) as err:
            self.execute_errors += 1
            self.last_execute = {**record, "error": str(err)[:200]}
            raise BridgeError(S.REJECT_FAILED, str(err) or type(err).__name__) from err
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle.set()
        self.executed += 1
        self.last_execute = record
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until)}

    @callback
    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._accept(connection, params, "heartbeat")
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until), "epoch": self.epoch}

    def _validate_publish(self, params: dict[str, Any]) -> tuple[CurtainConfig | None, dict[str, Any] | None]:
        config = None
        if params.get("config") is not None:
            try:
                if len(S.canonical(params["config"])) > S.MAX_CONFIG_BYTES:
                    raise ValueError("configuration too large")
                config = CurtainConfig.from_dict(params["config"])
            except (TypeError, ValueError, KeyError, AttributeError) as err:
                raise BridgeError(S.REJECT_INVALID, f"invalid curtain configuration: {err}") from err
            if S.config_revision(config.as_dict()) != params.get("config_revision"):
                raise BridgeError(S.REJECT_CONFIG, "the configuration does not match its revision")
        state = None
        if params.get("state") is not None:
            try:
                state = S.check_state(params["state"])
            except S.ProtocolError as err:
                raise BridgeError(err.code, str(err)) from err
        return config, state

    def _apply_publish(self, config: CurtainConfig | None, state: dict[str, Any] | None) -> None:
        now = dt_util.utcnow()
        if config is not None:
            self.mgr.async_set_mirrored(config)
        if state is not None:
            self.mirror = {**self.mirror, "state": state, "state_at": _iso(now), "epoch": self.epoch}
            self._save_mirror()
            self.mgr.notify()

    @callback
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """The App's configuration and runtime state (the mirror). All or nothing."""
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "publish")
            raise
        self._accept(connection, params, "publish")
        self._apply_publish(*parsed)
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until)}

    async def async_release(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """The App returns control (with its final configuration and state)."""
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "release")
            raise
        self._accept(connection, params, "release")
        self._apply_publish(*parsed)
        await self.async_fallback(S.REASON_RELEASE, by=user_id, detail=params.get("detail"))
        return {"ownership": self.status()}

    # -------------------------------------------------------- owner change
    @callback
    def _schedule_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> None:
        if self.owner != S.OWNER_APP:
            return
        self._change_owner(S.OWNER_INTEGRATION, reason, dt_util.utcnow(), by=by, detail=detail)
        self.hass.async_create_task(self._async_start_manager(reason), "smart_occupancy curtains fallback")

    async def async_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> bool:
        """The integration takes the curtains back. Returns False when it already controls them."""
        if self.owner != S.OWNER_APP:
            return False
        self._change_owner(S.OWNER_INTEGRATION, reason, dt_util.utcnow(), by=by, detail=detail)
        await self._async_start_manager(reason)
        return True

    async def _async_start_manager(self, reason: str) -> None:
        """A new integration manager from the mirror (the App's configuration and last state); applies nothing."""
        self._busy = True
        try:
            try:
                async with asyncio.timeout(S.COMMAND_SECONDS + 1):
                    await self._idle.wait()
            except TimeoutError:
                _LOGGER.warning("curtains_fallback_drain_timeout: App commands still running")
            old = self.mgr
            try:
                self._apply_renames(old)
                state = self.mirror.get("state")
                if isinstance(state, dict):
                    await old.async_import_state(state)
                await old.async_flush()
            except Exception:
                _LOGGER.exception("curtains_fallback_mirror_failed (the integration starts from its stored copy)")
            old.dormant = False
            old.async_stop()
            new = CurtainManager(self.hass, self.coordinator.occupancy)
            try:
                await new.async_load()
            except Exception:
                _LOGGER.exception("curtains_fallback_load_failed: curtains do not run; reload the integration")
                self._failed_fallback(old)
                return
            self.coordinator.curtains = new
            new.async_start()
            self.attach(new)
            self.mirror = {**self.mirror, "renames": [], "returned_at": _iso(dt_util.utcnow())}
            self._save_mirror()
        finally:
            self._busy = False
        self.coordinator.curtains.notify()
        level = logging.INFO if reason in (S.REASON_RELEASE, S.REASON_SERVICE) else logging.WARNING
        _LOGGER.log(level, "curtains_fallback reason=%s epoch=%d", reason, self.epoch)

    @callback
    def _failed_fallback(self, old: CurtainManager) -> None:
        old.fenced = True
        self.fallback_error = "the curtain manager could not be started; reload the integration"
        old.notify()

    # -------------------------------------------------------------- renames
    @callback
    def note_rename(self, old: str, new: str) -> None:
        """An entity id renamed while the App owns curtains but is not connected: given to it when it resumes
        (or applied here when the integration takes the curtains back)."""
        if not self.app_owns or (self.owner_session is not None and self.owner_session in self.sessions):
            return
        renames = [*(self.mirror.get("renames") or []), [old, new]][-MAX_RENAMES:]
        self.mirror = {**self.mirror, "renames": renames}
        self._save_mirror()

    def _take_renames(self) -> list[list[str]]:
        renames = [r for r in self.mirror.get("renames") or [] if isinstance(r, list) and len(r) == 2]
        if renames:
            self.mirror = {**self.mirror, "renames": []}
            self._save_mirror()
        return renames

    def _apply_renames(self, mgr: CurtainManager) -> None:
        from dataclasses import replace  # noqa: PLC0415

        for old, new in self._take_renames():
            windows = {}
            changed = False
            for window_id, window in mgr.config.windows.items():
                if old in window.entity_ids:
                    window = replace(window, layers=tuple(replace(x, entity_id=new) if x.entity_id == old else x
                                                          for x in window.layers))
                    changed = True
                windows[window_id] = window
            if changed:
                _LOGGER.info("entity_renamed old=%s new=%s (while the App was away)", old, new)
                mgr.async_set_mirrored(replace(mgr.config, windows=windows))

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

    # ---------------------------------------------------------- streaming
    def _broadcast(self, message: dict[str, Any]) -> None:
        for session in list(self.sessions.values()):
            try:
                session.send(message)
            except Exception:  # noqa: BLE001 - a broken connection must not break curtains
                _LOGGER.debug("curtains_send_failed session=%s", session.session_id[:8])


__all__ = ["BridgeError", "CurtainBridge", "MANAGED_BY_APP", "MANAGED_TEXT", "READ_ONLY_COMMANDS",
           "SIGNAL_CURTAINS_UPDATED"]
