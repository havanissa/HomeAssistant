// Curtain windows on the floor plan (Phase A5): the curtain subsystem's model
// as the floor plan reads it, and pure helpers for the overlay and summaries.
//
// The model is served by the App backend (`api/curtains/command`, forwarded to
// the integration or answered by the App's own runtime). The floor plan only
// reads it; the one write it ever sends is the reviewed orientation
// (`window/update {bearing}`), after the person confirmed it in a dialog.
import { postRooms } from "../rooms/model";
import type { LiveState } from "../types";
import type { Floor, Opening } from "./model";

export type CurtainMode = "off" | "observing" | "automatic";

export interface CurtainLayer {
  layer_id: string;
  entity_id: string;
  kind: string;
  kind_word?: string;
  name: string;
  state: string | null;
  available: boolean;
  missing: boolean;
  moving: boolean;
  position: number | null;
  caps?: { position?: boolean; open?: boolean; close?: boolean; stop?: boolean; known?: boolean };
  conflicts?: unknown[];
}

export interface CurtainWindow {
  window_id: string;
  name: string;
  area_id: string | null;
  area_name: string | null;
  direction: string | null;
  bearing: number | null;
  effective_bearing: number | null;
  compass: string | null;
  mode: CurtainMode;
  mode_word?: string;
  layers: CurtainLayer[];
  current: { status: string; preset_id: string | null; text: string } | null;
  selected_preset: string | null;
  presets: Array<{ preset_id: string; name: string; matches?: boolean }>;
  manual: { since?: string; reason?: string; text?: string } | Record<string, unknown> | null;
  last: { status?: string; explanation?: string; text?: string } | Record<string, unknown> | null;
  running: boolean;
  problems: Array<{ severity: string; text: string }>;
  exposure: { status: string; text: string; exposed: boolean } | null;
  auto: {
    status: { action?: string; word?: string; code?: string; text?: string; condition?: string; preset_name?: string } | null;
    hold: { policy_word?: string; reason?: string; created?: string; until?: string | null } | Record<string, unknown> | null;
    decision: { condition?: string; note?: string | null } | Record<string, unknown> | null;
    environment: {
      sun?: { status?: string; text?: string; exposed?: boolean; azimuth?: number | null; elevation?: number | null };
      glare?: { enabled?: boolean; active?: boolean };
      thermal?: string | null;
    } | null;
    next_evaluation: string | null;
  } | null;
}

export interface CurtainModel {
  version?: string;
  can_edit: boolean;
  windows: CurtainWindow[];
  rooms: Array<{ area_id: string | null; name: string; windows: string[] }>;
  words?: { modes?: Record<string, string>; conditions?: Record<string, string> };
}

export const MODE_WORD: Record<CurtainMode, string> = { off: "Off", observing: "Observing", automatic: "Automatic" };

export async function fetchCurtainModel(): Promise<CurtainModel> {
  const r = await postRooms<{ result: CurtainModel }>("api/curtains/command", { type: "smart_occupancy/curtains/model" });
  return r.result;
}

/** The one write of the floor plan: the reviewed orientation of a curtain window (administrators only). */
export async function setWindowBearing(windowId: string, bearing: number): Promise<void> {
  await postRooms("api/curtains/command", { type: "smart_occupancy/curtains/window/update", window_id: windowId, bearing });
}

// ------------------------------------------------------------- pure helpers
export type CoverTone = "ok" | "moving" | "unavailable" | "unknown" | "none";

export interface CoverView {
  tone: CoverTone;
  /** Open position 0..100 (100 = fully open), averaged over the covers that report one; null when unknown. */
  position: number | null;
  text: string;
}

function livePosition(layer: CurtainLayer, states?: Map<string, LiveState>): { position: number | null; moving: boolean; available: boolean } {
  const live = states?.get(layer.entity_id) as (LiveState & { attrs?: Record<string, unknown> }) | undefined;
  if (live) {
    const available = live.availability === "ok" && Boolean(live.state) && live.state !== "unknown";
    const pos = typeof live.attrs?.current_position === "number" ? (live.attrs.current_position as number) : null;
    return { position: available ? (pos ?? (live.state === "closed" ? 0 : live.state === "open" ? 100 : null)) : null, moving: live.state === "opening" || live.state === "closing", available };
  }
  return { position: layer.available ? layer.position : null, moving: layer.moving, available: layer.available && !layer.missing };
}

/** What the plan draws for a window: the covers' open position now (live states first, the model as fallback). */
export function coverView(window: CurtainWindow, states?: Map<string, LiveState>): CoverView {
  if (!window.layers.length) return { tone: "none", position: null, text: "No covers" };
  const views = window.layers.map((l) => livePosition(l, states));
  if (views.some((v) => !v.available)) {
    const bad = views.filter((v) => !v.available).length;
    return { tone: "unavailable", position: null, text: bad === views.length ? "Unavailable" : `${bad} of ${views.length} covers unavailable` };
  }
  const known = views.map((v) => v.position).filter((p): p is number => typeof p === "number");
  const position = known.length ? Math.round(known.reduce((s, p) => s + p, 0) / known.length) : null;
  if (views.some((v) => v.moving)) return { tone: "moving", position, text: position === null ? "Moving" : `Moving · ${position} % open` };
  if (position === null) return { tone: "unknown", position: null, text: "Position unknown" };
  return { tone: "ok", position, text: position === 0 ? "Closed" : position === 100 ? "Open" : `${position} % open` };
}

/** One cover's state and position, for the inspector. */
export function layerText(layer: CurtainLayer, states?: Map<string, LiveState>): { text: string; tone: CoverTone } {
  if (layer.missing) return { text: "Not in Home Assistant", tone: "unavailable" };
  const v = livePosition(layer, states);
  if (!v.available) return { text: "Unavailable", tone: "unavailable" };
  if (v.moving) return { text: v.position === null ? "Moving" : `Moving · ${v.position} %`, tone: "moving" };
  if (v.position === null) return { text: "Position unknown", tone: "unknown" };
  return { text: v.position === 0 ? "Closed" : v.position === 100 ? "Open" : `${v.position} % open`, tone: "ok" };
}

export interface LinkedWindow {
  opening: Opening;
  window: CurtainWindow | null;
  /** The link points to a curtain window that no longer exists. */
  dangling: boolean;
}

/** The window openings of a floor with their curtain window (when linked). `model` null: the data is not loaded. */
export function linkedWindows(floor: Floor, model: CurtainModel | null): LinkedWindow[] {
  const by = new Map((model?.windows ?? []).map((w) => [w.window_id, w]));
  return floor.openings
    .filter((o) => o.type === "window")
    .map((o) => {
      const window = o.curtain_window_id ? (by.get(o.curtain_window_id) ?? null) : null;
      return { opening: o, window, dangling: Boolean(o.curtain_window_id && model && !window) };
    });
}

/** Why a window needs attention (empty when all is well). */
export function windowAttention(window: CurtainWindow, states?: Map<string, LiveState>): string[] {
  const out: string[] = [];
  for (const p of window.problems ?? []) if (p.severity !== "info") out.push(p.text);
  const cover = coverView(window, states);
  if (cover.tone === "unavailable" && !out.some((t) => /unavailable|does not exist| is /.test(t))) out.push(cover.text);
  if (window.auto?.hold) out.push("Manual hold");
  if (window.exposure?.status === "no_direction") out.push("No orientation set");
  return [...new Set(out)];
}

export interface CurtainSummary {
  linked: number;
  automatic: number;
  observing: number;
  off: number;
  holds: number;
  moving: number;
  unavailable: number;
  dangling: number;
  attention: Array<{ opening: Opening; window: CurtainWindow | null; reasons: string[] }>;
}

export function curtainSummary(links: LinkedWindow[], states?: Map<string, LiveState>): CurtainSummary {
  const s: CurtainSummary = { linked: 0, automatic: 0, observing: 0, off: 0, holds: 0, moving: 0, unavailable: 0, dangling: 0, attention: [] };
  for (const l of links) {
    if (l.dangling) {
      s.dangling += 1;
      s.attention.push({ opening: l.opening, window: null, reasons: ["Linked curtain window no longer exists"] });
      continue;
    }
    if (!l.window) continue;
    s.linked += 1;
    s[l.window.mode === "automatic" ? "automatic" : l.window.mode === "observing" ? "observing" : "off"] += 1;
    if (l.window.auto?.hold) s.holds += 1;
    const cover = coverView(l.window, states);
    if (cover.tone === "moving") s.moving += 1;
    if (cover.tone === "unavailable") s.unavailable += 1;
    const reasons = windowAttention(l.window, states);
    if (reasons.length) s.attention.push({ opening: l.opening, window: l.window, reasons });
  }
  return s;
}

/** Curtain windows grouped by room, the rooms in `first` (area ids) before the others. */
export function groupedWindows(model: CurtainModel, first: Array<string | null>): Array<{ area_id: string | null; name: string; windows: CurtainWindow[] }> {
  const by = new Map(model.windows.map((w) => [w.window_id, w]));
  const rooms = model.rooms.map((r) => ({ area_id: r.area_id, name: r.name, windows: r.windows.map((id) => by.get(id)).filter((w): w is CurtainWindow => Boolean(w)) }));
  const wanted = new Set(first.filter(Boolean));
  return rooms.filter((r) => r.windows.length).sort((a, b) => Number(wanted.has(b.area_id)) - Number(wanted.has(a.area_id)) || a.name.localeCompare(b.name));
}

/** Every cover entity of the linked windows (for live states). */
export function curtainEntityIds(links: LinkedWindow[]): string[] {
  return [...new Set(links.flatMap((l) => l.window?.layers.map((x) => x.entity_id) ?? []))];
}
