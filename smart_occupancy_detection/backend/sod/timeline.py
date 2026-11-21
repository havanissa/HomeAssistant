"""A bounded, persisted timeline of occupancy decisions and sensor health (the room pages' "Recent detection").

Only meaningful moments are kept: state transitions (with the reason and the
sensor that caused them), a sensor becoming unavailable, stuck or usable again,
configuration changes, and start-up / connection events. Repeated sensor values
are never stored. At most :data:`SIZE` entries for the whole installation;
written to ``/data/state/timeline.json`` at most every :data:`SAVE_DELAY`
seconds and when the App stops.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import json
import logging
import os
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SIZE = 1000
SAVE_DELAY = 30.0
SCHEMA = 1


class Timeline:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.entries: deque[dict[str, Any]] = deque(maxlen=SIZE)
        self.seq = 0
        self.dirty = False

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("entries"), list):
                raise ValueError("unexpected content")
        except (OSError, ValueError) as err:
            _LOGGER.warning("The stored timeline is not usable (%s); starting a new one", err)
            return
        for entry in data["entries"][-SIZE:]:
            if isinstance(entry, dict) and isinstance(entry.get("seq"), int):
                self.entries.append(entry)
        self.seq = max((e["seq"] for e in self.entries), default=0)

    def add(self, kind: str, at: datetime, area_id: str | None = None, **data: Any) -> dict[str, Any]:
        self.seq += 1
        entry = {"seq": self.seq, "at": at.astimezone(UTC).isoformat(timespec="seconds"), "kind": kind,
                 "area_id": area_id, **{k: v for k, v in data.items() if v is not None}}
        self.entries.append(entry)
        self.dirty = True
        return entry

    def recent(self, area_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first; for a room, its own entries and the installation-wide ones (start-up, connection)."""
        out = []
        for entry in reversed(self.entries):
            if area_id is None or entry.get("area_id") in (area_id, None):
                out.append(entry)
                if len(out) >= limit:
                    break
        return out

    def snapshot(self) -> str | None:
        """The document to write (taken on the event loop; entries added later make it dirty again)."""
        if self.path is None or not self.dirty:
            return None
        self.dirty = False
        return json.dumps({"schema": SCHEMA, "entries": list(self.entries)}, ensure_ascii=False,
                          separators=(",", ":"))

    def write(self, text: str | None) -> None:
        if text is None or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self) -> None:
        self.write(self.snapshot())
