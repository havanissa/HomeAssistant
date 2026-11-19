"""Presence → Zones (App 0.9.0, Phase A6.2): the Presence-owned API of room zones.

A zone is detected presence in a part of a room. Lighting consumes its
occupancy; it does not own it. Until a later release splits the storage, the
one authoritative zone record stays where it is (the lighting configuration,
``zones.ZoneConfig``, with its stable id) and the one zone engine stays the
lighting runtime's ``ZoneManager``: this module is the **compatibility
boundary** that gives Presence its own commands over the existing, validated
zone commands - no second record, no second engine, no data migration:

* ``smart_occupancy/zones/room`` (read): a room's zones as Presence shows them -
  detection, state, timing, holds, availability policies, history - with what
  depends on each zone (the lights that respond to it and their brightness
  matrix cells), and the room's shared space (read-only here).
* ``smart_occupancy/zones/set`` (administrators): create a zone or change its
  **detection** settings only (``DETECTION_KEYS``). A lighting key is refused:
  lighting rules are edited in Lighting → a room → Zone lighting.
* ``smart_occupancy/zones/remove`` (administrators): delete a zone. When lights
  respond to it or brightness cells use it, ``confirm`` is required (the answer
  of ``zones/room`` lists them).
* ``smart_occupancy/zones/radar_test`` (read): a test distance against a draft
  radar band (never sends anything).

Every call goes through the lighting command router (``LightingApi.call``), so
the zone handlers validate exactly as before, wherever lighting runs.
"""

from __future__ import annotations

from typing import Any

ROOM = "smart_occupancy/zones/room"
RADAR_TEST = "smart_occupancy/zones/radar_test"
SET = "smart_occupancy/zones/set"
REMOVE = "smart_occupancy/zones/remove"
READ_COMMANDS: frozenset[str] = frozenset({ROOM, RADAR_TEST})
WRITE_COMMANDS: frozenset[str] = frozenset({SET, REMOVE})
COMMANDS: frozenset[str] = READ_COMMANDS | WRITE_COMMANDS

#: What Presence owns of a zone: its identity and detection.
DETECTION_KEYS: frozenset[str] = frozenset({
    "id", "name", "enabled", "detection", "radar", "sensors", "activation", "timing", "unavailable", "holds_room"})
#: What Lighting owns of a zone: which lights respond and how (Lighting → a room → Zone lighting).
LIGHTING_KEYS: frozenset[str] = frozenset({"targets", "priority", "manual", "daylight", "on_below", "release"})


class ZonesError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}


def dependents(room: dict[str, Any], zone_id: str) -> dict[str, Any]:
    """What uses a zone: the lights that respond to it and the brightness cells of its column."""
    z = room.get("zones") or {}
    names = {t["entity_id"]: t.get("name") or t["entity_id"] for t in z.get("targets") or []}
    zone = next((x for x in z.get("zones") or [] if x.get("id") == zone_id), None)
    lights = [{"entity_id": t["entity_id"], "name": names.get(t["entity_id"], t["entity_id"]),
               "action": t.get("action")} for t in (zone or {}).get("targets") or []]
    cells = []
    for light in (room.get("brightness") or {}).get("lights") or []:
        column = ((light.get("matrix") or {}).get("zones") or {}).get(zone_id) or {}
        configured = sum(1 for v in column.values() if v is not None)
        delays = len(((light.get("matrix") or {}).get("delays") or {}).get(zone_id) or {})
        if configured or delays:
            cells.append({"entity_id": light["entity_id"], "name": light.get("name") or light["entity_id"],
                          "cells": configured, "delays": delays})
    return {"lights": lights, "brightness": cells}


def presence_model(editor: dict[str, Any]) -> dict[str, Any]:
    """The Presence view of a room's zones, from the lighting editor model of that room."""
    room = editor.get("room") or {}
    z = room.get("zones")
    if z is None:
        return {"area_id": room.get("area_id"), "name": room.get("name"), "available": False,
                "can_edit": bool(editor.get("can_edit")), "zones": []}
    zones = []
    for zone in z.get("zones") or []:
        detection = {k: zone.get(k) for k in DETECTION_KEYS if k in zone}
        uses = dependents(room, zone["id"])
        zones.append({**detection, "status": zone.get("status"), "priority": zone.get("priority"),
                      "lighting": {"lights": uses["lights"], "brightness": uses["brightness"],
                                   "priority": zone.get("priority")}})
    return {
        "area_id": room.get("area_id"), "name": room.get("name"), "available": True,
        "can_edit": bool(editor.get("can_edit")), "revision": editor.get("revision"),
        "zones": zones, "sensors": z.get("sensors") or [], "radar": z.get("radar") or {},
        "room_timing": z.get("room_timing") or {}, "limits": z.get("limits") or {},
        "history": z.get("history") or [], "space": z.get("space"),
        "occupancy": room.get("occupancy"),
        "holds": sorted(x["name"] for x in zones if (x.get("status") or {}).get("active") and x.get("holds_room")),
    }


async def call(api: Any, command: str, params: dict[str, Any], *, user_id: str | None, is_admin: bool) -> Any:
    """One Presence zone command, composed of the lighting runtime's own zone commands."""
    area_id = params.get("area_id")
    if not isinstance(area_id, str) or not area_id:
        raise ZonesError("invalid_format", "A room (area_id) is required")

    async def run(kind: str, **body: Any) -> Any:
        return await api.call({"type": f"smart_occupancy/lighting/{kind}", **body}, user_id=user_id,
                              is_admin=is_admin)

    if command == ROOM:
        return presence_model(await run("editor", area_id=area_id))
    if command == RADAR_TEST:
        return await run("zone/radar_test", area_id=area_id, **{k: v for k, v in params.items() if k != "area_id"})
    if command == SET:
        zone = params.get("zone")
        if not isinstance(zone, dict):
            raise ZonesError("invalid_format", "A zone is required")
        if bad := sorted(set(zone) - DETECTION_KEYS):
            raise ZonesError("not_presence", (f"{', '.join(bad)}: lighting settings of a zone are edited in "
                                              "Lighting → the room → Zone lighting"), 400)
        result = await run("zone/set", area_id=area_id, zone=zone)
        return {**(result or {}), "sent": False}
    if command == REMOVE:
        zone_id = params.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id:
            raise ZonesError("invalid_format", "A zone (zone_id) is required")
        room = (await run("editor", area_id=area_id)).get("room") or {}
        uses = dependents(room, zone_id)
        if (uses["lights"] or uses["brightness"]) and params.get("confirm") is not True:
            raise ZonesError("confirmation_required", "Lights respond to this zone: confirm to delete it", 409,
                             uses)
        result = await run("zone/remove", area_id=area_id, zone_id=zone_id)
        return {**(result or {}), "removed": zone_id, "dependents": uses}
    raise ZonesError("not_allowed", f"{command!r} is not a zone command", 403)
