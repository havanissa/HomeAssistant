"""Persistent JSON stores (``homeassistant.helpers.storage.Store``), in Home Assistant's file format.

Files live in ``hass.storage_dir`` (the App's data folder for the authoritative
runtime). With no storage folder the store keeps its data in memory (the
non-commanding shadow runtime must never leave files behind). Writes are atomic
(temporary file, then rename); ``async_delay_save`` coalesces like Home
Assistant's. The host is told about every save (``HostBackend.store_saved``), so
the App can mirror its configuration and state to the integration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Generic, TypeVar

from ..core import CALLBACK_TYPE, HomeAssistant
from ..exceptions import HomeAssistantError
from .event import async_call_later

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_MEMORY = "_host_memory_stores"
_ALL = "_host_stores"


class UnsupportedStorageVersionError(HomeAssistantError):
    """The stored data is newer than this code."""


class Store(Generic[_T]):
    def __init__(self, hass: HomeAssistant, version: int, key: str, private: bool = False, *,
                 atomic_writes: bool = False, encoder: Any = None, minor_version: int = 1,
                 read_only: bool = False) -> None:
        self.hass = hass
        self.version = version
        self.minor_version = minor_version
        self.key = key
        self._encoder = encoder
        self._read_only = read_only
        self._pending: Callable[[], Any] | None = None
        self._timer: CALLBACK_TYPE | None = None
        self._lock = asyncio.Lock()
        hass.data.setdefault(_ALL, set()).add(self)

    @property
    def path(self) -> Path | None:
        return Path(self.hass.storage_dir) / self.key if self.hass.storage_dir else None

    def _memory(self) -> dict[str, Any]:
        return self.hass.data.setdefault(_MEMORY, {})

    def _read_raw(self) -> Any:
        path = self.path
        if path is None:
            raw = self._memory().get(self.key)
            return json.loads(raw) if raw is not None else None
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None

    async def async_load(self) -> _T | None:
        async with self._lock:
            if self._pending is not None:  # unsaved data is the newest (as in Home Assistant)
                return self._pending()
            raw = await asyncio.to_thread(self._read_raw) if self.path else self._read_raw()
        if raw is None:
            return None
        if not isinstance(raw, dict) or "data" not in raw:
            raise HomeAssistantError(f"Invalid storage file for {self.key}")
        major, minor = raw.get("version", 1), raw.get("minor_version", 1)
        data = raw["data"]
        if major == self.version and minor == self.minor_version:
            return data
        if major > self.version:
            raise UnsupportedStorageVersionError(f"{self.key}: stored version {major} is newer than {self.version}")
        migrate = self._async_migrate_func
        if len(inspect.signature(migrate).parameters) == 2:
            return await migrate(major, data)  # type: ignore[call-arg]
        return await migrate(major, minor, data)

    async def _async_migrate_func(self, old_major_version: int, old_minor_version: int, old_data: Any) -> Any:
        raise NotImplementedError

    def _payload(self, data: Any) -> str:
        return json.dumps({"version": self.version, "minor_version": self.minor_version, "key": self.key,
                           "data": data}, cls=self._encoder, indent=2, ensure_ascii=False)

    def _write(self, text: str) -> None:
        path = self.path
        if path is None:
            self._memory()[self.key] = text
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    async def async_save(self, data: _T) -> None:
        self._cancel_timer()
        self._pending = None
        await self._save(data)

    async def _save(self, data: Any) -> None:
        if self._read_only:
            return
        async with self._lock:
            text = self._payload(data)
            if self.path is not None:
                await asyncio.to_thread(self._write, text)
            else:
                self._write(text)
        backend = self.hass.backend
        if backend is not None and hasattr(backend, "store_saved"):
            backend.store_saved(self.key, data)

    def async_delay_save(self, data_func: Callable[[], _T], delay: float = 0) -> None:
        self._pending = data_func
        self._cancel_timer()

        def fire(_now: Any) -> None:
            self._timer = None
            self.hass.async_create_task(self._delayed_write(), f"store {self.key}")

        self._timer = async_call_later(self.hass, delay, fire)

    async def _delayed_write(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            await self._save(pending())

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer()
            self._timer = None

    async def async_flush(self) -> None:
        """Write a pending delayed save now (the host calls this before it stops)."""
        self._cancel_timer()
        await self._delayed_write()

    async def async_remove(self) -> None:
        self._cancel_timer()
        self._pending = None
        path = self.path
        if path is None:
            self._memory().pop(self.key, None)
        else:
            await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


async def async_flush_all(hass: HomeAssistant) -> None:
    for store in list(hass.data.get(_ALL, ())):
        try:
            await store.async_flush()
        except Exception:  # noqa: BLE001 - flush the others
            _LOGGER.exception("Could not write %s", store.key)
