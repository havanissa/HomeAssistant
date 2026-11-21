"""The App's core: the Home Assistant connection, the rule configuration and the deployment workflow.

Rules are saved as soon as they change (they are this App's own data). Nothing
reaches Home Assistant until the user asks for a deployment, which always goes
through the same steps:

1. ``plan``: compile the device's rules, check them for conflicts, let Home
   Assistant validate the generated triggers, conditions and actions, read what
   is in Home Assistant now and compare it with what this App deployed last;
2. the plan is shown (YAML, differences, conflicts, external changes);
3. ``deploy`` with the hash of the plan the user saw: anything that changed
   since is refused. An automation that was changed outside this App is only
   replaced with an explicit ``overwrite``;
4. every written automation is confirmed: Home Assistant must have loaded
   exactly this configuration. If a step fails, the automations already
   written in this deployment are put back as they were.
"""

from __future__ import annotations

import asyncio
import copy
import difflib
import logging
from typing import Any

import aiohttp

from . import blueprint, catalog, compiler, conflicts as conflict_check, describe, importer, model
from .const import APP_NAME, APP_VERSION
from .diagnostics import Observer
from .discovery import Index
from .ha import AutomationConfigApi, DeployError, HomeAssistantClient, HomeAssistantError, NotAllowed, utcnow_iso
from .logbuffer import BufferHandler
from .model import ConfigError
from .settings import Settings
from .store import ConfigStore, DeployStore

_LOGGER = logging.getLogger(__name__)

STATUS_ORDER = ("unknown", "modified", "missing", "foreign", "changes_pending", "not_deployed", "in_sync", "nothing")
STATUS_TEXT = {
    "unknown": "Home Assistant is not connected",
    "modified": "Changed in Home Assistant outside this App",
    "missing": "Deleted in Home Assistant outside this App",
    "foreign": "An automation with this id exists that this App did not deploy",
    "changes_pending": "Changes not deployed yet",
    "not_deployed": "Not deployed yet",
    "in_sync": "Deployed, in sync",
    "nothing": "No enabled rules",
}
DRAFT_RULE_ID = "r_00000000"


class AppCore:
    CONFIRM_TIMEOUT = 20.0
    CONFIRM_INTERVAL = 0.4
    RELATED_TTL = 60.0

    def __init__(self, settings: Settings, buffer: BufferHandler | None = None) -> None:
        self.settings = settings
        self.buffer = buffer
        self.store = ConfigStore(settings.data_dir)
        self.store.load()
        self.deploys = DeployStore(settings.data_dir)
        self.deploys.load()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.api: AutomationConfigApi | None = None
        self._admins: tuple[float, dict[str, bool]] | None = None
        self._related: dict[str, tuple[float, list[str]]] = {}
        self._deploy_lock = asyncio.Lock()
        self.last_results: dict[str, dict[str, Any]] = {}  # device id -> the last deployment attempt
        self.observer = Observer(self)
        self._recovering: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.api = AutomationConfigApi(self.session, self.settings.ha_api_url, self.settings.token)
        self.ha.add_listener(self._on_ha)
        await self.ha.start()
        _LOGGER.info("%s %s started (%d devices)", APP_NAME, APP_VERSION, len(self.store.config["devices"]))

    def _on_ha(self, kind: str, payload: Any) -> None:
        if kind == "state":
            self.observer.on_state(*payload)
        elif kind == "status" and isinstance(payload, dict) and payload.get("connected") and self.deploys.pending \
                and (self._recovering is None or self._recovering.done()):
            self._recovering = asyncio.get_running_loop().create_task(self.recover())

    async def stop(self) -> None:
        self.observer.stop()
        if self._recovering is not None:
            self._recovering.cancel()
        if self.ha:
            await self.ha.stop()
        if self.session:
            await self.session.close()

    # ----------------------------------------------------------------- users
    async def is_admin(self, user_id: str | None) -> bool:
        """Is this Home Assistant user an active administrator (Ingress passes the user id)?"""
        if not user_id or self.ha is None:
            return False
        now = asyncio.get_running_loop().time()
        if self._admins is None or now - self._admins[0] > 60 or user_id not in self._admins[1]:
            users = await self.ha.command("config/auth/list") or []
            self._admins = (now, {
                u.get("id"): bool(u.get("is_active", True)) and not u.get("system_generated")
                and (bool(u.get("is_owner")) or "system-admin" in (u.get("group_ids") or []))
                for u in users if isinstance(u, dict)
            })
        return bool(self._admins[1].get(user_id))

    # ---------------------------------------------------------------- reading
    def _require_ha(self) -> HomeAssistantClient:
        if self.ha is None or not self.ha.connected:
            raise ConfigError("not_connected", "Home Assistant is not connected; try again in a moment.", 503)
        return self.ha

    def index(self) -> Index:
        ha = self.ha
        if ha is None:
            return Index({}, [], [], [])
        return Index(ha.states, ha.entity_registry, ha.devices, ha.areas)

    def connection(self) -> dict[str, Any]:
        return {"home_assistant": self.ha.status() if self.ha else None, "versions": {"app": APP_VERSION}}

    def _device(self, device_id: Any) -> dict[str, Any]:
        device = self.store.config["devices"].get(device_id) if isinstance(device_id, str) else None
        if device is None:
            raise ConfigError("unknown_device", "This device is not managed (anymore).", 404)
        return device

    def _entity_of(self, automation_id: str) -> str | None:
        """The automation entity that runs the automation with this id (Home Assistant puts the id in an attribute)."""
        ha = self.ha
        if ha is None:
            return None
        for entity_id, state in ha.states.items():
            if entity_id.startswith("automation.") and (state.get("attributes") or {}).get("id") == automation_id:
                return entity_id
        return None

    async def _loaded(self, automation_id: str) -> tuple[str | None, dict[str, Any] | None]:
        """``(entity id, the configuration Home Assistant runs)`` of an automation, each ``None`` when absent."""
        entity_id = self._entity_of(automation_id)
        if entity_id is None or self.ha is None:
            return None, None
        try:
            result = await self.ha.command("automation/config", entity_id=entity_id)
        except (HomeAssistantError, ConnectionError, TimeoutError):
            return entity_id, None
        config = (result or {}).get("config")
        return entity_id, config if isinstance(config, dict) else None

    # --------------------------------------------------------------- analysis
    def _entity_problems(self, device: dict[str, Any], index: Index) -> list[dict[str, Any]]:
        if self.ha is None or not self.ha.connected:
            return []
        out = []
        for entity_id, uses in sorted(model.referenced_entities(device).items()):
            use = ", ".join(sorted(uses))
            if not index.exists(entity_id):
                out.append({"level": "warning", "code": "missing_entity", "entity_id": entity_id,
                            "message": f"{entity_id} ({use}) does not exist in Home Assistant."})
            elif (index.states.get(entity_id) or {}).get("state") == "unavailable":
                out.append({"level": "info", "code": "unavailable_entity", "entity_id": entity_id,
                            "message": f"{index.name(entity_id)} ({use}) is unavailable right now."})
        return out

    def analysis(self, device: dict[str, Any], index: Index | None = None) -> dict[str, Any]:
        """Everything that follows from the rules alone: the compiled automations, conflicts, notes, summaries."""
        index = index or self.index()
        compiled = compiler.compile_device(device, self.store.config["settings"])
        found = conflict_check.analyse(device, index.name)
        return {
            "automations": compiled["automations"],
            "notes": compiled["notes"],
            "conflicts": found,
            "blocking": len(conflict_check.blocking(found)),
            "entity_problems": self._entity_problems(device, index),
            "summaries": {r["id"]: describe.rule(r, index.name, device["targets"]) for r in device["rules"]},
        }

    # ------------------------------------------------------------ rule level
    def _rule_sync(self, device: dict[str, Any], rule: dict[str, Any], analysis: dict[str, Any]) -> dict[str, str]:
        """Is this rule, as it is now, what Home Assistant runs? (Compared with the device as it was deployed.)"""
        records = self.deploys.of_device(device["id"])
        deployed = None
        for record in records.values():
            for old in (record.get("device") or {}).get("rules", []):
                if old["id"] == rule["id"] and (record.get("blocks") is None or any(
                        b["rule_id"] == rule["id"] for b in record["blocks"])):
                    deployed = old
        wanted = device["enabled"] and rule["enabled"]
        if deployed is None:
            return ({"status": "not_deployed", "text": "Not deployed yet"} if wanted
                    else {"status": "off", "text": "Disabled, not in Home Assistant"})
        if not wanted:
            return {"status": "pending", "text": "Disabled here, still running in Home Assistant until you deploy"}
        strip = lambda r: {k: v for k, v in r.items() if k != "imported_from"}  # noqa: E731
        if strip(deployed) != strip(rule):
            return {"status": "pending", "text": "Changed since the last deployment"}
        home = next((a for a in analysis["automations"] if rule["id"] in a["rule_ids"]), None)
        if home is None or home["id"] not in records:
            return {"status": "pending", "text": "Moves to another automation at the next deployment"}
        return {"status": "in_sync", "text": "Deployed"}

    def _rule_views(self, device: dict[str, Any], analysis: dict[str, Any], live: dict[str, Any],
                    index: Index) -> dict[str, dict[str, Any]]:
        connected = bool(self.ha and self.ha.connected)
        states = {a["id"]: a for a in live["automations"]}
        out = {}
        for rule in device["rules"]:
            entities = []
            for entity_id, uses in sorted({**{t: {"controlled"} for t in device["targets"]},
                                           **model.rule_entities(rule)}.items()):
                state = (index.states.get(entity_id) or {}).get("state")
                entities.append({"entity_id": entity_id, "name": index.name(entity_id), "uses": sorted(uses),
                                 "state": state, "status": ("unknown" if not connected else "missing" if not index.exists(entity_id)
                                                            else "unavailable" if state == "unavailable" else "ok")})
            home = next((a for a in analysis["automations"] if rule["id"] in a["rule_ids"]), None)
            sync = self._rule_sync(device, rule, analysis)
            live_home = states.get(home["id"]) if home else None
            if sync["status"] == "in_sync" and live_home and live_home["status"] in ("modified", "missing", "foreign"):
                sync = {"status": live_home["status"], "text": live_home["status_text"]}
            out[rule["id"]] = {
                **self.observer.view(rule["id"]), "sync": sync, "entities": entities,
                "entity_problems": sum(1 for e in entities if e["status"] in ("missing", "unavailable")),
                "automation_id": home["id"] if home else None, "advanced": model.is_advanced(rule),
                "waits": model.waits(rule), "turned_off": bool(live_home and live_home["turned_off"]),
            }
        return out

    async def _live(self, device: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
        """The deployment status of a device: its automations in Home Assistant against the records and the rules."""
        desired = {a["id"]: a for a in analysis["automations"]}
        records = self.deploys.of_device(device["id"])
        connected = bool(self.ha and self.ha.connected)
        items = []
        for automation_id in [*desired, *(r for r in records if r not in desired)]:
            want, record = desired.get(automation_id), records.get(automation_id)
            entity_id, loaded = await self._loaded(automation_id) if connected else (None, None)
            state = ((self.ha.states.get(entity_id) or {}) if self.ha and entity_id else {})
            loaded_hash = compiler.config_hash(compiler.strip_id(loaded)) if loaded is not None else None
            if not connected:
                status = "unknown"
            elif record is None:
                status = "foreign" if entity_id else "not_deployed"
            elif entity_id is None:
                status = "missing"
            elif loaded_hash is not None and loaded_hash != record["hash"]:
                status = "modified"
            elif want is None or want["hash"] != record["hash"]:
                status = "changes_pending"
            else:
                status = "in_sync"
            attributes = state.get("attributes") or {}
            items.append({
                "id": automation_id, "alias": (want or {}).get("alias") or (record or {}).get("config", {}).get("alias"),
                "role": (want or {}).get("role"), "mode": (want or {}).get("mode"),
                "reason": (want or {}).get("reason"), "rule_ids": (want or {}).get("rule_ids", []),
                "wanted": want is not None, "status": status, "status_text": STATUS_TEXT[status],
                "entity_id": entity_id, "ha_state": state.get("state"),
                "turned_off": state.get("state") == "off", "last_triggered": attributes.get("last_triggered"),
                "deployed_at": (record or {}).get("deployed_at"), "deployed_by": (record or {}).get("by"),
                "previous_deployments": len((record or {}).get("history", [])),
            })
        if not items:
            status = "nothing"
        else:
            status = min((i["status"] for i in items), key=STATUS_ORDER.index)
        deployed_at = max((i["deployed_at"] for i in items if i["deployed_at"]), default=None)
        return {"status": status, "status_text": STATUS_TEXT[status], "automations": items,
                "last_sync": deployed_at, "deployed": bool(records),
                "turned_off": any(i["turned_off"] for i in items)}

    def _mismatches(self, device: dict[str, Any], index: Index) -> list[dict[str, Any]]:
        """Occupancy rules whose intended state is not what the entity shows right now (reported, never forced)."""
        out = []
        if not device["enabled"]:
            return out
        for rule in device["rules"]:
            if not rule["enabled"]:
                continue
            expected = compiler.expectations(rule, device["targets"])
            for trigger in rule["triggers"]:
                if trigger["type"] != "occupancy" or not expected["states"]:
                    continue
                occupancy = index.states.get(trigger["entity_id"]) or {}
                if occupancy.get("state") != ("on" if trigger["to"] == "occupied" else "off"):
                    continue
                for target, states in expected["states"].items():
                    current = index.states.get(target) or {}
                    if current.get("state") in (None, "unavailable", "unknown", *states):
                        continue
                    touched = (current.get("last_changed") or "") > (occupancy.get("last_changed") or "")
                    out.append({"rule_id": rule["id"], "entity_id": target, "message": (
                        f"{index.name(trigger['entity_id'])} is {trigger['to']} and “{rule['name']}” leaves "
                        f"{index.name(target)} “{states[0]}”, but it is “{current.get('state')}”. "
                        + ("It changed after the occupancy change, so the rechecks leave it alone: that may have "
                           "been a person." if touched else
                           "The rule's conditions may not hold right now, or the next recheck has not run yet."))})
        return out

    async def _related_automations(self, device: dict[str, Any], index: Index) -> list[dict[str, Any]]:
        """Automations in Home Assistant, not managed here, that reference the controlled entities."""
        ha = self.ha
        if ha is None or not ha.connected:
            return []
        now = asyncio.get_running_loop().time()
        found: dict[str, set[str]] = {}
        for target in device["targets"]:
            cached = self._related.get(target)
            if cached is None or now - cached[0] > self.RELATED_TTL:
                try:
                    result = await ha.command("search/related", item_type="entity", item_id=target)
                except (HomeAssistantError, ConnectionError, TimeoutError) as err:
                    _LOGGER.debug("search/related failed for %s: %s", target, err)
                    continue
                cached = (now, sorted((result or {}).get("automation") or []))
                self._related[target] = cached
            for entity_id in cached[1]:
                if not index.is_own_automation(entity_id):
                    found.setdefault(entity_id, set()).add(target)
        return [{"entity_id": entity_id, "name": index.name(entity_id),
                 "automation_id": ((index.states.get(entity_id) or {}).get("attributes") or {}).get("id"),
                 "state": (index.states.get(entity_id) or {}).get("state"),
                 "targets": sorted(targets),
                 "message": f"“{index.name(entity_id)}” also refers to {', '.join(index.name(t) for t in sorted(targets))}. "
                            "It is not managed here and is left untouched."}
                for entity_id, targets in sorted(found.items())]

    async def _traces(self, automation_id: str) -> list[dict[str, Any]]:
        ha = self.ha
        if ha is None or not ha.connected:
            return []
        try:
            traces = await ha.command("trace/list", domain="automation", item_id=automation_id) or []
        except (HomeAssistantError, ConnectionError, TimeoutError):
            return []
        out = [{"run_id": t.get("run_id"), "start": (t.get("timestamp") or {}).get("start"),
                "state": t.get("state"), "result": t.get("script_execution"), "trigger": t.get("trigger"),
                "error": t.get("error")} for t in traces if isinstance(t, dict)]
        out.sort(key=lambda t: t["start"] or "", reverse=True)
        return out[:8]

    # ------------------------------------------------------------------ views
    def _targets_view(self, device: dict[str, Any], index: Index) -> list[dict[str, Any]]:
        return [index.view(t) for t in device["targets"]]

    async def overview(self) -> dict[str, Any]:
        index = self.index()
        devices = []
        for device in sorted(self.store.config["devices"].values(), key=lambda d: d["name"].casefold()):
            analysis = self.analysis(device, index)
            live = await self._live(device, analysis)
            warnings = (analysis["blocking"] + sum(1 for p in analysis["entity_problems"] if p["level"] == "warning")
                        + (1 if live["status"] in ("modified", "missing", "foreign") else 0)
                        + (1 if live["turned_off"] else 0))
            devices.append({
                "id": device["id"], "name": device["name"], "enabled": device["enabled"],
                "room": index.room_of(device["targets"]),
                "default": bool(model.active_default(device)),
                "targets": self._targets_view(device, index),
                "rules": len(device["rules"]), "rules_enabled": sum(1 for r in device["rules"] if r["enabled"]),
                "automations": len(analysis["automations"]), "status": live["status"],
                "status_text": live["status_text"], "last_sync": live["last_sync"], "warnings": warnings,
                "blocking": analysis["blocking"], "turned_off": live["turned_off"],
                "summaries": [{"id": r["id"], "name": r["name"], "enabled": r["enabled"],
                               "text": analysis["summaries"][r["id"]]["text"]} for r in device["rules"]],
            })
        return {"devices": devices, "orphans": self.orphans(), "rooms": self._room_groups(devices),
                "totals": {"devices": len(devices), "rules": sum(d["rules"] for d in devices),
                           "automations": sum(d["automations"] for d in devices),
                           "warnings": sum(d["warnings"] for d in devices)}}

    @staticmethod
    def _room_groups(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The Overview's groups: one per room that has managed devices, then "Multiple rooms", then "Unassigned".

        Every device is in exactly one group. The groups are for finding things; each device still has its own
        automation(s), whatever room it is listed in.
        """
        groups: dict[str, dict[str, Any]] = {}
        for device in devices:
            room = device["room"]
            key = room["area_id"] if room["kind"] == "room" else room["kind"]
            group = groups.setdefault(key, {"key": key, "kind": room["kind"], "area_id": room["area_id"],
                                            "name": room["name"], "device_ids": [], "rules": 0, "warnings": 0})
            group["device_ids"].append(device["id"])
            group["rules"] += device["rules"]
            group["warnings"] += device["warnings"]
        order = {"room": 0, "multiple": 1, "unassigned": 2}
        return sorted(groups.values(), key=lambda g: (order[g["kind"]], str(g["name"]).casefold()))

    async def device_view(self, device_id: Any) -> dict[str, Any]:
        device = self._device(device_id)
        index = self.index()
        analysis = self.analysis(device, index)
        live = await self._live(device, analysis)
        for item in live["automations"]:
            item["traces"] = await self._traces(item["id"]) if item["entity_id"] else []
            if item["entity_id"]:
                await self.observer.refresh(item["id"])
        return {
            "rule_diagnostics": self._rule_views(device, analysis, live, index),
            "room": index.room_of(device["targets"]),
            "default_summary": (describe.default_rule(device["default_rule"], index.name, device["targets"])
                                if device.get("default_rule") else None),
            "originals": self._originals(device, index),
            "device": device, "targets": self._targets_view(device, index),
            "ha_device": index.device_name(device["ha_device_id"]),
            "summaries": analysis["summaries"], "notes": analysis["notes"], "conflicts": analysis["conflicts"],
            "blocking": analysis["blocking"], "entity_problems": analysis["entity_problems"],
            "yaml": "".join(compiler.to_yaml(compiler.with_id(a)) for a in analysis["automations"]),
            "live": live, "mismatches": self._mismatches(device, index),
            "related": await self._related_automations(device, index),
            "last_result": self.last_results.get(device["id"]),
            "blueprint_inputs": len(blueprint.inputs_of(device)),
        }

    def _originals(self, device: dict[str, Any], index: Index) -> list[dict[str, Any]]:
        """For imported rules: the automation each was copied from, when it still exists in Home Assistant."""
        out = []
        for rule in device["rules"]:
            origin = rule.get("imported_from")
            if not origin:
                continue
            entity_id = next((e for e, s in index.states.items() if e.startswith("automation.")
                              and str((s.get("attributes") or {}).get("id")) == origin["automation_id"]), None)
            state = (index.states.get(entity_id) or {}).get("state") if entity_id else None
            out.append({"rule_id": rule["id"], "rule_name": rule["name"], "automation_id": origin["automation_id"],
                        "alias": origin["alias"], "entity_id": entity_id, "state": state,
                        "running": state == "on", "allow_both": origin["allow_both"], "enabled": rule["enabled"]})
        return out

    def orphans(self) -> list[dict[str, Any]]:
        """Automations this App deployed for devices that are no longer in the configuration (import, restore)."""
        devices = self.store.config["devices"]
        return [{"id": automation_id, "alias": record.get("config", {}).get("alias"),
                 "device_id": record.get("device_id"), "deployed_at": record.get("deployed_at"),
                 "entity_id": self._entity_of(automation_id)}
                for automation_id, record in sorted(self.deploys.records.items())
                if record.get("device_id") not in devices]

    # ---------------------------------------------------------------- changes
    def _save(self, new_config: dict[str, Any], expected_revision: Any, reason: str, by: str | None) -> dict[str, Any]:
        clean = model.validate_config(new_config)
        for device in clean["devices"].values():  # a confirmation belongs to a conflict that still exists
            keys = {c["key"] for c in conflict_check.analyse(device)}
            device["acknowledged"] = [k for k in device["acknowledged"] if k in keys]
        saved = self.store.save(clean, expected_revision=expected_revision, reason=reason, by=by)
        keep = {r["id"] for d in clean["devices"].values() for r in d["rules"]} | {
            r["id"] for record in self.deploys.records.values() for r in (record.get("device") or {}).get("rules", [])}
        self.observer.stats.forget(keep)  # observations of rules that exist nowhere anymore
        return saved

    def _check_targets(self, targets: Any, index: Index, own_device: str | None = None) -> None:
        if not isinstance(targets, list):
            return
        problems = []
        for target in targets:
            if not isinstance(target, str):
                continue
            if index.is_own_automation(target) or target.startswith("automation."):
                problems.append({"path": "targets", "message": "An automation cannot be a controlled entity."})
            elif self.ha and self.ha.connected and not index.exists(target):
                problems.append({"path": "targets", "message": f"{target} does not exist in Home Assistant."})
        if problems:
            raise ConfigError("invalid", problems[0]["message"], problems=problems)

    def create_device(self, data: dict[str, Any], expected_revision: Any, by: str | None) -> dict[str, Any]:
        index = self.index()
        self._check_targets(data.get("targets"), index)
        device_id = model.new_device_id()
        new = copy.deepcopy(self.store.config)
        name = data.get("name")
        if not (isinstance(name, str) and name.strip()):
            targets = data.get("targets") if isinstance(data.get("targets"), list) else []
            name = index.device_name(data.get("ha_device_id")) or (index.name(targets[0]) if targets else "")
        new["devices"][device_id] = {"name": name, "targets": data.get("targets"), "enabled": True,
                                     "ha_device_id": data.get("ha_device_id"), "rules": []}
        return {**self._save(new, expected_revision, f"device added: {device_id}", by), "device_id": device_id}

    def update_device(self, device_id: Any, patch: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        device = self._device(device_id)
        if not isinstance(patch, dict):
            raise ConfigError("invalid", "Nothing to change")
        new = copy.deepcopy(self.store.config)
        target = new["devices"][device["id"]]
        if "targets" in patch:
            self._check_targets(patch["targets"], self.index(), device["id"])
            removed = [t for t in device["targets"] if t not in (patch["targets"] or [])]
            used = sorted({e for r in device["rules"] for a in r["actions"]
                           if a["type"] == "service" and isinstance(a["target"], list) for e in a["target"] if e in removed})
            if used:
                raise ConfigError("invalid", f"{', '.join(used)} is still used by a rule's action; change that rule first.",
                                  problems=[{"path": "targets", "message": "Still used by a rule's action."}])
            target["targets"] = patch["targets"]
        for key in ("name", "enabled", "blueprint", "default_rule"):
            if key in patch:
                target[key] = patch[key]
        acknowledged = set(target.get("acknowledged") or [])
        if isinstance(patch.get("acknowledge"), str):
            acknowledged.add(patch["acknowledge"])
        if isinstance(patch.get("unacknowledge"), str):
            acknowledged.discard(patch["unacknowledge"])
        target["acknowledged"] = sorted(acknowledged)
        return self._save(new, expected_revision, f"device changed: {device['id']}", by)

    def _draft(self, device: dict[str, Any], rule: Any) -> tuple[dict[str, Any], str, bool]:
        """The device with this rule put in (replacing the rule with its id, or added)."""
        if not isinstance(rule, dict):
            raise ConfigError("invalid", "Not a rule")
        rule = copy.deepcopy(rule)
        is_new = not rule.get("id")
        if is_new:
            rule["id"] = model.new_rule_id()
        draft = copy.deepcopy(device)
        if not (isinstance(rule.get("name"), str) and rule["name"].strip()):
            rule["name"] = f"Rule {len(draft['rules']) + (1 if is_new else 0)}"
        for i, existing in enumerate(draft["rules"]):
            if existing["id"] == rule["id"]:
                draft["rules"][i] = rule
                break
        else:
            if not is_new:
                raise ConfigError("unknown_rule", "This rule does not exist (anymore).", 404)
            draft["rules"].append(rule)
        return draft, rule["id"], is_new

    def preview_rule(self, device_id: Any, rule: Any) -> dict[str, Any]:
        """What a rule being edited would give: validity, its summary, the automation and the conflicts."""
        device = self._device(device_id)
        draft, rule_id, _ = self._draft(device, rule)
        problems = model.Problems()
        clean = model.validate_device(device["id"], draft, "device", problems)
        if problems.items:
            prefix = f"device.rules[{next(i for i, r in enumerate(draft['rules']) if r['id'] == rule_id)}]."
            return {"valid": False, "problems": [{**p, "path": p["path"].removeprefix(prefix)} for p in problems.items]}
        analysis = self.analysis(clean)
        mine = [a for a in analysis["automations"] if rule_id in a["rule_ids"]]
        clean_rule = next(r for r in clean["rules"] if r["id"] == rule_id)
        expected = compiler.expectations(clean_rule, clean["targets"])
        return {
            "valid": True, "problems": [], "rule": clean_rule, "summary": analysis["summaries"][rule_id],
            "notes": [n for n in analysis["notes"] if n.get("rule_id") == rule_id],
            "conflicts": [c for c in analysis["conflicts"] if rule_id in c["rule_ids"]],
            "automation": ({"id": mine[0]["id"], "alias": mine[0]["alias"], "role": mine[0]["role"],
                            "mode": mine[0]["mode"], "reason": mine[0]["reason"],
                            "rules": len(mine[0]["rule_ids"])} if mine else None),
            "yaml": "".join(compiler.to_yaml(compiler.with_id(a)) for a in mine),
            "has_occupancy": any(t["type"] == "occupancy" for t in clean_rule["triggers"]),
            "has_delay": model.waits(clean_rule), "advanced": model.is_advanced(clean_rule),
            "recheck": {**model.rule_recheck(clean_rule, self.store.config["settings"]),
                        "active": expected["repeatable"] and clean_rule["recheck"]["enabled"],
                        "why_not": compiler.RECHECK_OFF.get(expected["why"]) if not expected["repeatable"] else None},
        }

    def preview_default(self, device_id: Any, default: Any) -> dict[str, Any]:
        """What a default rule being edited would give (like ``preview_rule``; nothing is saved)."""
        device = self._device(device_id)
        problems = model.Problems()
        clean = model.validate_device(device["id"], {**device, "default_rule": default}, "device", problems)
        if problems.items:
            names = self.index().name
            return {"valid": False, "problems": [{**p, "path": p["path"].removeprefix("device.default_rule.")} for p in problems.items],
                    "rules": [{"name": r["name"], "enabled": r["enabled"], "text": describe.rule(r, names, device["targets"])["text"]}
                              for r in device["rules"]]}
        index = self.index()
        analysis = self.analysis(clean, index)
        configured = clean.get("default_rule")
        shared = next((a for a in analysis["automations"] if a["role"] == "shared"), None)
        return {
            "valid": True, "problems": [], "default_rule": configured,
            "summary": describe.default_rule(configured, index.name, clean["targets"]) if configured else None,
            "active": bool(shared and shared.get("default")),
            "notes": [n for n in analysis["notes"] if n["code"] == "default_without_rules"],
            "automation": ({"id": shared["id"], "alias": shared["alias"], "mode": shared["mode"], "reason": shared["reason"],
                            "rules": len(shared["rule_ids"])} if shared else None),
            "yaml": compiler.to_yaml(compiler.with_id(shared)) if shared else "",
            "rules": [{"name": r["name"], "enabled": r["enabled"], "text": analysis["summaries"][r["id"]]["text"]} for r in clean["rules"]],
        }

    def save_rule(self, device_id: Any, rule: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        device = self._device(device_id)
        draft, rule_id, is_new = self._draft(device, rule)
        new = copy.deepcopy(self.store.config)
        new["devices"][device["id"]] = draft
        result = self._save(new, expected_revision, f"rule {'added' if is_new else 'changed'}: {rule_id}", by)
        return {**result, "rule_id": rule_id}

    def delete_rule(self, device_id: Any, rule_id: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        device = self._device(device_id)
        if not any(r["id"] == rule_id for r in device["rules"]):
            raise ConfigError("unknown_rule", "This rule does not exist (anymore).", 404)
        new = copy.deepcopy(self.store.config)
        new["devices"][device["id"]]["rules"] = [r for r in device["rules"] if r["id"] != rule_id]
        return self._save(new, expected_revision, f"rule removed: {rule_id}", by)

    def move_rule(self, device_id: Any, rule_id: Any, direction: Any, expected_revision: Any,
                  by: str | None) -> dict[str, Any]:
        device = self._device(device_id)
        ids = [r["id"] for r in device["rules"]]
        if rule_id not in ids or direction not in ("up", "down"):
            raise ConfigError("unknown_rule", "This rule does not exist (anymore).", 404)
        i = ids.index(rule_id)
        j = i - 1 if direction == "up" else i + 1
        if not 0 <= j < len(ids):
            return {"revision": self.store.revision, "changed": False}
        new = copy.deepcopy(self.store.config)
        rules = new["devices"][device["id"]]["rules"]
        rules[i], rules[j] = rules[j], rules[i]
        return self._save(new, expected_revision, f"rule moved: {rule_id}", by)

    def save_settings(self, settings: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        new = copy.deepcopy(self.store.config)
        new["settings"] = settings
        return self._save(new, expected_revision, "settings changed", by)

    # --------------------------------------------------- export, import, backups
    def review_import(self, document: Any) -> dict[str, Any]:
        new = self.store.parse_import(document)
        before, after = self.store.config["devices"], new["devices"]
        changes = []
        for device_id in sorted(set(before) | set(after)):
            old, cur = before.get(device_id), after.get(device_id)
            if old is None:
                changes.append({"type": "added", "name": cur["name"], "rules": len(cur["rules"])})
            elif cur is None:
                deployed = bool(self.deploys.of_device(device_id))
                changes.append({"type": "removed", "name": old["name"], "rules": len(old["rules"]), "deployed": deployed})
            elif old != cur:
                changes.append({"type": "changed", "name": cur["name"], "rules": len(cur["rules"])})
        if before is not after and self.store.config["settings"] != new["settings"]:
            changes.append({"type": "settings", "name": "Settings", "rules": 0})
        return {"valid": True, "changes": changes, "devices": len(after), "revision": self.store.revision,
                "note": "Nothing in Home Assistant changes: deployed automations stay as they are until you deploy "
                        "each device again. Automations of devices that disappear are listed on the Overview."}

    def apply_import(self, document: Any, expected_revision: Any, by: str | None, reason: str = "import") -> dict[str, Any]:
        return self._save(self.store.parse_import(document), expected_revision, reason, by)

    def review_backup(self, name: Any) -> dict[str, Any]:
        return {**self.review_import(self.store.backup_document(name)), "name": name}

    def restore_backup(self, name: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        return self.apply_import(self.store.backup_document(name), expected_revision, by, "backup restored")

    # -------------------------------------------------------------- deployment
    async def _validate(self, config: dict[str, Any]) -> list[str]:
        """Home Assistant's own validation of the generated triggers, conditions and actions (changes nothing)."""
        ha = self._require_ha()
        try:
            result = await ha.command("validate_config", triggers=config["triggers"], conditions=config["conditions"],
                                      actions=config["actions"]) or {}
        except HomeAssistantError as err:
            return [f"Home Assistant could not validate the automation: {err.message}"]
        return [f"{key}: {value.get('error')}" for key, value in result.items()
                if isinstance(value, dict) and not value.get("valid")]

    async def plan(self, device_id: Any) -> dict[str, Any]:
        device = self._device(device_id)
        self._require_ha()
        assert self.api is not None
        index = self.index()
        analysis = self.analysis(device, index)
        desired = {a["id"]: a for a in analysis["automations"]}
        records = self.deploys.of_device(device["id"])
        items, fingerprint = [], [self.store.revision]
        for automation_id in [*desired, *(r for r in records if r not in desired)]:
            want, record = desired.get(automation_id), records.get(automation_id)
            try:
                actual = await self.api.get(automation_id)
            except DeployError as err:
                raise ConfigError("ha_error", str(err), 502) from err
            actual_hash = compiler.config_hash(compiler.strip_id(actual)) if actual is not None else None
            if want is not None and actual_hash == want["hash"]:
                external = None  # exactly what would be deployed: nothing to protect
            elif record is None:
                external = "foreign" if actual is not None else None
            elif actual is None:
                external = "missing"
            else:
                external = "modified" if actual_hash != record["hash"] else None
            if want is not None:
                operation = "create" if actual is None else ("none" if actual_hash == want["hash"] else "update")
            else:
                operation = "delete" if actual is not None else "forget"
            new_yaml = compiler.to_yaml(compiler.with_id(want)) if want else ""
            old_yaml = compiler.to_yaml(actual) if actual is not None else ""
            diff = "".join(difflib.unified_diff(old_yaml.splitlines(True), new_yaml.splitlines(True),
                                                "in Home Assistant now", "to deploy", n=2))
            errors = await self._validate(want["config"]) if want and operation != "none" else []
            items.append({
                "id": automation_id, "alias": (want or {}).get("alias") or (actual or {}).get("alias"),
                "operation": operation, "external": external, "role": (want or {}).get("role"),
                "mode": (want or {}).get("mode"), "reason": (want or {}).get("reason"),
                "rules": len((want or {}).get("rule_ids", [])), "yaml": new_yaml, "current_yaml": old_yaml,
                "diff": diff, "errors": errors, "entity_id": self._entity_of(automation_id),
            })
            fingerprint.append([automation_id, operation, (want or {}).get("hash"), actual_hash])
        blocking = conflict_check.blocking(analysis["conflicts"])
        invalid = [e for i in items for e in i["errors"]]
        reasons = []
        duplicates = [o for o in self._originals(device, index) if o["running"] and not o["allow_both"] and o["enabled"]
                      and device["enabled"]]
        for original in duplicates:
            reasons.append(f"“{original['rule_name']}” is a copy of the automation “{original['alias']}”, which is "
                           "still turned on in Home Assistant: both would run. Turn the original off in Home "
                           "Assistant (Automation Builder never changes it), or allow both in the review below.")
        fingerprint.append([[o["rule_id"], o["running"], o["allow_both"]] for o in self._originals(device, index)])
        if blocking:
            reasons.append(f"{len(blocking)} conflict{'s' if len(blocking) != 1 else ''} between rules must be "
                           "resolved or confirmed first.")
        if invalid:
            reasons.append("Home Assistant does not accept the generated automation: " + "; ".join(invalid))
        changes = any(i["operation"] != "none" for i in items)
        return {
            "device_id": device["id"], "name": device["name"], "revision": self.store.revision, "items": items,
            "conflicts": analysis["conflicts"], "notes": analysis["notes"],
            "entity_problems": analysis["entity_problems"], "changes": changes,
            "needs_overwrite": any(i["external"] in ("modified", "foreign") for i in items),
            "can_deploy": not reasons and changes, "reasons": reasons, "duplicates": duplicates,
            "plan_hash": compiler.config_hash(fingerprint),
        }

    async def _confirm_loaded(self, automation_id: str, config: dict[str, Any]) -> str:
        """Wait until Home Assistant runs exactly this configuration; returns the automation's entity id."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONFIRM_TIMEOUT
        wanted = compiler.config_hash(config)
        seen_entity = False
        while True:
            entity_id, loaded = await self._loaded(automation_id)
            seen_entity = seen_entity or entity_id is not None
            if loaded is not None and compiler.config_hash(compiler.strip_id(loaded)) == wanted:
                state = (self.ha.states.get(entity_id) or {}).get("state") if self.ha and entity_id else None
                if state == "unavailable":
                    raise DeployError("Home Assistant saved the automation but could not set it up: it shows as "
                                      "unavailable. Home Assistant's log has the reason.")
                return entity_id or ""
            if loop.time() >= deadline:
                break
            await asyncio.sleep(self.CONFIRM_INTERVAL)
        if not seen_entity:
            raise DeployError(
                "Home Assistant saved the automation in automations.yaml but did not load it. Check that "
                "configuration.yaml contains the line “automation: !include automations.yaml” (it is there by "
                "default).")
        raise DeployError("Home Assistant did not load the new version of the automation in time.")

    async def _confirm_removed(self, automation_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.CONFIRM_TIMEOUT
        while self._entity_of(automation_id) is not None:
            if loop.time() >= deadline:
                raise DeployError("Home Assistant removed the automation from automations.yaml, but its entity is "
                                  "still there.")
            await asyncio.sleep(self.CONFIRM_INTERVAL)

    async def _undo(self, applied: list[tuple[str, dict[str, Any] | None]]) -> list[str]:
        """Put back what this deployment replaced. Returns what could not be restored."""
        assert self.api is not None
        failed = []
        for automation_id, previous in reversed(applied):
            try:
                if previous is None:
                    await self.api.delete(automation_id)
                else:
                    await self.api.put(automation_id, compiler.strip_id(previous))
            except DeployError as err:
                failed.append(f"{automation_id}: {err}")
        return failed

    async def deploy(self, device_id: Any, expected_revision: Any, plan_hash: Any, overwrite: bool,
                     by: str | None) -> dict[str, Any]:
        async with self._deploy_lock:
            self.store.check_revision(expected_revision)
            plan = await self.plan(device_id)
            device = self._device(device_id)
            if plan["plan_hash"] != plan_hash:
                raise ConfigError("plan_changed", "Something changed since you looked at this deployment (the rules, "
                                                  "or the automation in Home Assistant). Review it again.", 409)
            if plan["reasons"]:
                raise ConfigError("blocked", " ".join(plan["reasons"]), 409)
            if plan["needs_overwrite"] and not overwrite:
                raise ConfigError("external_change", "The automation in Home Assistant was changed outside this App. "
                                                     "Confirm that it may be replaced.", 409)
            assert self.api is not None
            desired = {a["id"]: a for a in self.analysis(device)["automations"]}
            applied: list[tuple[str, dict[str, Any] | None]] = []
            done = []
            self.deploys.begin(device, {
                i["id"]: ({"hash": desired[i["id"]]["hash"], "config": desired[i["id"]]["config"],
                           "blocks": desired[i["id"]]["blocks"]} if i["operation"] in ("create", "update") else None)
                for i in plan["items"] if i["operation"] in ("create", "update", "delete")}, by)
            try:
                for item in sorted(plan["items"], key=lambda i: i["operation"] in ("delete", "forget")):
                    automation_id, operation = item["id"], item["operation"]
                    if operation in ("create", "update"):
                        previous = await self.api.get(automation_id)
                        await self.api.put(automation_id, desired[automation_id]["config"])
                        applied.append((automation_id, previous))
                        entity_id = await self._confirm_loaded(automation_id, desired[automation_id]["config"])
                        done.append({"id": automation_id, "operation": operation, "entity_id": entity_id})
                    elif operation == "delete":
                        previous = await self.api.get(automation_id)
                        await self.api.delete(automation_id)
                        applied.append((automation_id, previous))
                        await self._confirm_removed(automation_id)
                        done.append({"id": automation_id, "operation": operation, "entity_id": None})
            except (DeployError, ConnectionError, TimeoutError) as err:
                not_restored = await self._undo(applied)
                restored = ("Nothing was changed in Home Assistant." if not applied else
                            "The previous automation was put back." if not not_restored else
                            "The previous state could NOT be restored: " + "; ".join(not_restored))
                message = f"Deployment failed: {err} {restored}"
                _LOGGER.error("Deployment of %s failed: %s %s", device["id"], err, restored)
                self.last_results[device["id"]] = {"ok": False, "at": utcnow_iso(), "message": message}
                if not not_restored:
                    self.deploys.finish()  # otherwise the note stays: the next start looks at Home Assistant again
                raise ConfigError("deploy_failed", message, 502) from err
            for item in plan["items"]:
                automation_id = item["id"]
                if automation_id in desired:
                    record = self.deploys.records.get(automation_id)
                    if record is None or record["hash"] != desired[automation_id]["hash"] or item["operation"] != "none":
                        self.deploys.record(automation_id, device_id=device["id"],
                                            config_hash=desired[automation_id]["hash"],
                                            config=desired[automation_id]["config"], device=device, by=by,
                                            blocks=desired[automation_id]["blocks"])
                else:
                    self.deploys.forget(automation_id)
            self.deploys.finish()
            self._related.clear()
            message = ", ".join(f"{d['operation']}d {d['entity_id'] or d['id']}".replace("deleted", "removed")
                                for d in done) or "nothing to change"
            _LOGGER.info("Deployed %s: %s", device["id"], message)
            self.last_results[device["id"]] = {"ok": True, "at": utcnow_iso(),
                                               "message": f"Home Assistant accepted the deployment ({message})."}
            return {"deployed": True, "items": done, "message": self.last_results[device["id"]]["message"]}

    def _snapshot(self, device_id: str, which: str) -> dict[str, Any]:
        """The device as it was at the current (``deployed``) or the previous (``previous``) deployment."""
        records = self.deploys.of_device(device_id)
        candidates = []
        for record in records.values():
            source = record if which == "deployed" else (record.get("history") or [None])[0]
            if source and source.get("device"):
                candidates.append((source.get("deployed_at") or "", source["device"]))
        if not candidates:
            raise ConfigError("no_previous", "There is no earlier deployment of this device to go back to."
                              if which == "previous" else "This device has not been deployed yet.", 409)
        return copy.deepcopy(max(candidates, key=lambda c: c[0])[1])

    def restore_device(self, device_id: Any, which: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        """Put a device's rules back to a deployed version (the rules only; deploying is a separate, explicit step)."""
        device = self._device(device_id)
        if which not in ("deployed", "previous"):
            raise ConfigError("invalid", "Unknown version")
        new = copy.deepcopy(self.store.config)
        new["devices"][device["id"]] = self._snapshot(device["id"], which)
        return self._save(new, expected_revision, f"device restored ({which} deployment): {device['id']}", by)

    async def _remove_automation(self, automation_id: str, overwrite: bool) -> None:
        assert self.api is not None
        record = self.deploys.records.get(automation_id)
        try:
            actual = await self.api.get(automation_id)
            if actual is not None:
                changed = record is None or compiler.config_hash(compiler.strip_id(actual)) != record["hash"]
                if changed and not overwrite:
                    raise ConfigError("external_change", "The automation in Home Assistant was changed outside this "
                                                         "App. Confirm that it may be removed anyway.", 409)
                await self.api.delete(automation_id)
                await self._confirm_removed(automation_id)
        except DeployError as err:
            raise ConfigError("deploy_failed", f"Could not remove the automation: {err}", 502) from err
        self.deploys.forget(automation_id)

    async def delete_device(self, device_id: Any, expected_revision: Any, overwrite: bool, by: str | None) -> dict[str, Any]:
        """Remove a device with its rules and, first, its automations in Home Assistant."""
        async with self._deploy_lock:
            device = self._device(device_id)
            self.store.check_revision(expected_revision)
            records = self.deploys.of_device(device["id"])
            if records:
                self._require_ha()
                for automation_id in records:
                    await self._remove_automation(automation_id, overwrite)
            new = copy.deepcopy(self.store.config)
            del new["devices"][device["id"]]
            self.last_results.pop(device["id"], None)
            return {**self._save(new, expected_revision, f"device removed: {device['id']}", by),
                    "removed_automations": len(records)}

    async def remove_orphan(self, automation_id: Any, overwrite: bool) -> dict[str, Any]:
        async with self._deploy_lock:
            if automation_id not in {o["id"] for o in self.orphans()}:
                raise ConfigError("unknown_automation", "This automation is not a leftover of this App.", 404)
            self._require_ha()
            await self._remove_automation(automation_id, overwrite)
            return {"removed": automation_id}

    async def recover(self) -> dict[str, Any] | None:
        """Finish the bookkeeping of a deployment that was interrupted (the App or Home Assistant stopped midway).

        Nothing is written to Home Assistant here. For each automation the interrupted deployment was about to
        write, what Home Assistant has now is compared with what was intended:

        * exactly the intended automation: the write went through; it is recorded as deployed;
        * gone, and the deployment was removing it: the record is dropped;
        * anything else: left as it is. The device then shows "changes pending" (the write never happened) or
          "changed outside this App" (a half state), and the next deployment is reviewed as always.
        """
        pending = self.deploys.pending
        if not pending or self.api is None or self.ha is None or not self.ha.connected:
            return None
        async with self._deploy_lock:
            adopted, dropped, left = [], [], []
            try:
                for automation_id, intended in (pending.get("items") or {}).items():
                    actual = await self.api.get(automation_id)
                    actual_hash = compiler.config_hash(compiler.strip_id(actual)) if actual is not None else None
                    if intended is None:
                        if actual is None:
                            self.deploys.forget(automation_id)
                            dropped.append(automation_id)
                        else:
                            left.append(automation_id)
                    elif actual_hash == intended.get("hash"):
                        self.deploys.record(automation_id, device_id=pending["device_id"], config_hash=intended["hash"],
                                            config=intended["config"], device=pending["device"], by=pending.get("by"),
                                            blocks=intended.get("blocks"))
                        adopted.append(automation_id)
                    else:
                        left.append(automation_id)
            except (DeployError, NotAllowed, ConnectionError, TimeoutError) as err:
                _LOGGER.warning("Could not look into the interrupted deployment yet: %s", err)
                return None
            self.deploys.finish()
            parts = ([f"{len(adopted)} automation{'s' if len(adopted) != 1 else ''} had reached Home Assistant and "
                      f"{'are' if len(adopted) != 1 else 'is'} recorded as deployed"] if adopted else []) \
                + ([f"{len(dropped)} had been removed"] if dropped else []) \
                + ([f"{len(left)} had not been written (or only partly): review and deploy again"] if left else [])
            message = ("A deployment was interrupted (" + str(pending.get("at")) + "). " + "; ".join(parts) + "."
                       if parts else "A deployment was interrupted before anything was written.")
            _LOGGER.warning("%s (device %s)", message, pending.get("device_id"))
            result = {"ok": not left, "at": utcnow_iso(), "message": message, "recovered": True}
            if pending.get("device_id") in self.store.config["devices"]:
                self.last_results[pending["device_id"]] = result
            return {**result, "adopted": adopted, "dropped": dropped, "left": left}

    # --------------------------------------------------------------- blueprint
    async def blueprint_view(self, device_id: Any, draft: Any = None) -> dict[str, Any]:
        """The blueprint of a device with the stored choices (or with ``draft``, the choices being edited)."""
        device = self._device(device_id)
        index = self.index()
        if draft is not None:
            problems = model.Problems()
            device = model.validate_device(device["id"], {**device, "blueprint": draft}, "device", problems)
            if problems.items:
                raise ConfigError("invalid", problems.items[0]["message"], problems=problems.items)
        built = blueprint.build(device, self.store.config["settings"], names=index.name)
        validation: list[dict[str, str]] = [{"level": "error", "message": p} for p in built["problems"]]
        if built["config"] is not None and not built["problems"]:
            if self.ha is not None and self.ha.connected:
                errors = await self._validate(built["config"])
                validation += [{"level": "error", "message": f"Home Assistant does not accept it: {e}"} for e in errors]
                if not errors:
                    validation.append({"level": "ok", "message": "Home Assistant accepts the triggers, conditions and "
                                                                 "actions (checked with every input at its default)."})
            else:
                validation.append({"level": "warning", "message": "Home Assistant is not connected: the blueprint "
                                                                  "could not be checked there."})
            validation.append({"level": "ok", "message": "With every input at its default, the blueprint is exactly "
                                                         "the automation this device deploys."})
        validation += [{"level": "warning", "message": w} for w in built["warnings"]]
        installed = self.deploys.blueprints.get(built["path"]) if built["path"] else None
        digest = compiler.config_hash(built["yaml"]) if built["yaml"] else None
        return {
            "device_id": device["id"], "device_name": device["name"], "revision": self.store.revision,
            "settings": device.get("blueprint") or {"name": "", "description": "", "automation": None, "inputs": {}},
            "name": built.get("name"), "description": built.get("description"), "inputs": built["inputs"],
            "groups": [g for g in blueprint.GROUP_ORDER if any(i["group"] == g for i in built["inputs"])],
            "automation": built["automation"], "automations": built["automations"], "yaml": built["yaml"],
            "path": built["path"], "validation": validation,
            "valid": bool(built["yaml"]) and not any(v["level"] == "error" for v in validation),
            "installed": ({**installed, "current": installed["hash"] == digest} if installed else None),
            "filename": f"{blueprint._slug(built.get('name') or device['name']) or 'blueprint'}.yaml",  # noqa: SLF001
        }

    async def blueprint_install(self, device_id: Any, overwrite: bool, by: str | None) -> dict[str, Any]:
        """Save the device's blueprint in Home Assistant's blueprint folder (``blueprint/save``, own folder only)."""
        view = await self.blueprint_view(device_id)
        if not view["valid"]:
            raise ConfigError("blocked", "The blueprint has errors; see the validation results.", 409)
        ha = self._require_ha()
        path, digest = view["path"], compiler.config_hash(view["yaml"])
        try:
            existing = await ha.command("blueprint/list", domain="automation") or {}
        except HomeAssistantError as err:
            raise ConfigError("ha_error", f"Home Assistant could not list its blueprints: {err.message}", 502) from err
        exists = path in existing
        ours = path in self.deploys.blueprints
        if exists and not overwrite:
            raise ConfigError("exists", ("This blueprint is installed already. Installing again replaces the file; "
                                         "automations made from it are reloaded with the new version." if ours else
                                         "A blueprint file with this name exists in Home Assistant that Automation "
                                         "Builder did not install."), 409, installed_by_app=ours)
        try:
            await ha.command("blueprint/save", domain="automation", path=path, yaml=view["yaml"], allow_override=exists)
        except HomeAssistantError as err:
            raise ConfigError("ha_error", f"Home Assistant did not accept the blueprint: {err.message}", 502) from err
        self.deploys.blueprint_installed(path, digest=digest, device_id=view["device_id"], name=view["name"], by=by)
        _LOGGER.info("Blueprint installed: blueprints/automation/%s", path)
        return {"installed": True, "path": f"blueprints/automation/{path}", "replaced": exists,
                "message": f"Installed as blueprints/automation/{path}. Create automations from it under Settings → "
                           "Automations & scenes → Blueprints."}

    # ------------------------------------------------------------------ import
    def import_list(self) -> dict[str, Any]:
        """The automations in Home Assistant that this App does not manage."""
        self._require_ha()
        index = self.index()
        imported = {r["imported_from"]["automation_id"]: (d["id"], d["name"], r["name"])
                    for d in self.store.config["devices"].values() for r in d["rules"] if r.get("imported_from")}
        out = []
        for entity_id, state in index.states.items():
            if not entity_id.startswith("automation.") or index.is_own_automation(entity_id):
                continue
            attributes = state.get("attributes") or {}
            automation_id = str(attributes.get("id")) if attributes.get("id") is not None else None
            copy_of = imported.get(automation_id) if automation_id else None
            out.append({"entity_id": entity_id, "name": index.name(entity_id), "id": automation_id,
                        "state": state.get("state"), "last_triggered": attributes.get("last_triggered"),
                        "mode": attributes.get("mode"),
                        "imported": ({"device_id": copy_of[0], "device": copy_of[1], "rule": copy_of[2]} if copy_of else None)})
        out.sort(key=lambda a: str(a["name"]).casefold())
        return {"automations": out}

    async def import_analyse(self, entity_id: Any) -> dict[str, Any]:
        ha = self._require_ha()
        index = self.index()
        if not isinstance(entity_id, str) or not entity_id.startswith("automation.") or entity_id not in index.states:
            raise ConfigError("unknown_automation", "This automation does not exist in Home Assistant.", 404)
        if index.is_own_automation(entity_id):
            raise ConfigError("invalid", "This automation is managed by Automation Builder already.")
        try:
            config = ((await ha.command("automation/config", entity_id=entity_id)) or {}).get("config")
        except HomeAssistantError as err:
            raise ConfigError("ha_error", f"Home Assistant could not return the automation: {err.message}", 502) from err
        result = importer.analyse(config, entity_id=entity_id, state=index.states.get(entity_id),
                                  devices=self.store.config["devices"], settings=self.store.config["settings"],
                                  names=index.name)
        result["original_yaml"] = compiler.to_yaml(config) if isinstance(config, dict) else ""
        result["revision"] = self.store.revision
        return result

    async def import_convert(self, entity_id: Any, destination: Any, expected_revision: Any, by: str | None) -> dict[str, Any]:
        """Copy an analysed automation into the rule model: a new device, or (explicitly chosen) an existing one.

        Nothing is deployed and the original is not touched. While the original is still turned on, deploying
        the copy is refused unless the user allows both to run (``plan``).
        """
        result = await self.import_analyse(entity_id)
        if not result["can_convert"]:
            raise ConfigError("blocked", "This automation cannot be converted faithfully: "
                              + "; ".join(f"{u['what']}: {u['why']}" for u in result["unsupported"][:3]), 409)
        if any(o.get("rule_id") and "imported before" in o["message"] for o in result["overlaps"]):
            raise ConfigError("blocked", "This automation has been imported already.", 409)
        rule = {**result["rule"], "id": model.new_rule_id()}
        targets = [c["entity_id"] for c in result["controlled"]]
        new = copy.deepcopy(self.store.config)
        if destination in (None, "", "new"):
            self._check_targets(targets, self.index())
            device_id = model.new_device_id()
            new["devices"][device_id] = {"name": rule["name"], "targets": targets, "enabled": True,
                                         "ha_device_id": None, "rules": [rule]}
        else:
            device = self._device(destination)
            if not set(targets) <= set(device["targets"]):
                raise ConfigError("invalid", "That device does not control all entities of this automation.")
            device_id = device["id"]
            for action in importer.model_walk(rule["actions"]):  # "all" meant: all entities of the import
                if action.get("type") == "service" and action.get("target") == "all":
                    action["target"] = list(targets)
            new["devices"][device_id]["rules"].append(rule)
        saved = self._save(new, expected_revision, f"automation imported: {rule['id']}", by)
        return {**saved, "device_id": device_id, "rule_id": rule["id"]}

    # ------------------------------------------------------ device automations
    async def device_automations(self, ha_device_id: Any, kind: Any) -> dict[str, Any]:
        """The triggers, conditions or actions Home Assistant itself offers for a device (nothing is made up)."""
        ha = self._require_ha()
        if kind not in ("trigger", "condition", "action") or not isinstance(ha_device_id, str) or not ha_device_id:
            raise ConfigError("invalid", "Unknown device or kind")
        index = self.index()
        try:
            found = await ha.command(f"device_automation/{kind}/list", device_id=ha_device_id) or []
        except HomeAssistantError as err:
            raise ConfigError("ha_error", f"Home Assistant could not list the device's {kind}s: {err.message}", 502) from err
        items = []
        for raw in found:
            if not isinstance(raw, dict):
                continue
            config = {k: v for k, v in raw.items() if k not in ("platform", "trigger", "condition", "metadata")}
            words = [str(config.get("type", "")).replace("_", " ")]
            if config.get("subtype"):
                words.append(str(config["subtype"]).replace("_", " "))
            entity_id = index.entity_by_registry_id(config.get("entity_id")) or (
                config.get("entity_id") if isinstance(config.get("entity_id"), str) and "." in config["entity_id"] else None)
            label = " ".join(w for w in words if w)
            if entity_id:
                label = f"{index.name(entity_id)}: {label}"
            items.append({"config": config, "label": label[:model.MAX_STATE], "domain": config.get("domain"),
                          "secondary": bool((raw.get("metadata") or {}).get("secondary"))})
        items.sort(key=lambda i: (i["secondary"], i["label"].casefold()))
        return {"device_id": ha_device_id, "name": index.device_name(ha_device_id), "kind": kind, "items": items}

    def notify_targets(self) -> dict[str, Any]:
        """Where a notification can go: Home Assistant's notify actions and notify entities."""
        ha = self._require_ha()
        index = self.index()
        services = sorted(f"notify.{name}" for name in (ha.services.get("notify") or {}) if name != "send_message")
        entities = [index.view(e) for e in sorted(index.states) if e.startswith("notify.")]
        return {"actions": [*services, "persistent_notification.create"], "entities": entities}

    # ------------------------------------------------------------ diagnostics
    def settings_view(self) -> dict[str, Any]:
        return {"settings": self.store.config["settings"], "revision": self.store.revision,
                "saved_at": self.store.saved_at, "recovery": self.store.recovery, "backups": self.store.backups(),
                "periodic_choices": list(model.PERIODIC_CHOICES), "connection": self.connection(),
                "deploy_problem": self.deploys.problem,
                "mechanism": {"websocket": self.settings.ha_ws_url, "api": self.settings.ha_api_url,
                              "endpoint": "/config/automation/config/automation_builder_<id>",
                              "commands": sorted(self.ha.commands_sent) if self.ha else [],
                              "requests": dict(self.api.requests) if self.api else {}}}

    async def diagnostics(self) -> dict[str, Any]:
        index = self.index()
        devices = []
        for device in self.store.config["devices"].values():
            analysis = self.analysis(device, index)
            live = await self._live(device, analysis)
            devices.append({"id": device["id"], "name": device["name"], "enabled": device["enabled"],
                            "targets": device["targets"], "rules": len(device["rules"]),
                            "rules_enabled": sum(1 for r in device["rules"] if r["enabled"]),
                            "status": live["status"], "last_sync": live["last_sync"],
                            "automations": live["automations"], "conflicts": analysis["conflicts"],
                            "notes": analysis["notes"], "entity_problems": analysis["entity_problems"],
                            "mismatches": self._mismatches(device, index),
                            "rule_diagnostics": {rid: {k: v for k, v in view.items() if k != "entities"}
                                                 for rid, view in self._rule_views(device, analysis, live, index).items()},
                            "last_result": self.last_results.get(device["id"])})
        return {
            "app": {"name": APP_NAME, "version": APP_VERSION, "generated_at": utcnow_iso()},
            "settings": self.settings.public(), "connection": self.connection(),
            "configuration": {"revision": self.store.revision, "saved_at": self.store.saved_at,
                              "recovery": self.store.recovery, "devices": len(self.store.config["devices"]),
                              "backups": len(self.store.backups()), "settings": self.store.config["settings"]},
            "deployment_records": {"count": len(self.deploys.records), "problem": self.deploys.problem,
                                   "orphans": self.orphans(), "interrupted": bool(self.deploys.pending),
                                   "blueprints": self.deploys.blueprints},
            "devices": devices,
            "commands_sent": dict(self.ha.commands_sent) if self.ha else {},
            "api_requests": dict(self.api.requests) if self.api else {},
            "log": self.buffer.entries("info")[-150:] if self.buffer else [],
        }

    # ---------------------------------------------------------------- catalog
    def catalog(self) -> dict[str, Any]:
        return {**catalog.catalog(), "defaults": {"occupancy": dict(model.OCCUPANCY_DEFAULTS)},
                "settings": self.store.config["settings"], "weekdays": list(model.WEEKDAYS),
                "retrigger": [
                    {"value": "restart", "label": "Start the wait over"},
                    {"value": "single", "label": "Ignore the new trigger"},
                    {"value": "queued", "label": "Run again afterwards"},
                    {"value": "parallel", "label": "Run both at the same time"}],
                "limits": {"targets": model.MAX_TARGETS, "rules": model.MAX_RULES,
                           "action_depth": model.MAX_ACTION_DEPTH}}
