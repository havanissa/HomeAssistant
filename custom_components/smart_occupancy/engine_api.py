"""Websocket commands of the occupancy engine bridge (Phase A3.2, see ``engine_bridge``).

All commands need a Home Assistant administrator. They are used by the Smart
Lighting App's backend over its Supervisor connection; none of them can call a
service or change anything but the ownership of the published occupancy and
the App's accepted results.

* ``smart_occupancy/engine/status`` - the ownership record, lease, sessions,
  versions, accepted results and history (reads only);
* ``smart_occupancy/engine/subscribe`` - opens a bridge session; streams
  ``hello``, ``result``, ``input``, ``spec``, ``ownership`` and ``closed``;
* ``smart_occupancy/engine/claim`` - a reviewed handover to the App;
* ``smart_occupancy/engine/publish`` / ``heartbeat`` - the owner's results and
  lease renewal (fenced: session, epoch, lease token, sequence, revision);
* ``smart_occupancy/engine/release`` - return control to the integration.

The service ``smart_occupancy.engine_return_to_integration`` (administrators
only) does the same as ``release`` without the App, for a rollback when the App
cannot be reached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_register_admin_service

from .const import DOMAIN
from .engine_bridge import BridgeError
from .rooms_api import RoomsApiError, _loaded_entry

if TYPE_CHECKING:
    from .occupancy_manager import OccupancyManager

SERVICE_RETURN = "engine_return_to_integration"
_ID = vol.All(str, vol.Length(min=32, max=32))
_VERSION = vol.All(str, vol.Length(min=1, max=32))
_REVISION = vol.All(str, vol.Length(min=1, max=64))
_COUNTER = vol.All(int, vol.Range(min=0, max=2**53))
MAX_RESULTS = 500


def _manager(hass: HomeAssistant) -> OccupancyManager:
    return _loaded_entry(hass).runtime_data.occupancy


def _guarded(handler):
    """Answer refusals with their stable code (nothing changed)."""

    @callback
    def run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        try:
            result = handler(hass, connection, msg)
        except (BridgeError, RoomsApiError) as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        if result is not None:
            connection.send_result(msg["id"], result)

    return run


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/engine/status"})
@websocket_api.require_admin
@callback
def ws_engine_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        manager = _manager(hass)
    except RoomsApiError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    connection.send_result(msg["id"], manager.bridge.status())


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/engine/subscribe",
    vol.Required("instance_id"): _ID,
    vol.Required("protocol"): vol.All(int, vol.Range(min=0, max=1000)),
    vol.Required("app_version"): _VERSION,
    vol.Required("core_version"): _VERSION,
    vol.Required("fingerprint"): vol.All(str, vol.Length(min=64, max=64)),
    vol.Optional("resume_token"): vol.Any(None, _ID),
})
@websocket_api.require_admin
@callback
def ws_engine_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Open a bridge session. The first event is ``hello`` (the complete current picture)."""
    try:
        manager = _manager(hass)
    except RoomsApiError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    msg_id = msg["id"]

    @callback
    def send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    bridge = manager.bridge
    try:
        session, hello = bridge.open_session(connection, send, msg)
    except BridgeError as err:
        connection.send_error(msg_id, err.code, str(err))
        return

    @callback
    def unsubscribe() -> None:
        bridge.close_session(session.session_id)

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send(hello)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/engine/claim",
    vol.Required("session_id"): _ID,
    vol.Required("expected_epoch"): _COUNTER,
    vol.Required("config_revision"): _REVISION,
    vol.Required("readiness"): dict,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@_guarded
def ws_engine_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _manager(hass).bridge.claim(connection, msg, connection.user.id if connection.user else None)


_OWNER_MSG = {
    vol.Required("session_id"): _ID,
    vol.Required("epoch"): _COUNTER,
    vol.Required("lease_token"): _ID,
    vol.Required("seq"): vol.All(int, vol.Range(min=1, max=2**53)),
    vol.Required("config_revision"): _REVISION,
}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/engine/publish",
    **_OWNER_MSG,
    vol.Required("results"): vol.All(dict, vol.Length(max=MAX_RESULTS)),
})
@websocket_api.require_admin
@_guarded
def ws_engine_publish(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _manager(hass).bridge.publish(connection, msg)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/engine/heartbeat", **_OWNER_MSG})
@websocket_api.require_admin
@_guarded
def ws_engine_heartbeat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _manager(hass).bridge.heartbeat(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/engine/release",
    vol.Optional("expected_epoch"): vol.Any(None, _COUNTER),
    vol.Optional("reason"): vol.Any(None, vol.All(str, vol.Length(max=200))),
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@_guarded
def ws_engine_release(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """Return control to the integration's engine (works from any administrator connection)."""
    bridge = _manager(hass).bridge
    by = msg.get("requested_by") or (connection.user.id if connection.user else None)
    released = bridge.release(by=by, detail=msg.get("reason") or "returned from the App",
                              expected_epoch=msg.get("expected_epoch"))
    return {"released": released, "ownership": bridge.status()}


ENGINE_HANDLERS = (ws_engine_status, ws_engine_subscribe, ws_engine_claim, ws_engine_publish, ws_engine_heartbeat,
                   ws_engine_release)


@callback
def async_register_engine_service(hass: HomeAssistant) -> None:
    """``smart_occupancy.engine_return_to_integration``: rollback without the App (administrators only)."""

    async def handle(call: ServiceCall) -> None:
        try:
            manager = _manager(hass)
        except RoomsApiError as err:
            raise HomeAssistantError(str(err)) from err
        manager.bridge.release(by=call.context.user_id, detail="engine_return_to_integration service")

    async_register_admin_service(hass, DOMAIN, SERVICE_RETURN, handle, vol.Schema({}))
