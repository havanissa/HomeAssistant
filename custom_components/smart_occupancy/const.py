"""Constants for Smart Occupancy & Lighting."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

DOMAIN: Final = "smart_occupancy"
NAME: Final = "Smart Occupancy & Lighting"
PHASE: Final = "0.1"

# Version of the options payload stored in the config entry.
CONFIG_VERSION: Final = 1

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
    """How a room signal contributes to occupancy (used from a later phase)."""

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


class Severity(StrEnum):
    """Severity of a validation problem."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


DEFAULT_ROLE_FOR_KIND: Final[dict[SensorKind, SensorRole]] = {
    SensorKind.PIR: SensorRole.TRIGGER,
    SensorKind.RADAR: SensorRole.MAINTAIN,
    SensorKind.DOOR: SensorRole.TRIGGER,
    SensorKind.PC_ACTIVITY: SensorRole.MAINTAIN,
    SensorKind.BED: SensorRole.MAINTAIN,
    SensorKind.OTHER: SensorRole.IGNORE,
}

# Initial light categories: (stable key, display name).
DEFAULT_CATEGORIES: Final[list[tuple[str, str]]] = [
    ("downlights", "Downlights"),
    ("lamps", "Lamps"),
    ("led_strips", "LED strips"),
    ("decorative", "Decorative lighting"),
    ("task", "Task lighting"),
    ("path", "Path lighting"),
    ("outdoor", "Outdoor lighting"),
]

# Profile value limits.
BRIGHTNESS_MIN: Final = 0
BRIGHTNESS_MAX: Final = 100
KELVIN_MIN: Final = 1000
KELVIN_MAX: Final = 10000
TRANSITION_MIN: Final = 0.0
TRANSITION_MAX: Final = 300.0
