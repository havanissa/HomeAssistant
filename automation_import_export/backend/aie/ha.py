"""Connection to Home Assistant, restricted by an allowlist and by a per-operation write scope.

Through the Supervisor proxy the App acts as a Home Assistant administrator, so both channels enforce a fixed
allowlist *before* anything is sent (:func:`check_params`, :class:`AutomationConfigApi`).

**Websocket** (``ws://supervisor/core/websocket``), :data:`ALLOWED_COMMANDS`:

* Reads: states, the entity / label / category / area / floor / device registries, the user list (administrator
  check), ``automation/config`` (what an automation entity runs) and ``blueprint/list``.
* Writes, **only** for the automations of the restore operation that is running (:class:`WriteScope`):
  ``config/entity_registry/update`` of those automation entities (name, icon, area, labels, categories, aliases,
  hidden/disabled by the user, entity id), ``call_service`` ``automation.turn_on`` / ``automation.turn_off`` of
  those entities (their enabled state), ``homeassistant/expose_entity`` of those entities (assistant exposure).
* Creating a missing label, an automation category or an area that the user chose to recreate
  (``config/*_registry/create``). Nothing is ever *updated* or *deleted* in these registries.
* ``blueprint/save`` of a blueprint file that is missing and that the archive contains (never overwriting).

**HTTP** (``http://supervisor/core/api``), :class:`AutomationConfigApi`: only
``/config/automation/config/<id>`` - the endpoint Home Assistant's own automation editor uses to read, save and
delete one automation in ``automations.yaml``. GET for any automation; POST only for ids in the write scope; DELETE
only for ids in the deletion scope of a confirmed operation. Ids in Automation Builder's namespace
(``automation_builder_``) are refused for POST and DELETE whatever the scope.

The reconnect and ping handling follow Automation Builder's client (copied: the Apps share no code at run time).
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Iterable, Iterator
import contextlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .const import BUILDER_PREFIX

_LOGGER = logging.getLogger(__name__)

#: Ids this App can address through the editor endpoint. Anything else is reported as "not addressable".
AUTOMATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}")
ENTITY_ID = re.compile(r"automation\.[a-z0-9_]{1,250}")

READ_COMMANDS: frozenset[str] = frozenset({
    "ping",
    "get_config",
    "get_states",
    "config/auth/list",
    "config/entity_registry/list",
    "config/entity_registry/get_entries",
    "config/label_registry/list",
    "config/category_registry/list",
    "config/area_registry/list",
    "config/floor_registry/list",
    "config/device_registry/list",
    "automation/config",
    "blueprint/list",
    "validate_config",
})
WRITE_COMMANDS: frozenset[str] = frozenset({
    "config/entity_registry/update",
    "config/label_registry/create",
    "config/category_registry/create",
    "config/area_registry/create",
    "blueprint/save",
    "call_service",
    "homeassistant/expose_entity",
})
ALLOWED_COMMANDS: frozenset[str] = READ_COMMANDS | WRITE_COMMANDS

#: The registry fields this App may set on an automation entity (everything a restore needs, nothing else).
REGISTRY_UPDATE_FIELDS = frozenset({
    "entity_id", "new_entity_id", "name", "icon", "area_id", "labels", "categories", "aliases", "hidden_by",
    "disabled_by",
})
ASSISTANTS = frozenset({"conversation", "cloud.alexa", "cloud.google_assistant"})
LABEL_CREATE_FIELDS = frozenset({"name", "icon", "color", "description"})
CATEGORY_CREATE_FIELDS = frozenset({"scope", "name", "icon"})
AREA_CREATE_FIELDS = frozenset({"name", "icon", "aliases"})
BLUEPRINT_PATH = re.compile(r"[A-Za-z0-9_\-]+(/[A-Za-z0-9_.\-]+)*\.yaml")

RECONNECT_DELAYS = (1, 2, 5, 10, 30)
AUTH_FAILED_DELAY = 60
PING_INTERVAL = 15.0
PING_TIMEOUT = 10.0
COMMAND_TIMEOUT = 60.0


class NotAllowed(RuntimeError):
    """A command or parameter outside the allowlist / write scope was requested."""


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


def is_builder_id(value: Any) -> bool:
    """Automation Builder's namespace (case-insensitive, to be safe)."""
    return isinstance(value, str) and value.casefold().startswith(BUILDER_PREFIX)


def blueprint_path_ok(path: Any) -> bool:
    return (isinstance(path, str) and len(path) <= 255 and bool(BLUEPRINT_PATH.fullmatch(path))
            and ".." not in path.split("/") and not path.startswith(BUILDER_PREFIX.rstrip("_")))


@dataclass
class WriteScope:
    """What the running operation may change. Empty (the default) means: nothing."""

    configs: set[str] = field(default_factory=set)       # automation ids that may be POSTed
    deletions: set[str] = field(default_factory=set)     # automation ids that may be DELETEd
    entities: set[str] = field(default_factory=set)      # automation entity ids whose registry/state may change
    metadata: bool = False                               # labels / categories / areas / blueprints may be created

    def clear(self) -> None:
        self.configs.clear()
        self.deletions.clear()
        self.entities.clear()
        self.metadata = False


def _entity_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    raise NotAllowed("entity ids must be given explicitly")


def check_params(command_type: str, params: dict[str, Any], scope: WriteScope) -> None:
    """The limits on parameters, enforced before a command is sent."""
    if command_type not in ALLOWED_COMMANDS:
        raise NotAllowed(f"Command {command_type!r} is not allowed")
    if command_type == "automation/config" and not ENTITY_ID.fullmatch(str(params.get("entity_id"))):
        raise NotAllowed("automation/config is only sent for automation entities")
    if command_type == "config/entity_registry/get_entries":
        if not all(ENTITY_ID.fullmatch(e) for e in _entity_ids(params.get("entity_ids"))):
            raise NotAllowed("Only automation entities are read one by one")
    if command_type == "config/category_registry/list" and not isinstance(params.get("scope"), str):
        raise NotAllowed("A category scope is required")
    if command_type == "blueprint/list" and params.get("domain") != "automation":
        raise NotAllowed("Only automation blueprints are listed")
    if command_type not in WRITE_COMMANDS:
        return
    # ---------------------------------------------------------------- writes
    if command_type == "config/entity_registry/update":
        if set(params) - REGISTRY_UPDATE_FIELDS:
            raise NotAllowed(f"Registry fields {sorted(set(params) - REGISTRY_UPDATE_FIELDS)} are not changed")
        entity_id = params.get("entity_id")
        if not isinstance(entity_id, str) or entity_id not in scope.entities or not ENTITY_ID.fullmatch(entity_id):
            raise NotAllowed(f"{entity_id!r} is not an automation of the running restore")
        new = params.get("new_entity_id")
        if new is not None and (not isinstance(new, str) or not ENTITY_ID.fullmatch(new)):
            raise NotAllowed("The new entity id must be an automation entity id")
        for key in ("disabled_by", "hidden_by"):
            if key in params and params[key] not in (None, "user"):
                raise NotAllowed(f"{key} can only be set by the user")
        return
    if command_type == "call_service":
        if params.get("domain") != "automation" or params.get("service") not in ("turn_on", "turn_off", "reload"):
            raise NotAllowed("Only automation.turn_on / turn_off / reload are called")
        if set(params) - {"domain", "service", "target", "service_data", "return_response"}:
            raise NotAllowed("Unexpected service call fields")
        if params.get("return_response"):
            raise NotAllowed("No service responses are requested")
        if params["service"] == "reload":  # one automation of the running restore (a re-enabled registry entry)
            data = params.get("service_data") or {}
            if "target" in params or set(data) != {"id"} or data["id"] not in scope.configs:
                raise NotAllowed("Only an automation of the running restore is reloaded, by its id")
            return
        target = params.get("target")
        data = params.get("service_data") or {}
        if not isinstance(target, dict) or set(target) != {"entity_id"} or set(data) - {"stop_actions"}:
            raise NotAllowed("A service call must name its automation entities and nothing else")
        ids = _entity_ids(target["entity_id"])
        if not ids or any(e not in scope.entities or not ENTITY_ID.fullmatch(e) for e in ids):
            raise NotAllowed("Only automations of the running restore are turned on or off")
        return
    if command_type == "homeassistant/expose_entity":
        ids = _entity_ids(params.get("entity_ids"))
        assistants = params.get("assistants")
        if (not ids or any(e not in scope.entities or not ENTITY_ID.fullmatch(e) for e in ids)
                or not isinstance(assistants, list) or not set(assistants) <= ASSISTANTS
                or not isinstance(params.get("should_expose"), bool)):
            raise NotAllowed("Only the assistant exposure of automations of the running restore is set")
        return
    if not scope.metadata:
        raise NotAllowed(f"{command_type} is only sent by a confirmed restore")
    if command_type == "config/label_registry/create" and (
            set(params) - LABEL_CREATE_FIELDS or not isinstance(params.get("name"), str)):
        raise NotAllowed("Unexpected label fields")
    if command_type == "config/category_registry/create" and (
            set(params) - CATEGORY_CREATE_FIELDS or params.get("scope") != "automation"
            or not isinstance(params.get("name"), str)):
        raise NotAllowed("Only automation categories are created")
    if command_type == "config/area_registry/create" and (
            set(params) - AREA_CREATE_FIELDS or not isinstance(params.get("name"), str)):
        raise NotAllowed("Unexpected area fields")
    if command_type == "blueprint/save" and (
            params.get("domain") != "automation" or not blueprint_path_ok(params.get("path"))
            or params.get("allow_override") or "source_url" in params or not isinstance(params.get("yaml"), str)):
        raise NotAllowed(f"{params.get('path')!r} is not a missing automation blueprint")


class HomeAssistantClient:
    """Keeps one authenticated websocket open, with automatic reconnects. Every operation reads fresh data."""

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str | None) -> None:
        self._session = session
        self._url = url
        self._token = token
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._established = False
        self.scope = WriteScope()

        self.state = "connecting" if token else "not_configured"
        self.last_error: str | None = None if token else "No API token available (SUPERVISOR_TOKEN is not set)"
        self.ha_version: str | None = None
        self.connected_since: str | None = None
        self.reconnects = 0
        self.latency_ms: float | None = None
        self.commands_sent: Counter[str] = Counter()
        self.history: deque[dict[str, Any]] = deque(maxlen=20)
        self._connected_event = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    def status(self) -> dict[str, Any]:
        return {"state": self.state, "connected": self.connected, "ha_version": self.ha_version,
                "connected_since": self.connected_since, "last_error": self.last_error,
                "reconnects": self.reconnects, "latency_ms": self.latency_ms, "history": list(self.history)}

    async def start(self) -> None:
        if self._token and self._task is None:
            self._task = asyncio.create_task(self._run(), name="ha-websocket")

    async def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def wait_connected(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    @contextlib.contextmanager
    def scoped(self, *, configs: Iterable[str] = (), deletions: Iterable[str] = (), entities: Iterable[str] = (),
               metadata: bool = False) -> Iterator[WriteScope]:
        """Allow the writes of one confirmed operation; everything is refused again afterwards."""
        self.scope.configs |= set(configs)
        self.scope.deletions |= set(deletions)
        self.scope.entities |= set(entities)
        self.scope.metadata = metadata
        try:
            yield self.scope
        finally:
            self.scope.clear()

    async def command(self, command_type: str, **params: Any) -> Any:
        """Send one allowlisted command and return its result."""
        check_params(command_type, params, self.scope)
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
        return await asyncio.wait_for(future, COMMAND_TIMEOUT)

    # ------------------------------------------------------------ internals
    def _set_state(self, state: str, error: str | None = None) -> None:
        changed = state != self.state or error != self.last_error
        if changed and state in ("connected", "disconnected", "auth_failed"):
            self.history.appendleft({"at": utcnow_iso(), "state": state, "error": error})
        self.state = state
        if error is not None or state == "connected":
            self.last_error = error
        if state == "connected":
            self._connected_event.set()
        else:
            self._connected_event.clear()

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
            reader = asyncio.create_task(self._reader(ws), name="ha-websocket-reader")
            pinger: asyncio.Task[None] | None = None
            try:
                if self.connected_since is not None:
                    self.reconnects += 1
                self.connected_since = utcnow_iso()
                self._established = True
                self._set_state("connected")
                _LOGGER.info("Connected to Home Assistant %s", self.ha_version)
                pinger = asyncio.create_task(self._ping_loop(ws), name="ha-ping")
                await reader
            finally:
                for task in (pinger, reader):
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(t for t in (pinger, reader) if t is not None), return_exceptions=True)
                self._ws = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("Connection closed"))
                self._pending.clear()
            if not self._stopping:
                raise ConnectionError("Home Assistant closed the connection")

    async def _ping_loop(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            started = time.monotonic()
            try:
                await asyncio.wait_for(self.command("ping"), PING_TIMEOUT)
            except (TimeoutError, ConnectionError) as err:
                _LOGGER.warning("Home Assistant did not answer a ping (%s); reconnecting", type(err).__name__)
                await ws.close()
                return
            self.latency_ms = round((time.monotonic() - started) * 1000, 1)

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
        future = self._pending.pop(msg.get("id"), None)  # type: ignore[arg-type]
        if future is None or future.done():
            return
        kind = msg.get("type")
        if kind == "pong":
            future.set_result(None)
        elif kind == "result":
            if msg.get("success"):
                future.set_result(msg.get("result"))
            else:
                error = msg.get("error") or {}
                future.set_exception(HomeAssistantError(str(error.get("code")), str(error.get("message"))))


class ConfigApiError(Exception):
    """Home Assistant refused or failed a read or change of an automation's configuration."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AutomationConfigApi:
    """Home Assistant's automation configuration endpoint (the automation editor's), with this App's limits.

    ``GET`` returns the automation with this id exactly as it is in ``automations.yaml`` (unknown fields
    included; nothing is normalised), ``POST`` validates, writes it and reloads that one automation, ``DELETE``
    removes it from ``automations.yaml`` and removes its entity registry entry (Home Assistant keeps that entry
    as a "deleted entity" and gives its settings back when the same id is created again). The endpoint is not
    part of Home Assistant's documented REST API; everything that depends on it is in this class.
    """

    TIMEOUT = aiohttp.ClientTimeout(total=30)

    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str | None, scope: WriteScope) -> None:
        self._session = session
        self._base = base_url.rstrip("/")
        self._token = token
        self._scope = scope
        self.requests: Counter[str] = Counter()

    @staticmethod
    def addressable(automation_id: Any) -> bool:
        return isinstance(automation_id, str) and bool(AUTOMATION_ID.fullmatch(automation_id))

    def _url(self, automation_id: str) -> str:
        if not self.addressable(automation_id):
            raise NotAllowed(f"{automation_id!r} cannot be addressed through the automation editor endpoint")
        return f"{self._base}/config/automation/config/{quote(automation_id, safe='')}"

    async def _request(self, method: str, automation_id: str, body: Any = None) -> tuple[int, Any]:
        url = self._url(automation_id)
        if not self._token:
            raise ConfigApiError("No API token available (SUPERVISOR_TOKEN is not set)")
        self.requests[method] += 1
        try:
            async with self._session.request(method, url, json=body, timeout=self.TIMEOUT, headers={
                    "Authorization": f"Bearer {self._token}"}) as response:
                try:
                    data = await response.json(content_type=None)
                except ValueError:
                    data = None
                return response.status, data
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ConfigApiError(f"Home Assistant could not be reached ({type(err).__name__})") from err

    @staticmethod
    def _message(status: int, data: Any) -> str:
        text = data.get("message") if isinstance(data, dict) else None
        return str(text) if text else f"HTTP {status}"

    async def get(self, automation_id: str) -> dict[str, Any] | None:
        status, data = await self._request("GET", automation_id)
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise ConfigApiError(f"Home Assistant could not read the automation: {self._message(status, data)}",
                                 status)
        return data

    async def put(self, automation_id: str, config: dict[str, Any]) -> None:
        if is_builder_id(automation_id):
            raise NotAllowed("Automation Builder's automations are never written by this App")
        if automation_id not in self._scope.configs:
            raise NotAllowed(f"{automation_id!r} is not part of the confirmed restore")
        status, data = await self._request("POST", automation_id, config)
        if status != 200:
            raise ConfigApiError(f"Home Assistant did not accept the automation: {self._message(status, data)}",
                                 status)

    async def delete(self, automation_id: str) -> bool:
        """True when removed, False when it was not there (any more)."""
        if is_builder_id(automation_id):
            raise NotAllowed("Automation Builder's automations are never deleted by this App")
        if automation_id not in self._scope.deletions:
            raise NotAllowed(f"{automation_id!r} is not part of the confirmed deletion")
        status, data = await self._request("DELETE", automation_id)
        if status in (400, 404) and "not found" in self._message(status, data).lower():
            return False
        if status != 200:
            raise ConfigApiError(f"Home Assistant could not remove the automation: {self._message(status, data)}",
                                 status)
        return True
