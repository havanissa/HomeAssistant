// Floor plan Presence overlay (Phase A3.1): what the existing Live map shows
// for presence. Only the integration's own values: a sensor's condition as
// the engine sees it, the room's decision and its warnings. No coverage, no
// direction and no person positions are drawn; a radar distance band is only
// listed with its configured boundaries and the measured distance.
import type { DeviceTone } from "../floorplan/live";
import type { Diagnostic, PresenceOverview, PresenceRoom, Signal } from "./model";

export interface SensorUse {
  room: PresenceRoom;
  signal: Signal;
}

/** Every assigned sensor -> the rooms that use it (a sensor may be shared). */
export function sensorIndex(data: PresenceOverview | null): Map<string, SensorUse[]> {
  const out = new Map<string, SensorUse[]>();
  for (const room of data?.rooms ?? []) {
    for (const signal of room.signals) out.set(signal.entity_id, [...(out.get(signal.entity_id) ?? []), { room, signal }]);
  }
  return out;
}

/** Warnings and errors about an entity, from every room's diagnostics. */
export function entityWarnings(data: PresenceOverview | null): Map<string, Diagnostic[]> {
  const out = new Map<string, Diagnostic[]>();
  for (const room of data?.rooms ?? []) {
    for (const d of room.diagnostics) {
      if (!d.entity_id || d.level === "info") continue;
      out.set(d.entity_id, [...(out.get(d.entity_id) ?? []), { ...d, area_id: room.area_id, room: room.name }]);
    }
  }
  return out;
}

const TONE: Record<string, DeviceTone> = {
  active: "active",
  clear: "idle",
  disabled: "off",
  ignored: "off",
  unavailable: "unavailable",
  invalid: "unknown",
  stuck: "unavailable",
};

const WORD: Record<string, string> = {
  active: "Detected",
  clear: "Clear",
  disabled: "Excluded",
  ignored: "Ignored",
  unavailable: "Unavailable",
  invalid: "Not understood",
  stuck: "Stuck",
};

/** Tone and text of a presence sensor icon: the engine's view (a door shows open / closed from its own state). */
export function overlayView(uses: SensorUse[]): { tone: DeviceTone; text: string } {
  const s = uses[0].signal;
  // Shared sensors: the most significant condition across rooms.
  const order = ["stuck", "unavailable", "invalid", "active", "clear", "disabled", "ignored"];
  const condition = uses.map((u) => u.signal.condition).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0];
  const tone = TONE[condition] ?? "idle";
  if (s.kind === "door" && (condition === "active" || condition === "clear")) return { tone, text: s.label };
  return { tone, text: WORD[condition] ?? condition };
}
