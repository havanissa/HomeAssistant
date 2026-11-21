"""Websocket commands of the companion (administrators only): the App's session, pairing, lease and publishing.

None of them can call a service or change anything outside this integration's
own entities.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
import voluptuous as vol

from . import protocol as P
from .const import DOMAIN
from .hub import Hub, HubError

_SESSION = vol.All(str, vol.Length(min=32, max=32))
_FENCE = {vol.Required("session_id"): _SESSION, vol.Required("epoch"): int, vol.Required("lease_token"): str,
          vol.Required("seq"): int}


def _hub(hass: HomeAssistant) -> Hub | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


def _run(hass: HomeAssistant, connection: Any, msg: dict[str, Any], action) -> None:
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Occupancy Detection is not set up")
        return
    try:
        result = action(hub)
    except HubError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/session",
    vol.Required("protocol"): int,
    vol.Required("app_version"): vol.All(str, vol.Length(max=32)),
    vol.Required("instance_id"): vol.All(str, vol.Length(max=32)),
    vol.Optional("pairing_token"): vol.All(str, vol.Length(max=64)),
})
@callback
def ws_session(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """The App's session: a subscription that lasts as long as the App's connection."""
    hub = _hub(hass)
    if hub is None:
        connection.send_error(msg["id"], "not_loaded", "Smart Occupancy Detection is not set up")
        return
    msg_id = msg["id"]

    @callback
    def send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    try:
        session, hello = hub.open_session(connection, send, msg)
    except HubError as err:
        connection.send_error(msg_id, err.code, str(err))
        return
    connection.subscriptions[msg_id] = lambda: hub.close_session(session.session_id)
    connection.send_result(msg_id)
    send(hello)


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/pair",
    vol.Required("session_id"): _SESSION,
    vol.Optional("replace", default=False): bool,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@callback
def ws_pair(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.pair(connection, msg, connection.user.id if connection.user else None))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/unpair",
    vol.Required("session_id"): _SESSION,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@callback
def ws_unpair(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.unpair(connection, msg, connection.user.id if connection.user else None))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/claim", vol.Required("session_id"): _SESSION})
@callback
def ws_claim(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.claim(connection, msg))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/heartbeat", **_FENCE})
@callback
def ws_heartbeat(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.heartbeat(connection, msg))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/release", **_FENCE})
@callback
def ws_release(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.release(connection, msg))


@websocket_api.require_admin
@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/publish", **_FENCE,
    vol.Required("rooms"): dict,
    vol.Optional("disabled"): dict,
    vol.Optional("deleted"): list,
    vol.Optional("app_version"): vol.All(str, vol.Length(max=32)),
})
@callback
def ws_publish(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.publish(connection, msg))


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status"})
@callback
def ws_status(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    _run(hass, connection, msg, lambda hub: hub.status())


COMMANDS = (ws_session, ws_pair, ws_unpair, ws_claim, ws_heartbeat, ws_release, ws_publish, ws_status)


@callback
def async_register_api(hass: HomeAssistant) -> None:
    for command in COMMANDS:
        websocket_api.async_register_command(hass, command)


__all__ = ["P", "async_register_api"]
