// Phase A4: the Moods page's view of the lighting editor model (``smart_occupancy/lighting/editor``) and small
// helpers shared by its panels. Field names follow the integration's answers; nothing here is invented.
import { html, nothing, type TemplateResult } from "lit";

import { cmd, lighting } from "./api";
import { describeSetting, swatch, type Capabilities, type Setting } from "./components";

export type ProfileName = "occupied" | "vacant";

/** A resolved setting: what a target gets, with where each field comes from. */
export interface Resolved extends Setting {
  sources?: Record<string, { level: "global" | "room" | "light"; mood: string; target: string }>;
  adaptive?: Record<string, unknown>;
}

export interface MoodStatus {
  manual_mode: boolean;
  mood: string | null;
  source: string;
  main_mood: string | null;
  expected_main_mood?: string | null;
  expected?: string | null;
  follows: string | null;
  unrepresentable: string | null;
  detail: string;
}

export interface MoodInfo {
  name: string;
  in_main: boolean;
  in_auto: boolean;
  in_manual: boolean;
  parent: string | null;
  has_profile: boolean;
  handling: { mode: "follow_main" | "no_mood" | "use_mood"; mood: string | null } | null;
}

export interface Category {
  key: string;
  name: string;
  kind: "source" | "function" | "rgb";
  label_id: string | null;
  label_name: string | null;
  label_missing: boolean;
  lights: { entity_id: string; name: string }[];
  rgb_lights: number;
}

export interface TypeEntry {
  target: string;
  key: string;
  rgb: boolean;
  variant: boolean;
  name: string;
  kind: string;
  lights: { entity_id: string; name: string }[];
  caps: Capabilities;
  own: Setting | null;
  inherited: Resolved | null;
  effective: Resolved | null;
  configured: boolean;
  follows_global?: boolean;
}

export interface RoomLight {
  entity_id: string;
  name: string;
  category?: string | null;
  category_name?: string | null;
  labels?: string[];
  rgb?: boolean;
  state?: string | null;
  capabilities?: Capabilities;
  type?: string;
  is_group?: boolean;
  excluded?: string | null;
  missing?: boolean;
  override?: Record<string, unknown> | null;
  own?: Setting | null;
  inherited?: Resolved | null;
  effective?: Resolved | null;
  now?: Resolved | null;
}

export interface VacantRoom {
  action: string;
  used: boolean;
  in_charge: boolean;
  settings: { action?: string; mood?: string | null; others?: string | null; on_below?: number | null };
  global: { others: string; on_below: number | null };
  effective: { others: string; on_below: number | null };
  sources: { others: "room" | "global"; on_below: "room" | "global" };
  others_words: Record<string, string>;
  normal_on_below: number | null;
  mood: string | null;
  room_moods: string[];
  occupied_room_moods: string[];
}

export interface RoomModel {
  area_id: string;
  name: string;
  now_mood: string | null;
  enabled: boolean;
  profile: ProfileName;
  now_profile: ProfileName;
  vacant: VacantRoom | null;
  types: TypeEntry[];
  lights: RoomLight[];
  settings: Record<string, unknown>;
}

export interface SourceCandidate {
  entity_id: string;
  name: string;
  likely: boolean;
  value: number;
  unit: string | null;
}

export interface AdaptiveValue {
  entity_id: string | null;
  value: number | null;
  status: string;
  detail: string;
  updated_at: string | null;
  unit: string | null;
}

export interface AdaptiveModel {
  config: {
    brightness_entity: string | null;
    brightness_attribute: string | null;
    brightness_scale: string;
    color_temp_entity: string | null;
    color_temp_attribute: string | null;
    color_temp_scale: string;
    min_interval: number;
    brightness_step: number;
    kelvin_step: number;
    transition: number;
  };
  values: { status: string; brightness: AdaptiveValue; color_temp: AdaptiveValue };
  status: string;
  controllers: { entity_id: string; name?: string; kind?: string; active?: boolean }[];
  candidates?: { brightness: SourceCandidate[]; color_temp: SourceCandidate[] };
}

export interface EditorModel {
  version: string;
  revision: number;
  can_edit: boolean;
  managed_by_app?: boolean;
  mood: MoodStatus;
  moods: MoodInfo[];
  selected_mood: string | null;
  profile: ProfileName;
  vacant_defaults: { others: "off" | "leave"; on_below: number | null };
  categories: Category[];
  types: TypeEntry[];
  global: Record<string, TypeEntry>;
  label_suggestions: { label_id: string; name: string; kind: Category["kind"] }[];
  all_moods: string;
  manual_mode: boolean;
  adaptive: AdaptiveModel;
  settings: {
    manual_option: string;
    fallback_mood: string | null;
    exclude_label: string | null;
    manual_override_minutes: number;
    unattributed: string;
    unrepresentable: Record<string, { mode: string; mood: string | null }>;
  };
  labels: { label_id: string; name: string }[];
  rooms: { area_id: string; name: string; active: boolean; control_mode: string }[];
  problems: { code: string; severity: string; title?: string; message?: string; detail?: string; area_id?: string | null }[];
  room?: RoomModel;
  /** The configured mood source, when the integration reports it (``MoodConfig.as_dict`` + ``source``). */
  mood_source?: MoodSourceConfig;
}

export interface MoodSourceConfig {
  source?: "helpers" | "entity" | "builtin" | "none";
  auto_mood_entity: string | null;
  manual_mood_entity: string | null;
  main_mood_entity: string | null;
  auto_option: string;
  builtin?: string[];
}

export interface PreviewItem {
  entity_id: string;
  name: string;
  category_name: string | null;
  current: Record<string, unknown> | null;
  requested: Record<string, unknown> | null;
  decision: string;
  reason: string;
  unsupported: string[];
  persistent: boolean;
  override: Record<string, unknown> | null;
  rule?: { kind: string; name: string } | null;
}

export interface PreviewPlan {
  area_id: string;
  room: string;
  mood: string | null;
  profile: string;
  occupancy_state: string;
  will_execute: boolean;
  note: string | null;
  manual_mode: boolean;
  daylight: { text?: string } | null;
  vacancy: string | null;
  counts: { change: number; unchanged: number; blocked: number; skipped: number };
  items: PreviewItem[];
}

export const BUILTIN_MOOD_ENTITY = "select.smart_lighting_mood";

export function editor(params: { mood?: string | null; area_id?: string | null; profile?: ProfileName }): Promise<EditorModel> {
  return lighting<EditorModel>(cmd("editor"), params);
}

/** Retries a read while the integration reloads (mood source and light type changes reload it). */
export async function retrying<T>(read: () => Promise<T>, attempts = 8, delayMs = 1500): Promise<T> {
  let last: unknown;
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await read();
    } catch (err) {
      last = err;
      await new Promise((r) => setTimeout(r, delayMs));
    }
  }
  throw last;
}

/** A setting with empty fields left out (the integration treats missing and null the same). */
export function compact(s: Setting | null): Setting | null {
  if (!s) return null;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(s)) if (v !== null && v !== undefined && v !== "") out[k] = v;
  return out as Setting;
}

export function sameSetting(a: Setting | null | undefined, b: Setting | null | undefined): boolean {
  const norm = (s: Setting | null | undefined) => JSON.stringify(Object.entries(compact(s ?? null) ?? {}).sort());
  return (!a && !b) || (!!a && !!b && norm(a) === norm(b));
}

export function moodLabel(mood: string | null | undefined, all = "*"): string {
  if (!mood) return "—";
  return mood === all ? "Every mood" : mood;
}

/** Nothing configured anywhere: the resolved setting leaves the light alone and has no source. */
export function isUnset(r: Resolved | null | undefined): boolean {
  return !r || (r.action === "ignore" && !Object.keys(r.sources ?? {}).length);
}

const LEVEL: Record<string, string> = { global: "Global", room: "Room", light: "Light" };

/** Where a resolved setting's values come from, e.g. "Global · Dagur", "Room · every mood". */
export function sourceLabels(r: Resolved | null | undefined, selected: string | null, all = "*"): string[] {
  const out: string[] = [];
  for (const src of Object.values(r?.sources ?? {})) {
    const mood = src.mood === all ? "every mood" : src.mood;
    const text = `${LEVEL[src.level] ?? src.level} · ${mood}${selected && src.mood !== all && src.mood.toLowerCase() !== selected.toLowerCase() ? " (inherited mood)" : ""}`;
    if (!out.includes(text)) out.push(text);
  }
  return out;
}

/** One cell: swatch + words, with an optional "from" line. */
export function settingCell(s: Setting | Resolved | null | undefined, opts: { empty?: string; from?: string[]; strong?: boolean } = {}): TemplateResult {
  if (!s) return html`<span class="muted">${opts.empty ?? "—"}</span>`;
  if (isUnset(s as Resolved) && opts.empty) return html`<span class="muted">${opts.empty}</span>`;
  const text = describeSetting(s);
  return html`<span class="mo-setting ${opts.strong ? "mo-strong" : ""}">${swatch(s)}<span>${text}</span></span>
    ${opts.from?.length ? html`<span class="block small muted">from ${opts.from.join(", ")}</span>` : nothing}`;
}

export function kindLabel(kind: string): string {
  return { source: "Light type", function: "Function", rgb: "RGB tag" }[kind] ?? kind;
}

/** The mood chain as the integration resolves it: the mood, its parents, then the fallback mood. */
export function moodChain(moods: MoodInfo[], mood: string, fallback: string | null): string[] {
  const chain: string[] = [];
  const seen = new Set<string>();
  let current: string | null = mood;
  while (current && !seen.has(current.toLowerCase())) {
    chain.push(current);
    seen.add(current.toLowerCase());
    const c: string = current;
    current = moods.find((m) => m.name.toLowerCase() === c.toLowerCase())?.parent ?? null;
  }
  if (fallback && !seen.has(fallback.toLowerCase())) chain.push(fallback);
  return chain;
}

export function stateText(s: Record<string, unknown> | null | undefined): string {
  if (!s) return "—";
  const parts = [String(s.state ?? "—")];
  if (s.brightness_pct !== undefined && s.brightness_pct !== null) parts.push(`${s.brightness_pct} %`);
  if (s.color_temp_kelvin) parts.push(`${s.color_temp_kelvin} K`);
  if (Array.isArray(s.rgb_color)) parts.push(`RGB ${(s.rgb_color as number[]).join(", ")}`);
  if (s.effect) parts.push(`effect ${s.effect}`);
  return parts.join(" · ");
}
