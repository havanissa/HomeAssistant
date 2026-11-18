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

A **Priority Input** (0.6.8) is an explicit request too, deliberately armed:
an administrator enables each input after reviewing it. Its commands pass
``assert_priority_control_permitted``: the input must be enabled, its room
enabled, and the global Priority Input safety lock off. It is not blocked by
the room's Observing mode or paused by Manual mood (like an Active wall
switch); it never bypasses device availability or automation conflicts
(those are protections of the plan itself).

Curtains have their own gate, ``assert_curtain_command_permitted``. A
person's explicit, confirmed **manual preset** request
(``CurtainOrigin.MANUAL_CONFIRMED``, 0.6.1) is permitted for a window that is
not Off; it is allowed in Observing too, because a person asked for it. An
**automatic** command (``CurtainOrigin.AUTOMATIC``, 0.6.2) is permitted only
for a window whose mode is Automatic, which an administrator sets with an
explicit confirmation per window; Observing and Off windows never receive an
automatic command.
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


def assert_priority_control_permitted(enabled: bool, room_enabled: bool, locked: bool, area_id: str,
                                      input_id: str) -> None:
    """Gate for Priority Input commands: an enabled input, an enabled room, the safety lock off."""
    if locked:
        raise ControlNotPermittedError("Priority Inputs are locked (safety lock)")
    if not room_enabled:
        raise ControlNotPermittedError(f"Room {area_id} is not enabled")
    if not enabled:
        raise ControlNotPermittedError(f"Priority Input {input_id} in room {area_id} is not enabled")


class CurtainOrigin(StrEnum):
    """Who asked for a cover command."""

    MANUAL_CONFIRMED = "manual_confirmed"  # an administrator confirmed this exact preset in the card
    AUTOMATIC = "automatic"                # the window's automatic decision engine (curtains/auto.py)


# Automatic curtain control exists from 0.6.2, per window and only in Automatic mode.
AUTOMATIC_CURTAIN_CONTROL = True


def assert_curtain_command_permitted(origin: CurtainOrigin, *, confirmed: bool, window_mode: str,
                                     window_id: str) -> None:
    """Gate for every cover command (manual 0.6.1, automatic 0.6.2)."""
    if window_mode == "off":
        raise ControlNotPermittedError(f"Window {window_id} is Off")
    if origin == CurtainOrigin.MANUAL_CONFIRMED:
        if not confirmed:
            raise ControlNotPermittedError(f"A preset for window {window_id} needs an explicit confirmation")
        return
    if origin == CurtainOrigin.AUTOMATIC and AUTOMATIC_CURTAIN_CONTROL:
        if window_mode != "automatic":
            raise ControlNotPermittedError(f"Window {window_id} is not in Automatic: nothing is sent automatically")
        return
    raise ControlNotPermittedError(f"Curtain command not permitted (window {window_id})")
