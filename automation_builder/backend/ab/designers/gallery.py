"""Templates: starting points for the designers. A template turns on the controls it is about and names the
sources it needs; it never chooses an entity, a value or a default. Everything it leaves open is *unset*, shown
as such, and blocks deployment until the user fills it in or turns that control off again."""

from __future__ import annotations

import copy
from typing import Any

TEMPLATES: list[dict[str, Any]] = [
    {"id": "mood_lighting", "category": "Lighting", "designer": "lighting", "name": "Mood Lighting",
     "description": "Each light gets its own setting per mood. The lights change when the mood changes.",
     "required": ["A mood selector (a dropdown helper or select)"], "optional": ["Defaults per light or for the matrix"],
     "set": {"primary": "mood", "mood": {"enabled": True}}},
    {"id": "presence_lighting", "category": "Lighting", "designer": "lighting", "name": "Presence Lighting",
     "description": "Lights react to rooms and zones becoming occupied or vacant, with delays you choose.",
     "required": ["At least one presence source (room, zone, position zone or person)"],
     "optional": ["Light-level permission", "Mood palette for “Use mood” cells"],
     "set": {"primary": "presence", "presence": {"enabled": True}}},
    {"id": "lux_lighting", "category": "Lighting", "designer": "lighting", "name": "Lux-Adaptive Lighting",
     "description": "Light-level bands you name and limit decide what the lights do.",
     "required": ["A light-level sensor", "Band names and limits"], "optional": ["Hysteresis and delay"],
     "set": {"primary": "lux", "lux": {"enabled": True}}},
    {"id": "combined_lighting", "category": "Lighting", "designer": "lighting", "name": "Combined Mood + Presence + Lux",
     "description": "Presence decides, the mood supplies the settings (cells “Use mood”), the light level permits.",
     "required": ["Presence sources", "A mood selector", "A light-level sensor and bands"],
     "optional": ["“Re-apply when the mood changes”", "“Also act when the light level changes”"],
     "set": {"primary": "presence", "presence": {"enabled": True}, "mood": {"enabled": True}, "lux": {"enabled": True}}},
    {"id": "zone_lighting", "category": "Lighting", "designer": "lighting", "name": "Zone-Specific Lighting",
     "description": "Named zones inside a room (Sofa, Desk …), each with its own cells, and a policy for when "
                    "several are occupied.",
     "required": ["Zone sources (zone entities or a position sensor)", "A policy for overlapping zones"],
     "optional": ["The room's own presence for “Room vacant”"],
     "set": {"primary": "presence", "presence": {"enabled": True}}},
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

PLANNED = [
    {"category": "Climate", "text": "Temperature schedules per room"},
    {"category": "Ventilation", "text": "Humidity, CO₂ and temperature control of fans"},
    {"category": "Buttons/Remotes", "text": "Button-action mapping"},
    {"category": "Fans", "text": "Speed by temperature or humidity"},
    {"category": "Generic switches", "text": "On/off by time or state"},
]


def for_kinds(kinds: list[str]) -> dict[str, Any]:
    return {"templates": [t for t in TEMPLATES if t["designer"] in kinds],
            "planned": PLANNED,
            "note": "Specialised designers for climate, ventilation, buttons, fans and switches are not built yet. "
                    "Use the rule editor (IF / AND-OR / THEN) for those devices: it supports them fully."}


def apply(template_id: str, designer: dict[str, Any]) -> dict[str, Any]:
    """The designer with the template's controls turned on (merged into what is there; nothing is bound)."""
    template = next((t for t in TEMPLATES if t["id"] == template_id), None)
    if template is None or template["designer"] != designer["kind"]:
        raise KeyError(template_id)
    out = copy.deepcopy(designer)
    for key, value in template["set"].items():
        if isinstance(value, dict):
            out[key] = {**out.get(key, {}), **value}
        else:
            out[key] = value
    return out
