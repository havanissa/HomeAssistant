"""Constants of the Smart Occupancy Detection App backend."""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "Smart Occupancy Detection"
APP_VERSION: Final = "0.2.0"  # must match config.yaml and CHANGELOG.md (checked by the tests)

DEFAULT_PORT: Final = 8099
# Every Ingress request reaches the App from the Supervisor at this address.
SUPERVISOR_INGRESS_IP: Final = "172.30.32.2"
DEFAULT_HA_WS_URL: Final = "ws://supervisor/core/websocket"
OPTIONS_PATH: Final = "/data/options.json"
DATA_DIR: Final = "/data"

#: The domains of Smart Comfort's integration and of this App's companion. Their entities are occupancy *outputs*,
#: never suggested as sensors (an output used as an input would feed back on itself).
SMART_COMFORT_DOMAIN: Final = "smart_occupancy"
