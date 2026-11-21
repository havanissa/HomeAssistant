"""HTTP server: the web interface (Overview, Rooms, Settings) and its JSON API, through Ingress only.

* Requests are accepted only from the Supervisor's Ingress address.
* Every changing request is a POST with a JSON body that needs the header
  ``X-SOD: 1``, a same-origin ``Origin`` (when the browser sends one) and a Home
  Assistant administrator (the Supervisor passes the user id in
  ``X-Remote-User-Id``; the browser cannot set it).
* Every change is reviewed first (``.../review``) and saved with the revision
  the review showed (``expected_revision``); a stale revision is refused.

The interface uses relative URLs only; ``index.html`` gets a ``<base href>`` for
the Ingress prefix.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .config import ConfigError
from .const import APP_NAME, APP_VERSION
from .core import AppCore
from .publisher import LinkError
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", AppCore)
CSRF_HEADER = "X-SOD"
MAX_BODY = 512 * 1024
MAX_IMPORT_BODY = 8 * 1024 * 1024
_INGRESS_PATH = re.compile(r"^(/[A-Za-z0-9._~\-]+)+$")
_ASSET = re.compile(r"^[a-z0-9_\-]+\.(js|css|svg|png)$")
CONTENT_TYPES = {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml", "png": "image/png"}
CSP = "; ".join((
    "default-src 'self'", "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
    "connect-src 'self'", "frame-ancestors 'self'", "base-uri 'self'", "form-action 'none'", "object-src 'none'",
))


def base_href(request: web.Request) -> str:
    prefix = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return f"{prefix}/" if prefix and _INGRESS_PATH.match(prefix) else "/"


def origin_allowed(request: web.Request) -> bool:
    """A browser's Origin must be the page's own host (Ingress keeps Host and adds X-Forwarded-Host)."""
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parts = urlsplit(origin)

    def norm(host: str) -> str:
        host = host.lower()
        default = {"http": ":80", "https": ":443"}.get(parts.scheme)
        return host[: -len(default)] if default and host.endswith(default) else host

    candidates = {norm(v.strip()) for h in ("X-Forwarded-Host", "Host")
                  for v in request.headers.get(h, "").split(",") if v.strip()}
    return norm(parts.netloc) in candidates


def _json(data: Any, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    return web.Response(text=json.dumps(data, ensure_ascii=False, default=str), status=status,
                        content_type="application/json", headers=headers)


def _error(err: Exception) -> web.Response:
    if isinstance(err, ConfigError):
        return _json({"error": err.code, "message": str(err), "problems": err.problems, **err.extra}, err.status)
    if isinstance(err, LinkError):
        return _json({"error": err.code, "message": str(err)}, err.status)
    raise err


def build_app(settings: Settings, core: AppCore) -> web.Application:
    @web.middleware
    async def access(request: web.Request, handler: Any) -> web.StreamResponse:
        if not settings.is_allowed(request.remote):
            _LOGGER.warning("Refused a request from %s (only Home Assistant Ingress is allowed)", request.remote)
            return web.Response(status=403, text="Forbidden: open this App from the Home Assistant sidebar.")
        try:
            response = await handler(request)
        except (ConfigError, LinkError) as err:
            response = _error(err)
        except web.HTTPException as exc:
            if not request.path.startswith("/api/"):
                raise
            response = _json({"error": exc.reason, "message": exc.reason}, exc.status)
        if not response.prepared:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app = web.Application(middlewares=[access], client_max_size=MAX_IMPORT_BODY)
    app[CORE_KEY] = core

    async def body(request: web.Request, limit: int = MAX_BODY) -> tuple[dict[str, Any], str | None]:
        """CSRF and administrator checks for every changing request; returns (JSON body, user id)."""
        if request.headers.get(CSRF_HEADER) != "1" or request.content_type != "application/json":
            raise ConfigError("forbidden", f"This request must come from the {APP_NAME} interface", 403)
        if not origin_allowed(request):
            raise ConfigError("forbidden", "Cross-site requests are not allowed", 403)
        user_id = request.headers.get("X-Remote-User-Id")
        if not (settings.dev_mode and not user_id):
            try:
                admin = await core.is_admin(user_id)
            except Exception as err:  # noqa: BLE001 - fail closed
                raise ConfigError("forbidden", f"Could not verify the Home Assistant user ({type(err).__name__})",
                                  403) from err
            if not admin:
                _LOGGER.warning("Refused a change from a user who is not an administrator")
                raise ConfigError("forbidden", "Only Home Assistant administrators can change the configuration",
                                  403)
        if request.content_length is not None and request.content_length > limit:
            raise ConfigError("too_large", "The request is too large", 413)
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            raise ConfigError("too_large", "The request is too large", 413)
        try:
            data = json.loads(raw or b"{}")
        except ValueError as err:
            raise ConfigError("invalid", "The request body is not JSON") from err
        if not isinstance(data, dict):
            raise ConfigError("invalid", "The request body must be an object")
        return data, user_id

    # ------------------------------------------------------------- pages
    async def index(request: web.Request) -> web.Response:
        page = (settings.static_dir / "index.html").read_text(encoding="utf-8")
        page = page.replace("<head>", f'<head>\n  <base href="{base_href(request)}">', 1)
        page = page.replace("__VERSION__", APP_VERSION)
        return web.Response(text=page, content_type="text/html")

    async def asset(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        match = _ASSET.match(name)
        path = settings.static_dir / name
        if not match or not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type=CONTENT_TYPES[match.group(1)],
                            headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------- reads
    async def health(_request: web.Request) -> web.Response:
        return _json({"ok": True, "version": APP_VERSION})

    async def overview(request: web.Request) -> web.Response:
        runtime = core.runtime
        user_id = request.headers.get("X-Remote-User-Id")
        try:
            admin = (settings.dev_mode and not user_id) or await core.is_admin(user_id)
        except Exception:  # noqa: BLE001
            admin = False
        return _json({
            "app": {"name": APP_NAME, "version": APP_VERSION}, "user": {"is_admin": bool(admin)},
            "rooms": runtime.overview() if runtime else [], "connection": core.connection(),
            "revision": core.store.revision, "recovery": core.store.recovery,
            "runtime": runtime.status() if runtime else None,
        })

    async def room(request: web.Request) -> web.Response:
        area_id = request.match_info["area_id"]
        rooms = core.store.config["rooms"]
        if area_id not in rooms or core.runtime is None:
            raise ConfigError("unknown_room", "This room is not configured", 404)
        return _json({"room": core.runtime.room_view(area_id, detail=True), "config": rooms[area_id],
                      "connection": core.connection(), "recovery": core.store.recovery,
                      "runtime": core.runtime.status(),
                      "global_timing": core.store.config["timing"], "revision": core.store.revision,
                      "rooms": {a: r.get("name") or a for a, r in rooms.items()}})

    async def areas(_request: web.Request) -> web.Response:
        return _json({"areas": core.areas(), "defaults": core.defaults(), "revision": core.store.revision,
                      "global_timing": core.store.config["timing"],
                      "rooms": {a: r.get("name") or a for a, r in core.store.config["rooms"].items()}})

    async def discover(request: web.Request) -> web.Response:
        return _json({"candidates": core.discover(request.match_info["area_id"])})

    async def search(request: web.Request) -> web.Response:
        return _json({"results": core.search(request.query.get("q", ""), request.query.get("for") == "activity")})

    async def settings_view(_request: web.Request) -> web.Response:
        return _json({"timing": core.store.config["timing"], "defaults": core.defaults(),
                      "revision": core.store.revision, "saved_at": core.store.saved_at,
                      "recovery": core.store.recovery, "backups": core.store.backups(),
                      "connection": core.connection(), "runtime": core.runtime.status() if core.runtime else None})

    async def timeline(request: web.Request) -> web.Response:
        area_id = request.query.get("area_id") or None
        return _json({"entries": core.runtime.timeline.recent(area_id, 200) if core.runtime else []})

    async def export(_request: web.Request) -> web.Response:
        return _json(core.store.export(), headers={
            "Content-Disposition": 'attachment; filename="smart-occupancy-detection-config.json"'})

    async def diagnostics(_request: web.Request) -> web.Response:
        return _json(core.diagnostics(), headers={
            "Content-Disposition": 'attachment; filename="smart-occupancy-detection-diagnostics.json"'})

    # ----------------------------------------------------------- changes
    async def room_review(request: web.Request) -> web.Response:
        data, _ = await body(request)
        return _json(core.review_room(data.get("area_id"), data.get("room")))

    async def room_save(request: web.Request) -> web.Response:
        data, user = await body(request)
        return _json(core.save_room(data.get("area_id"), data.get("room"), data.get("expected_revision"), user))

    async def room_delete_review(request: web.Request) -> web.Response:
        data, _ = await body(request)
        return _json(core.review_delete(data.get("area_id")))

    async def room_delete(request: web.Request) -> web.Response:
        data, user = await body(request)
        return _json(core.delete_room(data.get("area_id"), data.get("expected_revision"), user))

    async def timing_review(request: web.Request) -> web.Response:
        data, _ = await body(request)
        return _json(core.review_timing(data.get("timing")))

    async def timing_save(request: web.Request) -> web.Response:
        data, user = await body(request)
        return _json(core.save_timing(data.get("timing"), data.get("expected_revision"), user))

    async def import_review(request: web.Request) -> web.Response:
        data, _ = await body(request, MAX_IMPORT_BODY)
        return _json(core.review_import(data.get("document")))

    async def import_apply(request: web.Request) -> web.Response:
        data, user = await body(request, MAX_IMPORT_BODY)
        return _json(core.apply_import(data.get("document"), data.get("expected_revision"), user))

    async def backup_review(request: web.Request) -> web.Response:
        data, _ = await body(request)
        return _json(core.review_backup(data.get("name")))

    async def backup_restore(request: web.Request) -> web.Response:
        data, user = await body(request)
        return _json(core.restore_backup(data.get("name"), data.get("expected_revision"), user))

    async def sc_review(request: web.Request) -> web.Response:
        data, _ = await body(request, MAX_IMPORT_BODY)
        return _json(core.review_smart_comfort(data.get("document")))

    async def sc_preview(request: web.Request) -> web.Response:
        data, _ = await body(request, MAX_IMPORT_BODY)
        return _json(core.preview_smart_comfort(data.get("document"), data.get("rooms"),
                                                bool(data.get("include_timing"))))

    async def sc_apply(request: web.Request) -> web.Response:
        data, user = await body(request, MAX_IMPORT_BODY)
        return _json(core.apply_smart_comfort(data.get("document"), data.get("rooms"),
                                              bool(data.get("include_timing")), data.get("expected_revision"), user))

    async def pair(request: web.Request) -> web.Response:
        data, user = await body(request)
        if core.link is None:
            raise LinkError("not_ready", "Not started yet", 503)
        return _json(await core.link.pair(replace=bool(data.get("replace")), user_id=user))

    async def unpair(request: web.Request) -> web.Response:
        _data, user = await body(request)
        if core.link is None:
            raise LinkError("not_ready", "Not started yet", 503)
        return _json(await core.link.unpair(user_id=user))

    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/assets/{name}", asset)
    r.add_get("/api/health", health)
    r.add_get("/api/overview", overview)
    r.add_get("/api/rooms/{area_id}", room)
    r.add_get("/api/areas", areas)
    r.add_get("/api/discover/{area_id}", discover)
    r.add_get("/api/entities", search)
    r.add_get("/api/settings", settings_view)
    r.add_get("/api/timeline", timeline)
    r.add_get("/api/export", export)
    r.add_get("/api/diagnostics", diagnostics)
    r.add_post("/api/rooms/review", room_review)
    r.add_post("/api/rooms/save", room_save)
    r.add_post("/api/rooms/delete/review", room_delete_review)
    r.add_post("/api/rooms/delete", room_delete)
    r.add_post("/api/timing/review", timing_review)
    r.add_post("/api/timing/save", timing_save)
    r.add_post("/api/import/review", import_review)
    r.add_post("/api/import/apply", import_apply)
    r.add_post("/api/backups/review", backup_review)
    r.add_post("/api/backups/restore", backup_restore)
    r.add_post("/api/smart-comfort/review", sc_review)
    r.add_post("/api/smart-comfort/preview", sc_preview)
    r.add_post("/api/smart-comfort/apply", sc_apply)
    r.add_post("/api/companion/pair", pair)
    r.add_post("/api/companion/unpair", unpair)
    return app
