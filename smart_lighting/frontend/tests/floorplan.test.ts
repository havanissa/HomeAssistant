// Unit tests of the floor plan logic (Phase A2.1): geometry, shared walls, openings,
// transforms, calibration, undo/redo and relationship suggestions on synthetic homes.
import { describe, expect, it } from "vitest";

import {
  addOpening,
  addPolygonRoom,
  addRectRoom,
  calibrate,
  deleteRoom,
  deleteVertex,
  insertVertex,
  moveRoom,
  moveVertex,
  moveWall,
  nearestWall,
  openingGeometry,
  openingsLostWithRoom,
  pairKey,
  placeEntity,
  roomAt,
  roomPoints,
  setWallLength,
  vertexMap,
  wallChain,
  wallRooms,
  wallUse,
} from "../src/floorplan/edit";
import {
  area,
  compassName,
  fitView,
  imageToWorld,
  isSimplePolygon,
  planBearing,
  pointInPolygon,
  snapAngle,
  snapToGrid,
  toScreen,
  toWorld,
  validPolygon,
  worldToImage,
  zoomAt,
} from "../src/floorplan/geometry";
import { History } from "../src/floorplan/history";
import { clonePlan, duplicateAreaLinks, emptyPlan, newFloor, samePlan, type Floor, type Plan } from "../src/floorplan/model";
import { suggest, windowDirections } from "../src/floorplan/suggestions";
import { updateOpening } from "../src/floorplan/edit";
import { bearingDifference, bearingText, openingOrientation, outwardNormal, wallOutwardBearing } from "../src/floorplan/orientation";
import { coverView, curtainSummary, groupedWindows, layerText, linkedWindows, windowAttention, type CurtainModel, type CurtainWindow } from "../src/floorplan/curtains";

function must<T>(r: { ok: true; value: T } | { ok: false; error: string }): T {
  if (!r.ok) throw new Error(r.error);
  return r.value;
}

function rooms(floor: Floor, rects: Array<[number, number, number, number, string?]>): { floor: Floor; ids: string[] } {
  const ids: string[] = [];
  for (const [x0, y0, x1, y1, name] of rects) {
    const r = must(addRectRoom(floor, { x: x0, y: y0 }, { x: x1, y: y1 }, name ?? ""));
    floor = r.floor;
    ids.push(r.roomId);
  }
  return { floor, ids };
}

function pts(floor: Floor, roomId: string) {
  return roomPoints(floor, floor.rooms.find((r) => r.id === roomId)!).map((p) => [p.x, p.y]);
}

function wallBetween(floor: Floor, a: [number, number], b: [number, number]) {
  const map = vertexMap(floor);
  return floor.walls.find((w) => {
    const pa = map.get(w.a)!;
    const pb = map.get(w.b)!;
    return (pa.x === a[0] && pa.y === a[1] && pb.x === b[0] && pb.y === b[1]) || (pa.x === b[0] && pa.y === b[1] && pb.x === a[0] && pb.y === a[1]);
  })!;
}

function planWith(...floors: Floor[]): Plan {
  return { ...emptyPlan(), floors };
}

// --------------------------------------------------------------- geometry
describe("polygons", () => {
  it("validates corners, crossing walls and area", () => {
    expect(validPolygon([{ x: 0, y: 0 }, { x: 1, y: 0 }])).toMatch(/3 corners/);
    expect(isSimplePolygon([{ x: 0, y: 0 }, { x: 2, y: 2 }, { x: 2, y: 0 }, { x: 0, y: 2 }])).toBe(false); // bow tie
    expect(validPolygon([{ x: 0, y: 0 }, { x: 2, y: 2 }, { x: 2, y: 0 }, { x: 0, y: 2 }])).toMatch(/cross/);
    expect(validPolygon([{ x: 0, y: 0 }, { x: 0.1, y: 0 }, { x: 0.1, y: 0.1 }])).toMatch(/too small/);
    expect(validPolygon([{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 4, y: 3 }, { x: 2, y: 1 }, { x: 0, y: 3 }])).toBeNull(); // concave is fine
  });

  it("finds points inside polygons, boundary included", () => {
    const sq = [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 2 }, { x: 0, y: 2 }];
    expect(pointInPolygon({ x: 1, y: 1 }, sq)).toBe(true);
    expect(pointInPolygon({ x: 2, y: 1 }, sq)).toBe(true);
    expect(pointInPolygon({ x: 3, y: 1 }, sq)).toBe(false);
    expect(area(sq)).toBe(4);
  });

  it("creates rectangles and irregular rooms", () => {
    let floor = newFloor("Ground", 0);
    floor = rooms(floor, [[0, 0, 4, 3, "Kitchen"]]).floor;
    expect(floor.rooms).toHaveLength(1);
    expect(floor.walls).toHaveLength(4);
    expect(pts(floor, floor.rooms[0].id)).toEqual([[0, 0], [4, 0], [4, 3], [0, 3]]);
    const l = must(addPolygonRoom(floor, [{ x: 10, y: 0 }, { x: 14, y: 0 }, { x: 14, y: 2 }, { x: 12, y: 2 }, { x: 12, y: 4 }, { x: 10, y: 4 }], "L room"));
    expect(l.floor.rooms).toHaveLength(2);
    expect(l.floor.walls).toHaveLength(10);
    expect(addRectRoom(floor, { x: 0, y: 0 }, { x: 0.1, y: 3 }).ok).toBe(false);
    expect(addPolygonRoom(floor, [{ x: 0, y: 0 }, { x: 2, y: 2 }, { x: 2, y: 0 }, { x: 0, y: 2 }]).ok).toBe(false);
  });

  it("snaps to the grid and to 45° angles", () => {
    expect(snapToGrid({ x: 1.234, y: -0.06 }, 0.1)).toEqual({ x: 1.2, y: -0.1 });
    const s = snapAngle({ x: 0, y: 0 }, { x: 3, y: 0.2 });
    expect(s.y).toBeCloseTo(0);
    expect(s.x).toBeCloseTo(3, 1);
  });
});

// ------------------------------------------------------------ shared walls
describe("shared walls", () => {
  it("rooms drawn side by side share corners and one wall", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    expect(floor.vertices).toHaveLength(6);
    expect(floor.walls).toHaveLength(7);
    const shared = wallBetween(floor, [4, 0], [4, 3]);
    expect(wallRooms(floor, shared).map((r) => r.id).sort()).toEqual([...ids].sort());
  });

  it("a room touching part of a longer wall splits it (T-junction)", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 6, 4], [0, 4, 3, 6], [3, 4, 6, 6]]);
    // The big room's bottom wall now has a corner at (3, 4), shared with both small rooms.
    expect(pts(floor, ids[0])).toEqual([[0, 0], [6, 0], [6, 4], [3, 4], [0, 4]]);
    expect(wallRooms(floor, wallBetween(floor, [0, 4], [3, 4])).map((r) => r.id).sort()).toEqual([ids[0], ids[1]].sort());
    expect(wallRooms(floor, wallBetween(floor, [3, 4], [6, 4])).map((r) => r.id).sort()).toEqual([ids[0], ids[2]].sort());
  });

  it("moving a shared corner reshapes both rooms", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const corner = floor.vertices.find((v) => v.x === 4 && v.y === 0)!;
    const moved = must(moveVertex(floor, corner.id, { x: 4.5, y: 0 }));
    expect(pts(moved, ids[0])).toContainEqual([4.5, 0]);
    expect(pts(moved, ids[1])).toContainEqual([4.5, 0]);
  });

  it("moving a wall moves the whole straight wall, keeping shared rooms connected", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 6, 4], [0, 4, 3, 6], [3, 4, 6, 6]]);
    const part = wallBetween(floor, [0, 4], [3, 4]);
    expect(wallChain(floor, part.id)).toHaveLength(3); // (0,4) (3,4) (6,4)
    const moved = must(moveWall(floor, part.id, { x: 0.7, y: 0.5 }));
    expect(pts(moved, ids[0])).toEqual([[0, 0], [6, 0], [6, 4.5], [3, 4.5], [0, 4.5]]); // only the cross-wall part counts
    expect(pts(moved, ids[1])).toEqual([[0, 4.5], [3, 4.5], [3, 6], [0, 6]]);
    const segment = must(moveWall(floor, part.id, { x: 0, y: 0.5 }, false));
    expect(pts(segment, ids[0])).toEqual([[0, 0], [6, 0], [6, 4], [3, 4.5], [0, 4.5]]);
  });

  it("refuses an edit that would make walls cross", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    const corner = floor.vertices.find((v) => v.x === 4 && v.y === 3)!;
    expect(moveVertex(floor, corner.id, { x: 2, y: -1 }).ok).toBe(false);
  });

  it("dragging a corner onto another room's corner connects them", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [5, 0, 8, 3]]);
    let f = floor;
    for (const [from, to] of [[[5, 0], [4, 0]], [[5, 3], [4, 3]]] as const) {
      const v = f.vertices.find((x) => x.x === from[0] && x.y === from[1])!;
      f = must(moveVertex(f, v.id, { x: to[0], y: to[1] }));
    }
    expect(f.vertices).toHaveLength(6);
    expect(wallRooms(f, wallBetween(f, [4, 0], [4, 3])).map((r) => r.id).sort()).toEqual([...ids].sort());
  });

  it("moving a whole room detaches it from its neighbours and carries its devices", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const placed = placeEntity(floor, { entity_id: "binary_sensor.pir" }, { x: 5, y: 1 });
    const moved = must(moveRoom(placed.floor, ids[1], { x: 2, y: 0 }));
    expect(pts(moved, ids[0])).toEqual([[0, 0], [4, 0], [4, 3], [0, 3]]);
    expect(pts(moved, ids[1])).toEqual([[6, 0], [9, 0], [9, 3], [6, 3]]);
    expect(moved.placements[0]).toMatchObject({ x: 7, y: 1 });
    expect(must(moveRoom(placed.floor, ids[1], { x: 2, y: 0 }, false)).placements[0]).toMatchObject({ x: 5, y: 1 });
  });

  it("adds and removes corners on shared walls in every room", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const shared = wallBetween(floor, [4, 0], [4, 3]);
    const added = must(insertVertex(floor, shared.id, { x: 4.2, y: 1 }));
    expect(pts(added.floor, ids[0])).toContainEqual([4, 1]);
    expect(pts(added.floor, ids[1])).toContainEqual([4, 1]);
    const removed = must(deleteVertex(added.floor, added.vertexId));
    expect(pts(removed, ids[0])).toEqual([[0, 0], [4, 0], [4, 3], [0, 3]]);
    const tri = rooms(newFloor("G", 0), []).floor;
    const t = must(addPolygonRoom(tri, [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 0, y: 2 }]));
    expect(deleteVertex(t.floor, t.floor.vertices[0].id).ok).toBe(false);
  });

  it("deleting a room keeps its neighbour and the shared wall", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const after = deleteRoom(floor, ids[0]);
    expect(after.rooms.map((r) => r.id)).toEqual([ids[1]]);
    expect(after.walls).toHaveLength(4);
    expect(after.vertices).toHaveLength(4);
  });

  it("sets a wall's length", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    const top = wallBetween(floor, [0, 0], [4, 0]);
    const longer = must(setWallLength(floor, top.id, 5));
    expect(pts(longer, ids[0])).toContainEqual(top.a === floor.vertices.find((v) => v.x === 0 && v.y === 0)!.id ? [5, 0] : [-1, 0]);
  });
});

// ---------------------------------------------------------------- openings
describe("doors and windows", () => {
  it("attach to a wall and follow it when the room changes", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const shared = wallBetween(floor, [4, 0], [4, 3]);
    const door = must(addOpening(floor, shared.id, "door", 1.5));
    const g = openingGeometry(door.floor, door.floor.openings[0])!;
    expect(g.center.x).toBeCloseTo(4);
    // Moving the wall keeps the door on it.
    const moved = must(moveWall(door.floor, shared.id, { x: 0.5, y: 0 }));
    expect(moved.openings).toHaveLength(1);
    expect(openingGeometry(moved, moved.openings[0])!.center.x).toBeCloseTo(4.5);
    expect(pts(moved, ids[1])[0][0]).toBeCloseTo(4.5);
  });

  it("stay where they are when their wall is split, and go with a deleted room's own walls", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 6, 3]]);
    const top = wallBetween(floor, [0, 0], [6, 0]);
    const win = must(addOpening(floor, top.id, "window", top.a === floor.vertices.find((v) => v.x === 0)!.id ? 4.5 : 1.5));
    const before = openingGeometry(win.floor, win.floor.openings[0])!.center;
    const split = must(insertVertex(win.floor, top.id, { x: 2, y: 0 }));
    expect(split.floor.openings).toHaveLength(1);
    const after = openingGeometry(split.floor, split.floor.openings[0])!.center;
    expect(after.x).toBeCloseTo(before.x);
    expect(split.floor.openings[0].wall_id).not.toBe(top.id);
    expect(openingsLostWithRoom(split.floor, ids[0])).toHaveLength(1);
    expect(deleteRoom(split.floor, ids[0]).openings).toHaveLength(0);
  });

  it("are clamped to a shorter wall and found by position", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    const side = wallBetween(floor, [4, 0], [4, 3]);
    const added = must(addOpening(floor, side.id, "opening", 10, 5));
    expect(added.floor.openings[0].width).toBe(3);
    expect(nearestWall(floor, { x: 4.1, y: 1 }, 0.2)!.wall.id).toBe(side.id);
    expect(nearestWall(floor, { x: 6, y: 1 }, 0.2)).toBeNull();
  });

  it("windows on outside walls get a compass direction", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    let f = floor;
    for (const [a, b] of [[[0, 0], [4, 0]], [[4, 0], [4, 3]]] as const) f = must(addOpening(f, wallBetween(f, a as [number, number], b as [number, number]).id, "window", 1.5)).floor;
    const plain = windowDirections(f, 0);
    expect([...plain.values()][0].map((w) => w.compass).sort()).toEqual(["E", "N"]);
    const turned = windowDirections(f, 90); // the top of the plan is east
    expect([...turned.values()][0].map((w) => w.compass).sort()).toEqual(["E", "S"]);
    expect(windowDirections(f, null).size).toBe(0);
    expect(compassName(planBearing({ x: -1, y: 0 }))).toBe("W");
  });
});

// -------------------------------------------------------------- transforms
describe("coordinates", () => {
  it("converts between screen and world and zooms about the cursor", () => {
    const view = { scale: 50, tx: 100, ty: 20 };
    const p = { x: 2, y: 3 };
    expect(toWorld(view, toScreen(view, p))).toEqual(p);
    const zoomed = zoomAt(view, 2, { x: 300, y: 200 });
    expect(zoomed.scale).toBe(100);
    expect(toWorld(zoomed, { x: 300, y: 200 })).toEqual(toWorld(view, { x: 300, y: 200 }));
  });

  it("fits a drawing into the viewport", () => {
    const v = fitView({ minX: 0, minY: 0, maxX: 10, maxY: 5 }, 1096, 600, 48);
    expect(v.scale).toBe(100);
    expect(toScreen(v, { x: 5, y: 2.5 })).toEqual({ x: 548, y: 300 });
  });

  it("maps background image pixels with scale and rotation", () => {
    const bg = { x: 1, y: 2, scale: 0.01, rotation: 90 };
    const w = imageToWorld(bg, 100, 0);
    expect(w.x).toBeCloseTo(1);
    expect(w.y).toBeCloseTo(3);
    const back = worldToImage(bg, w);
    expect(back.x).toBeCloseTo(100);
    expect(back.y).toBeCloseTo(0);
  });
});

describe("calibration", () => {
  it("scales the background (and optionally the drawing) to a known distance", () => {
    let floor = rooms(newFloor("G", 0), [[0, 0, 2, 1]]).floor;
    floor = { ...floor, background: { image_id: "img_0123456789abcdef", width_px: 1000, height_px: 500, x: 0, y: 0, scale: 0.002, rotation: 0, opacity: 0.5, visible: true, locked: false } };
    floor = must(addOpening(floor, floor.walls[0].id, "door", 1, 0.5)).floor;
    // The drawn 2 m wall is really 8 m: everything becomes 4x larger.
    const c = must(calibrate(floor, { x: 0, y: 0 }, { x: 2, y: 0 }, 8, true));
    expect(c.factor).toBe(4);
    expect(c.floor.background!.scale).toBeCloseTo(0.008);
    expect(pts(c.floor, c.floor.rooms[0].id)).toEqual([[0, 0], [8, 0], [8, 4], [0, 4]]);
    expect(c.floor.openings[0].width).toBe(2);
    expect(c.floor.measured).toBe(true);
    const imageOnly = must(calibrate(floor, { x: 1, y: 0 }, { x: 2, y: 0 }, 2, false));
    expect(imageOnly.floor.background).toMatchObject({ x: -1, scale: 0.004 });
    expect(pts(imageOnly.floor, imageOnly.floor.rooms[0].id)).toEqual([[0, 0], [2, 0], [2, 1], [0, 1]]);
    expect(calibrate(floor, { x: 0, y: 0 }, { x: 0, y: 0 }, 3, true).ok).toBe(false);
  });
});

// ------------------------------------------------------------- undo / redo
describe("history", () => {
  it("undoes and redoes edits in order", () => {
    const h = new History();
    const p0 = planWith(newFloor("G", 0));
    const p1 = { ...p0, floors: [rooms(p0.floors[0], [[0, 0, 4, 3]]).floor] };
    const p2 = { ...p1, floors: [rooms(p1.floors[0], [[4, 0, 7, 3]]).floor] };
    h.push(p0, "Add room");
    h.push(p1, "Add room");
    expect(h.undoLabel).toBe("Add room");
    const back1 = h.undo(p2)!;
    expect(samePlan(back1, p1)).toBe(true);
    const back0 = h.undo(back1)!;
    expect(samePlan(back0, p0)).toBe(true);
    expect(h.canUndo).toBe(false);
    expect(samePlan(h.redo(back0)!, p1)).toBe(true);
    h.push(p1, "Rename");
    expect(h.canRedo).toBe(false); // a new edit drops the redo steps
  });

  it("keeps placements and their links when rooms are edited", () => {
    const { floor, ids } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    const placed = placeEntity(floor, { entity_id: "light.ceiling", entity_uid: "abc", device_id: "dev1" }, { x: 2, y: 1 });
    const edited = deleteRoom(placed.floor, ids[0]);
    expect(edited.placements).toEqual(placed.floor.placements);
    expect(roomAt(placed.floor, { x: 2, y: 1 })!.id).toBe(ids[0]);
    expect(roomAt(edited, { x: 2, y: 1 })).toBeNull();
  });
});

// ------------------------------------------------------------- suggestions
const areaName = (id: string) => ({ kitchen: "Kitchen", hall: "Hall", lounge: "Lounge", bed: "Bedroom", bath: "Bath" })[id] ?? id;

function smallApartment(): Plan {
  const built = rooms(newFloor("Apartment", 0), [[0, 0, 4, 3, "Kitchen"], [4, 0, 6, 3, "Hall"], [6, 0, 10, 3, "Lounge"], [0, 3, 4, 6, "Bath"]]);
  let f = built.floor;
  const link: Record<string, string> = { Kitchen: "kitchen", Hall: "hall", Lounge: "lounge", Bath: "bath" };
  f = { ...f, rooms: f.rooms.map((r) => ({ ...r, area_id: link[r.name] ?? null })) };
  f = must(addOpening(f, wallBetween(f, [4, 0], [4, 3]).id, "door", 1.5)).floor;
  f = must(addOpening(f, wallBetween(f, [6, 0], [6, 3]).id, "opening", 1.5)).floor;
  return planWith(f);
}

describe("relationship suggestions", () => {
  it("suggests adjacency for doors and open-plan openings, and explains shared walls", () => {
    const integration = {
      kitchen: { configured: true, enabled: true, adjacent_rooms: [] },
      hall: { configured: true, enabled: true, adjacent_rooms: ["kitchen"] },
      lounge: { configured: true, enabled: true, adjacent_rooms: [] },
    };
    const s = suggest(smallApartment(), integration, areaName);
    const byKey = new Map(s.map((x) => [x.key, x]));
    const door = byKey.get("adjacency:hall|kitchen")!;
    expect(door.kind).toBe("doorway");
    expect(door.strength).toBe("strong");
    expect(door.changes).toEqual([{ area_id: "kitchen", add: "hall" }]); // the hall already lists the kitchen
    expect(door.reason).toMatch(/door/);
    const open = byKey.get("adjacency:hall|lounge")!;
    expect(open.kind).toBe("open_plan");
    expect(open.changes).toHaveLength(2);
    const wall = byKey.get("adjacency:bath|kitchen")!;
    expect(wall.kind).toBe("shared_wall");
    expect(wall.strength).toBe("possible");
    expect(wall.unobserved).toEqual(["bath"]); // not observed by the integration: nothing to add there
    expect(wall.changes).toEqual([{ area_id: "kitchen", add: "bath" }]);
  });

  it("reports integration adjacency the drawing does not show, without removing it", () => {
    const integration = { kitchen: { configured: true, enabled: true, adjacent_rooms: ["lounge"] }, lounge: { configured: true, enabled: true, adjacent_rooms: [] } };
    const s = suggest(smallApartment(), integration, areaName);
    const odd = s.find((x) => x.kind === "not_connected")!;
    expect(odd.areas.sort()).toEqual(["kitchen", "lounge"]);
    expect(odd.changes).toEqual([]);
  });

  it("works with unlinked rooms, open-plan parts of one area and multiple floors", () => {
    const unlinked = rooms(newFloor("G", 0), [[0, 0, 3, 3], [3, 0, 6, 3]]).floor;
    const s1 = suggest(planWith(unlinked), {}, areaName);
    expect(s1).toHaveLength(1);
    expect(s1[0].changes).toEqual([]);
    const parts = rooms(newFloor("G", 0), [[0, 0, 3, 3], [3, 0, 6, 3]]).floor;
    parts.rooms.forEach((r) => Object.assign(r, { area_id: "lounge", shares_area: true }));
    expect(suggest(planWith(parts), {}, areaName)).toHaveLength(0);
    const up = rooms(newFloor("Up", 1), [[0, 0, 3, 3]]).floor;
    up.rooms[0].area_id = "bed";
    const twoFloors = planWith(smallApartment().floors[0], up);
    expect(suggest(twoFloors, {}, areaName).some((x) => x.areas.includes("bed"))).toBe(false); // no wall across floors
  });

  it("detects duplicate area links unless modelled as a shared space", () => {
    const p = smallApartment();
    p.floors[0].rooms[1].area_id = "kitchen";
    expect(duplicateAreaLinks(p)).toEqual(["kitchen"]);
    p.floors[0].rooms[0].shares_area = true;
    p.floors[0].rooms[1].shares_area = true;
    expect(duplicateAreaLinks(p)).toEqual([]);
  });

  it("wall use counts each room once per wall", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [4, 0, 7, 3]]);
    const use = wallUse(floor);
    const shared = wallBetween(floor, [4, 0], [4, 3]);
    expect(use.get(pairKey(shared.a, shared.b))).toHaveLength(2);
  });
});

describe("overlap", () => {
  it("refuses a room drawn over another, allows rooms side by side", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    expect(addRectRoom(floor, { x: 2, y: 1 }, { x: 6, y: 4 }).ok).toBe(false); // crossing
    expect(addRectRoom(floor, { x: 1, y: 1 }, { x: 2, y: 2 }).ok).toBe(false); // inside
    expect(addRectRoom(floor, { x: 0, y: 0 }, { x: 4, y: 3 }).ok).toBe(false); // the same room again
    expect(addRectRoom(floor, { x: -1, y: -1 }, { x: 5, y: 4 }).ok).toBe(false); // around it
    expect(addRectRoom(floor, { x: 4, y: 0 }, { x: 6, y: 3 }).ok).toBe(true); // side by side
    expect(addRectRoom(floor, { x: 4, y: 3 }, { x: 6, y: 5 }).ok).toBe(true); // touching at a corner
  });

  it("refuses moving a corner into a neighbour", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3], [5, 0, 8, 3]]);
    const corner = floor.vertices.find((v) => v.x === 4 && v.y === 0)!;
    expect(moveVertex(floor, corner.id, { x: 6, y: 0 }).ok).toBe(false);
    expect(moveVertex(floor, corner.id, { x: 4.5, y: 0 }).ok).toBe(true);
  });
});

// ------------------------------------------------------- curtains (Phase A5)
describe("window orientation", () => {
  const square = [{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 4, y: 3 }, { x: 0, y: 3 }];

  it("points the outward normal away from the room", () => {
    expect(outwardNormal({ x: 0, y: 0 }, { x: 4, y: 0 }, square)!.y).toBeCloseTo(-1); // top wall: up (-y)
    expect(outwardNormal({ x: 4, y: 0 }, { x: 4, y: 3 }, square)!.x).toBeCloseTo(1); // right wall: right
    expect(outwardNormal({ x: 4, y: 3 }, { x: 0, y: 3 }, square)!.y).toBeCloseTo(1); // bottom wall: down
    expect(outwardNormal({ x: 0, y: 3 }, { x: 0, y: 0 }, square)!.x).toBeCloseTo(-1); // left wall: left
    // The wall's direction does not matter, only the room's side.
    expect(outwardNormal({ x: 4, y: 0 }, { x: 0, y: 0 }, square)!.y).toBeCloseTo(-1);
    expect(outwardNormal({ x: 1, y: 1 }, { x: 1, y: 1 }, square)).toBeNull();
  });

  it("turns the normal into a compass bearing with the plan's north", () => {
    expect(wallOutwardBearing({ x: 0, y: 0 }, { x: 4, y: 0 }, [square], 0)).toBe(0); // top of the plan is north
    expect(wallOutwardBearing({ x: 4, y: 0 }, { x: 4, y: 3 }, [square], 0)).toBe(90);
    expect(wallOutwardBearing({ x: 4, y: 3 }, { x: 0, y: 3 }, [square], 0)).toBe(180);
    expect(wallOutwardBearing({ x: 0, y: 3 }, { x: 0, y: 0 }, [square], 0)).toBe(270);
    expect(wallOutwardBearing({ x: 0, y: 0 }, { x: 4, y: 0 }, [square], 90)).toBe(90); // the top of the plan is east
    expect(wallOutwardBearing({ x: 0, y: 3 }, { x: 0, y: 0 }, [square], 135)).toBe(45); // wraps around
    // A diagonal wall of a triangle.
    const tri = [{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 0, y: 4 }];
    expect(wallOutwardBearing({ x: 4, y: 0 }, { x: 0, y: 4 }, [tri], 0)).toBe(135); // faces south-east
    // No north, a shared wall (two rooms) or no room: no suggestion.
    expect(wallOutwardBearing({ x: 0, y: 0 }, { x: 4, y: 0 }, [square], null)).toBeNull();
    expect(wallOutwardBearing({ x: 4, y: 0 }, { x: 4, y: 3 }, [square, [{ x: 4, y: 0 }, { x: 7, y: 0 }, { x: 7, y: 3 }, { x: 4, y: 3 }]], 0)).toBeNull();
    expect(wallOutwardBearing({ x: 0, y: 0 }, { x: 4, y: 0 }, [], 0)).toBeNull();
  });

  it("suggests an orientation for a window on an outside wall only", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3, "Kitchen"], [4, 0, 7, 3, "Hall"]]);
    let f = must(addOpening(floor, wallBetween(floor, [0, 0], [4, 0]).id, "window", 2)).floor;
    f = must(addOpening(f, wallBetween(f, [4, 0], [4, 3]).id, "window", 1.5)).floor; // on the shared wall
    f = must(addOpening(f, wallBetween(f, [0, 3], [4, 3]).id, "door", 2)).floor;
    const [top, shared, door] = f.openings;
    expect(openingOrientation(f, top, 0)).toMatchObject({ status: "ok", bearing: 0, compass: "N" });
    expect(openingOrientation(f, top, 90)).toMatchObject({ status: "ok", bearing: 90, compass: "E" });
    expect(openingOrientation(f, top, null)).toMatchObject({ status: "no_north", bearing: null });
    const amb = openingOrientation(f, shared, 0);
    expect(amb.status).toBe("shared_wall");
    expect(amb.rooms).toHaveLength(2);
    expect(openingOrientation(f, door, 0).status).toBe("not_window");
    expect(openingOrientation(f, { ...top, wall_id: "w_gone" }, 0).status).toBe("no_room");
  });

  it("formats and compares bearings", () => {
    expect(bearingText(135)).toBe("135° (SE)");
    expect(bearingText(359.6, "N")).toBe("360° (N)");
    expect(bearingText(null)).toBe("Not set");
    expect(bearingDifference(10, 350)).toBe(20);
    expect(bearingDifference(90, 90)).toBe(0);
    expect(bearingDifference(0, 180)).toBe(180);
  });
});

function curtainWindow(over: Partial<CurtainWindow> = {}): CurtainWindow {
  return {
    window_id: "win_1", name: "Kitchen south", area_id: "kitchen", area_name: "Kitchen", direction: "S", bearing: null,
    effective_bearing: 180, compass: "S", mode: "automatic",
    layers: [{ layer_id: "l1", entity_id: "cover.a", kind: "curtain", name: "Curtain", state: "open", available: true, missing: false, moving: false, position: 70 }],
    current: { status: "preset", preset_id: "p1", text: "Half shade" }, selected_preset: "p1", presets: [], manual: null, last: null, running: false,
    problems: [], exposure: { status: "exposed", text: "In the sun", exposed: true },
    auto: { status: { action: "applied", word: "Applied", text: "Half shade", condition: "day" }, hold: null, decision: null, environment: { sun: { status: "exposed", text: "In the sun", exposed: true }, glare: { enabled: true, active: false } }, next_evaluation: null },
    ...over,
  };
}

describe("curtain windows on the plan", () => {
  it("keeps the curtain link through clone, sameness, edits and a type change", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    const f = must(addOpening(floor, wallBetween(floor, [0, 0], [4, 0]).id, "window", 2)).floor;
    expect(f.openings[0].curtain_window_id).toBeNull();
    const linked = must(updateOpening(f, f.openings[0].id, { curtain_window_id: "win_1" }));
    expect(linked.openings[0].curtain_window_id).toBe("win_1");
    expect(f.openings[0].curtain_window_id).toBeNull(); // the original floor is untouched (undo keeps it)
    const plan = planWith(linked);
    const copy = clonePlan(plan);
    expect(copy.floors[0].openings[0].curtain_window_id).toBe("win_1");
    expect(samePlan(plan, copy)).toBe(true);
    expect(samePlan(plan, planWith(f))).toBe(false); // the link is part of the plan's content (unsaved-changes guard)
    const h = new History();
    h.push(planWith(f), "Link curtain window");
    expect(h.undo(plan)!.floors[0].openings[0].curtain_window_id).toBeNull();
    // Moving the window keeps the link; the wall moving keeps it too.
    expect(must(updateOpening(linked, linked.openings[0].id, { offset: 3 })).openings[0].curtain_window_id).toBe("win_1");
    const movedWall = must(moveWall(linked, linked.openings[0].wall_id, { x: 0, y: -0.5 }));
    expect(movedWall.openings[0].curtain_window_id).toBe("win_1");
    // Only a window carries the link: changing it to a door drops it, like the backend.
    expect(must(updateOpening(linked, linked.openings[0].id, { type: "door" })).openings[0].curtain_window_id).toBeNull();
  });

  it("reads cover positions, preferring live states, and marks unknown or unavailable covers", () => {
    const w = curtainWindow();
    expect(coverView(w)).toEqual({ tone: "ok", position: 70, text: "70 % open" });
    const states = new Map([["cover.a", { entity_id: "cover.a", state: "closed", availability: "ok", last_changed: "", last_updated: "", attrs: { current_position: 0 } }]]) as never;
    expect(coverView(w, states)).toEqual({ tone: "ok", position: 0, text: "Closed" });
    expect(layerText(w.layers[0], states)).toEqual({ text: "Closed", tone: "ok" });
    const two = curtainWindow({ layers: [w.layers[0], { ...w.layers[0], layer_id: "l2", entity_id: "cover.b", position: 30 }] });
    expect(coverView(two).position).toBe(50);
    expect(coverView(curtainWindow({ layers: [{ ...w.layers[0], moving: true, state: "closing" }] }))).toMatchObject({ tone: "moving", position: 70 });
    expect(coverView(curtainWindow({ layers: [{ ...w.layers[0], available: false, state: "unavailable" }] }))).toMatchObject({ tone: "unavailable", position: null, text: "Unavailable" });
    expect(coverView(curtainWindow({ layers: [{ ...w.layers[0], position: null }] }))).toMatchObject({ tone: "unknown", position: null });
    expect(coverView(curtainWindow({ layers: [] }))).toMatchObject({ tone: "none" });
    expect(layerText({ ...w.layers[0], missing: true }).tone).toBe("unavailable");
  });

  it("resolves links on a floor, including dangling ones, and summarises them", () => {
    const { floor } = rooms(newFloor("G", 0), [[0, 0, 4, 3]]);
    let f = must(addOpening(floor, wallBetween(floor, [0, 0], [4, 0]).id, "window", 2)).floor;
    f = must(addOpening(f, wallBetween(f, [4, 0], [4, 3]).id, "window", 1.5)).floor;
    f = must(addOpening(f, wallBetween(f, [0, 3], [4, 3]).id, "window", 2)).floor;
    f = must(addOpening(f, wallBetween(f, [0, 0], [0, 3]).id, "door", 1.5)).floor;
    f = must(updateOpening(f, f.openings[0].id, { curtain_window_id: "win_1" }));
    f = must(updateOpening(f, f.openings[1].id, { curtain_window_id: "win_gone" }));
    const held = curtainWindow({ window_id: "win_2", name: "Held", mode: "observing", auto: { ...curtainWindow().auto!, hold: { policy_word: "Until evening", reason: "Moved by hand" } } });
    const model: CurtainModel = { can_edit: true, windows: [curtainWindow(), held], rooms: [{ area_id: "kitchen", name: "Kitchen", windows: ["win_1"] }, { area_id: null, name: "No room", windows: ["win_2"] }] };
    expect(linkedWindows(f, null).map((l) => [l.window, l.dangling])).toEqual([[null, false], [null, false], [null, false]]); // not loaded: nothing dangles yet
    const links = linkedWindows(f, model);
    expect(links).toHaveLength(3); // the door is not a window
    expect(links[0].window?.window_id).toBe("win_1");
    expect(links[1]).toMatchObject({ window: null, dangling: true });
    expect(links[2]).toMatchObject({ window: null, dangling: false });
    const s = curtainSummary(links);
    expect(s).toMatchObject({ linked: 1, automatic: 1, holds: 0, moving: 0, unavailable: 0, dangling: 1 });
    expect(s.attention.map((a) => a.reasons[0])).toEqual(["Linked curtain window no longer exists"]);
    f = must(updateOpening(f, f.openings[2].id, { curtain_window_id: "win_2" }));
    const s2 = curtainSummary(linkedWindows(f, model));
    expect(s2).toMatchObject({ linked: 2, automatic: 1, observing: 1, holds: 1 });
    expect(s2.attention.find((a) => a.window?.window_id === "win_2")!.reasons).toEqual(["Manual hold"]);
    expect(windowAttention(curtainWindow({ problems: [{ severity: "warning", text: "cover.a is unavailable." }, { severity: "info", text: "No covers yet" }], exposure: { status: "no_direction", text: "", exposed: false } }))).toEqual(["cover.a is unavailable.", "No orientation set"]);
    // Grouping for the picker: the rooms of the wall first.
    expect(groupedWindows(model, []).map((g) => g.name)).toEqual(["Kitchen", "No room"]);
    expect(groupedWindows(model, ["kitchen"]).map((g) => g.name)).toEqual(["Kitchen", "No room"]);
  });
});
