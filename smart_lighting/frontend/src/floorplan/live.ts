// Live view (Phase A2.1): what the floor plan shows for a room or a device,
// from Home Assistant's current states only. Nothing is estimated: unknown or
// unavailable values are shown as such, never as vacant or off.
import type { Availability } from "../rooms/model";
import type { LiveState } from "../types";
import type { ContextEntity, PlanContext } from "./api";

export type DeviceTone = "active" | "idle" | "off" | "unknown" | "unavailable" | "missing";

export interface DeviceView {
  tone: DeviceTone;
  /** Short state text ("On · 70 %", "21.4 °C", "Unavailable"). */
  text: string;
  /** Brightness or position in percent, when reported. */
  level: number | null;
}

export const DEVICE_TONE_LABEL: Record<DeviceTone, string> = {
  active: "Active / on / open",
  idle: "Reporting (not active)",
  off: "Off / closed",
  unknown: "No value yet",
  unavailable: "Unavailable or offline",
  missing: "Not found in Home Assistant",
};

const BAD: Availability[] = ["unavailable", "device_offline", "not_provided", "disabled"];

export function deviceView(entityId: string, live: LiveState | undefined, entity: ContextEntity | undefined): DeviceView {
  const availability = (live?.availability ?? entity?.availability ?? "missing") as Availability;
  const state = live ? live.state : (entity?.state ?? null);
  const attrs = (live as (LiveState & { attrs?: Record<string, unknown> }) | undefined)?.attrs ?? {};
  if (!entity && !live) return { tone: "missing", text: "Not in Home Assistant", level: null };
  if (availability === "missing") return { tone: "missing", text: "Not in Home Assistant", level: null };
  if (BAD.includes(availability)) return { tone: "unavailable", text: availability === "device_offline" ? "Device offline" : availability === "disabled" ? "Disabled" : "Unavailable", level: null };
  if (state === null || state === "unknown" || availability === "unknown") return { tone: "unknown", text: "No value yet", level: null };
  const domain = entityId.split(".")[0];
  const dclass = (attrs.device_class as string | undefined) ?? null;
  switch (domain) {
    case "binary_sensor": {
      const on = state === "on";
      const words = dclass === "door" || dclass === "window" || dclass === "opening" || dclass === "garage_door" ? ["Open", "Closed"] : dclass === "motion" || dclass === "occupancy" || dclass === "presence" ? ["Detected", "Clear"] : ["On", "Off"];
      return { tone: on ? "active" : "idle", text: on ? words[0] : words[1], level: null };
    }
    case "light":
    case "switch":
    case "input_boolean": {
      const on = state === "on";
      const b = typeof attrs.brightness === "number" ? Math.round(((attrs.brightness as number) / 255) * 100) : null;
      return { tone: on ? "active" : "off", text: on ? (b !== null ? `On · ${b} %` : "On") : "Off", level: on ? b : null };
    }
    case "cover": {
      const pos = typeof attrs.current_position === "number" ? (attrs.current_position as number) : null;
      const label: Record<string, string> = { open: "Open", closed: "Closed", opening: "Opening", closing: "Closing", stopped: "Stopped" };
      return { tone: state === "closed" ? "off" : "active", text: `${label[state] ?? state}${pos !== null ? ` · ${pos} %` : ""}`, level: pos };
    }
    case "sensor": {
      const unit = (attrs.unit_of_measurement as string | undefined) ?? entity?.unit ?? "";
      const n = Number(state);
      const value = Number.isFinite(n) && state.trim() !== "" ? (Math.abs(n) >= 100 ? Math.round(n).toString() : (Math.round(n * 10) / 10).toString()) : state;
      return { tone: "idle", text: unit ? `${value} ${unit}` : value, level: null };
    }
    case "event": {
      const when = Date.parse(state);
      const recent = Number.isFinite(when) && Date.now() - when < 30_000;
      return { tone: recent ? "active" : "idle", text: recent ? "Pressed" : "Waiting", level: null };
    }
    case "device_tracker":
    case "person":
      return { tone: state === "home" ? "active" : "idle", text: state === "home" ? "Home" : state === "not_home" ? "Away" : state, level: null };
    default:
      return { tone: state === "on" ? "active" : "idle", text: state, level: null };
  }
}

export type RoomTone = "occupied" | "possible" | "vacating" | "vacant" | "uncertain" | "unavailable" | "not_observed" | "not_linked";

export interface RoomLive {
  tone: RoomTone;
  label: string;
  reason: string | null;
  /** "Automatic", "Observing", "Paused (Manual mood)", "Blocked" or null when not reported. */
  lighting: string | null;
  overrides: number | null;
}

export const ROOM_TONE_LABEL: Record<RoomTone, string> = {
  occupied: "Occupied",
  possible: "Possibly occupied",
  vacating: "Vacancy pending",
  vacant: "Vacant",
  uncertain: "Uncertain",
  unavailable: "Occupancy unavailable",
  not_observed: "Not observed by Smart Occupancy",
  not_linked: "Not linked to an area",
};

const OCCUPANCY: Record<string, RoomTone> = {
  confirmed_occupancy: "occupied",
  possible_occupancy: "possible",
  possible_vacancy: "vacating",
  vacant: "vacant",
  uncertain: "uncertain",
};

const LIGHTING: Record<string, string> = { active: "Automatic", observe: "Observing", paused: "Paused (Manual mood)", blocked: "Blocked" };

export function roomLive(areaId: string | null, ctx: PlanContext | null, states: Map<string, LiveState>): RoomLive {
  if (!areaId) return { tone: "not_linked", label: ROOM_TONE_LABEL.not_linked, reason: null, lighting: null, overrides: null };
  const ids = ctx?.room_entities[areaId];
  const cfg = ctx?.rooms[areaId];
  if (!ids?.state || !cfg?.enabled) {
    return { tone: "not_observed", label: ROOM_TONE_LABEL.not_observed, reason: null, lighting: cfg?.lighting === "automatic" ? "Automatic" : cfg?.lighting === "observing" ? "Observing" : null, overrides: null };
  }
  const s = states.get(ids.state) as (LiveState & { attrs?: Record<string, unknown> }) | undefined;
  const light = ids.mood_lighting ? (states.get(ids.mood_lighting) as (LiveState & { attrs?: Record<string, unknown> }) | undefined) : undefined;
  const lighting = light && light.availability === "ok" && light.state ? (LIGHTING[light.state] ?? light.state) : cfg.lighting === "automatic" ? "Automatic" : cfg.lighting === "observing" ? "Observing" : null;
  const overrides = typeof light?.attrs?.manual_overrides === "number" ? (light.attrs.manual_overrides as number) : null;
  if (!s) return { tone: "uncertain", label: "Waiting for the integration", reason: null, lighting, overrides };
  if (s.availability !== "ok" || !s.state) return { tone: "unavailable", label: ROOM_TONE_LABEL.unavailable, reason: null, lighting, overrides };
  const tone = OCCUPANCY[s.state] ?? "uncertain";
  return { tone, label: ROOM_TONE_LABEL[tone], reason: typeof s.attrs?.reason === "string" ? (s.attrs.reason as string) : null, lighting, overrides };
}

export const GROUPS: Array<{ key: string; label: string; icon: string }> = [
  { key: "presence", label: "Presence and contacts", icon: "motion-sensor" },
  { key: "lighting", label: "Lights", icon: "lightbulb" },
  { key: "switches", label: "Switches and remotes", icon: "light-switch" },
  { key: "climate", label: "Temperature and light level", icon: "thermometer" },
  { key: "curtains", label: "Curtains and covers", icon: "curtains" },
  { key: "other", label: "Other", icon: "puzzle" },
];
