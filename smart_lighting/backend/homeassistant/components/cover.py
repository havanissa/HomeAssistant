"""Cover entity features (``homeassistant.components.cover`` subset; same values as Home Assistant's)."""

from enum import IntFlag


class CoverEntityFeature(IntFlag):
    OPEN = 1
    CLOSE = 2
    SET_POSITION = 4
    STOP = 8
    OPEN_TILT = 16
    CLOSE_TILT = 32
    STOP_TILT = 64
    SET_TILT_POSITION = 128


ATTR_CURRENT_POSITION = "current_position"
ATTR_POSITION = "position"
