"""Read-only access to Home Assistant's configuration folder (mapped read-only at /homeassistant).

* ``blueprints/automation/<path>.yaml`` - the blueprint files that archived automations use, so that an archive
  carries the definitions it needs (Home Assistant has no API that returns a blueprint file).
* ``automations.yaml`` - parsed with every custom tag kept as a marker, **only** to find what the editor
  endpoint cannot show: automations without an id, ids that occur more than once, ids that are not strings.
* For the usage overview of helpers (searched as text, in memory, never stored or archived):
  the YAML files of the configuration (:meth:`HaConfigFolder.yaml_texts`; never ``secrets.yaml``), and the
  options of the config-entry helpers (templates, groups, ...) from ``.storage/core.config_entries``
  (:meth:`HaConfigFolder.helper_entry_options`) - only the entries Home Assistant lists as helpers; every other
  config entry (integration credentials) is skipped.

Nothing is ever written (the mapping is read-only as well), and when the folder is not mapped the App works
without it and says what is then not known.
"""

from __future__ import annotations

from collections import Counter
import json
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from .ha import BLUEPRINT_PATH
from .yamlio import Tagged, find_tags, json_compatible, load_tolerant

_LOGGER = logging.getLogger(__name__)
MAX_BLUEPRINT_BYTES = 1024 * 1024
MAX_YAML_FILES = 1000
MAX_YAML_BYTES = 2 * 1024 * 1024
MAX_ENTRIES_BYTES = 32 * 1024 * 1024
#: Folders whose files are not Home Assistant configuration (or are searched another way).
SKIP_DIRS = {".storage", "blueprints", "custom_components", "deps", "www", "tts", "backups", ".cloud", ".git",
             "node_modules", "image", "media", "__pycache__", ".esphome", "share", "ssl"}
#: Covered by the App's own readers (automations, scripts, scenes) or never read (secrets).
SKIP_FILES = {"secrets.yaml", "automations.yaml", "scripts.yaml", "scenes.yaml"}


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
    #: id -> configuration as written in the file (first occurrence), only for entries that are plain JSON values
    #: without tags: exactly what Home Assistant's editor endpoint returns for them. Used for listing; every
    #: export is verified against the editor endpoint itself.
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)

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
                if value not in scan.configs and json_compatible(entry) and not find_tags(entry):
                    scan.configs[value] = entry
            else:
                shown = str(value.value if isinstance(value, Tagged) else value)
                scan.non_string_ids.append(shown)
                scan.ids[shown] += 1
        return scan

    def yaml_texts(self) -> list[tuple[str, str]]:
        """The text of every YAML file of the configuration (relative path, text): read-only, for the usage
        search. ``secrets.yaml`` and the folders in :data:`SKIP_DIRS` are never read; symbolic links are not
        followed."""
        if not self.root.is_dir():
            return []
        out: list[tuple[str, str]] = []
        stack = [self.root]
        while stack and len(out) < MAX_YAML_FILES:
            folder = stack.pop()
            try:
                children = sorted(folder.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_symlink():
                    continue
                if child.is_dir():
                    if child.name not in SKIP_DIRS and not child.name.startswith("."):
                        stack.append(child)
                    continue
                if child.suffix not in (".yaml", ".yml") or child.name in SKIP_FILES:
                    continue
                if folder == self.root and child.name in SKIP_FILES:
                    continue
                try:
                    if child.stat().st_size > MAX_YAML_BYTES:
                        continue
                    text = child.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                out.append((str(child.relative_to(self.root)), text))
                if len(out) >= MAX_YAML_FILES:
                    break
        return out

    def helper_entry_options(self, entry_ids: set[str]) -> dict[str, Any]:
        """The options (and data) of the given config entries - Home Assistant's helpers such as templates and
        groups - for the usage search. Every other entry of the file is skipped unread."""
        path = self.root / ".storage" / "core.config_entries"
        if not entry_ids or not path.is_file() or path.is_symlink():
            return {}
        try:
            if path.stat().st_size > MAX_ENTRIES_BYTES:
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError) as err:
            _LOGGER.warning("The helper options could not be read: %s", err)
            return {}
        entries = ((data or {}).get("data") or {}).get("entries") if isinstance(data, dict) else None
        out: dict[str, Any] = {}
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("entry_id") in entry_ids:
                out[entry["entry_id"]] = {"options": entry.get("options") or {}, "data": entry.get("data") or {}}
        return out
