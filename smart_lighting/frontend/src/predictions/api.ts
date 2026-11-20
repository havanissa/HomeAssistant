// Phase A5: the Predictions API. One command API (the integration's own forecast websocket commands, same
// payloads and answers as its dashboard cards), wherever the forecasts are produced: the App backend forwards
// them to the integration while it produces the forecasts and runs the same handlers itself after the handover.
// Forecasts are observation only on both sides: nothing here commands a device.
import { ApiError, getRooms, postRooms } from "../rooms/model";
import type { OwnershipEntry, ReadinessCheck } from "../lighting/api";

export { ApiError };
export type { OwnershipEntry, ReadinessCheck };

/** One forecast command, e.g. forecast(cmd("settings"), { area_id: null, values }). */
export async function forecast<T = unknown>(type: string, params: Record<string, unknown> = {}): Promise<T> {
  const reply = await postRooms<{ result: T; handled_by: "app" | "integration" }>("api/predictions/command", { type, ...params });
  return reply.result;
}

/** The full command name: forecast(cmd("learn")). */
export function cmd(name: string): string {
  return `smart_occupancy/forecast/${name}`;
}

export const HORIZONS = [5, 15, 30, 60] as const;
export type ForecastStatus = "ok" | "insufficient_data" | "unavailable" | "disabled" | "learning";
export type ConfidenceLevel = "insufficient_data" | "low" | "moderate" | "high";

export interface Confidence {
  level: ConfidenceLevel | string;
  score: number;
  reasons: string[];
  components: Record<string, unknown>;
}

export interface Factor {
  kind: string;
  text: string;
  impact: number;
  details: Record<string, unknown>;
}

export interface ForecastWindow {
  kind: string;
  source: "forecast" | "schedule" | string;
  start: string | null;
  end: string | null;
  text: string;
}

/** ``RoomForecast.as_dict()``: percentages are whole numbers 0..100. */
export interface RoomForecast {
  area_id: string;
  status: ForecastStatus | string;
  horizon: number;
  threshold: number;
  confidence_threshold: string;
  occupied: boolean | null;
  state: string | null;
  probability: number | null;
  probabilities: Record<string, number> | null;
  entry: Record<string, number> | null;
  schedule: Record<string, number> | null;
  predicted: boolean | null;
  confidence: Confidence | null;
  explanation: string;
  factors: Factor[];
  window: ForecastWindow | null;
  next_rooms: Array<{ area_id: string; name: string; probability: number }>;
  updated: string | null;
}

export interface LearningStatus {
  state: string;
  detail: string;
  progress: [number, number] | null;
  last_update: string | null;
  last_error: string | null;
  recorder_oldest: string | null;
  queries: number;
  last_duration_seconds: number | null;
}

export interface CompareSide {
  status: string | null;
  probability: number | null;
  predicted: boolean | null;
  confidence: string | null;
}

export interface CompareRecord {
  at: string | null;
  area_id: string;
  integration: CompareSide;
  app: CompareSide;
}

export interface Comparison {
  running: boolean;
  started_at: string | null;
  seconds: number;
  pair_seconds: number;
  tolerance_points: number;
  pairs: number;
  agreements: number;
  disagreements: number;
  last_agreement_at: string | null;
  last_disagreement_at: string | null;
  rooms: Record<string, CompareRecord>;
  log: CompareRecord[];
}

/** ``Accuracy.as_dict(threshold)`` (prequential totals of one room or the house, one horizon). */
export interface AccuracyStats {
  forecasts: number;
  brier: number | null;
  brier_schedule: number | null;
  brier_persistence: number | null;
  skill_vs_schedule: number | null;
  skill_vs_persistence: number | null;
  calibration: Array<{ bin: string; forecasts: number; mean_forecast: number | null; observed: number | null }>;
  threshold: number;
  true_positive: number;
  false_positive: number;
  false_negative: number;
  true_negative: number;
}

export interface GlobalSettings {
  enabled: boolean;
  window_days: number;
  half_life_days: number;
  refresh_minutes: number;
  horizon_minutes: number;
  probability_threshold: number;
  confidence_threshold: string;
  min_days: number;
  min_entries: number;
  contexts: string[] | null;
  workday_entity: string | null;
}

export interface RoomOverrides {
  enabled: boolean | null;
  horizon_minutes: number | null;
  probability_threshold: number | null;
  confidence_threshold: string | null;
}

export interface Connection {
  a: string;
  b: string;
  kind: string;
}

export interface AllSettings {
  global: GlobalSettings;
  rooms: Record<string, RoomOverrides>;
  connections: Connection[] | null;
}

export interface NextRoom {
  area_id: string;
  probability: number;
  source: string;
  since: number;
  candidates: Array<[string, number]>;
}

export interface HouseRoom {
  area_id: string;
  name: string;
  now: RoomForecast | null;
  shadow_now: RoomForecast | null;
  enabled: boolean;
  horizon_minutes: number;
  probability_threshold: number;
  confidence_threshold: string;
  days: number;
  entries: number;
  observed_hours: number;
  comparison: CompareRecord | null;
}

export interface Decision {
  id: string;
  at: string;
  first_at: string;
  count: number;
  module: string;
  subject: string;
  trigger: string;
  kind: string;
  action: string;
  explanation: string;
  mode: string;
  status: string;
  target: string | null;
  details: Record<string, unknown>;
}

export interface DecisionLog {
  latest: Decision | null;
  entries: Decision[];
  size: number;
}

export interface House {
  mode: string;
  source: "app" | "integration";
  view_at: string | null;
  learning: Partial<LearningStatus>;
  coverage: {
    days_learned?: number;
    first_date?: string | null;
    last_date?: string | null;
    window_days?: number;
    days_without_data?: number;
    today_included?: boolean;
    recorder_oldest?: string | null;
    model_days?: number;
  };
  rooms: HouseRoom[];
  next_room: NextRoom | null;
  accuracy: Record<string, AccuracyStats>;
  settings: AllSettings | null;
  workday_entity: string | null;
  contexts: string[];
  house_log: DecisionLog | null;
}

export interface PredictionsStatus {
  mode: "starting" | "standalone" | "shadow" | "owner";
  bridge: string;
  bridge_error: string | null;
  instance_id: string;
  session_id: string | null;
  app_version: string;
  runtime_version: string;
  fingerprint: string | null;
  core_fingerprint: string | null;
  acknowledged_owner: string | null;
  commands_go_to: "app" | "integration";
  integration: {
    owner: string | null;
    state: string | null;
    epoch: number | null;
    integration_version: string | null;
    fingerprint: string | null;
    lease_until: string | null;
    changed_at: string | null;
    changed_by: string | null;
    settings_revision: string | null;
    days_learned: number | null;
    learned_revision: string | null;
    recorder_available: boolean | null;
    recorder_oldest: string | null;
    learning: Partial<LearningStatus> | null;
    manager: unknown;
    published: unknown;
    rejections: Record<string, number> | null;
    history: OwnershipEntry[] | null;
  };
  integration_status: Record<string, unknown> | null;
  epoch: number | null;
  lease_until: string | null;
  settings_revision: string | null;
  integration_settings_revision: string | null;
  learned_revision: string | null;
  days_learned: number;
  history_calls: number;
  running: boolean;
  counters: Record<string, number>;
  last_error: { at: string; code: string; detail: string } | null;
  notices: { code: string; text: string }[];
  ownership_log: { at: string; event: string; [key: string]: unknown }[];
  last_copy: CopyCheck | null;
  comparison: Comparison;
  readiness: {
    ready: boolean;
    checks: ReadinessCheck[];
    summary: { seconds: number; matched: number; mismatches: number };
    thresholds: { min_shadow_seconds: number; min_matches: number };
  };
  backups: { id: string; size: number }[];
  user: { is_admin: boolean };
  area_names: Record<string, string>;
  house: House | null;
  house_error?: string;
}

export interface CopyCheck {
  source_days: number;
  destination_days: number;
  source_revision: string | null;
  destination_revision: string | null;
  identical: boolean;
  differences: string[];
}

export interface Patterns {
  days: number;
  weekly: Array<Array<number | null>>;
  weekly_observed_hours: number[][];
  hourly_observed: Array<number | null>;
  hourly_entries: Array<number | null>;
  hourly_learned: Array<number | null> | null;
  hourly_workday: Array<number | null> | null;
  hourly_day_off: Array<number | null> | null;
}

export interface Movement {
  next: Array<{ area_id: string; name: string; probability: number | null; count: number; median_delay: number; by_part: Record<string, number | null> }>;
  previous: Array<{ area_id: string; name: string; share: number | null; count: number; median_delay: number }>;
  arrivals: number;
}

export interface ConnectionItem extends Connection {
  other: string;
  other_name: string;
  kind_name: string;
}

export interface ConnectionSuggestion extends Connection {
  reason: "configuration" | "learned" | string;
  moves?: number;
  other: string;
  other_name: string;
}

export interface Connections {
  configured: boolean;
  items: ConnectionItem[];
  suggestions: ConnectionSuggestion[];
  kinds: Array<{ key: string; name: string }>;
  rooms: Array<{ area_id: string; name: string }>;
}

export interface RoomAccuracy {
  by_horizon: Record<string, AccuracyStats>;
  daily: Array<{ date: string; forecasts: number; brier: number | null; brier_schedule: number | null; brier_persistence: number | null }>;
  house: AccuracyStats;
  horizon: number;
  method: string;
}

export interface ContextInfo {
  key: string;
  name: string;
  automatic: boolean;
  state: string | null;
  factor: { key: string; entry_on: number; entry_off: number; exit_on: number; exit_off: number; entries_on: number; expected_on: number; hours_on: number } | null;
}

export interface RoomSettingsModel {
  room: RoomOverrides;
  effective: { enabled: boolean; horizon_minutes: number; probability_threshold: number; confidence_threshold: string };
  overridden: string[];
  global: GlobalSettings;
  limits: Record<string, { default: number; min: number; max: number }>;
  horizons: number[];
  confidence_options: string[];
  confidence_levels: string[];
  automatic_contexts: Array<{ entity_id: string; name: string }>;
  contexts: Array<{ entity_id: string; name: string }>;
  workday_entity: string | null;
  workday_name: string | null;
  workday_automatic: boolean;
  max_contexts: number;
}

export interface ContextCandidate {
  entity_id: string;
  name: string;
  workday: boolean;
}

export interface RoomDetail {
  area_id: string;
  name: string;
  observation_only: boolean;
  now: RoomForecast | null;
  shadow_now: RoomForecast | null;
  source: "app" | "integration";
  comparison: CompareRecord | null;
  learning: Partial<LearningStatus> & {
    days_learned: number; today_included: boolean; window_days: number; room_days: number; room_weekdays: number;
    room_first_date: string | null; room_last_date: string | null; room_observed_hours: number; room_entries: number; model_days: number;
  };
  patterns: Patterns;
  movement: Movement;
  connections: Connections;
  accuracy: RoomAccuracy;
  contexts: ContextInfo[];
  settings: RoomSettingsModel;
  context_candidates: ContextCandidate[];
  log: DecisionLog | null;
  house_log: DecisionLog | null;
}

export interface ImportReview {
  writable: boolean;
  days: number;
  invalid_days: number;
  current_days: number;
  current_revision: string | null;
  incoming_revision: string | null;
  exported_at: string | null;
  same: boolean;
}

export function predictionsStatus(): Promise<PredictionsStatus> {
  return getRooms<PredictionsStatus>("api/predictions");
}

export function predictionsRoom(areaId: string): Promise<RoomDetail> {
  return getRooms<RoomDetail>(`api/predictions/rooms/${encodeURIComponent(areaId)}`);
}

export async function handover(): Promise<{ epoch: number; copy: CopyCheck }> {
  return (await postRooms<{ result: { epoch: number; copy: CopyCheck } }>("api/predictions/handover", { confirm: true })).result;
}

/** mode "release": the App hands its learned data back; "return": the integration takes over from its copy. */
export async function releaseControl(mode: "release" | "return"): Promise<unknown> {
  return (await postRooms<{ result: unknown }>("api/predictions/release", { mode })).result;
}

export const reviewImport = (file: unknown): Promise<ImportReview> => postRooms<ImportReview>("api/predictions/import/review", { file });

export const applyImport = (file: unknown, revision: string | null): Promise<{ applied: boolean; days: number }> =>
  postRooms<{ applied: boolean; days: number }>("api/predictions/import", { file, revision });

/** Who produces the forecasts now, in words (as the integration acknowledges it). */
export function ownerLabel(acknowledged: string | null | undefined): { app: boolean; label: string } {
  const app = acknowledged === "app" || acknowledged === "resume_pending";
  return { app, label: app ? "Smart Comfort" : "The integration" };
}
