"""The websocket command API the lighting handlers are written against (``websocket_api`` subset).

In the App these handlers do not run behind Home Assistant's websocket: the App's
command router (``smart_lighting.lighting_api``) validates a request with the
handler's own schema, calls it with a :class:`ActiveConnection` for the Ingress
user, and returns what the handler sent. The decorators record the command type
and schema exactly as Home Assistant's do; ``require_admin`` refuses
non-administrators the same way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import functools
from typing import Any

import voluptuous as vol

from ..core import Context, HomeAssistant
from ..exceptions import HomeAssistantError, Unauthorized

ERR_ID_REUSE = "id_reuse"
ERR_INVALID_FORMAT = "invalid_format"
ERR_NOT_ALLOWED = "not_allowed"
ERR_NOT_FOUND = "not_found"
ERR_NOT_SUPPORTED = "not_supported"
ERR_HOME_ASSISTANT_ERROR = "home_assistant_error"
ERR_SERVICE_VALIDATION_ERROR = "service_validation_error"
ERR_UNKNOWN_COMMAND = "unknown_command"
ERR_UNKNOWN_ERROR = "unknown_error"
ERR_UNAUTHORIZED = "unauthorized"
ERR_TIMEOUT = "timeout"
ERR_TEMPLATE_ERROR = "template_error"

BASE_COMMAND_MESSAGE_SCHEMA = vol.Schema({vol.Required("id"): vol.All(vol.Coerce(int), vol.Range(min=0))})


@dataclass(slots=True, frozen=True)
class User:
    id: str | None
    name: str | None = None
    is_admin: bool = False


def result_message(msg_id: int, result: Any = None) -> dict[str, Any]:
    return {"id": msg_id, "type": "result", "success": True, "result": result}


def error_message(msg_id: int | None, code: str, message: str) -> dict[str, Any]:
    return {"id": msg_id, "type": "result", "success": False, "error": {"code": code, "message": message}}


def event_message(msg_id: int, event: Any) -> dict[str, Any]:
    return {"id": msg_id, "type": "event", "event": event}


class ActiveConnection:
    """One request's connection: the handler's answer resolves :attr:`done`; events go to ``on_message``."""

    def __init__(self, user: User, *, on_message: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.user = user
        self.subscriptions: dict[int, Callable[[], None]] = {}
        self.on_message = on_message
        self.done: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

    def context(self, msg: dict[str, Any]) -> Context:
        return Context(user_id=self.user.id)

    def _finish(self, message: dict[str, Any]) -> None:
        if not self.done.done():
            self.done.set_result(message)

    def send_result(self, msg_id: int, result: Any = None) -> None:
        self._finish(result_message(msg_id, result))

    def send_error(self, msg_id: int | None, code: str, message: str, translation_key: str | None = None,
                   translation_domain: str | None = None, translation_placeholders: Any = None) -> None:
        self._finish(error_message(msg_id, code, message))

    def send_message(self, message: Any) -> None:
        if isinstance(message, dict) and message.get("type") == "result":
            self._finish(message)
        elif self.on_message is not None:
            self.on_message(message)

    def async_handle_exception(self, msg: dict[str, Any], err: Exception) -> None:
        if isinstance(err, Unauthorized):
            self.send_error(msg.get("id"), ERR_UNAUTHORIZED, "Unauthorized")
        elif isinstance(err, vol.Invalid):
            self.send_error(msg.get("id"), ERR_INVALID_FORMAT, str(err))
        elif isinstance(err, HomeAssistantError):
            self.send_error(msg.get("id"), ERR_HOME_ASSISTANT_ERROR, str(err) or type(err).__name__)
        else:
            self.send_error(msg.get("id"), ERR_UNKNOWN_ERROR, "Unknown error")

    def close(self) -> None:
        for cancel in list(self.subscriptions.values()):
            cancel()
        self.subscriptions.clear()


def websocket_command(schema: dict[Any, Any] | vol.Schema) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if isinstance(schema, vol.Schema):
        command = schema.schema["type"]
        extended = schema.extend(BASE_COMMAND_MESSAGE_SCHEMA.schema)
    else:
        command = next(v for k, v in schema.items() if str(k) == "type")
        extended = BASE_COMMAND_MESSAGE_SCHEMA.extend(schema)

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        func._ws_schema = extended  # type: ignore[attr-defined]
        func._ws_command = command  # type: ignore[attr-defined]
        return func

    return decorate


def require_admin(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def with_admin(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> Any:
        if connection.user is None or not connection.user.is_admin:
            raise Unauthorized
        return func(hass, connection, msg)

    return with_admin


def async_response(func: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(func)
    def schedule(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
        async def run() -> None:
            try:
                await func(hass, connection, msg)
            except Exception as err:  # noqa: BLE001 - reported to the caller like Home Assistant does
                connection.async_handle_exception(msg, err)

        hass.async_create_background_task(run(), f"websocket {msg.get('type')}")

    schedule._ws_async = True  # type: ignore[attr-defined]
    return schedule


def async_register_command(hass: HomeAssistant, command_or_handler: Any, handler: Any = None,
                           schema: Any = None) -> None:
    """Handlers are registered with the App's router instead (kept for the vendored registration code)."""
    hass.data.setdefault("_host_ws_handlers", {})[getattr(command_or_handler, "_ws_command", None)] = command_or_handler
