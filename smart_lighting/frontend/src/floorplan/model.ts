// Floor plan data model (Phase A2.1). Same shape as the backend's
// floorplan_model.py, which validates every save.
//
// Coordinate system: every floor is its own flat plane, in METRES. x grows to
// the right, y grows DOWNWARDS (screen/SVG convention); the origin is
// arbitrary. Angles are degrees clockwise from the top of the plan (-y).
// building.north_deg is the compass bearing of the plan's top edge (null:
// unknown). A floor's `measured` flag says whether its dimensions are real
// (calibrated or typed in) or a sketch.
//
// Rooms share corners (floor.vertices) by id, so a shared wall is the same
// pair of corners in both rooms and moves with them. Walls (floor.walls) are
// the rooms' edges with a stable id per corner pair; openings sit on a wall at
// `offset` metres (their centre) from the wall's corner `a`. Device
// placements are free points: editing rooms never moves or drops them.

export const FORMAT = "smart_lighting.floorplan";
export const SCHEMA_VERSION = 1;

export interface Vertex {
  id: string;
  x: number;
  y: number;
}

export interface Room {
  id: string;
  name: string;
  /** Home Assistant area (stable area_id), or null when not linked. */
  area_id: string | null;
  /** The area's name when it was linked (to notice a rename in Home Assistant). */
  area_name: string | null;
  /** Part of a space whose Home Assistant area is shared with other drawn rooms (open plan). */
  shares_area: boolean;
  vertices: string[];
}

export interface Wall {
  id: string;
  a: string;
  b: string;
}

export type OpeningType = "door" | "window" | "opening";

export interface Opening {
  id: string;
  type: OpeningType;
  wall_id: string;
  /** Distance of the opening's centre from the wall's corner `a` (m). */
  offset: number;
  width: number;
  swing: "left" | "right" | null;
  /**
   * Phase A5: the curtain window (curtain subsystem) this window shows, or null. Only a `window` opening
   * carries it; the floor plan is the only place the link lives (covers, presets and orientation stay
   * in the curtain configuration). It may dangle after the curtain window is deleted.
   */
  curtain_window_id: string | null;
}

export interface Placement {
  id: string;
  entity_id: string;
  /** Entity registry id: survives an entity id rename in Home Assistant. */
  entity_uid: string | null;
  device_id: string | null;
  x: number;
  y: number;
  label: string | null;
}

export interface Background {
  image_id: string;
  width_px: number;
  height_px: number;
  /** World position (m) of the image's top-left pixel. */
  x: number;
  y: number;
  /** Metres per image pixel. */
  scale: number;
  /** Degrees clockwise about the top-left corner. */
  rotation: number;
  opacity: number;
  visible: boolean;
  locked: boolean;
}

export interface Floor {
  id: string;
  name: string;
  level: number;
  ha_floor_id: string | null;
  measured: boolean;
  vertices: Vertex[];
  rooms: Room[];
  walls: Wall[];
  openings: Opening[];
  placements: Placement[];
  background: Background | null;
}

export interface Plan {
  format: string;
  version: number;
  building: { id: string; name: string; north_deg: number | null };
  floors: Floor[];
  dismissed: string[];
  updated_at: string | null;
}

export const OPENING_WIDTH: Record<OpeningType, number> = { door: 0.9, window: 1.2, opening: 1.6 };
export const OPENING_LABEL: Record<OpeningType, string> = { door: "Door", window: "Window", opening: "Open-plan opening" };

export function emptyPlan(): Plan {
  return { format: FORMAT, version: SCHEMA_VERSION, building: { id: "bld_home", name: "", north_deg: null }, floors: [], dismissed: [], updated_at: null };
}

let counter = 0;
/** A new identifier, e.g. "rm_k3x9q2ab" (prefix + random; matches the backend's pattern). */
export function newId(prefix: string): string {
  counter = (counter + 1) % 1296;
  const random = Math.random().toString(36).slice(2, 10).padEnd(8, "0");
  return `${prefix}_${random}${counter.toString(36).padStart(2, "0")}`;
}

export function newFloor(name: string, level: number): Floor {
  return { id: newId("fl"), name, level, ha_floor_id: null, measured: false, vertices: [], rooms: [], walls: [], openings: [], placements: [], background: null };
}

export function clonePlan(plan: Plan): Plan {
  return structuredClone(plan);
}

export function samePlan(a: Plan | null, b: Plan | null): boolean {
  if (!a || !b) return a === b;
  const strip = (p: Plan) => JSON.stringify({ ...p, updated_at: null });
  return strip(a) === strip(b);
}

export function floorById(plan: Plan, id: string | null | undefined): Floor | undefined {
  return plan.floors.find((f) => f.id === id);
}

export function sortedFloors(plan: Plan): Floor[] {
  return [...plan.floors].sort((a, b) => b.level - a.level || a.name.localeCompare(b.name));
}

export function roomLabel(room: Room, areaName?: (id: string) => string | undefined): string {
  if (room.name) return room.name;
  if (room.area_id) return areaName?.(room.area_id) ?? room.area_id;
  return "Unnamed room";
}

/** Every room linked to this area, across floors. */
export function roomsForArea(plan: Plan, areaId: string): Array<{ floor: Floor; room: Room }> {
  const out: Array<{ floor: Floor; room: Room }> = [];
  for (const floor of plan.floors) for (const room of floor.rooms) if (room.area_id === areaId) out.push({ floor, room });
  return out;
}

/** Duplicate area links that are not modelled as one shared space (the backend refuses to save them). */
export function duplicateAreaLinks(plan: Plan): string[] {
  const by = new Map<string, Room[]>();
  for (const f of plan.floors) for (const r of f.rooms) if (r.area_id) by.set(r.area_id, [...(by.get(r.area_id) ?? []), r]);
  return [...by.entries()].filter(([, rooms]) => rooms.length > 1 && !rooms.every((r) => r.shares_area)).map(([a]) => a);
}
