"""The curtain editor commands of the App's interface (Phase A5): one API, wherever the configuration lives.

The interface uses the integration's own curtain websocket commands (the same
commands, payloads and answers as its Smart Curtains card):

* **The integration controls the curtains** (and owns the configuration): reads
  are forwarded with the read-only path, changes with the configuration-write
  path (administrators only, checked by the web layer) - the integration
  validates and applies them as for its card. A forwarded command that changes a
  window's *runtime* (a confirmed preset applied, a hold released, a manual change
  cleared, the calibration reset) also runs on the App's shadow copy, so the
  shadow keeps evolving on the same events (``SHADOW_MIRRORED``).
* **The App controls the curtains**: the same command handlers (the vendored
  copies) run in the App against the App's own manager, with the Ingress user as
  the connection's user. A confirmed preset then moves a cover only through the
  integration's fenced ``execute``.

Nothing else is accepted: only commands of :data:`ha.CURTAIN_READ_COMMANDS` and
:data:`ha.CURTAIN_WRITE_COMMANDS`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import HomeAssistantError as HostError, Unauthorized

from .ha import CURTAIN_READ_COMMANDS, CURTAIN_WRITE_COMMANDS, HomeAssistantClient, HomeAssistantError

_LOGGER = logging.getLogger(__name__)
COMMAND_TIMEOUT = 30.0


class CurtainsApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _handlers() -> dict[str, Any]:
    from smart_occupancy.curtains.websocket import CURTAIN_HANDLERS  # noqa: PLC0415

    return {handler._ws_command: handler for handler in CURTAIN_HANDLERS}  # noqa: SLF001


class CurtainsApi:
    #: Forwarded commands that change a window's runtime state rather than its configuration (which reaches the
    #: shadow as a ``config`` event): the shadow runs the same handler on its own copy.
    SHADOW_MIRRORED: frozenset[str] = frozenset(f"smart_occupancy/curtains/{c}" for c in (
        "apply", "resume", "manual/clear", "calibration/reset", "rescan"))

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
        return command in CURTAIN_WRITE_COMMANDS

    def where(self) -> str:
        return "app" if self.runtime.mode == "owner" and self.runtime.manager is not None else "integration"

    async def call(self, msg: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        command = msg.get("type")
        if command not in CURTAIN_READ_COMMANDS and command not in CURTAIN_WRITE_COMMANDS:
            raise CurtainsApiError("not_allowed", f"{command!r} is not a curtain editor command", 403)
        if self.is_write(command) and not is_admin:
            raise CurtainsApiError("unauthorized", "Only administrators can change curtains", 403)
        if getattr(self.runtime, "_switching", False) and self.is_write(command):
            raise CurtainsApiError("busy", "Curtain control is changing owner; try again in a moment", 409)
        params = {k: v for k, v in msg.items() if k not in ("type", "id")}
        if self.where() == "app":
            return await self._local(command, params, user_id=user_id, is_admin=is_admin)
        mirrored = command in self.SHADOW_MIRRORED and self.runtime.mode == "shadow" and self.runtime.manager is not None
        if mirrored:
            # The shadow runs the command first: a confirmed preset or a resume plans against the covers as they are
            # *now*. Run after the integration's reply, the shadow would see a quick cover already at its target
            # (an open/close-only cover reports at once) and plan nothing, a false mismatch. The shadow's commands
            # reach nothing, so the order changes no cover; a refusal by the integration below discards the
            # diverged shadow (a fresh copy follows).
            try:
                await self._local(command, params, user_id=user_id, is_admin=is_admin)
            except CurtainsApiError as err:
                _LOGGER.debug("shadow did not mirror %s: %s", command, err)
        try:
            if self.is_write(command):
                result = await self.ha.config_write(command, **params)
            else:
                result = await self.ha.command(command, **params)
        except HomeAssistantError as err:
            if mirrored:
                self.runtime.discard_shadow(f"{command} refused by the integration")
            raise CurtainsApiError(err.code, str(err).split(": ", 1)[-1], 409 if err.code == "managed_by_app" else 400) \
                from err
        except (ConnectionError, TimeoutError) as err:
            if mirrored:
                self.runtime.discard_shadow(f"{command} not answered")
            raise CurtainsApiError("not_connected", "Home Assistant is not connected", 503) from err
        return result

    async def _local(self, command: str, params: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
        handler = self.handlers.get(command)
        inst = self.runtime.instance
        if handler is None or inst is None:
            raise CurtainsApiError("not_found", f"{command} is not available", 404)
        self._next_id += 1
        msg = {"id": self._next_id, "type": command, **params}
        try:
            msg = handler._ws_schema(msg)  # noqa: SLF001
        except vol.Invalid as err:
            raise CurtainsApiError(websocket_api.ERR_INVALID_FORMAT, str(err)) from err
        connection = websocket_api.ActiveConnection(websocket_api.User(user_id, is_admin=is_admin))
        try:
            handler(inst.hass, connection, msg)
        except Unauthorized as err:
            raise CurtainsApiError("unauthorized", "Only administrators can change curtains", 403) from err
        except (vol.Invalid, HostError) as err:
            connection.async_handle_exception(msg, err)
        try:
            answer = await asyncio.wait_for(asyncio.shield(connection.done), COMMAND_TIMEOUT)
        except TimeoutError as err:
            raise CurtainsApiError("timeout", f"{command} did not answer", 504) from err
        finally:
            connection.close()
        if not answer.get("success"):
            error = answer.get("error") or {}
            raise CurtainsApiError(str(error.get("code") or "error"), str(error.get("message") or "Refused"))
        return answer.get("result")
