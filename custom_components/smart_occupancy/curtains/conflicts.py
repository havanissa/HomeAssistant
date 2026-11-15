"""Existing automations that may compete for a window's covers (0.6.1). Read only.

An automation *may control* a cover when its actions call a cover-related
service (``cover.*``, ``homeassistant.turn_*``/``toggle``, ``scene.*`` or a
script that does) and reference the cover: directly, through a cover group
containing it or a member of it, its device, its area, or one of its labels,
or through a scene or script the automation runs. Automations are never
changed, disabled, triggered or deleted; this list is only shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ..conflicts import _calls_light_service, _component, _script_refs
from ..occupancy import friendly_name
from .discovery import members

_COVER_PREFIXES = ("cover.", "scene.", "script.")
_MAX_DEPTH = 3


@dataclass(slots=True)
class CoverRefs:
    entities: set[str] = field(default_factory=set)
    devices: set[str] = field(default_factory=set)
    areas: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)
    controls: bool = False
    via: dict[str, str] = field(default_factory=dict)  # cover -> "scene x" / "script y"


@dataclass(slots=True, frozen=True)
class CoverAutomation:
    entity_id: str
    name: str | None
    enabled: bool
    refs: CoverRefs


def _refs(script: Any) -> CoverRefs:
    base = _script_refs(script)
    return CoverRefs(set(base.entities), set(base.devices), set(base.areas), set(base.labels))


def _expand(hass: HomeAssistant, refs: CoverRefs, depth: int, seen: set[str]) -> None:
    if depth >= _MAX_DEPTH:
        return
    scripts = _component(hass, "script")
    for entity_id in sorted(refs.entities):
        if entity_id in seen:
            continue
        seen.add(entity_id)
        if entity_id.startswith("scene.") and (state := hass.states.get(entity_id)):
            covers = [m for m in state.attributes.get(ATTR_ENTITY_ID) or [] if isinstance(m, str)
                      and m.startswith("cover.")]
            for cover in covers:
                refs.via.setdefault(cover, f"scene {entity_id}")
            refs.entities |= set(covers)
        elif entity_id.startswith("script.") and scripts is not None:
            entity = scripts.get_entity(entity_id)
            script = getattr(entity, "script", None)
            if script is None:
                continue
            sub = _refs(script)
            sub.controls = _calls_light_service(getattr(entity, "raw_config", None), _COVER_PREFIXES, "cover")
            _expand(hass, sub, depth + 1, seen)
            if sub.controls:
                for cover in (e for e in sub.entities if e.startswith("cover.")):
                    refs.via.setdefault(cover, f"script {entity_id}")
                refs.entities |= sub.entities
                refs.devices |= sub.devices
                refs.areas |= sub.areas
                refs.labels |= sub.labels
                refs.controls = True


def collect(hass: HomeAssistant) -> list[CoverAutomation]:
    component = _component(hass, "automation")
    if component is None:
        return []
    out = []
    for entity in component.entities:
        script = getattr(entity, "action_script", None)
        refs = _refs(script) if script is not None else CoverRefs()
        raw = getattr(entity, "raw_config", None) or {}
        actions = raw.get("actions", raw.get("action")) if isinstance(raw, dict) else None
        refs.controls = _calls_light_service(actions, _COVER_PREFIXES, "cover")
        _expand(hass, refs, 0, set())
        if refs.controls:
            out.append(CoverAutomation(entity.entity_id, friendly_name(hass, entity.entity_id),
                                       hass.states.is_state(entity.entity_id, STATE_ON), refs))
    return out


def find(hass: HomeAssistant, covers: list[str], automations: list[CoverAutomation]) -> dict[str, list[dict[str, Any]]]:
    """Automations that may control each cover, enabled ones first."""
    if not covers or not automations:
        return {}
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    groups = {s.entity_id: members(hass, s.entity_id) for s in hass.states.async_all("cover")}
    out: dict[str, list[dict[str, Any]]] = {}
    for cover in covers:
        related: dict[str, str] = {cover: "entity"}
        for member in members(hass, cover) - {cover}:
            related.setdefault(member, "member")
        for group, inside in groups.items():
            if group != cover and cover in inside and len(inside) > 1:
                related.setdefault(group, "group")
        devices: dict[str, str] = {}
        areas: set[str] = set()
        labels: set[str] = set()
        for entity_id in related:
            if (entry := ent_reg.async_get(entity_id)) is None:
                continue
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            if device:
                devices.setdefault(device.id, device.name_by_user or device.name or device.id)
                labels |= set(device.labels)
            if area := entry.area_id or (device.area_id if device else None):
                areas.add(area)
            labels |= set(entry.labels)
        found = []
        for auto in automations:
            refs, hit = auto.refs, None
            for entity_id, how in related.items():
                if entity_id in refs.entities:
                    via = refs.via.get(entity_id)
                    hit = ("scene" if via and via.startswith("scene") else "script" if via else how,
                           f"{via} includes {entity_id}" if via else
                           f"targets {entity_id}" if how == "entity" else f"targets {how} {entity_id}")
                    break
            if hit is None and (d := devices.keys() & refs.devices):
                hit = ("device", f"targets device {devices[sorted(d)[0]]}")
            if hit is None and (a := areas & refs.areas):
                hit = ("area", f"targets area {sorted(a)[0]}")
            if hit is None and (lab := labels & refs.labels):
                hit = ("label", f"targets label {sorted(lab)[0]}")
            if hit is not None:
                found.append({"automation": auto.entity_id, "name": auto.name,
                              "enabled": hass.states.is_state(auto.entity_id, STATE_ON),
                              "entity_id": cover, "via": hit[0], "detail": hit[1]})
        if found:
            out[cover] = sorted(found, key=lambda c: (not c["enabled"], c["automation"]))
    return out
