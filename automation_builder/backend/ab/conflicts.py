"""Contradictions between the rules of one device.

Home Assistant runs every rule whose trigger and conditions match, in the listed
order. When two rules can run at the same moment and act on the same entity,
the result depends on that order. Automation Builder never decides this
silently: such a pair is a **blocking** conflict, and the device cannot be
deployed until the user either changes the rules (for example gives them
conditions that exclude each other) or explicitly confirms the listed order
(``acknowledged``). A confirmation is tied to the two rules and their order.
"""

from __future__ import annotations

from typing import Any

from . import catalog
from .compiler import _trigger, canonical
from .model import action_entities, sub_sequences, waits, walk_actions



def _together(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Can these two triggers fire at the same moment?"""
    if canonical(_trigger(a)[1]) == canonical(_trigger(b)[1]):
        if a["type"] == "time" and a["weekdays"] and b["weekdays"]:
            return bool(set(a["weekdays"]) & set(b["weekdays"]))
        return True
    stateish = ("state", "occupancy")
    if a["type"] in stateish and b["type"] in stateish and a["entity_id"] == b["entity_id"]:
        na, nb = _trigger(a)[1], _trigger(b)[1]
        if na.get("for") != nb.get("for"):
            return False
        for key in ("to", "from"):
            va, vb = na.get(key), nb.get(key)
            if va is not None and vb is not None and va != vb:
                return False
        return True
    return False


def _shared_trigger(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    for ta in a["triggers"]:
        for tb in b["triggers"]:
            if _together(ta, tb):
                return ta
    return None


def _complement(items: list[dict[str, Any]], others: list[dict[str, Any]]) -> bool:
    """Does one rule require "none of X" (or "not all of Y") where the other requires X (all of Y)?"""
    for item in items:
        if item["type"] != "not":
            continue
        if any(inner in others for inner in item["items"]):
            return True
        inner = item["items"]
        if len(inner) == 1 and inner[0]["type"] == "and" and inner[0]["items"] and all(i in others for i in inner[0]["items"]):
            return True
    return False


def _generated_exclusive(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Two rules of one designer that stand for different values of one thing (two moods of one selector, two
    light-level bands of one sensor): they exclude each other by construction."""
    ka, kb = (a.get("generated") or {}).get("exclusive") or {}, (b.get("generated") or {}).get("exclusive") or {}
    return any(key in kb and kb[key] != value for key, value in ka.items())


def _exclusive(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Do the two rules' conditions provably exclude each other? Only the simple, certain cases count."""
    if _generated_exclusive(a, b):
        return True
    if a["conditions"]["type"] != "and" or b["conditions"]["type"] != "and":
        return False
    if _complement(a["conditions"]["items"], b["conditions"]["items"]) or _complement(b["conditions"]["items"], a["conditions"]["items"]):
        return True
    for ca in a["conditions"]["items"]:
        for cb in b["conditions"]["items"]:
            if ca["type"] != cb["type"] or ca.get("entity_id") != cb.get("entity_id"):
                continue
            if ca["type"] == "state":
                if not ca["negate"] and not cb["negate"] and ca["state"] != cb["state"]:
                    return True
                if ca["negate"] != cb["negate"] and ca["state"] == cb["state"]:
                    return True
            if ca["type"] == "numeric":
                for low, high in ((ca, cb), (cb, ca)):
                    if low["below"] is not None and high["above"] is not None and high["above"] >= low["below"]:
                        return True
    return False


WAITS = ("delay", "wait_trigger", "wait_condition")


def _effects(rule: dict[str, Any], targets: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Per controlled entity: what the rule does to it before any wait (the part that runs at the trigger).

    An action inside a branch or a repeat only *may* run: it is marked ``conditional``.
    """
    out: dict[str, list[dict[str, Any]]] = {}

    def visit(actions: list[dict[str, Any]], conditional: bool) -> bool:
        for action in actions:
            if action["type"] in WAITS or (action["type"] == "script" and action["wait"]):
                return False
            if action["type"] == "service":
                kind = catalog.effect(action["action"], action["data"])
                for entity_id in action_entities(action, targets):
                    out.setdefault(entity_id, []).append({"action": action["action"], "data": action["data"],
                                                          "conditional": conditional, **kind})
            for _key, inner in sub_sequences(action):
                visit(inner, True)
        return True

    visit(rule["actions"], False)
    return out


def _late(rule: dict[str, Any], targets: list[str]) -> set[str]:
    """The controlled entities a waiting rule acts on *after* a wait."""
    found: set[str] = set()
    waited = False
    for action in walk_actions(rule["actions"]):
        if action["type"] in WAITS or (action["type"] == "script" and action["wait"]) \
                or (action["type"] == "repeat" and action["mode"] != "count"):
            waited = True
        elif waited and action["type"] == "service":
            found.update(action_entities(action, targets))
    return found


def _pair(a: dict[str, Any], b: dict[str, Any], entity_id: str, ea: dict[str, Any], eb: dict[str, Any],
          name: str) -> tuple[str, str, str] | None:
    """``(code, level, message)`` for the last thing each rule does to one entity."""
    both = f"“{a['name']}” and “{b['name']}” can run at the same moment"
    order = (f"Home Assistant runs them in the listed order, so “{b['name']}” has the last word. Give them "
             "conditions that exclude each other, or confirm that this order is what you want.")
    if ea["conditional"] or eb["conditional"]:
        different = (ea["action"], ea["data"]) != (eb["action"], eb["data"])
        opposite = ea["kind"] == "state" and eb["kind"] == "state" and not set(ea["states"]) & set(eb["states"])
        if opposite or "non_idempotent" in (ea["kind"], eb["kind"]) or (ea["action"] == eb["action"] and different):
            return ("possible_overlap", "warning",
                    f"{both}, and both can act on {name} ({ea['action']} and {eb['action']}), depending on a branch "
                    f"inside a rule. When both do, “{b['name']}” has the last word.")
        return None
    if ea["kind"] == "non_idempotent" or eb["kind"] == "non_idempotent":
        if ea["action"] == eb["action"]:
            return ("double_toggle", "blocking",
                    f"{both} and both run {ea['action']} on {name}: the second undoes or repeats the first. {order}")
        return ("toggle_overlap", "blocking",
                f"{both}; one runs {ea['action']} and the other {eb['action']} on {name}, so the result depends "
                f"on the order. {order}")
    if ea["kind"] == "state" and eb["kind"] == "state" and not set(ea["states"]) & set(eb["states"]):
        return ("contradiction", "blocking",
                f"{both} and contradict each other on {name}: one leaves it “{ea['states'][0]}”, the other "
                f"“{eb['states'][0]}”. {order}")
    if ea["action"] == eb["action"]:
        if ea["data"] == eb["data"]:
            return ("repeated_action", "warning",
                    f"{both} and both run {ea['action']} on {name}: the same command is sent twice.")
        return ("competing", "blocking",
                f"{both} and run {ea['action']} on {name} with different settings. {order}")
    return None


def analyse(device: dict[str, Any], describe_entity: Any = None) -> list[dict[str, Any]]:
    """Every conflict between the enabled rules of a device, in a stable order."""
    name_of = describe_entity or (lambda entity_id: entity_id)
    rules = [r for r in device["rules"] if r["enabled"]] if device["enabled"] else []
    acknowledged = set(device.get("acknowledged") or [])
    found: list[dict[str, Any]] = []

    def add(code: str, level: str, message: str, rule_ids: list[str], entity_id: str | None) -> None:
        key = ":".join([code, *rule_ids, entity_id or ""])
        if any(c["key"] == key for c in found):
            return
        found.append({"key": key, "code": code, "level": level, "message": message, "rule_ids": rule_ids,
                      "entity_id": entity_id, "acknowledged": level == "blocking" and key in acknowledged})

    for rule in rules:
        if waits(rule):
            late = _late(rule, device["targets"])
            for other in rules:
                shared = sorted(late & {e for a in walk_actions(other["actions"])
                                        for e in action_entities(a, device["targets"])}) if other is not rule else []
                if shared:
                    add("late_action", "warning",
                        f"“{rule['name']}” acts on {', '.join(name_of(e) for e in shared)} after a wait. If "
                        f"“{other['name']}” runs meanwhile, the later action still follows and has the last word.",
                        sorted([rule["id"], other["id"]]), shared[0])
            continue
        for entity_id, effects in _effects(rule, device["targets"]).items():
            states = [e["states"] for e in effects if e["kind"] == "state" and not e["conditional"]]
            if any(not set(x) & set(y) for x in states for y in states):
                add("self_contradiction", "warning",
                    f"“{rule['name']}” leaves {name_of(entity_id)} in two different states one right after the "
                    "other; only the last one stays.", [rule["id"]], entity_id)
    for i, a in enumerate(rules):
        for b in rules[i + 1:]:
            if _shared_trigger(a, b) is None or _exclusive(a, b):
                continue
            same = (canonical([a["triggers"], a["conditions"], a["actions"]])
                    == canonical([b["triggers"], b["conditions"], b["actions"]]))
            if same:
                add("duplicate_rule", "blocking",
                    f"“{b['name']}” is an exact copy of “{a['name']}”: every action would run twice. Remove or "
                    "disable one of them, or confirm that both should run.", [a["id"], b["id"]], None)
                continue
            ea, eb = _effects(a, device["targets"]), _effects(b, device["targets"])
            for entity_id in [e for e in ea if e in eb]:
                result = _pair(a, b, entity_id, ea[entity_id][-1], eb[entity_id][-1], name_of(entity_id))
                if result:
                    add(result[0], result[1], result[2], [a["id"], b["id"]], entity_id)
    return found


def blocking(conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in conflicts if c["level"] == "blocking" and not c["acknowledged"]]
