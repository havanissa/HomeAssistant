"""Simulation: what would the device's automation do in a given situation? Nothing is sent anywhere.

The simulation works on exactly the rules the compiler turns into the native automation (the device's own rules
and the rules its designers generate), with Home Assistant's meaning of every trigger and condition:

* a run is started by one chosen trigger of the automation;
* every rule whose triggers include it and whose conditions hold runs, in the order of the automation;
* conditions are three-valued: true, false, or *unknown* (a template written by hand, a sun condition, a device
  condition: the simulation cannot evaluate them and says so instead of guessing);
* the templates the designers generate (position zones, sun curves, manual-change pauses) are evaluated by their
  Python twins, which the runtime tests compare with Home Assistant's own rendering.

The states of the scenario come from Home Assistant ("live") or were typed in ("test"); the result names which.
It is a prediction from the rules, not a test of the deployed automation.
"""

from __future__ import annotations

from typing import Any

from . import catalog, compiler, describe, model

WEEKDAYS = model.WEEKDAYS


def trigger_key(trigger: dict[str, Any]) -> str:
    kind, native, _extra = compiler._trigger(trigger)  # noqa: SLF001
    return compiler.canonical(native)


def triggers_of(device: dict[str, Any], names: Any) -> list[dict[str, Any]]:
    """Every distinct trigger of the enabled rules, described (the choices of "what just happened")."""
    out: dict[str, dict[str, Any]] = {}
    for rule in device["rules"]:
        if not rule["enabled"]:
            continue
        for trigger in rule["triggers"]:
            key = trigger_key(trigger)
            entry = out.setdefault(key, {"key": key, "text": describe.trigger(trigger, names), "entity_id": trigger.get("entity_id"),
                                         "type": trigger["type"], "rules": 0})
            entry["rules"] += 1
    return sorted(out.values(), key=lambda t: t["text"].casefold())


# ------------------------------------------------------------------ conditions
def _value(states: dict[str, Any], entity_id: str, attribute: str | None) -> Any:
    state = states.get(entity_id)
    if state is None:
        return None
    if attribute:
        return (state.get("attributes") or {}).get(attribute)
    return state.get("state")


def _seconds(time: str) -> int:
    h, m, *s = (int(v) for v in time.split(":"))
    return h * 3600 + m * 60 + (s[0] if s else 0)


class Evaluator:
    def __init__(self, states: dict[str, Any], now: dict[str, Any], evaluators: dict[str, Any], names: Any) -> None:
        self.states = states
        self.now = now
        self.evaluators = evaluators
        self.names = names
        self.missing: set[str] = set()

    def condition(self, item: dict[str, Any], started: set[int]) -> bool | None:
        kind = item["type"]
        if kind in ("and", "or", "not"):
            values = [self.condition(i, started) for i in item["items"]]
            if kind == "and":
                return False if False in values else (None if None in values else True)
            found = True if True in values else (None if None in values else False)
            return found if kind == "or" else (None if found is None else not found)
        if kind == "state":
            if item["entity_id"] not in self.states:
                self.missing.add(item["entity_id"])
            value = _value(self.states, item["entity_id"], item.get("attribute"))
            held = (self.states.get(item["entity_id"]) or {}).get("held")
            result = value is not None and str(value) == str(item["state"]) and (
                not item["for_seconds"] or held is None or held > item["for_seconds"])
            return not result if item["negate"] else result
        if kind == "numeric":
            if item["entity_id"] not in self.states:
                self.missing.add(item["entity_id"])
            try:
                value = float(_value(self.states, item["entity_id"], item.get("attribute")))
            except (TypeError, ValueError):
                return False
            return (item["above"] is None or value > item["above"]) and (item["below"] is None or value < item["below"])
        if kind == "time":
            now = self.now
            if item["weekdays"] and now.get("weekday") not in item["weekdays"]:
                return False
            t = _seconds(now.get("time") or "12:00:00")
            after = _seconds(item["after"]) if item["after"] else None
            before = _seconds(item["before"]) if item["before"] else None
            if after is not None and before is not None and after > before:
                return t >= after or t < before
            return (after is None or t >= after) and (before is None or t < before)
        if kind == "template":
            fn = self.evaluators.get(item["value_template"])
            return bool(fn(self.states)) if fn else None
        if kind == "trigger":
            return bool(set(item["triggers"]) & started)
        return None  # sun, device: not evaluated offline

    def data(self, data: dict[str, Any]) -> dict[str, Any]:
        out = {}
        for key, value in data.items():
            if isinstance(value, str) and ("{{" in value or "{%" in value):
                fn = self.evaluators.get(value)
                out[key] = fn(self.states) if fn else "(template: not evaluated)"
            else:
                out[key] = value
        return out


def _actions(actions: list[dict[str, Any]], ev: Evaluator, targets: list[str], started: set[int], notes: list[str]
             ) -> tuple[list[dict[str, Any]], bool]:
    """``(calls, complete)``: the service calls a sequence makes; complete is False when it waits or branches on
    something unknown (what follows is not predicted)."""
    calls: list[dict[str, Any]] = []
    for action in actions:
        kind = action["type"]
        if kind == "service":
            calls.append({"action": action["action"], "entities": model.action_entities(action, targets),
                          "data": ev.data(action["data"])})
        elif kind in ("delay", "wait_trigger", "wait_condition") or (kind == "repeat" and action["mode"] != "count"):
            notes.append("waits: what follows the wait is not predicted")
            return calls, False
        elif kind == "if":
            result = ev.condition(action["conditions"], started)
            if result is None:
                notes.append("a branch depends on something the simulation cannot evaluate")
                return calls, False
            inner, done = _actions(action["then"] if result else action["else"], ev, targets, started, notes)
            calls += inner
            if not done:
                return calls, False
        elif kind == "choose":
            for branch in action["branches"]:
                result = ev.condition(branch["conditions"], started)
                if result is None:
                    notes.append("a branch depends on something the simulation cannot evaluate")
                    return calls, False
                if result:
                    inner, done = _actions(branch["actions"], ev, targets, started, notes)
                    calls += inner
                    break
            else:
                inner, done = _actions(action["default"], ev, targets, started, notes)
                calls += inner
            if not done:
                return calls, False
        elif kind == "repeat":
            inner, done = _actions(action["actions"], ev, targets, started, notes)
            calls += inner * action["count"]
        elif kind in ("scene", "script", "notify", "device", "variables"):
            calls.append({"action": kind, "entities": [action.get("entity_id")] if action.get("entity_id") else [],
                          "data": {}, "other": describe.action(action, ev.names, targets)})
    return calls, True


def _effect(call: dict[str, Any]) -> str:
    data = call["data"]
    if call["action"].endswith(".turn_off") or call["action"] == "cover.close_cover":
        return call["action"]
    return f"{call['action']} {sorted(data.items())}"


def run(device: dict[str, Any], scenario: dict[str, Any], evaluators: dict[str, Any], names: Any) -> dict[str, Any]:
    """The rules that would run for the chosen trigger, why the others would not, and the resulting calls."""
    states = scenario.get("states") or {}
    chosen = scenario.get("trigger")
    now = {"time": scenario.get("time") or "12:00:00", "weekday": scenario.get("weekday") or "mon"}
    ev = Evaluator(states, now, evaluators, names)
    rules = [r for r in device["rules"] if r["enabled"]] if device["enabled"] else []
    results, matched_shared = [], False
    for rule in rules:
        started = {i for i, t in enumerate(rule["triggers"]) if trigger_key(t) == chosen}
        entry: dict[str, Any] = {"id": rule["id"], "name": rule["name"], "generated": rule.get("generated"),
                                 "summary": describe.rule(rule, names, device["targets"])["text"],
                                 "started": bool(started), "result": "not_started", "failed": [], "unknown": [], "calls": [],
                                 "notes": []}
        if started:
            for trigger in (rule["triggers"][i] for i in started):
                if trigger["type"] == "time" and trigger["weekdays"] and now["weekday"] not in trigger["weekdays"]:
                    entry["failed"].append(f"not on {now['weekday']}")
            for item in rule["conditions"]["items"]:
                value = ev.condition(item, started)
                text = describe.condition(item, names, top=True, triggers=rule["triggers"])
                if value is False:
                    entry["failed"].append(text)
                elif value is None:
                    entry["unknown"].append(text)
            if entry["failed"]:
                entry["result"] = "blocked"
            elif entry["unknown"]:
                entry["result"] = "unknown"
            else:
                entry["result"] = "runs"
                entry["calls"], _done = _actions(rule["actions"], ev, device["targets"], started, entry["notes"])
                if not model.waits(rule):
                    matched_shared = True
        results.append(entry)
    default = model.active_default(device)
    default_result = None
    if default and chosen and any(r["started"] for r in results) and not any(
            r["result"] in ("runs", "unknown") for r in results):
        override = default["override_entity"]
        if override and str(_value(states, override, None)) != "off":
            default_result = {"result": "blocked", "failed": [f"{names(override)} is not off"], "calls": []}
        else:
            calls, _done = _actions(default["actions"], ev, device["targets"], set(), [])
            default_result = {"result": "runs", "failed": [], "calls": calls}
    elif default:
        default_result = {"result": "not_started" if not any(r["started"] for r in results) else "skipped", "failed": [], "calls": []}
    # what each controlled entity receives, and where rules disagree
    received: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for entry in results:
        for call in entry["calls"]:
            for entity_id in call["entities"]:
                received.setdefault(entity_id, []).append((entry["name"], call))
    for call in (default_result or {}).get("calls", []):
        for entity_id in call["entities"]:
            received.setdefault(entity_id, []).append(("Default", call))
    outcome = []
    for entity_id in device["targets"]:
        got = received.get(entity_id, [])
        effects = {_effect(c) for _n, c in got}
        outcome.append({"entity_id": entity_id, "name": names(entity_id), "calls": [{"rule": n, **c} for n, c in got],
                        "final": got[-1][1] if got else None, "conflict": len(effects) > 1,
                        "effect": catalog.effect(got[-1][1]["action"], got[-1][1]["data"])["kind"] if got else None})
    return {"trigger": chosen, "rules": results, "default": default_result, "outcome": outcome,
            "missing": sorted(ev.missing), "matched": matched_shared,
            "origins": {e: (s or {}).get("origin", "test") for e, s in states.items()},
            "note": "A prediction from the rules. Nothing was sent to any device. Test the deployed automation in "
                    "Home Assistant to know what really happens."}


def inputs(device: dict[str, Any], index: Any) -> list[dict[str, Any]]:
    """The entities whose states matter to the device's rules, with what Home Assistant reports now."""
    out = []
    used = model.referenced_entities(device)
    for rule in device["rules"]:
        for leaf in model.all_conditions(rule):
            if leaf.get("type") == "template":
                for entity_id in index.states:
                    if f"'{entity_id}'" in leaf["value_template"]:
                        used.setdefault(entity_id, set()).add("condition")
        for trigger in rule["triggers"]:
            if trigger["type"] == "template":
                for entity_id in index.states:
                    if f"'{entity_id}'" in trigger["value_template"]:
                        used.setdefault(entity_id, set()).add("trigger")
    for entity_id in sorted(used):
        state = index.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        keep = {k: attributes[k] for k in ("elevation", "azimuth", "current_position", "current_tilt_position", "fresh",
                                           "brightness", "color_temp_kelvin", "options", "unit_of_measurement") if k in attributes}
        out.append({"entity_id": entity_id, "name": index.name(entity_id), "uses": sorted(used[entity_id]),
                    "state": state.get("state"), "attributes": keep, "exists": index.exists(entity_id),
                    "suggested": index.suggested_states(entity_id)})
    return out
