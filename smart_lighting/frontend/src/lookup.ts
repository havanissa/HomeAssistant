// Areas, devices and entities for the searchable selectors (read-only data
// from the App backend's api/lookup).
import { getJson } from "./store";

export type LookupKind = "entity" | "device" | "area";

export interface LookupArea {
  id: string;
  name: string;
  device_count: number;
  entity_count: number;
}

export interface LookupDevice {
  id: string;
  name: string;
  manufacturer: string | null;
  model: string | null;
  area_id: string | null;
  area: string | null;
  entity_count: number;
}

export interface LookupEntity {
  entity_id: string;
  name: string;
  domain: string;
  state: string | null;
  unit: string | null;
  last_changed: string | null;
  platform: string | null;
  device_id: string | null;
  device: string | null;
  area_id: string | null;
  area: string | null;
}

export interface LookupData {
  areas: LookupArea[];
  devices: LookupDevice[];
  entities: LookupEntity[];
}

export interface LookupOption {
  id: string;
  primary: string;
  secondary: string;
  search: string;
}

const MAX_AGE_MS = 60_000;
let cache: { data: LookupData; at: number } | null = null;
let pending: Promise<LookupData> | null = null;

export async function loadLookup(force = false): Promise<LookupData> {
  if (!force && cache && Date.now() - cache.at < MAX_AGE_MS) return cache.data;
  pending ??= getJson<LookupData>("api/lookup")
    .then((data) => {
      cache = { data, at: Date.now() };
      return data;
    })
    .finally(() => {
      pending = null;
    });
  return pending;
}

/** Lower-case and strip accents, so "nordur" finds "Norður" and "uti" finds "Úti". */
export function normalize(text: string): string {
  return text
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .replace(/ð/g, "d")
    .replace(/Ð/g, "d")
    .replace(/[øØ]/g, "o")
    .replace(/[æÆ]/g, "ae")
    .toLowerCase();
}

export function options(data: LookupData, kind: LookupKind): LookupOption[] {
  const join = (...parts: Array<string | null | undefined>) => parts.filter(Boolean).join(" · ");
  switch (kind) {
    case "area":
      return data.areas.map((a) => ({
        id: a.id,
        primary: a.name,
        secondary: `${a.device_count} devices · ${a.entity_count} entities`,
        search: normalize(`${a.name} ${a.id}`),
      }));
    case "device":
      return data.devices.map((d) => ({
        id: d.id,
        primary: d.name,
        secondary: join(d.area, d.manufacturer, d.model),
        search: normalize(join(d.name, d.area, d.manufacturer, d.model)),
      }));
    default:
      return data.entities.map((e) => ({
        id: e.entity_id,
        primary: e.name,
        secondary: join(e.entity_id, e.area),
        search: normalize(join(e.name, e.entity_id, e.area, e.device)),
      }));
  }
}

/** Every word of the query must match; names that start with it rank first. */
export function filterOptions(all: LookupOption[], query: string, limit: number): { items: LookupOption[]; total: number } {
  const words = normalize(query).split(/\s+/).filter(Boolean);
  if (!words.length) return { items: all.slice(0, limit), total: all.length };
  const matched = all.filter((o) => words.every((w) => o.search.includes(w)));
  const first = words[0];
  matched.sort((a, b) => {
    const ra = normalize(a.primary).startsWith(first) ? 0 : 1;
    const rb = normalize(b.primary).startsWith(first) ? 0 : 1;
    return ra - rb || a.primary.localeCompare(b.primary);
  });
  return { items: matched.slice(0, limit), total: matched.length };
}
