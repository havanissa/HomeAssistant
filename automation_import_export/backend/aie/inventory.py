"""Discovery: every automation Home Assistant knows, with its configuration, registry metadata and ownership.

A :class:`Snapshot` is read fresh from Home Assistant for every operation (nothing is cached between them):
states, the entity / label / category / area / floor / device registries, the blueprints, and for every
automation id its configuration through the editor endpoint (exactly as it is in ``automations.yaml``).

Classification of every automation (:func:`classify`):

* **source** - ``ui``: in ``automations.yaml`` and addressable by id (the editor endpoint returns it; it can be
  exported losslessly, deleted and restored); ``external``: loaded from another YAML file (a package, an
  ``automation name: !include ...`` section, or an entry without an id) - read-only export of the configuration
  Home Assistant runs; ``integration``: an automation entity that another integration provides or whose registry
  entry belongs to a config entry; ``orphan``: a registry entry without a running automation and without a
  configuration (left behind) - shown, never touched.
* **owner** - ``builder``: an id in Automation Builder's namespace (``automation_builder_``, any case): protected,
  never deleted or written; ``review``: not in that namespace but carrying an Automation Builder marker (its
  description text, its alias suffix, or one of its blueprints): ownership is ambiguous, so it is excluded from
  deletion unless the user explicitly includes it; ``ordinary``: everything else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
import logging
from typing import Any

from . import deps
from .canonical import digest
from .const import BUILDER_ALIAS_SUFFIX, BUILDER_BLUEPRINT_FOLDER, BUILDER_DESCRIPTION
from .ha import AutomationConfigApi, ConfigApiError, HomeAssistantClient, HomeAssistantError, is_builder_id
from .haconfig import FileScan, HaConfigFolder

_LOGGER = logging.getLogger(__name__)
GET_CONCURRENCY = 8
#: The registry fields that identify and organise an automation, all of them archived and restored.
REGISTRY_FIELDS = ("entity_id", "unique_id", "platform", "id", "name", "icon", "area_id", "labels", "categories",
                   "aliases", "disabled_by", "hidden_by", "options", "original_name", "config_entry_id", "device_id",
                   "entity_category", "created_at", "modified_at")
#: The fields compared to decide whether an automation's metadata changed. Timestamps are excluded, and so are
#: the registry options: Home Assistant writes default assistant-exposure values into them by itself; the
#: explicit exposure choices are compared separately (:func:`exposure`).
REGISTRY_COMPARED = ("entity_id", "unique_id", "name", "icon", "area_id", "labels", "categories", "aliases",
                     "disabled_by", "hidden_by")
ASSISTANTS = ("conversation", "cloud.alexa", "cloud.google_assistant")


def exposure(entry: dict[str, Any] | None) -> dict[str, bool]:
    """The assistants an automation is explicitly exposed to (True) or hidden from (False)."""
    options = (entry or {}).get("options") or {}
    out = {}
    for assistant in ASSISTANTS:
        value = (options.get(assistant) or {}).get("should_expose") if isinstance(options.get(assistant), dict) else None
        if isinstance(value, bool):
            out[assistant] = value
    return out


def registry_view(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    out = {k: entry.get(k) for k in REGISTRY_FIELDS if k in entry}
    out["labels"] = sorted(entry.get("labels") or [])
    out["categories"] = dict(sorted((entry.get("categories") or {}).items()))
    out["aliases"] = list(entry.get("aliases") or [])
    out["options"] = entry.get("options") or {}
    return out


def registry_digest(entry: dict[str, Any] | None) -> str | None:
    if entry is None:
        return None
    return digest({k: entry.get(k) for k in REGISTRY_COMPARED})


@dataclass
class Snapshot:
    taken_at: str
    ha_config: dict[str, Any]
    states: dict[str, dict[str, Any]]
    registry: dict[str, dict[str, Any]]                  # automation entity id -> extended registry entry
    labels: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, dict[str, Any]]]     # scope -> category id -> definition
    areas: dict[str, dict[str, Any]]
    floors: dict[str, dict[str, Any]]
    devices: set[str]
    entity_ids: set[str]
    blueprints: dict[str, dict[str, Any]]                # path -> {"metadata": ...} or {"error": ...}
    configs: dict[str, dict[str, Any]]                   # automation id -> config from automations.yaml
    config_errors: dict[str, str]
    runtime_configs: dict[str, dict[str, Any]]           # entity id -> what the entity runs (automation/config)
    file_scan: FileScan
    problems: list[dict[str, Any]] = field(default_factory=list)
    config_origins: dict[str, str] = field(default_factory=dict)  # id -> "editor" | "file"
    items: list[Item] = field(default_factory=list)

    def item(self, key: str) -> Item | None:
        return next((i for i in self.items if i.key == key), None)


@dataclass
class Item:
    key: str
    config_id: str | None
    entity_id: str | None
    alias: str
    source: str
    owner: str
    state: str | None
    config: dict[str, Any] | None
    config_origin: str | None
    registry: dict[str, Any] | None
    problems: list[dict[str, Any]]
    references: dict[str, Any]
    attributes: dict[str, Any]

    @property
    def protected(self) -> bool:
        return self.owner == "builder" or self.source in ("integration", "orphan")

    @property
    def blocking(self) -> list[dict[str, Any]]:
        return [p for p in self.problems if p["level"] == "error"]

    @property
    def deletable(self) -> bool:
        """Can be part of a cleanup at all (a ``review`` item only with explicit acknowledgement)."""
        return self.source == "ui" and self.owner in ("ordinary", "review") and not self.blocking

    @property
    def config_hash(self) -> str | None:
        return digest(self.config) if self.config is not None else None

    @property
    def registry_hash(self) -> str | None:
        return registry_digest(self.registry)

    @property
    def blueprint(self) -> str | None:
        bp = self.references.get("blueprint")
        return bp.get("path") if bp else None

    def public(self, snapshot: Snapshot | None = None) -> dict[str, Any]:
        reg = self.registry or {}
        labels = reg.get("labels") or []
        categories = reg.get("categories") or {}
        area_id = reg.get("area_id")

        def label(i: str) -> dict[str, Any]:
            d = snapshot.labels.get(i) if snapshot else None
            return {"id": i, "name": d.get("name") if d else None, "color": d.get("color") if d else None,
                    "icon": d.get("icon") if d else None}

        def category(scope: str, i: str) -> dict[str, Any]:
            d = (snapshot.categories.get(scope) or {}).get(i) if snapshot else None
            return {"scope": scope, "id": i, "name": d.get("name") if d else None}

        area = None
        if area_id:
            d = snapshot.areas.get(area_id) if snapshot else None
            area = {"id": area_id, "name": d.get("name") if d else None}
        return {
            "key": self.key, "config_id": self.config_id, "entity_id": self.entity_id, "alias": self.alias,
            "name": reg.get("name") or self.alias,
            "description": (self.config or {}).get("description") if isinstance(self.config, dict) else None,
            "source": self.source, "owner": self.owner, "protected": self.protected, "state": self.state,
            "enabled": self.state == "on" if self.state in ("on", "off") else None,
            "registry_disabled": reg.get("disabled_by"), "hidden": reg.get("hidden_by"),
            "editable": self.source == "ui", "deletable": self.deletable,
            "needs_review": self.owner == "review",
            "area": area, "labels": [label(i) for i in labels],
            "categories": [category(s, i) for s, i in categories.items()],
            "blueprint": self.blueprint, "mode": (self.config or {}).get("mode") if self.config else None,
            "last_triggered": self.attributes.get("last_triggered"),
            "problems": self.problems, "references": self.references,
            "config_hash": self.config_hash, "registry_hash": self.registry_hash,
            "config_origin": self.config_origin,
        }


async def _read_list(ha: HomeAssistantClient, command: str, **params: Any) -> list[dict[str, Any]]:
    items = await ha.command(command, **params)
    return [i for i in items or [] if isinstance(i, dict)]


async def take_snapshot(ha: HomeAssistantClient, api: AutomationConfigApi, folder: HaConfigFolder,
                        *, now: str, fetch: Iterable[str] = ()) -> Snapshot:
    """Read everything fresh. Raises ConnectionError / HomeAssistantError when Home Assistant cannot be read.

    Configurations: Home Assistant's editor endpoint re-reads all of automations.yaml for every single id (one
    request at a time), which takes about a minute for a few hundred automations. So the listing uses the file
    itself (parsed once, read-only), and the editor endpoint is asked only for the ids in ``fetch`` (what an
    operation exports, verifies, deletes or restores), for entries the file parse cannot represent exactly, and
    for everything when the file cannot be read. Exports are always verified against the editor endpoint.
    """
    ha_config = await ha.command("get_config") or {}
    states_list = await ha.command("get_states") or []
    states = {s["entity_id"]: s for s in states_list if isinstance(s, dict) and "entity_id" in s}
    registry_all = await _read_list(ha, "config/entity_registry/list")
    labels = {d["label_id"]: d for d in await _read_list(ha, "config/label_registry/list") if "label_id" in d}
    areas = {d["area_id"]: d for d in await _read_list(ha, "config/area_registry/list") if "area_id" in d}
    try:
        floors = {d["floor_id"]: d for d in await _read_list(ha, "config/floor_registry/list") if "floor_id" in d}
    except HomeAssistantError:
        floors = {}
    devices = {d["id"] for d in await _read_list(ha, "config/device_registry/list") if "id" in d}
    try:
        blueprints = await ha.command("blueprint/list", domain="automation") or {}
    except HomeAssistantError as err:
        _LOGGER.warning("Could not list the automation blueprints: %s", err)
        blueprints = {}
    problems: list[dict[str, Any]] = []

    automation_entries = [e for e in registry_all if str(e.get("entity_id", "")).startswith("automation.")]
    registry: dict[str, dict[str, Any]] = {}
    ids = [e["entity_id"] for e in automation_entries]
    for start in range(0, len(ids), 200):
        extended = await ha.command("config/entity_registry/get_entries", entity_ids=ids[start:start + 200]) or {}
        for entity_id, entry in extended.items():
            if isinstance(entry, dict):
                registry[entity_id] = entry
    for entry in automation_entries:  # an entry removed between the two reads keeps its short form
        registry.setdefault(entry["entity_id"], entry)

    scopes = {"automation"} | {s for e in registry.values() for s in (e.get("categories") or {})}
    categories: dict[str, dict[str, dict[str, Any]]] = {}
    for scope in sorted(scopes):
        try:
            categories[scope] = {d["category_id"]: d for d in await _read_list(
                ha, "config/category_registry/list", scope=scope) if "category_id" in d}
        except HomeAssistantError as err:
            problems.append({"level": "warning", "code": "categories_unreadable",
                             "message": f"The categories of scope {scope!r} could not be read: {err}"})
            categories[scope] = {}

    config_ids: set[str] = set()
    for entity_id, entry in registry.items():
        if entry.get("platform") == "automation" and isinstance(entry.get("unique_id"), str):
            config_ids.add(entry["unique_id"])
    for entity_id, st in states.items():
        if entity_id.startswith("automation.") and isinstance((st.get("attributes") or {}).get("id"), str):
            config_ids.add(st["attributes"]["id"])
    file_scan = await asyncio.to_thread(folder.scan_automations_file)
    config_ids |= {i for i in file_scan.ids if isinstance(i, str)}

    configs: dict[str, dict[str, Any]] = {}
    config_errors: dict[str, str] = {}
    origins: dict[str, str] = {}
    wanted = set(fetch)
    if file_scan.available and file_scan.error is None:
        for config_id, config in file_scan.configs.items():
            if config_id not in wanted:
                configs[config_id] = config
                origins[config_id] = "file"
        # ids that are not in automations.yaml cannot be in the editor endpoint either (it reads that file)
        to_get = (wanted & config_ids) | {i for i in file_scan.ids if i not in file_scan.configs}
    else:
        to_get = set(config_ids)
    semaphore = asyncio.Semaphore(GET_CONCURRENCY)

    async def fetch_one(config_id: str) -> None:
        if not api.addressable(config_id):
            config_errors[config_id] = "This id cannot be addressed through Home Assistant's automation editor"
            return
        async with semaphore:
            try:
                value = await api.get(config_id)
            except ConfigApiError as err:
                config_errors[config_id] = str(err)
                return
        if value is not None:
            configs[config_id] = value
            origins[config_id] = "editor"

    await asyncio.gather(*(fetch_one(i) for i in sorted(to_get)))
    for config_id in config_ids - set(config_errors):
        if not api.addressable(config_id):
            config_errors[config_id] = "This id cannot be addressed through Home Assistant's automation editor"

    runtime: dict[str, dict[str, Any]] = {}
    in_file = set(configs)
    for entity_id, st in states.items():
        if not entity_id.startswith("automation."):
            continue
        cid = (st.get("attributes") or {}).get("id")
        if isinstance(cid, str) and cid in in_file:
            continue
        try:
            result = await ha.command("automation/config", entity_id=entity_id)
        except HomeAssistantError:
            continue
        if isinstance(result, dict) and isinstance(result.get("config"), dict):
            runtime[entity_id] = result["config"]

    snapshot = Snapshot(
        taken_at=now, ha_config=ha_config, states=states, registry=registry, labels=labels, categories=categories,
        areas=areas, floors=floors, devices=devices,
        entity_ids=set(states) | {e.get("entity_id") for e in registry_all if e.get("entity_id")},
        blueprints={p: v for p, v in blueprints.items() if isinstance(v, dict)}, configs=configs,
        config_errors=config_errors, runtime_configs=runtime, file_scan=file_scan, problems=problems,
        config_origins=origins)
    if not file_scan.available:
        problems.append({"level": "info", "code": "config_folder_unavailable",
                         "message": "Home Assistant's configuration folder is not readable by the App: blueprint "
                                    "files cannot be included and duplicate ids in automations.yaml cannot be "
                                    "detected."})
    elif file_scan.error:
        problems.append({"level": "warning", "code": "automations_file", "message": file_scan.error})
    snapshot.items = classify(snapshot)
    return snapshot


def _owner(config_id: str | None, config: dict[str, Any] | None, entity_id: str | None) -> tuple[str, list[str]]:
    if is_builder_id(config_id):
        return "builder", ["Its id is in Automation Builder's namespace (automation_builder_)"]
    reasons = []
    if isinstance(config, dict):
        if str(config.get("description") or "").startswith(BUILDER_DESCRIPTION):
            reasons.append("Its description says it is managed by Automation Builder")
        if str(config.get("alias") or "").endswith(BUILDER_ALIAS_SUFFIX):
            reasons.append("Its name ends with “(Automation Builder)”")
        bp = config.get("use_blueprint")
        if isinstance(bp, dict) and str(bp.get("path") or "").startswith(BUILDER_BLUEPRINT_FOLDER):
            reasons.append("It uses a blueprint that Automation Builder exported")
    if entity_id and "automation_builder" in entity_id and not reasons:
        reasons.append("Its entity id mentions automation_builder")
    return ("review", reasons) if reasons else ("ordinary", [])


def classify(snap: Snapshot) -> list[Item]:
    by_unique: dict[str, str] = {}
    for entity_id, entry in snap.registry.items():
        if entry.get("platform") == "automation" and isinstance(entry.get("unique_id"), str):
            by_unique[entry["unique_id"]] = entity_id
    keys: dict[str, dict[str, Any]] = {}
    for config_id in set(snap.configs) | set(by_unique) | set(snap.config_errors):
        keys[config_id] = {"config_id": config_id, "entity_id": by_unique.get(config_id)}
    for entity_id, st in snap.states.items():
        if not entity_id.startswith("automation."):
            continue
        cid = (st.get("attributes") or {}).get("id")
        if isinstance(cid, str):
            keys.setdefault(cid, {"config_id": cid, "entity_id": entity_id})
            keys[cid]["entity_id"] = keys[cid]["entity_id"] or entity_id
        else:
            keys[f"entity:{entity_id}"] = {"config_id": None, "entity_id": entity_id}
    for entity_id, entry in snap.registry.items():
        if entry.get("platform") != "automation" and not any(v["entity_id"] == entity_id for v in keys.values()):
            keys[f"entity:{entity_id}"] = {"config_id": None, "entity_id": entity_id}

    known = {
        "entities": snap.entity_ids, "devices": snap.devices, "areas": set(snap.areas), "floors": set(snap.floors),
        "labels": set(snap.labels), "blueprints": {p for p, v in snap.blueprints.items() if "metadata" in v},
    }
    items: list[Item] = []
    for key, ids in sorted(keys.items()):
        config_id, entity_id = ids["config_id"], ids["entity_id"]
        st = snap.states.get(entity_id) if entity_id else None
        entry = snap.registry.get(entity_id) if entity_id else None
        attributes = (st or {}).get("attributes") or {}
        problems: list[dict[str, Any]] = []
        config: dict[str, Any] | None = None
        origin: str | None = None
        if config_id is not None and config_id in snap.configs:
            config, origin, source = snap.configs[config_id], "automations.yaml", "ui"
        elif entity_id and entity_id in snap.runtime_configs:
            config, origin, source = snap.runtime_configs[entity_id], "automation/config", "external"
            problems.append({"level": "info", "code": "external_yaml",
                             "message": "Loaded from a YAML file other than automations.yaml (or without an id): "
                                        "exported read-only as Home Assistant runs it (includes and secrets "
                                        "resolved, a blueprint substituted). It is never deleted by this App."})
        elif st is None and entry is not None:
            source = "orphan" if entry.get("platform") == "automation" else "integration"
            problems.append({"level": "info", "code": "no_automation",
                             "message": "A registry entry without a running automation (disabled, or left behind "
                                        "by a removed automation)" if source == "orphan" else
                                        "Provided by another integration"})
        else:
            source = "external"
        if entry is not None and (entry.get("platform") != "automation" or entry.get("config_entry_id")):
            source = "integration"
        if config_id and config_id in snap.config_errors:
            problems.append({"level": "error", "code": "config_unreadable",
                             "message": f"Its configuration could not be read: {snap.config_errors[config_id]}"})
        if config_id and snap.file_scan.ids.get(config_id, 0) > 1:
            problems.append({"level": "error", "code": "duplicate_id",
                             "message": f"The id {config_id} occurs {snap.file_scan.ids[config_id]} times in "
                                        "automations.yaml: Home Assistant runs only one of them, and deleting by "
                                        "id would be ambiguous. Fix the file first."})
        if config_id and config_id in snap.file_scan.non_string_ids:
            problems.append({"level": "error", "code": "non_string_id",
                             "message": "Its id in automations.yaml is not text (for example an unquoted number): "
                                        "the editor endpoint cannot address it."})
        if source == "ui" and entry is None:
            problems.append({"level": "error", "code": "no_registry_entry",
                             "message": "Home Assistant has no registry entry for it (for example because another "
                                        "automation has the same id): its labels, category and area cannot be "
                                        "captured."})
        if entry is not None:
            for label_id in entry.get("labels") or []:
                if label_id not in snap.labels:
                    problems.append({"level": "error", "code": "label_unknown",
                                     "message": f"It has the label {label_id}, whose definition cannot be read"})
            for scope, category_id in (entry.get("categories") or {}).items():
                if category_id not in (snap.categories.get(scope) or {}):
                    problems.append({"level": "error", "code": "category_unknown",
                                     "message": f"It has the category {category_id} ({scope}), whose definition "
                                                "cannot be read"})
            if entry.get("area_id") and entry["area_id"] not in snap.areas:
                problems.append({"level": "error", "code": "area_unknown",
                                 "message": f"It is in the area {entry['area_id']}, which cannot be read"})
        owner, reasons = _owner(config_id, config, entity_id)
        if owner == "builder":
            problems.append({"level": "info", "code": "builder", "message": reasons[0] + ": protected."})
        elif owner == "review":
            problems.append({"level": "warning", "code": "ownership_ambiguous",
                             "message": "Ownership is ambiguous - " + "; ".join(reasons) + ". It is excluded from "
                                        "deletion unless you include it explicitly."})
        refs = deps.references(config) if config is not None else deps.references({})
        if config is not None:
            problems.extend(deps.check(refs, **known))
        if st is not None and st.get("state") == "unavailable" and source == "ui":
            problems.append({"level": "warning", "code": "unavailable",
                             "message": "Home Assistant could not load it (its configuration is invalid or its "
                                        "blueprint is missing); it is archived exactly as it is."})
        alias = str((config or {}).get("alias") or attributes.get("friendly_name") or (entry or {}).get(
            "original_name") or entity_id or config_id or key)
        items.append(Item(key=key, config_id=config_id, entity_id=entity_id, alias=alias, source=source,
                          owner=owner, state=(st or {}).get("state"), config=config, config_origin=origin,
                          registry=registry_view(entry), problems=problems, references=refs,
                          attributes={k: attributes.get(k) for k in ("friendly_name", "icon", "id", "mode",
                                                                     "current", "max", "last_triggered")
                                      if k in attributes}))
    return items


def summary(snap: Snapshot) -> dict[str, Any]:
    items = snap.items
    return {
        "total": len(items),
        "ordinary": sum(1 for i in items if i.source == "ui" and i.owner == "ordinary"),
        "protected": sum(1 for i in items if i.protected),
        "builder": sum(1 for i in items if i.owner == "builder"),
        "review": sum(1 for i in items if i.owner == "review"),
        "external": sum(1 for i in items if i.source == "external"),
        "integration": sum(1 for i in items if i.source in ("integration", "orphan")),
        "deletable": sum(1 for i in items if i.deletable and i.owner == "ordinary"),
        "blocked": sum(1 for i in items if i.source == "ui" and i.blocking),
        "enabled": sum(1 for i in items if i.state == "on"),
        "disabled": sum(1 for i in items if i.state == "off" or (i.registry or {}).get("disabled_by")),
    }
