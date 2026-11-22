"""Blueprint export: one generated automation of a device as a native Home Assistant automation blueprint.

A blueprint is the automation the compiler generates, with some of its values
replaced by ``!input`` references. Which values is the user's choice:
``candidates`` lists everything that can become an input (controlled entities,
trigger and sensor entities, thresholds, times, durations, brightness, colour
temperature and other action parameters), each with a **stable identifier**:

* an entity is one input wherever it is used: ``controlled_<object id>`` for a
  controlled entity, ``<domain>_<object id>`` for any other;
* a value is identified by its rule and its place in it:
  ``<rule id>_t1_above``, ``<rule id>_a2_brightness_pct``.

So exporting again after a rule was renamed or a value was changed gives the
same inputs, and an automation made from the earlier blueprint keeps working
with the new one.

``build`` puts :class:`compiler.Input` objects in place of the chosen values and
runs the ordinary compiler, which passes them through. The result is checked:
with every input at its default, the blueprint must be exactly the automation
the device deploys. Nothing of the managed device or its automation changes.

The blueprint runs without this App: it contains only native triggers,
conditions and actions.
"""

from __future__ import annotations

import copy
import re
from typing import Any

import yaml

from . import compiler
from .compiler import Input
from .model import GROUP_TYPES, sub_sequences

MIN_HA_VERSION = "2024.10.0"  # "triggers" / "actions" / "action:" need this release
FOLDER = "automation_builder"

GROUP_ORDER = ("Controlled entities", "Trigger entities", "Sensor entities", "Devices", "Thresholds", "Angles", "Time values",
               "Delays and durations", "Brightness", "Colour temperature", "Cover positions", "Action parameters", "States")
DATA_GROUP = {"brightness_pct": "Brightness", "brightness": "Brightness", "color_temp_kelvin": "Colour temperature"}
DATA_SELECTOR: dict[str, dict[str, Any]] = {
    "brightness_pct": {"number": {"min": 1, "max": 100, "unit_of_measurement": "%", "mode": "slider"}},
    "brightness": {"number": {"min": 1, "max": 255, "mode": "slider"}},
    "color_temp_kelvin": {"color_temp": {"unit": "kelvin", "min": 1500, "max": 9000}},
    "rgb_color": {"color_rgb": {}},
    "transition": {"number": {"min": 0, "max": 300, "unit_of_measurement": "s", "mode": "box"}},
    "percentage": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%", "mode": "slider"}},
    "position": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%", "mode": "slider"}},
    "humidity": {"number": {"min": 0, "max": 100, "unit_of_measurement": "%", "mode": "slider"}},
    "temperature": {"number": {"min": -50, "max": 120, "step": 0.5, "mode": "box"}},
    "volume_level": {"number": {"min": 0, "max": 1, "step": 0.01, "mode": "slider"}},
}
DATA_LABEL = {"brightness_pct": "brightness", "color_temp_kelvin": "colour temperature", "rgb_color": "colour",
              "transition": "transition time", "percentage": "speed", "position": "position",
              "temperature": "temperature", "hvac_mode": "mode", "preset_mode": "preset", "option": "option",
              "humidity": "humidity", "value": "value", "volume_level": "volume"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:50]


def _duration(seconds: int) -> dict[str, int]:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return {"hours": hours, "minutes": minutes, "seconds": secs}


def _value_selector(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolean": {}}
    if isinstance(value, (int, float)):
        return {"number": {"mode": "box", "step": "any"}}
    if isinstance(value, str):
        return {"text": {}}
    return {"object": {}}


class _Collector:
    """Walks a device and collects what can become a blueprint input, with every place the value occurs."""

    def __init__(self, device: dict[str, Any], rule_ids: list[str], names: Any) -> None:
        self.device = device
        self.names = names
        self.items: dict[str, dict[str, Any]] = {}
        self.fixed: list[str] = []
        self.templates: list[tuple[str, str]] = []  # user-written templates: kept as written
        for i, target in enumerate(device["targets"]):
            self.entity(target, ("targets", i), "Controlled entities", "Controlled entity", controlled=True)
        for index, rule in enumerate(device["rules"]):
            if rule["id"] in rule_ids:
                self.rule(rule, ("rules", index))

    # ------------------------------------------------------------- adding
    def add(self, key: str, path: tuple[Any, ...], *, group: str, kind: str, value: Any, name: str,
            description: str, selector: dict[str, Any], default: Any = None) -> None:
        if value in (None, "", [], {}):
            return
        item = self.items.setdefault(key, {
            "key": key, "group": group, "kind": kind, "value": value, "name": name[:80], "description": description,
            "selector": selector, "default": value if default is None else default, "paths": []})
        item["paths"].append(path)

    def entity(self, entity_id: Any, path: tuple[Any, ...], group: str, use: str, controlled: bool = False) -> None:
        if not isinstance(entity_id, str) or "." not in entity_id:
            return
        domain, object_id = entity_id.split(".", 1)
        if entity_id in self.device["targets"]:
            key, group, use = f"controlled_{_slug(object_id)}", "Controlled entities", "Controlled entity"
        else:
            key = f"{domain}_{_slug(object_id)}"
        existing = self.items.get(key)
        if existing and existing["value"] != entity_id:  # two ids that give the same slug
            key = f"{key}_{len(self.items)}"
        self.add(key, path, group=group, kind="entity", value=entity_id, name=self.names(entity_id),
                 description=f"{use}. In the original rules: {self.names(entity_id)} ({entity_id}).",
                 selector={"entity": {"domain": domain}})

    def duration(self, key: str, path: tuple[Any, ...], seconds: Any, name: str, what: str) -> None:
        if seconds:
            self.add(key, path, group="Delays and durations", kind="duration", value=seconds, name=name,
                     description=what, selector={"duration": {}}, default=_duration(seconds))

    # ------------------------------------------------------------ walking
    def trigger(self, trigger: dict[str, Any], path: tuple[Any, ...], key: str, rule_name: str, wait: bool = False) -> None:
        group = "Sensor entities" if wait else "Trigger entities"
        use = "Entity the rule waits for" if wait else "Entity that starts the rule"
        kind = trigger["type"]
        if kind in ("state", "numeric", "occupancy", "button"):
            self.entity(trigger["entity_id"], (*path, "entity_id"), group, use)
        if kind == "time":
            if ":" in trigger["at"]:
                self.add(f"{key}_at", (*path, "at"), group="Time values", kind="time", value=trigger["at"],
                         name=f"{rule_name}: time", description="The time of day that starts the rule.",
                         selector={"time": {}})
            else:
                self.entity(trigger["at"], (*path, "at"), group, "Date/time helper that starts the rule")
        if kind == "numeric":
            for bound in ("above", "below"):
                self.add(f"{key}_{bound}", (*path, bound), group="Thresholds", kind="number", value=trigger[bound],
                         name=f"{rule_name}: {'rises above' if bound == 'above' else 'drops below'}",
                         description=f"The threshold {self.names(trigger['entity_id'])} has to cross.",
                         selector={"number": {"mode": "box", "step": "any"}})
        if kind == "state":
            for field in ("from", "to"):
                self.add(f"{key}_{field}", (*path, field), group="States", kind="text", value=trigger[field],
                         name=f"{rule_name}: changes {field}", selector={"text": {}},
                         description=f"The state {self.names(trigger['entity_id'])} changes {field}.")
        if kind in ("state", "numeric", "template"):
            self.duration(f"{key}_for", (*path, "for_seconds"), trigger["for_seconds"], f"{rule_name}: stays so for",
                          "How long the trigger has to hold before the rule starts.")
        if kind == "device":
            self.add(f"{key}_device", (*path, "config", "device_id"), group="Devices", kind="device",
                     value=trigger["config"].get("device_id"), name=f"{rule_name}: {trigger['label'] or 'device'}",
                     description=("The device whose trigger starts the rule. Choose a device of the same kind: "
                                  f"it must offer the trigger “{trigger['label'] or trigger['config'].get('type')}”."),
                     selector={"device": {}})

    def group(self, group: dict[str, Any], path: tuple[Any, ...], key: str, rule_name: str, counter: list[int]) -> None:
        for i, item in enumerate(group["items"]):
            item_path = (*path, "items", i)
            if item["type"] in GROUP_TYPES:
                self.group(item, item_path, key, rule_name, counter)
                continue
            counter[0] += 1
            leaf = f"{key}_c{counter[0]}"
            if item["type"] in ("state", "numeric"):
                self.entity(item["entity_id"], (*item_path, "entity_id"), "Sensor entities", "Entity a condition looks at")
            if item["type"] == "numeric":
                for bound in ("above", "below"):
                    self.add(f"{leaf}_{bound}", (*item_path, bound), group="Thresholds", kind="number",
                             value=item[bound], name=f"{rule_name}: is {bound}",
                             description=f"The limit for {self.names(item['entity_id'])} in a condition.",
                             selector={"number": {"mode": "box", "step": "any"}})
            if item["type"] == "state":
                self.add(f"{leaf}_state", (*item_path, "state"), group="States", kind="text", value=item["state"],
                         name=f"{rule_name}: state", selector={"text": {}},
                         description=f"The state of {self.names(item['entity_id'])} a condition asks for.")
                self.duration(f"{leaf}_for", (*item_path, "for_seconds"), item["for_seconds"],
                              f"{rule_name}: for at least", "How long the state must have lasted.")
            if item["type"] == "time":
                for field in ("after", "before"):
                    self.add(f"{leaf}_{field}", (*item_path, field), group="Time values", kind="time",
                             value=item[field], name=f"{rule_name}: {field}", selector={"time": {}},
                             description=f"The time of day {field} which the rule may run.")
            if item["type"] in ("template", "device"):
                self.fixed_template(item.get("value_template"), rule_name)

    def fixed_template(self, template: Any, rule_name: str) -> None:
        if isinstance(template, str):
            self.templates.append((rule_name, template))

    def actions(self, actions: list[dict[str, Any]], path: tuple[Any, ...], key: str, rule_name: str,
                counter: list[int]) -> None:
        for i, action in enumerate(actions):
            action_path = (*path, i)
            step = f"{key}_a{i + 1}"
            kind = action["type"]
            if kind == "service":
                if isinstance(action["target"], list):
                    for j, entity_id in enumerate(action["target"]):
                        self.entity(entity_id, (*action_path, "target", j), "Controlled entities", "Controlled entity")
                if isinstance(action["data"], str):  # generated: one template of resolved values
                    self.fixed_template(action["data"], rule_name)
                for name, value in (action["data"].items() if isinstance(action["data"], dict) else []):
                    if isinstance(value, str) and ("{{" in value or "{%" in value):
                        self.fixed_template(value, rule_name)
                        continue
                    self.add(f"{step}_{_slug(name)}", (*action_path, "data", name),
                             group=DATA_GROUP.get(name, "Action parameters"), kind="data", value=value,
                             name=f"{rule_name}: {DATA_LABEL.get(name, name.replace('_', ' '))}",
                             description=f"“{name}” of the action {action['action']}.",
                             selector=DATA_SELECTOR.get(name) or _value_selector(value))
            elif kind == "delay":
                self.duration(f"{step}_delay", (*action_path, "seconds"), action["seconds"], f"{rule_name}: wait",
                              "How long the rule waits before it continues.")
            elif kind == "notify":
                self.entity(action["entity_id"], (*action_path, "entity_id"), "Action parameters", "Where the notification goes")
                for field in ("title", "message"):
                    value = action[field]
                    if isinstance(value, str) and ("{{" in value or "{%" in value):
                        self.fixed_template(value, rule_name)
                    else:
                        self.add(f"{step}_{field}", (*action_path, field), group="Action parameters", kind="text",
                                 value=value, name=f"{rule_name}: notification {field}",
                                 description=f"The {field} of the notification.",
                                 selector={"text": {"multiline": field == "message"}})
            elif kind == "scene":
                self.entity(action["entity_id"], (*action_path, "entity_id"), "Action parameters", "Scene that is activated")
            elif kind == "script":
                if action["wait"]:
                    self.fixed.append(f"“{rule_name}” waits for the script {action['entity_id']}: a script that is "
                                      "waited for is called by its name, which cannot be an input.")
                else:
                    self.entity(action["entity_id"], (*action_path, "entity_id"), "Action parameters", "Script that is run")
            elif kind == "device":
                self.fixed.append(f"“{rule_name}” uses the device action “{action['label'] or action['config'].get('type')}”: "
                                  "it stays bound to the device it was chosen for.")
            elif kind == "repeat" and action["mode"] == "count":
                self.add(f"{step}_count", (*action_path, "count"), group="Action parameters", kind="number",
                         value=action["count"], name=f"{rule_name}: repeat count",
                         description="How many times the actions repeat.",
                         selector={"number": {"min": 1, "max": 1000, "mode": "box"}})
            if kind in ("wait_trigger", "wait_condition"):
                self.duration(f"{step}_timeout", (*action_path, "timeout_seconds"), action["timeout_seconds"],
                              f"{rule_name}: wait at most", "How long the rule waits at most.")
            if kind == "wait_trigger":
                for j, trigger in enumerate(action["triggers"]):
                    self.trigger(trigger, (*action_path, "triggers", j), f"{step}_t{j + 1}", rule_name, wait=True)
            if kind in ("if", "wait_condition") or (kind == "repeat" and action["mode"] != "count"):
                self.group(action["conditions"], (*action_path, "conditions"), step, rule_name, counter)
            if kind == "choose":
                for j, branch in enumerate(action["branches"]):
                    self.group(branch["conditions"], (*action_path, "branches", j, "conditions"), f"{step}_b{j + 1}",
                               rule_name, counter)
                    self.actions(branch["actions"], (*action_path, "branches", j, "actions"), f"{step}_b{j + 1}",
                                 rule_name, counter)
            for name, inner in sub_sequences(action):
                if not name.startswith("branches"):
                    self.actions(inner, (*action_path, name), f"{step}_{name}", rule_name, counter)

    def rule(self, rule: dict[str, Any], path: tuple[Any, ...]) -> None:
        key, name = rule["id"], rule["name"]
        for i, trigger in enumerate(rule["triggers"]):
            self.trigger(trigger, (*path, "triggers", i), f"{key}_t{i + 1}", name)
            if trigger["type"] in ("template", "event", "mqtt"):
                self.fixed_template(trigger.get("value_template"), name)
        counter = [0]
        self.group(rule["conditions"], (*path, "conditions"), key, name, counter)
        self.actions(rule["actions"], (*path, "actions"), key, name, counter)
        self.entity(rule["recheck"]["override_entity"], (*path, "recheck", "override_entity"), "Sensor entities",
                    "Manual override signal: while it is on, the occupancy rechecks correct nothing")


def restore(config: Any, variables: dict[str, Input]) -> Any:
    """The blueprint body with every input at its default: ``!input`` and the template variables resolved."""
    def text(value: str) -> str:
        for name, ref in variables.items():
            literal = compiler.quote(ref.value) if isinstance(ref.value, str) else str(ref.value)
            value = re.sub(rf"(?<![A-Za-z0-9_.'])\b{re.escape(name)}\b", lambda _m, lit=literal: lit, value)
        return value

    def walk(value: Any) -> Any:
        if isinstance(value, Input):
            return walk(value.default)
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return text(value) if isinstance(value, str) and variables else value

    return walk(config)


def _by_content(config: dict[str, Any]) -> dict[str, Any]:
    """The automation with trigger ids replaced by the trigger's content (and equal triggers listed once).

    An id is a hash of the trigger's content, so it differs between a blueprint (the content holds ``!input``)
    and the automation, and two rules whose equal triggers got separate inputs no longer share one trigger.
    Neither changes what the automation does.
    """
    ids, triggers = {}, []
    for trigger in config.get("triggers", []):
        content = compiler.canonical({k: v for k, v in trigger.items() if k != "id"})
        ids[trigger.get("id")] = content
        if content not in triggers:
            triggers.append(content)

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get("condition") == "trigger" and isinstance(value.get("id"), list):
                return {**value, "id": sorted({ids.get(i, i) for i in value["id"]})}
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return {**walk({k: v for k, v in config.items() if k != "triggers"}), "triggers": triggers}


def _automations(device: dict[str, Any], settings: dict[str, Any], presence: dict[str, Any] | None = None,
                 exposed: dict[str, Input] | None = None) -> tuple[list[dict[str, Any]], Any]:
    """The device's automations, its designers' rules included, and the designers' expansion."""
    from . import designers
    expansion = designers.expand(device, presence, exposed=exposed)
    return compiler.compile_device(designers.effective(device, expansion), settings)["automations"], expansion


def candidates(device: dict[str, Any], settings: dict[str, Any], automation_id: str | None = None,
               names: Any = None, presence: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything of one generated automation that can become a blueprint input, in a stable order."""
    names = names or (lambda entity_id: entity_id)
    automations, expansion = _automations(device, settings, presence)
    chosen = next((a for a in automations if a["id"] == automation_id), automations[0] if automations else None)
    if chosen is None:
        return {"automation": None, "automations": [], "inputs": [], "collector": None, "expansion": expansion}
    collector = _Collector(device, chosen["rule_ids"], names)
    items = dict(collector.items)
    if any(r["id"] in chosen["rule_ids"] for r in expansion.rules):
        for key, param in expansion.params.items():  # the designers' values (the shared automation holds their rules)
            if key not in items:
                items[key] = {**param, "name": names(param["value"]) if param["kind"] == "entity" else param["name"]}
    found = sorted(items.values(), key=lambda i: (GROUP_ORDER.index(i["group"]), i["key"]))
    return {"automation": chosen, "automations": automations, "inputs": found, "collector": collector, "expansion": expansion}


def _set(holder: Any, path: tuple[Any, ...], value: Any) -> None:
    for step in path[:-1]:
        holder = holder[step]
    holder[path[-1]] = value


class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


_Dumper.add_representer(Input, lambda dumper, value: dumper.represent_scalar("!input", value.key))


def to_yaml(document: dict[str, Any]) -> str:
    return yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False, width=110)


def file_path(automation_id: str) -> str:
    """Where a blueprint of this automation is installed, below Home Assistant's ``blueprints/automation``."""
    return f"{FOLDER}/{automation_id.removeprefix(compiler.AUTOMATION_PREFIX)}.yaml"


def build(device: dict[str, Any], settings: dict[str, Any], *, names: Any = None,
          presence: dict[str, Any] | None = None) -> dict[str, Any]:
    """The blueprint of one of a device's automations, from the choices stored in ``device["blueprint"]``.

    ``{"yaml", "document", "config" (the automation with every input at its default), "inputs" (all candidates,
    each with "exposed"), "problems" (errors: no export), "warnings", "automation", "automations", "path"}``
    """
    choice = device.get("blueprint") or {}
    found = candidates(device, settings, choice.get("automation"), names, presence)
    automation, problems, warnings = found["automation"], [], []
    if automation is None:
        return {"yaml": "", "document": None, "config": None, "inputs": [], "automation": None, "automations": [],
                "path": None, "warnings": [],
                "problems": ["This device has no enabled rules: there is no automation to export."]}
    chosen = choice.get("inputs") or {}
    known = {item["key"] for item in found["inputs"]}
    for key in sorted(set(chosen) - known):
        warnings.append(f"The input “{key}” no longer exists in the rules (the value it stood for was removed or "
                        "moved) and is left out.")
    work = copy.deepcopy(device)
    exposed = []
    refs: dict[str, Input] = {}
    for item in found["inputs"]:
        item["exposed"] = item["key"] in chosen
        custom = chosen.get(item["key"]) or {}
        item["shown_name"] = custom.get("name") or item["name"]
        item["shown_description"] = custom.get("description") or item["description"]
        if item["exposed"]:
            ref = Input(item["key"], item["value"], item["default"])
            refs[item["key"]] = ref
            for path in item["paths"]:
                _set(work, path, ref)
            exposed.append(item)
    automations, expansion = _automations(work, settings, presence, exposed=refs)
    compiled = next(a for a in automations if a["id"] == automation["id"])
    config = compiled["config"]
    used = {name: ref for name, ref in expansion.refs.variables.items() if name in compiler.canonical(config)}
    if used:  # inputs inside the designers' templates: carried by automation variables, like the compiler's own
        merged = {**(config.get("variables") or {}), **used}
        config = {**{k: v for k, v in config.items() if k in ("alias", "description")},
                  "variables": {name: merged[name] for name in sorted(merged)},
                  **{k: v for k, v in config.items() if k not in ("alias", "description", "variables")}}
    restored = restore({k: v for k, v in config.items() if k != "variables"}, config.get("variables") or {})
    if _by_content(restored) != _by_content(automation["config"]):
        problems.append("Internal check failed: with every input at its default the blueprint is not the "
                        "automation this device deploys. Please report this; nothing was exported.")
    body = {k: v for k, v in config.items() if k not in ("alias", "description")}
    name = choice.get("name") or device["name"]
    rules = [r["name"] for r in [*device["rules"], *found["expansion"].rules] if r["id"] in automation["rule_ids"]]
    description = choice.get("description") or (
        f"Exported from Automation Builder. Rules: {'; '.join(rules)}.")
    document = {
        "blueprint": {
            "name": name, "description": description, "domain": "automation",
            "homeassistant": {"min_version": MIN_HA_VERSION},
            "input": {item["key"]: {"name": item["shown_name"], "description": item["shown_description"],
                                    "default": item["default"], "selector": item["selector"]} for item in exposed},
        },
        **body,
    }
    # ------------------------------------------------------------ what stays fixed, said clearly
    collector = found["collector"]
    others = [a for a in found["automations"] if a["id"] != automation["id"]]
    if others:
        warnings.append(
            "A blueprint holds one automation. This device compiles into "
            f"{len(found['automations'])}: this export contains “{automation['alias']}”; export "
            + ", ".join(f"“{a['alias']}”" for a in others) + " separately (choose it above).")
    disabled = [r["name"] for r in device["rules"] if not r["enabled"]]
    if disabled:
        warnings.append("Disabled rules are not part of the automation and not of the blueprint: " + ", ".join(disabled) + ".")
    warnings += collector.fixed
    warnings += _designer_warnings(device, automation, found["expansion"], presence)
    entity_inputs = {i["value"]: i["key"] for i in exposed if i["kind"] == "entity"}
    for rule_name, template in collector.templates:
        mentioned = sorted(e for e in entity_inputs if e in template)
        if mentioned:
            warnings.append(f"A template in “{rule_name}” names {', '.join(mentioned)} directly. Templates are "
                            "kept as written, so that entity stays fixed there although it is an input elsewhere.")
    if any(t.get("trigger") == "device" for t in compiler.plain(config["triggers"])) and not any(
            i["kind"] == "device" for i in exposed):
        warnings.append("A device trigger is bound to the device it was chosen for. Make the device an input to "
                        "use the blueprint with another device of the same kind.")
    if not exposed:
        warnings.append("No input is selected: the blueprint would reproduce this exact automation.")
    return {"yaml": to_yaml(document) if not problems else "", "document": document, "config": restored,
            "inputs": [{k: v for k, v in item.items() if k != "paths"} for item in found["inputs"]],
            "automation": {k: automation[k] for k in ("id", "alias", "role", "mode", "rule_ids")},
            "automations": [{"id": a["id"], "alias": a["alias"], "role": a["role"]} for a in found["automations"]],
            "path": file_path(automation["id"]), "name": name, "description": description,
            "problems": problems, "warnings": warnings}


def _designer_warnings(device: dict[str, Any], automation: dict[str, Any], expansion: Any,
                       presence: dict[str, Any] | None) -> list[str]:
    """What of the designers stays fixed in a blueprint (values inside the templates they generate)."""
    if not any(r["id"] in automation["rule_ids"] for r in expansion.rules):
        return []
    out = []
    sources = (presence or {}).get("sources") or {}
    from . import designers
    used = designers.referenced_sources(device.get("designers"))
    zones = sorted(sources[s]["name"] for s in used if s in sources and sources[s]["kind"] == "position")
    if zones:
        out.append(f"The shapes of the position zones {', '.join(zones)} are inside templates and stay as drawn; their "
                   "coordinate entities can be inputs.")
    for d in device.get("designers") or []:
        if d.get("kind") == "cover" and any((w.get("position") or {}).get("kind") == "curve" or (w.get("tilt") or {}).get("kind") == "curve"
                                            for w in (d.get("sun") or {}).get("windows", {}).values()):
            out.append(f"“{d['name']}”: the sun curves (angles and positions) are inside templates and stay fixed.")
        if (d.get("kind") == "lighting" and (d.get("lux") or {}).get("hysteresis")) or (
                d.get("kind") == "cover" and (d.get("sun") or {}).get("hysteresis")):
            out.append(f"“{d['name']}”: thresholds that include the hysteresis stay fixed (an input cannot be added to another).")
    return out


def inputs_of(device: dict[str, Any], presence: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Kept from Phase 0.1 (the count shown on the device page): every candidate input of the device's rules."""
    collector = _Collector(device, [r["id"] for r in device["rules"]], lambda entity_id: entity_id)
    items = {item["key"]: item for item in collector.items.values()}
    if device.get("designers"):
        from . import designers
        for key, param in designers.expand(device, presence).params.items():
            items.setdefault(key, param)
    return [{"path": item["key"], "kind": item["kind"], "value": item["value"], "label": item["name"]}
            for item in items.values()]
