"""Operating modes and the lighting-control safety gate.

The integration-wide operating mode stays **Observe** in Phase 0.3:
Supervise and Full Control exist in the configuration model for later
phases, but any request for them is downgraded to Observe here.

Mood lighting is enabled **per room** instead (``RoomLighting.control_enabled``,
set only by an explicit, admin-only activation in the Smart Room Lighting card).
Every automatic light command passes ``assert_room_control_permitted`` first,
so a room that was not activated can never be controlled automatically.

Wall switch actions are a person's explicit request and have their own,
independent gate (``assert_switch_control_permitted``): a switch set to
**Active** executes its button actions also while the room's automatic
lighting is Observing. A switch in **Observing** never sends anything.

A profile **Test** (0.4.0) is also a person's explicit request: an admin
asks for it in the room card and confirms it, and only then the integration
sends that profile to the room's lights (an enabled room, in Observing too).
It is never triggered automatically, and the tested lights become a manual
override, so automatic lighting does not act on them afterwards.

Curtains (0.6.1) have their own gate, ``assert_curtain_command_permitted``.
The only cover command this build permits is a person's explicit, confirmed
**manual preset** request (``CurtainOrigin.MANUAL_CONFIRMED``) for a window
that is not Off; it is allowed in Observing too, because a person asked for
it. Every automatic origin is refused: automatic curtain control does not
exist yet, whatever a window's stored mode says.
"""

from __future__ import annotations

from enum import StrEnum

from .const import OperatingMode

# Integration-wide modes this build is allowed to run in.
PERMITTED_MODES: frozenset[OperatingMode] = frozenset({OperatingMode.OBSERVE})


class ControlNotPermittedError(RuntimeError):
    """Raised when something tries to control lights where that is not permitted."""


def effective_mode(requested: OperatingMode) -> OperatingMode:
    """The integration-wide mode the integration actually runs in."""
    return requested if requested in PERMITTED_MODES else OperatingMode.OBSERVE


def mode_permitted(requested: OperatingMode) -> bool:
    return requested in PERMITTED_MODES


def control_permitted(mode: OperatingMode) -> bool:
    """Whether the integration-wide mode allows lighting control (never in Phase 0.3)."""
    return effective_mode(mode) in (OperatingMode.SUPERVISE, OperatingMode.FULL_CONTROL)


def assert_control_permitted(mode: OperatingMode) -> None:
    """Gate for integration-wide (Supervise / Full Control) code paths."""
    if not control_permitted(mode):
        raise ControlNotPermittedError(
            f"Lighting control is disabled (effective mode: {effective_mode(mode)})"
        )


def assert_room_control_permitted(room_active: bool, area_id: str) -> None:
    """Gate for mood lighting commands: only explicitly activated rooms."""
    if not room_active:
        raise ControlNotPermittedError(f"Mood lighting is not enabled for room {area_id}")


def assert_switch_control_permitted(switch_active: bool, area_id: str, switch_id: str) -> None:
    """Gate for wall switch button actions: only switches set to Active, in an enabled room."""
    if not switch_active:
        raise ControlNotPermittedError(f"Switch {switch_id} in room {area_id} is not Active")


class CurtainOrigin(StrEnum):
    """Who asked for a cover command."""

    MANUAL_CONFIRMED = "manual_confirmed"  # an administrator confirmed this exact preset in the card
    AUTOMATIC = "automatic"                # a future decision engine (Phase 4.6.1); refused in this build


# Automatic curtain control is not part of this build. Kept as a constant so a test can prove it stays off.
AUTOMATIC_CURTAIN_CONTROL = False


def assert_curtain_command_permitted(origin: CurtainOrigin, *, confirmed: bool, window_mode: str,
                                     window_id: str) -> None:
    """Gate for every cover command (0.6.1)."""
    if origin != CurtainOrigin.MANUAL_CONFIRMED or (origin == CurtainOrigin.AUTOMATIC and not AUTOMATIC_CURTAIN_CONTROL):
        raise ControlNotPermittedError(f"Automatic curtain control is not available (window {window_id})")
    if not confirmed:
        raise ControlNotPermittedError(f"A preset for window {window_id} needs an explicit confirmation")
    if window_mode == "off":
        raise ControlNotPermittedError(f"Window {window_id} is Off")
