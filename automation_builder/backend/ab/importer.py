"""A conservative importer: an existing Home Assistant automation as a rule of Automation Builder.

``analyse`` reads the raw configuration of one automation and reports

* what it consists of (triggers, conditions, actions, the entities it controls),
* what can be represented faithfully in the rule model (``supported``),
* what cannot (``unsupported``, each with the place and the reason),
* which managed devices and rules it would overlap with.

A conversion is offered only when **nothing** is unsupported: every parser here
accepts exactly the keys it understands, and anything else makes the automation
"not convertible" instead of being dropped. The result is a copy: the original
automation is never changed, turned off or deleted by this App (it has no way
to do so), and the copy remembers where it came from so that a deployment can
warn while the original is still running (``core.plan``).
"""

from __future__ import annotations

import re
from typing import Any

from . import compiler, conflicts, describe, model

_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_OFFSET = re.compile(r"^(-?)(\d{1,2}):(\d{2})(?::(\d{2}))?$")
TEMPLATE_MARKS = ("{{", "{%")
COSMETIC = ("alias", "metadata")
NOT_READY = {"unavailable", "unknown"}


def _is_template(value: Any) -> bool:
    return isinstance(value, str) and any(m in value for m in TEMPLATE_MARKS)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


class _Unsupported(Exception):
    def __init__(self, what: str, why: str) -> None:
        super().__init__(why)
        self.what = what
        self.why = why


class _Reader:
    def __init__(self) -> None:
        self.unsupported: list[dict[str, str]] = []
        self.supported: list[str] = []
        self.notes: list[str] = []
        self.controlled: list[str] = []
        self.trigger_ids: dict[str, list[int]] = {}
        self.triggers: list[dict[str, Any]] = []

    def no(self, path: str, what: str, why: str) -> None:
        self.unsupported.append({"path": path, "what": what, "why": why})

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # ------------------------------------------------------------ helpers
    def keys(self, item: dict[str, Any], allowed: tuple[str, ...], what: str) -> dict[str, Any]:
        """The item without cosmetic keys; any key that is not understood stops the conversion of this item."""
        if item.get("enabled") is False:
            raise _Unsupported(what, "it is disabled in the automation (a copy cannot keep a disabled step; remove "
                                     "it from the original or enable it)")
        if any(k in item for k in COSMETIC):
            self.note("Names given to single steps (alias) are not kept; the rule shows its own description.")
        extra = sorted(k for k in item if k not in allowed and k not in COSMETIC and k != "enabled")
        if extra:
            raise _Unsupported(what, f"uses {', '.join(extra)}, which the rule editor has no field for")
        return item

    @staticmethod
    def seconds(value: Any, what: str) -> int:
        if value in (None, ""):
            return 0
        if _is_template(value):
            raise _Unsupported(what, "the duration is a template")
        if isinstance(value, bool):
            raise _Unsupported(what, "the duration is not a time")
        if isinstance(value, (int, float)):
            total = float(value)
        elif isinstance(value, dict):
            if set(value) - {"days", "hours", "minutes", "seconds", "milliseconds"} or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) for v in value.values()):
                raise _Unsupported(what, "the duration is not a plain time")
            total = (value.get("days", 0) * 86400 + value.get("hours", 0) * 3600 + value.get("minutes", 0) * 60
                     + value.get("seconds", 0) + value.get("milliseconds", 0) / 1000)
        elif isinstance(value, str) and _TIME.match(value):
            h, m, s = _TIME.match(value).groups()  # type: ignore[union-attr]
            total = int(h) * 3600 + int(m) * 60 + int(s or 0)
        else:
            raise _Unsupported(what, f"the duration {value!r} is not understood")
        if total != int(total) or not 0 <= total <= model.MAX_SECONDS:
            raise _Unsupported(what, "the duration is not a whole number of seconds up to 24 hours")
        return int(total)

    @staticmethod
    def offset_minutes(value: Any, what: str) -> int:
        if value in (None, "", 0):
            return 0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = float(value)
        elif isinstance(value, str) and _OFFSET.match(value):
            sign, h, m, s = _OFFSET.match(value).groups()  # type: ignore[union-attr]
            seconds = (int(h) * 3600 + int(m) * 60 + int(s or 0)) * (-1 if sign else 1)
        else:
            raise _Unsupported(what, f"the offset {value!r} is not understood")
        if seconds % 60:
            raise _Unsupported(what, "the offset is not a whole number of minutes")
        return int(seconds // 60)

    @staticmethod
    def entities(value: Any, what: str) -> list[str]:
        out = []
        for entity_id in _list(value):
            if not isinstance(entity_id, str) or not model.ENTITY_ID.match(entity_id):
                raise _Unsupported(what, f"{entity_id!r} is not a single entity (a template or a group expression)")
            out.append(entity_id)
        return out

    # ----------------------------------------------------------- triggers
    def trigger(self, raw: Any, what: str, wait: bool = False) -> list[dict[str, Any]]:
        if not isinstance(raw, dict):
            raise _Unsupported(what, "it is not a trigger")
        raw = dict(raw)
        platform = raw.pop("trigger", None) or raw.pop("platform", None)
        raw.pop("id", None)
        if "variables" in raw:
            raise _Unsupported(what, "it sets variables")
        if platform == "state":
            t = self.keys(raw, ("entity_id", "from", "to", "for", "attribute", "not_from", "not_to"), what)
            entities = self.entities(t.get("entity_id"), what)
            if "not_from" in t or "not_to" in t:
                excluded = set(_list(t.get("not_from"))) | set(_list(t.get("not_to")))
                if (excluded <= NOT_READY and "from" not in t and "to" not in t and "for" not in t
                        and "attribute" not in t and all(e.startswith("event.") for e in entities)):
                    return [{"type": "button", "entity_id": e, "event_types": []} for e in entities]
                raise _Unsupported(what, "it excludes states (not_from / not_to)")
            for key in ("from", "to"):
                if isinstance(t.get(key), list):
                    raise _Unsupported(what, f"“{key}” lists several states")
            attribute = t.get("attribute")
            if "to" in t and t["to"] is None and "from" not in t:
                t = {k: v for k, v in t.items() if k != "to"}  # "to: null" = any change of the state itself
            elif "from" not in t and "to" not in t and not attribute:
                raise _Unsupported(what, "it fires on every change, attribute changes included; the rule editor's "
                                         "state trigger reacts to the state itself")
            return [{"type": "state", "entity_id": e, "to": t.get("to"), "from": t.get("from"),
                     "for_seconds": self.seconds(t.get("for"), what), "attribute": attribute} for e in entities]
        if platform == "numeric_state":
            t = self.keys(raw, ("entity_id", "above", "below", "for", "attribute"), what)
            for key in ("above", "below"):
                if isinstance(t.get(key), str):
                    raise _Unsupported(what, f"“{key}” refers to another entity or is a text")
            return [{"type": "numeric", "entity_id": e, "above": t.get("above"), "below": t.get("below"),
                     "for_seconds": self.seconds(t.get("for"), what), "attribute": t.get("attribute")}
                    for e in self.entities(t.get("entity_id"), what)]
        if platform == "time":
            t = self.keys(raw, ("at", "weekday"), what)
            if wait and t.get("weekday"):
                raise _Unsupported(what, "a wait cannot be limited to weekdays")
            out = []
            for at in _list(t.get("at")):
                if not isinstance(at, str):
                    raise _Unsupported(what, "the time uses an offset or is not a time of day")
                out.append({"type": "time", "at": at, "weekdays": _list(t.get("weekday"))})
            return out
        if platform == "sun":
            t = self.keys(raw, ("event", "offset"), what)
            return [{"type": "sun", "event": t.get("event"), "offset_minutes": self.offset_minutes(t.get("offset"), what)}]
        if platform == "event":
            t = self.keys(raw, ("event_type", "event_data"), what)
            return [{"type": "event", "event_type": e, "event_data": t.get("event_data") or {}}
                    for e in _list(t.get("event_type"))]
        if platform == "device":
            config = {k: v for k, v in raw.items() if k not in COSMETIC and k != "enabled"}
            self.keys(raw, tuple(raw), what)
            label = " ".join(str(config.get(k)) for k in ("type", "subtype") if config.get(k))
            return [{"type": "device", "config": config, "label": f"device trigger {label}"}]
        if platform == "mqtt":
            t = self.keys(raw, ("topic", "payload"), what)
            return [{"type": "mqtt", "topic": t.get("topic"), "payload": t.get("payload")}]
        if platform == "template":
            t = self.keys(raw, ("value_template", "for"), what)
            return [{"type": "template", "value_template": t.get("value_template"),
                     "for_seconds": self.seconds(t.get("for"), what)}]
        if platform == "time_pattern":
            t = self.keys(raw, ("hours", "minutes", "seconds"), what)
            return [{"type": "pattern", **{k: t.get(k) for k in ("hours", "minutes", "seconds")}}]
        if platform == "homeassistant":
            t = self.keys(raw, ("event",), what)
            return [{"type": "homeassistant", "event": t.get("event")}]
        raise _Unsupported(what, f"the trigger type “{platform}” is not available in the rule editor")

    # --------------------------------------------------------- conditions
    def group(self, raw: Any, path: str, kind: str = "and") -> dict[str, Any]:
        items = []
        for i, item in enumerate(_list(raw)):
            try:
                items.append(self.condition(item, f"{path}[{i}]"))
            except _Unsupported as err:
                self.no(f"{path}[{i}]", err.what, err.why)
        return {"type": kind, "items": items}

    def condition(self, raw: Any, path: str) -> dict[str, Any]:
        what = f"condition {path}"
        if isinstance(raw, str):
            if _is_template(raw):
                return {"type": "template", "value_template": raw}
            raise _Unsupported(what, "it is not a condition")
        if not isinstance(raw, dict):
            raise _Unsupported(what, "it is not a condition")
        raw = dict(raw)
        for short in ("and", "or", "not"):
            if short in raw and "condition" not in raw:
                self.keys(raw, (short,), what)
                return self.group(raw[short], path, short)
        kind = raw.pop("condition", None)
        if kind in ("and", "or", "not"):
            c = self.keys(raw, ("conditions",), what)
            return self.group(c.get("conditions"), path, kind)
        if kind == "state":
            c = self.keys(raw, ("entity_id", "state", "for", "attribute", "match"), what)
            states = _list(c.get("state"))
            if any(isinstance(s, bool) or not isinstance(s, (str, int, float)) for s in states) or not states:
                raise _Unsupported(what, "the state is not a plain value")
            per_entity = []
            for entity_id in self.entities(c.get("entity_id"), what):
                leaves = [{"type": "state", "entity_id": entity_id, "state": str(s), "negate": False,
                           "for_seconds": self.seconds(c.get("for"), what), "attribute": c.get("attribute")}
                          for s in states]
                per_entity.append(leaves[0] if len(leaves) == 1 else {"type": "or", "items": leaves})
            if len(per_entity) == 1:
                return per_entity[0]
            return {"type": "or" if c.get("match") == "any" else "and", "items": per_entity}
        if kind == "numeric_state":
            c = self.keys(raw, ("entity_id", "above", "below", "attribute"), what)
            for key in ("above", "below"):
                if isinstance(c.get(key), str):
                    raise _Unsupported(what, f"“{key}” refers to another entity or is a text")
            leaves = [{"type": "numeric", "entity_id": e, "above": c.get("above"), "below": c.get("below"),
                       "attribute": c.get("attribute")} for e in self.entities(c.get("entity_id"), what)]
            return leaves[0] if len(leaves) == 1 else {"type": "and", "items": leaves}
        if kind == "time":
            c = self.keys(raw, ("after", "before", "weekday"), what)
            for key in ("after", "before"):
                if c.get(key) is not None and not (isinstance(c[key], str) and _TIME.match(c[key])):
                    raise _Unsupported(what, f"“{key}” is not a fixed time of day")
            return {"type": "time", "after": c.get("after"), "before": c.get("before"), "weekdays": _list(c.get("weekday"))}
        if kind == "template":
            c = self.keys(raw, ("value_template",), what)
            return {"type": "template", "value_template": c.get("value_template")}
        if kind == "trigger":
            c = self.keys(raw, ("id",), what)
            indexes: list[int] = []
            for trigger_id in _list(c.get("id")):
                if str(trigger_id) not in self.trigger_ids:
                    raise _Unsupported(what, f"it refers to the trigger id “{trigger_id}”, which no trigger has")
                indexes += self.trigger_ids[str(trigger_id)]
            return {"type": "trigger", "triggers": sorted(set(indexes))}
        if kind == "device":
            config = {k: v for k, v in raw.items() if k not in COSMETIC and k != "enabled"}
            self.keys(raw, tuple(raw), what)
            return {"type": "device", "config": config, "label": f"device condition {config.get('type', '')}".strip()}
        if kind == "sun":
            c = self.keys(raw, ("after", "before", "after_offset", "before_offset"), what)
            return {"type": "sun", "after": c.get("after"), "before": c.get("before"),
                    "after_offset_minutes": self.offset_minutes(c.get("after_offset"), what),
                    "before_offset_minutes": self.offset_minutes(c.get("before_offset"), what)}
        raise _Unsupported(what, f"the condition type “{kind}” is not available in the rule editor")

    # ------------------------------------------------------------ actions
    def control(self, entity_ids: list[str]) -> None:
        for entity_id in entity_ids:
            if entity_id not in self.controlled:
                self.controlled.append(entity_id)

    def sequence(self, raw: Any, path: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        items = _list(raw)
        for i, item in enumerate(items):
            what = f"action {path}[{i}]"
            if isinstance(item, dict) and "condition" in item and not any(k in item for k in ("action", "service")):
                # A condition between actions stops the run when it is false: "if it holds, do the rest".
                rest = self.sequence(items[i + 1:], f"{path}[{i}+]")
                group = {"type": "and", "items": [self.condition(item, f"{path}[{i}]")]}
                if rest:
                    out.append({"type": "if", "conditions": group, "then": rest, "else": []})
                    self.note("A condition between the actions became “if … then” around the actions after it.")
                else:
                    self.note("A condition after the last action had no effect and was left out.")
                break
            try:
                out.append(self.action(item, f"{path}[{i}]", what))
            except _Unsupported as err:  # every unsupported action is reported, not only the first
                self.no(f"{path}[{i}]", err.what, err.why)
        return out

    def action(self, raw: Any, path: str, what: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise _Unsupported(what, "it is not an action")
        raw = dict(raw)
        if "delay" in raw:
            a = self.keys(raw, ("delay",), what)
            seconds = self.seconds(a["delay"], what)
            if seconds < 1:
                raise _Unsupported(what, "the delay is shorter than one second")
            return {"type": "delay", "seconds": seconds}
        if "wait_template" in raw:
            a = self.keys(raw, ("wait_template", "timeout", "continue_on_timeout"), what)
            return {"type": "wait_condition",
                    "conditions": {"type": "and", "items": [{"type": "template", "value_template": a["wait_template"]}]},
                    **self.timeout(a, what)}
        if "wait_for_trigger" in raw:
            a = self.keys(raw, ("wait_for_trigger", "timeout", "continue_on_timeout"), what)
            triggers = [t for item in _list(a["wait_for_trigger"]) for t in self.trigger(item, what, wait=True)]
            return {"type": "wait_trigger", "triggers": triggers, **self.timeout(a, what)}
        if "choose" in raw:
            a = self.keys(raw, ("choose", "default"), what)
            branches = []
            for i, branch in enumerate(_list(a["choose"])):
                if not isinstance(branch, dict):
                    raise _Unsupported(what, "a branch is not understood")
                b = self.keys(dict(branch), ("conditions", "sequence"), what)
                branches.append({"conditions": self.group(b.get("conditions"), f"{path}.choose[{i}].conditions"),
                                 "actions": self.sequence(b.get("sequence"), f"{path}.choose[{i}]")})
            return {"type": "choose", "branches": branches, "default": self.sequence(a.get("default"), f"{path}.default")}
        if "if" in raw:
            a = self.keys(raw, ("if", "then", "else"), what)
            return {"type": "if", "conditions": self.group(a["if"], f"{path}.if"),
                    "then": self.sequence(a.get("then"), f"{path}.then"), "else": self.sequence(a.get("else"), f"{path}.else")}
        if "repeat" in raw:
            a = self.keys(raw, ("repeat",), what)
            repeat = a["repeat"] if isinstance(a["repeat"], dict) else {}
            r = self.keys(dict(repeat), ("count", "while", "until", "sequence"), what)
            modes = [m for m in ("count", "while", "until") if m in r]
            if len(modes) != 1:
                raise _Unsupported(what, "the repeat is not a count, a “while” or an “until”")
            if modes[0] == "count" and (isinstance(r["count"], bool) or not isinstance(r["count"], int)):
                raise _Unsupported(what, "the repeat count is a template")
            return {"type": "repeat", "mode": modes[0], "count": r.get("count"),
                    "conditions": self.group(r.get(modes[0]), f"{path}.repeat") if modes[0] != "count" else None,
                    "actions": self.sequence(r.get("sequence"), f"{path}.repeat")}
        if "variables" in raw:
            a = self.keys(raw, ("variables",), what)
            return {"type": "variables", "variables": a["variables"]}
        if "device_id" in raw and "action" not in raw and "service" not in raw:
            config = {k: v for k, v in raw.items() if k not in COSMETIC and k != "enabled"}
            self.keys(raw, tuple(raw), what)
            return {"type": "device", "config": config, "label": f"device action {config.get('type', '')}".strip()}
        if "action" in raw or "service" in raw:
            return self.service(raw, what)
        kind = next((k for k in ("stop", "event", "parallel", "sequence", "set_conversation_response", "scene")
                     if k in raw), None)
        raise _Unsupported(what, f"the action “{kind or ', '.join(sorted(raw))}” is not available in the rule editor")

    def timeout(self, a: dict[str, Any], what: str) -> dict[str, Any]:
        seconds = self.seconds(a.get("timeout"), what) if "timeout" in a else 0
        if "timeout" in a and seconds < 1:
            raise _Unsupported(what, "the timeout is shorter than one second")
        return {"timeout_seconds": seconds or None,
                "on_timeout": "stop" if seconds and a.get("continue_on_timeout") is False else "continue"}

    def service(self, raw: dict[str, Any], what: str) -> dict[str, Any]:
        a = self.keys(raw, ("action", "service", "target", "data", "entity_id"), what)
        name = a.get("action") or a.get("service")
        if not isinstance(name, str) or _is_template(name) or not model.ACTION_NAME.match(name):
            raise _Unsupported(what, "the action name is a template or not an action")
        target = a.get("target") or {}
        data = dict(a.get("data") or {}) if isinstance(a.get("data") or {}, dict) else None
        if data is None or not isinstance(target, dict):
            raise _Unsupported(what, "its data or target is a template")
        other = sorted(k for k in target if k != "entity_id")
        if other:
            raise _Unsupported(what, f"it targets {', '.join(other)}: Automation Builder controls explicitly "
                                     "selected entities, never a whole area, device or label")
        entities = self.entities(_list(target.get("entity_id")) + _list(data.pop("entity_id", None))
                                 + _list(a.get("entity_id")), what)
        entities = list(dict.fromkeys(entities))
        domain, _, service = name.partition(".")
        if domain in ("notify", "persistent_notification") and isinstance(data.get("message"), str) \
                and set(data) <= {"message", "title"} and (not entities or (name == "notify.send_message" and len(entities) == 1)):
            return {"type": "notify", "action": name, "entity_id": entities[0] if entities else None,
                    "title": data.get("title"), "message": data["message"]}
        if name == "scene.turn_on" and len(entities) == 1 and not data:
            return {"type": "scene", "entity_id": entities[0]}
        if name == "script.turn_on" and len(entities) == 1 and set(data) <= {"variables"}:
            return {"type": "script", "entity_id": entities[0], "wait": False, "variables": data.get("variables") or {}}
        if domain == "script" and service not in ("turn_on", "turn_off", "toggle", "reload") and not entities:
            return {"type": "script", "entity_id": name, "wait": True, "variables": data}
        if not entities:
            return {"type": "service", "action": name, "target": "none", "data": data}
        wrong = [e for e in entities if domain != "homeassistant" and e.split(".", 1)[0] != domain]
        if wrong:
            raise _Unsupported(what, f"{name} is called on {', '.join(wrong)}, which is not a {domain} entity")
        self.control(entities)
        return {"type": "service", "action": name, "target": entities, "data": data}


def _fix_targets(actions: list[dict[str, Any]], controlled: list[str]) -> None:
    for action in model_walk(actions):
        if action.get("type") == "service" and isinstance(action.get("target"), list) \
                and set(action["target"]) == set(controlled):
            action["target"] = "all"


def model_walk(actions: list[dict[str, Any]]):
    """Like ``model.walk_actions`` for documents that are not validated yet."""
    for action in actions:
        yield action
        for key in ("then", "else", "default", "actions"):
            if isinstance(action.get(key), list):
                yield from model_walk(action[key])
        for branch in action.get("branches") or []:
            yield from model_walk(branch.get("actions") or [])


def analyse(config: Any, *, entity_id: str, state: dict[str, Any] | None, devices: dict[str, dict[str, Any]],
            settings: dict[str, Any], names: Any = None) -> dict[str, Any]:
    """What an automation consists of, what of it can be converted, and the converted rule when all of it can."""
    names = names or (lambda e: e)
    reader = _Reader()
    attributes = (state or {}).get("attributes") or {}
    automation = {"entity_id": entity_id, "id": attributes.get("id"), "state": (state or {}).get("state"),
                  "alias": (config or {}).get("alias") if isinstance(config, dict) else None,
                  "name": attributes.get("friendly_name") or entity_id, "mode": "single"}
    result: dict[str, Any] = {"automation": automation, "supported": reader.supported, "unsupported": reader.unsupported,
                              "notes": reader.notes, "controlled": [], "rule": None, "summary": None, "yaml": "",
                              "overlaps": [], "destinations": [], "can_convert": False, "counts": {}}
    if not isinstance(config, dict):
        reader.no("", "the automation", "Home Assistant did not return its configuration (it may be defined in a "
                                        "package or be unavailable)")
        return result
    if "use_blueprint" in config:
        reader.no("use_blueprint", "the automation", "it is made from a blueprint; its triggers and actions live in "
                                                     "the blueprint, not in the automation")
        return result
    if not automation["id"]:
        reader.no("id", "the automation", "it has no id (it is written by hand in YAML without one), so it cannot "
                                          "be told apart from its copy later")
    known = {"id", "alias", "description", "trigger", "triggers", "condition", "conditions", "action", "actions",
             "mode", "max", "max_exceeded", "trace", "initial_state"}
    for key in sorted(set(config) - known):
        reader.no(key, f"“{key}”", "automation-level setting that the rule model does not have")
    if config.get("initial_state") is False:
        reader.note("The original is set to start turned off (initial_state); the copy has no such setting.")
    mode = config.get("mode", "single")
    automation["mode"] = mode
    if mode not in model.RETRIGGER_MODES:
        reader.no("mode", f"mode {mode}", "unknown execution mode")
        mode = "single"

    # triggers
    raw_triggers = _list(config.get("triggers", config.get("trigger")))
    for i, raw in enumerate(raw_triggers):
        what = f"trigger {i + 1}"
        try:
            found = reader.trigger(raw, what)
        except _Unsupported as err:
            reader.no(f"triggers[{i}]", err.what, err.why)
            continue
        first = len(reader.triggers)
        fresh = [t for t in found if t not in reader.triggers]
        reader.triggers += fresh
        trigger_id = raw.get("id") if isinstance(raw, dict) else None
        if trigger_id is not None:
            reader.trigger_ids.setdefault(str(trigger_id), []).extend(range(first, first + len(fresh)))
    if not raw_triggers:
        reader.no("triggers", "the automation", "it has no trigger")
    # conditions
    conditions = {"type": "and", "items": []}
    for i, raw in enumerate(_list(config.get("conditions", config.get("condition")))):
        try:
            conditions["items"].append(reader.condition(raw, f"conditions[{i}]"))
        except _Unsupported as err:
            reader.no(f"conditions[{i}]", err.what, err.why)
    # actions
    raw_actions = _list(config.get("actions", config.get("action")))
    actions: list[dict[str, Any]] = []
    try:
        actions = reader.sequence(raw_actions, "actions")
    except _Unsupported as err:
        reader.no("actions", err.what, err.why)
    if not raw_actions:
        reader.no("actions", "the automation", "it has no action")
    result["controlled"] = [{"entity_id": e, "name": names(e)} for e in reader.controlled]
    result["counts"] = {"triggers": len(raw_triggers), "conditions": len(_list(config.get("conditions", config.get("condition")))),
                        "actions": len(raw_actions)}
    if not reader.unsupported and not reader.controlled:
        reader.no("actions", "the automation", "none of its actions names an entity to control; a managed device "
                                               "needs at least one controlled entity")
    if reader.unsupported:
        return result

    _fix_targets(actions, reader.controlled)
    name = (config.get("alias") or automation["name"] or "Imported rule")[:model.MAX_NAME]
    rule = {"id": "r_00000000", "name": name, "enabled": True, "triggers": reader.triggers, "conditions": conditions,
            "actions": actions, "retrigger": mode,
            "imported_from": {"automation_id": str(automation["id"]), "alias": name, "allow_both": False}}
    problems = model.Problems()
    draft = model.validate_device("d_00000000", {"name": name, "targets": reader.controlled, "rules": [rule]},
                                  "device", problems)
    if problems.items:
        for problem in problems.items:
            reader.no(problem["path"].removeprefix("device.rules[0].").removeprefix("device."), "the converted rule",
                      problem["message"])
        return result
    clean = draft["rules"][0]
    compiled = compiler.compile_device(draft, settings)
    if not model.waits(clean) and mode != "queued":
        reader.note(f"The original runs in mode “{mode}”. Its actions finish at once, so the mode only matters when "
                    "two triggers arrive in the same instant; as a rule it joins its device's automation, which "
                    "handles triggers one after the other.")
    if model.waits(clean):
        reader.note(f"The rule waits, so it keeps its own automation in mode “{mode}”, like the original.")
    if any(k in config for k in ("max", "max_exceeded", "trace")):
        reader.note("The settings max, max_exceeded and trace of the original are not copied; Automation Builder "
                    "sets its own.")
    if config.get("description"):
        reader.note("The description of the original is not copied.")
    summary = describe.rule(clean, names, draft["targets"])
    reader.supported += [f"IF {summary['if']}"] + ([f"AND {summary['and']}"] if summary["and"] else []) + [f"THEN {summary['then']}"]
    result.update(rule=clean, summary=summary, can_convert=True,
                  yaml="".join(compiler.to_yaml(compiler.with_id(a)) for a in compiled["automations"]))
    # overlaps with what is managed already
    controlled = set(reader.controlled)
    for device in devices.values():
        shared = sorted(controlled & set(device["targets"]))
        if controlled <= set(device["targets"]):
            result["destinations"].append({"device_id": device["id"], "name": device["name"]})
        for existing in device["rules"]:
            if (existing.get("imported_from") or {}).get("automation_id") == str(automation["id"]):
                result["overlaps"].append({"level": "warning", "device_id": device["id"], "rule_id": existing["id"],
                                           "message": f"This automation was imported before, as the rule "
                                                      f"“{existing['name']}” of “{device['name']}”."})
            elif shared and conflicts._shared_trigger(existing, clean) is not None:  # noqa: SLF001
                result["overlaps"].append({"level": "warning", "device_id": device["id"], "rule_id": existing["id"],
                                           "message": f"The rule “{existing['name']}” of “{device['name']}” starts "
                                                      f"on the same trigger and controls {', '.join(names(e) for e in shared)} too."})
        if shared and not any(o.get("device_id") == device["id"] for o in result["overlaps"]):
            result["overlaps"].append({"level": "info", "device_id": device["id"], "rule_id": None,
                                       "message": f"“{device['name']}” already controls {', '.join(names(e) for e in shared)}."})
    return result
