"""Constants of the Smart Occupancy & Lighting companion integration (Phase A6)."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "smart_occupancy"
NAME: Final = "Smart Occupancy & Lighting"
VERSION: Final = "1.0.0"
#: The App version this companion was released with (shown in diagnostics; not enforced).
APP_VERSION_HINT: Final = "0.7.0"

# Config entry versions. 1.2 was the last monolithic release (0.9.0); 1.3 is the companion. The options of a 1.2
# entry are kept untouched (read-only legacy data) until the App's migration retires them.
ENTRY_VERSION: Final = 1
ENTRY_MINOR_VERSION: Final = 3

STORE_VERSION: Final = 1
STORE_KEY: Final = f"{DOMAIN}.companion"
LEGACY_BACKUP_STORE_KEY: Final = f"{DOMAIN}.legacy_backup"
SAVE_DELAY_SECONDS: Final = 5

#: The built-in mood selector (a select entity of this integration whose options are the moods defined in the App).
BUILTIN_MOOD_ENTITY: Final = "select.smart_lighting_mood"

#: Stores of the retired 0.9.0 integration: key -> (version, minor version) as that release wrote them.
LEGACY_STORES: Final = {
    f"{DOMAIN}.occupancy_state": (1, 1),
    f"{DOMAIN}.lighting": (1, 9),
    f"{DOMAIN}.lighting_state": (1, 1),
    f"{DOMAIN}.curtains": (1, 4),
    f"{DOMAIN}.curtains_state": (1, 1),
    f"{DOMAIN}.forecast_settings": (1, 1),
    f"{DOMAIN}.forecast_days": (1, 1),
    f"{DOMAIN}.problem_history": (1, 1),
    f"{DOMAIN}.engine_ownership": (1, 1),
    f"{DOMAIN}.lighting_ownership": (1, 1),
    f"{DOMAIN}.lighting_mirror": (1, 1),
    f"{DOMAIN}.curtains_ownership": (1, 1),
    f"{DOMAIN}.curtains_mirror": (1, 1),
    f"{DOMAIN}.forecast_ownership": (1, 1),
    f"{DOMAIN}.forecast_mirror": (1, 1),
}
#: Legacy option keys that mean the entry still carries the monolithic configuration.
LEGACY_OPTION_KEYS: Final = ("rooms", "mood", "categories", "occupancy", "operating_mode", "config_version",
                             "global_profiles")

# Dashboard cards served by this integration (card.py).
CARD_URL_BASE: Final = "/smart_occupancy_static"
ROOM_CARD_FILENAME: Final = "smart-occupancy-room-card.js"
COMPACT_CARDS_FILENAME: Final = "smart-occupancy-cards.js"
CARD_FILENAMES: Final = (ROOM_CARD_FILENAME, COMPACT_CARDS_FILENAME)

# Entity option lists (the same values the engines publish; kept here so the entities need no engine code).
OCCUPANCY_STATES: Final = ["vacant", "possible_occupancy", "confirmed_occupancy", "possible_vacancy", "uncertain"]
STATUS_OPTIONS: Final = ["healthy", "warning", "config_error", "unavailable"]
OPERATING_MODES: Final = ["observe", "supervise", "full_control"]
LIGHTING_MODES: Final = ["observe", "active", "blocked", "paused"]
DARKNESS: Final = ["dark", "bright", "uncertain"]
COMPARISON_OPTIONS: Final = ["agree", "disagree", "unknown"]
CONFIDENCE_LEVELS: Final = ["insufficient_data", "low", "moderate", "high"]
LEARNING_STATES: Final = ["waiting", "learning", "ready", "disabled", "no_recorder", "error"]

SIGNAL_VIEW_UPDATED: Final = f"{DOMAIN}_companion_view_updated"
SIGNAL_ROOMS_CHANGED: Final = f"{DOMAIN}_companion_rooms_changed"
SIGNAL_OWNERSHIP: Final = f"{DOMAIN}_companion_ownership"
