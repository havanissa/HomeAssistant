"""The App's core configuration (Phase A6): rooms, sensors, lights, categories, mood source, occupancy timing.

Until Phase A6 these settings were the integration's config entry options,
edited through its validated commands. The App now owns them: one versioned
document in ``/data/config/core.json`` in exactly the options format the engine
modules read (``models.IntegrationConfig``), so nothing about the rules changes:

* ``schema`` versions the wrapper; the options inside keep their own
  ``config_version`` (2) and ``IntegrationConfig.from_dict`` keeps accepting
  unknown compatible keys (they are carried along, never dropped);
* every save is atomic (temporary file, ``os.replace``), keeps the previous file
  as ``core.json.bak`` and a bounded set of timestamped backups;
* every save checks the expected revision: two editors (or a migration and an
  editor) cannot overwrite each other unseen;
* an export is the complete document; an import is reviewed (changes, missing
  entities) before it is applied, with the same revision check.

The document is authoritative only once it exists: a fresh installation creates
it with the onboarding, an existing installation imports it from the retired
integration through the migration wizard. Until then the App forwards room and
presence edits to the 0.9.0 integration exactly as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .companion_protocol import revision as _revision

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
BACKUPS = 30
EXPORT_FORMAT = "smart_lighting.core_config"


class CoreConfigError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _atomic_write(path: Path, data: dict[str, Any], *, backup: bool) -> None:
    """Write, fsync and rename; the previous file is *copied* to ``.bak`` first so core.json exists at every instant."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=1))
        handle.flush()
        os.fsync(handle.fileno())
    if backup and path.exists():
        shutil.copyfile(path, path.with_suffix(".json.bak"))
    os.replace(tmp, path)


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        return {"_unreadable": str(err)}


def validate_options(options: Any) -> dict[str, Any]:
    """The options document the engines read; parsed by the shared model (which keeps unknown keys)."""
    from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

    if not isinstance(options, dict):
        raise CoreConfigError("invalid_options", "The configuration must be an object")
    try:
        IntegrationConfig.from_dict(options)
    except (TypeError, ValueError, KeyError, AttributeError) as err:
        raise CoreConfigError("invalid_options", f"The configuration is not valid: {err}") from err
    return dict(options)


def default_options() -> dict[str, Any]:
    """A fresh installation: no rooms, no mood source, the default light types, default timing."""
    from smart_occupancy.models import IntegrationConfig, default_categories  # noqa: PLC0415

    return IntegrationConfig(categories=default_categories()).as_dict()


class CoreConfigStore:
    def __init__(self, data_dir: Path | None) -> None:
        self.dir = data_dir / "config" if data_dir else None
        self.path = self.dir / "core.json" if self.dir else None
        self.options: dict[str, Any] | None = None
        self.revision: str | None = None
        self.updated_at: str | None = None
        self.source: str | None = None
        #: ``app``: the App is the configuration authority. ``staged``: imported from the monolithic integration
        #: while that integration still runs (its options stay authoritative until the companion is active).
        self.authority: str | None = None
        self.notices: list[dict[str, str]] = []
        self._listeners: list[Callable[[dict[str, Any], str], None]] = []
        self._lock = asyncio.Lock()
        self.saves = 0

    # ------------------------------------------------------------ status
    @property
    def authoritative(self) -> bool:
        return self.options is not None and self.authority == "app"

    @property
    def staged(self) -> bool:
        return self.options is not None and self.authority == "staged"

    def status(self) -> dict[str, Any]:
        rooms = (self.options or {}).get("rooms") or {}
        enabled = sum(1 for r in rooms.values() if isinstance(r, dict) and r.get("enabled"))
        return {"authoritative": self.authoritative, "staged": self.staged, "authority": self.authority,
                "revision": self.revision, "updated_at": self.updated_at,
                "source": self.source, "rooms": len(rooms), "enabled_rooms": enabled, "schema": SCHEMA,
                "path": str(self.path) if self.path else None, "saves": self.saves, "notices": list(self.notices),
                "backups": len(self.backups())}

    def add_listener(self, listener: Callable[[dict[str, Any], str], None]) -> Callable[[], None]:
        """``listener(options, reason)`` after every change (save, import, restore)."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self, reason: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(dict(self.options or {}), reason)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Core configuration listener failed")

    # ------------------------------------------------------------ loading
    async def load(self) -> None:
        if self.path is None:
            return
        data = await asyncio.to_thread(_read, self.path)
        if data is None:
            # No core.json (a fresh installation), or a crash between the backup copy and the rename: the ``.bak``
            # is then the latest complete document.
            data = await asyncio.to_thread(_read, self.path.with_suffix(".json.bak"))
            if data is None:
                return
            self.notices.append({"code": "recovered", "text": "core.json was missing; the previous version was loaded"})
        try:
            if not isinstance(data, dict) or "_unreadable" in data:
                raise ValueError(str((data or {}).get("_unreadable") or "not an object"))
            if data.get("schema") != SCHEMA:
                raise ValueError(f"schema {data.get('schema')!r}")
            options = validate_options(data.get("options"))
        except (ValueError, CoreConfigError) as err:
            backup = await asyncio.to_thread(_read, self.path.with_suffix(".json.bak"))
            if isinstance(backup, dict) and backup.get("schema") == SCHEMA:
                try:
                    options = validate_options(backup.get("options"))
                except CoreConfigError:
                    options = None
                if options is not None:
                    _LOGGER.error("core.json is not usable (%s); the previous version was loaded", err)
                    self.notices.append({"code": "recovered", "text": f"core.json was damaged ({err}); the previous "
                                                                        "version was loaded"})
                    self._set(options, backup.get("updated_at"), "recovered", backup.get("authority"))
                    return
            _LOGGER.error("core.json is not usable (%s) and no previous version could be loaded", err)
            self.notices.append({"code": "unreadable", "text": f"core.json is not usable ({err}); the App is not "
                                                                 "the configuration authority until it is restored"})
            return
        self._set(options, data.get("updated_at"), data.get("source"), data.get("authority"))

    async def _reload_locked(self) -> None:
        """Re-read the file (the lock is held); used when another process changed it."""
        data = await asyncio.to_thread(_read, self.path) if self.path else None
        if isinstance(data, dict) and data.get("schema") == SCHEMA:
            try:
                options = validate_options(data.get("options"))
            except CoreConfigError:
                return
            self._set(options, data.get("updated_at"), data.get("source"), data.get("authority"))
            self._changed("reloaded")

    def _set(self, options: dict[str, Any], updated_at: Any, source: Any, authority: Any = "app") -> None:
        self.options = options
        self.revision = _revision(options)
        self.updated_at = updated_at if isinstance(updated_at, str) else None
        self.source = source if isinstance(source, str) else None
        self.authority = authority if authority in ("app", "staged") else "app"

    # ------------------------------------------------------------- saving
    def _document(self, reason: str) -> dict[str, Any]:
        return {"schema": SCHEMA, "options": self.options, "revision": self.revision, "updated_at": self.updated_at,
                "source": self.source, "authority": self.authority, "reason": reason}

    async def save(self, options: dict[str, Any], *, expected_revision: str | None, reason: str,
                   source: str = "app", authority: str | None = None) -> dict[str, Any]:
        """Replace the document (revision-checked, atomic, backed up). Returns the new status."""
        options = validate_options(options)
        async with self._lock:
            if self.options is not None and expected_revision is not None and expected_revision != self.revision:
                raise CoreConfigError("stale_revision", "The configuration changed meanwhile; reload and try again",
                                      409, revision=self.revision)
            # Stale detection against the file itself: another process (a second App instance, a restore by hand)
            # may have written core.json since this store loaded it. Nothing is overwritten blindly.
            if self.path is not None and self.options is not None:
                on_disk = await asyncio.to_thread(_read, self.path)
                disk_revision = on_disk.get("revision") if isinstance(on_disk, dict) else None
                if isinstance(disk_revision, str) and disk_revision != self.revision:
                    self.notices.append({"code": "changed_on_disk", "text": "core.json was changed by another "
                                                                             "process; it was reloaded"})
                    await self._reload_locked()
                    raise CoreConfigError("stale_revision", "The configuration file changed on disk; reload and try "
                                                            "again", 409, revision=self.revision)
            authority = authority or self.authority or "app"
            if self.options is not None and _revision(options) == self.revision and authority == self.authority:
                return self.status()
            if self.options is not None:
                await self._backup(reason)
            self._set(options, iso(utcnow()), source, authority)
            if self.path is not None:
                await asyncio.to_thread(_atomic_write, self.path, self._document(reason), backup=True)
            self.saves += 1
        _LOGGER.info("core_config_saved reason=%s revision=%s rooms=%d", reason, self.revision,
                     len(options.get("rooms") or {}))
        self._changed(reason)
        return self.status()

    async def _backup(self, reason: str) -> None:
        if self.dir is None or self.options is None:
            return
        folder = self.dir / "backups"
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        safe = "".join(c if c.isalnum() else "-" for c in reason)[:40]
        path = folder / f"core-{stamp}-{self.revision}-{safe}.json"

        def write() -> None:
            folder.mkdir(parents=True, exist_ok=True)
            n = 1
            target = path
            while target.exists():
                n += 1
                target = folder / f"{path.stem}-{n}.json"
            target.write_text(json.dumps(self.export(reason), ensure_ascii=False, indent=1), encoding="utf-8")
            for old in sorted(folder.glob("core-*.json"))[:-BACKUPS]:
                old.unlink(missing_ok=True)

        await asyncio.to_thread(write)

    def backups(self) -> list[dict[str, Any]]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.glob("core-*.json"), reverse=True):
            parts = path.stem.split("-", 3)
            out.append({"id": path.stem, "created_at": parts[1] if len(parts) > 1 else None,
                        "revision": parts[2] if len(parts) > 2 else None, "reason": parts[3] if len(parts) > 3 else None,
                        "size": path.stat().st_size})
        return out

    def backup(self, backup_id: str) -> dict[str, Any]:
        folder = self.dir / "backups" if self.dir else None
        if folder is None or not backup_id.startswith("core-") or "/" in backup_id or ".." in backup_id:
            raise CoreConfigError("unknown_backup", "Unknown backup", 404)
        data = _read(folder / f"{backup_id}.json")
        if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
            raise CoreConfigError("unknown_backup", "Unknown backup", 404)
        return data

    # ------------------------------------------------------------ transfer
    def export(self, reason: str = "export") -> dict[str, Any]:
        return {"format": EXPORT_FORMAT, "version": SCHEMA, "created_at": iso(utcnow()), "reason": reason,
                "revision": self.revision, "options": self.options}

    def import_review(self, document: Any, *, entity_exists: Callable[[str], bool] | None = None) -> dict[str, Any]:
        """What an import would change (room by room), and the entities it references that do not exist."""
        if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
            raise CoreConfigError("invalid_import", "This is not a Smart Lighting core configuration export")
        options = validate_options(document.get("options"))
        current = self.options or {}
        changes = []
        rooms_before, rooms_after = current.get("rooms") or {}, options.get("rooms") or {}
        for area_id in sorted(set(rooms_before) | set(rooms_after)):
            if area_id not in rooms_before:
                changes.append({"type": "room_added", "area_id": area_id})
            elif area_id not in rooms_after:
                changes.append({"type": "room_removed", "area_id": area_id})
            elif rooms_before[area_id] != rooms_after[area_id]:
                changes.append({"type": "room_changed", "area_id": area_id})
        for key in ("mood", "categories", "occupancy", "operating_mode", "global_profiles"):
            if current.get(key) != options.get(key):
                changes.append({"type": f"{key}_changed"})
        missing = []
        if entity_exists is not None:
            from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

            config = IntegrationConfig.from_dict(options)
            seen: set[str] = set()
            for room in config.rooms.values():
                for entity_id in room.referenced_entities:
                    if entity_id not in seen and not entity_exists(entity_id):
                        missing.append(entity_id)
                    seen.add(entity_id)
            for entity_id in config.mood.entity_ids:
                if entity_id not in seen and not entity_exists(entity_id):
                    missing.append(entity_id)
        return {"revision": self.revision, "import_revision": _revision(options), "changes": changes,
                "missing_entities": missing, "rooms": len(rooms_after), "identical": not changes}

    async def import_apply(self, document: Any, *, expected_revision: str | None, source: str = "import",
                           reason: str = "import", authority: str | None = None) -> dict[str, Any]:
        review = self.import_review(document)
        if self.options is not None and expected_revision != self.revision:
            raise CoreConfigError("stale_revision", "The configuration changed since the import was reviewed", 409,
                                  revision=self.revision)
        status = await self.save(document["options"], expected_revision=expected_revision, reason=reason,
                                 source=source, authority=authority)
        return {**review, "status": status}

    async def promote(self, reason: str = "activated") -> dict[str, Any]:
        """A staged configuration becomes the authority (the companion replaced the monolithic integration)."""
        if self.options is None:
            raise CoreConfigError("no_config", "There is no configuration to promote", 409)
        if self.authoritative:
            return self.status()
        return await self.save(self.options, expected_revision=self.revision, reason=reason, source=self.source or "app",
                               authority="app")

    async def demote(self, reason: str = "rolled back") -> dict[str, Any]:
        """Back to a staged copy (a rollback to the monolithic integration): the App stops editing it."""
        if self.options is None or self.staged:
            return self.status()
        return await self.save(self.options, expected_revision=self.revision, reason=reason, source=self.source or "app",
                               authority="staged")

    # ------------------------------------------------------------- renames
    async def rename_entity(self, old: str, new: str) -> bool:
        from smart_occupancy.models import rename_entity_references  # noqa: PLC0415

        if self.options is None:
            return False
        updated = rename_entity_references(dict(self.options), old, new)
        if updated is None:
            return False
        await self.save(updated, expected_revision=self.revision, reason=f"rename {old}", source="rename")
        return True
