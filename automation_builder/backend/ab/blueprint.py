"""Blueprint readiness: the reusable inputs of a device's rules.

Phase 0.1 does not write blueprints. This module keeps the path open: every
value of the rule model that a blueprint would expose as an ``!input`` is a
plain field of a trigger, condition or action dictionary, and ``inputs_of``
lists them with a stable path and a selector type. A later phase turns a device
into a blueprint by replacing each listed value with ``!input <name>`` in the
compiler's output (the compiler reads these fields and nothing else), and the
entity inputs of the controlled targets become the blueprint's target selector.
"""

from __future__ import annotations

from typing import Any

from .model import condition_leaves


def inputs_of(device: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{path, kind, value, label}]``: kind is entity, number, duration, time, state or action_data."""
    out: list[dict[str, Any]] = []

    def add(path: str, kind: str, value: Any, label: str) -> None:
        if value not in (None, "", [], {}):
            out.append({"path": path, "kind": kind, "value": value, "label": label})

    for i, target in enumerate(device["targets"]):
        add(f"targets[{i}]", "entity", target, "Controlled entity")
    for rule in device["rules"]:
        base = f"rules.{rule['id']}"
        for i, trigger in enumerate(rule["triggers"]):
            path = f"{base}.triggers[{i}]"
            add(f"{path}.entity_id", "entity", trigger.get("entity_id"), f"{rule['name']}: trigger entity")
            for key in ("above", "below"):
                add(f"{path}.{key}", "number", trigger.get(key), f"{rule['name']}: threshold ({key})")
            add(f"{path}.for_seconds", "duration", trigger.get("for_seconds"), f"{rule['name']}: trigger duration")
            add(f"{path}.at", "time", trigger.get("at"), f"{rule['name']}: time")
            for key in ("to", "from"):
                if trigger["type"] == "state":
                    add(f"{path}.{key}", "state", trigger.get(key), f"{rule['name']}: state ({key})")
        for i, leaf in enumerate(condition_leaves(rule["conditions"])):
            path = f"{base}.conditions[{i}]"
            add(f"{path}.entity_id", "entity", leaf.get("entity_id"), f"{rule['name']}: condition entity")
            for key in ("above", "below"):
                add(f"{path}.{key}", "number", leaf.get(key), f"{rule['name']}: condition threshold ({key})")
            add(f"{path}.state", "state", leaf.get("state"), f"{rule['name']}: condition state")
        for i, action in enumerate(rule["actions"]):
            path = f"{base}.actions[{i}]"
            if action["type"] == "delay":
                add(f"{path}.seconds", "duration", action["seconds"], f"{rule['name']}: delay")
            else:
                for key, value in action["data"].items():
                    add(f"{path}.data.{key}", "action_data", value, f"{rule['name']}: {action['action']} {key}")
        recheck = rule["recheck"]
        for key in ("first_seconds", "second_seconds"):
            add(f"{base}.recheck.{key}", "duration", recheck[key], f"{rule['name']}: recheck delay")
        add(f"{base}.recheck.override_entity", "entity", recheck["override_entity"],
            f"{rule['name']}: manual override signal")
    return out
