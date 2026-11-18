// Window orientation from the drawn plan (Phase A5). Pure functions.
//
// A window on an outside wall faces away from its room. With the plan's north
// direction (building.north_deg) that becomes a compass bearing, which the
// curtain subsystem can use as the window's orientation. A 2D plan gives a
// direction only: nothing here knows about sun exposure, obstructions, the
// horizon or neighbouring buildings. The suggestion is never applied by
// itself; the person reviews it in the inspector.
import { add, centroid, compassName, dot, mul, planBearing, pointInPolygon, sub, unit, type Pt } from "./geometry";
import { openingGeometry, pairKey, roomPoints, vertexMap, wallUse } from "./edit";
import type { Floor, Opening } from "./model";

/**
 * Unit normal of the wall a→b pointing away from the room `polygon` (outward), or null when the wall
 * is degenerate. The side is decided by probing just off the wall's middle: the side that is not inside
 * the room is outside. When both probes agree (a very thin room), the room's centroid decides.
 */
export function outwardNormal(a: Pt, b: Pt, polygon: Pt[]): Pt | null {
  const dir = unit(sub(b, a));
  if (dir.x === 0 && dir.y === 0) return null;
  const normal = { x: -dir.y, y: dir.x };
  const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
  const probe = 0.05;
  const plusInside = pointInPolygon(add(mid, mul(normal, probe)), polygon);
  const minusInside = pointInPolygon(add(mid, mul(normal, -probe)), polygon);
  if (plusInside !== minusInside) return plusInside ? mul(normal, -1) : normal;
  // Ambiguous probes: away from the centroid.
  const away = sub(mid, centroid(polygon));
  return dot(away, normal) >= 0 ? normal : mul(normal, -1);
}

/**
 * Compass bearing (0..360, clockwise from north) a wall faces outward, given the polygons of the rooms
 * that have this wall and the plan's north direction. Null when north is unknown, when no room or more
 * than one room has the wall (a shared wall has no single outside), or when the wall is degenerate.
 */
export function wallOutwardBearing(a: Pt, b: Pt, rooms: Pt[][], northDeg: number | null): number | null {
  if (northDeg === null || !Number.isFinite(northDeg) || rooms.length !== 1) return null;
  const normal = outwardNormal(a, b, rooms[0]);
  if (!normal) return null;
  return Math.round(((((northDeg + planBearing(normal)) % 360) + 360) % 360) * 10) / 10;
}

export type OrientationStatus = "ok" | "no_north" | "shared_wall" | "no_room" | "not_window";

export interface Orientation {
  status: OrientationStatus;
  /** Compass bearing in degrees, only with status "ok". */
  bearing: number | null;
  compass: string | null;
  /** Ids of the rooms that have the wall (two or more for a shared wall). */
  rooms: string[];
}

/** The orientation a window opening suggests on this floor. */
export function openingOrientation(floor: Floor, opening: Opening, northDeg: number | null): Orientation {
  const wall = floor.walls.find((w) => w.id === opening.wall_id);
  const map = vertexMap(floor);
  const g = wall ? openingGeometry(floor, opening, map) : null;
  const ids = wall ? (wallUse(floor).get(pairKey(wall.a, wall.b)) ?? []) : [];
  const rooms = ids.map((id) => floor.rooms.find((r) => r.id === id)).filter((r): r is NonNullable<typeof r> => Boolean(r));
  const base = { bearing: null, compass: null, rooms: rooms.map((r) => r.id) };
  if (opening.type !== "window") return { status: "not_window", ...base };
  if (!wall || !g || !rooms.length) return { status: "no_room", ...base };
  if (rooms.length > 1) return { status: "shared_wall", ...base };
  if (northDeg === null) return { status: "no_north", ...base };
  const [a, b] = [map.get(wall.a)!, map.get(wall.b)!];
  const bearing = wallOutwardBearing(a, b, [roomPoints(floor, rooms[0], map)], northDeg);
  if (bearing === null) return { status: "no_room", ...base };
  return { status: "ok", bearing, compass: compassName(bearing), rooms: base.rooms };
}

/** "135° (SE)" */
export function bearingText(bearing: number | null | undefined, compass?: string | null): string {
  if (bearing === null || bearing === undefined || !Number.isFinite(bearing)) return "Not set";
  const rounded = Math.round(bearing);
  return `${rounded}° (${compass ?? compassName(bearing)})`;
}

/** Smallest difference between two bearings, in degrees (0..180). */
export function bearingDifference(a: number, b: number): number {
  return Math.abs(((((a - b) % 360) + 540) % 360) - 180);
}
