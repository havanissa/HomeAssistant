"""Diagnostics download: the hub's status and the published views. No tokens exist (only digests); user ids and
instance ids are redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

TO_REDACT = {"paired_by", "by", "instance", "session"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    hub = entry.runtime_data
    return async_redact_data({"status": hub.status(), "views": hub.views, "disabled": hub.disabled,
                              "orphaned": hub.orphaned}, TO_REDACT)
