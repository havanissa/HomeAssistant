"""The companion's hub: App sessions, pairing, the publishing lease and the accepted room snapshots.

It computes nothing about a room. It only verifies that a snapshot comes from
the one App session that holds the current lease (``protocol.py``) and that it
is well formed, then keeps it for the entities. There is no executor, no
service and no other domain: the App cannot command a device through this
integration.

What survives a restart (``.storage/smart_predictive_climate.published``): the
pairing digest, the epoch, the last accepted rooms and a short history. A lease
and a session never survive: after a restart the App must open a new session
and claim again (a new epoch), so nothing sent before the restart is accepted.
The stored rooms only tell which entities exist: until the App publishes again
they are all unavailable (a stale forecast must never look like a
recommendation).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import secrets
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import protocol as P
from .const import (
    HISTORY_SIZE,
    SAVE_DELAY_SECONDS,
    SIGNAL_FRESHNESS,
    SIGNAL_ROOMS,
    SIGNAL_VIEWS,
    STORE_KEY,
    STORE_VERSION,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


class HubError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(slots=True)
class Session:
    session_id: str
    instance_id: str
    connection: Any
    send: Callable[[dict[str, Any]], None]
    app_version: str
    trusted: bool
    opened_at: datetime
    last_seen: datetime

    def as_dict(self) -> dict[str, Any]:
        return {"session": self.session_id[:8], "instance": self.instance_id[:8], "app_version": self.app_version,
                "trusted": self.trusted, "opened_at": _iso(self.opened_at), "last_seen": _iso(self.last_seen)}


class Hub:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self.sessions: dict[str, Session] = {}
        self.pairing: dict[str, Any] | None = None
        #: An administrator unpaired (or another App replaced the pairing) at least once: an App must not pair
        #: again by itself. A fresh store (the integration was added again) has never been paired.
        self.ever_paired = False
        self.epoch = 0
        self.lease_session: str | None = None
        self.lease_hash: str | None = None
        self.lease_until: datetime | None = None
        self.seq = 0
        #: The rooms of the last accepted snapshot: room id -> {"name", "entities": {key: entity}}.
        self.views: dict[str, dict[str, Any]] = {}
        #: Rooms that were published earlier and are missing from the last snapshot (same shape; never shown).
        self.orphaned: dict[str, dict[str, Any]] = {}
        #: Rooms an administrator deleted in the App since this start (only their devices are ever removed).
        self.deleted: set[str] = set()
        self.published_at: datetime | None = None
        self.app_version: str | None = None
        self.history: deque[dict[str, Any]] = deque(maxlen=HISTORY_SIZE)
        self.refused: dict[str, int] = {}
        self.views_epoch = 0  # the epoch in which the current views were accepted
        self._lease_timer: CALLBACK_TYPE | None = None
        self._was_fresh = False
        self._stopped = False

    # ------------------------------------------------------------ storage
    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        pairing = data.get("pairing")
        if isinstance(pairing, dict) and isinstance(pairing.get("hash"), str):
            self.pairing = pairing
        self.ever_paired = bool(data.get("ever_paired")) or self.pairing is not None
        epoch = data.get("epoch")
        self.epoch = epoch if isinstance(epoch, int) and epoch >= 0 else 0
        try:
            self.views, _ = P.check_snapshot({"rooms": data.get("views") or {}})
            orphaned, _ = P.check_snapshot({"rooms": data.get("orphaned") or {}})
            self.orphaned = {room_id: room for room_id, room in orphaned.items() if room_id not in self.views}
        except P.ProtocolError as err:
            _LOGGER.warning("The stored rooms are not usable (%s); waiting for the App", err)
            self.views, self.orphaned = {}, {}
        published = data.get("published_at")
        self.published_at = dt_util.parse_datetime(published) if isinstance(published, str) else None
        self.app_version = data.get("app_version") if P.valid_version(data.get("app_version")) else None
        for item in data.get("history") or []:
            if isinstance(item, dict):
                self.history.append(item)

    def _data(self) -> dict[str, Any]:
        return {"pairing": self.pairing, "ever_paired": self.ever_paired, "epoch": self.epoch, "views": self.views,
                "orphaned": self.orphaned, "published_at": _iso(self.published_at), "app_version": self.app_version,
                "history": list(self.history)}

    def _save(self, *, now: bool = False) -> None:
        if self._stopped:
            return  # unloaded or removed: a late write must never bring the storage back
        if now:
            self.hass.async_create_task(self._store.async_save(self._data()))
        else:
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)

    async def async_flush(self) -> None:
        await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()

    @callback
    def async_stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._lease_timer:
            self._lease_timer()
            self._lease_timer = None
        for session in list(self.sessions.values()):
            self._fence_session(session, "The integration was unloaded.", closed=True)
        self.sessions.clear()
        self._clear_lease("unloaded")

    def _log(self, event: str, **data: Any) -> None:
        self.history.appendleft({"at": _iso(dt_util.utcnow()), "event": event, "epoch": self.epoch,
                                 **{k: v for k, v in data.items() if v is not None}})

    # ------------------------------------------------------------ state
    @property
    def paired(self) -> bool:
        return self.pairing is not None

    def lease_valid(self, now: datetime | None = None) -> bool:
        return (self.lease_session is not None and self.lease_session in self.sessions
                and self.lease_until is not None and (now or dt_util.utcnow()) < self.lease_until)

    @property
    def fresh(self) -> bool:
        """The rooms come from an App that holds a valid lease right now and published them in this epoch."""
        return self.lease_valid() and self.views_epoch == self.epoch

    def rooms(self) -> dict[str, str]:
        """Every room that has entities: room id -> name."""
        return {**{r: v["name"] for r, v in self.orphaned.items()}, **{r: v["name"] for r, v in self.views.items()}}

    def known(self) -> dict[str, dict[str, Any]]:
        """Every room that has entities, with their last description (published or orphaned)."""
        return {**self.orphaned, **self.views}

    def entity(self, room_id: str, key: str) -> dict[str, Any] | None:
        """The last description of one entity (None once its key or its room is gone)."""
        room = self.views.get(room_id) or self.orphaned.get(room_id)
        return room["entities"].get(key) if room else None

    def value(self, room_id: str, key: str) -> dict[str, Any] | None:
        """What an entity shows right now: its published entity, or None (unavailable) when the App is away, its
        room is not in the current snapshot or its key is gone."""
        if not self.fresh:
            return None
        room = self.views.get(room_id)
        return room["entities"].get(key) if room else None

    def _structure(self) -> Any:
        """What decides which devices and entities exist and how they are called."""
        return {room_id: (room["name"], {key: (e["platform"], e["name"]) for key, e in room["entities"].items()})
                for room_id, room in self.known().items()}

    def _freshness_changed(self) -> None:
        fresh = self.fresh
        if fresh != self._was_fresh:
            self._was_fresh = fresh
            async_dispatcher_send(self.hass, SIGNAL_FRESHNESS)

    # ----------------------------------------------------------- sessions
    def open_session(self, connection: Any, send: Callable[[dict[str, Any]], None],
                     params: dict[str, Any]) -> tuple[Session, dict[str, Any]]:
        if params["protocol"] != P.PROTOCOL_VERSION:
            raise HubError(P.REJECT_VERSION, f"this integration speaks protocol {P.PROTOCOL_VERSION}, the App "
                                             f"{params['protocol']}: update the App or the integration")
        if not P.valid_id(params["instance_id"]) or not P.valid_version(params["app_version"]):
            raise HubError(P.REJECT_INVALID, "invalid instance id or App version")
        if len(self.sessions) >= P.MAX_SESSIONS:
            idle = [s for s in self.sessions.values() if s.session_id != self.lease_session]
            if not idle:
                raise HubError(P.REJECT_BUSY, "too many App sessions")
            evicted = min(idle, key=lambda s: s.last_seen)
            self._fence_session(evicted, "This session was closed (too many App sessions).", closed=True)
            self.close_session(evicted.session_id)
        token = params.get("pairing_token")
        trusted = bool(self.pairing and P.valid_id(token) and secrets.compare_digest(
            P.token_hash(token), self.pairing["hash"]))
        now = dt_util.utcnow()
        session = Session(secrets.token_hex(16), params["instance_id"], connection, send, params["app_version"],
                          trusted, now, now)
        self.sessions[session.session_id] = session
        _LOGGER.info("spc_session opened=%s instance=%s app=%s trusted=%s", session.session_id[:8],
                     session.instance_id[:8], session.app_version, trusted)
        return session, {"type": "hello", "session_id": session.session_id, "companion_version": VERSION,
                         "protocol": P.PROTOCOL_VERSION, "paired": self.paired, "trusted": trusted,
                         "epoch": self.epoch, "paired_at": (self.pairing or {}).get("paired_at"),
                         # Never paired in this store (the integration was added fresh): an App that was paired
                         # before may pair again by itself. False after an explicit unpair.
                         "fresh_store": not self.ever_paired}

    @callback
    def close_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is None:
            return
        if self.lease_session == session_id:
            self._clear_lease("session_closed")
        _LOGGER.info("spc_session closed=%s", session_id[:8])

    def _session(self, session_id: Any, connection: Any) -> Session:
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None or session.connection is not connection:
            raise HubError(P.REJECT_UNKNOWN_SESSION, "unknown session (or another connection)")
        session.last_seen = dt_util.utcnow()
        return session

    def _fence_session(self, session: Session, detail: str, *, closed: bool = False) -> None:
        try:
            session.send({"type": "fenced", "trusted": session.trusted, "detail": detail, "closed": closed})
        except Exception:  # noqa: BLE001 - a closed connection must not break the fence
            _LOGGER.debug("Could not notify session %s", session.session_id[:8])

    # ------------------------------------------------------------ pairing
    def pair(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        """An administrator pairs the App from its interface: a new token; every other session is fenced."""
        session = self._session(params["session_id"], connection)
        if self.paired and not params.get("replace") and not session.trusted:
            raise HubError(P.REJECT_ALREADY_PAIRED, "already paired with another App installation; confirm that "
                                                    "this App replaces it")
        token = secrets.token_hex(16)
        now = dt_util.utcnow()
        by = user_id or (params.get("requested_by") if isinstance(params.get("requested_by"), str) else None)
        self.pairing = {"hash": P.token_hash(token), "paired_at": _iso(now), "by": by,
                        "instance": session.instance_id[:8], "automatic": bool(params.get("automatic"))}
        self.ever_paired = True
        if self.lease_session is not None and self.lease_session != session.session_id:
            self._clear_lease("paired_again")
        for other in self.sessions.values():
            was = other.trusted
            other.trusted = other is session
            if was and other is not session:
                self._fence_session(other, "Another App installation was paired with this integration.")
        self._log("paired", by=by, instance=session.instance_id[:8])
        self._save(now=True)
        async_dispatcher_send(self.hass, SIGNAL_FRESHNESS)
        _LOGGER.warning("spc_paired instance=%s by=%s", session.instance_id[:8], by)
        return {"pairing_token": token, "paired_at": self.pairing["paired_at"]}

    def unpair(self, connection: Any, params: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        session = self._session(params["session_id"], connection)
        if not session.trusted:
            raise HubError(P.REJECT_NOT_PAIRED, "only the paired App can unpair itself")
        self.pairing = None
        self._clear_lease("unpaired")
        for other in self.sessions.values():
            other.trusted = False
        self._log("unpaired", by=user_id)
        self._save(now=True)
        async_dispatcher_send(self.hass, SIGNAL_FRESHNESS)
        _LOGGER.warning("spc_unpaired by=%s", user_id)
        return {"paired": False}

    # -------------------------------------------------------------- lease
    def claim(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        session = self._session(params["session_id"], connection)
        if not session.trusted:
            self._refuse(P.REJECT_NOT_PAIRED)
            raise HubError(P.REJECT_NOT_PAIRED, "this App is not paired: pair it in the App's Settings")
        now = dt_util.utcnow()
        if self.lease_valid(now) and self.lease_session != session.session_id:
            self._refuse(P.REJECT_BUSY)
            raise HubError(P.REJECT_BUSY, "another App session is publishing (its lease is valid)")
        token = secrets.token_hex(16)
        self.epoch += 1
        self.lease_session = session.session_id
        self.lease_hash = P.token_hash(token)
        self.lease_until = now + timedelta(seconds=P.LEASE_SECONDS)
        self.seq = 0
        self._log("claimed", instance=session.instance_id[:8])
        self._schedule_lease()
        self._save(now=True)
        self._freshness_changed()
        _LOGGER.info("spc_claim epoch=%d instance=%s", self.epoch, session.instance_id[:8])
        return {"epoch": self.epoch, "lease_token": token, "lease_until": _iso(self.lease_until),
                "lease_seconds": P.LEASE_SECONDS, "heartbeat_seconds": P.HEARTBEAT_SECONDS}

    def _refuse(self, code: str) -> None:
        self.refused[code] = self.refused.get(code, 0) + 1

    def _accept(self, connection: Any, params: dict[str, Any]) -> Session:
        """The fence: this session, the current epoch, the lease token, a valid lease and a newer sequence."""
        try:
            epoch, token, seq = P.check_fence(params)
            session = self._session(params["session_id"], connection)
            if self.lease_session != session.session_id:
                raise HubError(P.REJECT_NOT_OWNER, "this session does not hold the lease")
            if epoch != self.epoch:
                raise HubError(P.REJECT_STALE_EPOCH, f"epoch {epoch} is not the current one ({self.epoch})")
            if self.lease_hash is None or not secrets.compare_digest(P.token_hash(token), self.lease_hash):
                raise HubError(P.REJECT_LEASE, "invalid lease token")
            if not self.lease_valid():
                raise HubError(P.REJECT_LEASE, "the lease has expired")
            if seq <= self.seq:
                raise HubError(P.REJECT_SEQUENCE, f"sequence {seq} is not newer than {self.seq}")
        except P.ProtocolError as err:
            self._refuse(err.code)
            raise HubError(err.code, str(err)) from err
        except HubError as err:
            self._refuse(err.code)
            _LOGGER.debug("spc_refused code=%s detail=%s", err.code, err)
            raise
        self.seq = seq
        self.lease_until = dt_util.utcnow() + timedelta(seconds=P.LEASE_SECONDS)
        self._schedule_lease()
        return session

    def heartbeat(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._accept(connection, params)
        self._freshness_changed()
        return {"seq": self.seq, "lease_until": _iso(self.lease_until)}

    def release(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        self._accept(connection, params)
        self._clear_lease("released")
        return {"released": True}

    def _clear_lease(self, reason: str) -> None:
        had = self.lease_session is not None
        self.lease_session = self.lease_hash = self.lease_until = None
        self.seq = 0
        if self._lease_timer:
            self._lease_timer()
            self._lease_timer = None
        if had:
            self._log(reason)
            self._save()
        self._freshness_changed()

    def _schedule_lease(self) -> None:
        if self._lease_timer:
            self._lease_timer()
        if self.lease_until is None:
            self._lease_timer = None
            return
        self._lease_timer = async_track_point_in_utc_time(
            self.hass, self._lease_check, self.lease_until + timedelta(seconds=1))

    @callback
    def _lease_check(self, now: datetime) -> None:
        self._lease_timer = None
        if self.lease_session is not None and not self.lease_valid(now):
            _LOGGER.warning("spc_lease_expired epoch=%d: the room entities are unavailable until the App publishes "
                            "again", self.epoch)
            self._clear_lease("lease_expired")

    # ------------------------------------------------------------ publish
    def publish(self, connection: Any, params: dict[str, Any]) -> dict[str, Any]:
        """A complete snapshot from the lease holder (validated before the fence advances the sequence)."""
        try:
            views, deleted = P.check_snapshot({"rooms": params.get("rooms"), "deleted": params.get("deleted")})
        except P.ProtocolError as err:
            self._refuse(err.code)
            raise HubError(err.code, str(err)) from err
        app_version = params.get("app_version")
        self._accept(connection, params)
        before = self._structure()
        # A room missing from the snapshot keeps its entities, unavailable (a reset App, an error, an import): only
        # rooms an administrator deleted in the App lose them. Orphans can also be deleted as devices in HA.
        self.orphaned = {room_id: room for room_id, room in self.known().items()
                         if room_id not in views and room_id not in deleted}
        self.views = views
        self.deleted = (self.deleted | set(deleted)) - set(views)
        self.views_epoch = self.epoch
        self.published_at = dt_util.utcnow()
        if P.valid_version(app_version):
            self.app_version = app_version
        self._save()
        if self._structure() != before or deleted:
            async_dispatcher_send(self.hass, SIGNAL_ROOMS)
        self._was_fresh = self.fresh
        async_dispatcher_send(self.hass, SIGNAL_VIEWS)
        return {"seq": self.seq, "lease_until": _iso(self.lease_until)}

    # ------------------------------------------------------------ reading
    def status(self) -> dict[str, Any]:
        return {
            "version": VERSION, "protocol": P.PROTOCOL_VERSION, "paired": self.paired,
            "paired_at": (self.pairing or {}).get("paired_at"), "paired_by": (self.pairing or {}).get("by"),
            "epoch": self.epoch, "fresh": self.fresh, "lease_until": _iso(self.lease_until), "seq": self.seq,
            "published_at": _iso(self.published_at), "app_version": self.app_version,
            "rooms": len(self.views), "entities": sum(len(room["entities"]) for room in self.views.values()),
            "orphaned": len(self.orphaned),
            "sessions": [s.as_dict() for s in self.sessions.values()], "refused": dict(self.refused),
            "history": list(self.history)[:10],
        }
