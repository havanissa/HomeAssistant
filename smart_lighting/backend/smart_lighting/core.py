"""The App's state: Home Assistant and Supervisor clients, health and overview."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
import logging
import platform
import time
from typing import Any

import aiohttp

from . import modules
from .const import APP_NAME, APP_PHASE, APP_VERSION
from .floorplan import FloorPlanService
from .ha import ALLOWED_COMMANDS, CONFIG_WRITE_COMMANDS, READ_ONLY_COMMANDS, HomeAssistantClient, utcnow_iso
from .logbuffer import RingBufferHandler
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
        self.started_at = utcnow_iso()
        self._started_monotonic = time.monotonic()
        self.session: aiohttp.ClientSession | None = None
        self.ha: HomeAssistantClient | None = None
        self.supervisor: SupervisorClient | None = None
        self.rooms: RoomsService | None = None
        self.floorplan: FloorPlanService | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._rooms_timer: asyncio.TimerHandle | None = None
        self._rooms_listeners: list = []

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.supervisor = SupervisorClient(self.session, self.settings.supervisor_url, self.settings.token)
        self.rooms = RoomsService(self.ha, self.settings.data_dir)
        self.floorplan = FloorPlanService(self.ha, self.rooms, self.settings.data_dir)
        self.floorplan.load()
        self.ha.add_listener(self._on_ha)
        await self.ha.start()
        self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="supervisor-refresh"))
        _LOGGER.info("%s %s (Phase %s) started; room configuration edits only through the integration's API",
                     APP_NAME, APP_VERSION, APP_PHASE)

    def add_rooms_listener(self, listener) -> None:
        """``listener(kind)`` with kind "rooms" (configuration changed) or "registries"."""
        self._rooms_listeners.append(listener)

    def _on_ha(self, kind: str, payload) -> None:
        if self.rooms is None:
            return
        if kind == "registries":
            self.rooms.invalidate_index()
            self._emit_rooms("registries")
        elif kind in ("config_entry", "integration") or (kind == "status" and self.ha and self.ha.connected):
            self._schedule_rooms_refresh()

    def _schedule_rooms_refresh(self) -> None:
        if self._rooms_timer is not None:
            self._rooms_timer.cancel()
        self._rooms_timer = asyncio.get_running_loop().call_later(ROOMS_REFRESH_DELAY, self._start_rooms_refresh)

    def _start_rooms_refresh(self) -> None:
        self._rooms_timer = None
        task = asyncio.get_running_loop().create_task(self._rooms_refresh())
        self._tasks.append(task)
        task.add_done_callback(lambda t: t in self._tasks and self._tasks.remove(t))

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
        if self._rooms_timer is not None:
            self._rooms_timer.cancel()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
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

    def safety(self) -> dict[str, Any]:
        sent = dict(self.ha.commands_sent) if self.ha else {}
        other_commands = sum(n for cmd, n in sent.items() if cmd not in ALLOWED_COMMANDS)
        writes = sum(n for cmd, n in sent.items() if cmd in CONFIG_WRITE_COMMANDS)
        return {
            "read_only": False,
            "configuration_writes": True,
            "write_scope": "Room configuration of the existing integration (sensor and light assignments, "
                           "through its validated, administrator-only commands); floor plans in the App's own "
                           "storage",
            "config_writes_sent": writes,
            "recent_config_writes": list(self.ha.config_writes) if self.ha else [],
            "device_control": False,
            "automation_engine": False,
            "automatic_migration": False,
            "modifies_integration": writes > 0,
            "service_calls_sent": other_commands,  # always 0: the client cannot send them
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
            checks.append(
                {
                    "key": "live_updates",
                    "title": "Live state updates",
                    "status": OK,
                    "detail": f"Subscribed; {ha.events_per_minute()} state changes in the last minute",
                }
            )

        if sup is None or not sup.configured:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": INFO, "detail": "Not used (development mode)"})
        elif sup.available and not sup.error:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": OK, "detail": "App and system information read"})
        elif sup.checked_at is None:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": INFO, "detail": "Not checked yet"})
        else:
            checks.append({"key": "supervisor", "title": "Supervisor API", "status": WARNING, "detail": sup.error or "Unavailable"})

        if ha and ha.integration.checked:
            report = ha.integration
            detail = {
                "loaded": f"Running (version {report.version or 'unknown'}); runs every automation",
                "installed": "Installed but not set up",
                "not_running": "Set up but not running",
                "not_installed": "Not installed",
            }.get(report.status, report.status)
            checks.append({"key": "integration", "title": "Existing integration", "status": INFO, "detail": detail})

        if ha and ha.connected and self.rooms is not None:
            api = self.rooms.api_state
            if api == "ok":
                writable = self.rooms.status()["writable"]
                checks.append({"key": "rooms_api", "title": "Room configuration API",
                               "status": OK if writable else WARNING,
                               "detail": "Available; room changes are saved in the integration" if writable
                               else "Readable; saving needs the integration to be running"})
            elif api == "unsupported":
                checks.append({"key": "rooms_api", "title": "Room configuration API", "status": WARNING,
                               "detail": self.rooms.api_error or "Not supported by the installed integration"})
            elif api in ("error", "unavailable"):
                checks.append({"key": "rooms_api", "title": "Room configuration API", "status": WARNING,
                               "detail": self.rooms.api_error or "Not available"})

        if self.floorplan is not None:
            fp = self.floorplan.status()
            if fp["read_only"]:
                checks.append({"key": "floorplan", "title": "Floor plan storage", "status": WARNING,
                               "detail": fp["read_only"]})
            elif any(n["code"].startswith("recovered") for n in fp["notices"]):
                checks.append({"key": "floorplan", "title": "Floor plan storage", "status": WARNING,
                               "detail": "Recovered after a damaged save; see the Floor plan page"})
            else:
                s = fp["summary"]
                checks.append({"key": "floorplan", "title": "Floor plan storage", "status": OK,
                               "detail": f"{s['floors']} floor(s), {s['rooms']} room(s), {s['placements']} device "
                                         "icon(s); stored in the App"})

        safety = self.safety()
        checks.append(
            {
                "key": "safety",
                "title": "Safety",
                "status": OK if safety["service_calls_sent"] == 0 else ERROR,
                "detail": "No service calls and no device control. Configuration changes only through the "
                          f"integration's room commands ({safety['config_writes_sent']} so far).",
            }
        )
        return checks

    def health(self) -> dict[str, Any]:
        checks = self.checks()
        statuses = {c["status"] for c in checks}
        overall = ERROR if ERROR in statuses else WARNING if WARNING in statuses else OK
        return {
            "status": overall,
            "version": APP_VERSION,
            "phase": APP_PHASE,
            "uptime_s": self.uptime_seconds(),
            "started_at": self.started_at,
            "home_assistant": self.ha.state if self.ha else "not_configured",
            "checks": checks,
        }

    def overview(self) -> dict[str, Any]:
        ha = self.ha
        health = self.health()
        return {
            "app": {
                "name": APP_NAME,
                "version": APP_VERSION,
                "phase": APP_PHASE,
                "started_at": self.started_at,
                "uptime_s": health["uptime_s"],
                "health": health["status"],
                "dev_mode": self.settings.dev_mode,
            },
            "checks": health["checks"],
            "home_assistant": {
                "connection": ha.status() if ha else {"state": "not_configured", "connected": False},
                "info": ha.info() if ha and ha.config else None,
            },
            "integration": (ha.integration.as_dict() if ha else None),
            "integration_entities": ha.integration_entities() if ha else [],
            "supervisor": self.supervisor.as_dict() if self.supervisor else None,
            "rooms": self.rooms.status() if self.rooms else None,
            "floorplan": self.floorplan.status() if self.floorplan else None,
            "modules": modules.modules(),
            "migration": modules.migration(),
            "safety": self.safety(),
            "generated_at": utcnow_iso(),
        }

    def diagnostics(self) -> dict[str, Any]:
        data = self.overview()
        data["settings"] = self.settings.public()
        data["runtime"] = {
            "python": platform.python_version(),
            "aiohttp": aiohttp.__version__,
            "platform": platform.platform(),
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        data["log"] = self.log_buffer.entries() if self.log_buffer else []
        return data
