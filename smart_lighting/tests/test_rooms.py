"""Rooms (Phase A2) against the fake Home Assistant: discovery, review/save, safety, backups, live updates."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from smart_lighting.core import AppCore
from smart_lighting.ha import CONFIG_WRITE_COMMANDS, READ_ONLY_COMMANDS
from smart_lighting.rooms import classify
from smart_lighting.web import build_app

from .conftest import ADMIN_HEADERS, TOKEN, USER_ID, FakeHomeAssistant, make_settings, revision_of, wait_for

KAMAR = "kamar_nordur"


def kamar_edit(fake: FakeHomeAssistant, **changes: Any) -> dict[str, Any]:
    room = fake.rooms_options[KAMAR]
    return {"enabled": room["enabled"], "sensors": [dict(s) for s in room["sensors"]], "lights": list(room["lights"]),
            "reference_occupancy_entity": room["reference_occupancy_entity"],
            "adjacent_rooms": list(room["adjacent_rooms"]), **changes}


async def post(client: Any, path: str, body: Any, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    resp = await client.post(path, data=json.dumps(body), headers={"Content-Type": "application/json",
                                                                     **(ADMIN_HEADERS if headers is None else headers)})
    return resp.status, await resp.json()


def sent(fake: FakeHomeAssistant, kind: str) -> list[dict[str, Any]]:
    return [c for c in fake.commands if c["type"] == kind]


# ---------------------------------------------------------------- discovery
def test_classification_uses_device_class_and_user_choice() -> None:
    def cat(entity_id: str, dclass: str | None = None, model: str | None = None, **kw: Any) -> tuple[str, str]:
        state = {"state": "off", "attributes": {"device_class": dclass} if dclass else {}}
        device = {"model": model} if model else None
        c, conf, _ = classify(entity_id, {}, state, device, relays=kw.get("relays", set()), switch_entities=set(),
                              assigned_kind=kw.get("kind"))
        return c, conf

    assert cat("binary_sensor.a", "motion") == ("pir", "high")
    assert cat("binary_sensor.a", "occupancy") == ("presence", "medium")  # many PIRs report this too
    assert cat("binary_sensor.a", "occupancy", "FP1 presence sensor") == ("presence", "high")
    assert cat("binary_sensor.a", "occupancy", "RTCGQ11LM motion") == ("pir", "medium")
    assert cat("binary_sensor.a", "occupancy", kind="pir") == ("pir", "user")  # the user's classification wins
    assert cat("binary_sensor.a", "door")[0] == "door"
    assert cat("binary_sensor.a", "window")[0] == "window"
    assert cat("sensor.t", "temperature")[0] == "temperature"
    assert cat("sensor.l", "illuminance")[0] == "illuminance"
    assert cat("switch.r", relays={"switch.r"}) == ("relay", "high")
    assert cat("switch.x") == ("switch_output", "low")
    assert cat("cover.c")[0] == "cover"
    assert cat("event.b", "button")[0] == "wall_switch"
    assert cat("device_tracker.phone")[0] == "presence_input"
    assert cat("sensor.lounge_name_motion_lol") == ("other", "low")  # never from the entity name


async def test_rooms_overview_lists_every_area(app_client: Any) -> None:
    data = await (await app_client.get("/api/rooms")).json()
    rooms = {r["area_id"]: r for r in data["rooms"]}
    assert list(rooms) == ["kamar_nordur", "stova", "uti"]  # sorted by name; unconfigured areas included
    kamar = rooms[KAMAR]
    assert (kamar["status"], kamar["configured"], kamar["enabled"]) == ("configured", True, True)
    assert kamar["occupancy"] == {"state": "confirmed_occupancy", "occupied": True, "reason": "Radar active"}
    assert (kamar["sensors"], kamar["lights"]) == (1, 1)
    assert kamar["automation"]["owner"] == "integration" and kamar["automation"]["lighting"] == "observing"
    assert kamar["unassigned_candidates"] >= 2  # the PIR and the lamp in the area
    assert rooms["stova"]["status"] == "not_configured" and rooms["stova"]["occupancy"] is None
    assert data["status"]["api"] == "ok" and data["status"]["writable"] is True


async def test_workspace_discovers_devices_and_status(app_client: Any) -> None:
    data = await (await app_client.get(f"/api/rooms/{KAMAR}")).json()
    assert data["room"]["config"]["timing"] == {"inactivity_timeout": 600}  # the stored room, as it is
    assert data["room"]["revision"] == revision_of(data["room"]["config"])
    e = data["entities"]
    assert e["binary_sensor.kamar_pir"]["category"] == "pir"
    assert e["binary_sensor.kamar_radar"]["category"] == "presence"
    assert e["binary_sensor.kamar_radar"]["category_confidence"] == "user"  # assigned as radar
    assert e["light.kamar_lamp"]["category"] == "light"
    assert e["cover.kamar_curtain"]["category"] == "cover" and "position" in e["cover.kamar_curtain"]["capabilities"]
    assert e["light.kamar_nordur_loft"]["light_rooms"] == [KAMAR]
    pir_device = data["devices"]["dev-pir"]
    assert pir_device["identity"] == ["zigbee: 00:15:8d:00:01:02:03:04"] and pir_device["status"] == "online"
    assert {a["id"] for a in data["areas"]} == {"kamar_nordur", "stova", "uti"}
    stova = await (await app_client.get("/api/rooms/stova")).json()
    assert stova["room"]["configured"] is False
    assert stova["entities"]["binary_sensor.stova_door"]["availability"] == "device_offline"  # its only entity
    assert (await app_client.get("/api/rooms/no_such_area")).status == 404
    assert (await app_client.get("/api/rooms/..%2Fetc")).status in (400, 404)


async def test_candidates_for_selectors(app_client: Any) -> None:
    entities = {e["entity_id"]: e for e in (await (await app_client.get("/api/rooms/candidates")).json())["entities"]}
    assert "binary_sensor.kamar_nordur_occupancy" not in entities  # the integration's own entities are not inputs
    assert entities["binary_sensor.stova_door"]["area_id"] == "stova"
    assert "switch.disabled_switch" not in entities


# ------------------------------------------------------------------ writing
async def test_review_and_save(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    revision = fake_ha.rooms_model()["rooms"][KAMAR]["revision"]
    edit = kamar_edit(fake_ha, lights=["light.kamar_nordur_loft", "light.kamar_lamp"])
    edit["sensors"].append({"entity_id": "binary_sensor.stova_door", "kind": "door", "mood_overrides": {"x": 1}})
    edit["type"] = "call_service"  # extra keys from the browser never reach Home Assistant
    status, review = await post(app_client, f"/api/rooms/{KAMAR}/review", {"room": edit, "revision": revision})
    assert status == 200, review
    assert {(c["type"], c["entity_id"]) for c in review["changes"]} == {
        ("sensor_added", "binary_sensor.stova_door"), ("light_added", "light.kamar_lamp")}
    assert {(c["code"], c["entity_id"]) for c in review["conflicts"]} == {("outside_area", "binary_sensor.stova_door")}
    assert {a["entity_id"]: a["availability"] for a in review["attention"]} == {
        "binary_sensor.stova_door": "device_offline"}
    sent_room = sent(fake_ha, "smart_occupancy/rooms/review")[-1]["room"]
    assert set(sent_room) <= {"enabled", "sensors", "lights", "reference_occupancy_entity", "adjacent_rooms"}
    assert all(set(s) <= {"entity_id", "kind", "role", "inverted", "enabled"} for s in sent_room["sensors"])
    assert fake_ha.rooms_options[KAMAR]["lights"] == ["light.kamar_nordur_loft"]  # a review changes nothing

    status, saved = await post(app_client, f"/api/rooms/{KAMAR}/save", {"room": edit, "revision": revision,
                                                                       "confirmed": []})
    assert status == 200, saved
    assert fake_ha.rooms_options[KAMAR]["lights"] == ["light.kamar_nordur_loft", "light.kamar_lamp"]
    assert fake_ha.rooms_options[KAMAR]["timing"] == {"inactivity_timeout": 600}  # untouched keys stay
    assert saved["workspace"]["room"]["revision"] == revision_of(fake_ha.rooms_options[KAMAR]) != revision
    assert saved["result"]["backup"]
    assert len(sent(fake_ha, "smart_occupancy/rooms/set")) == 1
    assert "call_service" not in {c["type"] for c in fake_ha.commands}
    assert {c["type"] for c in fake_ha.commands} <= READ_ONLY_COMMANDS | CONFIG_WRITE_COMMANDS
    safety = (await (await app_client.get("/api/overview")).json())["safety"]
    assert safety["config_writes_sent"] == 1 and safety["service_calls_sent"] == 0
    assert safety["recent_config_writes"][-1]["result"] == "ok"
    # The previous configuration is kept as a backup and can be downloaded.
    backups = (await (await app_client.get("/api/rooms/backups")).json())["backups"]
    assert backups[0]["id"] == saved["result"]["backup"] and backups[0]["reason"] == "Before saving Kamar Norður"
    previous = await (await app_client.get(f"/api/rooms/backups/{backups[0]['id']}")).json()
    assert previous["rooms"][KAMAR]["lights"] == ["light.kamar_nordur_loft"]
    assert (await app_client.get("/api/rooms/backups/..%2F..%2Fetc%2Fpasswd")).status == 404


async def test_stale_and_confirmation(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    revision = fake_ha.rooms_model()["rooms"][KAMAR]["revision"]
    fake_ha.rooms_options[KAMAR]["lights"] = []  # changed in the integration's own editor meanwhile
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save", {"room": kamar_edit(fake_ha), "revision": revision})
    assert (status, body["code"]) == (409, "stale_revision")
    assert not (await (await app_client.get("/api/rooms/backups")).json())["backups"]  # refused: no backup kept
    revision = revision_of(fake_ha.rooms_options[KAMAR])
    fake_ha.require_confirmation = ["automatic_lighting"]
    edit = kamar_edit(fake_ha, lights=["light.kamar_lamp"])
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save", {"room": edit, "revision": revision})
    assert (status, body["code"]) == (409, "confirmation_required")
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save",
                              {"room": edit, "revision": revision, "confirmed": ["automatic_lighting"]})
    assert status == 200, body
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save",
                              {"room": kamar_edit(fake_ha, lights=["switch.relay"]),
                               "revision": revision_of(fake_ha.rooms_options[KAMAR]), "confirmed": ["automatic_lighting"]})
    assert (status, body["code"]) == (400, "invalid_room")


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"X-Smart-Lighting": "1"}, None),  # no Home Assistant user (not through Ingress)
        ({"X-Remote-User-Id": USER_ID, "X-Smart-Lighting": "1"}, None),  # not an administrator
        ({"X-Remote-User-Id": "unknown-user", "X-Smart-Lighting": "1"}, None),
        ({"X-Remote-User-Id": "user-admin-1"}, None),  # no CSRF header
        ({**ADMIN_HEADERS, "Origin": "https://evil.example"}, None),  # cross-site
    ],
)
async def test_writes_need_an_administrator_and_same_origin(app_client: Any, fake_ha: FakeHomeAssistant,
                                                            headers: dict[str, str], body: Any) -> None:
    revision = fake_ha.rooms_model()["rooms"][KAMAR]["revision"]
    for path in (f"/api/rooms/{KAMAR}/review", f"/api/rooms/{KAMAR}/save", "/api/rooms/restore/review",
                 "/api/rooms/restore"):
        status, reply = await post(app_client, path, {"room": kamar_edit(fake_ha), "revision": revision},
                                   headers=headers)
        assert (status, reply["code"]) == (403, "forbidden"), path
    assert not sent(fake_ha, "smart_occupancy/rooms/set") and not sent(fake_ha, "smart_occupancy/rooms/review")


async def test_invalid_requests(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    revision = fake_ha.rooms_model()["rooms"][KAMAR]["revision"]
    for path, body, code in (
        (f"/api/rooms/{KAMAR}/save", {"room": kamar_edit(fake_ha), "revision": "nothex"}, "invalid_revision"),
        (f"/api/rooms/{KAMAR}/save", {"room": kamar_edit(fake_ha)}, "invalid_revision"),
        (f"/api/rooms/{KAMAR}/save", {"room": "x", "revision": revision}, "invalid_room"),
        (f"/api/rooms/{KAMAR}/save", {"room": kamar_edit(fake_ha), "revision": revision, "confirmed": ["Bad Code!"]},
         "invalid_confirmation"),
        ("/api/rooms/restore/review", {"backup": {"format": "other"}}, "invalid_backup"),
        ("/api/rooms/restore/review", {"backup": {"format": "smart_occupancy.rooms_backup", "version": 1,
                                                  "rooms": {"../x": {}}}}, "invalid_backup"),
    ):
        status, reply = await post(app_client, path, body)
        assert (status, reply["code"]) == (400, code), (path, body)
    resp = await app_client.post(f"/api/rooms/{KAMAR}/save", data="{not json",
                                 headers={"Content-Type": "application/json", **ADMIN_HEADERS})
    assert (resp.status, (await resp.json())["code"]) == (400, "invalid_json")
    assert not sent(fake_ha, "smart_occupancy/rooms/set")


async def test_connection_lost_while_saving(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    revision = fake_ha.rooms_model()["rooms"][KAMAR]["revision"]
    fake_ha.drop_on = "smart_occupancy/rooms/set"
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save",
                              {"room": kamar_edit(fake_ha, lights=[]), "revision": revision})
    assert (status, body["code"]) == (503, "connection_lost")
    assert "may or may not" in body["error"]
    assert (await (await app_client.get("/api/rooms/backups")).json())["backups"]  # kept: the outcome is unknown
    await wait_for(lambda: app_client.core.ha.connected)  # reconnects by itself
    writes = (await (await app_client.get("/api/overview")).json())["safety"]["recent_config_writes"]
    assert writes[-1]["result"].startswith("unknown")


async def test_integration_without_rooms_api(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    fake_ha.rooms_api = False
    settings = make_settings(fake_ha)
    core = AppCore(settings)
    app = build_app(settings, core)
    app.on_startup.insert(0, lambda _: core.start())
    app.on_cleanup.append(lambda _: core.stop())
    client = await aiohttp_client(app)
    await wait_for(lambda: core.ha.connected)
    data = await (await client.get("/api/rooms")).json()
    assert data["status"]["api"] == "unsupported" and "0.6.9" in data["status"]["error"]
    assert {r["area_id"] for r in data["rooms"]} == {"kamar_nordur", "stova", "uti"}  # still listed, read-only
    status, body = await post(client, f"/api/rooms/{KAMAR}/review", {"room": kamar_edit(fake_ha), "revision": None})
    assert (status, body["code"]) == (503, "api_unavailable")
    health = await (await client.get("/api/health")).json()
    assert {c["key"]: c["status"] for c in health["checks"]}["rooms_api"] == "warning"


async def test_integration_not_running(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    fake_ha.loaded = False
    await app_client.core.rooms.refresh()
    data = await (await app_client.get(f"/api/rooms/{KAMAR}")).json()
    assert data["status"]["writable"] is False
    assert "integration_not_running" in {w["code"] for w in data["summary"]["warnings"]}
    status, body = await post(app_client, f"/api/rooms/{KAMAR}/save",
                              {"room": kamar_edit(fake_ha), "revision": data["room"]["revision"]})
    assert (status, body["code"]) == (503, "not_loaded")


async def test_export_and_restore(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await app_client.get("/api/rooms/export")
    assert "attachment" in resp.headers["Content-Disposition"]
    backup = await resp.json()
    assert backup["format"] == "smart_occupancy.rooms_backup" and backup["rooms"][KAMAR] == fake_ha.rooms_options[KAMAR]
    assert TOKEN not in json.dumps(backup)
    fake_ha.rooms_options[KAMAR]["lights"] = []
    await app_client.core.rooms.refresh()
    status, review = await post(app_client, "/api/rooms/restore/review", {"backup": backup})
    assert status == 200 and review["requires_confirmation"] == ["restore"]
    status, body = await post(app_client, "/api/rooms/restore", {"backup": backup, "revision": "0" * 16,
                                                                 "confirmed": ["restore"]})
    assert (status, body["code"]) == (409, "stale_revision")
    status, body = await post(app_client, "/api/rooms/restore", {"backup": backup, "revision": review["revision"]})
    assert (status, body["code"]) == (409, "confirmation_required")
    status, body = await post(app_client, "/api/rooms/restore", {"backup": backup, "revision": review["revision"],
                                                                 "confirmed": ["restore"]})
    assert status == 200, body
    assert fake_ha.rooms_options[KAMAR]["lights"] == ["light.kamar_nordur_loft"]
    assert body["backup"]  # the replaced configuration is kept


async def test_live_watch_and_room_changes(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    async with app_client.ws_connect("/api/live") as ws:
        assert (await ws.receive_json(timeout=5))["type"] == "overview"
        await ws.send_str(json.dumps({"type": "watch", "entities": ["binary_sensor.kamar_pir", "bad id", 5]}))

        async def next_of(kind: str) -> dict[str, Any]:
            while True:
                msg = await ws.receive_json(timeout=5)
                if msg["type"] == kind:
                    return msg

        first = await next_of("states")
        assert [s["entity_id"] for s in first["changed"]] == ["binary_sensor.kamar_pir"]
        await fake_ha.emit_state("light.kamar_lamp", "on", "Lamp Kamar")  # not watched
        await fake_ha.emit_state("binary_sensor.kamar_pir", "on", "PIR Kamar")
        update = await next_of("states")
        assert update["changed"] == [{"entity_id": "binary_sensor.kamar_pir", "state": "on",
                                      "last_changed": "2026-11-17T10:00:00+00:00",
                                      "last_updated": "2026-11-17T10:00:00+00:00", "availability": "ok",
                                      "attrs": {}}]
        # A change through the integration's own editor reaches open pages as a "rooms" message.
        fake_ha.rooms_options[KAMAR]["lights"] = []
        await fake_ha.notify_entry_changed()
        rooms = await next_of("rooms")
        assert rooms["status"]["revision"] == revision_of(fake_ha.rooms_options)


async def test_app_restart_reads_the_integration_again(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    """The App stores no room configuration: after a restart it reads the integration's again."""
    for _ in range(2):
        settings = make_settings(fake_ha)
        core = AppCore(settings)
        await core.start()
        try:
            await wait_for(lambda: core.ha.connected and core.rooms.api_state == "ok")
            assert core.rooms.workspace(KAMAR)["room"]["config"] == fake_ha.rooms_options[KAMAR]
        finally:
            await core.stop()
        fake_ha.rooms_options[KAMAR]["lights"] = ["light.kamar_lamp"]
    await asyncio.sleep(0)
