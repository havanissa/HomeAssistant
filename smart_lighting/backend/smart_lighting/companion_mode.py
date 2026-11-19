"""Companion mode of a runtime (Phase A6): one domain's lease with the companion integration.

Once the monolithic integration is replaced by the companion, a runtime no
longer shadows anything: it is the only engine of its domain. This helper
holds the domain's lease and does what every runtime needs the same way:

* claim the domain when the companion session is trusted and the App is the
  configuration authority (again after every App restart, after a lost lease
  and after a revoke that was followed by a new pairing);
* send the owner's commands (publish, execute, heartbeat, release) with the
  lease's epoch, token and sequence number, and stop the runtime's instance
  at once when the companion says the App is no longer the owner;
* publish the domain's view (what the native entities show) and its command
  targets, debounced.

The runtime provides a small interface: ``companion_can_claim()``,
``companion_targets()``, ``companion_revision()``, ``companion_view()``,
``companion_start_owner()``, ``companion_stop_owner()`` and
``companion_config_changed(options)``.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import UTC, datetime
import logging
from typing import Any

from . import companion_protocol as P
from .companion_client import CompanionClient, CompanionError, Lease
from .core_config import CoreConfigStore

_LOGGER = logging.getLogger(__name__)

RECLAIM_DELAYS = (3, 5, 10, 30, 60)
PUBLISH_DELAY = 1.0


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


class CompanionMode:
    def __init__(self, runtime: Any, domain: str, companion: CompanionClient, core_config: CoreConfigStore) -> None:
        self.runtime = runtime
        self.domain = domain
        self.companion = companion
        self.core_config = core_config
        self.lease: Lease | None = None
        self.state = "waiting"  # waiting | absent | unpaired | no_config | claiming | owner | lost | released
        self.error: str | None = None
        self.counters: Counter[str] = Counter()
        self.last_claim: dict[str, Any] | None = None
        self.last_publish_at: str | None = None
        self.last_publish_error: str | None = None
        self.auto_claim = True  # a release turns it off until an administrator takes control again
        self._unsubs: list[Any] = []
        self._claim_task: asyncio.Task[None] | None = None
        self._claim_handle: asyncio.TimerHandle | None = None
        self._publish_handle: asyncio.TimerHandle | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._attempt = 0
        self._stopping = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._unsubs.append(self.companion.add_listener(self._on_companion))
        self._unsubs.append(self.core_config.add_listener(self._on_config))
        self._on_companion("state", self.companion.state)

    async def stop(self) -> None:
        self._stopping = True
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for handle in (self._claim_handle, self._publish_handle):
            if handle:
                handle.cancel()
        for task in (self._claim_task, self._heartbeat):
            if task:
                task.cancel()

    @property
    def active(self) -> bool:
        """The companion is (or may still turn out to be) the installed integration: no legacy bridge."""
        return self.companion.state not in ("absent",)

    @property
    def owner(self) -> bool:
        return self.lease is not None and self.state == "owner"

    def _changed(self) -> None:
        notify = getattr(self.runtime, "_changed", None)
        if notify is not None:
            notify()

    # --------------------------------------------------------------- events
    def _on_companion(self, kind: str, payload: Any) -> None:
        if kind == "state":
            if payload == "absent":
                self.state = "absent"
                self._changed()
                # The installed integration is the monolithic one (or none): the runtime falls back to its legacy
                # bridge (shadow until a reviewed handover) - the migration path of Phase A6.
                absent = getattr(self.runtime, "companion_absent", None)
                if absent is not None:
                    absent()
            elif payload == "present" and self.state in ("waiting", "absent"):
                self.state = "waiting"
        elif kind == "hello":
            self.schedule_claim(0)
        elif kind == "ownership":
            record = (payload or {}).get(self.domain) or {}
            if self.owner and self.lease is not None and (record.get("owner") != P.OWNER_APP
                                                          or record.get("epoch") != self.lease.epoch):
                self._spawn(self.lose(f"the companion changed the owner ({(record.get('history') or [{}])[0].get('reason')})"))
        elif kind == "closed":
            if self.lease is not None:
                self._spawn(self.lose("the companion session closed"))
            else:
                self.state = "waiting"
                self._changed()
        elif kind == "pairing":
            if (payload or {}).get("paired"):
                self.schedule_claim(0)
            elif self.lease is not None:
                self._spawn(self.lose("the App was unpaired"))
            else:
                self.state = "unpaired"
                self._changed()

    def _on_config(self, options: dict[str, Any], reason: str) -> None:
        if self.owner:
            try:
                self.runtime.companion_config_changed(options)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("%s: the configuration change could not be applied", self.domain)
            self.schedule_publish(0)
        else:
            self.schedule_claim(0)

    # --------------------------------------------------------------- claims
    def schedule_claim(self, delay: float) -> None:
        if self._stopping or self.owner or not self.auto_claim:
            return
        if self._claim_handle is not None:
            self._claim_handle.cancel()
        self._claim_handle = asyncio.get_running_loop().call_later(delay, self._start_claim)

    def _start_claim(self) -> None:
        self._claim_handle = None
        if self._claim_task is None or self._claim_task.done():
            self._claim_task = asyncio.get_running_loop().create_task(self.claim())

    async def claim(self, user_id: str | None = None, *, manual: bool = False) -> dict[str, Any]:
        """Take the domain (the companion decides): on success the runtime's owner instance starts."""
        async with self._lock:
            if self.owner:
                return {"claimed": True, "already": True, "epoch": self.lease.epoch if self.lease else None}
            if manual:
                self.auto_claim = True
            if not self.companion.ready:
                self.state = "unpaired" if (self.companion.present and not self.companion.trusted) else "waiting"
                self.error = None
                self._changed()
                if manual:
                    raise CompanionError(P.REJECT_NOT_PAIRED if self.state == "unpaired" else "not_connected",
                                         "The App is not paired with the companion" if self.state == "unpaired"
                                         else "The companion is not connected")
                return {"claimed": False, "state": self.state}
            if (why := self.runtime.companion_can_claim()) is not None:
                self.state, self.error = "no_config", why
                self._changed()
                if manual:
                    raise CompanionError("no_config", why)
                return {"claimed": False, "state": self.state, "error": why}
            self.state = "claiming"
            try:
                lease = await self.companion.claim(self.domain, config_revision=self.runtime.companion_revision(),
                                                   targets=self.runtime.companion_targets(), user_id=user_id)
            except CompanionError as err:
                self.error = f"{err.code}: {err}"
                self.counters["claim_refused"] += 1
                self.last_claim = {"at": iso(datetime.now(UTC)), "ok": False, "code": err.code, "detail": str(err)}
                self.state = "unpaired" if err.code == P.REJECT_NOT_PAIRED else "waiting"
                self._changed()
                if err.code in (P.REJECT_BUSY, "not_connected"):
                    delay = RECLAIM_DELAYS[min(self._attempt, len(RECLAIM_DELAYS) - 1)]
                    self._attempt += 1
                    self.schedule_claim(delay)
                if manual:
                    raise
                return {"claimed": False, "state": self.state, "error": self.error}
            self._attempt = 0
            self.lease = lease
            self.state, self.error = "owner", None
            self.counters["claims"] += 1
            self.last_claim = {"at": iso(datetime.now(UTC)), "ok": True, "epoch": lease.epoch, "by": user_id}
            try:
                await self.runtime.companion_start_owner()
            except Exception as err:  # noqa: BLE001 - the engine could not start: give the domain up again
                _LOGGER.exception("%s: the owner instance could not start", self.domain)
                self.error = f"start failed: {err}"
                await self._release_lease("the engine could not start")
                self.state = "lost"
                self._changed()
                if manual:
                    raise CompanionError("start_failed", str(err)) from err
                return {"claimed": False, "state": self.state, "error": self.error}
            self._start_heartbeat()
            self.schedule_publish(0)
            _LOGGER.warning("%s: the App controls this domain through the companion (epoch %s)", self.domain,
                            lease.epoch)
            self._changed()
            return {"claimed": True, "epoch": lease.epoch}

    # ------------------------------------------------------------- commands
    async def command(self, name: str, **params: Any) -> Any:
        if self.lease is None or not self.owner:
            raise CompanionError(P.REJECT_NOT_OWNER, f"The App does not control {self.domain}")
        try:
            return await self.companion.owner(name, self.lease, **params)
        except CompanionError as err:
            if err.code in P.LOST_CODES:
                self._spawn(self.lose(f"{name} refused: {err.code}"))
            raise

    async def execute(self, service_domain: str, service: str, data: dict[str, Any], context_id: str, *,
                      purpose: str | None, area_id: str | None) -> Any:
        self.counters["commands"] += 1
        try:
            return await self.command("execute", service_domain=service_domain, service=service, data=data,
                                      context_id=context_id, purpose=purpose, area_id=area_id)
        except CompanionError:
            self.counters["command_errors"] += 1
            raise

    def schedule_publish(self, delay: float = PUBLISH_DELAY) -> None:
        if self._publish_handle is not None or self._stopping:
            return
        self._publish_handle = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self.publish_now()))

    async def publish_now(self) -> None:
        self._publish_handle = None
        if not self.owner:
            return
        try:
            view = self.runtime.companion_view()
        except Exception:  # noqa: BLE001 - a view error must never stop the engine
            _LOGGER.exception("%s: the view could not be built", self.domain)
            return
        if view is None:
            return
        try:
            await self.command("publish", view=view, targets=self.runtime.companion_targets(),
                               config_revision=self.runtime.companion_revision())
        except CompanionError as err:
            self.last_publish_error = f"{err.code}: {err}"
            if self.owner:
                self.schedule_publish(10)
            return
        self.counters["publishes"] += 1
        self.last_publish_at = iso(datetime.now(UTC))
        self.last_publish_error = None

    def _start_heartbeat(self) -> None:
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.get_running_loop().create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self) -> None:
        while self.owner and not self._stopping:
            await asyncio.sleep(P.HEARTBEAT_SECONDS)
            if not self.owner:
                return
            try:
                reply = await self.command("heartbeat")
                if self.lease is not None:
                    self.lease.lease_until = (reply or {}).get("lease_until")
                self.counters["heartbeats"] += 1
            except CompanionError as err:
                self.counters["heartbeat_failed"] += 1
                _LOGGER.debug("%s heartbeat failed: %s", self.domain, err)

    # ------------------------------------------------------------- losing
    async def lose(self, why: str) -> None:
        """No longer the owner: the engine stops at once (nothing more is sent); a new claim follows."""
        if self.lease is None:
            return
        _LOGGER.warning("%s: the App lost control (%s); the engine stops until the domain is claimed again",
                        self.domain, why)
        self.lease = None
        self.state = "lost"
        self.error = why
        self.counters["lost"] += 1
        if self._heartbeat:
            self._heartbeat.cancel()
            self._heartbeat = None
        try:
            await self.runtime.companion_stop_owner(flush=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("%s: the owner instance could not be stopped cleanly", self.domain)
        self._changed()
        self.schedule_claim(RECLAIM_DELAYS[0])

    async def _release_lease(self, detail: str) -> None:
        lease, self.lease = self.lease, None
        if lease is None:
            return
        try:
            await self.companion.owner("release", lease, detail=detail[:200])
        except CompanionError as err:
            _LOGGER.debug("%s release refused: %s", self.domain, err)

    async def release(self, user_id: str | None, detail: str = "released from the App") -> dict[str, Any]:
        """An administrator stops the App's control of this domain (nothing else runs it; nothing is switched)."""
        async with self._lock:
            if self.lease is None:
                return {"released": False}
            self.auto_claim = False
            if self._heartbeat:
                self._heartbeat.cancel()
                self._heartbeat = None
            try:
                await self.runtime.companion_stop_owner(flush=True)
            finally:
                await self._release_lease(detail)
            self.state = "released"
            self.counters["releases"] += 1
            self._changed()
            _LOGGER.warning("%s: the App released control (by %s); nothing controls this domain until it is taken "
                            "again", self.domain, user_id)
            return {"released": True}

    # --------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        record = (self.companion.ownership.get(self.domain) or {}) if self.companion else {}
        return {
            "domain": self.domain, "state": self.state, "owner": self.owner, "auto_claim": self.auto_claim,
            "error": self.error, "epoch": self.lease.epoch if self.lease else None,
            "lease_until": self.lease.lease_until if self.lease else None,
            "companion": self.companion.state, "paired": self.companion.paired, "trusted": self.companion.trusted,
            "record": {k: record.get(k) for k in ("owner", "state", "epoch", "connected", "lease_valid", "executed",
                                                   "execute_errors", "rejections", "published_at", "history")},
            "counters": dict(self.counters), "last_claim": self.last_claim,
            "last_publish_at": self.last_publish_at, "last_publish_error": self.last_publish_error,
        }

    def _spawn(self, coro: Any) -> None:
        spawn = getattr(self.runtime, "_spawn", None)
        if spawn is not None:
            spawn(coro)
        else:
            asyncio.get_running_loop().create_task(coro)
