"""Floor plan (Phase A2.1): storage, validation, recovery, migrations, images, export/import, security, context."""

from __future__ import annotations

import base64
import copy
import json
import os
from pathlib import Path
import struct
import time
from typing import Any
import zlib

from aiohttp import web
import pytest

from smart_lighting import floorplan_model as fm
from smart_lighting.core import AppCore
from smart_lighting.floorplan import FloorPlanService
from smart_lighting.ha import READ_ONLY_COMMANDS, SUBSCRIPTION_COMMANDS
from smart_lighting.rooms import live_attributes
from smart_lighting.web import build_app

from .conftest import ADMIN_HEADERS, TOKEN, USER_ID, FakeHomeAssistant, make_settings, wait_for


# ------------------------------------------------------------------ helpers
def png(width: int = 64, height: int = 48, colour: int = 200) -> bytes:
    """A real (decodable) grey PNG."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes([colour]) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


class FloorBuilder:
    """Builds a floor from rectangles, sharing corners and splitting walls where rooms meet (like the editor)."""

    def __init__(self, floor_id: str, name: str, level: int = 0) -> None:
        self.floor: dict[str, Any] = {"id": floor_id, "name": name, "level": level, "ha_floor_id": None,
                                      "measured": True, "vertices": [], "rooms": [], "walls": [], "openings": [],
                                      "placements": [], "background": None}
        self.rects: list[tuple[str, str, tuple[float, float, float, float], str | None, bool]] = []

    def room(self, room_id: str, name: str, x0: float, y0: float, x1: float, y1: float, area: str | None = None,
             shares: bool = False) -> FloorBuilder:
        self.rects.append((room_id, name, (x0, y0, x1, y1), area, shares))
        return self

    def build(self) -> dict[str, Any]:
        corners = sorted({p for _, _, (x0, y0, x1, y1), _, _ in self.rects
                          for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))})
        tag = self.floor["id"].split("_", 1)[1]
        vid = {p: f"v_{tag}{i:03d}" for i, p in enumerate(corners)}
        self.floor["vertices"] = [{"id": vid[p], "x": p[0], "y": p[1]} for p in corners]
        walls: dict[frozenset[str], str] = {}
        for room_id, name, (x0, y0, x1, y1), area, shares in self.rects:
            ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            points: list[tuple[float, float]] = []
            for i, a in enumerate(ring):
                b = ring[(i + 1) % 4]
                on_edge = [p for p in corners if p not in (a, b) and _on_segment(a, b, p)]
                on_edge.sort(key=lambda p: (p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
                points += [a, *on_edge]
            ids = [vid[p] for p in points]
            self.floor["rooms"].append({"id": room_id, "name": name, "area_id": area, "area_name": None,
                                        "shares_area": shares, "vertices": ids})
            for i, a in enumerate(ids):
                pair = frozenset((a, ids[(i + 1) % len(ids)]))
                if pair not in walls:
                    walls[pair] = f"w_{tag}{len(walls):03d}"
                    self.floor["walls"].append({"id": walls[pair], "a": a, "b": ids[(i + 1) % len(ids)]})
        return self.floor

    def wall_between(self, p: tuple[float, float], q: tuple[float, float]) -> str:
        ids = {(v["x"], v["y"]): v["id"] for v in self.floor["vertices"]}
        pair = {ids[p], ids[q]}
        return next(w["id"] for w in self.floor["walls"] if {w["a"], w["b"]} == pair)


def _on_segment(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
    cross = (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    return abs(cross) < 1e-9 and min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])


def plan_of(*floors: dict[str, Any], north: float | None = None) -> dict[str, Any]:
    return {"format": fm.FORMAT, "version": fm.SCHEMA_VERSION,
            "building": {"id": "bld_home", "name": "Home", "north_deg": north}, "floors": list(floors),
            "dismissed": [], "updated_at": None}


def small_apartment() -> dict[str, Any]:
    b = FloorBuilder("fl_apt0", "Apartment").room("rm_live", "Living", 0, 0, 5, 4, "kamar_nordur") \
        .room("rm_kit", "Kitchen", 5, 0, 8, 4, "stova").room("rm_bath", "Bath", 0, 4, 3, 6) \
        .room("rm_bed", "Bedroom", 3, 4, 8, 6)
    floor = b.build()
    floor["openings"] = [
        {"id": "op_door1", "type": "door", "wall_id": b.wall_between((5, 0), (5, 4)), "offset": 2, "width": 0.9,
         "swing": "left"},
        {"id": "op_win1", "type": "window", "wall_id": b.wall_between((0, 0), (5, 0)), "offset": 2.5, "width": 1.2,
         "swing": None},
    ]
    floor["placements"] = [
        {"id": "pl_pir1", "entity_id": "binary_sensor.kamar_pir", "entity_uid": "reg-pir", "device_id": "dev-pir",
         "x": 1, "y": 1, "label": None},
        {"id": "pl_lamp", "entity_id": "light.kamar_lamp", "entity_uid": "reg-lamp", "device_id": None, "x": 2.5,
         "y": 2, "label": "Ceiling"},
    ]
    return plan_of(floor, north=0)


def multi_floor_house() -> dict[str, Any]:
    ground = FloorBuilder("fl_grnd", "Ground floor", 0).room("rm_hall", "Hall", 0, 0, 2, 6, "uti") \
        .room("rm_lounge", "Lounge", 2, 0, 8, 6, "kamar_nordur").build()
    upper = FloorBuilder("fl_up01", "First floor", 1).room("rm_bd1", "Bedroom 1", 0, 0, 4, 4, "stova") \
        .room("rm_bd2", "Bedroom 2", 4, 0, 8, 4).room("rm_land", "Landing", 0, 4, 8, 6).build()
    ground["ha_floor_id"] = "ground"
    return plan_of(ground, upper, north=90)


def open_plan_home() -> dict[str, Any]:
    b = FloorBuilder("fl_open", "Open plan").room("rm_ka", "Kitchen area", 0, 0, 4, 5, "kamar_nordur", True) \
        .room("rm_la", "Living area", 4, 0, 10, 5, "kamar_nordur", True).room("rm_ent", "Entrance", 10, 0, 12, 5)
    floor = b.build()
    floor["openings"] = [{"id": "op_open", "type": "opening", "wall_id": b.wall_between((4, 0), (4, 5)),
                          "offset": 2.5, "width": 5, "swing": None}]
    return plan_of(floor)


def few_areas_home() -> dict[str, Any]:
    return plan_of(FloorBuilder("fl_one", "Home").room("rm_a", "Room A", 0, 0, 3, 3)
                   .room("rm_b", "Room B", 3, 0, 6, 3).build())


async def post(client: Any, path: str, body: Any, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    resp = await client.post(path, data=json.dumps(body), headers={"Content-Type": "application/json",
                                                                     **(ADMIN_HEADERS if headers is None else headers)})
    return resp.status, await resp.json()


async def save(client: Any, plan: dict[str, Any], revision: str | None = None) -> tuple[int, Any]:
    if revision is None:
        revision = (await (await client.get("/api/floorplan")).json())["revision"]
    return await post(client, "/api/floorplan/save", {"plan": plan, "revision": revision})


async def start_app(fake: FakeHomeAssistant, aiohttp_client: Any, data_dir: Path) -> Any:
    settings = make_settings(fake, SL_DATA_DIR=str(data_dir))
    core = AppCore(settings)
    app = build_app(settings, core)

    async def start(_: web.Application) -> None:
        await core.start()

    async def stop(_: web.Application) -> None:
        await core.stop()

    app.on_startup.insert(0, start)
    app.on_cleanup.append(stop)
    client = await aiohttp_client(app)
    await wait_for(lambda: core.ha is not None and core.ha.connected)
    await wait_for(lambda: core.rooms is not None and core.rooms.api_state == "ok")
    client.core = core
    return client


def stored(fake: FakeHomeAssistant) -> Path:
    return Path(fake.data_dir) / "floorplan"


# ------------------------------------------------------------- validation
def test_validation_accepts_the_synthetic_homes() -> None:
    for build in (small_apartment, multi_floor_house, open_plan_home, few_areas_home):
        plan = fm.validate(build())
        assert fm.validate(plan) == plan  # stable: a validated plan validates to itself
    assert fm.summary(fm.validate(multi_floor_house())) == {
        "floors": 2, "rooms": 5, "linked_rooms": 3, "openings": 0, "linked_windows": 0, "placements": 0,
        "backgrounds": 0}


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda p: p["floors"][0]["rooms"][0].update(vertices=p["floors"][0]["rooms"][0]["vertices"][:2]),
     "invalid_polygon"),
    (lambda p: p["floors"][0]["rooms"][0]["vertices"].append("v_nope"), "unknown_vertex"),
    (lambda p: p["floors"][0]["openings"][0].update(wall_id="w_none"), "unknown_wall"),
    (lambda p: p["floors"][0]["openings"][0].update(type="portal"), "invalid_opening"),
    (lambda p: p["floors"][0]["rooms"][1].update(id=p["floors"][0]["rooms"][0]["id"]), "duplicate_id"),
    (lambda p: p["floors"][0]["rooms"][1].update(area_id="kamar_nordur"), "duplicate_area_link"),
    (lambda p: p["floors"][0]["placements"][0].update(entity_id="not an entity"), "invalid_entity"),
    (lambda p: p["floors"][0]["placements"][0].update(x=float("nan")), "invalid_number"),
    (lambda p: p["floors"][0]["placements"][0].update(x=1e9), "out_of_range"),
    (lambda p: p["floors"][0].update(name=""), "invalid_name"),
    (lambda p: p["floors"][0]["rooms"][0].update(area_id="../etc"), "invalid_area"),
    (lambda p: p.update(floors=[copy.deepcopy(p["floors"][0]) for _ in range(21)]), "too_many"),
    (lambda p: p.update(version=2), "invalid_format"),
])
def test_validation_rejects(mutate: Any, code: str) -> None:
    plan = small_apartment()
    mutate(plan)
    with pytest.raises(fm.PlanError) as err:
        fm.validate(plan)
    assert err.value.code == code


def test_self_intersecting_room_is_rejected() -> None:
    plan = few_areas_home()
    floor = plan["floors"][0]
    room = floor["rooms"][0]
    a, b, c, d = room["vertices"][:4]
    room["vertices"] = [a, c, b, d]  # a bow tie
    with pytest.raises(fm.PlanError) as err:
        fm.validate(plan)
    assert err.value.code == "invalid_polygon" and "cross" in err.value.message


def test_shared_area_needs_every_room_marked() -> None:
    plan = open_plan_home()
    fm.validate(plan)  # both parts are marked as a shared space
    plan["floors"][0]["rooms"][1]["shares_area"] = False
    with pytest.raises(fm.PlanError) as err:
        fm.validate(plan)
    assert err.value.code == "duplicate_area_link"


def test_openings_stay_on_a_shortened_wall() -> None:
    plan = small_apartment()
    plan["floors"][0]["openings"][1]["offset"] = 40  # beyond the 5 m wall
    clean = fm.validate(plan)
    assert clean["floors"][0]["openings"][1]["offset"] == pytest.approx(5 - 0.6)


# Phase A5: a window opening may be linked to a curtain window (the floor plan is the only place the link lives).
def test_curtain_window_link_is_validated() -> None:
    plan = small_apartment()
    window = plan["floors"][0]["openings"][1]
    assert window["type"] == "window" and "curtain_window_id" not in window
    clean = fm.validate(plan)
    assert clean["floors"][0]["openings"][1]["curtain_window_id"] is None  # a missing field is null
    assert clean["floors"][0]["openings"][0]["curtain_window_id"] is None
    assert fm.summary(clean)["linked_windows"] == 0
    for value in ("win_stova_1", "a-B_9", "x" * 64):
        window["curtain_window_id"] = value
        clean = fm.validate(plan)
        assert clean["floors"][0]["openings"][1]["curtain_window_id"] == value
        assert fm.validate(clean) == clean
    assert fm.summary(clean)["linked_windows"] == 1
    # Dangling links are allowed: the curtain configuration is not part of the plan, the editor shows them.
    window["curtain_window_id"] = "deleted_window"
    assert fm.validate(plan)["floors"][0]["openings"][1]["curtain_window_id"] == "deleted_window"
    for value in (None, ""):
        window["curtain_window_id"] = value
        assert fm.validate(plan)["floors"][0]["openings"][1]["curtain_window_id"] is None
    for bad in ("a b", "x" * 65, "../x", "win.1", 7, ["win"], "ünicode"):
        window["curtain_window_id"] = bad
        with pytest.raises(fm.PlanError) as err:
            fm.validate(plan)
        assert err.value.code == "invalid_curtain_window", bad
        assert err.value.path == "floors[0].openings[1].curtain_window_id"
    # Only a window carries the link: on a door it is dropped, like a window's door swing.
    plan["floors"][0]["openings"][0]["curtain_window_id"] = "win_stova_1"
    window["curtain_window_id"] = None
    clean = fm.validate(plan)
    assert clean["floors"][0]["openings"][0]["curtain_window_id"] is None
    assert fm.summary(clean)["linked_windows"] == 0


def test_curtain_window_link_changes_the_revision() -> None:
    plan = fm.validate(small_apartment())
    linked = copy.deepcopy(plan)
    linked["floors"][0]["openings"][1]["curtain_window_id"] = "win_1"
    assert fm.revision_of(linked) != fm.revision_of(plan)


def test_unused_corners_are_dropped() -> None:
    plan = few_areas_home()
    plan["floors"][0]["vertices"].append({"id": "v_stray", "x": 50, "y": 50})
    assert "v_stray" not in {v["id"] for v in fm.validate(plan)["floors"][0]["vertices"]}


def test_revision_ignores_the_save_time() -> None:
    plan = fm.validate(small_apartment())
    assert fm.revision_of({**plan, "updated_at": "2026-01-01T00:00:00+00:00"}) == fm.revision_of(plan)
    assert fm.revision_of({**plan, "dismissed": ["adjacency:a|b"]}) != fm.revision_of(plan)


def test_migration_chain_and_newer_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(fm.PlanError) as err:
        fm.migrate({**small_apartment(), "version": fm.SCHEMA_VERSION + 1})
    assert err.value.code == "newer_format"
    with pytest.raises(fm.PlanError):
        fm.migrate({"format": "something else", "version": 1})
    # A future format 2 that renames the building name field, migrated from format 1.
    monkeypatch.setattr(fm, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(fm.MIGRATIONS, 1, lambda p: {**p, "building": {**p["building"], "name": "migrated"}})
    plan, steps = fm.migrate({**small_apartment(), "version": 1})
    assert steps == [1] and plan["version"] == 2 and plan["building"]["name"] == "migrated"


def test_image_sniffing() -> None:
    assert fm.sniff_image(png(64, 48)) == ("png", 64, 48)
    jpeg = (b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
            + b"\xff\xc0" + struct.pack(">HBHH", 17, 8, 300, 400) + b"\x03" + b"\x00" * 9 + b"\xff\xd9")
    assert fm.sniff_image(jpeg) == ("jpeg", 400, 300)
    vp8x = b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x0a\x00\x00\x00" + b"\x00" * 4 + (99).to_bytes(3, "little") \
        + (49).to_bytes(3, "little")
    assert fm.sniff_image(vp8x) == ("webp", 100, 50)
    for bad, code in ((b"<svg xmlns='http://www.w3.org/2000/svg'/>", "unsupported_type"),
                      (b"GIF89a" + b"\x00" * 30, "unsupported_type"),
                      (b"\xff\xd8\xff\xe0garbage", "invalid_image"),
                      (png(4, 4), "invalid_size")):
        with pytest.raises(fm.ImageError) as err:
            fm.sniff_image(bad)
        assert err.value.code == code
    assert fm.image_id_for(png()) == fm.image_id_for(png()) != fm.image_id_for(png(colour=10))


def test_live_attributes_are_small_and_display_only() -> None:
    state = {"state": "on", "attributes": {"brightness": 128, "friendly_name": "x", "entity_id": ["a", "b"],
                                           "manual_overrides": [{"entity_id": "light.a"}], "reason": "r" * 400,
                                           "current_position": 40, "occupied": True}}
    assert live_attributes(state) == {"brightness": 128, "manual_overrides": 1, "current_position": 40,
                                      "occupied": True}


# ---------------------------------------------------------------- storage
async def test_empty_plan_then_save_and_reload(app_client: Any, fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    doc = await (await app_client.get("/api/floorplan")).json()
    assert doc["plan"]["floors"] == [] and doc["status"]["writable"] is True
    status, result = await save(app_client, small_apartment(), doc["revision"])
    assert status == 200 and result["saved"] is True and result["revision"] != doc["revision"]
    on_disk = json.loads((stored(fake_ha) / "plan.json").read_text())
    assert on_disk["floors"][0]["rooms"][0]["area_id"] == "kamar_nordur" and on_disk["updated_at"]
    # Saving the same content again changes nothing.
    status, again = await save(app_client, small_apartment(), result["revision"])
    assert (status, again["saved"], again["revision"]) == (200, False, result["revision"])
    # A restarted App (same /data) reads the same plan.
    other = await start_app(fake_ha, aiohttp_client, Path(fake_ha.data_dir))
    reloaded = await (await other.get("/api/floorplan")).json()
    assert reloaded["revision"] == result["revision"]
    assert reloaded["plan"]["floors"] == fm.validate(small_apartment())["floors"]


async def test_two_sessions_cannot_overwrite_each_other(app_client: Any) -> None:
    base = (await (await app_client.get("/api/floorplan")).json())["revision"]
    status, first = await save(app_client, small_apartment(), base)
    assert status == 200
    status, second = await save(app_client, few_areas_home(), base)  # the other session's stale base
    assert status == 409 and second["code"] == "stale_revision" and second["revision"] == first["revision"]
    current = await (await app_client.get("/api/floorplan")).json()
    assert current["plan"]["floors"][0]["id"] == "fl_apt0"  # the first save was kept
    status, _ = await save(app_client, few_areas_home(), second["revision"])  # explicit overwrite on the new base
    assert status == 200


async def test_live_channel_announces_saves(app_client: Any) -> None:
    async with app_client.ws_connect("/api/live") as ws:
        assert (await ws.receive_json(timeout=5))["type"] == "overview"
        status, result = await save(app_client, small_apartment())
        assert status == 200
        while True:
            message = await ws.receive_json(timeout=5)
            if message["type"] == "floorplan":
                break
        assert message == {"type": "floorplan", "revision": result["revision"]}


async def test_invalid_plan_is_refused_with_its_location(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    plan = small_apartment()
    plan["floors"][0]["openings"][0]["wall_id"] = "w_missing"
    status, body = await save(app_client, plan)
    assert status == 400 and body["code"] == "unknown_wall" and body["path"] == "floors[0].openings[0].wall_id"
    assert not (stored(fake_ha) / "plan.json").exists()


async def test_writes_need_an_administrator(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    revision = (await (await app_client.get("/api/floorplan")).json())["revision"]
    body = {"plan": small_apartment(), "revision": revision}
    for headers in ({"X-Remote-User-Id": USER_ID, "X-Smart-Lighting": "1"},  # not an administrator
                    {"X-Remote-User-Id": "ADMIN_ID"},  # no CSRF header
                    {"X-Smart-Lighting": "1"}):  # no Ingress user
        status, result = await post(app_client, "/api/floorplan/save", body, headers)
        assert status == 403 and result["code"] == "forbidden"
    resp = await app_client.post("/api/floorplan/save", data=json.dumps(body),
                                 headers={**ADMIN_HEADERS, "Content-Type": "application/json",
                                          "Origin": "https://evil.example"})
    assert resp.status == 403
    resp = await app_client.post("/api/floorplan/images", data=png(), headers={"Content-Type": "image/png",
                                                                                "X-Remote-User-Id": USER_ID,
                                                                                "X-Smart-Lighting": "1"})
    assert resp.status == 403
    assert not (stored(fake_ha) / "plan.json").exists()
    assert not list((stored(fake_ha) / "images").iterdir())


async def test_body_limits(app_client: Any) -> None:
    revision = (await (await app_client.get("/api/floorplan")).json())["revision"]
    big = json.dumps({"plan": small_apartment(), "revision": revision, "pad": "x" * (5 * 1024 * 1024)})
    resp = await app_client.post("/api/floorplan/save", data=big, headers={**ADMIN_HEADERS,
                                                                            "Content-Type": "application/json"})
    assert resp.status == 413
    resp = await app_client.post("/api/floorplan/images", data=b"\x89PNG" + b"\x00" * (16 * 1024 * 1024),
                                 headers={**ADMIN_HEADERS, "Content-Type": "image/png"})
    assert resp.status == 413


async def test_interrupted_save_is_recovered_from_a_backup(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    data = Path(fake_ha.data_dir)
    client = await start_app(fake_ha, aiohttp_client, data)
    _, first = await save(client, small_apartment())
    _, second = await save(client, multi_floor_house(), first["revision"])
    assert second["backup"]  # the apartment was backed up before the house replaced it
    folder = stored(fake_ha)
    (folder / "plan.json").write_text('{"format": "smart_lighting.floorplan", "version": 1, "floors": [', "utf-8")
    (folder / ".plan.json.deadbeef.tmp").write_text("partial")
    recovered = await start_app(fake_ha, aiohttp_client, data)
    doc = await (await recovered.get("/api/floorplan")).json()
    assert doc["plan"]["floors"][0]["id"] == "fl_apt0"  # the newest backup that can be read
    assert [n["code"] for n in doc["status"]["notices"]] == ["recovered"]
    assert list(folder.glob("plan.corrupt-*.json")) and not list(folder.glob(".*.tmp"))
    health = await (await recovered.get("/api/health")).json()
    assert next(c for c in health["checks"] if c["key"] == "floorplan")["status"] == "warning"
    # The recovered plan is stored again and can be edited.
    status, _ = await save(recovered, few_areas_home(), doc["revision"])
    assert status == 200


async def test_damaged_plan_without_backup_starts_empty(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    folder = Path(fake_ha.data_dir) / "floorplan"
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text("not json")
    client = await start_app(fake_ha, aiohttp_client, Path(fake_ha.data_dir))
    doc = await (await client.get("/api/floorplan")).json()
    assert doc["plan"]["floors"] == [] and doc["status"]["notices"][0]["code"] == "recovered_empty"
    assert list(folder.glob("plan.corrupt-*.json"))


async def test_newer_format_is_never_overwritten(fake_ha: FakeHomeAssistant, aiohttp_client: Any) -> None:
    folder = Path(fake_ha.data_dir) / "floorplan"
    folder.mkdir(parents=True)
    newer = json.dumps({**small_apartment(), "version": fm.SCHEMA_VERSION + 1})
    (folder / "plan.json").write_text(newer)
    client = await start_app(fake_ha, aiohttp_client, Path(fake_ha.data_dir))
    doc = await (await client.get("/api/floorplan")).json()
    assert doc["status"]["writable"] is False and doc["status"]["read_only_code"] == "newer_format"
    status, body = await save(client, few_areas_home(), doc["revision"])
    assert status == 409 and body["code"] == "newer_format"
    assert (folder / "plan.json").read_text() == newer


async def test_stored_plan_is_migrated_with_a_backup(fake_ha: FakeHomeAssistant, aiohttp_client: Any,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    folder = Path(fake_ha.data_dir) / "floorplan"
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text(json.dumps(small_apartment()))
    monkeypatch.setattr(fm, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(fm.MIGRATIONS, 1, lambda p: {**p, "building": {**p["building"], "name": "Migrated"}})
    client = await start_app(fake_ha, aiohttp_client, Path(fake_ha.data_dir))
    doc = await (await client.get("/api/floorplan")).json()
    assert doc["plan"]["version"] == 2 and doc["plan"]["building"]["name"] == "Migrated"
    assert [n["code"] for n in doc["status"]["notices"]] == ["migrated"]
    assert json.loads((folder / "plan.json").read_text())["version"] == 2
    backups = (await (await client.get("/api/floorplan/backups")).json())["backups"]
    assert backups[0]["reason"] == "Before migrating from format 1"


async def test_backups_and_restore(app_client: Any) -> None:
    _, first = await save(app_client, small_apartment())
    assert first["backup"] is None  # an empty plan is not worth a backup
    _, second = await save(app_client, multi_floor_house(), first["revision"])
    assert second["backup"]
    _, third = await save(app_client, open_plan_home(), second["revision"])
    assert third["backup"] is None  # coalesced: at most one automatic backup per 10 minutes
    backups = (await (await app_client.get("/api/floorplan/backups")).json())["backups"]
    assert [b["id"] for b in backups] == [second["backup"]] and backups[0]["summary"]["rooms"] == 4
    download = await app_client.get(f"/api/floorplan/backups/{second['backup']}")
    assert "attachment" in download.headers["Content-Disposition"]
    status, body = await post(app_client, f"/api/floorplan/backups/{second['backup']}/restore", {"revision": "0" * 16})
    assert status == 409 and body["code"] == "stale_revision"
    status, restored = await post(app_client, f"/api/floorplan/backups/{second['backup']}/restore",
                                  {"revision": third["revision"]})
    assert status == 200
    doc = await (await app_client.get("/api/floorplan")).json()
    assert doc["plan"]["floors"][0]["id"] == "fl_apt0" and doc["revision"] == restored["revision"]
    reasons = [b["reason"] for b in (await (await app_client.get("/api/floorplan/backups")).json())["backups"]]
    assert reasons[0] == "Before restoring a backup"
    for bad in ("../plan", "plan-x", "rooms-20261117T000000Z-0000000000000000"):
        assert (await app_client.get(f"/api/floorplan/backups/{bad}")).status == 404


# ----------------------------------------------------------------- images
async def test_image_upload_storage_and_serving(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    data = png(320, 200)
    resp = await app_client.post("/api/floorplan/images", data=data,
                                 headers={**ADMIN_HEADERS, "Content-Type": "image/png"})
    info = await resp.json()
    assert resp.status == 200 and info["image_id"] == fm.image_id_for(data)
    assert (info["width_px"], info["height_px"], info["content_type"]) == (320, 200, "image/png")
    again = await (await app_client.post("/api/floorplan/images", data=data,
                                         headers={**ADMIN_HEADERS, "Content-Type": "image/png"})).json()
    assert again["image_id"] == info["image_id"]  # content-addressed: stored once
    assert len(list((stored(fake_ha) / "images").glob("img_*"))) == 1
    served = await app_client.get(f"/api/floorplan/images/{info['image_id']}")
    assert served.status == 200 and await served.read() == data
    assert served.headers["Content-Type"] == "image/png"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in served.headers["Content-Security-Policy"]
    # Only the stored ids are served: nothing from the request becomes a path.
    for bad in ("img_0000000000000000", "..%2Fplan.json", "img_%2E%2E%2Fplan", "plan.json", "img_" + "a" * 16 + ".png"):
        assert (await app_client.get(f"/api/floorplan/images/{bad}")).status == 404, bad
    for body, kind in ((b"<svg onload=alert(1)>", "image/svg+xml"), (b"GIF89a" + b"\x00" * 40, "image/gif"),
                       (b"\x89PNG\r\n\x1a\nbroken", "image/png"), (data, "text/html")):
        resp = await app_client.post("/api/floorplan/images", data=body, headers={**ADMIN_HEADERS, "Content-Type": kind})
        assert resp.status == 415, kind


async def test_plan_must_reference_stored_images(app_client: Any) -> None:
    plan = small_apartment()
    plan["floors"][0]["background"] = {"image_id": "img_" + "1" * 16, "width_px": 100, "height_px": 50, "x": 0,
                                       "y": 0, "scale": 0.05, "rotation": 0, "opacity": 0.5}
    status, body = await save(app_client, plan)
    assert status == 400 and body["code"] == "unknown_image"


async def test_unused_images_are_removed_later(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    keep, orphan = png(colour=1), png(colour=2)
    ids = []
    for data in (keep, orphan):
        ids.append((await (await app_client.post("/api/floorplan/images", data=data, headers={
            **ADMIN_HEADERS, "Content-Type": "image/png"})).json())["image_id"])
    plan = small_apartment()
    plan["floors"][0]["background"] = {"image_id": ids[0], "width_px": 64, "height_px": 48, "x": 0, "y": 0,
                                       "scale": 0.05, "rotation": 0, "opacity": 0.5}
    old = time.time() - 2 * 86400
    for path in (stored(fake_ha) / "images").iterdir():
        os.utime(path, (old, old))
    assert (await save(app_client, plan))[0] == 200
    left = {p.stem for p in (stored(fake_ha) / "images").iterdir()}
    assert left == {ids[0]}


async def test_export_import_roundtrip(app_client: Any, fake_ha: FakeHomeAssistant, aiohttp_client: Any,
                                       tmp_path: Path) -> None:
    image = png(200, 100)
    info = await (await app_client.post("/api/floorplan/images", data=image,
                                        headers={**ADMIN_HEADERS, "Content-Type": "image/png"})).json()
    plan = multi_floor_house()
    plan["floors"][0]["background"] = {"image_id": info["image_id"], "width_px": 200, "height_px": 100, "x": -1,
                                       "y": -1, "scale": 0.05, "rotation": 15, "opacity": 0.4, "visible": True,
                                       "locked": True}
    _, saved = await save(app_client, plan)
    export = await app_client.get("/api/floorplan/export")
    assert "attachment" in export.headers["Content-Disposition"]
    exported = await export.json()
    assert base64.b64decode(exported["images"][info["image_id"]]["data"]) == image
    assert TOKEN not in json.dumps(exported)

    other = await start_app(fake_ha, aiohttp_client, tmp_path / "other-data")
    status, review = await post(other, "/api/floorplan/import/review", {"file": exported})
    assert status == 200 and review["summary"]["rooms"] == 5 and review["current"]["rooms"] == 0
    assert review["warnings"] == []
    status, body = await post(other, "/api/floorplan/import", {"file": exported, "revision": "0" * 16})
    assert status == 409
    status, imported = await post(other, "/api/floorplan/import", {"file": exported, "revision": review["revision"]})
    assert status == 200 and imported["revision"] == saved["revision"]  # same content, same revision
    assert (await (await other.get(f"/api/floorplan/images/{info['image_id']}")).read()) == image

    # A tampered image (content does not match its id) is refused.
    bad = copy.deepcopy(exported)
    bad["images"][info["image_id"]]["data"] = base64.b64encode(png(colour=3)).decode()
    status, body = await post(other, "/api/floorplan/import/review", {"file": bad})
    assert status == 400 and body["code"] == "invalid_image"
    # A file without its image keeps the plan and drops only that background.
    fresh = await start_app(fake_ha, aiohttp_client, tmp_path / "third")
    stripped = {**exported, "images": {}}
    status, review = await post(fresh, "/api/floorplan/import/review", {"file": stripped})
    assert status == 200 and len(review["warnings"]) == 1
    for junk in ({"format": "x"}, {"format": fm.EXPORT_FORMAT, "plan": {"format": fm.FORMAT, "version": 1,
                                                                         "floors": "x"}}, "text"):
        status, body = await post(fresh, "/api/floorplan/import/review", {"file": junk})
        assert status == 400


async def test_curtain_window_link_survives_save_export_and_import(app_client: Any, fake_ha: FakeHomeAssistant,
                                                                     aiohttp_client: Any, tmp_path: Path) -> None:
    plan = small_apartment()
    plan["floors"][0]["openings"][1]["curtain_window_id"] = "win_stova_south"
    commands_before = len(fake_ha.commands)
    status, saved = await save(app_client, plan)
    assert status == 200
    doc = await (await app_client.get("/api/floorplan")).json()
    window = doc["plan"]["floors"][0]["openings"][1]
    assert window["type"] == "window" and window["curtain_window_id"] == "win_stova_south"
    assert doc["status"]["summary"]["linked_windows"] == 1
    on_disk = json.loads((stored(fake_ha) / "plan.json").read_text())
    assert on_disk["floors"][0]["openings"][1]["curtain_window_id"] == "win_stova_south"
    # Linking changes nothing in the curtain subsystem or Home Assistant: no curtain command, no service call.
    assert not [c for c in fake_ha.commands[commands_before:] if "curtains" in c["type"]]
    assert app_client.core.safety()["service_calls_sent"] == 0

    exported = await (await app_client.get("/api/floorplan/export")).json()
    assert exported["plan"]["floors"][0]["openings"][1]["curtain_window_id"] == "win_stova_south"
    other = await start_app(fake_ha, aiohttp_client, tmp_path / "other-data")
    status, review = await post(other, "/api/floorplan/import/review", {"file": exported})
    assert status == 200 and review["summary"]["linked_windows"] == 1 and review["current"]["linked_windows"] == 0
    status, imported = await post(other, "/api/floorplan/import", {"file": exported, "revision": review["revision"]})
    assert status == 200 and imported["revision"] == saved["revision"]
    doc = await (await other.get("/api/floorplan")).json()
    assert doc["plan"]["floors"][0]["openings"][1]["curtain_window_id"] == "win_stova_south"
    # An invalid link in a file is refused with its location, like any other field.
    bad = copy.deepcopy(exported)
    bad["plan"]["floors"][0]["openings"][1]["curtain_window_id"] = "not valid!"
    status, body = await post(other, "/api/floorplan/import/review", {"file": bad})
    assert status == 400 and body["code"] == "invalid_curtain_window"
    assert body["path"] == "floors[0].openings[1].curtain_window_id"


# ------------------------------------------------------ Home Assistant side
async def test_context_lists_areas_floors_entities_and_room_entities(app_client: Any) -> None:
    ctx = await (await app_client.get("/api/floorplan/context")).json()
    assert [a["id"] for a in ctx["areas"]] == ["kamar_nordur", "stova", "uti"]
    assert ctx["floors"] == [{"id": "ground", "name": "Ground floor", "level": 0, "icon": None}]
    entities = {e["entity_id"]: e for e in ctx["entities"]}
    assert entities["binary_sensor.kamar_pir"]["entity_uid"] == "reg-pir"
    assert entities["binary_sensor.kamar_pir"]["category"] == "pir"
    assert entities["binary_sensor.kamar_pir"]["device"] == "Aqara motion"
    assert entities["cover.kamar_curtain"]["icon"] == "curtains"
    assert not any(e.startswith(("binary_sensor.kamar_nordur_occ", "sensor.kamar_nordur_st")) for e in entities)
    # The integration's own room entities, found by unique id (not by name).
    assert ctx["room_entities"] == {"kamar_nordur": {"state": "sensor.kamar_nordur_status",
                                                     "occupied": "binary_sensor.kamar_nordur_occupancy"}}
    assert ctx["rooms"]["kamar_nordur"]["enabled"] is True and ctx["rooms"]["kamar_nordur"]["adjacent_rooms"] == []
    assert ctx["units"] == "metric"


async def test_entity_details_show_assignments(app_client: Any) -> None:
    details = await (await app_client.get("/api/floorplan/entity/binary_sensor.kamar_radar")).json()
    assert details["entity"]["name"] == "Radar Kamar" and details["entity"]["availability"] == "ok"
    assert details["device"]["name"] == "FP1 radar"
    assert details["uses"] == [{"area_id": "kamar_nordur", "area": "Kamar Norður", "as": "sensor", "kind": "radar",
                                "kind_label": "Radar (presence)", "role": "maintain", "role_label": "Maintain",
                                "enabled": True, "inverted": False}]
    missing = await (await app_client.get("/api/floorplan/entity/light.does_not_exist")).json()
    assert missing["entity"]["availability"] == "missing" and missing["uses"] == []
    assert (await app_client.get("/api/floorplan/entity/not-an-entity")).status == 400


async def test_floor_plan_never_changes_home_assistant(app_client: Any, fake_ha: FakeHomeAssistant) -> None:
    options = copy.deepcopy(fake_ha.rooms_options)
    await app_client.get("/api/floorplan/context")
    await app_client.get("/api/floorplan/entity/binary_sensor.kamar_pir")
    image = await (await app_client.post("/api/floorplan/images", data=png(),
                                         headers={**ADMIN_HEADERS, "Content-Type": "image/png"})).json()
    plan = small_apartment()
    plan["floors"][0]["background"] = {"image_id": image["image_id"], "width_px": 64, "height_px": 48, "x": 0,
                                       "y": 0, "scale": 0.1, "rotation": 0, "opacity": 0.5}
    _, saved = await save(app_client, plan)
    moved = copy.deepcopy(plan)
    moved["floors"][0]["placements"][0].update(x=6, y=2)  # the PIR icon moved into another drawn room
    await save(app_client, moved, saved["revision"])
    await app_client.get("/api/floorplan/export")
    sent = {c["type"] for c in fake_ha.commands}
    assert sent <= READ_ONLY_COMMANDS | SUBSCRIPTION_COMMANDS  # reads only: no service call, no registry change, no room write
    assert not [c for c in fake_ha.commands if c["type"].startswith("smart_occupancy/rooms/set")]
    assert fake_ha.rooms_options == options
    assert app_client.core.safety()["service_calls_sent"] == 0


async def test_token_never_in_floor_plan_responses(app_client: Any) -> None:
    await save(app_client, small_apartment())
    for path in ("/api/floorplan", "/api/floorplan/context", "/api/floorplan/export", "/api/floorplan/backups",
                 "/api/floorplan/entity/binary_sensor.kamar_pir", "/api/diagnostics"):
        body = await (await app_client.get(path)).text()
        assert TOKEN not in body, path


async def test_synthetic_homes_round_trip(app_client: Any) -> None:
    revision = (await (await app_client.get("/api/floorplan")).json())["revision"]
    for build in (small_apartment, multi_floor_house, open_plan_home, few_areas_home):
        status, result = await save(app_client, build(), revision)
        assert status == 200, build.__name__
        doc = await (await app_client.get("/api/floorplan")).json()
        assert doc["plan"]["floors"] == fm.validate(build())["floors"]
        revision = result["revision"]


def test_service_without_storage_is_read_only() -> None:
    service = FloorPlanService(None, None, None)  # type: ignore[arg-type]
    service.load()
    assert service.status()["writable"] is False and service.status()["read_only_code"] == "no_storage"
