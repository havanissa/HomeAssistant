"""The App's session with the companion integration (Phase A6).

One session for every control domain. The client probes whether the installed
integration is the companion (``smart_occupancy/companion/subscribe`` exists);
if it is the monolithic 0.9.0 integration instead, the runtimes keep using its
legacy bridges (needed for the migration of an existing installation).

* **Pairing**: an administrator pairs the App once, from the App's interface
  (``pair``); the token is kept in ``/data/companion/pairing.json`` and presented
  by every later session, so a restarted App claims its domains again without a
  human. The companion keeps only a digest.
* **Claims**: each runtime claims its own domain with its own epoch, lease token
  and sequence (:class:`Lease`); the client only carries the messages.
* **Events** from the companion (ownership, renames, actions from dashboard
  cards, recheck, pairing, areas) are delivered to listeners.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any

from . import companion_protocol as P
from .ha import HomeAssistantClient, HomeAssistantError

_LOGGER = logging.getLogger(__name__)

RESUBSCRIBE_DELAYS = (2, 5, 15, 30, 60)
LOG_SIZE = 50
Listener = Callable[[str, Any], None]


class CompanionError(Exception):
    def __init__(self, code: str, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _detail(err: Exception) -> str:
    return str(err).split(": ", 1)[-1]


class Lease:
    """One domain's grant as the companion issued it (epoch, lease token, the next sequence number)."""

    def __init__(self, domain: str, grant: dict[str, Any]) -> None:
        self.domain = domain
        self.epoch = int(grant["epoch"])
        self.token = str(grant["lease_token"])
        self.lease_until: str | None = grant.get("lease_until")
        self.seq = 0

    def params(self, session_id: str) -> dict[str, Any]:
        self.seq += 1
        return {"session_id": session_id, "domain": self.domain, "epoch": self.epoch, "lease_token": self.token,
                "seq": self.seq}


class CompanionClient:
    def __init__(self, ha: HomeAssistantClient, data_dir: Path | None, *, app_version: str) -> None:
        self.ha = ha
        self.dir = data_dir / "companion" if data_dir else None
        self.app_version = app_version
        self.instance_id = secrets.token_hex(16)  # new for every App process: never resumes an old lease
        self.state = "unknown"  # unknown | probing | present | absent | incompatible | error
        self.error: str | None = None
        self.session_id: str | None = None
        self.sub_id: int | None = None
        self.hello: dict[str, Any] = {}
        self.ownership: dict[str, Any] = {}
        self.paired = False
        self.trusted = False
        self.pairing_token: str | None = None
        self.counters: Counter[str] = Counter()
        self.log: deque[dict[str, Any]] = deque(maxlen=LOG_SIZE)
        self._listeners: list[Listener] = []
        self._unsub: Callable[[], None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None
        self._attempt = 0
        self._stopping = False
        self._tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._load_token)
        self._unsub = self.ha.add_listener(self._on_ha)
        if self.ha.connected:
            self._connected()

    async def stop(self) -> None:
        self._stopping = True
        if self._unsub:
            self._unsub()
        for task in (self._subscribe_task, *self._tasks):
            if task:
                task.cancel()

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _notify(self, kind: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Companion listener failed for %s", kind)

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _record(self, event: str, **data: Any) -> None:
        self.log.appendleft({"at": iso(utcnow()), "event": event, **data})

    # ------------------------------------------------------------- pairing
    def _load_token(self) -> None:
        if self.dir is None:
            return
        try:
            data = json.loads((self.dir / "pairing.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            _LOGGER.warning("The pairing token could not be read: %s", err)
            return
        token = data.get("pairing_token") if isinstance(data, dict) else None
        self.pairing_token = token if P.valid_id(token) else None

    def _store_token(self, token: str | None, issued_at: str | None) -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / "pairing.json"
        if token is None:
            path.unlink(missing_ok=True)
            return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pairing_token": token, "issued_at": issued_at}), encoding="utf-8")
        os.replace(tmp, path)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)

    @property
    def present(self) -> bool:
        return self.state == "present"

    @property
    def ready(self) -> bool:
        """A session exists and the companion trusts it: domains can be claimed."""
        return self.present and self.session_id is not None and self.trusted

    # --------------------------------------------------------------- events
    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "status":
            if self.ha.connected:
                self._connected()
            else:
                self._disconnected(str((payload or {}).get("last_error") or "disconnected"))

    def _connected(self) -> None:
        if self.session_id is None and (self._subscribe_task is None or self._subscribe_task.done()):
            self._attempt = 0
            self._subscribe_task = asyncio.get_running_loop().create_task(self._subscribe_loop())

    def _disconnected(self, reason: str) -> None:
        had = self.session_id is not None
        self.session_id = None
        self.sub_id = None
        self.trusted = False
        if self.state == "present":
            self.state = "unknown"
        self.error = reason
        if had:
            self._record("disconnected", reason=reason)
            self._notify("closed", {"reason": reason})
        self._notify("state", self.state)

    async def _subscribe_loop(self) -> None:
        while not self._stopping and self.ha.connected and self.session_id is None:
            self.state = "probing"
            try:
                self.sub_id = await self.ha.subscribe(
                    "smart_occupancy/companion/subscribe", self._on_event, instance_id=self.instance_id,
                    protocol=P.PROTOCOL_VERSION, app_version=self.app_version,
                    **({"pairing_token": self.pairing_token} if self.pairing_token else {}))
                return
            except HomeAssistantError as err:
                if err.code in ("unknown_command", "not_found"):
                    self.state, self.error = "absent", None  # the monolithic integration (or none): legacy bridges
                    self._notify("state", self.state)
                    return
                if err.code == P.REJECT_VERSION:
                    self.state, self.error = "incompatible", _detail(err)
                else:
                    self.state, self.error = "error", f"{err.code}: {_detail(err)}"
            except (ConnectionError, TimeoutError) as err:
                self.state, self.error = "unknown", type(err).__name__
            self._notify("state", self.state)
            await asyncio.sleep(RESUBSCRIBE_DELAYS[min(self._attempt, len(RESUBSCRIBE_DELAYS) - 1)])
            self._attempt += 1

    async def reprobe(self) -> None:
        """After a Home Assistant reload (the integration may have been upgraded): a fresh session."""
        if self.sub_id is not None:
            with contextlib.suppress(Exception):
                await self.ha.unsubscribe(self.sub_id)
        self.session_id = None
        self.sub_id = None
        self.state = "unknown"
        self._connected()

    def _on_event(self, event: Any) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        try:
            if kind == "hello":
                self.hello = dict(event)
                self.session_id = str(event["session_id"])
                self.ownership = dict(event.get("ownership") or {})
                self.paired = bool(event.get("paired"))
                self.trusted = bool(event.get("trusted"))
                self.state, self.error = "present", None
                self.counters["sessions"] += 1
                self._record("session", session=self.session_id[:8], trusted=self.trusted, paired=self.paired)
                _LOGGER.info("Companion session %s: companion %s, paired=%s trusted=%s", self.session_id[:8],
                             event.get("companion_version"), self.paired, self.trusted)
                self._notify("state", self.state)
                self._notify("hello", self.hello)
            elif kind == "ownership":
                self.ownership = dict(event.get("ownership") or {})
                self._notify("ownership", self.ownership)
            elif kind == "pairing":
                self.paired = bool(event.get("paired"))
                if not self.paired:
                    self.trusted = False
                    self.pairing_token = None
                    self._spawn(asyncio.to_thread(self._store_token, None, None))
                self._notify("pairing", {"paired": self.paired})
            elif kind == "closed":
                self._disconnected(f"the companion closed the session ({event.get('reason')})")
                if self.ha.connected and not self._stopping:
                    self._connected()
            elif kind in ("renames", "action", "recheck", "areas"):
                self._notify(kind, event)
        except Exception as err:  # noqa: BLE001 - a bad message never breaks the connection
            self.counters["bad_messages"] += 1
            _LOGGER.warning("Ignored an invalid companion message (%s): %s", kind, err)

    # ------------------------------------------------------------- commands
    async def _command(self, command: str, **params: Any) -> Any:
        if self.session_id is None:
            raise CompanionError("not_connected", "The companion is not connected", 503)
        try:
            return await self.ha.companion_command(f"smart_occupancy/companion/{command}", **params)
        except HomeAssistantError as err:
            raise CompanionError(err.code, _detail(err)) from err
        except (ConnectionError, TimeoutError) as err:
            raise CompanionError("not_connected", "Home Assistant is not connected", 503) from err

    async def pair(self, user_id: str | None) -> dict[str, Any]:
        """An administrator (from the App's interface) pairs this App with the companion."""
        reply = await self._command("pair", session_id=self.session_id, requested_by=user_id)
        token = (reply or {}).get("pairing_token")
        if not P.valid_id(token):
            raise CompanionError("invalid", "The companion did not return a pairing token", 502)
        self.pairing_token = token
        self.paired = self.trusted = True
        await asyncio.to_thread(self._store_token, token, reply.get("issued_at"))
        self.counters["pairings"] += 1
        self._record("paired", by=user_id)
        self._notify("pairing", {"paired": True})
        self._notify("state", self.state)
        return {"paired": True, "issued_at": reply.get("issued_at")}

    async def claim(self, domain: str, *, config_revision: str | None, targets: dict[str, Any] | None,
                    user_id: str | None = None) -> Lease:
        record = self.ownership.get(domain) or {}
        reply = await self._command("claim", session_id=self.session_id, domain=domain,
                                    expected_epoch=int(record.get("epoch") or 0), config_revision=config_revision,
                                    targets=targets, requested_by=user_id)
        self.counters["claims"] += 1
        self._record("claim", domain=domain, epoch=reply.get("epoch"))
        return Lease(domain, reply)

    async def owner(self, command: str, lease: Lease, **params: Any) -> Any:
        return await self._command(command, **lease.params(self.session_id or ""), **params)

    async def publish_system(self, view: dict[str, Any]) -> Any:
        return await self._command("publish", session_id=self.session_id, domain="system", view=view)

    async def action_result(self, action_id: str, *, ok: bool, result: dict[str, Any] | None = None,
                            error: dict[str, Any] | None = None) -> None:
        with contextlib.suppress(CompanionError):
            await self._command("action_result", session_id=self.session_id, action_id=action_id, ok=ok,
                                result=result, error=error)

    async def renames_applied(self) -> None:
        with contextlib.suppress(CompanionError):
            await self._command("renames_applied", session_id=self.session_id)

    async def status(self) -> dict[str, Any] | None:
        try:
            return await self.ha.command("smart_occupancy/companion/status")
        except (HomeAssistantError, ConnectionError, TimeoutError):
            return None

    async def legacy_status(self) -> dict[str, Any]:
        return await self._command("legacy/status")

    async def legacy_export(self) -> dict[str, Any]:
        return await self._command("legacy/export")

    async def legacy_retire(self, expected_options_revision: str | None, user_id: str | None) -> dict[str, Any]:
        return await self._command("legacy/retire", confirm=True, expected_options_revision=expected_options_revision,
                                   requested_by=user_id)

    async def legacy_restore(self, user_id: str | None) -> dict[str, Any]:
        return await self._command("legacy/restore", confirm=True, requested_by=user_id)

    async def revoke(self, domain: str | None) -> dict[str, Any]:
        return await self._command("revoke", domain=domain)

    def brief(self) -> dict[str, Any]:
        return {
            "state": self.state, "error": self.error, "session": self.session_id[:8] if self.session_id else None,
            "paired": self.paired, "trusted": self.trusted, "token_stored": self.pairing_token is not None,
            "companion_version": self.hello.get("companion_version"), "protocol": self.hello.get("protocol"),
            "ha_version": self.hello.get("ha_version"), "boot_id": self.hello.get("boot_id"),
            "ownership": self.ownership, "legacy": self.hello.get("legacy"), "entities": len(self.hello.get("entities") or {}),
            "recorder_oldest": self.hello.get("recorder_oldest"), "counters": dict(self.counters), "log": list(self.log),
        }
