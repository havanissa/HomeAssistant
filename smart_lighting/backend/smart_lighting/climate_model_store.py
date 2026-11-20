"""Climate thermal learning (Phase A7.1): storage of the learned room models.

Layout (``/data/climate/models``)::

    <area>.json                     the current model of a room (``climate_thermal_model``)
    history/<area>@<stamp>.json     the previous versions of each room (the latest 3)
    archive/<area>@<stamp>-<why>.json  models reset by a person or of removed rooms (the latest 20)
    <area>.corrupt-<stamp>.json     an unreadable model, moved aside when it was recovered from its history
    state.json                      per room: the last training attempt (start, end, result, diagnostic)

Writes are atomic (temporary file, fsync, rename). A model written by a newer App is never overwritten: the
room's learning stops with a notice until the App is updated or the model is reset. Nothing here touches the
climate configuration or the observations.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import time
from typing import Any

from . import climate_thermal_model as tm

_LOGGER = logging.getLogger(__name__)

HISTORY_KEEP = 3
ARCHIVE_KEEP = 20
STATE_FORMAT = "smart_comfort.thermal_learning_state"


def _stamp() -> str:
    now = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now)) + f"{int(now * 1000) % 1000:03d}Z"


def _atomic_write(path: Path, data: bytes) -> None:
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


def file_name(area_id: str) -> str:
    """A file name for an area id (Home Assistant area ids are word characters and dashes; long ones are hashed)."""
    if len(area_id) <= 100:
        return area_id
    return f"{area_id[:80]}-{hashlib.sha256(area_id.encode()).hexdigest()[:12]}"


class ModelStore:
    """The models of every room. Blocking methods; the service calls them in a worker thread."""

    def __init__(self, directory: Path | None) -> None:
        self.dir = directory
        self.docs: dict[str, dict[str, Any]] = {}
        self.problems: dict[str, dict[str, str]] = {}   # area -> {code, message}
        self.protected: set[str] = set()                 # newer-schema models: never overwritten
        self.state: dict[str, dict[str, Any]] = {}
        self.notices: list[dict[str, str]] = []
        self.error: str | None = None if directory else "The App has no data folder"

    @property
    def writable(self) -> bool:
        return self.dir is not None and self.error is None

    def path(self, area_id: str) -> Path:
        assert self.dir is not None
        return self.dir / f"{file_name(area_id)}.json"

    # ---------------------------------------------------------------- loading
    def load(self) -> None:
        if self.dir is None:
            return
        try:
            for sub in (self.dir, self.dir / "history", self.dir / "archive"):
                sub.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self.error = f"The model folder cannot be created ({err})"
            _LOGGER.error("Thermal models are not stored: %s", err)
            return
        for leftover in [*self.dir.glob(".*.tmp"), *(self.dir / "history").glob(".*.tmp")]:
            with contextlib.suppress(OSError):
                leftover.unlink()
        self._load_state()
        for path in sorted(self.dir.glob("*.json")):
            if path.name == "state.json" or ".corrupt-" in path.name:
                continue
            self._load_one(path)
        self._refresh_stats()
        # A training attempt that started but never finished: the App stopped during the fit.
        for area, attempt in self.state.items():
            if attempt.get("started_at") and not attempt.get("finished_at"):
                attempt.update(finished_at=attempt["started_at"], result="interrupted",
                               error="The App stopped while this room's model was being trained; the last saved "
                                     "model (if any) is kept")

    def _load_state(self) -> None:
        path = self.dir / "state.json"  # type: ignore[operator]
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("The thermal learning state could not be read (%s); it starts again", err)
            return
        rooms = data.get("rooms") if isinstance(data, dict) else None
        if isinstance(rooms, dict):
            self.state = {a: v for a, v in rooms.items() if isinstance(a, str) and isinstance(v, dict)}

    def _read(self, path: Path) -> dict[str, Any]:
        doc = json.loads(path.read_text(encoding="utf-8"))
        tm.check_document(doc)
        return doc

    def _load_one(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            self._recover(path, f"{type(err).__name__}: {err}")
            return
        area = raw.get("area_id") if isinstance(raw, dict) else None
        try:
            tm.check_document(raw)
        except tm.ModelError as err:
            if err.code == "newer_schema" and isinstance(area, str):
                self.protected.add(area)
                self.problems[area] = {"code": "newer_schema", "message": str(err)}
                _LOGGER.warning("Thermal model of %s not loaded: %s", area, err)
                return
            self._recover(path, str(err))
            return
        if path.stem != file_name(raw["area_id"]):
            _LOGGER.warning("Ignoring %s: it holds the model of %s", path.name, raw["area_id"])
            return
        self.docs[raw["area_id"]] = raw

    def _recover(self, path: Path, reason: str) -> None:
        stem = path.stem
        aside = path.with_name(f"{stem}.corrupt-{_stamp()}.json")
        with contextlib.suppress(OSError):
            os.replace(path, aside)
        _LOGGER.error("The thermal model %s could not be read (%s); moved it to %s", path.name, reason, aside.name)
        for older in sorted((self.dir / "history").glob(f"{stem}@*.json"), reverse=True):  # type: ignore[operator]
            try:
                doc = self._read(older)
            except (OSError, ValueError, tm.ModelError):
                continue
            if file_name(doc["area_id"]) != stem:
                continue
            self.docs[doc["area_id"]] = doc
            with contextlib.suppress(OSError):
                _atomic_write(path, json.dumps(doc, ensure_ascii=False).encode())
            self.notices.append({"code": "model_recovered", "area_id": doc["area_id"],
                                 "message": f"The stored thermal model of {doc['area_id']} was damaged; the previous "
                                            f"version (trained {doc['trained_at']}) was restored. The damaged file was "
                                            f"kept as {aside.name}."})
            return
        self.notices.append({"code": "model_damaged", "area_id": stem,
                             "message": f"A stored thermal model ({path.name}) was damaged and no earlier version could "
                                        f"be read; the room learns again. The damaged file was kept as {aside.name}."})

    # ---------------------------------------------------------------- writing
    def save(self, doc: dict[str, Any]) -> None:
        if not self.writable:
            raise OSError(self.error or "not writable")
        tm.check_document(doc)
        area = doc["area_id"]
        if area in self.protected:
            raise PermissionError("A newer App's model is stored for this room")
        path = self.path(area)
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
        if path.exists():
            # Copied, not moved: the current model stays in place until the new one has replaced it atomically.
            hist = self.dir / "history" / f"{path.stem}@{_stamp()}.json"  # type: ignore[operator]
            with contextlib.suppress(OSError):
                shutil.copyfile(path, hist)
        _atomic_write(path, data)
        olds = sorted((self.dir / "history").glob(f"{path.stem}@*.json"), reverse=True)  # type: ignore[operator]
        for old in olds[HISTORY_KEEP:]:
            with contextlib.suppress(OSError):
                old.unlink()
        self.docs[area] = doc
        self.problems.pop(area, None)
        self._refresh_stats()

    def archive(self, area_id: str, why: str) -> bool:
        """Move a room's model (and its history) aside: it is no longer used. Observations are not touched."""
        if not self.writable:
            return False
        path = self.path(area_id)
        moved = False
        if path.exists():
            with contextlib.suppress(OSError):
                os.replace(path, self.dir / "archive" / f"{path.stem}@{_stamp()}-{why}.json")  # type: ignore[operator]
                moved = True
        for old in (self.dir / "history").glob(f"{path.stem}@*.json"):  # type: ignore[operator]
            with contextlib.suppress(OSError):
                old.unlink()
        for old in sorted((self.dir / "archive").glob("*.json"), reverse=True)[ARCHIVE_KEEP:]:  # type: ignore[operator]
            with contextlib.suppress(OSError):
                old.unlink()
        self.docs.pop(area_id, None)
        self.problems.pop(area_id, None)
        self.protected.discard(area_id)
        self.state.pop(area_id, None)
        self._refresh_stats()
        return moved

    def save_state(self) -> None:
        if not self.writable:
            return
        data = {"format": STATE_FORMAT, "version": 1, "rooms": self.state}
        try:
            _atomic_write(self.dir / "state.json", json.dumps(data, ensure_ascii=False).encode())  # type: ignore[operator]
        except OSError as err:
            _LOGGER.warning("Could not store the thermal learning state: %s", err)

    def _refresh_stats(self) -> None:
        """The folder's size, measured where the store is written (a worker thread), never on a status read."""
        size = 0
        if self.dir is not None and self.dir.is_dir():
            for p in self.dir.rglob("*.json"):
                with contextlib.suppress(OSError):
                    size += p.stat().st_size
        self._bytes = size

    def stats(self) -> dict[str, Any]:
        return {"models": len(self.docs), "bytes": getattr(self, "_bytes", 0), "writable": self.writable,
                "error": self.error}
