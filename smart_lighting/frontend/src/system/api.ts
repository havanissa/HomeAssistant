// Phase A6: Settings → Migration & System and Settings → Setup. Shapes of /api/system, /api/migration and
// /api/setup, and the requests of the wizard. Every owner shown comes from the companion's records (or the
// 0.9.0 integration's bridges while that is still installed), never from what the App wished for.
import { getRooms, postRooms } from "../rooms/model";

export type OwnerState = "app" | "integration" | "none" | "app_away" | string;
export type Tone = "ok" | "warning" | "error" | "info" | "neutral" | "pending";

export interface CompanionBrief {
  state: "unknown" | "probing" | "present" | "absent" | "incompatible" | "error" | string;
  error: string | null;
  session: string | null;
  paired: boolean;
  trusted: boolean;
  token_stored: boolean;
  companion_version: string | null;
  protocol: number | null;
  ha_version: string | null;
  ownership: Record<string, OwnershipRecord>;
  legacy: { present: boolean; retired: unknown; options_revision: string | null; rooms: number } | null;
  entities: number;
  counters: Record<string, number>;
  log: Array<{ at: string; event: string; [key: string]: unknown }>;
}

export interface OwnershipRecord {
  owner: string;
  state: OwnerState;
  epoch: number;
  connected: boolean;
  lease_valid: boolean;
  lease_until: string | null;
  executed: number;
  execute_errors: number;
  published_at: string | null;
  rejections: Record<string, number>;
  history: Array<{ epoch: number; owner: string; reason: string; at: string; by?: string | null; detail?: string }>;
}

export interface ConfigStatus {
  authoritative: boolean;
  staged: boolean;
  authority: string | null;
  revision: string | null;
  updated_at: string | null;
  source: string | null;
  rooms: number;
  enabled_rooms: number;
  backups: number;
  notices: Array<{ code: string; text: string }>;
}

export interface CompanionModeStatus {
  domain: string;
  state: string;
  owner: boolean;
  auto_claim: boolean;
  error: string | null;
  epoch: number | null;
  lease_until: string | null;
  companion: string;
  paired: boolean;
  trusted: boolean;
  record: Partial<OwnershipRecord>;
  counters: Record<string, number>;
  last_publish_at: string | null;
  last_publish_error: string | null;
}

export interface RuntimeStatus {
  mode: string;
  bridge: string;
  bridge_error: string | null;
  owner: OwnerState;
  companion: CompanionModeStatus | null;
  running: boolean;
  counters: Record<string, number>;
}

export interface SystemStatus {
  app: { version: string; phase: string };
  companion: CompanionBrief | null;
  config: ConfigStatus;
  config_authority: "app" | "integration";
  core_host: {
    authoritative: boolean; settled: boolean; snapshot_at: string | null; status: string | null; problems: number;
    rooms: number; published_at: string | null; publish_error: string | null;
  } | null;
  owners: Record<string, OwnerState>;
  runtimes: Record<string, RuntimeStatus>;
  integration: { status: string; detected: boolean; version: string | null } | null;
  migration: MigrationStatus | null;
  generated_at: string;
}

export interface Check {
  key: string;
  status: "ok" | "warning" | "error" | "info";
  title: string;
  detail: string;
}

export interface MigrationStep {
  key: string;
  status: "pending" | "done" | "blocked" | "reviewed" | "partial" | string;
  at: string | null;
  result: Record<string, unknown> | null;
}

export interface MigrationStatus {
  mode: "companion" | "legacy" | "none" | "unknown";
  started_at: string | null;
  completed_at: string | null;
  aborted: { at: string; by: string | null; actions: string[] } | null;
  steps: MigrationStep[];
  owners: Record<string, { owner: string; source: string; app_mode?: string; epoch?: number | null }>;
  config: ConfigStatus;
  companion: CompanionBrief | null;
}

export interface SetupStep {
  key: string;
  title: string;
  done: boolean;
  optional?: boolean;
  detail: string;
  route?: string;
}

export interface SetupStatus {
  legacy_installed: boolean;
  config: ConfigStatus;
  steps: SetupStep[];
  ready: boolean;
  companion: CompanionBrief | null;
}

export const STEP_TITLES: Record<string, string> = {
  inspect: "Inspect the system",
  backup: "Back everything up",
  validate: "Validate the App",
  transfer_config: "Transfer the configuration",
  transfer_control: "Transfer control",
  activate: "Activate the companion",
  verify: "Verify",
  retire: "Retire the legacy integration",
};

export const DOMAIN_TITLES: Record<string, string> = {
  occupancy: "Occupancy", lighting: "Lighting", curtains: "Curtains", forecast: "Predictions",
};

export function ownerText(state: OwnerState | undefined): { label: string; tone: Tone } {
  switch (state) {
    case "app":
      return { label: "The App", tone: "ok" };
    case "app_away":
      return { label: "The App (away)", tone: "warning" };
    case "integration":
      return { label: "Integration 0.9.0", tone: "info" };
    case "none":
      return { label: "Nobody", tone: "warning" };
    default:
      return { label: "Unknown", tone: "neutral" };
  }
}

export function companionText(c: CompanionBrief | null | undefined): { label: string; tone: Tone } {
  if (!c) return { label: "Not checked", tone: "neutral" };
  if (c.state === "present") return c.trusted ? { label: "Connected · paired", tone: "ok" } : { label: "Connected · not paired", tone: "warning" };
  if (c.state === "absent") return { label: "Not installed", tone: "info" };
  if (c.state === "incompatible") return { label: "Incompatible", tone: "error" };
  if (c.state === "error") return { label: "Error", tone: "error" };
  return { label: "Connecting…", tone: "neutral" };
}

export function stepTone(status: string): Tone {
  switch (status) {
    case "done":
      return "ok";
    case "blocked":
      return "error";
    case "reviewed":
    case "partial":
      return "warning";
    default:
      return "pending";
  }
}

/** The step the wizard should offer next (the first one not done), or null when every step is done. */
export function nextStep(steps: MigrationStep[]): string | null {
  for (const s of steps) if (s.status !== "done") return s.key;
  return null;
}

export const getSystem = () => getRooms<SystemStatus>("api/system");
export const getMigration = () => getRooms<MigrationStatus>("api/migration");
export const getSetup = () => getRooms<SetupStatus>("api/setup");
export const runStep = (step: string, body: Record<string, unknown> = {}) =>
  postRooms<{ result: Record<string, unknown>; status: MigrationStatus }>(`api/migration/${step}`, body);
export const pairCompanion = () => postRooms<{ result: unknown }>("api/system/pair", {});
export const claimDomain = (domain: string) => postRooms<{ result: unknown }>("api/system/claim", { domain });
export const releaseDomain = (domain: string) => postRooms<{ result: unknown }>("api/system/release", { domain });
export const revokeApp = (domain: string | null) => postRooms<{ result: unknown }>("api/system/revoke", { domain });
export const startFresh = () => postRooms<{ result: unknown }>("api/setup/start", {});
