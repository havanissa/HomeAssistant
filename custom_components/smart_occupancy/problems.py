"""Problem history: remembers when each current problem was first detected.

Stored in ``.storage/smart_occupancy.problem_history`` (not the Recorder) so
"first detected" survives reloads and restarts. The file is written only
when the set of current problems changes, with a short delay to coalesce
bursts.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import PROBLEM_STORE_KEY, PROBLEM_STORE_VERSION
from .validation import Problem

SAVE_DELAY_SECONDS = 10


class ProblemTracker:
    """Tracks first-detected timestamps of the current problems."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, PROBLEM_STORE_VERSION, PROBLEM_STORE_KEY)
        self._first: dict[str, str] = {}
        self._dirty = False

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self._first = {k: v for k, v in (data.get("first_detected") or {}).items() if isinstance(v, str)}

    def update(self, problems: Iterable[Problem], now: datetime, *, prune: bool) -> dict[str, str]:
        """Record new problems; forget resolved ones when ``prune`` is True.

        Pruning is skipped while Home Assistant is still starting, so problems
        that existed before a restart keep their original timestamp.
        """
        keys = {p.key for p in problems}
        changed = False
        stamp = now.isoformat(timespec="seconds")
        for key in keys - self._first.keys():
            self._first[key] = stamp
            changed = True
        if prune:
            for key in self._first.keys() - keys:
                del self._first[key]
                changed = True
        if changed:
            self._dirty = True
            self._store.async_delay_save(self._data, SAVE_DELAY_SECONDS)
        return {key: self._first[key] for key in keys}

    def _data(self) -> dict[str, Any]:
        self._dirty = False
        return {"first_detected": dict(self._first)}

    async def async_flush(self) -> None:
        """Write pending changes now (on unload, so a reload sees them).

        A direct save also cancels the pending delayed write.
        """
        if self._dirty:
            await self._store.async_save(self._data())

    async def async_remove(self) -> None:
        await self._store.async_remove()
