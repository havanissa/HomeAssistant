"""A fake Home Assistant 2026.11 that behaves like the real one where it matters to this App.

Modelled on Home Assistant's source (components/config/automation.py, view.py, helpers/entity_registry.py,
config/label_registry.py, category_registry.py):

* the editor endpoint stores the posted dict as is, ``id`` first (a posted ``id`` overrides the URL's), and a
  POST reloads that automation (its entity appears, ``on`` unless ``initial_state: false``);
* DELETE removes the entry from automations.yaml and the entity registry entry, which Home Assistant keeps as a
  *deleted entity*: creating the same id again gives back its entity id, name, icon, area, labels, categories,
  aliases, disabled/hidden flags and options;
* a registry update ignores label ids that do not exist; categories are merged per scope (``None`` removes);
* label ids are slugs of the name (``_2`` when taken), category ids are random, area ids are slugs.
"""

from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path
import re
import secrets
from typing import Any

import pytest

from aie.core import AppCore
from aie.ha import ConfigApiError, HomeAssistantError, NotAllowed, WriteScope, check_params, is_builder_id
from aie.settings import load_settings

ADMIN = "user-admin"
GUEST = "user-guest"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "unnamed"


def automation(config_id: str, alias: str, **extra: Any) -> dict[str, Any]:
    return {"id": config_id, "alias": alias, "description": extra.pop("description", ""),
            "triggers": extra.pop("triggers", [{"trigger": "state", "entity_id": "binary_sensor.motion", "to": "on"}]),
            "conditions": extra.pop("conditions", []),
            "actions": extra.pop("actions", [{"action": "light.turn_on", "target": {"entity_id": "light.kitchen"}}]),
            "mode": extra.pop("mode", "single"), **extra}


class FakeHome:
    def __init__(self) -> None:
        self.connected = True
        self.scope = WriteScope()
        self.commands_sent: Counter[str] = Counter()
        self.requests: Counter[str] = Counter()
        self.writes: list[tuple[str, Any]] = []         # every changing call (kind, detail)
        self.file: list[dict[str, Any]] = []            # automations.yaml
        self.external: dict[str, dict[str, Any]] = {}   # entity id -> config of automations from other YAML files
        self.registry: dict[str, dict[str, Any]] = {}   # entity id -> entry
        self.deleted: dict[str, dict[str, Any]] = {}    # unique id -> deleted entry
        self.states: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {}
        self.categories: dict[str, dict[str, dict[str, Any]]] = {"automation": {}}
        self.areas: dict[str, dict[str, Any]] = {}
        self.floors: dict[str, dict[str, Any]] = {"ground": {"floor_id": "ground", "name": "Ground floor"}}
        self.blueprints: dict[str, dict[str, Any]] = {
            "homeassistant/motion_light.yaml": {"metadata": {"name": "Motion-activated Light", "domain": "automation",
                                                             "input": {"motion_entity": {}, "light_target": {}}}}}
        self.blueprint_files: dict[str, str] = {}
        self.users = [{"id": ADMIN, "is_owner": True, "is_active": True, "group_ids": ["system-admin"]},
                      {"id": GUEST, "is_owner": False, "is_active": True, "group_ids": ["system-users"]}]
        self.other_entities = {"binary_sensor.motion", "light.kitchen", "light.hall", "script.goodnight",
                               "scene.evening", "input_boolean.guest"}
        self.fail_post: dict[str, str] = {}             # id -> message (the POST is refused)
        self.unavailable_on_load: set[str] = set()      # ids that load as unavailable
        for name in ("Lighting", "Security", "Night"):
            self.add_label(name)

    # ------------------------------------------------------------------ helpers for tests
    def add_label(self, name: str, **extra: Any) -> str:
        label_id = slug(name)
        n = 2
        while label_id in self.labels:
            label_id = f"{slug(name)}_{n}"
            n += 1
        self.labels[label_id] = {"label_id": label_id, "name": name, "icon": extra.get("icon"),
                                 "color": extra.get("color"), "description": extra.get("description"),
                                 "created_at": 0.0, "modified_at": 0.0}
        return label_id

    def add_category(self, name: str, icon: str | None = None) -> str:
        category_id = secrets.token_hex(13).upper()
        self.categories["automation"][category_id] = {"category_id": category_id, "name": name, "icon": icon,
                                                      "created_at": 0.0, "modified_at": 0.0}
        return category_id

    def add_area(self, name: str, **extra: Any) -> str:
        area_id = slug(name)
        self.areas[area_id] = {"area_id": area_id, "name": name, "icon": extra.get("icon"),
                               "floor_id": extra.get("floor_id"), "aliases": extra.get("aliases", []),
                               "labels": [], "picture": None}
        return area_id

    def add_ui(self, config: dict[str, Any], *, state: str = "on", **registry: Any) -> str:
        self.file.append(copy.deepcopy(config))
        return self._load(config["id"], state=state, **registry)

    def add_external(self, entity_id: str, config: dict[str, Any], *, with_id: bool = True) -> None:
        self.external[entity_id] = copy.deepcopy(config)
        attributes = {"friendly_name": config.get("alias"), "last_triggered": None, "mode": "single"}
        if with_id and "id" in config:
            attributes["id"] = config["id"]
            self.registry[entity_id] = self._entry(entity_id, config["id"], config.get("alias"))
        self.states[entity_id] = {"entity_id": entity_id, "state": "on", "attributes": attributes}

    def entity_of(self, config_id: str) -> str | None:
        return next((e for e, r in self.registry.items() if r["unique_id"] == config_id and r["platform"] == "automation"), None)

    def _entry(self, entity_id: str, unique_id: str, original_name: str | None) -> dict[str, Any]:
        return {"entity_id": entity_id, "unique_id": unique_id, "platform": "automation", "id": secrets.token_hex(16),
                "name": None, "icon": None, "area_id": None, "labels": [], "categories": {}, "aliases": [],
                "disabled_by": None, "hidden_by": None, "options": {}, "original_name": original_name,
                "config_entry_id": None, "device_id": None, "entity_category": None, "created_at": 1.0,
                "modified_at": 1.0, "capabilities": None, "device_class": None, "original_device_class": None,
                "original_icon": None, "has_entity_name": False, "translation_key": None}

    def _load(self, config_id: str, *, state: str | None = None, **registry: Any) -> str:
        config = next(c for c in self.file if c.get("id") == config_id)
        entity_id = self.entity_of(config_id)
        if entity_id is None:
            deleted = self.deleted.pop(config_id, None)
            if deleted is not None and deleted["entity_id"] not in self.registry and deleted["entity_id"] not in self.states:
                entry = deleted
            else:
                base = "automation." + slug(config.get("alias") or config_id)
                candidate, n = base, 2
                while candidate in self.registry or candidate in self.states:
                    candidate = f"{base}_{n}"
                    n += 1
                entry = self._entry(candidate, config_id, config.get("alias"))
            entity_id = entry["entity_id"]
            self.registry[entity_id] = entry
        entry = self.registry[entity_id]
        entry.update(registry)
        entry["original_name"] = config.get("alias")
        if entry.get("disabled_by"):
            self.states.pop(entity_id, None)
            return entity_id
        current = self.states.get(entity_id)
        if state is None:
            state = current["state"] if current and current["state"] in ("on", "off") else (
                "off" if config.get("initial_state") is False else "on")
        if config_id in self.unavailable_on_load:
            state = "unavailable"
        self.states[entity_id] = {"entity_id": entity_id, "state": state, "attributes": {
            "friendly_name": entry.get("name") or config.get("alias"), "id": config_id, "last_triggered": None,
            "mode": config.get("mode", "single"), "current": 0}}
        return entity_id

    def ui_config(self, config_id: str) -> dict[str, Any] | None:
        return next((copy.deepcopy(c) for c in self.file if c.get("id") == config_id), None)

    # ------------------------------------------------------------------ the client interface
    def status(self) -> dict[str, Any]:
        return {"state": "connected" if self.connected else "disconnected", "connected": self.connected,
                "ha_version": "2026.11.1"}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def scoped(self, **kwargs: Any) -> Any:
        from aie.ha import HomeAssistantClient
        return HomeAssistantClient.scoped(self, **kwargs)  # type: ignore[arg-type]

    async def command(self, kind: str, **params: Any) -> Any:
        check_params(kind, params, self.scope)
        if not self.connected:
            raise ConnectionError("Not connected to Home Assistant")
        self.commands_sent[kind] += 1
        handler = getattr(self, "ws_" + kind.replace("/", "_"), None)
        if handler is None:
            raise HomeAssistantError("unknown_command", kind)
        return handler(**params)

    def ws_ping(self) -> None:
        return None

    def ws_get_config(self) -> dict[str, Any]:
        return {"version": "2026.11.1", "location_name": "Test Home", "time_zone": "Europe/Madrid", "country": "ES",
                "language": "en", "state": "RUNNING"}

    def ws_get_states(self) -> list[dict[str, Any]]:
        others = [{"entity_id": e, "state": "off", "attributes": {}} for e in sorted(self.other_entities)]
        return copy.deepcopy(list(self.states.values())) + others

    def ws_config_auth_list(self) -> list[dict[str, Any]]:
        return self.users

    def ws_config_entity_registry_list(self) -> list[dict[str, Any]]:
        out = [{k: v for k, v in e.items() if k not in ("aliases", "capabilities", "device_class",
                                                         "original_device_class", "original_icon")}
               for e in self.registry.values()]
        out += [{"entity_id": e, "platform": e.split(".")[0], "unique_id": e, "labels": [], "categories": {}}
                for e in sorted(self.other_entities)]
        return copy.deepcopy(out)

    def ws_config_entity_registry_get_entries(self, entity_ids: list[str]) -> dict[str, Any]:
        return {e: copy.deepcopy(self.registry.get(e)) for e in entity_ids}

    def ws_config_label_registry_list(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.labels.values()))

    def ws_config_category_registry_list(self, scope: str) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.categories.get(scope, {}).values()))

    def ws_config_area_registry_list(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.areas.values()))

    def ws_config_floor_registry_list(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.floors.values()))

    def ws_config_device_registry_list(self) -> list[dict[str, Any]]:
        return [{"id": "dev-1", "name": "Lamp"}]

    def ws_blueprint_list(self, domain: str) -> dict[str, Any]:
        return copy.deepcopy(self.blueprints)

    def ws_automation_config(self, entity_id: str) -> dict[str, Any]:
        if entity_id in self.external:
            return {"config": copy.deepcopy(self.external[entity_id])}
        uid = (self.registry.get(entity_id) or {}).get("unique_id")
        config = self.ui_config(uid) if uid else None
        if config is None or entity_id not in self.states:
            raise HomeAssistantError("not_found", "Entity not found")
        return {"config": config}

    def ws_validate_config(self, **parts: Any) -> dict[str, Any]:
        out = {}
        for key, value in parts.items():
            bad = isinstance(value, list) and any(isinstance(v, dict) and v.get("trigger") == "broken" for v in value)
            out[key] = {"valid": not bad, "error": "invalid trigger" if bad else None}
        return out

    # writes
    def ws_config_entity_registry_update(self, entity_id: str, **changes: Any) -> dict[str, Any]:
        entry = self.registry.get(entity_id)
        if entry is None:
            raise HomeAssistantError("not_found", "Entity not found")
        self.writes.append(("registry", {"entity_id": entity_id, **changes}))
        if "labels" in changes:
            changes["labels"] = sorted(set(changes["labels"]) & set(self.labels))
        if "categories" in changes:
            categories = dict(entry["categories"])
            for scope, value in changes["categories"].items():
                if value is None:
                    categories.pop(scope, None)
                else:
                    categories[scope] = value
            changes["categories"] = categories
        new = changes.pop("new_entity_id", None)
        entry.update(changes)
        if new and new != entity_id:
            if new in self.registry or new in self.states:
                raise HomeAssistantError("invalid_info", "Entity with this ID is already registered")
            entry["entity_id"] = new
            self.registry[new] = self.registry.pop(entity_id)
            if entity_id in self.states:
                st = self.states.pop(entity_id)
                st["entity_id"] = new
                self.states[new] = st
            entity_id = new
        if entry.get("disabled_by"):
            self.states.pop(entity_id, None)
        if "name" in changes and entity_id in self.states:
            uid = entry["unique_id"]
            self.states[entity_id]["attributes"]["friendly_name"] = changes["name"] or (self.ui_config(uid) or {}).get("alias")
        return {"entity_entry": copy.deepcopy(entry)}

    def ws_config_label_registry_create(self, name: str, **extra: Any) -> dict[str, Any]:
        self.writes.append(("label_create", name))
        return copy.deepcopy(self.labels[self.add_label(name, **extra)])

    def ws_config_category_registry_create(self, scope: str, name: str, icon: str | None = None) -> dict[str, Any]:
        self.writes.append(("category_create", name))
        return copy.deepcopy(self.categories[scope][self.add_category(name, icon)])

    def ws_config_area_registry_create(self, name: str, **extra: Any) -> dict[str, Any]:
        self.writes.append(("area_create", name))
        return copy.deepcopy(self.areas[self.add_area(name, **extra)])

    def ws_blueprint_save(self, domain: str, path: str, yaml: str) -> dict[str, Any]:
        self.writes.append(("blueprint_save", path))
        if path in self.blueprints:
            raise HomeAssistantError("already_exists", "File already exists")
        self.blueprints[path] = {"metadata": {"name": path, "domain": "automation"}}
        self.blueprint_files[path] = yaml
        return {"overrides_existing": False}

    def ws_call_service(self, domain: str, service: str, target: dict[str, Any] | None = None,
                        service_data: dict[str, Any] | None = None) -> dict[str, Any]:
        self.writes.append(("service", f"{domain}.{service}", target, service_data))
        if service == "reload":
            self._load(service_data["id"])
            return {}
        for entity_id in target["entity_id"] if isinstance(target["entity_id"], list) else [target["entity_id"]]:
            if entity_id in self.states:
                self.states[entity_id]["state"] = "on" if service == "turn_on" else "off"
        return {}

    def ws_homeassistant_expose_entity(self, assistants: list[str], entity_ids: list[str], should_expose: bool) -> None:
        self.writes.append(("expose", assistants, entity_ids, should_expose))
        for entity_id in entity_ids:
            for assistant in assistants:
                self.registry[entity_id]["options"].setdefault(assistant, {})["should_expose"] = should_expose


class FakeApi:
    """The editor endpoint, with the same limits as AutomationConfigApi."""

    def __init__(self, home: FakeHome) -> None:
        self.home = home
        self.requests = home.requests

    @staticmethod
    def addressable(automation_id: Any) -> bool:
        from aie.ha import AutomationConfigApi
        return AutomationConfigApi.addressable(automation_id)

    async def get(self, automation_id: str) -> dict[str, Any] | None:
        if not self.addressable(automation_id):
            raise NotAllowed("not addressable")
        if not self.home.connected:
            raise ConfigApiError("Home Assistant could not be reached")
        self.requests["GET"] += 1
        return self.home.ui_config(automation_id)

    async def put(self, automation_id: str, config: dict[str, Any]) -> None:
        if is_builder_id(automation_id):
            raise NotAllowed("builder")
        if automation_id not in self.home.scope.configs:
            raise NotAllowed("not in scope")
        self.requests["POST"] += 1
        self.home.writes.append(("post", automation_id))
        if automation_id in self.home.fail_post:
            raise ConfigApiError(f"Home Assistant did not accept the automation: {self.home.fail_post[automation_id]}", 400)
        value = {"id": automation_id}
        for key in ("alias", "description", "triggers", "trigger", "conditions", "condition", "actions", "action"):
            if key in config:
                value[key] = copy.deepcopy(config[key])
        value.update(copy.deepcopy(config))
        for index, current in enumerate(self.home.file):
            if current.get("id") == automation_id:
                self.home.file[index] = value
                break
        else:
            self.home.file.append(value)
        self.home._load(value["id"])  # noqa: SLF001 - the post-write reload

    async def delete(self, automation_id: str) -> bool:
        if is_builder_id(automation_id):
            raise NotAllowed("builder")
        if automation_id not in self.home.scope.deletions:
            raise NotAllowed("not in scope")
        self.requests["DELETE"] += 1
        self.home.writes.append(("delete", automation_id))
        index = next((i for i, c in enumerate(self.home.file) if c.get("id") == automation_id), None)
        if index is None:
            return False
        self.home.file.pop(index)
        entity_id = self.home.entity_of(automation_id)
        if entity_id:
            self.home.deleted[automation_id] = self.home.registry.pop(entity_id)
            self.home.states.pop(entity_id, None)
        return True


def populate(home: FakeHome) -> dict[str, Any]:
    """The standard test home: ordinary automations with rich metadata, a Builder one, an ambiguous one, an
    external one, a blueprint one, a disabled one."""
    ids: dict[str, Any] = {}
    ids["cat_lights"] = home.add_category("Lights", "mdi:lightbulb")
    ids["cat_safety"] = home.add_category("Safety")
    ids["kitchen"] = home.add_area("Kitchen", icon="mdi:stove", floor_id="ground")
    ids["hall"] = home.add_area("Hall")
    home.add_ui(automation("1700000000001", "Kitchen motion light", description="Turns the light on",
                           variables={"brightness": 80}, trace={"stored_traces": 10}, max_exceeded="silent",
                           custom_field={"kept": [1, 2.5, True, None, "on"]}),
                labels=["lighting", "night"], categories={"automation": ids["cat_lights"]}, area_id="kitchen",
                name="Kitchen light (custom)", icon="mdi:motion-sensor", aliases=["kitchen lights"])
    home.add_ui(automation("1700000000002", "Hall alarm", mode="queued", max=5,
                           actions=[{"choose": [{"conditions": [{"condition": "state", "entity_id": "input_boolean.guest",
                                                                 "state": "on"}],
                                                 "sequence": [{"action": "script.goodnight"}]}],
                                     "default": [{"action": "scene.turn_on", "target": {"entity_id": "scene.evening"}}]},
                                    {"repeat": {"count": 2, "sequence": [{"delay": {"seconds": 1}}]}}]),
                state="off", labels=["security"], categories={"automation": ids["cat_safety"]}, area_id="hall")
    home.add_ui(automation("1700000000003", "No metadata"))
    home.add_ui({"id": "1700000000004", "alias": "Blueprint light",
                 "use_blueprint": {"path": "homeassistant/motion_light.yaml",
                                   "input": {"motion_entity": "binary_sensor.motion", "light_target": {"entity_id": "light.hall"}}}},
                labels=["lighting"])
    home.add_ui(automation("1700000000005", "Disabled entity"), disabled_by="user", labels=["night"])
    home.add_ui(automation("automation_builder_dev_1", "Lamp (Automation Builder)",
                           description="Managed by the Automation Builder App: edit the rules there."))
    home.add_ui(automation("1700000000007", "Copied builder (Automation Builder)",
                           description="Managed by the Automation Builder App: edit the rules there."))
    home.add_external("automation.package_rule", automation("package_rule_1", "Package rule"))
    home.add_external("automation.no_id_rule", {"alias": "No id rule", "triggers": [], "actions": []}, with_id=False)
    home.blueprint_files["homeassistant/motion_light.yaml"] = "blueprint:\n  name: Motion-activated Light\n"
    return ids


@pytest.fixture
def home() -> FakeHome:
    h = FakeHome()
    populate(h)
    return h


@pytest.fixture
def ha_folder(tmp_path: Path, home: FakeHome) -> Path:
    root = tmp_path / "homeassistant"
    (root / "blueprints" / "automation" / "homeassistant").mkdir(parents=True)
    (root / "configuration.yaml").write_text("automation: !include automations.yaml\n")
    for path, text in home.blueprint_files.items():
        (root / "blueprints" / "automation" / path).write_text(text)
    import yaml
    (root / "automations.yaml").write_text(yaml.safe_dump(home.file))
    return root


@pytest.fixture
async def core(tmp_path: Path, home: FakeHome, ha_folder: Path) -> AppCore:
    settings = load_settings({"AIE_DATA_DIR": str(tmp_path / "data"), "AIE_HA_CONFIG_DIR": str(ha_folder),
                              "AIE_ALLOWED_CLIENTS": "any"})
    app_core = AppCore(settings)
    app_core.settings.data_dir.mkdir(parents=True, exist_ok=True)
    app_core.identity.load()
    app_core.archives.load()
    app_core.journal.load()
    home.scope = WriteScope()
    app_core.attach(home, FakeApi(home))
    return app_core


async def wait_op(core: AppCore, op: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
    import asyncio
    for _ in range(int(timeout / 0.02)):
        if not core.lock.locked() and not core.tasks:
            break
        await asyncio.sleep(0.02)
    return core.journal.get(op["id"])


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    import aie.core
    import aie.restore
    monkeypatch.setattr(aie.core, "SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(aie.restore, "SETTLE_SECONDS", 0.0)
