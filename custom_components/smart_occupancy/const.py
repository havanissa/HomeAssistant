"""Constants for Smart Occupancy & Lighting."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "smart_occupancy"
NAME: Final = "Smart Occupancy & Lighting"
PHASE: Final = "0.3.5"
VERSION: Final = "0.3.9"

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
# Minor 2 (0.3.3): daylight settings; rooms activated before 0.3.3 keep waiting for confirmed occupancy.
# Minor 3 (0.3.5): wall switches and remotes (every room starts with none; legacy entities kept).
# Minor 4 (0.3.8): what automatic lighting does when a room becomes vacant (rooms without a setting: Turn off).
# Minor 5 (0.3.9): possible occupancy lighting (disabled; no room has a preliminary light).
LIGHTING_STORE_MINOR_VERSION: Final = 5
LIGHTING_STORE_KEY: Final = "smart_occupancy.lighting"
LIGHTING_SAVE_DELAY_SECONDS: Final = 1
# Runtime state of mood lighting (manual overrides, last applied profile).
LIGHTING_STATE_STORE_VERSION: Final = 1
LIGHTING_STATE_STORE_KEY: Final = "smart_occupancy.lighting_state"
LIGHTING_STATE_SAVE_DELAY_SECONDS: Final = 10
# Decision log (0.3.7): kept per room and per switch in the lighting state store.
DECISION_LOG_SIZE: Final = 10
# A decision that repeats the newest one of its room or switch within this time updates it (count, time).
DECISION_MERGE_SECONDS: Final = 15 * 60
# Repeated presses of the same button with the same result within this time are one log entry.
DECISION_PRESS_MERGE_SECONDS: Final = 60
# After a command is accepted, its targets get this long to report the requested state (plus any transition).
DECISION_VERIFY_SECONDS: Final = 5

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


class VacancyAction(StrEnum):
    """What automatic lighting does when a room becomes vacant (0.3.8)."""

    TURN_OFF = "turn_off"        # switch the room's lights off (ambient lights keep following the mood)
    VACANT_MOOD = "vacant_mood"  # apply a chosen mood's profile to the lights that are on
    LEAVE = "leave"              # leave every light as it is


DEFAULT_VACANCY_ACTION: Final = VacancyAction.TURN_OFF


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


# Daylight-aware automatic switch-on (Phase 0.3.3) -----------------------------
class AutoOn(StrEnum):
    """When occupancy may switch a room's lights on."""

    DARK = "dark"          # when occupied and dark (default)
    ALWAYS = "always"      # when occupied, regardless of daylight (behaviour before 0.3.3)
    NEVER = "never"        # never switched on automatically (lights already on still follow the mood)


class UncertainPolicy(StrEnum):
    """What automatic switch-on does while daylight cannot be determined."""

    TURN_ON = "turn_on"    # switch on (as before 0.3.3; default)
    STAY_OFF = "stay_off"  # leave the lights off


class ReportMode(StrEnum):
    """What counts as a new measurement of an indoor light sensor."""

    CHANGES = "changes"    # only a changed value (safe for sensors that repeat cached values)
    REPORTS = "reports"    # every report, also with an unchanged value (frequently reporting sensors)


class Reliability(StrEnum):
    NORMAL = "normal"
    LOW = "low"


class Darkness(StrEnum):
    """Daylight decision of a room."""

    DARK = "dark"
    BRIGHT = "bright"
    UNCERTAIN = "uncertain"


DEFAULT_ON_BELOW_LUX: Final = 50          # turn-on threshold
DEFAULT_OFF_ABOVE_LUX: Final = 100        # light-not-needed threshold (hysteresis)
DEFAULT_DAYLIGHT_STABLE_SECONDS: Final = 90
DEFAULT_SETTLE_SECONDS: Final = 60        # after influencing lights turn off
DEFAULT_SAMPLE_MAX_AGE_MINUTES: Final = 30
DEFAULT_OUTDOOR_DARK_LUX: Final = 50
DEFAULT_OUTDOOR_WINDOW_SECONDS: Final = 300
LUX_MIN: Final = 1
LUX_MAX_THRESHOLD: Final = 5000
LUX_VALID_MAX: Final = 200000             # a reading above this is invalid
STABLE_SECONDS_MAX: Final = 1800
SETTLE_SECONDS_MAX: Final = 900
SENSOR_MAX_AGE_MAX_MINUTES: Final = 1440
SAMPLE_MAX_AGE_MAX_MINUTES: Final = 240
OUTDOOR_WINDOW_MAX_SECONDS: Final = 1800
DAYLIGHT_FACTOR_MIN: Final = 0.0001
DAYLIGHT_FACTOR_MAX: Final = 1.0
# Sun elevation (degrees) below which it is reliably dark outside (end of civil twilight),
# and the horizon used for sunset (refraction included).
SUN_DARK_ELEVATION: Final = -6.0
SUN_HORIZON_ELEVATION: Final = -0.833
# Calibration: trusted indoor/outdoor pairs give the room's daylight factor.
CALIBRATION_MIN_OUTDOOR_LUX: Final = 200
CALIBRATION_MIN_INTERVAL_SECONDS: Final = 600
CALIBRATION_MAX_SAMPLES: Final = 20
CALIBRATION_MIN_SAMPLES: Final = 3
# Lux changes are coalesced this long before a background re-evaluation.
DAYLIGHT_DEBOUNCE_SECONDS: Final = 3.0
# A contaminated (lights on) indoor reading below the turn-on threshold proves darkness
# while it is at most this old.
BOUND_MAX_AGE_MINUTES: Final = 30


# Wall switches and remotes (Phase 0.3.5) --------------------------------------
# Button events come from Zigbee2MQTT (raw MQTT on the device topic), Home
# Assistant MQTT device triggers or event entities. Every switch starts in
# Observing (listen); Active (control) is an explicit activation per switch.
DEFAULT_MQTT_BASE_TOPIC: Final = "zigbee2mqtt"
MAX_SWITCHES_PER_ROOM: Final = 20
MAX_BUTTONS_PER_SWITCH: Final = 64
# Learning mode: how long it listens unless cancelled.
DEFAULT_LEARN_SECONDS: Final = 60
MAX_LEARN_SECONDS: Final = 300
# The same action through a second input path within this window is one press.
DEDUPE_WINDOW_SECONDS: Final = 0.5
# Repeats of the same action through the same path faster than this are one press.
DEFAULT_DEBOUNCE_MS: Final = 100
MAX_DEBOUNCE_MS: Final = 2000
# An event entity whose event is older than this when it is reported is stale (restored state).
STALE_EVENT_SECONDS: Final = 5.0
# Triggers that were just attached may replay a retained message; ignored for this long.
ATTACH_SETTLE_SECONDS: Final = 2.0
# Presses queued per switch while its previous command still runs; more are dropped.
MAX_QUEUED_PRESSES: Final = 4
# Brightness steps and hold-to-dim.
DEFAULT_STEP_PCT: Final = 10
DEFAULT_HOLD_STEP_PCT: Final = 8
DEFAULT_DIM_INTERVAL: Final = 0.4      # seconds between hold-to-dim commands
DIM_INTERVAL_MIN: Final = 0.2
DIM_INTERVAL_MAX: Final = 2.0
DEFAULT_DIM_TIMEOUT: Final = 10        # seconds: dimming stops if no STOP/release arrives
DIM_TIMEOUT_MAX: Final = 60
# A commanded brightness is trusted over the reported state for this long (state reports lag).
LEVEL_MEMORY_SECONDS: Final = 5.0
MQTT_PAYLOAD_MAX_BYTES: Final = 65536
BRIDGE_PAYLOAD_MAX_BYTES: Final = 8 * 1024 * 1024
SWITCH_HISTORY_SIZE: Final = 10


class SwitchSource(StrEnum):
    """Where a switch's button events are read from."""

    AUTO = "auto"                    # the best available source (preference order below)
    ZIGBEE2MQTT = "zigbee2mqtt"      # raw MQTT: the "action" of the device topic
    DEVICE_TRIGGER = "device_trigger"  # Home Assistant MQTT device triggers
    EVENT_ENTITY = "event_entity"    # a Home Assistant event entity


class SwitchMode(StrEnum):
    """What Smart Occupancy does with a switch's button events."""

    # Stored values are unchanged since 0.3.5; the cards call them Observing and Active (0.3.6).
    LISTEN = "listen"    # Observing: register manual intervention only; never sends a command (default)
    CONTROL = "control"  # Active: execute the configured button mappings (explicit activation)


class ButtonAction(StrEnum):
    """What a button press does."""

    NONE = "none"
    ON = "on"
    OFF = "off"
    TOGGLE = "toggle"
    BRIGHTNESS_UP = "brightness_up"      # one step per press
    BRIGHTNESS_DOWN = "brightness_down"
    DIM_UP = "dim_up"                    # start dimming up (hold)
    DIM_DOWN = "dim_down"
    DIM_STOP = "dim_stop"                # stop dimming (release / stop)
    SET_BRIGHTNESS = "set_brightness"
    SET_COLOR_TEMP = "set_color_temp"
    MOOD = "mood"                        # select a mood (room or global)
    MOOD_AUTO = "mood_auto"              # return to automatic mood selection
    SCENE = "scene"
    SCRIPT = "script"
    WLED_PRESET = "wled_preset"


class OnValues(StrEnum):
    """What an ON press switches lights on with."""

    MOOD = "mood"      # the currently effective mood's values (default)
    LAST = "last"      # the light's own last values
    FIXED = "fixed"    # the brightness / colour temperature of the mapping
