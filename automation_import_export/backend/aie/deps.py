"""What an automation refers to: entities, scripts, scenes, helpers, devices, areas, floors, labels, services,
blueprints. Found by walking the configuration (structured references) plus a best-effort scan of templates.

The result is informative: missing references are reported as warnings (an archive restores an automation as
it was, also when something it refers to is gone), except a missing blueprint, which Home Assistant needs to
load the automation at all.
"""

from __future__ import annotations

import re
from typing import Any

REFERENCE_KEYS = {"entity_id": "entities", "device_id": "devices", "area_id": "areas", "floor_id": "floors",
                  "label_id": "labels"}
_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_TEMPLATE_ENTITY = re.compile(r"""(?:states|is_state|state_attr|is_state_attr|has_value|expand)\(\s*['"]([a-z0-9_]+\.[a-z0-9_]+)['"]""")
_TEMPLATE_DOT = re.compile(r"""states\.([a-z0-9_]+)\.([a-z0-9_]+)""")
SCRIPT_SERVICES = {"turn_on", "turn_off", "toggle", "reload"}


def _add(found: dict[str, set[str]], kind: str, value: Any) -> None:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, str) and item and "{{" not in item and "{%" not in item:
            for part in item.split(","):
                part = part.strip()
                if part and part not in ("all", "none"):
                    found[kind].add(part)


def references(config: Any) -> dict[str, Any]:
    found: dict[str, set[str]] = {k: set() for k in ("entities", "devices", "areas", "floors", "labels", "services",
                                                     "template_entities")}
    blueprint: dict[str, Any] | None = None
    templates = 0

    def walk(value: Any, key: str | None = None) -> None:
        nonlocal blueprint, templates
        if isinstance(value, dict):
            for k, v in value.items():
                if k in REFERENCE_KEYS:
                    _add(found, REFERENCE_KEYS[k], v)
                elif k in ("action", "service") and isinstance(v, str) and "." in v and "{{" not in v:
                    found["services"].add(v)
                    domain, _, service = v.partition(".")
                    if domain == "script" and service not in SCRIPT_SERVICES:
                        found["entities"].add(v)
                elif k == "scene" and isinstance(v, str) and v.startswith("scene."):
                    found["entities"].add(v)
                elif k == "use_blueprint" and isinstance(v, dict):
                    blueprint = {"path": v.get("path"), "inputs": sorted(str(i) for i in (v.get("input") or {}))}
                walk(v, k)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and ("{{" in value or "{%" in value):
            templates += 1
            for match in _TEMPLATE_ENTITY.finditer(value):
                found["template_entities"].add(match.group(1))
            for domain, obj in _TEMPLATE_DOT.findall(value):
                found["template_entities"].add(f"{domain}.{obj}")

    walk(config)
    entities = sorted(e for e in found["entities"] if _ENTITY.match(e))
    return {
        "entities": entities,
        "scripts": [e for e in entities if e.startswith("script.")],
        "scenes": [e for e in entities if e.startswith("scene.")],
        "helpers": [e for e in entities if e.split(".", 1)[0].startswith("input_") or e.startswith(
            ("counter.", "timer.", "schedule.", "todo."))],
        "automations": [e for e in entities if e.startswith("automation.")],
        "devices": sorted(found["devices"]),
        "areas": sorted(found["areas"]),
        "floors": sorted(found["floors"]),
        "labels": sorted(found["labels"]),
        "services": sorted(found["services"]),
        "template_entities": sorted(found["template_entities"] - set(entities)),
        "templates": templates,
        "blueprint": blueprint,
    }


def check(refs: dict[str, Any], *, entities: set[str], devices: set[str], areas: set[str], floors: set[str],
          labels: set[str], blueprints: set[str], services: set[str] | None = None) -> list[dict[str, Any]]:
    """Missing references as problems (warnings; a missing blueprint is an error)."""
    out: list[dict[str, Any]] = []

    def missing(kind: str, values: list[str], known: set[str], what: str) -> None:
        gone = [v for v in values if v not in known]
        if gone:
            out.append({"level": "warning", "code": f"missing_{kind}",
                        "message": f"Refers to {what} that does not exist now: {', '.join(gone[:8])}"
                                   + (f" and {len(gone) - 8} more" if len(gone) > 8 else ""),
                        "items": gone})

    missing("entities", refs["entities"], entities, "entities")
    missing("template_entities", refs["template_entities"], entities, "entities (in templates)")
    missing("devices", refs["devices"], devices, "devices")
    missing("areas", refs["areas"], areas, "areas")
    missing("floors", refs["floors"], floors, "floors")
    missing("labels", refs["labels"], labels, "labels")
    if services is not None:
        missing("services", refs["services"], services, "actions")
    blueprint = refs.get("blueprint")
    if blueprint and blueprint.get("path") not in blueprints:
        out.append({"level": "error", "code": "missing_blueprint",
                    "message": f"Uses the blueprint {blueprint.get('path')}, which is not installed",
                    "items": [blueprint.get("path")]})
    return out
