"""The App's JSON API (registered by ``web.build_app``, which provides the security frame: Ingress only, CSRF
header, same origin, administrators only). Reads are GET; every change is a POST with the configuration revision
the page was loaded with."""

from __future__ import annotations

from typing import Any

from . import config as C, discovery
from .store import ConfigError


def add_routes(router: Any, core: Any, *, body: Any, require_admin: Any, json: Any) -> None:
    def get(path: str, handler: Any) -> None:
        async def wrapped(request: Any) -> Any:
            await require_admin(request)
            result = handler(request)
            if hasattr(result, "__await__"):
                result = await result
            return json(result)
        router.add_get(path, wrapped)

    def post(path: str, handler: Any) -> None:
        async def wrapped(request: Any) -> Any:
            data, user = await body(request)
            result = handler(data, user)
            if hasattr(result, "__await__"):
                result = await result
            return json(result)
        router.add_post(path, wrapped)

    def connected() -> Any:
        core._require_ha()  # noqa: SLF001
        return core.index()

    rev = "expected_revision"
    get("/api/overview", lambda r: {**core.overview(), "revision": core.store.revision, "connection": core.connection(),
                                    "recovery": core.store.recovery})
    get("/api/areas", lambda r: {"areas": connected().rooms(core.store.config["rooms"])})
    get("/api/areas/{area_id}", lambda r: _area(core, connected(), r.match_info["area_id"]))
    get("/api/entities", lambda r: {"results": connected().search(
        r.query.get("q", "")[:100], r.query.get("category") or None, r.query.get("area") or None)})
    get("/api/outdoor-candidates", lambda r: {"results": connected().outdoor_candidates()})
    get("/api/rooms/{area_id}", lambda r: core.room_view(r.match_info["area_id"]))
    get("/api/rooms/{area_id}/history", lambda r: core.room_history(r.match_info["area_id"],
                                                                    _float(r.query.get("hours"), 72.0)))
    get("/api/settings", lambda r: {"settings": core.store.config["settings"], "revision": core.store.revision,
                                    "backups": core.store.backups(), "recovery": core.store.recovery,
                                    "connection": core.connection(), "acquisition": core.acq.status,
                                    "coverage": core.acq.coverage(), "store": core.acq.store.stats(),
                                    "zones": core.store.config["zones"],
                                    "rooms": {a: r["name"] for a, r in core.store.config["rooms"].items()},
                                    "defaults": C.DEFAULT_SETTINGS, "strengths": list(C.STRENGTHS)})
    get("/api/diagnostics", lambda r: core.diagnostics())
    post("/api/rooms/save", lambda d, u: core.save_room(d.get("area_id"), d.get("room"), d.get(rev), u))
    post("/api/rooms/delete", lambda d, u: core.delete_room(d.get("area_id"), d.get(rev), u))
    post("/api/rooms/simulate", lambda d, u: core.simulate(d.get("area_id"), d.get("scenario")))
    post("/api/rooms/retrain", lambda d, u: core.retrain(d.get("area_id")))
    post("/api/zones/save", lambda d, u: core.save_zone(d.get("zone_id"), d.get("zone"), d.get(rev), u))
    post("/api/zones/delete", lambda d, u: core.delete_zone(d.get("zone_id"), d.get(rev), u))
    post("/api/settings/save", lambda d, u: core.save_settings(d.get("settings"), d.get(rev), u))
    post("/api/import/review", lambda d, u: core.review_import(d.get("document")))
    post("/api/import/apply", lambda d, u: core.apply_import(d.get("document"), d.get(rev), u))
    post("/api/backups/review", lambda d, u: core.review_import(core.store.backup_document(d.get("name"))))
    post("/api/backups/restore", lambda d, u: core.apply_import(core.store.backup_document(d.get("name")), d.get(rev), u,
                                                               "backup restored"))
    post("/api/windows/new-id", lambda d, u: {"id": C.new_window_id()})


def _area(core: Any, index: discovery.Index, area_id: str) -> dict[str, Any]:
    if area_id not in index.areas:
        raise ConfigError("unknown_area", "This area does not exist in Home Assistant", 404)
    return {"area_id": area_id, "name": index.areas[area_id], "devices": index.room_devices(area_id),
            "suggestions": index.suggest(area_id), "configured": core.store.config["rooms"].get(area_id),
            "categories": {k: v["label"] for k, v in discovery.CATEGORIES.items()},
            "outdoor": index.outdoor_candidates()[:20], "settings": core.store.config["settings"],
            "revision": core.store.revision}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
