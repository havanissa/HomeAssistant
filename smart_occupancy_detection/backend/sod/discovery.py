"""Discovery of likely occupancy sensors (read from Home Assistant's registries and states; nothing is assigned).

Suggestions are based on the domain and the device class, never on entity
names, and they are only suggestions: the user reviews and selects every
sensor. A sensor may belong to several rooms when it is assigned deliberately.
"""

from __future__ import annotations

from typing import Any

from sod_engine.types import DEFAULT_ROLE_FOR_KIND, SensorKind

from .config import KIND_DOMAINS
from .const import SMART_COMFORT_DOMAIN
from .protocol import DOMAIN as OWN_DOMAIN

#: binary_sensor device class -> suggested kind. Many Zigbee PIR sensors report "occupancy" like radars, and bed
#: sensors often do too, so the user confirms the kind (Smart Comfort's discovery rules).
SUGGESTED_KIND = {"motion": "pir", "occupancy": "radar", "presence": "radar", "door": "door", "garage_door": "door"}
#: Shown, but not suggested.
INFORMATIONAL = {"window", "opening"}
OUTPUT_PLATFORMS = frozenset({SMART_COMFORT_DOMAIN, OWN_DOMAIN})
SENSOR_DOMAINS = frozenset(d for domains in KIND_DOMAINS.values() if domains for d in domains)


def _area_of(entry: dict[str, Any], devices: dict[str, dict[str, Any]]) -> str | None:
    if entry.get("area_id"):
        return entry["area_id"]
    device = devices.get(entry.get("device_id") or "")
    return device.get("area_id") if device else None


def _name(entity_id: str, state: dict[str, Any] | None, entry: dict[str, Any] | None) -> str:
    attributes = (state or {}).get("attributes") or {}
    return str(attributes.get("friendly_name") or (entry or {}).get("name") or (entry or {}).get("original_name")
               or entity_id)


def is_output(entity_id: str, registry: dict[str, dict[str, Any]]) -> bool:
    """An occupancy output of this App or of Smart Comfort (never a sensor: it would feed back on itself)."""
    return (registry.get(entity_id) or {}).get("platform") in OUTPUT_PLATFORMS


def candidate(entity_id: str, state: dict[str, Any] | None, entry: dict[str, Any] | None,
              area_names: dict[str, str], devices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    domain = entity_id.split(".", 1)[0]
    device_class = ((state or {}).get("attributes") or {}).get("device_class") or (entry or {}).get(
        "original_device_class")
    kind = SUGGESTED_KIND.get(device_class) if domain == "binary_sensor" else None
    area_id = _area_of(entry or {}, devices) if entry else None
    return {
        "entity_id": entity_id, "name": _name(entity_id, state, entry), "domain": domain,
        "device_class": device_class, "state": (state or {}).get("state"),
        "suggested_kind": kind, "suggested_role": DEFAULT_ROLE_FOR_KIND[SensorKind(kind)].value if kind else None,
        "informational": device_class in INFORMATIONAL, "area_id": area_id, "area": area_names.get(area_id or ""),
    }


def discover(area_id: str, states: dict[str, dict[str, Any]], registry_list: list[dict[str, Any]],
             devices_list: list[dict[str, Any]], areas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Binary sensors of one area: suggested ones first (motion, occupancy, presence, doors), then the rest."""
    devices = {d.get("id"): d for d in devices_list if isinstance(d, dict)}
    area_names = {a.get("area_id"): a.get("name") for a in areas}
    out = []
    for entry in registry_list:
        entity_id = entry.get("entity_id")
        if not isinstance(entity_id, str) or entry.get("disabled_by") or entry.get("entity_category"):
            continue
        if entry.get("platform") in OUTPUT_PLATFORMS or not entity_id.startswith("binary_sensor."):
            continue
        if _area_of(entry, devices) != area_id:
            continue
        out.append(candidate(entity_id, states.get(entity_id), entry, area_names, devices))
    out.sort(key=lambda c: (c["suggested_kind"] is None, c["informational"], c["name"].casefold()))
    return out


def search(query: str, states: dict[str, dict[str, Any]], registry_list: list[dict[str, Any]],
           devices_list: list[dict[str, Any]], areas: list[dict[str, Any]], *, activity: bool,
           limit: int = 40) -> list[dict[str, Any]]:
    """Entities matching ``query`` (id or name) that may be used as a sensor (or, with ``activity``, as an activity
    entity), in any area."""
    devices = {d.get("id"): d for d in devices_list if isinstance(d, dict)}
    area_names = {a.get("area_id"): a.get("name") for a in areas}
    registry = {e.get("entity_id"): e for e in registry_list if isinstance(e, dict)}
    words = [w for w in query.casefold().split() if w]
    out = []
    for entity_id in sorted(set(states) | {e for e in registry if isinstance(e, str)}):
        entry = registry.get(entity_id)
        if entry and (entry.get("disabled_by") or entry.get("platform") in OUTPUT_PLATFORMS):
            continue
        domain = entity_id.split(".", 1)[0]
        if not activity and domain not in SENSOR_DOMAINS:
            continue
        info = candidate(entity_id, states.get(entity_id), entry, area_names, devices)
        haystack = f"{entity_id} {info['name']} {info['area'] or ''}".casefold()
        if all(w in haystack for w in words):
            out.append(info)
            if len(out) >= limit:
                break
    return out
