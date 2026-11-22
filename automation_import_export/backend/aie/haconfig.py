"""Read-only access to two things in Home Assistant's configuration folder (mapped read-only at /homeassistant).

* ``blueprints/automation/<path>.yaml`` - the blueprint files that archived automations use, so that an archive
  carries the definitions it needs (Home Assistant has no API that returns a blueprint file).
* ``automations.yaml`` - parsed with every custom tag kept as a marker, **only** to find what the editor
  endpoint cannot show: automations without an id, ids that occur more than once, ids that are not strings.

Nothing else in the folder is read, nothing is ever written (the mapping is read-only as well), and when the
folder is not mapped the App works without it and says what is then not known.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from .ha import BLUEPRINT_PATH
from .yamlio import Tagged, load_tolerant

_LOGGER = logging.getLogger(__name__)
MAX_BLUEPRINT_BYTES = 1024 * 1024


def readable_blueprint_path(relative: Any) -> bool:
    return (isinstance(relative, str) and len(relative) <= 255 and bool(BLUEPRINT_PATH.fullmatch(relative))
            and ".." not in relative.split("/"))


@dataclass
class FileScan:
    available: bool
    error: str | None = None
    ids: Counter[str] = field(default_factory=Counter)
    without_id: int = 0
    non_string_ids: list[str] = field(default_factory=list)
    entries: int = 0

    def public(self) -> dict[str, Any]:
        return {"available": self.available, "error": self.error, "entries": self.entries,
                "without_id": self.without_id, "duplicate_ids": sorted(k for k, n in self.ids.items() if n > 1),
                "non_string_ids": self.non_string_ids}


class HaConfigFolder:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def available(self) -> bool:
        return (self.root / "configuration.yaml").is_file() or (self.root / "automations.yaml").is_file()

    def _inside(self, path: Path, base: Path) -> bool:
        try:
            path.resolve(strict=True).relative_to(base.resolve(strict=True))
        except (OSError, ValueError):
            return False
        return True

    def read_blueprint(self, relative: str) -> bytes | None:
        """The blueprint file's bytes, or None when it cannot be read (missing folder, missing file, odd path)."""
        if not readable_blueprint_path(relative):
            return None
        base = self.root / "blueprints" / "automation"
        path = base / relative
        if not self._inside(path, base) or path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_BLUEPRINT_BYTES:
                return None
            return path.read_bytes()
        except OSError as err:
            _LOGGER.warning("Could not read the blueprint %s: %s", relative, err)
            return None

    def scan_automations_file(self) -> FileScan:
        path = self.root / "automations.yaml"
        if not self.root.is_dir():
            return FileScan(False, "Home Assistant's configuration folder is not mapped into the App")
        if not path.is_file():
            return FileScan(True, None)
        try:
            data = load_tolerant(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as err:
            return FileScan(True, f"automations.yaml could not be read: {err}")
        except Exception as err:  # noqa: BLE001 - a YAML error in the user's file
            return FileScan(True, f"automations.yaml could not be parsed: {type(err).__name__}")
        scan = FileScan(True)
        if data is None:
            return scan
        if not isinstance(data, list):
            scan.error = "automations.yaml is not a list of automations"
            return scan
        for entry in data:
            scan.entries += 1
            if not isinstance(entry, dict) or "id" not in entry:
                scan.without_id += 1
                continue
            value = entry["id"]
            if isinstance(value, str):
                scan.ids[value] += 1
            else:
                shown = str(value.value if isinstance(value, Tagged) else value)
                scan.non_string_ids.append(shown)
                scan.ids[shown] += 1
        return scan
