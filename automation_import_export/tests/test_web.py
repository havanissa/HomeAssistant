"""The HTTP layer: Ingress only, administrators only, CSRF and origin checks, limits, downloads."""

from __future__ import annotations

from dataclasses import replace
import json

from aiohttp.test_utils import TestClient, TestServer
from conftest import ADMIN, GUEST, wait_op
import pytest

from aie import web


@pytest.fixture
async def client(core):
    app = web.build_app(core.settings, core)
    async with TestClient(TestServer(app)) as c:
        yield c


H = {"X-Remote-User-Id": ADMIN}
POST = {**H, "X-AIE": "1", "Content-Type": "application/json"}


async def test_index_gets_the_ingress_base_and_a_strict_csp(client):
    r = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    text = await r.text()
    assert '<base href="/api/hassio_ingress/abc/">' in text
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    r = await client.get("/", headers={"X-Ingress-Path": "/evil\"><script>"})
    assert '<base href="/">' in await r.text()


async def test_reads_need_an_administrator(client):
    for path in ("/api/inventory", "/api/archives", "/api/operations", "/api/diagnostics"):
        assert (await client.get(path)).status == 403
        assert (await client.get(path, headers={"X-Remote-User-Id": GUEST})).status == 403
        assert (await client.get(path, headers=H)).status == 200
    data = await (await client.get("/api/overview", headers={"X-Remote-User-Id": GUEST})).json()
    assert data["user"]["is_admin"] is False and "summary" not in data


async def test_changes_need_the_header_the_origin_and_an_administrator(client):
    body = json.dumps({"keys": ["1700000000003"]})
    assert (await client.post("/api/export", data=body, headers={**H, "Content-Type": "application/json"})).status == 403
    assert (await client.post("/api/export", data=body, headers={**POST, "Origin": "http://evil.example"})).status == 403
    assert (await client.post("/api/export", data=body, headers={**POST, "X-Remote-User-Id": GUEST})).status == 403
    assert (await client.post("/api/export", data="[1]", headers=POST)).status == 400
    r = await client.post("/api/export", data=body, headers={**POST, "Origin": f"http://{client.host}:{client.port}"})
    assert r.status == 200


async def test_upload_limits_and_download(client, core, monkeypatch):
    r = await client.post("/api/archives/upload", data=b"not a zip", headers={**H, "X-AIE": "1", "Content-Type": "application/zip"})
    assert r.status == 422 and (await r.json())["error"] == "not_zip"
    monkeypatch.setattr(web, "MAX_ARCHIVE_BYTES", 10)
    r = await client.post("/api/archives/upload", data=b"x" * 100, headers={**H, "X-AIE": "1", "Content-Type": "application/zip"})
    assert r.status == 413
    await core.inventory_view()
    op = await wait_op(core, await core.start_export({"keys": ["1700000000003"]}, ADMIN))
    r = await client.get(f"/api/archives/{op['archive_id']}/download", headers=H)
    assert r.status == 200 and r.headers["Content-Type"] == "application/zip"
    assert "attachment" in r.headers["Content-Disposition"] and (await r.read())[:2] == b"PK"
    assert (await client.get("/api/archives/..%2Fx/download", headers=H)).status == 404
    assert (await client.get("/api/archives/aie-20260101T000000Z-abcdef/download", headers=H)).status == 404


async def test_only_ingress_may_connect(core):
    from aie.settings import _networks
    settings = replace(core.settings, allowed_networks=_networks("172.30.32.2"))
    async with TestClient(TestServer(web.build_app(settings, core))) as c:
        r = await c.get("/api/health")
        assert r.status == 403
