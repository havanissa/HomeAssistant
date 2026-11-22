"""The App's infrastructure: the configuration store, the web security frame, the companion link, the allowlist.

Nothing here knows about climate: ``spc.config`` and ``spc.api`` are replaced by stand-ins (``conftest.py``).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat

import aiohttp
from aiohttp import web
import pytest

from conftest import KAMAR, STOVA, FakeHA, entity, settle, snapshot, stub_config_module, wait_for
from spc import ha as HA, protocol as P, publisher as PUB, store as S
from spc.const import APP_VERSION
from spc.publisher import CompanionLink, LinkError
from spc.settings import load_settings
from spc.store import ConfigError, ConfigStore

HEADERS = {"X-SPC": "1", "X-Remote-User-Id": "admin"}


# =========================================================================== store
def make_store(tmp_path: Path, **kwargs) -> ConfigStore:
    stub = stub_config_module()
    store = ConfigStore(tmp_path, validate=stub.validate_config, empty=stub.empty_config, **kwargs)
    store.load()
    return store


def test_store_starts_empty_and_saves_with_a_revision(tmp_path, clock):
    store = make_store(tmp_path)
    assert store.config == {"rooms": {}} and store.saved_at is None and store.recovery is None
    assert store.revision == S.revision_of({"rooms": {}}) and len(store.revision) == 16
    seen = []
    remove = store.add_listener(seen.append)
    result = store.save({"rooms": {KAMAR: {"name": "Kamar"}}}, expected_revision=store.revision, reason="Add room",
                        by="admin")
    assert result == {"revision": store.revision, "changed": True} and seen == ["Add room"]
    document = json.loads((tmp_path / "config" / "config.json").read_text())
    assert document == {"schema": 1, "revision": store.revision, "saved_at": "2026-11-22T12:00:00+00:00",
                        "reason": "Add room", "by": "admin", "config": {"rooms": {KAMAR: {"name": "Kamar"}}}}
    assert not list((tmp_path / "config").glob("*.tmp"))
    # Saving the same again changes nothing and tells nobody.
    assert store.save(store.config, expected_revision=store.revision, reason="again")["changed"] is False
    assert seen == ["Add room"] and store.backups() == []
    # A stale revision is refused; so is an invalid configuration (nothing is written).
    with pytest.raises(ConfigError) as err:
        store.save({"rooms": {}}, expected_revision="0" * 16, reason="stale")
    assert err.value.code == "stale" and err.value.status == 409 and err.value.extra == {"revision": store.revision}
    with pytest.raises(ConfigError) as err:
        store.save({"rooms": [], "x": 1}, expected_revision=store.revision, reason="bad")
    assert err.value.code == "invalid" and err.value.status == 400 and err.value.problems
    assert json.loads((tmp_path / "config" / "config.json").read_text()) == document
    # A failing listener does not break a save; a removed one is not called.
    store.add_listener(lambda reason: 1 / 0)
    remove()
    store.save({"rooms": {}}, expected_revision=store.revision, reason="Delete room")
    assert seen == ["Add room"] and store.config == {"rooms": {}}
    # A new process reads the same document.
    again = make_store(tmp_path)
    assert again.config == store.config and again.revision == store.revision and again.saved_at == store.saved_at


def test_store_backups_are_dated_bounded_and_restorable(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(S, "BACKUPS", 5)
    store = make_store(tmp_path)
    for i in range(9):
        clock.advance(60)
        rooms = {f"room_{n}": {} for n in range(i + 1)}
        store.save({"rooms": rooms}, expected_revision=store.revision, reason=f"Step {i}!")
    backups = store.backups()
    assert len(backups) == 5 and len(list((tmp_path / "config" / "backups").iterdir())) == 5
    assert [b["rooms"] for b in backups] == [8, 7, 6, 5, 4]  # newest first: the documents before each save
    assert backups[0]["name"].endswith("-step_.json") and backups[0]["backed_up_at"] == "2026-11-22T12:09:00+00:00"
    assert all(S.BACKUP_NAME.match(b["name"]) and b["revision"] for b in backups)
    assert (tmp_path / "config" / "config.json.bak").exists()
    # A backup comes back as an export document and goes through the same validation as an import.
    document = store.backup_document(backups[-1]["name"])
    assert document["format"] == "smart_predictive_climate.config" and len(document["config"]["rooms"]) == 4
    assert len(store.parse_import(document)["rooms"]) == 4
    for bad in ("nope.json", "../config.json", None, 5, "2026-11-22T12-00-00-000000Z-x.json"):
        with pytest.raises(ConfigError) as err:
            store.backup_document(bad)
        assert err.value.code == "unknown_backup" and err.value.status == 404
    # A damaged backup is listed as such and cannot be restored.
    damaged = tmp_path / "config" / "backups" / backups[0]["name"]
    damaged.write_text("{not json")
    assert store.backups()[0] == {"name": damaged.name, "backed_up_at": None, "rooms": None, "revision": None,
                                  "damaged": True}
    with pytest.raises(ConfigError) as err:
        store.backup_document(damaged.name)
    assert err.value.code == "damaged_backup"


def test_store_export_and_import(tmp_path, clock):
    store = make_store(tmp_path)
    store.save({"rooms": {KAMAR: {"name": "Kamar"}}, "note": "x"}, expected_revision=store.revision, reason="seed")
    exported = store.export()
    assert exported == {"format": "smart_predictive_climate.config", "version": 1,
                        "exported_at": "2026-11-22T12:00:00+00:00", "revision": store.revision,
                        "config": store.config}
    exported["config"]["rooms"][KAMAR]["name"] = "changed"  # a copy: the store is not touched
    assert store.config["rooms"][KAMAR]["name"] == "Kamar"
    assert store.parse_import(json.loads(json.dumps(store.export()))) == store.config
    for bad, code in (({"format": "smart_occupancy_detection.config", "version": 1, "config": {}}, "invalid_import"),
                      ({"format": "smart_predictive_climate.config", "version": 2, "config": {}}, "invalid_import"),
                      ("text", "invalid_import"),
                      ({"format": "smart_predictive_climate.config", "version": 1, "config": {"x": 1}}, "invalid")):
        with pytest.raises(ConfigError) as err:
            store.parse_import(bad)
        assert err.value.code == code
    other = ConfigStore(tmp_path / "other", validate=stub_config_module().validate_config, empty=lambda: {"rooms": {}},
                        export_format="x.config", schema=3)
    assert other.export()["format"] == "x.config" and other.export()["version"] == 3


def test_store_recovers_from_a_damaged_file_without_overwriting_it(tmp_path, clock, caplog):
    store = make_store(tmp_path)
    store.save({"rooms": {KAMAR: {}}}, expected_revision=store.revision, reason="one")
    clock.advance(60)
    store.save({"rooms": {KAMAR: {}, STOVA: {}}}, expected_revision=store.revision, reason="two")
    path = tmp_path / "config" / "config.json"
    path.write_text('{"schema": 1, "revision": "broken", "config": {"rooms": {}}}')  # edited by hand: wrong revision
    recovered = make_store(tmp_path)
    assert recovered.config == {"rooms": {KAMAR: {}}}  # config.json.bak: the document before the last save
    assert recovered.recovery["used"] == "config.json.bak" and "revision" in recovered.recovery["errors"][0]
    assert not path.exists() and len(list((tmp_path / "config").glob("config.damaged-*.json"))) == 1
    assert "not usable" in caplog.text
    # The next save keeps a backup of the recovered configuration and clears the notice.
    recovered.save({"rooms": {}}, expected_revision=recovered.revision, reason="fixed")
    assert recovered.recovery is None and path.exists() and recovered.backups()[0]["rooms"] == 1
    # Everything damaged: start empty, keep the files, say so.
    for file in [path, path.with_name("config.json.bak"), *(tmp_path / "config" / "backups").iterdir()]:
        file.write_text("garbage")
    empty = make_store(tmp_path)
    assert empty.config == {"rooms": {}} and empty.recovery["used"] is None and len(empty.recovery["errors"]) >= 3
    assert (tmp_path / "config" / "backups").exists() and len(list((tmp_path / "config" / "backups").iterdir())) >= 2
    # A document of another schema or one the validation refuses is not used either.
    path.write_text(json.dumps({"schema": 2, "revision": "x", "config": {"rooms": {}}}))
    assert make_store(tmp_path).recovery["errors"][0].startswith("config.json: unsupported schema")


def test_atomic_write_and_time_helpers(tmp_path):
    target = tmp_path / "deep" / "file.json"
    S._atomic_write(target, {"a": "ð"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": "ð"} and not target.with_name("file.json.tmp").exists()
    assert S.utcnow().tzinfo is not None and S.iso(None) is None
    assert S.iso(S.utcnow().replace(microsecond=5)).endswith("+00:00")
    error = ConfigError("code", "Message", 418, problems=[{"path": "a", "message": "b"}], extra=1)
    assert (error.code, str(error), error.status, error.problems, error.extra) == \
        ("code", "Message", 418, [{"path": "a", "message": "b"}], {"extra": 1})


# =========================================================================== base core and web
@pytest.fixture
def settings(tmp_path):
    return load_settings({"SPC_DATA_DIR": str(tmp_path), "SPC_ALLOWED_CLIENTS": "127.0.0.1",
                          "SPC_OPTIONS": str(tmp_path / "none.json")})


@pytest.fixture
def core(settings, ha, stub_config):
    from spc.base import BaseCore

    core = BaseCore(settings)
    core.ha = ha
    core.link = CompanionLink(ha, settings.data_dir, app_version=APP_VERSION)
    ha.connect()
    return core


@pytest.fixture
async def client(aiohttp_client, settings, core, stub_api):
    from spc.web import build_app

    test_client = await aiohttp_client(build_app(settings, core))
    test_client.core = core
    return test_client


def test_settings_come_from_the_environment_and_hide_the_token(tmp_path):
    options = tmp_path / "options.json"
    options.write_text('{"log_level": "debug"}')
    settings = load_settings({"SUPERVISOR_TOKEN": "secret-token", "SPC_OPTIONS": str(options),
                              "SPC_DATA_DIR": str(tmp_path)})
    assert settings.log_level == "debug" and settings.port == 8099 and settings.dev_mode is False
    assert settings.ha_ws_url == "ws://supervisor/core/websocket" and settings.data_dir == tmp_path
    assert settings.is_allowed("172.30.32.2") and not settings.is_allowed("172.30.32.3")
    assert not settings.is_allowed(None) and settings.is_allowed("::ffff:172.30.32.2")
    assert "secret-token" not in repr(settings) and "secret-token" not in str(settings.public())
    assert settings.static_dir.name == "static" and (settings.static_dir / "index.html").exists()
    # Another App's variables mean nothing here.
    other = load_settings({"SOD_HA_WS_URL": "ws://elsewhere", "SOD_PORT": "1", "SPC_OPTIONS": str(tmp_path / "x")})
    assert other.ha_ws_url == "ws://supervisor/core/websocket" and other.port == 8099 and other.token is None


async def test_base_core_lifecycle(settings, stub_config, monkeypatch):
    from spc import base

    events = []

    class Core(base.BaseCore):
        async def on_start(self):
            events.append(("start", self.session is not None, self.ha is not None, self.link is not None,
                           self.ha._task is None))

        async def on_stop(self):
            events.append(("stop", self.session.closed))

    core = Core(settings, buffer=None)
    assert core.store.config == {"rooms": {}} and core.ha is None and core.link is None and core.session is None
    assert core.connection() == {"home_assistant": None, "companion": None,
                                 "versions": {"app": APP_VERSION, "home_assistant": None, "companion": None}}
    assert not await core.is_admin("admin")  # no connection: nobody is an administrator
    with pytest.raises(ConfigError) as err:
        core._require_ha()
    assert err.value.code == "not_connected" and err.value.status == 503
    await core.start()
    try:
        # on_start ran before anything connected (listeners can be registered in time); no token: no connection.
        assert events == [("start", True, True, True, True)]
        assert isinstance(core.ha, HA.HomeAssistantClient) and isinstance(core.link, CompanionLink)
        assert core.ha.state == "not_configured" and core.link.app_version == APP_VERSION
        connection = core.connection()
        assert connection["home_assistant"]["connected"] is False and connection["companion"]["state"] == "waiting"
        assert set(connection["versions"]) == {"app", "home_assistant", "companion"}
    finally:
        await core.stop()
    assert events[-1] == ("stop", False) and core.session.closed


async def test_is_admin_uses_the_user_list_and_caches_it(core, ha):
    assert await core.is_admin("admin") and not await core.is_admin("guest") and not await core.is_admin(None)
    assert not await core.is_admin("")
    ha.users.append({"id": "second", "is_active": True, "group_ids": ["system-admin"]})
    ha.users.append({"id": "inactive", "is_active": False, "is_owner": True})
    ha.users.append({"id": "system", "is_active": True, "group_ids": ["system-admin"], "system_generated": True})
    assert await core.is_admin("second")  # an unknown id is looked up again
    assert not await core.is_admin("inactive") and not await core.is_admin("system")
    sent = ha.commands_sent["config/auth/list"]
    assert await core.is_admin("admin") and ha.commands_sent["config/auth/list"] == sent  # cached
    assert core._require_ha() is ha
    assert core.connection()["home_assistant"]["connected"] is True


async def test_requests_from_outside_ingress_are_refused(aiohttp_client, tmp_path, core, stub_api):
    from spc.web import build_app

    settings = load_settings({"SPC_DATA_DIR": str(tmp_path), "SPC_ALLOWED_CLIENTS": "172.30.32.2",
                              "SPC_OPTIONS": str(tmp_path / "none.json")})
    test_client = await aiohttp_client(build_app(settings, core))
    for path in ("/api/health", "/", "/assets/icon.svg", "/api/export"):
        response = await test_client.get(path)
        assert response.status == 403 and "sidebar" in await response.text()
    assert (await test_client.post("/api/companion/pair", json={}, headers=HEADERS)).status == 403


async def test_index_assets_and_security_headers(client):
    response = await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    text = await response.text()
    assert response.status == 200 and response.content_type == "text/html"
    assert '<base href="/api/hassio_ingress/abc/">' in text and "__VERSION__" not in text
    assert f"assets/app.js?v={APP_VERSION}" in text and f"assets/app.css?v={APP_VERSION}" in text
    csp = response.headers["Content-Security-Policy"]
    for rule in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "frame-ancestors 'self'",
                 "form-action 'none'", "object-src 'none'", "base-uri 'self'"):
        assert rule in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff" and response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "same-origin"
    # A forged Ingress prefix cannot inject anything into the page.
    for forged in ('"><script>alert(1)</script>', "//evil.example", "/a b", "javascript:x"):
        text = await (await client.get("/index.html", headers={"X-Ingress-Path": forged})).text()
        assert '<base href="/">' in text and "evil" not in text and "alert(1)" not in text
    icon = await client.get("/assets/icon.svg")
    assert icon.status == 200 and icon.content_type == "image/svg+xml" and icon.headers["Cache-Control"] == "no-cache"
    assert "Content-Security-Policy" in icon.headers
    for missing in ("/assets/../store.py", "/assets/%2e%2e%2fstore.py", "/assets/index.html", "/assets/nothing.js",
                    "/assets/.hidden.css", "/assets/ICON.svg", "/store.py", "/static/index.html"):
        assert (await client.get(missing)).status == 404, missing
    health = await client.get("/api/health")
    assert await health.json() == {"ok": True, "version": APP_VERSION}
    # API errors are JSON, also the ones aiohttp raises.
    missing = await client.get("/api/nothing")
    assert missing.status == 404 and (await missing.json())["error"] == "Not Found"
    assert (await client.post("/api/health")).status == 405


async def test_assets_are_served_by_type(client, settings, tmp_path, monkeypatch):
    static = tmp_path / "static"
    static.mkdir()
    for name, content in (("app.js", "export {};"), ("app.css", "body{}"), ("logo.png", "\x89PNG"),
                          ("index.html", "<html><head></head></html>"), ("notes.txt", "x"), ("data.json", "{}")):
        (static / name).write_text(content)
    object.__setattr__(settings, "static_dir", static)
    assert (await client.get("/assets/app.js")).content_type == "text/javascript"
    assert (await client.get("/assets/app.css")).content_type == "text/css"
    assert (await client.get("/assets/logo.png")).content_type == "image/png"
    for refused in ("notes.txt", "data.json", "index.html"):
        assert (await client.get(f"/assets/{refused}")).status == 404


async def test_the_api_module_gets_the_frame(client, stub_api):
    call = stub_api.calls[0]
    assert call["core"] is client.core and set(call) == {"router", "core", "body", "require_admin", "json"}
    assert isinstance(call["router"], web.UrlDispatcher)
    response = call["json"]({"a": "ð"}, 201, {"X-Test": "1"})
    assert response.status == 201 and response.content_type == "application/json" and response.text == '{"a": "ð"}'
    ok = await client.post("/api/echo", json={"a": 1}, headers=HEADERS)
    assert ok.status == 200 and await ok.json() == {"data": {"a": 1}, "user": "admin"}
    assert "Content-Security-Policy" in ok.headers  # the frame's headers are on the API module's answers too


async def test_changes_need_the_csrf_header_json_same_origin_and_an_administrator(client, ha):
    async def post(headers, **kwargs):
        response = await client.post("/api/echo", headers=headers, **(kwargs or {"json": {"a": 1}}))
        return response.status, (await response.json()).get("error")

    assert await post(HEADERS) == (200, None)
    assert await post({"X-Remote-User-Id": "admin"}) == (403, "forbidden")           # no X-SPC header
    assert await post({**HEADERS, "X-SPC": "0"}) == (403, "forbidden")
    assert await post({"X-SOD": "1", "X-Remote-User-Id": "admin"}) == (403, "forbidden")  # another App's header
    assert await post(HEADERS, data="a=1") == (403, "forbidden")                    # a form cannot post
    assert await post({**HEADERS, "Content-Type": "text/plain"}, data='{"a": 1}') == (403, "forbidden")
    assert await post({**HEADERS, "X-Remote-User-Id": "guest"}) == (403, "forbidden")
    assert await post({**HEADERS, "X-Remote-User-Id": "nobody"}) == (403, "forbidden")
    assert await post({"X-SPC": "1"}) == (403, "forbidden")                         # no user at all
    cross = {**HEADERS, "Origin": "https://evil.example", "Host": "ha.local:8123"}
    assert await post(cross) == (403, "forbidden")
    assert await post({**HEADERS, "Origin": "null"}) == (403, "forbidden")
    same = {**HEADERS, "Origin": "http://ha.local:8123", "Host": "ha.local:8123"}
    assert await post(same) == (200, None)
    proxied = {**HEADERS, "Origin": "https://home.example", "X-Forwarded-Host": "home.example:443"}
    assert await post(proxied) == (200, None)
    # The user list cannot be read: fail closed.
    client.core._admins = None
    ha.errors["config/auth/list"] = HA.HomeAssistantError("unknown_error", "boom")
    assert await post(HEADERS) == (403, "forbidden")
    del ha.errors["config/auth/list"]
    ha.connected = False
    assert await post(HEADERS) == (403, "forbidden")
    ha.connected = True
    # Bodies: JSON objects only.
    assert await post({**HEADERS, "Content-Type": "application/json"}, data="{not json") == (400, "invalid")
    assert await post({**HEADERS, "Content-Type": "application/json"}, data="[1, 2]") == (400, "invalid")
    assert await post({**HEADERS, "Content-Type": "application/json"}, data="") == (200, None)


async def test_body_size_limits(client):
    from spc import web as W

    big = {"blob": "x" * (W.MAX_BODY + 10)}
    response = await client.post("/api/echo", json=big, headers=HEADERS)
    assert response.status == 413 and (await response.json())["error"] == "too_large"
    # A handler may ask for a larger limit (imports): the whole body arrives.
    response = await client.post("/api/big", json=big, headers=HEADERS)
    assert response.status == 200 and await response.json() == {"size": W.MAX_BODY + 10, "user": "admin"}
    response = await client.post("/api/big", json={"blob": "x" * (2 * 1024 * 1024 + 10)}, headers=HEADERS)
    assert response.status == 413
    # Without a Content-Length (chunked) the limit holds as well.
    async def chunks():
        yield b'{"blob": "'
        for _ in range(W.MAX_BODY // 65536 + 2):
            yield b"x" * 65536
        yield b'"}'

    response = await client.post("/api/echo", data=chunks(), headers={**HEADERS, "Content-Type": "application/json"})
    assert response.status == 413


async def test_require_admin_for_reads(client, settings):
    assert await (await client.get("/api/secret", headers={"X-Remote-User-Id": "admin"})).json() == {"user": "admin"}
    for headers in ({"X-Remote-User-Id": "guest"}, {}):
        response = await client.get("/api/secret", headers=headers)
        assert response.status == 403 and (await response.json())["error"] == "forbidden"
    # Development mode (never on Home Assistant OS): a request without a user id is accepted, a known one checked.
    object.__setattr__(settings, "dev_mode", True)
    assert await (await client.get("/api/secret")).json() == {"user": None}
    assert (await client.get("/api/secret", headers={"X-Remote-User-Id": "guest"})).status == 403
    assert (await client.post("/api/echo", json={}, headers={"X-SPC": "1"})).status == 200
    assert (await client.post("/api/echo", json={})).status == 403  # the CSRF header is still needed


async def test_errors_of_handlers_become_json(client):
    response = await client.get("/api/fail")
    assert response.status == 409 and await response.json() == {
        "error": "stale", "message": "Changed meanwhile", "problems": [{"path": "x", "message": "y"}],
        "revision": "abc"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    response = await client.get("/api/fail?kind=link")
    assert response.status == 409 and await response.json() == {"error": "busy", "message": "The companion is busy"}


async def test_export_is_a_download(client):
    core = client.core
    core.store.save({"rooms": {KAMAR: {"name": "Kamar"}}}, expected_revision=core.store.revision, reason="seed")
    response = await client.get("/api/export")
    assert response.headers["Content-Disposition"] == 'attachment; filename="smart-predictive-climate-config.json"'
    document = await response.json()
    assert document["format"] == "smart_predictive_climate.config" and document["config"] == core.store.config
    assert core.store.parse_import(document) == core.store.config


async def test_pairing_goes_through_the_link(client, ha, fast):
    core = client.core
    link, core.link = core.link, None
    response = await client.post("/api/companion/pair", json={}, headers=HEADERS)
    assert response.status == 503 and (await response.json())["error"] == "not_ready"
    core.link = link
    await link.start()
    await wait_for(lambda: link.state == "not_paired")
    assert (await client.post("/api/companion/pair", json={}, headers={**HEADERS, "X-Remote-User-Id": "guest"})).status == 403
    assert (await client.post("/api/companion/pair", json={}, headers={"X-Remote-User-Id": "admin"})).status == 403
    assert ha.companion.pairing_hash is None
    response = await client.post("/api/companion/pair", json={}, headers=HEADERS)
    assert response.status == 200 and await response.json() == {"paired": True, "state": "publishing"}
    assert ha.companion.requested_by == "admin"
    # The companion's refusal reaches the interface as a JSON error.
    ha.companion.pairing_hash = "0" * 64  # paired with another App meanwhile
    ha.disconnect()
    ha.connect()
    await wait_for(lambda: link.state == "pair_mismatch")
    response = await client.post("/api/companion/pair", json={}, headers=HEADERS)
    assert response.status == 409 and (await response.json())["error"] == "already_paired"
    response = await client.post("/api/companion/pair", json={"replace": True}, headers=HEADERS)
    assert response.status == 200
    response = await client.post("/api/companion/unpair", json={}, headers=HEADERS)
    assert response.status == 200 and await response.json() == {"paired": False}
    assert ha.companion.pairing_hash is None
    await link.stop()


# =========================================================================== companion link
@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(PUB, "PUBLISH_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RECLAIM_DELAY", 0.01)
    monkeypatch.setattr(PUB, "RETRY_SECONDS", 0.05)
    monkeypatch.setattr(PUB.P, "HEARTBEAT_SECONDS", 0.05)


async def paired_link(tmp_path, ha, **kwargs) -> CompanionLink:
    link = CompanionLink(ha, tmp_path, app_version="0.1.0", **kwargs)
    await link.start()
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, requested_by="admin")
    return link


async def test_nothing_is_published_before_an_administrator_pairs(tmp_path, ha, fast):
    link = CompanionLink(ha, tmp_path, app_version="0.1.0")
    link.publish(snapshot(KAMAR))  # before start and before any connection: kept for later
    await link.start()
    assert link.status()["state"] == "waiting" and ha.calls == []
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await asyncio.sleep(0.1)
    assert ha.companion.publishes == 0 and ha.commands_sent[f"{P.DOMAIN}/claim"] == 0
    assert ha.subscriptions(f"{P.DOMAIN}/session") == [
        {"protocol": 1, "app_version": "0.1.0", "instance_id": link.instance_id}]
    assert await link.pair(replace=False, requested_by="admin") == {"paired": True, "state": "publishing"}
    await wait_for(lambda: ha.companion.publishes == 1)
    assert ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 21.4 and ha.companion.fresh
    token_file = tmp_path / "companion.json"
    assert stat.S_IMODE(os.stat(token_file).st_mode) == 0o600
    assert P.valid_id(json.loads(token_file.read_text())["pairing_token"])
    # A new snapshot is published; several in a row are coalesced into the newest.
    for value in (20.0, 20.5, 21.0):
        link.publish(snapshot(KAMAR, value=value))
    await wait_for(lambda: ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 21.0)
    assert ha.companion.publishes == 2
    # Nothing changed: no new snapshot (also when the same one is handed over again), but heartbeats keep the lease.
    link.publish(snapshot(KAMAR, value=21.0))
    await asyncio.sleep(0.2)
    assert ha.companion.publishes == 2 and ha.companion.heartbeats >= 1 and link.counters["heartbeats"] >= 1
    assert ha.companion.refused == {}
    status = link.status()
    assert {"state", "detail", "paired", "trusted", "has_token", "paired_at", "epoch", "publishing",
            "last_publish_at", "last_error", "companion", "counters", "last_ok_at", "errors", "compatibility",
            "companion_version", "companion_view", "instance", "has_snapshot", "rooms"} == set(status)
    assert status["state"] == "publishing" and status["publishing"] and status["paired"] and status["has_token"]
    assert status["epoch"] == 1 and status["rooms"] == 1 and status["companion_version"] == "0.1.0"
    assert status["compatibility"]["level"] == "ok" and status["counters"]["publishes"] == 2
    assert json.dumps(status) and link.token not in json.dumps(status)  # JSON-safe, and no secret in it
    await link.stop()
    assert ha.companion.lease is None  # released at shutdown
    assert not ha.companion.fresh


async def test_the_caller_cannot_change_a_handed_over_snapshot(tmp_path, ha, fast):
    link = await paired_link(tmp_path, ha)
    mine = snapshot(KAMAR)
    link.publish(mine)
    mine["rooms"][KAMAR]["entities"]["forecast_1h"]["value"] = "broken"  # after the hand-over: must not matter
    await wait_for(lambda: ha.companion.publishes == 1)
    assert ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 21.4
    await link.stop()


async def test_an_invalid_snapshot_is_never_sent(tmp_path, ha, fast, caplog):
    link = await paired_link(tmp_path, ha)
    link.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.publishes == 1)
    sent = ha.commands_sent[f"{P.DOMAIN}/publish"]
    for bad in ({"rooms": {KAMAR: {"name": "Kamar", "entities": {"x": entity("switch", "Heater", True)}}}},
                {"rooms": {"Bad Id": {"name": "x", "entities": {}}}},
                {"rooms": {KAMAR: {"name": "Kamar", "entities": {"t": entity(value="warm")}}}},
                {"rooms": []}, "nothing", None):
        link.publish(bad)
        await asyncio.sleep(0.05)
    assert ha.commands_sent[f"{P.DOMAIN}/publish"] == sent and ha.companion.refused == {}
    assert "Not publishing" in caplog.text and link.status()["last_error"]
    assert ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 21.4  # the last valid one stays
    link.publish(snapshot(KAMAR, value=19.0))  # and the link recovers with the next valid one
    await wait_for(lambda: ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 19.0)
    await link.stop()


async def test_deleted_rooms_are_sent_once_and_only_when_they_left_the_snapshot(tmp_path, ha, fast):
    link = await paired_link(tmp_path, ha)
    link.publish(snapshot(KAMAR, STOVA))
    await wait_for(lambda: ha.companion.publishes == 1)
    # Missing from a snapshot: orphaned (kept), not deleted.
    link.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.publishes == 2)
    assert ha.companion.deleted == [] and set(ha.companion.orphaned) == {STOVA}
    # Deleted in the App while the room is still in the last snapshot: waits for the snapshot without it.
    link.publish(snapshot(KAMAR, STOVA))
    await wait_for(lambda: ha.companion.publishes == 3)
    link.mark_deleted(STOVA)
    link.mark_deleted("Not A Room")  # ignored
    await asyncio.sleep(0.1)
    assert ha.companion.deleted == [] and ha.companion.publishes == 3 and link.deleted == {STOVA}
    link.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.deleted == [STOVA])
    assert link.deleted == set() and ha.companion.orphaned == {}
    # Not repeated with the next snapshots; a list of ids and a "deleted" key in the snapshot work as well.
    link.publish(snapshot(KAMAR, value=20.0))
    await wait_for(lambda: ha.companion.publishes == 5)
    assert ha.companion.deleted == [STOVA]
    link.mark_deleted(["gongd"])
    await wait_for(lambda: ha.companion.deleted == [STOVA, "gongd"])
    link.publish({"rooms": {}, "deleted": [KAMAR]})
    await wait_for(lambda: ha.companion.deleted == [STOVA, "gongd", KAMAR])
    assert ha.companion.refused == {}
    await link.stop()


async def test_a_lost_lease_is_claimed_again_and_the_snapshot_is_resent(tmp_path, ha, fast):
    link = await paired_link(tmp_path, ha)
    link.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.publishes == 1)
    epoch = ha.companion.epoch
    ha.companion.expire_lease()
    assert not ha.companion.fresh
    # The next heartbeat is refused: a new claim (a new epoch), and the same snapshot again without being asked.
    await wait_for(lambda: ha.companion.epoch == epoch + 1 and ha.companion.publishes == 2)
    assert link.counters["refused"] >= 1 and link.state == "publishing" and ha.companion.fresh
    assert link.counters["claims"] == 2 and link.status()["errors"][0]["code"] == P.REJECT_NOT_OWNER
    # The connection drops: publishing stops at once; nothing is re-subscribed for the link, it opens a new session.
    ha.disconnect()
    assert link.state == "waiting" and not link.status()["publishing"]
    link.publish(snapshot(KAMAR, value=18.0))  # while away: kept
    await asyncio.sleep(0.05)
    assert ha.companion.publishes == 2
    ha.connect()
    await wait_for(lambda: ha.companion.publishes == 3)
    assert ha.companion.rooms[KAMAR]["entities"]["forecast_1h"]["value"] == 18.0 and ha.companion.epoch == epoch + 2
    await link.stop()


async def test_a_new_app_process_resumes_with_the_stored_pairing(tmp_path, ha, fast):
    link = await paired_link(tmp_path, ha)
    await wait_for(lambda: link.state == "publishing")
    await link.stop()
    ha.disconnect()
    link2 = CompanionLink(ha, tmp_path, app_version="0.1.0")
    await link2.start()
    assert link2.instance_id != link.instance_id and link2.token == link.token
    ha.connect()
    await wait_for(lambda: link2.state == "publishing")  # no human needed after a restart
    assert not ha.companion.fresh  # ...but nothing is shown until this process has a snapshot
    link2.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.fresh)
    await link2.stop()


async def test_integration_missing_not_set_up_incompatible_or_paired_elsewhere(tmp_path, ha, fast):
    ha.companion.installed = False
    link = CompanionLink(ha, tmp_path, app_version="0.1.0")
    await link.start()
    ha.connect()
    await wait_for(lambda: link.state == "not_installed")
    with pytest.raises(LinkError) as err:
        await link.pair(replace=False, requested_by="admin")
    assert err.value.code == "not_installed" and err.value.status == 409
    ha.companion.installed = True
    ha.companion.loaded = False
    await wait_for(lambda: link.state == "not_set_up")
    assert "Add integration" in link.detail
    ha.companion.loaded = True
    ha.companion.protocol = 2
    await wait_for(lambda: link.state == "incompatible")
    assert "update the App" in link.detail and ha.subscriptions() == []
    ha.companion.protocol = 1
    await wait_for(lambda: link.state == "not_paired")
    # Another App installation pairs first: this one must ask before replacing it.
    ha.companion.pairing_hash = "0" * 64
    ha.disconnect()
    ha.connect()
    await wait_for(lambda: link.state == "pair_mismatch")
    with pytest.raises(LinkError) as err:
        await link.pair(replace=False, requested_by="admin")
    assert err.value.code == "already_paired"
    await link.pair(replace=True, requested_by="admin")
    await wait_for(lambda: link.state == "publishing")
    assert await link.unpair(requested_by="admin") == {"paired": False}
    assert link.state == "not_paired" and not (tmp_path / "companion.json").exists()
    assert ha.companion.pairing_hash is None and not link.status()["publishing"]
    await link.stop()


async def test_pairs_again_by_itself_only_into_a_fresh_store(tmp_path, ha, fast):
    link = await paired_link(tmp_path, ha)
    link.publish(snapshot(KAMAR))
    await wait_for(lambda: ha.companion.publishes == 1)
    # The integration was removed and added again: a store that was never paired.
    old = ha.companion
    ha.disconnect()
    ha.companion = type(old)()
    ha.connect()
    await wait_for(lambda: ha.companion.publishes == 1)
    assert ha.companion.automatic is True and link.counters["auto_pairs"] == 1 and link.state == "publishing"
    # After an explicit unpair somewhere else (the store remembers): never by itself.
    ha.disconnect()
    ha.companion.pairing_hash = None
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await asyncio.sleep(0.1)
    assert link.counters["auto_pairs"] == 1 and ha.companion.pairing_hash is None
    await link.stop()


async def test_can_publish_blocks_the_claim(tmp_path, ha, fast):
    reason = ["Still learning"]
    link = CompanionLink(ha, tmp_path, app_version="0.1.0", can_publish=lambda: reason[0])
    await link.start()
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    link.publish(snapshot(KAMAR))
    await link.pair(replace=False, requested_by="admin")
    await settle()
    assert link.state == "blocked" and link.detail == "Still learning" and ha.companion.publishes == 0
    reason[0] = None
    await wait_for(lambda: ha.companion.publishes == 1)  # the claim is retried
    await link.stop()


async def test_listeners_hear_state_changes(tmp_path, ha, fast):
    link = CompanionLink(ha, tmp_path, app_version="0.1.0")
    states = []
    link.add_listener(lambda: states.append(link.state))
    link.add_listener(lambda: 1 / 0)  # a failing listener breaks nothing
    await link.start()
    ha.connect()
    await wait_for(lambda: link.state == "not_paired")
    await link.pair(replace=False, requested_by=None)
    await wait_for(lambda: link.state == "publishing")
    assert states == ["not_paired", "publishing"]
    await link.stop()


# =========================================================================== the Home Assistant client
class FakeCore:
    """A websocket server that speaks enough of Home Assistant's protocol for the real client."""

    def __init__(self) -> None:
        self.received: list[dict] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.token = "token"
        self.silent: set[str] = set()

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.1"})
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            msg = json.loads(message.data)
            self.received.append(msg)
            kind = msg["type"]
            if kind in self.silent:
                continue
            if kind == "ping":
                await ws.send_json({"id": msg["id"], "type": "pong"})
                continue
            result = {"get_config": {"state": "RUNNING", "version": "2026.11.1"},
                      "get_states": [{"entity_id": "sensor.t", "state": "20.5", "attributes": {}}],
                      "config/area_registry/list": [{"area_id": KAMAR, "name": "Kamar Suður"}],
                      "config/device_registry/list": [{"id": "d1"}],
                      "config/entity_registry/list": [{"entity_id": "sensor.t", "area_id": KAMAR}],
                      "history/history_during_period": {"sensor.t": [{"s": "20.5", "lu": 1.0}]},
                      "recorder/statistics_during_period": {"sensor.t": [{"start": 0, "mean": 20.0}]},
                      "recorder/list_statistic_ids": [{"statistic_id": "sensor.t"}]}.get(kind)
            if kind == "call_service":
                await ws.send_json({"id": msg["id"], "type": "result", "success": False,
                                    "error": {"code": "should_never_arrive", "message": "x"}})
                continue
            if kind == "manifest/get":
                await ws.send_json({"id": msg["id"], "type": "result", "success": False,
                                    "error": {"code": "not_found", "message": "Integration not found"}})
                continue
            await ws.send_json({"id": msg["id"], "type": "result", "success": True, "result": result})
            if kind == "weather/subscribe_forecast":
                await ws.send_json({"id": msg["id"], "type": "event",
                                    "event": {"type": msg["forecast_type"], "forecast": [{"temperature": 4.0}]}})
        return ws

    def types(self) -> list[str]:
        return [m["type"] for m in self.received]

    async def event(self, sub_type: str, event: dict, **match) -> None:
        sub = next(m for m in reversed(self.received) if m["type"] == sub_type
                   and all(m.get(k) == v for k, v in match.items()))
        await self.sockets[-1].send_json({"id": sub["id"], "type": "event", "event": event})


@pytest.fixture
async def fake_core(aiohttp_server):
    core = FakeCore()
    app = web.Application()
    app.router.add_get("/core/websocket", core.handler)
    server = await aiohttp_server(app)
    core.url = f"ws://127.0.0.1:{server.port}/core/websocket"
    return core


@pytest.fixture
async def ha_client(fake_core, monkeypatch):
    monkeypatch.setattr(HA, "RECONNECT_DELAYS", (0.05,))
    monkeypatch.setattr(HA, "REGISTRY_DEBOUNCE", 0.01)
    async with aiohttp.ClientSession() as session:
        client = HA.HomeAssistantClient(session, fake_core.url, "token")
        await client.start()
        await wait_for(lambda: client.connected)
        yield client
        await client.stop()


async def test_client_loads_states_and_registries_and_follows_changes(ha_client, fake_core):
    client = ha_client
    assert client.ha_version == "2026.11.1" and client.config["state"] == "RUNNING"
    assert client.states["sensor.t"]["state"] == "20.5" and client.areas[0]["area_id"] == KAMAR
    assert client.devices == [{"id": "d1"}] and client.entity_registry[0]["entity_id"] == "sensor.t"
    status = client.status()
    assert status["state"] == "connected" and status["connected"] and status["ha_version"] == "2026.11.1"
    assert {"connected_since", "last_disconnect", "last_error", "reconnects", "latency_ms", "events_total",
            "history"} <= set(status)
    # Only the five allowed event types were subscribed, before the snapshot was read.
    subscribed = [m["event_type"] for m in fake_core.received if m["type"] == "subscribe_events"]
    assert set(subscribed) == HA.ALLOWED_EVENT_TYPES and len(subscribed) == 5
    assert fake_core.types().index("get_states") > fake_core.types().index("subscribe_events")
    seen = []
    remove = client.add_listener(lambda kind, payload: seen.append((kind, payload)))
    new = {"entity_id": "sensor.t", "state": "21.0", "attributes": {}}
    await fake_core.event("subscribe_events", {"data": {"entity_id": "sensor.t", "new_state": new}},
                          event_type="state_changed")
    await wait_for(lambda: client.states["sensor.t"]["state"] == "21.0")
    assert ("state", ("sensor.t", new)) in seen
    await fake_core.event("subscribe_events", {"data": {"entity_id": "sensor.t", "new_state": None}},
                          event_type="state_changed")
    await wait_for(lambda: "sensor.t" not in client.states)
    reads = fake_core.types().count("config/area_registry/list")
    await fake_core.event("subscribe_events", {"data": {}}, event_type="area_registry_updated")
    await wait_for(lambda: ("registries", None) in seen)
    assert fake_core.types().count("config/area_registry/list") == reads + 1
    remove()
    assert set(client.commands_sent) <= HA.ALLOWED_COMMANDS and set(fake_core.types()) <= HA.ALLOWED_COMMANDS


async def test_client_reads_history_and_statistics(ha_client, fake_core):
    history = await ha_client.command("history/history_during_period", start_time="2026-11-01T00:00:00+00:00",
                                      end_time="2026-11-22T00:00:00+00:00", entity_ids=["sensor.t"],
                                      minimal_response=True, no_attributes=True)
    assert history == {"sensor.t": [{"s": "20.5", "lu": 1.0}]}
    sent = next(m for m in fake_core.received if m["type"] == "history/history_during_period")
    assert sent["entity_ids"] == ["sensor.t"] and sent["minimal_response"] is True
    statistics = await ha_client.command("recorder/statistics_during_period", start_time="2026-11-01T00:00:00+00:00",
                                         statistic_ids=["sensor.t"], period="hour", types=["mean"])
    assert statistics == {"sensor.t": [{"start": 0, "mean": 20.0}]}
    assert await ha_client.command("recorder/list_statistic_ids") == [{"statistic_id": "sensor.t"}]
    with pytest.raises(HA.HomeAssistantError) as err:
        await ha_client.command("manifest/get", integration="nothing")
    assert err.value.code == "not_found" and err.value.message == "Integration not found"
    assert HA.SLOW_COMMANDS <= HA.READ_COMMANDS and HA.SLOW_COMMAND_TIMEOUT > HA.COMMAND_TIMEOUT


async def test_client_refuses_everything_else_before_it_is_sent(ha_client, fake_core):
    before = len(fake_core.received)
    for command, params in (("call_service", {"domain": "climate", "service": "set_temperature",
                                              "service_data": {"temperature": 30}}),
                            ("fire_event", {"event_type": "x"}),
                            ("execute_script", {"sequence": []}),
                            ("config/entity_registry/update", {"entity_id": "sensor.t", "name": "x"}),
                            ("config/area_registry/delete", {"area_id": KAMAR}),
                            ("blueprint/save", {}), ("render_template", {"template": "x"}),
                            ("history/stream", {}), ("recorder/clear_statistics", {"statistic_ids": ["sensor.t"]}),
                            ("subscribe_events", {"event_type": "call_service"}),
                            ("subscribe_events", {"event_type": None}),
                            ("smart_occupancy_detection/publish", {}),
                            ("weather/subscribe_forecast", {"forecast_type": "hourly", "entity_id": "weather.home"})):
        with pytest.raises(HA.NotAllowed):
            await ha_client.command(command, **params)
    await asyncio.sleep(0.05)
    assert len(fake_core.received) == before and "call_service" not in ha_client.commands_sent


async def test_forecast_subscription_guard(ha_client, fake_core):
    events = []
    before = len(fake_core.received)
    for params in ({"forecast_type": "twice_daily", "entity_id": "weather.home"},
                   {"forecast_type": "hourly", "entity_id": "climate.living_room"},
                   {"forecast_type": "hourly", "entity_id": "sensor.weather.home"},
                   {"forecast_type": "hourly", "entity_id": ["weather.home"]},
                   {"forecast_type": "hourly", "entity_id": None},
                   {"forecast_type": "hourly"}, {"entity_id": "weather.home"}, {},
                   {"forecast_type": "hourly", "entity_id": "weather.home", "service": "climate.set_temperature"},
                   {"forecast_type": ["hourly"], "entity_id": "weather.home"}):
        with pytest.raises(HA.NotAllowed):
            await ha_client.subscribe("weather/subscribe_forecast", params, events.append)
    for command in ("subscribe_trigger", "subscribe_events", "call_service", "history/stream",
                    "smart_occupancy_detection/session", "weather/subscribe_forecasts"):
        with pytest.raises(HA.NotAllowed):
            await ha_client.subscribe(command, {"forecast_type": "hourly", "entity_id": "weather.home"}, events.append)
    await asyncio.sleep(0.05)
    assert len(fake_core.received) == before and events == []
    # The two allowed forms.
    unsubscribe = await ha_client.subscribe("weather/subscribe_forecast",
                                            {"forecast_type": "hourly", "entity_id": "weather.home"}, events.append)
    daily = []
    await ha_client.subscribe("weather/subscribe_forecast", {"entity_id": "weather.home", "forecast_type": "daily"},
                              daily.append)
    await wait_for(lambda: events and daily)
    assert events == [{"type": "hourly", "forecast": [{"temperature": 4.0}]}] and daily[0]["type"] == "daily"
    sent = [m for m in fake_core.received if m["type"] == "weather/subscribe_forecast"]
    assert [set(m) for m in sent] == [{"id", "type", "forecast_type", "entity_id"}] * 2
    await fake_core.event("weather/subscribe_forecast", {"type": "hourly", "forecast": None}, forecast_type="hourly")
    await wait_for(lambda: len(events) == 2)
    # Unsubscribing ends it at Home Assistant and here; doing it twice is harmless.
    await unsubscribe()
    await unsubscribe()
    assert [m["subscription"] for m in fake_core.received if m["type"] == "unsubscribe_events"] == [sent[0]["id"]]
    await fake_core.event("weather/subscribe_forecast", {"type": "hourly", "forecast": []}, forecast_type="hourly")
    await asyncio.sleep(0.05)
    assert len(events) == 2


async def test_subscriptions_are_not_reopened_after_a_reconnect(ha_client, fake_core):
    events, statuses = [], []
    ha_client.add_listener(lambda kind, payload: statuses.append(payload["state"]) if kind == "status" else None)
    unsubscribe = await ha_client.subscribe("weather/subscribe_forecast",
                                            {"forecast_type": "hourly", "entity_id": "weather.home"}, events.append)
    await wait_for(lambda: len(events) == 1)
    pending = asyncio.ensure_future(ha_client.command("get_config"))
    fake_core.silent.add("get_config")
    await asyncio.sleep(0.02)
    await fake_core.sockets[-1].close()
    with pytest.raises(ConnectionError):
        await pending  # a command in flight fails; it is not repeated
    fake_core.silent.clear()
    await wait_for(lambda: statuses[-2:] == ["connecting", "connected"] and len(fake_core.sockets) == 2)
    assert "disconnected" in statuses and ha_client.status()["reconnects"] == 1
    # The client's own event subscriptions are back; the caller's forecast subscription is not.
    assert [m["type"] for m in fake_core.received].count("weather/subscribe_forecast") == 1
    assert [m["type"] for m in fake_core.received].count("subscribe_events") == 10
    await unsubscribe()  # the old subscription is gone with its connection: nothing is sent
    assert "unsubscribe_events" not in fake_core.types()
    # The caller subscribes again on "connected" (what the status listener is for).
    await ha_client.subscribe("weather/subscribe_forecast", {"forecast_type": "hourly", "entity_id": "weather.home"},
                              events.append)
    await wait_for(lambda: len(events) == 2)
    with pytest.raises(ConnectionError):
        await ha_client.stop()
        await ha_client.command("get_states")


async def test_client_without_or_with_a_wrong_token(fake_core, monkeypatch):
    monkeypatch.setattr(HA, "AUTH_FAILED_DELAY", 0.05)
    async with aiohttp.ClientSession() as session:
        none = HA.HomeAssistantClient(session, fake_core.url, None)
        await none.start()
        assert none.state == "not_configured" and "SUPERVISOR_TOKEN" in none.last_error and not none.connected
        with pytest.raises(ConnectionError):
            await none.command("get_states")
        wrong = HA.HomeAssistantClient(session, fake_core.url, "wrong")
        await wrong.start()
        await wait_for(lambda: wrong.state == "auth_failed")
        assert "Invalid access token" in wrong.last_error and "wrong" not in json.dumps(wrong.status())
        await wrong.stop()
        await none.stop()


def test_fake_ha_enforces_the_same_allowlist():
    """The fake used by the App tests refuses what the real client refuses."""
    fake = FakeHA()
    fake.connect()

    async def run():
        for command in ("call_service", "fire_event", "config/entity_registry/update", f"{P.DOMAIN}/session"):
            with pytest.raises(HA.NotAllowed):
                await fake.command(command)
        with pytest.raises(HA.NotAllowed):
            await fake.command("subscribe_events", event_type="call_service")
        with pytest.raises(HA.NotAllowed):
            await fake.subscribe("weather/subscribe_forecast", {"forecast_type": "hourly", "entity_id": "light.x"},
                                 print)
        with pytest.raises(HA.NotAllowed):
            await fake.subscribe("subscribe_trigger", {}, print)
        fake.history["sensor.t"] = [{"s": "20.5", "lu": 1.0}]
        fake.statistics["sensor.t"] = [{"start": 0, "mean": 20.0}]
        fake.forecasts["weather.home"] = [{"temperature": 4.0}]
        assert await fake.command("history/history_during_period", entity_ids=["sensor.t", "sensor.none"]) == \
            {"sensor.t": [{"s": "20.5", "lu": 1.0}]}
        assert await fake.command("recorder/statistics_during_period", statistic_ids=["sensor.t"]) == \
            {"sensor.t": [{"start": 0, "mean": 20.0}]}
        assert [s["statistic_id"] for s in await fake.command("recorder/list_statistic_ids")] == ["sensor.t"]
        events = []
        unsubscribe = await fake.subscribe("weather/subscribe_forecast",
                                           {"forecast_type": "hourly", "entity_id": "weather.home"}, events.append)
        await settle()
        fake.push_forecast("weather.home", [{"temperature": 5.0}])
        assert [e["forecast"][0]["temperature"] for e in events] == [4.0, 5.0]
        assert fake.subscriptions("weather/subscribe_forecast") == [
            {"forecast_type": "hourly", "entity_id": "weather.home"}]
        await unsubscribe()
        fake.push_forecast("weather.home", [])
        assert len(events) == 2
        fake.errors["get_states"] = HA.HomeAssistantError("unknown_error", "boom")
        with pytest.raises(HA.HomeAssistantError):
            await fake.command("get_states")
        fake.disconnect()
        with pytest.raises(ConnectionError):
            await fake.command("get_config")
        assert ("history/history_during_period", {"entity_ids": ["sensor.t", "sensor.none"]}) in fake.calls

    asyncio.run(run())
