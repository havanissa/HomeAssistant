"""Automations and scripts as the lighting runtime's conflict detection sees them (Phase A4).

The integration reads Home Assistant's automation and script entities: their
raw configuration and the entities, devices, areas, floors and labels their
actions reference (``Script.referenced_*``). The App reads the raw
configuration of each one (``automation/config`` / ``script/config``) and
computes the references the way Home Assistant's ``Script`` does: service call
targets and data, scenes, conditions in the action sequence, ``choose``, ``if``,
``parallel`` and ``sequence``. Templates reference nothing (as in Home
Assistant).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

_ACTION_KEYS = ("action", "service")


def _templated(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _extract(data: Any, key: str, found: set[str]) -> None:
    if not isinstance(data, dict):
        return
    items = data.get(key)
    if items is None or _templated(items):
        return
    if isinstance(items, str):
        found.add(items)
    elif isinstance(items, (list, tuple)):
        found.update(str(i) for i in items if isinstance(i, str) and not _templated(i))


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _condition_entities(cond: Any, found: set[str]) -> None:
    """Home Assistant's ``condition.async_extract_entities``: entity ids of (nested) conditions."""
    for item in _as_list(cond):
        if isinstance(item, str):
            continue  # a template shorthand
        if not isinstance(item, dict):
            continue
        if item.get("condition") in ("and", "or", "not"):
            _condition_entities(item.get("conditions"), found)
            continue
        _extract(item, "entity_id", found)
        if item.get("condition") == "zone":
            _extract(item, "zone", found)


def _condition_devices(cond: Any, found: set[str]) -> None:
    for item in _as_list(cond):
        if not isinstance(item, dict):
            continue
        if item.get("condition") in ("and", "or", "not"):
            _condition_devices(item.get("conditions"), found)
        elif item.get("condition") == "device" and isinstance(item.get("device_id"), str):
            found.add(item["device_id"])


def _kind(step: dict[str, Any]) -> str:
    if any(k in step for k in _ACTION_KEYS):
        return "call_service"
    for key in ("choose", "if", "parallel", "sequence", "scene", "condition", "repeat"):
        if key in step:
            return key
    if "device_id" in step:
        return "device"
    return "other"


def _walk(steps: Any, visit: Any) -> None:
    for step in _as_list(steps):
        if not isinstance(step, dict):
            continue
        kind = _kind(step)
        visit(kind, step)
        if kind == "choose":
            for choice in _as_list(step.get("choose")):
                if isinstance(choice, dict):
                    visit("conditions", choice.get("conditions"))
                    _walk(choice.get("sequence"), visit)
            if "default" in step:
                _walk(step["default"], visit)
        elif kind == "if":
            visit("conditions", step.get("if"))
            _walk(step.get("then"), visit)
            if "else" in step:
                _walk(step["else"], visit)
        elif kind == "parallel":
            for branch in _as_list(step.get("parallel")):
                _walk(branch.get("sequence") if isinstance(branch, dict) and "sequence" in branch else branch, visit)
        elif kind == "sequence":
            _walk(step.get("sequence"), visit)
        elif kind == "repeat":
            repeat = step.get("repeat") if isinstance(step.get("repeat"), dict) else {}
            _walk(repeat.get("sequence"), visit)


@dataclass(slots=True)
class ScriptRefs:
    """``Script.referenced_*`` of an action sequence."""

    referenced_entities: set[str] = field(default_factory=set)
    referenced_devices: set[str] = field(default_factory=set)
    referenced_areas: set[str] = field(default_factory=set)
    referenced_floors: set[str] = field(default_factory=set)
    referenced_labels: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, steps: Any) -> ScriptRefs:
        refs = cls()

        def visit(kind: str, step: Any) -> None:
            if kind == "conditions":
                _condition_entities(step, refs.referenced_entities)
                _condition_devices(step, refs.referenced_devices)
                return
            if kind == "call_service":
                for data in (step, step.get("target"), step.get("data"), step.get("service_data"),
                             step.get("data_template")):
                    _extract(data, "entity_id", refs.referenced_entities)
                    _extract(data, "device_id", refs.referenced_devices)
                    _extract(data, "area_id", refs.referenced_areas)
                    _extract(data, "floor_id", refs.referenced_floors)
                    _extract(data, "label_id", refs.referenced_labels)
            elif kind == "condition":
                _condition_entities(step, refs.referenced_entities)
                _condition_devices(step, refs.referenced_devices)
            elif kind == "scene" and isinstance(step.get("scene"), str):
                refs.referenced_entities.add(step["scene"])
            elif kind == "device" and isinstance(step.get("device_id"), str):
                refs.referenced_devices.add(step["device_id"])

        _walk(steps, visit)
        return refs


class AutomationEntity:
    def __init__(self, entity_id: str, raw_config: dict[str, Any] | None) -> None:
        self.entity_id = entity_id
        self.raw_config = raw_config or {}
        actions = self.raw_config.get("actions", self.raw_config.get("action"))
        self.action_script = ScriptRefs.of(actions)


class ScriptEntity:
    def __init__(self, entity_id: str, raw_config: dict[str, Any] | None) -> None:
        self.entity_id = entity_id
        self.raw_config = raw_config or {}
        self.script = ScriptRefs.of(self.raw_config.get("sequence"))


class Component:
    """What ``hass.data["automation"]`` / ``hass.data["script"]`` offer the lighting runtime."""

    def __init__(self, entities: Iterable[Any]) -> None:
        self.entities = list(entities)
        self._by_id = {e.entity_id: e for e in self.entities}

    def get_entity(self, entity_id: str) -> Any:
        return self._by_id.get(entity_id)
