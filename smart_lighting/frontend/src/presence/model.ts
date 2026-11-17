// Presence (Phase A3.1): shapes of the backend's presence API and pure helpers
// (countdowns, timing inheritance, filters, the visit chain, the timeline).
//
// Everything shown comes from the integration's occupancy engine (its live
// view and its presence log) or from Home Assistant's Recorder. Nothing here
// estimates a value: a missing time or confidence is shown as missing.
import type { Availability, Change, Impact, Problem, Schema, SensorConfig } from "../rooms/model";

export type OccState = "vacant" | "possible_occupancy" | "confirmed_occupancy" | "possible_vacancy" | "uncertain";
export type Level = "error" | "warning" | "info";
export type Basis = "measured" | "heuristic" | "integration";
export type Filter = "all" | "occupied" | "possible" | "vacant" | "uncertain" | "unavailable" | "problems";

export interface TimingSchemaItem {
  key: string;
  label: string;
  help: string;
  unit: "s";
  min: number;
  max: number;
  default: number;
  basic: boolean;
  zero: string | null;
  min_nonzero: number | null;
  applies_to: string | null;
}

export interface TimingRow {
  key: string;
  label: string;
  help: string;
  min: number;
  max: number;
  default: number;
  global: number;
  value: number;
  overridden: boolean;
  basic: boolean;
}

export interface Diagnostic {
  code: string;
  level: Level;
  basis: Basis;
  title: string;
  detail: string;
  action: string;
  target: "sensors" | "timing" | "room" | "rooms_editor" | "zones";
  entity_id: string | null;
  setting: string | null;
  source?: string;
  area_id?: string | null;
  room?: string;
}

export interface Signal {
  entity_id: string;
  name: string;
  domain: string;
  device_class: string | null;
  kind: string;
  kind_name: string;
  role: string;
  role_name: string;
  inverted: boolean;
  enabled: boolean;
  state: string | null;
  label: string;
  value: "active" | "inactive" | "unknown";
  status: string | null;
  health: string;
  available: boolean;
  condition: "active" | "clear" | "disabled" | "ignored" | "unavailable" | "invalid" | "stuck";
  last_changed: string | null;
  since?: string | null;
  active_start?: string | null;
  active_end?: string | null;
  last_transition?: string | null;
  lost_while_active?: boolean;
  preliminary?: boolean;
  effect?: "trigger" | "presence" | "door" | "none";
  stuck_limit?: number | null;
  deadlines?: { sustained_at?: string; stuck_at?: string; grace_until?: string; recent_until?: string };
  // Added by the App from Home Assistant's registries.
  availability?: Availability;
  area_id_ha?: string | null;
  area_ha?: string | null;
  device?: string | null;
  device_model?: string | null;
  device_id?: string | null;
  ha_category?: string;
  ha_category_label?: string;
  ha_category_confidence?: string;
  ha_category_reason?: string;
  suggested_kind?: string | null;
  other_rooms?: string[];
  last_updated?: string | null;
}

export interface NextStep {
  code: string;
  text: string;
  at: string | null;
}

export interface Occupancy {
  state: OccState;
  state_label: string;
  occupied: boolean | null;
  state_since: string | null;
  reason: string;
  reason_code: string;
  confidence: { score: number; evidence_strength: number; sensor_coverage: number; basis: string; formula: string } | null;
  usable: boolean;
  last_activity: string | null;
  last_activity_source: string | null;
  last_activity_name: string | null;
  quiet_since: string | null;
  vacancy_at: string | null;
  required_quiet: number | null;
  vacancy_evidence: string[];
  starting_up: boolean;
  preliminary_signals: string[];
  zone_holds: string[];
}

export interface Zone {
  id: string;
  name: string;
  enabled: boolean;
  detection: "sensors" | "radar";
  holds_room: boolean;
  sensors: string[];
  radar: { presence: string | null; distance: string | null; min_m: number; max_m: number; hysteresis: number; entry_delay: number; exit_delay: number } | null;
  band: string | null;
  active: boolean | null;
  state: string | null;
  since: string | null;
  reason: string | null;
  available: boolean | null;
  live_radar: {
    configured: boolean;
    presence: boolean | null;
    distance_m: number | null;
    distance_text: string | null;
    distance_valid: boolean;
    state: string;
    phase: string | null;
    reason: string | null;
    presence_name?: string;
    distance_name?: string;
  } | null;
}

export interface PresenceRoom {
  area_id: string;
  name: string;
  icon: string | null;
  floor_id: string | null;
  area_exists: boolean;
  configured: boolean;
  enabled: boolean;
  revision: string;
  sensors: SensorConfig[];
  timing: TimingRow[];
  timing_overrides: Record<string, number>;
  adjacent_rooms: string[];
  reference_occupancy_entity: string | null;
  entities: Record<string, string | null>;
  problems: Problem[];
  diagnostics: Diagnostic[];
  automation: { lighting_automatic: boolean; lighting_paused: boolean; vacancy_action: string | null; curtains: Array<{ window_id: string; name: string; mode: string }>; curtains_automatic: string[] };
  zones: Zone[];
  occupancy: Occupancy | null;
  signals: Signal[];
  explain: { state_label: string; next: NextStep[]; next_evaluation: string | null; vacancy_path: "fast" | "normal" | null; visit_confirmed: boolean } | null;
  comparison?: Record<string, unknown> | null;
  tags: Filter[];
  counts: { sensors: number; active: number; unavailable: number; excluded: number; errors: number; warnings: number; info: number; overrides: number };
  confirm_at: string | null;
}

export interface LogEntry {
  seq: number;
  time: string;
  kind: "transition" | "config" | "start";
  area_id: string | null;
  state?: OccState;
  occupied?: boolean | null;
  previous_state?: OccState | null;
  previous_occupied?: boolean | null;
  reason?: string;
  reason_code?: string;
  confidence?: number;
  evidence?: Array<{ entity_id: string; name: string; status: string }>;
  stuck?: Array<{ entity_id: string; name: string }>;
  unknown?: Array<{ entity_id: string; name: string }>;
  vacancy_at?: string | null;
  required_quiet?: number | null;
  vacancy_evidence?: string[];
  starting_up?: boolean;
  changes?: Array<{ type: string; key?: string; old?: unknown; new?: unknown; entity_id?: string }>;
  applied?: string;
  source?: string;
  detail?: string;
}

export interface PresenceCommon {
  api_version: number;
  integration_version: string;
  now: string;
  loaded: boolean;
  writable: boolean;
  state: string;
  operating_mode: string;
  global: { timing: Record<string, number>; revision: string };
  timing_schema: TimingSchemaItem[];
  kinds: Array<{ value: string; label: string }>;
  roles: Array<{ value: string; label: string }>;
  states: Array<{ value: OccState; label: string }>;
  transitions: Array<{ from: string; to: string; when: string }>;
  state_values: { active: string[]; inactive: string[] } | null;
  schema: Schema | null;
  status: { api: string; error: string | null; recorder: boolean; history: boolean };
  log: LogEntry[];
  log_seq: number;
}

export interface PresenceOverview extends PresenceCommon {
  rooms: PresenceRoom[];
  unconfigured: Array<{ area_id: string; name: string; floor_id: string | null }>;
  counts: Record<Exclude<Filter, "all">, number>;
}

export interface Candidate {
  entity_id: string;
  name: string;
  domain: string;
  category: string;
  category_label: string;
  category_confidence: string;
  category_reason: string;
  suggested_kind: string;
  device_class: string | null;
  state: string | null;
  availability: Availability;
  last_changed: string | null;
  device_id: string | null;
  area_id: string | null;
  area: string | null;
  sensor_rooms: string[];
}

export interface PresenceWorkspace extends PresenceCommon {
  room: PresenceRoom;
  candidates: Candidate[];
  areas: Array<{ area_id: string; name: string }>;
  rooms: Array<{ area_id: string; name: string; enabled: boolean; state: OccState | null }>;
}

export interface Prediction {
  area_id: string;
  name: string;
  state: OccState;
  occupied: boolean | null;
  new_state: OccState;
  new_occupied: boolean | null;
  vacancy_at: string | null;
  new_vacancy_at: string | null;
  reason: string;
}

export interface PresenceReview {
  scope: "global" | "room";
  area_id: string | null;
  revision: string;
  stale: boolean;
  errors: Array<{ code: string; message: string; setting?: string; entity_id?: string }>;
  changes: Array<Change & { scope?: string; key?: string; old_effective?: number; new_effective?: number }>;
  affected_rooms: Array<{ area_id: string; name: string }>;
  impacts: Array<Impact & { predictions?: Prediction[]; rooms?: string[] }>;
  requires_confirmation: string[];
  problems_new: Problem[];
  problems_resolved: Problem[];
  apply: "none" | "live" | "reload";
  writable: boolean;
  conflicts?: Array<{ code: string; entity_id: string; rooms: string[] }>;
}

export interface PresenceChange {
  sensors?: SensorConfig[];
  timing?: Record<string, number | null>;
  reset_timing?: boolean;
}

export interface TimelineData {
  area_id: string;
  window: number;
  windows: number[];
  start: string;
  end: string;
  now: string;
  entities: Record<string, string | null>;
  sensors: Array<SensorConfig & { name: string }>;
  state_values: { active: string[]; inactive: string[] } | null;
  history: { available: boolean; error: string | null; series: Record<string, Array<{ t: string; s: string }>>; truncated: string[] };
  log: LogEntry[];
  log_seq: number;
  time_zone: string | null;
}

// ------------------------------------------------------------------ states
export const STATE_INFO: Record<OccState, { label: string; tone: "ok" | "info" | "warning" | "neutral" | "pending" | "error"; icon: string; short: string }> = {
  confirmed_occupancy: { label: "Occupied", tone: "ok", icon: "account", short: "Occupied" },
  possible_occupancy: { label: "Possible occupancy", tone: "info", icon: "motion-sensor", short: "Possible" },
  possible_vacancy: { label: "Vacancy pending", tone: "warning", icon: "history", short: "Pending" },
  vacant: { label: "Vacant", tone: "neutral", icon: "home", short: "Vacant" },
  uncertain: { label: "Uncertain", tone: "pending", icon: "help", short: "Uncertain" },
};

export const ORDER: OccState[] = ["vacant", "possible_occupancy", "confirmed_occupancy", "possible_vacancy", "uncertain"];

export const CONDITION_INFO: Record<string, { label: string; tone: "ok" | "info" | "warning" | "neutral" | "error" | "pending" }> = {
  active: { label: "Active", tone: "ok" },
  clear: { label: "Clear", tone: "neutral" },
  disabled: { label: "Excluded", tone: "pending" },
  ignored: { label: "Ignored (role)", tone: "pending" },
  unavailable: { label: "Unavailable", tone: "warning" },
  invalid: { label: "State not understood", tone: "warning" },
  stuck: { label: "Stuck (ignored)", tone: "error" },
};

export const ROLE_HELP: Record<string, { title: string; text: string }> = {
  trigger: {
    title: "Trigger occupancy",
    text: "Becoming active starts (possible) occupancy at once. While active it holds the room; active longer than “PIR stuck after” it is ignored as stuck. Its clearing is never vacancy evidence. Usual for motion (PIR) sensors.",
  },
  maintain: {
    title: "Maintain occupancy",
    text: "Holds the room occupied while active; short clears are bridged by the dropout bridge. It also starts possible occupancy when it becomes active. Usual for radar, PC activity and bed sensors.",
  },
  confirm_vacancy: {
    title: "Confirm vacancy",
    text: "Like Maintain while active. When it has cleared (past the dropout bridge), that is positive evidence that the room is empty, so vacancy needs only the fast vacancy timeout.",
  },
  ignore: { title: "Ignore", text: "Kept in the configuration but not used for occupancy." },
};
export const DOOR_HELP =
  "Door contacts count only their movements, whatever the role: a movement is activity, and a movement after which nothing new happens in the room is exit evidence (fast vacancy). A door alone never makes a room occupied.";

// --------------------------------------------------------------- clock
/** Milliseconds to add to Date.now() to get the integration's clock (from the answer's ``now``). */
export function clockOffset(serverNow: string | null | undefined, receivedAt: number = Date.now()): number {
  const t = serverNow ? Date.parse(serverNow) : NaN;
  return Number.isFinite(t) ? t - receivedAt : 0;
}

/** Seconds from now (server clock) until ``at``; null when ``at`` is unknown. Negative when past. */
export function secondsUntil(at: string | null | undefined, offset: number, now: number = Date.now()): number | null {
  if (!at) return null;
  const t = Date.parse(at);
  if (!Number.isFinite(t)) return null;
  return (t - (now + offset)) / 1000;
}

export function secondsSince(at: string | null | undefined, offset: number, now: number = Date.now()): number | null {
  const s = secondsUntil(at, offset, now);
  return s === null ? null : -s;
}

/** "0:42", "4:05", "1:02:03" for countdowns. */
export function clock(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.ceil(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

/** Readable duration of a setting: "45 s", "5 min", "1 h 30 min", "2 d". */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  if (seconds < 60 || seconds % 60) return seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes} min`;
  if (minutes % 60) return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
  const hours = minutes / 60;
  if (hours < 48 || hours % 24) return `${hours} h`;
  return `${hours / 24} d`;
}

/** Relative age: "just now", "45 s ago", "12 min ago", "3 h ago". */
export function ago(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)} s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return `${Math.floor(seconds / 86400)} d ago`;
}

export type Unit = "s" | "min" | "h";
export const UNIT_SECONDS: Record<Unit, number> = { s: 1, min: 60, h: 3600 };

/** The largest unit that shows ``seconds`` exactly (an input shows 600 s as 10 min). */
export function bestUnit(seconds: number): Unit {
  if (seconds && seconds % 3600 === 0) return "h";
  if (seconds && seconds % 60 === 0) return "min";
  return "s";
}

export function toSeconds(value: number, unit: Unit): number {
  return Math.round(value * UNIT_SECONDS[unit]);
}

// --------------------------------------------------------------- timing
/** Client-side check (the integration validates again and is authoritative). */
export function timingError(item: TimingSchemaItem, value: number | null): string | null {
  if (value === null) return null;
  if (!Number.isInteger(value)) return "Whole seconds only";
  if (value < item.min || value > item.max) return `From ${duration(item.min)} to ${duration(item.max)}`;
  if (item.min_nonzero && value !== 0 && value < item.min_nonzero) return `0 (off) or at least ${duration(item.min_nonzero)}`;
  return null;
}

export interface TimingView {
  key: string;
  item: TimingSchemaItem;
  /** The value in force after saving the draft. */
  effective: number;
  /** Room scope: the room overrides it (after the draft). Global scope: differs from the engine default. */
  overridden: boolean;
  global: number;
  stored: number | null;
  draft: number | null | undefined;
  changed: boolean;
  error: string | null;
}

/**
 * Global defaults -> room overrides. ``draft`` holds edits: a number sets a value; null means "use the global
 * default" (room) or "the engine's default" (global); a missing key keeps the stored value.
 */
export function timingViews(schema: TimingSchemaItem[], scope: "global" | "room", global: Record<string, number>,
                            overrides: Record<string, number>, draft: Record<string, number | null>, reset = false): TimingView[] {
  return schema.map((item) => {
    const g = global[item.key] ?? item.default;
    const stored = scope === "room" ? (reset ? null : (overrides[item.key] ?? null)) : g;
    const hasDraft = Object.prototype.hasOwnProperty.call(draft, item.key);
    const d = hasDraft ? draft[item.key] : undefined;
    let effective: number;
    let overridden: boolean;
    if (scope === "room") {
      const value = hasDraft ? d : stored;
      effective = value === null || value === undefined ? g : value;
      overridden = value !== null && value !== undefined;
    } else {
      effective = hasDraft ? (d === null || d === undefined ? item.default : d) : g;
      overridden = effective !== item.default;
    }
    // What is stored now, after a pending "use global for all" (so re-typing an old value is still a change).
    const before = scope === "room" ? (reset ? null : (overrides[item.key] ?? null)) : g;
    const after = scope === "room" ? (hasDraft ? (d ?? null) : stored) : effective;
    return { key: item.key, item, effective, overridden, global: g, stored, draft: d, changed: before !== after, error: hasDraft ? timingError(item, d ?? null) : null };
  });
}

/** Only the draft entries that change something (what is sent for review). */
export function timingChange(views: TimingView[]): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const v of views) if (v.changed && v.draft !== undefined) out[v.key] = v.draft;
  return out;
}

// --------------------------------------------------------------- filters
export function matches(room: PresenceRoom, filter: Filter, query: string): boolean {
  if (filter !== "all" && !room.tags.includes(filter)) return false;
  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return true;
  const text = [room.name, room.area_id, ...room.signals.map((s) => `${s.name} ${s.entity_id}`)].join(" ").toLowerCase();
  return words.every((w) => text.includes(w));
}

/** What a room card shows as its countdown, from the engine's own deadlines only. */
export function countdown(room: PresenceRoom, offset: number, now: number = Date.now()): { label: string; seconds: number; total: number | null } | null {
  const occ = room.occupancy;
  if (!occ) return null;
  if (occ.state === "possible_vacancy" && occ.vacancy_at) {
    const left = secondsUntil(occ.vacancy_at, offset, now);
    if (left === null) return null;
    return { label: "Vacant in", seconds: left, total: occ.required_quiet };
  }
  if (occ.state === "possible_occupancy" && room.confirm_at) {
    const left = secondsUntil(room.confirm_at, offset, now);
    if (left === null) return null;
    return { label: "Confirmed in", seconds: left, total: room.timing.find((t) => t.key === "correlation_window")?.value ?? null };
  }
  return null;
}

// ------------------------------------------------------------ visit chain
export interface ChainStep {
  time: string;
  text: string;
  state: OccState | null;
}

const STATE_VERB: Record<OccState, string> = {
  possible_occupancy: "Waiting for confirmation",
  confirmed_occupancy: "Occupied",
  possible_vacancy: "Vacancy confirmation started",
  vacant: "Vacant",
  uncertain: "Uncertain (sensors unavailable)",
};

/**
 * The current visit as a chain of the engine's own decisions (from the presence log): the transitions since the
 * room last became vacant, oldest first. "Motion detected by PIR → Waiting for confirmation → Radar confirmed
 * presence → Occupied". Empty when the log has nothing for this room.
 */
export function visitChain(log: LogEntry[], areaId: string, limit = 8): ChainStep[] {
  const transitions = log.filter((e) => e.kind === "transition" && e.area_id === areaId && e.state);
  let start = 0;
  for (let i = transitions.length - 1; i >= 0; i--) {
    if (transitions[i].previous_state === "vacant" || transitions[i].previous_state === "uncertain") {
      start = i;
      break;
    }
  }
  const visit = transitions.slice(start).slice(-limit);
  const steps: ChainStep[] = [];
  let seen = new Set<string>();
  for (const e of visit) {
    const state = e.state as OccState;
    const evidence = (e.evidence ?? []).filter((x) => x.status === "active" || x.status === "recent");
    const fresh = evidence.filter((x) => !seen.has(x.entity_id));
    if (state === "possible_occupancy" && fresh.length) {
      steps.push({ time: e.time, text: `${fresh.map((x) => x.name).join(" and ")} detected`, state: null });
    } else if (state === "confirmed_occupancy" && e.previous_state === "possible_occupancy") {
      const by = fresh.length ? fresh : evidence;
      if (by.length) steps.push({ time: e.time, text: `${by.map((x) => x.name).join(" and ")} confirmed presence`, state: null });
    } else if (state === "confirmed_occupancy" && e.previous_state === "possible_vacancy") {
      if (fresh.length || evidence.length) steps.push({ time: e.time, text: `${(fresh.length ? fresh : evidence).map((x) => x.name).join(" and ")} active again`, state: null });
    } else if (state === "possible_vacancy") {
      steps.push({ time: e.time, text: "Every signal cleared", state: null });
    }
    let text = STATE_VERB[state] ?? state;
    if (state === "possible_vacancy" && e.required_quiet) text += ` (waiting ${duration(e.required_quiet)}${e.vacancy_evidence?.length ? `: ${e.vacancy_evidence.join(", ")}` : ""})`;
    if (state === "vacant" && e.reason) text = e.reason.replace(/\.$/, "");
    steps.push({ time: e.time, text, state });
    seen = new Set([...seen, ...evidence.map((x) => x.entity_id)]);
  }
  return steps;
}

// --------------------------------------------------------------- timeline
export type EventKind = "sensor" | "occupancy" | "config" | "start" | "availability";

export interface TimelineEvent {
  id: string;
  time: string;
  kind: EventKind;
  entity_id: string | null;
  title: string;
  detail: string | null;
  state: string | null;
  tone: "ok" | "info" | "warning" | "neutral" | "error" | "pending";
  source: "engine" | "recorder" | "live";
  data?: LogEntry;
}

export type SignalValue = "active" | "inactive" | "unknown" | "unavailable";

/** The engine's own reading of a state string (from the integration's lists), with the inverted flag. */
export function interpret(state: string | null | undefined, inverted: boolean, values: { active: string[]; inactive: string[] } | null): SignalValue {
  if (state === null || state === undefined) return "unknown";
  const text = String(state).trim().toLowerCase();
  if (text === "unavailable") return "unavailable";
  const active = values?.active ?? ["on"];
  const inactive = values?.inactive ?? ["off"];
  let v: SignalValue = active.includes(text) ? "active" : inactive.includes(text) ? "inactive" : "unknown";
  if (inverted && v !== "unknown") v = v === "active" ? "inactive" : "active";
  return v;
}

const VALUE_EVENT: Record<SignalValue, { word: string; tone: TimelineEvent["tone"] }> = {
  active: { word: "activated", tone: "ok" },
  inactive: { word: "cleared", tone: "neutral" },
  unknown: { word: "reports no usable value", tone: "warning" },
  unavailable: { word: "became unavailable", tone: "warning" },
};

function logEvent(e: LogEntry, name: (id: string) => string): TimelineEvent {
  if (e.kind === "transition") {
    const state = (e.state ?? "uncertain") as OccState;
    const info = STATE_INFO[state];
    const from = e.previous_state ? STATE_INFO[e.previous_state as OccState]?.label ?? e.previous_state : null;
    return {
      id: `log-${e.seq}`, time: e.time, kind: "occupancy", entity_id: null, state,
      title: `${from ? `${from} → ` : ""}${info?.label ?? state}`, detail: e.reason ?? null, tone: info?.tone ?? "neutral", source: "engine", data: e,
    };
  }
  if (e.kind === "config") {
    const items = (e.changes ?? []).map((c) => {
      if (c.type.startsWith("timing")) return `${c.key?.replace(/_/g, " ")}: ${c.old ?? "global"} → ${c.new ?? "global"}`;
      return `${c.type.replace(/_/g, " ")}${c.entity_id ? ` ${name(c.entity_id)}` : ""}`;
    });
    return {
      id: `log-${e.seq}`, time: e.time, kind: "config", entity_id: null, state: null,
      title: `Presence settings changed${e.source === "app" ? " in the App" : ""}${e.applied === "reload" ? " (reload)" : ""}`,
      detail: items.join("; ") || null, tone: "info", source: "engine", data: e,
    };
  }
  return { id: `log-${e.seq}`, time: e.time, kind: "start", entity_id: null, state: null, title: "Smart Occupancy started", detail: e.detail ?? null, tone: "pending", source: "engine", data: e };
}

/**
 * Events of one room, oldest first: the Recorder's sensor history, the engine's transitions and configuration
 * changes from the presence log, room states from the Recorder only before the log begins, and live changes
 * seen in the browser after the history was read. Duplicates (same entity and value within 2 s) are dropped.
 */
export function buildEvents(data: TimelineData, live: TimelineEvent[] = [], limit = 3000): TimelineEvent[] {
  const names = new Map(data.sensors.map((s) => [s.entity_id, s.name]));
  const name = (id: string): string => names.get(id) ?? id;
  const inverted = new Map(data.sensors.map((s) => [s.entity_id, Boolean(s.inverted)]));
  const out: TimelineEvent[] = [];
  const series = data.history.series ?? {};
  for (const s of data.sensors) {
    const points = series[s.entity_id] ?? [];
    let prev: SignalValue | null = null;
    points.forEach((p, i) => {
      const v = interpret(p.s, inverted.get(s.entity_id) ?? false, data.state_values);
      if (v === prev) return;
      const first = i === 0 && p.t <= data.start;
      prev = v;
      if (first) return; // the state at the start of the window: shown in the lanes, not as an event
      const info = VALUE_EVENT[v];
      out.push({
        id: `rec-${s.entity_id}-${p.t}`, time: p.t, kind: v === "unavailable" || v === "unknown" ? "availability" : "sensor",
        entity_id: s.entity_id, state: p.s, title: `${name(s.entity_id)} ${info.word}`, detail: `State “${p.s}”${inverted.get(s.entity_id) ? " (inverted)" : ""}`,
        tone: info.tone, source: "recorder",
      });
    });
  }
  const area = data.area_id;
  const logEntries = data.log.filter((e) => e.area_id === area || e.area_id === null);
  const firstLog = logEntries.find((e) => e.kind === "transition")?.time ?? null;
  const stateId = data.entities.state;
  if (stateId) {
    const points = series[stateId] ?? [];
    const reasons = data.entities.reason ? (series[data.entities.reason] ?? []) : [];
    let prev: string | null = null;
    for (const p of points) {
      if (p.s === prev) continue;
      const was = prev;
      prev = p.s;
      if (p.t <= data.start || (firstLog && p.t >= firstLog)) continue;
      const reason = reasons.find((r) => Math.abs(Date.parse(r.t) - Date.parse(p.t)) < 2000)?.s ?? null;
      const info = STATE_INFO[p.s as OccState];
      const label = (s: string): string => STATE_INFO[s as OccState]?.label ?? (s === "unavailable" || s === "unknown" ? "Not reported" : s);
      out.push({
        id: `rec-state-${p.t}`, time: p.t, kind: "occupancy", entity_id: stateId, state: p.s,
        title: `${was ? `${label(was)} → ` : ""}${label(p.s)}`,
        detail: info ? (reason && reason !== "unavailable" && reason !== "unknown" ? reason : null)
          : "The room's state entity had no value (Home Assistant stopped or Smart Occupancy reloaded).",
        tone: info?.tone ?? "pending", source: "recorder",
      });
    }
  }
  for (const e of logEntries) if (e.time >= data.start) out.push(logEvent(e, name));
  const lastRecorded = new Map<string, TimelineEvent>();
  for (const e of out) if (e.entity_id) lastRecorded.set(e.entity_id, e);
  for (const e of live) {
    const dup = out.some((x) => x.entity_id === e.entity_id && x.kind === e.kind && x.state === e.state && Math.abs(Date.parse(x.time) - Date.parse(e.time)) < 2000);
    if (!dup && e.time >= data.start) out.push(e);
  }
  out.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return out.length > limit ? out.slice(out.length - limit) : out;
}

export interface Segment {
  start: number;
  end: number;
  value: string;
}

/** Lanes: the value of an entity over the window (the Recorder's points, then the live value up to ``end``). */
export function segments(points: Array<{ t: string; s: string }>, start: number, end: number, classify: (s: string) => string): Segment[] {
  const out: Segment[] = [];
  const sorted = points.map((p) => ({ t: Date.parse(p.t), v: classify(p.s) })).filter((p) => Number.isFinite(p.t)).sort((a, b) => a.t - b.t);
  for (let i = 0; i < sorted.length; i++) {
    const from = Math.max(sorted[i].t, start);
    const to = Math.min(i + 1 < sorted.length ? sorted[i + 1].t : end, end);
    if (to <= from) continue;
    const last = out[out.length - 1];
    if (last && last.value === sorted[i].v && last.end >= from) last.end = to;
    else out.push({ start: from, end: to, value: sorted[i].v });
  }
  return out;
}

/** A Home Assistant time (24-hour, in Home Assistant's time zone when it is valid). */
export function timeFormatter(timeZone: string | null | undefined, withSeconds = true): (iso: string | null | undefined) => string {
  let fmt: Intl.DateTimeFormat;
  let dayFmt: Intl.DateTimeFormat;
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}), hourCycle: "h23" };
  try {
    fmt = new Intl.DateTimeFormat(undefined, { ...opts, timeZone: timeZone || undefined });
    dayFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", timeZone: timeZone || undefined });
  } catch {
    fmt = new Intl.DateTimeFormat(undefined, opts);
    dayFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
  }
  return (iso) => {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "—";
    const today = dayFmt.format(new Date());
    const day = dayFmt.format(d);
    return day === today ? fmt.format(d) : `${day} ${fmt.format(d)}`;
  };
}

/** Where a diagnostic's "Go to" link leads (the matching configuration screen). */
export function diagnosticLink(d: Diagnostic, areaId: string | null | undefined): string {
  const area = areaId ?? d.area_id ?? null;
  if (!area) return "#/presence/settings";
  const base = `#/presence/${encodeURIComponent(area)}`;
  if (d.target === "rooms_editor") return `#/rooms/${encodeURIComponent(area)}/presence`;
  if (d.target === "timing") return `${base}?tab=timing${d.setting ? `&setting=${encodeURIComponent(d.setting)}` : ""}`;
  if (d.target === "sensors" && d.entity_id) return `${base}?sensor=${encodeURIComponent(d.entity_id)}`;
  if (d.target === "zones") return `${base}?tab=live`;
  return base;
}

export const BASIS_INFO: Record<Basis, { label: string; help: string }> = {
  measured: { label: "Measured", help: "Seen in the engine's current state or the sensors' states right now." },
  integration: { label: "Health check", help: "Reported by the integration's own configuration and health checks." },
  heuristic: { label: "Suggestion", help: "A hint from the configuration; it may be intended. Nothing is changed automatically." },
};
