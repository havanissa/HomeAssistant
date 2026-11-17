"""Live updates for the browser over a websocket (``api/live``).

The browser never talks to Home Assistant directly and never sees the App's
token. The backend forwards a small, read-only stream:

* ``overview``: the full overview, on connect and when the integration report changes
* ``status``: connection status changes and a periodic heartbeat with statistics
* ``entities``: state changes of the existing integration's entities (batched)
* ``states``: live states of the entities a page asked to watch (batched; Phase A2)
* ``rooms``: the room configuration changed (in the App, the integration's own
  editor or by a restore), with its new revision
* ``registries``: Home Assistant's area, device, entity or floor registry changed
* ``floorplan``: the stored floor plan changed (any browser session), with its new revision (Phase A2.1)

The browser may send ``ping`` and ``{"type": "watch", "entities": [...]}``
(entity ids only, at most 2000). Nothing it sends can change anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .core import AppCore

_LOGGER = logging.getLogger(__name__)

FLUSH_INTERVAL = 0.5
HEARTBEAT_INTERVAL = 5.0
MAX_WATCH = 2000
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def origin_allowed(request: web.Request) -> bool:
    """Reject cross-site websocket connections.

    Home Assistant's ingress proxy keeps the browser's ``Host`` and
    ``Origin`` headers and adds ``X-Forwarded-Host``; a page on the same
    Home Assistant always has a matching origin.
    """
    origin = request.headers.get("Origin")
    if not origin:
        return True  # not a browser (tests, tools)
    parts = urlsplit(origin)
    origin_host = _normalize_host(parts.netloc, parts.scheme)
    candidates = set()
    for header in ("X-Forwarded-Host", "Host"):
        for value in request.headers.get(header, "").split(","):
            if value.strip():
                candidates.add(_normalize_host(value.strip(), parts.scheme))
    return origin_host in candidates


def _normalize_host(host: str, scheme: str) -> str:
    """Lower-case a host[:port] and drop the scheme's default port."""
    host = host.lower()
    default = {"http": ":80", "https": ":443"}.get(scheme)
    if default and host.endswith(default):
        host = host[: -len(default)]
    return host


class LiveHub:
    def __init__(self, core: AppCore) -> None:
        self.core = core
        self.clients: set[web.WebSocketResponse] = set()
        self.watching: dict[web.WebSocketResponse, frozenset[str]] = {}
        self._pending_entities: dict[str, dict[str, Any]] = {}
        self._pending_states: set[str] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._unsubscribe = None

    async def start(self) -> None:
        assert self.core.ha is not None
        self._unsubscribe = self.core.ha.add_listener(self._on_ha)
        self.core.add_rooms_listener(self._on_rooms)
        if self.core.floorplan is not None:
            self.core.floorplan.add_listener(self._on_floorplan)
        self._tasks = [
            asyncio.create_task(self._flush_loop(), name="live-flush"),
            asyncio.create_task(self._heartbeat_loop(), name="live-heartbeat"),
        ]

    async def stop(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for ws in list(self.clients):
            await ws.close()

    def _on_rooms(self, kind: str) -> None:
        if not self.clients:
            return
        if kind == "rooms":
            rooms = self.core.rooms.status() if self.core.rooms else None
            self._schedule(self._broadcast({"type": "rooms", "status": rooms}))
        else:
            self._schedule(self._broadcast({"type": "registries"}))

    def _on_floorplan(self, revision: str) -> None:
        if self.clients:
            self._schedule(self._broadcast({"type": "floorplan", "revision": revision}))

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            if self.watching and any(payload[0] in w for w in self.watching.values()):
                self._pending_states.add(payload[0])
        elif kind == "entity":
            self._pending_entities[payload["entity_id"]] = payload
        elif kind == "status":
            self._schedule(self._broadcast_status())
        elif kind == "integration":
            self._schedule(self._broadcast({"type": "overview", "overview": self.core.overview()}))

    def _schedule(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    async def _broadcast(self, message: dict[str, Any]) -> None:
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            try:
                await ws.send_json(message)
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)

    async def _broadcast_status(self) -> None:
        if not self.clients:
            return
        overview = self.core.overview()
        await self._broadcast(
            {
                "type": "status",
                "app": overview["app"],
                "checks": overview["checks"],
                "connection": overview["home_assistant"]["connection"],
                "info": overview["home_assistant"]["info"],
            }
        )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if self._pending_entities and self.clients:
                changed = list(self._pending_entities.values())
                self._pending_entities.clear()
                await self._broadcast({"type": "entities", "changed": changed})
            elif not self.clients:
                self._pending_entities.clear()
            if self._pending_states:
                ids, self._pending_states = self._pending_states, set()
                await self._send_states(ids)

    async def _send_states(self, ids: set[str]) -> None:
        rooms = self.core.rooms
        if rooms is None:
            return
        views = {e: rooms.entity_live(e) for e in ids}
        for ws, watched in list(self.watching.items()):
            changed = [views[e] for e in ids & watched]
            if not changed or ws.closed:
                continue
            try:
                await ws.send_json({"type": "states", "changed": changed})
            except (ConnectionError, RuntimeError):
                self.clients.discard(ws)
                self.watching.pop(ws, None)

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await self._broadcast_status()

    def _on_client_message(self, ws: web.WebSocketResponse, data: str) -> None:
        """Only ``watch`` is accepted (read-only); anything else is ignored."""
        if len(data) > 200_000:
            return
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict) or message.get("type") != "watch":
            return
        entities = message.get("entities")
        if not isinstance(entities, list):
            return
        ids = frozenset(e for e in entities[:MAX_WATCH] if isinstance(e, str) and _ENTITY_ID.match(e))
        if ids:
            self.watching[ws] = ids
            self._schedule(self._send_initial(ws, ids))
        else:
            self.watching.pop(ws, None)

    async def _send_initial(self, ws: web.WebSocketResponse, ids: frozenset[str]) -> None:
        rooms = self.core.rooms
        if rooms is None or ws.closed:
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            await ws.send_json({"type": "states", "changed": [rooms.entity_live(e) for e in sorted(ids)]})

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if not origin_allowed(request):
            _LOGGER.warning("Rejected a live connection from origin %s", request.headers.get("Origin"))
            raise web.HTTPForbidden(text="Origin not allowed")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            await ws.send_json({"type": "overview", "overview": self.core.overview()})
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if msg.data == '{"type":"ping"}':
                        await ws.send_json({"type": "pong"})
                        continue
                    self._on_client_message(ws, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(ws)
            self.watching.pop(ws, None)
        return ws
