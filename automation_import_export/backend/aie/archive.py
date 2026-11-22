"""The archive: a ZIP file with the native YAML of every automation, its registry metadata, the definitions of
its labels, categories and areas, its dependencies, the blueprint files it needs, and a versioned manifest with
checksums of every file.

Layout (format version 1)::

    manifest.json                     format, ids, Home Assistant version, inventory, associations, checksums
    README.txt                        what the archive is and that it may contain private data
    automations.yaml                  every exported configuration, as one Home Assistant automation list
    automations/0001_<id>.yaml        every automation on its own (the exact configuration)
    data/automations.json             per automation: configuration, registry entry, state, metadata, references
    data/labels.json                  definitions of the labels used (id, name, icon, colour, description)
    data/categories.json              definitions of the categories used, by scope
    data/areas.json, data/floors.json definitions of the areas used (and their floors)
    data/blueprints.json              the blueprints used: metadata, whether the file is included, checksum
    blueprints/automation/<path>      the blueprint files (when Home Assistant's configuration folder is readable)

Reading never extracts to disk: :class:`SafeZip` checks names (no absolute paths, no ``..``, no backslashes,
no symlinks, no duplicates), sizes (archive, per file, total, compression ratio, number of files) and refuses
encrypted members, before any member is read.
"""

from __future__ import annotations

import io
import json
import re
import stat
from typing import Any
import zipfile

from . import yamlio
from .canonical import canonical, differences, digest, same, sha256_bytes
from .const import APP_NAME, APP_VERSION, ARCHIVE_FORMAT, ARCHIVE_VERSION
from .inventory import REGISTRY_COMPARED, Item, Snapshot, registry_digest

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_MEMBER_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_RATIO = 200
_NAME = re.compile(r"[A-Za-z0-9._\-]+(/[A-Za-z0-9._\-]+)*")
_SLUG = re.compile(r"[^a-z0-9_\-]+")

README = """This archive was created by the {app} App ({version}) for Home Assistant.

It contains Home Assistant automations exactly as they were configured, with their labels,
categories, areas, entity ids and enabled states, so that they can be restored by the App.

automations.yaml lists every exported automation in Home Assistant's own format.
manifest.json describes the archive and holds a checksum of every file.

Treat this file as private: automation configurations can contain personal information,
addresses, phone numbers, tokens or passwords. It was never uploaded anywhere by the App.
"""


class ArchiveError(Exception):
    def __init__(self, message: str, code: str = "invalid_archive") -> None:
        super().__init__(message)
        self.code = code


def safe_name(name: str) -> bool:
    return (0 < len(name) <= 255 and bool(_NAME.fullmatch(name))
            and not any(part in ("", ".", "..") for part in name.split("/")))


class SafeZip:
    """A ZIP archive read from memory with every limit checked first."""

    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ArchiveError(f"The archive is larger than {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB", "too_large")
        try:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        except (zipfile.BadZipFile, ValueError, OSError) as err:
            raise ArchiveError(f"This is not a readable ZIP archive ({err})", "not_zip") from err
        infos = self.zip.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(f"The archive has more than {MAX_MEMBERS} files", "too_many_files")
        self.members: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            name = info.filename
            if name.endswith("/"):
                if not safe_name(name.rstrip("/")):
                    raise ArchiveError(f"Unsafe folder name in the archive: {name[:80]!r}", "unsafe_path")
                continue
            if not safe_name(name):
                raise ArchiveError(f"Unsafe file name in the archive: {name[:80]!r}", "unsafe_path")
            if name in self.members:
                raise ArchiveError(f"The file {name} occurs twice in the archive", "duplicate_member")
            # Unix file type in the upper bits (zip writers often store permissions only: type 0 = a plain file)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if kind not in (0, stat.S_IFREG):
                raise ArchiveError(f"The archive contains a link or special file: {name}", "unsafe_member")
            if info.flag_bits & 0x1:
                raise ArchiveError(f"The archive contains an encrypted file: {name}", "encrypted")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ArchiveError(f"{name} is larger than {MAX_MEMBER_BYTES // (1024 * 1024)} MB", "too_large")
            if info.file_size > 1024 * 1024 and info.file_size > MAX_RATIO * max(info.compress_size, 1):
                raise ArchiveError(f"{name} is compressed suspiciously strongly", "zip_bomb")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ArchiveError("The archive expands to more than the allowed size", "too_large")
            self.members[name] = info

    def read(self, name: str) -> bytes:
        info = self.members.get(name)
        if info is None:
            raise ArchiveError(f"The archive has no {name}", "missing_file")
        try:
            with self.zip.open(info) as handle:
                data = handle.read(info.file_size + 1)
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zipfile.LargeZipFile) as err:
            raise ArchiveError(f"{name} cannot be read ({err})", "damaged") from err
        if len(data) != info.file_size:
            raise ArchiveError(f"{name} is not the size the archive says", "damaged")
        return data

    def json(self, name: str) -> Any:
        try:
            return json.loads(self.read(name).decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as err:
            raise ArchiveError(f"{name} is not valid JSON ({err})", "damaged") from err


def _slug(value: str) -> str:
    return _SLUG.sub("_", value.lower()).strip("_")[:60] or "automation"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False, allow_nan=False) + "\n").encode("utf-8")


def record_of(item: Item, snap: Snapshot) -> dict[str, Any]:
    """Everything this archive keeps about one automation."""
    reg = item.registry
    metadata: dict[str, Any] = {"labels": [], "categories": {}, "area": None, "floor": None}
    if reg is not None:
        metadata["labels"] = [snap.labels[i] for i in reg.get("labels") or [] if i in snap.labels]
        metadata["categories"] = {s: snap.categories.get(s, {}).get(i) for s, i in (reg.get("categories") or {}).items()
                                  if i in snap.categories.get(s, {})}
        if reg.get("area_id") in snap.areas:
            metadata["area"] = snap.areas[reg["area_id"]]
            floor_id = metadata["area"].get("floor_id")
            metadata["floor"] = snap.floors.get(floor_id) if floor_id else None
    st = snap.states.get(item.entity_id) if item.entity_id else None
    if item.source == "ui" and item.config is not None and reg is not None:
        restorable = "full"
    elif item.source == "yaml_file" and item.config is not None:
        restorable = "yaml_only"
    elif item.config is not None:
        restorable = "configuration_only"
    else:
        restorable = "none"
    return {
        "key": item.key, "config_id": item.config_id, "entity_id": item.entity_id, "alias": item.alias,
        "source": item.source, "owner": item.owner, "protected": item.protected,
        "config_origin": item.config_origin, "config": item.config, "registry": reg,
        "state": {"state": st.get("state"), "attributes": item.attributes} if st else None,
        "metadata": metadata, "references": item.references, "problems": item.problems,
        "restorable": restorable,
    }


def build(*, archive_id: str, created_at: str, purpose: str, selection: dict[str, Any], items: list[Item],
          snap: Snapshot, installation: dict[str, Any], blueprint_files: dict[str, bytes],
          extra_warnings: list[dict[str, Any]] | None = None) -> tuple[bytes, dict[str, Any]]:
    """The archive's bytes and its manifest."""
    files: dict[str, bytes] = {}
    records = [record_of(item, snap) for item in items]
    inventory, associations = [], {}
    combined = []
    used_names: set[str] = set()
    for index, record in enumerate(records, start=1):
        name = None
        if record["config"] is not None:
            name = f"automations/{index:04d}_{_slug(record['config_id'] or record['entity_id'] or 'automation')}.yaml"
            while name in used_names:
                name = name[:-5] + "_x.yaml"
            used_names.add(name)
            files[name] = yamlio.dump(record["config"]).encode("utf-8")
            combined.append(record["config"])
        reg = record["registry"] or {}
        associations[record["key"]] = {
            "entity_id": record["entity_id"], "labels": reg.get("labels") or [],
            "categories": reg.get("categories") or {}, "area_id": reg.get("area_id"),
            "name": reg.get("name"), "icon": reg.get("icon"), "aliases": reg.get("aliases") or [],
            "disabled_by": reg.get("disabled_by"), "hidden_by": reg.get("hidden_by"),
            "state": (record["state"] or {}).get("state"),
        }
        inventory.append({
            "key": record["key"], "config_id": record["config_id"], "entity_id": record["entity_id"],
            "alias": record["alias"], "source": record["source"], "owner": record["owner"],
            "protected": record["protected"], "restorable": record["restorable"], "file": name,
            "config_sha256": digest(record["config"]) if record["config"] is not None else None,
            "registry_sha256": registry_digest(record["registry"]),
            "blueprint": (record["references"].get("blueprint") or {}).get("path"),
            "problems": [p["code"] for p in record["problems"] if p["level"] in ("error", "warning")],
            "complete": record["restorable"] in ("full", "yaml_only")
            and not any(p["level"] == "error" for p in record["problems"]),
        })
    labels: dict[str, Any] = {}
    categories: dict[str, dict[str, Any]] = {}
    areas: dict[str, Any] = {}
    floors: dict[str, Any] = {}
    for record in records:
        meta = record["metadata"]
        for label in meta["labels"]:
            labels[label["label_id"]] = label
        for scope, category in meta["categories"].items():
            categories.setdefault(scope, {})[category["category_id"]] = category
        if meta["area"]:
            areas[meta["area"]["area_id"]] = meta["area"]
        if meta["floor"]:
            floors[meta["floor"]["floor_id"]] = meta["floor"]
    blueprints: dict[str, Any] = {}
    for record in records:
        path = (record["references"].get("blueprint") or {}).get("path")
        if not path or path in blueprints:
            continue
        content = blueprint_files.get(path)
        blueprints[path] = {"metadata": (snap.blueprints.get(path) or {}).get("metadata"),
                            "installed": "metadata" in (snap.blueprints.get(path) or {}),
                            "included": content is not None,
                            "sha256": sha256_bytes(content) if content is not None else None,
                            "file": f"blueprints/automation/{path}" if content is not None else None}
        if content is not None:
            files[f"blueprints/automation/{path}"] = content
    files["automations.yaml"] = yamlio.dump(combined).encode("utf-8") if combined else b"[]\n"
    files["data/automations.json"] = _json_bytes(records)
    files["data/labels.json"] = _json_bytes(labels)
    files["data/categories.json"] = _json_bytes(categories)
    files["data/areas.json"] = _json_bytes(areas)
    files["data/floors.json"] = _json_bytes(floors)
    files["data/blueprints.json"] = _json_bytes(blueprints)
    files["README.txt"] = README.format(app=APP_NAME, version=APP_VERSION).encode("utf-8")

    warnings = list(extra_warnings or [])
    for path, info in blueprints.items():
        if not info["included"]:
            warnings.append({"level": "warning", "code": "blueprint_not_included",
                             "message": f"The blueprint {path} is not included (Home Assistant's configuration "
                                        "folder is not readable or the file is missing). Restoring needs it "
                                        "installed.", "items": [path]})
    for record in records:
        for problem in record["problems"]:
            if problem["level"] in ("error", "warning"):
                warnings.append({**problem, "key": record["key"]})
    ha = snap.ha_config
    manifest = {
        "format": ARCHIVE_FORMAT, "format_version": ARCHIVE_VERSION, "archive_id": archive_id,
        "created_at": created_at, "created_by": {"app": APP_NAME, "version": APP_VERSION},
        "purpose": purpose, "selection": selection,
        "home_assistant": {"version": ha.get("version"), "location_name": ha.get("location_name"),
                           "time_zone": ha.get("time_zone"), "country": ha.get("country"),
                           "language": ha.get("language")},
        "source_installation": installation,
        "inventory": inventory, "associations": associations,
        "counts": {"automations": len(records), "complete": sum(1 for i in inventory if i["complete"]),
                   "labels": len(labels), "categories": sum(len(v) for v in categories.values()),
                   "areas": len(areas), "blueprints": len(blueprints),
                   "blueprint_files": sum(1 for b in blueprints.values() if b["included"])},
        "complete": all(i["complete"] for i in inventory) and all(b["included"] or b["installed"]
                                                                for b in blueprints.values()),
        "warnings": warnings,
        "compatibility": {
            "min_reader_format_version": 1,
            "home_assistant_min": "2026.10.0",
            "restore_via": ["/api/config/automation/config/<id> (Home Assistant's automation editor endpoint)",
                            "config/entity_registry/update", "automation.turn_on / automation.turn_off"],
            "key_order": "Mapping key order is not significant; values, types and list order are exact.",
        },
        "files": {name: {"sha256": sha256_bytes(data), "size": len(data)} for name, data in sorted(files.items())},
    }
    files["manifest.json"] = _json_bytes(manifest)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in ["manifest.json", "README.txt", *sorted(n for n in files if n not in ("manifest.json", "README.txt"))]:
            info = zipfile.ZipInfo(name, date_time=_zip_time(created_at))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            zf.writestr(info, files[name])
    return buffer.getvalue(), manifest


def _zip_time(iso: str) -> tuple[int, int, int, int, int, int]:
    try:
        date, time = iso[:19].split("T")
        y, m, d = (int(x) for x in date.split("-"))
        hh, mm, ss = (int(x) for x in time.split(":"))
        return (max(y, 1980), m, d, hh, mm, ss)
    except ValueError:
        return (1980, 1, 1, 0, 0, 0)


# --------------------------------------------------------------------------------------------------- reading
class Archive:
    """A parsed archive (from this App), with its files checked against the manifest."""

    def __init__(self, data: bytes) -> None:
        self.size = len(data)
        self.sha256 = sha256_bytes(data)
        self.zip = SafeZip(data)
        if "manifest.json" not in self.zip.members:
            raise ArchiveError("This is not an archive of this App (manifest.json is missing). To import an "
                               "ordinary automation file, upload the YAML file itself.", "no_manifest")
        manifest = self.zip.json("manifest.json")
        if not isinstance(manifest, dict) or manifest.get("format") != ARCHIVE_FORMAT:
            raise ArchiveError("manifest.json is not the manifest of an Automation Import/Export archive",
                               "wrong_format")
        version = manifest.get("format_version")
        if not isinstance(version, int) or version < 1:
            raise ArchiveError(f"Unknown archive format version {version!r}", "unknown_version")
        if version > ARCHIVE_VERSION:
            raise ArchiveError(f"The archive has format version {version}; this App reads up to version "
                               f"{ARCHIVE_VERSION}. Update the App to restore it.", "unknown_version")
        self.manifest: dict[str, Any] = manifest
        self.manifest_sha256 = sha256_bytes(self.zip.read("manifest.json"))
        self._records: list[dict[str, Any]] | None = None

    @property
    def records(self) -> list[dict[str, Any]]:
        if self._records is None:
            value = self.zip.json("data/automations.json")
            if not isinstance(value, list) or not all(isinstance(r, dict) for r in value):
                raise ArchiveError("data/automations.json is not a list of automation records", "damaged")
            self._records = value
        return self._records

    def definitions(self, name: str) -> dict[str, Any]:
        value = self.zip.json(f"data/{name}.json")
        if not isinstance(value, dict):
            raise ArchiveError(f"data/{name}.json is not an object", "damaged")
        return value

    def blueprint_file(self, path: str) -> bytes | None:
        name = f"blueprints/automation/{path}"
        return self.zip.read(name) if name in self.zip.members else None

    def summary(self) -> dict[str, Any]:
        m = self.manifest
        return {"archive_id": m.get("archive_id"), "created_at": m.get("created_at"), "purpose": m.get("purpose"),
                "home_assistant": m.get("home_assistant"), "source_installation": m.get("source_installation"),
                "counts": m.get("counts"), "complete": m.get("complete"), "format_version": m.get("format_version"),
                "created_by": m.get("created_by"), "selection": m.get("selection"), "size": self.size,
                "sha256": self.sha256}


def _check(checks: list[dict[str, Any]], check_id: str, title: str, problems: list[str]) -> bool:
    checks.append({"id": check_id, "title": title, "ok": not problems, "details": problems[:50],
                   "more": max(0, len(problems) - 50)})
    return not problems


def verify(data: bytes, *, expected_keys: list[str] | None = None, live: Snapshot | None = None,
           deletion_keys: list[str] | None = None, stored_sha256: str | None = None,
           verified_at: str | None = None) -> dict[str, Any]:
    """Every check of 0.1.4, per archive and per automation. Never raises for a bad archive: it reports."""
    checks: list[dict[str, Any]] = []
    per: dict[str, list[str]] = {}

    def fail(key: str, message: str) -> None:
        per.setdefault(key, []).append(message)

    report: dict[str, Any] = {"ok": False, "verified_at": verified_at, "checks": checks, "automations": {},
                              "deletable": [], "blocked": {}}
    if stored_sha256 is not None and sha256_bytes(data) != stored_sha256:
        _check(checks, "stored", "The stored archive is the one that was created", [
            "The archive file changed after it was created"])
        return report
    try:
        archive = Archive(data)
    except ArchiveError as err:
        _check(checks, "readable", "The archive is complete and readable", [str(err)])
        report["error"] = err.code
        return report
    manifest = archive.manifest
    readable: list[str] = []
    listed = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    if not listed:
        readable.append("The manifest lists no files")
    for name in listed:
        if name not in archive.zip.members:
            readable.append(f"{name} is listed in the manifest but missing")
    extra = sorted(set(archive.zip.members) - set(listed) - {"manifest.json"})
    for name in extra:
        readable.append(f"{name} is in the archive but not in the manifest")
    try:
        records = archive.records
        labels = archive.definitions("labels")
        categories = archive.definitions("categories")
        areas = archive.definitions("areas")
        blueprints = archive.definitions("blueprints")
    except ArchiveError as err:
        readable.append(str(err))
        records, labels, categories, areas, blueprints = [], {}, {}, {}, {}
    _check(checks, "readable", "The archive is complete and readable", readable)

    sums = []
    for name, info in listed.items():
        if name not in archive.zip.members or not isinstance(info, dict):
            continue
        try:
            content = archive.zip.read(name)
        except ArchiveError as err:
            sums.append(str(err))
            continue
        if sha256_bytes(content) != info.get("sha256") or len(content) != info.get("size"):
            sums.append(f"{name}: the checksum does not match")
    _check(checks, "checksums", "Checksums match", sums)

    inventory = manifest.get("inventory") if isinstance(manifest.get("inventory"), list) else []
    by_key = {r.get("key"): r for r in records if isinstance(r.get("key"), str)}
    inv_by_key = {i.get("key"): i for i in inventory if isinstance(i, dict)}
    identity = []
    if len(by_key) != len(records) or len(inv_by_key) != len(inventory):
        identity.append("An automation occurs more than once")
    if set(by_key) != set(inv_by_key):
        identity.append("The manifest's inventory and the automation records list different automations")
    for key, entry in inv_by_key.items():
        record = by_key.get(key)
        if record is None:
            continue
        for field_name in ("config_id", "entity_id", "source", "owner"):
            if entry.get(field_name) != record.get(field_name):
                identity.append(f"{key}: {field_name} differs between the manifest and the record")
                fail(key, f"{field_name} differs between the manifest and the record")
        if record.get("config") is not None and entry.get("config_sha256") != digest(record["config"]):
            identity.append(f"{key}: the configuration does not match the manifest's checksum")
            fail(key, "The configuration does not match the manifest")
        if entry.get("registry_sha256") != registry_digest(record.get("registry")):
            identity.append(f"{key}: the registry entry does not match the manifest's checksum")
            fail(key, "The registry entry does not match the manifest")

    configs, yaml_problems = [], []
    combined = []
    for key, entry in inv_by_key.items():
        record = by_key.get(key) or {}
        if record.get("restorable") == "full" and not isinstance(record.get("config"), dict):
            configs.append(f"{key}: no configuration")
            fail(key, "The configuration is missing")
        if record.get("config") is None:
            continue
        combined.append(record["config"])
        name = entry.get("file")
        if not name or name not in archive.zip.members:
            yaml_problems.append(f"{key}: its YAML file is missing")
            fail(key, "Its YAML file is missing")
            continue
        try:
            parsed = yamlio.load(archive.zip.read(name).decode("utf-8"))
        except Exception as err:  # noqa: BLE001 - any YAML error is a finding
            yaml_problems.append(f"{name}: not parseable ({type(err).__name__})")
            fail(key, "Its YAML file cannot be parsed")
            continue
        if not same(parsed, record["config"]):
            yaml_problems.append(f"{name}: differs from the exported configuration: "
                                 + "; ".join(differences(record["config"], parsed, limit=3)))
            fail(key, "Its YAML file differs from the exported configuration")
    if "automations.yaml" in archive.zip.members:
        try:
            parsed_all = yamlio.load(archive.zip.read("automations.yaml").decode("utf-8")) or []
            if not same(parsed_all, combined):
                yaml_problems.append("automations.yaml differs from the exported configurations")
        except Exception as err:  # noqa: BLE001
            yaml_problems.append(f"automations.yaml: not parseable ({type(err).__name__})")
    else:
        yaml_problems.append("automations.yaml is missing")
    _check(checks, "configurations", "Every selected automation has a complete configuration", configs)
    _check(checks, "yaml", "All exported YAML is parseable and identical to the configuration", yaml_problems)

    metadata = []
    for key, record in by_key.items():
        reg = record.get("registry")
        if record.get("restorable") != "full":
            continue
        if record.get("entity_id") and not isinstance(reg, dict):
            metadata.append(f"{key}: the registry entry is missing")
            fail(key, "Its registry entry (labels, category, area, entity id) is missing")
            continue
        if not isinstance(reg, dict):
            continue
        for field_name in REGISTRY_COMPARED:
            if field_name not in reg:
                metadata.append(f"{key}: the registry field {field_name} is missing")
                fail(key, f"The registry field {field_name} is missing")
        for label_id in reg.get("labels") or []:
            if label_id not in labels:
                metadata.append(f"{key}: the definition of the label {label_id} is missing")
                fail(key, f"The definition of the label {label_id} is missing")
        for scope, category_id in (reg.get("categories") or {}).items():
            if category_id not in (categories.get(scope) or {}):
                metadata.append(f"{key}: the definition of the category {category_id} is missing")
                fail(key, f"The definition of the category {category_id} is missing")
        if reg.get("area_id") and reg["area_id"] not in areas:
            metadata.append(f"{key}: the definition of the area {reg['area_id']} is missing")
            fail(key, f"The definition of the area {reg['area_id']} is missing")
        assoc = (manifest.get("associations") or {}).get(key) or {}
        if (sorted(assoc.get("labels") or []) != sorted(reg.get("labels") or [])
                or (assoc.get("categories") or {}) != (reg.get("categories") or {})
                or assoc.get("area_id") != reg.get("area_id") or assoc.get("entity_id") != record.get("entity_id")):
            metadata.append(f"{key}: the manifest's associations differ from the registry entry")
            fail(key, "The manifest's associations differ from the registry entry")
    _check(checks, "metadata", "Every selected automation has its required metadata", metadata)

    dependencies = []
    for key, record in by_key.items():
        refs = record.get("references")
        if not isinstance(refs, dict):
            dependencies.append(f"{key}: no dependency information")
            continue
        path = (refs.get("blueprint") or {}).get("path")
        if path:
            info = blueprints.get(path)
            if not isinstance(info, dict):
                dependencies.append(f"{key}: the blueprint {path} is not described")
                fail(key, f"The blueprint {path} is not described")
            elif info.get("included") and info.get("file") not in archive.zip.members:
                dependencies.append(f"{key}: the blueprint file {path} is missing")
                fail(key, f"The blueprint file {path} is missing")
    _check(checks, "dependencies", "Restore dependencies are present or explicitly identified", dependencies)

    if expected_keys is not None:
        missing = sorted(set(expected_keys) - set(by_key))
        unexpected = sorted(set(by_key) - set(expected_keys))
        _check(checks, "selection", "No selected automation was omitted", [
            *(f"{k} was selected but is not in the archive" for k in missing),
            *(f"{k} is in the archive but was not selected" for k in unexpected)])
        for key in missing:
            fail(key, "It was selected but is not in the archive")

    if deletion_keys is not None:
        protected = []
        for key in deletion_keys:
            record = by_key.get(key) or {}
            if record.get("protected") or record.get("owner") == "builder" or record.get("source") != "ui":
                protected.append(f"{key} is protected or not an ordinary UI automation")
                fail(key, "It is protected or not an ordinary UI automation")
        _check(checks, "protected", "Protected automations are excluded from deletion", protected)

    if live is not None:
        changed = []
        for key, record in by_key.items():
            item = live.item(key)
            if item is None:
                changed.append(f"{key} no longer exists in Home Assistant")
                fail(key, "It no longer exists in Home Assistant")
                continue
            if record.get("config") is not None and not same(item.config, record["config"]):
                diff = differences(record["config"], item.config, limit=3) if item.config is not None else ["gone"]
                changed.append(f"{key}: the configuration changed since the export ({'; '.join(diff)})")
                fail(key, "Its configuration changed since the export")
            if registry_digest(item.registry) != registry_digest(record.get("registry")):
                changed.append(f"{key}: its labels, category, area or registry settings changed since the export")
                fail(key, "Its registry metadata changed since the export")
            archived_state = (record.get("state") or {}).get("state")
            if archived_state in ("on", "off") and item.state in ("on", "off") and item.state != archived_state:
                changed.append(f"{key}: it was turned {item.state} since the export")
                fail(key, f"It was turned {item.state} since the export")
        _check(checks, "lossless", "A lossless comparison with Home Assistant finds no difference", changed)

    for key, record in by_key.items():
        problems = list(per.get(key, []))
        for problem in record.get("problems") or []:
            if problem.get("level") == "error":
                problems.append(problem.get("message") or problem.get("code"))
        complete = record.get("restorable") in ("full", "yaml_only") and not problems
        report["automations"][key] = {"ok": complete, "restorable": record.get("restorable"), "problems": problems,
                                      "alias": record.get("alias")}
    for key in deletion_keys or []:
        verdict = report["automations"].get(key)
        if verdict and verdict["ok"] and not (by_key.get(key) or {}).get("protected"):
            report["deletable"].append(key)
        else:
            report["blocked"][key] = (verdict or {}).get("problems") or ["It is not in the archive"]
    archive_level = {"readable", "checksums", "yaml", "selection", "protected"}
    archive_ok = all(c["ok"] for c in checks if c["id"] in archive_level)
    report["ok"] = archive_ok and all(c["ok"] for c in checks) and all(
        v["ok"] for v in report["automations"].values() if v["restorable"] in ("full", "yaml_only"))
    report["status"] = "verified" if report["ok"] else ("incomplete" if archive_ok else "failed")
    report["summary"] = archive.summary()
    report["manifest_sha256"] = archive.manifest_sha256
    return report


def canonical_text(value: Any) -> str:
    return canonical(value)
