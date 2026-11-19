"""App 0.9.0 (Phase A6.2): the Presence-owned zone commands, the light inspector and Identify through the App's
command router (``/api/lighting/command``), against the simulated integration bridge (tests/fake_lighting.py).

* Presence → Zones composes the lighting runtime's own zone commands: one zone record, one zone engine.
  It accepts detection settings only, needs a confirmation to delete a zone lights respond to, and is for
  administrators.
* The inspector and Identify exist only in the App's runtime: refused (not forwarded) while the 0.9.0
  integration still controls lighting.
"""

from __future__ import annotations

from typing import Any

from smart_lighting import zones_api

from .conftest import ADMIN_HEADERS, FakeHomeAssistant, wait_for
from .fake_lighting import AREA, LAMP
from .test_engine import PIR, RADAR
from .test_lighting_runtime import handover, lit_app, rt  # noqa: F401

GUEST = {"X-Remote-User-Id": "user-plain-2", "X-Smart-Lighting": "1"}


async def command(client: Any, headers: dict[str, str] | None = None, **msg: Any) -> tuple[int, Any]:
    resp = await client.post("/api/lighting/command", json=msg, headers=headers or ADMIN_HEADERS)
    return resp.status, await resp.json()


def test_presence_model_projection_and_dependents() -> None:
    editor = {"can_edit": True, "revision": 7, "room": {
        "area_id": "kamar", "name": "Kamar", "occupancy": {"occupied": True},
        "zones": {"zones": [
            {"id": "desk", "name": "Desk", "enabled": True, "detection": "sensors", "sensors": ["binary_sensor.d"],
             "activation": "possible", "timing": {}, "unavailable": "hold", "holds_room": True, "priority": 80,
             "manual": "respect", "daylight": "room", "release": "restore", "radar": None,
             "targets": [{"entity_id": "light.lamp", "action": "on"}], "status": {"active": True}},
            {"id": "bath", "name": "Bath", "enabled": True, "detection": "sensors", "sensors": [], "targets": [],
             "holds_room": False, "status": {"active": False}}],
            "targets": [{"entity_id": "light.lamp", "name": "Lamp"}], "sensors": [], "radar": {},
            "room_timing": {}, "limits": {}, "history": [], "space": None},
        "brightness": {"lights": [{"entity_id": "light.lamp", "name": "Lamp", "matrix": {
            "zones": {"desk": {"low": 45, "none": None}}, "delays": {"desk": {"low": {"on": 3, "off": 15}}}}}]}}}
    model = zones_api.presence_model(editor)
    desk = next(z for z in model["zones"] if z["id"] == "desk")
    # Presence shows the zone's detection, and what depends on it - never its lighting settings to edit.
    assert set(desk) - {"status", "priority", "lighting"} <= zones_api.DETECTION_KEYS
    assert "targets" not in desk and "manual" not in desk
    assert desk["lighting"]["lights"] == [{"entity_id": "light.lamp", "name": "Lamp", "action": "on"}]
    assert desk["lighting"]["brightness"] == [{"entity_id": "light.lamp", "name": "Lamp", "cells": 1, "delays": 1}]
    bath = next(z for z in model["zones"] if z["id"] == "bath")
    assert bath["lighting"] == {"lights": [], "brightness": [], "priority": None}  # a zone without lights
    assert model["holds"] == ["Desk"] and model["can_edit"] is True


async def test_presence_zone_commands_compose_the_zone_engine(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    await handover(lit_app)
    status, body = await command(lit_app, type=zones_api.ROOM, area_id=AREA)
    assert status == 200 and body["result"]["available"] is True and body["result"]["zones"] == []
    sensors = [s["entity_id"] for s in body["result"]["sensors"]]
    assert PIR in sensors and RADAR in sensors
    # Detection only: a lighting key is refused (it is Lighting's).
    status, body = await command(lit_app, type=zones_api.SET, area_id=AREA,
                                 zone={"name": "Desk", "sensors": [RADAR], "targets": [{"entity_id": LAMP}]})
    assert status == 400 and body["code"] == "not_presence", body
    # A non-administrator may read, not change.
    status, body = await command(lit_app, GUEST, type=zones_api.SET, area_id=AREA, zone={"name": "Desk"})
    assert status == 403
    status, body = await command(lit_app, type=zones_api.SET, area_id=AREA,
                                 zone={"name": "Desk", "sensors": [RADAR], "holds_room": False})
    assert status == 200 and body["result"]["zone"]["id"] == "desk" and body["result"]["sent"] is False, body
    zone = rt(lit_app).controller.lighting.room(AREA).zone("desk")
    assert zone.sensors == (RADAR,) and zone.targets == () and zone.holds_room is False
    # Lighting adds a light to it (its own command, lighting keys only); Presence edits keep it.
    status, body = await command(lit_app, type="smart_occupancy/lighting/zone/set", area_id=AREA,
                                 zone={"id": "desk", "targets": [{"entity_id": LAMP, "brightness_pct": 70}]})
    assert status == 200, body
    status, body = await command(lit_app, type=zones_api.SET, area_id=AREA, zone={"id": "desk", "name": "Desk area"})
    assert status == 200
    zone = rt(lit_app).controller.lighting.room(AREA).zone("desk")
    assert zone.name == "Desk area" and zone.id == "desk" and zone.targets[0].entity_id == LAMP
    # The same engine's state, seen from Presence; the lights that respond are listed.
    status, body = await command(lit_app, type=zones_api.ROOM, area_id=AREA)
    desk = body["result"]["zones"][0]
    assert desk["lighting"]["lights"][0]["entity_id"] == LAMP and "status" in desk
    # Deleting a zone a light responds to needs a confirmation.
    status, body = await command(lit_app, type=zones_api.REMOVE, area_id=AREA, zone_id="desk")
    assert status == 409 and body["code"] == "confirmation_required"
    assert rt(lit_app).controller.lighting.room(AREA).zone("desk") is not None
    status, body = await command(lit_app, type=zones_api.REMOVE, area_id=AREA, zone_id="desk", confirm=True)
    assert status == 200 and body["result"]["removed"] == "desk"
    assert rt(lit_app).controller.lighting.room(AREA).zones == ()


async def test_inspector_and_identify_are_the_apps_only(lit_app: Any, fake_ha: FakeHomeAssistant) -> None:
    # While the integration controls lighting (shadow): refused, never forwarded to it.
    before = len(fake_ha.lighting_editor_calls)
    status, body = await command(lit_app, type="smart_occupancy/lighting/light/inspect", entity_id=LAMP)
    assert status == 409 and body["code"] == "app_only"
    assert len(fake_ha.lighting_editor_calls) == before
    await handover(lit_app)
    status, body = await command(lit_app, type="smart_occupancy/lighting/light/inspect", entity_id=LAMP)
    assert status == 200 and body["handled_by"] == "app", body
    result = body["result"]
    assert result["configured"] and result["area_id"] == AREA and result["control"]["target"] == LAMP
    assert result["can_identify"] is True
    status, body = await command(lit_app, GUEST, type="smart_occupancy/lighting/light/identify", entity_id=LAMP)
    assert status == 403
    executed = len(fake_ha.lighting.executed)
    status, body = await command(lit_app, type="smart_occupancy/lighting/light/identify", entity_id=LAMP)
    assert status == 200 and body["result"]["outcome"] == "restored", body
    sent = fake_ha.lighting.executed[executed:]
    services = [(e.get("service"), (e.get("data") or {}).get("brightness_pct")) for e in sent]
    assert services[0] == ("turn_on", 100) and len(sent) == 2, sent  # full brightness, then exactly one restore
    controller = rt(lit_app).controller
    assert LAMP not in controller.runtime[AREA].overrides and not controller.identify.journal
    await wait_for(lambda: any(d["kind"] == "identify" for d in controller.decisions.entries(AREA)))
