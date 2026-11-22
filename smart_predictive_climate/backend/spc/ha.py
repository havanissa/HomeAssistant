"""Connection to the Home Assistant websocket API, restricted by an allowlist.

Through the Supervisor proxy (``ws://supervisor/core/websocket``) the App acts
as a Home Assistant administrator, so this client enforces a fixed allowlist
before anything is sent:

* :data:`READ_COMMANDS` - reads and event subscriptions (states, registries,
  the user list for the administrator check, recorded history and statistics,
  the companion's status);
* :data:`COMPANION_COMMANDS` and :data:`COMPANION_SUBSCRIPTION` - the companion
  integration's publishing protocol (session, pairing, lease, publish). They
  change only what the companion's own entities show;
* :data:`FORECAST_SUBSCRIPTION` - a weather entity's hourly or daily forecast
  (``weather/subscribe_forecast``; a read).

There is no way to call a service, write a state, fire an event or change a
registry through this client: Smart Adaptive & Predictive Climate observes and
recommends, it never controls a device. Any other command raises
:class:`NotAllowed` before it is sent.

Subscriptions (:meth:`HomeAssistantClient.subscribe`) end with the connection
and are **not** opened again by this client: whoever subscribed does it again
when the ``status`` listener reports ``connected`` (the client's own event
subscriptions for states and registries are the exception).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Awaitable, Callable
import contextlib
from datetime import UTC, datetime
import logging
import time
from typing import Any

import aiohttp

from .protocol import DOMAIN as COMPANION_DOMAIN

_LOGGER = logging.getLogger(__name__)

READ_COMMANDS: frozenset[str] = frozenset({
    "ping",
    "get_config",
    "get_states",
    "subscribe_events",
    "unsubscribe_events",
    "config/area_registry/list",
    "config/device_registry/list",
    "config/entity_registry/list",
    "config/auth/list",
    "manifest/get",
    "history/history_during_period",
    "recorder/statistics_during_period",
    "recorder/list_statistic_ids",
    f"{COMPANION_DOMAIN}/status",
})
COMPANION_COMMANDS: frozenset[str] = frozenset(
    f"{COMPANION_DOMAIN}/{c}" for c in ("pair", "unpair", "claim", "heartbeat", "publish", "release")
)
COMPANION_SUBSCRIPTION = f"{COMPANION_DOMAIN}/session"
FORECAST_SUBSCRIPTION = "weather/subscribe_forecast"
FORECAST_TYPES = ("hourly", "daily")
#: Commands that open a subscription: only through ``subscribe()``.
SUBSCRIPTIONS: frozenset[str] = frozenset({COMPANION_SUBSCRIPTION, FORECAST_SUBSCRIPTION})
ALLOWED_COMMANDS: frozenset[str] = READ_COMMANDS | COMPANION_COMMANDS | SUBSCRIPTIONS
ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({
    "state_changed", "area_registry_updated", "device_registry_updated", "entity_registry_updated",
    "homeassistant_started",
})
REGISTRY_EVENTS = ("area_registry_updated", "device_registry_updated", "entity_registry_updated")

STARTUP_POLL_SECONDS = 5.0
REGISTRY_DEBOUNCE = 2.0
RECONNECT_DELAYS = (1, 2, 5, 10, 30)
AUTH_FAILED_DELAY = 60
PING_INTERVAL = 15.0  # a half-open connection is noticed within 25 s
PING_TIMEOUT = 10.0
COMMAND_TIMEOUT = 30.0
#: Reading weeks of history or statistics may take longer than an ordinary command.
SLOW_COMMANDS: frozenset[str] = frozenset({"history/history_during_period", "recorder/statistics_during_period"})
SLOW_COMMAND_TIMEOUT = 180.0

Listener = Callable[[str, Any], None]


class NotAllowed(RuntimeError):
    """A command outside the allowlist was requested."""


class HomeAssistantError(Exception):
    """Home Assistant answered a command with an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AuthenticationFailed(Exception):
    """Home Assistant rejected the token."""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class HomeAssistantClient:
    """Keeps one authenticated websocket open, with automatic reconnects."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, Callable[[Any], None]] = {}
        self._listeners: list[Listener] = []
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._established = False
        self._started_seen = False
        self._registry_timer: asyncio.TimerHandle | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()

        self.state = "connecting" if token else "not_configured"
        self.last_error: str | None = None if token else "No API token available (SUPERVISOR_TOKEN is not set)"
        self.ha_version: str | None = None
        self.connected_since: str | None = None
        self.last_disconnect: str | None = None
        self.reconnects = 0
        self.latency_ms: float | None = None

        self.config: dict[str, Any] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.areas: list[dict[str, Any]] = []
        self.devices: list[dict[str, Any]] = []
        self.entity_registry: list[dict[str, Any]] = []
        self.commands_sent: Counter[str] = Counter()
        self.history: deque[dict[str, Any]] = deque(maxlen=20)  # connection changes (for diagnostics)
        self.events_total = 0

    # ------------------------------------------------------------------ API
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """``listener(kind, payload)``. Kinds: ``status``, ``state`` (``(entity_id, new_state)``), ``registries``."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state, "connected": self.connected, "ha_version": self.ha_version,
            "connected_since": self.connected_since, "last_disconnect": self.last_disconnect,
            "last_error": self.last_error, "reconnects": self.reconnects, "latency_ms": self.latency_ms,
            "events_total": self.events_total, "history": list(self.history),
        }

    async def start(self) -> None:
        if self._token and self._task is None:
            self._task = asyncio.create_task(self._run(), name="ha-websocket")

    async def stop(self) -> None:
        self._stopping = True
        if self._registry_timer is not None:
            self._registry_timer.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def command(self, command_type: str, **params: Any) -> Any:
        """Send one allowlisted command and return its result.

        Raises :class:`NotAllowed` (not on the allowlist; nothing is sent), ``ConnectionError`` (not connected, or
        the connection closed before the answer), ``TimeoutError`` or :class:`HomeAssistantError` (Home Assistant
        answered with an error).
        """
        if command_type in SUBSCRIPTIONS:
            raise NotAllowed(f"{command_type!r} is a subscription; use subscribe()")
        _, future = await self._send(command_type, params)
        return await asyncio.wait_for(future, SLOW_COMMAND_TIMEOUT if command_type in SLOW_COMMANDS
                                      else COMMAND_TIMEOUT)

    async def subscribe(self, command_type: str, params: dict[str, Any],
                        handler: Callable[[Any], None]) -> Callable[[], Awaitable[None]]:
        """Open one of the two allowed subscriptions; ``handler(event)`` gets every event. Returns an
        ``async unsubscribe()`` (awaiting it more than once, or after the connection ended, does nothing).

        * ``<domain>/session`` - the companion session; ``params``: ``protocol``, ``app_version``,
          ``instance_id`` and, when paired, ``pairing_token``;
        * ``weather/subscribe_forecast`` - ``params`` exactly ``{"forecast_type": "hourly" | "daily",
          "entity_id": "weather.<...>"}``; the events are ``{"type": <forecast type>, "forecast": [...] | None}``.

        Raises like :meth:`command` (:class:`NotAllowed` for anything else, before it is sent).

        A subscription ends with the connection and is **not** opened again by the client: subscribe again when
        the ``status`` listener reports ``connected``.
        """
        if command_type not in SUBSCRIPTIONS:
            raise NotAllowed(f"Subscribing with {command_type!r} is not allowed")
        sub_id = await self._subscribe_command(command_type, dict(params or {}), handler)

        async def unsubscribe() -> None:
            await self._unsubscribe(sub_id, handler)

        return unsubscribe

    async def _unsubscribe(self, sub_id: int, handler: Callable[[Any], None]) -> None:
        # Message ids start again with every connection: only end the subscription this id still belongs to.
        if self._subscriptions.get(sub_id) is not handler:
            return
        del self._subscriptions[sub_id]
        with contextlib.suppress(ConnectionError, TimeoutError, HomeAssistantError):
            _, future = await self._send("unsubscribe_events", {"subscription": sub_id})
            await asyncio.wait_for(future, COMMAND_TIMEOUT)

    # ------------------------------------------------------------ internals
    def _notify(self, kind: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001 - one listener must not break others
                _LOGGER.exception("Listener failed for %s", kind)

    def _set_state(self, state: str, error: str | None = None) -> None:
        changed = state != self.state or error != self.last_error
        if changed and state in ("connected", "disconnected", "auth_failed"):
            self.history.appendleft({"at": utcnow_iso(), "state": state, "error": error})
        self.state = state
        if error is not None or state == "connected":
            self.last_error = error
        if changed:
            self._notify("status", self.status())

    async def _send(self, command_type: str, params: dict[str, Any]) -> tuple[int, asyncio.Future[Any]]:
        if command_type not in ALLOWED_COMMANDS:
            raise NotAllowed(f"Command {command_type!r} is not allowed")
        if command_type == "subscribe_events" and params.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise NotAllowed(f"Subscribing to {params.get('event_type')!r} is not allowed")
        if command_type == FORECAST_SUBSCRIPTION:
            entity_id = params.get("entity_id")
            if set(params) != {"forecast_type", "entity_id"} or params.get("forecast_type") not in FORECAST_TYPES \
                    or not isinstance(entity_id, str) or not entity_id.startswith("weather."):
                raise NotAllowed("A forecast subscription needs forecast_type hourly or daily and a weather entity")
        ws = self._ws
        if ws is None or ws.closed:
            raise ConnectionError("Not connected to Home Assistant")
        self._next_id += 1
        msg_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        self.commands_sent[command_type] += 1
        try:
            await ws.send_json({"id": msg_id, "type": command_type, **params})
        except Exception:
            self._pending.pop(msg_id, None)
            raise
        return msg_id, future

    async def _subscribe_command(self, command_type: str, params: dict[str, Any],
                                 handler: Callable[[Any], None]) -> int:
        msg_id, future = await self._send(command_type, params)
        self._subscriptions[msg_id] = handler
        try:
            await asyncio.wait_for(future, COMMAND_TIMEOUT)
        except Exception:
            self._subscriptions.pop(msg_id, None)
            raise
        return msg_id

    async def _run(self) -> None:
        attempt = 0
        while not self._stopping:
            self._established = False
            try:
                await self._connect_once()
                delay = RECONNECT_DELAYS[0]
            except asyncio.CancelledError:
                raise
            except AuthenticationFailed as err:
                self._set_state("auth_failed", str(err))
                _LOGGER.error("Home Assistant rejected the App's token: %s", err)
                delay = AUTH_FAILED_DELAY
            except Exception as err:  # noqa: BLE001 - reconnect on anything
                if self._established:
                    attempt = 0
                reason = f"{type(err).__name__}: {err}".rstrip(": ")
                self._set_state("disconnected", reason)
                (_LOGGER.warning if attempt == 0 else _LOGGER.debug)("Home Assistant connection failed: %s", reason)
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
            if self._stopping:
                break
            if self.state == "connecting":
                self._set_state("disconnected", self.last_error)
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        self._set_state("connecting")
        async with self._session.ws_connect(self._url, max_msg_size=0, autoping=True) as ws:
            first = await ws.receive_json(timeout=COMMAND_TIMEOUT)
            if first.get("type") != "auth_required":
                raise ConnectionError(f"Unexpected first message {first.get('type')!r}")
            await ws.send_json({"type": "auth", "access_token": self._token})
            reply = await ws.receive_json(timeout=COMMAND_TIMEOUT)
            if reply.get("type") == "auth_invalid":
                raise AuthenticationFailed(reply.get("message") or "invalid token")
            if reply.get("type") != "auth_ok":
                raise ConnectionError(f"Unexpected auth reply {reply.get('type')!r}")
            self._ws = ws
            self.ha_version = reply.get("ha_version")
            self._started_seen = False
            reader = asyncio.create_task(self._reader(ws), name="ha-websocket-reader")
            helpers: list[asyncio.Task[None]] = []
            try:
                await self._initial_load()
                if self.connected_since is not None:
                    self.reconnects += 1
                self.connected_since = utcnow_iso()
                self._established = True
                self._set_state("connected")
                _LOGGER.info("Connected to Home Assistant %s (%d entities)", self.ha_version, len(self.states))
                helpers.append(asyncio.create_task(self._ping_loop(ws), name="ha-ping"))
                if (self.config.get("state") or "RUNNING") != "RUNNING":
                    helpers.append(asyncio.create_task(self._startup_watch(), name="ha-startup"))
                await reader
            finally:
                for task in (*helpers, reader):
                    task.cancel()
                await asyncio.gather(*helpers, reader, return_exceptions=True)
                self._ws = None
                self._subscriptions.clear()
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("Connection closed"))
                self._pending.clear()
                if self.connected:
                    self.last_disconnect = utcnow_iso()
            if not self._stopping:
                raise ConnectionError("Home Assistant closed the connection")

    async def _initial_load(self) -> None:
        # Subscribe first, so no state change between the snapshot and the subscription is lost (in-order answers).
        await self._subscribe_command("subscribe_events", {"event_type": "state_changed"}, self._on_state_changed)
        for event_type in REGISTRY_EVENTS:
            await self._subscribe_command("subscribe_events", {"event_type": event_type}, self._on_registry_event)
        await self._subscribe_command("subscribe_events", {"event_type": "homeassistant_started"}, self._on_started)
        self.config = await self.command("get_config") or {}
        states = await self.command("get_states") or []
        self.states = {s["entity_id"]: s for s in states if isinstance(s, dict) and "entity_id" in s}
        await self._refresh_registries()

    async def _read(self, command_type: str) -> list[dict[str, Any]] | None:
        try:
            items = await self.command(command_type)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not read %s: %s", command_type, err)
            return None
        return [i for i in items or [] if isinstance(i, dict)]

    async def _refresh_registries(self) -> None:
        areas = await self._read("config/area_registry/list")
        devices = await self._read("config/device_registry/list")
        entities = await self._read("config/entity_registry/list")
        if areas is not None:
            self.areas = areas
        if devices is not None:
            self.devices = devices
        if entities is not None:
            self.entity_registry = entities

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            started = time.monotonic()
            try:
                _, future = await self._send("ping", {})
                await asyncio.wait_for(future, PING_TIMEOUT)
            except (TimeoutError, ConnectionError) as err:
                _LOGGER.warning("Home Assistant did not answer a ping (%s); reconnecting", type(err).__name__)
                await ws.close()
                return
            self.latency_ms = round((time.monotonic() - started) * 1000, 1)
            await asyncio.sleep(PING_INTERVAL)

    async def _reader(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                try:
                    data = message.json()
                except ValueError:
                    _LOGGER.warning("Ignored a malformed message from Home Assistant")
                    continue
                for item in data if isinstance(data, list) else [data]:
                    if isinstance(item, dict):
                        self._dispatch(item)
            elif message.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break

    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        msg_id = msg.get("id")
        if kind == "event":
            handler = self._subscriptions.get(msg_id)  # type: ignore[arg-type]
            if handler is not None:
                try:
                    handler(msg.get("event") or {})
                except Exception:  # noqa: BLE001 - a handler must not end the connection
                    _LOGGER.exception("Event handler failed")
            return
        future = self._pending.pop(msg_id, None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if kind == "pong":
            future.set_result(None)
        elif kind == "result":
            if msg.get("success"):
                future.set_result(msg.get("result"))
            else:
                error = msg.get("error") or {}
                future.set_exception(HomeAssistantError(str(error.get("code")), str(error.get("message"))))

    def _on_state_changed(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        self.events_total += 1
        new_state = data.get("new_state")
        if new_state is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = new_state
        self._notify("state", (entity_id, new_state))

    def _on_started(self, _event: dict[str, Any]) -> None:
        self._started_seen = True
        self._background(self._resync_states())

    def _background(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _resync_states(self) -> None:
        """Home Assistant finished starting: the snapshot taken while it started was incomplete, and entities that
        were already there send no state change. Re-read everything and deliver what differs as state changes."""
        try:
            self.config = await self.command("get_config") or self.config
            states = await self.command("get_states") or []
        except (HomeAssistantError, ConnectionError, TimeoutError) as err:
            _LOGGER.warning("Could not re-read the states after Home Assistant started: %s", err)
            return
        changed: list[str] = []
        for wire in states:
            if not isinstance(wire, dict) or "entity_id" not in wire:
                continue
            entity_id = wire["entity_id"]
            current = self.states.get(entity_id)
            if current == wire or (current is not None and (current.get("last_updated") or "") >
                                   (wire.get("last_updated") or "")):
                continue  # a state change delivered meanwhile is newer than the snapshot
            self.states[entity_id] = wire
            changed.append(entity_id)
        for entity_id in changed:
            self._notify("state", (entity_id, self.states[entity_id]))
        await self._refresh_registries()
        self._notify("registries")
        _LOGGER.info("Re-read %d states after Home Assistant started (%d new or changed)", len(self.states),
                     len(changed))

    async def _startup_watch(self) -> None:
        """Connected while Home Assistant was starting: poll until it runs (its start-up event may be missed)."""
        for _ in range(120):
            await asyncio.sleep(STARTUP_POLL_SECONDS)
            if self._started_seen:
                return
            try:
                config = await self.command("get_config") or {}
            except (HomeAssistantError, ConnectionError, TimeoutError):
                continue
            if config.get("state") == "RUNNING":
                if not self._started_seen:
                    self._started_seen = True
                    await self._resync_states()
                return

    def _on_registry_event(self, _event: dict[str, Any]) -> None:
        """Re-read the registries shortly after they change (bursts are coalesced)."""
        if self._registry_timer is not None:
            self._registry_timer.cancel()
        self._registry_timer = asyncio.get_running_loop().call_later(REGISTRY_DEBOUNCE, self._registry_changed)

    def _registry_changed(self) -> None:
        self._registry_timer = None

        async def refresh() -> None:
            try:
                await self._refresh_registries()
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("Registry refresh skipped: %s", err)
                return
            self._notify("registries")

        self._background(refresh())
