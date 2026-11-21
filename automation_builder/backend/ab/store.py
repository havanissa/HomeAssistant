"""Storage in ``/data``: the rule configuration (with revision, backups and recovery) and the deployment records.

Two separate documents, on purpose:

* ``config/config.json`` - what the user defined (devices, rules, settings). Every save keeps the previous
  document as a backup; export, import and restore work on this document only.
* ``deploy/state.json`` - what this App wrote to Home Assistant: for every managed automation its id, the exact
  configuration, when, and the device as it was then (for rollback). It is the App's proof of ownership: an
  automation in Home Assistant that differs from its record was changed by someone else.
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

from .model import SCHEMA, ConfigError, empty_config, validate_config

_LOGGER = logging.getLogger(__name__)

EXPORT_FORMAT = "automation-builder-config"
BACKUPS = 30
HISTORY = 5
BACKUP_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{6}Z-[a-z_]{1,32}\.json$")


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def revision_of(config: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_document(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        raise ValueError(f"unsupported schema {document.get('schema') if isinstance(document, dict) else None!r}")
    config = validate_config(document.get("config"))
    return {**document, "config": config, "revision": revision_of(config)}


class ConfigStore:
    """The configuration document with its revision, backups and recovery."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "config"
        self.path = self.dir / "config.json"
        self.backup_dir = self.dir / "backups"
        self.config: dict[str, Any] = validate_config(empty_config())
        self.revision = revision_of(self.config)
        self.saved_at: str | None = None
        self.recovery: dict[str, Any] | None = None  # what happened at load when the file was not usable
        self._listeners: list[Callable[[str], None]] = []

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    # ----------------------------------------------------------------- load
    def load(self) -> None:
        if not self.path.exists():
            if not self.path.with_name("config.json.bak").exists() and not self._backup_files():
                return  # a fresh installation
        candidates = [self.path, self.path.with_name("config.json.bak"), *self._backup_files()]
        errors = []
        for candidate in candidates:
            if not candidate.exists():
                continue
            try:
                document = _read_document(candidate)
            except (OSError, ValueError, ConfigError) as err:
                errors.append(f"{candidate.name}: {err}")
                continue
            self.config, self.revision = document["config"], document["revision"]
            self.saved_at = document.get("saved_at")
            if candidate != self.path:
                self.recovery = {"used": candidate.name, "errors": errors, "at": iso(utcnow())}
                _LOGGER.error("The configuration file was not usable (%s); using %s instead. The damaged file is "
                              "kept; save once to replace it.", "; ".join(errors), candidate.name)
                self._keep_damaged()
            return
        self.recovery = {"used": None, "errors": errors, "at": iso(utcnow())}
        _LOGGER.error("No usable configuration was found (%s); starting without devices. The damaged files are "
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
                                       "import). Reload the page and make your change again.", 409,
                              revision=self.revision)

    def save(self, config: Any, *, expected_revision: Any, reason: str, by: str | None = None) -> dict[str, Any]:
        """Validate, back up the current document, write atomically. Returns the new revision."""
        self.check_revision(expected_revision)
        clean = validate_config(config)
        revision = revision_of(clean)
        if revision == self.revision and self.path.exists():
            return {"revision": revision, "changed": False}
        now = utcnow()
        if self.path.exists() or self.recovery is not None:
            self._backup(reason)
        document = {"schema": SCHEMA, "revision": revision, "saved_at": iso(now), "reason": reason[:100], "by": by,
                    "config": clean}
        if self.path.exists():
            try:
                os.replace(self.path, self.path.with_name("config.json.bak"))
            except OSError as err:
                _LOGGER.warning("Could not keep the previous configuration as config.json.bak: %s", err)
        _atomic_write(self.path, document)
        self.config, self.revision, self.saved_at, self.recovery = clean, revision, document["saved_at"], None
        _LOGGER.info("Configuration saved (%s, revision %s, %d devices)", reason, revision, len(clean["devices"]))
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
                      {"schema": SCHEMA, "revision": self.revision, "saved_at": self.saved_at,
                       "backed_up_at": iso(utcnow()), "reason": reason[:100], "config": self.config})
        for old in self._backup_files()[BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass

    # -------------------------------------------------------------- backups
    def backups(self) -> list[dict[str, Any]]:
        out = []
        for path in self._backup_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                devices = (data.get("config") or {}).get("devices") or {}
                out.append({"name": path.name, "backed_up_at": data.get("backed_up_at"), "devices": len(devices),
                            "rules": sum(len(d.get("rules") or []) for d in devices.values()),
                            "before": data.get("reason"), "revision": data.get("revision")})
            except (OSError, ValueError, AttributeError):
                out.append({"name": path.name, "backed_up_at": None, "devices": None, "rules": None,
                            "revision": None, "damaged": True})
        return out

    def backup_document(self, name: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not BACKUP_NAME.match(name):
            raise ConfigError("unknown_backup", "Unknown backup", 404)
        path = self.backup_dir / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError("unknown_backup", "Unknown backup", 404) from None
        except (OSError, ValueError) as err:
            raise ConfigError("damaged_backup", f"The backup cannot be read ({err})") from err
        return {"format": EXPORT_FORMAT, "version": SCHEMA, "config": data.get("config")}

    # ------------------------------------------------------- export, import
    def export(self) -> dict[str, Any]:
        return {"format": EXPORT_FORMAT, "version": SCHEMA, "exported_at": iso(utcnow()),
                "revision": self.revision, "config": copy.deepcopy(self.config)}

    def parse_import(self, document: Any) -> dict[str, Any]:
        if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
            raise ConfigError("invalid_import", "This is not an Automation Builder configuration export")
        if document.get("version") != SCHEMA:
            raise ConfigError("invalid_import", f"Unsupported export version {document.get('version')!r}")
        return validate_config(document.get("config"))


class DeployStore:
    """What this App deployed to Home Assistant, per automation id."""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "deploy" / "state.json"
        self.records: dict[str, dict[str, Any]] = {}
        self.problem: str | None = None

    def load(self) -> None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            records = document["records"]
            if not isinstance(records, dict):
                raise ValueError("records is not an object")
            self.records = {k: v for k, v in records.items() if isinstance(v, dict) and "hash" in v}
        except FileNotFoundError:
            self.records = {}
        except (OSError, ValueError, KeyError, TypeError) as err:
            # Without the records nothing in Home Assistant counts as this App's own: every automation with the
            # App's id prefix then shows as "not deployed by this App" and is never overwritten without a yes.
            self.problem = f"The deployment records could not be read ({err}); they are kept as state.json.damaged"
            _LOGGER.error(self.problem)
            try:
                os.replace(self.path, self.path.with_name("state.json.damaged"))
            except OSError:
                pass
            self.records = {}

    def _write(self) -> None:
        _atomic_write(self.path, {"schema": SCHEMA, "saved_at": iso(utcnow()), "records": self.records})

    def of_device(self, device_id: str) -> dict[str, dict[str, Any]]:
        return {k: v for k, v in self.records.items() if v.get("device_id") == device_id}

    def record(self, automation_id: str, *, device_id: str, config_hash: str, config: dict[str, Any],
               device: dict[str, Any], by: str | None) -> None:
        previous = self.records.get(automation_id)
        history = []
        if previous:
            history = [{k: previous.get(k) for k in ("hash", "config", "deployed_at", "device", "by")},
                       *previous.get("history", [])][:HISTORY]
        self.records[automation_id] = {"device_id": device_id, "hash": config_hash, "config": config,
                                       "deployed_at": iso(utcnow()), "device": copy.deepcopy(device), "by": by,
                                       "history": history}
        self._write()

    def forget(self, automation_id: str) -> None:
        if self.records.pop(automation_id, None) is not None:
            self._write()
