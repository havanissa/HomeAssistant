"""Validation of the occupancy timing values (shared by the options flow and the presence editors)."""

from __future__ import annotations

MIN_STUCK_SECONDS = 60


def validate_timing(values: dict[str, int]) -> dict[str, str]:
    errors: dict[str, str] = {}
    for key in ("trigger_stuck_after", "presence_stuck_after"):
        if (value := values.get(key)) and value < MIN_STUCK_SECONDS:
            errors[key] = "stuck_too_short"
    return errors
