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
  openings: Ref[];
  overrides: Targets;
}
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
export interface OpeningView { entity_id: string; name: string | null; kind: string | null; status: "open" | "closed" | "unavailable" | "unknown" | "missing"; since: string | null }
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
  learning: { history_days: number; min_history_days: number; available: boolean; text: string };
  status: Status;
  generated_at: string;
}
export interface Series { key: string; label: string; kind: "room" | "secondary" | "thermostat" | "setpoint" | "outdoor"; entity_id: string; points: Array<[number, number | null]> }
export interface Band { key: string; label: string; kind: "heating" | "open" | "occupied"; entity_id: string; intervals: Array<[number, number]>; reported?: boolean }
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
