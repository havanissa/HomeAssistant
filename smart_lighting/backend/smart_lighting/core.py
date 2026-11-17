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
from .ha import READ_ONLY_COMMANDS, HomeAssistantClient, utcnow_iso
from .logbuffer import RingBufferHandler
from .settings import Settings
from .supervisor import SupervisorClient

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_REFRESH = 600.0

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
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        self.session = aiohttp.ClientSession()
        self.ha = HomeAssistantClient(self.session, self.settings.ha_ws_url, self.settings.token)
        self.supervisor = SupervisorClient(self.session, self.settings.supervisor_url, self.settings.token)
        await self.ha.start()
        self._tasks.append(asyncio.create_task(self._supervisor_loop(), name="supervisor-refresh"))
        _LOGGER.info("%s %s (Phase %s) started; read-only mode", APP_NAME, APP_VERSION, APP_PHASE)

    async def stop(self) -> None:
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
        write_commands = sum(n for cmd, n in sent.items() if cmd not in READ_ONLY_COMMANDS)
        return {
            "read_only": True,
            "device_control": False,
            "automation_engine": False,
            "automatic_migration": False,
            "modifies_integration": False,
            "service_calls_sent": write_commands,  # always 0: the client cannot send them
            "commands_sent": sent,
            "allowed_commands": sorted(READ_ONLY_COMMANDS),
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
                "loaded": f"Running (version {report.version or 'unknown'}); unchanged by the App",
                "installed": "Installed but not set up",
                "not_running": "Set up but not running",
                "not_installed": "Not installed",
            }.get(report.status, report.status)
            checks.append({"key": "integration", "title": "Existing integration", "status": INFO, "detail": detail})

        safety = self.safety()
        checks.append(
            {
                "key": "read_only",
                "title": "Read-only mode",
                "status": OK if safety["service_calls_sent"] == 0 else ERROR,
                "detail": "No service calls, no device control, no configuration changes",
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
