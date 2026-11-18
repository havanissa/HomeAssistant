"""Connection to the Home Assistant websocket API, restricted by an allowlist.

Through the Supervisor proxy (``ws://supervisor/core/websocket``) the App acts
as a Home Assistant administrator, so this client enforces a fixed allowlist
before anything is sent:

* :data:`READ_ONLY_COMMANDS` - reads and subscriptions, sent with :meth:`command`.
* :data:`CONFIG_WRITE_COMMANDS` (Phase A2) - the existing integration's
  validated room configuration commands, sent only with :meth:`config_write`.
  They change the integration's room assignments and nothing else. Phase A3.1
  adds its validated presence command (sensors and occupancy timing).
* :data:`LIGHTING_BRIDGE_COMMANDS` (Phase A4) - the integration's lighting bridge,
  sent only with :meth:`lighting_command`. ``execute`` is the only path from the
  App to a device: the integration accepts it only from the session that controls
  lighting (epoch, lease, sequence) and only for an allowlisted service of a
  configured lighting target. The lighting editors' commands are forwarded with
  :meth:`command` (reads, :data:`LIGHTING_READ_COMMANDS`) and :meth:`config_write`
  (changes, :data:`LIGHTING_WRITE_COMMANDS`) while the integration controls lighting.
* :data:`ENGINE_COMMANDS` (Phase A3.2) - the integration's occupancy bridge,
  sent only with :meth:`engine_command` (claim, publish, heartbeat, release:
  they change which engine publishes occupancy and the App's published results,
  nothing else), and :data:`SUBSCRIPTION_COMMANDS`, opened only with
  :meth:`subscribe` (the bridge session).

There is no way to call a service directly, write a state, fire an event or
change a Home Assistant registry through this client. Any other command raises
:class:`ReadOnlyViolation` before it is sent.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime
import logging
import time
from typing import Any

import aiohttp

from .const import INTEGRATION_DOMAIN
from .integration import IntegrationReport, build_report

_LOGGER = logging.getLogger(__name__)

#: Websocket commands that only read (or subscribe). All may be sent with ``command()``.
READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ping",
        "get_config",
        "get_states",
        "subscribe_events",
        "config_entries/get",
        "config_entries/subscribe",
        "manifest/get",
        "config/entity_registry/list",
        "config/area_registry/list",
        "config/device_registry/list",
        "config/floor_registry/list",
        "config/auth/list",
        # The existing integration's room configuration API (0.6.9): reads and dry runs.
        "smart_occupancy/rooms/get",
        "smart_occupancy/rooms/review",
        "smart_occupancy/rooms/restore/review",
        # Phase A3.1: the integration's presence API (0.6.10): live engine view and dry runs.
        "smart_occupancy/presence/get",
        "smart_occupancy/presence/review",
        # The Recorder's history (bounded windows, on request only) for the presence timeline.
        "history/history_during_period",
        # Phase A3.2: the occupancy ownership record (integration 0.7.0).
        "smart_occupancy/engine/status",
        # Phase A4: what the lighting runtime reads (labels, automation and script configurations for conflict
        # detection, MQTT device triggers of wall switches, trigger validation) and the lighting ownership record.
        "config/label_registry/list",
        "automation/config",
        "script/config",
        "device_automation/trigger/list",
        "validate_config",
        "unsubscribe_events",
        "smart_occupancy/lighting/bridge/status",
    }
)
#: Phase A4: the integration's lighting editor commands that only read or simulate (forwarded while it controls
#: lighting; answered by the App's own handlers after the handover).
LIGHTING_READ_COMMANDS: frozenset[str] = frozenset(
    f"smart_occupancy/lighting/{c}" for c in (
        "editor", "preview", "room/activation", "brightness/preview", "priority/preview", "zone/radar_test",
        "switch/candidates", "supervisor/room")
)
#: Phase A4: the lighting editor commands that change the configuration (or test a profile with real commands),
#: administrators only, forwarded with config_write() while the integration controls lighting.
LIGHTING_WRITE_COMMANDS: frozenset[str] = frozenset(
    f"smart_occupancy/lighting/{c}" for c in (
        "adaptive/set", "ambient/levels", "ambient/set", "brightness/remove", "brightness/set", "brightness/template",
        "category/remove", "category/set", "daylight/set", "fixture/remove", "fixture/set", "mood/set",
        "mood_source/set", "override/clear", "preliminary/set", "priority/lock", "priority/remove", "priority/set",
        "profile/copy", "profile/reset", "profile/set", "profile/test", "relay/set", "room/acknowledge",
        "room/activate", "room/deactivate", "room/daylight", "room/group_choice", "room/set", "settings/set",
        "space/remove", "space/set", "supervisor/set", "switch/add", "switch/learn", "switch/mapping", "switch/mode",
        "switch/remove", "switch/set", "switch/settings", "vacant/set", "zone/remove", "zone/set")
)
#: Phase A4: the integration's lighting bridge (ownership, the owner's fenced commands, the mirror).
LIGHTING_BRIDGE_COMMANDS: frozenset[str] = frozenset(
    f"smart_occupancy/lighting/bridge/{c}" for c in (
        "claim", "execute", "heartbeat", "publish", "inputs", "options", "release", "return")
)
#: The only commands that change anything: the existing integration's validated, administrator-only room and
#: presence configuration commands (Phases A2, A3.1). They write its config entry options and nothing else.
CONFIG_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "smart_occupancy/rooms/set",
        "smart_occupancy/rooms/restore",
        # Phase A3.1: a room's sensors and timing overrides, or the global occupancy timing defaults.
        "smart_occupancy/presence/set",
    }
) | LIGHTING_WRITE_COMMANDS
#: Phase A3.2: the integration's occupancy bridge. They change which engine publishes room occupancy and the
#: App's published occupancy results; none of them can call a service or control a device.
ENGINE_COMMANDS: frozenset[str] = frozenset(
    {
        "smart_occupancy/engine/claim",
        "smart_occupancy/engine/publish",
        "smart_occupancy/engine/heartbeat",
        "smart_occupancy/engine/release",
    }
)
#: Subscriptions other than subscribe_events (opened only with subscribe()). Phase A4: the lighting bridge, the
#: lighting editors' change notifications, and the wall switch inputs the lighting runtime listens to (device
#: triggers run by Home Assistant, MQTT messages through Home Assistant's MQTT integration).
SUBSCRIPTION_COMMANDS: frozenset[str] = frozenset({
    "smart_occupancy/engine/subscribe", "smart_occupancy/lighting/bridge/subscribe", "smart_occupancy/lighting/subscribe",
    "subscribe_trigger", "mqtt/subscribe",
})
ALLOWED_COMMANDS: frozenset[str] = (READ_ONLY_COMMANDS | LIGHTING_READ_COMMANDS | CONFIG_WRITE_COMMANDS
                                    | ENGINE_COMMANDS | LIGHTING_BRIDGE_COMMANDS | SUBSCRIPTION_COMMANDS)
#: Event types the App may subscribe to.
ALLOWED_EVENT_TYPES: frozenset[str] = frozenset(
    {"state_changed", "area_registry_updated", "device_registry_updated", "entity_registry_updated",
     "floor_registry_updated", "label_registry_updated",
     # Phase A4: conflict detection follows reloaded automations and scripts; start-up of Home Assistant.
     "automation_reloaded", "script_reloaded", "homeassistant_started"}
)
REGISTRY_EVENTS = ("area_registry_updated", "device_registry_updated", "entity_registry_updated",
                   "floor_registry_updated", "label_registry_updated")
OTHER_EVENTS = ("automation_reloaded", "script_reloaded", "homeassistant_started")
REGISTRY_DEBOUNCE = 2.0

RECONNECT_DELAYS = (1, 2, 5, 10, 30)
AUTH_FAILED_DELAY = 60
PING_INTERVAL = 30.0
PING_TIMEOUT = 10.0
COMMAND_TIMEOUT = 30.0
DETECTION_INTERVAL = 300.0

Listener = Callable[[str, Any], None]


class ReadOnlyViolation(RuntimeError):
    """A command outside the allowlist (or a write through the read path) was requested."""


class HomeAssistantError(Exception):
    """Home Assistant answered a command with an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class AuthenticationFailed(Exception):
    """Home Assistant rejected the token."""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class HomeAssistantClient:
    """Keeps one authenticated websocket open, with automatic reconnects."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str | None,
        *,
        integration_domain: str = INTEGRATION_DOMAIN,
    ) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._domain = integration_domain
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._listeners: list[Listener] = []
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._established = False

        self.state = "connecting" if token else "not_configured"
        self.last_error: str | None = None if token else "No API token available (SUPERVISOR_TOKEN is not set)"
        self.ha_version: str | None = None
        self.connected_since: str | None = None
        self.last_disconnect: str | None = None
        self.connect_attempts = 0
        self.reconnects = 0
        self.latency_ms: float | None = None
        self.last_pong: str | None = None

        self.config: dict[str, Any] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.area_count: int | None = None
        self.areas: list[dict[str, Any]] = []
        self.devices: list[dict[str, Any]] = []
        self.floors: list[dict[str, Any]] = []
        self.entity_registry: list[dict[str, Any]] = []
        self.labels: list[dict[str, Any]] = []
        self.config_entries_all: list[dict[str, Any]] = []
        self.integration = IntegrationReport.unknown()

        self.commands_sent: Counter[str] = Counter()
        self.config_writes: list[dict[str, Any]] = []  # last writes (type, time, result), for diagnostics
        self.events_total = 0
        self._registry_timer: asyncio.TimerHandle | None = None
        self._event_times: deque[float] = deque()

    # ------------------------------------------------------------------ API
    def add_listener(self, listener: Listener) -> Callable[[], None]:
        """Register ``listener(kind, payload)``.

        Kinds: status, entity (integration entity changed), state (any entity: ``(entity_id, state)``),
        integration (report changed), config_entry (the integration's config entry changed), registries.
        """
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def events_per_minute(self) -> int:
        self._trim_events(time.monotonic())
        return len(self._event_times)

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "ha_version": self.ha_version,
            "connected_since": self.connected_since,
            "last_disconnect": self.last_disconnect,
            "last_error": self.last_error,
            "connect_attempts": self.connect_attempts,
            "reconnects": self.reconnects,
            "latency_ms": self.latency_ms,
            "last_pong": self.last_pong,
            "events_total": self.events_total,
            "events_per_minute": self.events_per_minute(),
        }

    def info(self) -> dict[str, Any]:
        """Basic Home Assistant information (from get_config)."""
        cfg = self.config
        units = cfg.get("unit_system") or {}
        return {
            "location_name": cfg.get("location_name"),
            "version": cfg.get("version") or self.ha_version,
            "state": cfg.get("state"),
            "time_zone": cfg.get("time_zone"),
            "country": cfg.get("country"),
            "language": cfg.get("language"),
            "currency": cfg.get("currency"),
            "unit_system": {"length": units.get("length"), "temperature": units.get("temperature")},
            "safe_mode": cfg.get("safe_mode"),
            "recovery_mode": cfg.get("recovery_mode"),
            "component_count": len(cfg.get("components") or []),
            "entity_count": len(self.states) if self.config else None,
            "area_count": self.area_count,
        }

    def integration_entities(self) -> list[dict[str, Any]]:
        """Current states of the existing integration's entities."""
        return [entity_summary(self.states.get(eid), eid) for eid in self.integration.entity_ids]

    def lookup(self) -> dict[str, list[dict[str, Any]]]:
        """Areas, devices and entities for the searchable selectors (read-only)."""
        area_names = {a.get("area_id"): a.get("name") for a in self.areas}
        devices = {}
        for d in self.devices:
            if d.get("disabled_by"):
                continue
            devices[d.get("id")] = {
                "id": d.get("id"),
                "name": d.get("name_by_user") or d.get("name") or d.get("id"),
                "manufacturer": d.get("manufacturer"),
                "model": d.get("model"),
                "area_id": d.get("area_id"),
                "area": area_names.get(d.get("area_id")),
                "entity_count": 0,
            }
        registry = {e.get("entity_id"): e for e in self.entity_registry}
        entities = []
        for entity_id in sorted(set(self.states) | {e for e, r in registry.items() if e and not r.get("disabled_by")}):
            reg = registry.get(entity_id) or {}
            if reg.get("disabled_by"):
                continue
            state = self.states.get(entity_id) or {}
            attributes = state.get("attributes") or {}
            device = devices.get(reg.get("device_id"))
            area_id = reg.get("area_id") or (device or {}).get("area_id")
            if device is not None:
                device["entity_count"] += 1
            entities.append(
                {
                    "entity_id": entity_id,
                    "name": attributes.get("friendly_name") or reg.get("name") or reg.get("original_name") or entity_id,
                    "domain": entity_id.split(".", 1)[0],
                    "state": state.get("state"),
                    "unit": attributes.get("unit_of_measurement"),
                    "last_changed": state.get("last_changed"),
                    "platform": reg.get("platform"),
                    "device_id": reg.get("device_id") if device else None,
                    "device": (device or {}).get("name"),
                    "area_id": area_id,
                    "area": area_names.get(area_id),
                }
            )
        area_list = []
        for a in self.areas:
            aid = a.get("area_id")
            area_list.append(
                {
                    "id": aid,
                    "name": a.get("name") or aid,
                    "device_count": sum(1 for d in devices.values() if d["area_id"] == aid),
                    "entity_count": sum(1 for e in entities if e["area_id"] == aid),
                }
            )
        return {
            "areas": sorted(area_list, key=lambda a: str(a["name"]).casefold()),
            "devices": sorted(devices.values(), key=lambda d: str(d["name"]).casefold()),
            "entities": entities,
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
        """Send one read-only command and return its result."""
        if command_type in CONFIG_WRITE_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} changes configuration; use config_write()")
        if command_type in ENGINE_COMMANDS or command_type in SUBSCRIPTION_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} is an occupancy bridge command; use engine_command()")
        if command_type in LIGHTING_BRIDGE_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} is a lighting bridge command; use lighting_command()")
        _, future = await self._send(command_type, params)
        return await asyncio.wait_for(future, COMMAND_TIMEOUT)

    async def config_write(self, command_type: str, **params: Any) -> Any:
        """Send one of the integration's validated configuration commands (CONFIG_WRITE_COMMANDS only)."""
        if command_type not in CONFIG_WRITE_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} is not a permitted configuration command")
        record: dict[str, Any] = {"type": command_type, "time": utcnow_iso(), "area_id": params.get("area_id")}
        self.config_writes = [*self.config_writes[-19:], record]
        try:
            _, future = await self._send(command_type, params)
            result = await asyncio.wait_for(future, COMMAND_TIMEOUT)
        except HomeAssistantError as err:
            record["result"] = err.code
            raise
        except (ConnectionError, TimeoutError) as err:
            # The write may or may not have been applied: the caller must re-read before trying again.
            record["result"] = f"unknown ({type(err).__name__})"
            raise
        record["result"] = "ok"
        return result

    async def engine_command(self, command_type: str, **params: Any) -> Any:
        """Send one of the integration's occupancy bridge commands (ENGINE_COMMANDS only)."""
        if command_type not in ENGINE_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} is not an occupancy bridge command")
        _, future = await self._send(command_type, params)
        return await asyncio.wait_for(future, COMMAND_TIMEOUT)

    async def lighting_command(self, command_type: str, **params: Any) -> Any:
        """Send one of the integration's lighting bridge commands (LIGHTING_BRIDGE_COMMANDS only)."""
        if command_type not in LIGHTING_BRIDGE_COMMANDS:
            raise ReadOnlyViolation(f"{command_type!r} is not a lighting bridge command")
        _, future = await self._send(command_type, params)
        return await asyncio.wait_for(future, COMMAND_TIMEOUT)

    async def unsubscribe(self, sub_id: int) -> None:
        """End one subscription this client opened (no error when the connection is already gone)."""
        if self._subscriptions.pop(sub_id, None) is None:
            return
        with contextlib.suppress(ConnectionError, TimeoutError, HomeAssistantError):
            _, future = await self._send("unsubscribe_events", {"subscription": sub_id})
            await asyncio.wait_for(future, COMMAND_TIMEOUT)

    async def subscribe(self, command_type: str, handler: Callable[[Any], None], **params: Any) -> int:
        """Open an allowlisted subscription (SUBSCRIPTION_COMMANDS). It ends when the connection does."""
        if command_type not in SUBSCRIPTION_COMMANDS:
            raise ReadOnlyViolation(f"Subscribing with {command_type!r} is not allowed")
        return await self._subscribe_command(command_type, params, handler)

    def config_writes_sent(self) -> int:
        return sum(n for cmd, n in self.commands_sent.items() if cmd in CONFIG_WRITE_COMMANDS)

    # ------------------------------------------------------------ internals
    def _notify(self, kind: str, payload: Any = None) -> None:
        for listener in list(self._listeners):
            try:
                listener(kind, payload)
            except Exception:  # noqa: BLE001 - one listener must not break others
                _LOGGER.exception("Listener failed for %s", kind)

    def _set_connection_state(self, state: str, error: str | None = None) -> None:
        changed = state != self.state or error != self.last_error
        self.state = state
        if error is not None or state == "connected":
            self.last_error = error
        if changed:
            self._notify("status", self.status())

    async def _send(self, command_type: str, params: dict[str, Any]) -> tuple[int, asyncio.Future[Any]]:
        if command_type not in ALLOWED_COMMANDS:
            raise ReadOnlyViolation(f"Command {command_type!r} is not allowed")
        if command_type == "subscribe_events" and params.get("event_type") not in ALLOWED_EVENT_TYPES:
            raise ReadOnlyViolation(f"Subscribing to {params.get('event_type')!r} is not allowed")
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

    async def _subscribe(self, event_type: str, handler: Callable[[dict[str, Any]], None]) -> None:
        await self._subscribe_command("subscribe_events", {"event_type": event_type}, handler)

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
            self.connect_attempts += 1
            self._established = False
            try:
                await self._connect_once()
                delay = RECONNECT_DELAYS[0]
            except asyncio.CancelledError:
                raise
            except AuthenticationFailed as err:
                self._set_connection_state("auth_failed", str(err))
                _LOGGER.error("Home Assistant rejected the App's token: %s", err)
                delay = AUTH_FAILED_DELAY
            except Exception as err:  # noqa: BLE001 - reconnect on anything
                if self._established:
                    attempt = 0  # a working session ended: start the backoff again
                reason = f"{type(err).__name__}: {err}".rstrip(": ")
                self._set_connection_state("disconnected", reason)
                if attempt == 0:
                    _LOGGER.warning("Home Assistant connection failed: %s", reason)
                else:
                    _LOGGER.debug("Home Assistant connection failed again: %s", reason)
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
            if self._stopping:
                break
            if self.state == "connecting":
                self._set_connection_state("disconnected", self.last_error)
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        self._set_connection_state("connecting")
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
            reader = asyncio.create_task(self._reader(ws), name="ha-websocket-reader")
            helpers: list[asyncio.Task[None]] = []
            try:
                await self._initial_load()
                was_connected_before = self.connected_since is not None
                self.connected_since = utcnow_iso()
                self._established = True
                if was_connected_before:
                    self.reconnects += 1
                self._set_connection_state("connected")
                _LOGGER.info(
                    "Connected to Home Assistant %s (%d entities, integration: %s)",
                    self.ha_version,
                    len(self.states),
                    self.integration.status,
                )
                helpers = [
                    asyncio.create_task(self._ping_loop(ws), name="ha-ping"),
                    asyncio.create_task(self._detection_loop(), name="ha-detection"),
                ]
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
        # Subscribe first, so no state change between the snapshot and the
        # subscription is lost (Home Assistant answers in order).
        await self._subscribe("state_changed", self._on_state_changed)
        for event_type in REGISTRY_EVENTS:
            await self._subscribe(event_type, self._on_registry_event)
        for event_type in OTHER_EVENTS:
            await self._subscribe(event_type, self._on_other_event)
        try:
            # Changes of the integration's config entry (also from its own options flow), state included.
            await self._subscribe_command("config_entries/subscribe", {"type_filter": ["service"]},
                                          self._on_config_entries)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not subscribe to config entry changes: %s", err)
        self.config = await self.command("get_config") or {}
        states = await self.command("get_states") or []
        self.states = {s["entity_id"]: s for s in states if isinstance(s, dict) and "entity_id" in s}
        await self._refresh_registries()

    async def _read_registry(self, command_type: str) -> list[dict[str, Any]] | None:
        try:
            items = await self.command(command_type)
        except HomeAssistantError as err:
            _LOGGER.warning("Could not read %s: %s", command_type, err)
            return None
        return [i for i in items or [] if isinstance(i, dict)]

    async def _refresh_registries(self) -> None:
        """Re-read areas, devices and entities, then the integration report."""
        areas = await self._read_registry("config/area_registry/list")
        devices = await self._read_registry("config/device_registry/list")
        registry = await self._read_registry("config/entity_registry/list")
        floors = await self._read_registry("config/floor_registry/list")
        labels = await self._read_registry("config/label_registry/list")
        self.labels = labels or []
        self.areas = areas or []
        self.floors = floors or []
        self.area_count = len(areas) if areas is not None else None
        self.devices = devices or []
        self.entity_registry = registry or []
        try:
            everything = await self.command("config_entries/get")
        except HomeAssistantError as err:
            _LOGGER.warning("Could not read config entries: %s", err)
            everything = None
        self.config_entries_all = [e for e in everything or [] if isinstance(e, dict)]
        entries = [e for e in self.config_entries_all if e.get("domain") == self._domain] \
            if everything is not None else None
        try:
            manifest = await self.command("manifest/get", integration=self._domain)
        except HomeAssistantError as err:
            if err.code not in {"not_found", "unknown_error"}:
                _LOGGER.debug("Integration manifest not readable: %s", err)
            manifest = None
        report = build_report(self._domain, entries, manifest, registry)
        if report != self.integration:
            self.integration = report
            self._notify("integration", report)

    async def _detection_loop(self) -> None:
        while True:
            await asyncio.sleep(DETECTION_INTERVAL)
            try:
                await self._refresh_registries()
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("Registry refresh skipped: %s", err)

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
            self.last_pong = utcnow_iso()
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
                handler(msg.get("event") or {})
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

    def _trim_events(self, now: float) -> None:
        while self._event_times and now - self._event_times[0] > 60:
            self._event_times.popleft()

    def _on_state_changed(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not entity_id:
            return
        now = time.monotonic()
        self.events_total += 1
        self._event_times.append(now)
        self._trim_events(now)
        new_state = data.get("new_state")
        if new_state is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = new_state
        if entity_id in self.integration.entity_set:
            self._notify("entity", entity_summary(new_state, entity_id))
        self._notify("state", (entity_id, new_state))

    def _on_other_event(self, event: dict[str, Any]) -> None:
        self._notify("event", event)

    def _on_registry_event(self, event: dict[str, Any]) -> None:
        """Re-read the registries shortly after they change (bursts are coalesced)."""
        self._notify("registry_event", event)
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

        asyncio.get_running_loop().create_task(refresh())

    def _on_config_entries(self, events: Any) -> None:
        for item in events if isinstance(events, list) else []:
            entry = (item or {}).get("entry") or {}
            if entry.get("domain") == self._domain:
                self._notify("config_entry", {"change": item.get("type"), "state": entry.get("state"),
                                              "entry_id": entry.get("entry_id")})


def entity_summary(state: dict[str, Any] | None, entity_id: str) -> dict[str, Any]:
    """A small, display-ready view of one entity state."""
    if state is None:
        return {"entity_id": entity_id, "state": None, "name": entity_id, "unit": None, "last_changed": None}
    attributes = state.get("attributes") or {}
    return {
        "entity_id": entity_id,
        "state": state.get("state"),
        "name": attributes.get("friendly_name") or entity_id,
        "unit": attributes.get("unit_of_measurement"),
        "device_class": attributes.get("device_class"),
        "icon": attributes.get("icon"),
        "last_changed": state.get("last_changed"),
    }
