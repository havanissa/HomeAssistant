"""The App's configuration authority (Phase A6): the room and presence APIs, the diagnostics snapshot, the system view.

Until Phase A6 the integration's coordinator owned the options (rooms, sensors,
lights, categories, mood source, timing), validated edits (``rooms_api``,
``presence_api``), built the diagnostics snapshot and fed the global entities.
The App now does the same with the same modules:

* a host ``HomeAssistant`` (``backend/homeassistant``) fed by the App's
  websocket client (states, registries, configuration), with one config entry
  whose options are the App's core configuration (``core_config.py``);
* :class:`CoreCoordinator`, what the API handlers read from the coordinator:
  the configuration, the App's occupancy runtime (results, engines, the helper
  comparison), the lighting controller, the curtain manager and the forecast
  manager when they run, the snapshot, and the two ways a handler writes
  options (``async_apply_presence`` for live presence changes,
  ``config_entries.async_update_entry`` for everything else), both of which end
  in the core configuration store;
* the integration's own room and presence handlers, run locally with the
  Ingress user as the connection's user (``call``);
* the diagnostics snapshot (``snapshot.build_snapshot``), rebuilt debounced on
  relevant changes, and the system view published to the companion (the global
  sensors, every room's observation sensor, the mood mismatch, the mood selector).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant, HostBackend, State
from homeassistant.exceptions import HomeAssistantError as HostError, Unauthorized
from homeassistant.helpers import _registry

from .companion_client import CompanionClient, CompanionError
from .core_config import CoreConfigError, CoreConfigStore
from .entity_views import system_view as build_system_view
from .ha import HomeAssistantClient

_LOGGER = logging.getLogger(__name__)

DOMAIN = "smart_occupancy"
ENTRY_ID = "smart_lighting_core"
SNAPSHOT_DELAY = 1.5
PUBLISH_DELAY = 1.0
PERIODIC_SECONDS = 15 * 60
COMMAND_TIMEOUT = 30.0
ROOM_COMMANDS = frozenset(f"smart_occupancy/rooms/{c}" for c in ("get", "review", "set", "restore/review", "restore"))
PRESENCE_COMMANDS = frozenset(f"smart_occupancy/presence/{c}" for c in ("get", "review", "set"))
LOCAL_COMMANDS = ROOM_COMMANDS | PRESENCE_COMMANDS


class CoreApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.data = data or {}


def utcnow() -> datetime:
    return datetime.now(UTC)


class _Bridge:
    """What ``presence_api`` reads of the occupancy ownership (the App is the only engine now)."""

    def __init__(self, host: CoreHost) -> None:
        self._host = host

    @property
    def owner(self) -> str:
        return "app"

    @property
    def epoch(self) -> int | None:
        record = (self._host.companion.ownership.get("occupancy") or {}) if self._host.companion else {}
        return record.get("epoch")

    def state(self) -> str:
        if self._host.companion is None:
            return "app"
        return str((self._host.companion.ownership.get("occupancy") or {}).get("state") or "none")


class CoreOccupancy:
    """The occupancy interface the handlers read: the App's runtime (results, engines, comparisons, inputs)."""

    def __init__(self, host: CoreHost) -> None:
        self._host = host
        self.bridge = _Bridge(host)

    @property
    def _runtime(self) -> Any:
        return self._host.occupancy

    @property
    def engines(self) -> dict[str, Any]:
        house = getattr(self._runtime, "house", None)
        return house.engines if house is not None else {}

    def result(self, area_id: str) -> Any:
        return self._runtime.result(area_id) if self._runtime is not None else None

    def comparison(self, area_id: str) -> Any:
        return self._runtime.comparison(area_id) if self._runtime is not None else None

    def published_by(self, area_id: str) -> str:
        return "app"

    def same_signals(self, config: Any) -> bool:
        return bool(self._runtime is not None and self._runtime.same_signals(config))

    def async_add_listener(self, area_id: str, update: Callable[[], None]) -> Callable[[], None]:
        if self._runtime is None:
            return lambda: None
        return self._runtime.add_result_listener(lambda a: update() if a == area_id else None)


class _NoCurtains:
    class config:  # noqa: N801 - the manager's attribute name
        windows: dict[str, Any] = {}


class _NoForecast:
    def summary(self, area_id: str) -> None:
        return None


class _LightingStandIn:
    """The lighting configuration as read-only data while no lighting controller runs in the App."""

    def __init__(self, hass: HomeAssistant, config: Any, lighting: Any) -> None:
        from smart_occupancy.lighting import LightIndex  # noqa: PLC0415

        self.hass = hass
        self.config = config
        self.lighting = lighting
        self.runtime: dict[str, Any] = {}
        self.managed: dict[str, Any] = {}
        self.index = LightIndex(hass, config.categories, frozenset(e for r in lighting.rooms.values() for e in r.relays))

    def managed_room(self, area_id: str) -> Any:
        from smart_occupancy.controller import ManagedRoom  # noqa: PLC0415

        return ManagedRoom(area_id)

    def room_active(self, area_id: str) -> bool:
        return False

    @property
    def manual_mode(self) -> bool:
        return False

    def async_registry_changed(self) -> None:
        self.index.rebuild()


class CoreCoordinator:
    """What the room and presence handlers (and the lighting controller's validation) read from the coordinator."""

    def __init__(self, host: CoreHost, hass: HomeAssistant, options: dict[str, Any]) -> None:
        from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

        self._host = host
        self.hass = hass
        self.options = dict(options)
        self.config = IntegrationConfig.from_dict(self.options)
        self.occupancy = CoreOccupancy(host)
        self.config_entry = ConfigEntry(ENTRY_ID, DOMAIN, title="Smart Occupancy & Lighting", options=self.options,
                                        state=ConfigEntryState.LOADED)
        self.config_entry.runtime_data = self
        self.data: Any = None
        self.last_update_success = True
        self.pending: list[asyncio.Task[Any]] = []

    @property
    def settled(self) -> bool:
        return self._host.settled

    @property
    def lighting(self) -> Any:
        ctrl = self._host.lighting_controller()
        if ctrl is not None:
            return ctrl
        from smart_occupancy.lighting_config import LightingConfig  # noqa: PLC0415

        return _LightingStandIn(self.hass, self.config, self._host.lighting_config() or LightingConfig())

    @property
    def curtains(self) -> Any:
        return self._host.curtain_manager() or _NoCurtains()

    @property
    def forecast(self) -> Any:
        return self._host.forecast_manager() or _NoForecast()

    @property
    def tracked_entities(self) -> list[str]:
        ids = set(self.config.mood.entity_ids)
        for room in self.config.enabled_rooms.values():
            ids.update(room.referenced_entities)
        return sorted(ids)

    def set_options(self, options: dict[str, Any]) -> None:
        from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

        self.options = dict(options)
        self.config = IntegrationConfig.from_dict(self.options)
        self.config_entry.options = self.options

    def async_apply_presence(self, config: Any, options: dict[str, Any] | None = None) -> bool:
        """A live presence change (same rooms and signals): saved and applied without a reload."""
        if not self.occupancy.same_signals(config):
            return False
        options = dict(options) if options is not None else config.as_dict()
        self.set_options(options)
        self.pending.append(self._host.save_options(options, "presence"))
        return True

    def async_schedule_refresh(self) -> None:
        self._host.schedule_snapshot()


class CoreBackend(HostBackend):
    def __init__(self, host: CoreHost) -> None:
        self._host = host

    async def async_call_service(self, domain: str, service: str, data: dict[str, Any], context: Any,
                                 blocking: bool) -> Any:
        raise HostError("the configuration host sends no service calls")

    def async_update_options(self, entry: Any, options: dict[str, Any]) -> bool:
        """A handler changed the options: applied at once (the handlers read them back), persisted right after."""
        coordinator = self._host.coordinator
        if coordinator is None or dict(options) == coordinator.options:
            return False
        coordinator.set_options(options)
        coordinator.pending.append(self._host.save_options(options, "options"))
        return True


class CoreHost:
    def __init__(self, ha: HomeAssistantClient, core_config: CoreConfigStore, *, companion: CompanionClient | None,
                 data_dir: Path | None) -> None:
        self.ha = ha
        self.core_config = core_config
        self.companion = companion
        self.dir = data_dir / "config" if data_dir else None
        self.hass: HomeAssistant | None = None
        self.coordinator: CoreCoordinator | None = None
        self.occupancy: Any = None
        self.lighting: Any = None
        self.curtains: Any = None
        self.forecast: Any = None
        self.snapshot: Any = None
        self.snapshot_at: datetime | None = None
        self.system_view: dict[str, Any] | None = None
        self.published_view: dict[str, Any] | None = None
        self.published_at: str | None = None
        self.publish_error: str | None = None
        self.tracker: Any = None
        self._mismatch_since: datetime | None = None
        self._snapshot_handle: asyncio.TimerHandle | None = None
        self._publish_handle: asyncio.TimerHandle | None = None
        self._periodic: asyncio.Task[None] | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._listeners: list[Callable[[str], None]] = []
        self._handlers: dict[str, Any] | None = None
        self._next_id = 0
        self._started = False
        self.counters: dict[str, int] = {"snapshots": 0, "publishes": 0, "calls": 0}

    # ------------------------------------------------------------ lifecycle
    def attach(self, *, occupancy: Any, lighting: Any, curtains: Any, forecast: Any) -> None:
        self.occupancy, self.lighting, self.curtains, self.forecast = occupancy, lighting, curtains, forecast

    async def start(self) -> None:
        from smart_occupancy.problems import ProblemTracker  # noqa: PLC0415

        self.hass = HomeAssistant(CoreBackend(self), asyncio.get_running_loop())
        self.hass.storage_dir = str(self.dir) if self.dir else None
        self.tracker = ProblemTracker(self.hass)
        with contextlib.suppress(Exception):
            await self.tracker.async_load()
        if self.core_config.authoritative:
            self._install_coordinator(self.core_config.options or {})
        self._unsubs.append(self.core_config.add_listener(self._on_config))
        self._unsubs.append(self.ha.add_listener(self._on_ha))
        if self.companion is not None:
            self._unsubs.append(self.companion.add_listener(self._on_companion))
        if self.ha.connected:
            self._load_home_assistant()
        self._periodic = asyncio.get_running_loop().create_task(self._periodic_loop())
        self._started = True
        self.schedule_snapshot()

    async def stop(self) -> None:
        self._started = False
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for handle in (self._snapshot_handle, self._publish_handle):
            if handle:
                handle.cancel()
        if self._periodic:
            self._periodic.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self.hass is not None:
            await self.hass.async_cancel_tasks()

    def add_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self, what: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Core host listener failed")

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def authoritative(self) -> bool:
        return self.coordinator is not None and self.core_config.authoritative

    @property
    def settled(self) -> bool:
        return bool(self.ha.connected and (self.ha.config or {}).get("state") in (None, "RUNNING"))

    @property
    def config(self) -> Any:
        return self.coordinator.config if self.coordinator else None

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.coordinator.options) if self.coordinator else {}

    # ------------------------------------------------------------- runtimes
    def lighting_controller(self) -> Any:
        return getattr(self.lighting, "controller", None) if self.lighting is not None else None

    def lighting_config(self) -> Any:
        return self.lighting.stored_config() if self.lighting is not None and hasattr(self.lighting, "stored_config") else None

    def curtain_manager(self) -> Any:
        return getattr(self.curtains, "manager", None) if self.curtains is not None else None

    def forecast_manager(self) -> Any:
        return getattr(self.forecast, "manager", None) if self.forecast is not None else None

    def lighting_active(self, area_id: str) -> bool:
        ctrl = self.lighting_controller()
        return bool(ctrl is not None and ctrl.room_active(area_id))

    def manual_mode(self) -> bool:
        ctrl = self.lighting_controller()
        return bool(ctrl is not None and ctrl.manual_mode)

    # --------------------------------------------------- the host picture
    def _install_coordinator(self, options: dict[str, Any]) -> None:
        assert self.hass is not None
        if self.coordinator is None:
            self.coordinator = CoreCoordinator(self, self.hass, options)
            self.hass.config_entries.add(self.coordinator.config_entry)
        else:
            self.coordinator.set_options(options)

    def _load_home_assistant(self) -> None:
        hass, ha = self.hass, self.ha
        if hass is None:
            return
        hass.config.update_from_wire(ha.config or {})
        hass.state = CoreState.running if (ha.config or {}).get("state") in (None, "RUNNING") else CoreState.starting
        hass.states.replace_all(s for s in (State.from_wire(v) for v in ha.states.values()) if s is not None)
        self._install_registries()

    def _install_registries(self) -> None:
        if self.hass is None:
            return
        _registry.install(self.hass, entities=self.ha.entity_registry, devices=self.ha.devices, areas=self.ha.areas,
                          labels=self.ha.labels)
        entries = [self.coordinator.config_entry] if self.coordinator else []
        for item in self.ha.config_entries_all:
            if item.get("domain") == DOMAIN or not item.get("entry_id"):
                continue
            entries.append(ConfigEntry(str(item["entry_id"]), str(item.get("domain")), title=str(item.get("title") or ""),
                                       state=ConfigEntryState.LOADED if item.get("state") == "loaded"
                                       else ConfigEntryState.NOT_LOADED))
        self.hass.config_entries.replace(entries)

    def _on_ha(self, kind: str, payload: Any) -> None:
        if self.hass is None:
            return
        if kind == "status" and self.ha.connected:
            self._load_home_assistant()
            self.schedule_snapshot()
        elif kind == "state":
            entity_id, new_state = payload
            old = self.hass.states.get(entity_id)
            new = State.from_wire(new_state) if new_state else None
            self.hass.async_state_changed(entity_id, new)
            if self.coordinator is not None and entity_id in self.coordinator.tracked_entities:
                from smart_occupancy.snapshot import diagnostics_relevant  # noqa: PLC0415

                if diagnostics_relevant(old, new, value_matters=entity_id in self.coordinator.config.mood.entity_ids):
                    self.schedule_snapshot()
        elif kind == "registries":
            self._install_registries()
            ctrl = self.lighting_controller()
            if ctrl is not None:
                ctrl.async_registry_changed()
            self.schedule_snapshot()
        elif kind == "event" and (payload or {}).get("event_type") == "homeassistant_started":
            self.hass.state = CoreState.running
            self.schedule_snapshot()

    def _on_config(self, options: dict[str, Any], reason: str) -> None:
        if self.hass is None:
            return
        self._install_coordinator(options)
        self._install_registries()
        self.schedule_snapshot()
        self._changed("config")

    def _on_companion(self, kind: str, payload: Any) -> None:
        if kind in ("hello", "ownership", "recheck"):
            self.published_view = None if kind == "hello" else self.published_view
            self.schedule_snapshot()

    # ------------------------------------------------------------ writing
    def save_options(self, options: dict[str, Any], reason: str) -> asyncio.Task[Any]:
        return self._spawn(self.core_config.save(options, expected_revision=None, reason=reason, source="app"))

    # ----------------------------------------------------------- handlers
    @property
    def handlers(self) -> dict[str, Any]:
        if self._handlers is None:
            from smart_occupancy.presence_api import PRESENCE_API_HANDLERS  # noqa: PLC0415
            from smart_occupancy.rooms_api import ROOMS_HANDLERS  # noqa: PLC0415

            self._handlers = {h._ws_command: h for h in (*ROOMS_HANDLERS, *PRESENCE_API_HANDLERS)}  # noqa: SLF001
        return self._handlers

    async def call(self, command: str, params: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        """Run the integration's own room / presence handler against the App's configuration and runtimes."""
        if not self.authoritative or self.hass is None or self.coordinator is None:
            raise CoreApiError("not_authoritative", "The App is not the configuration authority yet", 503)
        handler = self.handlers.get(command)
        if handler is None:
            raise CoreApiError("not_found", f"{command} is not available", 404)
        self._next_id += 1
        msg = {"id": self._next_id, "type": command, **params}
        try:
            msg = handler._ws_schema(msg)  # noqa: SLF001
        except vol.Invalid as err:
            raise CoreApiError(websocket_api.ERR_INVALID_FORMAT, str(err)) from err
        connection = websocket_api.ActiveConnection(websocket_api.User(user_id, is_admin=is_admin))
        self.coordinator.pending.clear()
        self.counters["calls"] += 1
        try:
            handler(self.hass, connection, msg)
        except Unauthorized as err:
            raise CoreApiError("unauthorized", "Only administrators can change the configuration", 403) from err
        except (vol.Invalid, HostError) as err:
            connection.async_handle_exception(msg, err)
        try:
            answer = await asyncio.wait_for(asyncio.shield(connection.done), COMMAND_TIMEOUT)
        except TimeoutError as err:
            raise CoreApiError("timeout", f"{command} did not answer", 504) from err
        finally:
            connection.close()
        pending, self.coordinator.pending = list(self.coordinator.pending), []
        for task in pending:  # the persisted outcome counts
            try:
                await task
            except CoreConfigError as err:
                raise CoreApiError(err.code, str(err), err.status) from err
        if not answer.get("success"):
            error = answer.get("error") or {}
            raise CoreApiError(str(error.get("code") or "error"), str(error.get("message") or "Refused"),
                               data={k: v for k, v in error.items() if k not in ("code", "message")})
        return answer.get("result")

    # ------------------------------------------------------------ snapshot
    def schedule_snapshot(self, delay: float = SNAPSHOT_DELAY) -> None:
        if not self._started or self._snapshot_handle is not None:
            return
        self._snapshot_handle = asyncio.get_running_loop().call_later(delay, self._rebuild)

    def _rebuild(self) -> None:
        self._snapshot_handle = None
        if self.hass is None or self.coordinator is None or not self.ha.connected:
            return
        from smart_occupancy import mood as mood_mod  # noqa: PLC0415
        from smart_occupancy.snapshot import build_snapshot  # noqa: PLC0415

        config = self.coordinator.config
        now = utcnow()
        try:
            observation = mood_mod.observe(self.hass, config.mood)
            mismatch = self._evaluate_mismatch(observation, now)
            ctrl = self.lighting_controller()
            draft = build_snapshot(self.hass, config, settled=self.settled, lighting=ctrl, observation=observation,
                                   mismatch=mismatch, first_detected={}, now=now, checked_bucket=None)
            first = self.tracker.update(draft.problems, now, prune=self.settled) if self.tracker else {}
            from dataclasses import replace  # noqa: PLC0415

            self.snapshot = replace(draft, first_detected=first)
        except Exception:  # noqa: BLE001 - diagnostics must never break the App
            _LOGGER.exception("The diagnostics snapshot could not be built")
            return
        self.snapshot_at = now
        self.coordinator.data = self.snapshot
        self.counters["snapshots"] += 1
        self._changed("snapshot")
        self.schedule_publish()

    def _evaluate_mismatch(self, observation: Any, now: datetime) -> bool:
        """A mismatch counts once it outlives the grace period (a timer re-evaluates after it)."""
        if observation.consistent is not False:
            self._mismatch_since = None
            return False
        grace = timedelta(seconds=self.coordinator.config.mood.mismatch_grace_seconds) if self.coordinator else timedelta(30)
        if self._mismatch_since is None:
            self._mismatch_since = now
        elapsed = now - self._mismatch_since
        if elapsed >= grace:
            return True
        self.schedule_snapshot((grace - elapsed).total_seconds() + 0.1)
        return False

    async def _periodic_loop(self) -> None:
        while True:
            await asyncio.sleep(PERIODIC_SECONDS)
            self.schedule_snapshot(0)

    # ------------------------------------------------------- system view
    def build_view(self) -> dict[str, Any] | None:
        if self.snapshot is None or self.coordinator is None or self.hass is None:
            return None
        from smart_occupancy.discovery import area_name  # noqa: PLC0415

        config = self.coordinator.config
        names = {a: (area_name(self.hass, a) or a) for a in config.enabled_rooms}
        ctrl = self.lighting_controller()
        manual = ctrl.lighting.manual_option if ctrl is not None else (self.lighting_config().manual_option
                                                                        if self.lighting_config() else "Manual")
        try:
            view = build_system_view(self.snapshot, config, room_names=names, manual_option=manual)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("The system view could not be built")
            return None
        self.system_view = view
        return view

    def schedule_publish(self, delay: float = PUBLISH_DELAY) -> None:
        if self.companion is None or self._publish_handle is not None:
            return
        self._publish_handle = asyncio.get_running_loop().call_later(delay, lambda: self._spawn(self._publish()))

    def owns_any(self) -> bool:
        return any(getattr(rt, "cm", None) is not None and rt.cm.owner
                   for rt in (self.occupancy, self.lighting, self.curtains, self.forecast) if rt is not None)

    async def _publish(self) -> None:
        self._publish_handle = None
        if self.companion is None or not self.companion.ready or not self.owns_any():
            return
        view = self.build_view()
        if view is None or view == self.published_view:
            return
        try:
            await self.companion.publish_system(view)
        except CompanionError as err:
            self.publish_error = f"{err.code}: {err}"
            self.schedule_publish(10)
            return
        self.published_view = view
        self.published_at = utcnow().isoformat(timespec="seconds")
        self.publish_error = None
        self.counters["publishes"] += 1

    def republish(self) -> None:
        self.published_view = None
        self.schedule_publish(0)

    # -------------------------------------------------------------- status
    def status(self) -> dict[str, Any]:
        s = self.snapshot
        return {
            "authoritative": self.authoritative, "settled": self.settled, "snapshot_at": self.snapshot_at.isoformat(
                timespec="seconds") if self.snapshot_at else None,
            "status": s.status.value if s else None, "problems": len(s.problems) if s else 0,
            "rooms": len(s.rooms) if s else 0, "mood": s.mood.as_dict() if s else None,
            "published_at": self.published_at, "publish_error": self.publish_error, "counters": dict(self.counters),
            "config": self.core_config.status(),
        }
