"""Diagnostics download: the hub's status and the published views (no tokens: only digests exist)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    return {"status": hub.status(), "views": hub.views, "disabled": hub.disabled, "orphaned": hub.orphaned}
