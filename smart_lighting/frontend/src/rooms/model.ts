// Rooms (Phase A2): shapes of the backend's rooms API, requests, and the
// editor's draft logic. The authoritative configuration lives in the
// existing integration; a draft is only the editor's unsaved copy.
import { apiUrl } from "../store";

export type Availability = "ok" | "unknown" | "unavailable" | "device_offline" | "not_provided" | "disabled" | "missing";
export type RoomStatus = "configured" | "not_configured" | "disabled" | "problem";
export type WarningLevel = "error" | "warning" | "info";

export interface SensorConfig {
  entity_id: string;
  kind: string;
  role: string;
  inverted?: boolean;
  enabled?: boolean;
}

export interface RoomConfig {
  enabled: boolean;
  sensors: SensorConfig[];
  lights: string[];
  reference_occupancy_entity: string | null;
  adjacent_rooms: string[];
  [key: string]: unknown;
}

export interface RoomWarning {
  code: string;
  level: WarningLevel;
  title: string;
  detail: string;
  action?: string;
  entity_id?: string | null;
  source?: string;
}

export interface RoomsStatus {
  api: "unknown" | "ok" | "unsupported" | "unavailable" | "error";
  error: string | null;
  loaded: boolean;
  writable: boolean;
  integration_version: string | null;
  integration_state: string | null;
  revision: string | null;
  loaded_at: string | null;
  backups: boolean;
}

export interface RoomSummary {
  area_id: string;
  name: string;
  icon: string | null;
  floor_id: string | null;
  area_exists: boolean;
  configured: boolean;
  enabled: boolean;
  status: RoomStatus;
  integration_status: string | null;
  occupancy: { state: string | null; occupied: boolean | null; reason: string | null } | null;
  sensors: number;
  lights: number;
  sensor_ids: string[];
  light_ids: string[];
  unassigned_candidates: number;
  devices: { online: number; offline: number; partial: number; unavailable_entities: number; missing_entities: number };
  automation: {
    owner: "integration";
    running: boolean;
    lighting: "automatic" | "observing" | null;
    switches_active: number;
    curtains_automatic: number;
  };
  warnings: RoomWarning[];
}

export interface RoomsOverview {
  status: RoomsStatus;
  rooms: RoomSummary[];
  generated_at: string;
}

export interface EntityInfo {
  entity_id: string;
  name: string;
  domain: string;
  category: string;
  category_label: string;
  category_confidence: "high" | "medium" | "low" | "user";
  category_reason: string;
  suggested_kind: string;
  device_class: string | null;
  unit: string | null;
  icon: string | null;
  state: string | null;
  last_changed: string | null;
  last_updated: string | null;
  availability: Availability;
  capabilities: string[];
  platform: string | null;
  registered: boolean;
  hidden: boolean;
  entity_category: string | null;
  device_id: string | null;
  area_id: string | null;
  area: string | null;
  sensor_rooms: string[];
  light_rooms: string[];
  own: boolean;
}

export interface DeviceInfo {
  id: string;
  name: string;
  manufacturer: string | null;
  model: string | null;
  model_id: string | null;
  sw_version: string | null;
  hw_version: string | null;
  area_id: string | null;
  area: string | null;
  disabled: boolean;
  identity: string[];
  entity_ids: string[];
  status: "online" | "offline" | "partial" | "no_entities";
}

export interface Problem {
  type: string;
  title: string;
  classification: string;
  severity: string;
  category: string;
  explanation: string;
  action: string;
  entity_id?: string;
  name?: string;
  key?: string;
}

export interface OccupancyInfo {
  state: string | null;
  occupied: boolean | null;
  reason: string | null;
  reason_code?: string;
  confidence?: string;
  usable?: boolean;
  vacancy_at?: string | null;
  last_activity?: string | null;
  last_activity_source?: string | null;
  signals?: Array<{ entity_id: string; value: string; status: string | null; condition: string; health: string; label: string; available: boolean }>;
}

export interface LightingInfo {
  automatic: boolean;
  control_enabled: boolean;
  activated_at: string | null;
  managed: string[];
  controlled?: string[];
  discovered?: string[];
  excluded?: Record<string, string>;
  auto_discover?: boolean;
  relays: string[];
  zones: Array<{ id: string; name: string; enabled: boolean; sensors: string[] }>;
  switches: Array<{ id: string; name: string; mode: string; device_id: string | null; event_entity: string | null }>;
  manual_switches?: string[];
  priority_inputs: Array<{ id: string; name: string; entity_id: string | null; enabled: boolean }>;
  vacancy_action?: string;
}

export interface CurtainWindow {
  window_id: string;
  name: string;
  mode: string;
  covers: string[];
}

export interface KindInfo {
  value: string;
  key?: string;
  label: string;
  domains: string[] | null;
  default_role: string;
}

export interface Schema {
  kinds: KindInfo[];
  roles: Array<{ value: string; label: string }>;
  reference_domains?: string[];
  limits?: Record<string, number>;
}

export interface Workspace {
  status: RoomsStatus;
  summary: RoomSummary;
  area: { id: string; name: string; icon: string | null; exists: boolean; aliases: string[] };
  room: {
    configured: boolean;
    config: RoomConfig;
    revision: string;
    integration_status: string | null;
    occupancy: OccupancyInfo | null;
    problems: Problem[];
    lighting: LightingInfo | null;
    curtains: CurtainWindow[];
  };
  entities: Record<string, EntityInfo>;
  devices: Record<string, DeviceInfo>;
  switch_devices: string[];
  other_rooms: Record<string, { name: string; enabled: boolean; sensors: string[]; lights: string[] }>;
  areas: Array<{ id: string; name: string }>;
  schema: Schema | null;
  timing: Record<string, number> | null;
  categories: Record<string, { label: string; icon: string; group: string }>;
  generated_at: string;
}

export interface Candidate {
  entity_id: string;
  name: string;
  domain: string;
  category: string;
  category_label: string;
  suggested_kind: string;
  area_id: string | null;
  area: string | null;
  state: string | null;
  availability: Availability;
  device_id: string | null;
  sensor_rooms: string[];
  light_rooms: string[];
}

export interface Change {
  type: string;
  entity_id?: string;
  area_id?: string;
  kind?: string;
  role?: string;
  old?: unknown;
  new?: unknown;
  inverted?: boolean;
  enabled?: boolean;
}

export interface Impact {
  code: string;
  level: "confirm" | "info";
  title: string;
  detail: string;
  area_id?: string;
}

export interface Review {
  area_id: string;
  room_revision: string;
  revision: string;
  stale: boolean;
  errors: Array<{ code: string; message: string; entity_id?: string }>;
  changes: Change[];
  problems_after: Problem[];
  problems_new: Problem[];
  problems_resolved: Problem[];
  impacts: Impact[];
  requires_confirmation: string[];
  apply: "none" | "live" | "reload";
  writable: boolean;
  entities: Record<string, EntityInfo>;
  conflicts: Array<{ code: string; entity_id: string; rooms: string[] }>;
  attention: Array<{ entity_id: string; name: string; availability: Availability }>;
}

export interface RestoreReview {
  revision: string;
  stale: boolean;
  errors: Array<{ code: string; message: string; area_id?: string }>;
  rooms: Array<{ area_id: string; name: string | null; changes: Change[]; area_exists: boolean; missing_entities: string[] }>;
  impacts: Impact[];
  requires_confirmation: string[];
  writable: boolean;
}

export interface BackupInfo {
  id: string;
  created_at: string | null;
  reason: string | null;
  rooms: number;
  revision: string | null;
}

export interface Backup {
  format: string;
  version: number;
  created_at: string;
  reason?: string;
  rooms: Record<string, RoomConfig>;
  revision?: string | null;
  integration_version?: string | null;
}

// ------------------------------------------------------------------ requests
export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function parse<T>(response: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) {
    const b = (body ?? {}) as { error?: string; code?: string };
    throw new ApiError(b.error ?? `HTTP ${response.status}`, b.code ?? "http_error", response.status);
  }
  return body as T;
}

export async function getRooms<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), { cache: "no-store", credentials: "same-origin" });
  } catch (err) {
    throw new ApiError(`The App cannot be reached (${String(err)})`, "network", 0);
  }
  return parse<T>(response);
}

/** Changing requests: JSON body plus the header the backend requires (no cross-site posts). */
export async function postRooms<T>(path: string, body: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Smart-Lighting": "1" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError(
      `The App could not be reached (${String(err)}). If you were saving, the change may or may not have been stored: reload the room to check.`,
      "network",
      0,
    );
  }
  return parse<T>(response);
}

export function downloadJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 1)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// -------------------------------------------------------------------- draft
export const EDITABLE: Array<keyof RoomConfig> = ["enabled", "sensors", "lights", "reference_occupancy_entity", "adjacent_rooms"];

/** The editable part of a stored room, normalized (what the editor works on). */
export function editable(config: RoomConfig): RoomConfig {
  return {
    enabled: Boolean(config.enabled),
    sensors: (config.sensors ?? []).map((s) => ({
      entity_id: s.entity_id,
      kind: s.kind,
      role: s.role,
      inverted: Boolean(s.inverted),
      enabled: s.enabled !== false,
    })),
    lights: [...(config.lights ?? [])],
    reference_occupancy_entity: config.reference_occupancy_entity ?? null,
    adjacent_rooms: [...(config.adjacent_rooms ?? [])],
  };
}

/** What the editor sends: only the editable fields (the integration keeps everything else). */
export function payload(draft: RoomConfig): Record<string, unknown> {
  return {
    enabled: draft.enabled,
    sensors: draft.sensors.map((s) => ({
      entity_id: s.entity_id,
      kind: s.kind,
      role: s.role,
      inverted: Boolean(s.inverted),
      enabled: s.enabled !== false,
    })),
    lights: draft.lights,
    reference_occupancy_entity: draft.reference_occupancy_entity || null,
    adjacent_rooms: draft.adjacent_rooms,
  };
}

/** Client-side change list (same meaning as the integration's review; the review is authoritative). */
export function diff(base: RoomConfig, draft: RoomConfig, configured: boolean): Change[] {
  const out: Change[] = [];
  if (!configured && (draft.enabled || draft.sensors.length || draft.lights.length)) out.push({ type: "room_added" });
  if (base.enabled !== draft.enabled && (configured || draft.enabled)) out.push({ type: draft.enabled ? "room_enabled" : "room_disabled" });
  const before = new Map(base.sensors.map((s) => [s.entity_id, s]));
  const after = new Map(draft.sensors.map((s) => [s.entity_id, s]));
  for (const [id, s] of after) if (!before.has(id)) out.push({ type: "sensor_added", entity_id: id, kind: s.kind, role: s.role });
  for (const [id, s] of before) if (!after.has(id)) out.push({ type: "sensor_removed", entity_id: id, kind: s.kind, role: s.role });
  for (const [id, s] of after) {
    const o = before.get(id);
    if (!o) continue;
    if (o.kind !== s.kind) out.push({ type: "sensor_kind", entity_id: id, old: o.kind, new: s.kind });
    if (o.role !== s.role) out.push({ type: "sensor_role", entity_id: id, old: o.role, new: s.role });
    if (Boolean(o.inverted) !== Boolean(s.inverted)) out.push({ type: "sensor_inverted", entity_id: id, old: Boolean(o.inverted), new: Boolean(s.inverted) });
    if ((o.enabled !== false) !== (s.enabled !== false)) out.push({ type: "sensor_enabled", entity_id: id, old: o.enabled !== false, new: s.enabled !== false });
  }
  for (const l of draft.lights) if (!base.lights.includes(l)) out.push({ type: "light_added", entity_id: l });
  for (const l of base.lights) if (!draft.lights.includes(l)) out.push({ type: "light_removed", entity_id: l });
  if ((base.reference_occupancy_entity ?? null) !== (draft.reference_occupancy_entity ?? null))
    out.push({ type: "reference_changed", old: base.reference_occupancy_entity, new: draft.reference_occupancy_entity });
  for (const a of draft.adjacent_rooms) if (!base.adjacent_rooms.includes(a)) out.push({ type: "adjacent_added", area_id: a });
  for (const a of base.adjacent_rooms) if (!draft.adjacent_rooms.includes(a)) out.push({ type: "adjacent_removed", area_id: a });
  return out;
}

export function kindDefaultRole(schema: Schema | null, kind: string): string {
  return schema?.kinds.find((k) => k.value === kind)?.default_role ?? (kind === "pir" || kind === "door" ? "trigger" : kind === "other" ? "ignore" : "maintain");
}

export function kindAllows(schema: Schema | null, kind: string, entityId: string): boolean {
  const domains = schema?.kinds.find((k) => k.value === kind)?.domains;
  return !domains || domains.includes(entityId.split(".")[0]);
}

/** Kinds an entity can be assigned as (by domain), suggested first. */
export function kindsFor(schema: Schema | null, entityId: string, suggested: string): KindInfo[] {
  const kinds = (schema?.kinds ?? []).filter((k) => kindAllows(schema, k.value, entityId));
  return kinds.sort((a, b) => (a.value === suggested ? -1 : b.value === suggested ? 1 : 0));
}

export const AVAILABILITY: Record<Availability, { tone: "ok" | "warning" | "error" | "info" | "neutral"; label: string; help: string }> = {
  ok: { tone: "ok", label: "Online", help: "The entity reports a state." },
  unknown: { tone: "neutral", label: "No value yet", help: "The entity exists but has not reported a value since Home Assistant started. Battery devices often report only on their next change." },
  unavailable: { tone: "warning", label: "Entity unavailable", help: "Home Assistant reports this entity as unavailable, while other entities of its device (if any) still respond." },
  device_offline: { tone: "error", label: "Device offline", help: "Every entity of this physical device is unavailable: the device is unreachable (power, battery, radio or its integration)." },
  not_provided: { tone: "error", label: "Not provided", help: "The entity is registered, but its integration does not provide it (it failed to load, or the device was removed from it)." },
  disabled: { tone: "warning", label: "Disabled", help: "The entity is disabled in Home Assistant, so it has no state." },
  missing: { tone: "error", label: "Missing", help: "The entity does not exist in Home Assistant (deleted, or renamed while the integration was not running)." },
};

export const STATUS: Record<RoomStatus, { tone: "ok" | "warning" | "error" | "info" | "neutral" | "pending"; label: string }> = {
  configured: { tone: "ok", label: "Configured" },
  not_configured: { tone: "neutral", label: "Not configured" },
  disabled: { tone: "pending", label: "Disabled" },
  problem: { tone: "error", label: "Problem" },
};

export const OCCUPANCY_LABEL: Record<string, string> = {
  confirmed_occupancy: "Occupied",
  possible_occupancy: "Possibly occupied",
  vacant: "Vacant",
  unknown: "Unknown",
};

export function changeText(c: Change, name: (id: string | undefined) => string, areaName: (id: string | undefined) => string, kindLabel: (k: unknown) => string, roleLabel: (r: unknown) => string): string {
  switch (c.type) {
    case "room_added":
      return "Room configuration is created";
    case "room_removed":
      return "Room configuration is removed";
    case "room_enabled":
      return "Room is enabled (occupancy is observed)";
    case "room_disabled":
      return "Room is disabled (occupancy is no longer observed)";
    case "sensor_added":
      return `${name(c.entity_id)} added as ${kindLabel(c.kind)} · ${roleLabel(c.role)}`;
    case "sensor_removed":
      return `${name(c.entity_id)} removed (was ${kindLabel(c.kind)})`;
    case "sensor_kind":
      return `${name(c.entity_id)}: type ${kindLabel(c.old)} → ${kindLabel(c.new)}`;
    case "sensor_role":
      return `${name(c.entity_id)}: role ${roleLabel(c.old)} → ${roleLabel(c.new)}`;
    case "sensor_inverted":
      return `${name(c.entity_id)}: ${c.new ? "inverted (active when off)" : "no longer inverted"}`;
    case "sensor_enabled":
      return `${name(c.entity_id)}: ${c.new ? "included in occupancy" : "excluded from occupancy"}`;
    case "light_added":
      return `${name(c.entity_id)} added as a light`;
    case "light_removed":
      return `${name(c.entity_id)} removed from the lights`;
    case "reference_changed":
      return `Reference occupancy entity: ${c.old ? name(String(c.old)) : "none"} → ${c.new ? name(String(c.new)) : "none"}`;
    case "adjacent_added":
      return `${areaName(c.area_id)} added as an adjacent room`;
    case "adjacent_removed":
      return `${areaName(c.area_id)} removed from the adjacent rooms`;
    case "normalized":
      return "Stored format is normalized (no functional change)";
    default:
      return c.type;
  }
}

export function changeGroup(type: string): "added" | "removed" | "classification" | "settings" | "relations" {
  if (type === "sensor_added" || type === "light_added" || type === "room_added") return "added";
  if (type === "sensor_removed" || type === "light_removed" || type === "room_removed") return "removed";
  if (type === "sensor_kind" || type === "sensor_role") return "classification";
  if (type.startsWith("adjacent") || type === "reference_changed") return "relations";
  return "settings";
}

/** Reviewed refresh: replay the user's own edits (``draft`` vs ``oldBase``) onto the current configuration. */
export function rebase(oldBase: RoomConfig, draft: RoomConfig, newBase: RoomConfig): RoomConfig {
  const out = editable(newBase);
  if (draft.enabled !== oldBase.enabled) out.enabled = draft.enabled;
  const before = new Map(oldBase.sensors.map((s) => [s.entity_id, s]));
  const after = new Map(draft.sensors.map((s) => [s.entity_id, s]));
  out.sensors = out.sensors.filter((s) => !(before.has(s.entity_id) && !after.has(s.entity_id)));
  for (const [id, s] of after) {
    const old = before.get(id);
    const current = out.sensors.find((x) => x.entity_id === id);
    if (!old) {
      if (!current) out.sensors.push({ ...s });
      continue;
    }
    if (!current) continue; // removed elsewhere: the other change wins
    for (const key of ["kind", "role", "inverted", "enabled"] as const) {
      if ((old[key] ?? null) !== (s[key] ?? null)) (current as unknown as Record<string, unknown>)[key] = s[key];
    }
  }
  const removedLights = oldBase.lights.filter((l) => !draft.lights.includes(l));
  const addedLights = draft.lights.filter((l) => !oldBase.lights.includes(l));
  out.lights = [...out.lights.filter((l) => !removedLights.includes(l)), ...addedLights.filter((l) => !out.lights.includes(l))];
  if ((draft.reference_occupancy_entity ?? null) !== (oldBase.reference_occupancy_entity ?? null))
    out.reference_occupancy_entity = draft.reference_occupancy_entity;
  const removedAdj = oldBase.adjacent_rooms.filter((a) => !draft.adjacent_rooms.includes(a));
  const addedAdj = draft.adjacent_rooms.filter((a) => !oldBase.adjacent_rooms.includes(a));
  out.adjacent_rooms = [...out.adjacent_rooms.filter((a) => !removedAdj.includes(a)), ...addedAdj.filter((a) => !out.adjacent_rooms.includes(a))];
  return out;
}
