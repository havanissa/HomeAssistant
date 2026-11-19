// Phase A5: the curtains API. One command API (the integration's own curtain websocket commands, the same
// payloads and answers as its Smart Curtains card), wherever the configuration lives: the App backend forwards
// them to the integration while it controls the curtains and runs the same handlers itself after the handover.
import { ApiError, getRooms, postRooms } from "../rooms/model";

export { ApiError };

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type Obj = Record<string, any>;

/** One curtain command, e.g. curtains(cmd("model")). */
export async function curtains<T = unknown>(type: string, params: Record<string, unknown> = {}): Promise<T> {
  const reply = await postRooms<{ result: T; handled_by: "app" | "integration" }>("api/curtains/command", { type, ...params });
  return reply.result;
}

/** The full command name: curtains(cmd("preset/select"), …). */
export function cmd(name: string): string {
  return `smart_occupancy/curtains/${name}`;
}

// ------------------------------------------------------------------ status (api/curtains)
export interface ReadinessCheck {
  key: string;
  status: "ok" | "error" | "warning";
  title: string;
  detail: string;
}

export interface CoverCommandBrief {
  purpose: string | null;
  window_id: string | null;
  service: string;
  entity_id: string | null;
  position: number | null;
  error?: string | null;
}

export interface CoverDiscrepancy {
  at: string | null;
  only: "app" | "integration";
  purpose: string;
  window_id: string | null;
  command: CoverCommandBrief;
  other_side: CoverCommandBrief[];
}

export interface CurtainDecisionDifference {
  at: string | null;
  only: "app" | "integration";
  window_id: string | null;
  kind: string | null;
  action: string | null;
  word: string | null;
  code: string | null;
  condition: string | null;
  preset_id: string | null;
  preset_name: string | null;
  text: string | null;
  summary: string | null;
  window: string | null;
}

export interface OwnershipEntry {
  epoch: number;
  owner: string;
  reason: string;
  at: string | null;
  by: string | null;
  detail?: string;
}

export interface CurtainsStatus {
  mode: "starting" | "standalone" | "shadow" | "owner";
  bridge: string;
  bridge_error: string | null;
  instance_id: string;
  session_id: string | null;
  app_version: string;
  runtime_version: string;
  fingerprint: string | null;
  core_fingerprint: string | null;
  acknowledged_owner: "integration" | "app" | "resume_pending" | null;
  commands_go_to: "app" | "integration";
  integration: {
    owner: string | null;
    state: string | null;
    epoch: number | null;
    integration_version: string | null;
    fingerprint: string | null;
    core_fingerprint: string | null;
    lease_until: string | null;
    changed_at: string | null;
    changed_by: string | null;
    config_revision: string | null;
    manager: string | null;
    moving: string[] | null;
    windows: number | null;
    executed: number | null;
    execute_errors: number | null;
    rejections: Record<string, number> | null;
    history: OwnershipEntry[] | null;
  };
  integration_status: Obj | null;
  epoch: number | null;
  lease_until: string | null;
  config_revision: string | null;
  integration_revision: string | null;
  running: boolean;
  moving: string[];
  counters: Record<string, number>;
  last_error: { at: string; code: string; detail: string } | null;
  notices: { code: string; text: string }[];
  ownership_log: { at: string; event: string; [key: string]: unknown }[];
  comparison: {
    running: boolean;
    started_at: string | null;
    seconds: number;
    window_seconds: number;
    decision_window_seconds: number;
    commands: { matched: number; mismatches: number; by_purpose: Record<string, number>; mismatch_by_purpose: Record<string, number>;
      pending: Record<string, number>; last_match_at: string | null; last_mismatch_at: string | null };
    decisions: { matched: number; differences: number };
    discrepancies: CoverDiscrepancy[];
    decision_differences: CurtainDecisionDifference[];
    recent: { app: CoverCommandBrief[]; integration: CoverCommandBrief[] };
  };
  readiness: {
    ready: boolean;
    checks: ReadinessCheck[];
    summary: { seconds: number; matched: number; command_matches: number; mismatches: number; decision_differences: number };
    thresholds: { min_shadow_seconds: number; min_decisions: number };
  };
  backups: { id: string; size: number }[];
  user: { is_admin: boolean };
  area_names: Record<string, string>;
}

export function curtainsStatus(): Promise<CurtainsStatus> {
  return getRooms<CurtainsStatus>("api/curtains");
}

export async function handover(): Promise<{ epoch: number; copy: { identical: boolean; differences: string[] } }> {
  return (await postRooms<{ result: { epoch: number; copy: { identical: boolean; differences: string[] } } }>(
    "api/curtains/handover", { confirm: true })).result;
}

/** mode "release": the App hands back its final state; "return": the integration takes over from its mirror
 * (refused with code "moving" while a cover is being moved, unless forced). */
export async function releaseControl(mode: "release" | "return", force = false): Promise<unknown> {
  return (await postRooms<{ result: unknown }>("api/curtains/release", mode === "return" ? { mode, force } : { mode })).result;
}

export interface ImportReview {
  writable: boolean;
  revision: string | null;
  incoming_revision: string | null;
  changes: string[];
  windows: number;
  missing_entities: string[];
  dropped_entities: string[];
  /** Windows the file would switch to Automatic (restored as Observing unless confirmed). */
  automatic_windows: { id: string | null; name: string | null }[];
  exported_at: string | null;
  same: boolean;
}

export function reviewImport(file: unknown): Promise<ImportReview> {
  return postRooms<ImportReview>("api/curtains/import/review", { file });
}

export function applyImport(file: unknown, revision: string | null, confirmAutomatic = false): Promise<{ applied: boolean; revision: string | null; observing_instead: string[] }> {
  return postRooms<{ applied: boolean; revision: string | null; observing_instead: string[] }>("api/curtains/import", { file, revision, confirm_automatic: confirmAutomatic });
}

/** Who controls the curtains now, in words (as the integration acknowledges it). */
export function ownerLabel(acknowledged: string | null | undefined): { app: boolean; label: string } {
  const app = acknowledged === "app" || acknowledged === "resume_pending";
  return { app, label: app ? "The Smart Lighting App" : "The integration" };
}

// ------------------------------------------------------------------ the model (smart_occupancy/curtains/model)
export type WindowMode = "off" | "observing" | "automatic";

export interface Caps {
  position: boolean;
  open: boolean;
  close: boolean;
  stop: boolean;
  known: boolean;
}

export interface Conflict {
  automation: string;
  name: string | null;
  entity_id: string;
  enabled: boolean;
  detail: string;
  [key: string]: unknown;
}

export interface CoverPreset {
  preset_id: string;
  name: string;
  position: number;
}

export interface LayerModel {
  layer_id: string;
  entity_id: string;
  kind: string;
  kind_word: string;
  label: string | null;
  name: string;
  entity_name: string;
  state: string | null;
  missing: boolean;
  available: boolean;
  moving: boolean;
  position: number | null;
  caps: Caps;
  members: string[];
  presets: CoverPreset[];
  desired: number | null;
  matches_desired: boolean | null;
  conflicts: Conflict[];
}

export interface PresetModel {
  preset_id: string;
  name: string;
  positions: Record<string, number | null>;
  matches: boolean;
}

export interface Obstruction {
  from_azimuth: number;
  to_azimuth: number;
  min_elevation: number;
}

export interface SolarSettings {
  exposure_width: number;
  min_elevation: number;
  obstructions: Obstruction[];
  max_elevation: number | null;
}

export interface LayerTarget {
  layer_id: string;
  state: "preset" | "no_action" | "missing" | "nothing";
  position: number | null;
  source: string | null;
  source_word: string;
  preset_id: string | null;
  preset_name: string | null;
  library: string | null;
  fallback: boolean;
}

export interface Effective {
  mode: string;
  state: "target" | "no_action" | "nothing";
  label: string;
  preset_id: string | null;
  positions: Record<string, number | null>;
  source: string | null;
  source_word: string;
  fallback: boolean;
  layers: LayerTarget[];
  problems: string[];
  text?: string;
}

export interface Decision {
  condition: string;
  condition_word: string;
  mode_word: string;
  base: string;
  base_word: string;
  preset_id: string | null;
  preset_name: string | null;
  target: Effective;
  source: string | null;
  source_word: string;
  text: string;
  evidence: string[];
  hold_release: string | null;
  next_check: string | null;
  opening_blocked: boolean;
  phase: string | null;
  direct_sun: Obj | null;
  thermal: Obj | null;
  lamp_bypassed: boolean;
  note: string | null;
}

export interface AutoStatus {
  action: string;
  word: string;
  code: string | null;
  text: string;
  at: string | null;
  [key: string]: unknown;
}

export interface Hold {
  source: "manual" | "preset" | string;
  created: string | null;
  policy: string;
  policy_word: string;
  phase?: string | null;
  entity_id?: string;
  name?: string;
  at?: string;
  from?: number | null;
  to?: number | null;
  by?: string;
  preset_id?: string;
}

export interface LogEntry {
  at: string | null;
  action: string;
  word: string;
  text: string;
  summary?: string;
  mode?: string;
  preset?: string | null;
  preset_name?: string | null;
  condition?: string | null;
  [key: string]: unknown;
}

export interface Transition {
  phase: string;
  word: string;
  at: string;
  time: string;
  approx: boolean;
  reason: string;
}

export interface Reading {
  role: string;
  entity_id: string | null;
  status: string;
  status_word: string;
  value: number | null;
  unit: string;
  detail: string;
  since: string | null;
  reported: string | null;
  name?: string;
  outdoor_hint?: boolean;
  in_use?: boolean;
  [key: string]: unknown;
}

export interface EnvModel {
  sun: { azimuth: number | null; elevation: number | null; sun_angle: number | null; offset: number | null; incidence: number | null;
    status: string; text: string; exposed: boolean; offset_via_sun_angle: number | null; calibration: string };
  readings: Record<string, Reading>;
  comfort: { low: number; high: number; top: number; source: string } | null;
  band: string | null;
  band_word: string | null;
  direct_sun: Obj | null;
  thermal: Obj | null;
  thermal_enabled: boolean;
  glare: { enabled: boolean; when: string; active: boolean };
  sun_evidence: string;
  /** App 0.8.0: the window's own sunlight sensors (ANY activates, ALL below for the delay releases). */
  multi_sun?: MultiSunModel;
}

export interface SunSensorReading {
  entity_id: string;
  name: string;
  enabled: boolean;
  status: string;
  status_word?: string;
  value: number | null;
  detail?: string;
}

export interface MultiSunResult {
  active: boolean;
  changed: string | null;
  triggered_by: string | null;
  since: string | null;
  above_on: string[];
  blocking: string[];
  invalid: string[];
  countdown_started: string | null;
  release_at: string | null;
  remaining_seconds: number | null;
  reason: string;
  release_confirmable: boolean;
}

export interface MultiSunModel {
  enabled: boolean;
  active_source: boolean;
  sensors: SunSensorReading[];
  result: MultiSunResult | null;
  levels: { on_lux: number; off_lux: number; release_minutes: number };
}

export interface MultiSunStep extends MultiSunResult {
  minutes: number;
}

export interface AutoModel {
  environment: EnvModel;
  next_evaluation: string | null;
  decision: Decision | null;
  status: AutoStatus | null;
  hold: Hold | null;
  sleep: { enabled: boolean; entity: string | null; state: string | null; active: boolean };
  lamp: { enabled: boolean; entity: string | null; state: string | null; preset_id: string | null; active: boolean; releasing_until: string | null };
  light: { entity: string | null; value: number | null; daylight: boolean | null; strong: boolean | null; fallback: string };
  morning_since: string | null;
  applied: Obj | null;
  defer: Obj | null;
  moves_today: number;
  last_move_at: string | null;
  log: LogEntry[];
  precedence: { key: string; word: string }[];
  values: Obj;
  schedule: { evening: { at: string | null; reason: string | null } | null; note: string | null; next: Transition[] };
  sources: Record<string, string>;
  warnings: string[];
}

export interface OperationItem {
  layer_id: string;
  entity_id: string;
  name: string;
  kind: string;
  target: number | null;
  service: string | null;
  status: string;
  detail: string;
  start_position: number | null;
  final_position: number | null;
}

export interface Operation {
  preset_id: string;
  preset_name: string;
  status: string;
  started: string | null;
  finished: string | null;
  items: OperationItem[];
  explanation: string;
  origin?: string;
  [key: string]: unknown;
}

export interface ManualChange {
  entity_id: string;
  name: string;
  at: string;
  from: number | null;
  to: number | null;
  source: "person" | "outside" | string;
  [key: string]: unknown;
}

export interface Problem {
  severity: "info" | "warning" | string;
  text: string;
}

export interface Exposure {
  status: string;
  offset: number | null;
  incidence: number | null;
  elevation: number | null;
  text: string;
  exposed: boolean;
}

export interface WindowModel {
  window_id: string;
  name: string;
  area_id: string | null;
  area_name: string | null;
  direction: string | null;
  bearing: number | null;
  effective_bearing: number | null;
  compass: string | null;
  mode: WindowMode;
  mode_word: string;
  solar_protection: boolean;
  privacy: boolean;
  sleep_protection: boolean;
  /** App 0.8.0 */
  multi_sun?: boolean;
  sun_sensors?: Array<{ entity_id: string; enabled: boolean }>;
  solar: SolarSettings;
  layers: LayerModel[];
  presets: PresetModel[];
  selected_preset: string | null;
  current: { status: string; preset_id: string | null; text: string; also?: string[] };
  conditions: Record<string, string | null>;
  cover_modes: Record<string, Record<string, string>>;
  layered: boolean;
  effective: Record<string, Effective>;
  overrides: Obj;
  usage: Record<string, string[]>;
  exposure: Exposure;
  manual: ManualChange | null;
  last: Operation | null;
  running: boolean;
  history: Operation[];
  problems: Problem[];
  auto: AutoModel;
  allowed_automations: string[];
}

export interface Rule {
  key: string;
  kind: "bool" | "int" | "float" | "time" | "choice" | "entity" | "days" | "set" | "dayplan" | string;
  default: unknown;
  group: string;
  label: string;
  hint: string;
  low: number | null;
  high: number | null;
  unit: string;
  choices: [string, string][];
  domains: string[];
  scopes: string[];
  step: number | null;
  confirm: boolean;
  entity_list: string | null;
}

export interface RulesModel {
  schema: Rule[];
  groups: [string, string, string][];
  global: Obj;
  rooms: Record<string, Obj>;
  defaults: Obj;
  weekdays: string[];
}

export interface Candidate {
  entity_id: string;
  name: string;
  area_id: string | null;
  area_name: string | null;
  kind: string;
  caps: Caps;
  available: boolean;
  position: number | null;
  assigned_to: string | null;
  members: string[];
}

export interface Proposal {
  key: string;
  name: string;
  area_id: string | null;
  area_name: string | null;
  covers: { entity_id: string; name: string; kind: string }[];
}

export interface ModeOption {
  preset_id: string;
  name: string;
  detail: string;
}

export interface TargetStatus {
  mode: WindowMode;
  mode_word: string;
  hold: boolean;
  condition: string | null;
  condition_word: string | null;
  active_mode: string | null;
  deferred: boolean;
}

export interface AssignmentRow {
  window_id: string;
  layer_id: string | null;
  preset_id: string;
  name: string;
  window_name: string;
  cover_name: string | null;
  room: string;
  area_id: string | null;
  type: string;
  type_word: string;
  options: ModeOption[];
  missing: boolean;
  effective: string;
  hidden: { word: string; cover?: string; label: string }[];
  problems: string[];
  conflicts: string[];
  status: TargetStatus;
  active: boolean;
}

export interface ModeInfo {
  key: string;
  word: string;
  icon: string;
  hint: string;
  advanced: boolean;
  count: number;
  rows: AssignmentRow[];
  others: Obj[];
  warnings: string[];
}

export interface MatrixCell {
  label: string;
  state: string;
  source: string | null;
  source_word?: string;
  text?: string;
  global?: string | null;
  override?: string | null;
  covers_assigned?: boolean;
  problems?: string[];
  inherited?: boolean;
  fallback?: boolean;
  value?: string | null;
}

export interface MatrixRow {
  kind: "room" | "window" | "cover";
  area_id?: string | null;
  window_id?: string;
  layer_id?: string | null;
  name: string;
  window_name?: string;
  mode?: WindowMode;
  type?: string;
  cells: Record<string, MatrixCell> | null;
  options: ModeOption[];
}

export interface ModesModel {
  modes: ModeInfo[];
  common: string[];
  advanced: string[];
  targets: { window_id: string; layer_id: string | null; name: string; room: string; type: string; type_word: string; options: ModeOption[];
    has_covers: boolean; status: TargetStatus }[];
  matrix: MatrixRow[];
  overrides: number;
  log: { at: string | null; action: string; word: string; window: string; summary: string; source_word?: string }[];
  policies: string[];
}

export interface ImpactChange {
  mode: string;
  word: string;
  before: string;
  after: string;
  targets_change: boolean;
}

export interface Impact {
  window_id: string;
  name: string;
  room: string;
  mode: WindowMode;
  changes: ImpactChange[];
  condition: string | null;
  condition_word: string | null;
  before: string | null;
  after: string | null;
  active: boolean;
  moves: number;
  items: PlanItem[];
  hold: boolean;
  status: string | null;
  blocked: string | null;
  conflicts: string[];
  needs_confirm: boolean;
  predicted?: string;
  predicted_word?: string;
}

/** The answer of modes/change, schedule/change and migration/change. */
export interface ChangeResult {
  changes: string[];
  impact: Impact[];
  needs_confirm: boolean;
  saved: boolean;
  transitions?: { before: Transition[]; after: Transition[] };
  global_modes?: ModesModel;
  schedule?: ScheduleModel;
  // migration/change
  pending?: boolean;
  items?: MigrationItem[];
  words?: Record<string, string>;
  missing?: string[];
}

export interface MigrationItem {
  id: string;
  kind: "conflict" | "mapping" | "inexact" | "morning" | "schedule" | string;
  title: string;
  detail: string;
  choices: string[];
  default: string | null;
  required: boolean;
  window?: string;
  [key: string]: unknown;
}

export interface HouseModel {
  phase: string;
  word: string;
  since: string | null;
  until: string | null;
  reason: string;
  waiting: string | null;
  next: Transition[];
  timeline: { phase: string; word: string; from: number; to: number }[];
  log: { at: string; text: string }[];
  sun_events: { sunset: string | null; dusk: string | null };
}

export interface ScheduleOverride {
  scope: "room" | "window";
  area_id?: string;
  window_id?: string;
  name: string;
  keys: { key: string; label: string; value: string }[];
}

export interface ScheduleModel {
  house: HouseModel;
  values: Obj;
  sources: Record<string, string>;
  keys: string[];
  sleep_keys: string[];
  lux_entity: string | null;
  overrides: ScheduleOverride[];
  implementation: string;
  warnings: string[];
  time_zone: string;
}

export interface EnvironmentSummary {
  roles: { role: string; word: string; candidates: Reading[]; chosen: string[] }[];
  global: Record<string, string | null>;
  status_words: Record<string, string>;
  band_words: Record<string, string>;
}

export interface CurtainsModel {
  version: string;
  can_edit: boolean;
  settings: { tolerance: number; verify_timeout: number; sun_angle_entity: string | null; azimuth_entity: string; elevation_entity: string };
  sun: { azimuth: number | null; elevation: number | null; sun_angle: number | null;
    calibration: { samples: number; span: number; status: string; sign: number | null; offset: number | null; residual: number | null; zero_azimuth: number | null; text: string };
    sources: { azimuth: string; elevation: string; sun_angle: string | null; sun_angle_exists: boolean } };
  rooms: { area_id: string | null; name: string; windows: string[] }[];
  windows: WindowModel[];
  areas: { area_id: string; name: string }[];
  candidates: Candidate[];
  proposals: Proposal[];
  automations: number;
  words: { kinds: Record<string, string>; conditions: Record<string, string>; modes: Record<string, string>; plan: Record<string, string>;
    results: Record<string, string>; overall: Record<string, string> };
  limits: { windows: number; layers: number; presets: number; obstructions: number; tolerance: [number, number]; verify_timeout: [number, number];
    exposure_width: [number, number]; elevation: [number, number]; max_elevation: [number, number] };
  compass: string[];
  kinds: string[];
  modes: string[];
  permitted_modes: string[];
  conditions: string[];
  rules: RulesModel;
  entities: Record<string, { entity_id: string; name: string }[]>;
  environment: EnvironmentSummary;
  time_zone: string;
  global_modes: ModesModel;
  schedule: ScheduleModel;
  migration: { pending: boolean; count: number; required: number; words: Record<string, string>; text: string };
  centralized: boolean;
}

// ------------------------------------------------------------------ command answers
export interface PlanItem {
  layer_id: string;
  entity_id: string;
  name: string;
  kind: string;
  target: number | null;
  current: number | null;
  state: string | null;
  decision: string;
  word: string;
  service: string | null;
  reason: string;
}

/** preview: what applying a preset would do now (nothing moves). */
export interface Plan {
  window_id: string;
  preset_id: string;
  preset_name: string;
  positions: Record<string, number | null>;
  mode: WindowMode;
  items: PlanItem[];
  moves: number;
  running: boolean;
}

/** auto/preview: what enabling Automatic would do now. */
export interface EnableReview {
  window_id: string;
  mode: WindowMode;
  decision: Decision | null;
  preset_id: string | null;
  preset_name: string | null;
  items: PlanItem[];
  moves: number;
  blockers: string[];
  warnings: string[];
  conflicts: Conflict[];
  precedence: { key: string; word: string }[];
  hold: Hold | null;
}

/** auto/simulate: what every affected window would decide with changed settings. */
export interface SimulationWindow {
  window_id: string;
  name: string;
  mode: WindowMode;
  current: Decision | null;
  current_preset_name?: string | null;
  predicted: Decision | null;
  preset_name: string | null;
  items?: PlanItem[];
  moves: number;
  changes: boolean;
  warnings?: string[];
}

export function loadModel(): Promise<CurtainsModel> {
  return curtains<CurtainsModel>(cmd("model"));
}
