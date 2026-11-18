"""Recorder history (placeholder): the App reads history through the websocket API instead."""

from __future__ import annotations

from typing import Any

from ...exceptions import HomeAssistantError


def get_significant_states(*args: Any, **kwargs: Any) -> Any:
    raise HomeAssistantError("The Recorder is not available in the App; history is read through Home Assistant's API")
