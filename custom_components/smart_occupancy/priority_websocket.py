"""Websocket API of fixture types and Priority Inputs (0.6.8, Phase 4.8).

Reads are part of the room editor model (``priority_model``, key
``priority`` of ``smart_occupancy/lighting/editor``; fixture data is also in
each light of ``brightness``). Writes are admin only and go to the lighting
store (no reload). Nothing here sends a light command:

* ``fixture/set`` / ``fixture/remove`` classify a managed lighting target (a
  Dumb Light is commanded on/off only). ``register_relay`` explicitly makes a
  switch a lighting output of the room (switches are never assumed to be
  lights);
* ``priority/set`` adds or changes an input. A new input is disabled;
  ``enabled: true`` needs ``confirm`` after ``priority/preview`` (what it
  would do now), and records who armed it and when;
* ``priority/lock`` is the global safety lock (no Priority Input sends
  anything while it is on).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er, label_registry as lr
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .controller import LightingController
from .fixtures import KIND_NAMES, MAX_FIXTURES, Fixture, FixtureKind, onoff_only
from .occupancy import friendly_name
from .priority import (
    DELAY_MAX,
    GRACE_MAX,
    INPUT_DOMAINS,
    KELVIN_MAX,
    KELVIN_MIN,
    MAX_INPUTS,
    MAX_TARGETS,
    Appearance,
    PriorityInput,
    ReleaseManual,
    Unavailable,
)
from .targets import is_relay
from .zone_websocket import _controller, _room, relay_candidates
from .zones import slug


def input_candidates(hass: HomeAssistant, area_id: str, chosen: list[str]) -> list[dict[str, Any]]:
    """Entities that can be a Priority Input: contacts and other binary sensors, input_booleans, switches.

    Switches are listed only for explicit selection; a switch that is a lighting output is never an input.
    """
    from homeassistant.helpers import device_registry as dr

    ent_reg, dev_reg = er.async_get(hass), dr.async_get(hass)
    out = []
    for entry in ent_reg.entities.values():
        if entry.domain not in INPUT_DOMAINS or entry.disabled_by is not None or entry.platform == DOMAIN:
            continue
        device = dev_reg.async_get(entry.device_id) if entry.device_id else None
        area = entry.area_id or (device.area_id if device else None)
        state = hass.states.get(entry.entity_id)
        dc = entry.device_class or entry.original_device_class
        out.append({"entity_id": entry.entity_id, "name": friendly_name(hass, entry.entity_id) or entry.entity_id,
                    "domain": entry.domain, "device_class": dc, "in_area": area == area_id,
                    "state": state.state if state else None, "chosen": entry.entity_id in chosen,
                    "contact": dc in ("door", "window", "opening", "garage_door")})
    for entity_id in hass.states.async_entity_ids("input_boolean"):
        if all(c["entity_id"] != entity_id for c in out):
            state = hass.states.get(entity_id)
            out.append({"entity_id": entity_id, "name": friendly_name(hass, entity_id) or entity_id,
                        "domain": "input_boolean", "device_class": None, "in_area": False,
                        "state": state.state if state else None, "chosen": entity_id in chosen, "contact": False})
    out.sort(key=lambda c: (not c["chosen"], not c["in_area"], not c["contact"], c["domain"] == "switch",
                            c["name"].casefold()))
    return out[:200]


def priority_model(hass: HomeAssistant, ctrl: LightingController, area_id: str) -> dict[str, Any]:
    """Everything Settings → Lighting → Fixture Types and Priority Inputs show."""
    room = ctrl.lighting.room(area_id)
    managed = ctrl.managed_room(area_id)
    targets = {t.entity_id: t for t in ctrl._targets(area_id)}
    status = {s["id"]: s for s in ctrl.priority.status(area_id)}
    claims = ctrl.priority.claims(area_id)
    plan = ctrl.effective_plan(area_id, "preview")
    items = {i.entity_id: i for i in plan.items}
    lights = []
    for entity_id in managed.controlled:
        t = targets.get(entity_id)
        fixture = room.fixtures.get(entity_id)
        raw = ctrl.hass.states.get(entity_id)
        item = items.get(entity_id)
        lights.append({
            "entity_id": entity_id, "name": (t.name if t else None) or friendly_name(hass, entity_id) or entity_id,
            "type": "relay" if is_relay(entity_id) else "light",
            "fixture": fixture.as_dict() | {"text": fixture.describe()} if fixture else None,
            "capabilities": t.capabilities.as_dict() if t else {},
            "onoff": bool(t is not None and onoff_only(t.capabilities)),
            # What the entity itself reports (a Dumb Light classification narrows this; never widens it).
            "entity_capabilities": _caps(hass, entity_id),
            "state": raw.state if raw else None,
            "claim": claims[entity_id].as_dict() if entity_id in claims else None,
            "rule": (item.rule or {}).get("name") if item else None,
            "decision": item.decision if item else None, "reason": item.reason if item else None,
        })
    labels = sorted(({"label_id": lb.label_id, "name": lb.name} for lb in lr.async_get(hass).async_list_labels()),
                    key=lambda x: x["name"].casefold())
    return {
        "lights": lights,
        "inputs": [{**pi.as_dict(), "status": status.get(pi.id),
                    "entity_name": friendly_name(hass, pi.entity_id) if pi.entity_id else None,
                    "missing_targets": [e for e in pi.targets if e not in managed.controlled]}
                   for pi in room.priority_inputs],
        "candidates": input_candidates(hass, area_id, [p.entity_id for p in room.priority_inputs if p.entity_id]),
        "relay_candidates": relay_candidates(hass, area_id, room.relays),
        "labels": labels,
        "kinds": [{"id": k.value, "name": n} for k, n in KIND_NAMES.items()],
        "lock": ctrl.lighting.priority_lock,
        "settled": ctrl.priority.settled,
        "limits": {"max_inputs": MAX_INPUTS, "max_targets": MAX_TARGETS, "delay": [0, DELAY_MAX],
                   "grace": [0, GRACE_MAX], "kelvin": [KELVIN_MIN, KELVIN_MAX]},
    }


def _caps(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    from .targets import capabilities

    return capabilities(hass, entity_id).as_dict()


# ------------------------------------------------------------------ fixtures
@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/fixture/set",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): cv.entity_id,
    vol.Required("kind"): vol.In([k.value for k in FixtureKind]),
    vol.Optional("label"): vol.Any(None, vol.All(str, vol.Length(max=60))),
    vol.Optional("ha_label"): vol.Any(None, vol.All(str, vol.Length(max=100))),
    vol.Optional("name"): vol.Any(None, vol.All(str, vol.Length(max=60))),
    vol.Optional("register_relay"): bool,
})
@websocket_api.require_admin
@callback
def ws_fixture_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Classify a managed light or relay output (``register_relay``: explicitly add a switch as a lighting output)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    entity_id = msg["entity_id"]
    room = ctrl.lighting.room(area_id)
    relays = room.relays
    if msg.get("register_relay"):
        if not is_relay(entity_id):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{entity_id} is not a switch entity")
            return
        entry = er.async_get(hass).async_get(entity_id)
        if entry is not None and entry.platform == DOMAIN:
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"{entity_id} belongs to Smart Occupancy")
            return
        if any(p.entity_id == entity_id for p in room.priority_inputs):
            connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                                  f"{entity_id} is a Priority Input of this room; an input is not a lighting output")
            return
        relays = tuple(dict.fromkeys((*relays, entity_id)))
    elif entity_id not in ctrl.managed_room(area_id).targets and entity_id not in room.fixtures:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              f"{entity_id} is not a light or relay output this room manages")
        return
    if entity_id not in room.fixtures and len(room.fixtures) >= MAX_FIXTURES:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, f"At most {MAX_FIXTURES} fixtures")
        return
    if msg.get("kind") == FixtureKind.CUSTOM.value and not (msg.get("label") or "").strip():
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, "A custom classification needs a label")
        return
    fixture = Fixture.from_dict(entity_id, {"kind": msg["kind"], "label": msg.get("label"),
                                            "ha_label": msg.get("ha_label"), "name": msg.get("name")})
    fixtures = {**room.fixtures, entity_id: fixture}
    ctrl.store.async_set(ctrl.with_room(area_id, fixtures=fixtures, relays=relays))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "fixture": fixture.as_dict(), "sent": False})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/fixture/remove",
    vol.Required("area_id"): str,
    vol.Required("entity_id"): cv.entity_id,
})
@websocket_api.require_admin
@callback
def ws_fixture_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Remove a classification (the target keeps being managed as before; a relay stays a relay)."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    room = ctrl.lighting.room(area_id)
    if msg["entity_id"] not in room.fixtures:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"{msg['entity_id']} has no classification")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, fixtures={e: f for e, f in room.fixtures.items()
                                                           if e != msg["entity_id"]}))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


# ------------------------------------------------------------ priority inputs
_INPUT = vol.Schema({
    vol.Optional("id"): vol.All(str, vol.Length(min=1, max=60)),
    vol.Optional("name"): vol.All(str, vol.Strip, vol.Length(min=1, max=60)),
    vol.Optional("entity_id"): vol.Any(None, cv.entity_id),
    vol.Optional("enabled"): bool,
    vol.Optional("active_when"): vol.In(["on", "off"]),
    vol.Optional("targets"): vol.All([cv.entity_id], vol.Length(max=MAX_TARGETS)),
    vol.Optional("brightness_pct"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=1, max=100))),
    vol.Optional("appearance"): vol.In([a.value for a in Appearance]),
    vol.Optional("kelvin"): vol.Any(None, vol.All(vol.Coerce(int), vol.Range(min=KELVIN_MIN, max=KELVIN_MAX))),
    vol.Optional("debounce"): vol.All(vol.Coerce(float), vol.Range(min=0, max=DELAY_MAX)),
    vol.Optional("release_delay"): vol.All(vol.Coerce(float), vol.Range(min=0, max=DELAY_MAX)),
    vol.Optional("unavailable"): vol.In([u.value for u in Unavailable]),
    vol.Optional("grace"): vol.All(vol.Coerce(float), vol.Range(min=0, max=GRACE_MAX)),
    vol.Optional("override_manual"): bool,
    vol.Optional("release_manual"): vol.In([r.value for r in ReleaseManual]),
})


def _merged(ctrl: LightingController, area_id: str, data: dict[str, Any]) -> tuple[PriorityInput | None, str | None]:
    room = ctrl.lighting.room(area_id)
    input_id = data.pop("id", None)
    current = room.priority_input(input_id) if input_id else None
    if input_id and current is None:
        return None, f"Priority Input {input_id} does not exist"
    if current is None:
        if len(room.priority_inputs) >= MAX_INPUTS:
            return None, f"At most {MAX_INPUTS} Priority Inputs per room"
        name = data.get("name") or f"Priority Input {len(room.priority_inputs) + 1}"
        current = PriorityInput(id=slug(name, {p.id for p in room.priority_inputs}, "input"), name=name)
    merged = current.as_dict() | data
    pi = PriorityInput.from_dict(merged)
    return pi, None


def _input_error(hass: HomeAssistant, ctrl: LightingController, area_id: str, pi: PriorityInput) -> str | None:
    room = ctrl.lighting.room(area_id)
    if pi.entity_id is not None:
        if pi.entity_id.split(".", 1)[0] not in INPUT_DOMAINS:
            return f"{pi.entity_id} cannot be a Priority Input (a binary_sensor, input_boolean or switch)"
        if pi.entity_id in room.relays or pi.entity_id in ctrl.managed_room(area_id).targets:
            return f"{pi.entity_id} is a lighting output of this room: an output being controlled is not an input"
        entry = er.async_get(hass).async_get(pi.entity_id)
        if entry is not None and entry.platform == DOMAIN:
            return f"{pi.entity_id} belongs to Smart Occupancy"
    controlled = set(ctrl.managed_room(area_id).controlled)
    if bad := [e for e in pi.targets if e not in controlled]:
        return f"{', '.join(bad)} is not a light or relay output this room controls"
    return None


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/priority/preview",
    vol.Required("area_id"): str,
    vol.Required("input"): _INPUT,
})
@callback
def ws_priority_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """What an input (a draft, or a stored one) would do to each of its lights if it became active now."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    pi, error = _merged(ctrl, area_id, dict(msg["input"]))
    if pi is None or (error := error or _input_error(hass, ctrl, area_id, pi)):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, error or "Invalid input")
        return
    connection.send_result(msg["id"], ctrl.priority_preview(area_id, pi))


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/priority/set",
    vol.Required("area_id"): str,
    vol.Required("input"): _INPUT,
    vol.Optional("confirm"): bool,
})
@websocket_api.require_admin
@callback
def ws_priority_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Add (no ``id``) or change an input. Enabling one needs ``confirm`` (after its preview). Never sends a
    command itself: an enabled input acts when its entity becomes active."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    data = dict(msg["input"])
    room = ctrl.lighting.room(area_id)
    was = room.priority_input(data.get("id")) if data.get("id") else None
    pi, error = _merged(ctrl, area_id, data)
    if pi is None or (error := error or _input_error(hass, ctrl, area_id, pi)):
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT, error or "Invalid input")
        return
    if msg["input"].get("enabled") and not pi.enabled:
        connection.send_error(msg["id"], websocket_api.ERR_INVALID_FORMAT,
                              "An input needs its entity and at least one light before it can be enabled")
        return
    arming = pi.enabled and not (was and was.enabled)
    if arming and not msg.get("confirm"):
        connection.send_error(msg["id"], "preview_required",
                              "Enabling a Priority Input needs its preview first (priority/preview), then confirm")
        return
    if arming:
        pi = replace(pi, enabled_at=dt_util.utcnow().isoformat(timespec="seconds"),
                     enabled_by=connection.user.id if connection.user else None)
    inputs = tuple(pi if p.id == pi.id else p for p in room.priority_inputs) if room.priority_input(pi.id) \
        else (*room.priority_inputs, pi)
    ctrl.store.async_set(ctrl.with_room(area_id, priority_inputs=inputs))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "input": pi.as_dict(), "sent": False})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/priority/remove",
    vol.Required("area_id"): str,
    vol.Required("input_id"): str,
})
@websocket_api.require_admin
@callback
def ws_priority_remove(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Delete an input. If it is active, its claim ends: its lights follow the current rules."""
    if (ctrl := _controller(hass, connection, msg)) is None or (area_id := _room(ctrl, connection, msg)) is None:
        return
    room = ctrl.lighting.room(area_id)
    if room.priority_input(msg["input_id"]) is None:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, f"Priority Input {msg['input_id']} does not exist")
        return
    ctrl.store.async_set(ctrl.with_room(area_id, priority_inputs=tuple(
        p for p in room.priority_inputs if p.id != msg["input_id"])))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision})


@websocket_api.websocket_command({
    vol.Required("type"): "smart_occupancy/lighting/priority/lock",
    vol.Required("lock"): bool,
})
@websocket_api.require_admin
@callback
def ws_priority_lock(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The global safety lock: while it is on, no Priority Input claims a light or sends anything."""
    if (ctrl := _controller(hass, connection, msg)) is None:
        return
    ctrl.store.async_set(replace(ctrl.lighting, priority_lock=msg["lock"]))
    connection.send_result(msg["id"], {"revision": ctrl.store.revision, "lock": msg["lock"]})


PRIORITY_HANDLERS = (ws_fixture_set, ws_fixture_remove, ws_priority_preview, ws_priority_set, ws_priority_remove,
                     ws_priority_lock)
