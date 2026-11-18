// Engine migration (Phase A3.2): types and pure helpers of the Presence → Engine page.
// Everything shown comes from the App's backend (/api/engine): the integration's acknowledged owner,
// the App's engine and the shadow comparison. Nothing here decides anything.
import type { OccState } from "./model";

export type CheckLevel = "ok" | "warning" | "error" | "info";

export interface EngineCheck {
  key: string;
  status: CheckLevel;
  title: string;
  detail: string;
}

export interface Brief {
  state: OccState;
  occupied: boolean | null;
  reason_code: string;
  reason?: string;
  vacancy_at?: string | null;
  state_since?: string | null;
}

export interface RoomCompare {
  area_id: string;
  agree: boolean | null;
  status: "agree" | "disagree" | "settling" | "unknown";
  open_for_s: number | null;
  app: Brief | null;
  integration: Brief | null;
  transitions: number;
  matched: number;
  timing_differences: number;
  timing_max_ms: number;
  disagreements: number;
  deadline_differences: number;
  last_disagreement_at: string | null;
}

export interface Totals {
  transitions: number;
  matched: number;
  timing_differences: number;
  disagreements: number;
  deadline_differences: number;
  timing_max_ms: number;
  deadline_max_s: number;
}

export interface HistoryEntry {
  epoch: number;
  owner: "integration" | "app";
  reason: string;
  at: string;
  by?: string | null;
  detail?: string;
}

export interface IntegrationEngineStatus {
  owner: string;
  state: string;
  epoch: number;
  lease_until: string | null;
  owner_connected: boolean;
  changed_at: string | null;
  last_accepted_at: string | null;
  accepted_total: number;
  rejections: Record<string, number>;
  last_rejection: { at: string; code: string; detail: string } | null;
  published_by: Record<string, string>;
  history: HistoryEntry[];
  sessions: Array<{ session_id: string; instance_id: string; app_version: string; owner: boolean; opened_at: string }>;
}

export interface EngineEvent {
  at: string;
  kind: string;
  entity_id?: string;
  state?: string | null;
  zones?: string[];
  source?: string;
  signals?: string[];
}

export interface EngineRecord {
  kind: "disagreement" | "timing" | "deadline";
  area_id: string;
  opened_at: string;
  closed_at?: string;
  duration_s?: number;
  delta_s?: number;
  app: Brief | null;
  integration: Brief | null;
  app_last?: Brief | null;
  integration_last?: Brief | null;
  events: EngineEvent[];
}

export interface EngineStatus {
  mode: "starting" | "standalone" | "shadow" | "authoritative";
  acknowledged_owner: string;
  epoch: number | null;
  lease_until: string | null;
  bridge: string;
  bridge_error: string | null;
  instance: string;
  session: string | null;
  versions: Record<string, string | number | null>;
  config: { app_revision: string | null; integration_revision: string | null; in_sync: boolean };
  self_test: { scenarios: number; transitions: number; passed: boolean; failed: Array<{ name: string }> } | null;
  rooms: number;
  last_publish_at: string | null;
  last_publish_error: { at: string; code: string } | null;
  counters: Record<string, number>;
  notices: Array<{ code: string; text: string }>;
  app_log: Array<{ at: string; event: string; reason?: string; epoch?: number }>;
  comparison: {
    running: boolean;
    since: string | null;
    seconds: number;
    tolerance_s: number;
    totals: Totals;
    open_disagreements: string[];
    last_disagreement_at: string | null;
    rooms: RoomCompare[];
  };
  readiness: { ready: boolean; checks: EngineCheck[]; thresholds: { min_shadow_seconds: number; min_transitions: number } };
  integration_status: IntegrationEngineStatus | null;
  area_names: Record<string, string>;
  now: string;
}

/** The owner as the integration acknowledges it (never the App's wish). */
export function ownerInfo(state: string | null | undefined): { label: string; tone: CheckLevel | "neutral"; app: boolean } {
  switch (state) {
    case "app":
      return { label: "Smart Lighting App", tone: "ok", app: true };
    case "resume_pending":
      return { label: "Smart Lighting App (reconnecting after a restart)", tone: "warning", app: true };
    case "integration":
      return { label: "Smart Occupancy integration", tone: "info", app: false };
    default:
      return { label: "Unknown (the integration cannot be reached)", tone: "neutral", app: false };
  }
}

export function modeLabel(mode: EngineStatus["mode"]): string {
  return {
    starting: "Starting",
    standalone: "Standalone (no comparison)",
    shadow: "Shadow mode: compared, not published",
    authoritative: "Publishing occupancy",
  }[mode];
}

const REASONS: Record<string, string> = {
  handover: "Reviewed handover",
  released: "Control returned to the integration",
  lease_expired: "The App stopped answering (lease expired)",
  app_restarted: "The App restarted",
  not_resumed: "The App did not reconnect after the restart",
  resumed: "The same App process reconnected",
  revoked: "The App could not prove its lease",
};

export function reasonText(reason: string): string {
  return REASONS[reason] ?? reason;
}

/** Seconds between two ISO times (null when unknown). */
export function secondsBetween(from: string | null | undefined, to: string | null | undefined): number | null {
  if (!from || !to) return null;
  const a = Date.parse(from);
  const b = Date.parse(to);
  return Number.isNaN(a) || Number.isNaN(b) ? null : Math.round((b - a) / 1000);
}

/** Share of compared transitions the App matched, in percent (null before the first transition). */
export function matchedPercent(t: Totals): number | null {
  return t.transitions ? Math.round((100 * t.matched) / t.transitions) : null;
}

export function recordTitle(r: EngineRecord): string {
  if (r.kind === "deadline") return `Vacancy deadlines differ by ${r.delta_s} s`;
  if (r.kind === "timing") return `Followed ${r.duration_s} s later (within the tolerance)`;
  return `Disagreement for ${r.duration_s ?? "?"} s`;
}

export function eventText(e: EngineEvent, name: (id: string) => string): string {
  switch (e.kind) {
    case "state":
      return `${name(e.entity_id ?? "")} → ${e.state ?? "removed"}`;
    case "zone_hold":
      return e.zones?.length ? `Zones holding the room: ${e.zones.join(", ")}` : "No zone holds the room";
    case "preliminary":
      return e.signals?.length ? `Possible occupancy lighting signals: ${e.signals.map(name).join(", ")}` : "No possible occupancy lighting signals";
    case "activity":
      return `Activity: ${e.source}`;
    default:
      return e.kind;
  }
}
