"""The rule model: what the user defines, independent of the Home Assistant automation generated from it.

A *managed device* is one controlled target: one or more explicitly selected
Home Assistant entities (never "everything in an area"). It owns a list of
IFTTT rules::

    IF   any of the rule's triggers fires
    AND  the rule's condition tree (AND/OR groups) holds
    THEN run the rule's actions

Every trigger, condition and action is a small dictionary with a ``type``. New
types are added by registering a validator here and a generator in
``compiler.py`` (and a describer in ``describe.py``); nothing else in the model
changes. Each value that a future blueprint would turn into an input (entities,
thresholds, delays, action data) is a plain field of such a dictionary, see
``blueprint.py``.

``validate_config`` returns the canonical form (every key present, fixed
order), so equal configurations are equal documents and compile to the same
automation.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from typing import Any

SCHEMA = 1

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
DEVICE_ID = re.compile(r"^d_[0-9a-f]{8}$")
RULE_ID = re.compile(r"^r_[0-9a-f]{8}$")
ACTION_NAME = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
TIME_OF_DAY = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
RETRIGGER_MODES = ("restart", "single", "queued", "parallel")
PERIODIC_CHOICES = (0, 1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30)  # minutes; divisors of 60 (0 = off)

MAX_NAME = 80
MAX_STATE = 120
MAX_DEVICES = 200
MAX_TARGETS = 20
MAX_RULES = 40
MAX_TRIGGERS = 10
MAX_CONDITIONS = 20
MAX_ACTIONS = 20
MAX_DEPTH = 3
MAX_SECONDS = 86400
MAX_DATA_BYTES = 4000
LIGHT_COLOUR_KEYS = ("color_temp_kelvin", "rgb_color", "hs_color", "xy_color", "rgbw_color", "rgbww_color", "color_name")

#: Defaults of the automatic occupancy rechecks (Settings; a rule can override them).
OCCUPANCY_DEFAULTS: dict[str, Any] = {"first_seconds": 2, "second_seconds": 15, "periodic_minutes": 5,
                                      "startup_grace_seconds": 300}


class ConfigError(Exception):
    """A request or configuration that cannot be accepted."""

    def __init__(self, code: str, message: str, status: int = 400, problems: list[dict[str, str]] | None = None,
                 **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.problems = problems or []
        self.extra = extra


class Problems:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, path: str, message: str) -> None:
        if len(self.items) < 100:
            self.items.append({"path": path, "message": message})


def new_device_id() -> str:
    return f"d_{secrets.token_hex(4)}"


def new_rule_id() -> str:
    return f"r_{secrets.token_hex(4)}"


def empty_config() -> dict[str, Any]:
    return {"settings": {"occupancy": dict(OCCUPANCY_DEFAULTS)}, "devices": {}}


# ---------------------------------------------------------------- primitives
def _text(raw: Any, path: str, p: Problems, *, limit: int = MAX_NAME, required: bool = True, what: str = "a text") -> str:
    if raw is None and not required:
        return ""
    if not isinstance(raw, str):
        p.add(path, f"Must be {what}")
        return ""
    value = raw.strip()
    if not value and required:
        p.add(path, "Must not be empty")
    if len(value) > limit:
        p.add(path, f"At most {limit} characters")
        value = value[:limit]
    return value


def _entity(raw: Any, path: str, p: Problems) -> str:
    if not isinstance(raw, str) or not ENTITY_ID.match(raw) or len(raw) > 255:
        p.add(path, "Choose an entity")
        return ""
    return raw


def _state(raw: Any, path: str, p: Problems, *, required: bool) -> str | None:
    """A state value. Kept exactly as given (states are case-sensitive; ``input_select`` options may contain spaces)."""
    if raw is None or raw == "":
        if required:
            p.add(path, "Choose a state")
        return None
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        p.add(path, "Must be a state value")
        return None
    value = str(raw)
    if len(value) > MAX_STATE:
        p.add(path, f"At most {MAX_STATE} characters")
        return None
    return value


def _number(raw: Any, path: str, p: Problems) -> float | int | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        try:
            raw = float(raw.replace(",", "."))
        except ValueError:
            p.add(path, "Must be a number")
            return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
        p.add(path, "Must be a number")
        return None
    return int(raw) if float(raw).is_integer() else round(float(raw), 4)


def _seconds(raw: Any, path: str, p: Problems, *, minimum: int = 0, maximum: int = MAX_SECONDS) -> int:
    if raw is None or raw == "":
        raw = minimum
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw) or not minimum <= raw <= maximum:
        p.add(path, f"Must be a whole number of seconds between {minimum} and {maximum}")
        return minimum
    return int(raw)


def _time(raw: Any, path: str, p: Problems, *, required: bool) -> str | None:
    if raw is None or raw == "":
        if required:
            p.add(path, "Choose a time")
        return None
    if not isinstance(raw, str) or not TIME_OF_DAY.match(raw):
        p.add(path, "Must be a time of day (HH:MM)")
        return None
    return raw if len(raw) == 8 else f"{raw}:00"


def _weekdays(raw: Any, path: str, p: Problems) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or any(d not in WEEKDAYS for d in raw):
        p.add(path, "Must be a list of weekdays")
        return []
    chosen = [d for d in WEEKDAYS if d in raw]
    return [] if len(chosen) == 7 else chosen  # every day is the same as no restriction


def _threshold(raw: dict[str, Any], path: str, p: Problems) -> tuple[Any, Any]:
    above, below = _number(raw.get("above"), f"{path}.above", p), _number(raw.get("below"), f"{path}.below", p)
    if above is None and below is None:
        p.add(path, "Give a threshold (above, below or both)")
    elif above is not None and below is not None and above >= below:
        p.add(path, "“Above” must be lower than “below” (the value must lie between them)")
    return above, below


# ------------------------------------------------------------------ triggers
def _trigger_state(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    to, from_ = _state(raw.get("to"), f"{path}.to", p, required=False), _state(raw.get("from"), f"{path}.from", p, required=False)
    if to is not None and to == from_:
        p.add(path, "“From” and “to” are the same state: this never happens")
    return {"type": "state", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "to": to, "from": from_,
            "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p)}


def _trigger_numeric(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    above, below = _threshold(raw, path, p)
    return {"type": "numeric", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "above": above,
            "below": below, "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p)}


def _trigger_time(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return {"type": "time", "at": _time(raw.get("at"), f"{path}.at", p, required=True),
            "weekdays": _weekdays(raw.get("weekdays"), f"{path}.weekdays", p)}


def _trigger_sun(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    event = raw.get("event")
    if event not in ("sunrise", "sunset"):
        p.add(f"{path}.event", "Choose sunrise or sunset")
        event = "sunset"
    offset = raw.get("offset_minutes") or 0
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or offset != int(offset) or abs(offset) > 720:
        p.add(f"{path}.offset_minutes", "The offset must be a whole number of minutes between -720 and 720")
        offset = 0
    return {"type": "sun", "event": event, "offset_minutes": int(offset)}


def _trigger_occupancy(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    to = raw.get("to")
    if to not in ("occupied", "vacant"):
        p.add(f"{path}.to", "Choose “becomes occupied” or “becomes vacant”")
        to = "occupied"
    entity_id = _entity(raw.get("entity_id"), f"{path}.entity_id", p)
    if entity_id and not entity_id.startswith("binary_sensor."):
        p.add(f"{path}.entity_id", "An occupancy trigger needs a binary sensor (on = occupied)")
    return {"type": "occupancy", "entity_id": entity_id, "to": to}


TRIGGER_TYPES = {"state": _trigger_state, "numeric": _trigger_numeric, "time": _trigger_time, "sun": _trigger_sun,
                 "occupancy": _trigger_occupancy}


# ---------------------------------------------------------------- conditions
def _condition_state(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return {"type": "state", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p),
            "state": _state(raw.get("state"), f"{path}.state", p, required=True), "negate": raw.get("negate") is True,
            "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p)}


def _condition_numeric(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    above, below = _threshold(raw, path, p)
    return {"type": "numeric", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "above": above,
            "below": below}


def _condition_time(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    after, before = _time(raw.get("after"), f"{path}.after", p, required=False), _time(raw.get("before"), f"{path}.before", p, required=False)
    weekdays = _weekdays(raw.get("weekdays"), f"{path}.weekdays", p)
    if after is None and before is None and not weekdays:
        p.add(path, "Give a time range, weekdays or both")
    return {"type": "time", "after": after, "before": before, "weekdays": weekdays}


CONDITION_TYPES = {"state": _condition_state, "numeric": _condition_numeric, "time": _condition_time}


def _group(raw: Any, path: str, p: Problems, depth: int, budget: list[int]) -> dict[str, Any]:
    if raw is None:
        return {"type": "and", "items": []}
    if not isinstance(raw, dict) or raw.get("type") not in ("and", "or") or not isinstance(raw.get("items"), list):
        p.add(path, "Must be a group of conditions (all of / any of)")
        return {"type": "and", "items": []}
    items = []
    for i, item in enumerate(raw["items"]):
        item_path = f"{path}.items[{i}]"
        budget[0] -= 1
        if budget[0] < 0:
            p.add(path, f"At most {MAX_CONDITIONS} conditions per rule")
            break
        if not isinstance(item, dict):
            p.add(item_path, "Must be a condition")
        elif item.get("type") in ("and", "or"):
            if depth >= MAX_DEPTH:
                p.add(item_path, "Groups are nested too deeply")
                continue
            group = _group(item, item_path, p, depth + 1, budget)
            if not group["items"]:
                p.add(item_path, "A group needs at least one condition")
            items.append(group)
        elif item.get("type") in CONDITION_TYPES:
            items.append(CONDITION_TYPES[item["type"]](item, item_path, p))
        else:
            p.add(item_path, f"Unsupported condition type {item.get('type')!r}")
    return {"type": raw["type"], "items": items}


# ------------------------------------------------------------------- actions
def _json_size(value: Any, depth: int = 0) -> bool:
    """Is this plain JSON data (no deeper than 6 levels)?"""
    if depth > 6:
        return False
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_json_size(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_size(v, depth + 1) for k, v in value.items())
    return False


def _action_service(raw: dict[str, Any], path: str, p: Problems, targets: list[str]) -> dict[str, Any]:
    action = raw.get("action")
    if not isinstance(action, str) or not ACTION_NAME.match(action) or len(action) > 120:
        p.add(f"{path}.action", "Choose an action (for example light.turn_on)")
        action = ""
    target = raw.get("target", "all")
    if target in ("all", "none"):
        pass
    elif isinstance(target, list) and target and all(isinstance(e, str) for e in target):
        outside = [e for e in target if e not in targets]
        if outside:
            p.add(f"{path}.target", f"{', '.join(outside)} is not one of this device's selected entities")
        target = [e for e in targets if e in target]
        if len(target) == len(targets):
            target = "all"
    else:
        p.add(f"{path}.target", "Choose which of the device's entities the action controls")
        target = "all"
    domain = action.split(".", 1)[0]
    if action and domain != "homeassistant":
        entities = targets if target == "all" else [] if target == "none" else target
        wrong = [e for e in entities if e.split(".", 1)[0] != domain]
        if wrong:
            p.add(f"{path}.target", f"{action} cannot control {', '.join(wrong)}: choose the {domain} entities it "
                                    "should control")
    data = raw.get("data") or {}
    if not isinstance(data, dict) or not _json_size(data) or len(json.dumps(data)) > MAX_DATA_BYTES:
        p.add(f"{path}.data", "The action data must be a small JSON object")
        data = {}
    if "entity_id" in data or "target" in data:
        p.add(f"{path}.data", "Leave entity_id out of the data: the controlled entities are chosen above")
    if action == "light.turn_on":
        colours = [k for k in LIGHT_COLOUR_KEYS if k in data]
        if len(colours) > 1:
            p.add(f"{path}.data", f"Choose one way to set the colour, not several ({', '.join(colours)}): Home "
                                  "Assistant refuses the combination")
    return {"type": "service", "action": action, "target": target, "data": {k: data[k] for k in sorted(data)}}


def _action_delay(raw: dict[str, Any], path: str, p: Problems, _targets: list[str]) -> dict[str, Any]:
    return {"type": "delay", "seconds": _seconds(raw.get("seconds"), f"{path}.seconds", p, minimum=1)}


ACTION_TYPES = {"service": _action_service, "delay": _action_delay}


# --------------------------------------------------------------------- rules
def _recheck(raw: Any, path: str, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {"enabled": raw.get("enabled") is not False}
    for key, low, high in (("first_seconds", 1, 60), ("second_seconds", 2, 3600)):
        value = raw.get(key)
        out[key] = None if value in (None, "") else _seconds(value, f"{path}.{key}", p, minimum=low, maximum=high)
    periodic = raw.get("periodic_minutes")
    if periodic in (None, ""):
        out["periodic_minutes"] = None
    elif isinstance(periodic, bool) or periodic not in PERIODIC_CHOICES:
        p.add(f"{path}.periodic_minutes", f"Choose one of {', '.join(map(str, PERIODIC_CHOICES))} minutes (0 = off)")
        out["periodic_minutes"] = None
    else:
        out["periodic_minutes"] = int(periodic)
    override = raw.get("override_entity")
    out["override_entity"] = _entity(override, f"{path}.override_entity", p) if override else None
    return out


def validate_rule(raw: Any, path: str, p: Problems, targets: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        p.add(path, "Must be a rule")
        raw = {}
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not RULE_ID.match(rule_id):
        p.add(f"{path}.id", "Invalid rule id")
        rule_id = ""
    triggers = []
    raw_triggers = raw.get("triggers") if isinstance(raw.get("triggers"), list) else []
    if not raw_triggers:
        p.add(f"{path}.triggers", "Add at least one trigger (IF)")
    if len(raw_triggers) > MAX_TRIGGERS:
        p.add(f"{path}.triggers", f"At most {MAX_TRIGGERS} triggers per rule")
    for i, trigger in enumerate(raw_triggers[:MAX_TRIGGERS]):
        kind = trigger.get("type") if isinstance(trigger, dict) else None
        if kind not in TRIGGER_TYPES:
            p.add(f"{path}.triggers[{i}]", f"Unsupported trigger type {kind!r}")
            continue
        clean = TRIGGER_TYPES[kind](trigger, f"{path}.triggers[{i}]", p)
        if clean in triggers:
            p.add(f"{path}.triggers[{i}]", "This trigger is listed twice")
        else:
            triggers.append(clean)
    actions = []
    raw_actions = raw.get("actions") if isinstance(raw.get("actions"), list) else []
    if not raw_actions:
        p.add(f"{path}.actions", "Add at least one action (THEN)")
    if len(raw_actions) > MAX_ACTIONS:
        p.add(f"{path}.actions", f"At most {MAX_ACTIONS} actions per rule")
    for i, action in enumerate(raw_actions[:MAX_ACTIONS]):
        kind = action.get("type") if isinstance(action, dict) else None
        if kind not in ACTION_TYPES:
            p.add(f"{path}.actions[{i}]", f"Unsupported action type {kind!r}")
            continue
        actions.append(ACTION_TYPES[kind](action, f"{path}.actions[{i}]", p, targets))
    if actions and actions[-1]["type"] == "delay":
        p.add(f"{path}.actions[{len(actions) - 1}]", "A delay at the end does nothing: add the action that follows it")
    retrigger = raw.get("retrigger", "restart")
    if retrigger not in RETRIGGER_MODES:
        p.add(f"{path}.retrigger", "Unknown retrigger behaviour")
        retrigger = "restart"
    return {
        "id": rule_id,
        "name": _text(raw.get("name"), f"{path}.name", p, what="a name"),
        "enabled": raw.get("enabled") is not False,
        "triggers": triggers,
        "conditions": _group(raw.get("conditions"), f"{path}.conditions", p, 1, [MAX_CONDITIONS]),
        "actions": actions,
        "retrigger": retrigger,
        "recheck": _recheck(raw.get("recheck"), f"{path}.recheck", p),
    }


def validate_device(device_id: str, raw: Any, path: str, p: Problems) -> dict[str, Any]:
    if not isinstance(raw, dict):
        p.add(path, "Must be a device")
        raw = {}
    targets: list[str] = []
    raw_targets = raw.get("targets") if isinstance(raw.get("targets"), list) else []
    if not raw_targets:
        p.add(f"{path}.targets", "Select at least one entity to control")
    if len(raw_targets) > MAX_TARGETS:
        p.add(f"{path}.targets", f"At most {MAX_TARGETS} entities per device")
    for i, target in enumerate(raw_targets[:MAX_TARGETS]):
        entity_id = _entity(target, f"{path}.targets[{i}]", p)
        if entity_id and entity_id not in targets:
            targets.append(entity_id)
    raw_rules = raw.get("rules") if isinstance(raw.get("rules"), list) else []
    if len(raw_rules) > MAX_RULES:
        p.add(f"{path}.rules", f"At most {MAX_RULES} rules per device")
    rules, seen = [], set()
    for i, rule in enumerate(raw_rules[:MAX_RULES]):
        clean = validate_rule(rule, f"{path}.rules[{i}]", p, targets)
        if clean["id"] in seen:
            p.add(f"{path}.rules[{i}].id", "Two rules share an id")
        seen.add(clean["id"])
        rules.append(clean)
    ha_device = raw.get("ha_device_id")
    acknowledged = raw.get("acknowledged") if isinstance(raw.get("acknowledged"), list) else []
    return {
        "id": device_id,
        "name": _text(raw.get("name"), f"{path}.name", p, what="a name"),
        "enabled": raw.get("enabled") is not False,
        "targets": targets,
        "ha_device_id": ha_device if isinstance(ha_device, str) and 0 < len(ha_device) <= 64 else None,
        "rules": rules,
        "acknowledged": sorted({a for a in acknowledged if isinstance(a, str) and len(a) <= 120})[:100],
    }


def validate_settings(raw: Any, p: Problems) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    occ = raw.get("occupancy") if isinstance(raw.get("occupancy"), dict) else {}
    first = _seconds(occ.get("first_seconds", OCCUPANCY_DEFAULTS["first_seconds"]), "settings.occupancy.first_seconds",
                     p, minimum=1, maximum=60)
    second = _seconds(occ.get("second_seconds", OCCUPANCY_DEFAULTS["second_seconds"]),
                      "settings.occupancy.second_seconds", p, minimum=2, maximum=3600)
    if second <= first:
        p.add("settings.occupancy.second_seconds", "The second check must come after the first")
    periodic = occ.get("periodic_minutes", OCCUPANCY_DEFAULTS["periodic_minutes"])
    if isinstance(periodic, bool) or periodic not in PERIODIC_CHOICES:
        p.add("settings.occupancy.periodic_minutes",
              f"Choose one of {', '.join(map(str, PERIODIC_CHOICES))} minutes (0 = off)")
        periodic = OCCUPANCY_DEFAULTS["periodic_minutes"]
    grace = _seconds(occ.get("startup_grace_seconds", OCCUPANCY_DEFAULTS["startup_grace_seconds"]),
                     "settings.occupancy.startup_grace_seconds", p, minimum=0, maximum=3600)
    return {"occupancy": {"first_seconds": first, "second_seconds": second, "periodic_minutes": int(periodic),
                          "startup_grace_seconds": grace}}


def validate_config(raw: Any) -> dict[str, Any]:
    """The canonical configuration, or :class:`ConfigError` with every problem found."""
    p = Problems()
    if not isinstance(raw, dict):
        raise ConfigError("invalid", "The configuration must be an object")
    devices_raw = raw.get("devices") if isinstance(raw.get("devices"), dict) else {}
    if len(devices_raw) > MAX_DEVICES:
        p.add("devices", f"At most {MAX_DEVICES} devices")
    devices, rule_ids = {}, set()
    for device_id in sorted(devices_raw)[:MAX_DEVICES]:
        if not isinstance(device_id, str) or not DEVICE_ID.match(device_id):
            p.add(f"devices.{device_id}", "Invalid device id")
            continue
        device = validate_device(device_id, devices_raw[device_id], f"devices.{device_id}", p)
        for rule in device["rules"]:
            if rule["id"] in rule_ids:
                p.add(f"devices.{device_id}", f"Rule id {rule['id']} is used twice")
            rule_ids.add(rule["id"])
        devices[device_id] = device
    config = {"settings": validate_settings(raw.get("settings"), p), "devices": devices}
    if p.items:
        raise ConfigError("invalid", "The configuration is not valid", problems=p.items)
    return config


# ------------------------------------------------------------------ helpers
def rule_recheck(rule: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """The effective recheck timing of a rule: its own values over the defaults in Settings."""
    defaults = settings["occupancy"]
    own = rule["recheck"]
    first = own["first_seconds"] if own["first_seconds"] is not None else defaults["first_seconds"]
    second = own["second_seconds"] if own["second_seconds"] is not None else defaults["second_seconds"]
    periodic = own["periodic_minutes"] if own["periodic_minutes"] is not None else defaults["periodic_minutes"]
    return {"enabled": own["enabled"], "first_seconds": first, "second_seconds": max(second, first + 1),
            "periodic_minutes": periodic, "startup_grace_seconds": defaults["startup_grace_seconds"],
            "override_entity": own["override_entity"]}


def condition_leaves(group: dict[str, Any]):
    for item in group["items"]:
        if item["type"] in ("and", "or"):
            yield from condition_leaves(item)
        else:
            yield item


def referenced_entities(device: dict[str, Any]) -> dict[str, set[str]]:
    """Every entity a device's rules read or control, with how it is used (for the missing-entity check)."""
    out: dict[str, set[str]] = {}

    def add(entity_id: str | None, use: str) -> None:
        if entity_id:
            out.setdefault(entity_id, set()).add(use)

    for target in device["targets"]:
        add(target, "controlled")
    for rule in device["rules"]:
        if not rule["enabled"]:
            continue
        for trigger in rule["triggers"]:
            add(trigger.get("entity_id"), "trigger")
        for leaf in condition_leaves(rule["conditions"]):
            add(leaf.get("entity_id"), "condition")
        add(rule["recheck"]["override_entity"], "manual override signal")
    return out


def has_delay(rule: dict[str, Any]) -> bool:
    return any(a["type"] == "delay" for a in rule["actions"])


def action_entities(action: dict[str, Any], targets: list[str]) -> list[str]:
    if action["type"] != "service" or action["target"] == "none":
        return []
    return list(targets) if action["target"] == "all" else list(action["target"])
