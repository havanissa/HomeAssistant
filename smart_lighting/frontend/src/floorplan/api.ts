// Floor plan API (Phase A2.1): shapes of /api/floorplan/* and requests.
import { apiUrl } from "../store";
import { ApiError, getRooms, postRooms, type Availability, type DeviceInfo, type EntityInfo, type RoomsStatus } from "../rooms/model";
import type { Plan } from "./model";

export interface PlanStatus {
  writable: boolean;
  read_only: string | null;
  read_only_code: string | null;
  schema_version: number;
  revision: string;
  saved_at: string | null;
  notices: Array<{ code: string; message: string }>;
  summary: { floors: number; rooms: number; linked_rooms: number; openings: number; placements: number; backgrounds: number };
}

export interface PlanDocument {
  plan: Plan;
  revision: string;
  status: PlanStatus;
}

export interface ContextEntity {
  entity_id: string;
  entity_uid: string | null;
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
  device: string | null;
  icon: string;
  group: string;
  unit: string | null;
  sensor_rooms: string[];
  light_rooms: string[];
}

export interface ContextRoom {
  configured: boolean;
  enabled: boolean;
  adjacent_rooms: string[];
  sensors: string[];
  lights: string[];
  lighting: "automatic" | "observing" | null;
  occupancy: { state: string | null; occupied: boolean | null; reason: string | null } | null;
}

export interface PlanContext {
  areas: Array<{ id: string; name: string; floor_id: string | null; icon: string | null }>;
  floors: Array<{ id: string; name: string; level: number | null; icon: string | null }>;
  entities: ContextEntity[];
  rooms: Record<string, ContextRoom>;
  room_entities: Record<string, { state?: string; mood_lighting?: string; occupied?: string }>;
  integration: RoomsStatus;
  categories: Record<string, { label: string; icon: string; group: string }>;
  units: "metric" | "imperial";
  generated_at: string;
}

export interface EntityUse {
  area_id: string;
  area: string;
  as: "sensor" | "light" | "reference" | "relay" | "switch" | "curtain";
  kind?: string;
  kind_label?: string;
  role?: string;
  role_label?: string;
  enabled?: boolean;
  inverted?: boolean;
  mode?: string;
  name?: string;
}

export interface EntityDetails {
  entity: EntityInfo & { attrs: Record<string, unknown>; entity_uid: string | null };
  device: DeviceInfo | null;
  uses: EntityUse[];
  siblings: Array<{ entity_id: string; name: string; category: string; category_label: string; state: string | null; availability: Availability; unit: string | null }>;
  integration: RoomsStatus;
}

export interface BackupInfo {
  id: string;
  created_at: string | null;
  reason: string | null;
  revision: string | null;
  summary: PlanStatus["summary"] | null;
}

export interface ImportReview {
  summary: PlanStatus["summary"];
  current: PlanStatus["summary"];
  warnings: string[];
  revision: string;
  writable: boolean;
  identical: boolean;
}

export interface SaveResult {
  saved: boolean;
  revision: string;
  saved_at: string | null;
  backup: string | null;
  warnings?: string[];
}

export const getPlan = () => getRooms<PlanDocument>("api/floorplan");
export const getContext = () => getRooms<PlanContext>("api/floorplan/context");
export const getEntityDetails = (entityId: string) => getRooms<EntityDetails>(`api/floorplan/entity/${encodeURIComponent(entityId)}`);
export const savePlan = (plan: Plan, revision: string) => postRooms<SaveResult>("api/floorplan/save", { plan, revision });
export const getBackups = async () => (await getRooms<{ backups: BackupInfo[] }>("api/floorplan/backups")).backups;
export const restoreBackup = (id: string, revision: string) => postRooms<SaveResult>(`api/floorplan/backups/${encodeURIComponent(id)}/restore`, { revision });
export const reviewImport = (file: unknown) => postRooms<ImportReview>("api/floorplan/import/review", { file });
export const importPlan = (file: unknown, revision: string) => postRooms<SaveResult>("api/floorplan/import", { file, revision });

export function imageUrl(imageId: string): string {
  return apiUrl(`api/floorplan/images/${encodeURIComponent(imageId)}`);
}

export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp"];
export const MAX_IMAGE_MB = 15;

export async function uploadImage(file: File): Promise<{ image_id: string; width_px: number; height_px: number }> {
  if (!IMAGE_TYPES.includes(file.type)) throw new ApiError("Choose a PNG, JPEG or WebP image.", "unsupported_type", 415);
  if (file.size > MAX_IMAGE_MB * 1024 * 1024) throw new ApiError(`The image is larger than ${MAX_IMAGE_MB} MB.`, "too_large", 413);
  let response: Response;
  try {
    response = await fetch(apiUrl("api/floorplan/images"), {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Content-Type": file.type, "X-Smart-Lighting": "1" },
      body: file,
    });
  } catch (err) {
    throw new ApiError(`The App could not be reached (${String(err)}).`, "network", 0);
  }
  let body: { error?: string; code?: string } & Record<string, unknown> = {};
  try {
    body = await response.json();
  } catch {
    /* not JSON */
  }
  if (!response.ok) throw new ApiError(body.error ?? `HTTP ${response.status}`, body.code ?? "http_error", response.status);
  return body as unknown as { image_id: string; width_px: number; height_px: number };
}
