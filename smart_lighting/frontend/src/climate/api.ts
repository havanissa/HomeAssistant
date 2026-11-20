// Phase A7.0: the Climate API of Smart Comfort (observation only). Every temperature from the backend is in °C;
// the interface converts to the display unit (Settings → General) and back. Nothing here can command a thermostat.
import { ApiError, getAdmin, getRooms, postRooms } from "../rooms/model";

export { ApiError };

export type Quality = "ok" | "stale" | "unavailable" | "unknown" | "missing" | "not_numeric" | "wrong_unit" | "implausible" | "none";

export interface Ref { entity_id: string; uid?: string | null }
export interface ThermostatRef extends Ref { kind: string; capabilities?: Capabilities | null }
export interface Capabilities {
  hvac_modes: string[];
  min_temp_c: number | null;
  max_temp_c: number | null;
  step: number | null;
  target: boolean;
  target_range: boolean;
  presets: string[];
  reports_action: boolean;
  reports_current: boolean;
  turn_off: boolean;
  external_input: string | null;
}
export interface Targets { comfort_c: number | null; setback_c: number | null; min_target_c: number | null; max_target_c: number | null; stale_minutes: number | null }
export interface RoomConfig {
  thermostats: ThermostatRef[];
  primary_sensor: Ref | null;
  secondary_sensors: Ref[];
  openings: OpeningRef[];
  overrides: Targets;
}
/** A7.1: a person can say what an opening sensor is when Home Assistant's device class does not. */
export interface OpeningRef extends Ref { kind?: "window" | "door" | null }
export interface Zone {
  id: string;
  name: string;
  rooms: string[];
  layout: "single" | "connected" | "open_plan" | string;
  shared_thermostats: string[];
  heating_circuit: string | null;
  origin: string;
  names?: string[];
}
export interface ClimateConfig {
  format: string;
  schema: number;
  mode: "observing";
  units: { display: "auto" | "celsius" | "fahrenheit" };
  defaults: Targets & { stale_minutes: number };
  observation: { enabled: boolean; retention_days: number; backfill_days: number; report_minutes: number };
  outdoor: Ref | null;
  rooms: Record<string, RoomConfig>;
  zones: Zone[];
  prediction: { min_history_days: number };
  updated_at: string | null;
}
export interface Backfill { state: string; done_at: string | null; error: string | null; windows: number; records: number; entities: number }
export interface Status {
  writable: boolean;
  read_only: string | null;
  read_only_code: string | null;
  revision: string;
  saved_at: string | null;
  notices: Array<{ code: string; message: string }>;
  mode: string;
  rooms: number;
  zones: number;
  observation: { files: number; bytes: number; first_day: string | null; last_day: string | null; written: number; pending: number; writable: boolean; error: string | null; tracked: number; backfill: Backfill };
}
export interface ClimateDocument { config: ClimateConfig; revision: string; status: Status; unit: Unit; ha_unit: string }
export type Unit = "°C" | "°F";

export interface Reading {
  entity_id: string | null;
  name: string | null;
  value_c: number | null;
  raw: string | null;
  unit: string | null;
  quality: Quality;
  age_s: number | null;
  last_changed: string | null;
}
export interface ThermostatView {
  entity_id: string;
  name: string | null;
  available: boolean;
  state: string;
  hvac_mode: string | null;
  hvac_action: string | null;
  heating: { state: "heating" | "not_heating" | "unknown"; basis: "reported" | "not_reported" | "unavailable" };
  measured: Reading;
  target_c: number | null;
  target_low_c: number | null;
  target_high_c: number | null;
  preset: string | null;
  kind: string;
  kind_label: string;
  capabilities: Capabilities | null;
  capabilities_changed: boolean;
  shared: boolean;
}
export interface OpeningView {
  entity_id: string;
  name: string | null;
  /** window | door (set by a person, or from Home Assistant's device class), null when not known. */
  kind: "window" | "door" | null;
  kind_source: "configured" | "device_class" | "none";
  device_class_kind: string | null;
  status: "open" | "closed" | "unavailable" | "unknown" | "missing";
  since: string | null;
}
export interface CompletenessItem { key: string; ok: boolean; required: boolean; text: string }
export interface RoomView {
  area_id: string;
  name: string;
  area_exists: boolean;
  temperature: { value_c: number | null; source: "primary" | "secondary" | "none"; entity_ids: string[] };
  primary: Reading;
  secondary: Reading[];
  thermostats: ThermostatView[];
  openings: OpeningView[];
  any_open: boolean;
  heating: { reported: number; not_reported: number; devices: number };
  demand: { basis: "inferred"; entity_ids: string[]; text: string } | null;
  outdoor_difference_c: number | null;
  occupancy: { occupied: boolean | null; state: string | null; source: string } | null;
  occupancy_entity: string | null;
  zone: { id: string; name: string; layout: string } | null;
  targets: Targets;
  completeness: { score: number; ready: boolean; items: CompletenessItem[] };
  covers: string[];
  learning?: LearningSummary;
}
export interface Problem { area_id: string | null; room: string | null; role: string; entity_id: string; quality: string; text: string }
export interface Overview {
  mode: string;
  unit: Unit;
  ha_unit: string;
  rooms: RoomView[];
  outdoor: Reading;
  problems: Problem[];
  zones: Zone[];
  completeness: { rooms: number; ready: number; score: number };
  learning: { history_days: number; min_history_days: number; available: boolean; text: string } & Partial<LearningOverview>;
  status: Status;
  generated_at: string;
}
export interface Series { key: string; label: string; kind: "room" | "secondary" | "thermostat" | "setpoint" | "outdoor" | "model" | "persistence"; entity_id: string; points: Array<[number, number | null]> }
export interface Band { key: string; label: string; kind: "heating" | "open" | "occupied" | "excluded"; entity_id: string; intervals: Array<[number, number]>; reported?: boolean; opening?: string | null }
export interface History {
  area_id: string;
  window: number;
  windows: number[];
  start: number;
  end: number;
  series: Series[];
  bands: Band[];
  unit: Unit;
  observed_from: number | null;
  gaps: Record<string, Array<[number, number]>>;
  records: number;
  backfill: Backfill;
  source: string;
  time_zone: string | null;
}

export interface DiscoveredThermostat {
  entity_id: string;
  name: string;
  area_id: string | null;
  area: string | null;
  device: string | null;
  model: string | null;
  platform: string | null;
  kind: string;
  capabilities: Capabilities | null;
  state: ThermostatView;
}
export interface DiscoveredSensor {
  entity_id: string;
  name: string;
  area_id: string | null;
  area: string | null;
  device: string | null;
  outdoor: boolean;
  on_thermostat: boolean;
  not_room: boolean;
  value_c: number | null;
  quality: Quality;
  unit: string | null;
}
export interface DiscoveredOpening { entity_id: string; name: string; area_id: string | null; area: string | null; kind: string; status: string }
export interface DiscoveredOutdoor { entity_id: string; name: string; area: string | null; source: "sensor" | "weather"; value_c: number | null; quality: Quality }
export interface RoomSuggestion { thermostats: Array<{ entity_id: string; kind: string }>; primary_sensor: string | null; secondary_sensors: string[]; openings: string[]; reason: string }
export interface ZoneSuggestion { origin: string; name: string | null; rooms: string[]; layout: string; reason: string; configured: boolean; grouped: boolean }
export interface Discovery {
  areas: Array<{ id: string; name: string }>;
  thermostats: DiscoveredThermostat[];
  sensors: DiscoveredSensor[];
  openings: DiscoveredOpening[];
  outdoor: DiscoveredOutdoor[];
  suggestions: { rooms: Record<string, RoomSuggestion>; zones: ZoneSuggestion[]; outdoor: string | null };
  kinds: Record<string, string>;
  layouts: Record<string, string>;
  unit: Unit;
  ha_unit: string;
}
export interface ConfigProblem { path: string; message: string; code: string }
export interface Change { path: string; kind: "room_added" | "room_removed" | "zone_added" | "zone_removed" | "changed"; before: unknown; after: unknown; area_id?: string; zone_id?: string }
export interface Review {
  valid: boolean;
  errors: ConfigProblem[];
  warnings: ConfigProblem[];
  changes: Change[];
  identical?: boolean;
  revision: string;
  stale: boolean;
  writable: boolean;
  summary?: { rooms: number; zones: number; thermostats: number };
  current?: { rooms: number; zones: number; thermostats: number };
}
export interface BackupInfo { id: string; created_at: string | null; reason: string | null; revision: string | null; summary: { rooms: number; zones: number; thermostats: number } | null }

export const WINDOW_LABELS: Record<number, string> = { 21600: "6 h", 86400: "24 h", 259200: "3 days", 604800: "7 days", 2592000: "30 days" };

export const getDocument = (): Promise<ClimateDocument> => getRooms<ClimateDocument>("api/climate");
export const getOverview = (): Promise<Overview> => getRooms<Overview>("api/climate/overview");
export const getDiscovery = (): Promise<Discovery> => getRooms<Discovery>("api/climate/discovery");
export const getRoom = (areaId: string): Promise<RoomView> => getRooms<RoomView>(`api/climate/rooms/${encodeURIComponent(areaId)}`);
export const getHistory = (areaId: string, window: number): Promise<History> =>
  getRooms<History>(`api/climate/rooms/${encodeURIComponent(areaId)}/history?window=${window}`);
export const getBackups = async (): Promise<BackupInfo[]> => (await getAdmin<{ backups: BackupInfo[] }>("api/climate/backups")).backups;
export const review = (config: ClimateConfig, revision: string): Promise<Review> => postRooms<Review>("api/climate/review", { config, revision });
export const save = (config: ClimateConfig, revision: string): Promise<{ result: { saved: boolean; backup: string | null }; document: ClimateDocument }> =>
  postRooms("api/climate/save", { config, revision });
export const restoreReview = (backup: string): Promise<Review> => postRooms<Review>("api/climate/restore/review", { backup });
export const restore = (backup: string, revision: string): Promise<{ document: ClimateDocument }> => postRooms("api/climate/restore", { backup, revision });
export const importReview = (file: unknown): Promise<Review> => postRooms<Review>("api/climate/import/review", { file });
export const importApply = (file: unknown, revision: string): Promise<{ document: ClimateDocument }> => postRooms("api/climate/import", { file, revision });
export const exportUrl = "api/climate/export";

// ------------------------------------------------------------------ units
export function toDisplay(c: number | null | undefined, unit: Unit): number | null {
  if (c === null || c === undefined) return null;
  return unit === "°F" ? c * 1.8 + 32 : c;
}

export function fromDisplay(v: number, unit: Unit): number {
  return Math.round((unit === "°F" ? (v - 32) / 1.8 : v) * 100) / 100;
}

/** A temperature for people: one decimal in the display unit, or "—". */
export function temp(c: number | null | undefined, unit: Unit): string {
  const v = toDisplay(c, unit);
  return v === null ? "—" : `${v.toFixed(1)} ${unit}`;
}

/** A temperature difference (no 32° offset for °F). */
export function delta(c: number | null | undefined, unit: Unit): string {
  if (c === null || c === undefined) return "—";
  const v = unit === "°F" ? c * 1.8 : c;
  return `${v > 0 ? "+" : ""}${v.toFixed(1)} ${unit}`;
}

export const QUALITY: Record<string, { label: string; tone: "ok" | "warning" | "error" | "neutral" | "info" }> = {
  ok: { label: "OK", tone: "ok" },
  stale: { label: "Not updated recently", tone: "warning" },
  unavailable: { label: "Unavailable", tone: "error" },
  unknown: { label: "No value", tone: "warning" },
  missing: { label: "Entity not found", tone: "error" },
  not_numeric: { label: "Not a number", tone: "error" },
  wrong_unit: { label: "Not a temperature unit", tone: "error" },
  implausible: { label: "Implausible", tone: "warning" },
  none: { label: "Not assigned", tone: "neutral" },
};

export function age(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

export function emptyRoom(): RoomConfig {
  return { thermostats: [], primary_sensor: null, secondary_sensors: [], openings: [], overrides: { comfort_c: null, setback_c: null, min_target_c: null, max_target_c: null, stale_minutes: null } };
}

export function slugify(name: string, taken: Set<string>): string {
  const base = name.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40) || "zone";
  let id = base;
  let n = 2;
  while (taken.has(id)) id = `${base}_${n++}`;
  return id;
}

/** A deep copy without the backend-filled fields that the browser does not decide (registry ids are kept). */
export function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

// ------------------------------------------------------- thermal learning (A7.1)
export type LearningStatus = "no_data" | "collecting" | "insufficient" | "learning" | "low" | "moderate" | "high" | "unidentifiable" | "error";
export interface Prerequisite { key: string; text: string; blocking: boolean }
export interface LearningSummary {
  area_id: string;
  status: LearningStatus;
  status_text: string;
  queued: boolean;
  running: boolean;
  observed_days: number;
  usable_days: number | null;
  min_history_days: number;
  ready: boolean;
  validity: { state: "none" | "valid" | "outdated" | "invalidated"; reasons: string[]; keys?: string[] };
  prerequisites: Prerequisite[];
  last_attempt: { started_at: string | null; finished_at: string | null; result: string | null; error: string | null; reason: string | null; kept: string | null } | null;
  problem: { code: string; message: string } | null;
  trained_at: string | null;
  preliminary: boolean | null;
  heating?: { value: number | null; basis: "reported" | "inferred" | null; confidence: string | null; reason: string | null };
  cooling?: { time_constant_h: number | null; k_per_h: number | null; reference: "outdoor" | "fixed"; confidence: string | null };
  confidence?: { level: string; score: number };
  accuracy?: { horizon_min: number; mae: number; rmse: number; persistence_mae: number; origins: number };
}
export interface LearningOverview {
  rooms: Record<string, LearningSummary>;
  counts: Record<string, number>;
  running: string | null;
  queued: string[];
  notices: Array<{ code: string; message: string; area_id?: string }>;
  store: { models: number; bytes: number; writable: boolean; error: string | null };
  algorithm: string;
  statuses: Record<string, string>;
}
export interface ModelParam {
  value: number | null;
  unit: string;
  available: boolean;
  se: number | null;
  confidence: "none" | "low" | "moderate" | "high";
  basis: string | null;
  reason: string | null;
  text: string | null;
}
export interface Metrics { mae: number; rmse: number; bias: number; n: number }
export interface HorizonResult { available: boolean; origins: number; reason?: string; model?: Metrics; persistence?: Metrics; passive?: Metrics | null; skill?: number | null }
export interface ValidationChart {
  from: number; to: number;
  actual: Array<[number, number | null]>;
  model: Array<[number, number | null]>;
  persistence: Array<[number, number | null]>;
  error_model: Array<[number, number | null]>;
  error_persistence: Array<[number, number | null]>;
  excluded: Array<[number, number]>;
  restart_hours: number;
}
export interface ThermalModelDoc {
  format: string; schema: number; algorithm: string; area_id: string; trained_at: string; app_version?: string;
  status: string; preliminary: boolean; reason?: string; training_seconds?: number; config_revision?: string;
  structure: { reference: "outdoor" | "fixed"; reference_c: number | null; heating: "reported" | "inferred" | null; solar: boolean; night: boolean; coupling: string[] };
  parameters: Record<string, ModelParam>;
  dynamics: { lag_min: number; tau_min: number; identifiable?: boolean; reason?: string | null };
  data: { from: number | null; to: number | null; split?: number; train?: { from: number; to: number; blocks: number }; test?: { from: number; to: number }; grid_s: number; block_s: number; min_history_days: number };
  evidence: Evidence;
  selection?: Array<{ term: string; accepted: boolean; gain?: number; reason?: string; removed?: boolean; rmse_without?: number; rmse_with?: number }>;
  stability?: { windows: number; cv: Record<string, number>; values?: Record<string, number[]> };
  validation: null | {
    from: number; to: number; usable_hours: number; coverage: number; headline: string | null;
    horizons: Record<string, HorizonResult>;
    conditions: Record<string, Record<string, { origins: number; model_mae: number; persistence_mae: number }>>;
    chart: ValidationChart;
  };
  confidence: { level: string; score: number; factors: Array<{ key: string; value: number; weight: number; text: string }>; note?: string; capped?: string };
  uncertainty: Record<string, { q50: number; q80: number; q95: number; n: number }>;
  explanations: string[];
  fit?: { rmse_c_per_h: number; blocks: number; residual_autocorrelation: number };
}
export interface Evidence {
  calendar_days: number; observed_hours: number; usable_hours: number; usable_days: number;
  excluded: Record<string, number>; excluded_text: Record<string, string>;
  episodes: Record<"heating" | "passive" | "solar", { count: number; hours: number }>;
  open_windows: { count: number; hours: number };
  secondary_hours: number; discontinuities: number; heat_basis: string; solar_basis: string; inferred_demand_hours: number;
  reference: string; outdoor_hours: number;
  heating_rates?: Array<{ bin: string; episodes: number; median_rate_c_per_h: number }>;
  cooling_rates?: Array<{ bin: string; episodes: number; median_rate_c_per_h: number }>;
  heating_median_rate_c_per_h?: number | null;
  passive_median_rate_c_per_h?: number | null;
  after_heating?: { stops: number; median_change_c: number; share_warming: number } | null;
}
export interface LearningRoom {
  summary: LearningSummary;
  model: ThermalModelDoc | null;
  attempt: Record<string, unknown> & { diagnostic?: { evidence?: Evidence; reason?: string; status?: string }; previous?: { result: string | null; finished_at: string | null; error: string | null } | null };
  entities: Record<string, string>;
  recorder_gaps: Record<string, Array<[number, number]>>;
  recorder_gap_hours: number;
  coverage: Record<string, { from: number; to: number } | null>;
  algorithm: string;
  schema: number;
  unit: Unit;
  names: Record<string, string>;
}

export const getLearning = (): Promise<LearningOverview> => getRooms<LearningOverview>("api/climate/learning");
export const getRoomLearning = (areaId: string): Promise<LearningRoom> => getRooms<LearningRoom>(`api/climate/rooms/${encodeURIComponent(areaId)}/learning`);
export const rebuild = (areaId: string | null): Promise<{ queued: string[] }> => postRooms("api/climate/learning/rebuild", { area_id: areaId });
export const resetModel = (areaId: string): Promise<{ reset: boolean; archived: boolean }> => postRooms("api/climate/learning/reset", { area_id: areaId, confirm: true });
export const learningExportUrl = (areaId: string): string => `api/climate/rooms/${encodeURIComponent(areaId)}/learning/export`;

/** A rate in °C/h shown in the display unit (a difference: no 32° offset). */
export function rate(cph: number | null | undefined, unit: Unit, digits = 2): string {
  if (cph === null || cph === undefined) return "—";
  const v = unit === "°F" ? cph * 1.8 : cph;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)} ${unit}/h`;
}

export const OPENING_LABEL: Record<string, string> = { window: "Window", door: "Door" };
/** "Window", "Door", or that the type is not known (Home Assistant's device class does not say). */
export function openingLabel(kind: string | null | undefined): string {
  return (kind && OPENING_LABEL[kind]) || "Window or door (type not set)";
}

export const LEARNING_TONE: Record<string, "ok" | "warning" | "error" | "neutral" | "info"> = {
  no_data: "neutral", collecting: "info", insufficient: "warning", learning: "info", low: "warning", moderate: "ok",
  high: "ok", unidentifiable: "warning", error: "error",
};
