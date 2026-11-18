// Phase A4: the lighting editor API. One command API (the integration's own lighting websocket commands,
// same payloads and answers as its dashboard cards), wherever the configuration lives: the App backend forwards
// them to the integration while it controls lighting and runs the same handlers itself after the handover.
import { ApiError, getRooms, postRooms } from "../rooms/model";

export { ApiError };

/** One lighting editor command, e.g. lighting("smart_occupancy/lighting/editor", { area_id }). */
export async function lighting<T = unknown>(type: string, params: Record<string, unknown> = {}): Promise<T> {
  const reply = await postRooms<{ result: T; handled_by: "app" | "integration" }>("api/lighting/command", { type, ...params });
  return reply.result;
}

/** The short command name: lighting(cmd("room/activate"), …). */
export function cmd(name: string): string {
  return `smart_occupancy/lighting/${name}`;
}

export interface ReadinessCheck {
  key: string;
  status: "ok" | "error" | "warning";
  title: string;
  detail: string;
}

export interface CommandBrief {
  at: string | null;
  purpose: string | null;
  area_id: string | null;
  service: string;
  entities: string[];
  data: Record<string, unknown>;
  error?: string | null;
}

export interface Discrepancy {
  at: string | null;
  only: "app" | "integration";
  purpose: string;
  area_id: string | null;
  command: CommandBrief;
  other_side: CommandBrief[];
}

export interface DecisionDifference {
  at: string | null;
  only: "app" | "integration";
  subject: string;
  kind: string;
  trigger: string;
  status: string;
  target: string | null;
  action: string;
  explanation: string;
}

export interface OwnershipEntry {
  epoch: number;
  owner: string;
  reason: string;
  at: string | null;
  by: string | null;
  detail?: string;
}

export interface LightingStatus {
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
  epoch: number | null;
  lease_until: string | null;
  config_revision: string | null;
  integration_revision: string | null;
  running: boolean;
  counters: Record<string, number>;
  last_error: { at: string; code: string; detail: string } | null;
  notices: { code: string; text: string }[];
  ownership_log: { at: string; event: string; [key: string]: unknown }[];
  integration: Record<string, unknown>;
  integration_status: {
    owner: string;
    state: string;
    epoch: number;
    controller: string;
    integration_version: string;
    fingerprint: string | null;
    executed: number;
    execute_errors: number;
    last_execute: Record<string, unknown> | null;
    lease_until: string | null;
    owner_connected: boolean;
    changed_at: string | null;
    mirror_at: string | null;
    rejections: Record<string, number>;
    history: OwnershipEntry[];
  } | null;
  comparison: {
    running: boolean;
    started_at: string | null;
    seconds: number;
    window_seconds: number;
    commands: { matched: number; mismatches: number; by_purpose: Record<string, number>; mismatch_by_purpose: Record<string, number>;
      pending: Record<string, number>; last_match_at: string | null; last_mismatch_at: string | null };
    decisions: { matched: number; differences: number };
    discrepancies: Discrepancy[];
    decision_differences: DecisionDifference[];
    recent: { app: CommandBrief[]; integration: CommandBrief[] };
  };
  readiness: {
    ready: boolean;
    checks: ReadinessCheck[];
    summary: { seconds: number; matched: number; mismatches: number; decision_differences: number };
    thresholds: { min_shadow_seconds: number; min_commands: number };
  };
  backups: { id: string; size: number }[];
  user: { is_admin: boolean };
  area_names: Record<string, string>;
}

export function lightingStatus(): Promise<LightingStatus> {
  return getRooms<LightingStatus>("api/lighting");
}

export async function handover(): Promise<{ epoch: number; copy: { identical: boolean; differences: string[] } }> {
  return (await postRooms<{ result: { epoch: number; copy: { identical: boolean; differences: string[] } } }>(
    "api/lighting/handover", { confirm: true })).result;
}

/** mode "release": the App hands back its final state; "return": the integration takes over from its mirror. */
export async function releaseControl(mode: "release" | "return"): Promise<unknown> {
  return (await postRooms<{ result: unknown }>("api/lighting/release", { mode })).result;
}

/** Who controls lighting now, in words (as the integration acknowledges it). */
export function ownerLabel(acknowledged: string | null | undefined): { app: boolean; label: string } {
  const app = acknowledged === "app" || acknowledged === "resume_pending";
  return { app, label: app ? "The Smart Lighting App" : "The integration" };
}
