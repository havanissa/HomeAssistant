"""The App's state: Home Assistant and Supervisor clients, the runtimes, the companion, health and overview."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging
import platform
import time
from typing import Any

import aiohttp

from . import companion_protocol as P, modules
from .companion_client import CompanionClient
from .const import APP_NAME, APP_PHASE, APP_SUBTITLE, APP_VERSION
from .climate import ClimateService
from .core_config import CoreConfigStore
from .core_host import CoreHost
from .curtains_api import CurtainsApi
from .curtains_runtime import CurtainRuntime
from .floorplan import FloorPlanService
from .forecast_api import ForecastApi
from .forecast_runtime import ForecastRuntime
from .ha import (
    ALLOWED_COMMANDS,
    COMPANION_COMMANDS,
    CONFIG_WRITE_COMMANDS,
    CURTAIN_BRIDGE_COMMANDS,
    ENGINE_COMMANDS,
    FORECAST_BRIDGE_COMMANDS,
    LIGHTING_BRIDGE_COMMANDS,
    READ_ONLY_COMMANDS,
    HomeAssistantClient,
    utcnow_iso,
)
from .lighting_api import LightingApi
from .lighting_runtime import LightingRuntime
from .logbuffer import RingBufferHandler
from .migration import MigrationService
from .occupancy_runtime import OccupancyRuntime
from .onboarding import OnboardingService
from .presence import PresenceService
from .rooms import RoomsService
from .settings import Settings
from .supervisor import SupervisorClient

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_REFRESH = 600.0
ROOMS_REFRESH_DELAY = 0.5

OK, WARNING, ERROR, INFO = "ok", "warning", "error", "info"


class AppCore:
    def __init__(self, settings: Settings, log_buffer: RingBufferHandler | None = None) -> None:
        self.settings = settings
        self.log_buffer = log_buffer
        self.app_version = APP_VERSION
        self.started_at = utcnow_iso()
        self._started_monotonic = time.monotonic()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.supervisor: SupervisorClient | None = None
        self.rooms: RoomsService | None = None
        self.floorplan: FloorPlanService | None = None
        self.climate: ClimateService | None = None  # Phase A7.0: Smart Comfort's climate module (observation only)
        self.presence: PresenceService | None = None
        self.occupancy: OccupancyRuntime | None = None
        self.lighting: LightingRuntime | None = None
        self.lighting_api: LightingApi | None = None
        self.curtains: CurtainRuntime | None = None
        self.curtains_api: CurtainsApi | None = None
        self.forecast: ForecastRuntime | None = None
        self.forecast_api: ForecastApi | None = None
        # Phase A6: the App's own configuration, the companion session, the configuration host, the wizard.
        self.core_config = CoreConfigStore(settings.data_dir)
        self.companion: CompanionClient | None = None
        self.core_host: CoreHost | None = None
        self.migration: MigrationService | None = None
        self.onboarding: OnboardingService | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._rooms_timer: asyncio.TimerHandle | None = None
        self._rooms_listeners: list = []
        self._system_listeners: list = []
        self._system_handle: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.supervisor = SupervisorClient(self.session, self.settings.supervisor_url, self.settings.token)
        await self.core_config.load()
        self.companion = CompanionClient(self.ha, self.settings.data_dir, app_version=APP_VERSION)
        self.core_host = CoreHost(self.ha, self.core_config, companion=self.companion, data_dir=self.settings.data_dir)
        self.rooms = RoomsService(self.ha, self.settings.data_dir, local=self.core_host)
        self.floorplan = FloorPlanService(self.ha, self.rooms, self.settings.data_dir)
        self.presence = PresenceService(self.ha, self.rooms)
        self.floorplan.load()
        self.climate = ClimateService(self.ha, self.rooms, self.settings.data_dir)
        self.climate.load()
        # Phase A3.2: the shared occupancy engine (shadow next to the 0.9.0 integration; the only engine with the
        # companion, A6).
        self.occupancy = OccupancyRuntime(self.ha, self.settings.data_dir, app_version=APP_VERSION,
                                          min_shadow_seconds=self.settings.engine_min_shadow_seconds,
                                          min_transitions=self.settings.engine_min_transitions,
                                          companion=self.companion, core_config=self.core_config)
        self.occupancy.core_host = self.core_host
        await self.occupancy.start()
        # Phase A4: the lighting runtime (shadow until a reviewed handover on 0.9.0; with the companion it commands
        # lights only through the companion's fenced executor).
        self.lighting = LightingRuntime(self.ha, self.occupancy, self.settings.data_dir, app_version=APP_VERSION,
                                        min_shadow_seconds=self.settings.lighting_min_shadow_seconds,
                                        min_commands=self.settings.lighting_min_commands,
                                        companion=self.companion, core_config=self.core_config)
        self.lighting.core_host = self.core_host
        await self.lighting.start()
        self.lighting_api = LightingApi(self.ha, self.lighting)
        # Phase A5: the curtain runtime and the forecast runtime (observation only on every path).
        self.curtains = CurtainRuntime(self.ha, self.occupancy, self.settings.data_dir, app_version=APP_VERSION,
                                       min_shadow_seconds=self.settings.curtains_min_shadow_seconds,
                                       min_decisions=self.settings.curtains_min_decisions,
                                       companion=self.companion, core_config=self.core_config)
        self.curtains.core_host = self.core_host
        await self.curtains.start()
        self.curtains_api = CurtainsApi(self.ha, self.curtains)
        self.forecast = ForecastRuntime(self.ha, self.occupancy, self.settings.data_dir, app_version=APP_VERSION,
                                        min_shadow_seconds=self.settings.forecast_min_shadow_seconds,
                                        min_matches=self.settings.forecast_min_matches,
                                        companion=self.companion, core_config=self.core_config)
        self.forecast.core_host = self.core_host
        await self.forecast.start()
        self.forecast_api = ForecastApi(self.ha, self.forecast)
        self.core_host.attach(occupancy=self.occupancy, lighting=self.lighting, curtains=self.curtains,
                              forecast=self.forecast)
        self.migration = MigrationService(self, self.settings.data_dir)
        await self.migration.load()
        self.onboarding = OnboardingService(self)
        self.companion.add_listener(self._on_companion)
        self.core_config.add_listener(lambda _o, _r: self._system_changed("config"))
        self.core_host.add_listener(lambda what: self._system_changed(what))
        for rt in (self.occupancy, self.lighting, self.curtains, self.forecast):
            rt.add_listener(lambda _k, d=rt: self._system_changed("runtime"))
        self.ha.add_listener(self._on_ha)
        self.climate.occupancy, self.climate.lighting, self.climate.curtains = self.occupancy, self.lighting, \
            self.curtains
        await self.climate.start()
        await self.ha.start()
        await self.companion.start()
        await self.core_host.start()
        self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="supervisor-refresh"))
        _LOGGER.info("%s %s (Phase %s) started; configuration authority: %s", APP_NAME, APP_VERSION, APP_PHASE,
                     "the App" if self.core_config.authoritative else "the integration (until migrated)")

    def add_rooms_listener(self, listener) -> None:
        """``listener(kind)`` with kind "rooms" (configuration changed) or "registries"."""
        self._rooms_listeners.append(listener)

    def add_system_listener(self, listener) -> None:
        """``listener(what)``: the companion, the configuration authority or a runtime's control changed."""
        self._system_listeners.append(listener)

    def _system_changed(self, what: str) -> None:
        if self._system_handle is not None:
            return
        loop = asyncio.get_running_loop()
        self._system_handle = loop.call_later(0.3, self._emit_system, what)

    def _emit_system(self, what: str) -> None:
        self._system_handle = None
        for listener in list(self._system_listeners):
            try:
                listener(what)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("System listener failed")
        if what in ("config", "runtime", "state", "hello", "ownership", "pairing"):
            self._schedule_rooms_refresh()

    def _on_companion(self, kind: str, payload: Any) -> None:
        if kind in ("state", "hello", "ownership", "pairing", "closed"):
            self._system_changed(kind)
        elif kind == "action":
            self._spawn(self._handle_action(payload or {}))
        elif kind == "renames":
            self._spawn(self._handle_renames(payload or {}))
        elif kind == "recheck":
            if self.core_host is not None:
                self.core_host.schedule_snapshot(0)
        elif kind == "areas":
            if self.core_host is not None:
                self.core_host.republish()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(task)
        task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

    async def _handle_action(self, event: dict[str, Any]) -> None:
        """A dashboard card's action, relayed by the companion: run with the App's own validated handlers."""
        action_id = str(event.get("action_id") or "")
        kind = str(event.get("kind") or "")
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        user = event.get("user") if isinstance(event.get("user"), str) else None
        assert self.companion is not None
        try:
            if kind == "room/resume":
                result = await self.lighting_api.call({"type": "smart_occupancy/lighting/override/clear",
                                                       "area_id": params.get("area_id")}, user_id=user, is_admin=True)
            elif kind == "room/mode":
                command = "room/activate" if params.get("mode") == "automatic" else "room/deactivate"
                result = await self.lighting_api.call({"type": f"smart_occupancy/lighting/{command}",
                                                       "area_id": params.get("area_id"), "confirm": True},
                                                      user_id=user, is_admin=True)
            elif kind == "mood/select":
                result = await self.lighting_api.call({"type": "smart_occupancy/lighting/mood/select",
                                                       "mood": params.get("mood")}, user_id=user, is_admin=True)
            elif kind == "curtains/apply":
                result = await self.curtains_api.call({"type": "smart_occupancy/curtains/apply",
                                                       "window_id": params.get("window_id"),
                                                       "preset_id": params.get("preset_id"), "confirm": True,
                                                       "expected": params.get("expected")}, user_id=user, is_admin=True)
            elif kind == "curtains/resume":
                result = await self.curtains_api.call({"type": "smart_occupancy/curtains/resume",
                                                       "window_id": params.get("window_id")}, user_id=user, is_admin=True)
            elif kind == "curtains/mode":
                result = await self.curtains_api.call({"type": "smart_occupancy/curtains/window/update",
                                                       "window_id": params.get("window_id"),
                                                       "mode": params.get("mode")}, user_id=user, is_admin=True)
            else:
                raise ValueError(f"unknown action {kind}")
        except Exception as err:  # noqa: BLE001 - reported to the card
            await self.companion.action_result(action_id, ok=False, error={"code": getattr(err, "code", "error"),
                                                                             "message": str(err)[:200]})
            return
        await self.companion.action_result(action_id, ok=True, result=result if isinstance(result, dict) else {})

    async def _handle_renames(self, event: dict[str, Any]) -> None:
        """Entity ids renamed in Home Assistant (also while the App was away): every configuration follows."""
        renames = P.check_renames(event.get("renames"))
        if not renames:
            return
        for old, new in renames:
            with contextlib.suppress(Exception):
                await self.core_config.rename_entity(old, new)
            ctrl = self.lighting.controller if self.lighting else None
            if ctrl is not None:
                from smart_occupancy.lighting_config import rename_entity  # noqa: PLC0415

                if (lighting := rename_entity(ctrl.store.config, old, new)) is not None:
                    ctrl.store.async_set(lighting)
            mgr = self.curtains.manager if self.curtains else None
            if mgr is not None:
                mgr.rename_entity(old, new)
            if self.climate is not None:
                with contextlib.suppress(Exception):
                    await self.climate.rename(old, new)
        if self.companion is not None:
            await self.companion.renames_applied()

    def _on_ha(self, kind: str, payload) -> None:
        if self.rooms is None:
            return
        if kind == "registries":
            self.rooms.invalidate_index()
            self._emit_rooms("registries")
        elif kind in ("config_entry", "integration") or (kind == "status" and self.ha and self.ha.connected):
            self._schedule_rooms_refresh()
            if kind == "config_entry" and self.companion is not None and self.companion.state == "absent":
                # The integration was reloaded or upgraded: the companion may exist now.
                self._spawn(self.companion.reprobe())
            self._system_changed("ha")

    def _schedule_rooms_refresh(self) -> None:
        if self._rooms_timer is not None:
            self._rooms_timer.cancel()
        self._rooms_timer = asyncio.get_running_loop().call_later(ROOMS_REFRESH_DELAY, self._start_rooms_refresh)

    def _start_rooms_refresh(self) -> None:
        self._rooms_timer = None
        self._spawn(self._rooms_refresh())

    async def _rooms_refresh(self) -> None:
        assert self.rooms is not None
        if self.ha is None or not self.ha.connected:
            return
        if await self.rooms.refresh():
            self.rooms.invalidate_index()
            self._emit_rooms("rooms")

    def _emit_rooms(self, kind: str) -> None:
        for listener in list(self._rooms_listeners):
            try:
                listener(kind)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Rooms listener failed")

    async def stop(self) -> None:
        for handle in (self._rooms_timer, self._system_handle):
            if handle is not None:
                handle.cancel()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.core_host is not None:
            await self.core_host.stop()
        if self.climate is not None:
            await self.climate.stop()
        if self.forecast is not None:
            await self.forecast.stop()
        if self.curtains is not None:
            await self.curtains.stop()
        if self.lighting is not None:
            await self.lighting.stop()
        if self.occupancy is not None:
            await self.occupancy.stop()
        if self.companion is not None:
            await self.companion.stop()
        if self.ha is not None:
            await self.ha.stop()
        if self.session is not None:
            await self.session.close()

    async def _supervisor_loop(self) -> None:
        assert self.supervisor is not None
        while True:
            await self.supervisor.refresh()
            await asyncio.sleep(SUPERVISOR_REFRESH)

    # ------------------------------------------------------------- reports
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self._started_monotonic)

    def interface_present(self) -> bool:
        return (self.settings.static_dir / "index.html").is_file()

    @property
    def companion_present(self) -> bool:
        return self.companion is not None and self.companion.present

    def _owner(self, runtime: Any, app_states: tuple[str, ...]) -> str:
        """Which side runs a subsystem: ``app``, ``integration`` or ``none`` (companion present, not claimed)."""
        if runtime is None:
            return "integration"
        cm = getattr(runtime, "cm", None)
        if cm is not None and cm.active:
            return "app" if cm.owner else "none"
        return "app" if runtime.integration.get("state") in app_states else "integration"

    def occupancy_owner(self) -> str:
        return self._owner(self.occupancy, ("app", "resume_pending"))

    def lighting_owner(self) -> str:
        return self._owner(self.lighting, ("app", "resume_pending"))

    def curtains_owner(self) -> str:
        return self._owner(self.curtains, ("app", "resume_pending"))

    def forecast_owner(self) -> str:
        return self._owner(self.forecast, ("app", "resume_pending"))

    def config_authority(self) -> str:
        """``app``, ``staged`` (a copy that does not count yet: the migration) or ``integration``."""
        if self.core_config.authoritative:
            return "app"
        return "staged" if self.core_config.staged else "integration"

    def safety(self) -> dict[str, Any]:
        sent = dict(self.ha.commands_sent) if self.ha else {}
        other_commands = sum(n for cmd, n in sent.items() if cmd not in ALLOWED_COMMANDS)
        writes = sum(n for cmd, n in sent.items() if cmd in CONFIG_WRITE_COMMANDS)
        engine = sum(n for cmd, n in sent.items() if cmd in ENGINE_COMMANDS)
        lighting_commands = sent.get("smart_occupancy/lighting/bridge/execute", 0)
        curtain_commands = sent.get("smart_occupancy/curtains/bridge/execute", 0)
        companion_commands = sent.get("smart_occupancy/companion/execute", 0)
        owner = self.lighting_owner()
        curtains_owner = self.curtains_owner()
        return {
            "curtains_owner": curtains_owner,
            "curtains_mode": self.curtains.mode if self.curtains else None,
            "curtain_commands_sent": curtain_commands,
            "allowed_curtain_bridge_commands": sorted(CURTAIN_BRIDGE_COMMANDS),
            "forecast_owner": self.forecast_owner(),
            "forecast_mode": self.forecast.mode if self.forecast else None,
            "allowed_forecast_bridge_commands": sorted(FORECAST_BRIDGE_COMMANDS),
            "read_only": False,
            "configuration_writes": True,
            "config_authority": self.config_authority(),
            "write_scope": ("The App's own configuration storage (rooms, sensors, lighting, moods, switches, curtains, "
                            "predictions, floor plans); the companion integration holds no automation configuration"
                            if self.core_config.authoritative else
                            "Room and presence configuration of the existing integration through its validated, "
                            "administrator-only commands, and the App's own storage after each handover"),
            "config_writes_sent": writes,
            "recent_config_writes": list(self.ha.config_writes) if self.ha else [],
            "device_control": owner == "app" or curtains_owner == "app",
            "lighting_owner": owner,
            "lighting_mode": self.lighting.mode if self.lighting else None,
            "lighting_commands_sent": lighting_commands,
            "allowed_lighting_bridge_commands": sorted(LIGHTING_BRIDGE_COMMANDS),
            # Phase A6: with the companion every device command goes through its fenced executor.
            "companion": self.companion_present,
            "companion_commands_sent": companion_commands,
            "allowed_companion_commands": sorted(COMPANION_COMMANDS),
            "automation_engine": self.companion_present,
            "occupancy_engine": self.occupancy_owner(),
            "occupancy_engine_mode": self.occupancy.mode if self.occupancy else None,
            "engine_commands_sent": engine,
            "allowed_engine_commands": sorted(ENGINE_COMMANDS),
            "automatic_migration": False,
            # Phase A7.0: the climate module observes only; no thermostat can be commanded by the App.
            "heating_control": False,
            "modifies_integration": writes > 0,
            "service_calls_sent": other_commands,  # always 0: the client cannot call a service directly
            "commands_sent": sent,
            "allowed_commands": sorted(READ_ONLY_COMMANDS),
            "allowed_write_commands": sorted(CONFIG_WRITE_COMMANDS),
        }

    def checks(self) -> list[dict[str, str]]:
        ha, sup = self.ha, self.supervisor
        checks = [{"key": "backend", "title": "App backend", "status": OK, "detail": "Running"}]
        checks.append(
            {"key": "interface", "title": "Web interface", "status": OK, "detail": "Installed"}
            if self.interface_present()
            else {"key": "interface", "title": "Web interface", "status": ERROR, "detail": "Interface files are missing"}
        )
        if not self.settings.token_present:
            checks.append({"key": "token", "title": "API credentials", "status": ERROR, "detail": "SUPERVISOR_TOKEN is not set"})
        else:
            checks.append({"key": "token", "title": "API credentials", "status": OK, "detail": "Provided by the Supervisor (never sent to the browser)"})

        state = ha.state if ha else "not_configured"
        detail = {
            "connected": f"Connected to Home Assistant {ha.ha_version}" if ha else "",
            "connecting": "Connecting…",
            "disconnected": f"Not connected: {ha.last_error}" if ha else "Not connected",
            "auth_failed": "Home Assistant rejected the App's token",
            "not_configured": "No credentials",
        }.get(state, state)
        status = {"connected": OK, "connecting": WARNING, "disconnected": WARNING}.get(state, ERROR)
        checks.append({"key": "home_assistant", "title": "Home Assistant API", "status": status, "detail": detail})

        if ha and ha.connected:
            checks.append({"key": "live_updates", "title": "Live state updates", "status": OK,
                           "detail": f"Subscribed; {ha.events_per_minute()} state changes in the last minute"})

        if sup is None or not sup.configured:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": INFO, "detail": "Not used (development mode)"})
        elif sup.available and not sup.error:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": OK, "detail": "App and system information read"})
        elif sup.checked_at is None:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": INFO, "detail": "Not checked yet"})
        else:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": WARNING, "detail": sup.error or "Unavailable"})

        companion = self.companion
        if companion is not None:
            if companion.present:
                detail = (f"Connected (companion {companion.hello.get('companion_version')}); "
                          + ("paired" if companion.trusted else "not paired: pair it in Settings → System"))
                checks.append({"key": "companion", "title": "Companion integration",
                               "status": OK if companion.trusted else WARNING, "detail": detail})
            elif companion.state == "absent" and ha and ha.integration.detected:
                checks.append({"key": "companion", "title": "Companion integration", "status": INFO,
                               "detail": f"The monolithic integration {ha.integration.version or ''} is installed; "
                                         "migrate it in Settings → Migration & System"})
            elif companion.state in ("incompatible", "error"):
                checks.append({"key": "companion", "title": "Companion integration", "status": ERROR,
                               "detail": companion.error or companion.state})
            elif ha and ha.connected:
                checks.append({"key": "companion", "title": "Companion integration", "status": WARNING,
                               "detail": "Not installed: the App has no native entities and cannot command devices"})

        if ha and ha.connected and self.rooms is not None:
            api = self.rooms.api_state
            where = self.rooms.where
            if api == "ok":
                writable = self.rooms.status()["writable"]
                checks.append({"key": "rooms_api", "title": "Room configuration API",
                               "status": OK if writable else WARNING,
                               "detail": ("Available; room changes are saved in the App" if where == "app" else
                                          "Available; room changes are saved in the integration") if writable
                               else "Readable; saving needs the integration to be running"})
            elif api == "unsupported":
                checks.append({"key": "rooms_api", "title": "Room configuration API", "status": WARNING,
                               "detail": self.rooms.api_error or "Not supported by the installed integration"})
            elif api in ("error", "unavailable"):
                checks.append({"key": "rooms_api", "title": "Room configuration API", "status": WARNING,
                               "detail": self.rooms.api_error or "Not available"})
        if ha and ha.connected and self.presence is not None and self.presence.api_state != "unknown":
            p = self.presence.status()
            if p["api"] == "ok":
                checks.append({"key": "presence_api", "title": "Presence API", "status": OK,
                               "detail": ("Available; the App's occupancy engine is shown and configured"
                                          if self.rooms is not None and self.rooms.where == "app" else
                                          "Available; the integration's occupancy engine is shown and configured")
                               + ("" if p["history"] else " (no Recorder history)")})
            else:
                checks.append({"key": "presence_api", "title": "Presence API", "status": WARNING,
                               "detail": p["error"] or "Not available"})

        cfg = self.core_config.status()
        if cfg["authoritative"]:
            checks.append({"key": "config", "title": "Configuration", "status": OK,
                           "detail": f"Stored in the App (revision {cfg['revision']}, {cfg['enabled_rooms']} rooms)"})
        elif cfg["staged"]:
            checks.append({"key": "config", "title": "Configuration", "status": WARNING,
                           "detail": "A staged copy waits for the companion; the integration's options still count"})
        elif ha and ha.connected and self.rooms is not None and self.rooms.api_state == "ok":
            checks.append({"key": "config", "title": "Configuration", "status": INFO,
                           "detail": "Stored in the integration; edited through its validated commands"})
        elif ha and ha.connected:
            checks.append({"key": "config", "title": "Configuration", "status": WARNING,
                           "detail": "Not created yet: Settings → Setup"})
        if cfg["notices"]:
            checks.append({"key": "config_storage", "title": "Configuration storage", "status": WARNING,
                           "detail": cfg["notices"][-1]["text"]})

        if self.floorplan is not None:
            fp = self.floorplan.status()
            if fp["read_only"]:
                checks.append({"key": "floorplan", "title": "Floor plan storage", "status": WARNING,
                               "detail": fp["read_only"]})
            elif any(n["code"].startswith("recovered") for n in fp["notices"]):
                checks.append({"key": "floorplan", "title": "Floor plan storage", "status": WARNING,
                               "detail": "Recovered after a damaged save; see the Floor plan page"})

        if self.climate is not None:
            cs = self.climate.status()
            if cs["read_only"]:
                checks.append({"key": "climate", "title": "Climate (Observing)", "status": WARNING,
                               "detail": cs["read_only"]})
            elif cs["notices"]:
                checks.append({"key": "climate", "title": "Climate (Observing)", "status": WARNING,
                               "detail": cs["notices"][-1]["message"]})
            elif cs["rooms"]:
                obs = cs["observation"]
                checks.append({"key": "climate", "title": "Climate (Observing)",
                               "status": OK if obs["writable"] else WARNING,
                               "detail": f"{cs['rooms']} rooms observed, {obs['tracked']} entities; no thermostat is "
                                         "controlled" if obs["writable"] else obs["error"] or "Not stored"})

        for key, title, rt, words in (
            ("occupancy_engine", "Occupancy engine", self.occupancy,
             {"authoritative": "The App's engine decides room occupancy", "shadow": "Shadow mode next to the integration's engine",
              "standalone": "Not publishing (no companion lease and no integration bridge)", "starting": "Starting"}),
            ("lighting_runtime", "Lighting", self.lighting,
             {"owner": "The App controls lighting", "shadow": "Shadow mode: the integration controls the lights",
              "standalone": "Not running (waiting for a companion lease or the integration's bridge)", "starting": "Starting"}),
            ("curtains_runtime", "Curtains", self.curtains,
             {"owner": "The App controls the curtains", "shadow": "Shadow mode: the integration moves the covers",
              "standalone": "Not running (waiting for a companion lease or the integration's bridge)", "starting": "Starting"}),
            ("forecast_runtime", "Predictions", self.forecast,
             {"owner": "The App produces the forecasts", "shadow": "Shadow mode: the integration publishes",
              "standalone": "Not running", "starting": "Starting"}),
        ):
            if rt is None:
                continue
            detail = words.get(rt.mode, rt.mode)
            cm = getattr(rt, "cm", None)
            if cm is not None and cm.active:
                via = " (through the companion)" if cm.owner else ""
                detail = detail + via if cm.owner else {
                    "unpaired": "Waiting: pair the App with the companion", "no_config": f"Waiting: {cm.error}",
                    "released": "Stopped by an administrator (take control again in Settings → System)",
                    "lost": f"Lost control: {cm.error}; claiming again", "claiming": "Claiming control",
                    "waiting": "Waiting for the companion session", "absent": detail}.get(cm.state, detail)
                status = OK if cm.owner else WARNING
            else:
                status = OK if rt.mode in ("shadow", "owner", "authoritative") else (
                    WARNING if rt.bridge in ("incompatible", "error") else INFO)
                if rt.bridge in ("incompatible", "error", "unsupported") and getattr(rt, "bridge_error", None):
                    detail = rt.bridge_error
            if key == "occupancy_engine" and rt.self_test and not rt.self_test["passed"]:
                status, detail = ERROR, "The engine self-test failed"
            checks.append({"key": key, "title": title, "status": status, "detail": detail})

        safety = self.safety()
        parts = []
        if safety["companion"]:
            parts.append(f"devices only through the companion's fenced executor ({safety['companion_commands_sent']} commands)")
        else:
            if safety["lighting_owner"] == "app":
                parts.append(f"lights only through the integration's fenced executor ({safety['lighting_commands_sent']} commands)")
            if safety["curtains_owner"] == "app":
                parts.append(f"covers only through the integration's fenced executor ({safety['curtain_commands_sent']} commands)")
        device = ("Device control: " + "; ".join(parts)) if parts else "No device control"
        checks.append({"key": "safety", "title": "Safety", "status": OK if safety["service_calls_sent"] == 0 else ERROR,
                       "detail": f"No direct service calls. {device}."})
        return checks

    def health(self) -> dict[str, Any]:
        checks = self.checks()
        statuses = {c["status"] for c in checks}
        overall = ERROR if ERROR in statuses else WARNING if WARNING in statuses else OK
        return {"status": overall, "version": APP_VERSION, "phase": APP_PHASE, "uptime_s": self.uptime_seconds(),
                "started_at": self.started_at, "home_assistant": self.ha.state if self.ha else "not_configured",
                "checks": checks}

    def system(self) -> dict[str, Any]:
        """Settings → System: every owner, the configuration authority, the companion, engine health, sync."""
        runtimes = {}
        for domain, rt in (("occupancy", self.occupancy), ("lighting", self.lighting), ("curtains", self.curtains),
                           ("forecast", self.forecast)):
            if rt is None:
                continue
            cm = getattr(rt, "cm", None)
            runtimes[domain] = {"mode": rt.mode, "bridge": rt.bridge, "bridge_error": getattr(rt, "bridge_error", None),
                                "owner": self._owner(rt, ("app", "resume_pending")),
                                "companion": cm.status() if cm is not None else None,
                                "running": bool(getattr(rt, "instance", None) is not None
                                                and getattr(getattr(rt, "instance", None), "started", False))
                                if domain != "occupancy" else rt.house is not None,
                                "counters": dict(getattr(rt, "counters", {}) or {})}
        return {
            "app": {"version": APP_VERSION, "phase": APP_PHASE},
            "companion": self.companion.brief() if self.companion else None,
            "config": self.core_config.status(),
            "config_authority": self.config_authority(),
            "core_host": self.core_host.status() if self.core_host else None,
            "owners": {"occupancy": self.occupancy_owner(), "lighting": self.lighting_owner(),
                       "curtains": self.curtains_owner(), "forecast": self.forecast_owner()},
            "runtimes": runtimes,
            "integration": self.ha.integration.as_dict() if self.ha else None,
            "migration": self.migration.status() if self.migration else None,
            "safety": self.safety(),
            "generated_at": utcnow_iso(),
        }

    def dashboard(self) -> dict[str, Any]:
        """The main dashboard: house occupancy, lighting, mood, room modes, overrides, curtains, forecasts, offline
        devices, health and the engines - from the App's own runtimes (nothing is computed twice)."""
        from smart_occupancy.models import IntegrationConfig  # noqa: PLC0415

        options = self.core_config.options
        if options is None:
            options = (self.lighting.options if self.lighting and self.lighting.options else None) or {}
        config = IntegrationConfig.from_dict(options)
        ha = self.ha
        areas = {a.get("area_id"): a.get("name") for a in (ha.areas if ha else [])}
        occ = self.occupancy
        ctrl = self.lighting.controller if self.lighting else None
        mgr = self.curtains.manager if self.curtains else None
        fc = self.forecast.manager if self.forecast else None
        fresh = {d: bool(getattr(rt, "cm", None) and rt.cm.owner) or (
            rt is not None and getattr(rt, "cm", None) is not None and not rt.cm.active
            and rt.mode in ("owner", "authoritative"))
            for d, rt in (("occupancy", occ), ("lighting", self.lighting), ("curtains", self.curtains),
                          ("forecast", self.forecast))}
        rooms = []
        occupied = vacant = unknown = 0
        for area_id, room in sorted(config.enabled_rooms.items(), key=lambda kv: str(areas.get(kv[0]) or kv[0])):
            result = occ.result(area_id) if occ is not None else None
            wire = occ.published_wire(area_id) if occ is not None else None
            state = result.state.value if result is not None else (wire or {}).get("state")
            is_occupied = result.occupied if result is not None else (wire or {}).get("occupied")
            if is_occupied is True:
                occupied += 1
            elif is_occupied is False:
                vacant += 1
            else:
                unknown += 1
            lighting: dict[str, Any] = {"mode": None, "active": False, "overrides": 0, "paused": False, "mood": None}
            daylight = None
            if ctrl is not None:
                try:
                    active = ctrl.room_active(area_id)
                    rt = ctrl.runtime.get(area_id)
                    lighting = {"mode": "paused" if active and ctrl.manual_mode else "active" if active else "observe",
                                "active": active, "overrides": len(rt.overrides) if rt else 0,
                                "paused": active and ctrl.manual_mode, "mood": ctrl.room_mood(area_id)[0]}
                    daylight = ctrl.daylight.status(area_id).summary().get("decision")
                except Exception:  # noqa: BLE001 - diagnostics only
                    pass
            forecast = None
            if fc is not None and (rf := fc.room(area_id)) is not None and rf.status == "ok":
                forecast = {"probability": round(rf.probability * 100) if rf.probability is not None else None,
                            "predicted": rf.predicted, "horizon": rf.horizon}
            windows = [w for w in mgr.config.windows.values() if w.area_id == area_id] if mgr is not None else []
            rooms.append({
                "area_id": area_id, "name": areas.get(area_id) or area_id,
                "occupancy": {"state": state, "occupied": is_occupied,
                              "reason": result.reason if result is not None else (wire or {}).get("reason"),
                              "since": result.state_since.isoformat(timespec="seconds") if result is not None
                              and result.state_since else (wire or {}).get("state_since"),
                              "fresh": fresh["occupancy"]},
                "lighting": lighting, "daylight": daylight, "windows": len(windows), "forecast": forecast,
                "sensors": len(room.sensors), "lights": len(room.lights),
            })
        mood: dict[str, Any] = {"current": None, "manual": False, "source": config.mood.source, "detail": None,
                                "options": list(config.mood.builtin or ())}
        if ctrl is not None:
            try:
                status = ctrl.mood_status()
                mood.update(current=status.mood, manual=status.manual, detail=status.detail,
                            options=[m["name"] for m in ctrl.discovered_moods()] if hasattr(ctrl, "discovered_moods")
                            else mood["options"], main_mood=status.main_mood)
            except Exception:  # noqa: BLE001
                pass
        curtains = None
        if mgr is not None:
            windows_out = []
            for window_id, window in mgr.config.windows.items():
                running = mgr.running(window_id)
                windows_out.append({"window_id": window_id, "name": window.name, "area_id": window.area_id,
                                    "room": areas.get(window.area_id) or window.area_id, "mode": window.mode,
                                    "moving": running is not None, "covers": [x.entity_id for x in window.layers]})
            curtains = {"windows": windows_out, "automatic": sum(1 for w in windows_out if w["mode"] == "automatic"),
                        "moving": sum(1 for w in windows_out if w["moving"]), "fresh": fresh["curtains"]}
        forecast_summary = None
        if fc is not None:
            nr = fc.next_room
            forecast_summary = {"learning": fc.status.state, "days": len(fc.days),
                                "next_room": {"name": fc.name(nr["area_id"]), "probability": round(nr["probability"] * 100)}
                                if nr else None, "fresh": fresh["forecast"]}
        snapshot = self.core_host.snapshot if self.core_host is not None else None
        unavailable = []
        if snapshot is not None:
            from smart_occupancy.occupancy import CONFIGURATION_INVALID, OPERATIONAL_UNAVAILABLE  # noqa: PLC0415

            for item in snapshot.entity_issues(OPERATIONAL_UNAVAILABLE | CONFIGURATION_INVALID):
                unavailable.append({"entity_id": item["entity_id"], "name": item.get("name"), "rooms": item.get("rooms"),
                                    "title": item.get("title")})
        health = self.health()
        return {
            "generated_at": utcnow_iso(),
            "house": {"rooms": len(rooms), "occupied": occupied, "vacant": vacant, "unknown": unknown,
                      "occupied_rooms": [r["name"] for r in rooms if r["occupancy"]["occupied"] is True]},
            "rooms": rooms, "mood": mood, "curtains": curtains, "forecast": forecast_summary,
            "devices": {"unavailable": unavailable, "problems": len(snapshot.problems) if snapshot else 0,
                        "status": snapshot.status.value if snapshot else None},
            "health": {"status": health["status"], "checks": health["checks"]},
            "engines": {d: {"owner": o, "fresh": fresh[d]} for d, o in (
                ("occupancy", self.occupancy_owner()), ("lighting", self.lighting_owner()),
                ("curtains", self.curtains_owner()), ("forecast", self.forecast_owner()))},
            "config_authority": self.config_authority(),
            "companion": self.companion.brief() if self.companion else None,
        }

    def overview(self) -> dict[str, Any]:
        ha = self.ha
        health = self.health()
        authority = self.config_authority()
        return {
            "app": {"name": APP_NAME, "subtitle": APP_SUBTITLE, "version": APP_VERSION, "phase": APP_PHASE,
                    "started_at": self.started_at,
                    "uptime_s": health["uptime_s"], "health": health["status"], "dev_mode": self.settings.dev_mode},
            "checks": health["checks"],
            "home_assistant": {"connection": ha.status() if ha else {"state": "not_configured", "connected": False},
                               "info": ha.info() if ha and ha.config else None},
            "integration": (ha.integration.as_dict() if ha else None),
            "integration_entities": ha.integration_entities() if ha else [],
            "supervisor": self.supervisor.as_dict() if self.supervisor else None,
            "rooms": self.rooms.status() if self.rooms else None,
            "floorplan": self.floorplan.status() if self.floorplan else None,
            "climate": self.climate.status() if self.climate else None,
            "presence": self.presence.status() if self.presence else None,
            "modules": modules.modules(self.occupancy_owner(), self.lighting_owner(), self.curtains_owner(),
                                       self.forecast_owner(), authority),
            "migration": modules.migration(self.occupancy_owner(), self.lighting_owner(), self.curtains_owner(),
                                           self.forecast_owner(), authority),
            "lighting": ({"mode": self.lighting.mode, "owner": self.lighting_owner(), "bridge": self.lighting.bridge}
                         if self.lighting else None),
            "curtains": ({"mode": self.curtains.mode, "owner": self.curtains_owner(), "bridge": self.curtains.bridge}
                         if self.curtains else None),
            "predictions": ({"mode": self.forecast.mode, "owner": self.forecast_owner(), "bridge": self.forecast.bridge}
                            if self.forecast else None),
            "engine": ({"mode": self.occupancy.mode, "owner": self.occupancy_owner(), "bridge": self.occupancy.bridge}
                       if self.occupancy else None),
            "companion": self.companion.brief() if self.companion else None,
            "config": self.core_config.status(),
            "system": self.core_host.status() if self.core_host else None,
            "safety": self.safety(),
            "generated_at": utcnow_iso(),
        }

    def diagnostics(self) -> dict[str, Any]:
        data = self.overview()
        data["settings"] = self.settings.public()
        data["runtime"] = {"python": platform.python_version(), "aiohttp": aiohttp.__version__,
                           "platform": platform.platform(), "time": datetime.now(UTC).isoformat(timespec="seconds")}
        data["log"] = self.log_buffer.entries() if self.log_buffer else []
        return data
