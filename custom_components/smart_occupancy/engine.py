"""Occupancy engine (re-export).

Since Phase A3.2 the engine lives in the shared package ``occupancy_core``,
which the Smart Lighting App runs too (a byte-identical copy). This module
keeps every existing import working with the very same objects.
"""

from __future__ import annotations

from .occupancy_core.engine import (  # noqa: F401
    ACTIVE_STATES,
    INACTIVE_STATES,
    MAX_REASON_LENGTH,
    PRESENCE_ROLES,
    UNKNOWN_STATES,
    Confidence,
    RoomEngine,
    RoomResult,
    SignalStatus,
    SignalTrack,
    SignalView,
    StateError,
    Value,
    _duration,
    _join,
    _later,
    _occupied_word,
    interpret,
)
