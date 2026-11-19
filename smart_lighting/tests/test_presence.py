"""Presence (Phase A3.1) against the fake Home Assistant: views, timeline, review/save, safety."""

from __future__ import annotations

import json
from typing import Any

from smart_lighting.ha import CONFIG_WRITE_COMMANDS, READ_ONLY_COMMANDS, SUBSCRIPTION_COMMANDS
from smart_lighting.presence import MAX_POINTS_PER_ENTITY, PresenceService, parse_history

from .conftest import ADMIN_HEADERS, USER_ID, FakeHomeAssistant, revision_of, wait_for

KAMAR = "kamar_nordur"


async def post(client: Any, path: str, body: Any, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    resp = await client.post(path, data=json.dumps(body), headers={"Content-Type": "application/json",
                                                                     **(ADMIN_HEADERS if headers is None else headers)})
    return resp.status, await resp.json()


def sent(fake: FakeHomeAssistant, kind: str) -> list[dict[str, Any]]:
    return [c for c in fake.commands if c["type"] == kind]


def test_allowlist_has_only_the_presence_commands() -> None:
    assert "smart_occupancy/presence/set" in CONFIG_WRITE_COMMANDS
    assert {"smart_occupancy/presence/get", "smart_occupancy/presence/review",
            "history/history_during_period"} <= READ_ONLY_COMMANDS
    assert not any(c.startswith(("call_service", "execute_script", "fire_event", "recorder/"))
                   or (c.endswith("/update") and not c.startswith("smart_occupancy/curtains/"))
                   for c in READ_ONLY_COMMANDS | CONFIG_WRITE_COMMANDS)


def test_parse_history_is_bounded_and_drops_attribute_only_updates() -> None:
    raw = {"binary_sensor.a": [{"s": "off", "lu": 10.0}, {"s": "on", "lu": 11.0}, {"s": "on", "lu": 12.0},
                               {"s": "off", "lc": 13.0, "lu": 14.0}, "junk", {"lu": 15.0}],
           "binary_sensor.b": [{"s": str(i % 2), "lu": float(i)} for i in range(MAX_POINTS_PER_ENTITY + 10)]}
    out = parse_history(raw, ["binary_sensor.a", "binary_sensor.b", "binary_sensor.c"])
    assert [p["s"] for p in out["series"]["binary_sensor.a"]] == ["off", "on", "off"]
    assert out["series"]["binary_sensor.a"][2]["t"].startswith("1970-01-01T00:00:13")  # last_changed, not updated
    assert len(out["series"]["binary_sensor.b"]) == MAX_POINTS_PER_ENTITY and out["truncated"] == ["binary_sensor.b"]
    assert out["series"]["binary_sensor.c"] == []


def test_change_is_cleaned() -> None:
    clean = PresenceService.clean_change({"sensors": [{"entity_id": "binary_sensor.a", "kind": "pir", "evil": 1}],
                                          "timing": {"inactivity_timeout": 300}, "extra": True})
    assert clean == {"sensors": [{"entity_id": "binary_sensor.a", "kind": "pir"}], "timing": {"inactivity_timeout": 300}}
    for bad in ({"timing": {"rm_rf": 1}}, {"sensors": "x"}, "x"):
        try:
            PresenceService.clean_change(bad)
        except Exception as err:  # noqa: BLE001
            assert getattr(err, "code", "") == "invalid_change"
        else:
            raise AssertionError(bad)


async def test_overview_workspace_and_diagnostics(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await app_client.get("/api/presence")
    assert resp.status == 200
    data = await resp.json()
    assert data["integration_version"] == "0.6.10" and data["status"]["history"] is True
    room = next(r for r in data["rooms"] if r["area_id"] == KAMAR)
    assert room["name"] == "Kamar Norður" and "occupied" in room["tags"]
    radar = room["signals"][0]
    # The App adds what Home Assistant's registries say: device, area, Home Assistant's own classification.
    assert radar["device"] == "FP1 radar" and radar["ha_category"] == "presence" and radar["availability"] == "ok"
    assert {a["area_id"] for a in data["unconfigured"]} == {"stova", "uti"}
    assert data["counts"]["occupied"] == 1

    ws = await (await app_client.get(f"/api/presence/rooms/{KAMAR}")).json()
    assert ws["room"]["area_id"] == KAMAR and ws["global"]["timing"]["inactivity_timeout"] == 600
    # Unassigned presence sensors of the area are offered (the PIR), assigned ones are not.
    assert [c["entity_id"] for c in ws["candidates"]] == ["binary_sensor.kamar_pir"]
    resp = await app_client.get("/api/presence/rooms/stova")
    assert resp.status == 404 and (await resp.json())["code"] == "not_configured"
    resp = await app_client.get("/api/presence/rooms/..%2Fetc")
    assert resp.status in (400, 404)

    diag = await (await app_client.get("/api/presence/diagnostics")).json()
    assert any(d["code"] == "single_signal" and d["room"] == "Kamar Norður" for d in diag["items"])
    assert all(d["basis"] in ("measured", "heuristic", "integration") for d in diag["items"])
    assert sent(fake_ha, "smart_occupancy/presence/set") == []


async def test_timeline_reads_a_bounded_window_on_request(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    resp = await app_client.get(f"/api/presence/rooms/{KAMAR}/timeline?window=3600")
    assert resp.status == 200
    data = await resp.json()
    call = fake_ha.history_calls[-1]
    assert call["entity_ids"] == ["sensor.kamar_nordur_status", "binary_sensor.kamar_radar"]
    assert call["minimal_response"] and call["no_attributes"] and not call["significant_changes_only"]
    series = data["history"]["series"]
    assert [p["s"] for p in series["binary_sensor.kamar_radar"]] == ["off", "on"]
    assert data["window"] == 3600 and data["time_zone"] == "Atlantic/Faroe"
    # Cached: a second request within 30 s does not query the Recorder again.
    await app_client.get(f"/api/presence/rooms/{KAMAR}/timeline?window=3600")
    assert len(fake_ha.history_calls) == 1
    resp = await app_client.get(f"/api/presence/rooms/{KAMAR}/timeline?window=999999")
    assert resp.status == 400 and (await resp.json())["code"] == "invalid_window"
    assert len(fake_ha.history_calls) == 1


async def test_timeline_without_recorder(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    from smart_lighting.core import AppCore
    from smart_lighting.web import build_app

    from .conftest import make_settings

    fake_ha.history_components = False
    settings = make_settings(fake_ha)
    core = AppCore(settings)
    app = build_app(settings, core)
    app.on_startup.insert(0, lambda _: core.start())
    app.on_cleanup.append(lambda _: core.stop())
    client = await aiohttp_client(app)
    await wait_for(lambda: core.rooms is not None and core.rooms.api_state == "ok")
    data = await (await client.get(f"/api/presence/rooms/{KAMAR}/timeline?window=900")).json()
    assert data["history"]["available"] is False and "not available" in data["history"]["error"]
    assert fake_ha.history_calls == []
    assert data["log"]  # the integration's log still works


async def test_review_and_save(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    rev = revision_of(fake_ha.global_timing)
    status, review = await post(app_client, "/api/presence/review",
                                {"area_id": None, "change": {"timing": {"inactivity_timeout": 900}}, "revision": rev})
    assert status == 200 and review["apply"] == "live" and review["changes"][0]["key"] == "inactivity_timeout"
    assert sent(fake_ha, "smart_occupancy/presence/set") == []  # a review writes nothing
    status, body = await post(app_client, "/api/presence/save", {"area_id": None, "change": {
        "timing": {"inactivity_timeout": 900}}, "revision": rev, "confirmed": []})
    assert status == 200, body
    assert fake_ha.global_timing["inactivity_timeout"] == 900
    assert body["result"]["backup"]  # a backup of the rooms section and the global defaults before the change
    backup = await (await app_client.get(f"/api/rooms/backups/{body['result']['backup']}")).json()
    assert backup["occupancy"] == {"inactivity_timeout": 300}  # what the rooms API reported before
    writes = sent(fake_ha, "smart_occupancy/presence/set")
    assert len(writes) == 1 and writes[0]["change"] == {"timing": {"inactivity_timeout": 900}}
    # A stale revision is refused and reported as a conflict.
    status, body = await post(app_client, "/api/presence/save", {"area_id": None, "change": {
        "timing": {"inactivity_timeout": 1200}}, "revision": rev, "confirmed": []})
    assert status == 409 and body["code"] == "stale_revision"
    # Room scope, with a required confirmation.
    fake_ha.require_confirmation = ["automatic_rooms"]
    room_rev = revision_of(fake_ha.rooms_options[KAMAR])
    change = {"timing": {"correlation_window": 20}}
    status, body = await post(app_client, "/api/presence/save", {"area_id": KAMAR, "change": change,
                                                                 "revision": room_rev, "confirmed": []})
    assert status == 409 and body["code"] == "confirmation_required"
    status, body = await post(app_client, "/api/presence/save", {"area_id": KAMAR, "change": change,
                                                                 "revision": room_rev,
                                                                 "confirmed": ["automatic_rooms"]})
    assert status == 200, body
    assert fake_ha.rooms_options[KAMAR]["timing"]["correlation_window"] == 20


async def test_writes_need_an_admin_and_the_app_header(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    body = {"area_id": None, "change": {"timing": {"inactivity_timeout": 900}},
            "revision": revision_of(fake_ha.global_timing), "confirmed": []}
    for headers in ({"X-Remote-User-Id": USER_ID, "X-Smart-Lighting": "1"}, {"X-Remote-User-Id": "nobody",
                                                                            "X-Smart-Lighting": "1"},
                    {"X-Smart-Lighting": "1"}, {k: v for k, v in ADMIN_HEADERS.items() if k != "X-Smart-Lighting"}):
        for path in ("/api/presence/review", "/api/presence/save"):
            status, reply = await post(app_client, path, body, headers)
            assert status == 403, (headers, path, reply)
    status, _ = await post(app_client, "/api/presence/save", body, {**ADMIN_HEADERS, "Origin": "https://evil.example"})
    assert status == 403
    for bad in ({**body, "revision": "not-a-revision"}, {**body, "confirmed": ["Bad Code!"]},
                {**body, "area_id": "../x"}, {**body, "change": {"timing": {"oops": 1}}}):
        status, _ = await post(app_client, "/api/presence/save", bad)
        assert status == 400, bad
    assert sent(fake_ha, "smart_occupancy/presence/set") == []
    assert fake_ha.global_timing["inactivity_timeout"] == 600


async def test_old_integration_is_reported(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    fake_ha.presence_api = False
    resp = await app_client.get("/api/presence")
    assert resp.status == 503
    data = await resp.json()
    assert data["code"] == "unsupported" and "0.6.10" in data["error"]
    health = await (await app_client.get("/api/health")).json()
    check = next(c for c in health["checks"] if c["key"] == "presence_api")
    assert check["status"] == "warning"


async def test_no_service_call_and_no_token_in_any_answer(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    from .conftest import TOKEN

    texts = []
    for path in ("/api/presence", f"/api/presence/rooms/{KAMAR}", f"/api/presence/rooms/{KAMAR}/timeline",
                 "/api/presence/diagnostics", "/api/overview"):
        texts.append(await (await app_client.get(path)).text())
    assert all(TOKEN not in t for t in texts)
    kinds = {c["type"] for c in fake_ha.commands}
    assert kinds <= READ_ONLY_COMMANDS | CONFIG_WRITE_COMMANDS | SUBSCRIPTION_COMMANDS
    assert not any("call_service" in k for k in kinds)
