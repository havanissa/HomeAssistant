"""Detection of existing automations that also control a managed light.

Read only: automations are inspected, never changed, disabled or triggered.

An automation *appears to control* a light when its actions (not its
triggers) reference the light directly, a light group containing it, a
member of it, its device, its area or floor, or one of its labels, and the
actions call a light-related service (``light.*``, ``homeassistant.turn_*``,
``scene.*`` or a script that does). Scenes and scripts called by the
automation are followed. Templated targets cannot be detected.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from .lighting import LightIndex, expand_group
from .occupancy import friendly_name

_LIGHT_SERVICE_PREFIXES = ("light.", "scene.", "script.")
_LIGHT_SERVICES = frozenset({"homeassistant.turn_on", "homeassistant.turn_off", "homeassistant.toggle"})
_MAX_DEPTH = 3


@dataclass(slots=True)
class References:
    entities: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    areas: set[str] = field(default_factory=set)
    floors: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    controls_lights: bool = False
    via_scene: dict[str, str] = field(default_factory=dict)  # light -> scene
    via_script: set[str] = field(default_factory=set)

    def merge(self, other: References) -> None:
        self.entities |= other.entities
        self.devices |= other.devices
        self.areas |= other.areas
        self.floors |= other.floors
        self.labels |= other.labels
        self.controls_lights |= other.controls_lights
        self.via_scene.update(other.via_scene)
        self.via_script |= other.via_script


@dataclass(slots=True, frozen=True)
class AutomationInfo:
    entity_id: str
    name: str | None
    enabled: bool
    refs: References


def _calls_light_service(config: Any) -> bool:
    """Does an action configuration call a light-related service?"""
    stack = [config]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in ("action", "service"):
                value = item.get(key)
                if isinstance(value, str) and (
                    value.startswith(_LIGHT_SERVICE_PREFIXES) or value in _LIGHT_SERVICES
                ):
                    return True
            if item.get("domain") == "light" and "device_id" in item:
                return True  # device action
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _script_refs(script: Any) -> References:
    return References(
        entities=set(getattr(script, "referenced_entities", ()) or ()),
        devices=set(getattr(script, "referenced_devices", ()) or ()),
        areas=set(getattr(script, "referenced_areas", ()) or ()),
        floors=set(getattr(script, "referenced_floors", ()) or ()),
        labels=set(getattr(script, "referenced_labels", ()) or ()),
    )


def _component(hass: HomeAssistant, domain: str) -> Any:
    return hass.data.get(domain)


def _expand(hass: HomeAssistant, refs: References, depth: int, seen: set[str]) -> None:
    """Follow scripts and scenes referenced by the actions."""
    if depth >= _MAX_DEPTH:
        return
    scripts = _component(hass, "script")
    for entity_id in sorted(refs.entities):
        if entity_id in seen:
            continue
        seen.add(entity_id)
        if entity_id.startswith("scene.") and (state := hass.states.get(entity_id)):
            members = state.attributes.get(ATTR_ENTITY_ID) or []
            lights = [m for m in members if isinstance(m, str) and m.startswith("light.")]
            for light in lights:
                refs.via_scene.setdefault(light, entity_id)
            if lights:
                refs.entities |= set(lights)
                refs.controls_lights = True
        elif entity_id.startswith("script.") and scripts is not None:
            entity = scripts.get_entity(entity_id)
            script = getattr(entity, "script", None)
            if script is None:
                continue
            sub = _script_refs(script)
            sub.controls_lights = _calls_light_service(getattr(entity, "raw_config", None))
            _expand(hass, sub, depth + 1, seen)
            if sub.controls_lights:
                refs.via_script.add(entity_id)
            refs.merge(sub)


def collect_automations(hass: HomeAssistant) -> list[AutomationInfo]:
    """Every automation with the targets its actions reference."""
    component = _component(hass, "automation")
    if component is None:
        return []
    out: list[AutomationInfo] = []
    for entity in component.entities:
        script = getattr(entity, "action_script", None)
        refs = _script_refs(script) if script is not None else References(
            entities=set(getattr(entity, "referenced_entities", ()) or ()),
            devices=set(getattr(entity, "referenced_devices", ()) or ()),
            areas=set(getattr(entity, "referenced_areas", ()) or ()),
            floors=set(getattr(entity, "referenced_floors", ()) or ()),
            labels=set(getattr(entity, "referenced_labels", ()) or ()),
        )
        raw = getattr(entity, "raw_config", None) or {}
        refs.controls_lights = _calls_light_service(raw.get("actions", raw.get("action")) if isinstance(raw, dict) else None)
        _expand(hass, refs, 0, set())
        out.append(AutomationInfo(
            entity_id=entity.entity_id,
            name=friendly_name(hass, entity.entity_id),
            enabled=hass.states.is_state(entity.entity_id, STATE_ON),
            refs=refs,
        ))
    return out


@dataclass(slots=True, frozen=True)
class Conflict:
    """An automation that appears to control a managed light."""

    automation: str
    name: str | None
    enabled: bool
    light: str
    via: str  # "entity", "group", "member", "device", "area", "floor", "label", "scene"
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "automation": self.automation,
            "name": self.name,
            "enabled": self.enabled,
            "light": self.light,
            "via": self.via,
            "detail": self.detail,
        }


def find_conflicts(
    hass: HomeAssistant,
    index: LightIndex,
    lights: Iterable[str],
    automations: list[AutomationInfo],
    groups_containing: dict[str, set[str]] | None = None,
) -> dict[str, list[Conflict]]:
    """Automations that appear to control each of the given lights."""
    lights = list(lights)
    if not automations or not lights:
        return {}
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    area_reg = ar.async_get(hass)
    containing = groups_containing if groups_containing is not None else index.groups_containing(lights)
    out: dict[str, list[Conflict]] = {}
    for light in lights:
        related: dict[str, str] = {light: "entity"}
        for member in expand_group(hass, light):
            related.setdefault(member, "member")
        for group in containing.get(light, ()):
            related.setdefault(group, "group")
        devices: dict[str, str] = {}
        areas: dict[str, str] = {}
        labels: dict[str, str] = {}
        for entity_id in related:
            entry = ent_reg.async_get(entity_id)
            if entry is None:
                continue
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            if device:
                devices.setdefault(device.id, device.name_by_user or device.name or device.id)
                for label in device.labels:
                    labels.setdefault(label, label)
            area_id = entry.area_id or (device.area_id if device else None)
            if area_id:
                areas.setdefault(area_id, area_id)
            for label in entry.labels:
                labels.setdefault(label, label)
        floors: dict[str, str] = {}
        for area_id in list(areas):
            if (area := area_reg.async_get_area(area_id)) is not None:
                areas[area_id] = area.name
                if area.floor_id:
                    floors.setdefault(area.floor_id, area.floor_id)

        found: list[Conflict] = []
        for auto in automations:
            refs = auto.refs
            if not refs.controls_lights:
                continue
            hit: tuple[str, str] | None = None
            for entity_id, how in related.items():
                if entity_id in refs.entities:
                    scene = refs.via_scene.get(entity_id)
                    if scene:
                        hit = ("scene", f"scene {scene} includes {entity_id}")
                    elif how == "entity":
                        hit = ("entity", f"targets {entity_id}")
                    else:
                        hit = (how, f"targets {how} {entity_id}")
                    break
            if hit is None and (d := devices.keys() & refs.devices):
                hit = ("device", f"targets device {devices[sorted(d)[0]]}")
            if hit is None and (a := areas.keys() & refs.areas):
                hit = ("area", f"targets area {areas[sorted(a)[0]]}")
            if hit is None and (f := floors.keys() & refs.floors):
                hit = ("floor", f"targets floor {sorted(f)[0]}")
            if hit is None and (lab := labels.keys() & refs.labels):
                hit = ("label", f"targets label {sorted(lab)[0]}")
            if hit is not None:
                found.append(Conflict(auto.entity_id, auto.name, auto.enabled, light, hit[0], hit[1]))
        if found:
            out[light] = sorted(found, key=lambda c: (not c.enabled, c.automation))
    return out
