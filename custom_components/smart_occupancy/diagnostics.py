"""Downloadable diagnostics (Settings → Devices & services → ⋮ → Download diagnostics). No tokens, no secrets."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from . import legacy
from .companion import Companion
from .const import VERSION


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    companion: Companion = entry.runtime_data
    return {
        "companion_version": VERSION,
        "entry": {"version": entry.version, "minor_version": entry.minor_version,
                  "legacy_options_present": legacy.legacy_options(entry) is not None,
                  "options_keys": sorted(entry.options)},
        "companion": companion.diagnostics(),
        "legacy_backup": await legacy.async_backup_info(hass),
    }
