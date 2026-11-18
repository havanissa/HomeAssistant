"""Lighting ownership (Phase A4): the one authority over which controller commands lights.

The Smart Lighting App runs this integration's own lighting runtime (a
byte-identical copy, ``runtime_manifest.py``). This module decides which of the
two controllers commands lights - every path: automatic lighting, vacancy,
possible occupancy, wall switches, profile tests, Room Supervisor corrections,
Priority Inputs, global adjustment. Exactly one controller runs at a time:

* **Integration owns** (the default, also after every install or upgrade): the
  integration's controller runs as before. App bridge sessions get a copy of
  its configuration and state when they open and every command and decision it
  makes (the App's non-commanding shadow compares itself with them).
* **App owns**: the integration's controller is *dormant* - loaded, not
  running, fenced (it cannot send anything) and never saving. Its configuration
  store is a mirror of the App's (written only by the App's ``publish``) and its
  lighting editors refuse changes. The App's commands reach a device only
  through :meth:`LightingBridge.async_execute`, which accepts a command only from
  the owner's session on the same connection, with the current epoch, the lease
  token, a strictly increasing sequence number and an allowlisted service for a
  configured target (``lighting_sync.check_command``).

Ownership record (``.storage/smart_occupancy.lighting_ownership``): owner,
epoch (increases with every change), owner instance, lease token digest,
versions and a bounded history. The App's published state is kept in
``.storage/smart_occupancy.lighting_mirror``.

Handover (``claim``, reviewed in the App): the integration's controller is
fenced, its in-flight commands finish (bounded), its configuration and state are
exported and it stops. The App starts a new controller from exactly that copy;
start takes the current mood, occupancy and lights as baseline and applies
nothing.

Fallback (the App releases, an administrator calls
``lighting_return_to_integration``, the lease expires, another App instance
connects, or the App does not resume after Home Assistant restarted): the epoch
increases first (every later App command is refused), in-flight App commands
finish (bounded), then a new integration controller starts from the mirror (the
App's last configuration and state, with this integration's own forecast
decisions kept), again applying nothing. A recovered App never takes ownership
back by itself; only the same App process may resume within the grace period
after Home Assistant restarted or the integration reloaded.
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
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import lighting_sync as S
from .const import COMMAND_TIMEOUT_SECONDS, DOMAIN, SIGNAL_LIGHTING_UPDATED, VERSION
from .lighting_config import LightingConfig
from .occupancy_core import fingerprint as core_fingerprint
from .runtime_manifest import RUNTIME_VERSION, fingerprint

if TYPE_CHECKING:
    from .controller import LightingController
    from .coordinator import SmartOccupancyCoordinator

_LOGGER = logging.getLogger(__name__)

OWNERSHIP_STORE_KEY = f"{DOMAIN}.lighting_ownership"
MIRROR_STORE_KEY = f"{DOMAIN}.lighting_mirror"
STORE_VERSION = 1
SAVE_DELAY_SECONDS = 5
MAX_RENAMES = 50
MAX_SESSIONS = 4
RECENT_COMMANDS = 50
#: Lighting websocket commands that keep answering while the App owns lighting: they only read or simulate.
READ_ONLY_COMMANDS = frozenset({
    "smart_occupancy/lighting/editor", "smart_occupancy/lighting/subscribe", "smart_occupancy/lighting/preview",
    "smart_occupancy/lighting/room/activation", "smart_occupancy/lighting/brightness/preview",
    "smart_occupancy/lighting/priority/preview", "smart_occupancy/lighting/zone/radar_test",
    "smart_occupancy/lighting/switch/candidates", "smart_occupancy/lighting/supervisor/room",
})
MANAGED_BY_APP = "managed_by_app"
MANAGED_TEXT = ("Lighting is managed by the Smart Lighting App: change it there (Lighting, Moods and Switches). "
                "This editor is read-only until control is returned to the integration.")


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


class LightingBridge:
    """Ownership of lighting control, the App's bridge sessions and the mirror of the App's lighting."""

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
        # The App's published state and per-room view (the mirror), and its recent command contexts.
        self.mirror: dict[str, Any] = {}
        self._recent: deque[tuple[float, str]] = deque(maxlen=RECENT_COMMANDS)
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._busy = False
        self._timer: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._running = False
        self._stopped = False
        self.fallback_error: str | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def ctrl(self) -> LightingController:
        return self.coordinator.lighting

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
            # A damaged record: the integration keeps lighting (the safe owner); the epoch only grows.
            _LOGGER.warning("lighting_ownership_invalid error=%s; the integration keeps lighting", err)
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
            # Home Assistant restarted or the integration reloaded while the App controlled lighting: the
            # controller stays dormant; the same App process may resume within the grace period.
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
        """A handover or fallback is in progress (lighting editors wait)."""
        return self._busy

    @callback
    def async_prepare_controller(self) -> None:
        """Before the controller starts: a dormant one when the App owned lighting (it is never started)."""
        if self.app_owns:
            self._make_dormant(self.ctrl)

    @callback
    def async_start(self) -> None:
        """After the controller was loaded (and started, when the integration owns lighting)."""
        self._running = True
        if not self.app_owns:
            self.attach(self.ctrl)
        self._schedule_lease()
        if self.resume_pending:
            _LOGGER.info("lighting_resume_pending epoch=%d grace=%ds (the App controlled lighting before the "
                         "restart; the integration's lighting stays dormant meanwhile)", self.epoch,
                         S.RESUME_GRACE_SECONDS)

    @callback
    def async_stop(self) -> None:
        self._running = False
        self._stopped = True  # a later (delayed) save could overwrite the next bridge's record
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

    # ------------------------------------------------- the running controller
    @callback
    def attach(self, ctrl: LightingController) -> None:
        """Stream what the integration's controller does to the App's sessions (its shadow compares)."""
        self.detach()
        ctrl.command_observer = self._on_command
        ctrl.decisions.observer = self._on_decision
        self._unsubs.append(ctrl.store.async_add_listener(self._on_config))

    @callback
    def detach(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @staticmethod
    def _make_dormant(ctrl: LightingController) -> None:
        ctrl.fenced = True
        ctrl.dormant = True
        ctrl.store.locked = True  # edits go to the App; only the App's published copy is written here
        ctrl.command_observer = None
        ctrl.decisions.observer = None

    @callback
    def _on_command(self, record: dict[str, Any]) -> None:
        if self.sessions:
            self._broadcast({"type": "command", "command": record})

    @callback
    def _on_decision(self, subject: str, decision: dict[str, Any]) -> None:
        if self.sessions:
            self._broadcast({"type": "decision", "subject": subject, "decision": decision})

    @callback
    def _on_config(self, _old: LightingConfig, new: LightingConfig) -> None:
        if self.sessions:
            doc = new.as_dict()
            # fired: full precision, so the App's shadow starts its timers from this instant (as for state events).
            self._broadcast({"type": "config", "config": doc, "config_revision": S.config_revision(doc),
                             "fired": dt_util.utcnow().isoformat()})

    # ------------------------------------------------------------- reading
    def config_doc(self) -> dict[str, Any]:
        return self.ctrl.lighting.as_dict()

    def config_revision(self) -> str:
        return S.config_revision(self.config_doc())

    def state(self) -> str:
        if self.owner == S.OWNER_APP:
            return "resume_pending" if self.resume_pending else S.OWNER_APP
        return S.OWNER_INTEGRATION

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
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
            "controller": "failed" if self.fallback_error else "dormant" if self.ctrl.dormant else "running",
            "error": self.fallback_error,
            "sessions": [s.as_dict(self.owner_session) for s in self.sessions.values()],
            "executed": self.executed, "execute_errors": self.execute_errors, "last_execute": self.last_execute,
            "mirror_at": self.mirror.get("state_at"), "rejections": dict(self.rejections),
            "last_rejection": self.last_rejection, "history": list(reversed(self.history)),
        }

    # ------------------------------------------------------------ sessions
    def _hello(self, session: Session, now: datetime, lease_token: str | None = None) -> dict[str, Any]:
        ctrl = self.ctrl
        entry = self.coordinator.config_entry
        hello: dict[str, Any] = {
            "type": "hello", "session_id": session.session_id, "ownership": self.status(now),
            "options": dict(entry.options) if entry is not None else {},
            "config": self.config_doc(), "config_revision": self.config_revision(), "lease_token": lease_token,
        }
        if lease_token is not None:
            hello["renames"] = self._take_renames()
        if not self.app_owns:
            # The shadow starts from an exact copy of the running controller.
            hello["state"] = ctrl.export_state()
            hello["recent"] = ctrl.recent_commands()
            hello["running"] = ctrl._running  # noqa: SLF001 - start-up baseline taken
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
            # Another App process: the one that controlled lighting is gone (restarted or upgraded). The
            # integration's controller runs again before this session gets its copy.
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
        _LOGGER.info("lighting_session_open session=%s instance=%s app=%s owner=%s epoch=%d",
                     session.session_id[:8], instance[:8], session.app_version, self.owner, self.epoch)
        return session, self._hello(session, now, lease_token)

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        _LOGGER.info("lighting_session_closed session=%s owner=%s", session_id[:8], session_id == self.owner_session)
        if session_id == self.owner_session:
            self.owner_session = None  # it may reconnect within its lease; otherwise the lease expires

    def _session(self, session_id: Any, connection: Any) -> Session:
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.connection is not connection:
            raise BridgeError(S.REJECT_UNKNOWN_SESSION, "unknown bridge session")
        session.last_seen = dt_util.utcnow()
        session.messages += 1
        return session

    def _check_owner(self, session: Session, params: dict[str, Any], now: datetime) -> None:
        if self.owner != S.OWNER_APP or session.session_id != self.owner_session or self._busy:
            raise BridgeError(S.REJECT_NOT_OWNER, "this session does not control lighting")
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
        _LOGGER.debug("lighting_rejected message=%s code=%s detail=%s", what, err.code, err)

    # ------------------------------------------------------------ handover
    async def async_claim(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """A reviewed handover to the App: fence, drain and stop the integration's controller; hand over its copy."""
        session = self._session(params["session_id"], connection)
        if self.owner != S.OWNER_INTEGRATION or self._busy:
            raise BridgeError(S.REJECT_BUSY, "the App already controls lighting (or a change is in progress)")
        if self.fallback_error:
            raise BridgeError(S.REJECT_BUSY, self.fallback_error)
        if params["expected_epoch"] != self.epoch:
            raise BridgeError(S.REJECT_BUSY, f"the ownership changed (epoch {self.epoch})")
        if session.fingerprint != self.fingerprint or session.core_fingerprint != self.core_fingerprint:
            raise BridgeError(S.REJECT_VERSION, "the App runs different lighting code; update the App and the "
                                                "integration to matching versions")
        if params["config_revision"] != self.config_revision():
            raise BridgeError(S.REJECT_CONFIG, "the App's copy of the lighting configuration is not the current one")
        readiness = _readiness(params.get("readiness"))
        if not readiness["ready"]:
            raise BridgeError(S.REJECT_NOT_READY, "the App reports that it is not ready for a handover")
        ctrl = self.ctrl
        self._busy = True
        try:
            ctrl.fenced = True
            try:
                if not await ctrl.async_drain(S.DRAIN_SECONDS):
                    raise BridgeError(S.REJECT_BUSY, "lighting commands are still being sent; try again")
                await ctrl.async_flush()
                if self.sessions.get(session.session_id) is not session or not self._running:
                    raise BridgeError(S.REJECT_UNKNOWN_SESSION, "the bridge session ended during the handover")
            except BaseException:
                ctrl.fenced = False  # the integration keeps control
                raise
            # From here on nothing awaits: no edit or command can come between the copy and the switch.
            doc = ctrl.lighting.as_dict()
            state = ctrl.export_state()
            recent = ctrl.recent_commands()
            self.detach()
            ctrl.async_stop()
            self._make_dormant(ctrl)
            now = dt_util.utcnow()
            token = secrets.token_hex(16)
            by = params.get("requested_by") if isinstance(params.get("requested_by"), str) else user_id
            self.mirror = {"state": state, "state_at": _iso(now), "epoch": self.epoch + 1, "rooms": {},
                           "manual_mode": None}
            self._save_mirror()
            self._change_owner(S.OWNER_APP, S.REASON_HANDOVER, now, session=session, token=token, by=by,
                               readiness=readiness)
        finally:
            self._busy = False
        self.coordinator.async_schedule_refresh()
        async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)
        _LOGGER.info("lighting_handover epoch=%d by=%s", self.epoch, by)
        return {
            "epoch": self.epoch, "lease_token": token, "lease_until": _iso(self.lease_until),
            "lease_seconds": S.LEASE_SECONDS, "heartbeat_seconds": S.HEARTBEAT_SECONDS,
            "config": doc, "config_revision": S.config_revision(doc), "state": state, "recent": recent,
        }

    # ------------------------------------------------------------ the owner
    async def async_execute(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """One command of the App's controller: fenced, allowlisted, sent with the App's context."""
        self._accept(connection, params, "execute")
        domain, service, data = params["domain"], params["service"], params["data"]
        mood = self.coordinator.config.mood
        try:
            if domain in ("input_select", "select") and service == "select_option" and \
                    isinstance(data.get("entity_id"), str) and \
                    data["entity_id"] in {mood.manual_mood_entity, mood.main_mood_entity} - {None}:
                ids = [data["entity_id"]]
                if set(data) - {"entity_id", "option"} or not isinstance(data.get("option"), str):
                    raise S.ProtocolError(S.REJECT_NOT_ALLOWED, "invalid mood selection")
            else:
                ids = S.check_command(domain, service, data, lights=self._lights(), doc=self.config_doc())
        except S.ProtocolError as err:
            error = BridgeError(err.code, str(err))
            self._rejected(error, "execute")
            raise error from err
        context_id = params["context_id"]
        if not S.valid_context_id(context_id):
            raise BridgeError(S.REJECT_INVALID, "invalid context id")
        self._recent.append((time.monotonic(), context_id))
        self._inflight += 1
        self._idle.clear()
        record = {"at": _iso(dt_util.utcnow()), "domain": domain, "service": service, "entities": ids[:10],
                  "purpose": params.get("purpose"), "area_id": params.get("area_id"), "seq": params["seq"]}
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
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

    def _lights(self) -> set[str]:
        """Light targets of the (mirrored) configuration: managed room lights and the members of managed groups."""
        out: set[str] = set()
        for managed in self.ctrl.managed.values():
            out.update(managed.targets)
            for members in managed.members.values():
                out.update(members)
        return {e for e in out if e.startswith("light.")}

    @callback
    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._accept(connection, params, "heartbeat")
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until), "epoch": self.epoch}

    def _validate_publish(self, params: dict[str, Any]) -> tuple[LightingConfig | None, dict[str, Any] | None,
                                                                  dict[str, Any] | None]:
        config = None
        if params.get("config") is not None:
            try:
                if len(S.canonical(params["config"])) > S.MAX_CONFIG_BYTES:
                    raise ValueError("configuration too large")
                config = LightingConfig.from_dict(params["config"])
            except (TypeError, ValueError, KeyError, AttributeError) as err:
                raise BridgeError(S.REJECT_INVALID, f"invalid lighting configuration: {err}") from err
            if S.config_revision(config.as_dict()) != params.get("config_revision"):
                raise BridgeError(S.REJECT_CONFIG, "the configuration does not match its revision")
        state = None
        if params.get("state") is not None:
            try:
                state = S.check_state(params["state"])
            except S.ProtocolError as err:
                raise BridgeError(err.code, str(err)) from err
        rooms = params.get("rooms")
        if rooms is not None and not (isinstance(rooms, dict) and all(
                isinstance(k, str) and isinstance(v, dict) for k, v in rooms.items())):
            raise BridgeError(S.REJECT_INVALID, "invalid room view")
        return config, state, rooms

    def _apply_publish(self, config: LightingConfig | None, state: dict[str, Any] | None,
                       rooms: dict[str, Any] | None, manual_mode: Any) -> None:
        now = dt_util.utcnow()
        if config is not None and config != self.ctrl.lighting:
            ctrl = self.ctrl
            ctrl.store.async_set_mirrored(config)  # the dormant controller's store: saved shortly
            ctrl.async_config_mirrored()
        if state is not None:
            self.mirror = {**self.mirror, "state": state, "state_at": _iso(now), "epoch": self.epoch}
        if rooms is not None:
            self.mirror = {**self.mirror, "rooms": rooms, "manual_mode": manual_mode is True, "rooms_at": _iso(now)}
            async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)
        if state is not None or rooms is not None:
            self._save_mirror()

    @callback
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """The App's configuration, runtime state and per-room view (the mirror). All or nothing."""
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "publish")
            raise
        self._accept(connection, params, "publish")
        self._apply_publish(*parsed, params.get("manual_mode"))
        return {"seq": self.last_seq, "lease_until": _iso(self.lease_until)}

    @callback
    def inputs(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Occupancy inputs of the App's lighting runtime (possible occupancy signals, zone holds, switch activity)."""
        kind = params["kind"]
        occupancy = self.coordinator.occupancy
        if kind == "preliminary":
            signals = params.get("signals")
            if not isinstance(signals, dict) or not all(isinstance(v, list) for v in signals.values()):
                raise BridgeError(S.REJECT_INVALID, "invalid signals")
            self._accept(connection, params, "inputs")
            occupancy.async_set_preliminary({a: frozenset(str(e) for e in v) for a, v in signals.items()
                                             if a in occupancy.engines})
        elif kind == "hold":
            zones = params.get("zones")
            if not isinstance(params.get("area_id"), str) or not isinstance(zones, list):
                raise BridgeError(S.REJECT_INVALID, "invalid hold")
            self._accept(connection, params, "inputs")
            occupancy.async_set_holds(params["area_id"], frozenset(str(z) for z in zones))
        elif kind == "activity":
            if not isinstance(params.get("area_id"), str) or not isinstance(params.get("source"), str):
                raise BridgeError(S.REJECT_INVALID, "invalid activity")
            self._accept(connection, params, "inputs")
            occupancy.async_note_activity(params["area_id"], params["source"][:255])
        else:
            raise BridgeError(S.REJECT_INVALID, f"unknown input {kind}")
        return {"seq": self.last_seq}

    async def async_release(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """The App returns control (with its final configuration and state)."""
        try:
            parsed = self._validate_publish(params)
        except BridgeError as err:
            self._rejected(err, "release")
            raise
        self._accept(connection, params, "release")
        self._apply_publish(*parsed, params.get("manual_mode"))
        await self.async_fallback(S.REASON_RELEASE, by=user_id, detail=params.get("detail"))
        return {"ownership": self.status()}

    # -------------------------------------------------------- owner change
    @callback
    def _schedule_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> None:
        """Fence the App at once (new epoch); the integration's controller starts right after."""
        if self.owner != S.OWNER_APP:
            return
        now = dt_util.utcnow()
        self._change_owner(S.OWNER_INTEGRATION, reason, now, by=by, detail=detail)
        self.hass.async_create_task(self._async_start_controller(reason), "smart_occupancy lighting fallback")

    async def async_fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> bool:
        """The integration takes lighting back. Returns False when it already controls lighting."""
        if self.owner != S.OWNER_APP:
            return False
        self._change_owner(S.OWNER_INTEGRATION, reason, dt_util.utcnow(), by=by, detail=detail)
        await self._async_start_controller(reason)
        return True

    async def _async_start_controller(self, reason: str) -> None:
        """A new integration controller from the mirror (the App's configuration and last state); applies nothing."""
        from .controller import LightingController  # noqa: PLC0415 - import cycle

        self._busy = True
        try:
            try:
                # An App command already accepted runs until it ends or times out (COMMAND_TIMEOUT_SECONDS).
                async with asyncio.timeout(max(S.DRAIN_SECONDS, COMMAND_TIMEOUT_SECONDS + 1)):
                    await self._idle.wait()
            except TimeoutError:
                _LOGGER.warning("lighting_fallback_drain_timeout: App commands still running")
            old = self.ctrl
            try:
                self._apply_renames(old)
                state = self.mirror.get("state")
                if isinstance(state, dict):
                    merged = S.merge_decisions(state, old.decisions.persist())
                    await old._state_store.async_save(merged)  # noqa: SLF001 - the next controller loads it
                await old.store.async_flush()
            except Exception:
                # Lighting must run again: the next controller loads what the stores hold.
                _LOGGER.exception("lighting_fallback_mirror_failed (the integration starts from its stored copy)")
            old.dormant = False
            old.async_stop()
            new = LightingController(self.hass, self.coordinator)
            try:
                await new.async_load()
            except Exception:
                _LOGGER.exception("lighting_fallback_load_failed: lighting does not run; reload the integration")
                self._failed_fallback(old, new)
                return
            now = time.monotonic()
            new.note_recent_commands({"contexts": [c for t, c in self._recent if now - t <= S.OWN_CONTEXT_SECONDS]})
            self.coordinator.lighting = new
            new.async_start()
            self.attach(new)
            self.mirror = {**self.mirror, "rooms": {}, "renames": [], "returned_at": _iso(dt_util.utcnow())}
            self._save_mirror()
        finally:
            self._busy = False
        self.coordinator.async_schedule_refresh()
        async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)
        level = logging.INFO if reason in (S.REASON_RELEASE, S.REASON_SERVICE) else logging.WARNING
        _LOGGER.log(level, "lighting_fallback reason=%s epoch=%d", reason, self.epoch)

    @callback
    def _failed_fallback(self, old: LightingController, new: LightingController) -> None:
        """The next controller could not load: nothing commands lights; the status says so (a reload retries)."""
        old.fenced = True
        self.fallback_error = "the lighting controller could not be started; reload the integration"
        self.coordinator.async_schedule_refresh()
        async_dispatcher_send(self.hass, SIGNAL_LIGHTING_UPDATED)

    # -------------------------------------------------------------- renames
    @callback
    def note_rename(self, old: str, new: str) -> None:
        """An entity id renamed while the App owns lighting but is not connected: given to it when it resumes
        (or applied here when the integration takes lighting back). A connected App follows renames itself."""
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

    def _apply_renames(self, ctrl: LightingController) -> None:
        from .lighting_config import rename_entity  # noqa: PLC0415 - import cycle

        for old, new in self._take_renames():
            if (lighting := rename_entity(ctrl.store.config, old, new)) is not None:
                _LOGGER.info("entity_renamed old=%s new=%s (while the App was away)", old, new)
                ctrl.store.async_set_mirrored(lighting)

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
    @callback
    def options_changed(self, options: dict[str, Any]) -> None:
        """Options applied live (presence settings): the App's controller uses the same configuration."""
        self._broadcast({"type": "options", "options": dict(options)})

    def _broadcast(self, message: dict[str, Any]) -> None:
        for session in list(self.sessions.values()):
            try:
                session.send(message)
            except Exception:  # noqa: BLE001 - a broken connection must not break lighting
                _LOGGER.debug("lighting_send_failed session=%s", session.session_id[:8])

    # ------------------------------------------- the view of the room entities
    def room_view(self, area_id: str) -> dict[str, Any] | None:
        """What the App published for a room's entities (only while it controls lighting)."""
        if not self.app_owns:
            return None
        view = (self.mirror.get("rooms") or {}).get(area_id)
        return view if isinstance(view, dict) else None


class LightingView:
    """What the room entities show: the running controller's values, or the App's while it controls lighting."""

    def __init__(self, coordinator: SmartOccupancyCoordinator) -> None:
        self.coordinator = coordinator

    def _view(self, area_id: str) -> dict[str, Any] | None:
        bridge = getattr(self.coordinator, "lighting_bridge", None)
        return bridge.room_view(area_id) if bridge is not None else None

    def room_active(self, area_id: str) -> bool:
        view = self._view(area_id)
        if view is not None and isinstance(view.get("active"), bool):
            return view["active"]
        return self.coordinator.lighting.room_active(area_id)

    @property
    def manual_mode(self) -> bool:
        bridge = getattr(self.coordinator, "lighting_bridge", None)
        if bridge is not None and bridge.app_owns and isinstance(bridge.mirror.get("manual_mode"), bool):
            return bridge.mirror["manual_mode"]
        return self.coordinator.lighting.manual_mode

    def room_diagnostics(self, area_id: str) -> dict[str, Any]:
        view = self._view(area_id)
        if view is not None and isinstance(view.get("diagnostics"), dict):
            return dict(view["diagnostics"])
        return self.coordinator.lighting.room_diagnostics(area_id)

    def daylight_summary(self, area_id: str) -> dict[str, Any]:
        view = self._view(area_id)
        if view is not None and isinstance(view.get("daylight"), dict):
            return dict(view["daylight"])
        return self.coordinator.lighting.daylight.status(area_id).summary()

    def activation(self, area_id: str) -> tuple[str | None, str | None]:
        view = self._view(area_id)
        if view is not None and isinstance(view.get("activation"), dict):
            return view["activation"].get("at"), view["activation"].get("text")
        rt = self.coordinator.lighting.runtime.get(area_id)
        activation = rt.activation if rt else None
        return (rt.last_activation_at if rt else None), (activation.get("text") if activation else None)

