// Shapes of the backend's read-only JSON API (see backend/smart_lighting/core.py).

export type CheckStatus = "ok" | "warning" | "error" | "info";

export interface Check {
  key: string;
  title: string;
  status: CheckStatus;
  detail: string;
}

export interface Connection {
  state: "connected" | "connecting" | "disconnected" | "auth_failed" | "not_configured" | string;
  connected: boolean;
  ha_version?: string | null;
  connected_since?: string | null;
  last_disconnect?: string | null;
  last_error?: string | null;
  connect_attempts?: number;
  reconnects?: number;
  latency_ms?: number | null;
  last_pong?: string | null;
  events_total?: number;
  events_per_minute?: number;
}

export interface HaInfo {
  location_name: string | null;
  version: string | null;
  state: string | null;
  time_zone: string | null;
  country: string | null;
  language: string | null;
  currency: string | null;
  unit_system: { length: string | null; temperature: string | null };
  safe_mode: boolean | null;
  recovery_mode: boolean | null;
  component_count: number;
  entity_count: number | null;
  area_count: number | null;
}

export interface IntegrationEntry {
  title: string | null;
  state: string | null;
  disabled_by: string | null;
  reason: string | null;
}

export interface Integration {
  domain: string;
  status: "loaded" | "installed" | "not_running" | "not_installed" | "unknown" | string;
  detected: boolean;
  version: string | null;
  name: string | null;
  entries: IntegrationEntry[];
  entity_count: number;
  entities_by_domain: Record<string, number>;
  checked: boolean;
  errors: string[];
}

export interface EntitySummary {
  entity_id: string;
  state: string | null;
  name: string;
  unit: string | null;
  device_class?: string | null;
  icon?: string | null;
  last_changed: string | null;
}

export interface ModuleInfo {
  key: string;
  title: string;
  icon: string;
  kind: "foundation" | "feature";
  status: "available" | "gui" | "pending";
  summary: string;
  handled_by: string | null;
  controlled_by: "integration" | "app" | null;
  gui_owner?: "integration" | "app" | null;
  config_source?: "integration" | "app" | null;
  automation_owner?: "integration" | "app" | null;
  scope?: string | null;
}

export interface MigrationItem {
  key: string;
  title: string;
  status: "done" | "pending" | "available" | "gui";
  phase: string | null;
  controlled_by: "integration" | "app" | null;
  gui_owner?: "integration" | "app" | null;
  config_source?: "integration" | "app" | null;
  automation_owner?: "integration" | "app" | null;
}

export interface Migration {
  current_phase: string;
  foundation_complete: boolean;
  modules_total: number;
  modules_migrated: number;
  interfaces_migrated?: number;
  percent: number;
  interface_percent?: number;
  automatic_migration: boolean;
  controlled_by_integration: number;
  controlled_by_app: number;
  items: MigrationItem[];
}

export interface Safety {
  read_only: boolean;
  configuration_writes?: boolean;
  write_scope?: string;
  config_writes_sent?: number;
  recent_config_writes?: Array<{ type: string; time: string; area_id: string | null; result?: string }>;
  allowed_write_commands?: string[];
  device_control: boolean;
  automation_engine: boolean;
  automatic_migration: boolean;
  modifies_integration: boolean;
  service_calls_sent: number;
  commands_sent: Record<string, number>;
  allowed_commands: string[];
}

export interface SupervisorInfo {
  configured: boolean;
  available: boolean;
  app: Record<string, unknown>;
  system: Record<string, unknown>;
  error: string | null;
  checked_at: string | null;
}

export interface AppInfo {
  name: string;
  version: string;
  phase: string;
  started_at: string;
  uptime_s: number;
  health: CheckStatus;
  dev_mode: boolean;
}

export interface Overview {
  app: AppInfo;
  checks: Check[];
  home_assistant: { connection: Connection; info: HaInfo | null };
  integration: Integration | null;
  integration_entities: EntitySummary[];
  supervisor: SupervisorInfo | null;
  modules: ModuleInfo[];
  migration: Migration;
  safety: Safety;
  rooms?: Record<string, unknown> | null;
  /** Phase A3.2: the occupancy engine (owner as the integration acknowledges it). */
  engine?: EngineBrief | null;
  /** Phase A4 / A5: the lighting, curtain and prediction runtimes (owner as the integration acknowledges it). */
  lighting?: EngineBrief | null;
  curtains?: EngineBrief | null;
  predictions?: EngineBrief | null;
  generated_at: string;
}

export interface EngineBrief {
  mode: string;
  owner: string;
  bridge: string;
}

export interface LogEntry {
  time: string;
  level: string;
  logger: string;
  message: string;
}

export interface Diagnostics extends Overview {
  settings: Record<string, unknown>;
  runtime: Record<string, string>;
  log: LogEntry[];
}

export type LiveMessage =
  | { type: "overview"; overview: Overview }
  | {
      type: "status";
      app: AppInfo;
      checks: Check[];
      connection: Connection;
      info: HaInfo | null;
    }
  | { type: "entities"; changed: EntitySummary[] }
  | { type: "states"; changed: LiveState[] }
  | { type: "rooms"; status: Record<string, unknown> | null }
  | { type: "registries" }
  | { type: "floorplan"; revision: string }
  | { type: "engine"; mode: string; owner: string; bridge: string }
  | { type: "lighting"; what: string; mode: string; owner: string; bridge: string }
  | { type: "curtains"; what: string; mode: string; owner: string; bridge: string }
  | { type: "predictions"; what: string; mode: string; owner: string; bridge: string }
  | { type: "pong" };

export interface LiveState {
  entity_id: string;
  state: string | null;
  last_changed: string | null;
  last_updated: string | null;
  availability: string;
  /** A few display-only attributes (brightness, position, the integration's room state). */
  attrs?: Record<string, unknown>;
}
