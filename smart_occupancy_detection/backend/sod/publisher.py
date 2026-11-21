"""The App's side of the publishing protocol (``protocol.py``): native entities through the companion integration.

Steps, each shown in Settings -> Connection:

1. Home Assistant reachable, the companion integration installed
   (``manifest/get``) and set up (its ``status`` command answers);
2. a session (``<domain>/session`` subscription) with this process's random
   instance id and, once paired, the pairing token (``/data/companion.json``,
   readable by the App only);
3. pairing - only when an administrator asks for it in the App (Settings);
4. claim - a new epoch and lease; then every change of a room's decision is
   published as a complete snapshot (coalesced), with a strictly increasing
   sequence; a heartbeat keeps the lease while nothing changes.

A refusal that means "no longer the owner" (``protocol.LOST_CODES``) stops
publishing at once; the App claims again (a new epoch) before it sends anything
else. Nothing is ever replayed: a new claim publishes the current snapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import copy
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any

from collections import deque

from . import compat, protocol as P
from .ha import COMPANION_SUBSCRIPTION, HomeAssistantClient, HomeAssistantError

_LOGGER = logging.getLogger(__name__)

PUBLISH_DELAY = 0.25
RETRY_SECONDS = 30.0
RECLAIM_DELAY = 2.0


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class LinkError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class CompanionLink:
    def __init__(self, ha: HomeAssistantClient, rooms: Callable[[], tuple[dict, dict]], data_dir: Path | None, *,
                 app_version: str, can_publish: Callable[[], str | None]) -> None:
        self.ha = ha
        self._rooms = rooms
        self._can_publish = can_publish
        self.app_version = app_version
        self.path = data_dir / "companion.json" if data_dir else None
        self.instance_id = secrets.token_hex(16)
        self.token: str | None = None
        self.paired_at: str | None = None
        self.state = "waiting"
        self.detail: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.companion: dict[str, Any] = {}
        self.paired = False
        self.trusted = False
        self.epoch: int | None = None
        self.lease_token: str | None = None
        self.seq = 0
        self.last_published: dict[str, Any] | None = None
        self.last_publish_at: str | None = None
        self.last_error: str | None = None
        self.counters = {"publishes": 0, "heartbeats": 0, "refused": 0, "claims": 0, "auto_pairs": 0}
        self.last_ok_at: str | None = None  # the last message the companion accepted
        self.errors: deque[dict[str, Any]] = deque(maxlen=20)
        self.companion_status: dict[str, Any] = {}
        self.compatibility: dict[str, Any] = {"level": "unknown", "message": None}
        self._status_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._publish_handle: asyncio.TimerHandle | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._retry: asyncio.TimerHandle | None = None
        self._claim_retry: asyncio.TimerHandle | None = None
        self._claiming = False
        #: Rooms an administrator deleted, sent with the next snapshot (only these lose their entities).
        self.deleted: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._stopping = False
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ lifecycle
    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("The pairing record is not readable (%s); pair the App again", err)
            return
        if isinstance(data, dict) and P.valid_id(data.get("pairing_token")):
            self.token = data["pairing_token"]
            self.paired_at = data.get("paired_at")

    def _store_token(self, token: str | None) -> None:
        self.token = token
        self.paired_at = _now() if token else None
        if self.path is None:
            return
        if token is None:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pairing_token": token, "paired_at": self.paired_at}, handle)
        os.replace(tmp, self.path)

    async def start(self) -> None:
        await asyncio.to_thread(self.load)
        self.ha.add_listener(self._on_ha)
        if self.ha.connected:
            self._spawn(self._open())

    async def stop(self) -> None:
        self._stopping = True
        for handle in (self._publish_handle, self._retry, self._claim_retry):
            if handle:
                handle.cancel()
        if self._heartbeat:
            self._heartbeat.cancel()
        if self._status_task:
            self._status_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self.lease_token and self.session_id and self.ha.connected:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._send("release", **self._fence()), 3)
        self.lease_token = None

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _error(self, code: str, message: str) -> None:
        self.last_error = f"{code}: {message}"
        self.errors.appendleft({"at": _now(), "code": code, "message": message[:300]})

    def _ok(self) -> None:
        self.last_ok_at = _now()

    def _set(self, state: str, detail: str | None = None) -> None:
        if state in ("error", "not_installed", "not_set_up", "incompatible", "busy") and \
                (state, detail) != (self.state, self.detail):
            self._error(state, detail or state)
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            for listener in list(self._listeners):
                with contextlib.suppress(Exception):
                    listener()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _retry_later(self, delay: float | None = None) -> None:
        delay = RETRY_SECONDS if delay is None else delay
        if self._retry is None and not self._stopping:
            def fire() -> None:
                self._retry = None
                if self.ha.connected and self.session_id is None:
                    self._spawn(self._open())
            self._retry = asyncio.get_running_loop().call_later(delay, fire)

    # --------------------------------------------------------------- session
    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "status":
            if self.ha.connected:
                self._spawn(self._open())
            else:
                self._lost_session("Home Assistant connection lost")
        elif kind == "registries" and self.session_id is None and self.ha.connected:
            self._spawn(self._open())  # the companion may have been set up meanwhile

    def _reopen(self, why: str) -> None:
        """The companion no longer knows this session (it was reloaded, or the session was closed): open a new one."""
        sub_id = self.sub_id
        self._lost_session(why)
        if sub_id is not None:
            self._spawn(self.ha.unsubscribe(sub_id))
        if self.ha.connected and not self._stopping:
            self._spawn(self._open())

    def _lost_session(self, why: str) -> None:
        self.session_id = self.sub_id = None
        self.lease_token = self.epoch = None
        self.trusted = False
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        self._set("waiting", why)

    async def _open(self) -> None:
        async with self._lock:
            if self.session_id is not None or not self.ha.connected or self._stopping:
                return
            try:
                manifest = await self.ha.command("manifest/get", integration=P.DOMAIN) or {}
            except HomeAssistantError:
                self._set("not_installed", "The Smart Occupancy Detection integration is not installed in Home "
                                           "Assistant.")
                self._retry_later()
                return
            except (ConnectionError, TimeoutError) as err:
                self._set("waiting", str(err) or type(err).__name__)
                self._retry_later()
                return
            try:
                status = await self.ha.command(f"{P.DOMAIN}/status") or {}
            except HomeAssistantError as err:
                # Its commands exist once its config entry is set up ("unknown_command" before; "not_loaded" while
                # the entry is not loaded).
                self._set("not_set_up", "The integration is installed but not set up (Settings -> Devices & "
                                        "services -> Add integration -> Smart Occupancy Detection)."
                          if err.code in ("unknown_command", "not_loaded") else f"The integration answered: {err}")
                self._retry_later()
                return
            except (ConnectionError, TimeoutError) as err:
                self._set("waiting", str(err) or type(err).__name__)
                self._retry_later()
                return
            self.companion_status = dict(status)
            self.compatibility = compat.check(status.get("version") or manifest.get("version"), status.get("protocol"))
            if self.compatibility["level"] == "incompatible":
                self._set("incompatible", self.compatibility["message"])
                self._retry_later()
                return
            params: dict[str, Any] = {"protocol": P.PROTOCOL_VERSION, "app_version": self.app_version,
                                      "instance_id": self.instance_id}
            if self.token:
                params["pairing_token"] = self.token
            try:
                self.sub_id = await self.ha.subscribe(COMPANION_SUBSCRIPTION, self._on_event, **params)
            except HomeAssistantError as err:
                self._set("error", f"The companion refused the session: {err.message} ({err.code})")
                self._retry_later()
                return
            except (ConnectionError, TimeoutError) as err:
                self._set("waiting", str(err) or type(err).__name__)
                self._retry_later()

    def _on_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "hello":
            self.session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else None
            self.paired = bool(event.get("paired"))
            self.trusted = bool(event.get("trusted"))
            self.companion = {k: event.get(k) for k in ("companion_version", "protocol", "epoch", "paired_at")}
            if self.trusted:
                self._spawn(self.claim())
            elif not self.paired and self.token:
                # This App was paired before, and the integration lost its pairing (it was removed and added again):
                # pair again by itself. An administrator approved this App once; after an explicit unpair the App
                # forgets its token, so this never happens then.
                self._spawn(self._auto_pair())
            elif self.paired:
                self._set("pair_mismatch", "The companion is paired with another App installation (or this App "
                                           "was reinstalled). Pair again in Settings to publish from this App.")
            else:
                self._set("not_paired", "Pair the App with the companion integration in Settings to create the "
                                        "occupancy entities.")
        elif kind == "fenced":
            if event.get("closed"):
                self._reopen(str(event.get("detail") or "The companion closed the session"))
                return
            # Another session was paired or claimed: this one may no longer publish.
            self.lease_token = self.epoch = None
            self.trusted = bool(event.get("trusted", False))
            self._set("pair_mismatch" if not self.trusted else "waiting", event.get("detail") or "Publishing was "
                                                                                                  "stopped by the companion.")
            if self.trusted:
                self._spawn(self.claim())

    async def _send(self, command: str, **params: Any) -> Any:
        return await self.ha.command(f"{P.DOMAIN}/{command}", **params)

    def _fence(self) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": self.session_id, "epoch": self.epoch, "lease_token": self.lease_token, "seq": self.seq}

    # ------------------------------------------------------------- pairing
    async def pair(self, *, replace: bool, user_id: str | None) -> dict[str, Any]:
        if self.session_id is None:
            await self._open()
        if self.session_id is None:
            raise LinkError(self.state, self.detail or "The companion integration is not reachable")
        try:
            result = await self._send("pair", session_id=self.session_id, replace=bool(replace),
                                      requested_by=user_id)
        except HomeAssistantError as err:
            raise LinkError(err.code, err.message) from err
        token = (result or {}).get("pairing_token")
        if not P.valid_id(token):
            raise LinkError("invalid", "The companion answered without a valid pairing token")
        await asyncio.to_thread(self._store_token, token)
        self.paired = self.trusted = True
        _LOGGER.warning("Paired with the companion integration (requested by %s)", user_id)
        await self.claim()
        return {"paired": True, "state": self.state}

    async def _auto_pair(self) -> None:
        try:
            await self.pair(replace=False, user_id=None)
        except LinkError as err:
            self._set("not_paired", f"Pairing again automatically failed ({err}); pair in Settings.")
            return
        self.counters["auto_pairs"] += 1
        _LOGGER.warning("The integration had lost its pairing (removed and added again?): paired again automatically")

    async def unpair(self, *, user_id: str | None) -> dict[str, Any]:
        if self.session_id is not None:
            try:
                await self._send("unpair", session_id=self.session_id, requested_by=user_id)
            except HomeAssistantError as err:
                raise LinkError(err.code, err.message) from err
        await asyncio.to_thread(self._store_token, None)
        self.paired = self.trusted = False
        self.lease_token = self.epoch = None
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        self._set("not_paired", "Unpaired: the occupancy entities keep their last state and show that they are not "
                                "fresh.")
        return {"paired": False}

    # -------------------------------------------------------------- lease
    async def claim(self) -> None:
        if self.session_id is None or not self.trusted or self._stopping or self._claiming:
            return
        if why := self._can_publish():
            self._set("blocked", why)
            self._retry_claim()
            return
        self._claiming = True
        try:
            result = await self._send("claim", session_id=self.session_id)
        except HomeAssistantError as err:
            self._error(err.code, err.message)
            if err.code == P.REJECT_UNKNOWN_SESSION:
                self._reopen("The companion no longer knows this session")
                return
            if err.code == P.REJECT_BUSY:
                self._set("busy", "Another App session publishes occupancy (is the App running twice?).")
            else:
                self._set("error", f"Claim refused: {err.message}")
            self._retry_claim()
            return
        except (ConnectionError, TimeoutError) as err:
            self._set("waiting", str(err) or type(err).__name__)
            self._retry_claim()
            return
        finally:
            self._claiming = False
        self.epoch, self.lease_token, self.seq = result["epoch"], result["lease_token"], 0
        self._ok()
        if self._status_task is None or self._status_task.done():
            self._status_task = asyncio.get_running_loop().create_task(self._status_loop())
        self.last_published = None
        self.counters["claims"] += 1
        self._set("publishing", None)
        _LOGGER.info("Publishing occupancy to the companion (epoch %s)", self.epoch)
        await self.publish_now()
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    def _retry_claim(self, delay: float | None = None) -> None:
        """One pending retry at most (a new failure does not stack timers)."""
        if self._stopping or self._claim_retry is not None:
            return

        def fire() -> None:
            self._claim_retry = None
            if self.lease_token is None:
                self._spawn(self.claim())

        self._claim_retry = asyncio.get_running_loop().call_later(RETRY_SECONDS if delay is None else delay, fire)

    def _lost(self, err: HomeAssistantError) -> None:
        self.counters["refused"] += 1
        self._error(err.code, err.message)
        self.lease_token = self.epoch = None
        self.last_published = None
        _LOGGER.warning("The companion refused a message (%s: %s); claiming again", err.code, err.message)
        if err.code == P.REJECT_UNKNOWN_SESSION:
            self._reopen("The companion no longer knows this session")
            return
        self._set("waiting", f"The companion refused a message ({err.code}); claiming again")
        self._retry_claim(RECLAIM_DELAY)

    # ------------------------------------------------------------ publish
    def mark_deleted(self, area_ids: list[str]) -> None:
        """An administrator deleted these rooms: their entities are removed with the next snapshot."""
        if area_ids:
            self.deleted.update(area_ids)
            self.schedule_publish()

    def schedule_publish(self) -> None:
        if self.lease_token is None or self._publish_handle is not None or self._stopping:
            return
        self._publish_handle = asyncio.get_running_loop().call_later(
            PUBLISH_DELAY, lambda: self._spawn(self.publish_now()))

    async def publish_now(self) -> None:
        self._publish_handle = None
        async with self._lock:
            if self.lease_token is None or self.session_id is None:
                return
            rooms, disabled = self._rooms()
            deleted = sorted(a for a in self.deleted if a not in rooms and a not in disabled)
            snapshot = {"rooms": rooms, "disabled": disabled, "deleted": deleted}
            if snapshot == self.last_published:
                return
            try:
                P.check_deleted(deleted, P.check_snapshot(rooms), P.check_disabled(disabled, rooms))
            except P.ProtocolError as err:  # a bug: never send what the companion would refuse
                _LOGGER.error("Not publishing an invalid snapshot: %s", err)
                self.last_error = str(err)
                return
            try:
                await self._send("publish", **self._fence(), rooms=rooms, disabled=disabled, deleted=deleted,
                                 app_version=self.app_version)
            except HomeAssistantError as err:
                if err.code in P.LOST_CODES or err.code == P.REJECT_SEQUENCE:
                    self._lost(err)
                else:
                    self.counters["refused"] += 1
                    self._error(err.code, err.message)
                    _LOGGER.error("The companion refused the snapshot: %s", err)
                return
            except (ConnectionError, TimeoutError) as err:
                self.last_error = str(err) or type(err).__name__
                return
            self.last_published = copy.deepcopy(snapshot)
            self._ok()
            self.deleted -= set(deleted)
            self.last_publish_at = _now()
            self.counters["publishes"] += 1

    async def _heartbeat_loop(self) -> None:
        while self.lease_token is not None and not self._stopping:
            await asyncio.sleep(P.HEARTBEAT_SECONDS)
            async with self._lock:
                if self.lease_token is None or self.session_id is None:
                    return
                try:
                    await self._send("heartbeat", **self._fence())
                    self.counters["heartbeats"] += 1
                    self._ok()
                except HomeAssistantError as err:
                    if err.code in P.LOST_CODES or err.code == P.REJECT_SEQUENCE:
                        self._lost(err)
                        return
                    self._error(err.code, err.message)
                except (ConnectionError, TimeoutError) as err:
                    self.last_error = str(err) or type(err).__name__

    async def _status_loop(self) -> None:
        """The companion's own view (its version, freshness, last accepted snapshot), read once a minute."""
        while self.session_id is not None and not self._stopping:
            try:
                self.companion_status = dict(await self.ha.command(f"{P.DOMAIN}/status") or {})
            except (HomeAssistantError, ConnectionError, TimeoutError):
                pass
            await asyncio.sleep(60)

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state, "detail": self.detail, "paired": self.paired, "trusted": self.trusted,
            "has_token": bool(self.token), "paired_at": self.paired_at, "epoch": self.epoch,
            "publishing": self.lease_token is not None, "last_publish_at": self.last_publish_at,
            "last_error": self.last_error, "companion": self.companion, "counters": dict(self.counters),
            "last_ok_at": self.last_ok_at, "errors": list(self.errors), "compatibility": self.compatibility,
            "companion_version": self.companion_status.get("version") or self.companion.get("companion_version"),
            "companion_view": {k: self.companion_status.get(k) for k in (
                "fresh", "epoch", "published_at", "lease_until", "rooms", "disabled", "orphaned", "protocol")},
            "instance": self.instance_id[:8],
        }
