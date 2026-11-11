"""Constants for Smart Occupancy & Lighting."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "smart_occupancy"
NAME: Final = "Smart Occupancy & Lighting"
PHASE: Final = "0.3.2"
VERSION: Final = "0.3.2"

# Version of the options payload stored in the config entry.
# 1: Phase 0.1 / 0.1.1. 2: Phase 0.2 (occupancy timing, adjacency, inverted signals).
CONFIG_VERSION: Final = 2

# Debounce windows (seconds). All updates are event driven; these only
# coalesce bursts of events into a single recomputation.
STATE_DEBOUNCE_SECONDS: Final = 1.0
REGISTRY_DEBOUNCE_SECONDS: Final = 2.0

DEFAULT_AUTO_MOOD_ENTITY: Final = "input_select.auto_mood"
DEFAULT_MANUAL_MOOD_ENTITY: Final = "input_select.mood"
DEFAULT_MAIN_MOOD_ENTITY: Final = "input_select.main_mood"
DEFAULT_AUTO_OPTION: Final = "Auto"
# Existing automations copy Auto Mood into Main Mood shortly after a change.
# A mismatch is only reported once it persists longer than this grace period.
DEFAULT_MISMATCH_GRACE_SECONDS: Final = 30

# Name of the pilot room. Used only to pre-select the matching area in the
# setup form; nothing is assumed about the entities inside it.
PILOT_AREA_NAME: Final = "Kamar Norður"

MOOD_ENTITY_DOMAINS: Final = ["input_select", "select"]


class OperatingMode(StrEnum):
    """Operating modes of the integration."""

    OBSERVE = "observe"
    SUPERVISE = "supervise"
    FULL_CONTROL = "full_control"


class SensorKind(StrEnum):
    """What a room signal physically is."""

    PIR = "pir"
    RADAR = "radar"
    DOOR = "door"
    PC_ACTIVITY = "pc_activity"
    BED = "bed"
    OTHER = "other"


class SensorRole(StrEnum):
    """How a room signal contributes to occupancy."""

    TRIGGER = "trigger"
    MAINTAIN = "maintain"
    CONFIRM_VACANCY = "confirm_vacancy"
    IGNORE = "ignore"


class LightAction(StrEnum):
    """Action of a mood profile entry."""

    ON = "on"
    OFF = "off"
    INHERIT = "inherit"
    IGNORE = "ignore"


class AdjustMode(StrEnum):
    """How a profile sets brightness or colour temperature (Phase 0.3.2).

    Not set (``None``) means inherit. A profile that sets a value without a
    mode is ``fixed``, so every profile made before 0.3.2 stays fixed.
    """

    FIXED = "fixed"      # the profile's own value
    GLOBAL = "global"    # the existing global adjustment's current value
    OFFSET = "offset"    # the global value plus the profile's offset
    IGNORE = "ignore"    # leave this property unchanged


class Severity(StrEnum):
    """Severity of a validation problem."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ProblemCategory(StrEnum):
    """Whether a problem is in the configuration or in the running system."""

    CONFIGURATION = "configuration"
    OPERATIONAL = "operational"


class Classification(StrEnum):
    """User-facing classification of rooms, the integration and problems."""

    HEALTHY = "healthy"
    WARNING = "warning"
    CONFIG_ERROR = "config_error"
    UNAVAILABLE = "unavailable"
    INFO = "info"  # problems only: informational note, never degrades status


# Status precedence, worst first.
STATUS_ORDER: Final = (
    Classification.CONFIG_ERROR,
    Classification.UNAVAILABLE,
    Classification.WARNING,
    Classification.HEALTHY,
)

# After Home Assistant has started, entities that are registered but not yet
# provided by their integration are given this long to appear before they are
# reported (slow integrations such as MQTT finish loading after start).
STARTUP_GRACE_SECONDS: Final = 120

# Entity attributes carry a "last_checked" timestamp. While problems exist it
# is republished at most this often, which bounds Recorder writes.
LAST_CHECKED_REFRESH_MINUTES: Final = 15

PROBLEM_STORE_VERSION: Final = 1
PROBLEM_STORE_KEY: Final = "smart_occupancy.problem_history"


DEFAULT_ROLE_FOR_KIND: Final[dict[SensorKind, SensorRole]] = {
    SensorKind.PIR: SensorRole.TRIGGER,
    SensorKind.RADAR: SensorRole.MAINTAIN,
    SensorKind.DOOR: SensorRole.TRIGGER,
    SensorKind.PC_ACTIVITY: SensorRole.MAINTAIN,
    SensorKind.BED: SensorRole.MAINTAIN,
    SensorKind.OTHER: SensorRole.IGNORE,
}

class CategoryKind(StrEnum):
    """What a light category label describes (Phase 0.3.1).

    A light can carry several labels at once, e.g. ``Spottar`` + ``Loft ljós``
    + ``Rgb``. Source types say what the light is, functions what it is used
    for, and the RGB tag marks lights that may get colour profiles.
    """

    SOURCE = "source"      # Spottar, Loft ljós, LED strips, Downlights, Lamps ...
    FUNCTION = "function"  # Uppljós, Decorative, Task, Path, Outdoor ...
    RGB = "rgb"            # eligibility tag for colour profiles (not a type)


# Profile target for only the RGB-tagged lights of a category ("<key>+rgb").
RGB_SUFFIX: Final = "+rgb"

# Initial light categories: (stable key, display name, kind).
DEFAULT_CATEGORIES: Final[list[tuple[str, str, CategoryKind]]] = [
    ("downlights", "Downlights", CategoryKind.SOURCE),
    ("lamps", "Lamps", CategoryKind.SOURCE),
    ("led_strips", "LED strips", CategoryKind.SOURCE),
    ("decorative", "Decorative lighting", CategoryKind.FUNCTION),
    ("task", "Task lighting", CategoryKind.FUNCTION),
    ("path", "Path lighting", CategoryKind.FUNCTION),
    ("outdoor", "Outdoor lighting", CategoryKind.FUNCTION),
    ("uplight", "Uppljós", CategoryKind.FUNCTION),
]
# Categories created before 0.3.1 have no kind; it is derived from these names.
FUNCTION_CATEGORY_NAMES: Final = frozenset({
    "decorative", "task", "path", "outdoor", "uplight", "uplights", "uppljós", "uppljos",
    "decorative lighting", "task lighting", "path lighting", "outdoor lighting", "pynt", "pyntiljós",
})
RGB_CATEGORY_NAMES: Final = frozenset({"rgb", "rgb lights", "rgbw", "rgbww", "rgb ljós", "litljós"})

# Profile value limits. Brightness 0 is not a level: use the Off action.
BRIGHTNESS_MIN: Final = 1
BRIGHTNESS_MAX: Final = 100
KELVIN_MIN: Final = 1000
KELVIN_MAX: Final = 10000
TRANSITION_MIN: Final = 0.0
TRANSITION_MAX: Final = 300.0


class OccupancyState(StrEnum):
    """Internal occupancy state of a room (see engine.py for transitions)."""

    VACANT = "vacant"
    POSSIBLE_OCCUPANCY = "possible_occupancy"
    CONFIRMED_OCCUPANCY = "confirmed_occupancy"
    POSSIBLE_VACANCY = "possible_vacancy"
    UNCERTAIN = "uncertain"


# Occupancy timing parameters: key -> (default seconds, min, max).
# All are configurable globally (Configure -> Occupancy timing) and can be
# overridden per room. A value of 0 disables the two "stuck" detections.
TIMING_PARAMETERS: Final[dict[str, tuple[int, int, int]]] = {
    "correlation_window": (10, 1, 120),
    "exit_confirmation": (300, 0, 3600),
    "inactivity_timeout": (600, 30, 14400),
    "dropout_grace": (30, 0, 600),
    "trigger_stuck_after": (1800, 0, 86400),
    "presence_stuck_after": (0, 0, 172800),
    "startup_hold": (120, 0, 3600),
}

OCCUPANCY_STORE_VERSION: Final = 1
OCCUPANCY_STORE_KEY: Final = "smart_occupancy.occupancy_state"
# Engine state is written on occupancy transitions only, coalesced this long.
OCCUPANCY_SAVE_DELAY_SECONDS: Final = 15

# Dashboard cards served by this integration.
CARD_URL_BASE: Final = "/smart_occupancy_static"
CARD_FILENAME: Final = "smart-occupancy-room-card.js"
LIGHTING_CARD_FILENAME: Final = "smart-occupancy-lighting-cards.js"
CARD_FILENAMES: Final = (CARD_FILENAME, LIGHTING_CARD_FILENAME)

# Mood lighting (Phase 0.3) -------------------------------------------------
# User configuration of mood lighting (profiles, room overrides, activation).
# Owned by the integration, edited by the dashboard cards over the websocket
# API, and saved without reloading the integration.
LIGHTING_STORE_VERSION: Final = 1
LIGHTING_STORE_KEY: Final = "smart_occupancy.lighting"
LIGHTING_SAVE_DELAY_SECONDS: Final = 1
# Runtime state of mood lighting (manual overrides, last applied profile).
LIGHTING_STATE_STORE_VERSION: Final = 1
LIGHTING_STATE_STORE_KEY: Final = "smart_occupancy.lighting_state"
LIGHTING_STATE_SAVE_DELAY_SECONDS: Final = 10

# Coalesces Manual Mood -> Main Mood propagation and bursts of triggers.
APPLY_DEBOUNCE_SECONDS: Final = 1.0
# A light service call that takes longer than this is reported as failed.
COMMAND_TIMEOUT_SECONDS: Final = 15
# After a command, state changes matching the requested state within this
# window are attributed to us (MQTT lights report back with a new context).
OWN_CHANGE_WINDOW_SECONDS: Final = 20
# A light change this soon after a configured wall-switch entity changed is
# attributed to that switch.
SWITCH_ATTRIBUTION_SECONDS: Final = 3
DEFAULT_MANUAL_OVERRIDE_MINUTES: Final = 60
MANUAL_OVERRIDE_MAX_MINUTES: Final = 1440
# Light state tolerances when deciding whether a light already matches.
BRIGHTNESS_TOLERANCE_PCT: Final = 2
KELVIN_TOLERANCE: Final = 60
RGB_TOLERANCE: Final = 4
HS_TOLERANCE: Final = 2.0

SIGNAL_LIGHTING_UPDATED: Final = f"{DOMAIN}_lighting_updated"


class UnrepresentableMode(StrEnum):
    """What lighting does while the expected mood is not a Main Mood option."""

    FOLLOW_MAIN = "follow_main"  # keep following Main Mood's actual value (default)
    NO_MOOD = "no_mood"          # treat as "no mood": lighting changes nothing
    USE_MOOD = "use_mood"        # treat it as another (configured) mood


class UnattributedPolicy(StrEnum):
    """How light changes without any Home Assistant context are treated."""

    IGNORE = "ignore"            # not a manual override (default)
    MANUAL = "manual"            # treat as a manual override


class LightingMode(StrEnum):
    """Lighting control state of a room."""

    OBSERVE = "observe"
    ACTIVE = "active"
    BLOCKED = "blocked"
    # Main Mood is the Manual option: every automatic lighting command is paused.
    PAUSED = "paused"


# Global light adjustment (Phase 0.3.2) ---------------------------------------
# Profiles can follow the values of the user's existing adaptive brightness and
# colour temperature system. Smart Occupancy does not calculate them.
#
# Pseudo mood of profile entries that apply in every mood (below the entries
# of the mood itself, at each level).
ALL_MOODS: Final = "*"
# Main Mood option that pauses all automatic lighting (not a mood).
DEFAULT_MANUAL_OPTION: Final = "Manual"
MANUAL_PAUSED_TEXT: Final = "Manual control — automatic lighting paused"
# Defaults for gradual (adaptive-only) updates; mood changes are not throttled.
DEFAULT_ADAPTIVE_MIN_INTERVAL: Final = 60       # seconds per light or group
DEFAULT_ADAPTIVE_BRIGHTNESS_STEP: Final = 3     # percentage points
DEFAULT_ADAPTIVE_KELVIN_STEP: Final = 100       # K
DEFAULT_ADAPTIVE_TRANSITION: Final = 10.0       # seconds; 0 = none
ADAPTIVE_MIN_INTERVAL_MAX: Final = 3600
ADAPTIVE_BRIGHTNESS_STEP_MAX: Final = 50
ADAPTIVE_KELVIN_STEP_MAX: Final = 2000
# Source changes arriving within this window are coalesced into one update.
ADAPTIVE_DEBOUNCE_SECONDS: Final = 2.0
BRIGHTNESS_OFFSET_MAX: Final = 99    # +- percentage points
KELVIN_OFFSET_MAX: Final = 5000      # +- K


class SourceScale(StrEnum):
    """Unit of a global source value."""

    AUTO = "auto"
    PERCENT = "percent"   # brightness 0-100 %
    BYTE = "byte"         # brightness 0-255
    KELVIN = "kelvin"
    MIRED = "mired"
