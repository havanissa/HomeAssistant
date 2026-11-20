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

Phase A2.1 adds the floor plan API (``/api/floorplan/...``). It reads and
writes only the App's own storage (``/data/floorplan``) and Home Assistant
reads; its changing requests (save, image upload, import, restore) need the
same administrator and CSRF checks. Uploaded images are served only through
this Ingress-only server, never from a public URL.

Phase A3.1 adds the presence API (``/api/presence/...``): the integration's
live occupancy view, a bounded Recorder timeline, diagnostics, and reviewed
changes of sensors and occupancy timing (``review`` / ``save``, with the same
administrator and CSRF checks; saved through ``smart_occupancy/presence/set``).

Phase A3.2 adds the engine API (``/api/engine...``): the App's occupancy engine,
the integration's acknowledged owner, the shadow comparison and its readiness
checks (reads), and two administrator requests with the same checks: a reviewed
handover (``handover``, needs ``"confirm": true`` and every readiness check)
and ``release`` (return control to the integration).

Phase A5 adds the curtain API (``/api/curtains...``) and the predictions API
(``/api/predictions...``) with the same shape as the lighting API: status with
the acknowledged owner, the editors' commands (forwarded or local), a reviewed
handover, release / return, export, a reviewed import and backups.
"""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from aiohttp import web

from .const import APP_VERSION
from .climate import ClimateError
from .climate_model import AREA_ID
from .core import AppCore
from .floorplan import FloorPlanError
from .ha import HomeAssistantError
from .curtains_api import CurtainsApiError
from .curtains_runtime import CurtainsError
from .forecast_api import ForecastApiError
from .forecast_runtime import ForecastError
from .lighting_api import LightingApiError
from .lighting_runtime import LightingError
from .migration import MigrationError
from .onboarding import OnboardingError
from .companion_client import CompanionError
from .core_config import CoreConfigError
from .occupancy_runtime import EngineError
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
MAX_PLAN_BODY = 4 * 1024 * 1024
MAX_IMPORT_BODY = 48 * 1024 * 1024
MAX_IMAGE_BODY = 15 * 1024 * 1024

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

    # Each changing request reads its body with its own limit (read_limited); this is only the outer bound.
    app = web.Application(middlewares=[access_middleware, headers_middleware], client_max_size=MAX_IMPORT_BODY)
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
            headers = {"Content-Disposition": f'attachment; filename="smart-comfort-diagnostics-{stamp}.json"'}
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

    async def read_limited(request: web.Request, limit: int) -> bytes:
        if request.content_length is not None and request.content_length > limit:
            raise RoomsError("too_large", f"The request is larger than {limit // (1024 * 1024)} MB", 413)
        chunks, size = [], 0
        async for chunk in request.content.iter_chunked(65536):
            size += len(chunk)
            if size > limit:
                raise RoomsError("too_large", f"The request is larger than {limit // (1024 * 1024)} MB", 413)
            chunks.append(chunk)
        return b"".join(chunks)

    async def admin_guard(request: web.Request) -> None:
        """CSRF and administrator checks for every changing request (before its body is read)."""
        if request.headers.get(CSRF_HEADER) != "1":
            raise RoomsError("forbidden", "This request must come from the Smart Comfort interface", 403)
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

    async def write_guard(request: web.Request, limit: int = MAX_BODY) -> dict[str, Any]:
        """Checks for every changing JSON request; returns the JSON body."""
        if request.content_type != "application/json":
            raise RoomsError("forbidden", "This request must come from the Smart Comfort interface", 403)
        await admin_guard(request)
        try:
            body = json.loads(await read_limited(request, limit))
        except (ValueError, UnicodeDecodeError) as err:
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
            except FloorPlanError as err:
                return _json({"error": str(err), "code": err.code, **err.extra}, status=err.status)
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

    # ----------------------------------------------------------- climate (A7.0)
    def climate_service(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.climate is None:
            raise ClimateError("not_connected", "The App is starting", 503)
        return core.climate

    def climate_handler(func: Any) -> Any:
        async def handler(request: web.Request) -> web.Response:
            try:
                return await func(request)
            except ClimateError as err:
                return _json({"error": str(err), "code": err.code, **err.extra}, status=err.status)
            except RoomsError as err:
                return rooms_error(err)
        return handler

    @climate_handler
    async def climate_get(request: web.Request) -> web.Response:
        return _json(climate_service(request).document())

    @climate_handler
    async def climate_overview(request: web.Request) -> web.Response:
        return _json(climate_service(request).overview())

    @climate_handler
    async def climate_discovery(request: web.Request) -> web.Response:
        rooms_service(request)  # Home Assistant must be connected
        return _json(climate_service(request).discovery())

    @climate_handler
    async def climate_room(request: web.Request) -> web.Response:
        area_id = request.match_info["area_id"]
        if not AREA_ID.match(area_id):
            raise ClimateError("invalid_area", "Invalid area id")
        return _json(climate_service(request).room_view(area_id))

    @climate_handler
    async def climate_history(request: web.Request) -> web.Response:
        area_id = request.match_info["area_id"]
        if not AREA_ID.match(area_id):
            raise ClimateError("invalid_area", "Invalid area id")
        return _json(await climate_service(request).history(area_id, request.query.get("window")))

    @climate_handler
    async def climate_review(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await climate_service(request).review(body.get("config"), body.get("revision")))

    @climate_handler
    async def climate_save(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = climate_service(request)
        result = await service.save(body.get("config"), body.get("revision"))
        return _json({"result": result, "document": service.document()})

    @climate_handler
    async def climate_backups(request: web.Request) -> web.Response:
        await admin_guard(request)
        return _json({"backups": climate_service(request).backups()})

    @climate_handler
    async def climate_backup(request: web.Request) -> web.Response:
        await admin_guard(request)
        backup_id = request.match_info["backup_id"]
        data = climate_service(request).backup(backup_id)
        return _json(data, headers={"Content-Disposition": f'attachment; filename="smart-comfort-{backup_id}.json"'})

    @climate_handler
    async def climate_restore_review(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await climate_service(request).restore_review(body.get("backup")))

    @climate_handler
    async def climate_restore(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = climate_service(request)
        result = await service.restore(body.get("backup"), body.get("revision"))
        return _json({"result": result, "document": service.document()})

    @climate_handler
    async def climate_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        data = climate_service(request).export()
        return _json(data, headers=download_name("smart-comfort-climate", data["exported_at"]))

    @climate_handler
    async def climate_import_review(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        return _json(await climate_service(request).import_review(body.get("file")))

    @climate_handler
    async def climate_import(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        service = climate_service(request)
        result = await service.import_apply(body.get("file"), body.get("revision"))
        return _json({"result": result, "document": service.document()})

    # ------------------------------------------------- thermal learning (A7.1)
    def learning_area(request: web.Request, value: Any) -> str:
        if not isinstance(value, str) or not AREA_ID.match(value):
            raise ClimateError("invalid_area", "Invalid area id")
        if value not in climate_service(request).config["rooms"]:
            raise ClimateError("not_configured", "This room is not configured for climate", 404)
        return value

    @climate_handler
    async def learning_overview(request: web.Request) -> web.Response:
        return _json(climate_service(request).learning.overview())

    @climate_handler
    async def learning_room(request: web.Request) -> web.Response:
        area_id = learning_area(request, request.match_info["area_id"])
        return _json(climate_service(request).learning.room(area_id))

    @climate_handler
    async def learning_rebuild(request: web.Request) -> web.Response:
        body = await write_guard(request)
        area = body.get("area_id")
        area_id = None if area is None else learning_area(request, area)
        learning = climate_service(request).learning
        queued = learning.request(area_id, user=request.headers.get("X-Remote-User-Id"))
        return _json({"queued": queued, "learning": learning.overview()})

    @climate_handler
    async def learning_reset(request: web.Request) -> web.Response:
        body = await write_guard(request)
        area_id = body.get("area_id")
        if not isinstance(area_id, str) or not AREA_ID.match(area_id):
            raise ClimateError("invalid_area", "Invalid area id")
        if body.get("confirm") is not True:
            raise ClimateError("confirm_required", "Resetting a learned model must be confirmed. The observations "
                               "and the configuration are kept.", 409)
        learning = climate_service(request).learning
        try:
            archived = await learning.reset(area_id)
        except KeyError as err:
            raise ClimateError("not_configured", "This room has no climate configuration or model", 404) from err
        return _json({"reset": True, "archived": archived, "learning": learning.summary(area_id)
                      if area_id in climate_service(request).config["rooms"] else None})

    @climate_handler
    async def learning_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        area_id = learning_area(request, request.match_info["area_id"])
        data = climate_service(request).learning.export(area_id)
        return _json(data, headers=download_name("smart-comfort-thermal-" + re.sub(r"[^A-Za-z0-9_-]", "_", area_id), data["exported_at"]))

    # -------------------------------------------------------- floor plan
    def floorplan_service(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.floorplan is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.floorplan

    def download_name(prefix: str, stamp: str | None) -> dict[str, str]:
        clean = re.sub(r"[^0-9TZ]", "", stamp or "")[:16]
        return {"Content-Disposition": f'attachment; filename="{prefix}-{clean}.json"'}

    @rooms_handler
    async def floorplan_get(request: web.Request) -> web.Response:
        return _json(floorplan_service(request).document())

    @rooms_handler
    async def floorplan_save(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        return _json(await floorplan_service(request).save(body.get("plan"), body.get("revision")))

    @rooms_handler
    async def floorplan_context(request: web.Request) -> web.Response:
        service = floorplan_service(request)
        rooms_service(request)  # Home Assistant must be connected
        await service.rooms.ensure()
        return _json(service.context())

    @rooms_handler
    async def floorplan_entity(request: web.Request) -> web.Response:
        service = floorplan_service(request)
        rooms_service(request)
        return _json(service.entity_details(request.match_info["entity_id"]))

    @rooms_handler
    async def floorplan_upload(request: web.Request) -> web.Response:
        if not request.content_type.startswith("image/"):
            raise FloorPlanError("unsupported_type", "Only PNG, JPEG and WebP images can be used", 415)
        await admin_guard(request)
        data = await read_limited(request, MAX_IMAGE_BODY)
        return _json(await floorplan_service(request).upload_image(data))

    @rooms_handler
    async def floorplan_image(request: web.Request) -> web.StreamResponse:
        found = floorplan_service(request).image_path(request.match_info["image_id"])
        if found is None:
            raise FloorPlanError("unknown_image", "Unknown image", 404)
        path, content_type = found
        return web.FileResponse(path, headers={
            "Content-Type": content_type,
            "Cache-Control": "private, max-age=31536000, immutable",  # content-addressed
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    @rooms_handler
    async def floorplan_export(request: web.Request) -> web.Response:
        data = floorplan_service(request).export()
        return _json(data, headers=download_name("smart-comfort-floorplan", data["exported_at"]))

    @rooms_handler
    async def floorplan_import_review(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_IMPORT_BODY)
        return _json(floorplan_service(request).import_review(body.get("file")))

    @rooms_handler
    async def floorplan_import(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_IMPORT_BODY)
        return _json(await floorplan_service(request).import_plan(body.get("file"), body.get("revision")))

    @rooms_handler
    async def floorplan_backups(request: web.Request) -> web.Response:
        return _json({"backups": floorplan_service(request).backups()})

    @rooms_handler
    async def floorplan_backup(request: web.Request) -> web.Response:
        data = floorplan_service(request).backup(request.match_info["backup_id"])
        return _json(data, headers=download_name("smart-comfort-floorplan-backup", data.get("created_at")))

    @rooms_handler
    async def floorplan_restore(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await floorplan_service(request).restore(request.match_info["backup_id"], body.get("revision")))

    # ---------------------------------------------------------- presence
    def presence_service(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        rooms_service(request)  # Home Assistant must be connected
        if core.presence is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.presence

    def scope(body: dict[str, Any]) -> Any:
        area_id = body.get("area_id")
        if area_id is not None and not isinstance(area_id, str):
            raise RoomsError("invalid_area", "Invalid area id")
        return area_id or None

    @rooms_handler
    async def presence_overview(request: web.Request) -> web.Response:
        return _json(await presence_service(request).overview())

    @rooms_handler
    async def presence_diagnostics(request: web.Request) -> web.Response:
        return _json(await presence_service(request).diagnostics())

    @rooms_handler
    async def presence_room(request: web.Request) -> web.Response:
        return _json(await presence_service(request).workspace(request.match_info["area_id"]))

    @rooms_handler
    async def presence_timeline(request: web.Request) -> web.Response:
        return _json(await presence_service(request).timeline(request.match_info["area_id"],
                                                              request.query.get("window", "3600")))

    @rooms_handler
    async def presence_review(request: web.Request) -> web.Response:
        body = await write_guard(request)
        return _json(await presence_service(request).review(scope(body), body.get("change"), body.get("revision")))

    @rooms_handler
    async def presence_save(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = presence_service(request)
        result = await service.save(scope(body), body.get("change"), body.get("revision"), body.get("confirmed"))
        return _json({"result": result})

    # ------------------------------------------------------------ engine
    def engine_runtime(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.occupancy is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.occupancy

    def engine_error(err: EngineError) -> web.Response:
        return _json({"error": str(err), "code": err.code}, status=err.status)

    @rooms_handler
    async def engine_status(request: web.Request) -> web.Response:
        core: AppCore = request.app[CORE_KEY]
        runtime = engine_runtime(request)
        acknowledged = None
        if core.ha is not None and core.ha.connected:
            try:
                acknowledged = await core.ha.command("smart_occupancy/engine/status")
            except (HomeAssistantError, ConnectionError, TimeoutError):
                acknowledged = None
        data = runtime.status()
        if isinstance(acknowledged, dict):
            # Read from the integration just now: the owner that really publishes occupancy.
            data["acknowledged_owner"] = acknowledged.get("state")
            data["epoch"] = acknowledged.get("epoch")
            data["lease_until"] = acknowledged.get("lease_until")
        data["integration_status"] = acknowledged
        data["area_names"] = {a.get("area_id"): a.get("name") for a in (core.ha.areas if core.ha else [])}
        return _json(data)

    @rooms_handler
    async def engine_comparison(request: web.Request) -> web.Response:
        try:
            limit = max(1, min(200, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        return _json({"records": engine_runtime(request).comparator.records(limit)})

    @rooms_handler
    async def engine_handover(request: web.Request) -> web.Response:
        body = await write_guard(request)
        if body.get("confirm") is not True:
            raise RoomsError("confirmation_required", "Confirm the handover after reviewing the readiness checks")
        try:
            result = await engine_runtime(request).handover(request.headers.get("X-Remote-User-Id"))
        except EngineError as err:
            return engine_error(err)
        return _json({"result": result})

    @rooms_handler
    async def engine_release(request: web.Request) -> web.Response:
        await write_guard(request)
        try:
            result = await engine_runtime(request).release(request.headers.get("X-Remote-User-Id"))
        except EngineError as err:
            return engine_error(err)
        return _json({"result": result})

    # ---------------------------------------------------------- lighting (A4)
    def lighting_runtime(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.lighting is None or core.lighting_api is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.lighting, core.lighting_api

    def lighting_error(err: Exception) -> web.Response:
        return _json({"error": str(err), "code": getattr(err, "code", "error")}, status=getattr(err, "status", 409))

    async def current_user(request: web.Request) -> tuple[str | None, bool]:
        user_id = request.headers.get("X-Remote-User-Id")
        if settings.dev_mode and not user_id:
            return None, True
        try:
            return user_id, bool(await rooms_service(request).is_admin(user_id))
        except Exception:  # noqa: BLE001 - fail closed
            return user_id, False

    @rooms_handler
    async def lighting_status(request: web.Request) -> web.Response:
        runtime, api = lighting_runtime(request)
        core: AppCore = request.app[CORE_KEY]
        acknowledged = None
        if core.ha is not None and core.ha.connected:
            try:
                acknowledged = await core.ha.command("smart_occupancy/lighting/bridge/status")
            except (HomeAssistantError, ConnectionError, TimeoutError):
                acknowledged = None
        data = runtime.status()
        data["acknowledged_owner"] = acknowledged.get("state") if isinstance(acknowledged, dict) else None
        data["integration_status"] = acknowledged
        data["commands_go_to"] = api.where()
        data["backups"] = runtime.backups()
        user_id, admin = await current_user(request)
        data["user"] = {"is_admin": admin}
        data["area_names"] = {a.get("area_id"): a.get("name") for a in (core.ha.areas if core.ha else [])}
        return _json(data)

    @rooms_handler
    async def lighting_command(request: web.Request) -> web.Response:
        """One lighting editor command (the integration's API): forwarded, or run by the App after the handover."""
        runtime, api = lighting_runtime(request)
        if request.content_type != "application/json" or request.headers.get(CSRF_HEADER) != "1":
            raise RoomsError("forbidden", "This request must come from the Smart Comfort interface", 403)
        if request.headers.get("Origin") and not origin_allowed(request):
            raise RoomsError("forbidden", "Cross-site requests are not allowed", 403)
        try:
            body = json.loads(await read_limited(request, MAX_BODY))
        except (ValueError, UnicodeDecodeError) as err:
            raise RoomsError("invalid_json", "The request body is not valid JSON") from err
        if not isinstance(body, dict) or not isinstance(body.get("type"), str):
            raise RoomsError("invalid_json", "The request needs a command type")
        if api.is_write(body["type"]):
            await admin_guard(request)
        user_id, admin = await current_user(request)
        try:
            result = await api.call(body, user_id=user_id, is_admin=admin)
        except LightingApiError as err:
            return lighting_error(err)
        return _json({"result": result, "handled_by": api.where()})

    @rooms_handler
    async def lighting_handover(request: web.Request) -> web.Response:
        body = await write_guard(request)
        if body.get("confirm") is not True:
            raise RoomsError("confirmation_required", "Confirm the handover after reviewing the readiness checks")
        runtime, _api = lighting_runtime(request)
        try:
            result = await runtime.handover(request.headers.get("X-Remote-User-Id"))
        except LightingError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def lighting_release(request: web.Request) -> web.Response:
        body = await write_guard(request)
        runtime, _api = lighting_runtime(request)
        try:
            if body.get("mode") == "return":
                result = await runtime.return_control(request.headers.get("X-Remote-User-Id"))
            else:
                result = await runtime.release(request.headers.get("X-Remote-User-Id"))
        except LightingError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def lighting_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        runtime, _api = lighting_runtime(request)
        data = runtime.export()
        return web.Response(text=json.dumps(data, indent=1), content_type="application/json",
                            headers=download_name("smart-comfort-lighting", data["at"]))

    # ---------------------------------------------------------- curtains (A5)
    def curtains_runtime(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.curtains is None or core.curtains_api is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.curtains, core.curtains_api

    async def read_command(request: web.Request, api: Any) -> tuple[dict[str, Any], str | None, bool]:
        if request.content_type != "application/json" or request.headers.get(CSRF_HEADER) != "1":
            raise RoomsError("forbidden", "This request must come from the Smart Comfort interface", 403)
        if request.headers.get("Origin") and not origin_allowed(request):
            raise RoomsError("forbidden", "Cross-site requests are not allowed", 403)
        try:
            body = json.loads(await read_limited(request, MAX_BODY))
        except (ValueError, UnicodeDecodeError) as err:
            raise RoomsError("invalid_json", "The request body is not valid JSON") from err
        if not isinstance(body, dict) or not isinstance(body.get("type"), str):
            raise RoomsError("invalid_json", "The request needs a command type")
        if api.is_write(body["type"]):
            await admin_guard(request)
        user_id, admin = await current_user(request)
        return body, user_id, admin

    @rooms_handler
    async def curtains_status(request: web.Request) -> web.Response:
        runtime, api = curtains_runtime(request)
        core: AppCore = request.app[CORE_KEY]
        acknowledged = None
        if core.ha is not None and core.ha.connected:
            try:
                acknowledged = await core.ha.command("smart_occupancy/curtains/bridge/status")
            except (HomeAssistantError, ConnectionError, TimeoutError):
                acknowledged = None
        data = runtime.status()
        data["acknowledged_owner"] = acknowledged.get("state") if isinstance(acknowledged, dict) else None
        data["integration_status"] = acknowledged
        data["commands_go_to"] = api.where()
        data["backups"] = runtime.backups()
        _user_id, admin = await current_user(request)
        data["user"] = {"is_admin": admin}
        data["area_names"] = {a.get("area_id"): a.get("name") for a in (core.ha.areas if core.ha else [])}
        return _json(data)

    @rooms_handler
    async def curtains_command(request: web.Request) -> web.Response:
        _runtime, api = curtains_runtime(request)
        body, user_id, admin = await read_command(request, api)
        try:
            result = await api.call(body, user_id=user_id, is_admin=admin)
        except CurtainsApiError as err:
            return lighting_error(err)
        return _json({"result": result, "handled_by": api.where()})

    @rooms_handler
    async def curtains_handover(request: web.Request) -> web.Response:
        body = await write_guard(request)
        if body.get("confirm") is not True:
            raise RoomsError("confirmation_required", "Confirm the handover after reviewing the readiness checks")
        runtime, _api = curtains_runtime(request)
        try:
            result = await runtime.handover(request.headers.get("X-Remote-User-Id"))
        except CurtainsError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def curtains_release(request: web.Request) -> web.Response:
        body = await write_guard(request)
        runtime, _api = curtains_runtime(request)
        try:
            if body.get("mode") == "return":
                result = await runtime.return_control(request.headers.get("X-Remote-User-Id"),
                                                      force=body.get("force") is True)
            else:
                result = await runtime.release(request.headers.get("X-Remote-User-Id"))
        except CurtainsError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def curtains_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        runtime, _api = curtains_runtime(request)
        data = runtime.export()
        return web.Response(text=json.dumps(data, indent=1), content_type="application/json",
                            headers=download_name("smart-comfort-curtains", data["at"]))

    @rooms_handler
    async def curtains_import_review(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        runtime, _api = curtains_runtime(request)
        try:
            return _json(runtime.import_review(body.get("file")))
        except CurtainsError as err:
            return lighting_error(err)

    @rooms_handler
    async def curtains_import(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        runtime, _api = curtains_runtime(request)
        try:
            return _json(await runtime.import_apply(body.get("file"), body.get("revision"),
                                        confirm_automatic=body.get("confirm_automatic") is True))
        except CurtainsError as err:
            return lighting_error(err)

    @rooms_handler
    async def curtains_backups(request: web.Request) -> web.Response:
        runtime, _api = curtains_runtime(request)
        return _json({"backups": runtime.backups()})

    # ------------------------------------------------------- predictions (A5)
    def forecast_runtime(request: web.Request):
        core: AppCore = request.app[CORE_KEY]
        if core.forecast is None or core.forecast_api is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.forecast, core.forecast_api

    @rooms_handler
    async def predictions_status(request: web.Request) -> web.Response:
        runtime, api = forecast_runtime(request)
        core: AppCore = request.app[CORE_KEY]
        acknowledged = None
        if core.ha is not None and core.ha.connected:
            try:
                acknowledged = await core.ha.command("smart_occupancy/forecast/bridge/status")
            except (HomeAssistantError, ConnectionError, TimeoutError):
                acknowledged = None
        data = runtime.status()
        data["acknowledged_owner"] = acknowledged.get("state") if isinstance(acknowledged, dict) else None
        data["integration_status"] = acknowledged
        data["commands_go_to"] = api.where()
        data["backups"] = runtime.backups()
        _user_id, admin = await current_user(request)
        data["user"] = {"is_admin": admin}
        data["area_names"] = {a.get("area_id"): a.get("name") for a in (core.ha.areas if core.ha else [])}
        try:
            data["house"] = runtime.house()
        except ForecastError as err:
            data["house"] = None
            data["house_error"] = str(err)
        return _json(data)

    @rooms_handler
    async def predictions_room(request: web.Request) -> web.Response:
        runtime, _api = forecast_runtime(request)
        try:
            return _json(runtime.room(request.match_info["area_id"]))
        except ForecastError as err:
            return lighting_error(err)

    @rooms_handler
    async def predictions_command(request: web.Request) -> web.Response:
        _runtime, api = forecast_runtime(request)
        body, user_id, admin = await read_command(request, api)
        try:
            result = await api.call(body, user_id=user_id, is_admin=admin)
        except ForecastApiError as err:
            return lighting_error(err)
        return _json({"result": result, "handled_by": api.where()})

    @rooms_handler
    async def predictions_handover(request: web.Request) -> web.Response:
        body = await write_guard(request)
        if body.get("confirm") is not True:
            raise RoomsError("confirmation_required", "Confirm the handover after reviewing the readiness checks")
        runtime, _api = forecast_runtime(request)
        try:
            result = await runtime.handover(request.headers.get("X-Remote-User-Id"))
        except ForecastError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def predictions_release(request: web.Request) -> web.Response:
        body = await write_guard(request)
        runtime, _api = forecast_runtime(request)
        try:
            if body.get("mode") == "return":
                result = await runtime.return_control(request.headers.get("X-Remote-User-Id"))
            else:
                result = await runtime.release(request.headers.get("X-Remote-User-Id"))
        except ForecastError as err:
            return lighting_error(err)
        return _json({"result": result})

    @rooms_handler
    async def predictions_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        runtime, _api = forecast_runtime(request)
        data = runtime.export()
        return web.Response(text=json.dumps(data, indent=1), content_type="application/json",
                            headers=download_name("smart-comfort-predictions", data["at"]))

    @rooms_handler
    async def predictions_import_review(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_IMPORT_BODY)
        runtime, _api = forecast_runtime(request)
        try:
            return _json(runtime.import_review(body.get("file")))
        except ForecastError as err:
            return lighting_error(err)

    @rooms_handler
    async def predictions_import(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_IMPORT_BODY)
        runtime, _api = forecast_runtime(request)
        try:
            return _json(await runtime.import_apply(body.get("file"), body.get("revision")))
        except ForecastError as err:
            return lighting_error(err)

    # ------------------------------------------------------ system (A6)
    def system_error(err: Exception) -> web.Response:
        return _json({"error": str(err), "code": getattr(err, "code", "error")}, status=getattr(err, "status", 409))

    @rooms_handler
    async def system_status(request: web.Request) -> web.Response:
        core: AppCore = request.app[CORE_KEY]
        return _json(core.system())

    @rooms_handler
    async def dashboard(request: web.Request) -> web.Response:
        core: AppCore = request.app[CORE_KEY]
        return _json(core.dashboard())

    @rooms_handler
    async def system_pair(request: web.Request) -> web.Response:
        await write_guard(request)
        core: AppCore = request.app[CORE_KEY]
        if core.onboarding is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        try:
            return _json({"result": await core.onboarding.pair(request.headers.get("X-Remote-User-Id"))})
        except OnboardingError as err:
            return system_error(err)

    def _runtime_of(core: AppCore, domain: Any) -> Any:
        runtime = {"occupancy": core.occupancy, "lighting": core.lighting, "curtains": core.curtains,
                   "forecast": core.forecast}.get(domain)
        if runtime is None:
            raise RoomsError("invalid_domain", "Unknown control domain", 400)
        return runtime

    @rooms_handler
    async def system_claim(request: web.Request) -> web.Response:
        body = await write_guard(request)
        core: AppCore = request.app[CORE_KEY]
        runtime = _runtime_of(core, body.get("domain"))
        try:
            result = await runtime.handover(request.headers.get("X-Remote-User-Id"))
        except Exception as err:  # noqa: BLE001 - the runtimes' typed refusals
            return system_error(err)
        return _json({"result": result})

    @rooms_handler
    async def system_release(request: web.Request) -> web.Response:
        body = await write_guard(request)
        core: AppCore = request.app[CORE_KEY]
        runtime = _runtime_of(core, body.get("domain"))
        try:
            result = await runtime.release(request.headers.get("X-Remote-User-Id"))
        except Exception as err:  # noqa: BLE001
            return system_error(err)
        return _json({"result": result})

    @rooms_handler
    async def system_revoke(request: web.Request) -> web.Response:
        body = await write_guard(request)
        core: AppCore = request.app[CORE_KEY]
        if core.companion is None or not core.companion.present:
            raise RoomsError("not_connected", "The companion is not connected", 503)
        try:
            return _json({"result": await core.companion.revoke(body.get("domain"))})
        except CompanionError as err:
            return system_error(err)

    # ----------------------------------------------------- migration (A6)
    def migration_service(request: web.Request) -> Any:
        core: AppCore = request.app[CORE_KEY]
        if core.migration is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return core.migration

    @rooms_handler
    async def migration_status(request: web.Request) -> web.Response:
        return _json(migration_service(request).status())

    @rooms_handler
    async def migration_step(request: web.Request) -> web.Response:
        body = await write_guard(request)
        service = migration_service(request)
        step = request.match_info["step"]
        user = request.headers.get("X-Remote-User-Id")
        try:
            if step == "inspect":
                result = await service.inspect()
            elif step == "backup":
                result = await service.backup(user)
            elif step == "validate":
                result = await service.validate()
            elif step == "transfer_config":
                result = await service.transfer_config(apply=body.get("apply") is True, user_id=user)
            elif step == "transfer_control":
                result = await service.transfer_control(str(body.get("domain") or ""), user)
            elif step == "activate":
                result = await service.activate(reimport=body.get("reimport") is True, user_id=user)
            elif step == "verify":
                result = await service.verify()
            elif step == "retire":
                if body.get("confirm") is not True:
                    raise RoomsError("confirmation_required", "Confirm the retirement of the legacy data")
                result = await service.retire(user)
            elif step == "abort":
                result = await service.abort(user, restore_legacy=body.get("restore_legacy") is True)
            else:
                raise RoomsError("unknown_step", "Unknown migration step", 404)
        except (MigrationError, CompanionError, CoreConfigError) as err:
            return system_error(err)
        return _json({"result": result, "status": service.status()})

    @rooms_handler
    async def migration_backups(request: web.Request) -> web.Response:
        return _json({"backups": migration_service(request).backups()})

    @rooms_handler
    async def migration_backup(request: web.Request) -> web.Response:
        await admin_guard(request)
        try:
            data = migration_service(request).backup_file(request.match_info["backup_id"])
        except MigrationError as err:
            return system_error(err)
        return web.Response(text=json.dumps(data, indent=1, default=str), content_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="{request.match_info["backup_id"]}.json"'})

    # --------------------------------------------------------- setup (A6)
    @rooms_handler
    async def setup_status(request: web.Request) -> web.Response:
        core: AppCore = request.app[CORE_KEY]
        if core.onboarding is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        return _json(core.onboarding.status())

    @rooms_handler
    async def setup_start(request: web.Request) -> web.Response:
        await write_guard(request)
        core: AppCore = request.app[CORE_KEY]
        if core.onboarding is None:
            raise RoomsError("not_connected", "The App is starting", 503)
        try:
            return _json({"result": await core.onboarding.start_fresh(request.headers.get("X-Remote-User-Id"))})
        except OnboardingError as err:
            return system_error(err)

    # -------------------------------------------- core configuration (A6)
    @rooms_handler
    async def config_export(request: web.Request) -> web.Response:
        await admin_guard(request)
        core: AppCore = request.app[CORE_KEY]
        data = core.core_config.export()
        return web.Response(text=json.dumps(data, indent=1), content_type="application/json",
                            headers=download_name("smart-comfort-config", data["created_at"]))

    @rooms_handler
    async def config_import_review(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        core: AppCore = request.app[CORE_KEY]
        states = core.ha.states if core.ha else {}
        try:
            return _json(core.core_config.import_review(body.get("file"), entity_exists=lambda e: e in states))
        except CoreConfigError as err:
            return system_error(err)

    @rooms_handler
    async def config_import(request: web.Request) -> web.Response:
        body = await write_guard(request, MAX_PLAN_BODY)
        core: AppCore = request.app[CORE_KEY]
        try:
            result = await core.core_config.import_apply(body.get("file"), expected_revision=body.get("revision"),
                                                         source="import", reason="import")
        except CoreConfigError as err:
            return system_error(err)
        if core.core_host is not None:
            core.core_host.schedule_snapshot(0)
        return _json(result)

    @rooms_handler
    async def config_backups(request: web.Request) -> web.Response:
        await admin_guard(request)
        core: AppCore = request.app[CORE_KEY]
        return _json({"backups": core.core_config.backups()})

    @rooms_handler
    async def config_backup(request: web.Request) -> web.Response:
        await admin_guard(request)
        core: AppCore = request.app[CORE_KEY]
        try:
            data = core.core_config.backup(request.match_info["backup_id"])
        except CoreConfigError as err:
            return system_error(err)
        return _json(data, headers={"Content-Disposition": f'attachment; filename="{request.match_info["backup_id"]}.json"'})

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
    app.router.add_get("/api/floorplan", floorplan_get)
    app.router.add_post("/api/floorplan/save", floorplan_save)
    app.router.add_get("/api/floorplan/context", floorplan_context)
    app.router.add_get("/api/floorplan/entity/{entity_id}", floorplan_entity)
    app.router.add_post("/api/floorplan/images", floorplan_upload)
    app.router.add_get("/api/floorplan/images/{image_id}", floorplan_image)
    app.router.add_get("/api/floorplan/export", floorplan_export)
    app.router.add_post("/api/floorplan/import/review", floorplan_import_review)
    app.router.add_post("/api/floorplan/import", floorplan_import)
    app.router.add_get("/api/floorplan/backups", floorplan_backups)
    app.router.add_get("/api/floorplan/backups/{backup_id}", floorplan_backup)
    app.router.add_post("/api/floorplan/backups/{backup_id}/restore", floorplan_restore)
    app.router.add_get("/api/climate", climate_get)
    app.router.add_get("/api/climate/overview", climate_overview)
    app.router.add_get("/api/climate/discovery", climate_discovery)
    app.router.add_get("/api/climate/rooms/{area_id}", climate_room)
    app.router.add_get("/api/climate/rooms/{area_id}/history", climate_history)
    app.router.add_get("/api/climate/learning", learning_overview)
    app.router.add_get("/api/climate/rooms/{area_id}/learning", learning_room)
    app.router.add_get("/api/climate/rooms/{area_id}/learning/export", learning_export)
    app.router.add_post("/api/climate/learning/rebuild", learning_rebuild)
    app.router.add_post("/api/climate/learning/reset", learning_reset)
    app.router.add_post("/api/climate/review", climate_review)
    app.router.add_post("/api/climate/save", climate_save)
    app.router.add_get("/api/climate/backups", climate_backups)
    app.router.add_get("/api/climate/backups/{backup_id}", climate_backup)
    app.router.add_post("/api/climate/restore/review", climate_restore_review)
    app.router.add_post("/api/climate/restore", climate_restore)
    app.router.add_get("/api/climate/export", climate_export)
    app.router.add_post("/api/climate/import/review", climate_import_review)
    app.router.add_post("/api/climate/import", climate_import)
    app.router.add_get("/api/presence", presence_overview)
    app.router.add_get("/api/presence/diagnostics", presence_diagnostics)
    app.router.add_post("/api/presence/review", presence_review)
    app.router.add_post("/api/presence/save", presence_save)
    app.router.add_get("/api/presence/rooms/{area_id}", presence_room)
    app.router.add_get("/api/presence/rooms/{area_id}/timeline", presence_timeline)
    app.router.add_get("/api/engine", engine_status)
    app.router.add_get("/api/engine/comparison", engine_comparison)
    app.router.add_post("/api/engine/handover", engine_handover)
    app.router.add_post("/api/engine/release", engine_release)
    app.router.add_get("/api/lighting", lighting_status)
    app.router.add_post("/api/lighting/command", lighting_command)
    app.router.add_post("/api/lighting/handover", lighting_handover)
    app.router.add_post("/api/lighting/release", lighting_release)
    app.router.add_get("/api/lighting/export", lighting_export)
    app.router.add_get("/api/curtains", curtains_status)
    app.router.add_post("/api/curtains/command", curtains_command)
    app.router.add_post("/api/curtains/handover", curtains_handover)
    app.router.add_post("/api/curtains/release", curtains_release)
    app.router.add_get("/api/curtains/export", curtains_export)
    app.router.add_post("/api/curtains/import/review", curtains_import_review)
    app.router.add_post("/api/curtains/import", curtains_import)
    app.router.add_get("/api/curtains/backups", curtains_backups)
    app.router.add_get("/api/predictions", predictions_status)
    app.router.add_get("/api/predictions/rooms/{area_id}", predictions_room)
    app.router.add_post("/api/predictions/command", predictions_command)
    app.router.add_post("/api/predictions/handover", predictions_handover)
    app.router.add_post("/api/predictions/release", predictions_release)
    app.router.add_get("/api/predictions/export", predictions_export)
    app.router.add_post("/api/predictions/import/review", predictions_import_review)
    app.router.add_post("/api/predictions/import", predictions_import)
    app.router.add_get("/api/system", system_status)
    app.router.add_get("/api/dashboard", dashboard)
    app.router.add_post("/api/system/pair", system_pair)
    app.router.add_post("/api/system/claim", system_claim)
    app.router.add_post("/api/system/release", system_release)
    app.router.add_post("/api/system/revoke", system_revoke)
    app.router.add_get("/api/migration", migration_status)
    app.router.add_get("/api/migration/backups", migration_backups)
    app.router.add_get("/api/migration/backups/{backup_id}", migration_backup)
    app.router.add_post("/api/migration/{step}", migration_step)
    app.router.add_get("/api/setup", setup_status)
    app.router.add_post("/api/setup/start", setup_start)
    app.router.add_get("/api/config/export", config_export)
    app.router.add_post("/api/config/import/review", config_import_review)
    app.router.add_post("/api/config/import", config_import)
    app.router.add_get("/api/config/backups", config_backups)
    app.router.add_get("/api/config/backups/{backup_id}", config_backup)
    app.router.add_route("*", "/api/{tail:.*}", api_not_found)
    assets = settings.static_dir / "assets"
    if assets.is_dir():
        app.router.add_static("/assets/", assets, append_version=False)
    app.router.add_get("/" + _STATIC_FILE, static_file)
    return app
