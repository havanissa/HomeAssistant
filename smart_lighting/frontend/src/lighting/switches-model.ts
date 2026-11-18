// Phase A4: Switches page, the model as the integration's editor answer carries it (``room.switches``,
// ``room.priority``, ``room.lights[].override``) and plain-word helpers. Read only; no command is sent here.
import type { ReactiveControllerHost } from "lit";

import type { Tone } from "../ui";

export type SourceKind = "zigbee2mqtt" | "device_trigger" | "event_entity";

export interface SwitchEvent {
  source: string;
  raw: string;
  label: string;
  button?: string;
  press?: string;
  at: string | null;
  new?: boolean;
}

export interface SwitchStatus {
  sources: string[];
  authoritative: string | null;
  available_sources: string[];
  topic: string | null;
  availability: string | null;
  problem: string | null;
  last_event: SwitchEvent | null;
  last_result: string | null;
  last_result_ok: boolean | null;
  last_outcome: string | null;
  last_command: string | null;
  override: boolean;
  last_ignored: { at: string | null; source: string; reason: string; text: string } | null;
  ignored_updates: number;
  history: { at: string | null; raw: string; label: string; result: string; ok?: boolean | null; outcome?: string; source: string }[];
}

export interface Mapping {
  action: string;
  target?: string;
  on_values?: "mood" | "last" | "fixed";
  brightness?: number | null;
  color_temp?: number | null;
  mood?: string | null;
  scope?: "room" | "global";
  entity?: string | null;
  option?: string | null;
  minutes?: number | null;
}

export interface ButtonRow {
  raw: string;
  label: string;
  button: string | null;
  press: string | null;
  source: string | null;
  learned_at: string | null;
  mapping: Mapping | null;
  describe: string;
}

export interface Competing {
  entity_id: string;
  name: string | null;
  enabled: boolean;
  via: string;
  buttons: string[];
}

export interface Dimming {
  step: number;
  hold_step: number;
  interval: number;
  min_brightness: number;
  max_brightness: number;
  timeout: number;
}

export interface SwitchItem {
  id: string;
  name: string;
  device_id: string | null;
  ieee: string | null;
  z2m_name: string | null;
  topic: string | null;
  event_entity: string | null;
  source: string;
  mode: "listen" | "control";
  activated_at: string | null;
  activated_by: string | null;
  acknowledged: string[];
  dimming: Dimming;
  debounce_ms: number;
  activity_evidence: boolean;
  override_minutes: number | null;
  status: SwitchStatus;
  decisions: { latest: Decision | null; entries: Decision[]; size: number } | null;
  device: { exists?: boolean; name?: string | null; manufacturer?: string | null; model?: string | null; [k: string]: unknown };
  manufacturer: string | null;
  model: string | null;
  description: string | null;
  connection: string;
  topic_now: string | null;
  event_entity_now: string | null;
  buttons_list: ButtonRow[];
  declared: string[];
  bindings: { cluster: string; target: string; endpoint?: string | number }[];
  competing: Competing[];
  warnings: { code: string; text: string }[];
  operating: { mode: string; state: "observing" | "active" | "blocked" | "attention"; blocked_by: string | null; text: string };
  can_control: boolean;
  learning: { active: boolean; until: string | null; captured: SwitchEvent[] } | null;
  dimming_now: unknown;
}

export interface Decision {
  id?: string;
  at: string | null;
  action: string;
  explanation: string;
  status: string;
  [k: string]: unknown;
}

export interface SwitchesModel {
  items: SwitchItem[];
  mqtt: "not_configured" | "waiting" | "connected" | "disconnected" | string;
  mqtt_configured: boolean;
  settings: { mqtt_base_topic: string };
  legacy_entities: string[];
  actions: { value: string; label: string }[];
  ignored_words: Record<string, string>;
  targets: { entity_id: string; name: string | null; group: boolean }[];
  scenes: { entity_id: string; name: string }[];
  scripts: { entity_id: string; name: string }[];
  wled_presets: { entity_id: string; name: string | null; options: string[] }[];
  global_mood_helper: string | null;
  global_mood_options: string[];
}

export interface Candidate {
  device_id: string | null;
  ieee: string | null;
  name: string;
  z2m_name: string | null;
  manufacturer: string | null;
  model: string | null;
  description: string | null;
  area: string | null;
  available: boolean;
  sources: string[];
  event_entities: string[];
  actions: string[];
  bindings: number;
  assigned_room: string | null;
}

export interface CandidatesAnswer {
  candidates: Candidate[];
  mqtt: string;
  mqtt_configured: boolean;
  bridge_seen_at: string | null;
  base_topic: string;
}

export interface PriorityInput {
  id: string;
  name: string;
  entity_id: string | null;
  enabled: boolean;
  active_when: "on" | "off";
  targets: string[];
  brightness_pct: number | null;
  appearance: "none" | "mood" | "kelvin";
  kelvin: number | null;
  debounce: number;
  release_delay: number;
  unavailable: "hold" | "release" | "keep";
  grace: number;
  override_manual: boolean;
  release_manual: "respect" | "resume";
  enabled_at: string | null;
  enabled_by: string | null;
}

export interface PriorityStatus {
  id: string;
  status: "active" | "inactive" | "disabled" | "unavailable" | "missing" | "locked" | "settling" | string;
  active: boolean;
  since: string | null;
  input_state: string | null;
  reading: boolean | null;
  timer: "entry" | "release" | "grace" | null;
  timer_due: string | null;
  unavailable_since: string | null;
  history: { at: string | null; text: string }[];
}

export interface PriorityModel {
  lights: { entity_id: string; name: string; type: string; onoff: boolean; state: string | null;
    claim: { inputs: string[]; names: string[]; winner: string; winner_name: string; brightness_pct: number | null } | null;
    rule: string | null; decision: string | null; reason: string | null }[];
  inputs: (PriorityInput & { status: PriorityStatus | null; entity_name: string | null; missing_targets: string[] })[];
  candidates: { entity_id: string; name: string; domain: string; device_class: string | null; in_area: boolean;
    state: string | null; chosen: boolean; contact: boolean }[];
  lock: boolean;
  settled: boolean;
  limits: { max_inputs: number; max_targets: number; delay: [number, number]; grace: [number, number]; kelvin: [number, number] };
}

export interface PriorityPreview {
  input: PriorityInput;
  lights: ({ entity_id: string; missing: true } | { entity_id: string; missing?: false; name: string; onoff: boolean;
    now: PlanItemBrief; forced: PlanItemBrief })[];
  notes: string[];
  sends_now: boolean;
  locked: boolean;
}

export interface PlanItemBrief {
  decision: string;
  reason: string;
  data: Record<string, unknown>;
  service: string | null;
  rule: { name?: string; kind?: string } | null;
}

export interface Override {
  entity_id: string;
  source: string;
  since: string | null;
  until: string | null;
  user_id?: string | null;
  state?: string | null;
  switch?: string;
  switch_id?: string;
  action?: string;
  reason?: string;
}

export interface RoomLight {
  entity_id: string;
  name: string | null;
  state?: string | null;
  override?: Override | null;
  missing?: boolean;
}

export interface RoomModel {
  area_id: string;
  name: string;
  enabled: boolean;
  switches: SwitchesModel;
  priority: PriorityModel | null;
  lights: RoomLight[];
  diagnostics: { manual_override_minutes?: number | null; [k: string]: unknown };
}

export interface EditorAnswer {
  can_edit: boolean;
  moods: { name: string }[];
  rooms: { area_id: string; name: string; active: boolean; control_mode: string }[];
  room?: RoomModel;
}

/** What the Switches page needs from its element (the mapping and Priority Input editors). */
export interface SwitchesHost extends ReactiveControllerHost {
  readonly canEdit: boolean;
  busy: boolean;
  run(action: () => Promise<unknown>, done?: string): Promise<boolean>;
}

// ------------------------------------------------------------------ words
export const SOURCE_WORDS: Record<string, string> = {
  zigbee2mqtt: "Zigbee2MQTT (through MQTT)",
  device_trigger: "MQTT device triggers",
  event_entity: "Home Assistant event entity",
  auto: "Automatic (best available)",
  discovered: "declared by the device",
};

export const SHORT_SOURCE: Record<string, string> = {
  zigbee2mqtt: "Zigbee2MQTT", device_trigger: "Device triggers", event_entity: "Event entity",
};

export const MQTT_WORDS: Record<string, { tone: Tone; label: string; text: string }> = {
  not_configured: { tone: "neutral", label: "MQTT not set up", text: "MQTT is not set up in Home Assistant: Zigbee2MQTT switches and MQTT device triggers need it. Event entities work without it." },
  waiting: { tone: "pending", label: "MQTT starting", text: "Waiting for Home Assistant's MQTT connection." },
  connected: { tone: "ok", label: "MQTT connected", text: "MQTT is connected: Zigbee2MQTT and MQTT device-trigger switches are received." },
  disconnected: { tone: "warning", label: "MQTT disconnected", text: "MQTT is disconnected: presses of MQTT switches are not received until it reconnects." },
};

export const OPERATING: Record<string, { tone: Tone; label: string }> = {
  observing: { tone: "neutral", label: "Observing" },
  active: { tone: "ok", label: "Active" },
  blocked: { tone: "warning", label: "Active · blocked" },
  attention: { tone: "warning", label: "Active · needs attention" },
};

export const OVERRIDE_SOURCES: Record<string, string> = {
  ui: "Changed in Home Assistant (a person)",
  wall_switch: "Wall switch or remote",
  unattributed: "Changed at the device (not attributed)",
  profile_test: "Profile test",
  automation: "An automation",
};

export const PRIORITY_STATUS: Record<string, { tone: Tone; label: string }> = {
  active: { tone: "ok", label: "Active · claiming" },
  inactive: { tone: "neutral", label: "Armed · waiting" },
  disabled: { tone: "neutral", label: "Disabled" },
  unavailable: { tone: "warning", label: "Input unavailable" },
  missing: { tone: "warning", label: "Input missing" },
  locked: { tone: "warning", label: "Locked" },
  settling: { tone: "pending", label: "Starting" },
};

export const LIGHT_ACTIONS = ["on", "off", "toggle", "brightness_up", "brightness_down", "dim_up", "dim_down", "dim_stop",
  "set_brightness", "set_color_temp"];

export function deviceLine(x: SwitchItem | Candidate): string {
  return [x.manufacturer, x.model].filter(Boolean).join(" ") || x.description || "";
}

/** A mapping with the defaults that make a new choice valid at once (as the integration's card does). */
export function withDefaults(m: Mapping, sw: SwitchesModel, moods: string[], changed: Partial<Mapping>): Mapping {
  const out: Mapping = { ...m, ...changed };
  if (changed.action) {
    if (out.action === "set_brightness" && !out.brightness) out.brightness = 50;
    if (out.action === "set_color_temp" && !out.color_temp) out.color_temp = 3000;
    if (out.action === "mood" && !out.mood) out.mood = out.scope === "global" ? sw.global_mood_options.find((o) => o !== "Auto") : moods[0];
    if (out.action === "scene" && !String(out.entity ?? "").startsWith("scene.")) out.entity = sw.scenes[0]?.entity_id ?? null;
    if (out.action === "script" && !String(out.entity ?? "").startsWith("script.")) out.entity = sw.scripts[0]?.entity_id ?? null;
    if (out.action === "wled_preset" && !sw.wled_presets.some((p) => p.entity_id === out.entity)) {
      out.entity = sw.wled_presets[0]?.entity_id ?? null;
      out.option = sw.wled_presets[0]?.options[0] ?? null;
    }
  }
  if (changed.scope && out.action === "mood" && changed.scope === "global") {
    out.mood = sw.global_mood_options.find((o) => o === out.mood) ?? sw.global_mood_options.find((o) => o !== "Auto") ?? null;
  }
  if (out.action === "on" && out.on_values === "fixed" && !out.brightness && !out.color_temp) out.brightness = 100;
  return out;
}

/** The mapping as ``switch/mapping`` takes it (empty fields left out; "none" means unassigned). */
export function mappingMessage(m: Mapping): Mapping | null {
  if (!m.action || m.action === "none") return null;
  return Object.fromEntries(Object.entries(m).filter(([, v]) => v !== null && v !== undefined && v !== "")) as unknown as Mapping;
}

export function sameMapping(a: Mapping | null, b: Mapping | null): boolean {
  const key = (m: Mapping | null) => {
    const msg = mappingMessage(m ?? { action: "none" });
    return msg ? JSON.stringify(Object.entries(msg).sort(([x], [y]) => x.localeCompare(y))) : "none";
  };
  return key(a) === key(b);
}
