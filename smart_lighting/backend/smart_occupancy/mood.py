"""Observation of the existing mood system.

Existing rule (implemented by the user's own automations, not by us)::

    if Manual Mood == "Auto": Main Mood follows Auto Mood
    else:                     Main Mood follows Manual Mood

This module only reads the three helpers and reports whether Main Mood
currently matches that rule. It never writes to them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .models import MoodConfig

SOURCE_AUTO = "auto"
SOURCE_MANUAL = "manual"


@dataclass(slots=True, frozen=True)
class MoodObservation:
    """Snapshot of the mood helpers and their relationship."""

    auto_mood: str | None = None
    manual_mood: str | None = None
    main_mood: str | None = None
    expected_main_mood: str | None = None
    # "auto" when Main Mood should follow Auto Mood, "manual" otherwise.
    source: str | None = None
    # True/False when all inputs are known, None when it cannot be judged.
    consistent: bool | None = None
    unavailable: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    main_mood_options: tuple[str, ...] = field(default=(), compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "auto_mood": self.auto_mood,
            "manual_mood": self.manual_mood,
            "main_mood": self.main_mood,
            "expected_main_mood": self.expected_main_mood,
            "source": self.source,
            "consistent": self.consistent,
            "unavailable": list(self.unavailable),
            "missing": list(self.missing),
        }


def _norm(value: str | None) -> str | None:
    return value.casefold().strip() if isinstance(value, str) else None


def expected_main_mood(
    auto_mood: str | None, manual_mood: str | None, auto_option: str
) -> tuple[str | None, str | None]:
    """Apply the existing rule. Returns (expected main mood, source)."""
    if manual_mood is None:
        return None, None
    if _norm(manual_mood) == _norm(auto_option):
        return auto_mood, SOURCE_AUTO
    return manual_mood, SOURCE_MANUAL


def observe(hass: HomeAssistant, config: MoodConfig) -> MoodObservation:
    """Read the configured mood helpers and evaluate the relationship."""
    values: dict[str, str | None] = {}
    unavailable: list[str] = []
    missing: list[str] = []
    options: tuple[str, ...] = ()
    for key, entity_id in (
        ("auto", config.auto_mood_entity),
        ("manual", config.manual_mood_entity),
        ("main", config.main_mood_entity),
    ):
        if not entity_id:
            values[key] = None
            missing.append(f"<{key}_mood not configured>")
            continue
        state = hass.states.get(entity_id)
        if state is None:
            values[key] = None
            missing.append(entity_id)
        elif state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            values[key] = None
            unavailable.append(entity_id)
        else:
            values[key] = state.state
            if key == "main":
                options = tuple(state.attributes.get("options") or ())

    expected, source = expected_main_mood(
        values["auto"], values["manual"], config.auto_option
    )
    consistent: bool | None = None
    if expected is not None and values["main"] is not None:
        consistent = _norm(expected) == _norm(values["main"])

    return MoodObservation(
        auto_mood=values["auto"],
        manual_mood=values["manual"],
        main_mood=values["main"],
        expected_main_mood=expected,
        source=source,
        consistent=consistent,
        unavailable=tuple(unavailable),
        missing=tuple(missing),
        main_mood_options=options,
    )


def mood_names(hass: HomeAssistant, config: MoodConfig) -> list[str]:
    """Known mood names (Main Mood options, falling back to Auto Mood)."""
    for entity_id in (config.main_mood_entity, config.auto_mood_entity):
        if entity_id and (state := hass.states.get(entity_id)):
            if opts := state.attributes.get("options"):
                return [o for o in opts if _norm(o) != _norm(config.auto_option)]
    return []
