"""Restoration planning (read-only) and execution (staged, journalled, verified).

Planning (:func:`build_plan`) follows the staged order of 0.1.6: the archive is read and its checksums are
verified, Home Assistant's current configuration is inspected, id collisions are found, the label, category and
area definitions are resolved (same id first, then the same name - never a duplicate just because a name
exists - else recreated or mapped as the user decides), blueprints are checked, the configurations are prepared
and validated by Home Assistant, and everything that deviates from an exact restoration needs an explicit
acknowledgement. The plan carries a hash over the archive, the decisions and the relevant live state: the
execution refuses a plan that no longer matches (something changed meanwhile).

Decisions (all optional; defaults in brackets)::

    {"selection": [keys] [every restorable automation],
     "collisions": {key: "skip" | "replace" | "new"} [skip; identical configurations only get their metadata],
     "new_ids": {key: "<new id>"} [generated],
     "labels": {label_id: {"action": "use", "target": id} | {"action": "create"} | {"action": "drop"}},
     "categories": {"<scope>:<id>": ...}, "areas": {area_id: ...}  [same id > same name > create],
     "acknowledgements": [codes]  (every deviation from an exact restoration)}
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import copy
import hashlib
import logging
from typing import Any

from .archive import Archive
from .canonical import differences, digest, same, sha256_bytes
from .ha import AutomationConfigApi, HomeAssistantClient, HomeAssistantError, NotAllowed, is_builder_id
from .haconfig import HaConfigFolder
from .inventory import ASSISTANTS, Snapshot, exposure

_LOGGER = logging.getLogger(__name__)
WAIT_SECONDS = 60.0
SETTLE_SECONDS = 1.0  # Home Assistant finishes a reload / entity removal in a task after the write
ACTIONS_WRITING = ("create", "replace", "new")


class PlanError(Exception):
    def __init__(self, message: str, code: str = "invalid", status: int = 400, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra


def _casefold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _resolve(kind: str, archived: dict[str, Any], live: dict[str, dict[str, Any]], decision: dict[str, Any] | None,
             id_field: str) -> dict[str, Any]:
    """How one archived label / category / area is restored."""
    source_id = archived.get(id_field)
    name = archived.get("name")
    out: dict[str, Any] = {"kind": kind, "source_id": source_id, "name": name, "definition": archived}
    if decision:
        action = decision.get("action")
        if action == "use":
            target = decision.get("target")
            if target not in live:
                raise PlanError(f"The {kind} chosen for {name!r} does not exist", "unknown_target")
            return {**out, "action": "use", "target": target, "target_name": live[target].get("name"),
                    "chosen": True}
        if action == "create":
            if any(_casefold(v.get("name")) == _casefold(name) for v in live.values()):
                return {**out, "action": "unresolved", "target": None, "target_name": None, "chosen": True,
                        "note": f"a {kind} called {name!r} exists already (Home Assistant needs unique names): use it"}
            return {**out, "action": "create", "target": None, "target_name": name, "chosen": True}
        if action == "drop":
            return {**out, "action": "drop", "target": None, "target_name": None, "chosen": True,
                    "ack": f"drop_{kind}:{source_id}"}
        raise PlanError(f"Unknown action {action!r} for the {kind} {name!r}")
    if source_id in live:
        target = live[source_id]
        note = None if _casefold(target.get("name")) == _casefold(name) else (
            f"renamed meanwhile: {name} → {target.get('name')}")
        return {**out, "action": "use", "target": source_id, "target_name": target.get("name"), "match": "id",
                "note": note}
    by_name = [k for k, v in live.items() if _casefold(v.get("name")) == _casefold(name)]
    if len(by_name) == 1:
        return {**out, "action": "use", "target": by_name[0], "target_name": live[by_name[0]].get("name"),
                "match": "name", "note": f"the existing {kind} with the same name is used ({by_name[0]})"}
    if len(by_name) > 1:
        return {**out, "action": "unresolved", "target": None, "target_name": None,
                "note": f"several {kind}s are called {name!r}: choose one"}
    return {**out, "action": "create", "target": None, "target_name": name, "match": None,
            "note": f"missing: it will be created"}


def _new_id(taken: set[str], seed: str) -> str:
    """A new numeric id like the ones Home Assistant's editor creates, derived from the archive and the
    automation: the same plan computed again (at execution) gives the same id."""
    base = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16) % 9_000_000_000_000 + 1_000_000_000_000
    while str(base) in taken:
        base += 1
    return str(base)


async def build_plan(archive: Archive, snap: Snapshot, decisions: dict[str, Any], *, instance_id: str,
                     folder: HaConfigFolder, ha: HomeAssistantClient | None = None) -> dict[str, Any]:
    if not isinstance(decisions, dict):
        raise PlanError("The decisions must be an object")
    records = archive.records
    by_key = {r["key"]: r for r in records if isinstance(r, dict) and isinstance(r.get("key"), str)}
    selection = decisions.get("selection")
    if selection is None:
        selection = [k for k, r in by_key.items() if r.get("restorable") in ("full", "yaml_only")
                     and r.get("source") not in ("external", "integration", "orphan") and r.get("owner") != "builder"]
    if not isinstance(selection, list) or not all(isinstance(k, str) for k in selection):
        raise PlanError("The selection must be a list of automation keys")
    unknown = [k for k in selection if k not in by_key]
    if unknown:
        raise PlanError(f"Not in this archive: {', '.join(unknown[:5])}", "unknown_key")
    collisions = decisions.get("collisions") or {}
    new_ids = decisions.get("new_ids") or {}
    acks = set(decisions.get("acknowledgements") or [])
    manifest = archive.manifest
    same_installation = (manifest.get("source_installation") or {}).get("app_instance_id") == instance_id

    labels_def = archive.definitions("labels")
    categories_def = archive.definitions("categories")
    areas_def = archive.definitions("areas")
    blueprints_def = archive.definitions("blueprints")

    selected = [by_key[k] for k in selection]
    used_labels = sorted({i for r in selected for i in ((r.get("registry") or {}).get("labels") or [])})
    used_categories = sorted({(s, i) for r in selected for s, i in ((r.get("registry") or {}).get("categories") or {}).items()})
    used_areas = sorted({(r.get("registry") or {}).get("area_id") for r in selected} - {None})

    label_plan = {i: _resolve("label", labels_def.get(i) or {"label_id": i, "name": i}, snap.labels,
                              (decisions.get("labels") or {}).get(i), "label_id") for i in used_labels}
    category_plan = {f"{s}:{i}": {**_resolve("category", (categories_def.get(s) or {}).get(i) or {
        "category_id": i, "name": i}, snap.categories.get(s) or {}, (decisions.get("categories") or {}).get(
        f"{s}:{i}"), "category_id"), "scope": s} for s, i in used_categories}
    area_plan = {i: _resolve("area", areas_def.get(i) or {"area_id": i, "name": i}, snap.areas,
                             (decisions.get("areas") or {}).get(i), "area_id") for i in used_areas}
    for key, entry in category_plan.items():
        if entry["scope"] != "automation" and entry["action"] == "create":
            entry.update(action="unresolved", note="only automation categories can be recreated: choose one")

    live_ids = set(snap.configs) | {(e.get("unique_id")) for e in snap.registry.values()
                                    if e.get("platform") == "automation"}
    entity_owner = {eid: e.get("unique_id") for eid, e in snap.registry.items()}
    for eid in snap.states:
        if eid.startswith("automation.") and eid not in entity_owner:
            entity_owner[eid] = (snap.states[eid].get("attributes") or {}).get("id")
    taken_ids = set(live_ids) | set(new_ids.values())
    blueprint_plan: dict[str, dict[str, Any]] = {}
    items = []
    for record in records:
        if record.get("key") not in selection:
            items.append({"key": record.get("key"), "alias": record.get("alias"), "config_id": record.get("config_id"),
                          "source": record.get("source"), "owner": record.get("owner"),
                          "restorable": record.get("restorable"), "problems": [], "acks": [],
                          "action": "not_selected"})
            continue
        key = record["key"]
        config = record.get("config")
        config_id = record.get("config_id")
        reg = record.get("registry") or {}
        problems: list[dict[str, Any]] = []
        needs: list[str] = []
        item: dict[str, Any] = {"key": key, "alias": record.get("alias"), "config_id": config_id,
                                "source": record.get("source"), "owner": record.get("owner"),
                                "restorable": record.get("restorable"), "problems": problems, "acks": needs}
        items.append(item)
        if not isinstance(config, dict):
            item.update(action="blocked", reason="The archive has no configuration for it")
            continue
        external = record.get("source") in ("external", "integration", "orphan")
        choice = collisions.get(key)
        existing = snap.configs.get(config_id) if config_id else None
        id_in_use = bool(config_id) and config_id in live_ids
        if config_id is not None and not AutomationConfigApi.addressable(config_id) and choice != "new":
            item.update(action="skip", reason="Its id cannot be written through Home Assistant's automation "
                                              "editor: choose “restore as new”", collision="protected")
            continue
        if external or record.get("owner") == "builder" or is_builder_id(config_id):
            why = ("it was loaded from another YAML file and still is (or is maintained there)" if external else
                   "it belongs to Automation Builder: restore it from Automation Builder")
            if choice != "new":
                item.update(action="skip", reason=f"Not restored to its own id: {why}. Choose “restore as new” "
                                                  "to create a separate copy.", collision="protected")
                continue
            needs.append(f"copy:{key}")
        if choice == "new":
            target_id = new_ids.get(key) or _new_id(taken_ids, f"{archive.sha256}:{key}")
            if not AutomationConfigApi.addressable(target_id) or is_builder_id(target_id) or target_id in live_ids:
                raise PlanError(f"The new id {target_id!r} for {record.get('alias')!r} is not usable", "bad_new_id")
            taken_ids.add(target_id)
            item.update(action="new", target_id=target_id)
        elif id_in_use:
            if existing is not None and same(existing, config):
                item.update(action="metadata", target_id=config_id, collision="identical",
                            reason="The same configuration is in Home Assistant: only its metadata is restored")
            elif existing is None:
                item.update(action="skip", target_id=config_id, collision="external",
                            reason="An automation with this id is loaded from another YAML file: choose “restore "
                                   "as new”")
                continue
            elif choice == "replace":
                item.update(action="replace", target_id=config_id, collision="different",
                            diff=differences(existing, config, limit=8))
            else:
                item.update(action="skip", target_id=config_id, collision="different",
                            diff=differences(existing, config, limit=8),
                            reason="An automation with this id exists and differs: skip, replace (after a "
                                   "backup) or restore as new")
                continue
        else:
            if not config_id:
                target_id = _new_id(taken_ids, f"{archive.sha256}:{key}")
                taken_ids.add(target_id)
                item.update(action="new", target_id=target_id, reason="It has no id: a new id is assigned")
            else:
                item.update(action="create", target_id=config_id)
        target_id = item["target_id"]
        write = copy.deepcopy(config)
        if "id" in write or item["action"] == "new":
            write = {"id": target_id, **{k: v for k, v in write.items() if k != "id"}}
        item["config"] = write

        # entity id
        archived_entity = record.get("entity_id")
        if item["action"] == "new":
            item["entity_target"] = None
        elif archived_entity:
            owner = entity_owner.get(archived_entity)
            if archived_entity in entity_owner and owner != target_id:
                item["entity_target"] = None
                needs.append(f"entity_id:{key}")
                problems.append({"level": "warning", "code": "entity_id_taken",
                                 "message": f"{archived_entity} is used by another entity now: Home Assistant "
                                            "assigns another entity id."})
            else:
                item["entity_target"] = archived_entity
        else:
            item["entity_target"] = None

        # metadata
        if record.get("restorable") == "full":
            labels, categories, area = [], {}, None
            for label_id in reg.get("labels") or []:
                entry = label_plan[label_id]
                if entry["action"] == "drop":
                    needs.append(entry["ack"])
                elif entry["action"] == "unresolved":
                    problems.append({"level": "error", "code": "label_unresolved", "message": entry["note"]})
                else:
                    labels.append({"source": label_id, "plan": entry})
            for scope, category_id in (reg.get("categories") or {}).items():
                entry = category_plan[f"{scope}:{category_id}"]
                if entry["action"] == "drop":
                    needs.append(entry["ack"])
                elif entry["action"] == "unresolved":
                    problems.append({"level": "error", "code": "category_unresolved", "message": entry["note"]})
                else:
                    categories[scope] = {"source": category_id, "plan": entry}
            if reg.get("area_id"):
                entry = area_plan[reg["area_id"]]
                if entry["action"] == "drop":
                    needs.append(entry["ack"])
                elif entry["action"] == "unresolved":
                    problems.append({"level": "error", "code": "area_unresolved", "message": entry["note"]})
                else:
                    area = {"source": reg["area_id"], "plan": entry}
            item["metadata"] = {
                "labels": [lab["source"] for lab in labels],
                "categories": {s: c["source"] for s, c in categories.items()},
                "area": area["source"] if area else None,
                "name": reg.get("name"), "icon": reg.get("icon"), "aliases": reg.get("aliases") or [],
                "hidden_by": "user" if reg.get("hidden_by") else None,
                "disabled_by": "user" if reg.get("disabled_by") else None,
                "exposure": exposure(reg),
            }
            if reg.get("disabled_by") not in (None, "user"):
                problems.append({"level": "warning", "code": "disabled_by_other",
                                 "message": f"It was disabled by {reg.get('disabled_by')}; it is restored as "
                                            "disabled by the user"})
            state = (record.get("state") or {}).get("state")
            item["state"] = state if state in ("on", "off") else None
            item["expected_unavailable"] = state == "unavailable"
        else:
            item["metadata"] = None
            item["state"] = None
            item["expected_unavailable"] = False
            if record.get("restorable") == "yaml_only":
                problems.append({"level": "info", "code": "yaml_only",
                                 "message": "The source file has no labels, category, area or state: only the "
                                            "configuration is restored."})
            else:
                needs.append(f"no_metadata:{key}")
                problems.append({"level": "warning", "code": "no_metadata",
                                 "message": "The archive has no restorable metadata for it: only the "
                                            "configuration is restored."})

        # blueprint
        bp = config.get("use_blueprint") if isinstance(config.get("use_blueprint"), dict) else None
        if bp:
            path = bp.get("path")
            info = blueprint_plan.get(path)
            if info is None:
                archived = blueprints_def.get(path) or {}
                content = archive.blueprint_file(path) if archived.get("included") else None
                installed = path in snap.blueprints and "metadata" in snap.blueprints[path]
                info = {"path": path, "installed": installed, "included": content is not None}
                if installed:
                    current = folder.read_blueprint(path)
                    if content is not None and current is not None and sha256_bytes(current) != sha256_bytes(content):
                        info.update(action="keep", note="The installed blueprint differs from the archived one: "
                                                        "the restored automation uses the installed version",
                                    ack=f"blueprint_differs:{path}")
                    else:
                        info.update(action="keep")
                elif content is not None:
                    info.update(action="install", sha256=sha256_bytes(content))
                else:
                    info.update(action="missing", ack=f"blueprint_missing:{path}",
                                note="Not installed and not in the archive: the automation stays unavailable "
                                     "until the blueprint is installed")
                blueprint_plan[path] = info
            item["blueprint"] = path
            if info.get("ack"):
                needs.append(info["ack"])
            if info["action"] == "missing":
                item["expected_unavailable"] = True

    # Home Assistant's own check of what will be written (blueprint automations are checked after substitution)
    if ha is not None:
        for item in items:
            if item.get("action") not in ACTIONS_WRITING or "use_blueprint" in item["config"]:
                continue
            config = item["config"]
            payload = {}
            for plural, singular in (("triggers", "trigger"), ("conditions", "condition"), ("actions", "action")):
                value = config.get(plural, config.get(singular))
                if value is not None:
                    payload[plural] = value
            try:
                result = await ha.command("validate_config", **payload) or {}
            except (HomeAssistantError, NotAllowed, ConnectionError, TimeoutError) as err:
                item["problems"].append({"level": "warning", "code": "not_validated",
                                         "message": f"Home Assistant could not check it: {err}"})
                continue
            invalid = {k: v.get("error") for k, v in result.items() if isinstance(v, dict) and not v.get("valid")}
            if invalid:
                item["problems"].append({"level": "warning", "code": "invalid_config",
                                         "message": "Home Assistant reports: " + "; ".join(
                                             f"{k}: {v}" for k, v in invalid.items()),
                                         "details": invalid})
                if not item.get("expected_unavailable"):
                    item["acks"].append(f"invalid:{item['key']}")

    for item in items:
        if any(p["level"] == "error" for p in item["problems"]) and item.get("action") not in ("skip", "blocked"):
            item["action_planned"] = item["action"]
            item["action"] = "blocked"
            item["reason"] = "; ".join(p["message"] for p in item["problems"] if p["level"] == "error")
    active = [i for i in items if i.get("action") in (*ACTIONS_WRITING, "metadata")]
    required = sorted({a for i in active for a in i["acks"]})
    missing_acks = [a for a in required if a not in acks]
    plan = {
        "archive_id": manifest.get("archive_id"), "archive_sha256": archive.sha256,
        "same_installation": same_installation,
        "source": {"location_name": (manifest.get("home_assistant") or {}).get("location_name"),
                   "ha_version": (manifest.get("home_assistant") or {}).get("version"),
                   "created_at": manifest.get("created_at")},
        "items": items,
        # every label / category / area of the selected automations, also unresolved ones (they must be chosen)
        "labels": list(label_plan.values()),
        "categories": list(category_plan.values()),
        "areas": list(area_plan.values()),
        "blueprints": list(blueprint_plan.values()),
        "acknowledgements_required": required, "acknowledgements_missing": missing_acks,
        "selection": list(selection),
        "counts": {a: sum(1 for i in items if i.get("action") == a)
                   for a in ("create", "replace", "new", "metadata", "skip", "blocked", "not_selected")},
        "ready": bool(active) and not missing_acks,
    }
    live_fingerprint = {
        i["key"]: [digest(snap.configs.get(i.get("target_id"))) if i.get("target_id") in snap.configs else None,
                   sorted(eid for eid, uid in entity_owner.items() if uid == i.get("target_id"))]
        for i in items}
    plan["plan_hash"] = digest({
        "archive": archive.sha256, "decisions": {k: decisions.get(k) for k in sorted(decisions) if k != "plan_hash"},
        "items": [{k: i.get(k) for k in ("key", "action", "target_id", "entity_target", "metadata", "state",
                                          "blueprint")} for i in items],
        "labels": label_plan, "categories": category_plan, "areas": area_plan, "blueprints": blueprint_plan,
        "live": live_fingerprint, "known": [sorted(snap.labels), sorted(snap.areas),
                                            {s: sorted(v) for s, v in snap.categories.items()}],
    })[:24]
    return plan


# ------------------------------------------------------------------------------------------------ execution
class RestoreRunner:
    """Executes a confirmed plan. Every step is journalled; the write scope is opened for exactly its ids."""

    def __init__(self, *, ha: HomeAssistantClient, api: AutomationConfigApi, archive: Archive,
                 plan: dict[str, Any], log: Callable[[str, str], None], item: Callable[..., None],
                 snapshot: Callable[..., Awaitable[Snapshot]], hook: Callable[[str, int], Awaitable[None]],
                 items_view: dict[str, Any]) -> None:
        self.ha = ha
        self.items_view = items_view  # the operation's per-automation journal entries (read)
        self.api = api
        self.archive = archive
        self.plan = plan
        self.log = log
        self.item = item
        self.snapshot = snapshot
        self.hook = hook
        self.created: dict[str, dict[str, str]] = {"labels": {}, "categories": {}, "areas": {}, "blueprints": {}}

    def _active(self) -> list[dict[str, Any]]:
        return [i for i in self.plan["items"] if i.get("action") in (*ACTIONS_WRITING, "metadata")]

    async def run(self) -> dict[str, Any]:
        active = self._active()
        config_ids = {i["target_id"] for i in active}
        entities = {i["entity_target"] for i in active if i.get("entity_target")}
        with self.ha.scoped(configs=config_ids, entities=entities, metadata=True) as scope:
            mapping = await self._metadata()
            written: list[dict[str, Any]] = []
            for index, entry in enumerate(active):
                if entry["action"] == "metadata":
                    continue
                await self.hook("restore_write", index)
                self.item(entry["key"], status="writing", target_id=entry["target_id"], action=entry["action"])
                try:
                    await self.api.put(entry["target_id"], entry["config"])
                except Exception as err:  # noqa: BLE001 - reported per automation
                    self.item(entry["key"], status="failed", message=str(err))
                    self.log("error", f"{entry['alias']}: Home Assistant did not accept it: {err}")
                    continue
                self.item(entry["key"], status="written")
                written.append(entry)
            pending = {i["target_id"]: i for i in active
                       if i["action"] == "metadata" or self._status(i["key"]) == "written"}
            found = await self._wait_registry(set(pending))
            for target_id, entry in pending.items():
                entity_id = found.get(target_id)
                if entity_id is None:
                    self.item(entry["key"], status="incomplete", message="Home Assistant created no entity for it")
                    self.log("error", f"{entry['alias']}: no entity appeared after {WAIT_SECONDS:.0f} s")
                    continue
                scope.entities.add(entity_id)
                try:
                    await self._apply_metadata(entry, entity_id, mapping, scope)
                except Exception as err:  # noqa: BLE001
                    self.item(entry["key"], status="incomplete", message=f"Metadata not restored: {err}")
                    self.log("error", f"{entry['alias']}: its metadata could not be restored: {err}")
                    continue
                if self._status(entry["key"]) not in ("incomplete", "failed"):
                    self.item(entry["key"], status="metadata_applied")
        return await self.verify(mapping)

    def _status(self, key: str) -> str | None:
        return (self.items_view.get(key) or {}).get("status")

    async def _metadata(self) -> dict[str, dict[str, str]]:
        mapping: dict[str, dict[str, str]] = {"labels": {}, "categories": {}, "areas": {}}
        for entry in self.plan["labels"]:
            if entry["action"] == "use":
                mapping["labels"][entry["source_id"]] = entry["target"]
            elif entry["action"] == "create":
                d = entry["definition"]
                params = {"name": d.get("name") or entry["source_id"]}
                for k in ("icon", "color", "description"):
                    if d.get(k) is not None:
                        params[k] = d[k]
                result = await self.ha.command("config/label_registry/create", **params)
                mapping["labels"][entry["source_id"]] = result["label_id"]
                self.created["labels"][entry["source_id"]] = result["label_id"]
                self.log("info", f"Created the label {params['name']} ({result['label_id']})")
        for entry in self.plan["categories"]:
            key = f"{entry['scope']}:{entry['source_id']}"
            if entry["action"] == "use":
                mapping["categories"][key] = entry["target"]
            elif entry["action"] == "create":
                d = entry["definition"]
                params = {"scope": "automation", "name": d.get("name") or entry["source_id"]}
                if d.get("icon"):
                    params["icon"] = d["icon"]
                result = await self.ha.command("config/category_registry/create", **params)
                mapping["categories"][key] = result["category_id"]
                self.created["categories"][key] = result["category_id"]
                self.log("info", f"Created the category {params['name']} ({result['category_id']})")
        for entry in self.plan["areas"]:
            if entry["action"] == "use":
                mapping["areas"][entry["source_id"]] = entry["target"]
            elif entry["action"] == "create":
                d = entry["definition"]
                params: dict[str, Any] = {"name": d.get("name") or entry["source_id"]}
                if d.get("icon"):
                    params["icon"] = d["icon"]
                if d.get("aliases"):
                    params["aliases"] = list(d["aliases"])
                result = await self.ha.command("config/area_registry/create", **params)
                mapping["areas"][entry["source_id"]] = result["area_id"]
                self.created["areas"][entry["source_id"]] = result["area_id"]
                self.log("info", f"Created the area {params['name']} ({result['area_id']})")
        for entry in self.plan["blueprints"]:
            if entry.get("action") != "install":
                continue
            content = self.archive.blueprint_file(entry["path"])
            if content is None:
                continue
            try:
                await self.ha.command("blueprint/save", domain="automation", path=entry["path"],
                                      yaml=content.decode("utf-8"))
            except HomeAssistantError as err:
                self.log("error", f"The blueprint {entry['path']} could not be installed: {err}")
                continue
            self.created["blueprints"][entry["path"]] = entry["path"]
            self.log("info", f"Installed the blueprint {entry['path']}")
        self.mapping = mapping
        return mapping

    async def _wait_registry(self, ids: set[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
        while ids - set(found):
            entries = await self.ha.command("config/entity_registry/list") or []
            for e in entries:
                if e.get("platform") == "automation" and e.get("unique_id") in ids:
                    found[e["unique_id"]] = e["entity_id"]
            if not ids - set(found) or asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.5)
        return found

    async def _wait_state(self, entity_id: str, timeout: float = 20.0) -> dict[str, Any] | None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            states = await self.ha.command("get_states") or []
            st = next((s for s in states if s.get("entity_id") == entity_id), None)
            if st is not None or asyncio.get_running_loop().time() > deadline:
                return st
            await asyncio.sleep(0.5)

    async def _apply_metadata(self, entry: dict[str, Any], entity_id: str, mapping: dict[str, dict[str, str]],
                              scope: Any) -> None:
        meta = entry.get("metadata")
        target = entry.get("entity_target")
        current = (await self.ha.command("config/entity_registry/get_entries", entity_ids=[entity_id]) or {}).get(
            entity_id) or {}
        if target and target != entity_id:
            scope.entities.add(target)
            await self.ha.command("config/entity_registry/update", entity_id=entity_id, new_entity_id=target)
            self.log("info", f"{entry['alias']}: entity id {entity_id} → {target}")
            entity_id = target
        self.item(entry["key"], entity_id=entity_id)
        if meta is None:
            return
        categories: dict[str, Any] = {s: None for s in (current.get("categories") or {})}
        for scope_name, source in meta["categories"].items():
            categories[scope_name] = mapping["categories"][f"{scope_name}:{source}"]
        update = {
            "name": meta["name"], "icon": meta["icon"],
            "area_id": mapping["areas"].get(meta["area"]) if meta["area"] else None,
            "labels": sorted(mapping["labels"][s] for s in meta["labels"]),
            "categories": categories, "aliases": meta["aliases"],
            "hidden_by": meta["hidden_by"],
        }
        await self.ha.command("config/entity_registry/update", entity_id=entity_id, **update)
        for assistant, expose in meta["exposure"].items():
            current_value = exposure(current).get(assistant)
            if current_value != expose:
                await self.ha.command("homeassistant/expose_entity", assistants=[assistant], entity_ids=[entity_id],
                                      should_expose=expose)
        was_disabled = current.get("disabled_by") is not None
        if not meta["disabled_by"] and was_disabled:
            await self.ha.command("config/entity_registry/update", entity_id=entity_id, disabled_by=None)
            await self.ha.command("call_service", domain="automation", service="reload",
                                  service_data={"id": entry["target_id"]})
            self.log("info", f"{entry['alias']}: enabled its registry entry again")
        if entry.get("state") in ("on", "off") and not meta["disabled_by"]:
            st = await self._wait_state(entity_id)
            if st is not None and st.get("state") in ("on", "off") and st.get("state") != entry["state"]:
                await self.ha.command("call_service", domain="automation",
                                      service="turn_on" if entry["state"] == "on" else "turn_off",
                                      target={"entity_id": entity_id})
        if meta["disabled_by"] and not was_disabled:
            await self.ha.command("config/entity_registry/update", entity_id=entity_id, disabled_by="user")

    async def verify(self, mapping: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
        """Compare what Home Assistant has now with the plan (configuration, identity, metadata, state)."""
        mapping = mapping or getattr(self, "mapping", {"labels": {}, "categories": {}, "areas": {}})
        await asyncio.sleep(SETTLE_SECONDS)
        snap = await self.snapshot(fetch=[e["target_id"] for e in self._active()])
        results: dict[str, Any] = {}
        for entry in self._active():
            key = entry["key"]
            if self._status(key) == "failed":
                results[key] = {"status": "failed", "discrepancies": [(self.items_view.get(key) or {}).get(
                    "message") or "not written"]}
                continue
            found: list[str] = []
            item = snap.item(entry["target_id"])
            if item is None or item.config is None:
                results[key] = {"status": "failed", "discrepancies": ["It is not in Home Assistant"]}
                self.item(key, status="failed", message="It is not in Home Assistant")
                continue
            if not same(item.config, entry["config"]):
                found.append("configuration differs: " + "; ".join(differences(entry["config"], item.config, limit=5)))
            reg = item.registry
            meta = entry.get("metadata")
            if entry.get("entity_target") and item.entity_id != entry["entity_target"]:
                found.append(f"entity id is {item.entity_id}, expected {entry['entity_target']}")
            if meta is not None:
                if reg is None:
                    found.append("no registry entry")
                else:
                    want_labels = sorted(mapping["labels"].get(s, s) for s in meta["labels"])
                    if sorted(reg.get("labels") or []) != want_labels:
                        found.append(f"labels are {sorted(reg.get('labels') or [])}, expected {want_labels}")
                    want_categories = {s: mapping["categories"].get(f"{s}:{c}", c) for s, c in meta["categories"].items()}
                    if (reg.get("categories") or {}) != want_categories:
                        found.append(f"categories are {reg.get('categories')}, expected {want_categories}")
                    want_area = mapping["areas"].get(meta["area"], meta["area"]) if meta["area"] else None
                    if reg.get("area_id") != want_area:
                        found.append(f"area is {reg.get('area_id')}, expected {want_area}")
                    for field_name in ("name", "icon"):
                        if reg.get(field_name) != meta[field_name]:
                            found.append(f"{field_name} is {reg.get(field_name)!r}, expected {meta[field_name]!r}")
                    if list(reg.get("aliases") or []) != list(meta["aliases"]):
                        found.append("voice aliases differ")
                    if (reg.get("hidden_by") or None) != meta["hidden_by"]:
                        found.append(f"hidden is {reg.get('hidden_by')}, expected {meta['hidden_by']}")
                    if (reg.get("disabled_by") or None) != meta["disabled_by"]:
                        found.append(f"disabled is {reg.get('disabled_by')}, expected {meta['disabled_by']}")
                    live_exposure = exposure(reg)
                    for assistant in ASSISTANTS:
                        want = meta["exposure"].get(assistant)
                        if want is True and live_exposure.get(assistant) is not True:
                            found.append(f"not exposed to {assistant}")
                        if want is False and live_exposure.get(assistant) is True:
                            found.append(f"exposed to {assistant}")
            expected_unavailable = entry.get("expected_unavailable") and item.state == "unavailable"
            if entry.get("state") in ("on", "off") and not (meta or {}).get("disabled_by") and not expected_unavailable:
                if item.state != entry["state"]:
                    found.append(f"it is {item.state}, expected {entry['state']}")
            if item.state == "unavailable" and not entry.get("expected_unavailable"):
                found.append("Home Assistant could not load it (unavailable)")
            status = "restored" if not found else "incomplete"
            results[key] = {"status": status, "discrepancies": found, "entity_id": item.entity_id,
                            "target_id": entry["target_id"]}
            self.item(key, status=status, discrepancies=found, entity_id=item.entity_id)
        statuses = [r["status"] for r in results.values()]
        overall = ("completed" if statuses and all(s == "restored" for s in statuses) else
                   "failed" if statuses and all(s == "failed" for s in statuses) else
                   "incomplete" if statuses else "nothing")
        return {"status": overall, "results": results, "created": self.created,
                "counts": {s: statuses.count(s) for s in ("restored", "incomplete", "failed")}}
