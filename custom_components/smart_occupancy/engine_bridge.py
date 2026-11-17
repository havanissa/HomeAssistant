"""Occupancy engine bridge and ownership (Phase A3.2): the one authority over who publishes occupancy.

The Smart Lighting App can run the shared occupancy engine (``occupancy_core``)
next to this integration's own. This module decides which of the two decides
the published occupancy - the room entities and every consumer that reads
``OccupancyManager.result()`` (lighting, zones, curtains, forecasts, the
presence API). Nothing else may decide it, and nothing here controls a device.

Ownership record (``.storage/smart_occupancy.engine_ownership``)
------------------------------------------------------------------
``owner`` is ``integration`` (the default, also after every install or
upgrade) or ``app``. ``epoch`` increases with every change of owner. While the
App owns occupancy it holds a lease (``protocol.LEASE_SECONDS``) bound to one
bridge session (one websocket connection); every accepted publish or heartbeat
renews it. Only a digest of the lease token is stored.

Fencing
-------
A publish or heartbeat is accepted only from the owner's session, on the same
connection, with the current epoch, the lease token, a sequence number higher
than every earlier one and the current configuration revision. Everything else
is refused (``protocol.REJECT_*``) and changes nothing. The published result of
a room is the owner's latest accepted result; while the integration owns
occupancy (or the App has not published a room yet) it is the integration
engine's own result. So exactly one engine is authoritative at any time.

Hot standby and fallback
------------------------
The integration's engine never stops: it observes every signal and input while
the App owns occupancy. The integration takes over (``epoch + 1``) when the
lease expires, when a different App instance connects (the App restarted or
was upgraded), when the owner releases control, when an administrator calls
the ``engine_return_to_integration`` service, or when the App does not resume
after Home Assistant restarted or the integration reloaded. It then reconciles
each room with the last accepted App decision (``RoomEngine.adopt``): equal
decisions keep their timers; otherwise an occupied room stays occupied and its
quiet period starts again, so losing the App never causes vacancy. Listeners
are notified only for rooms whose published result actually changed.

A recovered App never takes ownership back: it returns to shadow mode until an
administrator performs a new reviewed handover (``claim``). Only the *same* App
process may resume after Home Assistant restarted or the integration reloaded,
within ``protocol.RESUME_GRACE_SECONDS``, by presenting its lease token.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
import secrets
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import ENGINE_STORE_KEY, ENGINE_STORE_VERSION, VERSION
from .occupancy_core import CORE_VERSION, fingerprint
from .occupancy_core import protocol as P
from .occupancy_core.engine import RoomResult, StateError
from .occupancy_core.wire import WireError, decision_of, iso, result_from_wire, result_to_wire, spec_to_wire
from .presence_log import presence_log

if TYPE_CHECKING:
    from .occupancy_manager import OccupancyManager

_LOGGER = logging.getLogger(__name__)

SAVE_DELAY_SECONDS = 5
MAX_SESSIONS = 4
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z.+\-]{1,32}$")
_SHORT = re.compile(r"^[\w.\-]{1,64}$")
_CHECK_STATUS = frozenset({"ok", "warning", "error", "info", "pending"})


class BridgeError(Exception):
    """A bridge request was refused; nothing changed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(slots=True)
class Session:
    """One App bridge connection (a websocket subscription)."""

    session_id: str
    instance_id: str
    connection: Any
    send: Callable[[dict[str, Any]], None]
    app_version: str
    core_version: str
    fingerprint: str
    protocol: int
    opened_at: datetime
    last_seen: datetime
    messages: int = 0

    def as_dict(self, owner_session: str | None) -> dict[str, Any]:
        return {
            "session_id": self.session_id[:8],
            "instance_id": self.instance_id[:8],
            "app_version": self.app_version,
            "core_version": self.core_version,
            "fingerprint": self.fingerprint,
            "protocol": self.protocol,
            "opened_at": iso(self.opened_at),
            "last_seen": iso(self.last_seen),
            "messages": self.messages,
            "owner": self.session_id == owner_session,
        }


@dataclass(slots=True)
class Accepted:
    """The latest accepted App result of one room."""

    result: RoomResult
    wire: dict[str, Any]
    received_at: datetime
    seq: int
    epoch: int


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    return dt_util.parse_datetime(value)


def _readiness_summary(readiness: Any) -> dict[str, Any]:
    """A bounded copy of the App's readiness verdict, kept in the ownership history."""
    if not isinstance(readiness, dict):
        return {"ready": False}
    checks = []
    for item in (readiness.get("checks") or [])[:20] if isinstance(readiness.get("checks"), list) else []:
        if isinstance(item, dict) and isinstance(item.get("key"), str) and _SHORT.match(item["key"]):
            status = item.get("status") if item.get("status") in _CHECK_STATUS else "info"
            checks.append({"key": item["key"], "status": status})
    out: dict[str, Any] = {"ready": readiness.get("ready") is True, "checks": checks}
    for key in ("shadow_seconds", "transitions", "disagreements"):
        value = readiness.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9:
            out[key] = value
    return out


class EngineBridge:
    """Ownership of the published occupancy, and the App's bridge sessions."""

    def __init__(self, hass: HomeAssistant, manager: OccupancyManager) -> None:
        self.hass = hass
        self.manager = manager
        self.boot_id = secrets.token_hex(16)
        self.started_at = dt_util.utcnow()
        self._store: Store[dict[str, Any]] = Store(hass, ENGINE_STORE_VERSION, ENGINE_STORE_KEY)
        self.fingerprint: str | None = None
        # The ownership record.
        self.owner = P.OWNER_INTEGRATION
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
        # Accepted results (current owner) and the last accepted decisions (persisted, for a restart).
        self.accepted: dict[str, Accepted] = {}
        self.decisions: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, Session] = {}
        self.rejections: Counter[str] = Counter()
        self.last_rejection: dict[str, Any] | None = None
        self.accepted_total = 0
        self.last_accepted_at: datetime | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._sent: dict[str, dict[str, Any]] = {}
        self._holds: dict[str, list[str]] = {}
        self._preliminary: dict[str, list[str]] = {}
        self._running = False

    # ------------------------------------------------------------ lifecycle
    async def async_load(self) -> None:
        self.fingerprint = await self.hass.async_add_executor_job(fingerprint)
        data = await self._store.async_load()
        if data is None:
            return
        try:
            self._restore(data)
        except (TypeError, ValueError, KeyError) as err:
            # A damaged record: the integration keeps occupancy (the safe owner); the epoch only grows.
            _LOGGER.warning("engine_ownership_invalid error=%s; the integration keeps occupancy", err)
            epoch = data.get("epoch") if isinstance(data, dict) else None
            self.owner, self.epoch = P.OWNER_INTEGRATION, (epoch + 1 if isinstance(epoch, int) and epoch >= 0 else 1)
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
        self.history = [h for h in history if isinstance(h, dict)][-P.HISTORY_SIZE:] if isinstance(history, list) else []
        decisions = data.get("decisions") or {}
        self.decisions = {a: d for a, d in decisions.items() if isinstance(a, str) and isinstance(d, dict)} \
            if isinstance(decisions, dict) else {}
        self.changed_at = _time(data.get("changed_at"))
        self.changed_by = data.get("changed_by") if isinstance(data.get("changed_by"), str) else None
        owner = data.get("owner")
        instance, lease = data.get("owner_instance"), data.get("lease_hash")
        if owner == P.OWNER_APP and P.valid_id(instance) and isinstance(lease, str) and _HEX64.match(lease):
            # Home Assistant restarted or the integration reloaded while the App owned occupancy: the same App
            # process may resume within the grace period; this engine publishes meanwhile.
            self.owner = P.OWNER_APP
            self.owner_instance, self._lease_hash = instance, lease
            versions = data.get("owner_versions")
            self.owner_versions = versions if isinstance(versions, dict) else {}
            self.resume_pending = True
            self.lease_until = dt_util.utcnow() + timedelta(seconds=P.RESUME_GRACE_SECONDS)
        elif owner not in (P.OWNER_INTEGRATION, P.OWNER_APP, None):
            raise ValueError("invalid owner")

    @callback
    def async_start(self) -> None:
        self._running = True
        self._schedule_lease()
        if self.resume_pending:
            _LOGGER.info("engine_resume_pending epoch=%d grace=%ds (the App owned occupancy before the restart)",
                         self.epoch, P.RESUME_GRACE_SECONDS)

    @callback
    def async_stop(self) -> None:
        self._running = False
        if self._timer:
            self._timer()
            self._timer = None
        self._broadcast({"type": "closed", "reason": "integration_stopping"})
        self.sessions.clear()

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "epoch": self.epoch,
            "owner_instance": self.owner_instance,
            "lease_hash": self._lease_hash,
            "owner_versions": self.owner_versions,
            "changed_at": iso(self.changed_at),
            "changed_by": self.changed_by,
            "decisions": self.decisions,
            "history": self.history,
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    # ------------------------------------------------------------- reading
    @property
    def app_owns(self) -> bool:
        """The App is the owner (its accepted results are published)."""
        return self.owner == P.OWNER_APP

    def published(self, area_id: str) -> Accepted | None:
        """The App's accepted result for a room, when it is the published one."""
        return self.accepted.get(area_id) if self.owner == P.OWNER_APP else None

    def published_by(self, area_id: str) -> str:
        return P.OWNER_APP if self.published(area_id) is not None else P.OWNER_INTEGRATION

    def state(self) -> str:
        if self.owner == P.OWNER_APP:
            return "resume_pending" if self.resume_pending else P.OWNER_APP
        return P.OWNER_INTEGRATION

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
        return {
            "protocol": P.PROTOCOL_VERSION,
            "integration_version": VERSION,
            "core_version": CORE_VERSION,
            "fingerprint": self.fingerprint,
            "boot_id": self.boot_id,
            "started_at": iso(self.started_at),
            "now": iso(now),
            "owner": self.owner,
            "state": self.state(),
            "epoch": self.epoch,
            "owner_instance": self.owner_instance[:8] if self.owner_instance else None,
            "owner_connected": self.owner_session in self.sessions if self.owner_session else False,
            "owner_versions": self.owner_versions,
            "lease_until": iso(self.lease_until),
            "lease_seconds": P.LEASE_SECONDS,
            "heartbeat_seconds": P.HEARTBEAT_SECONDS,
            "resume_grace_seconds": P.RESUME_GRACE_SECONDS,
            "changed_at": iso(self.changed_at),
            "changed_by": self.changed_by,
            "last_seq": self.last_seq,
            "config_revision": self.manager.spec_revision,
            "sessions": [s.as_dict(self.owner_session) for s in self.sessions.values()],
            "published_by": {a: self.published_by(a) for a in self.manager.engines},
            "accepted": {
                a: {"received_at": iso(acc.received_at), "seq": acc.seq, "epoch": acc.epoch,
                    "state": acc.result.state.value, "occupied": acc.result.occupied}
                for a, acc in self.accepted.items()
            },
            "accepted_total": self.accepted_total,
            "last_accepted_at": iso(self.last_accepted_at),
            "rejections": dict(self.rejections),
            "last_rejection": self.last_rejection,
            "history": list(reversed(self.history)),
        }

    # ------------------------------------------------------------ sessions
    def _hello(self, session: Session, now: datetime, lease_token: str | None = None) -> dict[str, Any]:
        house = self.manager.house
        return {
            "type": "hello",
            "session_id": session.session_id,
            "ownership": self.status(now),
            "spec": spec_to_wire(house.spec),
            "state": house.export_state(),
            "results": self.manager.local_wires(),
            "inputs": {"preliminary": {a: sorted(e.preliminary) for a, e in house.engines.items()},
                       "holds": {a: sorted(e.external_hold) for a, e in house.engines.items()}},
            "lease_token": lease_token,
        }

    @callback
    def open_session(self, connection: Any, send: Callable[[dict[str, Any]], None],
                     params: dict[str, Any]) -> tuple[Session, dict[str, Any]]:
        """A new App bridge session. Returns the session and its ``hello`` (sent first)."""
        instance = params["instance_id"]
        if not P.valid_id(instance):
            raise BridgeError(P.REJECT_INVALID, "invalid instance id")
        if params["protocol"] != P.PROTOCOL_VERSION:
            raise BridgeError(P.REJECT_VERSION, f"protocol {params['protocol']} is not supported "
                                                f"(this integration speaks {P.PROTOCOL_VERSION})")
        if len(self.sessions) >= MAX_SESSIONS:
            raise BridgeError(P.REJECT_BUSY, "too many bridge sessions")
        now = dt_util.utcnow()
        session = Session(
            session_id=secrets.token_hex(16), instance_id=instance, connection=connection, send=send,
            app_version=params["app_version"], core_version=params["core_version"],
            fingerprint=params["fingerprint"], protocol=params["protocol"], opened_at=now, last_seen=now,
        )
        lease_token: str | None = None
        if self.owner == P.OWNER_APP and instance != self.owner_instance:
            # Another App process: the one that owned occupancy is gone (restarted or upgraded). Fence it out.
            self.fallback(P.REASON_APP_RESTARTED, detail="a new App instance connected")
        elif self.owner == P.OWNER_APP:
            token = params.get("resume_token")
            lease_valid = self.resume_pending or (self.lease_until is not None and now < self.lease_until)
            if (isinstance(token, str) and P.valid_id(token) and P.token_hash(token) == self._lease_hash
                    and lease_valid and session.fingerprint == self.fingerprint
                    and session.core_version == CORE_VERSION):
                # The same App process is back (Home Assistant restarted, the integration reloaded, or the
                # connection was interrupted): it keeps its reviewed ownership, in a new epoch.
                lease_token = secrets.token_hex(16)
                self._change_owner(P.OWNER_APP, P.REASON_RESUMED, now, session=session, token=lease_token,
                                   by=self.changed_by)
            else:
                self.fallback(P.REASON_REVOKED, detail="the App could not resume its lease")
        self.sessions[session.session_id] = session
        _LOGGER.info("engine_session_open session=%s instance=%s app=%s core=%s owner=%s epoch=%d",
                     session.session_id[:8], instance[:8], session.app_version, session.core_version,
                     self.owner, self.epoch)
        return session, self._hello(session, now, lease_token)

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        _LOGGER.info("engine_session_closed session=%s owner=%s", session_id[:8], session_id == self.owner_session)
        if session_id == self.owner_session:
            # The owner may reconnect within its lease; otherwise the lease expires and this engine takes over.
            self.owner_session = None

    def _session(self, session_id: Any, connection: Any) -> Session:
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.connection is not connection:
            raise BridgeError(P.REJECT_UNKNOWN_SESSION, "unknown bridge session")
        session.last_seen = dt_util.utcnow()
        session.messages += 1
        return session

    # ------------------------------------------------------------ handover
    @callback
    def claim(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """A reviewed handover to the App (compare-and-swap on the epoch)."""
        session = self._session(params["session_id"], connection)
        if self.owner != P.OWNER_INTEGRATION or self.resume_pending:
            raise BridgeError(P.REJECT_BUSY, "the App already owns occupancy")
        if params["expected_epoch"] != self.epoch:
            raise BridgeError(P.REJECT_BUSY, f"the ownership changed (epoch {self.epoch})")
        if session.fingerprint != self.fingerprint or session.core_version != CORE_VERSION:
            raise BridgeError(P.REJECT_VERSION, "the App runs a different occupancy engine; update the App and "
                                                "the integration to matching versions")
        if params["config_revision"] != self.manager.spec_revision:
            raise BridgeError(P.REJECT_CONFIG, "the App's occupancy configuration is not the current one")
        readiness = _readiness_summary(params.get("readiness"))
        if not readiness["ready"]:
            raise BridgeError(P.REJECT_NOT_READY, "the App reports that it is not ready for a handover")
        now = dt_util.utcnow()
        token = secrets.token_hex(16)
        by = params.get("requested_by") if isinstance(params.get("requested_by"), str) else user_id
        self._change_owner(P.OWNER_APP, P.REASON_HANDOVER, now, session=session, token=token, by=by,
                           readiness=readiness)
        house = self.manager.house
        return {
            "epoch": self.epoch,
            "lease_token": token,
            "lease_until": iso(self.lease_until),
            "lease_seconds": P.LEASE_SECONDS,
            "heartbeat_seconds": P.HEARTBEAT_SECONDS,
            "config_revision": self.manager.spec_revision,
            "state": house.export_state(),
            "results": self.manager.local_wires(),
        }

    def _check_owner(self, session: Session, params: dict[str, Any], now: datetime) -> None:
        if self.owner != P.OWNER_APP or session.session_id != self.owner_session:
            raise BridgeError(P.REJECT_NOT_OWNER, "this session does not own occupancy")
        if params["epoch"] != self.epoch:
            raise BridgeError(P.REJECT_STALE_EPOCH, f"epoch {params['epoch']} is not the current one ({self.epoch})")
        token = params["lease_token"]
        if not P.valid_id(token) or P.token_hash(token) != self._lease_hash:
            raise BridgeError(P.REJECT_LEASE, "invalid lease")
        if self.lease_until is None or now >= self.lease_until:
            raise BridgeError(P.REJECT_LEASE, "the lease has expired")
        if params["seq"] <= self.last_seq:
            raise BridgeError(P.REJECT_SEQUENCE, f"sequence {params['seq']} is not newer than {self.last_seq}")

    def _renew(self, seq: int, now: datetime) -> None:
        self.last_seq = seq
        self.lease_until = now + timedelta(seconds=P.LEASE_SECONDS)
        self._schedule_lease()

    @callback
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """The owner's results. All or nothing: one invalid room refuses the whole message."""
        now = dt_util.utcnow()
        try:
            session = self._session(params["session_id"], connection)
            self._check_owner(session, params, now)
            if params["config_revision"] != self.manager.spec_revision:
                raise BridgeError(P.REJECT_CONFIG, "configuration revision mismatch")
            spec = self.manager.house.spec
            results: dict[str, tuple[RoomResult, dict[str, Any]]] = {}
            for area_id, wire in params["results"].items():
                room = spec.room(area_id)
                if room is None:
                    raise BridgeError(P.REJECT_INVALID, f"unknown room {area_id}")
                try:
                    results[area_id] = (result_from_wire(wire, room), wire)
                except WireError as err:
                    raise BridgeError(P.REJECT_INVALID, f"{area_id}: {err}") from err
        except BridgeError as err:
            self._rejected(err, "publish")
            raise
        self._renew(params["seq"], now)
        self.accepted_total += 1
        self.last_accepted_at = now
        save = False
        for area_id, (result, wire) in results.items():
            previous = self.accepted.get(area_id)
            before = self.manager.published_key(area_id)
            self.accepted[area_id] = Accepted(result, wire, now, params["seq"], self.epoch)
            decision = decision_of(wire)
            if self.decisions.get(area_id, {}).get("state") != decision["state"] or \
                    self.decisions.get(area_id, {}).get("occupied") != decision["occupied"]:
                self.decisions[area_id] = {**decision, "received_at": iso(now), "epoch": self.epoch}
                save = True
            if previous is None or previous.wire != wire or before[0] != P.OWNER_APP:
                self.manager.async_published_changed(area_id, now, before)
        if save:
            self._save()
        return {"seq": self.last_seq, "lease_until": iso(self.lease_until)}

    @callback
    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        now = dt_util.utcnow()
        try:
            session = self._session(params["session_id"], connection)
            self._check_owner(session, params, now)
            if params["config_revision"] != self.manager.spec_revision:
                raise BridgeError(P.REJECT_CONFIG, "configuration revision mismatch")
        except BridgeError as err:
            self._rejected(err, "heartbeat")
            raise
        self._renew(params["seq"], now)
        return {"seq": self.last_seq, "lease_until": iso(self.lease_until), "epoch": self.epoch}

    @callback
    def release(self, *, by: str | None, detail: str | None = None, expected_epoch: int | None = None) -> bool:
        """Return control to the integration. Returns False when the integration already owns occupancy."""
        if self.owner != P.OWNER_APP:
            return False
        if expected_epoch is not None and expected_epoch != self.epoch:
            raise BridgeError(P.REJECT_BUSY, f"the ownership changed (epoch {self.epoch})")
        self.fallback(P.REASON_RELEASE, by=by, detail=detail)
        return True

    def _rejected(self, err: BridgeError, what: str) -> None:
        self.rejections[err.code] += 1
        self.last_rejection = {"at": iso(dt_util.utcnow()), "message": what, "code": err.code, "detail": str(err)}
        _LOGGER.debug("engine_rejected message=%s code=%s detail=%s", what, err.code, err)

    # -------------------------------------------------------- owner change
    @callback
    def fallback(self, reason: str, *, by: str | None = None, detail: str | None = None) -> None:
        """The integration's engine takes over (fencing the App out first)."""
        if self.owner != P.OWNER_APP:
            return
        now = dt_util.utcnow()
        # The decisions of the current App process (after a restart there are none: the stored ones were
        # already reconciled when the integration loaded, see restore_decisions).
        decisions = {a: decision_of(acc.wire) for a, acc in self.accepted.items()}
        before = {a: self.manager.published_key(a) for a in self.manager.engines}
        self._change_owner(P.OWNER_INTEGRATION, reason, now, by=by, detail=detail)
        self.accepted.clear()
        self.manager.async_take_over(decisions, before, now)
        level = logging.INFO if reason == P.REASON_RELEASE else logging.WARNING
        _LOGGER.log(level, "engine_fallback reason=%s epoch=%d detail=%s", reason, self.epoch, detail)

    def _change_owner(self, owner: str, reason: str, now: datetime, *, session: Session | None = None,
                      token: str | None = None, by: str | None = None, detail: str | None = None,
                      readiness: dict[str, Any] | None = None) -> None:
        self.epoch += 1
        self.owner = owner
        self.last_seq = 0
        self.resume_pending = False
        self.changed_at = now
        self.changed_by = by
        if owner == P.OWNER_APP:
            assert session is not None and token is not None
            self.owner_instance = session.instance_id
            self.owner_session = session.session_id
            self._lease_hash = P.token_hash(token)
            self.lease_until = now + timedelta(seconds=P.LEASE_SECONDS)
            self.owner_versions = {"app": session.app_version, "core": session.core_version,
                                   "fingerprint": session.fingerprint}
        else:
            self.owner_instance = self.owner_session = self._lease_hash = None
            self.lease_until = None
            self.owner_versions = {}
        entry: dict[str, Any] = {"epoch": self.epoch, "owner": owner, "reason": reason, "at": iso(now), "by": by,
                                 "instance": session.instance_id[:8] if session else None}
        if detail:
            entry["detail"] = detail[:200]
        if readiness is not None:
            entry["readiness"] = readiness
        self.history = [*self.history, entry][-P.HISTORY_SIZE:]
        self._schedule_lease()
        self._save()
        presence_log(self.hass).add("engine", None, at=now, owner=owner, epoch=self.epoch, reason=reason)
        self._broadcast({"type": "ownership", "ownership": self.status(now)})

    def _schedule_lease(self) -> None:
        if self._timer:
            self._timer()
            self._timer = None
        if self._running and self.owner == P.OWNER_APP and self.lease_until is not None:
            self._timer = async_track_point_in_utc_time(self.hass, self._lease_check, self.lease_until)

    @callback
    def _lease_check(self, _now: datetime) -> None:
        self._timer = None
        if self.owner != P.OWNER_APP or self.lease_until is None:
            return
        if dt_util.utcnow() < self.lease_until:
            self._schedule_lease()  # renewed meanwhile
            return
        if self.resume_pending:
            self.fallback(P.REASON_NOT_RESUMED, detail="the App did not reconnect after the restart")
        else:
            self.fallback(P.REASON_LEASE_EXPIRED, detail="no heartbeat from the App within the lease")

    @callback
    def restore_decisions(self, now: datetime) -> None:
        """After a restart while the App owned occupancy: keep what it had published occupied (conservative)."""
        if not self.resume_pending:
            return
        for area_id, decision in self.decisions.items():
            engine = self.manager.engines.get(area_id)
            if engine is None or decision.get("occupied") is not True or engine.occupied:
                continue
            try:
                engine.adopt(decision, now)
            except StateError as err:
                _LOGGER.warning("engine_decision_invalid area=%s error=%s", area_id, err)

    # ---------------------------------------------------------- streaming
    def _broadcast(self, message: dict[str, Any]) -> None:
        for session in list(self.sessions.values()):
            try:
                session.send(message)
            except Exception:  # noqa: BLE001 - a broken connection must not break occupancy
                _LOGGER.debug("engine_send_failed session=%s", session.session_id[:8])

    @callback
    def local_evaluated(self, area_id: str, now: datetime) -> None:
        """The integration engine evaluated a room: stream its result when it changed (only with a session)."""
        if not self.sessions:
            self._sent.pop(area_id, None)
            return
        engine = self.manager.engines[area_id]
        if engine.result is None:
            return
        wire = result_to_wire(engine.result, engine.visit_confirmed)
        if self._sent.get(area_id) != wire:
            self._sent[area_id] = wire
            self._broadcast({"type": "result", "area_id": area_id, "at": iso(now), "result": wire})

    @callback
    def input_preliminary(self, signals: dict[str, frozenset[str]], now: datetime) -> None:
        wanted = {a: sorted(v) for a, v in signals.items() if v}
        if wanted != self._preliminary:
            self._preliminary = wanted
            self._broadcast({"type": "input", "kind": "preliminary", "at": iso(now), "signals": wanted})

    @callback
    def input_hold(self, area_id: str, names: frozenset[str], now: datetime) -> None:
        wanted = sorted(names)
        if self._holds.get(area_id, []) != wanted:
            self._holds[area_id] = wanted
            self._broadcast({"type": "input", "kind": "hold", "at": iso(now), "area_id": area_id, "zones": wanted})

    @callback
    def input_activity(self, area_id: str, source: str, now: datetime) -> None:
        self._broadcast({"type": "input", "kind": "activity", "at": iso(now), "area_id": area_id,
                         "source": source[:255]})

    @callback
    def spec_changed(self) -> None:
        self._broadcast({"type": "spec", "spec": spec_to_wire(self.manager.house.spec)})
