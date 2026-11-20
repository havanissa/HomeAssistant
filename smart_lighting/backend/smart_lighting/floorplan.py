"""Floor plans (Phase A2.1): storage in the App's own ``/data`` volume, and the Home Assistant context.

The floor plan is the App's own data. It never goes into the integration's
configuration, and nothing here sends a Home Assistant command other than the
reads of :mod:`smart_lighting.ha`. A drawn room links to a Home Assistant area
by its ``area_id``; a placed icon links to an entity by its ``entity_id`` and
registry id. Moving an icon changes nothing in Home Assistant or in the
integration: assignments are changed only in the Rooms editor, through the
integration's reviewed commands.

Storage (``/data/floorplan``)::

    plan.json                     the current plan (atomic writes: temporary file, fsync, rename)
    backups/plan-<stamp>-<rev>.json  earlier versions (the latest 30)
    images/img_<sha>.<png|jpg|webp>  uploaded backgrounds, content-addressed, checked by their bytes
    plan.corrupt-<stamp>.json     an unreadable plan.json, moved aside when the plan was recovered

Every save names the revision it was based on (stale saves from another
browser session are refused with 409), and other sessions are told about the
new revision over the live channel.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import time
from typing import Any

from . import floorplan_model as fm
from .const import APP_VERSION, INTEGRATION_DOMAIN
from .ha import HomeAssistantClient, utcnow_iso
from .rooms import CATEGORIES, ENTITY_ID, RoomsService, _device_view, live_attributes

_LOGGER = logging.getLogger(__name__)

BACKUP_FORMAT = "smart_lighting.floorplan_backup"
BACKUP_KEEP = 30
BACKUP_COALESCE = 600.0  # seconds: while editing, at most one automatic backup per 10 minutes
BACKUP_ID = re.compile(r"^plan-\d{8}T\d{9}Z-[0-9a-f]{16}(?:-\d+)?$")
IMAGE_EXT = {"png": "png", "jpeg": "jpg", "webp": "webp"}
EXT_TYPE = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
MAX_IMAGE_STORE = 200 * 1024 * 1024
ORPHAN_IMAGE_AGE = 24 * 3600.0
ROOM_ENTITY = re.compile(rf"^{INTEGRATION_DOMAIN}_room_(?P<area>.+)_(?P<key>state|mood_lighting|occupied)$")


class FloorPlanError(Exception):
    """A floor plan request was refused or failed; ``code`` is stable, the message is for people."""

    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra



def _stamp(millis: bool = False) -> str:
    now = time.time()
    base = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))
    return f"{base}{int(now * 1000) % 1000:03d}Z" if millis else f"{base}Z"


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` so that ``path`` holds either the old or the new content, never a partial file."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    with contextlib.suppress(OSError):
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class FloorPlanService:
    def __init__(self, ha: HomeAssistantClient, rooms: RoomsService, data_dir: Path | None) -> None:
        self.ha = ha
        self.rooms = rooms
        self.dir = data_dir / "floorplan" if data_dir else None
        self.plan: dict[str, Any] = fm.empty_plan()
        self.revision = fm.revision_of(self.plan)
        self.read_only: str | None = None if self.dir else "The App has no data folder, so floor plans cannot be saved."
        self.read_only_code: str | None = None if self.dir else "no_storage"
        self.notices: list[dict[str, str]] = []
        self.saved_at: str | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[str], None]] = []
        self._last_auto_backup = 0.0

    # ------------------------------------------------------------- paths
    @property
    def plan_path(self) -> Path:
        assert self.dir is not None
        return self.dir / "plan.json"

    @property
    def backup_dir(self) -> Path:
        assert self.dir is not None
        return self.dir / "backups"

    @property
    def image_dir(self) -> Path:
        assert self.dir is not None
        return self.dir / "images"

    def add_listener(self, listener: Callable[[str], None]) -> None:
        """``listener(revision)`` after every stored change."""
        self._listeners.append(listener)

    def _emit(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self.revision)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Floor plan listener failed")

    # ------------------------------------------------------------- loading
    def load(self) -> None:
        """Read the stored plan at start; recover from an interrupted or damaged save."""
        if self.dir is None:
            return
        try:
            for path in (self.dir, self.backup_dir, self.image_dir):
                path.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self.read_only, self.read_only_code = f"The floor plan folder cannot be created ({err}).", "no_storage"
            _LOGGER.error("Floor plan storage is not available: %s", err)
            return
        for leftover in [*self.dir.glob(".*.tmp"), *self.image_dir.glob(".*.tmp"), *self.backup_dir.glob(".*.tmp")]:
            _LOGGER.warning("Removed %s, left over from an interrupted save (the previous version is kept)",
                            leftover.name)
            with contextlib.suppress(OSError):
                leftover.unlink()
        if not self.plan_path.exists():
            self._gc_images()
            return
        try:
            raw = json.loads(self.plan_path.read_text(encoding="utf-8"))
            plan, steps = fm.migrate(raw)
            plan = fm.validate(plan)
        except fm.PlanError as err:
            if err.code == "newer_format":
                self.read_only, self.read_only_code = err.message, "newer_format"
                _LOGGER.error("Floor plan not loaded: %s", err.message)
                return
            self._recover(f"{err.code}: {err}")
            return
        except (OSError, ValueError) as err:
            self._recover(f"{type(err).__name__}: {err}")
            return
        self.plan, self.revision = plan, fm.revision_of(plan)
        self.saved_at = plan.get("updated_at")
        if steps:
            self._write_backup(raw, f"Before migrating from format {steps[0]}", force=True)
            self._store(plan)
            _LOGGER.info("Floor plan migrated from format %s to %s", steps[0], fm.SCHEMA_VERSION)
            self.notices.append({"code": "migrated", "message": f"The floor plan was updated from format {steps[0]} "
                                 f"to {fm.SCHEMA_VERSION}; the previous file is kept as a backup."})
        self._gc_images()

    def _recover(self, reason: str) -> None:
        aside = self.dir / f"plan.corrupt-{_stamp()}.json"  # type: ignore[operator]
        with contextlib.suppress(OSError):
            os.replace(self.plan_path, aside)
        _LOGGER.error("The stored floor plan could not be read (%s); moved it to %s", reason, aside.name)
        for info in self.backups():
            try:
                plan = fm.validate(fm.migrate(self._read_backup(info["id"])["plan"])[0])
            except (fm.PlanError, OSError, ValueError, KeyError, TypeError):
                continue
            self.plan, self.revision = plan, fm.revision_of(plan)
            self._store(plan)
            self.notices.append({"code": "recovered", "message": f"The stored floor plan was damaged (for example by "
                                 f"an interrupted save). The backup from {info['created_at']} was restored; the "
                                 f"damaged file was kept as {aside.name}."})
            _LOGGER.warning("Floor plan restored from the backup %s", info["id"])
            return
        self.notices.append({"code": "recovered_empty", "message": "The stored floor plan was damaged and no backup "
                             f"could be read, so the plan starts empty. The damaged file was kept as {aside.name}."})

    # ------------------------------------------------------------ reading
    def status(self) -> dict[str, Any]:
        return {
            "writable": self.read_only is None,
            "read_only": self.read_only,
            "read_only_code": self.read_only_code,
            "schema_version": fm.SCHEMA_VERSION,
            "revision": self.revision,
            "saved_at": self.saved_at,
            "notices": list(self.notices),
            "summary": fm.summary(self.plan),
        }

    def document(self) -> dict[str, Any]:
        return {"plan": self.plan, "revision": self.revision, "status": self.status()}

    # ------------------------------------------------------------ writing
    def _store(self, plan: dict[str, Any]) -> None:
        atomic_write(self.plan_path, json.dumps(plan, ensure_ascii=False, indent=1).encode())

    def _check_writable(self, expected: Any) -> None:
        if self.read_only is not None:
            raise FloorPlanError(self.read_only_code or "read_only", self.read_only, 409)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{16}", expected):
            raise FloorPlanError("invalid_revision", "A revision is required to save")
        if expected != self.revision:
            raise FloorPlanError("stale_revision", "The floor plan was changed in another session after you opened it.",
                                 409, revision=self.revision)

    def _check_images(self, plan: dict[str, Any]) -> None:
        for image_id in fm.image_ids(plan):
            if self.image_path(image_id) is None:
                raise FloorPlanError("unknown_image", f"The background image {image_id} is not stored in the App")

    async def save(self, plan: Any, expected: Any) -> dict[str, Any]:
        async with self._lock:
            self._check_writable(expected)
            try:
                clean = fm.validate(plan)
            except fm.PlanError as err:
                raise FloorPlanError(err.code, err.message, 400, path=err.path) from err
            self._check_images(clean)
            if fm.revision_of(clean) == self.revision:
                return {"saved": False, "revision": self.revision, "saved_at": self.saved_at, "backup": None}
            backup = self._auto_backup()
            return await self._commit(clean, backup)

    async def _commit(self, plan: dict[str, Any], backup: str | None) -> dict[str, Any]:
        plan["updated_at"] = utcnow_iso()
        try:
            await asyncio.to_thread(self._store, plan)
        except OSError as err:
            raise FloorPlanError("storage_error", f"The floor plan could not be written ({err}). The stored version "
                                 "is unchanged.", 500) from err
        self.plan, self.revision, self.saved_at = plan, fm.revision_of(plan), plan["updated_at"]
        self.notices = [n for n in self.notices if n["code"] not in ("recovered", "recovered_empty", "migrated")]
        self._emit()
        self._gc_images()
        return {"saved": True, "revision": self.revision, "saved_at": self.saved_at, "backup": backup}

    # ------------------------------------------------------------ backups
    def _auto_backup(self) -> str | None:
        if not self.plan.get("floors"):
            return None  # nothing worth keeping yet
        now = time.monotonic()
        if self._last_auto_backup and now - self._last_auto_backup < BACKUP_COALESCE:
            return None
        self._last_auto_backup = now
        return self._write_backup(self.plan, "Before saving")

    def _write_backup(self, plan: Any, reason: str, *, force: bool = False) -> str | None:
        if self.dir is None or (not force and not (isinstance(plan, dict) and plan.get("floors"))):
            return None
        revision = fm.revision_of(plan) if isinstance(plan, dict) else "0" * 16
        data = {"format": BACKUP_FORMAT, "version": 1, "created_at": utcnow_iso(), "reason": reason,
                "app_version": APP_VERSION, "revision": revision, "plan": plan}
        name = f"plan-{_stamp(millis=True)}-{revision}"  # sorts by time
        path = self.backup_dir / f"{name}.json"
        n = 1
        while path.exists():
            n += 1
            path = self.backup_dir / f"{name}-{n}.json"
        try:
            atomic_write(path, json.dumps(data, ensure_ascii=False, indent=1).encode())
            for old in sorted(self.backup_dir.glob("plan-*.json"), reverse=True)[BACKUP_KEEP:]:
                old.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.warning("Could not store a floor plan backup: %s", err)
            return None
        return path.stem

    def backups(self) -> list[dict[str, Any]]:
        if self.dir is None or not self.backup_dir.is_dir():
            return []
        out = []
        for path in sorted(self.backup_dir.glob("plan-*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            plan = data.get("plan") if isinstance(data, dict) else None
            out.append({"id": path.stem, "created_at": data.get("created_at"), "reason": data.get("reason"),
                        "revision": data.get("revision"),
                        "summary": fm.summary(plan) if isinstance(plan, dict) else None})
        return out

    def _read_backup(self, backup_id: str) -> dict[str, Any]:
        if self.dir is None or not BACKUP_ID.match(backup_id):
            raise FloorPlanError("unknown_backup", "Unknown backup", 404)
        try:
            data = json.loads((self.backup_dir / f"{backup_id}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise FloorPlanError("unknown_backup", "Unknown backup", 404) from err
        if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
            raise FloorPlanError("unknown_backup", "Unknown backup", 404)
        return data

    def backup(self, backup_id: str) -> dict[str, Any]:
        return self._read_backup(backup_id)

    async def restore(self, backup_id: str, expected: Any) -> dict[str, Any]:
        data = self._read_backup(backup_id)
        async with self._lock:
            self._check_writable(expected)
            plan, warnings = self._incoming(data.get("plan"), {})
            saved = self._write_backup(self.plan, "Before restoring a backup")
            result = await self._commit(plan, saved)
        _LOGGER.info("Floor plan restored from the backup %s", backup_id)
        return {**result, "warnings": warnings}

    # ------------------------------------------------------- export/import
    def export(self) -> dict[str, Any]:
        images = {}
        for image_id in sorted(fm.image_ids(self.plan)):
            found = self.image_path(image_id)
            if found:
                images[image_id] = {"content_type": found[1],
                                    "data": base64.b64encode(found[0].read_bytes()).decode("ascii")}
        return {"format": fm.EXPORT_FORMAT, "version": 1, "exported_at": utcnow_iso(), "app_version": APP_VERSION,
                "revision": self.revision, "plan": self.plan, "images": images}

    @staticmethod
    def _unpack(doc: Any) -> tuple[Any, dict[str, Any]]:
        """(plan, images) from an export file, a backup file or a bare plan."""
        if not isinstance(doc, dict):
            raise FloorPlanError("invalid_file", "This file is not a Smart Comfort floor plan")
        if doc.get("format") == fm.EXPORT_FORMAT:
            images = doc.get("images") or {}
            if not isinstance(images, dict) or len(images) > fm.LIMITS["floors"]:
                raise FloorPlanError("invalid_file", "The file's image section is not valid")
            return doc.get("plan"), images
        if doc.get("format") == BACKUP_FORMAT:
            return doc.get("plan"), {}
        if doc.get("format") == fm.FORMAT:
            return doc, {}
        raise FloorPlanError("invalid_file", "This file is not a Smart Comfort floor plan")

    def _decode_images(self, images: dict[str, Any]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for image_id, item in images.items():
            if not isinstance(image_id, str) or not fm.IMAGE_ID.match(image_id) or not isinstance(item, dict):
                raise FloorPlanError("invalid_file", "The file contains an invalid image entry")
            try:
                data = base64.b64decode(str(item.get("data") or ""), validate=True)
                fm.sniff_image(data)
            except (binascii.Error, ValueError) as err:
                raise FloorPlanError("invalid_image", f"The image {image_id} in the file is not valid: {err}") from err
            if fm.image_id_for(data) != image_id:
                raise FloorPlanError("invalid_image", f"The image {image_id} does not match its content")
            out[image_id] = data
        return out

    def _incoming(self, plan: Any, images: dict[str, bytes]) -> tuple[dict[str, Any], list[str]]:
        """Migrate and validate an imported or restored plan; drop backgrounds whose image is not available."""
        try:
            clean = fm.validate(fm.migrate(plan)[0])
        except fm.PlanError as err:
            raise FloorPlanError(err.code, f"The floor plan in the file is not valid: {err}", 400, path=err.path) from err
        warnings = []
        for floor in clean["floors"]:
            bg = floor.get("background")
            if bg and bg["image_id"] not in images and self.image_path(bg["image_id"]) is None:
                warnings.append(f"The background image of {floor['name']} is not available; the floor is kept "
                                "without it.")
                floor["background"] = None
        return clean, warnings

    def import_review(self, doc: Any) -> dict[str, Any]:
        plan, raw_images = self._unpack(doc)
        images = self._decode_images(raw_images)
        clean, warnings = self._incoming(plan, images)
        return {"summary": fm.summary(clean), "current": fm.summary(self.plan), "warnings": warnings,
                "revision": self.revision, "writable": self.read_only is None,
                "identical": fm.revision_of(clean) == self.revision}

    async def import_plan(self, doc: Any, expected: Any) -> dict[str, Any]:
        plan, raw_images = self._unpack(doc)
        images = self._decode_images(raw_images)
        async with self._lock:
            self._check_writable(expected)
            clean, warnings = self._incoming(plan, images)
            for image_id, data in images.items():
                if image_id in fm.image_ids(clean):
                    await self._store_image(data)
            saved = self._write_backup(self.plan, "Before importing a file")
            result = await self._commit(clean, saved)
        _LOGGER.info("Floor plan imported (%s)", fm.summary(clean))
        return {**result, "warnings": warnings}

    # ------------------------------------------------------------- images
    def image_path(self, image_id: str) -> tuple[Path, str] | None:
        """The stored file of an image id (validated against the id pattern: no path from the request is used)."""
        if self.dir is None or not isinstance(image_id, str) or not fm.IMAGE_ID.match(image_id):
            return None
        base = self.image_dir.resolve()
        for ext, content_type in EXT_TYPE.items():
            path = (self.image_dir / f"{image_id}.{ext}").resolve()
            if path.parent == base and path.is_file():
                return path, content_type
        return None

    def _image_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.image_dir.glob("img_*") if p.is_file())

    async def _store_image(self, data: bytes) -> dict[str, Any]:
        kind, width, height = fm.sniff_image(data)
        image_id = fm.image_id_for(data)
        if self.image_path(image_id) is None:
            self._gc_images()
            if self._image_bytes() + len(data) > MAX_IMAGE_STORE:
                raise FloorPlanError("storage_full", "The App's image storage is full (200 MB). Remove backgrounds "
                                                     "that are no longer used.", 507)
            path = self.image_dir / f"{image_id}.{IMAGE_EXT[kind]}"
            await asyncio.to_thread(atomic_write, path, data)
        return {"image_id": image_id, "width_px": width, "height_px": height, "content_type": fm.IMAGE_TYPES[kind],
                "bytes": len(data)}

    async def upload_image(self, data: bytes) -> dict[str, Any]:
        if self.read_only is not None:
            raise FloorPlanError(self.read_only_code or "read_only", self.read_only, 409)
        try:
            return await self._store_image(data)
        except fm.ImageError as err:
            raise FloorPlanError(err.code, str(err), 413 if err.code == "too_large" else 415) from err

    def _gc_images(self) -> None:
        """Remove uploaded images that neither the plan nor any backup uses (after a day, so uploads survive)."""
        if self.dir is None or not self.image_dir.is_dir():
            return
        used = fm.image_ids(self.plan)
        for info in self.backups():
            with contextlib.suppress(FloorPlanError):
                plan = self._read_backup(info["id"]).get("plan")
                if isinstance(plan, dict):
                    used |= fm.image_ids(plan)
        now = time.time()
        for path in self.image_dir.glob("img_*"):
            if path.stem not in used and now - path.stat().st_mtime > ORPHAN_IMAGE_AGE:
                with contextlib.suppress(OSError):
                    path.unlink()

    # ----------------------------------------------- Home Assistant context
    def room_entities(self) -> dict[str, dict[str, str]]:
        """area_id -> the integration's own per-room entities, found by their registry unique ids (never by name)."""
        out: dict[str, dict[str, str]] = {}
        for entity_id, reg in self.rooms.index().registry.items():
            if reg.get("platform") != INTEGRATION_DOMAIN or reg.get("disabled_by"):
                continue
            match = ROOM_ENTITY.match(str(reg.get("unique_id") or ""))
            if match:
                out.setdefault(match["area"], {})[match["key"]] = entity_id
        return out

    def context(self) -> dict[str, Any]:
        """Areas, floors, placeable entities and the integration's rooms, for the editor (read-only)."""
        idx = self.rooms.index()
        rooms_cfg = (self.rooms.model or {}).get("rooms") or {}
        room_entities = self.room_entities()
        entities = []
        for c in self.rooms.candidates():
            reg = idx.registry.get(c["entity_id"]) or {}
            device = idx.devices.get(c["device_id"] or "") or {}
            state = self.ha.states.get(c["entity_id"]) or {}
            entities.append({
                **c,
                "entity_uid": reg.get("id") if isinstance(reg.get("id"), str) else None,
                "device": device.get("name_by_user") or device.get("name") if device else None,
                "icon": CATEGORIES.get(c["category"], CATEGORIES["other"])[1],
                "group": CATEGORIES.get(c["category"], CATEGORIES["other"])[2],
                "unit": (state.get("attributes") or {}).get("unit_of_measurement"),
            })
        rooms = {}
        for area_id, room in rooms_cfg.items():
            cfg = room.get("config") or {}
            lighting = room.get("lighting")
            rooms[area_id] = {
                "configured": True,
                "enabled": bool(cfg.get("enabled")),
                "adjacent_rooms": list(cfg.get("adjacent_rooms") or []),
                "sensors": [s.get("entity_id") for s in cfg.get("sensors") or []],
                "lights": list(cfg.get("lights") or []),
                "lighting": None if lighting is None else ("automatic" if lighting.get("automatic") else "observing"),
                "occupancy": room.get("occupancy"),
            }
        units = ((self.ha.config.get("unit_system") or {}).get("length"))
        return {
            "areas": sorted(({"id": a, "name": v.get("name") or a, "floor_id": v.get("floor_id"), "icon": v.get("icon")}
                             for a, v in idx.areas.items()), key=lambda x: str(x["name"]).casefold()),
            "floors": sorted(({"id": f.get("floor_id"), "name": f.get("name") or f.get("floor_id"),
                               "level": f.get("level"), "icon": f.get("icon")}
                              for f in self.ha.floors if f.get("floor_id")),
                             key=lambda f: (f["level"] if isinstance(f["level"], int) else 0, str(f["name"]))),
            "entities": entities,
            "rooms": rooms,
            "room_entities": room_entities,
            "integration": self.rooms.status(),
            "categories": {k: {"label": v[0], "icon": v[1], "group": v[2]} for k, v in CATEGORIES.items()},
            "units": "imperial" if units == "mi" else "metric",
            "generated_at": utcnow_iso(),
        }

    def entity_details(self, entity_id: str) -> dict[str, Any]:
        if not ENTITY_ID.match(entity_id):
            raise FloorPlanError("invalid_entity", "Invalid entity id")
        idx = self.rooms.index()
        info = self.rooms.entity_info(entity_id)
        device = idx.devices.get(info["device_id"] or "")
        model = self.rooms.model or {}
        schema = model.get("schema") or {}
        kinds = {k.get("value"): k.get("label") for k in schema.get("kinds") or []}
        roles = {r.get("value"): str(r.get("label") or "").split(":")[0] for r in schema.get("roles") or []}
        uses = []
        for area_id, room in (model.get("rooms") or {}).items():
            cfg = room.get("config") or {}
            name = (idx.areas.get(area_id) or {}).get("name") or area_id
            for s in cfg.get("sensors") or []:
                if s.get("entity_id") == entity_id:
                    uses.append({"area_id": area_id, "area": name, "as": "sensor", "kind": s.get("kind"),
                                 "kind_label": kinds.get(s.get("kind"), s.get("kind")), "role": s.get("role"),
                                 "role_label": roles.get(s.get("role"), s.get("role")),
                                 "enabled": s.get("enabled") is not False, "inverted": bool(s.get("inverted"))})
            if entity_id in (cfg.get("lights") or []):
                uses.append({"area_id": area_id, "area": name, "as": "light"})
            if cfg.get("reference_occupancy_entity") == entity_id:
                uses.append({"area_id": area_id, "area": name, "as": "reference"})
            lighting = room.get("lighting") or {}
            if entity_id in (lighting.get("relays") or []):
                uses.append({"area_id": area_id, "area": name, "as": "relay"})
            for sw in lighting.get("switches") or []:
                if sw.get("event_entity") == entity_id:
                    uses.append({"area_id": area_id, "area": name, "as": "switch", "mode": sw.get("mode"),
                                 "name": sw.get("name")})
            for window in room.get("curtains") or []:
                if entity_id in (window.get("covers") or []):
                    uses.append({"area_id": area_id, "area": name, "as": "curtain", "mode": window.get("mode"),
                                 "name": window.get("name")})
        siblings = []
        if device:
            for other in idx.device_entities.get(device["id"], []):
                reg = idx.registry.get(other) or {}
                if other != entity_id and not reg.get("disabled_by") and not reg.get("hidden_by") \
                        and not reg.get("entity_category") and ENTITY_ID.match(other):
                    sib = self.rooms.entity_info(other)
                    siblings.append({k: sib[k] for k in ("entity_id", "name", "category", "category_label", "state",
                                                         "availability", "unit")})
        return {
            "entity": {**info, "attrs": live_attributes(self.ha.states.get(entity_id)),
                       "entity_uid": (idx.registry.get(entity_id) or {}).get("id")},
            "device": _device_view(device, idx, self.ha.states) if device else None,
            "uses": uses,
            "siblings": siblings,
            "integration": self.rooms.status(),
        }
