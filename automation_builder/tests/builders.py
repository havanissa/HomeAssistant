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
