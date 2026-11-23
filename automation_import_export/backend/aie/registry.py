"""Helpers, labels and categories: what exists, how each one is organised, and what uses it (the cleanup overview).

A :class:`RegistrySnapshot` is read fresh from Home Assistant:

* **helpers** - the ones Home Assistant keeps in its storage collections (Settings -> Helpers: input_boolean,
  input_number, input_text, input_select, input_datetime, input_button, counter, timer, schedule), with their
  configuration (``<domain>/list``) and entity registry entry. They can be backed up, deleted and re-created.
  Helpers defined in YAML and helpers that are config entries (template, group, utility meter, ...) are listed
  read-only: this App cannot re-create them.
* **labels** (id, name, icon, colour, description) and where they are assigned: entities, devices, areas;
* **categories** per scope (automation, script, helpers) and the entities that carry them.

**Usage** (:func:`usage`) - what refers to each item:

* helpers (and any entity of a config-entry helper): automations, scripts, scenes, dashboards, other helpers (the
  options of config-entry helpers such as templates and groups) and Home Assistant's YAML files. An entity is
  found by its entity id anywhere in the text (also inside templates) or by its entity registry id (what device
  triggers and conditions use);
* labels: the entities, devices and areas that carry them, and automations and scripts that target them
  (``label_id``);
* categories: the entities that carry them.

"Not used" therefore means: no reference was found in these places. Other Apps (Node-RED, AppDaemon), ESPHome
devices outside the configuration folder, custom cards with unusual configuration and external services are not
searched - the interface says so, and every deletion is archived first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
from typing import Any

from . import deps
from .canonical import digest
from .ha import HELPER_DOMAINS, HomeAssistantClient, HomeAssistantError, registry_key

_LOGGER = logging.getLogger(__name__)

HELPER_NAMES = {"input_boolean": "Toggle", "input_number": "Number", "input_text": "Text", "input_select": "Dropdown",
                "input_datetime": "Date and/or time", "input_button": "Button", "counter": "Counter",
                "timer": "Timer", "schedule": "Schedule"}
SCOPE_NAMES = {"automation": "Automations", "script": "Scripts", "helpers": "Helpers"}
#: The category scopes Home Assistant's frontend offers (more are read when an entity uses another one).
KNOWN_SCOPES = ("automation", "script", "helpers")
#: The registry fields of a helper's entity that are archived and restored.
HELPER_REGISTRY_FIELDS = ("entity_id", "name", "icon", "area_id", "labels", "categories", "aliases", "hidden_by",
                          "disabled_by")
_CHAIN = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)+")
_UUID = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")
USAGE_KINDS = ("automations", "scripts", "scenes", "dashboards", "helpers", "yaml", "entities", "devices", "areas")


def label_definition(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry.get(k) for k in ("label_id", "name", "icon", "color", "description")}


def category_definition(entry: dict[str, Any]) -> dict[str, Any]:
    return {k: entry.get(k) for k in ("category_id", "name", "icon")}


def helper_registry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    out = {k: entry.get(k) for k in HELPER_REGISTRY_FIELDS}
    out["labels"] = sorted(entry.get("labels") or [])
    out["categories"] = dict(sorted((entry.get("categories") or {}).items()))
    out["aliases"] = sorted(a for a in entry.get("aliases") or [] if isinstance(a, str))
    options = entry.get("options") or {}
    out["exposure"] = {a: (options.get(a) or {}).get("should_expose") for a in ("conversation", "cloud.alexa",
                                                                               "cloud.google_assistant")
                       if isinstance((options.get(a) or {}).get("should_expose"), bool)}
    return out


def helper_config(item: dict[str, Any]) -> dict[str, Any]:
    """A helper's configuration as Home Assistant's ``<domain>/create`` and ``update`` take it (without its id)."""
    return {k: v for k, v in item.items() if k != "id"}


@dataclass
class Helper:
    domain: str
    item_id: str
    config: dict[str, Any]                       # what <domain>/list returns (with "id")
    entity_id: str | None
    registry: dict[str, Any] | None              # helper_registry() of the entity registry entry
    registry_id: str | None                      # the entity registry id (device triggers refer to it)
    state: str | None

    @property
    def key(self) -> str:
        return registry_key("helper", self.domain, self.item_id)

    @property
    def name(self) -> str:
        return str((self.registry or {}).get("name") or self.config.get("name") or self.item_id)

    def fingerprint(self) -> str:
        """What a deletion compares: the configuration and the registry settings (not the state)."""
        return digest({"config": self.config, "registry": self.registry})


@dataclass
class User:
    """Something that can refer to a helper, label or category."""

    kind: str                                    # automations | scripts | scenes | dashboards | helpers | yaml
    ref: str                                     # entity id, dashboard path, file path, config entry id
    name: str
    text: str = ""                               # searched for entity ids and registry ids
    labels: set[str] = field(default_factory=set)  # label ids it targets (label_id)
    own_entities: set[str] = field(default_factory=set)  # a config-entry helper's own entities (not a use)


@dataclass
class RegistrySnapshot:
    taken_at: str
    states: dict[str, dict[str, Any]]
    entities: dict[str, dict[str, Any]]                  # entity id -> entity registry entry (list form)
    devices: dict[str, dict[str, Any]]
    areas: dict[str, dict[str, Any]]
    labels: dict[str, dict[str, Any]]
    categories: dict[str, dict[str, dict[str, Any]]]     # scope -> category id -> definition
    helpers: dict[str, Helper]                           # registry_key -> storage helper
    yaml_helpers: list[str]                              # helper entities that are not in a storage collection
    entry_helpers: list[dict[str, Any]]                  # config-entry helpers: entry id, domain, title, entities
    users: list[User] = field(default_factory=list)
    problems: list[dict[str, Any]] = field(default_factory=list)
    searched: dict[str, int] = field(default_factory=dict)

    def label_assignments(self, label_id: str) -> dict[str, list[str]]:
        return {"entities": sorted(e for e, r in self.entities.items() if label_id in (r.get("labels") or [])),
                "devices": sorted(d for d, r in self.devices.items() if label_id in (r.get("labels") or [])),
                "areas": sorted(a for a, r in self.areas.items() if label_id in (r.get("labels") or []))}

    def category_assignments(self, scope: str, category_id: str) -> dict[str, list[str]]:
        return {"entities": sorted(e for e, r in self.entities.items()
                                   if (r.get("categories") or {}).get(scope) == category_id)}

    def device_name(self, device_id: str) -> str:
        d = self.devices.get(device_id) or {}
        return str(d.get("name_by_user") or d.get("name") or device_id)

    def area_name(self, area_id: str) -> str:
        return str((self.areas.get(area_id) or {}).get("name") or area_id)

    def entity_name(self, entity_id: str) -> str:
        reg = self.entities.get(entity_id) or {}
        st = self.states.get(entity_id) or {}
        return str(reg.get("name") or (st.get("attributes") or {}).get("friendly_name") or reg.get("original_name")
                   or entity_id)


async def _list(ha: HomeAssistantClient, command: str, **params: Any) -> list[dict[str, Any]]:
    value = await ha.command(command, **params)
    return [v for v in value or [] if isinstance(v, dict)]


async def take_registry_snapshot(ha: HomeAssistantClient, *, now: str) -> RegistrySnapshot:
    """Helpers, labels, categories and their assignments (no usage search: see :func:`collect_users`)."""
    states = {s["entity_id"]: s for s in await _list(ha, "get_states") if "entity_id" in s}
    entities = {e["entity_id"]: e for e in await _list(ha, "config/entity_registry/list") if e.get("entity_id")}
    devices = {d["id"]: d for d in await _list(ha, "config/device_registry/list") if d.get("id")}
    areas = {a["area_id"]: a for a in await _list(ha, "config/area_registry/list") if a.get("area_id")}
    labels = {d["label_id"]: d for d in await _list(ha, "config/label_registry/list") if d.get("label_id")}
    problems: list[dict[str, Any]] = []
    scopes = set(KNOWN_SCOPES) | {s for e in entities.values() for s in (e.get("categories") or {})}
    categories: dict[str, dict[str, dict[str, Any]]] = {}
    for scope in sorted(scopes):
        try:
            categories[scope] = {d["category_id"]: d for d in await _list(
                ha, "config/category_registry/list", scope=scope) if d.get("category_id")}
        except HomeAssistantError as err:
            categories[scope] = {}
            problems.append({"level": "warning", "code": "categories_unreadable",
                             "message": f"The categories of scope {scope!r} could not be read: {err}"})
    by_unique = {(e.get("platform"), e.get("unique_id")): e for e in entities.values()}
    helpers: dict[str, Helper] = {}
    storage_entities: set[str] = set()
    for domain in HELPER_DOMAINS:
        try:
            items = await _list(ha, f"{domain}/list")
        except HomeAssistantError as err:  # the integration is not loaded (no helper of this kind yet)
            _LOGGER.debug("%s/list: %s", domain, err)
            continue
        for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            entry = by_unique.get((domain, item_id))
            entity_id = entry.get("entity_id") if entry else None
            helpers[registry_key("helper", domain, item_id)] = Helper(
                domain=domain, item_id=item_id, config=item, entity_id=entity_id, registry=None,
                registry_id=entry.get("id") if entry else None,
                state=(states.get(entity_id) or {}).get("state") if entity_id else None)
            if entity_id:
                storage_entities.add(entity_id)
    ids = sorted(storage_entities)
    extended: dict[str, Any] = {}
    for start in range(0, len(ids), 200):
        extended.update(await ha.command("config/entity_registry/get_entries", entity_ids=ids[start:start + 200]) or {})
    for helper in helpers.values():
        if helper.entity_id:
            helper.registry = helper_registry(extended.get(helper.entity_id) or entities.get(helper.entity_id))
    yaml_helpers = sorted(e for e in states if e.split(".", 1)[0] in HELPER_DOMAINS and e not in storage_entities
                          and (entities.get(e) or {}).get("config_entry_id") is None)
    entry_helpers: list[dict[str, Any]] = []
    try:
        for entry in await _list(ha, "config_entries/get", type_filter=["helper"]):
            own = sorted(e for e, r in entities.items() if r.get("config_entry_id") == entry.get("entry_id"))
            entry_helpers.append({"entry_id": entry.get("entry_id"), "domain": entry.get("domain"),
                                  "title": entry.get("title"), "entities": own})
    except HomeAssistantError as err:
        problems.append({"level": "warning", "code": "config_entries_unreadable",
                         "message": f"The helpers that are integrations (templates, groups, ...) could not be "
                                    f"listed: {err}"})
    return RegistrySnapshot(taken_at=now, states=states, entities=entities, devices=devices, areas=areas,
                            labels=labels, categories=categories, helpers=helpers, yaml_helpers=yaml_helpers,
                            entry_helpers=sorted(entry_helpers, key=lambda e: str(e.get("title")).casefold()),
                            problems=problems)


async def collect_users(ha: HomeAssistantClient, snap: RegistrySnapshot, *, automations: list[tuple[str, str, Any]],
                        entry_options: dict[str, Any], yaml_files: list[tuple[str, str]]) -> None:
    """Everything that can refer to an item: automations (given), scripts, scenes, dashboards, config-entry helpers
    (their options, read by the caller from the configuration folder) and YAML files (given)."""
    users: list[User] = []
    for ref, name, config in automations:
        refs = deps.references(config) if isinstance(config, dict) else {"labels": []}
        users.append(User("automations", ref, name, json.dumps(config, ensure_ascii=False, default=str),
                          set(refs.get("labels") or [])))
    for entity_id, st in sorted(snap.states.items()):
        domain = entity_id.split(".", 1)[0]
        name = str((st.get("attributes") or {}).get("friendly_name") or entity_id)
        if domain == "script":
            try:
                result = await ha.command("script/config", entity_id=entity_id)
            except HomeAssistantError:
                continue
            config = (result or {}).get("config") if isinstance(result, dict) else None
            if isinstance(config, dict):
                users.append(User("scripts", entity_id, name, json.dumps(config, ensure_ascii=False, default=str),
                                  set(deps.references(config).get("labels") or [])))
        elif domain == "scene":
            members = (st.get("attributes") or {}).get("entity_id") or []
            users.append(User("scenes", entity_id, name, " ".join(m for m in members if isinstance(m, str))))
    dashboards = [{"url_path": None, "title": "Overview"}]
    try:
        dashboards += [d for d in await _list(ha, "lovelace/dashboards/list") if isinstance(d.get("url_path"), str)]
    except HomeAssistantError as err:
        snap.problems.append({"level": "info", "code": "dashboards_unreadable",
                              "message": f"The dashboards could not be listed: {err}"})
    for dashboard in dashboards:
        try:
            config = await ha.command("lovelace/config", url_path=dashboard["url_path"])
        except HomeAssistantError:  # auto-generated (no configuration), or not readable
            continue
        if isinstance(config, dict) and "strategy" not in config:
            users.append(User("dashboards", dashboard["url_path"] or "lovelace",
                              str(dashboard.get("title") or dashboard["url_path"]),
                              json.dumps(config, ensure_ascii=False, default=str)))
    for entry in snap.entry_helpers:
        options = entry_options.get(entry["entry_id"])
        if options is not None:
            users.append(User("helpers", str(entry["entry_id"]), str(entry.get("title") or entry.get("domain")),
                              json.dumps(options, ensure_ascii=False, default=str), own_entities=set(entry["entities"])))
    for path, text in yaml_files:
        users.append(User("yaml", path, path, text))
    snap.users = users
    snap.searched = {kind: sum(1 for u in users if u.kind == kind) for kind in USAGE_KINDS[:6]}
    snap.searched["entry_helpers_unread"] = sum(1 for e in snap.entry_helpers if e["entry_id"] not in entry_options)


def _tokens(text: str) -> tuple[set[str], set[str]]:
    """Entity ids (every ``a.b`` pair in a dotted chain, so ``states.input_boolean.x.state`` finds
    ``input_boolean.x``) and 32-digit registry ids in a text."""
    found: set[str] = set()
    for chain in _CHAIN.findall(text):
        parts = chain.split(".")
        for i in range(len(parts) - 1):
            found.add(f"{parts[i]}.{parts[i + 1]}")
    return found, set(_UUID.findall(text))


def usage(snap: RegistrySnapshot) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Per entity id (helpers and config-entry helper entities), label key and category key: what uses it."""
    by_registry_id = {r.get("id"): e for e, r in snap.entities.items() if r.get("id")}
    watched = {h.entity_id for h in snap.helpers.values() if h.entity_id} | set(snap.yaml_helpers) | {
        e for entry in snap.entry_helpers for e in entry["entities"]}
    out: dict[str, dict[str, list[dict[str, str]]]] = {}

    def add(key: str, kind: str, ref: str, name: str) -> None:
        bucket = out.setdefault(key, {}).setdefault(kind, [])
        if not any(x["id"] == ref for x in bucket):
            bucket.append({"id": ref, "name": name})

    for user in snap.users:
        entity_ids, uuids = _tokens(user.text)
        hits = (entity_ids & watched) | {by_registry_id[u] for u in uuids if by_registry_id.get(u) in watched}
        for entity_id in hits - user.own_entities - {user.ref}:
            add(entity_id, user.kind, user.ref, user.name)
        for label_id in user.labels & set(snap.labels):
            add(registry_key("label", label_id), user.kind, user.ref, user.name)
    for label_id in snap.labels:
        key = registry_key("label", label_id)
        assigned = snap.label_assignments(label_id)
        for entity_id in assigned["entities"]:
            add(key, "entities", entity_id, snap.entity_name(entity_id))
        for device_id in assigned["devices"]:
            add(key, "devices", device_id, snap.device_name(device_id))
        for area_id in assigned["areas"]:
            add(key, "areas", area_id, snap.area_name(area_id))
    for scope, defs in snap.categories.items():
        for category_id in defs:
            for entity_id in snap.category_assignments(scope, category_id)["entities"]:
                add(registry_key("category", scope, category_id), "entities", entity_id, snap.entity_name(entity_id))
    return out


def _count(u: dict[str, list[Any]] | None) -> int:
    return sum(len(v) for v in (u or {}).values())


def overview(snap: RegistrySnapshot) -> dict[str, Any]:
    """The interface's listing: every helper, label and category with its usage."""
    used = usage(snap)
    helpers = []
    for h in sorted(snap.helpers.values(), key=lambda h: h.name.casefold()):
        u = used.get(h.entity_id or "", {})
        reg = h.registry or {}
        helpers.append({"key": h.key, "kind": "storage", "domain": h.domain, "type": HELPER_NAMES.get(h.domain, h.domain),
                        "id": h.item_id, "name": h.name, "entity_id": h.entity_id, "state": h.state,
                        "icon": reg.get("icon") or h.config.get("icon"), "area": reg.get("area_id"),
                        "labels": reg.get("labels") or [], "categories": reg.get("categories") or {},
                        "usage": u, "count": _count(u), "deletable": True})
    for entity_id in snap.yaml_helpers:
        u = used.get(entity_id, {})
        domain = entity_id.split(".", 1)[0]
        helpers.append({"key": f"yaml:{entity_id}", "kind": "yaml", "domain": domain,
                        "type": HELPER_NAMES.get(domain, domain), "id": entity_id.split(".", 1)[1],
                        "name": snap.entity_name(entity_id), "entity_id": entity_id,
                        "state": (snap.states.get(entity_id) or {}).get("state"), "usage": u, "count": _count(u),
                        "deletable": False, "note": "Defined in YAML: edit or remove it in Home Assistant's "
                                                    "configuration files"})
    for entry in snap.entry_helpers:
        u: dict[str, list[dict[str, str]]] = {}
        for entity_id in entry["entities"]:
            for kind, refs in used.get(entity_id, {}).items():
                for ref in refs:
                    if not any(x["id"] == ref["id"] for x in u.setdefault(kind, [])):
                        u[kind].append(ref)
        helpers.append({"key": f"entry:{entry['entry_id']}", "kind": "entry", "domain": entry.get("domain"),
                        "type": str(entry.get("domain") or "").replace("_", " ").capitalize(),
                        "id": entry["entry_id"], "name": entry.get("title") or entry.get("domain"),
                        "entity_id": entry["entities"][0] if len(entry["entities"]) == 1 else None,
                        "entities": entry["entities"], "usage": u, "count": _count(u), "deletable": False,
                        "note": "An integration helper (template, group, ...): this App cannot back it up or "
                                "re-create it; remove it in Settings -> Devices & services -> Helpers"})
    labels = []
    for label_id, d in sorted(snap.labels.items(), key=lambda kv: str(kv[1].get("name")).casefold()):
        u = used.get(registry_key("label", label_id), {})
        labels.append({"key": registry_key("label", label_id), "id": label_id, "name": d.get("name"),
                       "icon": d.get("icon"), "color": d.get("color"), "description": d.get("description"),
                       "usage": u, "count": _count(u), "deletable": True})
    categories = []
    for scope, defs in sorted(snap.categories.items()):
        for category_id, d in sorted(defs.items(), key=lambda kv: str(kv[1].get("name")).casefold()):
            u = used.get(registry_key("category", scope, category_id), {})
            categories.append({"key": registry_key("category", scope, category_id), "id": category_id,
                               "scope": scope, "scope_name": SCOPE_NAMES.get(scope, scope), "name": d.get("name"),
                               "icon": d.get("icon"), "usage": u, "count": _count(u), "deletable": True})
    summary = {
        "helpers": len(helpers), "helpers_unused": sum(1 for h in helpers if not h["count"]),
        "labels": len(labels), "labels_unused": sum(1 for x in labels if not x["count"]),
        "categories": len(categories), "categories_unused": sum(1 for x in categories if not x["count"]),
    }
    return {"taken_at": snap.taken_at, "helpers": helpers, "labels": labels, "categories": categories,
            "summary": summary, "searched": snap.searched, "problems": snap.problems,
            "areas": {a: snap.area_name(a) for a in snap.areas},
            "label_names": {k: v.get("name") for k, v in snap.labels.items()}}


def item_fingerprint(snap: RegistrySnapshot, key: str) -> str | None:
    """The state of one item that a deletion compares with the archive (None when it does not exist)."""
    kind, _, rest = key.partition(":")
    if kind == "label":
        d = snap.labels.get(rest)
        return digest(label_definition(d)) if d else None
    if kind == "category":
        scope, _, category_id = rest.partition(":")
        d = (snap.categories.get(scope) or {}).get(category_id)
        return digest(category_definition(d)) if d else None
    if kind == "helper":
        h = snap.helpers.get(key)
        return h.fingerprint() if h else None
    return None
