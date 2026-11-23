"""Restoring helpers, labels and categories from a registry archive: a read-only plan, then a confirmed run that is
verified against Home Assistant.

Decisions (all optional; defaults in brackets)::

    {"selection": [keys] [every item of the archive],
     "collisions": {key: "skip" | "update"} [skip]  - an item that exists with other settings,
     "assignments": true | false [true]  - put labels and categories back on the entities, devices and areas
                                            that carried them}

Per item:

* **label / category** - the same id exists: *already there* (identical) or a collision (skip / update its name,
  icon, colour, description); otherwise one with the same name: *use the existing one*; otherwise *create* (Home
  Assistant gives a label an id from its name and a category a random id: the archive's ids are mapped).
* **helper** - the same id exists: *already there*, *settings only* (the configuration is identical, the entity's
  name, area, labels ... differ) or a collision (skip / replace); otherwise *create* with its original id. Home
  Assistant derives a new helper's id from its name, so the App creates it under its id as the name and renames
  it to its real name right after (the same two calls the helper editor makes).
* **assignments** - every entity, device and area of the archive that exists now gets the label again; every
  entity that exists and has no category in that scope gets the category again (an entity that has another
  category there keeps it: reported, never overwritten).

Labels and categories a restored helper uses but that are not in the selection are mapped the same way (same id,
same name) and created when they are missing; an area that no longer exists is left empty (reported).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

from .canonical import differences, digest, same
from .ha import HELPER_DOMAINS, HomeAssistantClient, HomeAssistantError, NotAllowed, registry_key
from .regarchive import RegistryArchive
from .registry import (HELPER_NAMES, RegistrySnapshot, category_definition, helper_config, label_definition,
                       take_registry_snapshot)
from .restore import PlanError

_LOGGER = logging.getLogger(__name__)
SETTLE_SECONDS = 1.0
WRITING = ("create", "update", "replace", "metadata")
ACTION_TEXT = {"create": "Create", "exists": "Already there", "use_existing": "Use the existing one (same name)",
               "update": "Update it", "replace": "Replace it", "metadata": "Restore its settings", "skip": "Skip",
               "not_selected": "Not selected", "blocked": "Blocked"}


def _fold(value: Any) -> str:
    return str(value or "").strip().casefold()


def _label_fields(definition: dict[str, Any]) -> dict[str, Any]:
    return {k: definition.get(k) for k in ("name", "icon", "color", "description")}


def _items(archive: RegistryArchive) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label_id, record in archive.labels.items():
        out[registry_key("label", label_id)] = {"kind": "label", "id": label_id, **record}
    for ckey, record in archive.categories.items():
        out[registry_key("category", ckey)] = {"kind": "category", "id": ckey.partition(":")[2], **record}
    for key, record in archive.helpers.items():
        out[key] = {"kind": "helper", **record}
    return out


def _resolve_label(definition: dict[str, Any], snap: RegistrySnapshot) -> tuple[str, str | None]:
    """("exists" | "differs" | "same_name" | "missing", live id)."""
    label_id = definition.get("label_id")
    live = snap.labels.get(label_id)
    if live is not None:
        return ("exists" if label_definition(live) == definition else "differs"), label_id
    for other_id, other in snap.labels.items():
        if _fold(other.get("name")) == _fold(definition.get("name")):
            return "same_name", other_id
    return "missing", None


def _resolve_category(scope: str, definition: dict[str, Any], snap: RegistrySnapshot) -> tuple[str, str | None]:
    defs = snap.categories.get(scope) or {}
    category_id = definition.get("category_id")
    if category_id in defs:
        return ("exists" if category_definition(defs[category_id]) == definition else "differs"), category_id
    for other_id, other in defs.items():
        if _fold(other.get("name")) == _fold(definition.get("name")):
            return "same_name", other_id
    return "missing", None


def build_registry_plan(archive: RegistryArchive, snap: RegistrySnapshot, decisions: dict[str, Any], *,
                        instance_id: str) -> dict[str, Any]:
    if not isinstance(decisions, dict):
        raise PlanError("The decisions must be an object")
    records = _items(archive)
    selection = decisions.get("selection")
    if selection is None:
        selection = list(records)
    if not isinstance(selection, list) or not all(isinstance(k, str) for k in selection):
        raise PlanError("The selection must be a list of item keys")
    unknown = [k for k in selection if k not in records]
    if unknown:
        raise PlanError(f"Not in this archive: {', '.join(unknown[:5])}", "unknown_key")
    collisions = decisions.get("collisions") or {}
    assignments = decisions.get("assignments", True) is not False
    items = []
    for key, record in records.items():
        kind = record["kind"]
        item: dict[str, Any] = {"key": key, "kind": kind, "problems": [], "notes": []}
        if kind == "label":
            definition = record["definition"]
            item.update(name=definition.get("name"), id=record["id"])
        elif kind == "category":
            definition = record["definition"]
            item.update(name=definition.get("name"), id=record["id"], scope=record.get("scope"))
        else:
            item.update(name=(record.get("registry") or {}).get("name") or (record.get("config") or {}).get("name")
                        or record.get("id"), id=record.get("id"), domain=record.get("domain"),
                        type=HELPER_NAMES.get(record.get("domain"), record.get("domain")),
                        entity_id=record.get("entity_id"))
        items.append(item)
        if key not in selection:
            item["action"] = "not_selected"
            continue
        choice = collisions.get(key)
        if kind in ("label", "category"):
            state, target = (_resolve_label(definition, snap) if kind == "label"
                             else _resolve_category(record.get("scope"), definition, snap))
            item["target"] = target
            if state == "exists":
                item["action"] = "exists"
            elif state == "differs":
                item["collision"] = "different"
                item["choices"] = ["skip", "update"]
                item["action"] = "update" if choice == "update" else "skip"
                live = snap.labels[target] if kind == "label" else snap.categories[record["scope"]][target]
                now = label_definition(live) if kind == "label" else category_definition(live)
                item["diff"] = differences(now, definition, limit=6)
            elif state == "same_name":
                item["action"] = "use_existing"
                item["notes"].append(f"A {kind} called “{definition.get('name')}” exists with another id: it is used")
            else:
                item["action"] = "create"
                if kind == "category" or definition.get("label_id") != _slug(definition.get("name")):
                    item["notes"].append("Home Assistant gives it a new id; the archive's id is mapped to it")
            if assignments and item["action"] != "skip":
                item["assign"] = _assignment_estimate(record, kind, target if item["action"] != "create" else None,
                                                      snap)
        else:
            domain = record.get("domain")
            config = record.get("config")
            if domain not in HELPER_DOMAINS or not isinstance(config, dict) or not isinstance(record.get("id"), str):
                item.update(action="blocked", reason="This kind of helper cannot be restored by the App")
                continue
            live = snap.helpers.get(key)
            if live is None:
                item["action"] = "create"
                taken = record.get("entity_id") and record["entity_id"] in snap.states and (
                    snap.entities.get(record["entity_id"]) or {}).get("unique_id") != record["id"]
                if taken:
                    item["notes"].append(f"{record['entity_id']} is taken by another entity: Home Assistant assigns "
                                         "another entity id")
                if _slug(record["id"]) != record["id"]:
                    item["notes"].append("Its id cannot be reproduced exactly: Home Assistant assigns one")
            elif same(live.config, config):
                expected = _mapped_registry(record, snap)
                item["action"] = "exists" if _registry_same(live.registry, expected) else "metadata"
                if item["action"] == "metadata":
                    item["diff"] = differences(live.registry or {}, expected or {}, limit=6)
            else:
                item["collision"] = "different"
                item["choices"] = ["skip", "replace"]
                item["action"] = "replace" if choice == "replace" else "skip"
                item["diff"] = differences(live.config, config, limit=6)
            if item["action"] in WRITING:
                uses = record.get("uses") or {}
                for label in (uses.get("labels") or {}).values():
                    state, _ = _resolve_label(label, snap)
                    if state == "missing" and registry_key("label", str(label.get("label_id"))) not in selection:
                        item["notes"].append(f"Its label “{label.get('name')}” is missing: it is created")
                area = uses.get("area")
                if area and area.get("area_id") not in snap.areas and not any(
                        _fold(a.get("name")) == _fold(area.get("name")) for a in snap.areas.values()):
                    item["notes"].append(f"Its area “{area.get('name')}” no longer exists: it is restored without "
                                         "an area")
    counts: dict[str, int] = {}
    for item in items:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    assign_total = sum(sum((i.get("assign") or {}).get(k, 0) for k in ("entities", "devices", "areas"))
                       for i in items)
    active = [i for i in items if i["action"] in (*WRITING, "use_existing", "exists")]
    writing = [i for i in items if i["action"] in WRITING]
    source = archive.manifest.get("source_installation") or {}
    plan = {
        "archive_id": archive.manifest.get("archive_id"), "kind": "registry", "items": items, "counts": counts,
        "selection": list(selection), "assignments": assignments, "assignments_total": assign_total,
        "ready": bool(writing) or (assignments and assign_total > 0 and bool(active)),
        "same_installation": source.get("app_instance_id") == instance_id,
        "source": {"location_name": (archive.manifest.get("home_assistant") or {}).get("location_name"),
                   "ha_version": (archive.manifest.get("home_assistant") or {}).get("version"),
                   "created_at": archive.manifest.get("created_at")},
    }
    plan["plan_hash"] = digest({"archive": archive.sha256, "decisions": {
        "selection": plan["selection"], "collisions": collisions, "assignments": assignments},
        "items": [[i["key"], i["action"], i.get("target"), i.get("assign")] for i in items]})[:24]
    return plan


def _slug(value: Any) -> str:
    import re
    text = re.sub(r"[^a-z0-9_]+", "_", str(value or "").lower())
    return re.sub(r"_+", "_", text).strip("_")


def _mapped_registry(record: dict[str, Any], snap: RegistrySnapshot) -> dict[str, Any] | None:
    """The archived registry settings of a helper with its labels, categories and area mapped to this installation
    (same id, else same name) - what a restore would set."""
    archived = record.get("registry")
    if archived is None:
        return None
    uses = record.get("uses") or {}
    labels = []
    for label_id in archived.get("labels") or []:
        definition = (uses.get("labels") or {}).get(label_id) or {"label_id": label_id}
        labels.append(_resolve_label(definition, snap)[1] or label_id)
    categories = {}
    for scope, category_id in (archived.get("categories") or {}).items():
        definition = (uses.get("categories") or {}).get(f"{scope}:{category_id}") or {"category_id": category_id}
        categories[scope] = _resolve_category(scope, definition, snap)[1] or category_id
    area = archived.get("area_id")
    if area and area not in snap.areas:
        name = ((uses.get("area") or {}).get("name"))
        area = next((a for a, d in snap.areas.items() if name and _fold(d.get("name")) == _fold(name)), area)
    return {**archived, "labels": sorted(labels), "categories": dict(sorted(categories.items())), "area_id": area}


def _registry_same(live: dict[str, Any] | None, archived: dict[str, Any] | None) -> bool:
    keys = ("entity_id", "name", "icon", "area_id", "labels", "categories", "aliases", "hidden_by", "disabled_by")
    return {k: (live or {}).get(k) for k in keys} == {k: (archived or {}).get(k) for k in keys}


def _assignment_estimate(record: dict[str, Any], kind: str, target: str | None,
                         snap: RegistrySnapshot) -> dict[str, int]:
    out = {"entities": 0, "devices": 0, "areas": 0, "missing": 0, "conflicts": 0, "present": 0}
    assigned = record.get("assignments") or {}
    for group, live in (("entities", snap.entities), ("devices", snap.devices), ("areas", snap.areas)):
        for ref in assigned.get(group) or []:
            ref_id = ref.get("id") if isinstance(ref, dict) else ref
            entry = live.get(ref_id)
            if entry is None:
                out["missing"] += 1
                continue
            if kind == "label":
                if target is not None and target in (entry.get("labels") or []):
                    out["present"] += 1
                else:
                    out[group] += 1
            else:
                current = (entry.get("categories") or {}).get(record.get("scope"))
                if current is None:
                    out[group] += 1
                elif current == target:
                    out["present"] += 1
                else:
                    out["conflicts"] += 1
    return out


class RegistryRestoreRunner:
    """Executes a plan. Every write is allowed only for the items of this plan (WriteScope)."""

    def __init__(self, *, ha: HomeAssistantClient, archive: RegistryArchive, plan: dict[str, Any],
                 log: Callable[[str, str], None], item: Callable[..., None], now: Callable[[], str],
                 hook: Callable[[str, int], Awaitable[None]] | None = None) -> None:
        self.ha = ha
        self.archive = archive
        self.plan = plan
        self.log = log
        self.item = item
        self.now = now
        self.hook = hook
        self.records = _items(archive)
        self.labels: dict[str, str] = {}        # archived label id -> live id
        self.categories: dict[str, str] = {}    # "<scope>:<archived id>" -> live id
        self.created: dict[str, list[str]] = {"labels": [], "categories": [], "helpers": []}
        self.discrepancies: dict[str, list[str]] = {}
        self.areas_map: dict[str, str] = {}      # archived area id -> live area id (same id, else same name)

    def _active(self, kind: str) -> list[dict[str, Any]]:
        return [i for i in self.plan["items"] if i["kind"] == kind
                and i["action"] in (*WRITING, "exists", "use_existing")]

    def _note(self, key: str, message: str) -> None:
        self.discrepancies.setdefault(key, []).append(message)

    async def run(self) -> dict[str, Any]:
        snap = await take_registry_snapshot(self.ha, now=self.now())
        await self._labels(snap)
        snap = await take_registry_snapshot(self.ha, now=self.now())
        await self._categories(snap)
        snap = await take_registry_snapshot(self.ha, now=self.now())
        await self._dependencies(snap)
        await self._helpers()
        if self.plan.get("assignments"):
            await asyncio.sleep(SETTLE_SECONDS)
            snap = await take_registry_snapshot(self.ha, now=self.now())
            await self._assignments(snap)
        return await self.verify()

    # ----------------------------------------------------------------- labels and categories
    async def _labels(self, snap: RegistrySnapshot) -> None:
        for index, item in enumerate(self._active("label")):
            key = item["key"]
            definition = self.records[key]["definition"]
            if self.hook:
                await self.hook("registry_restore", index)
            self.item(key, status="writing", action=item["action"])
            try:
                if item["action"] == "create":
                    with self.ha.scoped(metadata=True):
                        result = await self.ha.command("config/label_registry/create", **{
                            k: v for k, v in _label_fields(definition).items() if v is not None})
                    self.labels[definition["label_id"]] = result["label_id"]
                    self.created["labels"].append(result["label_id"])
                elif item["action"] == "update":
                    with self.ha.scoped(registry_updates={key}):
                        await self.ha.command("config/label_registry/update", label_id=item["target"],
                                              **_label_fields(definition))
                    self.labels[definition["label_id"]] = item["target"]
                else:
                    self.labels[definition["label_id"]] = item["target"]
                self.item(key, status="written", target=self.labels[definition["label_id"]])
            except (HomeAssistantError, NotAllowed) as err:
                self.item(key, status="failed", message=str(err))
                self.log("error", f"Label {definition.get('name')}: {err}")

    async def _categories(self, snap: RegistrySnapshot) -> None:
        for item in self._active("category"):
            key = item["key"]
            record = self.records[key]
            scope = record["scope"]
            definition = record["definition"]
            self.item(key, status="writing", action=item["action"])
            try:
                if item["action"] == "create":
                    with self.ha.scoped(metadata=True, category_scopes={scope}):
                        result = await self.ha.command("config/category_registry/create", scope=scope, **{
                            k: v for k, v in {"name": definition.get("name"), "icon": definition.get("icon")}.items()
                            if v is not None})
                    target = result["category_id"]
                    self.created["categories"].append(f"{scope}:{target}")
                elif item["action"] == "update":
                    target = item["target"]
                    with self.ha.scoped(registry_updates={registry_key("category", scope, target)}):
                        await self.ha.command("config/category_registry/update", scope=scope, category_id=target,
                                              name=definition.get("name"), icon=definition.get("icon"))
                else:
                    target = item["target"]
                self.categories[f"{scope}:{definition['category_id']}"] = target
                self.item(key, status="written", target=target)
            except (HomeAssistantError, NotAllowed) as err:
                self.item(key, status="failed", message=str(err))
                self.log("error", f"Category {definition.get('name')}: {err}")

    async def _dependencies(self, snap: RegistrySnapshot) -> None:
        """Labels and categories that restored helpers use but that were not selected: mapped, or created."""
        for item in [i for i in self.plan["items"] if i["kind"] == "helper" and i["action"] in WRITING]:
            uses = self.records[item["key"]].get("uses") or {}
            for label in (uses.get("labels") or {}).values():
                if label.get("label_id") in self.labels:
                    continue
                state, target = _resolve_label(label, snap)
                if state == "missing":
                    try:
                        with self.ha.scoped(metadata=True):
                            result = await self.ha.command("config/label_registry/create", **{
                                k: v for k, v in _label_fields(label).items() if v is not None})
                        target = result["label_id"]
                        snap.labels[target] = result
                        self.created["labels"].append(target)
                        self.log("info", f"Label “{label.get('name')}” created (used by a restored helper)")
                    except (HomeAssistantError, NotAllowed) as err:
                        self.log("warning", f"Label “{label.get('name')}” could not be created: {err}")
                        continue
                self.labels[label["label_id"]] = target
            for ckey, category in (uses.get("categories") or {}).items():
                if ckey in self.categories:
                    continue
                scope = ckey.partition(":")[0]
                state, target = _resolve_category(scope, category, snap)
                if state == "missing":
                    try:
                        with self.ha.scoped(metadata=True, category_scopes={scope}):
                            result = await self.ha.command("config/category_registry/create", scope=scope, **{
                                k: v for k, v in {"name": category.get("name"), "icon": category.get("icon")}.items()
                                if v is not None})
                        target = result["category_id"]
                        snap.categories.setdefault(scope, {})[target] = result
                        self.created["categories"].append(f"{scope}:{target}")
                    except (HomeAssistantError, NotAllowed) as err:
                        self.log("warning", f"Category “{category.get('name')}” could not be created: {err}")
                        continue
                self.categories[ckey] = target
            area = uses.get("area")
            if area:
                if area.get("area_id") in snap.areas:
                    self.areas_map[area["area_id"]] = area["area_id"]
                else:
                    match = next((a for a, d in snap.areas.items() if _fold(d.get("name")) == _fold(area.get("name"))),
                                 None)
                    if match:
                        self.areas_map[area["area_id"]] = match

    # ----------------------------------------------------------------- helpers
    async def _helpers(self) -> None:
        for index, item in enumerate([i for i in self.plan["items"] if i["kind"] == "helper"
                                      and i["action"] in WRITING]):
            key = item["key"]
            record = self.records[key]
            domain, item_id = record["domain"], record["id"]
            config = helper_config(record["config"])
            if self.hook:
                await self.hook("registry_restore_helper", index)
            self.item(key, status="writing", action=item["action"])
            try:
                live_id = item_id
                if item["action"] == "create":
                    with self.ha.scoped(helper_create={domain}):
                        created = await self.ha.command(f"{domain}/create", **{**config, "name": item_id})
                    live_id = str(created.get("id"))
                    self.created["helpers"].append(f"{domain}.{live_id}")
                    if live_id != item_id:
                        self._note(key, f"Home Assistant gave it the id {live_id} instead of {item_id}")
                    with self.ha.scoped(registry_updates={registry_key("helper", domain, live_id)}):
                        await self.ha.command(f"{domain}/update", **{f"{domain}_id": live_id, **config})
                elif item["action"] == "replace":
                    with self.ha.scoped(registry_updates={key}):
                        await self.ha.command(f"{domain}/update", **{f"{domain}_id": item_id, **config})
                self.item(key, status="written", target=live_id)
                await self._helper_registry(key, record, domain, live_id)
            except (HomeAssistantError, NotAllowed, ConnectionError, TimeoutError) as err:
                self.item(key, status="failed", message=str(err))
                self.log("error", f"Helper {domain}.{item_id}: {err}")

    async def _helper_registry(self, key: str, record: dict[str, Any], domain: str, live_id: str) -> None:
        archived = record.get("registry")
        if not archived:
            return
        entity_id = None
        for _ in range(40):
            entries = await self.ha.command("config/entity_registry/list") or []
            entity_id = next((e["entity_id"] for e in entries if isinstance(e, dict) and e.get("platform") == domain
                              and e.get("unique_id") == live_id), None)
            if entity_id:
                break
            await asyncio.sleep(0.25)
        if entity_id is None:
            self._note(key, "Its entity did not appear in Home Assistant")
            return
        areas_map = self.areas_map
        changes: dict[str, Any] = {
            "name": archived.get("name"), "icon": archived.get("icon"),
            "area_id": areas_map.get(archived.get("area_id")) if archived.get("area_id") else None,
            "labels": sorted({self.labels[i] for i in archived.get("labels") or [] if i in self.labels}),
            "aliases": list(archived.get("aliases") or []), "hidden_by": archived.get("hidden_by"),
            "disabled_by": archived.get("disabled_by"),
        }
        categories = {}
        for scope, category_id in (archived.get("categories") or {}).items():
            mapped = self.categories.get(f"{scope}:{category_id}")
            if mapped:
                categories[scope] = mapped
        if categories:
            changes["categories"] = categories
        wanted = archived.get("entity_id")
        if wanted and wanted != entity_id:
            entries = {e.get("entity_id") for e in await self.ha.command("config/entity_registry/list") or []
                       if isinstance(e, dict)}
            if wanted not in entries:
                changes["new_entity_id"] = wanted
        with self.ha.scoped(helper_entities={entity_id}):
            result = await self.ha.command("config/entity_registry/update", entity_id=entity_id, **changes)
            entity_id = ((result or {}).get("entity_entry") or {}).get("entity_id") or changes.get(
                "new_entity_id") or entity_id
            exposure = archived.get("exposure") or {}
            for should in (True, False):
                assistants = sorted(a for a, v in exposure.items() if v is should)
                if assistants:
                    await self.ha.command("homeassistant/expose_entity", assistants=assistants,
                                          entity_ids=[entity_id], should_expose=should)
        self.item(key, entity_id=entity_id)

    # ----------------------------------------------------------------- assignments
    async def _assignments(self, snap: RegistrySnapshot) -> None:
        entity_labels: dict[str, set[str]] = {}
        entity_categories: dict[str, dict[str, str]] = {}
        device_labels: dict[str, set[str]] = {}
        area_labels: dict[str, set[str]] = {}
        for item in self.plan["items"]:
            if item["kind"] == "helper" or item["action"] in ("skip", "not_selected", "blocked"):
                continue
            record = self.records[item["key"]]
            assigned = record.get("assignments") or {}
            if item["kind"] == "label":
                live = self.labels.get(record["id"])
                if live is None:
                    continue
                for group, target in (("entities", entity_labels), ("devices", device_labels),
                                      ("areas", area_labels)):
                    for ref in assigned.get(group) or []:
                        target.setdefault(ref["id"], set()).add(live)
            else:
                scope = record["scope"]
                live = self.categories.get(f"{scope}:{record['definition']['category_id']}")
                if live is None:
                    continue
                for ref in assigned.get("entities") or []:
                    current = ((snap.entities.get(ref["id"]) or {}).get("categories") or {}).get(scope)
                    if current is None:
                        entity_categories.setdefault(ref["id"], {})[scope] = live
                    elif current != live:
                        self._note(item["key"], f"{ref['id']} has another category in this scope: kept")
        missing = 0
        for entity_id in sorted(set(entity_labels) | set(entity_categories)):
            entry = snap.entities.get(entity_id)
            if entry is None:
                missing += 1
                continue
            changes: dict[str, Any] = {}
            add = entity_labels.get(entity_id, set()) - set(entry.get("labels") or [])
            if add:
                changes["labels"] = sorted(set(entry.get("labels") or []) | add)
            if entity_categories.get(entity_id):
                changes["categories"] = entity_categories[entity_id]
            if changes:
                with self.ha.scoped(assign_entities={entity_id}):
                    await self.ha.command("config/entity_registry/update", entity_id=entity_id, **changes)
        for device_id, labels in sorted(device_labels.items()):
            entry = snap.devices.get(device_id)
            if entry is None:
                missing += 1
                continue
            if labels - set(entry.get("labels") or []):
                with self.ha.scoped(devices={device_id}):
                    await self.ha.command("config/device_registry/update", device_id=device_id,
                                          labels=sorted(set(entry.get("labels") or []) | labels))
        for area_id, labels in sorted(area_labels.items()):
            entry = snap.areas.get(area_id)
            if entry is None:
                missing += 1
                continue
            if labels - set(entry.get("labels") or []):
                with self.ha.scoped(areas={area_id}):
                    await self.ha.command("config/area_registry/update", area_id=area_id,
                                          labels=sorted(set(entry.get("labels") or []) | labels))
        if missing:
            self.log("warning", f"{missing} entit(ies), device(s) or area(s) of the archive no longer exist: their "
                                "labels and categories were not restored")

    # ----------------------------------------------------------------- verification
    async def verify(self) -> dict[str, Any]:
        await asyncio.sleep(SETTLE_SECONDS)
        snap = await take_registry_snapshot(self.ha, now=self.now())
        results: dict[str, Any] = {}
        counts = {"restored": 0, "incomplete": 0, "failed": 0}
        for item in self.plan["items"]:
            if item["action"] in ("skip", "not_selected", "blocked"):
                continue
            key = item["key"]
            record = self.records[key]
            problems = list(self.discrepancies.get(key, []))
            if item["kind"] == "label":
                live_id = self.labels.get(record["id"])
                live = snap.labels.get(live_id) if live_id else None
                if live is None:
                    problems.append("The label is not in Home Assistant")
                else:
                    if item["action"] in ("create", "update") and _label_fields(label_definition(live)) != \
                            _label_fields(record["definition"]):
                        problems.append("Its name, icon, colour or description differ from the archive")
                    if self.plan.get("assignments"):
                        problems += self._missing_assignments(record, snap, live_id, None)
            elif item["kind"] == "category":
                scope = record["scope"]
                live_id = self.categories.get(f"{scope}:{record['definition']['category_id']}")
                live = (snap.categories.get(scope) or {}).get(live_id) if live_id else None
                if live is None:
                    problems.append("The category is not in Home Assistant")
                else:
                    if item["action"] in ("create", "update") and (live.get("name"), live.get("icon")) != (
                            record["definition"].get("name"), record["definition"].get("icon")):
                        problems.append("Its name or icon differ from the archive")
                    if self.plan.get("assignments"):
                        problems += self._missing_assignments(record, snap, live_id, scope)
            else:
                live = next((h for h in snap.helpers.values() if h.domain == record["domain"]
                             and h.item_id == record["id"]), None)
                if live is None:
                    problems.append("The helper is not in Home Assistant")
                else:
                    if not same(live.config, record["config"]):
                        problems.append("Its configuration differs: " + "; ".join(
                            differences(record["config"], live.config, limit=3)))
                    problems += self._registry_problems(record.get("registry") or {}, live.registry or {})
            status = "restored" if not problems else ("failed" if any("is not in Home Assistant" in p for p in problems)
                                                      else "incomplete")
            counts[status] += 1
            results[key] = {"status": status, "discrepancies": problems}
            self.item(key, status=status, discrepancies=problems)
        status = "completed" if not counts["incomplete"] and not counts["failed"] else (
            "failed" if not counts["restored"] and counts["failed"] else "incomplete")
        return {"status": status, "counts": counts, "results": results, "created": self.created}

    def _missing_assignments(self, record: dict[str, Any], snap: RegistrySnapshot, live_id: str,
                             scope: str | None) -> list[str]:
        out = []
        assigned = record.get("assignments") or {}
        for group, live in (("entities", snap.entities), ("devices", snap.devices), ("areas", snap.areas)):
            for ref in assigned.get(group) or []:
                entry = live.get(ref["id"])
                if entry is None:
                    continue  # gone meanwhile: reported in the log, not a fault of the restore
                if scope is None and live_id not in (entry.get("labels") or []):
                    out.append(f"{ref.get('name') or ref['id']} does not carry it")
                if scope is not None:
                    current = (entry.get("categories") or {}).get(scope)
                    if current is not None and current != live_id:
                        continue  # kept its other category (reported)
                    if current != live_id:
                        out.append(f"{ref.get('name') or ref['id']} does not carry it")
        return out

    def _registry_problems(self, archived: dict[str, Any], live: dict[str, Any]) -> list[str]:
        out = []
        areas_map = self.areas_map
        expected_area = areas_map.get(archived.get("area_id")) if archived.get("area_id") else None
        if live.get("area_id") != expected_area:
            out.append("Its area differs")
        for key in ("name", "icon", "hidden_by", "disabled_by"):
            if live.get(key) != archived.get(key):
                out.append(f"Its {key.replace('_by', '')} differs")
        if sorted(live.get("aliases") or []) != sorted(archived.get("aliases") or []):
            out.append("Its voice aliases differ")
        expected_labels = sorted({self.labels.get(i, i) for i in archived.get("labels") or []})
        if sorted(live.get("labels") or []) != expected_labels:
            out.append("Its labels differ")
        expected_categories = {s: self.categories.get(f"{s}:{c}", c) for s, c in (archived.get("categories") or {}).items()}
        if (live.get("categories") or {}) != expected_categories:
            out.append("Its categories differ")
        if archived.get("entity_id") and live.get("entity_id") != archived["entity_id"]:
            out.append(f"Its entity id is {live.get('entity_id')} (archived: {archived['entity_id']})")
        for assistant, value in (archived.get("exposure") or {}).items():
            if (live.get("exposure") or {}).get(assistant) != value:
                out.append(f"Its exposure to {assistant} differs")
        return out
