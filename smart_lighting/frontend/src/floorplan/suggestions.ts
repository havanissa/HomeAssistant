// Spatial relationships from the drawn geometry (Phase A2.1).
//
// Only suggestions: nothing here changes the integration. An accepted
// adjacency is prepared as a draft in the Rooms editor, where the
// integration reviews and validates it before anything is saved. A drawn
// doorway never makes occupancy spread between rooms by itself.
import { centroid, compassName, dist, mul, add, planBearing, pointInPolygon, type Pt } from "./geometry";
import { openingGeometry, pairKey, roomEdges, roomPoints, vertexMap, wallUse } from "./edit";
import type { Floor, Plan, Room } from "./model";

export type SuggestionKind = "open_plan" | "doorway" | "shared_wall" | "not_connected" | "orientation";

export interface AdjacencyChange {
  /** The room (area) whose adjacent rooms would get `add`. */
  area_id: string;
  add: string;
}

export interface Suggestion {
  key: string;
  kind: SuggestionKind;
  strength: "strong" | "possible" | "info";
  floor_id: string;
  rooms: string[];
  areas: string[];
  title: string;
  reason: string;
  /** Adjacency entries still missing in the integration (empty when it already matches). */
  changes: AdjacencyChange[];
  /** Linked areas the integration does not observe yet (adjacency only matters for observed rooms). */
  unobserved: string[];
  shared_length?: number;
  doors?: number;
  openings?: number;
}

export interface IntegrationRooms {
  [areaId: string]: { configured: boolean; enabled: boolean; adjacent_rooms: string[] };
}

export const ADJACENCY_HELP =
  "In Smart Occupancy an adjacent room is extra evidence when someone leaves: after a door of this room moves, movement in an adjacent room confirms vacancy sooner, and forecasts use the connection. Occupancy never spreads from one room to another because they are adjacent.";

function roomName(room: Room, areaName: (id: string) => string): string {
  return room.name || (room.area_id ? areaName(room.area_id) : "Unnamed room");
}

/** Room pairs that share walls: pair key of room ids -> walls, length, openings. */
export function roomConnections(floor: Floor): Map<string, { rooms: [Room, Room]; length: number; doors: number; openings: number; windows: number }> {
  const map = vertexMap(floor);
  const use = wallUse(floor);
  const out = new Map<string, { rooms: [Room, Room]; length: number; doors: number; openings: number; windows: number }>();
  for (const wall of floor.walls) {
    const ids = use.get(pairKey(wall.a, wall.b)) ?? [];
    if (ids.length < 2) continue;
    const a = map.get(wall.a);
    const b = map.get(wall.b);
    if (!a || !b) continue;
    for (let i = 0; i < ids.length; i++) {
      for (let j = i + 1; j < ids.length; j++) {
        const key = pairKey(ids[i], ids[j]);
        const rooms = [floor.rooms.find((r) => r.id === ids[i])!, floor.rooms.find((r) => r.id === ids[j])!] as [Room, Room];
        const entry = out.get(key) ?? { rooms, length: 0, doors: 0, openings: 0, windows: 0 };
        entry.length += dist(a, b);
        for (const o of floor.openings.filter((x) => x.wall_id === wall.id)) {
          if (o.type === "door") entry.doors += 1;
          else if (o.type === "opening") entry.openings += 1;
          else entry.windows += 1;
        }
        out.set(key, entry);
      }
    }
  }
  return out;
}

/** Compass direction each exterior window faces (needs building.north_deg). */
export function windowDirections(floor: Floor, northDeg: number | null): Map<string, Array<{ opening_id: string; bearing: number; compass: string }>> {
  const out = new Map<string, Array<{ opening_id: string; bearing: number; compass: string }>>();
  if (northDeg === null) return out;
  const map = vertexMap(floor);
  const use = wallUse(floor);
  for (const o of floor.openings) {
    if (o.type !== "window") continue;
    const wall = floor.walls.find((w) => w.id === o.wall_id);
    if (!wall) continue;
    const ids = use.get(pairKey(wall.a, wall.b)) ?? [];
    if (ids.length !== 1) continue; // an interior window has no outside direction
    const room = floor.rooms.find((r) => r.id === ids[0]);
    const g = openingGeometry(floor, o, map);
    if (!room || !g) continue;
    const pts = roomPoints(floor, room, map);
    // The outward normal points away from the room's interior.
    const probe: Pt = add(g.center, mul(g.normal, 0.05));
    const outward = pointInPolygon(probe, pts) ? mul(g.normal, -1) : g.normal;
    const bearing = (northDeg + planBearing(outward)) % 360;
    const list = out.get(room.id) ?? [];
    list.push({ opening_id: o.id, bearing, compass: compassName(bearing) });
    out.set(room.id, list);
  }
  return out;
}

export function suggestionKey(kind: string, a: string, b: string): string {
  return `${kind === "orientation" ? "orientation" : "adjacency"}:${[a, b].sort().join("|")}`;
}

export function suggest(plan: Plan, rooms: IntegrationRooms, areaName: (id: string) => string): Suggestion[] {
  const out: Suggestion[] = [];
  const drawnPairs = new Set<string>();
  for (const floor of plan.floors) {
    for (const { rooms: [r1, r2], length, doors, openings } of roomConnections(floor).values()) {
      const n1 = roomName(r1, areaName);
      const n2 = roomName(r2, areaName);
      const kind: SuggestionKind = openings ? "open_plan" : doors ? "doorway" : "shared_wall";
      const areas = [r1.area_id, r2.area_id].filter((a): a is string => Boolean(a));
      if (r1.area_id && r2.area_id) drawnPairs.add(pairKey(r1.area_id, r2.area_id));
      if (r1.area_id && r1.area_id === r2.area_id) continue; // one area drawn as two parts: nothing to relate
      const changes: AdjacencyChange[] = [];
      const unobserved: string[] = [];
      if (r1.area_id && r2.area_id) {
        for (const [self, other] of [[r1.area_id, r2.area_id], [r2.area_id, r1.area_id]]) {
          const cfg = rooms[self];
          if (!cfg?.configured || !cfg.enabled) unobserved.push(self);
          else if (!cfg.adjacent_rooms.includes(other)) changes.push({ area_id: self, add: other });
        }
      }
      const metres = `${length.toFixed(length < 10 ? 1 : 0)} m`;
      const reason =
        kind === "open_plan"
          ? `${n1} and ${n2} are joined by an open-plan opening on a ${metres} shared wall. People walk freely between them, and sensors in one may also see the other.`
          : kind === "doorway"
            ? `${n1} and ${n2} share a ${metres} wall with ${doors === 1 ? "a door" : `${doors} doors`}, so people walk directly between them.`
            : `${n1} and ${n2} share a ${metres} wall, but no door or opening is drawn. They are adjacent for occupancy only if people can walk directly between them.`;
      out.push({
        key: suggestionKey("adjacency", r1.area_id ?? r1.id, r2.area_id ?? r2.id),
        kind,
        strength: kind === "shared_wall" ? "possible" : "strong",
        floor_id: floor.id,
        rooms: [r1.id, r2.id],
        areas,
        title: kind === "open_plan" ? `${n1} ↔ ${n2}: open plan` : kind === "doorway" ? `${n1} ↔ ${n2}: connected by a door` : `${n1} ↔ ${n2}: shared wall`,
        reason,
        changes,
        unobserved,
        shared_length: length,
        doors,
        openings,
      });
    }
    const dirs = windowDirections(floor, plan.building.north_deg);
    for (const [roomId, windows] of dirs) {
      const room = floor.rooms.find((r) => r.id === roomId)!;
      const faces = [...new Set(windows.map((w) => w.compass))];
      out.push({
        key: `orientation:${room.id}`,
        kind: "orientation",
        strength: "info",
        floor_id: floor.id,
        rooms: [room.id],
        areas: room.area_id ? [room.area_id] : [],
        title: `${roomName(room, areaName)}: windows facing ${faces.join(", ")}`,
        reason: `${windows.length === 1 ? "One window" : `${windows.length} windows`} on outside walls, facing ${faces.join(" and ")} (from the north direction you set). Kept for future curtain and sun analysis; nothing is changed.`,
        changes: [],
        unobserved: [],
      });
    }
  }
  // Adjacent in the integration, but nothing connects them on the drawn plan.
  const drawnAreas = new Set(plan.floors.flatMap((f) => f.rooms.map((r) => r.area_id)).filter(Boolean) as string[]);
  const reported = new Set<string>();
  for (const [areaId, cfg] of Object.entries(rooms)) {
    for (const other of cfg.adjacent_rooms) {
      const key = pairKey(areaId, other);
      if (reported.has(key) || drawnPairs.has(key) || !drawnAreas.has(areaId) || !drawnAreas.has(other)) continue;
      reported.add(key);
      out.push({
        key: `unconnected:${[areaId, other].sort().join("|")}`,
        kind: "not_connected",
        strength: "info",
        floor_id: "",
        rooms: [],
        areas: [areaId, other],
        title: `${areaName(areaId)} ↔ ${areaName(other)}: adjacent, but not connected on the plan`,
        reason: `The integration lists them as adjacent, but they share no wall on the floor plan. If that is wrong, review the adjacent rooms in the Rooms editor; nothing is removed automatically.`,
        changes: [],
        unobserved: [],
      });
    }
  }
  const order: Record<SuggestionKind, number> = { open_plan: 0, doorway: 1, shared_wall: 2, not_connected: 3, orientation: 4 };
  return out.sort((a, b) => order[a.kind] - order[b.kind] || a.title.localeCompare(b.title));
}

/** Centre of a room (for labels). */
export function roomCentre(floor: Floor, room: Room): Pt {
  return centroid(roomPoints(floor, room));
}

export { roomEdges };
