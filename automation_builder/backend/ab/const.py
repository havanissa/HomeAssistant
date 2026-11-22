"""Constants of the Automation Builder App backend."""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "Automation Builder"
APP_VERSION: Final = "0.3.0"  # must match config.yaml, the Dockerfile and CHANGELOG.md (checked by the tests)

DEFAULT_PORT: Final = 8099
# Every Ingress request reaches the App from the Supervisor at this address.
SUPERVISOR_INGRESS_IP: Final = "172.30.32.2"
DEFAULT_HA_WS_URL: Final = "ws://supervisor/core/websocket"
DEFAULT_HA_API_URL: Final = "http://supervisor/core/api"
OPTIONS_PATH: Final = "/data/options.json"
DATA_DIR: Final = "/data"
