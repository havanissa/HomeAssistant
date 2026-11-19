"""Presence (Phase A3.1): live occupancy, presence configuration, diagnostics and a debugging timeline.

The occupancy engine runs in the existing integration and stays there. This
service only reads it and edits its configuration:

* ``smart_occupancy/presence/get`` (integration 0.6.10) - every room's live
  engine view: state, evidence, deadlines, confidence, timing with its
  inheritance, diagnostics and the integration's bounded presence log;
* ``smart_occupancy/presence/review`` / ``set`` - reviewed, revision-checked
  changes of a room's sensors and timing overrides, or of the global defaults.
  The integration validates and stores them in its own configuration; the App
  keeps no copy (it stores a backup of the rooms section, now with the global
  defaults, before each change, like the Rooms editor);
* ``history/history_during_period`` - the Recorder's history of a room's state
  and reason entities and its sensors, for a bounded window only, on request
  (never polled), cached briefly.

The App adds what it knows from Home Assistant's registries: where a sensor
is, whether its device is offline, and how Home Assistant classifies it, as
*heuristic* hints. Nothing here sends a service call or changes a registry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
import re
import time
from typing import Any

from .ha import HomeAssistantClient, HomeAssistantError, utcnow_iso
from .rooms import CATEGORIES, KIND_CATEGORY, RoomsError, RoomsService, classify

_LOGGER = logging.getLogger(__name__)

AREA_ID = re.compile(r"^[\w\-]{1,255}$")
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
REVISION = re.compile(r"^[0-9a-f]{16}$")
IMPACT_CODE = re.compile(r"^[a-z_]{1,64}$")
TIMING_KEYS = ("correlation_window", "exit_confirmation", "inactivity_timeout", "dropout_grace",
               "trigger_stuck_after", "presence_stuck_after", "startup_hold")
SENSOR_KEYS = ("entity_id", "kind", "role", "inverted", "enabled")
#: History windows the timeline offers (seconds). Bounded: the Recorder is never asked for more.
WINDOWS = (900, 3600, 6 * 3600, 24 * 3600)
MAX_HISTORY_ENTITIES = 40
MAX_POINTS_PER_ENTITY = 2000
HISTORY_CACHE_SECONDS = 30.0
PRESENCE_CATEGORIES = {"pir", "presence", "door", "window", "pc_activity", "bed", "presence_input"}
OCCUPIED_STATES = {"confirmed_occupancy", "possible_vacancy"}
FILTERS = ("occupied", "possible", "vacant", "uncertain", "unavailable", "problems")


class PresenceService:
    """Presence views and changes through the integration's API (no engine, no device control)."""

    def __init__(self, ha: HomeAssistantClient, rooms: RoomsService) -> None:
        self.ha = ha
        self.rooms = rooms
        self.api_state = "unknown"  # unknown | ok | unsupported | unavailable | error
        self.api_error: str | None = None
        self._history_cache: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    # --------------------------------------------------------------- reading
    async def _get(self, area_id: str | None = None) -> dict[str, Any]:
        if self.rooms.where == "app":
            from .core_host import CoreApiError  # noqa: PLC0415

            try:
                model = await self.rooms.local.call("smart_occupancy/presence/get",
                                                    {"area_id": area_id} if area_id else {}, user_id=None, is_admin=True)
            except CoreApiError as err:
                self.api_state, self.api_error = "error", f"{err.code}: {err}"
                raise RoomsError(err.code, str(err), err.status) from err
            self.api_state, self.api_error = "ok", None
            return model if isinstance(model, dict) else {}
        try:
            model = await self.ha.command("smart_occupancy/presence/get", **({"area_id": area_id} if area_id else {}))
        except HomeAssistantError as err:
            if err.code in ("unknown_command", "not_found"):
                self.api_state, self.api_error = "unsupported", (
                    "The installed Smart Occupancy integration has no presence API (it needs version 0.6.10 or "
                    "newer). Update the integration to use the Presence pages.")
                raise RoomsError("unsupported", self.api_error, 503) from err
            self.api_state, self.api_error = "error", f"{err.code}: {err}"
            raise RoomsError(err.code, str(err).split(": ", 1)[-1], 502) from err
        except (ConnectionError, TimeoutError) as err:
            self.api_state, self.api_error = "unavailable", "Home Assistant is not connected"
            raise RoomsError("not_connected", "Home Assistant is not connected", 503) from err
        self.api_state, self.api_error = "ok", None
        return model if isinstance(model, dict) else {}

    def status(self) -> dict[str, Any]:
        components = set((self.ha.config or {}).get("components") or [])
        return {"api": self.api_state, "error": self.api_error,
                "recorder": "recorder" in components, "history": "history" in components}

    def _context(self) -> tuple:
        return self.rooms._context()  # noqa: SLF001 - same package

    def _annotate_signal(self, signal: dict[str, Any], area_id: str, ctx: tuple) -> dict[str, Any]:
        info = self.rooms.entity_info(signal["entity_id"], context=ctx)
        idx = self.rooms.index()
        reg = idx.registry.get(signal["entity_id"])
        device = idx.devices.get((reg or {}).get("device_id") or "")
        state = self.ha.states.get(signal["entity_id"])
        # How Home Assistant itself would classify it (without the user's choice), to spot a mismatch.
        category, confidence, reason = classify(signal["entity_id"], reg, state, device, relays=set(),
                                                switch_entities=set())
        return {
            **signal,
            "availability": info["availability"],
            "area_id_ha": info["area_id"],
            "area_ha": info["area"],
            "device": (device or {}).get("name_by_user") or (device or {}).get("name"),
            "device_model": " ".join(str(v) for v in ((device or {}).get("manufacturer"), (device or {}).get("model"))
                                     if v) or None,
            "device_id": info["device_id"],
            "device_class": info["device_class"] or signal.get("device_class"),
            "ha_category": category,
            "ha_category_label": CATEGORIES[category][0],
            "ha_category_confidence": confidence,
            "ha_category_reason": reason,
            "suggested_kind": info["suggested_kind"] if info["category"] != "other" else None,
            "other_rooms": [a for a in info["sensor_rooms"] if a != area_id],
            "last_updated": info["last_updated"],
        }

    def _app_diagnostics(self, room: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        area_id = room["area_id"]

        def add(code: str, level: str, basis: str, title: str, detail: str, action: str, **extra: Any) -> None:
            out.append({"code": f"app:{code}", "level": level, "basis": basis, "title": title, "detail": detail,
                        "action": action, "target": "sensors", "setting": None, "source": "app", **extra})

        for s in room.get("signals") or []:
            name = s.get("name") or s["entity_id"]
            if s.get("availability") == "device_offline":
                add("device_offline", "warning", "measured", f"{name}: device offline",
                    f"Every entity of {s.get('device') or 'its device'} is unavailable (unreachable or no power).",
                    "Check the device's power and its connection (Zigbee, Wi-Fi, …).", entity_id=s["entity_id"])
            if s.get("area_id_ha") and s["area_id_ha"] != area_id:
                add("outside_area", "info", "heuristic", f"{name} is in another area",
                    f"Home Assistant places it in {s.get('area_ha') or s['area_id_ha']}.",
                    "Fine for a shared sensor; otherwise check the assignment.", entity_id=s["entity_id"])
            expected = KIND_CATEGORY.get(s.get("kind") or "")
            ha_cat = s.get("ha_category")
            if expected and ha_cat and s.get("ha_category_confidence") == "high" and ha_cat != expected and not (
                    {expected, ha_cat} <= {"pir", "presence"}):
                add("classification_differs", "warning", "heuristic", f"{name}: type differs from Home Assistant",
                    f"Assigned as {s.get('kind_name') or s.get('kind')}, but Home Assistant reports it as "
                    f"{s.get('ha_category_label')} ({s.get('ha_category_reason')}).",
                    "Check the sensor type in the inspector.", entity_id=s["entity_id"])
        return out

    def _summary(self, room: dict[str, Any], ctx: tuple) -> dict[str, Any]:
        signals = [self._annotate_signal(s, room["area_id"], ctx) for s in room.get("signals") or []]
        room = {**room, "signals": signals}
        diags = (room.get("diagnostics") or []) + self._app_diagnostics(room)
        occ = room.get("occupancy") or {}
        state = occ.get("state")
        unavailable = [s for s in signals if s.get("condition") in ("unavailable", "invalid")
                       or s.get("availability") in ("unavailable", "device_offline", "missing", "not_provided")]
        sustained = [s["deadlines"].get("sustained_at") for s in signals
                     if s.get("status") == "active" and (s.get("deadlines") or {}).get("sustained_at")]
        tags = []
        if state in OCCUPIED_STATES:
            tags.append("occupied")
        if state == "possible_occupancy":
            tags.append("possible")
        if state == "vacant":
            tags.append("vacant")
        if state == "uncertain" or (room.get("enabled") and not occ):
            tags.append("uncertain")
        if unavailable:
            tags.append("unavailable")
        if any(d["level"] in ("error", "warning") for d in diags):
            tags.append("problems")
        idx = self.rooms.index()
        area = idx.areas.get(room["area_id"]) or {}
        return {
            **room,
            "name": area.get("name") or room.get("name") or room["area_id"],
            "icon": area.get("icon"),
            "floor_id": area.get("floor_id"),
            "area_exists": bool(area),
            "diagnostics": diags,
            "tags": tags,
            "counts": {
                "sensors": len(signals),
                "active": sum(1 for s in signals if s.get("condition") == "active"),
                "unavailable": len(unavailable),
                "excluded": sum(1 for s in signals if s.get("enabled") is False),
                "errors": sum(1 for d in diags if d["level"] == "error"),
                "warnings": sum(1 for d in diags if d["level"] == "warning"),
                "info": sum(1 for d in diags if d["level"] == "info"),
                "overrides": len(room.get("timing_overrides") or {}),
            },
            "confirm_at": min(sustained) if sustained and state == "possible_occupancy" else None,
        }

    def _common(self, model: dict[str, Any]) -> dict[str, Any]:
        return {k: model.get(k) for k in ("api_version", "integration_version", "now", "loaded", "writable", "state",
                                           "operating_mode", "global", "timing_schema", "kinds", "roles", "states",
                                           "transitions", "state_values", "schema")}

    async def overview(self) -> dict[str, Any]:
        await self.rooms.ensure()
        model = await self._get()
        ctx = self._context()
        rooms = [self._summary(r, ctx) for r in (model.get("rooms") or {}).values()]
        rooms.sort(key=lambda r: str(r["name"]).casefold())
        idx = self.rooms.index()
        configured = {r["area_id"] for r in rooms}
        unconfigured = sorted(({"area_id": a, "name": v.get("name") or a, "floor_id": v.get("floor_id")}
                               for a, v in idx.areas.items() if a not in configured),
                              key=lambda x: str(x["name"]).casefold())
        counts = {f: sum(1 for r in rooms if f in r["tags"]) for f in FILTERS}
        return {**self._common(model), "status": self.status(), "rooms": rooms, "unconfigured": unconfigured,
                "counts": counts, "log": model.get("log") or [], "log_seq": model.get("log_seq"),
                "generated_at": utcnow_iso()}

    def _candidates(self, area_id: str, assigned: set[str], ctx: tuple) -> list[dict[str, Any]]:
        """Presence-capable entities in this area that are not assigned to this room (most likely first)."""
        idx = self.rooms.index()
        out = []
        for entity_id, reg in idx.registry.items():
            if entity_id in assigned or not ENTITY_ID.match(entity_id):
                continue
            device = idx.devices.get(reg.get("device_id") or "")
            if (reg.get("area_id") or (device or {}).get("area_id")) != area_id:
                continue
            info = self.rooms.entity_info(entity_id, context=ctx)
            if info["category"] not in PRESENCE_CATEGORIES or info["own"] or info["hidden"] or \
                    info["entity_category"] is not None or info["availability"] == "disabled":
                continue
            out.append({k: info[k] for k in ("entity_id", "name", "domain", "category", "category_label",
                                              "category_confidence", "category_reason", "suggested_kind",
                                              "device_class", "state", "availability", "last_changed", "device_id",
                                              "area_id", "area", "sensor_rooms")})
        rank = {"pir": 0, "presence": 0, "door": 1, "pc_activity": 1, "bed": 1, "window": 2, "presence_input": 3}
        out.sort(key=lambda c: (rank.get(c["category"], 9), str(c["name"]).casefold()))
        return out

    async def workspace(self, area_id: str) -> dict[str, Any]:
        if not AREA_ID.match(area_id):
            raise RoomsError("invalid_area", "Invalid area id")
        await self.rooms.ensure()
        model = await self._get(area_id)
        room = (model.get("rooms") or {}).get(area_id)
        idx = self.rooms.index()
        if room is None:
            if area_id not in idx.areas:
                raise RoomsError("unknown_area", "This area does not exist in Home Assistant", 404)
            raise RoomsError("not_configured", "This room is not configured yet. Set it up in the Rooms editor first.",
                             404)
        ctx = self._context()
        summary = self._summary(room, ctx)
        assigned = {s["entity_id"] for s in summary["signals"]}
        others = [{"area_id": a, "name": (v.get("name") or a)} for a, v in idx.areas.items() if a != area_id]
        # Fast room switching: the configured rooms with their current state (from the Rooms API's model).
        switcher = []
        for a, r in (self.rooms.model or {}).get("rooms", {}).items():
            occ = r.get("occupancy") or {}
            switcher.append({"area_id": a, "name": (idx.areas.get(a) or {}).get("name") or a,
                             "enabled": bool((r.get("config") or {}).get("enabled")), "state": occ.get("state")})
        switcher.sort(key=lambda x: str(x["name"]).casefold())
        return {
            **self._common(model),
            "status": self.status(),
            "room": summary,
            "candidates": self._candidates(area_id, assigned, ctx),
            "areas": sorted(others, key=lambda x: str(x["name"]).casefold()),
            "rooms": switcher,
            "log": model.get("log") or [],
            "log_seq": model.get("log_seq"),
            "generated_at": utcnow_iso(),
        }

    async def diagnostics(self) -> dict[str, Any]:
        overview = await self.overview()
        items = []
        for room in overview["rooms"]:
            for d in room["diagnostics"]:
                items.append({**d, "area_id": room["area_id"], "room": room["name"]})
        status = overview["status"]
        deps = []
        if not overview.get("loaded"):
            deps.append({"code": "integration_not_running", "level": "error", "basis": "measured",
                         "title": "Smart Occupancy is not running", "target": "room", "area_id": None,
                         "detail": "No occupancy engine runs, so no room is observed and nothing can be saved.",
                         "action": "Check the integration in Settings → Devices & services."})
        if not status["recorder"]:
            deps.append({"code": "recorder_missing", "level": "info", "basis": "measured", "target": "room",
                         "title": "Recorder not available", "area_id": None,
                         "detail": "Without the Recorder, the timeline shows only what happened while the App or the "
                                   "integration was running.", "action": "Enable the Recorder to keep history."})
        level = {"error": 0, "warning": 1, "info": 2}
        basis = {"measured": 0, "integration": 1, "heuristic": 2}
        items.sort(key=lambda d: (level.get(d["level"], 3), basis.get(d["basis"], 3), str(d.get("room")).casefold()))
        return {"items": deps + items, "rooms": [{k: r[k] for k in ("area_id", "name", "tags", "counts")}
                                                 for r in overview["rooms"]],
                "now": overview.get("now"), "generated_at": utcnow_iso()}

    # -------------------------------------------------------------- timeline
    async def timeline(self, area_id: str, window: Any) -> dict[str, Any]:
        """Recorder history of a room (bounded window, on request) plus the integration's presence log."""
        if not AREA_ID.match(area_id):
            raise RoomsError("invalid_area", "Invalid area id")
        try:
            seconds = int(window)
        except (TypeError, ValueError):
            seconds = 3600
        if seconds not in WINDOWS:
            raise RoomsError("invalid_window", f"The window must be one of {', '.join(map(str, WINDOWS))} seconds")
        model = await self._get(area_id)
        room = (model.get("rooms") or {}).get(area_id)
        if room is None:
            raise RoomsError("not_configured", "This room is not configured", 404)
        entities = room.get("entities") or {}
        sensor_ids = [s["entity_id"] for s in room.get("sensors") or []]
        ids = [e for e in (entities.get("state"), entities.get("reason")) if e] + sensor_ids
        ids = list(dict.fromkeys(ids))[:MAX_HISTORY_ENTITIES]
        now = datetime.now(UTC)
        start = now - timedelta(seconds=seconds)
        history: dict[str, Any] = {"available": False, "error": None, "series": {}, "truncated": []}
        key = (area_id, seconds)
        cached = self._history_cache.get(key)
        if cached and time.monotonic() - cached[0] < HISTORY_CACHE_SECONDS and cached[1].get("ids") == ids:
            history = cached[1]["history"]
        elif not self.status()["history"]:
            history["error"] = "The Recorder history is not available in this Home Assistant"
        else:
            async with self._lock:
                try:
                    raw = await self.ha.command(
                        "history/history_during_period", start_time=start.isoformat(), entity_ids=ids,
                        include_start_time_state=True, significant_changes_only=False, minimal_response=True,
                        no_attributes=True)
                except HomeAssistantError as err:
                    history["error"] = f"The Recorder did not answer ({err.code})"
                except (ConnectionError, TimeoutError) as err:
                    raise RoomsError("not_connected", "Home Assistant is not connected", 503) from err
                else:
                    history = {"available": True, "error": None, **parse_history(raw, ids)}
                    self._history_cache[key] = (time.monotonic(), {"ids": ids, "history": history})
            for k in [k for k, v in self._history_cache.items() if time.monotonic() - v[0] > HISTORY_CACHE_SECONDS]:
                self._history_cache.pop(k, None)
        names = {s["entity_id"]: s.get("name") for s in room.get("signals") or []}
        return {
            "area_id": area_id,
            "window": seconds,
            "windows": list(WINDOWS),
            "start": start.isoformat(timespec="seconds"),
            "end": now.isoformat(timespec="seconds"),
            "now": model.get("now"),
            "entities": entities,
            "sensors": [{**s, "name": names.get(s["entity_id"]) or s["entity_id"]} for s in room.get("sensors") or []],
            "state_values": model.get("state_values"),
            "history": history,
            "log": [e for e in model.get("log") or [] if e.get("time", "") >= start.isoformat()[:19]],
            "log_seq": model.get("log_seq"),
            "time_zone": (self.ha.config or {}).get("time_zone"),
        }

    # --------------------------------------------------------------- changes
    @staticmethod
    def clean_change(change: Any) -> dict[str, Any]:
        """Only the fields the integration accepts; it validates the values."""
        if not isinstance(change, dict):
            raise RoomsError("invalid_change", "The change must be an object")
        out: dict[str, Any] = {}
        if "sensors" in change and change["sensors"] is not None:
            sensors = change["sensors"]
            if not isinstance(sensors, list) or len(sensors) > 100:
                raise RoomsError("invalid_change", "Invalid sensor list")
            out["sensors"] = [{k: s[k] for k in SENSOR_KEYS if k in s} if isinstance(s, dict) else s for s in sensors]
        if "timing" in change and change["timing"] is not None:
            timing = change["timing"]
            if not isinstance(timing, dict) or not all(k in TIMING_KEYS for k in timing):
                raise RoomsError("invalid_change", "Unknown timing setting")
            out["timing"] = dict(timing)
        if change.get("reset_timing"):
            out["reset_timing"] = True
        return out

    @staticmethod
    def _check(area_id: Any, revision: Any = None, confirmed: Any = None) -> list[str]:
        if area_id is not None and (not isinstance(area_id, str) or not AREA_ID.match(area_id)):
            raise RoomsError("invalid_area", "Invalid area id")
        if revision is not None and (not isinstance(revision, str) or not REVISION.match(revision)):
            raise RoomsError("invalid_revision", "Invalid revision")
        if confirmed is None:
            return []
        if not isinstance(confirmed, list) or len(confirmed) > 50 or not all(
                isinstance(c, str) and IMPACT_CODE.match(c) for c in confirmed):
            raise RoomsError("invalid_confirmation", "Invalid confirmation list")
        return list(confirmed)

    async def review(self, area_id: Any, change: Any, revision: Any) -> dict[str, Any]:
        self._check(area_id, revision)
        cleaned = self.clean_change(change)
        result = await self.rooms._call("smart_occupancy/presence/review", area_id=area_id, change=cleaned,  # noqa: SLF001
                                        expected_revision=revision)
        if area_id and cleaned.get("sensors") is not None:
            annotated = self.rooms._annotate(result, area_id, {"sensors": cleaned["sensors"]})  # noqa: SLF001
            result = {**result, "conflicts": annotated.get("conflicts", []), "attention": annotated.get("attention", [])}
        return result

    async def save(self, area_id: Any, change: Any, revision: Any, confirmed: Any) -> dict[str, Any]:
        confirmed = self._check(area_id, revision, confirmed)
        if not isinstance(revision, str):
            raise RoomsError("invalid_revision", "A revision is required to save")
        cleaned = self.clean_change(change)
        await self.rooms.refresh()
        self.rooms._require_api()  # noqa: SLF001
        where = "the global presence defaults" if area_id is None else self.rooms._area_name(area_id)  # noqa: SLF001
        backup = self.rooms._write_backup(f"Before a presence change ({where})")  # noqa: SLF001
        try:
            result = await self.rooms._call("smart_occupancy/presence/set", write=True, area_id=area_id,  # noqa: SLF001
                                            change=cleaned, expected_revision=revision, confirmed=confirmed)
        except RoomsError as err:
            if err.code != "connection_lost":
                self.rooms._delete_backup(backup)  # noqa: SLF001
            raise
        _LOGGER.info("Presence settings saved through the integration (%s, %s, %d changes)", area_id or "global",
                     result.get("applied"), len(result.get("changes") or []))
        await self.rooms._settle(result.get("applied"))  # noqa: SLF001
        self._history_cache.clear()
        return {**result, "backup": backup}


# ------------------------------------------------------------------ helpers
def _ts(value: Any) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), UTC).isoformat(timespec="milliseconds")
    if isinstance(value, str):
        return value
    return None


def parse_history(raw: Any, ids: list[str]) -> dict[str, Any]:
    """The Recorder's compressed history (``s``, ``lc``/``lu``) as ``{entity_id: [{t, s}]}``, bounded per entity."""
    series: dict[str, list[dict[str, Any]]] = {}
    truncated: list[str] = []
    data = raw if isinstance(raw, dict) else {}
    for entity_id in ids:
        points = []
        for item in data.get(entity_id) or []:
            if not isinstance(item, dict):
                continue
            value = item.get("s", item.get("state"))
            t = _ts(item.get("lc", item.get("lu", item.get("last_changed"))))
            if t is None or value is None:
                continue
            if points and points[-1]["s"] == value:
                continue  # an attribute-only update: not a change of state
            points.append({"t": t, "s": str(value)[:255]})
        if len(points) > MAX_POINTS_PER_ENTITY:
            truncated.append(entity_id)
            points = points[-MAX_POINTS_PER_ENTITY:]
        series[entity_id] = points
    return {"series": series, "truncated": truncated}
