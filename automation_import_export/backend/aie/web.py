"""HTTP server: the web interface (Overview, Export, Import, Archives) and its JSON API, through Ingress only.

* Requests are accepted only from the Supervisor's Ingress address.
* Every read and every change requires a Home Assistant administrator (the Supervisor passes the user id in
  ``X-Remote-User-Id``; the browser cannot set it): automation configurations and archives are private.
* Every changing request is a POST that needs the header ``X-AIE: 1`` and a same-origin ``Origin`` (when the
  browser sends one). JSON bodies are limited to 8 MB, archive uploads to 64 MB.
* Destructive steps carry the hash of the plan the user reviewed and an explicit confirmation.

The interface uses relative URLs only; ``index.html`` gets a ``<base href>`` for the Ingress prefix.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlsplit

from aiohttp import web

from .archive import MAX_ARCHIVE_BYTES, ArchiveError
from .const import APP_NAME, APP_VERSION
from .core import AppCore, OperationError
from .restore import PlanError
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", AppCore)
CSRF_HEADER = "X-AIE"
MAX_BODY = 8 * 1024 * 1024
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


class Forbidden(Exception):
    pass


def build_app(settings: Settings, core: AppCore) -> web.Application:
    @web.middleware
    async def access(request: web.Request, handler: Any) -> web.StreamResponse:
        if not settings.is_allowed(request.remote):
            _LOGGER.warning("Refused a request from %s (only Home Assistant Ingress is allowed)", request.remote)
            return web.Response(status=403, text="Forbidden: open this App from the Home Assistant sidebar.")
        try:
            response = await handler(request)
        except Forbidden as err:
            response = _json({"error": "forbidden", "message": str(err)}, 403)
        except (OperationError, PlanError) as err:
            response = _json({"error": err.code, "message": str(err), **err.extra}, err.status)
        except ArchiveError as err:
            response = _json({"error": err.code, "message": str(err)}, 422)
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

    app = web.Application(middlewares=[access], client_max_size=MAX_ARCHIVE_BYTES + 1024 * 1024)
    app[CORE_KEY] = core

    async def is_admin(request: web.Request) -> bool:
        user_id = request.headers.get("X-Remote-User-Id")
        if settings.dev_mode and not user_id:
            return True
        try:
            return await core.is_admin(user_id)
        except Exception:  # noqa: BLE001 - fail closed
            return False

    async def require_admin(request: web.Request) -> str | None:
        if not await is_admin(request):
            raise Forbidden(f"Only Home Assistant administrators can use the {APP_NAME}")
        return request.headers.get("X-Remote-User-Id")

    async def guard_post(request: web.Request, content_type: str) -> str | None:
        if request.headers.get(CSRF_HEADER) != "1" or request.content_type != content_type:
            raise Forbidden(f"This request must come from the {APP_NAME} interface")
        if not origin_allowed(request):
            raise Forbidden("Cross-site requests are not allowed")
        user = await require_admin(request)
        return user

    async def read_body(request: web.Request, limit: int) -> bytes:
        if request.content_length is not None and request.content_length > limit:
            raise OperationError("The request is too large", "too_large", 413)
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            raise OperationError("The request is too large", "too_large", 413)
        return raw

    async def json_body(request: web.Request) -> tuple[dict[str, Any], str | None]:
        user = await guard_post(request, "application/json")
        raw = await read_body(request, MAX_BODY)
        try:
            data = json.loads(raw or b"{}")
        except ValueError as err:
            raise OperationError("The request body is not JSON") from err
        if not isinstance(data, dict):
            raise OperationError("The request body must be an object")
        return data, user

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
        admin = await is_admin(request)
        base = {"app": {"name": APP_NAME, "version": APP_VERSION}, "user": {"is_admin": admin},
                "connection": core.connection()}
        if not admin:
            return _json(base)
        return _json({**base, **await core.overview()})

    async def inventory(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.inventory_view())

    async def registry(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.registry_view())

    async def operations(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json({"operations": core.journal.listing(100)})

    async def operation(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.operation(request.match_info["op_id"]))

    async def archives(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json({"archives": core.archive_list()})

    async def archive(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.archive_view(request.match_info["archive_id"]))

    async def download(request: web.Request) -> web.Response:
        await require_admin(request)
        data, filename = core.archive_file(request.match_info["archive_id"])
        return web.Response(body=data, content_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}"})

    async def diagnostics(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.diagnostics(), headers={
            "Content-Disposition": 'attachment; filename="automation-import-export-diagnostics.json"'})

    # ----------------------------------------------------------- changes
    def post(handler: Any) -> Any:
        async def wrapped(request: web.Request) -> web.Response:
            data, user = await json_body(request)
            return _json(await handler(data, user))
        return wrapped

    async def upload(request: web.Request) -> web.Response:
        user = await guard_post(request, "application/zip")
        raw = await read_body(request, MAX_ARCHIVE_BYTES)
        return _json(await core.upload(raw, request.headers.get("X-Filename", "")[:120], user))

    routes = {
        "/api/export": core.start_export,
        "/api/cleanup/start": core.start_cleanup,
        "/api/cleanup/confirm": core.confirm_cleanup,
        "/api/cleanup/resume": core.resume_cleanup,
        "/api/operations/cancel": core.cancel,
        "/api/operations/reconcile": core.reconcile,
        "/api/archives/verify": core.verify_archive,
        "/api/archives/delete": core.delete_archive,
        "/api/import/yaml": core.import_yaml,
        "/api/restore/plan": lambda d, u: core.restore_plan(d),
        "/api/restore/execute": core.restore_execute,
        "/api/restore/rollback": core.rollback,
        "/api/registry/backup": core.start_registry_backup,
        "/api/registry/cleanup/start": core.start_registry_cleanup,
        "/api/registry/cleanup/confirm": core.confirm_registry_cleanup,
        "/api/registry/restore/plan": lambda d, u: core.registry_restore_plan(d),
        "/api/registry/restore/execute": core.registry_restore_execute,
    }

    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/assets/{name}", asset)
    r.add_get("/api/health", health)
    r.add_get("/api/overview", overview)
    r.add_get("/api/inventory", inventory)
    r.add_get("/api/registry", registry)
    r.add_get("/api/operations", operations)
    r.add_get("/api/operations/{op_id}", operation)
    r.add_get("/api/archives", archives)
    r.add_get("/api/archives/{archive_id}", archive)
    r.add_get("/api/archives/{archive_id}/download", download)
    r.add_get("/api/diagnostics", diagnostics)
    r.add_post("/api/archives/upload", upload)
    for path, handler in routes.items():
        r.add_post(path, post(handler))
    return app
