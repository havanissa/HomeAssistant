"""The retired 0.9.0 integration's data: read-only export for the App's migration, a reviewed retirement, restore.

A 1.2 config entry (Smart Occupancy & Lighting 0.9.0) carries the monolithic
configuration in its options and in the ``.storage/smart_occupancy.*`` stores.
The companion never reads them for its own work and never writes them, except:

* ``async_export`` - everything, read-only, so the App's migration wizard can
  import the configuration, the learned forecast data, the manual holds and
  the runtime state (``companion/legacy/export``);
* ``async_retire`` - an explicit, administrator-reviewed step of the migration:
  a complete backup is written to ``.storage/smart_occupancy.legacy_backup``
  first, then the legacy stores are removed and the entry's options are reduced
  to the companion's own. Never automatic.
* ``async_restore`` - the reverse (for a rollback to 0.9.0): the backup's
  stores and options are written back.

The stores are read through Home Assistant's ``Store`` with a subclass that
accepts any stored version (the data is handed over as it is, with its version).
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import protocol as P
from .const import DOMAIN, LEGACY_BACKUP_STORE_KEY, LEGACY_OPTION_KEYS, LEGACY_STORES, VERSION

_LOGGER = logging.getLogger(__name__)

BACKUP_SCHEMA = 1


class _AnyVersionStore(Store[dict[str, Any]]):
    """Reads a store whatever version it was written with, and never writes: the export neither migrates nor
    rewrites a legacy store (a plain ``Store`` would save the file back under its own version after a mismatch)."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        super().__init__(hass, 1, key)
        self.found_version: tuple[int, int] | None = None

    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]) -> Any:
        self.found_version = (old_major_version, old_minor_version)
        return old_data

    async def async_save(self, data: Any) -> None:  # read-only: the version header on disk stays as it was
        return None

    def async_delay_save(self, data_func: Any, delay: float = 0) -> None:  # read-only
        return None


def legacy_options(entry: ConfigEntry) -> dict[str, Any] | None:
    """The 0.9.0 options still carried by the entry, or None when they were retired."""
    options = dict(entry.options)
    return options if any(k in options for k in LEGACY_OPTION_KEYS) else None


def legacy_summary_sync(hass: HomeAssistant, entry: ConfigEntry, retired: dict[str, Any] | None) -> dict[str, Any]:
    """What a session's hello says about legacy data (no store is read here: the export does that)."""
    options = legacy_options(entry)
    rooms = (options or {}).get("rooms") or {}
    return {
        "present": options is not None,
        "retired": retired,
        "options_revision": P.revision(options) if options is not None else None,
        "rooms": len(rooms) if isinstance(rooms, dict) else 0,
        "entry_minor_version": entry.minor_version,
    }


async def async_export(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    stores: dict[str, Any] = {}
    for key in LEGACY_STORES:
        store = _AnyVersionStore(hass, key)
        try:
            data = await store.async_load()
        except Exception as err:  # noqa: BLE001 - one unreadable store must not hide the others
            _LOGGER.warning("legacy store %s could not be read: %s", key, err)
            stores[key] = {"error": str(err)[:200]}
            continue
        if data is None:
            stores[key] = None
            continue
        version = store.found_version or (1, 1)
        stores[key] = {"version": version[0], "minor_version": version[1], "data": data}
    options = legacy_options(entry)
    owners = {}
    for domain, key in (("occupancy", f"{DOMAIN}.engine_ownership"), ("lighting", f"{DOMAIN}.lighting_ownership"),
                        ("curtains", f"{DOMAIN}.curtains_ownership"), ("forecast", f"{DOMAIN}.forecast_ownership")):
        record = (stores.get(key) or {}).get("data") if isinstance(stores.get(key), dict) else None
        owners[domain] = (record or {}).get("owner") if isinstance(record, dict) else None
    return {
        "schema": BACKUP_SCHEMA,
        "exported_at": dt_util.utcnow().isoformat(timespec="seconds"),
        "companion_version": VERSION,
        "entry": {"entry_id": entry.entry_id, "title": entry.title, "version": entry.version,
                  "minor_version": entry.minor_version, "options": options},
        "options_revision": P.revision(options) if options is not None else None,
        "legacy_owners": owners,
        "stores": stores,
    }


def _has_legacy_data(export: dict[str, Any]) -> bool:
    return export["entry"]["options"] is not None or any(
        isinstance(v, dict) and "data" in v for v in export["stores"].values())


async def async_retire(hass: HomeAssistant, entry: ConfigEntry, by: str | None) -> dict[str, Any]:
    """Back the legacy data up, then remove it from the active installation. Reviewed, explicit, reversible."""
    export = await async_export(hass, entry)
    if not _has_legacy_data(export):
        raise P.ProtocolError(P.REJECT_NO_LEGACY, "there is no legacy data to retire")
    backup = Store[dict[str, Any]](hass, BACKUP_SCHEMA, LEGACY_BACKUP_STORE_KEY)
    existing = await backup.async_load()
    previous = (existing or {}).get("export") if isinstance((existing or {}).get("export"), dict) else None
    if previous is not None:
        # The backup store is the only copy of the retired data: never replace it with a weaker export (for
        # example after a half-finished restore). Restore first, then retire again.
        old_stores = {k for k, v in (previous.get("stores") or {}).items() if isinstance(v, dict) and "data" in v}
        new_stores = {k for k, v in export["stores"].items() if isinstance(v, dict) and "data" in v}
        if (previous["entry"].get("options") is not None and export["entry"]["options"] is None) or not old_stores <= new_stores:
            raise P.ProtocolError(P.REJECT_BUSY, "the legacy backup holds data this retirement would drop; restore "
                                                 "the legacy data first, then retire again")
    history = [h for h in ((existing or {}).get("history") or []) if isinstance(h, dict)][-4:]
    if existing and existing.get("export"):
        history.append({"backed_up_at": existing.get("backed_up_at"), "by": existing.get("by"),
                        "options_revision": (existing.get("export") or {}).get("options_revision")})
    payload = {"backed_up_at": export["exported_at"], "by": by, "export": export, "history": history}
    await backup.async_save(payload)
    removed = []
    for key, value in export["stores"].items():
        if isinstance(value, dict) and "data" in value:
            await Store(hass, 1, key).async_remove()
            removed.append(key)
    if export["entry"]["options"] is not None:
        hass.config_entries.async_update_entry(
            entry, options={"companion": {"retired_at": export["exported_at"], "by": by,
                                          "backup": LEGACY_BACKUP_STORE_KEY,
                                          "options_revision": export["options_revision"]}})
    _LOGGER.warning("legacy_retired by=%s stores=%d options=%s backup=%s", by, len(removed),
                    export["entry"]["options"] is not None, LEGACY_BACKUP_STORE_KEY)
    return {"retired_at": export["exported_at"], "stores_removed": removed, "backup": LEGACY_BACKUP_STORE_KEY,
            "options_revision": export["options_revision"]}


async def async_backup_info(hass: HomeAssistant) -> dict[str, Any] | None:
    backup = await Store[dict[str, Any]](hass, BACKUP_SCHEMA, LEGACY_BACKUP_STORE_KEY).async_load()
    if not backup or not isinstance(backup.get("export"), dict):
        return None
    export = backup["export"]
    return {"backed_up_at": backup.get("backed_up_at"), "by": backup.get("by"),
            "options_revision": export.get("options_revision"),
            "stores": sorted(k for k, v in (export.get("stores") or {}).items() if isinstance(v, dict) and "data" in v),
            "rooms": len(((export.get("entry") or {}).get("options") or {}).get("rooms") or {}),
            "history": backup.get("history") or []}


async def async_restore(hass: HomeAssistant, entry: ConfigEntry, by: str | None) -> dict[str, Any]:
    """Write the retired data back (before a rollback to 0.9.0). The current companion data is untouched."""
    backup = await Store[dict[str, Any]](hass, BACKUP_SCHEMA, LEGACY_BACKUP_STORE_KEY).async_load()
    export = (backup or {}).get("export")
    if not isinstance(export, dict) or not _has_legacy_data(export):
        raise P.ProtocolError(P.REJECT_NO_LEGACY, "there is no legacy backup to restore")
    # The options first (what 0.9.0 needs most), then the stores with the version they were written with.
    options = (export.get("entry") or {}).get("options")
    if isinstance(options, dict):
        hass.config_entries.async_update_entry(entry, options=options)
    restored = []
    for key, value in (export.get("stores") or {}).items():
        if key in LEGACY_STORES and isinstance(value, dict) and isinstance(value.get("data"), (dict, list)):
            await Store(hass, int(value.get("version") or 1), key,
                        minor_version=int(value.get("minor_version") or 1)).async_save(value["data"])
            restored.append(key)
    _LOGGER.warning("legacy_restored by=%s stores=%d options=%s", by, len(restored), isinstance(options, dict))
    return {"restored_at": dt_util.utcnow().isoformat(timespec="seconds"), "stores_restored": restored,
            "options": isinstance(options, dict), "options_revision": export.get("options_revision")}


def stamp(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None
