"""The light inspector (App 0.9.0, Phase A6.2): what controls this light, and Identify.

``smart_occupancy/lighting/light/inspect`` answers, for one light, relay output
or group: the device (registry, capabilities, groups), what is controlling it
right now (the room's mode, mood, occupancy, active zones, ambient category,
the winning rule of the same plan every executor uses, the rules it won over,
manual override, Priority Inputs, Room Supervisor, competing automations,
pending brightness delays, an identification in progress) and a bounded recent
history (the room's existing decision log and Room Supervisor log, filtered to
this light; the delay and Identify events). It reads only: nothing is
recorded, planned with side effects or sent.

It keeps three things apart that are not necessarily the same: the **winning
rule** now (what the plan wants), the **last command** Smart Occupancy sent,
and the **reported state** of the device. When the last change was not made by
Smart Occupancy and Home Assistant did not say by whom, it says so instead of
guessing.

``smart_occupancy/lighting/light/identify`` (administrators) runs Identify
(``light_identify.py``) and answers with its outcome.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .decisions import MODULE_LIGHTING
from .discovery import area_name
from .fixtures import onoff_only
from .light_identify import IDENTIFY_SECONDS, IdentifyError
from .occupancy import friendly_name
from .supervisor import log_subject
from .targets import capabilities as target_capabilities, is_relay
from .zone_websocket import _controller

HISTORY = 25
_SOURCE_WORDS = {
    "ui": "a person, from Home Assistant", "wall_switch": "a wall switch or remote",
    "automation": "a Home Assistant automation or script",
    "unattributed": "unknown: Home Assistant did not say who changed it (no user, no automation)",
}
_COLOUR = ("color_mode", "color_temp_kelvin", "hs_color", "rgb_color", "rgbw_color", "rgbww_color", "xy_color",
           "effect")


def _rooms_of(ctrl, entity_id: str) -> list[str]:
    """Rooms whose lighting manages this entity (as a target, or as a member of a managed group)."""
    out = []
    for area_id in ctrl.config.enabled_rooms:
        managed = ctrl.managed_room(area_id)
        if entity_id in managed.targets or any(entity_id in m for m in managed.members.values()):
            out.append(area_id)
    return out


def _device(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    state = hass.states.get(entity_id)
    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    entry = ent_reg.async_get(entity_id)
    device = dev_reg.async_get(entry.device_id) if entry and entry.device_id else None
    area = (entry.area_id if entry else None) or (device.area_id if device else None)
    attrs = dict(state.attributes) if state else {}
    caps = target_capabilities(hass, entity_id)
    brightness = attrs.get("brightness")
    return {
        "entity_id": entity_id, "name": friendly_name(hass, entity_id) or entity_id,
        "area_id": area, "area": area_name(hass, area) if area else None,
        "device": ({"name": device.name_by_user or device.name, "manufacturer": device.manufacturer,
                    "model": device.model} if device else None),
        "integration": entry.platform if entry else None,
        "available": state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN),
        "state": state.state if state else None,
        "last_changed": state.last_changed.isoformat(timespec="seconds") if state else None,
        "last_updated": state.last_updated.isoformat(timespec="seconds") if state else None,
        "brightness_pct": round(brightness / 255 * 100) if isinstance(brightness, (int, float)) else None,
        "colour": {k: attrs.get(k) for k in _COLOUR if attrs.get(k) is not None},
        "supported_color_modes": list(attrs.get("supported_color_modes") or []),
        "capabilities": caps.as_dict(),
        "relay": is_relay(entity_id),
        "onoff_only": onoff_only(caps),
        "group_members": [m for m in attrs.get("entity_id") or [] if isinstance(m, str)]
        if entity_id.startswith("light.") else [],
    }


def _mentions(entry: dict[str, Any], entity_id: str, name: str) -> bool:
    """Whether a decision record is about this light (its id, or its name as the records write it)."""
    if entry.get("target") in (entity_id, name):
        return True
    details = entry.get("details") or {}
    if details.get("entity_id") == entity_id or entity_id in (details.get("errors") or {}):
        return True
    for key in ("targets", "service", "brightness_rules", "manual_overrides"):
        for line in details.get(key) or []:
            if isinstance(line, str) and (entity_id in line.split() or line.startswith(f"{name}:")):
                return True
    target = entry.get("target") or ""
    return isinstance(target, str) and name in [t.strip() for t in target.replace(" and ", ",").split(",")]


def _history(ctrl, area_id: str, entity_id: str, name: str) -> list[dict[str, Any]]:
    out = []
    for subject in [area_id, log_subject(area_id), *(s for s in ctrl.decisions._logs  # noqa: SLF001
                                                      if s.startswith(f"{area_id}/"))]:
        for entry in ctrl.decisions.entries(subject):
            if _mentions(entry, entity_id, name):
                details = entry.get("details") or {}
                out.append({"at": entry["at"], "count": entry.get("count", 1), "module": entry["module"],
                            "subject": subject, "trigger": entry.get("trigger"), "kind": entry.get("kind"),
                            "decision": entry.get("action"), "explanation": entry.get("explanation"),
                            "status": entry.get("status"), "mode": entry.get("mode"),
                            "service": [s for s in details.get("service") or [] if entity_id in str(s).split()],
                            "result": details.get("result"), "errors": (details.get("errors") or {}).get(entity_id),
                            "attribution": ("switch" if "/" in subject else "supervisor" if subject.endswith(
                                "#supervisor") else "identify" if entry.get("kind") == "identify" else "manual"
                                if entry.get("kind") == "override" else "automatic")})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out[:HISTORY]


def inspect(hass: HomeAssistant, ctrl, entity_id: str, area_id: str | None = None) -> dict[str, Any]:
    """Everything the inspector shows for one light (see the module docstring). Reads only."""
    rooms = _rooms_of(ctrl, entity_id)
    area_id = area_id if area_id in rooms else (ctrl.light_owner(entity_id) if not rooms else (
        ctrl.light_owner(entity_id, rooms[0]) or rooms[0]))
    device = _device(hass, entity_id)
    out: dict[str, Any] = {"entity_id": entity_id, "device": device, "rooms": [
        {"area_id": a, "name": area_name(hass, a) or a} for a in rooms], "area_id": area_id, "configured": bool(rooms)}
    out["identify"] = ctrl.identify.status(entity_id) | {"seconds": IDENTIFY_SECONDS}
    if not area_id or area_id not in ctrl.config.enabled_rooms:
        out["control"] = None
        out["identify"]["available"] = False
        out["identify"]["why"] = "Only lights Smart Occupancy controls in a room can be identified."
        return out
    managed = ctrl.managed_room(area_id)
    groups = sorted(g for g, members in managed.members.items() if entity_id in members)
    target = entity_id if entity_id in managed.targets else managed.covered_by.get(entity_id) or (
        groups[0] if groups else entity_id)
    device["member_of"] = groups
    device["covered_by"] = managed.covered_by.get(entity_id)
    device["excluded"] = managed.excluded.get(entity_id)
    device["is_group"] = entity_id in managed.members or entity_id in managed.opaque_groups
    device["members"] = sorted(managed.members.get(entity_id, ())) or device["group_members"]
    fixture = ctrl.lighting.room(area_id).fixtures.get(entity_id)
    device["fixture"] = ({**fixture.as_dict(), "text": fixture.describe()} if fixture else None)
    rt = ctrl.runtime.get(area_id)
    status = ctrl.mood_status()
    mood, mood_source = ctrl.room_mood(area_id, status)
    result = ctrl.coordinator.occupancy.result(area_id)
    plan = ctrl.effective_plan(area_id, "preview")
    item = next((i for i in plan.items if i.entity_id == target), None)
    rule = (item.rule or {}) if item else {}
    ambient = ctrl.ambient.status(area_id)
    zones = [{"id": z.zone.id, "name": z.zone.name, "priority": z.zone.priority} for z in ctrl.zones.active(area_id)]
    override = rt.overrides.get(target) if rt else None
    if override and override.get("until"):
        until = dt_util.parse_datetime(override["until"])
        override = {**override, "remaining": max(0, round((until - dt_util.utcnow()).total_seconds()))
                    if until else None}
    sup = next((t for t in ctrl.supervisor.model(area_id, details=True)["targets"] if t["entity_id"] == target),
               None)
    priority = [{**p, "claims": True} for p in ctrl.priority.status(area_id)
                if (pi := ctrl.lighting.room(area_id).priority_input(p["id"])) is not None
                and (target in pi.targets or entity_id in pi.targets)]
    claim = ctrl.priority.claims(area_id).get(target)
    last_cmd = rt.last_commands.get(target) if rt else None
    external = rt.last_external.get(target) if rt else None
    if external:
        external = {**external, "who": _SOURCE_WORDS.get(external.get("source") or "", external.get("source"))}
    diag = ctrl.room_diagnostics(area_id)
    authority = {"fenced": ctrl.fenced, "dormant": ctrl.dormant, "controller": "Smart Lighting App",
                 "text": "Lighting control is changing owner: no command is sent now" if ctrl.fenced else
                 "The App's lighting controller commands this light through the companion's fenced executor"}
    out["control"] = {
        "area_id": area_id, "room": area_name(hass, area_id) or area_id, "target": target,
        "authority": authority,
        "mode": "automatic" if ctrl.room_active(area_id) else "observing",
        "mode_reason": diag.get("control_mode_reason"), "control_mode": diag.get("control_mode"),
        "manual_mood": ctrl.manual_mode,
        "mood": mood, "mood_source": mood_source,
        "occupancy": {"state": result.state.value if result else None,
                      "occupied": result.occupied if result else None,
                      "reason": result.reason if result else None},
        "zones": zones,
        "ambient": {"level": ambient.key, "name": ambient.name, "label": ambient.label(),
                    "enabled": ctrl.lighting.room(area_id).ambient.enabled},
        # The three things that need not agree: the winning rule, the last command sent and the reported state.
        "winning": {"rule": rule.get("name"), "kind": rule.get("kind"), "zone": rule.get("zone"),
                    "decision": item.decision if item else None, "reason": item.reason if item else None,
                    "reason_code": item.reason_code if item else None,
                    "requested": item.requested if item else None, "service": item.service if item else None,
                    "data": item.data if item else None, "brightness": rule.get("brightness"),
                    "superseded": rule.get("superseded") or [], "delay": rule.get("delay")} if item else None,
        "last_command": ({"service": last_cmd[0], "data": last_cmd[1]} if last_cmd else None),
        "supervisor_command": (sup or {}).get("last_command"),
        "reported": {"state": device["state"], "brightness_pct": device["brightness_pct"],
                     "colour": device["colour"], "available": device["available"]},
        "last_external": external,
        "override": override,
        "priority": {"inputs": priority, "claim": claim.as_dict() if claim else None,
                     "locked": ctrl.lighting.priority_lock},
        "supervisor": {"mode": (m.value if (m := ctrl.supervisor.settings(area_id).mode) else None), **({
            "result": sup.get("result"), "reason": sup.get("reason"), "desired": sup.get("desired"),
            "actual": sup.get("actual"), "attempts": sup.get("attempts"),
            "last_verification": sup.get("last_verification"), "last_correction": sup.get("last_correction"),
        } if sup else {})},
        "conflicts": [c.as_dict() | {"acknowledged": c.automation in set(ctrl.lighting.room(area_id).acknowledged)}
                      for c in ctrl.conflicts(area_id).get(target, [])],
        "delay": ctrl.delays.status(area_id, target),
        "matrix": bool(ctrl.lighting.room(area_id).matrix(target)),
    }
    out["history"] = _history(ctrl, area_id, target, friendly_name(hass, target) or target)
    try:
        ctrl.identify.check(area_id, entity_id, whole_group=True)
        out["identify"]["available"], out["identify"]["why"] = True, None
    except IdentifyError as err:
        out["identify"]["available"] = err.code == "group" or False
        out["identify"]["why"] = str(err) if err.code != "group" else None
    out["identify"]["group"] = device["is_group"] or bool(device["group_members"])
    return out


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/light/inspect",
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("area_id"): vol.Any(None, str),
})
@callback
def ws_light_inspect(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """What controls this light right now, and its recent history. Never sends anything."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    out = inspect(hass, ctrl, msg["entity_id"], msg.get("area_id"))
    out["can_identify"] = bool(connection.user and connection.user.is_admin)
    connection.send_result(msg["id"], out)


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/light/identify",
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("area_id"): vol.Any(None, str),
    # A group is identified only when this is explicitly chosen (otherwise: pick one of its lights).
    vol.Optional("whole_group"): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_light_identify(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Identify a light: full brightness for about 2.5 s, then its previous state (unless superseded)."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    entity_id = msg["entity_id"]
    rooms = _rooms_of(ctrl, entity_id)
    area_id = msg.get("area_id") if msg.get("area_id") in rooms else (rooms[0] if rooms else None)
    if area_id is None:
        connection.send_error(msg["id"], "not_configured",
                              f"{entity_id} is not a light Smart Occupancy controls: it cannot be identified")
        return
    try:
        outcome = await ctrl.async_identify(area_id, entity_id, whole_group=bool(msg.get("whole_group")),
                                            user_id=connection.user.id if connection.user else None)
    except IdentifyError as err:
        connection.send_error(msg["id"], err.code, str(err) + (
            f" ({', '.join(err.details['members'])})" if err.details.get("members") else ""))
        return
    connection.send_result(msg["id"], outcome | {"module": MODULE_LIGHTING})


INSPECTOR_HANDLERS = (ws_light_inspect, ws_light_identify)
