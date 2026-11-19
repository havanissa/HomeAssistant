"""Constants for the Smart Occupancy & Lighting App backend."""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "Smart Occupancy & Lighting"
APP_VERSION: Final = "0.8.0"  # must match config.yaml (checked by the tests)
APP_PHASE: Final = "A6.1"

# The existing custom integration this App will eventually replace. It keeps
# running every automation. Since Phase A2 the App edits its room
# configuration through the integration's own validated commands.
INTEGRATION_DOMAIN: Final = "smart_occupancy"

DEFAULT_PORT: Final = 8099
# Every Ingress request reaches the App from the Supervisor at this address.
SUPERVISOR_INGRESS_IP: Final = "172.30.32.2"

DEFAULT_HA_WS_URL: Final = "ws://supervisor/core/websocket"
DEFAULT_SUPERVISOR_URL: Final = "http://supervisor"
OPTIONS_PATH: Final = "/data/options.json"
DATA_DIR: Final = "/data"
