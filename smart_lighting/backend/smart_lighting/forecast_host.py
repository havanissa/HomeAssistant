"""Runs the integration's forecast manager inside the App (Phase A5).

One :class:`ForecastInstance` is one ``ForecastManager`` (the vendored,
byte-identical integration code) with its own host (``backend/homeassistant``):

* **History** - the manager's history source is Home Assistant's websocket
  command ``history/history_during_period`` (bounded windows, one local day at a
  time, significant changes only, no attributes): the same rows the integration
  reads through the Recorder's own API, through a supported API instead of the
  database. The App never touches the Recorder's files and assumes no backend.
  The oldest history the Recorder still has is what the integration reports over
  the bridge (there is no websocket command for it); without it the learning
  simply probes the configured window day by day, once, as the integration does.
* **Occupancy** - the published room occupancy (the integration's engine, or the
  App's after the occupancy handover) through :class:`AppOccupancy`.
* **Configuration** - the integration's options (rooms and sensors) from the
  bridge; the forecast settings and the learned days in the instance's own stores
  (memory for the shadow, ``/data/predictions/store`` for the owner).
* **Decisions** - the manager's observations (predicted, confirmed, missed,
  pattern changed) go to the instance's own bounded decision log; nothing here
  commands anything.

Model building and the daily replay run in the executor (``async_add_executor_job``),
off the event loop, exactly as in the integration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, State
from homeassistant.helpers import _registry, storage
from homeassistant.helpers.storage import Store

from .lighting_host import DOMAIN, AppBackend, AppCoordinator, AppOccupancy

_LOGGER = logging.getLogger(__name__)

SETTINGS_STORE_KEY = f"{DOMAIN}.forecast_settings"
DAYS_STORE_KEY = f"{DOMAIN}.forecast_days"
LOG_STORE_KEY = f"{DOMAIN}.forecast_log"
HISTORY_TIMEOUT = 60.0


def _wire_store(version: int, minor: int, key: str, data: Any) -> str:
    return json.dumps({"version": version, "minor_version": minor, "key": key, "data": data})


class _LightingShim:
    """What the forecast manager reads from ``coordinator.lighting``: only the decision log (replaced by the sink)."""

    def __init__(self, decisions: Any) -> None:
        self.decisions = decisions


class ForecastInstance:
    """One running forecast manager of the App (shadow or owner)."""

    def __init__(self, ha: Any, *, role: str, options: dict[str, Any], export: dict[str, Any] | None,
                 published: Callable[[str], Any], storage_dir: Path | None,
                 recorder_oldest: Callable[[], float | None]) -> None:
        self.ha = ha
        self.role = role
        self.backend = AppBackend(ha, execute=None)
        self.hass = HomeAssistant(self.backend, asyncio.get_running_loop())
        self.hass.storage_dir = str(storage_dir) if storage_dir else None
        self.occupancy = AppOccupancy(published, lambda *_: None)
        self.coordinator = AppCoordinator(self.hass, options, self.occupancy)
        self.hass.config_entries.add(self.coordinator.config_entry)
        self._seed = export
        self._recorder_oldest = recorder_oldest
        self.manager: Any = None
        self.decisions: Any = None
        self._log_store: Store[dict[str, Any]] | None = None
        self.started = False
        self.history_calls = 0

    # ---------------------------------------------------------------- feeding
    def load_home_assistant(self) -> None:
        ha, hass = self.ha, self.hass
        hass.config.update_from_wire(ha.config or {})
        hass.state = CoreState.running if (ha.config or {}).get("state") in (None, "RUNNING") else CoreState.starting
        hass.states.replace_all(s for s in (State.from_wire(v) for v in ha.states.values()) if s is not None)
        self.install_registries()

    def install_registries(self) -> None:
        _registry.install(self.hass, entities=self.ha.entity_registry, devices=self.ha.devices, areas=self.ha.areas,
                          labels=self.ha.labels)

    def state_changed(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        if self.manager is None:
            return
        self.hass.async_state_changed(entity_id, State.from_wire(new_state) if new_state else None)

    def state_reported(self, entity_id: str, wire: dict[str, Any] | None) -> None:
        """An unchanged report: only ``last_reported`` moves (no event, as in Home Assistant)."""
        if self.manager is None or not wire:
            return
        if (state := State.from_wire(wire)) is not None:
            self.hass.async_state_reported(entity_id, state)

    def registries_changed(self) -> None:
        self.install_registries()

    def occupancy_changed(self, area_id: str, fired: Any = None) -> None:
        if self.manager is not None and self.started:
            try:
                self.hass.event_time = datetime.fromisoformat(fired) if isinstance(fired, str) else None
            except ValueError:
                self.hass.event_time = None
            try:
                self.occupancy.notify(area_id)
            finally:
                self.hass.event_time = None

    def options_changed(self, options: dict[str, Any]) -> None:
        self.coordinator.set_options(options)
        mgr = self.manager
        if mgr is not None:
            mgr._spec = None  # noqa: SLF001 - the replay specification follows the rooms

    # --------------------------------------------------------------- history
    async def fetch_history(self, entity_ids: list[str], start: datetime, end: datetime) -> dict[str, list[tuple[float, str]]]:
        """The integration's ``_recorder_fetch`` through the websocket API: (timestamp, state) rows per entity."""
        self.history_calls += 1
        try:
            async with asyncio.timeout(HISTORY_TIMEOUT):
                raw = await self.ha.command(
                    "history/history_during_period", start_time=start.isoformat(), end_time=end.isoformat(),
                    entity_ids=list(entity_ids), minimal_response=True, no_attributes=True,
                    significant_changes_only=True, include_start_time_state=True)
        except TimeoutError as err:
            raise RuntimeError("history request timed out") from err
        out: dict[str, list[tuple[float, str]]] = {}
        for entity_id, items in (raw or {}).items() if isinstance(raw, dict) else ():
            rows: list[tuple[float, str]] = []
            for item in items or ():
                if not isinstance(item, dict):
                    continue
                ts = item.get("lc") or item.get("lu")
                state = item.get("s")
                if isinstance(ts, (int, float)) and not isinstance(ts, bool) and state is not None:
                    rows.append((float(ts), str(state)))
            out[str(entity_id)] = rows
        return out

    # -------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        from smart_occupancy.const import (  # noqa: PLC0415
            FORECAST_DAYS_STORE_VERSION,
            FORECAST_SETTINGS_STORE_VERSION,
        )
        from smart_occupancy.decisions import DecisionLog  # noqa: PLC0415
        from smart_occupancy.forecast import ForecastManager  # noqa: PLC0415

        hass = self.hass
        seed = self._seed
        if seed is not None:
            seeds = {
                SETTINGS_STORE_KEY: (FORECAST_SETTINGS_STORE_VERSION, 1, seed.get("settings") or {}),
                DAYS_STORE_KEY: (FORECAST_DAYS_STORE_VERSION, 1, {
                    "days": seed.get("days") or {}, "no_data": sorted(seed.get("no_data") or []),
                    "recorder_oldest": seed.get("recorder_oldest"), "last_update": seed.get("last_update")}),
            }
            for key, (version, minor, data) in seeds.items():
                text = _wire_store(version, minor, key, data)
                if hass.storage_dir is None:
                    hass.data.setdefault("_host_memory_stores", {})[key] = text
                else:
                    path = Path(hass.storage_dir) / key
                    path.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(path.write_text, text)
        self.load_home_assistant()
        self.decisions = DecisionLog(hass, self._log_changed)
        self._log_store = Store(hass, 1, LOG_STORE_KEY)
        with contextlib.suppress(Exception):
            if (stored := await self._log_store.async_load()) is not None:
                self.decisions.restore(stored)
        self.coordinator.lighting = _LightingShim(self.decisions)
        mgr = ForecastManager(hass, self.coordinator)
        mgr.fetch_history = self.fetch_history
        mgr.oldest_history = self._recorder_oldest
        mgr.decision_sink = self.decisions.record
        self.coordinator.forecast = mgr
        self.manager = mgr
        await mgr.async_load()
        mgr.async_start()
        self.started = True
        if hass.state is CoreState.running:
            self.coordinator.settled = True

    def _log_changed(self) -> None:
        if self._log_store is not None and self.role == "owner" and self.decisions is not None:
            self._log_store.async_delay_save(self.decisions.persist, 5)

    def ha_started(self) -> None:
        if self.hass.state is not CoreState.running:
            self.hass.state = CoreState.running
            self.hass.bus.async_fire_internal(Event(EVENT_HOMEASSISTANT_STARTED))
            self.coordinator.settled = True

    async def async_stop(self, *, flush: bool = True) -> None:
        mgr = self.manager
        if mgr is not None:
            mgr.async_stop()
            if flush and self.role == "owner":
                with contextlib.suppress(Exception):
                    await mgr.async_flush()
        if self.decisions is not None:
            self.decisions.async_stop()
        self.started = False
        if flush and self.role == "owner":
            await storage.async_flush_all(self.hass)
        await self.backend.async_close()
        await self.hass.async_cancel_tasks()
