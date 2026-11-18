"""Constants of Home Assistant that the lighting runtime uses (same values as Home Assistant's)."""

from typing import Final

MATCH_ALL: Final = "*"

ATTR_ENTITY_ID: Final = "entity_id"
ATTR_SUPPORTED_FEATURES: Final = "supported_features"
ATTR_UNIT_OF_MEASUREMENT: Final = "unit_of_measurement"
ATTR_FRIENDLY_NAME: Final = "friendly_name"

EVENT_HOMEASSISTANT_STARTED: Final = "homeassistant_started"
EVENT_STATE_CHANGED: Final = "state_changed"

STATE_ON: Final = "on"
STATE_OFF: Final = "off"
STATE_UNAVAILABLE: Final = "unavailable"
STATE_UNKNOWN: Final = "unknown"
