"""The compiler: one managed device's rules -> native Home Assistant automation configuration.

The rules of a device are consolidated into **one** automation::

    triggers:    every distinct trigger of every enabled rule, each with a stable id
    conditions:  none (each rule checks its own); only with periodic occupancy checks: skip a
                 periodic run while no occupancy rule's state is current
    actions:     one ``if`` block per rule, in the listed order:
                   if   the automation was started by one of this rule's triggers
                   and  the rule's conditions hold
                   then the rule's actions

Why ``if`` blocks and not ``choose``: ``choose`` runs only the first branch that
matches, which would silently give the first of two rules that share a trigger
priority over the other. With one ``if`` per rule every matching rule runs, and
rules that contradict each other are reported by ``conflicts.py`` instead of
being resolved silently.

Execution mode. Rules whose actions finish at once share the automation in mode
``queued``: every trigger is handled, in order, and nothing is dropped. A rule
with a delay keeps a run alive for the length of the delay, so:

* a rule that restarts, ignores or queues a new trigger while it waits needs
  that mode for the whole automation - which would cancel, drop or block the
  other rules. Such a rule gets **its own** automation, and the reason is
  reported (``separate``);
* a rule whose waits may overlap (``parallel``) stays in the shared automation,
  which then runs in mode ``parallel``.

Occupancy rules get their recheck triggers here (see ``_occupancy``): state
triggers with ``for:`` and a time pattern, never a waiting action sequence.

Compilation is deterministic: the same device and settings always give the same
configuration, byte for byte (ids come from the stable device and rule ids and
from the content of each trigger).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml

from . import catalog
from .model import action_entities, has_delay, rule_recheck

AUTOMATION_PREFIX = "automation_builder_"
QUEUE_MAX = 20
TRACES_WITH_PERIODIC = 20
SEPARATE_REASONS = {
    "restart": "it waits (delay) and starts over when it is triggered again; in the shared automation that would "
               "cancel the other rules while they run",
    "single": "it waits (delay) and ignores new triggers meanwhile; in the shared automation every other rule would "
              "be ignored during the wait",
    "queued": "it waits (delay) and handles new triggers one after the other; in the shared automation every other "
              "rule would wait behind the delay",
}
NOT_READY = ["unavailable", "unknown"]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def config_hash(config: Any) -> str:
    return hashlib.sha256(canonical(config).encode()).hexdigest()[:20]


def automation_id(device_id: str, rule_id: str | None = None) -> str:
    return f"{AUTOMATION_PREFIX}{device_id}" + (f"_{rule_id}" if rule_id else "")


def is_managed_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(AUTOMATION_PREFIX)


def _duration(seconds: int) -> dict[str, int]:
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return {"hours": hours, "minutes": minutes, "seconds": secs}


def _offset(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}:00"


# ----------------------------------------------------------------- triggers
def _trigger(trigger: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """``(kind, native trigger, extra conditions that belong to this trigger)``."""
    kind = trigger["type"]
    if kind == "state":
        native: dict[str, Any] = {"trigger": "state", "entity_id": trigger["entity_id"]}
        if trigger["from"] is not None:
            native["from"] = trigger["from"]
        if trigger["to"] is not None:
            native["to"] = trigger["to"]
        if trigger["from"] is None and trigger["to"] is None:
            native["to"] = None  # any change of the state itself, not of an attribute
        if trigger["for_seconds"]:
            native["for"] = _duration(trigger["for_seconds"])
        return kind, native, []
    if kind == "numeric":
        native = {"trigger": "numeric_state", "entity_id": trigger["entity_id"]}
        for key in ("above", "below"):
            if trigger[key] is not None:
                native[key] = trigger[key]
        if trigger["for_seconds"]:
            native["for"] = _duration(trigger["for_seconds"])
        return kind, native, []
    if kind == "time":
        extra = [{"condition": "time", "weekday": list(trigger["weekdays"])}] if trigger["weekdays"] else []
        return kind, {"trigger": "time", "at": trigger["at"]}, extra
    if kind == "sun":
        native = {"trigger": "sun", "event": trigger["event"]}
        if trigger["offset_minutes"]:
            native["offset"] = _offset(trigger["offset_minutes"])
        return kind, native, []
    if kind == "occupancy":
        on = trigger["to"] == "occupied"
        return kind, {"trigger": "state", "entity_id": trigger["entity_id"], "from": "off" if on else "on",
                      "to": "on" if on else "off"}, []
    raise ValueError(f"unsupported trigger type {kind!r}")


class _Triggers:
    """The automation's trigger list: identical triggers of different rules are listed once."""

    def __init__(self) -> None:
        self.by_content: dict[str, str] = {}
        self.items: list[dict[str, Any]] = []

    def add(self, kind: str, native: dict[str, Any]) -> str:
        key = canonical(native)
        if key not in self.by_content:
            trigger_id = f"{kind}_{hashlib.sha256(key.encode()).hexdigest()[:8]}"
            self.by_content[key] = trigger_id
            self.items.append({**native, "id": trigger_id})
        return self.by_content[key]


def _gate(alternatives: list[tuple[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """The condition "this run was started by one of these triggers" (with each trigger's own extra conditions)."""
    plain = [trigger_id for trigger_id, extra in alternatives if not extra]
    options: list[dict[str, Any]] = []
    if plain:
        options.append({"condition": "trigger", "id": plain})
    for trigger_id, extra in alternatives:
        if extra:
            options.append({"condition": "and", "conditions": [{"condition": "trigger", "id": [trigger_id]}, *extra]})
    return options[0] if len(options) == 1 else {"condition": "or", "conditions": options}


# --------------------------------------------------------------- conditions
def _condition(item: dict[str, Any]) -> dict[str, Any]:
    kind = item["type"]
    if kind in ("and", "or"):
        return {"condition": kind, "conditions": [_condition(i) for i in item["items"]]}
    if kind == "state":
        native: dict[str, Any] = {"condition": "state", "entity_id": item["entity_id"], "state": item["state"]}
        if item["for_seconds"]:
            native["for"] = _duration(item["for_seconds"])
        return {"condition": "not", "conditions": [native]} if item["negate"] else native
    if kind == "numeric":
        native = {"condition": "numeric_state", "entity_id": item["entity_id"]}
        for key in ("above", "below"):
            if item[key] is not None:
                native[key] = item[key]
        return native
    if kind == "time":
        native = {"condition": "time"}
        for key in ("after", "before"):
            if item[key] is not None:
                native[key] = item[key]
        if item["weekdays"]:
            native["weekday"] = list(item["weekdays"])
        return native
    raise ValueError(f"unsupported condition type {kind!r}")


def _conditions(group: dict[str, Any]) -> list[dict[str, Any]]:
    if not group["items"]:
        return []
    if group["type"] == "and":
        return [_condition(i) for i in group["items"]]
    return [_condition(group)]


# ------------------------------------------------------------------ actions
def _action(action: dict[str, Any], targets: list[str]) -> dict[str, Any]:
    if action["type"] == "delay":
        return {"delay": _duration(action["seconds"])}
    native: dict[str, Any] = {"action": action["action"]}
    entities = action_entities(action, targets)
    if entities:
        native["target"] = {"entity_id": entities}
    if action["data"]:
        native["data"] = dict(action["data"])
    return native


def expectations(rule: dict[str, Any], targets: list[str]) -> dict[str, Any]:
    """What a rule's actions leave behind, and whether they may be repeated.

    ``{"repeatable": bool, "why": code | None, "states": {entity_id: [expected states]}}``
    """
    states: dict[str, list[str]] = {}
    why = None
    for action in rule["actions"]:
        if action["type"] == "delay":
            why = why or "delay"
            continue
        kind = catalog.effect(action["action"], action["data"])
        if kind["kind"] in ("non_idempotent", "unknown"):
            why = why or kind["kind"]
            continue
        if kind["kind"] == "state":
            for entity_id in action_entities(action, targets):
                if entity_id in states and states[entity_id] != kind["states"]:
                    why = why or "self_contradiction"
                states[entity_id] = kind["states"]
    if why is None and not states:
        why = "unverifiable"
    return {"repeatable": why is None, "why": why, "states": states}


RECHECK_OFF = {
    "delay": "it contains a delay (a recheck would start the waiting sequence again)",
    "non_idempotent": "it contains an action that does something again every time it runs (such as toggle)",
    "unknown": "it contains a hand-entered action whose effect Automation Builder cannot check",
    "self_contradiction": "its actions leave the same entity in two different states",
    "unverifiable": "the result of its actions cannot be read from the entities' states, so a recheck could not "
                    "tell whether anything is wrong",
}


def _occupancy(rule: dict[str, Any], trigger: dict[str, Any], triggers: _Triggers, device: dict[str, Any],
               settings: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any] | None:
    """The recheck block of one occupancy trigger of a rule.

    The rechecks are *triggers* of the same automation (state triggers with ``for:`` and a time pattern), never
    a waiting action: nothing blocks the other rules, and a recheck whose moment passes while Home Assistant
    restarts is simply not fired.

    A recheck runs the rule's actions only when all of this holds *at that moment*:

    * the occupancy entity still shows the state the rule is about (so not ``unavailable`` or ``unknown``), and
      does not declare itself stale (attribute ``fresh: false``, as Smart Occupancy Detection's entities do);
    * the occupancy change happened while this automation was loaded, and not in its first minutes (the start-up
      grace). After a restart of Home Assistant every entity gets a new "last changed" time, so the order of
      changes says nothing then: a lamp somebody switched off before the restart must not come back on;
    * the manual override signal, when one is configured, is off;
    * the rule's own conditions hold;
    * a controlled entity is available and not in the state the rule's actions leave it in, **and** it has not
      changed since the occupancy change. An entity that changed after it was switched by someone or something:
      that may be a person, so it is left alone (the App reports the mismatch instead).
    """
    timing = rule_recheck(rule, settings)
    entity_id, state = trigger["entity_id"], "on" if trigger["to"] == "occupied" else "off"
    ids = [
        triggers.add("recheck", {"trigger": "state", "entity_id": entity_id, "to": state,
                                 "for": _duration(seconds)})
        for seconds in (timing["first_seconds"], timing["second_seconds"])
    ]
    if timing["periodic_minutes"]:
        ids.append(triggers.add("periodic", {"trigger": "time_pattern", "minutes": f"/{timing['periodic_minutes']}"}))
    checks: list[dict[str, Any]] = [
        {"condition": "trigger", "id": ids},
        {"condition": "state", "entity_id": entity_id, "state": state},
        {"condition": "template", "value_template": f"{{{{ state_attr('{entity_id}', 'fresh') != false }}}}"},
        {"condition": "template", "value_template": (
            f"{{{{ states['{entity_id}'].last_changed >= states[this.entity_id].last_changed + "
            f"timedelta(seconds={timing['startup_grace_seconds']}) }}}}")},
    ]
    if timing["override_entity"]:
        checks.append({"condition": "state", "entity_id": timing["override_entity"], "state": "off"})
    checks += _conditions(rule["conditions"])
    wrong = [
        {"condition": "and", "conditions": [
            {"condition": "not", "conditions": [
                {"condition": "state", "entity_id": target, "state": [*states, *NOT_READY]}]},
            {"condition": "template",
             "value_template": (f"{{{{ states['{target}'].last_changed <= "
                                f"states['{entity_id}'].last_changed }}}}")},
        ]}
        for target, states in expected["states"].items()
    ]
    checks.append(wrong[0] if len(wrong) == 1 else {"condition": "or", "conditions": wrong})
    return {"alias": f"Recheck: {rule['name']}", "if": checks,
            "then": [_action(a, device["targets"]) for a in rule["actions"]]}


# ------------------------------------------------------------------ compile
def _blocks(rule: dict[str, Any], triggers: _Triggers, device: dict[str, Any], settings: dict[str, Any],
            notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alternatives = []
    occupancy = []
    for trigger in rule["triggers"]:
        kind, native, extra = _trigger(trigger)
        alternatives.append((triggers.add(kind, native), extra))
        if kind == "occupancy":
            occupancy.append(trigger)
    blocks = [{"alias": f"Rule: {rule['name']}", "if": [_gate(alternatives), *_conditions(rule["conditions"])],
               "then": [_action(a, device["targets"]) for a in rule["actions"]]}]
    if occupancy:
        expected = expectations(rule, device["targets"])
        if not rule["recheck"]["enabled"]:
            notes.append({"level": "info", "code": "recheck_disabled", "rule_id": rule["id"],
                          "message": f"“{rule['name']}”: the automatic occupancy rechecks are turned off for this rule."})
        elif not expected["repeatable"]:
            notes.append({"level": "warning", "code": f"recheck_off_{expected['why']}", "rule_id": rule["id"],
                          "message": f"“{rule['name']}” gets no automatic occupancy rechecks, because "
                                     f"{RECHECK_OFF[expected['why']]}. It still runs when the occupancy changes."})
        else:
            for trigger in occupancy:
                blocks.append(_occupancy(rule, trigger, triggers, device, settings, expected))
    return blocks


def _automation(device: dict[str, Any], rules: list[dict[str, Any]], settings: dict[str, Any], *, mode: str,
                separate: dict[str, Any] | None, notes: list[dict[str, Any]]) -> dict[str, Any]:
    triggers = _Triggers()
    actions: list[dict[str, Any]] = []
    for rule in rules:
        actions += _blocks(rule, triggers, device, settings, notes)
    periodic = [t["id"] for t in triggers.items if t["trigger"] == "time_pattern"]
    relevant: list[dict[str, Any]] = []
    for rule in rules:  # the periodic check runs only while an occupancy rule's state is current
        for trigger in rule["triggers"]:
            state = {"condition": "state", "entity_id": trigger.get("entity_id"),
                     "state": "on" if trigger.get("to") == "occupied" else "off"}
            if trigger["type"] == "occupancy" and state not in relevant:
                relevant.append(state)
    names = "; ".join(rule["name"] for rule in rules)
    alias = (f"{device['name']}: {separate['name']} (Automation Builder)" if separate
             else f"{device['name']} (Automation Builder)")
    config: dict[str, Any] = {
        "alias": alias,
        "description": ("Managed by the Automation Builder App: edit the rules there. A change made here is "
                        f"detected and reported, and is replaced only if you confirm it. Rules: {names}."),
        "triggers": triggers.items,
        "conditions": ([{"condition": "or", "conditions": [
            {"condition": "not", "conditions": [{"condition": "trigger", "id": periodic}]}, *relevant]}]
            if periodic else []),
        "actions": actions,
        "mode": mode,
    }
    if periodic:
        config["trace"] = {"stored_traces": TRACES_WITH_PERIODIC}  # the periodic checks must not push real runs out
    if mode in ("queued", "parallel"):
        config["max"] = QUEUE_MAX
    return {"id": automation_id(device["id"], separate["id"] if separate else None), "alias": alias,
            "role": "separate" if separate else "shared", "mode": mode, "rule_ids": [r["id"] for r in rules],
            "reason": None, "config": config, "hash": config_hash(config)}


def compile_device(device: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """``{"automations": [...], "notes": [...]}`` for one device (no automation when nothing is enabled)."""
    notes: list[dict[str, Any]] = []
    rules = [r for r in device["rules"] if r["enabled"]] if device["enabled"] else []
    shared, separate = [], []
    for rule in rules:
        if has_delay(rule):
            notes.append({"level": "info", "code": "delay_restart", "rule_id": rule["id"],
                          "message": f"“{rule['name']}” waits (delay). A restart of Home Assistant during the wait "
                                     "cancels it: the actions after the delay then do not run. A trigger with "
                                     "“for …” survives better and blocks nothing."})
        (separate if has_delay(rule) and rule["retrigger"] != "parallel" else shared).append(rule)
    automations = []
    if shared:
        mode = "parallel" if any(has_delay(r) for r in shared) else "queued"
        main = _automation(device, shared, settings, mode=mode, separate=None, notes=notes)
        main["reason"] = (f"{len(shared)} rule{'s' if len(shared) != 1 else ''} in one automation"
                          + (", running in parallel because a rule waits (delay) and may overlap with itself"
                             if mode == "parallel" else ""))
        automations.append(main)
    for rule in separate:
        own = _automation(device, [rule], settings, mode=rule["retrigger"], separate=rule, notes=notes)
        own["reason"] = f"“{rule['name']}” has its own automation because {SEPARATE_REASONS[rule['retrigger']]}."
        notes.append({"level": "info", "code": "separate_automation", "rule_id": rule["id"], "message": own["reason"]})
        automations.append(own)
    return {"automations": automations, "notes": notes}


def with_id(automation: dict[str, Any]) -> dict[str, Any]:
    return {"id": automation["id"], **automation["config"]}


def to_yaml(config: dict[str, Any]) -> str:
    """The YAML a user would see in ``automations.yaml`` (one list item)."""
    return yaml.safe_dump([config], sort_keys=False, allow_unicode=True, default_flow_style=False, width=110)


def strip_id(config: Any) -> Any:
    return {k: v for k, v in config.items() if k != "id"} if isinstance(config, dict) else config
