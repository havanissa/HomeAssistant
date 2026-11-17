"""Stand-in for the Home Assistant Supervisor and Core's Ingress proxy.

For end-to-end tests of the App container without Home Assistant OS. It runs
in a container at 172.30.32.2 (network alias ``supervisor``), like the real
Supervisor, and provides:

* ``ws://supervisor/core/websocket``: the Core API proxy. It checks the App's
  SUPERVISOR_TOKEN and forwards to a real Home Assistant with a long-lived
  token, like the real Supervisor. Every command the App sends is recorded.
* ``/addons/self/info`` and ``/info``: the basic Supervisor endpoints.
* ``/api/hassio_ingress/<token>/...``: Ingress, with the same header handling
  as Core (homeassistant/components/hassio/ingress.py) and the Supervisor
  (supervisor/api/ingress.py): X-Ingress-Path, X-Forwarded-*, X-Remote-User-*,
  Host and Origin kept, requests arriving at the App from 172.30.32.2.
* ``/hassio/ingress/smart_lighting``: a page that embeds the App in a
  full-size iframe, like the Home Assistant sidebar panel.
* ``/_test/...``: test hooks (recorded commands, drop the Core connection).

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

# Core's INIT_HEADERS_FILTER plus the Supervisor's identity headers.
REQUEST_FILTER = {
    "content-length", "content-encoding", "transfer-encoding", "accept-encoding",
    "sec-websocket-extensions", "sec-websocket-protocol", "sec-websocket-version", "sec-websocket-key",
    "x-supervisor-token", "x-hassio-key", "x-remote-user-id", "x-remote-user-name",
    "x-remote-user-display-name", "x-remote-user-groups",
}
RESPONSE_FILTER = {"transfer-encoding", "content-length", "content-type", "content-encoding"}

recorded_commands: list[dict] = []
ingress_requests: list[dict] = []
core_sockets: set[web.WebSocketResponse] = set()


def _authorized(request: web.Request) -> bool:
    return request.headers.get("Authorization") == f"Bearer {SUPERVISOR_TOKEN}"


async def self_info(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"result": "error", "message": "Unauthorized"}, status=401)
    return web.json_response({"result": "ok", "data": {
        "name": "Smart Occupancy & Lighting", "slug": "local_smart_lighting", "version": "0.1.1",
        "version_latest": "0.1.1", "update_available": False, "state": "started", "arch": "amd64",
        "repository": "local", "ingress": True, "ingress_panel": True, "boot": "auto", "watchdog": False,
        "ingress_url": f"/api/hassio_ingress/{INGRESS_TOKEN}/", "options": {"log_level": "info"},
    }})


async def system_info(request: web.Request) -> web.Response:
    if not _authorized(request):
        return web.json_response({"result": "error", "message": "Unauthorized"}, status=401)
    session: aiohttp.ClientSession = request.app["api_session"]
    async with session.get(f"{HA_URL}/api/config", headers={"Authorization": f"Bearer {HA_TOKEN}"}) as resp:
        version = (await resp.json()).get("version")
    return web.json_response({"result": "ok", "data": {
        "supervisor": "2026.11.2 (simulated)", "homeassistant": version, "hassos": None,
        "operating_system": "End-to-end test (simulated Supervisor)", "machine": "qemux86-64",
        "arch": "amd64", "channel": "stable", "supported": True, "healthy": True, "docker": "test",
        "hostname": "e2e", "timezone": "Atlantic/Faroe",
    }})


async def core_websocket(request: web.Request) -> web.WebSocketResponse:
    app_ws = web.WebSocketResponse(max_msg_size=0)
    await app_ws.prepare(request)
    session: aiohttp.ClientSession = request.app["api_session"]
    async with session.ws_connect(f"{HA_URL}/api/websocket", max_msg_size=0) as core:
        required = await core.receive_json()
        await app_ws.send_json(required)
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
                recorded_commands.append({k: v for k, v in data.items() if k in ("id", "type", "event_type", "domain", "integration")})
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


def _ingress_headers(request: web.Request, token: str) -> CIMultiDict[str]:
    headers = CIMultiDict((k, v) for k, v in request.headers.items() if k.lower() not in REQUEST_FILTER)
    headers["X-Hass-Source"] = "core.ingress"
    headers["X-Ingress-Path"] = f"/api/hassio_ingress/{token}"
    peer = request.transport.get_extra_info("peername")[0] if request.transport else "127.0.0.1"
    forwarded = request.headers.get("X-Forwarded-For")
    headers["X-Forwarded-For"] = f"{forwarded}, {peer}" if forwarded else peer
    headers["X-Forwarded-Host"] = request.headers.get("X-Forwarded-Host") or request.host
    headers["X-Forwarded-Proto"] = request.headers.get("X-Forwarded-Proto") or request.scheme
    headers["X-Remote-User-Id"] = "e2e-admin-id"
    headers["X-Remote-User-Name"] = "admin"
    headers["X-Remote-User-Display-Name"] = "E2E Admin"
    return headers


async def ingress(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    if token != INGRESS_TOKEN:
        raise web.HTTPUnauthorized()
    path = request.match_info.get("path", "")
    url = f"{APP_URL}/{quote(path)}"
    if request.query_string:
        url = f"{url}?{request.query_string}"
    headers = _ingress_headers(request, token)
    ingress_requests.append({"method": request.method, "path": path, "ws": request.headers.get("Upgrade", "").lower() == "websocket"})
    session: aiohttp.ClientSession = request.app["session"]

    if "upgrade" in request.headers.get("Connection", "").lower() and request.headers.get("Upgrade", "").lower() == "websocket":
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        browser = web.WebSocketResponse(protocols=protocols, autoclose=False, autoping=False)
        await browser.prepare(request)
        async with session.ws_connect(url, headers=headers, protocols=protocols, autoclose=False, autoping=False) as app_ws:
            async def forward(src, dst) -> None:
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type == WSMsgType.PING:
                        await dst.ping()
                    elif msg.type == WSMsgType.PONG:
                        await dst.pong()
                    elif dst.closed or msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                        await dst.close(code=src.close_code or 1000)
                        return
                await dst.close()
            await asyncio.wait(
                [asyncio.create_task(forward(browser, app_ws)), asyncio.create_task(forward(app_ws, browser))],
                return_when=asyncio.FIRST_COMPLETED,
            )
        return browser

    async with session.request(request.method, url, headers=headers, data=await request.read(), allow_redirects=False) as resp:
        body = await resp.read()
        out = web.Response(status=resp.status, body=body,
                           headers=CIMultiDict((k, v) for k, v in resp.headers.items() if k.lower() not in RESPONSE_FILTER))
        if ctype := resp.headers.get("Content-Type"):
            out.headers["Content-Type"] = ctype
        return out


async def panel(request: web.Request) -> web.Response:
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Smart Lighting - Home Assistant</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>html,body{{margin:0;height:100%;overflow:hidden}}iframe{{border:0;width:100%;height:100%;display:block}}</style></head>
<body><iframe id="ingress" src="/api/hassio_ingress/{INGRESS_TOKEN}/" title="Smart Lighting" allow="fullscreen"></iframe></body></html>"""
    return web.Response(text=page, content_type="text/html")


async def test_commands(request: web.Request) -> web.Response:
    return web.json_response({"commands": recorded_commands, "ingress_requests": len(ingress_requests),
                              "core_connections": len(core_sockets)})


async def test_drop_core(request: web.Request) -> web.Response:
    count = len(core_sockets)
    for ws in list(core_sockets):
        await ws.close()
    return web.json_response({"dropped": count})


async def on_startup(app: web.Application) -> None:
    # Ingress passes bodies through untouched (like Core); API calls decode.
    app["session"] = aiohttp.ClientSession(auto_decompress=False)
    app["api_session"] = aiohttp.ClientSession()


async def on_cleanup(app: web.Application) -> None:
    await app["session"].close()
    await app["api_session"].close()


def main() -> None:
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/addons/self/info", self_info)
    app.router.add_get("/info", system_info)
    app.router.add_get("/core/websocket", core_websocket)
    app.router.add_route("*", "/api/hassio_ingress/{token}/{path:.*}", ingress)
    app.router.add_route("*", "/api/hassio_ingress/{token}", ingress)
    app.router.add_get("/hassio/ingress/smart_lighting", panel)
    app.router.add_get("/_test/commands", test_commands)
    app.router.add_post("/_test/drop_core", test_drop_core)
    web.run_app(app, host="0.0.0.0", port=80, print=lambda *_: print("fake supervisor ready", flush=True))


if __name__ == "__main__":
    main()
