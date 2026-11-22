"""Stand-in for the Supervisor and Core's Ingress proxy, for end-to-end tests of the real App image.

It runs in a container on the test
network and provides:

* ``ws://<this>/core/websocket`` - the Core API proxy: checks the App's SUPERVISOR_TOKEN and forwards to a real
  (disposable) Home Assistant with a long-lived token, in both directions (results, subscriptions and their
  events). **Every message the App sends is recorded** (its type and the fields that would name a service, an
  event type, an entity or a forecast type), so a test can prove that the App never sent a service call or any
  command outside its allowlist.
* ``/api/hassio_ingress/<token>/...`` - Ingress with Core's and the Supervisor's header handling
  (X-Ingress-Path, X-Forwarded-*, X-Remote-User-*), requests reaching the App from this container's address.
* ``/hassio/ingress/spc`` - a page embedding the App in a full-size iframe, like the sidebar panel.
* ``/_test/commands`` and ``/_test/drop_core`` - test hooks.

Environment: SUPERVISOR_TOKEN, HA_URL, HA_TOKEN, APP_URL, INGRESS_TOKEN.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import quote

import aiohttp
from aiohttp import WSMsgType, web
from multidict import CIMultiDict

SUPERVISOR_TOKEN = os.environ["SUPERVISOR_TOKEN"]
HA_URL = os.environ["HA_URL"].rstrip("/")
HA_TOKEN = os.environ["HA_TOKEN"]
APP_URL = os.environ["APP_URL"].rstrip("/")
INGRESS_TOKEN = os.environ["INGRESS_TOKEN"]

REQUEST_FILTER = {
    "content-length", "content-encoding", "transfer-encoding", "accept-encoding", "x-supervisor-token",
    "x-hassio-key", "x-remote-user-id", "x-remote-user-name", "x-remote-user-display-name", "x-remote-user-groups",
}
RESPONSE_FILTER = {"transfer-encoding", "content-length", "content-type", "content-encoding"}

recorded: list[dict] = []
ingress_user: dict = {"id": "unknown", "name": "admin", "display": "E2E Admin"}
core_sockets: set[web.WebSocketResponse] = set()


async def core_websocket(request: web.Request) -> web.WebSocketResponse:
    app_ws = web.WebSocketResponse(max_msg_size=0)
    await app_ws.prepare(request)
    session: aiohttp.ClientSession = request.app["api_session"]
    async with session.ws_connect(f"{HA_URL}/api/websocket", max_msg_size=0) as core:
        await app_ws.send_json(await core.receive_json())
        auth = await app_ws.receive_json()
        if auth.get("type") != "auth" or auth.get("access_token") != SUPERVISOR_TOKEN:
            await app_ws.send_json({"type": "auth_invalid", "message": "Invalid access"})
            await app_ws.close()
            return app_ws
        await core.send_json({"type": "auth", "access_token": HA_TOKEN})
        await app_ws.send_json(await core.receive_json())
        core_sockets.add(app_ws)

        async def app_to_core() -> None:
            async for msg in app_ws:
                if msg.type != WSMsgType.TEXT:
                    break
                data = msg.json()
                for item in data if isinstance(data, list) else [data]:
                    recorded.append({k: item.get(k) for k in ("type", "event_type", "domain", "service", "integration",
                                                                  "forecast_type", "entity_id")
                                     if k in item})
                await core.send_str(msg.data)
            await core.close()

        async def core_to_app() -> None:
            async for msg in core:
                if msg.type != WSMsgType.TEXT:
                    break
                await app_ws.send_str(msg.data)
            await app_ws.close()

        try:
            await asyncio.gather(app_to_core(), core_to_app())
        finally:
            core_sockets.discard(app_ws)
    return app_ws


async def ingress(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    if token != INGRESS_TOKEN:
        raise web.HTTPUnauthorized()
    path = request.match_info.get("path", "")
    url = f"{APP_URL}/{quote(path)}" + (f"?{request.query_string}" if request.query_string else "")
    headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in REQUEST_FILTER)
    headers["X-Hass-Source"] = "core.ingress"
    headers["X-Ingress-Path"] = f"/api/hassio_ingress/{token}"
    peer = request.transport.get_extra_info("peername")[0] if request.transport else "127.0.0.1"
    headers["X-Forwarded-For"] = peer
    headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host") or request.host
    headers["X-Forwarded-Proto"] = request.scheme
    user = request.headers.get("X-Test-User") or ingress_user["id"]  # a test may act as another HA user
    headers["X-Remote-User-Id"] = user
    headers["X-Remote-User-Name"] = ingress_user["name"]
    async with request.app["session"].request(request.method, url, headers=headers, data=await request.read(),
                                              allow_redirects=False) as resp:
        out = web.Response(status=resp.status, body=await resp.read(), headers=CIMultiDict(
            (k, v) for k, v in resp.headers.items() if k.lower() not in RESPONSE_FILTER))
        if ctype := resp.headers.get("Content-Type"):
            out.headers["Content-Type"] = ctype
        return out


async def panel(_request: web.Request) -> web.Response:
    return web.Response(content_type="text/html", text=f"""<!doctype html><html><head><meta charset="utf-8">
<title>Predictive Climate - Home Assistant</title><meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{{margin:0;height:100%;overflow:hidden}}iframe{{border:0;width:100%;height:100%;display:block}}</style>
</head><body><iframe id="ingress" src="/api/hassio_ingress/{INGRESS_TOKEN}/" title="Predictive Climate"></iframe></body></html>""")


async def test_commands(_request: web.Request) -> web.Response:
    return web.json_response({"commands": recorded, "core_connections": len(core_sockets),
                              "ingress_user": ingress_user["id"]})


async def test_drop_core(_request: web.Request) -> web.Response:
    count = len(core_sockets)
    for ws in list(core_sockets):
        await ws.close()
    return web.json_response({"dropped": count})


async def on_startup(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession(auto_decompress=False)
    app["api_session"] = aiohttp.ClientSession()
    async with app["api_session"].ws_connect(f"{HA_URL}/api/websocket") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": HA_TOKEN})
        await ws.receive_json()
        await ws.send_json({"id": 1, "type": "auth/current_user"})
        user = (await ws.receive_json()).get("result") or {}
    ingress_user.update(id=user.get("id", "unknown"), display=user.get("name") or "E2E Admin")


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()
    await app["api_session"].close()


def main() -> None:
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/core/websocket", core_websocket)
    app.router.add_route("*", "/api/hassio_ingress/{token}/{path:.*}", ingress)
    app.router.add_route("*", "/api/hassio_ingress/{token}", ingress)
    app.router.add_get("/hassio/ingress/spc", panel)
    app.router.add_get("/_test/commands", test_commands)
    app.router.add_post("/_test/drop_core", test_drop_core)
    web.run_app(app, host="0.0.0.0", port=80, print=lambda *_: print("fake supervisor ready", flush=True))


if __name__ == "__main__":
    main()
