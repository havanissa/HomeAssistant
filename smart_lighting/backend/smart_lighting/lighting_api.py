"""The lighting editor commands of the App's interface (Phase A4): one API, wherever the configuration lives.

The interface uses the integration's own lighting websocket commands (the same
50 commands, payloads and answers as its dashboard cards):

* **The integration controls lighting** (and owns its configuration): reads are
  forwarded with the read-only path, changes with the configuration-write path
  (administrators only, checked by the web layer) - the integration validates
  and applies them as for its cards.
* **The App controls lighting**: the same command handlers (the vendored copies)
  run in the App against the App's own controller and configuration, with the
  Ingress user as the connection's user. Changes to the integration's options
  (light types, the mood source) are sent to it over the fenced bridge and
  awaited.

Nothing else is accepted: only commands of :data:`ha.LIGHTING_READ_COMMANDS` and
:data:`ha.LIGHTING_WRITE_COMMANDS`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import HomeAssistantError as HostError, Unauthorized

from .ha import LIGHTING_READ_COMMANDS, LIGHTING_WRITE_COMMANDS, HomeAssistantClient, HomeAssistantError

_LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT = 30.0


class LightingApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _handlers() -> dict[str, Any]:
    from smart_occupancy.brightness_websocket import BRIGHTNESS_HANDLERS  # noqa: PLC0415
    from smart_occupancy.lighting_websocket import LIGHTING_HANDLERS  # noqa: PLC0415
    from smart_occupancy.preliminary_websocket import PRELIMINARY_HANDLERS  # noqa: PLC0415
    from smart_occupancy.priority_websocket import PRIORITY_HANDLERS  # noqa: PLC0415
    from smart_occupancy.profile_websocket import PROFILE_HANDLERS  # noqa: PLC0415
    from smart_occupancy.switch_websocket import SWITCH_HANDLERS  # noqa: PLC0415
    from smart_occupancy.zone_websocket import ZONE_HANDLERS  # noqa: PLC0415

    out = {}
    for handler in (*LIGHTING_HANDLERS, *SWITCH_HANDLERS, *PRELIMINARY_HANDLERS, *PROFILE_HANDLERS, *ZONE_HANDLERS,
                    *BRIGHTNESS_HANDLERS, *PRIORITY_HANDLERS):
        out[handler._ws_command] = handler  # noqa: SLF001
    return out


class LightingApi:
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
        return command in LIGHTING_WRITE_COMMANDS

    #: Forwarded commands that change the controller's runtime state (not only its configuration, which reaches
    #: the shadow as a ``config`` event): a manual override cleared, a profile test, switch learning, a daylight
    #: calibration reset, Priority Input claims and the lock, a supervisor check. The shadow runs the same handler
    #: on its own copy, so it keeps evolving on the same events as the integration's controller.
    SHADOW_MIRRORED: frozenset[str] = frozenset(f"smart_occupancy/lighting/{c}" for c in (
        "override/clear", "profile/test", "switch/learn", "room/daylight", "priority/set", "priority/remove",
        "priority/lock", "supervisor/room"))

    def where(self) -> str:
        """Where commands go: ``app`` (local handlers) or ``integration`` (forwarded to the 0.9.0 bridges)."""
        cm = getattr(self.runtime, "cm", None)
        if cm is not None and cm.active:
            return "app"  # Phase A6: the companion runs no engine; the App's handlers are the only editor
        return "app" if self.runtime.mode == "owner" and self.runtime.controller is not None else "integration"

    async def call(self, msg: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        command = msg.get("type")
        if command not in LIGHTING_READ_COMMANDS and command not in LIGHTING_WRITE_COMMANDS:
            raise LightingApiError("not_allowed", f"{command!r} is not a lighting editor command", 403)
        if self.is_write(command) and not is_admin:
            raise LightingApiError("unauthorized", "Only administrators can change lighting", 403)
        if getattr(self.runtime, "_switching", False) and self.is_write(command):
            raise LightingApiError("busy", "Lighting control is changing owner; try again in a moment", 409)
        params = {k: v for k, v in msg.items() if k not in ("type", "id")}
        if self.where() == "app":
            return await self._local(command, params, user_id=user_id, is_admin=is_admin)
        try:
            if self.is_write(command):
                result = await self.ha.config_write(command, **params)
            else:
                result = await self.ha.command(command, **params)
        except HomeAssistantError as err:
            raise LightingApiError(err.code, str(err).split(": ", 1)[-1], 409 if err.code == "managed_by_app" else 400) \
                from err
        except (ConnectionError, TimeoutError) as err:
            raise LightingApiError("not_connected", "Home Assistant is not connected", 503) from err
        if command in self.SHADOW_MIRRORED and self.runtime.mode == "shadow" and self.runtime.controller is not None:
            try:
                await self._local(command, params, user_id=user_id, is_admin=is_admin)
            except LightingApiError as err:  # the integration's answer counts; the shadow is diagnostics
                _LOGGER.debug("shadow did not mirror %s: %s", command, err)
        return result

    async def _local(self, command: str, params: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        """Run the integration's own handler against the App's controller (exactly the command's validation)."""
        handler = self.handlers.get(command)
        inst = self.runtime.instance
        if handler is None or inst is None:
            raise LightingApiError("not_found", f"{command} is not available", 404)
        self._next_id += 1
        msg = {"id": self._next_id, "type": command, **params}
        try:
            msg = handler._ws_schema(msg)  # noqa: SLF001 - the handler's own schema
        except vol.Invalid as err:
            raise LightingApiError(websocket_api.ERR_INVALID_FORMAT, str(err)) from err
        connection = websocket_api.ActiveConnection(websocket_api.User(user_id, is_admin=is_admin))
        backend = inst.backend
        backend.pending_options.clear()
        try:
            handler(inst.hass, connection, msg)
        except Unauthorized as err:
            raise LightingApiError("unauthorized", "Only administrators can change lighting", 403) from err
        except (vol.Invalid, HostError) as err:
            connection.async_handle_exception(msg, err)
        try:
            answer = await asyncio.wait_for(asyncio.shield(connection.done), COMMAND_TIMEOUT)
        except TimeoutError as err:
            raise LightingApiError("timeout", f"{command} did not answer", 504) from err
        finally:
            connection.close()
        for task in list(backend.pending_options):  # changes of the integration's options: their outcome counts
            try:
                await task
            except (HostError, HomeAssistantError, ConnectionError, TimeoutError) as err:
                raise LightingApiError(getattr(err, "code", "failed"), f"The integration refused the change: {err}",
                                       409) from err
        backend.pending_options.clear()
        if not answer.get("success"):
            error = answer.get("error") or {}
            raise LightingApiError(str(error.get("code") or "error"), str(error.get("message") or "Refused"))
        return answer.get("result")
