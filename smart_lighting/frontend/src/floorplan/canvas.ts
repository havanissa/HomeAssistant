// The interactive floor plan canvas (SVG). It renders one floor and turns
// pointer input (mouse, touch and pen through Pointer Events) into edits,
// which it reports to the page as new floors; the page keeps the plan,
// undo history and saving. World units are metres (see model.ts).
//
// Input: drag on empty space (or with the middle button, or two fingers) pans;
// the wheel or a pinch zooms about the pointer. Snapping: corners, walls and
// the grid (hold Alt to place freely; Shift keeps drawn walls at 45° steps).
import { html, LitElement, nothing, svg, type SVGTemplateResult } from "lit";

import { icon, ICONS } from "../icons";
import {
  addOpening,
  addPolygonRoom,
  addRectRoom,
  insertVertex,
  moveRoom,
  moveVertex,
  moveWall,
  nearestWall,
  openingGeometry,
  pairKey,
  roomAt,
  roomPoints,
  updateOpening,
  vertexMap,
  wallChain,
  wallEnds,
  wallUse,
  movePlacement,
} from "./edit";
import {
  area,
  boundsOf,
  centroid,
  dist,
  distToSegment,
  fitView,
  imageToWorld,
  pointInPolygon,
  projectToSegment,
  round,
  snapAngle,
  snapToGrid,
  sub,
  toWorld,
  zoomAt,
  type Pt,
  type View,
} from "./geometry";
import { OPENING_WIDTH, type Floor, type OpeningType } from "./model";
import { formatArea, formatLength, type Units } from "./units";

export type Mode = "build" | "configure" | "live";
export type Tool = "select" | "rect" | "polygon" | "door" | "window" | "opening" | "calibrate";
export type SelectionKind = "room" | "wall" | "vertex" | "opening" | "placement" | "background";
export interface Selection {
  kind: SelectionKind;
  id: string;
}

export interface RoomView {
  /** CSS class suffix for the fill: build, linked, unlinked, problem, occupied, possible, ... */
  tone: string;
  title: string;
  subtitle?: string | null;
  detail?: string | null;
}

export interface PlacementView {
  id: string;
  x: number;
  y: number;
  icon: string;
  /** active | idle | off | unknown | unavailable | missing | placed */
  tone: string;
  title: string;
  value?: string | null;
  badge?: "!" | "?" | null;
  dim?: boolean;
}

export interface EditDetail {
  floor: Floor;
  label: string;
  select?: Selection | null;
  /** Switch back to the select tool after this edit. */
  done?: boolean;
}

type Drag =
  | { kind: "pan"; start: Pt; view: View }
  | { kind: "vertex" | "wall" | "room" | "opening" | "placement" | "background"; id: string; start: Pt; startWorld: Pt; base: Floor; moved: boolean }
  | { kind: "rect"; start: Pt; startWorld: Pt; moved: boolean };

interface SnapResult {
  point: Pt;
  kind: "corner" | "wall" | "grid" | "none";
}

const TAP = 5; // px: a press that moves less is a click
const HANDLE = 11; // px: corner handle hit radius
const WALL_HIT = 8; // px
const DEVICE_R = 15; // px

export class SlPlanCanvas extends LitElement {
  static properties = {
    floor: { attribute: false },
    mode: { type: String },
    tool: { type: String },
    selection: { attribute: false },
    grid: { type: Number },
    snap: { type: Boolean },
    showDims: { type: Boolean },
    showValues: { type: Boolean },
    units: { type: String },
    readonly: { type: Boolean },
    chain: { type: Boolean },
    rooms: { attribute: false },
    placements: { attribute: false },
    placing: { attribute: false },
    imageUrl: { attribute: false },
    preview: { state: true },
    error: { state: true },
    poly: { state: true },
    hover: { state: true },
    calib: { state: true },
  };
  declare floor: Floor | null;
  declare mode: Mode;
  declare tool: Tool;
  declare selection: Selection | null;
  declare grid: number;
  declare snap: boolean;
  declare showDims: boolean;
  declare showValues: boolean;
  declare units: Units;
  declare readonly: boolean;
  declare chain: boolean;
  declare rooms: Map<string, RoomView>;
  declare placements: PlacementView[];
  declare placing: { entity_id: string; name: string } | null;
  declare imageUrl: (id: string) => string;
  declare preview: Floor | null;
  declare error: string | null;
  declare poly: Pt[];
  declare hover: SnapResult | null;
  declare calib: Pt[];

  view: View = { scale: 40, tx: 48, ty: 48 };
  private views = new Map<string, View>();
  private shownFloor: string | null = null;
  private size = { w: 0, h: 0 };
  private drag: Drag | null = null;
  private rectStart: Pt | null = null;
  private pointers = new Map<number, Pt>();
  private pinch: { d: number; c: Pt; view: View } | null = null;
  private observer: ResizeObserver | null = null;
  private lastTap = { t: 0, x: 0, y: 0 };

  constructor() {
    super();
    this.floor = null;
    this.mode = "build";
    this.tool = "select";
    this.selection = null;
    this.grid = 0.1;
    this.snap = true;
    this.showDims = false;
    this.showValues = true;
    this.units = "metric";
    this.readonly = false;
    this.chain = true;
    this.rooms = new Map();
    this.placements = [];
    this.placing = null;
    this.imageUrl = () => "";
    this.preview = null;
    this.error = null;
    this.poly = [];
    this.hover = null;
    this.calib = [];
  }

  protected createRenderRoot(): HTMLElement {
    return this;
  }

  connectedCallback(): void {
    super.connectedCallback();
    this.observer = new ResizeObserver(() => this.measure());
    this.observer.observe(this);
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this.observer?.disconnect();
  }

  private measure(): void {
    const r = this.getBoundingClientRect();
    const first = this.size.w === 0;
    this.size = { w: r.width, h: r.height };
    if (first && this.floor) this.fit();
    this.requestUpdate();
  }

  protected willUpdate(changed: Map<string, unknown>): void {
    if (changed.has("floor") && this.floor && this.floor.id !== this.shownFloor) {
      if (this.shownFloor) this.views.set(this.shownFloor, this.view);
      this.shownFloor = this.floor.id;
      const saved = this.views.get(this.floor.id);
      if (saved) this.view = saved;
      else this.fit();
      this.resetTool();
    }
    if (changed.has("tool") || changed.has("mode")) this.resetTool();
  }

  resetTool(): void {
    this.poly = [];
    this.calib = [];
    this.rectStart = null;
    this.preview = null;
    this.drag = null;
    this.error = null;
  }

  // ------------------------------------------------------------- view
  private worldPoints(floor: Floor): Pt[] {
    const out: Pt[] = [...floor.vertices, ...floor.placements];
    const bg = floor.background;
    if (bg && bg.visible) for (const [u, v] of [[0, 0], [bg.width_px, 0], [0, bg.height_px], [bg.width_px, bg.height_px]]) out.push(imageToWorld(bg, u, v));
    return out;
  }

  fit(): void {
    if (!this.floor) return;
    const b = boundsOf(this.worldPoints(this.floor));
    this.view = b ? fitView(b, this.size.w, this.size.h) : { scale: 40, tx: 48, ty: 48 };
    this.requestUpdate();
  }

  resetView(): void {
    this.view = { scale: 40, tx: 48, ty: 48 };
    this.requestUpdate();
  }

  zoomBy(factor: number): void {
    this.view = zoomAt(this.view, factor, { x: this.size.w / 2, y: this.size.h / 2 });
    this.requestUpdate();
  }

  /** Centre the view on a world point (keeps the zoom). */
  centreOn(p: Pt): void {
    this.view = { ...this.view, tx: this.size.w / 2 - p.x * this.view.scale, ty: this.size.h / 2 - p.y * this.view.scale };
    this.requestUpdate();
  }

  private local(e: PointerEvent | MouseEvent | DragEvent): Pt {
    const r = this.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  private emit<T>(name: string, detail: T): void {
    this.dispatchEvent(new CustomEvent(name, { detail, bubbles: true, composed: true }));
  }

  private select(selection: Selection | null): void {
    this.emit("plan-select", selection);
  }

  private commit(detail: EditDetail): void {
    this.emit("plan-edit", detail);
  }

  private fail(message: string): void {
    this.emit("plan-notice", message);
  }

  // ----------------------------------------------------------- snapping
  private snapPoint(world: Pt, e: { altKey: boolean }, exclude: Set<string> = new Set()): SnapResult {
    const floor = this.floor;
    if (!floor || e.altKey) return { point: { x: round(world.x), y: round(world.y) }, kind: "none" };
    const s = this.view.scale;
    let best: Pt | null = null;
    let bestD = HANDLE / s;
    for (const v of floor.vertices) {
      if (exclude.has(v.id)) continue;
      const d = dist(v, world);
      if (d < bestD) {
        bestD = d;
        best = { x: v.x, y: v.y };
      }
    }
    if (best) return { point: best, kind: "corner" };
    const map = vertexMap(floor);
    let wallBest: Pt | null = null;
    let wallD = WALL_HIT / s;
    for (const w of floor.walls) {
      if (exclude.has(w.a) || exclude.has(w.b)) continue;
      const ends = wallEnds(floor, w, map);
      if (!ends) continue;
      const { point } = projectToSegment(world, ends[0], ends[1]);
      const d = dist(point, world);
      if (d < wallD) {
        wallD = d;
        wallBest = point;
      }
    }
    if (wallBest) {
      // On a wall: prefer the grid crossing along it when the wall is straight across the grid.
      if (this.snap && this.grid > 0) {
        const g = snapToGrid(wallBest, this.grid);
        if (dist(g, wallBest) < 1e-6 || Math.abs(g.x - wallBest.x) < 1e-6 || Math.abs(g.y - wallBest.y) < 1e-6) {
          const along = { x: Math.abs(g.x - wallBest.x) < 1e-6 ? g.x : wallBest.x, y: Math.abs(g.y - wallBest.y) < 1e-6 ? g.y : wallBest.y };
          const wall = nearestWall(floor, along, 1e-4);
          if (wall) return { point: { x: round(along.x), y: round(along.y) }, kind: "wall" };
        }
      }
      return { point: { x: round(wallBest.x), y: round(wallBest.y) }, kind: "wall" };
    }
    if (this.snap && this.grid > 0) return { point: snapToGrid(world, this.grid), kind: "grid" };
    return { point: { x: round(world.x), y: round(world.y) }, kind: "none" };
  }

  private snapDelta(d: Pt, e: { altKey: boolean }): Pt {
    if (!this.snap || e.altKey || !(this.grid > 0)) return { x: round(d.x), y: round(d.y) };
    return snapToGrid(d, this.grid);
  }

  // --------------------------------------------------------- hit tests
  private hitVertex(floor: Floor, w: Pt): string | null {
    if (this.mode !== "build") return null;
    const visible = this.handleVertices(floor);
    let best: string | null = null;
    let bestD = HANDLE / this.view.scale;
    for (const v of floor.vertices) {
      if (!visible.has(v.id)) continue;
      const d = dist(v, w);
      if (d < bestD) {
        bestD = d;
        best = v.id;
      }
    }
    return best;
  }

  private hitOpening(floor: Floor, w: Pt): string | null {
    if (this.mode !== "build") return null;
    const map = vertexMap(floor);
    for (const o of floor.openings) {
      const g = openingGeometry(floor, o, map);
      if (g && distToSegment(w, g.start, g.end) < WALL_HIT / this.view.scale) return o.id;
    }
    return null;
  }

  private hitWall(floor: Floor, w: Pt): string | null {
    if (this.mode !== "build") return null;
    return nearestWall(floor, w, WALL_HIT / this.view.scale)?.wall.id ?? null;
  }

  private hitPlacement(w: Pt): string | null {
    if (this.mode === "build") return null;
    const r = (DEVICE_R + 2) / this.view.scale;
    for (let i = this.placements.length - 1; i >= 0; i--) {
      const p = this.placements[i];
      if (dist(p, w) <= r) return p.id;
    }
    return null;
  }

  private hitBackground(floor: Floor, w: Pt): boolean {
    const bg = floor.background;
    if (!bg || !bg.visible || this.mode !== "build") return false;
    const corners = [imageToWorld(bg, 0, 0), imageToWorld(bg, bg.width_px, 0), imageToWorld(bg, bg.width_px, bg.height_px), imageToWorld(bg, 0, bg.height_px)];
    return pointInPolygon(w, corners);
  }

  /** Corners with a handle: those of the selected room, wall or corner. */
  private handleVertices(floor: Floor): Set<string> {
    const sel = this.selection;
    if (!sel || this.mode !== "build") return new Set();
    if (sel.kind === "room") return new Set(floor.rooms.find((r) => r.id === sel.id)?.vertices ?? []);
    if (sel.kind === "wall") {
      const w = floor.walls.find((x) => x.id === sel.id);
      return new Set(w ? [w.a, w.b] : []);
    }
    if (sel.kind === "vertex") {
      const rooms = floor.rooms.filter((r) => r.vertices.includes(sel.id));
      return new Set(rooms.flatMap((r) => r.vertices));
    }
    return new Set();
  }

  // ------------------------------------------------------------ pointer
  private onPointerDown = (e: PointerEvent): void => {
    if (!this.floor) return;
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
    const at = this.local(e);
    this.pointers.set(e.pointerId, at);
    (this.querySelector(".plan-canvas") as HTMLElement | null)?.focus({ preventScroll: true });
    if (this.pointers.size === 2) {
      // Two fingers: pinch to zoom and pan; cancel anything that started with the first finger.
      const [a, b] = [...this.pointers.values()];
      this.pinch = { d: dist(a, b), c: { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, view: this.view };
      this.drag = null;
      this.preview = null;
      return;
    }
    if (this.pointers.size > 2) return;
    const world = toWorld(this.view, at);
    if (e.button === 1 || e.button === 2) {
      this.drag = { kind: "pan", start: at, view: this.view };
      return;
    }
    const floor = this.floor;
    const editable = !this.readonly;
    if (this.mode === "build" && editable && this.tool !== "select") {
      this.toolDown(e, at, world);
      return;
    }
    if (this.mode === "configure" && this.placing && editable) {
      this.emit("plan-place", { x: round(world.x, 3), y: round(world.y, 3), entity_id: this.placing.entity_id });
      return;
    }
    const vertex = this.hitVertex(floor, world);
    const opening = vertex ? null : this.hitOpening(floor, world);
    const wall = vertex || opening ? null : this.hitWall(floor, world);
    const placement = vertex || opening || wall ? null : this.hitPlacement(world);
    const room = vertex || opening || wall || placement ? null : roomAt(floor, world);
    const bg = !vertex && !opening && !wall && !placement && !room && this.hitBackground(floor, world);
    const base = floor;
    const draggable = editable && this.mode !== "live";
    if (vertex) {
      this.select({ kind: "vertex", id: vertex });
      if (draggable) this.drag = { kind: "vertex", id: vertex, start: at, startWorld: world, base, moved: false };
    } else if (opening) {
      this.select({ kind: "opening", id: opening });
      if (draggable) this.drag = { kind: "opening", id: opening, start: at, startWorld: world, base, moved: false };
    } else if (wall) {
      this.select({ kind: "wall", id: wall });
      if (draggable) this.drag = { kind: "wall", id: wall, start: at, startWorld: world, base, moved: false };
    } else if (placement) {
      this.select({ kind: "placement", id: placement });
      if (draggable) this.drag = { kind: "placement", id: placement, start: at, startWorld: world, base, moved: false };
    } else if (room) {
      const already = this.selection?.kind === "room" && this.selection.id === room.id;
      this.select({ kind: "room", id: room.id });
      // A room moves only once it is selected (so a click on it to select never shifts it by accident).
      if (draggable && already && this.mode === "build") this.drag = { kind: "room", id: room.id, start: at, startWorld: world, base, moved: false };
      else this.drag = { kind: "pan", start: at, view: this.view };
    } else if (bg) {
      this.select({ kind: "background", id: floor.id });
      if (draggable && !floor.background!.locked) this.drag = { kind: "background", id: floor.id, start: at, startWorld: world, base, moved: false };
      else this.drag = { kind: "pan", start: at, view: this.view };
    } else {
      this.select(null);
      this.drag = { kind: "pan", start: at, view: this.view };
    }
  };

  private toolDown(e: PointerEvent, at: Pt, world: Pt): void {
    const floor = this.floor!;
    const snapped = this.snapPoint(world, e).point;
    switch (this.tool) {
      case "rect":
        if (this.rectStart) {
          const r = addRectRoom(floor, this.rectStart, snapped, this.nextName(floor));
          this.rectStart = null;
          this.preview = null;
          if (r.ok) this.commit({ floor: r.value.floor, label: "Draw room", select: { kind: "room", id: r.value.roomId }, done: true });
          else this.fail(r.error);
        } else this.drag = { kind: "rect", start: at, startWorld: snapped, moved: false };
        return;
      case "polygon": {
        let point = snapped;
        if (e.shiftKey && this.poly.length) point = snapAngle(this.poly[this.poly.length - 1], snapped);
        const now = Date.now();
        const double = now - this.lastTap.t < 350 && Math.hypot(at.x - this.lastTap.x, at.y - this.lastTap.y) < 8;
        this.lastTap = { t: now, x: at.x, y: at.y };
        if (this.poly.length >= 3 && (dist(point, this.poly[0]) * this.view.scale < HANDLE || double)) {
          this.finishPolygon();
          return;
        }
        if (this.poly.length && dist(point, this.poly[this.poly.length - 1]) < 0.01) return;
        this.poly = [...this.poly, point];
        return;
      }
      case "door":
      case "window":
      case "opening": {
        const hit = nearestWall(floor, world, 16 / this.view.scale);
        if (!hit) {
          this.fail("Click on a wall to add it.");
          return;
        }
        const type = this.tool as OpeningType;
        const offset = this.snap && !e.altKey ? Math.round(hit.offset / 0.05) * 0.05 : hit.offset;
        const r = addOpening(floor, hit.wall.id, type, offset, Math.min(OPENING_WIDTH[type], hit.length));
        if (r.ok) this.commit({ floor: r.value.floor, label: `Add ${type === "opening" ? "opening" : type}`, select: { kind: "opening", id: r.value.openingId } });
        else this.fail(r.error);
        return;
      }
      case "calibrate": {
        const next = [...this.calib, snapped];
        if (next.length === 2) {
          this.calib = [];
          this.emit("plan-calibrate", { p1: next[0], p2: next[1] });
        } else this.calib = next;
        return;
      }
      default:
        return;
    }
  }

  finishPolygon(): void {
    if (!this.floor || this.poly.length < 3) {
      this.fail("A room needs at least 3 corners.");
      return;
    }
    const r = addPolygonRoom(this.floor, this.poly, this.nextName(this.floor));
    this.poly = [];
    if (r.ok) this.commit({ floor: r.value.floor, label: "Draw room", select: { kind: "room", id: r.value.roomId }, done: true });
    else this.fail(r.error);
  }

  /** "Room 3": a name to tell new rooms apart until they are named or linked. */
  private nextName(floor: Floor): string {
    const used = new Set(floor.rooms.map((r) => r.name));
    let n = floor.rooms.length + 1;
    while (used.has(`Room ${n}`)) n += 1;
    return `Room ${n}`;
  }

  undoPoint(): void {
    this.poly = this.poly.slice(0, -1);
  }

  private onPointerMove = (e: PointerEvent): void => {
    if (!this.floor) return;
    const at = this.local(e);
    if (this.pointers.has(e.pointerId)) this.pointers.set(e.pointerId, at);
    if (this.pinch && this.pointers.size >= 2) {
      const [a, b] = [...this.pointers.values()];
      const c = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const factor = dist(a, b) / Math.max(this.pinch.d, 1);
      const v = zoomAt(this.pinch.view, factor, this.pinch.c);
      this.view = { ...v, tx: v.tx + (c.x - this.pinch.c.x), ty: v.ty + (c.y - this.pinch.c.y) };
      this.requestUpdate();
      return;
    }
    const world = toWorld(this.view, at);
    const drag = this.drag;
    if (!drag) {
      // Hover: show where a click would snap or place.
      if (this.mode === "build" && this.tool !== "select") {
        this.hover = this.snapPoint(world, e);
        if (this.tool === "polygon" && e.shiftKey && this.poly.length) this.hover = { ...this.hover, point: snapAngle(this.poly[this.poly.length - 1], this.hover.point) };
        if (this.tool === "rect" && this.rectStart) this.updateRectPreview(this.rectStart, this.hover.point);
      } else if (this.hover) this.hover = null;
      return;
    }
    if (drag.kind === "pan") {
      this.view = { ...drag.view, tx: drag.view.tx + at.x - drag.start.x, ty: drag.view.ty + at.y - drag.start.y };
      this.requestUpdate();
      return;
    }
    const moved = Math.hypot(at.x - drag.start.x, at.y - drag.start.y) > TAP;
    if (!moved && !drag.moved) return;
    drag.moved = true;
    if (drag.kind === "rect") {
      const end = this.snapPoint(world, e).point;
      this.hover = { point: end, kind: "grid" };
      this.updateRectPreview(drag.startWorld, end);
      return;
    }
    const base = drag.base;
    const delta = sub(world, drag.startWorld);
    let result: { ok: true; value: Floor } | { ok: false; error: string };
    switch (drag.kind) {
      case "vertex": {
        const target = this.snapPoint(world, e, new Set([drag.id])).point;
        this.hover = { point: target, kind: "corner" };
        result = moveVertex(base, drag.id, target);
        break;
      }
      case "wall":
        result = moveWall(base, drag.id, this.snapDelta(delta, e), this.chain && !e.shiftKey);
        break;
      case "room":
        result = moveRoom(base, drag.id, this.snapDelta(delta, e), true);
        break;
      case "opening": {
        const o = base.openings.find((x) => x.id === drag.id);
        const wall = o ? base.walls.find((w) => w.id === o.wall_id) : null;
        const ends = wall ? wallEnds(base, wall) : null;
        if (!o || !ends) return;
        const { t } = projectToSegment(world, ends[0], ends[1]);
        const raw = t * dist(ends[0], ends[1]);
        result = updateOpening(base, o.id, { offset: this.snap && !e.altKey ? Math.round(raw / 0.05) * 0.05 : raw });
        break;
      }
      case "placement": {
        const p = base.placements.find((x) => x.id === drag.id);
        if (!p) return;
        result = { ok: true, value: movePlacement(base, p.id, { x: p.x + delta.x, y: p.y + delta.y }) };
        break;
      }
      case "background": {
        const bg = base.background;
        if (!bg) return;
        result = { ok: true, value: { ...base, background: { ...bg, x: round(bg.x + delta.x), y: round(bg.y + delta.y) } } };
        break;
      }
    }
    if (result.ok) {
      this.preview = result.value;
      this.error = null;
    } else this.error = result.error;
  };

  private updateRectPreview(a: Pt, b: Pt): void {
    if (!this.floor) return;
    const r = addRectRoom(this.floor, a, b);
    this.preview = r.ok ? r.value.floor : null;
    this.error = r.ok ? null : Math.abs(a.x - b.x) > 0.05 || Math.abs(a.y - b.y) > 0.05 ? r.error : null;
    this.requestUpdate();
  }

  private onPointerUp = (e: PointerEvent): void => {
    this.pointers.delete(e.pointerId);
    if (this.pinch) {
      if (this.pointers.size < 2) this.pinch = null;
      return;
    }
    const drag = this.drag;
    this.drag = null;
    if (!drag || drag.kind === "pan") return;
    if (drag.kind === "rect") {
      const world = toWorld(this.view, this.local(e));
      if (!drag.moved) {
        this.rectStart = drag.startWorld; // click-click: the next click sets the other corner
        return;
      }
      const r = addRectRoom(this.floor!, drag.startWorld, this.snapPoint(world, e).point, this.nextName(this.floor!));
      this.preview = null;
      this.hover = null;
      if (r.ok) this.commit({ floor: r.value.floor, label: "Draw room", select: { kind: "room", id: r.value.roomId }, done: true });
      else this.fail(r.error);
      return;
    }
    const preview = this.preview;
    const error = this.error;
    this.preview = null;
    this.error = null;
    this.hover = null;
    if (!drag.moved) return;
    if (error || !preview) {
      if (error) this.fail(`${error} The change was not applied.`);
      return;
    }
    const labels: Record<string, string> = { vertex: "Move corner", wall: "Move wall", room: "Move room", opening: "Move opening", placement: "Move device icon", background: "Move background" };
    this.commit({ floor: preview, label: labels[drag.kind] });
  };

  private onDoubleClick = (e: MouseEvent): void => {
    if (!this.floor || this.readonly || this.mode !== "build" || this.tool !== "select") return;
    const world = toWorld(this.view, this.local(e));
    const wall = this.hitWall(this.floor, world);
    if (!wall) return;
    const r = insertVertex(this.floor, wall, world);
    if (r.ok) this.commit({ floor: r.value.floor, label: "Add corner", select: { kind: "vertex", id: r.value.vertexId } });
    else this.fail(r.error);
  };

  private onWheel = {
    handleEvent: (e: WheelEvent): void => {
      e.preventDefault();
      const factor = Math.exp(-(e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY) * (e.ctrlKey ? 0.01 : 0.0015));
      this.view = zoomAt(this.view, factor, this.local(e));
      this.requestUpdate();
    },
    passive: false,
  };

  private onDragOver = (e: DragEvent): void => {
    if (this.mode === "configure" && !this.readonly && e.dataTransfer?.types.includes("application/x-sl-entity")) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  };

  private onDrop = (e: DragEvent): void => {
    const entity = e.dataTransfer?.getData("application/x-sl-entity");
    if (!entity || this.mode !== "configure" || this.readonly) return;
    e.preventDefault();
    const w = toWorld(this.view, this.local(e));
    this.emit("plan-place", { x: round(w.x, 3), y: round(w.y, 3), entity_id: entity });
  };

  // ---------------------------------------------------------- rendering
  private renderGrid(): SVGTemplateResult {
    const s = this.view.scale;
    let minor = this.grid > 0 ? this.grid : 0.5;
    while (minor * s < 8) minor *= minor < 1 ? 2 : 5;
    const major = minor < 1 ? 1 : minor * 5;
    const ms = minor * s;
    const Ms = major * s;
    const mod = (a: number, n: number) => ((a % n) + n) % n;
    return svg`<defs>
        <pattern id="fp-grid-minor" patternUnits="userSpaceOnUse" width=${ms} height=${ms} x=${mod(this.view.tx, ms)} y=${mod(this.view.ty, ms)}>
          <path d="M ${ms} 0 L 0 0 0 ${ms}" class="fp-grid-minor" fill="none"></path></pattern>
        <pattern id="fp-grid-major" patternUnits="userSpaceOnUse" width=${Ms} height=${Ms} x=${mod(this.view.tx, Ms)} y=${mod(this.view.ty, Ms)}>
          <path d="M ${Ms} 0 L 0 0 0 ${Ms}" class="fp-grid-major" fill="none"></path></pattern>
        <pattern id="fp-hatch" patternUnits="userSpaceOnUse" width=${8 / s} height=${8 / s} patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2=${8 / s} class="fp-hatch-line"></line></pattern>
      </defs>
      <rect class="fp-grid-bg" width="100%" height="100%"></rect>
      ${this.mode === "build" ? svg`<rect width="100%" height="100%" fill="url(#fp-grid-minor)"></rect>` : nothing}
      <rect width="100%" height="100%" fill="url(#fp-grid-major)" class=${this.mode === "build" ? "" : "faint"}></rect>`;
  }

  private renderRooms(floor: Floor): SVGTemplateResult {
    const map = vertexMap(floor);
    const s = this.view.scale;
    return svg`${floor.rooms.map((room) => {
      const pts = roomPoints(floor, room, map);
      if (pts.length < 3) return nothing;
      const view = this.rooms.get(room.id);
      const selected = this.selection?.kind === "room" && this.selection.id === room.id;
      const d = pts.map((p, i) => `${i ? "L" : "M"}${p.x} ${p.y}`).join(" ") + " Z";
      return svg`<g class="fp-room tone-${view?.tone ?? "build"} ${selected ? "selected" : ""}" data-room=${room.id}>
        <path d=${d} class="fp-room-fill"></path>
        ${view?.tone === "uncertain" || view?.tone === "unavailable" ? svg`<path d=${d} class="fp-room-hatch" fill="url(#fp-hatch)"></path>` : nothing}
        <path d=${d} class="fp-room-outline"></path>
      </g>`;
    })}${floor.rooms.map((room) => {
      const pts = roomPoints(floor, room, map);
      if (pts.length < 3) return nothing;
      const view = this.rooms.get(room.id);
      let c = centroid(pts);
      if (!pointInPolygon(c, pts)) c = pts.reduce((a, p) => ({ x: a.x + p.x / pts.length, y: a.y + p.y / pts.length }), { x: 0, y: 0 });
      const lines = [view?.subtitle, this.showDims ? formatArea(area(pts), this.units) : null, view?.detail].filter(Boolean) as string[];
      const fs = 13 / s;
      return svg`<g class="fp-label" transform="translate(${c.x} ${c.y})">
        <text class="fp-room-name" font-size=${fs} y=${(-lines.length * 0.6 * fs).toString()}>${view?.title ?? room.name}</text>
        ${lines.map((l, i) => svg`<text class="fp-room-sub" font-size=${11 / s} y=${(-lines.length * 0.6 * fs + (i + 1) * 1.25 * fs).toString()}>${l}</text>`)}
      </g>`;
    })}`;
  }

  private renderWalls(floor: Floor): SVGTemplateResult {
    const map = vertexMap(floor);
    const use = wallUse(floor);
    const sel = this.selection;
    const chain = sel?.kind === "wall" && this.mode === "build" && this.chain ? new Set(wallChain(floor, sel.id)) : null;
    return svg`${floor.walls.map((w) => {
      const ends = wallEnds(floor, w, map);
      if (!ends) return nothing;
      const exterior = (use.get(pairKey(w.a, w.b)) ?? []).length < 2;
      const selected = sel?.kind === "wall" && sel.id === w.id;
      const inChain = chain && chain.has(w.a) && chain.has(w.b) && !selected;
      return svg`<line x1=${ends[0].x} y1=${ends[0].y} x2=${ends[1].x} y2=${ends[1].y}
        class="fp-wall ${exterior ? "exterior" : "interior"} ${selected ? "selected" : ""} ${inChain ? "chain" : ""}"></line>`;
    })}`;
  }

  private renderOpenings(floor: Floor): SVGTemplateResult {
    const map = vertexMap(floor);
    return svg`${floor.openings.map((o) => {
      const g = openingGeometry(floor, o, map);
      if (!g) return nothing;
      const selected = this.selection?.kind === "opening" && this.selection.id === o.id;
      const cls = `fp-opening ${o.type} ${selected ? "selected" : ""}`;
      const gap = svg`<line x1=${g.start.x} y1=${g.start.y} x2=${g.end.x} y2=${g.end.y} class="fp-gap"></line>`;
      if (o.type === "window") {
        const off = 0.05;
        const n = g.normal;
        return svg`<g class=${cls}>${gap}
          <line x1=${g.start.x + n.x * off} y1=${g.start.y + n.y * off} x2=${g.end.x + n.x * off} y2=${g.end.y + n.y * off} class="fp-glass"></line>
          <line x1=${g.start.x - n.x * off} y1=${g.start.y - n.y * off} x2=${g.end.x - n.x * off} y2=${g.end.y - n.y * off} class="fp-glass"></line>
          <line x1=${g.start.x} y1=${g.start.y} x2=${g.end.x} y2=${g.end.y} class="fp-sill"></line></g>`;
      }
      if (o.type === "opening") {
        return svg`<g class=${cls}>${gap}<line x1=${g.start.x} y1=${g.start.y} x2=${g.end.x} y2=${g.end.y} class="fp-open-line"></line></g>`;
      }
      const hinge = o.swing === "right" ? g.end : g.start;
      const other = o.swing === "right" ? g.start : g.end;
      const w = dist(g.start, g.end);
      const leaf = { x: hinge.x + g.normal.x * w, y: hinge.y + g.normal.y * w };
      const v1 = sub(leaf, hinge);
      const v2 = sub(other, hinge);
      const sweep = v1.x * v2.y - v1.y * v2.x > 0 ? 1 : 0;
      return svg`<g class=${cls}>${gap}
        <line x1=${hinge.x} y1=${hinge.y} x2=${leaf.x} y2=${leaf.y} class="fp-leaf"></line>
        <path d="M ${leaf.x} ${leaf.y} A ${w} ${w} 0 0 ${sweep} ${other.x} ${other.y}" class="fp-swing"></path></g>`;
    })}`;
  }

  private renderDims(floor: Floor): SVGTemplateResult | typeof nothing {
    if (!this.showDims) return nothing;
    const map = vertexMap(floor);
    const s = this.view.scale;
    return svg`${floor.walls.map((w) => {
      const ends = wallEnds(floor, w, map);
      if (!ends) return nothing;
      const l = dist(ends[0], ends[1]);
      if (l * s < 56) return nothing;
      const mid = { x: (ends[0].x + ends[1].x) / 2, y: (ends[0].y + ends[1].y) / 2 };
      let angle = (Math.atan2(ends[1].y - ends[0].y, ends[1].x - ends[0].x) * 180) / Math.PI;
      if (angle > 90) angle -= 180;
      if (angle < -90) angle += 180;
      return svg`<text class="fp-dim" font-size=${10 / s} transform="translate(${mid.x} ${mid.y}) rotate(${angle}) translate(0 ${-6 / s})">${formatLength(l, this.units)}</text>`;
    })}`;
  }

  private renderHandles(floor: Floor): SVGTemplateResult | typeof nothing {
    if (this.mode !== "build") return nothing;
    const ids = this.handleVertices(floor);
    if (!ids.size) return nothing;
    const r = 6 / this.view.scale;
    return svg`${floor.vertices.filter((v) => ids.has(v.id)).map((v) => svg`<circle cx=${v.x} cy=${v.y} r=${r}
      class="fp-handle ${this.selection?.kind === "vertex" && this.selection.id === v.id ? "selected" : ""}"></circle>`)}`;
  }

  private renderPlacements(): SVGTemplateResult | typeof nothing {
    if (this.mode === "build") {
      const r = 5 / this.view.scale;
      return svg`${this.placements.map((p) => svg`<circle cx=${p.x} cy=${p.y} r=${r} class="fp-device-dot"></circle>`)}`;
    }
    const k = 1 / this.view.scale;
    return svg`${this.placements.map((p) => {
      const selected = this.selection?.kind === "placement" && this.selection.id === p.id;
      const path = ICONS[p.icon] ?? ICONS.puzzle;
      return svg`<g class="fp-device tone-${p.tone} ${selected ? "selected" : ""} ${p.dim ? "dim" : ""}" transform="translate(${p.x} ${p.y}) scale(${k})" data-placement=${p.id}>
        <title>${p.title}</title>
        ${p.tone === "active" ? svg`<circle r=${DEVICE_R + 6} class="fp-device-glow"></circle>` : nothing}
        <circle r=${DEVICE_R} class="fp-device-body"></circle>
        <path d=${path} transform="translate(-10.5 -10.5) scale(0.875)" class="fp-device-icon"></path>
        ${p.badge ? svg`<g transform="translate(11 -11)"><circle r="7" class="fp-device-badge"></circle><text class="fp-device-badge-text" y="3.5">${p.badge}</text></g>` : nothing}
        ${this.showValues && p.value ? svg`<text class="fp-device-value" y=${DEVICE_R + 13}>${p.value}</text>` : nothing}
      </g>`;
    })}`;
  }

  private renderBackground(floor: Floor): SVGTemplateResult | typeof nothing {
    const bg = floor.background;
    if (!bg || !bg.visible) return nothing;
    const selected = this.selection?.kind === "background";
    const corners = [imageToWorld(bg, 0, 0), imageToWorld(bg, bg.width_px, 0), imageToWorld(bg, bg.width_px, bg.height_px), imageToWorld(bg, 0, bg.height_px)];
    return svg`<image href=${this.imageUrl(bg.image_id)} x="0" y="0" width=${bg.width_px} height=${bg.height_px} preserveAspectRatio="none"
        transform="translate(${bg.x} ${bg.y}) rotate(${bg.rotation}) scale(${bg.scale})" opacity=${this.mode === "build" ? bg.opacity : Math.min(bg.opacity, 0.35)} class="fp-bg"></image>
      ${selected ? svg`<path class="fp-bg-outline" d=${corners.map((p, i) => `${i ? "L" : "M"}${p.x} ${p.y}`).join(" ") + " Z"}></path>` : nothing}`;
  }

  private renderToolOverlay(): SVGTemplateResult | typeof nothing {
    const s = this.view.scale;
    const parts: SVGTemplateResult[] = [];
    const hover = this.hover;
    if (this.mode === "build" && this.tool === "polygon" && this.poly.length) {
      const pts = [...this.poly, ...(hover ? [hover.point] : [])];
      parts.push(svg`<polyline points=${pts.map((p) => `${p.x},${p.y}`).join(" ")} class="fp-draft"></polyline>`);
      parts.push(svg`${this.poly.map((p, i) => svg`<circle cx=${p.x} cy=${p.y} r=${(i === 0 ? 7 : 4) / s} class="fp-draft-point ${i === 0 ? "first" : ""}"></circle>`)}`);
      if (hover) {
        const last = this.poly[this.poly.length - 1];
        const l = dist(last, hover.point);
        if (l * s > 40) parts.push(svg`<text class="fp-dim draft" font-size=${11 / s} x=${(last.x + hover.point.x) / 2} y=${(last.y + hover.point.y) / 2 - 8 / s}>${formatLength(l, this.units)}</text>`);
      }
    }
    if (this.mode === "build" && this.tool === "rect" && this.rectStart && !this.preview) {
      parts.push(svg`<circle cx=${this.rectStart.x} cy=${this.rectStart.y} r=${5 / s} class="fp-draft-point first"></circle>`);
    }
    if (this.mode === "build" && this.tool === "calibrate") {
      const pts = [...this.calib, ...(hover && this.calib.length ? [hover.point] : [])];
      if (pts.length === 2) {
        parts.push(svg`<line x1=${pts[0].x} y1=${pts[0].y} x2=${pts[1].x} y2=${pts[1].y} class="fp-calib"></line>`);
        parts.push(svg`<text class="fp-dim draft" font-size=${11 / s} x=${(pts[0].x + pts[1].x) / 2} y=${(pts[0].y + pts[1].y) / 2 - 8 / s}>${formatLength(dist(pts[0], pts[1]), this.units)} now</text>`);
      }
      parts.push(svg`${this.calib.map((p) => svg`<circle cx=${p.x} cy=${p.y} r=${5 / s} class="fp-calib-point"></circle>`)}`);
    }
    if (this.mode === "build" && ["door", "window", "opening"].includes(this.tool) && hover && this.floor) {
      const hit = nearestWall(this.floor, hover.point, 16 / s);
      if (hit) {
        const ends = wallEnds(this.floor, hit.wall)!;
        const dir = { x: (ends[1].x - ends[0].x) / hit.length, y: (ends[1].y - ends[0].y) / hit.length };
        const w = Math.min(OPENING_WIDTH[this.tool as OpeningType], hit.length);
        const c = Math.min(Math.max(hit.offset, w / 2), hit.length - w / 2);
        const a = { x: ends[0].x + dir.x * (c - w / 2), y: ends[0].y + dir.y * (c - w / 2) };
        const b = { x: ends[0].x + dir.x * (c + w / 2), y: ends[0].y + dir.y * (c + w / 2) };
        parts.push(svg`<line x1=${a.x} y1=${a.y} x2=${b.x} y2=${b.y} class="fp-ghost"></line>`);
      }
    }
    if (hover && this.mode === "build" && (this.tool !== "select" || this.drag)) {
      parts.push(svg`<circle cx=${hover.point.x} cy=${hover.point.y} r=${(hover.kind === "corner" ? 7 : 4) / s} class="fp-snap ${hover.kind}"></circle>`);
    }
    return parts.length ? svg`${parts}` : nothing;
  }

  private hint(): string | null {
    if (this.readonly && this.mode !== "live") return "Read-only: the floor plan cannot be saved right now.";
    if (this.mode === "live") return null;
    if (this.mode === "configure") return this.placing ? `Click or tap on the plan to place ${this.placing.name}. Esc cancels.` : null;
    switch (this.tool) {
      case "rect":
        return this.rectStart ? "Click the opposite corner. Esc cancels." : "Drag to draw a room, or click two opposite corners. Corners snap to walls, corners and the grid (Alt: no snapping).";
      case "polygon":
        return this.poly.length < 3 ? "Click each corner of the room (Shift: 45° steps). Esc cancels." : "Click the first corner, double-click or press Enter to finish. Backspace removes the last corner.";
      case "door":
      case "window":
      case "opening":
        return `Click on a wall to add ${this.tool === "opening" ? "an open-plan opening" : `a ${this.tool}`}. Esc returns to Select.`;
      case "calibrate":
        return this.calib.length ? "Click the second point of the known distance." : "Click two points whose real distance you know (for example the ends of a wall on the image).";
      default:
        return null;
    }
  }

  protected render(): unknown {
    const floor = this.preview ?? this.floor;
    const v = this.view;
    const hint = this.hint();
    return html`<div class="plan-canvas mode-${this.mode} tool-${this.tool} ${this.drag?.kind === "pan" ? "panning" : ""}" tabindex="0"
        role="application" aria-label="Floor plan canvas. Use the tools on the left, or the inspector, to edit."
        @pointerdown=${this.onPointerDown} @pointermove=${this.onPointerMove} @pointerup=${this.onPointerUp} @pointercancel=${this.onPointerUp}
        @dblclick=${this.onDoubleClick} @wheel=${this.onWheel} @dragover=${this.onDragOver} @drop=${this.onDrop}
        @contextmenu=${(e: Event) => e.preventDefault()}>
      <svg class="plan-svg" width="100%" height="100%" data-scale=${v.scale.toFixed(2)}>
        ${this.renderGrid()}
        ${floor
          ? svg`<g transform="matrix(${v.scale} 0 0 ${v.scale} ${v.tx} ${v.ty})">
              ${this.renderBackground(floor)}
              ${this.renderRooms(floor)}
              ${this.renderWalls(floor)}
              ${this.renderOpenings(floor)}
              ${this.renderDims(floor)}
              ${this.renderHandles(floor)}
              ${this.renderPlacements()}
              ${this.renderToolOverlay()}
            </g>`
          : nothing}
      </svg>
      <div class="canvas-controls" role="toolbar" aria-label="View">
        <button type="button" class="icon-btn" aria-label="Zoom in" title="Zoom in" @click=${() => this.zoomBy(1.25)}>${icon("plus", 20)}</button>
        <button type="button" class="icon-btn" aria-label="Zoom out" title="Zoom out" @click=${() => this.zoomBy(0.8)}>${icon("minus", 20)}</button>
        <button type="button" class="icon-btn" aria-label="Fit to screen" title="Fit to screen" data-testid="fit-view" @click=${() => this.fit()}>${icon("fit", 20)}</button>
        <button type="button" class="icon-btn" aria-label="Reset view" title="Reset view (40 px per metre)" @click=${() => this.resetView()}>${icon("restore", 20)}</button>
      </div>
      ${hint || this.error || (this.tool === "polygon" && this.poly.length)
        ? html`<div class="canvas-hint ${this.error ? "error" : ""}" role="status">
            <span>${this.error ?? hint}</span>
            ${this.tool === "polygon" && this.poly.length && this.mode === "build"
              ? html`<span class="row-actions">
                  <button type="button" class="btn" @click=${() => this.undoPoint()}>${icon("undo", 18)}Remove last corner</button>
                  <button type="button" class="btn btn-primary" ?disabled=${this.poly.length < 3} data-testid="finish-polygon" @click=${() => this.finishPolygon()}>${icon("check", 18)}Finish room</button>
                </span>`
              : nothing}
          </div>`
        : nothing}
      ${!floor ? html`<div class="canvas-empty">${icon("home-map-marker", 40)}<p>Add a floor to start drawing.</p></div>` : nothing}
    </div>`;
  }
}

customElements.define("sl-plan-canvas", SlPlanCanvas);
