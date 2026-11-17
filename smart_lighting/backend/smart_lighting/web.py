"""HTTP server for the web interface and its JSON API.

Ingress: Home Assistant serves the App under ``/api/hassio_ingress/<token>/``
and passes that prefix in ``X-Ingress-Path``. The interface uses only
relative URLs; ``index.html`` gets a ``<base href>`` for the prefix, so it
works with and without a trailing slash and on any Home Assistant URL.

Phase A2 adds the Rooms API. Reads are GET. Every request that can change the
room configuration is a POST that needs:

* a Home Assistant administrator: the Supervisor's Ingress passes the user id
  in ``X-Remote-User-Id`` (the browser cannot set it: Core and the Supervisor
  replace it); the App checks it against Home Assistant's user list;
* the ``X-Smart-Lighting: 1`` header, a JSON body and (when the browser sends
  one) a same-origin ``Origin`` header, so another site cannot post to it.

The browser sends room data only; the backend chooses the Home Assistant
command from its fixed allowlist (``ha.CONFIG_WRITE_COMMANDS``).
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
from .live import LiveHub, origin_allowed
from .rooms import RoomsError
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", AppCore)
HUB_KEY = web.AppKey("hub", LiveHub)

_INGRESS_PATH = re.compile(r"^(/[A-Za-z0-9._~\-]+)+$")
_STATIC_FILE = r"{name:[A-Za-z0-9_\-]+\.(?:svg|png|ico|webmanifest)}"
CSRF_HEADER = "X-Smart-Lighting"
MAX_BODY = 2 * 1024 * 1024

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

    app = web.Application(middlewares=[access_middleware, headers_middleware], client_max_size=MAX_BODY)
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

    # ------------------------------------------------------------ rooms
    def rooms_service(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.rooms is None or core.ha is None or not core.ha.config:
            raise RoomsError("not_connected", "Home Assistant is not connected", 503)
        return core.rooms

    def rooms_error(err: RoomsError) -> web.Response:
        return _json({"error": str(err), "code": err.code}, status=err.status)

    async def write_guard(request: web.Request) -> dict[str, Any]:
        """CSRF and administrator checks for every changing request; returns the JSON body."""
        if request.headers.get(CSRF_HEADER) != "1" or request.content_type != "application/json":
            raise RoomsError("forbidden", "This request must come from the Smart Lighting interface", 403)
        if request.headers.get("Origin") and not origin_allowed(request):
            raise RoomsError("forbidden", "Cross-site requests are not allowed", 403)
        service = rooms_service(request)
        user_id = request.headers.get("X-Remote-User-Id")
        if not (settings.dev_mode and not user_id):
            try:
                admin = await service.is_admin(user_id)
            except Exception as err:  # noqa: BLE001 - fail closed
                raise RoomsError("forbidden", f"Could not verify the Home Assistant user ({type(err).__name__})",
                                 403) from err
            if not admin:
                _LOGGER.warning("Refused a configuration request from a user who is not an administrator")
                raise RoomsError("forbidden", "Only Home Assistant administrators can change the configuration", 403)
        try:
            body = await request.json()
        except ValueError as err:
            raise RoomsError("invalid_json", "The request body is not valid JSON") from err
        if not isinstance(body, dict):
            raise RoomsError("invalid_json", "The request body must be an object")
        return body

    def rooms_handler(func: Any) -> Any:
        async def handler(request: web.Request) -> web.Response:
            try:
                return await func(request)
            except RoomsError as err:
                return rooms_error(err)
        return handler

    @rooms_handler
    async def rooms_overview(request: web.Request) -> web.Response:
        service = rooms_service(request)
        await service.ensure()
        return _json(service.overview())

    @rooms_handler
    async def room_workspace(request: web.Request) -> web.Response:
        service = rooms_service(request)
        if request.query.get("refresh"):
            await service.refresh()
        else:
            await service.ensure()
        return _json(service.workspace(request.match_info["area_id"]))

    @rooms_handler
    async def room_candidates(request: web.Request) -> web.Response:
        return _json({"entities": rooms_service(request).candidates()})

    @rooms_handler
    async def room_review(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = rooms_service(request)
        return _json(await service.review(request.match_info["area_id"], body.get("room"), body.get("revision")))

    @rooms_handler
    async def room_save(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = rooms_service(request)
        result = await service.save(request.match_info["area_id"], body.get("room"), body.get("revision"),
                                    body.get("confirmed"))
        return _json({"result": result, "workspace": service.workspace(request.match_info["area_id"])})

    @rooms_handler
    async def rooms_export(request: web.Request) -> web.Response:
        service = rooms_service(request)
        await service.refresh()
        data = service.export()
        stamp = data["created_at"].replace(":", "").replace("-", "")[:15]
        return _json(data, headers={"Content-Disposition":
                                    f'attachment; filename="smart-occupancy-rooms-{stamp}.json"'})

    @rooms_handler
    async def rooms_backups(request: web.Request) -> web.Response:
        return _json({"backups": rooms_service(request).backups()})

    @rooms_handler
    async def rooms_backup(request: web.Request) -> web.Response:
        backup_id = request.match_info["backup_id"]
        data = rooms_service(request).backup(backup_id)
        return _json(data, headers={"Content-Disposition": f'attachment; filename="smart-occupancy-{backup_id}.json"'})

    @rooms_handler
    async def rooms_restore_review(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await rooms_service(request).restore_review(body.get("backup")))

    @rooms_handler
    async def rooms_restore(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await rooms_service(request).restore(body.get("backup"), body.get("revision"),
                                                           body.get("confirmed")))

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
    app.router.add_get("/api/rooms", rooms_overview)
    app.router.add_get("/api/rooms/candidates", room_candidates)
    app.router.add_get("/api/rooms/export", rooms_export)
    app.router.add_get("/api/rooms/backups", rooms_backups)
    app.router.add_get("/api/rooms/backups/{backup_id}", rooms_backup)
    app.router.add_post("/api/rooms/restore/review", rooms_restore_review)
    app.router.add_post("/api/rooms/restore", rooms_restore)
    app.router.add_get("/api/rooms/{area_id}", room_workspace)
    app.router.add_post("/api/rooms/{area_id}/review", room_review)
    app.router.add_post("/api/rooms/{area_id}/save", room_save)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)
    assets = settings.static_dir / "assets"
    if assets.is_dir():
        app.router.add_static("/assets/", assets, append_version=False)
    app.router.add_get("/" + _STATIC_FILE, static_file)
    return app
