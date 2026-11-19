"""The forecast commands of the App's interface (Phase A5): one API, wherever the data lives.

* **The integration produces the forecasts**: reads are forwarded with the
  read-only path, settings changes with the configuration-write path
  (administrators only). A forwarded change also runs on the App's shadow copy
  (``SHADOW_MIRRORED``), so the shadow keeps the same settings and connections;
  the bridge's ``settings`` event covers it as well.
* **The App produces the forecasts**: the same command handlers (the vendored
  copies) run in the App against the App's own manager.

The Predictions pages read most of what they show from the App's own instance
(it runs the same model on the same learned days in both modes); the forecasts
*that count* (the ``now`` part) come from the authoritative side.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import HomeAssistantError as HostError, Unauthorized

from .ha import FORECAST_READ_COMMANDS, FORECAST_WRITE_COMMANDS, HomeAssistantClient, HomeAssistantError

_LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT = 120.0  # learn / rebuild read the Recorder


class ForecastApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _handlers() -> dict[str, Any]:
    from smart_occupancy.forecast_websocket import FORECAST_HANDLERS  # noqa: PLC0415

    return {handler._ws_command: handler for handler in FORECAST_HANDLERS}  # noqa: SLF001


class ForecastApi:
    SHADOW_MIRRORED: frozenset[str] = frozenset(f"smart_occupancy/forecast/{c}" for c in (
        "settings", "connection", "learn", "rebuild"))

    def __init__(self, ha: HomeAssistantClient, runtime: Any) -> None:
        self.ha = ha
        self.runtime = runtime
        self._handlers: dict[str, Any] | None = None
        self._next_id = 0

    @property
    def handlers(self) -> dict[str, Any]:
        if self._handlers is None:
            self._handlers = _handlers()
        return self._handlers

    @staticmethod
    def is_write(command: str) -> bool:
        return command in FORECAST_WRITE_COMMANDS

    def where(self) -> str:
        return "app" if self.runtime.mode == "owner" and self.runtime.manager is not None else "integration"

    async def call(self, msg: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        command = msg.get("type")
        if command not in FORECAST_READ_COMMANDS and command not in FORECAST_WRITE_COMMANDS:
            raise ForecastApiError("not_allowed", f"{command!r} is not a forecast command", 403)
        if self.is_write(command) and not is_admin:
            raise ForecastApiError("unauthorized", "Only administrators can change predictions", 403)
        if getattr(self.runtime, "_switching", False) and self.is_write(command):
            raise ForecastApiError("busy", "Forecast control is changing owner; try again in a moment", 409)
        params = {k: v for k, v in msg.items() if k not in ("type", "id")}
        if self.where() == "app":
            return await self.local(command, params, user_id=user_id, is_admin=is_admin)
        try:
            if self.is_write(command):
                result = await self.ha.config_write(command, **params)
            else:
                result = await self.ha.command(command, **params)
        except HomeAssistantError as err:
            raise ForecastApiError(err.code, str(err).split(": ", 1)[-1], 409 if err.code == "managed_by_app" else 400) \
                from err
        except (ConnectionError, TimeoutError) as err:
            raise ForecastApiError("not_connected", "Home Assistant is not connected", 503) from err
        if command in self.SHADOW_MIRRORED and self.runtime.mode == "shadow" and self.runtime.manager is not None:
            try:
                await self.local(command, params, user_id=user_id, is_admin=is_admin)
            except ForecastApiError as err:
                _LOGGER.debug("shadow did not mirror %s: %s", command, err)
        return result

    async def local(self, command: str, params: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        """Run the integration's own handler against the App's manager (also for the App's own pages)."""
        handler = self.handlers.get(command)
        inst = self.runtime.instance
        if handler is None or inst is None or inst.manager is None:
            raise ForecastApiError("not_found", f"{command} is not available", 404)
        self._next_id += 1
        msg = {"id": self._next_id, "type": command, **params}
        try:
            msg = handler._ws_schema(msg)  # noqa: SLF001
        except vol.Invalid as err:
            raise ForecastApiError(websocket_api.ERR_INVALID_FORMAT, str(err)) from err
        connection = websocket_api.ActiveConnection(websocket_api.User(user_id, is_admin=is_admin))
        try:
            handler(inst.hass, connection, msg)
        except Unauthorized as err:
            raise ForecastApiError("unauthorized", "Only administrators can change predictions", 403) from err
        except (vol.Invalid, HostError) as err:
            connection.async_handle_exception(msg, err)
        try:
            answer = await asyncio.wait_for(asyncio.shield(connection.done), COMMAND_TIMEOUT)
        except TimeoutError as err:
            raise ForecastApiError("timeout", f"{command} did not answer", 504) from err
        finally:
            connection.close()
        if not answer.get("success"):
            error = answer.get("error") or {}
            raise ForecastApiError(str(error.get("code") or "error"), str(error.get("message") or "Refused"))
        return answer.get("result")
