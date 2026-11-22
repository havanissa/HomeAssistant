"""Fixtures of the App tests: a fake Home Assistant client (allowlist-checked), a fake companion and a clock.

``FakeHA`` stands in for ``spc.ha.HomeAssistantClient``. Everything a test can make it answer:

* ``fake.states`` / ``fake.set(entity_id, state, at, ...)`` - current states (a ``state`` listener event when
  connected); ``fake.areas``, ``fake.devices``, ``fake.entity_registry``, ``fake.config``, ``fake.users``;
* ``fake.history[entity_id] = [state, ...]`` - the answer of ``history/history_during_period`` (per requested
  entity id, as given: no filtering by time);
* ``fake.statistics[statistic_id] = [row, ...]`` - ``recorder/statistics_during_period``;
  ``fake.statistic_ids`` (default: one entry per key of ``fake.statistics``) - ``recorder/list_statistic_ids``;
* ``fake.forecasts[entity_id] = [...]`` - the first event of a ``weather/subscribe_forecast`` subscription
  (``None``/missing: the entity has no forecast); ``fake.push_forecast(entity_id, forecast)`` sends an update;
* ``fake.errors[command_type] = HomeAssistantError(...)`` (or any exception) - that command fails;
* ``fake.calls`` - every command and subscription sent, as ``(command_type, params)``.

A command or subscription outside ``spc.ha``'s allowlist raises ``NotAllowed``, exactly like the real client.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
import secrets
import sys
import types
from typing import Any

import pytest

from spc import protocol as P
from spc.ha import (
    ALLOWED_COMMANDS,
    ALLOWED_EVENT_TYPES,
    COMPANION_SUBSCRIPTION,
    FORECAST_SUBSCRIPTION,
    FORECAST_TYPES,
    SUBSCRIPTIONS,
    HomeAssistantError,
    NotAllowed,
)

T0 = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
KAMAR = "kamar_sudur"
STOVA = "stova"


class Clock:
    """A settable ``utcnow``: ``monkeypatch.setattr(module, "utcnow", clock)``."""

    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock(monkeypatch) -> Clock:
    """The store's clock (other modules: patch their own ``utcnow``/``now`` with this object)."""
    c = Clock()
    import spc.store

    monkeypatch.setattr(spc.store, "utcnow", c)
    return c


def entity(platform: str = "sensor", name: str = "Forecast in 1 hour", value: Any = 21.4, **extra: Any) -> dict[str, Any]:
    """A published entity (``protocol.py``); the default is a forecast temperature."""
    if platform == "sensor" and not extra:
        extra = {"device_class": "temperature", "unit": "°C", "state_class": "measurement"}
    return {"platform": platform, "name": name, "value": value, **extra}


def snapshot(*room_ids: str, value: Any = 21.4) -> dict[str, Any]:
    """A valid snapshot with one forecast sensor per room."""
    return {"rooms": {room_id: {"name": room_id.replace("_", " ").title(),
                                "entities": {"forecast_1h": entity(value=value)}} for room_id in room_ids or (KAMAR,)}}


class FakeCompanion:
    """The companion's rules (pairing, lease, epoch, sequence, validation, freshness), in memory."""

    def __init__(self) -> None:
        self.installed = True
        self.loaded = True
        self.pairing_hash: str | None = None
        self.epoch = 0
        self.lease: tuple[str, str] | None = None  # (session, token)
        self.seq = 0
        self.sessions: dict[str, Callable[[dict], None]] = {}
        self.trusted: dict[str, bool] = {}
        self.rooms: dict[str, Any] = {}
        self.orphaned: dict[str, Any] = {}
        self.deleted: list[str] = []      # every room id ever deleted
        self.views_epoch = 0
        self.publishes = 0
        self.heartbeats = 0
        self.refused: Counter[str] = Counter()
        self.version = "0.1.0"
        self.ever_paired = False
        self.automatic = False
        self.protocol = P.PROTOCOL_VERSION

    @property
    def fresh(self) -> bool:
        """What the entities show: available only with a lease and a snapshot accepted in its epoch."""
        return self.lease is not None and self.views_epoch == self.epoch

    def session(self, params: dict[str, Any], send: Callable[[dict], None]) -> str:
        if params.get("protocol") != self.protocol:
            raise HomeAssistantError(P.REJECT_VERSION, "protocol mismatch")
        session_id = secrets.token_hex(16)
        token = params.get("pairing_token")
        trusted = bool(self.pairing_hash and token and P.token_hash(token) == self.pairing_hash)
        self.sessions[session_id] = send
        self.trusted[session_id] = trusted
        send({"type": "hello", "session_id": session_id, "paired": bool(self.pairing_hash), "trusted": trusted,
              "companion_version": self.version, "protocol": self.protocol, "epoch": self.epoch,
              "fresh_store": not self.ever_paired})
        return session_id

    def close(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        if self.lease and self.lease[0] == session_id:
            self.lease = None

    def _refuse(self, code: str, message: str) -> HomeAssistantError:
        self.refused[code] += 1
        return HomeAssistantError(code, message)

    def command(self, kind: str, params: dict[str, Any]) -> Any:
        session_id = params.get("session_id")
        if session_id not in self.sessions:
            raise self._refuse(P.REJECT_UNKNOWN_SESSION, "unknown session")
        if kind == "pair":
            if self.pairing_hash and not params.get("replace") and not self.trusted[session_id]:
                raise self._refuse(P.REJECT_ALREADY_PAIRED, "already paired")
            token = secrets.token_hex(16)
            self.pairing_hash = P.token_hash(token)
            self.ever_paired = True
            self.automatic = bool(params.get("automatic"))
            self.requested_by = params.get("requested_by")
            for s in self.trusted:
                self.trusted[s] = s == session_id
            return {"pairing_token": token}
        if kind == "unpair":
            if not self.trusted[session_id]:
                raise self._refuse(P.REJECT_NOT_PAIRED, "only the paired App can unpair itself")
            self.pairing_hash = None
            self.lease = None
            self.trusted = dict.fromkeys(self.trusted, False)
            return {"paired": False}
        if kind == "claim":
            if not self.trusted[session_id]:
                raise self._refuse(P.REJECT_NOT_PAIRED, "not paired")
            if self.lease is not None and self.lease[0] != session_id:
                raise self._refuse(P.REJECT_BUSY, "another session holds the lease")
            token = secrets.token_hex(16)
            self.epoch += 1
            self.lease = (session_id, token)
            self.seq = 0
            return {"epoch": self.epoch, "lease_token": token}
        # fenced commands
        if kind == "publish":  # validated before the fence advances the sequence
            try:
                rooms, deleted = P.check_snapshot({"rooms": params.get("rooms"), "deleted": params.get("deleted")})
            except P.ProtocolError as err:
                raise self._refuse(err.code, str(err)) from err
        try:
            epoch, token, seq = P.check_fence(params)
        except P.ProtocolError as err:
            raise self._refuse(err.code, str(err)) from err
        if self.lease is None or self.lease[0] != session_id:
            raise self._refuse(P.REJECT_NOT_OWNER, "not owner")
        if epoch != self.epoch:
            raise self._refuse(P.REJECT_STALE_EPOCH, "stale")
        if token != self.lease[1]:
            raise self._refuse(P.REJECT_LEASE, "lease")
        if seq <= self.seq:
            raise self._refuse(P.REJECT_SEQUENCE, "seq")
        self.seq = seq
        if kind == "publish":
            known = {**self.orphaned, **self.rooms}
            self.orphaned = {r: v for r, v in known.items() if r not in rooms and r not in deleted}
            self.rooms = rooms
            self.deleted += deleted
            self.views_epoch = self.epoch
            self.publishes += 1
        elif kind == "heartbeat":
            self.heartbeats += 1
        elif kind == "release":
            self.lease = None
        return {"seq": self.seq}

    def expire_lease(self) -> None:
        self.lease = None

    def status(self) -> dict[str, Any]:
        return {"paired": bool(self.pairing_hash), "version": self.version, "protocol": self.protocol,
                "fresh": self.fresh, "epoch": self.epoch, "rooms": len(self.rooms),
                "entities": sum(len(r["entities"]) for r in self.rooms.values()), "orphaned": len(self.orphaned)}


class FakeHA:
    """What the core, the companion link and the web API use of ``HomeAssistantClient``."""

    def __init__(self) -> None:
        self.connected = False
        self.ha_version = "2026.11.1"
        self.config: dict[str, Any] = {"state": "RUNNING", "time_zone": "UTC", "latitude": 62.0, "longitude": -6.8,
                                       "elevation": 30, "unit_system": {"temperature": "°C"}}
        self.states: dict[str, dict[str, Any]] = {}
        self.areas = [{"area_id": KAMAR, "name": "Kamar Suður"}, {"area_id": STOVA, "name": "Stova"},
                      {"area_id": "gongd", "name": "Gongd"}]
        self.devices: list[dict[str, Any]] = []
        self.entity_registry: list[dict[str, Any]] = []
        self.users = [{"id": "admin", "is_owner": True, "is_active": True},
                      {"id": "guest", "is_active": True, "group_ids": ["system-users"]}]
        self.history: dict[str, list[dict[str, Any]]] = {}
        self.statistics: dict[str, list[dict[str, Any]]] = {}
        self.statistic_ids: list[dict[str, Any]] | None = None
        self.forecasts: dict[str, list[dict[str, Any]] | None] = {}
        self.errors: dict[str, Exception] = {}
        self.companion = FakeCompanion()
        self.commands_sent: Counter[str] = Counter()
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._listeners: list[Callable[[str, Any], None]] = []
        self._subs: dict[int, tuple[str, dict[str, Any], Callable[[Any], None], str | None]] = {}
        self._next_sub = 0

    # ------------------------------------------------------------- the client's API
    def add_listener(self, listener: Callable[[str, Any], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def status(self) -> dict[str, Any]:
        return {"state": "connected" if self.connected else "disconnected", "connected": self.connected,
                "ha_version": self.ha_version, "last_error": None}

    def _check(self, command_type: str, params: dict[str, Any]) -> None:
        """The real client's allowlist (``HomeAssistantClient._send``)."""
        if command_type not in ALLOWED_COMMANDS:
            raise NotAllowed(f"Command {command_type!r} is not allowed")
        if command_type == "subscribe_events" and params.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise NotAllowed(f"Subscribing to {params.get('event_type')!r} is not allowed")
        if command_type == FORECAST_SUBSCRIPTION:
            entity_id = params.get("entity_id")
            if set(params) != {"forecast_type", "entity_id"} or params.get("forecast_type") not in FORECAST_TYPES \
                    or not isinstance(entity_id, str) or not entity_id.startswith("weather."):
                raise NotAllowed("A forecast subscription needs forecast_type hourly or daily and a weather entity")
        if not self.connected:
            raise ConnectionError("Not connected to Home Assistant")
        self.commands_sent[command_type] += 1
        self.calls.append((command_type, dict(params)))
        if command_type in self.errors:
            raise self.errors[command_type]

    async def command(self, command_type: str, **params: Any) -> Any:
        if command_type in SUBSCRIPTIONS:
            raise NotAllowed(f"{command_type!r} is a subscription; use subscribe()")
        self._check(command_type, params)
        await asyncio.sleep(0)
        if command_type == "get_config":
            return self.config
        if command_type == "get_states":
            return list(self.states.values())
        if command_type == "config/area_registry/list":
            return self.areas
        if command_type == "config/device_registry/list":
            return self.devices
        if command_type == "config/entity_registry/list":
            return self.entity_registry
        if command_type == "config/auth/list":
            return self.users
        if command_type == "history/history_during_period":
            return {e: list(self.history[e]) for e in params.get("entity_ids") or [] if e in self.history}
        if command_type == "recorder/statistics_during_period":
            return {s: list(self.statistics[s]) for s in params.get("statistic_ids") or [] if s in self.statistics}
        if command_type == "recorder/list_statistic_ids":
            if self.statistic_ids is not None:
                return self.statistic_ids
            return [{"statistic_id": s, "has_mean": True, "has_sum": False, "source": "recorder",
                     "statistics_unit_of_measurement": "°C", "unit_class": "temperature"} for s in self.statistics]
        if command_type == "manifest/get":
            if not self.companion.installed or params.get("integration") != P.DOMAIN:
                raise HomeAssistantError("not_found", "Integration not found")
            return {"domain": P.DOMAIN, "version": self.companion.version}
        if command_type == f"{P.DOMAIN}/status":
            if not self.companion.loaded:
                raise HomeAssistantError("unknown_command", "Unknown command.")
            return self.companion.status()
        if command_type.startswith(P.DOMAIN + "/"):
            if not self.companion.loaded:
                raise HomeAssistantError("unknown_command", "Unknown command.")
            return self.companion.command(command_type.split("/", 1)[1], params)
        return None

    async def subscribe(self, command_type: str, params: dict[str, Any],
                        handler: Callable[[Any], None]) -> Callable[[], Awaitable[None]]:
        if command_type not in SUBSCRIPTIONS:
            raise NotAllowed(f"Subscribing with {command_type!r} is not allowed")
        params = dict(params or {})
        self._check(command_type, params)
        await asyncio.sleep(0)
        self._next_sub += 1
        sub_id = self._next_sub
        loop = asyncio.get_running_loop()

        def deliver(event: Any) -> None:
            if sub_id in self._subs:
                loop.call_soon(lambda: sub_id in self._subs and handler(event))

        session_id = None
        if command_type == COMPANION_SUBSCRIPTION:
            if not self.companion.loaded:
                raise HomeAssistantError("unknown_command", "Unknown command.")
            self._subs[sub_id] = (command_type, params, handler, None)
            try:
                session_id = self.companion.session(params, deliver)
            except HomeAssistantError:
                del self._subs[sub_id]
                raise
            self._subs[sub_id] = (command_type, params, handler, session_id)
        else:
            if params["entity_id"] not in self.forecasts and params["entity_id"] not in self.states:
                raise HomeAssistantError("invalid_entity_id", "Entity not found")
            self._subs[sub_id] = (command_type, params, handler, None)
            deliver({"type": params["forecast_type"], "forecast": self.forecasts.get(params["entity_id"])})

        async def unsubscribe() -> None:
            sub = self._subs.pop(sub_id, None)
            if sub is not None and sub[3] is not None:
                self.companion.close(sub[3])

        return unsubscribe

    # ------------------------------------------------------------- what a test drives
    def _notify(self, kind: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            listener(kind, payload)

    def connect(self) -> None:
        self.connected = True
        self._notify("status", self.status())

    def disconnect(self) -> None:
        """The connection ends: every subscription and companion session with it (nothing is re-subscribed)."""
        self.connected = False
        self._subs.clear()
        self.companion.sessions.clear()
        self.companion.lease = None
        self._notify("status", self.status())

    def set(self, entity_id: str, state: str | None, at: datetime | None = None, name: str | None = None,
            attributes: dict[str, Any] | None = None) -> None:
        at = at or T0
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

    def push_forecast(self, entity_id: str, forecast: list[dict[str, Any]] | None) -> None:
        """A new forecast for the entity: stored, and sent to everyone subscribed to it."""
        self.forecasts[entity_id] = forecast
        for command_type, params, handler, _ in list(self._subs.values()):
            if command_type == FORECAST_SUBSCRIPTION and params["entity_id"] == entity_id:
                handler({"type": params["forecast_type"], "forecast": forecast})

    def subscriptions(self, command_type: str | None = None) -> list[dict[str, Any]]:
        """The parameters of the open subscriptions."""
        return [params for kind, params, _, _ in self._subs.values() if command_type in (None, kind)]


@pytest.fixture
def ha() -> FakeHA:
    return FakeHA()


# ----------------------------------------------------------------- stand-ins for the climate modules
def stub_config_module() -> types.ModuleType:
    """A minimal ``spc.config`` (``validate_config`` / ``empty_config``) for the infrastructure tests."""
    from spc.store import ConfigError

    module = types.ModuleType("spc.config")

    def empty_config() -> dict[str, Any]:
        return {"rooms": {}}

    def validate_config(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) - {"rooms", "note"} or not isinstance(raw.get("rooms", {}), dict):
            raise ConfigError("invalid", "The configuration is not valid",
                              problems=[{"path": "", "message": "must be an object with rooms"}])
        out: dict[str, Any] = {"rooms": {str(k): dict(v) for k, v in raw.get("rooms", {}).items()}}
        if "note" in raw:
            out["note"] = str(raw["note"])
        return out

    module.empty_config = empty_config
    module.validate_config = validate_config
    return module


@pytest.fixture
def stub_config(monkeypatch) -> types.ModuleType:
    """``spc.base`` with a stand-in for ``spc.config`` (the real one is not needed by the infrastructure)."""
    module = stub_config_module()
    if "spc.config" not in sys.modules:
        try:
            import spc.config  # noqa: F401
        except ImportError:
            monkeypatch.setitem(sys.modules, "spc.config", module)
    import spc.base

    monkeypatch.setattr(spc.base, "config", module)
    return module


@pytest.fixture
def stub_api(monkeypatch) -> types.ModuleType:
    """A stand-in for ``spc.api``: records how ``build_app`` calls ``add_routes`` and adds three test routes."""
    import spc

    module = types.ModuleType("spc.api")
    module.calls = []

    def add_routes(router, core, *, body, require_admin, json) -> None:
        module.calls.append({"router": router, "core": core, "body": body, "require_admin": require_admin,
                             "json": json})

        async def echo(request):
            data, user = await body(request)
            return json({"data": data, "user": user})

        async def big(request):
            data, user = await body(request, 2 * 1024 * 1024)
            return json({"size": len(data.get("blob", "")), "user": user})

        async def secret(request):
            return json({"user": await require_admin(request)})

        async def fail(request):
            from spc.publisher import LinkError
            from spc.store import ConfigError

            if request.query.get("kind") == "link":
                raise LinkError("busy", "The companion is busy")
            raise ConfigError("stale", "Changed meanwhile", 409, problems=[{"path": "x", "message": "y"}],
                              revision="abc")

        router.add_post("/api/echo", echo)
        router.add_post("/api/big", big)
        router.add_get("/api/secret", secret)
        router.add_get("/api/fail", fail)

    module.add_routes = add_routes
    monkeypatch.setitem(sys.modules, "spc.api", module)
    monkeypatch.setattr(spc, "api", module, raising=False)
    return module


async def settle(turns: int = 5) -> None:
    for _ in range(turns):
        await asyncio.sleep(0)


async def wait_for(predicate: Callable[[], Any], timeout: float = 2.0) -> None:
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")
