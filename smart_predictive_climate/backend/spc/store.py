"""The App's configuration document: versioned, validated by the caller's rules, written atomically, backed up.

One document, ``<data>/config/config.json``::

    {"schema": 1, "revision": "<16 hex>", "saved_at": "...", "reason": "...", "by": "<user id>", "config": {...}}

This module knows nothing about what a configuration contains: the store gets
``validate(raw) -> dict`` (the canonical form; raises :class:`ConfigError`
listing every problem) and ``empty() -> dict`` (a fresh installation) from the
domain module (``config.py``).

* every save is checked against the expected revision (two editors cannot
  overwrite each other unseen), validated strictly (an invalid configuration
  is refused, never repaired silently), written atomically (temporary file,
  ``os.replace``) and preceded by a backup of the previous document
  (``backups/``, the newest :data:`BACKUPS`);
* a damaged file is never used and never overwritten: the store falls back to
  ``config.json.bak`` or the newest valid backup and reports it (``recovery``);
* an export is the complete document; an import is parsed and validated first.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
EXPORT_FORMAT = "smart_predictive_climate.config"
BACKUPS = 30
BACKUP_NAME = re.compile(r"^[0-9TZ\-]{16,40}(-[a-z_]{1,32})?\.json$")


class ConfigError(Exception):
    """A request was refused (``code`` is stable; ``problems`` lists what is wrong; ``extra`` goes into the JSON
    error)."""

    def __init__(self, code: str, message: str, status: int = 400, problems: list[dict[str, str]] | None = None,
                 **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.problems = problems or []
        self.extra = extra


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def revision_of(config: dict[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _rooms(config: Any) -> int | None:
    rooms = config.get("rooms") if isinstance(config, dict) else None
    return len(rooms or {}) if rooms is None or isinstance(rooms, (dict, list)) else None


class ConfigStore:
    """The configuration document with its revision, backups and recovery."""

    def __init__(self, data_dir: Path, *, validate: Callable[[Any], dict[str, Any]],
                 empty: Callable[[], dict[str, Any]], export_format: str = EXPORT_FORMAT,
                 schema: int = SCHEMA) -> None:
        self.dir = Path(data_dir) / "config"
        self.path = self.dir / "config.json"
        self.backup_dir = self.dir / "backups"
        self._validate = validate
        self._empty = empty
        self.export_format = export_format
        self.schema = schema
        self.config: dict[str, Any] = empty()
        self.revision = revision_of(self.config)
        self.saved_at: str | None = None
        self.recovery: dict[str, Any] | None = None  # what happened at load when the file was not usable
        self._listeners: list[Callable[[str], None]] = []

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """``listener(reason)`` after every save that changed the configuration. Returns a function that removes
        it."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    # ----------------------------------------------------------------- load
    def _read_document(self, path: Path) -> dict[str, Any]:
        """A stored document, validated; raises ``ValueError`` (or ``OSError``, ``ConfigError``) if unusable."""
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != self.schema:
            raise ValueError(f"unsupported schema {data.get('schema') if isinstance(data, dict) else None!r}")
        config = self._validate(data.get("config"))
        if data.get("revision") != revision_of(config):
            raise ValueError("the revision does not match the content")
        return {**data, "config": config}

    def load(self) -> None:
        bak = self.path.with_name("config.json.bak")
        if not self.path.exists() and not bak.exists() and not self._backup_files():
            return  # a fresh installation
        errors = []
        for candidate in (self.path, bak, *self._backup_files()):
            if not candidate.exists():
                continue
            try:
                document = self._read_document(candidate)
            except (OSError, ValueError, ConfigError) as err:
                errors.append(f"{candidate.name}: {err}")
                continue
            self.config, self.revision = document["config"], document["revision"]
            self.saved_at = document.get("saved_at")
            if candidate != self.path:
                self.recovery = {"used": candidate.name, "errors": errors, "at": iso(utcnow())}
                _LOGGER.error("The configuration file was not usable (%s); using %s instead. The damaged file is "
                              "kept; save once to replace it.", "; ".join(errors) or "missing", candidate.name)
                self._keep_damaged()
            return
        self.recovery = {"used": None, "errors": errors, "at": iso(utcnow())}
        _LOGGER.error("No usable configuration was found (%s); starting with an empty one. The damaged files are "
                      "kept.", "; ".join(errors))
        self._keep_damaged()

    def _keep_damaged(self) -> None:
        if self.path.exists():
            target = self.dir / f"config.damaged-{utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
            try:
                os.replace(self.path, target)
            except OSError as err:
                _LOGGER.warning("Could not set the damaged configuration aside: %s", err)

    def _backup_files(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted((p for p in self.backup_dir.iterdir() if BACKUP_NAME.match(p.name)), reverse=True)

    # ----------------------------------------------------------------- save
    def check_revision(self, expected: Any) -> None:
        if expected != self.revision:
            raise ConfigError("stale", "The configuration was changed meanwhile (in another window or by an "
                                       "import). Reload and try again.", 409, revision=self.revision)

    def save(self, config: Any, *, expected_revision: Any, reason: str, by: str | None = None) -> dict[str, Any]:
        """Validate, back up the current document, write atomically. Returns ``{"revision", "changed"}``."""
        self.check_revision(expected_revision)
        clean = self._validate(config)
        revision = revision_of(clean)
        if revision == self.revision and self.path.exists():
            return {"revision": revision, "changed": False}
        now = utcnow()
        if self.path.exists() or self.recovery is not None:
            self._backup(reason)  # also the configuration recovered from a copy: it must exist somewhere
        document = {"schema": self.schema, "revision": revision, "saved_at": iso(now), "reason": reason[:100],
                    "by": by, "config": clean}
        if self.path.exists():
            try:
                os.replace(self.path, self.path.with_name("config.json.bak"))
            except OSError as err:
                _LOGGER.warning("Could not keep the previous configuration as config.json.bak: %s", err)
        _atomic_write(self.path, document)
        self.config, self.revision, self.saved_at, self.recovery = clean, revision, document["saved_at"], None
        _LOGGER.info("Configuration saved (%s, revision %s)", reason, revision)
        for listener in list(self._listeners):
            try:
                listener(reason)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Configuration listener failed")
        return {"revision": revision, "changed": True}

    def _backup(self, reason: str) -> None:
        stamp = utcnow().strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        slug = re.sub(r"[^a-z_]", "", reason.lower().replace(" ", "_"))[:32] or "change"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.backup_dir / f"{stamp}-{slug}.json",
                      {"schema": self.schema, "revision": self.revision, "saved_at": self.saved_at,
                       "backed_up_at": iso(utcnow()), "config": self.config})
        for old in self._backup_files()[BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass

    # -------------------------------------------------------------- backups
    def backups(self) -> list[dict[str, Any]]:
        """The dated backups, newest first: name, backed_up_at, rooms (their number), revision."""
        out = []
        for path in self._backup_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("not an object")
                out.append({"name": path.name, "backed_up_at": data.get("backed_up_at"),
                            "rooms": _rooms(data.get("config")), "revision": data.get("revision")})
            except (OSError, ValueError):
                out.append({"name": path.name, "backed_up_at": None, "rooms": None, "revision": None,
                            "damaged": True})
        return out

    def backup_document(self, name: Any) -> dict[str, Any]:
        """A backup in the form of an export (for ``parse_import``)."""
        if not isinstance(name, str) or not BACKUP_NAME.match(name):
            raise ConfigError("unknown_backup", "Unknown backup", 404)
        path = self.backup_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            raise ConfigError("unknown_backup", "Unknown backup", 404) from None
        except (OSError, ValueError) as err:
            raise ConfigError("damaged_backup", f"The backup cannot be read ({err})") from err
        return {"format": self.export_format, "version": self.schema, "config": data.get("config")}

    # ------------------------------------------------------- export, import
    def export(self) -> dict[str, Any]:
        return {"format": self.export_format, "version": self.schema, "exported_at": iso(utcnow()),
                "revision": self.revision, "config": copy.deepcopy(self.config)}

    def parse_import(self, document: Any) -> dict[str, Any]:
        """The validated configuration of an export (or of ``backup_document``); raises ``ConfigError``."""
        if not isinstance(document, dict) or document.get("format") != self.export_format:
            raise ConfigError("invalid_import", "This is not a configuration export of this App")
        if document.get("version") != self.schema:
            raise ConfigError("invalid_import", f"Unsupported export version {document.get('version')!r}")
        return self._validate(document.get("config"))
