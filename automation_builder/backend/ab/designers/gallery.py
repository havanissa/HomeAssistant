"""Templates: starting points for the designers. A template turns on the controls it is about and names the
sources it needs; it never chooses an entity, a value or a default. Everything it leaves open is *unset*, shown
as such, and blocks deployment until the user fills it in or turns that control off again."""

from __future__ import annotations

import copy
from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {"id": "sun_shading", "category": "Covers", "designer": "cover", "name": "Sun-Angle Shading",
     "description": "Each window's orientation (0° = South) and sector; a fixed position or one that follows the sun.",
     "required": ["Per window: direction, sector, lowest sun elevation, response"],
     "optional": ["Light level, outdoor temperature, weather", "Hysteresis, minimum change, minimum interval"],
     "set": {"sun": {"enabled": True}}},
    {"id": "privacy_presets", "category": "Covers", "designer": "cover", "name": "Privacy Presets",
     "description": "Cover modes you name (for example Privacy, View) with a position per cover.",
     "required": ["Mode names", "A mode selector, a mood mapping or timed entries"], "optional": ["Defaults"],
     "set": {"modes": {"enabled": True}}},
    {"id": "cover_moods", "category": "Covers", "designer": "cover", "name": "Cover Mood Matrix",
     "description": "A matrix of covers × cover modes, driven by a selector of its own.",
     "required": ["Mode names", "The cover-mode selector"], "optional": ["Defaults"],
     "set": {"modes": {"enabled": True, "driver": "selector"}}},
    {"id": "mood_linked_covers", "category": "Covers", "designer": "cover", "name": "Mood-Linked Covers",
     "description": "Household moods mapped to cover modes (for example Hygge → Privacy). Unmapped moods do nothing.",
     "required": ["The household mood selector", "The mapping you choose"], "optional": [],
     "set": {"modes": {"enabled": True, "driver": "mood"}}},
    {"id": "scheduled_covers", "category": "Covers", "designer": "cover", "name": "Scheduled Covers",
     "description": "Positions or modes at times of day.", "required": ["Times and what happens"], "optional": ["Weekdays"],
     "set": {"schedule": {"enabled": True}}},
]

# The Lighting Designer's templates (version 2) come from its module; version 1 designers get no templates (they are
# kept and compiled as they are, and can be converted).
from . import lighting2  # noqa: E402

TEMPLATES = [{"category": "Lighting", "designer": "lighting", **{k: v for k, v in t.items() if k != "add"}}
             for t in lighting2.TEMPLATES] + TEMPLATES

PLANNED = [
    {"category": "Climate", "text": "Temperature schedules per room"},
    {"category": "Ventilation", "text": "Humidity, CO₂ and temperature control of fans"},
    {"category": "Fans", "text": "Speed by temperature or humidity"},
]


def for_kinds(kinds: list[str]) -> dict[str, Any]:
    return {"templates": [t for t in TEMPLATES if t["designer"] in kinds],
            "planned": PLANNED,
            "note": "Specialised designers for climate, ventilation and fans are not built yet. Switches, buttons and "
                    "remotes are mapped in the Lighting Designer (Switches & Remotes); for anything else the rule "
                    "editor (IF / AND-OR / THEN) supports them fully."}


def apply(template_id: str, designer: dict[str, Any]) -> dict[str, Any]:
    """The designer with the template's controls turned on (merged into what is there; nothing is bound)."""
    template = next((t for t in TEMPLATES if t["id"] == template_id), None)
    if template is None or template["designer"] != designer["kind"]:
        raise KeyError(template_id)
    if designer["kind"] == "lighting":
        if designer.get("version") != lighting2.VERSION:
            raise KeyError(template_id)  # a version 1 designer: convert it first
        return lighting2.apply_template(template_id, designer)
    out = copy.deepcopy(designer)
    for key, value in template["set"].items():
        if isinstance(value, dict):
            out[key] = {**out.get(key, {}), **value}
        else:
            out[key] = value
    return out
