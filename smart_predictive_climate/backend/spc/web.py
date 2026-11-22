"""HTTP server: the security frame of the web interface and its JSON API, through Ingress only.

* Requests are accepted only from the Supervisor's Ingress address.
* Every changing request is a POST with a JSON body that needs the header
  ``X-SPC: 1``, a same-origin ``Origin`` (when the browser sends one) and a Home
  Assistant administrator (the Supervisor passes the user id in
  ``X-Remote-User-Id``; the browser cannot set it). Handlers get all of that
  from ``body(request)``.
* ``ConfigError`` and ``LinkError`` raised by a handler become JSON errors
  (``{"error": code, "message": ..., "problems": [...]}``) with their status.

Only what every App needs is here (the page, its assets, health, the export of
the configuration, pairing); everything about climate is added by
``api.add_routes(router, core, body=..., require_admin=..., json=...)``.

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

from .base import BaseCore
from .const import APP_NAME, APP_VERSION
from .publisher import LinkError
from .settings import Settings
from .store import ConfigError

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", BaseCore)
CSRF_HEADER = "X-SPC"
USER_HEADER = "X-Remote-User-Id"
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


def build_app(settings: Settings, core: BaseCore) -> web.Application:
    """The aiohttp application: the security frame, the generic routes and ``api.add_routes``."""
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

    async def require_admin(request: web.Request) -> str | None:
        """The request's Home Assistant user must be an administrator (403 otherwise); returns the user id.

        For reads that only an administrator may see (``body()`` does this check for every change). In
        development mode (``SPC_DEV``) a request without a user id is accepted (returns None).
        """
        user_id = request.headers.get(USER_HEADER)
        if settings.dev_mode and not user_id:
            return None
        try:
            admin = await core.is_admin(user_id)
        except Exception as err:  # noqa: BLE001 - fail closed
            raise ConfigError("forbidden", f"Could not verify the Home Assistant user ({type(err).__name__})",
                              403) from err
        if not admin:
            _LOGGER.warning("Refused a request from a user who is not an administrator")
            raise ConfigError("forbidden", "Only Home Assistant administrators can do this", 403)
        return user_id

    async def body(request: web.Request, limit: int = MAX_BODY) -> tuple[dict[str, Any], str | None]:
        """CSRF and administrator checks for every changing request; returns (JSON body, user id).

        ``limit`` is the largest accepted body in bytes (``MAX_BODY``; at most ``MAX_IMPORT_BODY``).
        """
        if request.headers.get(CSRF_HEADER) != "1" or request.content_type != "application/json":
            raise ConfigError("forbidden", f"This request must come from the {APP_NAME} interface", 403)
        if not origin_allowed(request):
            raise ConfigError("forbidden", "Cross-site requests are not allowed", 403)
        user_id = await require_admin(request)
        limit = min(limit, MAX_IMPORT_BODY)
        if request.content_length is not None and request.content_length > limit:
            raise ConfigError("too_large", "The request is too large", 413)
        raw = await request.content.read(limit + 1)
        while len(raw) <= limit and (more := await request.content.read(limit + 1 - len(raw))):
            raw += more
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

    async def export(_request: web.Request) -> web.Response:
        return _json(core.store.export(), headers={
            "Content-Disposition": 'attachment; filename="smart-predictive-climate-config.json"'})

    # ----------------------------------------------------------- changes
    async def pair(request: web.Request) -> web.Response:
        data, user = await body(request)
        if core.link is None:
            raise LinkError("not_ready", "Not started yet", 503)
        return _json(await core.link.pair(replace=bool(data.get("replace")), requested_by=user))

    async def unpair(request: web.Request) -> web.Response:
        _data, user = await body(request)
        if core.link is None:
            raise LinkError("not_ready", "Not started yet", 503)
        return _json(await core.link.unpair(requested_by=user))

    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/assets/{name}", asset)
    r.add_get("/api/health", health)
    r.add_get("/api/export", export)
    r.add_post("/api/companion/pair", pair)
    r.add_post("/api/companion/unpair", unpair)

    from . import api  # noqa: PLC0415 - the climate API; imported late so the frame has no import cycle with it

    api.add_routes(r, core, body=body, require_admin=require_admin, json=_json)
    return app
