"""The archive of helpers, labels and categories: a ZIP file next to the automation archives (same store, same
safety limits when read), with its own format name.

Layout (format version 1)::

    manifest.json            format, ids, Home Assistant version, the items with their fingerprints, checksums
    README.txt               what the archive is
    data/labels.json         label id -> definition (name, icon, colour, description) + assignments
                             (the entities, devices and areas that carried it, with their names)
    data/categories.json     "<scope>:<id>" -> scope, definition (name, icon) + assignments (entities)
    data/helpers.json        "helper:<domain>:<id>" -> configuration (exactly what Home Assistant's
                             <domain>/list returns), entity id, registry settings (name, icon, area, labels,
                             categories, aliases, hidden/disabled by the user, assistant exposure), state, and
                             the definitions of the labels, categories and area it uses
    helpers/<domain>.yaml    the same configurations, readable

Assignments are what a restore puts back after a label or category was deleted (Home Assistant removes a deleted
label from every entity, device and area, and a deleted category from every entity).
"""

from __future__ import annotations

import io
import stat
from typing import Any
import zipfile

from . import yamlio
from .archive import ArchiveError, SafeZip, _check, _json_bytes, _zip_time
from .canonical import digest, same, sha256_bytes
from .const import APP_NAME, APP_VERSION
from .ha import registry_key
from .registry import RegistrySnapshot, category_definition, item_fingerprint, label_definition

REGISTRY_FORMAT = "automation-import-export-registry-archive"
REGISTRY_VERSION = 1

README = """This archive was created by the {app} App ({version}) for Home Assistant.

It contains Home Assistant helpers (Settings -> Helpers), labels and categories, with where the
labels and categories were assigned (entities, devices, areas), so that they can be restored by the App.

helpers/<type>.yaml lists the helper configurations in Home Assistant's own format.
manifest.json describes the archive and holds a checksum of every file.
"""


def build_registry_archive(*, archive_id: str, created_at: str, purpose: str, selection: dict[str, Any],
                           keys: list[str], snap: RegistrySnapshot, installation: dict[str, Any],
                           ha_config: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    labels: dict[str, Any] = {}
    categories: dict[str, Any] = {}
    helpers: dict[str, Any] = {}
    items = []

    def assignment_names(assigned: dict[str, list[str]]) -> dict[str, list[dict[str, str]]]:
        return {"entities": [{"id": e, "name": snap.entity_name(e)} for e in assigned.get("entities", [])],
                "devices": [{"id": d, "name": snap.device_name(d)} for d in assigned.get("devices", [])],
                "areas": [{"id": a, "name": snap.area_name(a)} for a in assigned.get("areas", [])]}

    for key in keys:
        kind, _, rest = key.partition(":")
        if kind == "label":
            d = snap.labels[rest]
            labels[rest] = {"definition": label_definition(d), "assignments": assignment_names(
                snap.label_assignments(rest))}
            items.append({"key": key, "kind": "label", "name": d.get("name"), "fingerprint": item_fingerprint(snap, key)})
        elif kind == "category":
            scope, _, category_id = rest.partition(":")
            d = snap.categories[scope][category_id]
            categories[rest] = {"scope": scope, "definition": category_definition(d),
                                "assignments": assignment_names(snap.category_assignments(scope, category_id))}
            items.append({"key": key, "kind": "category", "scope": scope, "name": d.get("name"),
                          "fingerprint": item_fingerprint(snap, key)})
        elif kind == "helper":
            h = snap.helpers[key]
            reg = h.registry or {}
            helpers[key] = {
                "domain": h.domain, "id": h.item_id, "config": h.config, "entity_id": h.entity_id,
                "registry": h.registry, "state": h.state,
                "uses": {
                    "labels": {i: label_definition(snap.labels[i]) for i in reg.get("labels") or [] if i in snap.labels},
                    "categories": {f"{s}:{i}": category_definition(snap.categories[s][i])
                                   for s, i in (reg.get("categories") or {}).items() if i in snap.categories.get(s, {})},
                    "area": {"area_id": reg["area_id"], "name": snap.area_name(reg["area_id"])}
                    if reg.get("area_id") in snap.areas else None,
                }}
            items.append({"key": key, "kind": "helper", "domain": h.domain, "name": h.name,
                          "entity_id": h.entity_id, "fingerprint": h.fingerprint()})
        else:
            raise ArchiveError(f"Unknown item {key}", "unknown_item")
    files: dict[str, bytes] = {
        "data/labels.json": _json_bytes(labels), "data/categories.json": _json_bytes(categories),
        "data/helpers.json": _json_bytes(helpers),
        "README.txt": README.format(app=APP_NAME, version=APP_VERSION).encode("utf-8"),
    }
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for h in helpers.values():
        by_domain.setdefault(h["domain"], []).append(h["config"])
    for domain, configs in sorted(by_domain.items()):
        files[f"helpers/{domain}.yaml"] = yamlio.dump(configs).encode("utf-8")
    manifest = {
        "format": REGISTRY_FORMAT, "format_version": REGISTRY_VERSION, "archive_id": archive_id,
        "created_at": created_at, "created_by": {"app": APP_NAME, "version": APP_VERSION}, "purpose": purpose,
        "selection": selection,
        "home_assistant": {k: ha_config.get(k) for k in ("version", "location_name", "time_zone", "country",
                                                          "language")},
        "source_installation": installation, "items": items,
        "counts": {"labels": len(labels), "categories": len(categories), "helpers": len(helpers),
                   "items": len(items),
                   "assignments": sum(len(v) for x in [*labels.values(), *categories.values()]
                                      for v in x["assignments"].values())},
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


def archive_format(data: bytes) -> str | None:
    """The format name in the manifest of a ZIP file (None when there is no readable manifest)."""
    try:
        manifest = SafeZip(data).json("manifest.json")
    except ArchiveError:
        return None
    return manifest.get("format") if isinstance(manifest, dict) else None


class RegistryArchive:
    def __init__(self, data: bytes) -> None:
        self.size = len(data)
        self.sha256 = sha256_bytes(data)
        self.zip = SafeZip(data)
        if "manifest.json" not in self.zip.members:
            raise ArchiveError("manifest.json is missing", "no_manifest")
        manifest = self.zip.json("manifest.json")
        if not isinstance(manifest, dict) or manifest.get("format") != REGISTRY_FORMAT:
            raise ArchiveError("This is not an archive of helpers, labels and categories", "wrong_format")
        version = manifest.get("format_version")
        if not isinstance(version, int) or version < 1:
            raise ArchiveError(f"Unknown archive format version {version!r}", "unknown_version")
        if version > REGISTRY_VERSION:
            raise ArchiveError(f"The archive has format version {version}; this App reads up to version "
                               f"{REGISTRY_VERSION}. Update the App to restore it.", "unknown_version")
        self.manifest: dict[str, Any] = manifest

    def data(self, name: str) -> dict[str, Any]:
        value = self.zip.json(f"data/{name}.json")
        if not isinstance(value, dict) or not all(isinstance(v, dict) for v in value.values()):
            raise ArchiveError(f"data/{name}.json is not an object of records", "damaged")
        return value

    @property
    def labels(self) -> dict[str, Any]:
        return self.data("labels")

    @property
    def categories(self) -> dict[str, Any]:
        return self.data("categories")

    @property
    def helpers(self) -> dict[str, Any]:
        return self.data("helpers")

    def summary(self) -> dict[str, Any]:
        m = self.manifest
        return {"archive_id": m.get("archive_id"), "created_at": m.get("created_at"), "purpose": m.get("purpose"),
                "home_assistant": m.get("home_assistant"), "source_installation": m.get("source_installation"),
                "counts": m.get("counts"), "format_version": m.get("format_version"),
                "created_by": m.get("created_by"), "selection": m.get("selection"), "size": self.size,
                "sha256": self.sha256, "kind": "registry"}


def record_fingerprint(key: str, archive: RegistryArchive) -> str | None:
    kind, _, rest = key.partition(":")
    if kind == "label":
        record = archive.labels.get(rest)
        return digest(record["definition"]) if record else None
    if kind == "category":
        record = archive.categories.get(rest)
        return digest(record["definition"]) if record else None
    record = archive.helpers.get(key)
    return digest({"config": record["config"], "registry": record["registry"]}) if record else None


def verify_registry(data: bytes, *, live: RegistrySnapshot | None = None, expected_keys: list[str] | None = None,
                    deletion_keys: list[str] | None = None, stored_sha256: str | None = None,
                    verified_at: str | None = None) -> dict[str, Any]:
    """The archive checks (readable, checksums, YAML, completeness, selection) and, with ``live``, the lossless
    comparison with Home Assistant. ``deletable``: the deletion keys whose archived copy is complete and equal to
    Home Assistant's current state."""
    checks: list[dict[str, Any]] = []
    report: dict[str, Any] = {"ok": False, "verified_at": verified_at, "checks": checks, "items": {},
                              "deletable": [], "blocked": {}, "kind": "registry"}
    if stored_sha256 is not None and sha256_bytes(data) != stored_sha256:
        _check(checks, "stored", "The stored archive is the one that was created", [
            "The archive file changed after it was created"])
        report["status"] = "failed"
        return report
    try:
        archive = RegistryArchive(data)
        labels, categories, helpers = archive.labels, archive.categories, archive.helpers
    except ArchiveError as err:
        _check(checks, "readable", "The archive is complete and readable", [str(err)])
        report["status"] = "failed"
        return report
    manifest = archive.manifest
    listed = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    readable = [f"{n} is listed in the manifest but missing" for n in listed if n not in archive.zip.members]
    readable += [f"{n} is in the archive but not in the manifest"
                 for n in sorted(set(archive.zip.members) - set(listed) - {"manifest.json"})]
    _check(checks, "readable", "The archive is complete and readable", readable)
    sums = []
    for name, info in listed.items():
        if name in archive.zip.members and isinstance(info, dict):
            content = archive.zip.read(name)
            if sha256_bytes(content) != info.get("sha256") or len(content) != info.get("size"):
                sums.append(f"{name}: the checksum does not match")
    _check(checks, "checksums", "Checksums match", sums)
    per: dict[str, list[str]] = {}
    items = {i.get("key"): i for i in manifest.get("items") or [] if isinstance(i, dict)}
    records = {**{registry_key("label", k): v for k, v in labels.items()},
               **{registry_key("category", k): v for k, v in categories.items()}, **helpers}
    complete = []
    if set(items) != set(records):
        complete.append("The manifest's items and the records list different items")
    for key, item in items.items():
        record = records.get(key)
        if record is None:
            per.setdefault(key, []).append("It is not in the archive")
            continue
        if item.get("fingerprint") != record_fingerprint(key, archive):
            complete.append(f"{key}: the record does not match the manifest")
            per.setdefault(key, []).append("The record does not match the manifest")
        if item.get("kind") == "helper" and not isinstance(record.get("config"), dict):
            complete.append(f"{key}: no configuration")
            per.setdefault(key, []).append("The configuration is missing")
    _check(checks, "records", "Every item has a complete record", complete)
    yaml_problems = []
    by_domain: dict[str, list[Any]] = {}
    for record in helpers.values():
        by_domain.setdefault(record.get("domain"), []).append(record.get("config"))
    for domain, configs in by_domain.items():
        name = f"helpers/{domain}.yaml"
        try:
            if not same(yamlio.load(archive.zip.read(name).decode("utf-8")), configs):
                yaml_problems.append(f"{name} differs from the archived configurations")
        except Exception as err:  # noqa: BLE001 - any YAML error is a finding
            yaml_problems.append(f"{name}: not readable ({type(err).__name__})")
    _check(checks, "yaml", "The helper YAML is parseable and identical to the configurations", yaml_problems)
    if expected_keys is not None:
        _check(checks, "selection", "No selected item was omitted", [
            *(f"{k} was selected but is not in the archive" for k in sorted(set(expected_keys) - set(items))),
            *(f"{k} is in the archive but was not selected" for k in sorted(set(items) - set(expected_keys)))])
    if live is not None:
        changed = []
        for key in items:
            now = item_fingerprint(live, key)
            if now is None:
                changed.append(f"{key} no longer exists in Home Assistant")
                per.setdefault(key, []).append("It no longer exists in Home Assistant")
            elif now != record_fingerprint(key, archive):
                changed.append(f"{key} changed since the backup")
                per.setdefault(key, []).append("It changed since the backup")
        _check(checks, "lossless", "A lossless comparison with Home Assistant finds no difference", changed)
    for key, item in items.items():
        report["items"][key] = {"ok": not per.get(key), "problems": per.get(key, []), "name": item.get("name"),
                                "kind": item.get("kind")}
    for key in deletion_keys or []:
        verdict = report["items"].get(key)
        if verdict and verdict["ok"]:
            report["deletable"].append(key)
        else:
            report["blocked"][key] = (verdict or {}).get("problems") or ["It is not in the archive"]
    archive_ok = all(c["ok"] for c in checks if c["id"] in ("readable", "checksums", "yaml", "selection"))
    report["ok"] = archive_ok and all(c["ok"] for c in checks)
    report["status"] = "verified" if report["ok"] else ("incomplete" if archive_ok else "failed")
    report["summary"] = archive.summary()
    return report
