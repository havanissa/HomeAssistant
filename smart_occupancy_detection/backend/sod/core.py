"""The App's core: the Home Assistant client, the configuration, the occupancy runtime and the companion link.

It also holds the services behind the web API. Every change goes through the
same path: build the complete new configuration, validate it, show what changes
(and what the rooms would show right afterwards), then save it with the
revision the user reviewed.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any

import aiohttp

from sod_engine import DERIVED_FROM, ENGINE_VERSION

from . import config as C, discovery, smart_comfort_import
from .const import APP_NAME, APP_VERSION
from .ha import HomeAssistantClient, utcnow_iso
from .logbuffer import BufferHandler
from .publisher import CompanionLink
from .runtime import OccupancyRuntime
from .settings import Settings

_LOGGER = logging.getLogger(__name__)


class AppCore:
    def __init__(self, settings: Settings, buffer: BufferHandler | None = None) -> None:
        self.settings = settings
        self.buffer = buffer
        self.store = C.ConfigStore(settings.data_dir)
        self.store.load()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.runtime: OccupancyRuntime | None = None
        self.link: CompanionLink | None = None
        self._admins: tuple[float, dict[str, bool]] | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.runtime = OccupancyRuntime(self.ha, self.store, self.settings.data_dir)
        self.link = CompanionLink(self.ha, self.runtime.published_rooms, self.settings.data_dir,
                                  app_version=APP_VERSION, can_publish=self._can_publish)
        self.runtime.add_listener(lambda _areas: self.link.schedule_publish() if self.link else None)
        await self.runtime.start()
        await self.link.start()
        await self.ha.start()
        _LOGGER.info("%s %s started (engine %s, self-test %s)", APP_NAME, APP_VERSION, ENGINE_VERSION,
                     "passed" if self.runtime.self_test["passed"] else "FAILED")

    async def stop(self) -> None:
        if self.link:
            await self.link.stop()
        if self.runtime:
            await self.runtime.stop()
        if self.ha:
            await self.ha.stop()
        if self.session:
            await self.session.close()

    def _can_publish(self) -> str | None:
        if self.runtime and not self.runtime.self_test["passed"]:
            return "The engine self-test failed: nothing is published (see Settings -> Diagnostics)."
        if self.runtime and self.runtime.house is None:
            return "Waiting for Home Assistant's sensor states."
        return None

    # ----------------------------------------------------------------- users
    async def is_admin(self, user_id: str | None) -> bool:
        """Is this Home Assistant user an active administrator (Ingress passes the user id)?"""
        if not user_id or self.ha is None:
            return False
        now = asyncio.get_running_loop().time()
        if self._admins is None or now - self._admins[0] > 60 or user_id not in self._admins[1]:
            users = await self.ha.command("config/auth/list") or []
            self._admins = (now, {
                u.get("id"): bool(u.get("is_active", True)) and not u.get("system_generated")
                and (bool(u.get("is_owner")) or "system-admin" in (u.get("group_ids") or []))
                for u in users if isinstance(u, dict)
            })
        return bool(self._admins[1].get(user_id))

    # ---------------------------------------------------------------- reading
    def _require_ha(self) -> HomeAssistantClient:
        if self.ha is None or not self.ha.connected:
            raise C.ConfigError("not_connected", "Home Assistant is not connected; try again in a moment.", 503)
        return self.ha

    def areas(self) -> list[dict[str, Any]]:
        ha = self.ha
        rooms = self.store.config.get("rooms") or {}
        out = [{"area_id": a.get("area_id"), "name": a.get("name") or a.get("area_id"),
                "configured": a.get("area_id") in rooms} for a in (ha.areas if ha else [])]
        known = {a["area_id"] for a in out}
        for area_id, room in rooms.items():
            if area_id not in known:
                out.append({"area_id": area_id, "name": room.get("name") or area_id, "configured": True,
                            "missing": True})
        return sorted(out, key=lambda a: str(a["name"]).casefold())

    def discover(self, area_id: str) -> list[dict[str, Any]]:
        ha = self._require_ha()
        return discovery.discover(area_id, ha.states, ha.entity_registry, ha.devices, ha.areas)

    def search(self, query: str, activity: bool) -> list[dict[str, Any]]:
        ha = self._require_ha()
        return discovery.search(query[:100], ha.states, ha.entity_registry, ha.devices, ha.areas, activity=activity)

    def _entity_exists(self, entity_id: str) -> bool:
        ha = self.ha
        if not ha:
            return False
        if entity_id in ha.states:
            return True
        known = {e.get("entity_id") for e in ha.entity_registry}
        return entity_id in known

    def _area_exists(self, area_id: str) -> bool:
        return bool(self.ha) and any(a.get("area_id") == area_id for a in self.ha.areas)

    def _area_name(self, area_id: str) -> str | None:
        area = next((a for a in (self.ha.areas if self.ha else []) if a.get("area_id") == area_id), None)
        return str(area.get("name"))[:C.MAX_NAME] if area and area.get("name") else None

    def defaults(self) -> dict[str, Any]:
        return {
            "timing": {k: {"default": v[0], "min": v[1], "max": v[2], **C.TIMING_INFO[k],
                           "basic": k in C.BASIC_TIMING} for k, v in C.TIMING_PARAMETERS.items()},
            "kinds": [{"value": k, "label": C.KIND_LABELS[k], "domains": list(d) if d else None,
                       "default_role": C.DEFAULT_ROLE_FOR_KIND[C.SensorKind(k)].value} for k, d in C.KIND_DOMAINS.items()],
            "roles": [{"value": r, "label": label} for r, label in C.ROLE_LABELS.items()],
        }

    # ---------------------------------------------------------------- reviews
    def _extra_problems(self, config: dict[str, Any], changed: set[str]) -> list[dict[str, str]]:
        """Checks that need Home Assistant: outputs used as sensors; rooms of areas that do not exist."""
        ha = self.ha
        registry = {e.get("entity_id"): e for e in (ha.entity_registry if ha else [])}
        problems = []
        for area_id in sorted(changed):
            room = (config.get("rooms") or {}).get(area_id)
            if room is None:
                continue
            for i, sensor in enumerate(room.get("sensors", [])):
                if discovery.is_output(sensor["entity_id"], registry):
                    problems.append({"path": f"rooms.{area_id}.sensors[{i}]", "message": (
                        f"{sensor['entity_id']} is an occupancy result (of this App or of Smart Comfort), not a "
                        "sensor: using it would feed occupancy back into itself.")})
            for i, entity_id in enumerate(room.get("activity", [])):
                if discovery.is_output(entity_id, registry):
                    problems.append({"path": f"rooms.{area_id}.activity[{i}]",
                                     "message": f"{entity_id} is an occupancy result, not activity."})
        return problems

    def _notices(self, config: dict[str, Any], changed: set[str]) -> list[dict[str, str]]:
        notices = []
        for area_id in sorted(changed):
            room = (config.get("rooms") or {}).get(area_id)
            if room is None:
                continue
            for sensor in room.get("sensors", []):
                if not self._entity_exists(sensor["entity_id"]):
                    notices.append({"path": f"rooms.{area_id}", "message": (
                        f"{sensor['entity_id']} does not exist in Home Assistant at the moment; it counts as "
                        "unavailable until it appears.")})
        return notices

    def review(self, new_config: dict[str, Any], *, changed: set[str] | None = None) -> dict[str, Any]:
        """Validation, the changes, and what every affected room would show right after saving."""
        current = self.store.config
        try:
            clean = C.validate_config(new_config)
        except C.ConfigError as err:
            return {"valid": False, "problems": err.problems, "warnings": [], "changes": [], "impact": {},
                    "revision": self.store.revision}
        changes = C.diff(current, clean)
        if changed is None:
            changed = {c["area_id"] for c in changes if "area_id" in c}
            if any(c["type"] == "global_timing" for c in changes):
                changed |= set(clean["rooms"])
        problems = self._extra_problems(clean, changed)
        impact = {}
        if not problems and self.runtime is not None:
            predicted = self.runtime.predict(clean)
            impact = {a: {**v, "name": clean["rooms"].get(a, {}).get("name") or a}
                      for a, v in predicted.items() if a in changed}
        return {"valid": not problems, "problems": problems,
                "impact_unknown": self.runtime is None or self.runtime.suspended,
                "warnings": [*C.warnings_of(clean), *self._notices(clean, changed)], "changes": changes,
                "impact": impact, "revision": self.store.revision}

    def _save(self, new_config: dict[str, Any], expected_revision: Any, reason: str, by: str | None,
              changed: set[str] | None = None) -> dict[str, Any]:
        # Not while Home Assistant is away: the checks need its registries, and decisions are frozen meanwhile.
        self._require_ha()
        self.store.check_revision(expected_revision)
        review = self.review(new_config, changed=changed)
        if not review["valid"]:
            raise C.ConfigError("invalid", "The configuration is not valid", problems=review["problems"])
        result = self.store.save(new_config, expected_revision=expected_revision, reason=reason, by=by)
        removed = [c["area_id"] for c in review["changes"] if c["type"] == "room_removed"]
        if removed and self.link is not None:
            self.link.mark_deleted(removed)  # deliberate, reviewed: only now do their entities go
        return {**result, "changes": review["changes"]}

    def _with_room(self, area_id: str, room: Any) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        if isinstance(room, dict):
            room = {**room, "name": self._area_name(area_id) or room.get("name") or area_id}
        new["rooms"][area_id] = room
        return new

    def _check_area(self, area_id: Any) -> str:
        if not isinstance(area_id, str) or not C.AREA_ID.match(area_id):
            raise C.ConfigError("invalid", "Not a valid area")
        if area_id not in (self.store.config.get("rooms") or {}):
            self._require_ha()
            if not self._area_exists(area_id):
                raise C.ConfigError("unknown_area", "This Home Assistant area does not exist (anymore).", 404)
        return area_id

    def review_room(self, area_id: Any, room: Any) -> dict[str, Any]:
        area_id = self._check_area(area_id)
        return self.review(self._with_room(area_id, room), changed={area_id})

    def save_room(self, area_id: Any, room: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        area_id = self._check_area(area_id)
        added = area_id not in (self.store.config.get("rooms") or {})
        return self._save(self._with_room(area_id, room), expected_revision,
                          f"room {'added' if added else 'changed'}: {area_id}", by, {area_id})

    def _without_room(self, area_id: str) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        if area_id not in new["rooms"]:
            raise C.ConfigError("unknown_room", "This room is not configured", 404)
        del new["rooms"][area_id]
        for room in new["rooms"].values():
            room["adjacent"] = [a for a in room.get("adjacent", []) if a != area_id]
        return new

    def review_delete(self, area_id: Any) -> dict[str, Any]:
        return self.review(self._without_room(str(area_id)))

    def delete_room(self, area_id: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        return self._save(self._without_room(str(area_id)), expected_revision, f"room removed: {area_id}", by)

    def _with_timing(self, timing: Any) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        new["timing"] = timing
        return new

    def review_timing(self, timing: Any) -> dict[str, Any]:
        return self.review(self._with_timing(timing))

    def save_timing(self, timing: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        return self._save(self._with_timing(timing), expected_revision, "global timing changed", by)

    # --------------------------------------------------- export, import, backups
    def review_import(self, document: Any) -> dict[str, Any]:
        new = self.store.parse_import(document)
        return self.review(new)

    def apply_import(self, document: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        return self._save(self.store.parse_import(document), expected_revision, "import", by)

    def review_backup(self, name: Any) -> dict[str, Any]:
        return {**self.review_import(self.store.backup_document(name)), "name": name}

    def restore_backup(self, name: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        return self._save(self.store.parse_import(self.store.backup_document(name)), expected_revision,
                          "backup restored", by)

    def review_smart_comfort(self, document: Any) -> dict[str, Any]:
        return smart_comfort_import.review(document, self.store.config, area_name=self._area_name,
                                           area_exists=self._area_exists, entity_exists=self._entity_exists)

    def preview_smart_comfort(self, document: Any, rooms: Any, include_timing: bool) -> dict[str, Any]:
        new, notes = smart_comfort_import.apply(document, self.store.config, rooms, include_timing,
                                                area_name=self._area_name)
        return {**self.review(new), "notes": notes}

    def apply_smart_comfort(self, document: Any, rooms: Any, include_timing: bool, expected_revision: Any,
                            by: str | None) -> dict[str, Any]:
        new, notes = smart_comfort_import.apply(document, self.store.config, rooms, include_timing,
                                                area_name=self._area_name)
        return {**self._save(new, expected_revision, "Smart Comfort import", by), "notes": notes}

    # ------------------------------------------------------------ diagnostics
    def connection(self) -> dict[str, Any]:
        return {"home_assistant": self.ha.status() if self.ha else None,
                "companion": self.link.status() if self.link else None}

    def diagnostics(self) -> dict[str, Any]:
        runtime = self.runtime
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "engine": ENGINE_VERSION, "derived_from": DERIVED_FROM,
                    "generated_at": utcnow_iso()},
            "settings": self.settings.public(),
            "connection": self.connection(),
            "runtime": runtime.status() if runtime else None,
            "configuration": {"revision": self.store.revision, "saved_at": self.store.saved_at,
                              "recovery": self.store.recovery, "rooms": len(self.store.config["rooms"]),
                              "backups": len(self.store.backups())},
            "rooms": [runtime.room_view(a, detail=True) for a in self.store.config["rooms"]] if runtime else [],
            "commands_sent": dict(self.ha.commands_sent) if self.ha else {},
            "log": self.buffer.entries("info")[-150:] if self.buffer else [],
        }
