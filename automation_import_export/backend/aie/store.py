"""Persistent storage in ``/data`` (included in Home Assistant backups of the App):

* ``archives/<archive id>.zip`` + ``archives/index.json`` - the stored archives (files 0600) and what is known
  about them (purpose, counts, checksum, verification). The index can be rebuilt from the files.
* ``operations/<operation id>.json`` - the durable journal of every operation: each step and each automation's
  status is written *before* and *after* it happens, so an interrupted export, deletion or restore can be
  recognised and reconciled at the next start.
* ``instance.json`` - a random id of this App installation (the archive's "source installation identity").
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
import re
import secrets
from typing import Any
import uuid

_LOGGER = logging.getLogger(__name__)
ARCHIVE_ID = re.compile(r"aie-\d{8}T\d{6}Z-[0-9a-f]{6}")
OPERATION_ID = re.compile(r"op-\d{8}T\d{6}Z-[0-9a-f]{6}")
RUNNING = ("running", "exporting", "verifying", "deleting", "restoring", "rolling_back")
KEEP_OPERATIONS = 200


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{utcnow().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{secrets.token_hex(3)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=1, default=str) + "\n").encode("utf-8"))


class InstanceIdentity:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "instance.json"
        self.instance_id = ""

    def load(self) -> None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.instance_id = str(value["instance_id"])
            uuid.UUID(self.instance_id)
        except (OSError, ValueError, KeyError, TypeError):
            self.instance_id = str(uuid.uuid4())
            write_json(self.path, {"instance_id": self.instance_id, "created_at": iso()})


class ArchiveStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "archives"
        self.index_path = self.dir / "index.json"
        self.entries: dict[str, dict[str, Any]] = {}

    def load(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        for stale in self.dir.glob("*.tmp-*"):  # an export interrupted while writing
            try:
                stale.unlink()
                _LOGGER.warning("Removed an incomplete archive file %s (interrupted export)", stale.name)
            except OSError:
                pass
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
            entries = value.get("archives") if isinstance(value, dict) else None
            self.entries = {k: v for k, v in (entries or {}).items() if ARCHIVE_ID.fullmatch(k) and isinstance(v, dict)}
        except FileNotFoundError:
            self.entries = {}
        except (OSError, ValueError) as err:
            _LOGGER.error("The archive index could not be read (%s); it is rebuilt from the archive files", err)
            self.entries = {}
        for path in self.dir.glob("aie-*.zip"):
            archive_id = path.stem
            if ARCHIVE_ID.fullmatch(archive_id) and archive_id not in self.entries:
                self.entries[archive_id] = {"archive_id": archive_id, "size": path.stat().st_size,
                                            "recovered": True, "created_at": None}
        for archive_id in list(self.entries):
            if not self.path(archive_id).is_file():
                _LOGGER.warning("The archive %s is listed but its file is missing", archive_id)
                self.entries[archive_id]["missing"] = True
        self._write_index()

    def _write_index(self) -> None:
        write_json(self.index_path, {"archives": self.entries})

    def path(self, archive_id: str) -> Path:
        if not ARCHIVE_ID.fullmatch(archive_id):
            raise KeyError(archive_id)
        return self.dir / f"{archive_id}.zip"

    def add(self, archive_id: str, data: bytes, entry: dict[str, Any]) -> dict[str, Any]:
        atomic_write(self.path(archive_id), data)
        self.entries[archive_id] = {**entry, "archive_id": archive_id, "size": len(data)}
        self._write_index()
        return self.entries[archive_id]

    def update(self, archive_id: str, **changes: Any) -> None:
        if archive_id in self.entries:
            self.entries[archive_id].update(changes)
            self._write_index()

    def read(self, archive_id: str) -> bytes:
        try:
            return self.path(archive_id).read_bytes()
        except (KeyError, FileNotFoundError) as err:
            raise KeyError(archive_id) from err

    def delete(self, archive_id: str) -> None:
        path = self.path(archive_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.entries.pop(archive_id, None)
        self._write_index()

    def listing(self) -> list[dict[str, Any]]:
        return sorted(self.entries.values(), key=lambda e: e.get("created_at") or "", reverse=True)


class OperationJournal:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "operations"
        self.operations: dict[str, dict[str, Any]] = {}

    def load(self) -> list[dict[str, Any]]:
        """Read every operation; the ones that were running are marked interrupted (and returned)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        interrupted = []
        for path in sorted(self.dir.glob("op-*.json")):
            try:
                op = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as err:
                _LOGGER.error("The operation record %s could not be read: %s", path.name, err)
                continue
            if not isinstance(op, dict) or not OPERATION_ID.fullmatch(str(op.get("id"))):
                continue
            self.operations[op["id"]] = op
            if op.get("status") in RUNNING:
                op["interrupted_status"] = op["status"]
                op["status"] = "interrupted"
                self.log(op, "error", f"The App stopped while this operation was {op['interrupted_status']}. "
                                      "Check the current state before continuing.")
                interrupted.append(op)
        for stale in self.dir.glob("*.tmp-*"):
            try:
                stale.unlink()
            except OSError:
                pass
        return interrupted

    def create(self, kind: str, by: str | None, **fields: Any) -> dict[str, Any]:
        op = {"id": new_id("op"), "kind": kind, "status": "running", "created_at": iso(), "updated_at": iso(),
              "by": by, "log": [], "items": {}, **fields}
        self.operations[op["id"]] = op
        self.save(op)
        return op

    def save(self, op: dict[str, Any]) -> None:
        op["updated_at"] = iso()
        write_json(self.dir / f"{op['id']}.json", op)

    def log(self, op: dict[str, Any], level: str, message: str, **extra: Any) -> None:
        op.setdefault("log", []).append({"at": iso(), "level": level, "message": message, **extra})
        if len(op["log"]) > 2000:
            op["log"] = op["log"][-2000:]
        self.save(op)
        (_LOGGER.warning if level in ("warning", "error") else _LOGGER.info)("[%s] %s", op["id"], message)

    def item(self, op: dict[str, Any], key: str, **changes: Any) -> None:
        entry = op.setdefault("items", {}).setdefault(key, {})
        entry.update(changes, updated_at=iso())
        self.save(op)

    def get(self, op_id: str) -> dict[str, Any] | None:
        return self.operations.get(op_id)

    def listing(self, limit: int = 50) -> list[dict[str, Any]]:
        ops = sorted(self.operations.values(), key=lambda o: o.get("created_at") or "", reverse=True)
        return [{k: o.get(k) for k in ("id", "kind", "status", "created_at", "updated_at", "archive_id", "summary",
                                       "interrupted_status", "title")} for o in ops[:limit]]

    def prune(self) -> None:
        ops = sorted(self.operations.values(), key=lambda o: o.get("created_at") or "", reverse=True)
        for op in ops[KEEP_OPERATIONS:]:
            if op.get("status") in (*RUNNING, "interrupted", "awaiting_confirmation"):
                continue
            try:
                (self.dir / f"{op['id']}.json").unlink()
            except OSError:
                pass
            self.operations.pop(op["id"], None)
