"""The App's own configuration: rooms, their sensors, timing, adjacent rooms. Versioned, validated, backed up.

One document, ``/data/config/config.json``::

    {"schema": 1, "revision": "<16 hex>", "saved_at": "...", "reason": "...", "by": "<user id>",
     "config": {
       "timing": {"correlation_window": 10, ...},          # global defaults (every parameter)
       "rooms": {"<area_id>": {
           "name": "Kamar Suður",                          # the area's name when last saved (display only)
           "enabled": true,
           "sensors": [{"entity_id": "binary_sensor.pir", "kind": "pir", "role": "trigger",
                        "inverted": false, "enabled": true}],
           "activity": ["event.wall_switch"],              # optional activity evidence
           "timing": {"inactivity_timeout": 900},          # only the overrides
           "adjacent": ["<area_id>"]}}}}

* rooms are keyed by the Home Assistant **area id**, which does not change when
  an area is renamed;
* every save is checked against the expected revision (two editors cannot
  overwrite each other unseen), validated strictly (an invalid configuration
  is refused with every problem listed, never repaired silently), written
  atomically (temporary file, ``os.replace``) and preceded by a backup of the
  previous document (``backups/``, bounded);
* a damaged file is never used and never overwritten: the store falls back to
  ``config.json.bak`` or the newest valid backup and reports it;
* an export is the complete document; an import is reviewed first.

Nothing here reads Smart Comfort's storage: its export can be imported through
``smart_comfort_import.py`` (reviewed, read-only).
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

from sod_engine.house import HouseSpec, RoomSpec
from sod_engine.types import (
    DEFAULT_ROLE_FOR_KIND,
    TIMING_PARAMETERS,
    OccupancyTiming,
    SensorAssignment,
    SensorKind,
    SensorRole,
)

_LOGGER = logging.getLogger(__name__)

SCHEMA = 1
EXPORT_FORMAT = "smart_occupancy_detection.config"
BACKUPS = 30
MAX_ROOMS = 100
MAX_SENSORS = 30
MAX_ACTIVITY = 10
MAX_ADJACENT = 20
MAX_NAME = 100
ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
AREA_ID = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")
BACKUP_NAME = re.compile(r"^[0-9TZ\-]{16,40}(-[a-z_]{1,32})?\.json$")

#: Timing shown first in the interface; everything else is under Advanced.
BASIC_TIMING = ("correlation_window", "inactivity_timeout", "exit_confirmation")
TIMING_INFO: dict[str, dict[str, str]] = {
    "correlation_window": {
        "label": "Occupancy confirmation",
        "help": "How long movement must last, or within how long two sensors must agree, before occupancy is "
                "confirmed.",
    },
    "inactivity_timeout": {
        "label": "Vacancy timeout",
        "help": "How long a room must be quiet before it becomes vacant (normal case).",
    },
    "exit_confirmation": {
        "label": "Fast exit timeout",
        "help": "Quiet time that is enough when there is evidence that everyone left (a door exit, a cleared "
                "presence sensor with the Confirm vacancy role, or a visit that was never confirmed).",
    },
    "dropout_grace": {
        "label": "Radar dropout grace",
        "help": "A presence sensor that clears for less than this is still counted as present.",
    },
    "trigger_stuck_after": {
        "label": "Motion sensor stuck after",
        "help": "A motion sensor active longer than this is ignored as stuck (0 = never).",
    },
    "presence_stuck_after": {
        "label": "Presence sensor stuck after",
        "help": "A presence sensor active longer than this is ignored as stuck (0 = never, the default).",
    },
    "startup_hold": {
        "label": "Start-up hold",
        "help": "After a start or an interruption of the Home Assistant connection no room is made vacant for "
                "this long.",
    },
}

#: Which domains each sensor kind may use (Smart Comfort's editor fields, presence_fields.KIND_FIELDS).
KIND_DOMAINS: dict[str, tuple[str, ...] | None] = {
    "pir": ("binary_sensor",),
    "radar": ("binary_sensor",),
    "door": ("binary_sensor",),
    "pc_activity": ("binary_sensor", "sensor", "input_boolean", "switch", "media_player", "device_tracker"),
    "bed": ("binary_sensor", "input_boolean"),
    "other": None,
}
KIND_LABELS = {"pir": "Motion (PIR)", "radar": "Presence (mmWave radar)", "door": "Door contact",
               "pc_activity": "PC activity", "bed": "Bed occupancy", "other": "Other"}
ROLE_LABELS = {"trigger": "Triggers occupancy", "maintain": "Maintains occupancy",
               "confirm_vacancy": "Maintains, and confirms vacancy when clear", "ignore": "Ignored"}


class ConfigError(Exception):
    """A request about the configuration was refused (``code`` is stable; ``problems`` lists what is wrong)."""

    def __init__(self, code: str, message: str, status: int = 400, problems: list[dict[str, str]] | None = None,
                 **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.problems = problems or []
        self.extra = extra


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def empty_config() -> dict[str, Any]:
    return {"timing": OccupancyTiming().as_dict(), "rooms": {}}


def revision_of(config: dict[str, Any]) -> str:
    text = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- validation
class _Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        self.items.append({"path": path, "message": message})


def _timing(raw: Any, path: str, problems: _Problems, *, complete: bool) -> dict[str, int]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.add(path, "must be an object")
        return {}
    out: dict[str, int] = {}
    for key in raw:
        if key not in TIMING_PARAMETERS:
            problems.add(f"{path}.{key}", "unknown timing parameter")
    for key, (default, low, high) in TIMING_PARAMETERS.items():
        value = raw.get(key)
        if value is None:
            if complete:
                out[key] = default
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            problems.add(f"{path}.{key}", "must be a whole number of seconds")
            continue
        if not low <= value <= high:
            problems.add(f"{path}.{key}", f"must be between {low} and {high} seconds")
            continue
        out[key] = value
    return out


def _sensor(raw: Any, path: str, problems: _Problems) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        problems.add(path, "must be an object")
        return None
    unknown = set(raw) - {"entity_id", "kind", "role", "inverted", "enabled"}
    if unknown:
        problems.add(path, f"unknown fields {sorted(unknown)}")
    entity_id = raw.get("entity_id")
    if not isinstance(entity_id, str) or not ENTITY_ID.match(entity_id):
        problems.add(f"{path}.entity_id", "not a valid entity id")
        return None
    kind = raw.get("kind")
    if kind not in KIND_DOMAINS:
        problems.add(f"{path}.kind", f"must be one of {', '.join(KIND_DOMAINS)}")
        return None
    domains = KIND_DOMAINS[kind]
    if domains is not None and entity_id.split(".", 1)[0] not in domains:
        problems.add(f"{path}.entity_id", f"a {KIND_LABELS[kind]} sensor must be one of: {', '.join(domains)}")
    role = raw.get("role", DEFAULT_ROLE_FOR_KIND[SensorKind(kind)].value)
    if role not in {r.value for r in SensorRole}:
        problems.add(f"{path}.role", f"must be one of {', '.join(r.value for r in SensorRole)}")
        return None
    out = {"entity_id": entity_id, "kind": kind, "role": role}
    for flag, default in (("inverted", False), ("enabled", True)):
        value = raw.get(flag, default)
        if not isinstance(value, bool):
            problems.add(f"{path}.{flag}", "must be true or false")
            value = default
        out[flag] = value
    return out


def _room(area_id: str, raw: Any, path: str, problems: _Problems) -> dict[str, Any]:
    if not isinstance(raw, dict):
        problems.add(path, "must be an object")
        return {}
    unknown = set(raw) - {"name", "enabled", "sensors", "activity", "timing", "adjacent"}
    if unknown:
        problems.add(path, f"unknown fields {sorted(unknown)}")
    name = raw.get("name") or area_id
    if not isinstance(name, str) or len(name) > MAX_NAME:
        problems.add(f"{path}.name", f"must be a text of at most {MAX_NAME} characters")
        name = area_id
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.add(f"{path}.enabled", "must be true or false")
        enabled = True
    sensors_raw = raw.get("sensors") or []
    sensors: list[dict[str, Any]] = []
    if not isinstance(sensors_raw, list) or len(sensors_raw) > MAX_SENSORS:
        problems.add(f"{path}.sensors", f"must be a list of at most {MAX_SENSORS} sensors")
        sensors_raw = []
    seen: set[str] = set()
    for i, item in enumerate(sensors_raw):
        sensor = _sensor(item, f"{path}.sensors[{i}]", problems)
        if sensor is None:
            continue
        if sensor["entity_id"] in seen:
            problems.add(f"{path}.sensors[{i}]", f"{sensor['entity_id']} is assigned twice")
            continue
        seen.add(sensor["entity_id"])
        sensors.append(sensor)
    activity_raw = raw.get("activity") or []
    activity: list[str] = []
    if not isinstance(activity_raw, list) or len(activity_raw) > MAX_ACTIVITY:
        problems.add(f"{path}.activity", f"must be a list of at most {MAX_ACTIVITY} entities")
        activity_raw = []
    for i, entity_id in enumerate(activity_raw):
        if not isinstance(entity_id, str) or not ENTITY_ID.match(entity_id):
            problems.add(f"{path}.activity[{i}]", "not a valid entity id")
        elif entity_id in seen:
            problems.add(f"{path}.activity[{i}]", f"{entity_id} is already a sensor of this room")
        elif entity_id in activity:
            problems.add(f"{path}.activity[{i}]", f"{entity_id} is listed twice")
        else:
            activity.append(entity_id)
    adjacent_raw = raw.get("adjacent") or []
    adjacent: list[str] = []
    if not isinstance(adjacent_raw, list) or len(adjacent_raw) > MAX_ADJACENT:
        problems.add(f"{path}.adjacent", f"must be a list of at most {MAX_ADJACENT} rooms")
        adjacent_raw = []
    for i, other in enumerate(adjacent_raw):
        if not isinstance(other, str) or not AREA_ID.match(other) or other == area_id:
            problems.add(f"{path}.adjacent[{i}]", "not another room")
        elif other not in adjacent:
            adjacent.append(other)
    timing = _timing(raw.get("timing"), f"{path}.timing", problems, complete=False)
    if enabled:
        usable = [s for s in sensors if s["enabled"] and s["role"] != "ignore" and s["kind"] != "door"]
        if not usable:
            problems.add(f"{path}.sensors", "an enabled room needs at least one enabled sensor that can detect "
                                            "presence (a door contact alone cannot); add one or disable the room")
    return {"name": name, "enabled": enabled, "sensors": sensors, "activity": activity, "timing": timing,
            "adjacent": adjacent}


def validate_config(raw: Any) -> dict[str, Any]:
    """The configuration in its canonical form; raises ``ConfigError`` listing every problem."""
    problems = _Problems()
    if not isinstance(raw, dict):
        raise ConfigError("invalid", "The configuration must be an object", problems=[{"path": "", "message":
                                                                                         "must be an object"}])
    unknown = set(raw) - {"timing", "rooms"}
    if unknown:
        problems.add("", f"unknown fields {sorted(unknown)}")
    timing = _timing(raw.get("timing"), "timing", problems, complete=True)
    rooms_raw = raw.get("rooms") or {}
    rooms: dict[str, Any] = {}
    if not isinstance(rooms_raw, dict) or len(rooms_raw) > MAX_ROOMS:
        problems.add("rooms", f"must be an object with at most {MAX_ROOMS} rooms")
        rooms_raw = {}
    for area_id, room in rooms_raw.items():
        if not isinstance(area_id, str) or not AREA_ID.match(area_id):
            problems.add(f"rooms.{area_id}", "not a valid area id")
            continue
        rooms[area_id] = _room(area_id, room, f"rooms.{area_id}", problems)
    for area_id, room in rooms.items():
        for other in room.get("adjacent", []):
            if other not in rooms:
                problems.add(f"rooms.{area_id}.adjacent", f"{other} is not a configured room")
    if problems.items:
        raise ConfigError("invalid", "The configuration is not valid", problems=problems.items)
    return {"timing": timing, "rooms": rooms}


def warnings_of(config: dict[str, Any]) -> list[dict[str, str]]:
    """Things that are allowed but probably not intended (shown in the review, never blocking)."""
    out = []

    def check(path: str, timing: OccupancyTiming) -> None:
        if timing.exit_confirmation > timing.inactivity_timeout:
            out.append({"path": path, "message": "The fast exit timeout is longer than the vacancy timeout, so "
                                                 "exit evidence would slow vacancy down."})

    base = OccupancyTiming.from_dict(config.get("timing"))
    check("timing", base)
    for area_id, room in (config.get("rooms") or {}).items():
        check(f"rooms.{area_id}.timing", base.with_overrides(room.get("timing") or {}))
        kinds = {s["kind"] for s in room.get("sensors", []) if s.get("enabled", True) and s.get("role") != "ignore"}
        if room.get("enabled", True) and kinds and kinds <= {"pir", "door"} and "pir" in kinds:
            out.append({"path": f"rooms.{area_id}", "message": "Only motion sensors: someone sitting still will "
                                                               "be counted as gone after the vacancy timeout."})
    return out


# --------------------------------------------------------------------------- engine view
def house_spec(config: dict[str, Any]) -> HouseSpec:
    """The engines' configuration: every enabled room, its sensors, effective timing and enabled neighbours."""
    base = OccupancyTiming.from_dict(config.get("timing"))
    enabled = {a for a, r in (config.get("rooms") or {}).items() if r.get("enabled", True)}
    rooms = []
    for area_id, room in (config.get("rooms") or {}).items():
        if area_id not in enabled:
            continue
        rooms.append(RoomSpec(
            area_id,
            tuple(SensorAssignment.from_dict(s) for s in room.get("sensors", [])),
            base.with_overrides(room.get("timing") or {}),
            tuple(a for a in room.get("adjacent", []) if a in enabled),
        ))
    return HouseSpec(tuple(rooms))


def activity_entities(config: dict[str, Any]) -> dict[str, list[str]]:
    """Activity entity -> the enabled rooms that use it."""
    out: dict[str, list[str]] = {}
    for area_id, room in (config.get("rooms") or {}).items():
        if room.get("enabled", True):
            for entity_id in room.get("activity", []):
                out.setdefault(entity_id, []).append(area_id)
    return out


# --------------------------------------------------------------------------- storage
def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_document(path: Path) -> dict[str, Any]:
    """A stored document, validated; raises ``ValueError`` (or ``OSError``) if it cannot be used."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError(f"unsupported schema {data.get('schema') if isinstance(data, dict) else None!r}")
    config = validate_config(data.get("config"))
    if data.get("revision") != revision_of(config):
        raise ValueError("the revision does not match the content")
    return {**data, "config": config}


class ConfigStore:
    """The configuration document with its revision, backups and recovery."""

    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir / "config"
        self.path = self.dir / "config.json"
        self.backup_dir = self.dir / "backups"
        self.config: dict[str, Any] = empty_config()
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
        _LOGGER.error("No usable configuration was found (%s); starting without rooms. The damaged files are kept.",
                      "; ".join(errors))
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
                                       "import). Reload and review your change again.", 409,
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
            self._backup(reason)  # also the configuration recovered from a copy: it must exist somewhere
        document = {"schema": SCHEMA, "revision": revision, "saved_at": iso(now), "reason": reason[:100], "by": by,
                    "config": clean}
        if self.path.exists():
            try:
                os.replace(self.path, self.path.with_name("config.json.bak"))
            except OSError as err:
                _LOGGER.warning("Could not keep the previous configuration as config.json.bak: %s", err)
        _atomic_write(self.path, document)
        self.config, self.revision, self.saved_at, self.recovery = clean, revision, document["saved_at"], None
        _LOGGER.info("Configuration saved (%s, revision %s, %d rooms)", reason, revision, len(clean["rooms"]))
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
                       "backed_up_at": iso(utcnow()), "config": self.config})
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
                rooms = len((data.get("config") or {}).get("rooms") or {})
                out.append({"name": path.name, "backed_up_at": data.get("backed_up_at"), "rooms": rooms,
                            "revision": data.get("revision")})
            except (OSError, ValueError):
                out.append({"name": path.name, "backed_up_at": None, "rooms": None, "revision": None,
                            "damaged": True})
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
            raise ConfigError("invalid_import", "This is not a Smart Occupancy Detection configuration export")
        if document.get("version") != SCHEMA:
            raise ConfigError("invalid_import", f"Unsupported export version {document.get('version')!r}")
        return validate_config(document.get("config"))


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What changes between two configurations, room by room (for every review)."""
    changes: list[dict[str, Any]] = []
    if before.get("timing") != after.get("timing"):
        keys = [k for k in TIMING_PARAMETERS if (before.get("timing") or {}).get(k) != (after.get("timing") or {}).get(k)]
        changes.append({"type": "global_timing", "keys": keys})
    rooms_before, rooms_after = before.get("rooms") or {}, after.get("rooms") or {}
    for area_id in sorted(set(rooms_before) | set(rooms_after)):
        old, new = rooms_before.get(area_id), rooms_after.get(area_id)
        if old is None:
            changes.append({"type": "room_added", "area_id": area_id, "name": new.get("name")})
        elif new is None:
            changes.append({"type": "room_removed", "area_id": area_id, "name": old.get("name")})
        elif old != new:
            fields = [k for k in ("enabled", "sensors", "activity", "timing", "adjacent", "name") if old.get(k) != new.get(k)]
            changes.append({"type": "room_changed", "area_id": area_id, "name": new.get("name"), "fields": fields})
    return changes
