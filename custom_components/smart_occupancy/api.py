"""Websocket commands and the service of the companion (Phase A6).

Every command that changes anything needs an administrator (``require_admin``);
the App sends them only through its own allowlist. ``view`` and ``view/subscribe``
are read-only and open to every authenticated user: the dashboard cards use them.
"""

from __future__ import annotations

from collections.abc import Callable
import functools
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.service import async_register_admin_service

from . import legacy, protocol as P
from .companion import Companion, CompanionError
from .const import DOMAIN, SIGNAL_OWNERSHIP, SIGNAL_ROOMS_CHANGED, SIGNAL_VIEW_UPDATED

SERVICE_REVOKE = "revoke_app_control"
_ID = vol.All(str, vol.Length(min=32, max=32))
_SHORT_ID = vol.All(str, vol.Length(min=8, max=32))
_VERSION = vol.All(str, vol.Length(min=1, max=32))
_REVISION = vol.All(str, vol.Length(min=1, max=64))
_COUNTER = vol.All(int, vol.Range(min=0, max=2**53))
_TEXT = vol.Any(None, vol.All(str, vol.Length(max=200)))
_DOMAIN = vol.In(list(P.DOMAINS))
_OWNER = {
    vol.Required("session_id"): _ID,
    vol.Required("domain"): _DOMAIN,
    vol.Required("epoch"): _COUNTER,
    vol.Required("lease_token"): _ID,
    vol.Required("seq"): vol.All(int, vol.Range(min=1, max=2**53)),
}


def companion_of(hass: HomeAssistant) -> Companion | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries and isinstance(entries[0].runtime_data, Companion) else None


def _companion(hass: HomeAssistant) -> Companion:
    if (companion := companion_of(hass)) is None:
        raise CompanionError(websocket_api.ERR_NOT_FOUND, "Smart Occupancy & Lighting is not loaded")
    return companion


def _user(connection: Any) -> str | None:
    return connection.user.id if connection.user else None


def _answer(handler: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(handler)
    @callback
    def run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        try:
            result = handler(hass, connection, msg)
        except (CompanionError, P.ProtocolError) as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        connection.send_result(msg["id"], result)

    return run


def _answer_async(handler: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(handler)
    async def run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
        try:
            result = await handler(hass, connection, msg)
        except (CompanionError, P.ProtocolError) as err:
            connection.send_error(msg["id"], err.code, str(err))
            return
        connection.send_result(msg["id"], result)

    return run


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/status"})
@websocket_api.require_admin
@_answer
def ws_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).status()


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/subscribe",
    vol.Required("instance_id"): _ID,
    vol.Required("protocol"): vol.All(int, vol.Range(min=0, max=1000)),
    vol.Required("app_version"): _VERSION,
    vol.Optional("pairing_token"): vol.Any(None, _ID),
})
@websocket_api.require_admin
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Open an App session. The first event is ``hello``; then ownership, renames, actions, recheck, closed."""
    msg_id = msg["id"]
    try:
        companion = _companion(hass)
    except CompanionError as err:
        connection.send_error(msg_id, err.code, str(err))
        return

    @callback
    def send(payload: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg_id, payload))

    try:
        session, hello = companion.open_session(connection, send, msg)
    except CompanionError as err:
        connection.send_error(msg_id, err.code, str(err))
        return

    @callback
    def unsubscribe() -> None:
        companion.close_session(session.session_id)

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)
    send(hello)
    return None


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/pair",
    vol.Required("session_id"): _ID,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@_answer
def ws_pair(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).pair(connection, msg, _user(connection))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/unpair"})
@websocket_api.require_admin
@_answer
def ws_unpair(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    companion = _companion(hass)
    companion.revoke(None, _user(connection), P.REASON_REVOKED)
    companion.unpair(_user(connection))
    return {"ownership": companion.ownership()}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/claim",
    vol.Required("session_id"): _ID,
    vol.Required("domain"): _DOMAIN,
    vol.Required("expected_epoch"): _COUNTER,
    vol.Optional("config_revision"): vol.Any(None, _REVISION),
    vol.Optional("targets"): vol.Any(None, dict),
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@_answer
def ws_claim(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).claim(connection, msg, _user(connection))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/heartbeat", **_OWNER})
@websocket_api.require_admin
@_answer
def ws_heartbeat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).heartbeat(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/publish",
    vol.Required("session_id"): _ID,
    vol.Required("domain"): vol.In([*P.DOMAINS, "system"]),
    vol.Optional("epoch"): _COUNTER,
    vol.Optional("lease_token"): _ID,
    vol.Optional("seq"): vol.All(int, vol.Range(min=1, max=2**53)),
    vol.Required("view"): dict,
    vol.Optional("targets"): vol.Any(None, dict),
    vol.Optional("config_revision"): vol.Any(None, _REVISION),
})
@websocket_api.require_admin
@_answer
def ws_publish(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    if msg["domain"] != "system" and not all(k in msg for k in ("epoch", "lease_token", "seq")):
        raise CompanionError(P.REJECT_INVALID, "a domain view needs epoch, lease_token and seq")
    return _companion(hass).publish(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/execute",
    **_OWNER,
    vol.Required("service_domain"): vol.All(str, vol.Length(min=1, max=32)),
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
    return await _companion(hass).async_execute(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/release", **_OWNER, vol.Optional("detail"): _TEXT,
})
@websocket_api.require_admin
@_answer
def ws_release(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).release(connection, msg, _user(connection))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/revoke",
    vol.Optional("domain"): vol.Any(None, _DOMAIN),
    vol.Optional("reason"): _TEXT,
})
@websocket_api.require_admin
@_answer
def ws_revoke(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    companion = _companion(hass)
    changed = companion.revoke(msg.get("domain"), _user(connection))
    return {"revoked": changed, "ownership": companion.ownership()}


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/action_result",
    vol.Required("session_id"): _ID,
    vol.Required("action_id"): vol.All(str, vol.Length(min=8, max=32)),
    vol.Required("ok"): bool,
    vol.Optional("result"): vol.Any(None, dict),
    vol.Optional("error"): vol.Any(None, dict),
})
@websocket_api.require_admin
@_answer
def ws_action_result(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).action_result(connection, msg)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/renames_applied", vol.Required("session_id"): _ID,
})
@websocket_api.require_admin
@_answer
def ws_renames_applied(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    return _companion(hass).renames_applied(connection, msg)


# ------------------------------------------------------------------ legacy
@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/legacy/export"})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_legacy_export(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    companion = _companion(hass)
    return await legacy.async_export(hass, companion.entry)


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/legacy/status"})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_legacy_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    companion = _companion(hass)
    export = await legacy.async_export(hass, companion.entry)
    return {
        "present": legacy._has_legacy_data(export),  # noqa: SLF001
        "options": export["entry"]["options"] is not None,
        "options_revision": export["options_revision"],
        "rooms": len((export["entry"]["options"] or {}).get("rooms") or {}),
        "stores": sorted(k for k, v in export["stores"].items() if isinstance(v, dict) and "data" in v),
        "legacy_owners": export["legacy_owners"],
        "retired": companion.retired,
        "backup": await legacy.async_backup_info(hass),
    }


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/legacy/retire",
    vol.Required("confirm"): bool,
    vol.Optional("expected_options_revision"): vol.Any(None, _REVISION),
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_legacy_retire(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    if msg["confirm"] is not True:
        raise CompanionError(P.REJECT_INVALID, "retiring the legacy data must be confirmed")
    companion = _companion(hass)
    options = legacy.legacy_options(companion.entry)
    if msg.get("expected_options_revision") and (options is None or P.revision(options) != msg["expected_options_revision"]):
        raise CompanionError(P.REJECT_BUSY, "the legacy options changed since they were read; inspect again")
    by = _user(connection) or msg.get("requested_by")
    result = await legacy.async_retire(hass, companion.entry, by)
    companion.retired = {"at": result["retired_at"], "by": by, "options_revision": result["options_revision"]}
    await companion.async_flush()
    return result


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/legacy/restore",
    vol.Required("confirm"): bool,
    vol.Optional("requested_by"): vol.Any(None, vol.All(str, vol.Length(max=64))),
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_legacy_restore(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    if msg["confirm"] is not True:
        raise CompanionError(P.REJECT_INVALID, "restoring the legacy data must be confirmed")
    companion = _companion(hass)
    by = _user(connection) or msg.get("requested_by")
    result = await legacy.async_restore(hass, companion.entry, by)
    companion.retired = None
    await companion.async_flush()
    return result


# -------------------------------------------------------------------- cards
def _card_view(companion: Companion) -> dict[str, Any]:
    now = None
    return {
        "owners": {d: companion.state(d, now) for d in P.DOMAINS},
        "app_connected": any(companion.fresh(d) for d in P.DOMAINS),
        "rooms": companion.rooms,
        "views_at": dict(companion.views_at),
        "occupancy": (companion.view("occupancy") or {}).get("rooms") or {},
        "lighting": companion.view("lighting") or {"rooms": {}, "manual_mode": False, "summary": {}},
        "curtains": companion.view("curtains") or {"windows": {}, "summary": {}},
        "forecast": companion.view("forecast") or {"rooms": {}},
        "system": companion.view("system") or {},
    }


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/view"})
@_answer
def ws_view(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """The published views (read-only; every authenticated user): the data of the dashboard cards."""
    return _card_view(_companion(hass))


@websocket_api.websocket_command({vol.Required("type"): "smart_occupancy/companion/view/subscribe"})
@callback
def ws_view_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Pushes ``{"changed": <domain>}`` whenever a view or an owner changes (coalesced by the card)."""
    try:
        _companion(hass)
    except CompanionError as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    msg_id = msg["id"]

    @callback
    def changed(domain: Any = None) -> None:
        connection.send_message(websocket_api.event_message(msg_id, {"changed": domain}))

    @callback
    def rooms_changed() -> None:
        changed("rooms")

    unsubs = [async_dispatcher_connect(hass, SIGNAL_VIEW_UPDATED, changed),
              async_dispatcher_connect(hass, SIGNAL_OWNERSHIP, changed),
              async_dispatcher_connect(hass, SIGNAL_ROOMS_CHANGED, rooms_changed)]

    @callback
    def unsubscribe() -> None:
        for unsub in unsubs:
            unsub()

    connection.subscriptions[msg_id] = unsubscribe
    connection.send_result(msg_id)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/companion/action",
    vol.Required("kind"): vol.In(sorted(P.ACTIONS)),
    vol.Optional("params", default=dict): dict,
})
@websocket_api.require_admin
@websocket_api.async_response
@_answer_async
async def ws_action(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> dict:
    """A dashboard card's action, relayed to the App (which validates and performs it)."""
    return await _companion(hass).async_action(msg["kind"], msg["params"], _user(connection))


HANDLERS = (ws_status, ws_subscribe, ws_pair, ws_unpair, ws_claim, ws_heartbeat, ws_publish, ws_execute,
            ws_release, ws_revoke, ws_action_result, ws_renames_applied, ws_legacy_export, ws_legacy_status,
            ws_legacy_retire, ws_legacy_restore, ws_view, ws_view_subscribe, ws_action)

_REGISTERED = f"{DOMAIN}_companion_api"


def async_register_api(hass: HomeAssistant) -> None:
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True
    for handler in HANDLERS:
        websocket_api.async_register_command(hass, handler)

    async def handle_revoke(call: ServiceCall) -> None:
        companion = companion_of(hass)
        if companion is None:
            raise HomeAssistantError("Smart Occupancy & Lighting is not loaded")
        companion.revoke(call.data.get("domain"), call.context.user_id, P.REASON_REVOKED)

    async_register_admin_service(hass, DOMAIN, SERVICE_REVOKE, handle_revoke,
                                 vol.Schema({vol.Optional("domain"): vol.In(list(P.DOMAINS))}))
