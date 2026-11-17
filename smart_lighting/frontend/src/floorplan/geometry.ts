// Plane geometry for the floor plan (metres, y downwards). Pure functions.

export interface Pt {
  x: number;
  y: number;
}

/** Corners closer than this are the same corner (1 mm). */
export const EPS = 0.001;

export const add = (a: Pt, b: Pt): Pt => ({ x: a.x + b.x, y: a.y + b.y });
export const sub = (a: Pt, b: Pt): Pt => ({ x: a.x - b.x, y: a.y - b.y });
export const mul = (a: Pt, k: number): Pt => ({ x: a.x * k, y: a.y * k });
export const dot = (a: Pt, b: Pt): number => a.x * b.x + a.y * b.y;
export const cross = (a: Pt, b: Pt): number => a.x * b.y - a.y * b.x;
export const len = (a: Pt): number => Math.hypot(a.x, a.y);
export const dist = (a: Pt, b: Pt): number => Math.hypot(a.x - b.x, a.y - b.y);
export const lerp = (a: Pt, b: Pt, t: number): Pt => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });

export function round(value: number, digits = 4): number {
  const k = 10 ** digits;
  return Math.round(value * k) / k;
}

export function roundPt(p: Pt): Pt {
  return { x: round(p.x), y: round(p.y) };
}

/** Unit vector, or (0, 0) for a zero vector. */
export function unit(a: Pt): Pt {
  const l = len(a);
  return l < 1e-12 ? { x: 0, y: 0 } : { x: a.x / l, y: a.y / l };
}

/** Signed area (shoelace); positive when the corners run clockwise on screen (y down). */
export function signedArea(points: Pt[]): number {
  let total = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    total += a.x * b.y - b.x * a.y;
  }
  return total / 2;
}

export function area(points: Pt[]): number {
  return Math.abs(signedArea(points));
}

export function perimeter(points: Pt[]): number {
  let total = 0;
  for (let i = 0; i < points.length; i++) total += dist(points[i], points[(i + 1) % points.length]);
  return total;
}

export function centroid(points: Pt[]): Pt {
  const a = signedArea(points);
  if (Math.abs(a) < 1e-9) {
    const n = points.length || 1;
    return { x: points.reduce((s, p) => s + p.x, 0) / n, y: points.reduce((s, p) => s + p.y, 0) / n };
  }
  let cx = 0;
  let cy = 0;
  for (let i = 0; i < points.length; i++) {
    const p = points[i];
    const q = points[(i + 1) % points.length];
    const f = p.x * q.y - q.x * p.y;
    cx += (p.x + q.x) * f;
    cy += (p.y + q.y) * f;
  }
  return { x: cx / (6 * a), y: cy / (6 * a) };
}

/** Point in polygon (ray casting); points on the boundary count as inside. */
export function pointInPolygon(p: Pt, points: Pt[]): boolean {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const a = points[i];
    const b = points[j];
    if (distToSegment(p, a, b) < EPS) return true;
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

/** Parameter t (0..1) of the closest point on segment ab, and that point. */
export function projectToSegment(p: Pt, a: Pt, b: Pt): { t: number; point: Pt } {
  const ab = sub(b, a);
  const l2 = dot(ab, ab);
  const t = l2 < 1e-12 ? 0 : Math.max(0, Math.min(1, dot(sub(p, a), ab) / l2));
  return { t, point: lerp(a, b, t) };
}

export function distToSegment(p: Pt, a: Pt, b: Pt): number {
  return dist(p, projectToSegment(p, a, b).point);
}

/** Distance from p to the infinite line through a and b. */
export function distToLine(p: Pt, a: Pt, b: Pt): number {
  const ab = sub(b, a);
  const l = len(ab);
  return l < 1e-12 ? dist(p, a) : Math.abs(cross(ab, sub(p, a))) / l;
}

/** p lies on segment ab, strictly between its ends. */
export function strictlyOnSegment(p: Pt, a: Pt, b: Pt, eps = EPS): boolean {
  if (dist(p, a) < eps || dist(p, b) < eps) return false;
  return distToSegment(p, a, b) < eps;
}

function orient(a: Pt, b: Pt, c: Pt): number {
  return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

function onSeg(a: Pt, b: Pt, c: Pt): boolean {
  return Math.min(a.x, b.x) - 1e-9 <= c.x && c.x <= Math.max(a.x, b.x) + 1e-9 && Math.min(a.y, b.y) - 1e-9 <= c.y && c.y <= Math.max(a.y, b.y) + 1e-9;
}

/** Segments p1p2 and p3p4 touch or cross (same rule as the backend). */
export function segmentsCross(p1: Pt, p2: Pt, p3: Pt, p4: Pt): boolean {
  const e = 1e-12;
  const d1 = orient(p3, p4, p1);
  const d2 = orient(p3, p4, p2);
  const d3 = orient(p1, p2, p3);
  const d4 = orient(p1, p2, p4);
  if (((d1 > e && d2 < -e) || (d1 < -e && d2 > e)) && ((d3 > e && d4 < -e) || (d3 < -e && d4 > e))) return true;
  return (Math.abs(d1) <= e && onSeg(p3, p4, p1)) || (Math.abs(d2) <= e && onSeg(p3, p4, p2)) || (Math.abs(d3) <= e && onSeg(p1, p2, p3)) || (Math.abs(d4) <= e && onSeg(p1, p2, p4));
}

/** At least 3 distinct corners and no two non-neighbouring walls touch. */
export function isSimplePolygon(points: Pt[]): boolean {
  const n = points.length;
  if (n < 3) return false;
  for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) if (dist(points[i], points[j]) < 1e-9) return false;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      if (segmentsCross(points[i], points[(i + 1) % n], points[j], points[(j + 1) % n])) return false;
    }
  }
  return true;
}

/** Segments cross at a single interior point (touching, sharing an end or running along each other does not count). */
export function segmentsProperlyCross(p1: Pt, p2: Pt, p3: Pt, p4: Pt): boolean {
  const e = 1e-9;
  const d1 = orient(p3, p4, p1);
  const d2 = orient(p3, p4, p2);
  const d3 = orient(p1, p2, p3);
  const d4 = orient(p1, p2, p4);
  return ((d1 > e && d2 < -e) || (d1 < -e && d2 > e)) && ((d3 > e && d4 < -e) || (d3 < -e && d4 > e));
}

/** p is inside the polygon and not on its boundary. */
export function strictlyInside(p: Pt, points: Pt[]): boolean {
  for (let i = 0; i < points.length; i++) if (distToSegment(p, points[i], points[(i + 1) % points.length]) < EPS) return false;
  return pointInPolygon(p, points);
}

/** The interiors of two simple polygons overlap (sharing walls or corners is fine). */
export function polygonsOverlap(a: Pt[], b: Pt[]): boolean {
  for (let i = 0; i < a.length; i++) {
    for (let j = 0; j < b.length; j++) {
      if (segmentsProperlyCross(a[i], a[(i + 1) % a.length], b[j], b[(j + 1) % b.length])) return true;
    }
  }
  const probes = (p: Pt[]): Pt[] => [...p, ...p.map((q, i) => lerp(q, p[(i + 1) % p.length], 0.5)), centroid(p)];
  return probes(a).some((q) => strictlyInside(q, b)) || probes(b).some((q) => strictlyInside(q, a));
}

export const MIN_ROOM_AREA = 0.05; // m² (the backend refuses below 0.01)

export function validPolygon(points: Pt[]): string | null {
  if (points.length < 3) return "A room needs at least 3 corners.";
  if (!isSimplePolygon(points)) return "The walls of a room must not cross each other.";
  if (area(points) < MIN_ROOM_AREA) return "The room is too small.";
  return null;
}

/** Bearing of a direction on the plan: degrees clockwise from the top of the plan (-y). */
export function planBearing(v: Pt): number {
  const deg = (Math.atan2(v.x, -v.y) * 180) / Math.PI;
  return (deg + 360) % 360;
}

const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
export function compassName(bearing: number): string {
  return COMPASS[Math.round((((bearing % 360) + 360) % 360) / 45) % 8];
}

// ---------------------------------------------------------------- snapping
export function snapToGrid(p: Pt, step: number): Pt {
  if (!(step > 0)) return p;
  return { x: round(Math.round(p.x / step) * step), y: round(Math.round(p.y / step) * step) };
}

/** Constrain b so that a→b is horizontal, vertical or diagonal (45°), keeping its length along that axis. */
export function snapAngle(a: Pt, b: Pt): Pt {
  const d = sub(b, a);
  const angle = Math.atan2(d.y, d.x);
  const step = Math.PI / 4;
  const snapped = Math.round(angle / step) * step;
  const l = len(d) * Math.cos(angle - snapped);
  return add(a, { x: Math.cos(snapped) * l, y: Math.sin(snapped) * l });
}

// -------------------------------------------------------------- transforms
/** Screen view of the plan: screen = world * scale + (tx, ty); scale in pixels per metre. */
export interface View {
  scale: number;
  tx: number;
  ty: number;
}

export const MIN_SCALE = 4;
export const MAX_SCALE = 800;

export function toScreen(v: View, p: Pt): Pt {
  return { x: p.x * v.scale + v.tx, y: p.y * v.scale + v.ty };
}

export function toWorld(v: View, p: Pt): Pt {
  return { x: (p.x - v.tx) / v.scale, y: (p.y - v.ty) / v.scale };
}

/** Zoom by `factor` keeping the screen point `at` fixed (the world point under the cursor stays put). */
export function zoomAt(v: View, factor: number, at: Pt): View {
  const scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, v.scale * factor));
  const world = toWorld(v, at);
  return { scale, tx: at.x - world.x * scale, ty: at.y - world.y * scale };
}

export interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export function boundsOf(points: Pt[]): Bounds | null {
  if (!points.length) return null;
  return points.reduce(
    (b, p) => ({ minX: Math.min(b.minX, p.x), minY: Math.min(b.minY, p.y), maxX: Math.max(b.maxX, p.x), maxY: Math.max(b.maxY, p.y) }),
    { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity },
  );
}

/** The view that shows `b` centred in a viewport of width × height pixels, with a margin. */
export function fitView(b: Bounds | null, width: number, height: number, margin = 48): View {
  if (!b || width <= 0 || height <= 0) return { scale: 40, tx: margin, ty: margin };
  const w = Math.max(b.maxX - b.minX, 1);
  const h = Math.max(b.maxY - b.minY, 1);
  const scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, Math.min((width - 2 * margin) / w, (height - 2 * margin) / h)));
  const cx = (b.minX + b.maxX) / 2;
  const cy = (b.minY + b.maxY) / 2;
  return { scale, tx: width / 2 - cx * scale, ty: height / 2 - cy * scale };
}

/** Background image: pixel (u, v) to world, given top-left (x, y), metres per pixel and rotation (deg clockwise). */
export function imageToWorld(bg: { x: number; y: number; scale: number; rotation: number }, u: number, v: number): Pt {
  const r = (bg.rotation * Math.PI) / 180;
  const sx = u * bg.scale;
  const sy = v * bg.scale;
  return { x: bg.x + sx * Math.cos(r) - sy * Math.sin(r), y: bg.y + sx * Math.sin(r) + sy * Math.cos(r) };
}

export function worldToImage(bg: { x: number; y: number; scale: number; rotation: number }, p: Pt): Pt {
  const r = (bg.rotation * Math.PI) / 180;
  const dx = p.x - bg.x;
  const dy = p.y - bg.y;
  return { x: (dx * Math.cos(r) + dy * Math.sin(r)) / bg.scale, y: (-dx * Math.sin(r) + dy * Math.cos(r)) / bg.scale };
}

/** Scale p about the fixed point `origin` by k. */
export function scaleAbout(p: Pt, origin: Pt, k: number): Pt {
  return { x: origin.x + (p.x - origin.x) * k, y: origin.y + (p.y - origin.y) * k };
}

/** Rotate p about `origin` by deg (clockwise on screen). */
export function rotateAbout(p: Pt, origin: Pt, deg: number): Pt {
  const r = (deg * Math.PI) / 180;
  const dx = p.x - origin.x;
  const dy = p.y - origin.y;
  return { x: origin.x + dx * Math.cos(r) - dy * Math.sin(r), y: origin.y + dx * Math.sin(r) + dy * Math.cos(r) };
}
