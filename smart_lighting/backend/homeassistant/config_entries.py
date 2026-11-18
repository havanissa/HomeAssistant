"""Config entries as the lighting runtime sees them: the integration's (with its options) and others by domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import HomeAssistant


class ConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"


class ConfigEntry:
    """One config entry (read from ``config_entries/get``; the integration's also has its options)."""

    def __init__(self, entry_id: str, domain: str, *, title: str = "", state: str = ConfigEntryState.LOADED,
                 options: dict[str, Any] | None = None, data: dict[str, Any] | None = None) -> None:
        self.entry_id = entry_id
        self.domain = domain
        self.title = title
        self.state = state
        self.options: dict[str, Any] = dict(options or {})
        self.data: dict[str, Any] = dict(data or {})
        self.runtime_data: Any = None

    def __repr__(self) -> str:
        return f"<ConfigEntry {self.domain} {self.entry_id} {self.state}>"


class ConfigEntries:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._entries: dict[str, ConfigEntry] = {}

    def async_entries(self, domain: str | None = None, include_ignore: bool = True,
                      include_disabled: bool = True) -> list[ConfigEntry]:
        return [e for e in self._entries.values() if domain is None or e.domain == domain]

    def async_loaded_entries(self, domain: str) -> list[ConfigEntry]:
        return [e for e in self.async_entries(domain) if e.state == ConfigEntryState.LOADED and e.runtime_data]

    def async_get_entry(self, entry_id: str) -> ConfigEntry | None:
        return self._entries.get(entry_id)

    def async_update_entry(self, entry: ConfigEntry, *, options: dict[str, Any] | None = None, **_: Any) -> bool:
        """Options of the integration are changed only through the host backend (the integration's API)."""
        if options is None:
            return False
        backend = self._hass.backend
        if backend is None:
            from .exceptions import HomeAssistantError  # noqa: PLC0415

            raise HomeAssistantError("No connection to Home Assistant")
        return backend.async_update_options(entry, options)

    # The host's interface -------------------------------------------------------
    def replace(self, entries: list[ConfigEntry]) -> None:
        runtime = {e.entry_id: e.runtime_data for e in self._entries.values()}
        self._entries = {}
        for entry in entries:
            entry.runtime_data = entry.runtime_data or runtime.get(entry.entry_id)
            self._entries[entry.entry_id] = entry

    def add(self, entry: ConfigEntry) -> None:
        self._entries[entry.entry_id] = entry
