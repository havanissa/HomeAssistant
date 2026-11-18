// Editing operations on a floor. Every operation works on a copy and returns
// a new floor (or an error for an edit that would break a room), so the
// editor can keep the previous floor for undo. After each geometric edit
// `normalizeFloor` welds corners that meet (shared walls), splits walls where
// another room's corner touches them, rebuilds the wall list with stable ids
// and keeps every door and window on its wall.
import {
  add,
  area,
  dist,
  distToLine,
  distToSegment,
  dot,
  EPS,
  len,
  mul,
  pointInPolygon,
  polygonsOverlap,
  projectToSegment,
  round,
  roundPt,
  scaleAbout,
  strictlyOnSegment,
  sub,
  unit,
  validPolygon,
  type Pt,
} from "./geometry";
import { newId, OPENING_WIDTH, type Floor, type Opening, type OpeningType, type Placement, type Room, type Vertex, type Wall } from "./model";

export type Result<T> = { ok: true; value: T } | { ok: false; error: string };
const ok = <T>(value: T): Result<T> => ({ ok: true, value });
const fail = <T>(error: string): Result<T> => ({ ok: false, error });

export function cloneFloor(floor: Floor): Floor {
  return structuredClone(floor);
}

export function pairKey(a: string, b: string): string {
  return a < b ? `${a}|${b}` : `${b}|${a}`;
}

export function vertexMap(floor: Floor): Map<string, Vertex> {
  return new Map(floor.vertices.map((v) => [v.id, v]));
}

export function roomPoints(floor: Floor, room: Room, map = vertexMap(floor)): Pt[] {
  return room.vertices.map((id) => map.get(id)).filter((v): v is Vertex => Boolean(v));
}

export function roomEdges(room: Room): Array<[string, string]> {
  return room.vertices.map((v, i) => [v, room.vertices[(i + 1) % room.vertices.length]]);
}

export function wallEnds(floor: Floor, wall: Wall, map = vertexMap(floor)): [Pt, Pt] | null {
  const a = map.get(wall.a);
  const b = map.get(wall.b);
  return a && b ? [a, b] : null;
}

export function wallLength(floor: Floor, wall: Wall, map = vertexMap(floor)): number {
  const ends = wallEnds(floor, wall, map);
  return ends ? dist(ends[0], ends[1]) : 0;
}

/** pair key -> ids of the rooms that have this wall. */
export function wallUse(floor: Floor): Map<string, string[]> {
  const use = new Map<string, string[]>();
  for (const room of floor.rooms) {
    for (const [a, b] of roomEdges(room)) {
      const key = pairKey(a, b);
      const list = use.get(key) ?? [];
      if (!list.includes(room.id)) list.push(room.id);
      use.set(key, list);
    }
  }
  return use;
}

export function wallRooms(floor: Floor, wall: Wall): Room[] {
  const ids = wallUse(floor).get(pairKey(wall.a, wall.b)) ?? [];
  return floor.rooms.filter((r) => ids.includes(r.id));
}

export interface OpeningGeometry {
  center: Pt;
  start: Pt;
  end: Pt;
  /** Unit direction of the wall (a → b). */
  dir: Pt;
  /** Unit normal (dir rotated 90° clockwise on screen). */
  normal: Pt;
  wallLength: number;
}

export function clampOffset(offset: number, width: number, length: number): number {
  const half = Math.min(width, length) / 2;
  return round(Math.min(Math.max(offset, half), Math.max(length - half, half)));
}

export function openingGeometry(floor: Floor, opening: Opening, map = vertexMap(floor)): OpeningGeometry | null {
  const wall = floor.walls.find((w) => w.id === opening.wall_id);
  const ends = wall ? wallEnds(floor, wall, map) : null;
  if (!ends) return null;
  const [a, b] = ends;
  const length = dist(a, b);
  const dir = unit(sub(b, a));
  const offset = clampOffset(opening.offset, opening.width, length);
  const half = Math.min(opening.width, length) / 2;
  const center = add(a, mul(dir, offset));
  return { center, start: add(center, mul(dir, -half)), end: add(center, mul(dir, half)), dir, normal: { x: -dir.y, y: dir.x }, wallLength: length };
}

// ----------------------------------------------------------------- normalize
/**
 * Weld and tidy a floor after an edit (in place; returns it).
 * `before` (the floor before the edit) lets openings whose wall was split or
 * merged find their new wall by position; `wallHints` maps a removed wall id to
 * the corner pair that replaces it.
 */
export function normalizeFloor(floor: Floor, before: Floor | null = null, wallHints: Map<string, string> = new Map()): Floor {
  // 1. Corners at the same place become one corner (connected rooms share it).
  const reps = new Map<string, string>();
  const kept: Vertex[] = [];
  for (const v of floor.vertices) {
    const twin = kept.find((k) => dist(k, v) < EPS);
    if (twin) reps.set(v.id, twin.id);
    else {
      kept.push({ id: v.id, x: round(v.x), y: round(v.y) });
      reps.set(v.id, v.id);
    }
  }
  floor.vertices = kept;
  const map = vertexMap(floor);
  for (const room of floor.rooms) {
    const ids: string[] = [];
    for (const id of room.vertices.map((v) => reps.get(v) ?? v)) if (map.has(id) && ids[ids.length - 1] !== id) ids.push(id);
    while (ids.length > 1 && ids[0] === ids[ids.length - 1]) ids.pop();
    room.vertices = ids;
  }
  for (const [oldId, key] of [...wallHints]) {
    const [a, b] = key.split("|");
    wallHints.set(oldId, pairKey(reps.get(a) ?? a, reps.get(b) ?? b));
  }

  // 2. A corner lying on another room's wall splits that wall (T-junction), so both rooms share that part.
  const used = new Set(floor.rooms.flatMap((r) => r.vertices));
  const candidates = floor.vertices.filter((v) => used.has(v.id));
  for (const room of floor.rooms) {
    const out: string[] = [];
    for (const [a, b] of roomEdges(room)) {
      out.push(a);
      const pa = map.get(a)!;
      const pb = map.get(b)!;
      const between = candidates
        .filter((v) => v.id !== a && v.id !== b && !room.vertices.includes(v.id) && strictlyOnSegment(v, pa, pb))
        .sort((p, q) => dist(pa, p) - dist(pa, q));
      out.push(...between.map((v) => v.id));
    }
    room.vertices = out;
  }

  // 3. Walls: one per corner pair; keep ids (and direction) of walls that still exist.
  const oldWalls = new Map(floor.walls.map((w) => [pairKey(reps.get(w.a) ?? w.a, reps.get(w.b) ?? w.b), { ...w, a: reps.get(w.a) ?? w.a, b: reps.get(w.b) ?? w.b }]));
  const walls: Wall[] = [];
  const byKey = new Map<string, Wall>();
  for (const room of floor.rooms) {
    for (const [a, b] of roomEdges(room)) {
      const key = pairKey(a, b);
      if (byKey.has(key)) continue;
      const wall = oldWalls.get(key) ?? { id: newId("w"), a, b };
      byKey.set(key, wall);
      walls.push(wall);
    }
  }
  const surviving = new Set(walls.map((w) => w.id));

  // 4. Openings stay on their wall; on a split or merged wall they move to the new wall at the same place.
  const beforeMap = before ? vertexMap(before) : null;
  const openings: Opening[] = [];
  for (const o of floor.openings) {
    let wall: Wall | undefined = surviving.has(o.wall_id) ? walls.find((w) => w.id === o.wall_id) : undefined;
    let offset = o.offset;
    if (!wall) {
      const old = before && beforeMap ? openingGeometry(before, o, beforeMap) : null;
      const hinted = wallHints.get(o.wall_id);
      const target = hinted ? byKey.get(hinted) : undefined;
      if (old && target) {
        const [a, b] = wallEnds(floor, target, map)!;
        wall = target;
        offset = projectToSegment(old.center, a, b).t * dist(a, b);
      } else if (old) {
        for (const w of walls) {
          const [a, b] = wallEnds(floor, w, map)!;
          if (distToSegment(old.center, a, b) < 0.02 && Math.abs(dot(unit(sub(b, a)), old.dir)) > 0.999) {
            wall = w;
            offset = projectToSegment(old.center, a, b).t * dist(a, b);
            break;
          }
        }
      }
    }
    if (!wall) continue; // its wall is gone (the room was deleted)
    openings.push({ ...o, wall_id: wall.id, offset: clampOffset(offset, o.width, wallLength(floor, wall, map)) });
  }
  floor.walls = walls;
  floor.openings = openings;

  // 5. Corners no room uses are dropped.
  const usedNow = new Set(floor.rooms.flatMap((r) => r.vertices));
  floor.vertices = floor.vertices.filter((v) => usedNow.has(v.id));
  return floor;
}

/** Every room is a valid polygon (or the first problem found). */
export function checkFloor(floor: Floor): string | null {
  const map = vertexMap(floor);
  for (const room of floor.rooms) {
    const problem = validPolygon(roomPoints(floor, room, map));
    if (problem) return `${room.name || "A room"}: ${problem}`;
  }
  return null;
}

/** Pairs of rooms whose interiors overlap (pair keys of room ids). */
export function overlaps(floor: Floor): Map<string, [Room, Room]> {
  const map = vertexMap(floor);
  const shapes = floor.rooms.map((room) => ({ room, pts: roomPoints(floor, room, map) }));
  const out = new Map<string, [Room, Room]>();
  for (let i = 0; i < shapes.length; i++) {
    for (let j = i + 1; j < shapes.length; j++) {
      if (polygonsOverlap(shapes[i].pts, shapes[j].pts)) out.set(pairKey(shapes[i].room.id, shapes[j].room.id), [shapes[i].room, shapes[j].room]);
    }
  }
  return out;
}

function finish(floor: Floor, before: Floor, hints?: Map<string, string>): Result<Floor> {
  normalizeFloor(floor, before, hints);
  const problem = checkFloor(floor);
  if (problem) return fail(problem);
  // Rooms may share walls, but must not overlap (a new overlap is refused; old ones are left alone).
  const old = overlaps(before);
  for (const [key, [a, b]] of overlaps(floor)) {
    if (!old.has(key)) return fail(`${a.name || "A room"} and ${b.name || "another room"} would overlap. Rooms can share walls: draw them side by side (corners snap to walls).`);
  }
  return ok(floor);
}

// -------------------------------------------------------------------- rooms
export function addPolygonRoom(floor: Floor, points: Pt[], name = ""): Result<{ floor: Floor; roomId: string }> {
  const clean = points.map(roundPt);
  const problem = validPolygon(clean);
  if (problem) return fail(problem);
  const next = cloneFloor(floor);
  const ids = clean.map((p) => {
    const v = { id: newId("v"), x: p.x, y: p.y };
    next.vertices.push(v);
    return v.id;
  });
  const room: Room = { id: newId("rm"), name, area_id: null, area_name: null, shares_area: false, vertices: ids };
  next.rooms.push(room);
  const done = finish(next, floor);
  return done.ok ? ok({ floor: done.value, roomId: room.id }) : fail(done.error);
}

export function addRectRoom(floor: Floor, p1: Pt, p2: Pt, name = ""): Result<{ floor: Floor; roomId: string }> {
  const x0 = Math.min(p1.x, p2.x);
  const x1 = Math.max(p1.x, p2.x);
  const y0 = Math.min(p1.y, p2.y);
  const y1 = Math.max(p1.y, p2.y);
  if (x1 - x0 < 0.2 || y1 - y0 < 0.2) return fail("Drag a larger rectangle (at least 20 cm on each side).");
  return addPolygonRoom(floor, [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }], name);
}

/** Rooms containing p, smallest first. */
export function roomsAt(floor: Floor, p: Pt): Room[] {
  const map = vertexMap(floor);
  return floor.rooms
    .map((room) => ({ room, pts: roomPoints(floor, room, map) }))
    .filter(({ pts }) => pts.length >= 3 && pointInPolygon(p, pts))
    .sort((a, b) => area(a.pts) - area(b.pts))
    .map(({ room }) => room);
}

export function roomAt(floor: Floor, p: Pt): Room | null {
  return roomsAt(floor, p)[0] ?? null;
}

/** Openings that disappear with the room (those on walls no other room uses). */
export function openingsLostWithRoom(floor: Floor, roomId: string): Opening[] {
  const use = wallUse(floor);
  const room = floor.rooms.find((r) => r.id === roomId);
  if (!room) return [];
  const own = new Set(roomEdges(room).map(([a, b]) => pairKey(a, b)).filter((k) => (use.get(k) ?? []).length === 1));
  const walls = new Set(floor.walls.filter((w) => own.has(pairKey(w.a, w.b))).map((w) => w.id));
  return floor.openings.filter((o) => walls.has(o.wall_id));
}

export function deleteRoom(floor: Floor, roomId: string): Floor {
  const next = cloneFloor(floor);
  next.rooms = next.rooms.filter((r) => r.id !== roomId);
  return normalizeFloor(next, floor);
}

/**
 * Move a whole room. Corners it shares with other rooms are copied first, so
 * the neighbours keep their shape; it welds again where it is dropped.
 */
export function moveRoom(floor: Floor, roomId: string, delta: Pt, withDevices = true): Result<Floor> {
  const next = cloneFloor(floor);
  const room = next.rooms.find((r) => r.id === roomId);
  if (!room) return fail("Unknown room");
  const map = vertexMap(next);
  const before = roomPoints(floor, floor.rooms.find((r) => r.id === roomId)!);
  const others = new Set(next.rooms.filter((r) => r.id !== roomId).flatMap((r) => r.vertices));
  const use = wallUse(next);
  const renamed = new Map<string, string>();
  room.vertices = room.vertices.map((id) => {
    if (!others.has(id)) return id;
    const v = map.get(id)!;
    const copy = { id: newId("v"), x: v.x, y: v.y };
    next.vertices.push(copy);
    renamed.set(id, copy.id);
    return copy.id;
  });
  // Walls only this room has go with it (with their doors and windows).
  for (const w of next.walls) {
    if ((use.get(pairKey(w.a, w.b)) ?? []).length === 1 && (use.get(pairKey(w.a, w.b)) ?? [])[0] === roomId) {
      w.a = renamed.get(w.a) ?? w.a;
      w.b = renamed.get(w.b) ?? w.b;
    }
  }
  const moved = new Set(room.vertices);
  for (const v of next.vertices) {
    if (moved.has(v.id)) {
      v.x = round(v.x + delta.x);
      v.y = round(v.y + delta.y);
    }
  }
  if (withDevices) {
    for (const p of next.placements) {
      if (pointInPolygon(p, before)) {
        p.x = round(p.x + delta.x);
        p.y = round(p.y + delta.y);
      }
    }
  }
  return finish(next, floor);
}

// ---------------------------------------------------------- corners, walls
export function moveVertex(floor: Floor, vertexId: string, to: Pt): Result<Floor> {
  const next = cloneFloor(floor);
  const v = next.vertices.find((x) => x.id === vertexId);
  if (!v) return fail("Unknown corner");
  v.x = round(to.x);
  v.y = round(to.y);
  return finish(next, floor);
}

/** Corners that move with a wall: the wall, plus connected walls on the same straight line. */
export function wallChain(floor: Floor, wallId: string): string[] {
  const map = vertexMap(floor);
  const wall = floor.walls.find((w) => w.id === wallId);
  if (!wall) return [];
  const a = map.get(wall.a)!;
  const b = map.get(wall.b)!;
  const chain = new Set([wall.a, wall.b]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const w of floor.walls) {
      const inA = chain.has(w.a);
      const inB = chain.has(w.b);
      if (inA === inB) continue;
      const other = map.get(inA ? w.b : w.a)!;
      if (distToLine(other, a, b) < EPS) {
        chain.add(inA ? w.b : w.a);
        grew = true;
      }
    }
  }
  return [...chain];
}

/** Move a wall sideways (only the part of `delta` across the wall is used). */
export function moveWall(floor: Floor, wallId: string, delta: Pt, chain = true): Result<Floor> {
  const wall = floor.walls.find((w) => w.id === wallId);
  if (!wall) return fail("Unknown wall");
  const ends = wallEnds(floor, wall);
  if (!ends) return fail("Unknown wall");
  const dir = unit(sub(ends[1], ends[0]));
  const normal = { x: -dir.y, y: dir.x };
  const shift = mul(normal, dot(delta, normal));
  const ids = new Set(chain ? wallChain(floor, wallId) : [wall.a, wall.b]);
  const next = cloneFloor(floor);
  for (const v of next.vertices) {
    if (ids.has(v.id)) {
      v.x = round(v.x + shift.x);
      v.y = round(v.y + shift.y);
    }
  }
  return finish(next, floor);
}

/** Set a wall's length by moving its corner `b` along the wall. */
export function setWallLength(floor: Floor, wallId: string, length: number): Result<Floor> {
  if (!(length >= 0.1 && length <= 1000)) return fail("Enter a length between 0.1 and 1000 m.");
  const wall = floor.walls.find((w) => w.id === wallId);
  const ends = wall ? wallEnds(floor, wall) : null;
  if (!wall || !ends) return fail("Unknown wall");
  const to = add(ends[0], mul(unit(sub(ends[1], ends[0])), length));
  return moveVertex(floor, wall.b, to);
}

/** Add a corner on a wall (every room with that wall gets it). */
export function insertVertex(floor: Floor, wallId: string, at: Pt): Result<{ floor: Floor; vertexId: string }> {
  const wall = floor.walls.find((w) => w.id === wallId);
  const ends = wall ? wallEnds(floor, wall) : null;
  if (!wall || !ends) return fail("Unknown wall");
  const { t, point } = projectToSegment(at, ends[0], ends[1]);
  if (t * dist(ends[0], ends[1]) < 0.05 || (1 - t) * dist(ends[0], ends[1]) < 0.05) return fail("Too close to a corner.");
  const next = cloneFloor(floor);
  const v = { id: newId("v"), ...roundPt(point) };
  next.vertices.push(v);
  for (const room of next.rooms) {
    const n = room.vertices.length;
    for (let i = 0; i < n; i++) {
      const p = room.vertices[i];
      const q = room.vertices[(i + 1) % n];
      if (pairKey(p, q) === pairKey(wall.a, wall.b)) {
        room.vertices.splice(i + 1, 0, v.id);
        break;
      }
    }
  }
  const done = finish(next, floor);
  return done.ok ? ok({ floor: done.value, vertexId: v.id }) : fail(done.error);
}

/** Remove a corner from every room that has it (each keeps at least 3 corners). */
export function deleteVertex(floor: Floor, vertexId: string): Result<Floor> {
  const next = cloneFloor(floor);
  const hints = new Map<string, string>();
  for (const room of next.rooms) {
    const i = room.vertices.indexOf(vertexId);
    if (i < 0) continue;
    if (room.vertices.length <= 3) return fail(`${room.name || "A room"} needs at least 3 corners.`);
    const n = room.vertices.length;
    const prev = room.vertices[(i - 1 + n) % n];
    const following = room.vertices[(i + 1) % n];
    for (const w of next.walls) {
      const key = pairKey(w.a, w.b);
      if (key === pairKey(prev, vertexId) || key === pairKey(vertexId, following)) hints.set(w.id, pairKey(prev, following));
    }
    room.vertices.splice(i, 1);
  }
  return finish(next, floor, hints);
}

export interface WallHit {
  wall: Wall;
  point: Pt;
  /** Distance from the wall's corner a (m). */
  offset: number;
  distance: number;
  length: number;
}

export function nearestWall(floor: Floor, p: Pt, maxDistance: number): WallHit | null {
  const map = vertexMap(floor);
  let best: WallHit | null = null;
  for (const wall of floor.walls) {
    const ends = wallEnds(floor, wall, map);
    if (!ends) continue;
    const { t, point } = projectToSegment(p, ends[0], ends[1]);
    const d = dist(p, point);
    if (d <= maxDistance && (!best || d < best.distance)) {
      const length = dist(ends[0], ends[1]);
      best = { wall, point, offset: t * length, distance: d, length };
    }
  }
  return best;
}

// ----------------------------------------------------------------- openings
export function addOpening(floor: Floor, wallId: string, type: OpeningType, offset: number, width = OPENING_WIDTH[type]): Result<{ floor: Floor; openingId: string }> {
  const wall = floor.walls.find((w) => w.id === wallId);
  if (!wall) return fail("Doors and windows go on a wall.");
  const length = wallLength(floor, wall);
  const w = Math.min(width, length);
  if (w < 0.1) return fail("This wall is too short.");
  const next = cloneFloor(floor);
  const opening: Opening = { id: newId("op"), type, wall_id: wallId, offset: clampOffset(offset, w, length), width: round(w), swing: type === "door" ? "left" : null, curtain_window_id: null };
  next.openings.push(opening);
  return ok({ floor: next, openingId: opening.id });
}

export function updateOpening(floor: Floor, openingId: string, patch: Partial<Opening>): Result<Floor> {
  const next = cloneFloor(floor);
  const o = next.openings.find((x) => x.id === openingId);
  if (!o) return fail("Unknown opening");
  Object.assign(o, patch);
  const wall = next.walls.find((w) => w.id === o.wall_id);
  if (!wall) return fail("Doors and windows go on a wall.");
  const length = wallLength(next, wall);
  o.width = round(Math.max(0.1, Math.min(o.width, length)));
  o.offset = clampOffset(o.offset, o.width, length);
  if (o.type !== "door") o.swing = null;
  else if (!o.swing) o.swing = "left";
  // Only a window can show a curtain window (the backend drops the link on other types too).
  if (o.type !== "window") o.curtain_window_id = null;
  else if (o.curtain_window_id === undefined) o.curtain_window_id = null;
  return ok(next);
}

export function deleteOpening(floor: Floor, openingId: string): Floor {
  const next = cloneFloor(floor);
  next.openings = next.openings.filter((o) => o.id !== openingId);
  return next;
}

// --------------------------------------------------------------- placements
export function placeEntity(floor: Floor, entity: { entity_id: string; entity_uid?: string | null; device_id?: string | null }, at: Pt): { floor: Floor; placementId: string } {
  const next = cloneFloor(floor);
  const p: Placement = { id: newId("pl"), entity_id: entity.entity_id, entity_uid: entity.entity_uid ?? null, device_id: entity.device_id ?? null, ...roundPt(at), label: null };
  next.placements.push(p);
  return { floor: next, placementId: p.id };
}

export function movePlacement(floor: Floor, placementId: string, to: Pt): Floor {
  const next = cloneFloor(floor);
  const p = next.placements.find((x) => x.id === placementId);
  if (p) Object.assign(p, roundPt(to));
  return next;
}

export function updatePlacement(floor: Floor, placementId: string, patch: Partial<Placement>): Floor {
  const next = cloneFloor(floor);
  const p = next.placements.find((x) => x.id === placementId);
  if (p) Object.assign(p, patch);
  return next;
}

export function removePlacement(floor: Floor, placementId: string): Floor {
  const next = cloneFloor(floor);
  next.placements = next.placements.filter((p) => p.id !== placementId);
  return next;
}

// -------------------------------------------------------------- calibration
/**
 * Calibrate with a known distance: the two points are `real` metres apart.
 * Scales the background image about p1 and, with `drawing`, the rooms, doors,
 * windows and device icons too (so they stay on the image).
 */
export function calibrate(floor: Floor, p1: Pt, p2: Pt, real: number, drawing: boolean): Result<{ floor: Floor; factor: number }> {
  const measured = dist(p1, p2);
  if (measured < 0.01) return fail("Pick two points further apart.");
  if (!(real > 0 && real < 10000)) return fail("Enter the real distance in metres.");
  const k = real / measured;
  const next = cloneFloor(floor);
  if (next.background) {
    const origin = scaleAbout({ x: next.background.x, y: next.background.y }, p1, k);
    next.background = { ...next.background, x: round(origin.x), y: round(origin.y), scale: next.background.scale * k };
  }
  if (drawing || !next.background) {
    for (const v of next.vertices) Object.assign(v, roundPt(scaleAbout(v, p1, k)));
    for (const p of next.placements) Object.assign(p, roundPt(scaleAbout(p, p1, k)));
    for (const o of next.openings) {
      o.offset = round(o.offset * k);
      o.width = round(o.width * k);
    }
  }
  next.measured = true;
  return ok({ floor: next, factor: k });
}

/** Length of a vector (re-exported for the canvas' measuring labels). */
export const vectorLength = len;
