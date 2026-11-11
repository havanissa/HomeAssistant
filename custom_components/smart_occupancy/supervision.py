"""Operating modes and the lighting-control safety gate.

Phase 0.1 permits Observe only. Supervise and Full Control exist in the
configuration model so later phases can enable them, but any request for
them is downgraded to Observe here, and ``assert_control_permitted`` refuses
every attempt to control lights.
"""

from __future__ import annotations

from .const import OperatingMode

# Modes this build is allowed to run in.
PERMITTED_MODES: frozenset[OperatingMode] = frozenset({OperatingMode.OBSERVE})


class ControlNotPermittedError(RuntimeError):
    """Raised when something tries to control lights in a non-control phase."""


def effective_mode(requested: OperatingMode) -> OperatingMode:
    """The mode the integration actually runs in."""
    return requested if requested in PERMITTED_MODES else OperatingMode.OBSERVE


def mode_permitted(requested: OperatingMode) -> bool:
    return requested in PERMITTED_MODES


def control_permitted(mode: OperatingMode) -> bool:
    """Whether any light may be changed. Always False in Phase 0.1."""
    return effective_mode(mode) in (OperatingMode.SUPERVISE, OperatingMode.FULL_CONTROL)


def assert_control_permitted(mode: OperatingMode) -> None:
    """Gate for future lighting code paths."""
    if not control_permitted(mode):
        raise ControlNotPermittedError(
            f"Lighting control is disabled (effective mode: {effective_mode(mode)})"
        )
