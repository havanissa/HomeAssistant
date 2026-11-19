"""The Recorder is not part of the App (``homeassistant.components.recorder`` placeholder).

The vendored forecast runtime imports the Recorder only inside the functions it
uses when no other history source is installed. In the App the forecast runtime
reads history through Home Assistant's websocket API
(``history/history_during_period``, see ``smart_lighting.forecast_host``), so
these names exist for the import and refuse to be used: the App never touches
the Recorder's database and does not assume its backend.
"""

from __future__ import annotations

from typing import Any

from ...exceptions import HomeAssistantError
from . import history  # noqa: F401 - the name the forecast runtime imports


def get_instance(hass: Any) -> Any:
    raise HomeAssistantError("The Recorder is not available in the App; history is read through Home Assistant's API")
