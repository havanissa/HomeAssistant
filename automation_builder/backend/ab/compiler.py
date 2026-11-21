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
``queued``: every trigger is handled, in order, and nothing is dropped. That
mode never changes. A rule that *waits* (a delay, a wait for a trigger or a
condition, an open-ended repeat, a script it waits for) keeps a run alive, and
whatever the automation's mode, that would cancel, drop, delay or interleave
the other rules. Such a rule therefore gets **its own** automation, in the mode
the user chose for it (start over, ignore, queue, run in parallel), and the
reason is reported (``separate``). Nothing is sacrificed to keep the number of
automations low, and no mode is ever chosen silently.

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
from .model import action_entities, condition_leaves, rule_recheck, simple_template, waits

AUTOMATION_PREFIX = "automation_builder_"
QUEUE_MAX = 20
TRACES_WITH_PERIODIC = 20
SEPARATE_REASONS = {
    "restart": "it waits and starts over when it is triggered again; in the shared automation that would cancel "
               "the other rules while they run",
    "single": "it waits and ignores new triggers meanwhile; in the shared automation every other rule would be "
              "ignored during the wait",
    "queued": "it waits and handles new triggers one after the other; in the shared automation every other rule "
              "would wait behind it",
    "parallel": "it waits and may run several times at once; the shared automation handles one trigger after the "
                "other, and running it in parallel would let the other rules' runs overlap too",
}
NOT_READY = ["unavailable", "unknown"]


class Input:
    """A value that a blueprint takes from an input (``!input key``) instead of the rule.

    ``blueprint.py`` puts these in place of the chosen values before compiling; the compiler passes them through
    and decides everything structural (rechecks, separation) from ``value``, the value the rule has now. So a
    blueprint has exactly the shape of the automation it was exported from.
    """

    __slots__ = ("key", "value", "default")

    def __init__(self, key: str, value: Any, default: Any = None) -> None:
        self.key = key
        self.value = value                                     # as the rule model holds it (seconds, an entity id)
        self.default = value if default is None else default   # as the automation holds it (a duration mapping)

    def __repr__(self) -> str:
        return f"!input {self.key}"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Input) and other.key == self.key

    def __hash__(self) -> int:
        return hash(("input", self.key))

    def __bool__(self) -> bool:
        return self.value not in (None, "", 0)

    @property
    def variable(self) -> str:
        """The automation variable that carries this input into templates."""
        return f"ab_{self.key}"


def plain(value: Any) -> Any:
    """The value with every blueprint input replaced by what the rule has now."""
    if isinstance(value, Input):
        return plain(value.value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=lambda o: {"!input": o.key})


def config_hash(config: Any) -> str:
    return hashlib.sha256(canonical(config).encode()).hexdigest()[:20]


def automation_id(device_id: str, rule_id: str | None = None) -> str:
    return f"{AUTOMATION_PREFIX}{device_id}" + (f"_{rule_id}" if rule_id else "")


def is_managed_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(AUTOMATION_PREFIX)


def _duration(seconds: Any) -> Any:
    if isinstance(seconds, Input):
        return seconds
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return {"hours": hours, "minutes": minutes, "seconds": secs}


def _offset(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}:00"


class _Ctx:
    """What compiling one rule needs: the device's targets, the ids of the rule's triggers, template variables."""

    def __init__(self, targets: list[Any], variables: dict[str, Input] | None = None) -> None:
        self.targets = targets
        self.alternatives: list[tuple[str, list[dict[str, Any]]]] = []  # per trigger of the rule: (id, extras)
        self.variables = variables if variables is not None else {}

    def ref(self, entity_id: Any) -> str:
        """An entity inside a template: the quoted id, or the variable that carries a blueprint input."""
        if isinstance(entity_id, Input):
            self.variables[entity_id.variable] = entity_id
            return entity_id.variable
        return f"'{entity_id}'"

    def literal(self, value: Any, text: bool) -> str:
        """A value inside a template: written out, or the variable of a blueprint input."""
        if isinstance(value, Input):
            self.variables[value.variable] = value
            return value.variable
        return quote(value) if text else str(value)


def quote(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


# ----------------------------------------------------------------- triggers
def _trigger(trigger: dict[str, Any]) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    """``(kind, native trigger, extra conditions that belong to this trigger)``."""
    kind = trigger["type"]
    if kind == "state":
        native: dict[str, Any] = {"trigger": "state", "entity_id": trigger["entity_id"]}
        if trigger.get("attribute"):
            native["attribute"] = trigger["attribute"]
        if trigger["from"] is not None:
            native["from"] = trigger["from"]
        if trigger["to"] is not None:
            native["to"] = trigger["to"]
        if trigger["from"] is None and trigger["to"] is None and not trigger.get("attribute"):
            native["to"] = None  # any change of the state itself, not of an attribute
        if trigger["for_seconds"]:
            native["for"] = _duration(trigger["for_seconds"])
        return kind, native, []
    if kind == "numeric":
        native = {"trigger": "numeric_state", "entity_id": trigger["entity_id"]}
        if trigger.get("attribute"):
            native["attribute"] = trigger["attribute"]
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
    if kind == "button":
        # An event entity's state is the time of its last event. Coming back from "unavailable" (a restart, a
        # reconnect) is a state change too and must not count as a press; the first event ever ("unknown" before
        # it) is a real one.
        native = {"trigger": "state", "entity_id": trigger["entity_id"], "not_from": ["unavailable"],
                  "not_to": list(NOT_READY)}
        extra = []
        if trigger["event_types"]:
            kinds = ", ".join(quote(k) for k in trigger["event_types"])
            extra = [{"condition": "template",
                      "value_template": f"{{{{ trigger.to_state.attributes.event_type in [{kinds}] }}}}"}]
        return kind, native, extra
    if kind == "device":
        return kind, {"trigger": "device", **trigger["config"]}, []
    if kind == "event":
        native = {"trigger": "event", "event_type": trigger["event_type"]}
        if trigger["event_data"]:
            native["event_data"] = dict(trigger["event_data"])
        return kind, native, []
    if kind == "mqtt":
        native = {"trigger": "mqtt", "topic": trigger["topic"]}
        if trigger["payload"] is not None:
            native["payload"] = trigger["payload"]
        return kind, native, []
    if kind == "template":
        native = {"trigger": "template", "value_template": trigger["value_template"]}
        if trigger["for_seconds"]:
            native["for"] = _duration(trigger["for_seconds"])
        return kind, native, []
    if kind == "pattern":
        return kind, {"trigger": "time_pattern", **{k: trigger[k] for k in ("hours", "minutes", "seconds")
                                                    if trigger[k] is not None}}, []
    if kind == "homeassistant":
        return kind, {"trigger": "homeassistant", "event": trigger["event"]}, []
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
    plain_ids = [trigger_id for trigger_id, extra in alternatives if not extra]
    options: list[dict[str, Any]] = []
    if plain_ids:
        options.append({"condition": "trigger", "id": plain_ids})
    for trigger_id, extra in alternatives:
        if extra:
            options.append({"condition": "and", "conditions": [{"condition": "trigger", "id": [trigger_id]}, *extra]})
    return options[0] if len(options) == 1 else {"condition": "or", "conditions": options}


# --------------------------------------------------------------- conditions
def _condition(item: dict[str, Any], ctx: _Ctx | None = None) -> dict[str, Any]:
    kind = item["type"]
    if kind in ("and", "or", "not"):
        return {"condition": kind, "conditions": [_condition(i, ctx) for i in item["items"]]}
    if kind == "state":
        native: dict[str, Any] = {"condition": "state", "entity_id": item["entity_id"]}
        if item.get("attribute"):
            native["attribute"] = item["attribute"]
        native["state"] = item["state"]
        if item["for_seconds"]:
            native["for"] = _duration(item["for_seconds"])
        return {"condition": "not", "conditions": [native]} if item["negate"] else native
    if kind == "numeric":
        native = {"condition": "numeric_state", "entity_id": item["entity_id"]}
        if item.get("attribute"):
            native["attribute"] = item["attribute"]
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
    if kind == "template":
        return {"condition": "template", "value_template": item["value_template"]}
    if kind == "trigger":
        # Two triggers of a rule can be one native trigger (two kinds of press of one button): the check is the
        # trigger's id *and* what tells them apart, exactly as in the rule's own trigger check.
        known = ctx.alternatives if ctx else []
        return _gate([known[i] for i in item["triggers"] if i < len(known)])
    if kind == "device":
        return {"condition": "device", **item["config"]}
    if kind == "sun":
        native = {"condition": "sun"}
        for key in ("after", "before"):
            if item[key] is not None:
                native[key] = item[key]
                if item[f"{key}_offset_minutes"]:
                    native[f"{key}_offset"] = _offset(item[f"{key}_offset_minutes"])
        return native
    raise ValueError(f"unsupported condition type {kind!r}")


def _conditions(group: dict[str, Any], ctx: _Ctx | None = None) -> list[dict[str, Any]]:
    if not group["items"]:
        return []
    if group["type"] == "and":
        return [_condition(i, ctx) for i in group["items"]]
    return [_condition(group, ctx)]


def _jinja(item: dict[str, Any], ctx: _Ctx) -> str:
    """A condition as a template expression (for ``wait_template``; Home Assistant has no "wait for condition")."""
    kind = item["type"]
    if kind in ("and", "or"):
        return "(" + f" {kind} ".join(_jinja(i, ctx) for i in item["items"]) + ")"
    if kind == "not":
        return "not (" + " or ".join(_jinja(i, ctx) for i in item["items"]) + ")"
    ref = ctx.ref(item["entity_id"]) if "entity_id" in item else ""
    if kind == "state":
        value = (f"(state_attr({ref}, {quote(item['attribute'])}) | string)" if item.get("attribute")
                 else f"states({ref})")
        return f"{value} {'!=' if item['negate'] else '=='} {ctx.literal(item['state'], True)}"
    if kind == "numeric":
        value = (f"(state_attr({ref}, {quote(item['attribute'])}) | float(none))" if item.get("attribute")
                 else f"(states({ref}) | float(none))")
        parts = [f"{value} is not none"]
        if item["above"] is not None:
            parts.append(f"{value} > {ctx.literal(item['above'], False)}")
        if item["below"] is not None:
            parts.append(f"{value} < {ctx.literal(item['below'], False)}")
        return "(" + " and ".join(parts) + ")"
    if kind == "template":
        return f"({simple_template(item['value_template'])})"
    raise ValueError(f"a wait cannot watch a {kind!r} condition")


def wait_template(group: dict[str, Any], ctx: _Ctx) -> str:
    items = group["items"]
    if group["type"] != "not" and len(items) == 1 and items[0]["type"] == "template":
        return items[0]["value_template"]  # the user's own template, unchanged
    return "{{ " + _jinja(group, ctx) + " }}"


# ------------------------------------------------------------------ actions
def _action(action: dict[str, Any], ctx: _Ctx) -> dict[str, Any]:
    kind = action["type"]
    if kind == "delay":
        return {"delay": _duration(action["seconds"])}
    if kind == "service":
        native: dict[str, Any] = {"action": action["action"]}
        entities = action_entities(action, ctx.targets)
        if entities:
            native["target"] = {"entity_id": entities}
        if action["data"]:
            native["data"] = dict(action["data"])
        return native
    if kind == "notify":
        native = {"action": action["action"]}
        if action["entity_id"]:
            native["target"] = {"entity_id": [action["entity_id"]]}
        native["data"] = {"message": action["message"]}
        if action["title"] is not None:
            native["data"]["title"] = action["title"]
        return native
    if kind == "script":
        entity_id = plain(action["entity_id"])
        if action["wait"]:  # calling the script as an action waits until it has finished
            native = {"action": f"script.{entity_id.split('.', 1)[1]}"}
            if action["variables"]:
                native["data"] = dict(action["variables"])
            return native
        native = {"action": "script.turn_on", "target": {"entity_id": [action["entity_id"]]}}
        if action["variables"]:
            native["data"] = {"variables": dict(action["variables"])}
        return native
    if kind == "scene":
        return {"action": "scene.turn_on", "target": {"entity_id": [action["entity_id"]]}}
    if kind == "device":
        return dict(action["config"])
    if kind == "variables":
        return {"variables": dict(action["variables"])}
    if kind == "if":
        native = {"if": _conditions(action["conditions"], ctx), "then": _sequence(action["then"], ctx)}
        if action["else"]:
            native["else"] = _sequence(action["else"], ctx)
        return native
    if kind == "choose":
        native = {"choose": [{"conditions": _conditions(b["conditions"], ctx), "sequence": _sequence(b["actions"], ctx)}
                             for b in action["branches"]]}
        if action["default"]:
            native["default"] = _sequence(action["default"], ctx)
        return native
    if kind == "wait_trigger":
        native = {"wait_for_trigger": [_trigger(t)[1] for t in action["triggers"]]}
    elif kind == "wait_condition":
        native = {"wait_template": wait_template(action["conditions"], ctx)}
    elif kind == "repeat":
        repeat: dict[str, Any] = {}
        if action["mode"] == "count":
            repeat["count"] = action["count"]
        else:
            repeat[action["mode"]] = _conditions(action["conditions"], ctx)
        repeat["sequence"] = _sequence(action["actions"], ctx)
        return {"repeat": repeat}
    else:
        raise ValueError(f"unsupported action type {kind!r}")
    if action["timeout_seconds"]:
        native["timeout"] = _duration(action["timeout_seconds"])
        native["continue_on_timeout"] = True
    return native


WAIT_COMPLETED = {"condition": "template", "value_template": "{{ wait.completed }}"}


def _sequence(actions: list[dict[str, Any]], ctx: _Ctx) -> list[dict[str, Any]]:
    """A list of actions. A wait that stops at its timeout: what follows it runs only when the wait completed.

    Home Assistant's own ``continue_on_timeout: false`` aborts the *whole* automation run; in the shared
    automation that would also skip the rules listed after this one. So the timeout always continues, and the
    rest of the list is wrapped in ``if wait.completed``: the same result for the rule, no effect on the others.
    """
    out: list[dict[str, Any]] = []
    for i, action in enumerate(actions):
        out.append(_action(action, ctx))
        if action["type"] in ("wait_trigger", "wait_condition") and action["timeout_seconds"] \
                and action["on_timeout"] == "stop":
            rest = _sequence(actions[i + 1:], ctx)
            if rest:
                out.append({"if": [dict(WAIT_COMPLETED)], "then": rest})
            break
    return out


def expectations(rule: dict[str, Any], targets: list[Any]) -> dict[str, Any]:
    """What a rule's actions leave behind, and whether they may be repeated.

    ``{"repeatable": bool, "why": code | None, "states": {entity_id: [expected states]}}``
    """
    states: dict[Any, list[Any]] = {}
    why = None
    if any(leaf["type"] == "trigger" for leaf in condition_leaves(rule["conditions"])):
        why = "trigger_condition"
    for action in rule["actions"]:
        if action["type"] == "delay":
            why = why or "delay"
            continue
        if action["type"] != "service":
            why = why or "advanced"
            continue
        kind = catalog.effect(action["action"], plain(action["data"]))
        if kind["kind"] in ("non_idempotent", "unknown"):
            why = why or kind["kind"]
            continue
        if kind["kind"] == "state":
            expected = kind["states"]
            given = [v for v in action["data"].values() if isinstance(v, Input) and [plain(v)] == expected]
            if given:  # the state follows a blueprint input (a selected option, a mode)
                expected = [given[0]]
            for entity_id in action_entities(action, targets):
                if entity_id in states and plain(states[entity_id]) != plain(expected):
                    why = why or "self_contradiction"
                states[entity_id] = expected
    if why is None and not states:
        why = "unverifiable"
    return {"repeatable": why is None, "why": why, "states": states}


RECHECK_OFF = {
    "delay": "it contains a delay (a recheck would start the waiting sequence again)",
    "non_idempotent": "it contains an action that does something again every time it runs (such as toggle)",
    "unknown": "it contains a hand-entered action whose effect Automation Builder cannot check",
    "self_contradiction": "its actions leave the same entity in two different states",
    "advanced": "it contains branches, waits or other advanced actions (a recheck would run the whole sequence "
                "again)",
    "trigger_condition": "its conditions ask which trigger started it (a recheck is started by none of them)",
    "unverifiable": "the result of its actions cannot be read from the entities' states, so a recheck could not "
                    "tell whether anything is wrong",
}


def _occupancy(rule: dict[str, Any], trigger: dict[str, Any], triggers: _Triggers, ctx: _Ctx,
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
        {"condition": "template", "value_template": f"{{{{ state_attr({ctx.ref(entity_id)}, 'fresh') != false }}}}"},
        {"condition": "template", "value_template": (
            f"{{{{ states[{ctx.ref(entity_id)}].last_changed >= states[this.entity_id].last_changed + "
            f"timedelta(seconds={timing['startup_grace_seconds']}) }}}}")},
    ]
    if timing["override_entity"]:
        checks.append({"condition": "state", "entity_id": timing["override_entity"], "state": "off"})
    checks += _conditions(rule["conditions"], ctx)
    wrong = [
        {"condition": "and", "conditions": [
            {"condition": "not", "conditions": [
                {"condition": "state", "entity_id": target, "state": [*states, *NOT_READY]}]},
            {"condition": "template",
             "value_template": (f"{{{{ states[{ctx.ref(target)}].last_changed <= "
                                f"states[{ctx.ref(entity_id)}].last_changed }}}}")},
        ]}
        for target, states in expected["states"].items()
    ]
    checks.append(wrong[0] if len(wrong) == 1 else {"condition": "or", "conditions": wrong})
    return {"alias": f"Recheck: {rule['name']}", "if": checks, "then": _sequence(rule["actions"], ctx)}


# ------------------------------------------------------------------ compile
def _blocks(rule: dict[str, Any], triggers: _Triggers, device: dict[str, Any], settings: dict[str, Any],
            notes: list[dict[str, Any]], variables: dict[str, Input]) -> list[tuple[str, dict[str, Any]]]:
    """``[(kind, block)]`` of one rule: its ``rule`` block and one ``recheck`` block per occupancy trigger."""
    ctx = _Ctx(device["targets"], variables)
    alternatives = []
    occupancy = []
    for trigger in rule["triggers"]:
        kind, native, extra = _trigger(trigger)
        trigger_id = triggers.add(kind, native)
        alternatives.append((trigger_id, extra))
        if kind == "occupancy":
            occupancy.append(trigger)
    ctx.alternatives = alternatives
    blocks = [("rule", {"alias": f"Rule: {rule['name']}",
                        "if": [_gate(alternatives), *_conditions(rule["conditions"], ctx)],
                        "then": _sequence(rule["actions"], ctx)})]
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
                blocks.append(("recheck", _occupancy(rule, trigger, triggers, ctx, settings, expected)))
    return blocks


def _automation(device: dict[str, Any], rules: list[dict[str, Any]], settings: dict[str, Any], *, mode: str,
                separate: dict[str, Any] | None, notes: list[dict[str, Any]]) -> dict[str, Any]:
    triggers = _Triggers()
    actions: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    variables: dict[str, Input] = {}
    for rule in rules:
        for kind, block in _blocks(rule, triggers, device, settings, notes, variables):
            blocks.append({"rule_id": rule["id"], "kind": kind, "index": len(actions)})
            actions.append(block)
    periodic = [t["id"] for t in triggers.items if t["id"].startswith("periodic_")]
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
    if variables:  # only in a blueprint: the inputs that templates refer to
        config = {**{k: v for k, v in config.items() if k in ("alias", "description")},
                  "variables": {name: variables[name] for name in sorted(variables)},
                  **{k: v for k, v in config.items() if k not in ("alias", "description")}}
    if periodic:
        config["trace"] = {"stored_traces": TRACES_WITH_PERIODIC}  # the periodic checks must not push real runs out
    if mode in ("queued", "parallel"):
        config["max"] = QUEUE_MAX
    return {"id": automation_id(device["id"], separate["id"] if separate else None), "alias": alias,
            "role": "separate" if separate else "shared", "mode": mode, "rule_ids": [r["id"] for r in rules],
            "reason": None, "config": config, "hash": config_hash(config), "blocks": blocks}


def compile_device(device: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """``{"automations": [...], "notes": [...]}`` for one device (no automation when nothing is enabled)."""
    notes: list[dict[str, Any]] = []
    rules = [r for r in device["rules"] if r["enabled"]] if device["enabled"] else []
    shared, separate = [], []
    for rule in rules:
        if waits(rule):
            notes.append({"level": "info", "code": "delay_restart", "rule_id": rule["id"],
                          "message": f"“{rule['name']}” waits. A restart of Home Assistant during the wait "
                                     "cancels it: the actions after the wait then do not run. A trigger with "
                                     "“for …” survives better and blocks nothing."})
        (separate if waits(rule) else shared).append(rule)
    automations = []
    if shared:
        main = _automation(device, shared, settings, mode="queued", separate=None, notes=notes)
        main["reason"] = f"{len(shared)} rule{'s' if len(shared) != 1 else ''} in one automation"
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
