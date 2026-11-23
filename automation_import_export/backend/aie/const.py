"""Constants of the Automation Import/Export Manager App backend."""

from __future__ import annotations

from typing import Final

APP_NAME: Final = "Automation Import/Export Manager"
APP_VERSION: Final = "0.2.0"  # must match config.yaml, the Dockerfile and CHANGELOG.md (checked by the tests)

DEFAULT_PORT: Final = 8099
# Every Ingress request reaches the App from the Supervisor at this address.
SUPERVISOR_INGRESS_IP: Final = "172.30.32.2"
DEFAULT_HA_WS_URL: Final = "ws://supervisor/core/websocket"
DEFAULT_HA_API_URL: Final = "http://supervisor/core/api"
OPTIONS_PATH: Final = "/data/options.json"
DATA_DIR: Final = "/data"
# Home Assistant's configuration folder, mapped read-only (config.yaml: map homeassistant_config). Only
# blueprints/automation/** and automations.yaml are ever read from it (backend/aie/haconfig.py).
HA_CONFIG_DIR: Final = "/homeassistant"

#: The stable id namespace of the Automation Builder App: such automations are never deleted or written.
BUILDER_PREFIX: Final = "automation_builder_"
#: Texts by which Automation Builder marks the automations it writes (used only to flag ambiguous ownership).
BUILDER_DESCRIPTION: Final = "Managed by the Automation Builder App"
BUILDER_ALIAS_SUFFIX: Final = "(Automation Builder)"
BUILDER_BLUEPRINT_FOLDER: Final = "automation_builder/"

ARCHIVE_FORMAT: Final = "automation-import-export-archive"
ARCHIVE_VERSION: Final = 1
