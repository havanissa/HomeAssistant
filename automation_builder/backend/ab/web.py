"""HTTP server: the web interface (Overview, Devices, Rule Editor, Settings) and its JSON API, through Ingress only.

* Requests are accepted only from the Supervisor's Ingress address.
* Every changing request is a POST with a JSON body that needs the header
  ``X-AB: 1``, a same-origin ``Origin`` (when the browser sends one) and a Home
  Assistant administrator (the Supervisor passes the user id in
  ``X-Remote-User-Id``; the browser cannot set it).
* Every change of the configuration carries the revision the page was loaded
  with (``expected_revision``); a stale revision is refused.
* A deployment additionally carries the hash of the plan the user looked at.

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

from .const import APP_NAME, APP_VERSION
from .core import AppCore
from .discovery import SEARCH_PURPOSES
from .model import ConfigError
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

CORE_KEY = web.AppKey("core", AppCore)
CSRF_HEADER = "X-AB"
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


def build_app(settings: Settings, core: AppCore) -> web.Application:
    @web.middleware
    async def access(request: web.Request, handler: Any) -> web.StreamResponse:
        if not settings.is_allowed(request.remote):
            _LOGGER.warning("Refused a request from %s (only Home Assistant Ingress is allowed)", request.remote)
            return web.Response(status=403, text="Forbidden: open this App from the Home Assistant sidebar.")
        try:
            response = await handler(request)
        except ConfigError as err:
            response = _json({"error": err.code, "message": str(err), "problems": err.problems, **err.extra},
                             err.status)
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

    async def is_admin(request: web.Request) -> bool:
        user_id = request.headers.get("X-Remote-User-Id")
        if settings.dev_mode and not user_id:
            return True
        return await core.is_admin(user_id)

    async def body(request: web.Request, limit: int = MAX_BODY) -> tuple[dict[str, Any], str | None]:
        """CSRF and administrator checks for every POST; returns (JSON body, user id)."""
        if request.headers.get(CSRF_HEADER) != "1" or request.content_type != "application/json":
            raise ConfigError("forbidden", f"This request must come from the {APP_NAME} interface", 403)
        if not origin_allowed(request):
            raise ConfigError("forbidden", "Cross-site requests are not allowed", 403)
        try:
            admin = await is_admin(request)
        except Exception as err:  # noqa: BLE001 - fail closed
            raise ConfigError("forbidden", f"Could not verify the Home Assistant user ({type(err).__name__})",
                              403) from err
        if not admin:
            _LOGGER.warning("Refused a change from a user who is not an administrator")
            raise ConfigError("forbidden", "Only Home Assistant administrators can use Automation Builder", 403)
        if request.content_length is not None and request.content_length > limit:
            raise ConfigError("too_large", "The request is too large", 413)
        # read() of the stream returns what has arrived so far: read until the end (a large import arrives in parts)
        raw = bytearray()
        while len(raw) <= limit:
            chunk = await request.content.read(limit + 1 - len(raw))
            if not chunk:
                break
            raw += chunk
        raw = bytes(raw)
        if len(raw) > limit:
            raise ConfigError("too_large", "The request is too large", 413)
        try:
            data = json.loads(raw or b"{}")
        except ValueError as err:
            raise ConfigError("invalid", "The request body is not JSON") from err
        if not isinstance(data, dict):
            raise ConfigError("invalid", "The request body must be an object")
        return data, request.headers.get("X-Remote-User-Id")

    async def require_admin(request: web.Request) -> None:
        """Reads that reveal the home's configuration are for administrators too."""
        try:
            admin = await is_admin(request)
        except Exception:  # noqa: BLE001 - fail closed
            admin = False
        if not admin:
            raise ConfigError("forbidden", "Only Home Assistant administrators can use Automation Builder", 403)

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
        try:
            admin = await is_admin(request)
        except Exception:  # noqa: BLE001
            admin = False
        base = {"app": {"name": APP_NAME, "version": APP_VERSION}, "user": {"is_admin": bool(admin)},
                "connection": core.connection(), "revision": core.store.revision, "recovery": core.store.recovery,
                "deploy_problem": core.deploys.problem}
        if not admin:
            return _json({**base, "devices": [], "orphans": [], "totals": None})
        return _json({**base, **await core.overview()})

    async def device(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json({**await core.device_view(request.match_info["device_id"]), "revision": core.store.revision,
                      "connection": core.connection()})

    async def catalog(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.catalog())

    async def entities(request: web.Request) -> web.Response:
        await require_admin(request)
        purpose = request.query.get("purpose", "any")
        core._require_ha()  # noqa: SLF001
        index = core.index()
        area = request.query.get("area") or None
        keep = request.query.get("keep") or None
        return _json({"results": index.search(request.query.get("q", "")[:100],
                                              purpose if purpose in SEARCH_PURPOSES else "any",
                                              area=area if area in index.areas else None, keep=keep)})

    async def rooms(request: web.Request) -> web.Response:
        await require_admin(request)
        core._require_ha()  # noqa: SLF001
        return _json({"rooms": core.index().rooms(request.query.get("q", "")[:100])})

    async def room_devices(request: web.Request) -> web.Response:
        await require_admin(request)
        core._require_ha()  # noqa: SLF001
        index = core.index()
        area = request.query.get("area") or None
        if area is not None and area not in index.areas:
            raise ConfigError("unknown_room", "This room does not exist in Home Assistant (anymore).", 404)
        return _json({"area_id": area, "name": index.areas.get(area, "Unassigned"),
                      "devices": index.room_devices(area, request.query.get("q", "")[:100])})

    async def entity(request: web.Request) -> web.Response:
        await require_admin(request)
        ids = [e for e in request.query.get("ids", "").split(",") if e][:60]
        index = core.index()
        return _json({"entities": {e: index.view(e, detail=True) for e in ids}})

    async def ha_devices(request: web.Request) -> web.Response:
        await require_admin(request)
        core._require_ha()  # noqa: SLF001
        return _json({"devices": core.index().physical_devices(request.query.get("q", "")[:100])})

    async def all_devices(request: web.Request) -> web.Response:
        await require_admin(request)
        core._require_ha()  # noqa: SLF001
        return _json({"devices": core.index().all_devices(request.query.get("q", "")[:100])})

    async def device_automations(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.device_automations(request.query.get("device_id"), request.query.get("kind")))

    async def notify_targets(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.notify_targets())

    async def blueprint_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.blueprint_view(request.match_info["device_id"]))

    async def blueprint_download(request: web.Request) -> web.Response:
        await require_admin(request)
        view = await core.blueprint_view(request.match_info["device_id"])
        if not view["yaml"]:
            raise ConfigError("blocked", "There is no blueprint to download; see the validation results.", 409)
        return web.Response(text=view["yaml"], content_type="application/yaml", headers={
            "Content-Disposition": f'attachment; filename="{view["filename"]}"'})

    async def designer_page(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.designer_page(request.match_info["device_id"], request.match_info["designer_id"]))

    async def simulation_inputs(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.simulation_inputs(request.match_info["device_id"]))

    async def switch_sources(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.switch_sources(request.query.get("area"), request.query.get("q", "")))

    async def switch_interactions(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.switch_interactions(request.query.get("device_id"), request.query.get("entity_id")))

    async def presence_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.presence_view())

    async def levels_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.levels_view())

    async def level_sensors(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.level_sensors(request.query.get("area"), request.query.get("all") == "1", request.query.get("q", "")))

    async def heating_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.heating_view())

    async def gallery_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.gallery_view(request.match_info["device_id"]))

    async def sun_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.sun_view())

    async def import_list(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json({**core.import_list(), "revision": core.store.revision})

    async def settings_view(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.settings_view())

    async def export(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(core.store.export(), headers={
            "Content-Disposition": 'attachment; filename="automation-builder-config.json"'})

    async def diagnostics(request: web.Request) -> web.Response:
        await require_admin(request)
        return _json(await core.diagnostics(), headers={
            "Content-Disposition": 'attachment; filename="automation-builder-diagnostics.json"'})

    # ----------------------------------------------------------- changes
    def post(handler: Any, limit: int = MAX_BODY) -> Any:
        async def wrapped(request: web.Request) -> web.Response:
            data, user = await body(request, limit)
            result = handler(data, user)
            if hasattr(result, "__await__"):
                result = await result
            return _json(result)
        return wrapped

    rev = "expected_revision"
    routes = {
        "/api/devices/create": lambda d, u: core.create_device(d, d.get(rev), u),
        "/api/devices/update": lambda d, u: core.update_device(d.get("device_id"), d.get("patch"), d.get(rev), u),
        "/api/devices/delete": lambda d, u: core.delete_device(d.get("device_id"), d.get(rev),
                                                               d.get("overwrite") is True, u),
        "/api/devices/restore": lambda d, u: core.restore_device(d.get("device_id"), d.get("version"), d.get(rev), u),
        "/api/devices/plan": lambda d, u: core.plan(d.get("device_id")),
        "/api/devices/deploy": lambda d, u: core.deploy(d.get("device_id"), d.get(rev), d.get("plan_hash"),
                                                        d.get("overwrite") is True, u),
        "/api/rules/preview": lambda d, u: core.preview_rule(d.get("device_id"), d.get("rule")),
        "/api/default/preview": lambda d, u: core.preview_default(d.get("device_id"), d.get("default_rule")),
        "/api/rules/save": lambda d, u: core.save_rule(d.get("device_id"), d.get("rule"), d.get(rev), u),
        "/api/rules/delete": lambda d, u: core.delete_rule(d.get("device_id"), d.get("rule_id"), d.get(rev), u),
        "/api/rules/move": lambda d, u: core.move_rule(d.get("device_id"), d.get("rule_id"), d.get("direction"),
                                                       d.get(rev), u),
        "/api/settings/save": lambda d, u: core.save_settings(d.get("settings"), d.get(rev), u),
        "/api/backups/review": lambda d, u: core.review_backup(d.get("name")),
        "/api/backups/restore": lambda d, u: core.restore_backup(d.get("name"), d.get(rev), u),
        "/api/orphans/remove": lambda d, u: core.remove_orphan(d.get("automation_id"), d.get("overwrite") is True),
        "/api/blueprint/preview": lambda d, u: core.blueprint_view(d.get("device_id"), d.get("settings") or {}),
        "/api/blueprint/install": lambda d, u: core.blueprint_install(d.get("device_id"), d.get("overwrite") is True, u),
        "/api/import/analyse": lambda d, u: core.import_analyse(d.get("entity_id")),
        "/api/designers/create": lambda d, u: core.create_designer(d.get("device_id"), d.get("kind"), d.get("template"), d.get(rev), u),
        "/api/designers/save": lambda d, u: core.save_designer(d.get("device_id"), d.get("designer"), d.get(rev), u),
        "/api/designers/delete": lambda d, u: core.delete_designer(d.get("device_id"), d.get("designer_id"), d.get(rev), u),
        "/api/designers/preview": lambda d, u: core.designer_page(d.get("device_id"), (d.get("designer") or {}).get("id"), d.get("designer")),
        "/api/designers/simulate": lambda d, u: core.simulation(d.get("device_id"), d.get("scenario"), d.get("designer")),
        "/api/designers/convert": lambda d, u: core.convert_designer(d.get("device_id"), d.get("designer_id"),
                                                                     d.get("apply") is True, d.get(rev), u),
        "/api/designers/adopt": lambda d, u: core.adopt_rule(d.get("device_id"), d.get("designer_id"), d.get("rule_id"),
                                                             d.get(rev), u),
        "/api/gallery/apply": lambda d, u: core.apply_template(d.get("device_id"), d.get("template"), d.get(rev), u),
        "/api/presence/save": lambda d, u: core.save_source(d.get("source"), d.get(rev), u),
        "/api/presence/delete": lambda d, u: core.delete_source(d.get("source_id"), d.get(rev), u),
        "/api/import/convert": lambda d, u: core.import_convert(d.get("entity_id"), d.get("destination"), d.get(rev), u),
        "/api/light-levels/template/save": lambda d, u: core.save_level_template(d.get("template"), d.get(rev), u),
        "/api/light-levels/template/preset": lambda d, u: core.add_level_preset(d.get(rev), u),
        "/api/light-levels/template/duplicate": lambda d, u: core.duplicate_level_template(d.get("template_id"), d.get(rev), u),
        "/api/light-levels/template/delete": lambda d, u: core.delete_level_template(d.get("template_id"), d.get(rev), u),
        "/api/light-levels/matrix/save": lambda d, u: core.save_level_matrix(d.get("matrix"), d.get(rev), u),
        "/api/light-levels/matrix/delete": lambda d, u: core.delete_level_matrix(d.get("matrix_id"), d.get(rev), u),
        "/api/light-levels/defaults": lambda d, u: core.save_level_defaults(d.get("defaults"), d.get(rev), u),
        "/api/light-levels/room/save": lambda d, u: core.save_level_room(d.get("room"), d.get(rev), u),
        "/api/light-levels/room/delete": lambda d, u: core.delete_level_room(d.get("area_id"), d.get(rev), d.get("overwrite") is True, u),
        "/api/light-levels/observations/clear": lambda d, u: core.clear_observations(d.get("area_id"), d.get("light")),
        "/api/heating/template/save": lambda d, u: core.save_heating_template(d.get("template"), d.get(rev), u),
        "/api/heating/template/delete": lambda d, u: core.delete_heating_template(d.get("template_id"), d.get(rev), u),
        "/api/heating/room/save": lambda d, u: core.save_heating_room(d.get("room"), d.get(rev), u),
        "/api/heating/room/delete": lambda d, u: core.delete_heating_room(d.get("area_id"), d.get(rev), u),
        "/api/heating/quick": lambda d, u: core.heating_quick(d.get("device_id"), d.get("designer_id"), d.get("kind"), d.get(rev), u),
    }
    large = {
        "/api/import/review": lambda d, u: core.review_import(d.get("document")),
        "/api/import/apply": lambda d, u: core.apply_import(d.get("document"), d.get(rev), u),
    }

    r = app.router
    r.add_get("/", index)
    r.add_get("/index.html", index)
    r.add_get("/assets/{name}", asset)
    r.add_get("/api/health", health)
    r.add_get("/api/overview", overview)
    r.add_get("/api/devices/{device_id}", device)
    r.add_get("/api/catalog", catalog)
    r.add_get("/api/entities", entities)
    r.add_get("/api/entity", entity)
    r.add_get("/api/ha-devices", ha_devices)
    r.add_get("/api/rooms", rooms)
    r.add_get("/api/room-devices", room_devices)
    r.add_get("/api/all-devices", all_devices)
    r.add_get("/api/device-automations", device_automations)
    r.add_get("/api/notify-targets", notify_targets)
    r.add_get("/api/blueprint/{device_id}", blueprint_view)
    r.add_get("/api/blueprint/{device_id}/download", blueprint_download)
    r.add_get("/api/automations", import_list)
    r.add_get("/api/designers/{device_id}/{designer_id}", designer_page)
    r.add_get("/api/simulation/{device_id}", simulation_inputs)
    r.add_get("/api/presence", presence_view)
    r.add_get("/api/switch-sources", switch_sources)
    r.add_get("/api/switch-interactions", switch_interactions)
    r.add_get("/api/gallery/{device_id}", gallery_view)
    r.add_get("/api/light-levels", levels_view)
    r.add_get("/api/light-levels/sensors", level_sensors)
    r.add_get("/api/heating", heating_view)
    r.add_get("/api/sun", sun_view)
    r.add_get("/api/settings", settings_view)
    r.add_get("/api/export", export)
    r.add_get("/api/diagnostics", diagnostics)
    for path, handler in routes.items():
        r.add_post(path, post(handler))
    for path, handler in large.items():
        r.add_post(path, post(handler, MAX_IMPORT_BODY))
    return app
