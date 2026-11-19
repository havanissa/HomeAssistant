"""Runs the integration's curtain manager inside the App (Phase A5).

One :class:`CurtainInstance` is one ``CurtainManager`` (the vendored, byte-identical
``curtains`` package in ``backend/smart_occupancy``) with its own host
(``backend/homeassistant``), built the same way as the lighting instance
(``lighting_host.py``): the App's websocket client feeds every state change (with
Home Assistant's times and contexts) into the host, registries and the
automation / script configurations (for conflict detection) are installed as the
integration would see them, the published room occupancy is given through the
same :class:`AppOccupancy` interface, and the host backend's service call is the
only way out. In the shadow it goes to a recorder and reaches nothing; for the
owner it is the integration's fenced ``curtains/bridge/execute``.

Nothing here moves a cover on its own: a manager starts exactly as the
integration's does (load, then the start-up settle period, then the rules).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
import contextlib
from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, State
from homeassistant.helpers import _registry, storage

from .lighting_host import DOMAIN, AppBackend, AppCoordinator, AppOccupancy
from .lighting_scripts import AutomationEntity, Component, ScriptEntity

_LOGGER = logging.getLogger(__name__)

CURTAINS_STORE_KEY = f"{DOMAIN}.curtains"
CURTAINS_STATE_STORE_KEY = f"{DOMAIN}.curtains_state"


def _wire_store(version: int, minor: int, key: str, data: Any) -> str:
    return json.dumps({"version": version, "minor_version": minor, "key": key, "data": data})


class CurtainInstance:
    """One running curtain manager of the App (shadow or owner)."""

    def __init__(self, ha: Any, *, role: str, options: dict[str, Any], config: dict[str, Any] | None,
                 state: dict[str, Any] | None, published: Callable[[str], Any], backend: AppBackend,
                 storage_dir: Path | None, automations: list[AutomationEntity] | None = None,
                 scripts: list[ScriptEntity] | None = None) -> None:
        self.ha = ha
        self.role = role  # "shadow" | "owner"
        self.backend = backend
        self.hass = HomeAssistant(backend, asyncio.get_running_loop())
        self.hass.storage_dir = str(storage_dir) if storage_dir else None
        self.occupancy = AppOccupancy(published, lambda *_: None)
        self.coordinator = AppCoordinator(self.hass, options, self.occupancy)
        self.hass.config_entries.add(self.coordinator.config_entry)
        self._seed_config = config
        self._seed_state = state
        self._automations = list(automations or [])
        self._scripts = list(scripts or [])
        self.manager: Any = None
        self.started = False

    # ---------------------------------------------------------------- feeding
    def load_home_assistant(self) -> None:
        ha, hass = self.ha, self.hass
        hass.config.update_from_wire(ha.config or {})
        hass.state = CoreState.running if (ha.config or {}).get("state") in (None, "RUNNING") else CoreState.starting
        hass.states.replace_all(s for s in (State.from_wire(v) for v in ha.states.values()) if s is not None)
        self.install_registries()
        hass.data["automation"] = Component(self._automations)
        hass.data["script"] = Component(self._scripts)

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
        if self.manager is not None and self.started:
            self.manager.rescan()

    def automations_changed(self, automations: list[AutomationEntity], scripts: list[ScriptEntity]) -> None:
        self._automations, self._scripts = list(automations), list(scripts)
        self.hass.data["automation"] = Component(automations)
        self.hass.data["script"] = Component(scripts)
        self.hass.bus.async_fire_internal(Event("automation_reloaded"))

    def occupancy_changed(self, area_id: str, fired: Any = None) -> None:
        if self.manager is not None and self.started:
            with self.at_event_time(fired):
                self.occupancy.notify(area_id)

    def config_changed(self, config: Any, fired: Any = None) -> None:
        if self.manager is not None:
            with self.at_event_time(fired):
                self.manager.async_set(config)

    @contextlib.contextmanager
    def at_event_time(self, fired: Any) -> Iterator[None]:
        try:
            self.hass.event_time = datetime.fromisoformat(fired) if isinstance(fired, str) else None
        except ValueError:
            self.hass.event_time = None
        try:
            yield
        finally:
            self.hass.event_time = None

    # -------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Seed the stores with the copy, load and start the manager (start applies nothing)."""
        from smart_occupancy.curtains.manager import (  # noqa: PLC0415
            STATE_STORE_VERSION,
            STORE_MINOR_VERSION,
            STORE_VERSION,
            CurtainManager,
        )

        hass = self.hass
        seeds = {CURTAINS_STORE_KEY: (STORE_VERSION, STORE_MINOR_VERSION, self._seed_config),
                 CURTAINS_STATE_STORE_KEY: (STATE_STORE_VERSION, 1, self._seed_state)}
        for key, (version, minor, data) in seeds.items():
            if data is None:
                continue
            text = _wire_store(version, minor, key, data)
            if hass.storage_dir is None:
                hass.data.setdefault("_host_memory_stores", {})[key] = text
            else:
                path = Path(hass.storage_dir) / key
                path.parent.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread(path.write_text, text)
        self.load_home_assistant()
        mgr = CurtainManager(hass, self.occupancy)
        self.coordinator.curtains = mgr
        self.manager = mgr
        await mgr.async_load()
        mgr.async_start()
        self.started = True
        if hass.state is CoreState.running:
            self.coordinator.settled = True

    def ha_started(self) -> None:
        if self.hass.state is not CoreState.running:
            self.hass.state = CoreState.running
            self.hass.bus.async_fire_internal(Event(EVENT_HOMEASSISTANT_STARTED))
            self.coordinator.settled = True

    async def async_stop(self, *, flush: bool = True) -> None:
        mgr = self.manager
        if mgr is not None:
            mgr.fenced = True
            if flush and self.role == "owner":
                with contextlib.suppress(Exception):
                    await mgr.async_flush()
            mgr.async_stop()
        self.started = False
        if flush and self.role == "owner":
            await storage.async_flush_all(self.hass)
        await self.backend.async_close()
        await self.hass.async_cancel_tasks()
