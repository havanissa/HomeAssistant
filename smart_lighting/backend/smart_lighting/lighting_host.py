"""Runs the integration's lighting controller inside the App (Phase A4).

One :class:`LightingInstance` is one ``LightingController`` (the vendored,
byte-identical integration code in ``backend/smart_occupancy``) with its own
host (``backend/homeassistant``):

* **States and events** - the App's websocket client feeds every
  ``state_changed`` (with Home Assistant's times and contexts) into the host's
  state machine, in order; registry lists and the automation / script
  configurations (for conflict detection) are installed as the integration
  would see them.
* **Occupancy** - :class:`AppOccupancy` gives the controller the *published*
  occupancy of each room (the integration's engine, or the App's after the
  occupancy handover) and forwards its occupancy inputs (possible occupancy
  signals, zone holds, switch activity) to the integration while the App
  controls lighting.
* **Configuration** - :class:`AppCoordinator` holds the integration's options
  (rooms, sensors, light types, mood source); the lighting configuration is in
  the controller's own store.
* **Commands** - the host backend's service call is the only way out. In the
  shadow it goes to a recorder and reaches nothing; for the owner it is the
  integration's fenced ``execute``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, Context, CoreState, Event, HomeAssistant, HostBackend, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import _registry, storage
from homeassistant.components import mqtt as mqtt_component

from occupancy_core.wire import WireError, result_from_wire

from .lighting_scripts import AutomationEntity, Component, ScriptEntity

_LOGGER = logging.getLogger(__name__)

DOMAIN = "smart_occupancy"
ENTRY_ID = "smart_lighting_app"
LIGHTING_STORE_KEY = f"{DOMAIN}.lighting"
LIGHTING_STATE_STORE_KEY = f"{DOMAIN}.lighting_state"


def _wire_store(version: int, minor: int, key: str, data: Any) -> str:
    return json.dumps({"version": version, "minor_version": minor, "key": key, "data": data})


class AppOccupancy:
    """The occupancy interface the lighting controller uses (``OccupancyManager``'s, in part)."""

    def __init__(self, published: Callable[[str], Any], send_input: Callable[[str, dict[str, Any]], None]) -> None:
        self._published = published  # area -> RoomResult | None
        self._send_input = send_input
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self.inputs: dict[str, Any] = {"preliminary": {}, "holds": {}}
        self.engines: dict[str, Any] = {}

    def result(self, area_id: str) -> Any:
        return self._published(area_id)

    def async_add_listener(self, area_id: str, update: Callable[[], None]) -> CALLBACK_TYPE:
        listeners = self._listeners.setdefault(area_id, [])
        listeners.append(update)
        return lambda: listeners.remove(update) if update in listeners else None

    def notify(self, area_id: str) -> None:
        for update in list(self._listeners.get(area_id, ())):
            try:
                update()
            except Exception:  # noqa: BLE001 - as the integration's manager: one room never breaks another
                _LOGGER.exception("Lighting occupancy listener failed for %s", area_id)

    def async_set_preliminary(self, signals: dict[str, frozenset[str]]) -> None:
        wanted = {a: sorted(v) for a, v in signals.items()}
        self.inputs["preliminary"] = wanted
        self._send_input("preliminary", {"signals": wanted})

    def async_set_holds(self, area_id: str, names: frozenset[str]) -> None:
        self.inputs["holds"][area_id] = sorted(names)
        self._send_input("hold", {"area_id": area_id, "zones": sorted(names)})

    def async_note_activity(self, area_id: str, source: str) -> bool:
        self._send_input("activity", {"area_id": area_id, "source": source})
        return True

    def resend(self) -> None:
        """After (re)gaining control: tell the integration's engine every current input again."""
        self._send_input("preliminary", {"signals": self.inputs["preliminary"]})
        for area_id, zones in self.inputs["holds"].items():
            self._send_input("hold", {"area_id": area_id, "zones": zones})


class _NoForecast:
    def summary(self, area_id: str) -> None:
        return None


class _Snapshot:
    """The problem list part of the coordinator's snapshot (the lighting problems of the shared validation)."""

    def __init__(self, problems: list[Any]) -> None:
        self.problems = problems
        self.rooms: dict[str, Any] = {}
        self.first_detected: dict[str, Any] = {}
        self.last_checked = None

    def problem_dict(self, p: Any) -> dict[str, Any]:
        return p.as_dict(self.first_detected.get(p.key), self.last_checked)

    def problems_where(self, *, category: Any = None, area_id: str | None = None,
                       include_info: bool = False) -> list[Any]:
        from smart_occupancy.const import Severity  # noqa: PLC0415

        return [p for p in self.problems if (include_info or p.severity != Severity.INFO)
                and (category is None or p.category == category) and (area_id is None or p.area_id == area_id)]


class AppCoordinator:
    """What the controller and the lighting websocket handlers read from the integration's coordinator."""

    def __init__(self, hass: HomeAssistant, options: dict[str, Any], occupancy: AppOccupancy) -> None:
        from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

        self.hass = hass
        self.options = dict(options)
        self.config = IntegrationConfig.from_dict(self.options)
        self.occupancy = occupancy
        self.forecast = _NoForecast()
        self.data: _Snapshot | None = _Snapshot([])
        self.settled = False
        self.lighting: Any = None
        self.config_entry = ConfigEntry(ENTRY_ID, DOMAIN, title="Smart Occupancy & Lighting", options=self.options,
                                        state=ConfigEntryState.LOADED)
        self.config_entry.runtime_data = self
        self.last_update_success = True
        self._refresh: asyncio.TimerHandle | None = None

    def set_options(self, options: dict[str, Any]) -> None:
        from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

        self.options = dict(options)
        self.config = IntegrationConfig.from_dict(self.options)
        self.config_entry.options = self.options

    def async_schedule_refresh(self) -> None:
        """The integration re-validates after lighting changes; the App refreshes its problem list (debounced)."""
        if self._refresh is None:
            self._refresh = self.hass.loop.call_later(1.0, self._validate)

    def _validate(self) -> None:
        self._refresh = None
        ctrl = self.lighting
        if ctrl is None:
            return
        try:
            from smart_occupancy.validation import validate_daylight, validate_lighting  # noqa: PLC0415

            problems = validate_lighting(self.hass, self.config, ctrl)
            problems += validate_daylight(self.hass, self.config, ctrl, settled=self.settled)
        except Exception:  # noqa: BLE001 - problems are diagnostics; never break lighting
            _LOGGER.debug("lighting validation failed", exc_info=True)
            return
        self.data = _Snapshot(problems)


class AppBackend(HostBackend):
    """Remote operations of one lighting instance (through the App's allowlisted websocket client)."""

    def __init__(self, ha: Any, *, execute: Callable[[str, str, dict[str, Any], Context], Awaitable[Any]] | None,
                 record: Callable[[dict[str, Any]], None] | None = None,
                 update_options: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
                 store_saved: Callable[[str, Any], None] | None = None) -> None:
        self.ha = ha
        self._execute = execute
        self._record = record
        self._update_options = update_options
        self._store_saved = store_saved
        self.pending_options: list[asyncio.Task[Any]] = []
        self._subscriptions: set[int] = set()
        self.closed = False

    async def async_call_service(self, domain: str, service: str, data: dict[str, Any], context: Context,
                                 blocking: bool) -> Any:
        if self.closed:
            raise HomeAssistantError("lighting instance stopped")
        if self._execute is None:  # the shadow: nothing leaves the App
            if self._record is not None:
                self._record({"domain": domain, "service": service, "data": data, "context_id": context.id})
            return None
        return await self._execute(domain, service, data, context)

    async def async_validate_triggers(self, configs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from homeassistant.helpers.config_validation import vol  # noqa: PLC0415

        result = await self.ha.command("validate_config", trigger=configs)
        verdict = (result or {}).get("trigger") or {}
        if not verdict.get("valid"):
            raise vol.Invalid(str(verdict.get("error") or "invalid trigger"))
        return configs

    async def async_subscribe_triggers(self, configs: list[dict[str, Any]],
                                       action: Callable[..., Any]) -> CALLBACK_TYPE:
        loop = asyncio.get_running_loop()

        def handle(event: Any) -> None:
            variables = (event or {}).get("variables") or {}
            context = Context.from_wire((event or {}).get("context"))
            result = action(variables, context)
            if asyncio.iscoroutine(result):
                loop.create_task(result)

        sub_id = await self.ha.subscribe("subscribe_trigger", handle, trigger=configs)
        return self._unsubscriber(sub_id)

    async def async_device_triggers(self, device_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for device_id in device_ids:
            try:
                found = await self.ha.command("device_automation/trigger/list", device_id=device_id)
            except (HomeAssistantError, ConnectionError, TimeoutError) as err:
                _LOGGER.debug("device triggers of %s: %s", device_id, err)
                continue
            out[device_id] = [t for t in found or [] if isinstance(t, dict)]
        return out

    async def async_mqtt_subscribe(self, topic: str, handler: Callable[[Any], None]) -> CALLBACK_TYPE:
        sub_id = await self.ha.subscribe("mqtt/subscribe", lambda event: handler(event or {}), topic=topic, qos=0)
        return self._unsubscriber(sub_id)

    def mqtt_connected(self) -> bool:
        return self.ha.connected

    def _unsubscriber(self, sub_id: int) -> CALLBACK_TYPE:
        self._subscriptions.add(sub_id)

        def remove() -> None:
            if sub_id in self._subscriptions:
                self._subscriptions.discard(sub_id)
                asyncio.get_running_loop().create_task(self.ha.unsubscribe(sub_id))

        return remove

    def async_update_options(self, entry: Any, options: dict[str, Any]) -> bool:
        if self._update_options is None:
            raise HomeAssistantError("The integration's options cannot be changed by this lighting instance")
        task = asyncio.get_running_loop().create_task(self._update_options(options))
        self.pending_options.append(task)
        return True

    def store_saved(self, key: str, data: Any) -> None:
        if self._store_saved is not None:
            self._store_saved(key, data)

    async def async_close(self) -> None:
        self.closed = True
        for sub_id in list(self._subscriptions):
            with contextlib.suppress(Exception):
                await self.ha.unsubscribe(sub_id)
        self._subscriptions.clear()


class LightingInstance:
    """One running lighting controller of the App (shadow or owner)."""

    def __init__(self, ha: Any, *, role: str, options: dict[str, Any], config: dict[str, Any], state: dict[str, Any] | None,
                 published: Callable[[str], Any], send_input: Callable[[str, dict[str, Any]], None],
                 backend: AppBackend, storage_dir: Path | None, automations: list[AutomationEntity] | None = None,
                 scripts: list[ScriptEntity] | None = None) -> None:
        self.ha = ha
        self.role = role  # "shadow" | "owner"
        self.backend = backend
        self.hass = HomeAssistant(backend, asyncio.get_running_loop())
        self.hass.storage_dir = str(storage_dir) if storage_dir else None
        self.occupancy = AppOccupancy(published, send_input)
        self.coordinator = AppCoordinator(self.hass, options, self.occupancy)
        self.hass.config_entries.add(self.coordinator.config_entry)
        self._seed_config = config
        self._seed_state = state
        self._automations = list(automations or [])
        self._scripts = list(scripts or [])
        self.controller: Any = None
        self.started = False

    # ---------------------------------------------------------------- feeding
    def load_home_assistant(self) -> None:
        """The current picture from the App's websocket client: config, states, registries, entries, automations."""
        ha, hass = self.ha, self.hass
        hass.config.update_from_wire(ha.config or {})
        hass.state = CoreState.running if (ha.config or {}).get("state") in (None, "RUNNING") else CoreState.starting
        hass.states.replace_all(s for s in (State.from_wire(v) for v in ha.states.values()) if s is not None)
        self.install_registries()
        self.install_entries()
        hass.data["automation"] = Component(self._automations)
        hass.data["script"] = Component(self._scripts)

    def install_registries(self) -> None:
        _registry.install(self.hass, entities=self.ha.entity_registry, devices=self.ha.devices, areas=self.ha.areas,
                          labels=self.ha.labels)

    def install_entries(self) -> None:
        entries = [self.coordinator.config_entry]
        for item in self.ha.config_entries_all:
            if item.get("domain") == DOMAIN or not item.get("entry_id"):
                continue
            entries.append(ConfigEntry(str(item["entry_id"]), str(item.get("domain")), title=str(item.get("title") or ""),
                                       state=ConfigEntryState.LOADED if item.get("state") == "loaded"
                                       else ConfigEntryState.NOT_LOADED))
        self.hass.config_entries.replace(entries)

    def state_changed(self, entity_id: str, new_state: dict[str, Any] | None) -> None:
        if self.controller is None:
            return
        self.hass.async_state_changed(entity_id, State.from_wire(new_state) if new_state else None)

    def registries_changed(self) -> None:
        self.install_registries()
        self.install_entries()
        if self.controller is not None and self.started:
            self.controller.async_registry_changed()

    def automations_changed(self, automations: list[AutomationEntity], scripts: list[ScriptEntity]) -> None:
        self._automations, self._scripts = list(automations), list(scripts)
        self.hass.data["automation"] = Component(automations)
        self.hass.data["script"] = Component(scripts)
        self.hass.bus.async_fire_internal(Event("automation_reloaded"))

    def mqtt_changed(self, connected: bool) -> None:
        mqtt_component.async_connection_changed(self.hass, connected)

    def occupancy_changed(self, area_id: str) -> None:
        if self.controller is not None and self.started:
            self.occupancy.notify(area_id)

    # -------------------------------------------------------------- lifecycle
    async def async_start(self) -> None:
        """Seed the stores with the copy, load and start the controller (start applies nothing)."""
        from smart_occupancy.const import LIGHTING_STATE_STORE_VERSION, LIGHTING_STORE_MINOR_VERSION, \
            LIGHTING_STORE_VERSION  # noqa: PLC0415
        from smart_occupancy.controller import LightingController  # noqa: PLC0415

        hass = self.hass
        seeds = {LIGHTING_STORE_KEY: (LIGHTING_STORE_VERSION, LIGHTING_STORE_MINOR_VERSION, self._seed_config),
                 LIGHTING_STATE_STORE_KEY: (LIGHTING_STATE_STORE_VERSION, 1, self._seed_state)}
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
        ctrl = LightingController(hass, self.coordinator)
        self.coordinator.lighting = ctrl
        self.controller = ctrl
        await ctrl.async_load()
        ctrl.async_start()
        self.started = True
        if hass.state is not CoreState.running:
            return
        hass.bus.async_fire_internal(Event(EVENT_HOMEASSISTANT_STARTED))
        self.coordinator.settled = True
        self.coordinator.async_schedule_refresh()

    def ha_started(self) -> None:
        if self.hass.state is not CoreState.running:
            self.hass.state = CoreState.running
            self.hass.bus.async_fire_internal(Event(EVENT_HOMEASSISTANT_STARTED))
            self.coordinator.settled = True

    async def async_stop(self, *, flush: bool = True) -> None:
        ctrl = self.controller
        if ctrl is not None:
            ctrl.fenced = True
            if flush and self.role == "owner":
                with contextlib.suppress(Exception):
                    await ctrl.async_flush()
            ctrl.async_stop()
        self.started = False
        if flush and self.role == "owner":
            await storage.async_flush_all(self.hass)
        await self.backend.async_close()
        await self.hass.async_cancel_tasks()


def room_result(occupancy: Any, area_id: str) -> Any:
    """A published occupancy result as the lighting controller's ``RoomResult`` (None while unknown)."""
    wire = occupancy.published_wire(area_id)
    spec = occupancy.room_spec(area_id)
    if wire is None or spec is None:
        return None
    try:
        return result_from_wire(wire, spec)
    except (WireError, KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("occupancy result of %s not usable: %s", area_id, err)
        return None
