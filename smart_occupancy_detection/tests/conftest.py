"""Fixtures of the App tests: a fake Home Assistant client, a controllable clock and a fake companion."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import secrets
from typing import Any

import pytest

from sod import protocol as P
from sod.ha import ALLOWED_COMMANDS, COMPANION_SUBSCRIPTION, HomeAssistantError, NotAllowed

T0 = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
KAMAR = "kamar_sudur"
STOVA = "stova"


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    import sod.config
    import sod.runtime

    monkeypatch.setattr(sod.runtime, "utcnow", c)
    monkeypatch.setattr(sod.config, "utcnow", c)
    return c


class FakeCompanion:
    """The companion's rules (pairing, lease, epoch, sequence, validation), in memory."""

    def __init__(self) -> None:
        self.installed = True
        self.loaded = True
        self.pairing_hash: str | None = None
        self.epoch = 0
        self.lease: tuple[str, str] | None = None  # (session, token)
        self.seq = 0
        self.sessions: dict[str, Callable[[dict], None]] = {}
        self.views: dict[str, Any] = {}
        self.disabled: dict[str, str] = {}
        self.publishes = 0
        self.version = "0.2.0"
        self.ever_paired = False
        self.protocol = 1

    def session(self, params: dict[str, Any], send: Callable[[dict], None]) -> None:
        session_id = secrets.token_hex(16)
        token = params.get("pairing_token")
        trusted = bool(self.pairing_hash and token and P.token_hash(token) == self.pairing_hash)
        self.sessions[session_id] = send
        self._trusted = getattr(self, "_trusted", {})
        self._trusted[session_id] = trusted
        send({"type": "hello", "session_id": session_id, "paired": bool(self.pairing_hash), "trusted": trusted,
              "companion_version": self.version, "protocol": 1, "epoch": self.epoch,
              "fresh_store": not self.ever_paired})

    def command(self, kind: str, params: dict[str, Any]) -> Any:
        session_id = params.get("session_id")
        if session_id not in self.sessions:
            raise HomeAssistantError(P.REJECT_UNKNOWN_SESSION, "unknown session")
        if kind == "pair":
            if self.pairing_hash and not params.get("replace") and not self._trusted[session_id]:
                raise HomeAssistantError(P.REJECT_ALREADY_PAIRED, "already paired")
            token = secrets.token_hex(16)
            self.pairing_hash = P.token_hash(token)
            self.ever_paired = True
            self.automatic = bool(params.get("automatic"))
            for s in self._trusted:
                self._trusted[s] = s == session_id
            return {"pairing_token": token}
        if kind == "unpair":
            self.pairing_hash = None
            self.lease = None
            return {"paired": False}
        if kind == "claim":
            if not self._trusted[session_id]:
                raise HomeAssistantError(P.REJECT_NOT_PAIRED, "not paired")
            token = secrets.token_hex(16)
            self.epoch += 1
            self.lease = (session_id, token)
            self.seq = 0
            return {"epoch": self.epoch, "lease_token": token}
        # fenced commands
        if self.lease is None or self.lease[0] != session_id:
            raise HomeAssistantError(P.REJECT_NOT_OWNER, "not owner")
        if params["epoch"] != self.epoch:
            raise HomeAssistantError(P.REJECT_STALE_EPOCH, "stale")
        if params["lease_token"] != self.lease[1]:
            raise HomeAssistantError(P.REJECT_LEASE, "lease")
        if params["seq"] <= self.seq:
            raise HomeAssistantError(P.REJECT_SEQUENCE, "seq")
        if kind == "publish":
            try:
                views = P.check_snapshot(params["rooms"])
                disabled = P.check_disabled(params.get("disabled"), views)
                self.deleted = P.check_deleted(params.get("deleted"), views, disabled)
            except P.ProtocolError as err:
                raise HomeAssistantError(err.code, str(err)) from err
            self.views, self.disabled = views, disabled
            self.publishes += 1
        self.seq = params["seq"]
        if kind == "release":
            self.lease = None
        return {"seq": self.seq}

    def expire_lease(self) -> None:
        self.lease = None


class FakeHA:
    """What the runtime, the companion link and the web API use of ``HomeAssistantClient``."""

    def __init__(self) -> None:
        self.connected = False
        self.states: dict[str, dict[str, Any]] = {}
        self.areas = [{"area_id": KAMAR, "name": "Kamar Suður"}, {"area_id": STOVA, "name": "Stova"},
                      {"area_id": "gongd", "name": "Gongd"}]
        self.devices: list[dict[str, Any]] = []
        self.entity_registry: list[dict[str, Any]] = []
        self.users = [{"id": "admin", "is_owner": True, "is_active": True},
                      {"id": "guest", "is_active": True, "group_ids": ["system-users"]}]
        self.companion = FakeCompanion()
        self.commands_sent: Counter[str] = Counter()
        self.ha_version = "2026.11.1"
        self._listeners: list[Callable[[str, Any], None]] = []
        self._subs: dict[int, Callable[[Any], None]] = {}

    def add_listener(self, listener):
        self._listeners.append(listener)
        return lambda: None

    def status(self) -> dict[str, Any]:
        return {"state": "connected" if self.connected else "disconnected", "connected": self.connected,
                "ha_version": self.ha_version, "last_error": None}

    def _notify(self, kind: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            listener(kind, payload)

    def connect(self) -> None:
        self.connected = True
        self._notify("status", self.status())

    def disconnect(self) -> None:
        self.connected = False
        self._subs.clear()
        self.companion.sessions.clear()
        self.companion.lease = None
        self._notify("status", self.status())

    def set(self, entity_id: str, state: str | None, at: datetime, name: str | None = None,
            attributes: dict[str, Any] | None = None) -> None:
        if state is None:
            self.states.pop(entity_id, None)
            new = None
        else:
            new = {"entity_id": entity_id, "state": state, "last_changed": at.isoformat(),
                   "last_updated": at.isoformat(),
                   "attributes": {"friendly_name": name or entity_id, **(attributes or {})}}
            self.states[entity_id] = new
        if self.connected:
            self._notify("state", (entity_id, new))

    async def command(self, command_type: str, **params: Any) -> Any:
        if command_type not in ALLOWED_COMMANDS or command_type == COMPANION_SUBSCRIPTION:
            raise NotAllowed(command_type)
        if not self.connected:
            raise ConnectionError("Not connected")
        self.commands_sent[command_type] += 1
        if command_type == "config/auth/list":
            return self.users
        if command_type == "manifest/get":
            if not self.companion.installed:
                raise HomeAssistantError("not_found", "Integration not found")
            return {"domain": P.DOMAIN}
        if command_type == f"{P.DOMAIN}/status":
            if not self.companion.loaded:
                raise HomeAssistantError("unknown_command", "Unknown command.")
            return {"paired": bool(self.companion.pairing_hash), "version": self.companion.version,
                    "protocol": self.companion.protocol, "fresh": self.companion.lease is not None}
        if command_type.startswith(P.DOMAIN + "/"):
            return self.companion.command(command_type.split("/", 1)[1], params)
        return None

    async def subscribe(self, command_type: str, handler, **params: Any) -> int:
        assert command_type == COMPANION_SUBSCRIPTION
        self.commands_sent[command_type] += 1
        sub_id = len(self._subs) + 1
        self._subs[sub_id] = handler
        loop = asyncio.get_running_loop()
        self.companion.session(params, lambda event: loop.call_soon(handler, event))
        return sub_id


@pytest.fixture
def ha() -> FakeHA:
    return FakeHA()


def room(sensors: list[tuple[str, str, str]] | None = None, **extra: Any) -> dict[str, Any]:
    """A room configuration: sensors as (entity_id, kind, role)."""
    sensors = sensors if sensors is not None else [("binary_sensor.kamar_pir", "pir", "trigger"),
                                                   ("binary_sensor.kamar_radar", "radar", "maintain")]
    return {"name": extra.pop("name", "Kamar Suður"), "enabled": extra.pop("enabled", True),
            "sensors": [{"entity_id": e, "kind": k, "role": r, "inverted": False, "enabled": True}
                        for e, k, r in sensors],
            "activity": extra.pop("activity", []), "timing": extra.pop("timing", {}),
            "adjacent": extra.pop("adjacent", [])}


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
