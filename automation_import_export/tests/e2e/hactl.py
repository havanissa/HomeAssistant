"""A small client for the disposable test Home Assistant (websocket + REST), used by prepare.py and the checks.
It acts as the test's own administrator ("Test Admin"), never as the App."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

URL = "http://127.0.0.1:18123"


class HA:
    def __init__(self, session: aiohttp.ClientSession, token: str, url: str = URL) -> None:
        self.s = session
        self.token = token
        self.url = url
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.n = 0

    async def __aenter__(self) -> HA:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def connect(self) -> None:
        for _ in range(120):
            try:
                self.ws = await self.s.ws_connect(self.url + "/api/websocket", max_msg_size=0)
                await self.ws.receive_json()
                await self.ws.send_json({"type": "auth", "access_token": self.token})
                reply = await self.ws.receive_json()
                if reply.get("type") == "auth_ok":
                    return
                raise RuntimeError(f"auth failed: {reply}")
            except (aiohttp.ClientError, ConnectionError):
                await asyncio.sleep(1)
        raise RuntimeError("Home Assistant is not reachable")

    async def cmd(self, kind: str, **params: Any) -> Any:
        assert self.ws is not None
        self.n += 1
        msg_id = self.n
        await self.ws.send_json({"id": msg_id, "type": kind, **params})
        while True:
            msg = await self.ws.receive_json(timeout=60)
            if msg.get("id") == msg_id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise RuntimeError(f"{kind}: {msg.get('error')}")
                return msg.get("result")

    async def rest(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        async with self.s.request(method, f"{self.url}/api/{path}", json=body,
                                  headers={"Authorization": f"Bearer {self.token}"}) as r:
            text = await r.text()
            try:
                return r.status, json.loads(text) if text else None
            except ValueError:
                return r.status, text

    async def config(self, config_id: str) -> dict[str, Any] | None:
        status, data = await self.rest("GET", f"config/automation/config/{config_id}")
        return data if status == 200 else None

    async def post(self, config_id: str, config: dict[str, Any]) -> None:
        status, data = await self.rest("POST", f"config/automation/config/{config_id}", config)
        if status != 200:
            raise RuntimeError(f"POST {config_id}: {status} {data}")

    async def delete(self, config_id: str) -> bool:
        status, _ = await self.rest("DELETE", f"config/automation/config/{config_id}")
        return status == 200

    async def registry(self) -> dict[str, dict[str, Any]]:
        entries = await self.cmd("config/entity_registry/list")
        ids = [e["entity_id"] for e in entries if e["entity_id"].startswith("automation.")]
        extended = await self.cmd("config/entity_registry/get_entries", entity_ids=ids) if ids else {}
        return {e: v for e, v in extended.items() if v}

    async def by_unique(self) -> dict[str, dict[str, Any]]:
        return {e["unique_id"]: e for e in (await self.registry()).values() if e.get("platform") == "automation"}

    async def states(self) -> dict[str, dict[str, Any]]:
        return {s["entity_id"]: s for s in await self.cmd("get_states")}

    async def wait_entity(self, config_id: str, timeout: float = 30) -> dict[str, Any]:
        for _ in range(int(timeout * 4)):
            entry = (await self.by_unique()).get(config_id)
            if entry is not None:
                return entry
            await asyncio.sleep(0.25)
        raise RuntimeError(f"no entity for {config_id}")

    async def names(self) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        """label name -> id, category name -> id, area name -> id."""
        labels = {x["name"]: x["label_id"] for x in await self.cmd("config/label_registry/list")}
        cats = {x["name"]: x["category_id"] for x in await self.cmd("config/category_registry/list", scope="automation")}
        areas = {x["name"]: x["area_id"] for x in await self.cmd("config/area_registry/list")}
        return labels, cats, areas


def registry_fields(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """What a restore must reproduce exactly."""
    if entry is None:
        return None
    options = entry.get("options") or {}
    return {"entity_id": entry["entity_id"], "unique_id": entry["unique_id"], "name": entry.get("name"),
            "icon": entry.get("icon"), "area_id": entry.get("area_id"), "labels": sorted(entry.get("labels") or []),
            "categories": entry.get("categories") or {}, "aliases": entry.get("aliases") or [],
            "disabled_by": entry.get("disabled_by"), "hidden_by": entry.get("hidden_by"),
            "conversation": (options.get("conversation") or {}).get("should_expose") is True}
