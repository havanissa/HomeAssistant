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
MAX_TEMPLATE = 2000
MAX_ACTION_DEPTH = 4      # nesting of branches, repeats and waits inside a rule's actions
MAX_ACTIONS_TOTAL = 60    # actions of one rule, nested ones included
MAX_BRANCHES = 10
MAX_REPEAT = 1000
EVENT_TYPE = re.compile(r"^[A-Za-z0-9_.\-]{1,120}$")
VARIABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
PATTERN_FIELD = re.compile(r"^(\*|/[1-9]\d?|\d{1,2})$")
#: Names Home Assistant sets itself inside a running automation: a rule's own variable must not replace them.
RESERVED_VARIABLES = frozenset({"trigger", "this", "wait", "repeat", "context"})
ON_TIMEOUT = ("continue", "stop")
REPEAT_MODES = ("count", "while", "until")
TIME_ENTITY_DOMAINS = ("input_datetime", "sensor")
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


def _template(raw: Any, path: str, p: Problems) -> str:
    if not isinstance(raw, str) or not raw.strip():
        p.add(path, "Enter a template")
        return ""
    if len(raw) > MAX_TEMPLATE:
        p.add(path, f"At most {MAX_TEMPLATE} characters")
        return ""
    if "{{" not in raw and "{%" not in raw:
        p.add(path, "A template contains {{ … }} (for example {{ states('sensor.x') | float(0) > 5 }})")
    return raw.strip()


def _attribute(raw: Any, path: str, p: Problems) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or len(raw) > 80 or not re.fullmatch(r"[A-Za-z0-9_ .\-]+", raw):
        p.add(path, "Must be the name of an attribute")
        return None
    return raw


def _small_json(raw: Any, path: str, p: Problems, what: str) -> dict[str, Any]:
    data = raw or {}
    if not isinstance(data, dict) or not _json_size(data) or len(json.dumps(data)) > MAX_DATA_BYTES:
        p.add(path, f"{what} must be a small JSON object")
        return {}
    return {k: data[k] for k in sorted(data)}


def _device_automation(raw: dict[str, Any], path: str, p: Problems, kind: str) -> dict[str, Any]:
    """A device trigger, condition or action exactly as Home Assistant lists it for a device (never invented here)."""
    config = raw.get("config")
    if (not isinstance(config, dict) or not _json_size(config) or len(json.dumps(config)) > MAX_DATA_BYTES
            or not all(isinstance(config.get(k), str) and config.get(k) for k in ("device_id", "domain", "type"))):
        p.add(path, f"Choose one of the {kind}s Home Assistant offers for the device")
        config = {}
    clean = {k: config[k] for k in sorted(config) if k not in ("platform", "trigger", "condition", "id", "metadata", "alias")}
    label = raw.get("label")
    return {"type": "device", "config": clean, "label": label.strip()[:MAX_STATE] if isinstance(label, str) else ""}


# ------------------------------------------------------------------ triggers
def _trigger_state(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    to, from_ = _state(raw.get("to"), f"{path}.to", p, required=False), _state(raw.get("from"), f"{path}.from", p, required=False)
    if to is not None and to == from_:
        p.add(path, "“From” and “to” are the same state: this never happens")
    return {"type": "state", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "to": to, "from": from_,
            "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p),
            "attribute": _attribute(raw.get("attribute"), f"{path}.attribute", p)}


def _trigger_numeric(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    above, below = _threshold(raw, path, p)
    return {"type": "numeric", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "above": above,
            "below": below, "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p),
            "attribute": _attribute(raw.get("attribute"), f"{path}.attribute", p)}


def _trigger_time(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    at = raw.get("at")
    if isinstance(at, str) and ENTITY_ID.match(at) and at.split(".", 1)[0] in TIME_ENTITY_DOMAINS:
        pass  # the time held by a date/time helper or a timestamp sensor
    else:
        at = _time(at, f"{path}.at", p, required=True)
    return {"type": "time", "at": at, "weekdays": _weekdays(raw.get("weekdays"), f"{path}.weekdays", p)}


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


def _trigger_button(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    """A button or remote that Home Assistant shows as an event entity (Zigbee2MQTT, Matter, Hue, ZHA quirks v2 …)."""
    entity_id = _entity(raw.get("entity_id"), f"{path}.entity_id", p)
    if entity_id and not entity_id.startswith("event."):
        p.add(f"{path}.entity_id", "Choose an event entity (a button or remote)")
    kinds = raw.get("event_types") or []
    if not isinstance(kinds, list) or len(kinds) > 30 or any(not isinstance(k, str) or not 0 < len(k) <= MAX_STATE for k in kinds):
        p.add(f"{path}.event_types", "Must be a list of the button's event types")
        kinds = []
    return {"type": "button", "entity_id": entity_id, "event_types": sorted(set(kinds))}


def _trigger_device(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return _device_automation(raw, path, p, "trigger")


def _trigger_event(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not EVENT_TYPE.match(event_type):
        p.add(f"{path}.event_type", "Enter the event type (for example zha_event)")
        event_type = ""
    return {"type": "event", "event_type": event_type,
            "event_data": _small_json(raw.get("event_data"), f"{path}.event_data", p, "The event data")}


def _trigger_mqtt(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    topic = raw.get("topic")
    if not isinstance(topic, str) or not topic.strip() or len(topic) > 250 or any(c in topic for c in "\n\r\0"):
        p.add(f"{path}.topic", "Enter the MQTT topic (for example zigbee2mqtt/My remote/action)")
        topic = ""
    payload = raw.get("payload")
    if payload in (None, ""):
        payload = None
    elif not isinstance(payload, str) or len(payload) > MAX_STATE:
        p.add(f"{path}.payload", f"The payload must be a text of at most {MAX_STATE} characters")
        payload = None
    return {"type": "mqtt", "topic": topic.strip(), "payload": payload}


def _trigger_template(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return {"type": "template", "value_template": _template(raw.get("value_template"), f"{path}.value_template", p),
            "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p)}


def _trigger_pattern(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "pattern"}
    for key in ("hours", "minutes", "seconds"):
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if value in (None, ""):
            value = None
        elif not isinstance(value, str) or not PATTERN_FIELD.match(value):
            p.add(f"{path}.{key}", "Use a number, /N (every N) or *")
            value = None
        out[key] = value
    if out["hours"] is None and out["minutes"] is None and out["seconds"] is None:
        p.add(path, "Give at least one of hours, minutes or seconds (for example minutes /15)")
    return out


def _trigger_homeassistant(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    event = raw.get("event")
    if event not in ("start", "shutdown"):
        p.add(f"{path}.event", "Choose “starts” or “shuts down”")
        event = "start"
    return {"type": "homeassistant", "event": event}


TRIGGER_TYPES = {"state": _trigger_state, "numeric": _trigger_numeric, "time": _trigger_time, "sun": _trigger_sun,
                 "occupancy": _trigger_occupancy, "button": _trigger_button, "device": _trigger_device,
                 "event": _trigger_event, "mqtt": _trigger_mqtt, "template": _trigger_template,
                 "pattern": _trigger_pattern, "homeassistant": _trigger_homeassistant}
#: Trigger types that can end a "wait for trigger" action (an occupancy trigger brings its rechecks: not there).
WAIT_TRIGGER_TYPES = tuple(t for t in TRIGGER_TYPES if t != "occupancy")


def _triggers(raw: Any, path: str, p: Problems, *, allowed: Any = None, what: str = "Add at least one trigger (IF)",
              limit: int = MAX_TRIGGERS) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    raw_triggers = raw if isinstance(raw, list) else []
    if not raw_triggers:
        p.add(path, what)
    if len(raw_triggers) > limit:
        p.add(path, f"At most {limit} triggers")
    for i, trigger in enumerate(raw_triggers[:limit]):
        kind = trigger.get("type") if isinstance(trigger, dict) else None
        if kind not in TRIGGER_TYPES or (allowed is not None and kind not in allowed):
            p.add(f"{path}[{i}]", f"Unsupported trigger type {kind!r}")
            continue
        clean = TRIGGER_TYPES[kind](trigger, f"{path}[{i}]", p)
        if clean in triggers:
            p.add(f"{path}[{i}]", "This trigger is listed twice")
        else:
            triggers.append(clean)
    return triggers


# ---------------------------------------------------------------- conditions
def _condition_state(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return {"type": "state", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p),
            "state": _state(raw.get("state"), f"{path}.state", p, required=True), "negate": raw.get("negate") is True,
            "for_seconds": _seconds(raw.get("for_seconds"), f"{path}.for_seconds", p),
            "attribute": _attribute(raw.get("attribute"), f"{path}.attribute", p)}


def _condition_numeric(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    above, below = _threshold(raw, path, p)
    return {"type": "numeric", "entity_id": _entity(raw.get("entity_id"), f"{path}.entity_id", p), "above": above,
            "below": below, "attribute": _attribute(raw.get("attribute"), f"{path}.attribute", p)}


def _condition_time(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    after, before = _time(raw.get("after"), f"{path}.after", p, required=False), _time(raw.get("before"), f"{path}.before", p, required=False)
    weekdays = _weekdays(raw.get("weekdays"), f"{path}.weekdays", p)
    if after is None and before is None and not weekdays:
        p.add(path, "Give a time range, weekdays or both")
    return {"type": "time", "after": after, "before": before, "weekdays": weekdays}


def _condition_template(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return {"type": "template", "value_template": _template(raw.get("value_template"), f"{path}.value_template", p)}


def _condition_trigger(raw: dict[str, Any], path: str, p: Problems, count: int = 0) -> dict[str, Any]:
    """"The rule was started by trigger number …" (positions in the rule's own trigger list)."""
    chosen = raw.get("triggers")
    if (not isinstance(chosen, list) or not chosen
            or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count for i in chosen)):
        p.add(f"{path}.triggers", "Choose which of the rule's triggers this is about")
        chosen = []
    return {"type": "trigger", "triggers": sorted(set(chosen))}


def _condition_device(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    return _device_automation(raw, path, p, "condition")


def _sun_offset(raw: Any, path: str, p: Problems) -> int:
    raw = raw or 0
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw) or abs(raw) > 720:
        p.add(path, "The offset must be a whole number of minutes between -720 and 720")
        return 0
    return int(raw)


def _condition_sun(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "sun"}
    for key in ("after", "before"):
        value = raw.get(key) or None
        if value not in (None, "sunrise", "sunset"):
            p.add(f"{path}.{key}", "Choose sunrise or sunset")
            value = None
        out[key] = value
        out[f"{key}_offset_minutes"] = _sun_offset(raw.get(f"{key}_offset_minutes"), f"{path}.{key}_offset_minutes", p) if value else 0
    if out["after"] is None and out["before"] is None:
        p.add(path, "Choose “after”, “before” or both")
    return out


CONDITION_TYPES = {"state": _condition_state, "numeric": _condition_numeric, "time": _condition_time,
                   "template": _condition_template, "trigger": _condition_trigger, "device": _condition_device,
                   "sun": _condition_sun}
GROUP_TYPES = ("and", "or", "not")


def _group(raw: Any, path: str, p: Problems, depth: int, budget: list[int], triggers: int = 0,
           max_depth: int = MAX_DEPTH) -> dict[str, Any]:
    if raw is None:
        return {"type": "and", "items": []}
    if not isinstance(raw, dict) or raw.get("type") not in GROUP_TYPES or not isinstance(raw.get("items"), list):
        p.add(path, "Must be a group of conditions (all of / any of / none of)")
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
        elif item.get("type") in GROUP_TYPES:
            if depth >= max_depth:
                p.add(item_path, "Groups are nested too deeply")
                continue
            group = _group(item, item_path, p, depth + 1, budget, triggers, max_depth)
            if not group["items"]:
                p.add(item_path, "A group needs at least one condition")
            items.append(group)
        elif item.get("type") == "trigger":
            items.append(_condition_trigger(item, item_path, p, triggers))
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


def _action_service(raw: dict[str, Any], path: str, p: Problems, ctx: "_Ctx") -> dict[str, Any]:
    targets = ctx.targets
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
    if ctx.generated and isinstance(data, str):
        # Only a designer's generated rule: the whole data as one template that renders a mapping (the Lighting
        # Designer's apply step sends exactly the attributes that were resolved, no more).
        if not data.startswith("{{") or len(data) > ctx.limits["data"]:
            p.add(f"{path}.data", "Internal: generated action data must be one template")
            data = {}
        return {"type": "service", "action": action, "target": target, "data": data}
    if not isinstance(data, dict) or not _json_size(data) or len(json.dumps(data)) > ctx.limits["data"]:
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


def _action_delay(raw: dict[str, Any], path: str, p: Problems, _ctx: "_Ctx") -> dict[str, Any]:
    return {"type": "delay", "seconds": _seconds(raw.get("seconds"), f"{path}.seconds", p, minimum=1)}


#: Limits of the actions of a rule; generated rules (designers) evaluate several sources in one rule by nature.
ACTION_LIMITS = {"actions": MAX_ACTIONS, "total": MAX_ACTIONS_TOTAL, "branches": MAX_BRANCHES, "depth": MAX_ACTION_DEPTH,
                 "conditions": MAX_CONDITIONS * 2, "condition_depth": MAX_DEPTH, "data": MAX_DATA_BYTES}
GENERATED_ACTION_LIMITS = {"actions": 80, "total": 600, "branches": 40, "depth": 6, "conditions": 600,
                           "condition_depth": 6, "data": 24000}


class _Ctx:
    """What the validation of a rule's actions needs to know about the rule."""

    def __init__(self, targets: list[str], triggers: int, generated: bool = False) -> None:
        self.targets = targets
        self.triggers = triggers
        self.generated = generated
        self.limits = GENERATED_ACTION_LIMITS if generated else ACTION_LIMITS
        self.depth = 0
        self.budget = [self.limits["total"]]
        self.conditions = [self.limits["conditions"]]


def _other_entity(raw: Any, path: str, p: Problems, domain: str, what: str) -> str:
    entity_id = _entity(raw, path, p)
    if entity_id and entity_id.split(".", 1)[0] != domain:
        p.add(path, f"Choose {what}")
    return entity_id


def _action_notify(raw: dict[str, Any], path: str, p: Problems, _ctx: _Ctx) -> dict[str, Any]:
    action = raw.get("action")
    if (not isinstance(action, str) or not ACTION_NAME.match(action) or len(action) > 120
            or action.split(".", 1)[0] not in ("notify", "persistent_notification")):
        p.add(f"{path}.action", "Choose where the notification goes (a notify action)")
        action = ""
    entity_id = raw.get("entity_id") or None
    if action == "notify.send_message":
        entity_id = _other_entity(entity_id, f"{path}.entity_id", p, "notify", "a notify entity")
    elif entity_id is not None:
        p.add(f"{path}.entity_id", "Only notify.send_message takes a notify entity")
        entity_id = None
    message = raw.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_TEMPLATE:
        p.add(f"{path}.message", f"Enter the message (at most {MAX_TEMPLATE} characters)")
        message = ""
    title = raw.get("title")
    if title in (None, ""):
        title = None
    elif not isinstance(title, str) or len(title) > 200:
        p.add(f"{path}.title", "The title must be a text of at most 200 characters")
        title = None
    return {"type": "notify", "action": action, "entity_id": entity_id, "title": title, "message": message.strip()}


def _variables(raw: Any, path: str, p: Problems, limit: int = MAX_DATA_BYTES, ordered: bool = False) -> dict[str, Any]:
    data = raw or {}
    if not isinstance(data, dict) or not _json_size(data) or len(json.dumps(data)) > limit:
        p.add(path, "The variables must be a small JSON object")
        data = {}
    # Generated rules keep their order (Home Assistant renders variables one after the other, so a later one can
    # use an earlier one); a user's variables are sorted, as they always were.
    data = {k: data[k] for k in (data if ordered else sorted(data))}
    for name in data:
        if not VARIABLE_NAME.match(name):
            p.add(path, f"“{name}” is not a variable name (lower-case letters, digits and _)")
        elif name in RESERVED_VARIABLES:
            p.add(path, f"“{name}” is set by Home Assistant itself: choose another name")
    return data


def _action_script(raw: dict[str, Any], path: str, p: Problems, _ctx: _Ctx) -> dict[str, Any]:
    return {"type": "script", "entity_id": _other_entity(raw.get("entity_id"), f"{path}.entity_id", p, "script", "a script"),
            "wait": raw.get("wait") is True, "variables": _variables(raw.get("variables"), f"{path}.variables", p)}


def _action_scene(raw: dict[str, Any], path: str, p: Problems, _ctx: _Ctx) -> dict[str, Any]:
    return {"type": "scene", "entity_id": _other_entity(raw.get("entity_id"), f"{path}.entity_id", p, "scene", "a scene")}


def _action_device(raw: dict[str, Any], path: str, p: Problems, _ctx: _Ctx) -> dict[str, Any]:
    return _device_automation(raw, path, p, "action")


def _action_variables(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    variables = _variables(raw.get("variables"), f"{path}.variables", p, ctx.limits["data"], ordered=ctx.generated)
    if not variables:
        p.add(f"{path}.variables", "Give at least one variable, for example {\"level\": 40}")
    return {"type": "variables", "variables": variables}


def _action_event(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    """Fire an event (generated rules only: the Lighting Designer asks a light's automation to re-evaluate)."""
    if not ctx.generated:
        p.add(path, "Unsupported action type 'event'")
    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or not EVENT_TYPE.match(event_type):
        p.add(f"{path}.event_type", "Enter the event type")
        event_type = ""
    return {"type": "event", "event_type": event_type,
            "event_data": _small_json(raw.get("event_data"), f"{path}.event_data", p, "The event data")}


def _conditions_of(raw: Any, path: str, p: Problems, ctx: _Ctx, *, required: bool) -> dict[str, Any]:
    group = _group(raw, path, p, 1, ctx.conditions, ctx.triggers, ctx.limits["condition_depth"])
    if required and not group["items"]:
        p.add(path, "Add at least one condition")
    return group


def _action_if(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    then = _sequence(raw.get("then"), f"{path}.then", p, ctx, required=True)
    return {"type": "if", "conditions": _conditions_of(raw.get("conditions"), f"{path}.conditions", p, ctx, required=True),
            "then": then, "else": _sequence(raw.get("else"), f"{path}.else", p, ctx, required=False)}


def _action_choose(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    raw_branches = raw.get("branches") if isinstance(raw.get("branches"), list) else []
    if not raw_branches:
        p.add(f"{path}.branches", "Add at least one branch")
    if len(raw_branches) > ctx.limits["branches"]:
        p.add(f"{path}.branches", f"At most {ctx.limits['branches']} branches")
    branches = []
    for i, branch in enumerate(raw_branches[:ctx.limits["branches"]]):
        branch = branch if isinstance(branch, dict) else {}
        branches.append({
            "conditions": _conditions_of(branch.get("conditions"), f"{path}.branches[{i}].conditions", p, ctx, required=True),
            "actions": _sequence(branch.get("actions"), f"{path}.branches[{i}].actions", p, ctx, required=True)})
    return {"type": "choose", "branches": branches,
            "default": _sequence(raw.get("default"), f"{path}.default", p, ctx, required=False)}


def _timeout(raw: dict[str, Any], path: str, p: Problems) -> dict[str, Any]:
    seconds = raw.get("timeout_seconds")
    seconds = None if seconds in (None, "", 0) else _seconds(seconds, f"{path}.timeout_seconds", p, minimum=1)
    on_timeout = raw.get("on_timeout", "continue")
    if on_timeout not in ON_TIMEOUT:
        p.add(f"{path}.on_timeout", "Choose what happens when the time is up")
        on_timeout = "continue"
    return {"timeout_seconds": seconds, "on_timeout": on_timeout if seconds else "continue"}


def _action_wait_trigger(raw: dict[str, Any], path: str, p: Problems, _ctx: _Ctx) -> dict[str, Any]:
    triggers = _triggers(raw.get("triggers"), f"{path}.triggers", p, allowed=WAIT_TRIGGER_TYPES,
                         what="Add what the rule waits for")
    for i, trigger in enumerate(triggers):
        if trigger["type"] == "time" and trigger["weekdays"]:
            p.add(f"{path}.triggers[{i}].weekdays", "A wait cannot be limited to weekdays")
        if trigger["type"] == "button" and trigger["event_types"]:
            p.add(f"{path}.triggers[{i}].event_types", "A wait reacts to any event of the button; to wait for one "
                                                       "kind of press, use the device's own trigger or an event trigger")
    return {"type": "wait_trigger", "triggers": triggers, **_timeout(raw, path, p)}


WAITABLE_CONDITIONS = ("state", "numeric", "template")
_SIMPLE_TEMPLATE = re.compile(r"^\s*\{\{(?P<body>(?:(?!\{\{|\}\}|\{%|%\}).)*)\}\}\s*$", re.S)


def simple_template(template: str) -> str | None:
    """The expression of a template that is exactly one ``{{ … }}``."""
    match = _SIMPLE_TEMPLATE.match(template)
    return match.group("body").strip() if match else None


def _action_wait_condition(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    group = _conditions_of(raw.get("conditions"), f"{path}.conditions", p, ctx, required=True)
    leaves = list(condition_leaves(group))
    for leaf in leaves:
        if leaf["type"] not in WAITABLE_CONDITIONS:
            p.add(f"{path}.conditions", "A wait can watch entity states, numbers and templates; for anything else "
                                        "use “wait for a trigger”")
        elif leaf["type"] == "state" and leaf["for_seconds"]:
            p.add(f"{path}.conditions", "A wait cannot ask how long a state has lasted; use “wait for a trigger” "
                                        "with “and stays so for”")
        elif leaf["type"] == "template" and len(leaves) > 1 and simple_template(leaf["value_template"]) is None:
            p.add(f"{path}.conditions", "A template that is combined with other conditions must be one {{ … }} "
                                        "expression")
    return {"type": "wait_condition", "conditions": group, **_timeout(raw, path, p)}


def _action_repeat(raw: dict[str, Any], path: str, p: Problems, ctx: _Ctx) -> dict[str, Any]:
    mode = raw.get("mode")
    if mode not in REPEAT_MODES:
        p.add(f"{path}.mode", "Choose how often the actions repeat")
        mode = "count"
    count = None
    if mode == "count":
        count = raw.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_REPEAT:
            p.add(f"{path}.count", f"Must be a whole number between 1 and {MAX_REPEAT}")
            count = 2
    conditions = (_conditions_of(raw.get("conditions"), f"{path}.conditions", p, ctx, required=True)
                  if mode != "count" else {"type": "and", "items": []})
    actions = _sequence(raw.get("actions"), f"{path}.actions", p, ctx, required=True)
    if mode != "count" and actions and not sequence_waits(actions):
        p.add(f"{path}.actions", "A repeat “while” or “until” needs a wait in its actions (for example a delay), "
                                 "otherwise it spins without pause")
    return {"type": "repeat", "mode": mode, "count": count, "conditions": conditions, "actions": actions}


ACTION_TYPES = {"service": _action_service, "delay": _action_delay, "notify": _action_notify, "script": _action_script,
                "scene": _action_scene, "device": _action_device, "variables": _action_variables, "if": _action_if,
                "choose": _action_choose, "wait_trigger": _action_wait_trigger,
                "wait_condition": _action_wait_condition, "repeat": _action_repeat, "event": _action_event}
#: The basic actions of Phase 0.1; everything else is offered under "more" in the editor.
BASIC_ACTIONS = ("service", "delay")
#: Sub-sequences of an action, by key.
SEQUENCE_KEYS = {"if": ("then", "else"), "choose": ("default",), "repeat": ("actions",)}


def _sequence(raw: Any, path: str, p: Problems, ctx: _Ctx, *, required: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    raw_actions = raw if isinstance(raw, list) else []
    if required and not raw_actions:
        p.add(path, "Add at least one action" + (" (THEN)" if ctx.depth == 0 else ""))
    if len(raw_actions) > ctx.limits["actions"]:
        p.add(path, f"At most {ctx.limits['actions']} actions in one list")
    if ctx.depth >= ctx.limits["depth"] and raw_actions:
        p.add(path, "Actions are nested too deeply")
        return []
    ctx.depth += 1
    for i, action in enumerate(raw_actions[:ctx.limits["actions"]]):
        kind = action.get("type") if isinstance(action, dict) else None
        ctx.budget[0] -= 1
        if ctx.budget[0] < 0:
            p.add(path, f"At most {ctx.limits['total']} actions per rule")
            break
        if kind not in ACTION_TYPES:
            p.add(f"{path}[{i}]", f"Unsupported action type {kind!r}")
            continue
        actions.append(ACTION_TYPES[kind](action, f"{path}[{i}]", p, ctx))
    ctx.depth -= 1
    if actions and actions[-1]["type"] == "delay":
        p.add(f"{path}[{len(actions) - 1}]", "A delay at the end does nothing: add the action that follows it")
    return actions


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


def _imported_from(raw: Any) -> dict[str, Any] | None:
    """Where an imported rule came from (the original automation is never changed by this App)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("automation_id"), str) or not raw["automation_id"]:
        return None
    return {"automation_id": raw["automation_id"][:120],
            "alias": raw["alias"].strip()[:MAX_STATE] if isinstance(raw.get("alias"), str) else "",
            "allow_both": raw.get("allow_both") is True}


#: Limits of the rules a designer generates (checked like any rule, but they combine several sources by nature).
GENERATED_LIMITS = {"triggers": 100, "conditions": 300, "depth": 6}


def validate_rule(raw: Any, path: str, p: Problems, targets: list[str], *, generated: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        p.add(path, "Must be a rule")
        raw = {}
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not RULE_ID.match(rule_id):
        p.add(f"{path}.id", "Invalid rule id")
        rule_id = ""
    limits = GENERATED_LIMITS if generated else {"triggers": MAX_TRIGGERS, "conditions": MAX_CONDITIONS, "depth": MAX_DEPTH}
    triggers = _triggers(raw.get("triggers"), f"{path}.triggers", p, limit=limits["triggers"])
    ctx = _Ctx(targets, len(triggers), generated)
    actions = _sequence(raw.get("actions"), f"{path}.actions", p, ctx, required=True)
    retrigger = raw.get("retrigger", "restart")
    if retrigger not in RETRIGGER_MODES:
        p.add(f"{path}.retrigger", "Unknown retrigger behaviour")
        retrigger = "restart"
    rule = {
        "id": rule_id,
        "name": _text(raw.get("name"), f"{path}.name", p, what="a name"),
        "enabled": raw.get("enabled") is not False,
        "triggers": triggers,
        "conditions": _group(raw.get("conditions"), f"{path}.conditions", p, 1, [limits["conditions"]], len(triggers),
                             limits["depth"]),
        "actions": actions,
        "retrigger": retrigger,
        "recheck": _recheck(raw.get("recheck"), f"{path}.recheck", p),
    }
    imported = _imported_from(raw.get("imported_from"))
    if imported:  # only present on imported rules: every other rule keeps the document of Phase 0.1
        rule["imported_from"] = imported
    return rule


def _default_rule(raw: Any, path: str, p: Problems, targets: list[str]) -> dict[str, Any] | None:
    """The optional fallback of a device: actions that run when one of the device's triggers fired and no rule matched.

    It has no trigger and no conditions of its own. Its actions must finish at once: it lives in the device's
    shared automation, which handles one trigger after the other.
    """
    if not isinstance(raw, dict):
        return None
    raw_actions = raw.get("actions") if isinstance(raw.get("actions"), list) else []
    override = raw.get("override_entity") or None
    if not raw_actions and not override and raw.get("enabled") is not True:
        return None  # nothing configured
    actions = _sequence(raw_actions, f"{path}.actions", p, _Ctx(targets, 0), required=raw.get("enabled") is not False)
    if sequence_waits(actions):
        p.add(f"{path}.actions", "The default cannot wait (no delay, wait or open-ended repeat): it runs inside the "
                                 "device's shared automation. Use an ordinary rule for a sequence that waits.")
    for action in walk_actions(actions):
        for group in action_groups(action):
            if any(leaf["type"] == "trigger" for leaf in condition_leaves(group)):
                p.add(f"{path}.actions", "The default has no triggers of its own to ask about")
    return {"enabled": raw.get("enabled") is not False, "actions": actions,
            "override_entity": _entity(override, f"{path}.override_entity", p) if override else None}


INPUT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_BLUEPRINT_INPUTS = 80


def _blueprint_settings(raw: Any, path: str, p: Problems) -> dict[str, Any] | None:
    """What the user chose on the blueprint export screen (kept, so the next export has the same inputs)."""
    if not isinstance(raw, dict):
        return None
    inputs: dict[str, dict[str, str]] = {}
    raw_inputs = raw.get("inputs") if isinstance(raw.get("inputs"), dict) else {}
    if len(raw_inputs) > MAX_BLUEPRINT_INPUTS:
        p.add(f"{path}.inputs", f"At most {MAX_BLUEPRINT_INPUTS} inputs")
    for key in sorted(raw_inputs)[:MAX_BLUEPRINT_INPUTS]:
        value = raw_inputs[key] if isinstance(raw_inputs[key], dict) else {}
        if not isinstance(key, str) or not INPUT_KEY.match(key):
            p.add(f"{path}.inputs", f"“{key}” is not an input identifier")
            continue
        inputs[key] = {"name": _text(value.get("name"), f"{path}.inputs.{key}.name", p, required=False),
                       "description": _text(value.get("description"), f"{path}.inputs.{key}.description", p,
                                            limit=300, required=False)}
    automation = raw.get("automation")
    return {"name": _text(raw.get("name"), f"{path}.name", p, required=False),
            "description": _text(raw.get("description"), f"{path}.description", p, limit=1000, required=False),
            "automation": automation if isinstance(automation, str) and len(automation) <= 120 else None,
            "inputs": inputs}


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
    blueprint = _blueprint_settings(raw.get("blueprint"), f"{path}.blueprint", p)
    extra = {"blueprint": blueprint} if blueprint else {}
    default = _default_rule(raw.get("default_rule"), f"{path}.default_rule", p, targets)
    if default:  # only present when the user configured one: every other device keeps its document
        extra["default_rule"] = default
    from . import designers  # here: the designers build on this module
    found = designers.validate_all(raw.get("designers"), targets, f"{path}.designers", p)
    if found:  # only present when the device has a designer (Phase 0.3): every other device keeps its document
        extra["designers"] = found
    return {
        "id": device_id,
        "name": _text(raw.get("name"), f"{path}.name", p, what="a name"),
        "enabled": raw.get("enabled") is not False,
        "targets": targets,
        "ha_device_id": ha_device if isinstance(ha_device, str) and 0 < len(ha_device) <= 64 else None,
        "rules": rules,
        "acknowledged": sorted({a for a in acknowledged if isinstance(a, str) and len(a) <= 120})[:100],
        **extra,
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
    from . import designers
    presence = designers.presence.validate_all(raw.get("presence"), p)
    if presence:  # shared presence sources of the designers (Phase 0.3); absent unless configured
        config["presence"] = presence
    designers.check_references(config, p)
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
        if item["type"] in GROUP_TYPES:
            yield from condition_leaves(item)
        else:
            yield item


def sub_sequences(action: dict[str, Any]):
    """``(key, actions)`` for every list of actions inside an action (branches included)."""
    for key in SEQUENCE_KEYS.get(action["type"], ()):
        yield key, action[key]
    if action["type"] == "choose":
        for i, branch in enumerate(action["branches"]):
            yield f"branches[{i}].actions", branch["actions"]


def action_groups(action: dict[str, Any]):
    """The condition groups an action holds itself (not those of nested actions)."""
    if action["type"] in ("if", "wait_condition") or (action["type"] == "repeat" and action["mode"] != "count"):
        yield action["conditions"]
    if action["type"] == "choose":
        for branch in action["branches"]:
            yield branch["conditions"]


def walk_actions(actions: list[dict[str, Any]]):
    """Every action of a sequence, nested ones included, in order."""
    for action in actions:
        yield action
        for _key, inner in sub_sequences(action):
            yield from walk_actions(inner)


def sequence_waits(actions: list[dict[str, Any]]) -> bool:
    """Does this sequence keep a run alive: a delay, a wait, an open-ended repeat or a script it waits for?"""
    for action in walk_actions(actions):
        if action["type"] in ("delay", "wait_trigger", "wait_condition"):
            return True
        if action["type"] == "repeat" and action["mode"] != "count":
            return True
        if action["type"] == "script" and action["wait"]:
            return True
    return False


def waits(rule: dict[str, Any]) -> bool:
    return sequence_waits(rule["actions"])


def has_delay(rule: dict[str, Any]) -> bool:
    return any(a["type"] == "delay" for a in walk_actions(rule["actions"]))


def is_advanced(rule: dict[str, Any]) -> bool:
    """Does the rule use more than plain actions and delays (branches, waits, repeats, variables …)?"""
    return any(a["type"] not in BASIC_ACTIONS for a in walk_actions(rule["actions"]))


def all_conditions(rule: dict[str, Any]):
    """Every condition leaf of a rule: its own and those inside its actions."""
    yield from condition_leaves(rule["conditions"])
    for action in walk_actions(rule["actions"]):
        for group in action_groups(action):
            yield from condition_leaves(group)


def referenced_entities(device: dict[str, Any]) -> dict[str, set[str]]:
    """Every entity a device's rules read or control, with how it is used (for the missing-entity check)."""
    out: dict[str, set[str]] = {}
    for target in device["targets"]:
        out.setdefault(target, set()).add("controlled")
    for rule in device["rules"]:
        if not rule["enabled"]:
            continue
        for entity_id, uses in rule_entities(rule).items():
            out.setdefault(entity_id, set()).update(uses)
    default = active_default(device)
    if default:
        for entity_id, uses in rule_entities({"triggers": [], "conditions": {"type": "and", "items": []},
                                              "actions": default["actions"],
                                              "recheck": {"override_entity": default["override_entity"]}}).items():
            out.setdefault(entity_id, set()).update({"default" if u != "manual override signal" else u for u in uses})
    return out


def active_default(device: dict[str, Any]) -> dict[str, Any] | None:
    """The device's default rule when it is enabled and has actions."""
    default = device.get("default_rule")
    return default if default and default["enabled"] and default["actions"] and device["enabled"] else None


def rule_entities(rule: dict[str, Any]) -> dict[str, set[str]]:
    """The entities one rule reads or uses besides the device's controlled entities."""
    out: dict[str, set[str]] = {}

    def add(entity_id: Any, use: str) -> None:
        if isinstance(entity_id, str) and ENTITY_ID.match(entity_id):
            out.setdefault(entity_id, set()).add(use)

    for trigger in rule["triggers"]:
        add(trigger.get("entity_id"), "trigger")
        if trigger["type"] == "time":
            add(trigger["at"], "trigger")
    for leaf in all_conditions(rule):
        add(leaf.get("entity_id"), "condition")
    for action in walk_actions(rule["actions"]):
        if action["type"] == "wait_trigger":
            for trigger in action["triggers"]:
                add(trigger.get("entity_id"), "wait")
        elif action["type"] in ("script", "scene", "notify"):
            add(action.get("entity_id"), action["type"])
    add(rule["recheck"]["override_entity"], "manual override signal")
    return out


def action_entities(action: dict[str, Any], targets: list[str]) -> list[str]:
    if action["type"] != "service" or action["target"] == "none":
        return []
    return list(targets) if action["target"] == "all" else list(action["target"])
