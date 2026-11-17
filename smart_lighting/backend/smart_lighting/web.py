"""HTTP server for the web interface and its read-only JSON API.

Ingress: Home Assistant serves the App under ``/api/hassio_ingress/<token>/``
and passes that prefix in ``X-Ingress-Path``. The interface uses only
relative URLs; ``index.html`` gets a ``<base href>`` for the prefix, so it
works with and without a trailing slash and on any Home Assistant URL.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from aiohttp import web

from .const import APP_VERSION
from .core import AppCore
from .live import LiveHub
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", AppCore)
HUB_KEY = web.AppKey("hub", LiveHub)

_INGRESS_PATH = re.compile(r"^(/[A-Za-z0-9._~\-]+)+$")
_STATIC_FILE = r"{name:[A-Za-z0-9_\-]+\.(?:svg|png|ico|webmanifest)}"

CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self' ws: wss:",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "form-action 'none'",
        "object-src 'none'",
    )
)


def base_href(request: web.Request) -> str:
    """The base URL of the interface, from the Ingress prefix if valid."""
    prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
    if prefix and _INGRESS_PATH.match(prefix):
        return f"{prefix}/"
    return "/"


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.Response(
        text=json.dumps(data, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json",
        headers=headers,
    )


def build_app(settings: Settings, core: AppCore | None = None) -> web.Application:
    """Create the aiohttp application. ``core`` may be injected by tests."""

    @web.middleware
    async def access_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        if not settings.is_allowed(request.remote):
            _LOGGER.warning("Refused a request from %s (only Home Assistant Ingress is allowed)", request.remote)
            return web.Response(status=403, text="Forbidden: open this App from the Home Assistant sidebar.")
        return await handler(request)

    @web.middleware
    async def headers_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            if not request.path.startswith("/api/"):
                raise
            response = _json({"error": exc.reason}, status=exc.status)
        if not response.prepared:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("Content-Security-Policy", CSP)
            if request.path.startswith("/assets/"):
                response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            else:
                response.headers.setdefault("Cache-Control", "no-store")
        return response

    app = web.Application(middlewares=[access_middleware, headers_middleware])
    owns_core = core is None
    app[CORE_KEY] = core or AppCore(settings)
    app[HUB_KEY] = LiveHub(app[CORE_KEY])

    async def on_startup(app: web.Application) -> None:
        if owns_core:
            await app[CORE_KEY].start()
        await app[HUB_KEY].start()

    async def on_cleanup(app: web.Application) -> None:
        await app[HUB_KEY].stop()
        if owns_core:
            await app[CORE_KEY].stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    async def index(request: web.Request) -> web.Response:
        try:
            page = (settings.static_dir / "index.html").read_text(encoding="utf-8")
        except FileNotFoundError:
            return web.Response(status=503, text="The web interface is not installed in this build.")
        base = html.escape(base_href(request), quote=True)
        page = page.replace("<head>", f'<head>\n    <base href="{base}">', 1)
        return web.Response(text=page, content_type="text/html")

    async def static_file(request: web.Request) -> web.FileResponse:
        path = settings.static_dir / request.match_info["name"]
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def health(request: web.Request) -> web.Response:
        return _json(request.app[CORE_KEY].health())

    async def overview(request: web.Request) -> web.Response:
        return _json(request.app[CORE_KEY].overview())

    async def diagnostics(request: web.Request) -> web.Response:
        data = request.app[CORE_KEY].diagnostics()
        headers = None
        if request.query.get("download"):
            stamp = data["generated_at"].replace(":", "").replace("-", "")[:15]
            headers = {"Content-Disposition": f'attachment; filename="smart-lighting-diagnostics-{stamp}.json"'}
        return _json(data, headers=headers)

    async def lookup(request: web.Request) -> web.Response:
        ha = request.app[CORE_KEY].ha
        if ha is None or not ha.config:
            return _json({"error": "Home Assistant is not connected"}, status=503)
        return _json(ha.lookup())

    async def version(request: web.Request) -> web.Response:
        return _json({"version": APP_VERSION})

    async def live(request: web.Request) -> web.StreamResponse:
        return await request.app[HUB_KEY].handle(request)

    async def api_not_found(request: web.Request) -> web.Response:
        return _json({"error": "Not found"}, status=404)

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/overview", overview)
    app.router.add_get("/api/diagnostics", diagnostics)
    app.router.add_get("/api/version", version)
    app.router.add_get("/api/lookup", lookup)
    app.router.add_get("/api/live", live)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)
    assets = settings.static_dir / "assets"
    if assets.is_dir():
        app.router.add_static("/assets/", assets, append_version=False)
    app.router.add_get("/" + _STATIC_FILE, static_file)
    return app
