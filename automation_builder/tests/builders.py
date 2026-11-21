"""Short builders for rule-model documents, shared by the App tests and the Home Assistant runtime tests."""

from __future__ import annotations

from typing import Any

from ab import model


def state(entity_id: str, to: Any = None, frm: Any = None, for_seconds: int = 0) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "to": to, "from": frm, "for_seconds": for_seconds}


def numeric(entity_id: str, above: Any = None, below: Any = None, for_seconds: int = 0) -> dict[str, Any]:
    return {"type": "numeric", "entity_id": entity_id, "above": above, "below": below, "for_seconds": for_seconds}


def at(time: str, weekdays: list[str] | None = None) -> dict[str, Any]:
    return {"type": "time", "at": time, "weekdays": weekdays or []}


def sun(event: str = "sunset", offset_minutes: int = 0) -> dict[str, Any]:
    return {"type": "sun", "event": event, "offset_minutes": offset_minutes}


def occupancy(entity_id: str, to: str = "occupied") -> dict[str, Any]:
    return {"type": "occupancy", "entity_id": entity_id, "to": to}


def c_state(entity_id: str, value: str, negate: bool = False, for_seconds: int = 0) -> dict[str, Any]:
    return {"type": "state", "entity_id": entity_id, "state": value, "negate": negate, "for_seconds": for_seconds}


def c_numeric(entity_id: str, above: Any = None, below: Any = None) -> dict[str, Any]:
    return {"type": "numeric", "entity_id": entity_id, "above": above, "below": below}


def c_time(after: str | None = None, before: str | None = None, weekdays: list[str] | None = None) -> dict[str, Any]:
    return {"type": "time", "after": after, "before": before, "weekdays": weekdays or []}


def all_of(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "and", "items": list(items)}


def any_of(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "or", "items": list(items)}


def call(action: str, target: Any = "all", **data: Any) -> dict[str, Any]:
    return {"type": "service", "action": action, "target": target, "data": data}


def delay(seconds: int) -> dict[str, Any]:
    return {"type": "delay", "seconds": seconds}


def rule(number: int, name: str, triggers: list[dict[str, Any]], actions: list[dict[str, Any]],
         conditions: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": f"r_{number:08x}", "name": name, "enabled": True, "triggers": triggers,
            "conditions": conditions or all_of(), "actions": actions, **extra}


def device(number: int, name: str, targets: list[str], rules: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    """A validated (canonical) device; raises ``ConfigError`` with the problems when it is not valid."""
    problems = model.Problems()
    device_id = f"d_{number:08x}"
    clean = model.validate_device(device_id, {"name": name, "targets": targets, "rules": rules, **extra},
                                  f"devices.{device_id}", problems)
    if problems.items:
        raise model.ConfigError("invalid", "invalid device", problems=problems.items)
    return clean


def settings(**occupancy_overrides: Any) -> dict[str, Any]:
    problems = model.Problems()
    clean = model.validate_settings({"occupancy": occupancy_overrides}, problems)
    assert not problems.items, problems.items
    return clean


# ------------------------------------------------------------------ Phase 0.2
def button(entity_id: str, *event_types: str) -> dict[str, Any]:
    return {"type": "button", "entity_id": entity_id, "event_types": list(event_types)}


def event(event_type: str, **event_data: Any) -> dict[str, Any]:
    return {"type": "event", "event_type": event_type, "event_data": event_data}


def device_trigger(label: str = "", **config: Any) -> dict[str, Any]:
    return {"type": "device", "config": config, "label": label}


def mqtt(topic: str, payload: str | None = None) -> dict[str, Any]:
    return {"type": "mqtt", "topic": topic, "payload": payload}


def template(value_template: str, for_seconds: int = 0) -> dict[str, Any]:
    return {"type": "template", "value_template": value_template, "for_seconds": for_seconds}


def pattern(**fields: Any) -> dict[str, Any]:
    return {"type": "pattern", **fields}


def c_template(value_template: str) -> dict[str, Any]:
    return {"type": "template", "value_template": value_template}


def c_trigger(*indexes: int) -> dict[str, Any]:
    return {"type": "trigger", "triggers": list(indexes)}


def c_sun(after: str | None = None, before: str | None = None, after_offset: int = 0, before_offset: int = 0) -> dict[str, Any]:
    return {"type": "sun", "after": after, "before": before, "after_offset_minutes": after_offset,
            "before_offset_minutes": before_offset}


def none_of(*items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "not", "items": list(items)}


def if_(conditions: dict[str, Any], then: list[dict[str, Any]], else_: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "if", "conditions": conditions, "then": then, "else": else_ or []}


def choose(*branches: tuple[dict[str, Any], list[dict[str, Any]]], default: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"type": "choose", "branches": [{"conditions": c, "actions": a} for c, a in branches], "default": default or []}


def wait_trigger(*triggers: dict[str, Any], timeout: int | None = None, on_timeout: str = "continue") -> dict[str, Any]:
    return {"type": "wait_trigger", "triggers": list(triggers), "timeout_seconds": timeout, "on_timeout": on_timeout}


def wait_condition(conditions: dict[str, Any], timeout: int | None = None, on_timeout: str = "continue") -> dict[str, Any]:
    return {"type": "wait_condition", "conditions": conditions, "timeout_seconds": timeout, "on_timeout": on_timeout}


def repeat(actions: list[dict[str, Any]], count: int | None = None, while_: dict[str, Any] | None = None,
           until: dict[str, Any] | None = None) -> dict[str, Any]:
    mode = "count" if count is not None else "while" if while_ is not None else "until"
    return {"type": "repeat", "mode": mode, "count": count, "conditions": while_ or until, "actions": actions}


def variables(**values: Any) -> dict[str, Any]:
    return {"type": "variables", "variables": values}


def notify(action: str, message: str, title: str | None = None, entity_id: str | None = None) -> dict[str, Any]:
    return {"type": "notify", "action": action, "entity_id": entity_id, "title": title, "message": message}


def script(entity_id: str, wait: bool = False, **values: Any) -> dict[str, Any]:
    return {"type": "script", "entity_id": entity_id, "wait": wait, "variables": values}


def scene(entity_id: str) -> dict[str, Any]:
    return {"type": "scene", "entity_id": entity_id}
