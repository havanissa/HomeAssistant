"""The companion's core: ownership records, App sessions, published views and the fenced executor (Phase A6).

The Smart Lighting App runs every automation engine (occupancy, lighting,
curtains, forecasts) and owns every automation configuration. This companion
integration provides what an App cannot: native Home Assistant entities, a
command executor that runs inside Home Assistant, ownership records that
survive the App, and the events and subscriptions the App needs.

Safety policy (what happens when the App is away):

* a command is executed only for the one App session that holds the current
  lease of the domain (epoch, lease token, strictly increasing sequence, same
  connection); everything else is refused and changes nothing;
* when the App disconnects, crashes or its lease expires, the record keeps the
  App as the designated controller (``app_away``): no engine takes over, no
  light is switched off, no cover is moved, no vacancy is inferred; the native
  entities keep their last published state and report ``fresh: false``;
* a new App process opens a fresh session and claims every domain again with a
  new epoch (an old lease token is never valid again); nothing in flight is
  replayed;
* an administrator can revoke the App's control at any time (service
  ``smart_occupancy.revoke_app_control``): every later command is refused until
  the App is paired again.

Pairing: the first claim needs an administrator to pair the App from the App's
own interface (``pair``); the companion keeps only a digest of the pairing
token. Afterwards any App process that presents the token may claim a domain
whose owner is away, so automation resumes after an App restart without a human.
Pairing again rotates the token.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import functools
import logging
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
import voluptuous as vol

from . import protocol as P
from .const import (
    DOMAIN,
    SAVE_DELAY_SECONDS,
    SIGNAL_OWNERSHIP,
    SIGNAL_ROOMS_CHANGED,
    SIGNAL_VIEW_UPDATED,
    STORE_KEY,
    STORE_VERSION,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)

VIEW_DOMAINS = (*P.DOMAINS, "system")


class CompanionError(Exception):
    """A request was refused; nothing changed (``code`` is a protocol REJECT_* code)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)


@dataclass(slots=True)
class Session:
    session_id: str
    instance_id: str
    connection: Any
    send: Callable[[dict[str, Any]], None]
    app_version: str
    protocol: int
    trusted: bool
    opened_at: datetime
    last_seen: datetime
    messages: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"session_id": self.session_id[:8], "instance_id": self.instance_id[:8],
                "app_version": self.app_version, "protocol": self.protocol, "trusted": self.trusted,
                "opened_at": _iso(self.opened_at), "last_seen": _iso(self.last_seen), "messages": self.messages}


@dataclass(slots=True)
class Record:
    """The ownership record of one control domain."""

    domain: str
    owner: str = P.OWNER_NONE
    epoch: int = 0
    session: str | None = None
    instance: str | None = None
    lease_hash: str | None = None
    lease_until: datetime | None = None
    seq: int = 0
    config_revision: str | None = None
    targets: dict[str, list[str]] = field(default_factory=dict)
    granted_at: datetime | None = None
    granted_by: str | None = None
    changed_at: datetime | None = None
    executed: int = 0
    execute_errors: int = 0
    last_execute: dict[str, Any] | None = None
    published_at: datetime | None = None
    rejections: Counter[str] = field(default_factory=Counter)
    last_rejection: dict[str, Any] | None = None
    history: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=P.HISTORY_SIZE))
    inflight: int = 0

    def lease_valid(self, now: datetime) -> bool:
        return self.lease_until is not None and now < self.lease_until

    def state(self, now: datetime, connected: bool) -> str:
        if self.owner != P.OWNER_APP:
            return P.STATE_NONE
        return P.STATE_APP if self.session and connected and self.lease_valid(now) else P.STATE_APP_AWAY

    def persist(self) -> dict[str, Any]:
        return {
            "owner": self.owner, "epoch": self.epoch, "instance": self.instance, "lease_hash": self.lease_hash,
            "lease_until": _iso(self.lease_until), "seq": self.seq, "config_revision": self.config_revision,
            "targets": self.targets, "granted_at": _iso(self.granted_at), "granted_by": self.granted_by,
            "changed_at": _iso(self.changed_at), "executed": self.executed, "execute_errors": self.execute_errors,
            "published_at": _iso(self.published_at), "history": list(self.history),
        }

    @classmethod
    def restore(cls, domain: str, data: Any) -> Record:
        record = cls(domain)
        if not isinstance(data, dict):
            return record
        record.owner = P.OWNER_APP if data.get("owner") == P.OWNER_APP else P.OWNER_NONE
        record.epoch = int(data.get("epoch") or 0) if isinstance(data.get("epoch"), int) else 0
        record.instance = data.get("instance") if isinstance(data.get("instance"), str) else None
        record.lease_hash = None  # a lease never survives a restart: the App claims again
        record.lease_until = None
        record.seq = 0
        record.config_revision = data.get("config_revision") if P.valid_revision(data.get("config_revision")) else None
        try:
            record.targets = P.check_targets(domain, data.get("targets") or {})
        except P.ProtocolError:
            record.targets = {}
        record.granted_at = _time(data.get("granted_at"))
        record.granted_by = data.get("granted_by") if isinstance(data.get("granted_by"), str) else None
        record.changed_at = _time(data.get("changed_at"))
        record.executed = int(data.get("executed") or 0)
        record.execute_errors = int(data.get("execute_errors") or 0)
        record.published_at = _time(data.get("published_at"))
        for item in data.get("history") or []:
            if isinstance(item, dict):
                record.history.append(item)
        return record

    def as_dict(self, now: datetime, connected: bool) -> dict[str, Any]:
        return {
            "domain": self.domain, "owner": self.owner, "state": self.state(now, connected), "epoch": self.epoch,
            "session": self.session[:8] if self.session else None, "instance": self.instance[:8] if self.instance else None,
            "connected": connected, "lease_until": _iso(self.lease_until), "lease_valid": self.lease_valid(now),
            "seq": self.seq, "config_revision": self.config_revision,
            "targets": {k: len(v) for k, v in self.targets.items()}, "granted_at": _iso(self.granted_at),
            "granted_by": self.granted_by, "changed_at": _iso(self.changed_at), "executed": self.executed,
            "execute_errors": self.execute_errors, "last_execute": self.last_execute,
            "published_at": _iso(self.published_at), "rejections": dict(self.rejections),
            "last_rejection": self.last_rejection, "history": list(self.history), "inflight": self.inflight,
        }


class Companion:
    """Ownership, sessions, views and the executor of one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.records: dict[str, Record] = {d: Record(d) for d in P.DOMAINS}
        self.sessions: dict[str, Session] = {}
        self.views: dict[str, dict[str, Any]] = {}
        self.views_at: dict[str, str] = {}
        self.pairing: dict[str, Any] | None = None
        self.renames: list[list[str]] = []
        self.retired: dict[str, Any] | None = None
        self.boot_id = secrets.token_hex(8)
        self.started_at = dt_util.utcnow()
        self._lease_timers: dict[str, CALLBACK_TYPE] = {}
        self._actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._action_sessions: dict[str, str] = {}  # action id -> the session it was sent to
        self._idle: dict[str, asyncio.Event] = {}
        self._running = False
        self._stopped = False

    # ------------------------------------------------------------- storage
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        for domain in P.DOMAINS:
            self.records[domain] = Record.restore(domain, (data.get("records") or {}).get(domain))
            if self.records[domain].owner == P.OWNER_APP:
                self.records[domain].history.appendleft({"epoch": self.records[domain].epoch, "owner": P.OWNER_APP,
                                                         "reason": P.REASON_RESTART, "at": _iso(dt_util.utcnow()),
                                                         "by": None})
        views = data.get("views") if isinstance(data.get("views"), dict) else {}
        for domain in VIEW_DOMAINS:
            if domain in views:
                try:
                    self.views[domain] = P.check_view(domain, views[domain])
                except P.ProtocolError as err:
                    _LOGGER.warning("stored %s view not usable (%s); dropped", domain, err)
        self.views_at = {k: v for k, v in (data.get("views_at") or {}).items() if isinstance(v, str)}
        pairing = data.get("pairing")
        if isinstance(pairing, dict) and isinstance(pairing.get("hash"), str):
            self.pairing = {k: pairing.get(k) for k in ("hash", "issued_at", "by", "instance")}
        self.renames = P.check_renames(data.get("renames"))
        self.retired = data.get("retired") if isinstance(data.get("retired"), dict) else None

    @callback
    def async_start(self) -> None:
        self._running = True

    @callback
    def async_stop(self) -> None:
        self._running = False
        self._stopped = True
        for cancel in self._lease_timers.values():
            cancel()
        self._lease_timers.clear()
        for session in list(self.sessions.values()):
            self._safe_send(session, {"type": "closed", "reason": "companion_stopped"})
        self.sessions.clear()
        for future in self._actions.values():
            if not future.done():
                future.set_exception(CompanionError(P.REJECT_TIMEOUT, "the companion stopped"))
        self._actions.clear()

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    def _data(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "records": {d: r.persist() for d, r in self.records.items()},
            "views": self.views,
            "views_at": self.views_at,
            "pairing": self.pairing,
            "renames": self.renames,
            "retired": self.retired,
        }

    @callback
    def _save(self) -> None:
        if not self._stopped:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    # ------------------------------------------------------------ helpers
    @property
    def rooms(self) -> dict[str, dict[str, Any]]:
        return (self.views.get("system") or {}).get("rooms") or {}

    @property
    def mood_select(self) -> dict[str, Any]:
        return (self.views.get("system") or {}).get("mood_select") or {"enabled": False, "options": []}

    @property
    def paired(self) -> bool:
        return self.pairing is not None

    def connected(self, domain: str) -> bool:
        record = self.records[domain]
        return bool(record.session and record.session in self.sessions)

    def state(self, domain: str, now: datetime | None = None) -> str:
        return self.records[domain].state(now or dt_util.utcnow(), self.connected(domain))

    def fresh(self, domain: str) -> bool:
        return self.state(domain) == P.STATE_APP

    def view(self, domain: str) -> dict[str, Any] | None:
        return self.views.get(domain)

    def _entity_map(self) -> dict[str, str]:
        reg = er.async_get(self.hass)
        return {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, self.entry.entry_id)
                if e.platform == DOMAIN and isinstance(e.unique_id, str)}

    def _recorder_oldest(self) -> float | None:
        """The oldest recorded timestamp (not a stable API: best effort, never required)."""
        try:
            from homeassistant.components import recorder  # noqa: PLC0415

            instance = recorder.get_instance(self.hass)
            value = getattr(getattr(instance, "states_manager", None), "oldest_ts", None)
            return float(value) if isinstance(value, (int, float)) else None
        except Exception:  # noqa: BLE001 - optional information
            return None

    def _safe_send(self, session: Session, message: dict[str, Any]) -> None:
        try:
            session.send(message)
        except Exception:  # noqa: BLE001 - a dead connection must not break the companion
            _LOGGER.debug("session %s: send failed", session.session_id[:8], exc_info=True)

    def _broadcast(self, message: dict[str, Any]) -> None:
        for session in list(self.sessions.values()):
            self._safe_send(session, message)

    def _rejected(self, record: Record | None, code: str, what: str, detail: str) -> None:
        if record is not None:
            record.rejections[code] += 1
            record.last_rejection = {"at": _iso(dt_util.utcnow()), "message": what, "code": code, "detail": detail}
        _LOGGER.debug("companion_rejected message=%s code=%s detail=%s", what, code, detail)

    # ----------------------------------------------------------- sessions
    def open_session(self, connection: Any, send: Callable[[dict[str, Any]], None],
                     params: dict[str, Any]) -> tuple[Session, dict[str, Any]]:
        if params["protocol"] != P.PROTOCOL_VERSION:
            raise CompanionError(P.REJECT_VERSION, f"this companion speaks protocol {P.PROTOCOL_VERSION}, the App "
                                                   f"{params['protocol']}; update the App or the integration")
        if len(self.sessions) >= P.MAX_SESSIONS:
            # Evict the least recently seen session that holds no lease; a lease holder is never evicted.
            holders = {r.session for r in self.records.values() if r.owner == P.OWNER_APP and r.session}
            idle = [s for s in self.sessions.values() if s.session_id not in holders]
            if not idle:
                raise CompanionError(P.REJECT_BUSY, "too many App sessions, every one of them controls a domain")
            self.close_session(min(idle, key=lambda s: s.last_seen).session_id)
        now = dt_util.utcnow()
        token = params.get("pairing_token")
        trusted = bool(self.pairing and P.valid_id(token) and P.token_hash(token) == self.pairing["hash"])
        session = Session(secrets.token_hex(16), params["instance_id"], connection, send, params["app_version"],
                          params["protocol"], trusted, now, now)
        self.sessions[session.session_id] = session
        _LOGGER.info("companion_session opened=%s instance=%s app=%s trusted=%s", session.session_id[:8],
                     session.instance_id[:8], session.app_version, trusted)
        return session, self._hello(session, now)

    def _hello(self, session: Session, now: datetime) -> dict[str, Any]:
        from .legacy import legacy_summary_sync  # noqa: PLC0415

        return {
            "type": "hello", "session_id": session.session_id, "companion_version": VERSION,
            "protocol": P.PROTOCOL_VERSION, "ha_version": HA_VERSION, "boot_id": self.boot_id,
            "paired": self.paired, "trusted": session.trusted, "ownership": self.ownership(now),
            "renames": list(self.renames), "entities": self._entity_map(),
            "rooms": self.rooms, "views_at": dict(self.views_at), "recorder_oldest": self._recorder_oldest(),
            "legacy": legacy_summary_sync(self.hass, self.entry, self.retired),
            "entry_id": self.entry.entry_id,
        }

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        now = dt_util.utcnow()
        for record in self.records.values():
            if record.session == session_id:
                record.session = None  # the App is away: nothing more is executed; the record keeps the owner
                record.changed_at = now
                record.history.appendleft({"epoch": record.epoch, "owner": record.owner,
                                           "reason": P.REASON_SESSION_CLOSED, "at": _iso(now), "by": None})
                async_dispatcher_send(self.hass, SIGNAL_OWNERSHIP, record.domain)
        for future in [f for f in self._actions.values() if not f.done()]:
            future.set_exception(CompanionError(P.REJECT_TIMEOUT, "the App disconnected"))
        self._save()
        self._broadcast({"type": "ownership", "ownership": self.ownership(now)})
        _LOGGER.info("companion_session closed=%s", session_id[:8])

    def _session(self, session_id: Any, connection: Any) -> Session:
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.connection is not connection:
            raise CompanionError(P.REJECT_UNKNOWN_SESSION, "unknown bridge session (or another connection)")
        session.last_seen = dt_util.utcnow()
        session.messages += 1
        return session

    # ------------------------------------------------------------ pairing
    def pair(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """An administrator pairs this App (from the App's interface): a new pairing token, trusted at once."""
        session = self._session(params["session_id"], connection)
        token = secrets.token_hex(16)
        now = dt_util.utcnow()
        by = user_id or (params.get("requested_by") if isinstance(params.get("requested_by"), str) else None)
        self.pairing = {"hash": P.token_hash(token), "issued_at": _iso(now), "by": by, "instance": session.instance_id[:8]}
        for other in self.sessions.values():
            other.trusted = other is session  # an older token is no longer valid anywhere
        # An App paired with the old token loses every lease at once (it could otherwise keep executing).
        for record in self.records.values():
            if record.owner == P.OWNER_APP and record.session != session.session_id:
                self._change_owner(record, P.OWNER_NONE, P.REASON_PAIRED, by, "paired again: the previous App's "
                                                                                  "leases were revoked")
                async_dispatcher_send(self.hass, SIGNAL_OWNERSHIP, record.domain)
        self._save()
        _LOGGER.warning("companion_paired instance=%s by=%s (any App process with this token may now claim control)",
                        session.instance_id[:8], by)
        self._broadcast({"type": "pairing", "paired": True, "issued_at": self.pairing["issued_at"]})
        return {"pairing_token": token, "issued_at": self.pairing["issued_at"]}

    def unpair(self, by: str | None) -> None:
        self.pairing = None
        for session in self.sessions.values():
            session.trusted = False
        self._save()
        self._broadcast({"type": "pairing", "paired": False})
        _LOGGER.warning("companion_unpaired by=%s", by)

    # ---------------------------------------------------------- ownership
    def ownership(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or dt_util.utcnow()
        return {d: r.as_dict(now, self.connected(d)) for d, r in self.records.items()}

    def claim(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """A trusted App session takes a domain whose owner is away (or none): a new epoch and lease."""
        session = self._session(params["session_id"], connection)
        domain = params["domain"]
        record = self.records[domain]
        if not session.trusted:
            self._rejected(record, P.REJECT_NOT_PAIRED, "claim", "the App is not paired")
            raise CompanionError(P.REJECT_NOT_PAIRED, "this App is not paired with the companion: pair it first "
                                                      "(an administrator, from the App)")
        if params["expected_epoch"] != record.epoch:
            self._rejected(record, P.REJECT_BUSY, "claim", f"epoch {record.epoch}")
            raise CompanionError(P.REJECT_BUSY, f"the ownership changed (epoch {record.epoch})")
        now = dt_util.utcnow()
        other = self.sessions.get(record.session or "")
        if record.owner == P.OWNER_APP and other is not None and other is not session and record.lease_valid(now):
            self._rejected(record, P.REJECT_BUSY, "claim", "another App session holds the lease")
            raise CompanionError(P.REJECT_BUSY, "another App session controls this domain (its lease is valid)")
        if domain in P.COMMAND_DOMAINS or params.get("targets") is not None:
            try:
                targets = P.check_targets(domain, params.get("targets") or {})
            except P.ProtocolError as err:
                self._rejected(record, err.code, "claim", str(err))
                raise CompanionError(err.code, str(err)) from err
        else:
            targets = {}
        if domain in P.COMMAND_DOMAINS and params.get("targets") is None:
            raise CompanionError(P.REJECT_INVALID, "a command domain needs its targets")
        token = secrets.token_hex(16)
        by = user_id or (params.get("requested_by") if isinstance(params.get("requested_by"), str) else None)
        record.owner = P.OWNER_APP
        record.epoch += 1
        record.session = session.session_id
        record.instance = session.instance_id
        record.lease_hash = P.token_hash(token)
        record.lease_until = now + timedelta(seconds=P.LEASE_SECONDS)
        record.seq = 0
        record.config_revision = params.get("config_revision") if P.valid_revision(params.get("config_revision")) else None
        record.targets = targets
        record.granted_at = now
        record.granted_by = by
        record.changed_at = now
        record.history.appendleft({"epoch": record.epoch, "owner": P.OWNER_APP, "reason": P.REASON_CLAIM,
                                   "at": _iso(now), "by": by, "instance": session.instance_id[:8]})
        self._schedule_lease(domain)
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_OWNERSHIP, domain)
        self._broadcast({"type": "ownership", "ownership": self.ownership(now)})
        _LOGGER.info("companion_claim domain=%s epoch=%d instance=%s by=%s", domain, record.epoch,
                     session.instance_id[:8], by)
        return {"domain": domain, "epoch": record.epoch, "lease_token": token, "lease_until": _iso(record.lease_until),
                "lease_seconds": P.LEASE_SECONDS, "heartbeat_seconds": P.HEARTBEAT_SECONDS}

    def _check_owner(self, session: Session, record: Record, params: dict[str, Any], now: datetime) -> None:
        if record.owner != P.OWNER_APP or record.session != session.session_id:
            raise CompanionError(P.REJECT_NOT_OWNER, f"this session does not control {record.domain}")
        if params["epoch"] != record.epoch:
            raise CompanionError(P.REJECT_STALE_EPOCH, f"epoch {params['epoch']} is not the current one ({record.epoch})")
        token = params["lease_token"]
        if not P.valid_id(token) or P.token_hash(token) != record.lease_hash:
            raise CompanionError(P.REJECT_LEASE, "invalid lease")
        if not record.lease_valid(now):
            raise CompanionError(P.REJECT_LEASE, "the lease has expired")
        if params["seq"] <= record.seq:
            raise CompanionError(P.REJECT_SEQUENCE, f"sequence {params['seq']} is not newer than {record.seq}")

    def _accept(self, connection: Any, params: dict[str, Any], what: str) -> tuple[Session, Record]:
        domain = params["domain"]
        if domain not in self.records:
            raise CompanionError(P.REJECT_INVALID, f"unknown domain {domain}")
        record = self.records[domain]
        now = dt_util.utcnow()
        try:
            session = self._session(params["session_id"], connection)
            self._check_owner(session, record, params, now)
        except CompanionError as err:
            self._rejected(record, err.code, what, str(err))
            raise
        record.seq = params["seq"]
        record.lease_until = now + timedelta(seconds=P.LEASE_SECONDS)
        self._schedule_lease(domain)
        return session, record

    @callback
    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        _session, record = self._accept(connection, params, "heartbeat")
        return {"seq": record.seq, "lease_until": _iso(record.lease_until), "epoch": record.epoch}

    @callback
    def _schedule_lease(self, domain: str) -> None:
        if cancel := self._lease_timers.pop(domain, None):
            cancel()
        record = self.records[domain]
        if record.lease_until is None or not self._running:
            return
        # A real callback: a plain function would be run in an executor thread by Home Assistant.
        self._lease_timers[domain] = async_track_point_in_utc_time(
            self.hass, callback(functools.partial(self._lease_fired, domain)), record.lease_until + timedelta(seconds=1))

    @callback
    def _lease_fired(self, domain: str, _now: datetime) -> None:
        self._lease_check(domain)

    @callback
    def _lease_check(self, domain: str) -> None:
        self._lease_timers.pop(domain, None)
        record = self.records[domain]
        now = dt_util.utcnow()
        if record.owner != P.OWNER_APP or record.lease_valid(now):
            return
        _LOGGER.warning("companion_lease_expired domain=%s epoch=%d (the App is away: nothing is commanded, the "
                        "published state is kept and marked stale)", domain, record.epoch)
        record.history.appendleft({"epoch": record.epoch, "owner": P.OWNER_APP, "reason": P.REASON_LEASE_EXPIRED,
                                   "at": _iso(now), "by": None})
        record.changed_at = now
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_OWNERSHIP, domain)
        self._broadcast({"type": "ownership", "ownership": self.ownership(now)})

    def release(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """The owner gives a domain up (owner none). Published views are kept, marked as not fresh."""
        _session, record = self._accept(connection, params, "release")
        self._change_owner(record, P.OWNER_NONE, P.REASON_RELEASE, user_id, params.get("detail"))
        return {"ownership": self.ownership()}

    def revoke(self, domain: str | None, by: str | None, reason: str = P.REASON_REVOKED) -> list[str]:
        """An administrator fences the App: every later command of the old epoch is refused."""
        changed = []
        for name in ([domain] if domain else list(P.DOMAINS)):
            record = self.records[name]
            if record.owner == P.OWNER_APP:
                self._change_owner(record, P.OWNER_NONE, reason, by, None)
                changed.append(name)
        return changed

    def _change_owner(self, record: Record, owner: str, reason: str, by: str | None, detail: Any) -> None:
        now = dt_util.utcnow()
        record.epoch += 1
        record.owner = owner
        record.session = None
        record.instance = None
        record.lease_hash = None
        record.lease_until = None
        record.seq = 0
        record.changed_at = now
        entry = {"epoch": record.epoch, "owner": owner, "reason": reason, "at": _iso(now), "by": by}
        if isinstance(detail, str) and detail:
            entry["detail"] = detail[:200]
        record.history.appendleft(entry)
        if cancel := self._lease_timers.pop(record.domain, None):
            cancel()
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_OWNERSHIP, record.domain)
        self._broadcast({"type": "ownership", "ownership": self.ownership(now)})
        _LOGGER.warning("companion_owner domain=%s owner=%s epoch=%d reason=%s by=%s", record.domain, owner,
                        record.epoch, reason, by)

    # ------------------------------------------------------------ publish
    @callback
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """The owner's view of its domain (what the native entities show), or the system view."""
        domain = params["domain"]
        if domain == "system":
            session = self._session(params["session_id"], connection)
            if not session.trusted or not any(r.owner == P.OWNER_APP and r.session == session.session_id
                                              for r in self.records.values()):
                raise CompanionError(P.REJECT_NOT_OWNER, "only the App session that controls a domain may publish "
                                                         "the system view")
            try:
                view = P.check_view("system", params.get("view"))
            except P.ProtocolError as err:
                raise CompanionError(err.code, str(err)) from err
            self._apply_system(view)
            return {"ok": True}
        if domain not in self.records:
            raise CompanionError(P.REJECT_INVALID, f"unknown domain {domain}")
        try:
            view = P.check_view(domain, params.get("view"))
        except P.ProtocolError as err:
            self._rejected(self.records[domain], err.code, "publish", str(err))
            raise CompanionError(err.code, str(err)) from err
        _session, record = self._accept(connection, params, "publish")
        if params.get("targets") is not None:
            try:
                record.targets = P.check_targets(domain, params["targets"])
            except P.ProtocolError as err:
                self._rejected(record, err.code, "publish", str(err))
                raise CompanionError(err.code, str(err)) from err
        if P.valid_revision(params.get("config_revision")):
            record.config_revision = params["config_revision"]
        now = dt_util.utcnow()
        record.published_at = now
        self.views[domain] = view
        self.views_at[domain] = _iso(now) or ""
        self._save()
        async_dispatcher_send(self.hass, SIGNAL_VIEW_UPDATED, domain)
        return {"seq": record.seq, "lease_until": _iso(record.lease_until)}

    def _apply_system(self, view: dict[str, Any]) -> None:
        before = self.rooms, self.mood_select
        self.views["system"] = view
        self.views_at["system"] = _iso(dt_util.utcnow()) or ""
        self._save()
        if before != (self.rooms, self.mood_select):
            async_dispatcher_send(self.hass, SIGNAL_ROOMS_CHANGED)
        async_dispatcher_send(self.hass, SIGNAL_VIEW_UPDATED, "system")

    # ------------------------------------------------------------ execute
    async def async_execute(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """One device command of the owner of a command domain: fenced, allowlisted, with the App's context."""
        domain = params["domain"]
        if domain not in P.COMMAND_DOMAINS:
            raise CompanionError(P.REJECT_NOT_ALLOWED, f"{domain} commands nothing")
        _session, record = self._accept(connection, params, "execute")
        service_domain, service, data = params["service_domain"], params["service"], params["data"]
        try:
            ids = P.check_command(domain, service_domain, service, data, targets=record.targets)
        except P.ProtocolError as err:
            self._rejected(record, err.code, "execute", str(err))
            raise CompanionError(err.code, str(err)) from err
        context_id = params["context_id"]
        if not P.valid_context_id(context_id):
            raise CompanionError(P.REJECT_INVALID, "invalid context id")
        now = dt_util.utcnow()
        item = {"at": _iso(now), "domain": service_domain, "service": service, "entities": ids[:10],
                "purpose": params.get("purpose"), "area_id": params.get("area_id"), "seq": params["seq"]}
        record.inflight += 1
        idle = self._idle.setdefault(domain, asyncio.Event())
        idle.clear()
        try:
            async with asyncio.timeout(P.COMMAND_TIMEOUT_SECONDS):
                await self.hass.services.async_call(service_domain, service, dict(data), blocking=True,
                                                    context=Context(id=context_id))
        except TimeoutError as err:
            record.execute_errors += 1
            record.last_execute = {**item, "error": "timeout"}
            raise CompanionError(P.REJECT_FAILED, "no response within timeout") from err
        except vol.Invalid as err:
            record.execute_errors += 1
            record.last_execute = {**item, "error": str(err)[:200]}
            raise CompanionError(P.REJECT_INVALID, f"Home Assistant refused the data: {err}") from err
        except (HomeAssistantError, ValueError) as err:
            record.execute_errors += 1
            record.last_execute = {**item, "error": str(err)[:200]}
            raise CompanionError(P.REJECT_FAILED, str(err) or type(err).__name__) from err
        finally:
            record.inflight -= 1
            if record.inflight <= 0:
                record.inflight = 0
                idle.set()
        record.executed += 1
        record.last_execute = item
        return {"seq": record.seq, "lease_until": _iso(record.lease_until)}

    async def async_drain(self, domain: str, seconds: float) -> bool:
        record = self.records[domain]
        if record.inflight <= 0:
            return True
        idle = self._idle.setdefault(domain, asyncio.Event())
        try:
            async with asyncio.timeout(seconds):
                await idle.wait()
        except TimeoutError:
            return False
        return True

    # ------------------------------------------------------------- actions
    async def async_action(self, kind: str, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """Relay a dashboard card's action to the App session that controls the matching domain."""
        if kind not in P.ACTIONS:
            raise CompanionError(P.REJECT_INVALID, f"unknown action {kind}")
        domain = "curtains" if kind.startswith("curtains/") else "lighting"
        record = self.records[domain]
        session = self.sessions.get(record.session or "")
        if session is None or record.owner != P.OWNER_APP or not record.lease_valid(dt_util.utcnow()):
            raise CompanionError(P.REJECT_NOT_OWNER, "the Smart Lighting App is not connected")
        action_id = secrets.token_hex(8)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._actions[action_id] = future
        self._action_sessions[action_id] = session.session_id
        self._safe_send(session, {"type": "action", "action_id": action_id, "kind": kind, "params": params,
                                  "user": user_id})
        try:
            async with asyncio.timeout(P.ACTION_TIMEOUT_SECONDS):
                return await future
        except TimeoutError as err:
            raise CompanionError(P.REJECT_TIMEOUT, "the App did not answer") from err
        finally:
            self._actions.pop(action_id, None)
            self._action_sessions.pop(action_id, None)

    @callback
    def action_result(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params["session_id"], connection)
        future = self._actions.get(params["action_id"])
        if future is None or future.done() or self._action_sessions.get(params["action_id"]) != session.session_id:
            return {"accepted": False}  # only the session the action was sent to may answer it
        if params.get("ok"):
            future.set_result(params.get("result") if isinstance(params.get("result"), dict) else {})
        else:
            error = params.get("error") if isinstance(params.get("error"), dict) else {}
            future.set_exception(CompanionError(str(error.get("code") or "error"),
                                                str(error.get("message") or "refused")))
        return {"accepted": True}

    # -------------------------------------------------- Home Assistant events
    @callback
    def note_rename(self, old: str, new: str) -> None:
        """An entity id changed: tell every session; keep it for an App that is away."""
        self.renames = [r for r in self.renames if r[0] != old][-P.MAX_RENAMES + 1:] + [[old, new]]
        self._save()
        self._broadcast({"type": "renames", "renames": [[old, new]]})

    @callback
    def renames_applied(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._session(params["session_id"], connection)
        self.renames = []
        self._save()
        return {"ok": True}

    @callback
    def recheck(self) -> None:
        self._broadcast({"type": "recheck"})

    @callback
    def areas_changed(self) -> None:
        self._broadcast({"type": "areas"})

    # -------------------------------------------------------------- reports
    def status(self) -> dict[str, Any]:
        from .legacy import legacy_summary_sync  # noqa: PLC0415 - legacy imports this module's constants

        now = dt_util.utcnow()
        return {
            "companion_version": VERSION, "protocol": P.PROTOCOL_VERSION, "ha_version": HA_VERSION,
            "boot_id": self.boot_id, "started_at": _iso(self.started_at), "paired": self.paired,
            "pairing": ({k: v for k, v in self.pairing.items() if k != "hash"} if self.pairing else None),
            "sessions": [s.as_dict() for s in self.sessions.values()],
            "ownership": self.ownership(now), "views_at": dict(self.views_at),
            "rooms": len(self.rooms), "mood_select": self.mood_select, "pending_renames": len(self.renames),
            "retired": self.retired, "entry_minor_version": self.entry.minor_version,
            "entities": self._entity_map(),  # live (room entities appear after the system view is published)
            "legacy": legacy_summary_sync(self.hass, self.entry, self.retired),
        }

    def diagnostics(self) -> dict[str, Any]:
        return {**self.status(), "views": {d: _summary(v) for d, v in self.views.items()}}


def _summary(view: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in view.items():
        out[key] = len(value) if isinstance(value, (dict, list)) else value
    return out
