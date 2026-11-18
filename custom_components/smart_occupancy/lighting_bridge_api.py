"""Websocket commands of the lighting bridge (Phase A4), the rollback service and the read-only guard.

Every command needs an administrator. The App's backend sends them only through
its own allowlist. ``subscribe`` opens a bridge session; ``claim`` is the reviewed
handover; ``execute``, ``heartbeat``, ``publish``, ``inputs``, ``options`` and
``release`` are accepted only from the session that controls lighting (see
``lighting_bridge.py``); ``return`` and the service
``smart_occupancy.lighting_return_to_integration`` take control back from any
administrator, also without the App.

While the App controls lighting, the integration's lighting editors (the cards'
commands and the options flow's lighting steps) refuse changes with
``managed_by_app``; reading and previews keep working.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_register_admin_service

from . import lighting_sync as S
from .const import DOMAIN
from .lighting_bridge import MANAGED_BY_APP, MANAGED_TEXT, READ_ONLY_COMMANDS, BridgeError, LightingBridge
from .models import IntegrationConfig

SERVICE_RETURN = "lighting_return_to_integration"
_ID = vol.All(str, vol.Length(min=32, max=32))
_VERSION = vol.All(str, vol.Length(min=1, max=32))
_HASH = vol.All(str, vol.Length(min=64, max=64))
_REVISION = vol.All(str, vol.Length(min=1, max=64))
_COUNTER = vol.All(int, vol.Range(min=0, max=2**53))
_TEXT = vol.Any(None, vol.All(str, vol.Length(max=200)))


def bridge_of(hass: HomeAssistant) -> LightingBridge | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return getattr(entries[0].runtime_data, "lighting_bridge", None) if entries else None


def _bridge(hass: HomeAssistant) -> LightingBridge:
    if (bridge := bridge_of(hass)) is None:
        raise BridgeError(websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
    return bridge


def _user(connection: Any) -> str | None:
    return connection.user.id if connection.user else None


def _answer(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Answer with the handler's result, or a refusal with its stable code (nothing changed)."""

    @functools.wraps(handler)
    @callback
    def run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        try:
            result = handler(hass, connection, msg)
        except BridgeError as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        connection.send_result(msg["id"], result)

    return run


def _answer_async(handler: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(handler)
    async def run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        try:
            result = await handler(hass, connection, msg)
        except BridgeError as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        connection.send_result(msg["id"], result)

    return run


_OWNER = {
    vol.Required("session_id"): _ID,
    vol.Required("epoch"): _COUNTER,
    vol.Required("lease_token"): _ID,
    vol.Required("seq"): vol.All(int, vol.Range(min=1, max=2**53)),
}
_PUBLISH = {
    vol.Optional("config"): vol.Any(None, dict),
    vol.Optional("config_revision"): vol.Any(None, _REVISION),
    vol.Optional("state"): vol.Any(None, dict),
    vol.Optional("rooms"): vol.Any(None, dict),
    vol.Optional("manual_mode"): vol.Any(None, bool),
}


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/lighting/bridge/status"})
@websocket_api.require_admin
@_answer
def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _bridge(hass).status()


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/subscribe",
    vol.Required("instance_id"): _ID,
    vol.Required("protocol"): vol.All(int, vol.Range(min=0, max=1000)),
    vol.Required("app_version"): _VERSION,
    vol.Required("runtime_version"): _VERSION,
    vol.Required("fingerprint"): _HASH,
    vol.Required("core_fingerprint"): _HASH,
    vol.Optional("resume_token"): vol.Any(None, _ID),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Open a bridge session. The first event is ``hello`` (configuration, options, state, ownership)."""
    msg_id = msg["id"]
    try:
        bridge = _bridge(hass)
    except BridgeError as err:
        connection.send_error(msg_id, err.code, str(err))
        return

    @callback
    def send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    closed = False

    @callback
    def closed_early() -> None:
        nonlocal closed
        closed = True

    # Opening may wait (a fallback): a connection closed meanwhile must not leave its session behind.
    connection.subscriptions[msg_id] = closed_early
    try:
        session, hello = await bridge.async_open_session(connection, send, msg)
    except BridgeError as err:
        connection.subscriptions.pop(msg_id, None)
        if not closed:
            connection.send_error(msg_id, err.code, str(err))
        return
    if closed:
        bridge.close_session(session.session_id)
        return

    @callback
    def unsubscribe() -> None:
        bridge.close_session(session.session_id)

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send(hello)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/claim",
    vol.Required("session_id"): _ID,
    vol.Required("expected_epoch"): _COUNTER,
    vol.Required("config_revision"): _REVISION,
    vol.Required("readiness"): dict,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return await _bridge(hass).async_claim(connection, msg, _user(connection))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/execute",
    **_OWNER,
    vol.Required("domain"): vol.All(str, vol.Length(min=1, max=32)),
    vol.Required("service"): vol.All(str, vol.Length(min=1, max=32)),
    vol.Required("data"): dict,
    vol.Required("context_id"): vol.All(str, vol.Length(min=26, max=26)),
    vol.Optional("purpose"): vol.Any(None, vol.All(str, vol.Length(max=32))),
    vol.Optional("area_id"): vol.Any(None, vol.All(str, vol.Length(max=128))),
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_execute(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return await _bridge(hass).async_execute(connection, msg)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/lighting/bridge/heartbeat", **_OWNER})
@websocket_api.require_admin
@_answer
def ws_heartbeat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _bridge(hass).heartbeat(connection, msg)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/lighting/bridge/publish", **_OWNER,
                                  **_PUBLISH})
@websocket_api.require_admin
@_answer
def ws_publish(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _bridge(hass).publish(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/inputs",
    **_OWNER,
    vol.Required("kind"): vol.In(["preliminary", "hold", "activity"]),
    vol.Optional("signals"): dict,
    vol.Optional("area_id"): str,
    vol.Optional("zones"): [str],
    vol.Optional("source"): str,
})
@websocket_api.require_admin
@_answer
def ws_inputs(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _bridge(hass).inputs(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/options",
    **_OWNER,
    vol.Required("options_revision"): _REVISION,
    vol.Optional("mood"): dict,
    vol.Optional("categories"): list,
})
@websocket_api.require_admin
@_answer
def ws_options(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """The App's lighting commands that change options (light types, mood source). The integration reloads."""
    bridge = _bridge(hass)
    entry = bridge.coordinator.config_entry
    current = dict(entry.options)
    if msg["options_revision"] != S.config_revision(current):
        raise BridgeError(S.REJECT_CONFIG, "the options changed meanwhile; reload and try again")
    changes = {k: msg[k] for k in ("mood", "categories") if k in msg}
    if not changes:
        raise BridgeError(S.REJECT_INVALID, "nothing to change")
    try:
        IntegrationConfig.from_dict({**current, **changes})
    except (TypeError, ValueError, KeyError) as err:
        raise BridgeError(S.REJECT_INVALID, f"invalid options: {err}") from err
    bridge._accept(connection, msg, "options")  # noqa: SLF001
    changed = hass.config_entries.async_update_entry(entry, options={**current, **changes})
    return {"changed": changed}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/release",
    **_OWNER,
    **_PUBLISH,
    vol.Optional("detail"): _TEXT,
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_release(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """The owner returns control, with its final configuration and state."""
    return await _bridge(hass).async_release(connection, msg, _user(connection))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/bridge/return",
    vol.Optional("expected_epoch"): vol.Any(None, _COUNTER),
    vol.Optional("reason"): _TEXT,
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_return(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """Any administrator: the integration takes lighting back (from the mirror of the App's last state)."""
    bridge = _bridge(hass)
    if msg.get("expected_epoch") is not None and msg["expected_epoch"] != bridge.epoch:
        raise BridgeError(S.REJECT_BUSY, f"the ownership changed (epoch {bridge.epoch})")
    returned = await bridge.async_fallback(S.REASON_SERVICE, by=_user(connection),
                                           detail=msg.get("reason") or "returned from the App")
    return {"returned": returned, "ownership": bridge.status()}


LIGHTING_BRIDGE_HANDLERS = (ws_status, ws_subscribe, ws_claim, ws_execute, ws_heartbeat, ws_publish, ws_inputs,
                            ws_options, ws_release, ws_return)


def managed_guard(handler: Callable[..., Any]) -> Callable[..., Any]:
    """A lighting editor command: refused while the App controls lighting (or a handover is in progress)."""
    command = getattr(handler, "_ws_command", None)
    if command in READ_ONLY_COMMANDS:
        return handler

    @functools.wraps(handler)
    def guarded(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> Any:
        bridge = bridge_of(hass)
        if bridge is not None and (bridge.app_owns or bridge.busy):
            connection.send_error(msg["id"], MANAGED_BY_APP, MANAGED_TEXT)
            return None
        return handler(hass, connection, msg)

    return guarded


def async_register_lighting_service(hass: HomeAssistant) -> None:
    """``smart_occupancy.lighting_return_to_integration``: rollback without the App (administrators only)."""

    async def handle(call: ServiceCall) -> None:
        bridge = bridge_of(hass)
        if bridge is None:
            raise HomeAssistantError("Smart Occupancy & Lighting is not loaded")
        await bridge.async_fallback(S.REASON_SERVICE, by=call.context.user_id,
                                    detail="lighting_return_to_integration service")

    async_register_admin_service(hass, DOMAIN, SERVICE_RETURN, handle, vol.Schema({}))
