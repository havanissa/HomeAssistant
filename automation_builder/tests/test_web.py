"""The HTTP interface: access rules, the JSON API end to end, the pages."""

from __future__ import annotations

import pytest

from ab.settings import load_settings
from ab.web import build_app
import builders as B
from conftest import ADMIN, GUEST, make_core

LAMP = "light.bed_lamp"
POST = {"X-AB": "1", "X-Remote-User-Id": ADMIN}
GET = {"X-Remote-User-Id": ADMIN}


@pytest.fixture
async def client(aiohttp_client, tmp_path, home):
    core = make_core(tmp_path, home)
    app = build_app(core.settings, core)
    client = await aiohttp_client(app)
    client.core = core
    return client


async def post(client, path, body, **headers):
    response = await client.post(path, json=body, headers={**POST, **headers})
    return response.status, await response.json()


async def get(client, path, user=ADMIN):
    response = await client.get(path, headers={"X-Remote-User-Id": user})
    return response.status, await response.json()


async def test_page_and_assets(client):
    response = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    page = await response.text()
    assert response.status == 200 and '<base href="/api/hassio_ingress/abc/">' in page and "__VERSION__" not in page
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    bad = await client.get("/", headers={"X-Ingress-Path": '/"><script>'})
    assert '<base href="/">' in await bad.text()
    for name in ("app.js", "ui.js", "editor.js", "app.css", "icon.svg"):
        assert (await client.get(f"/assets/{name}")).status == 200, name
    assert (await client.get("/assets/..%2Fweb.py")).status == 404
    assert (await client.get("/assets/index.html")).status == 404
    assert (await (await client.get("/api/health")).json())["ok"] is True


async def test_only_ingress_may_connect(aiohttp_client, tmp_path, home):
    core = make_core(tmp_path, home)
    settings = load_settings({"AB_DATA_DIR": str(tmp_path), "AB_OPTIONS": str(tmp_path / "o.json"), "SUPERVISOR_TOKEN": "t"})
    assert [str(n) for n in settings.allowed_networks] == ["172.30.32.2/32"]
    client = await aiohttp_client(build_app(settings, core))
    assert (await client.get("/")).status == 403
    assert (await client.get("/api/overview")).status == 403


async def test_changes_need_csrf_header_same_origin_and_admin(client):
    body = {"name": "Lamp", "targets": [LAMP], "expected_revision": client.core.store.revision}
    assert (await client.post("/api/devices/create", json=body, headers={"X-Remote-User-Id": ADMIN})).status == 403
    assert (await client.post("/api/devices/create", data="{}", headers=POST)).status == 403  # not JSON
    status, data = await post(client, "/api/devices/create", body, Origin="https://evil.example")
    assert status == 403 and "Cross-site" in data["message"]
    status, _ = await post(client, "/api/devices/create", body, **{"X-Remote-User-Id": GUEST})
    assert status == 403
    response = await client.post("/api/devices/create", json=body, headers={"X-AB": "1"})  # no user at all
    assert response.status == 403
    assert client.core.store.config["devices"] == {}
    status, data = await post(client, "/api/devices/create", body, Origin=f"http://{client.host}:{client.port}")
    assert status == 200 and data["device_id"].startswith("d_")


async def test_reads_are_for_administrators(client):
    status, data = await get(client, "/api/overview", GUEST)
    assert status == 200 and data["user"] == {"is_admin": False} and data["devices"] == [] and data["totals"] is None
    for path in ("/api/catalog", "/api/entities", "/api/ha-devices", "/api/settings", "/api/export", "/api/diagnostics",
                 "/api/entity?ids=light.bed_lamp", "/api/devices/d_00000000"):
        assert (await get(client, path, GUEST))[0] == 403, path
    assert (await get(client, "/api/devices/d_00000000"))[0] == 404


async def test_whole_workflow_over_http(client, home):
    status, found = await get(client, "/api/ha-devices?q=relay")
    assert status == 200 and [e["entity_id"] for e in found["devices"][0]["entities"]] == ["switch.relay_1", "switch.relay_2"]
    status, catalog = await get(client, "/api/catalog")
    assert "light" in catalog["domains"] and catalog["settings"]["occupancy"]["second_seconds"] == 15
    revision = (await get(client, "/api/overview"))[1]["revision"]
    status, created = await post(client, "/api/devices/create", {"targets": [LAMP], "expected_revision": revision})
    device_id, revision = created["device_id"], created["revision"]
    rule = {"name": "Motion", "triggers": [B.state("binary_sensor.motion", to="on")], "conditions": B.all_of(),
            "actions": [B.call("light.turn_on", brightness_pct=70)]}
    status, preview = await post(client, "/api/rules/preview", {"device_id": device_id, "rule": rule})
    assert preview["valid"] and "brightness 70 %" in preview["summary"]["text"]
    status, stale = await post(client, "/api/rules/save", {"device_id": device_id, "rule": rule, "expected_revision": "old"})
    assert status == 409 and stale["error"] == "stale" and stale["revision"] == revision
    status, saved = await post(client, "/api/rules/save", {"device_id": device_id, "rule": rule, "expected_revision": revision})
    assert status == 200
    revision = saved["revision"]
    status, invalid = await post(client, "/api/rules/save", {"device_id": device_id, "rule": {**rule, "actions": []},
                                                              "expected_revision": revision})
    assert status == 400 and invalid["problems"][0]["message"].startswith("Add at least one action")
    status, plan = await post(client, "/api/devices/plan", {"device_id": device_id})
    assert plan["can_deploy"] and home.file == {}
    status, refused = await post(client, "/api/devices/deploy", {"device_id": device_id, "expected_revision": revision,
                                                                  "plan_hash": "something-else"})
    assert status == 409 and refused["error"] == "plan_changed"
    status, deployed = await post(client, "/api/devices/deploy", {"device_id": device_id, "expected_revision": revision,
                                                                   "plan_hash": plan["plan_hash"]})
    assert status == 200 and deployed["deployed"] and len(home.file) == 1
    status, view = await get(client, f"/api/devices/{device_id}")
    assert view["live"]["status"] == "in_sync" and "automation_builder_" in view["yaml"]
    status, overview = await get(client, "/api/overview")
    assert overview["totals"] == {"devices": 1, "rules": 1, "automations": 1, "warnings": 0}
    status, diagnostics = await get(client, "/api/diagnostics")
    assert diagnostics["devices"][0]["status"] == "in_sync" and "secret-token" not in str(diagnostics)
    assert diagnostics["settings"]["token"] == "present (hidden)"
    status, export = await get(client, "/api/export")
    assert export["format"] == "automation-builder-config" and device_id in export["config"]["devices"]
    status, settings = await get(client, "/api/settings")
    assert settings["mechanism"]["endpoint"].startswith("/config/automation/config/automation_builder_")
    status, deleted = await post(client, "/api/devices/delete", {"device_id": device_id, "expected_revision": revision,
                                                                  "overwrite": False})
    assert status == 200 and deleted["removed_automations"] == 1 and home.file == {}


async def test_errors_are_json(client):
    status, data = await get(client, "/api/nope")
    assert status == 404 and data["error"]
    status, data = await post(client, "/api/devices/plan", {"device_id": "d_ffffffff"})
    assert status == 404 and data["error"] == "unknown_device"
    response = await client.post("/api/rules/save", data="[1]", headers={**POST, "Content-Type": "application/json"})
    assert response.status == 400
    big = await client.post("/api/rules/save", data=b'{"x":"' + b"a" * 600_000 + b'"}',
                            headers={**POST, "Content-Type": "application/json"})
    assert big.status == 413
