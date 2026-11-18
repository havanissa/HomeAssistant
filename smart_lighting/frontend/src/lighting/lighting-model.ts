// Phase A4: what the Lighting pages read from the integration's lighting editor model (``smart_occupancy/lighting/editor``)
// and the words they show for its values. The models are the integration's own; only the fields the pages use are
// typed. Nothing here decides anything: every status and explanation comes from the lighting runtime.
import { html, nothing, type TemplateResult } from "lit";

import { icon } from "../icons";
import { chip, type Tone } from "../ui";
import { haTime } from "../presence/components";
import { cmd, lighting } from "./api";
import { describeSetting, swatch, type Capabilities, type Setting } from "./components";

/* eslint-disable @typescript-eslint/no-explicit-any */
export type Obj = Record<string, any>;

/** A resolved setting (``ResolvedSetting.as_dict``): values plus where each one comes from. */
export interface Resolved extends Setting {
  sources?: Record<string, { level: "global" | "room" | "light"; mood?: string; target?: string }>;
  adaptive?: Obj;
}

export interface RoomBrief {
  area_id: string;
  name: string;
  active: boolean;
  control_mode: "active" | "observe" | "paused" | "blocked" | string;
}

export interface Problem {
  type?: string;
  code?: string;
  title?: string;
  severity?: string;
  classification?: string;
  category?: string;
  explanation?: string;
  action?: string;
  name?: string;
  area_id?: string | null;
  entity_id?: string | null;
  [key: string]: unknown;
}

export interface Decision {
  id: string;
  at: string;
  first_at?: string | null;
  count?: number;
  module?: string;
  trigger?: string;
  kind?: string;
  action: string;
  explanation: string;
  mode?: string;
  status: string;
  target?: string | null;
  details?: Obj;
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
  type?: "light" | "relay";
  is_group?: boolean;
  members?: string[];
  opaque_group?: boolean;
  covered_by?: string | null;
  excluded?: string | null;
  explicit?: boolean;
  discovered?: boolean;
  missing?: boolean;
  override?: Obj | null;
  conflicts?: Array<{ automation: string; name?: string; enabled: boolean; acknowledged: boolean; via?: string; detail?: string; light?: string }>;
  own?: Setting | null;
  inherited?: Resolved | null;
  effective?: Resolved | null;
  now?: Resolved | null;
}

export interface RoomModel {
  area_id: string;
  name: string;
  now_mood: string | null;
  enabled: boolean;
  diagnostics: Obj;
  settings: Obj;
  profile: string;
  now_profile: string;
  types: Obj[];
  lights: RoomLight[];
  group_issues: Array<{ group: string; members: string[]; members_known: boolean; dismissed: boolean }>;
  daylight: Obj | null;
  decisions: { latest: Decision | null; entries: Decision[]; size: number };
  vacancy: Obj;
  vacant: Obj | null;
  preliminary: Obj | null;
  zones: Obj | null;
  supervisor: Obj | null;
  brightness: Obj | null;
  priority: Obj | null;
  occupancy: { state: string | null; occupied: boolean | null; confidence: number | null; reason: string | null };
  health: Obj | null;
  problems: Problem[];
}

export interface EditorModel {
  version: string;
  revision: number | string;
  can_edit: boolean;
  mood: Obj;
  moods: Array<{ name: string; in_main?: boolean; has_profile?: boolean }>;
  selected_mood: string | null;
  categories: Obj[];
  adaptive: Obj;
  settings: Obj;
  daylight: Obj;
  labels: Array<{ label_id: string; name: string }>;
  rooms: RoomBrief[];
  problems: Problem[];
  room?: RoomModel;
}

export function loadEditor(areaId?: string | null): Promise<EditorModel> {
  return lighting<EditorModel>(cmd("editor"), areaId ? { area_id: areaId } : {});
}

// ------------------------------------------------------------------------ words
export const OCC: Record<string, [string, Tone]> = {
  vacant: ["Vacant", "neutral"], possible_occupancy: ["Possible occupancy", "pending"],
  confirmed_occupancy: ["Occupied", "ok"], possible_vacancy: ["Possible vacancy", "info"], uncertain: ["Uncertain", "warning"],
};

export const DAYLIGHT: Record<string, [string, Tone]> = { dark: ["Dark", "info"], bright: ["Bright", "ok"], uncertain: ["Uncertain", "warning"] };

export const AUTO_ON: Array<[string, string]> = [
  ["dark", "When occupied and dark (recommended)"], ["always", "When occupied, regardless of daylight"], ["never", "Never automatically"],
];

export const VACANCY: Array<[string, string, string]> = [
  ["turn_off", "Turn lights off", "power"], ["vacant_mood", "Vacant profile", "bed"], ["leave", "Leave unchanged", "lightbulb"],
];

export const DECISION: Record<string, [string, Tone, string]> = {
  executed: ["Executed", "ok", "ok"], dispatched: ["Sent", "info", "arrow-right"], partial: ["Partly failed", "error", "warning"],
  failed: ["Failed", "error", "error"], observed: ["Observed only", "neutral", "eye"], blocked: ["Blocked", "warning", "close-circle"],
  no_change: ["No change needed", "neutral", "check"], no_action: ["No action", "neutral", "minus"],
  suppressed: ["Not done (setting)", "neutral", "cog"], noted: ["Noted", "neutral", "info"],
};

export const DECISION_MODES: Record<string, string> = {
  observing: "Observing", automatic: "Automatic", paused: "Automatic · paused (Manual mood)", active: "Active",
};

export const DETAIL_LABELS: Array<[string, string]> = [
  ["trigger", "Trigger"], ["trigger_entity", "Triggering entity"], ["trigger_state", "Triggering state"],
  ["switch", "Switch"], ["button", "Button"], ["press", "Press type"], ["input", "Received through"],
  ["occupancy", "Occupancy"], ["occupancy_reason", "Occupancy reason"], ["occupancy_inputs", "Occupancy inputs"],
  ["mood", "Mood / profile"], ["mood_detail", "Mood source"],
  ["daylight", "Daylight"], ["lux", "Light level (lx)"], ["on_below_lux", "Darkness threshold (lx)"],
  ["daylight_detail", "Daylight detail"], ["auto_on", "Automatic switch-on"],
  ["manual_overrides", "Manual overrides"], ["manual_override", "Manual override registered"],
  ["override_source", "Changed by"], ["override_minutes", "Respected for (min)"],
  ["vacancy_action", "When vacant"], ["vacant_mood", "Vacant mood"], ["vacancy_at", "Vacant at (if quiet)"],
  ["cancelled_by", "Cancelled by"], ["overrides_cleared", "Manual overrides ended"],
  ["lights_on", "Lights on"], ["targets", "Lights"], ["assigned_action", "Assigned action"],
  ["global_values", "Global adjustment"], ["captured", "Captured buttons"],
  ["service", "Requested action"], ["result", "Result"], ["verification", "Verification"], ["errors", "Error"],
];

export const SUP_TONE: Record<string, Tone> = {
  healthy: "ok", observing: "info", waiting: "neutral", mismatch: "warning", correcting: "info", degraded: "warning",
  device_failure: "error", conflict: "error", unknown: "neutral", off: "neutral",
};
export const SUP_MODE: Record<string, string> = { observing: "Observing", automatic: "Automatic", off: "Off" };
export const SUP_RESULT: Record<string, [string, Tone]> = {
  correct: ["Correct", "ok"], mismatch: ["Mismatch", "warning"], waiting: ["Waiting", "neutral"], correcting: ["Correcting", "info"],
  device_failure: ["Device failure", "error"], override: ["Manual change", "info"], unavailable: ["Unavailable", "warning"],
  not_supervised: ["Not supervised", "neutral"], external: ["Changed elsewhere", "warning"], conflict: ["Conflict suspected", "error"],
  pending: ["Pending", "neutral"], paused: ["Paused", "neutral"], no_expectation: ["No expectation", "neutral"],
};
export const SUP_LIMITS: Array<[string, string, string]> = [
  ["verify_delay", "Verify after", "s"], ["reconcile_interval", "Check every", "s"], ["health_interval", "Sensor health every", "s"],
  ["max_retries", "Corrections per mismatch", ""], ["retry_delay", "First retry after", "s"],
  ["max_per_hour", "Corrections per device per hour", ""], ["brightness_tolerance", "Brightness tolerance", "%"],
  ["kelvin_tolerance", "Colour temperature tolerance", "K"],
];

export const AMB_BASIS: Record<string, [string, Tone]> = {
  measured: ["measured", "ok"], estimated: ["estimated", "info"], bound: ["measured with lamps on", "info"],
  held: ["held", "warning"], fallback: ["fallback", "warning"], uncertain: ["uncertain", "warning"],
};

/** The room's lighting mode, in words (Observing, Automatic, paused, blocked). */
export function roomMode(enabled: boolean, controlMode: string | undefined): { key: string; label: string; tone: Tone; icon: string } {
  if (!enabled || controlMode === "observe") return { key: "observe", label: "Observing", tone: "neutral", icon: "eye" };
  if (controlMode === "paused") return { key: "paused", label: "Automatic · paused", tone: "warning", icon: "pending" };
  if (controlMode === "blocked") return { key: "blocked", label: "Automatic · blocked", tone: "warning", icon: "warning" };
  return { key: "active", label: "Automatic", tone: "ok", icon: "lightbulb-auto" };
}

export function modeChip(enabled: boolean, controlMode: string | undefined): TemplateResult {
  const m = roomMode(enabled, controlMode);
  return chip(m.tone, m.label);
}

export function occChip(state: string | null | undefined): TemplateResult {
  if (!state) return chip("neutral", "No occupancy");
  const [label, tone] = OCC[state] ?? [state, "neutral"];
  return chip(tone, label);
}

export function decisionChip(d: Decision): TemplateResult {
  if (d.status === "dispatched" && d.details?.verification) return html`<span class="chip chip-warning lp-dchip">${icon("arrow-right", 14)}<span class="chip-label">Sent · not confirmed</span></span>`;
  const [label, tone, ic] = DECISION[d.status] ?? [d.status, "neutral", "help"];
  return html`<span class="chip chip-${tone} lp-dchip">${icon(ic, 14)}<span class="chip-label">${label}</span></span>`;
}

/** Short, readable result of a resolved setting: "Off", "40 % · 2700 K", "Not set". */
export function summary(s: Resolved | Setting | null | undefined): string {
  if (!s || !s.action || s.action === "inherit") return "Not set";
  const r = s as Resolved;
  if (s.action === "ignore" && !(r.sources && r.sources.action)) return "Not set";
  if (s.action === "off") return "Off";
  if (s.action === "ignore") return "Left unchanged";
  const parts: string[] = [];
  const gb = s.brightness_mode === "global" || s.brightness_mode === "offset";
  if (s.brightness_pct !== null && s.brightness_pct !== undefined) parts.push(`${s.brightness_pct} %${gb ? " (global)" : ""}`);
  else if (gb) parts.push("global brightness (no value)");
  const gk = s.color_temp_mode === "global" || s.color_temp_mode === "offset";
  if (s.rgb_color) parts.push(`RGB ${s.rgb_color.join(", ")}`);
  else if (s.hs_color) parts.push(`HS ${s.hs_color.join(", ")}`);
  else if (s.color_temp_kelvin) parts.push(`${s.color_temp_kelvin} K${gk ? " (global)" : ""}`);
  else if (gk) parts.push("global colour temperature (no value)");
  if (s.effect) parts.push(s.effect);
  return parts.length ? parts.join(" · ") : "On";
}

/** Where a resolved value comes from: "global (Dagur)", "room", "this light". */
export function sourceText(src: { level: string; mood?: string } | undefined, mood?: string | null): string {
  if (!src) return "default";
  if (src.level === "global") return src.mood && src.mood === "*" ? "global, every mood" : src.mood && mood && src.mood.toLowerCase() !== mood.toLowerCase() ? `global (from ${src.mood})` : "global";
  if (src.level === "room") return src.mood === "*" ? "room, every mood" : "room";
  return "this light";
}

export function sourceTag(src: { level: string; mood?: string } | undefined, mood?: string | null): TemplateResult {
  const own = src?.level === "light" || src?.level === "room";
  return html`<span class="inherit-tag ${own ? "override" : ""}">${sourceText(src, mood)}</span>`;
}

export function settingLine(s: Resolved | Setting | null | undefined): TemplateResult {
  return html`<span class="lp-setting">${swatch(s ?? null)}<span>${summary(s)}</span></span>`;
}

export { describeSetting };

export function capsList(caps: Capabilities | null | undefined, type?: string): TemplateResult {
  if (type === "relay") return html`<span class="cap-list"><span class="cap">On/off (relay)</span><span class="cap lp-cap-no">no dimming</span><span class="cap lp-cap-no">no colour</span></span>`;
  if (!caps || caps.known === false) return html`<span class="cap-list"><span class="cap lp-cap-unknown">Capabilities unknown</span></span>`;
  const items: Array<[boolean, string]> = [
    [true, "On/off"],
    [!!caps.brightness, "Dimming"],
    [!!caps.color_temp, caps.color_temp && caps.min_kelvin && caps.max_kelvin ? `White ${caps.min_kelvin}–${caps.max_kelvin} K` : "White temperature"],
    [!!caps.color, "Colour"],
    [!!caps.transition, "Fade"],
  ];
  return html`<span class="cap-list">${items.map(([ok, label]) => html`<span class="cap ${ok ? "" : "lp-cap-no"}" title=${ok ? "Supported" : "Not supported by this light"}>${ok ? label : `no ${label.toLowerCase()}`}</span>`)}</span>`;
}

/** "07:00" today, "14 Nov 23:55" another day, in Home Assistant's time zone. */
export function fmt(iso: string | null | undefined): string {
  return iso ? haTime(false)(iso) : "—";
}

export function fmtSecs(s: number | null | undefined): string {
  if (s === null || s === undefined) return "—";
  if (s >= 3600 && s % 3600 === 0) return `${s / 3600} h`;
  if (s >= 60 && s % 60 === 0) return `${s / 60} min`;
  return `${s} s`;
}

export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

export function detailValue(k: string, v: unknown): unknown {
  if (v === true) return "Yes";
  if (v === false) return "No";
  if (k === "occupancy" && typeof v === "string") return OCC[v]?.[0] ?? v;
  if (k === "daylight" && typeof v === "string") return DAYLIGHT[v]?.[0] ?? v;
  if (Array.isArray(v)) return v.length ? html`<ul class="lp-dlist">${v.map((x) => html`<li>${typeof x === "object" ? JSON.stringify(x) : String(x)}</li>`)}</ul>` : "—";
  if (v && typeof v === "object") return html`<ul class="lp-dlist">${Object.entries(v as Obj).map(([a, b]) => html`<li>${a}: ${typeof b === "object" ? JSON.stringify(b) : String(b)}</li>`)}</ul>`;
  return String(v);
}

/** Everything the runtime recorded for one decision (the page adds no diagnosis of its own). */
export function decisionDetails(d: Decision): TemplateResult {
  const det = d.details ?? {};
  const known = new Set(DETAIL_LABELS.map(([k]) => k));
  const rows: Array<[string, unknown]> = [
    ["Time", `${haTime(true)(d.at)}${(d.count ?? 1) > 1 && d.first_at ? ` (${d.count}× since ${haTime(true)(d.first_at)})` : ""}`],
    ["Mode at that time", DECISION_MODES[d.mode ?? ""] ?? d.mode ?? "—"],
    ["Result", decisionChip(d)],
  ];
  if (d.target) rows.push(["Target", d.target]);
  for (const [k, label] of DETAIL_LABELS) {
    const v = det[k];
    if (v === undefined || v === null || v === "") continue;
    rows.push([label, detailValue(k, v)]);
  }
  for (const [k, v] of Object.entries(det)) if (!known.has(k) && v !== null && v !== undefined) rows.push([k.replace(/_/g, " "), detailValue(k, v)]);
  return html`<dl class="kv lp-ddl">${rows.map(([k, v]) => html`<dt>${k}</dt><dd>${v}</dd>`)}</dl>`;
}

/** One decision as a compact line: when, what, result, why. */
export function decisionLine(d: Decision, opts: { open?: boolean; onToggle?: () => void } = {}): TemplateResult {
  const body = html`<span class="lp-dtime mono" title=${haTime(true)(d.at)}>${fmt(d.at)}${(d.count ?? 1) > 1 ? html`<span class="small muted"> ×${d.count}</span>` : nothing}</span>
    <span class="lp-dmain"><span class="lp-row-line"><strong>${d.action}</strong>${d.target && !d.action.includes(d.target) ? html`<span class="muted">${d.target}</span>` : nothing}${decisionChip(d)}</span>
      <span class="block small muted">${d.explanation}</span></span>`;
  if (!opts.onToggle) return html`<li class="lp-decision">${body}</li>`;
  return html`<li class="lp-decision ${opts.open ? "open" : ""}">
    <button type="button" class="lp-dbtn" aria-expanded=${opts.open ? "true" : "false"} @click=${opts.onToggle}>${body}${icon(opts.open ? "chevron-down" : "chevron-right", 18)}</button>
    ${opts.open ? html`<div class="lp-ddet">${decisionDetails(d)}</div>` : nothing}</li>`;
}

export function problemLevel(p: Problem): "error" | "warning" | "info" {
  const l = String(p.severity ?? p.classification ?? "warning");
  return l === "error" || l === "critical" ? "error" : l === "info" ? "info" : "warning";
}

export function problemItem(p: Problem): TemplateResult {
  const level = problemLevel(p);
  const title = String(p.title ?? p.type ?? p.code ?? "Problem");
  return html`<li class="lp-problem lp-problem-${level}"><span class="tone-${level}">${icon(level === "info" ? "info" : level, 18)}</span>
    <span><strong>${title}</strong>${p.name ? html` <span class="muted">· ${p.name}</span>` : nothing}
      ${p.explanation ? html`<span class="block small">${p.explanation}</span>` : nothing}
      ${p.action ? html`<span class="block small muted">What to do: ${p.action}</span>` : nothing}</span></li>`;
}

/** A labelled number input whose empty value means "inherit" (null). */
export function numberField(o: {
  label: string; value: number | null | undefined; placeholder?: string | number | null; unit?: string; min?: number; max?: number;
  step?: number; disabled?: boolean; help?: unknown; tag?: unknown; onChange: (v: number | null) => void; testid?: string;
}): TemplateResult {
  return html`<label class="lp-field"><span class="lp-label">${o.label}</span>
    <span class="lp-input-row"><input type="number" inputmode="decimal" .value=${o.value === null || o.value === undefined ? "" : String(o.value)}
      placeholder=${o.placeholder === null || o.placeholder === undefined ? "" : String(o.placeholder)} min=${o.min ?? nothing} max=${o.max ?? nothing} step=${o.step ?? 1}
      ?disabled=${o.disabled} data-testid=${o.testid ?? nothing}
      @change=${(e: Event) => { const raw = (e.target as HTMLInputElement).value.trim(); o.onChange(raw === "" ? null : Number(raw)); }} />
      ${o.unit ? html`<span class="muted">${o.unit}</span>` : nothing}${o.tag ?? nothing}</span>
    ${o.help ? html`<span class="small muted">${o.help}</span>` : nothing}</label>`;
}

export function selectField<T extends string>(o: {
  label: string; value: T | null | undefined; options: Array<[T, string]>; disabled?: boolean; help?: unknown; tag?: unknown;
  onChange: (v: T) => void; testid?: string;
}): TemplateResult {
  return html`<label class="lp-field"><span class="lp-label">${o.label}</span>
    <span class="lp-input-row"><select ?disabled=${o.disabled} data-testid=${o.testid ?? nothing} @change=${(e: Event) => o.onChange((e.target as HTMLSelectElement).value as T)}>
      ${o.options.map(([v, l]) => html`<option value=${v} ?selected=${(o.value ?? "") === v}>${l}</option>`)}</select>${o.tag ?? nothing}</span>
    ${o.help ? html`<span class="small muted">${o.help}</span>` : nothing}</label>`;
}

export function toggleField(o: { label: string; checked: boolean; disabled?: boolean; help?: unknown; onChange: (v: boolean) => void; testid?: string }): TemplateResult {
  return html`<label class="switch-row lp-switch"><input type="checkbox" role="switch" .checked=${o.checked} ?disabled=${o.disabled} data-testid=${o.testid ?? nothing}
      @change=${(e: Event) => o.onChange((e.target as HTMLInputElement).checked)} />
    <span><span>${o.label}</span>${o.help ? html`<span class="block small muted">${o.help}</span>` : nothing}</span></label>`;
}

/** "Room override" / "Global default" with the global value, and a button to return to it. */
export function inheritTag(own: boolean, globalText: string, onReset?: (() => void) | null): TemplateResult {
  return html`<span class="inherit-tag ${own ? "override" : ""}">${own ? "Room value" : `Global default`}</span>
    ${own && onReset ? html`<button type="button" class="lp-link" @click=${onReset}>Use global (${globalText})</button>` : !own ? html`<span class="small muted">${globalText}</span>` : nothing}`;
}

/** A choice of a few options as buttons (aria-pressed). */
export function segments<T extends string>(value: T | null | undefined, options: Array<[T, string, string?]>, onPick: (v: T) => void,
                                           opts: { disabled?: boolean; label: string; testid?: string }): TemplateResult {
  return html`<div class="lp-seg" role="group" aria-label=${opts.label} data-testid=${opts.testid ?? nothing}>${options.map(([v, l, ic]) => html`<button type="button"
      class=${value === v ? "on" : ""} aria-pressed=${value === v ? "true" : "false"} ?disabled=${opts.disabled}
      @click=${() => value !== v && onPick(v)}>${ic ? icon(ic, 18) : nothing}${l}</button>`)}</div>`;
}

/** What a sub-panel of a lighting page needs from its page: run a change (errors shown, model reloaded). */
export interface LightingHost {
  run(action: () => Promise<unknown>, done?: string): Promise<boolean>;
  busy: boolean;
  canEdit: boolean;
  error: unknown;
}

/** A light state as the runtime reports it ({state, brightness_pct, color_temp_kelvin}): "On 80 % · 2700 K". */
export function stateText(d: Obj | null | undefined): string {
  if (!d) return "—";
  if (d.state === "off") return "Off";
  if (d.state && d.state !== "on") return String(d.state);
  const parts = [d.brightness_pct !== null && d.brightness_pct !== undefined ? `${d.brightness_pct} %` : "On"];
  if (d.color_temp_kelvin) parts.push(`${d.color_temp_kelvin} K`);
  if (d.rgb_color) parts.push(`RGB ${d.rgb_color.join(", ")}`);
  return parts.join(" · ");
}

export function card(title: string, ic: string, body: unknown, opts: { extra?: unknown; wide?: boolean; testid?: string; cls?: string } = {}): TemplateResult {
  return html`<section class="card ${opts.wide ? "wide" : ""} ${opts.cls ?? ""}" data-testid=${opts.testid ?? nothing}>
    <header class="card-head">${icon(ic)}<h3>${title}</h3>${opts.extra ?? nothing}</header>${body}</section>`;
}

/** The Advanced toggle (a button, not <details>). */
export function advancedToggle(open: boolean, onToggle: () => void, label = "Advanced"): TemplateResult {
  return html`<button type="button" class="collapse-toggle lp-adv" aria-expanded=${open ? "true" : "false"} @click=${onToggle}>${icon(open ? "chevron-down" : "chevron-right", 18)}${label}</button>`;
}
