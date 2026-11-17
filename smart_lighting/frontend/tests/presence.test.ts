// Unit tests of the presence logic (Phase A3.1): countdowns from the engine's deadlines, timing inheritance,
// filters, the visit chain from the presence log, and the debugging timeline (Recorder + log + live).
import { describe, expect, it } from "vitest";

import {
  ago,
  bestUnit,
  buildEvents,
  clock,
  clockOffset,
  countdown,
  diagnosticLink,
  duration,
  interpret,
  matches,
  secondsUntil,
  segments,
  timeFormatter,
  timingChange,
  timingError,
  timingViews,
  toSeconds,
  visitChain,
  type LogEntry,
  type PresenceRoom,
  type TimelineData,
  type TimingSchemaItem,
} from "../src/presence/model";

const SCHEMA: TimingSchemaItem[] = [
  { key: "inactivity_timeout", label: "Vacancy timeout", help: "", unit: "s", min: 30, max: 14400, default: 600, basic: true, zero: null, min_nonzero: null, applies_to: null },
  { key: "correlation_window", label: "Occupancy confirmation", help: "", unit: "s", min: 1, max: 120, default: 10, basic: true, zero: null, min_nonzero: null, applies_to: null },
  { key: "trigger_stuck_after", label: "PIR stuck after", help: "", unit: "s", min: 0, max: 86400, default: 1800, basic: false, zero: "off", min_nonzero: 60, applies_to: null },
];

function room(over: Partial<PresenceRoom> = {}): PresenceRoom {
  return {
    area_id: "kitchen", name: "Kitchen", icon: null, floor_id: null, area_exists: true, configured: true, enabled: true, revision: "0123456789abcdef",
    sensors: [], timing: [{ key: "correlation_window", label: "", help: "", min: 1, max: 120, default: 10, global: 10, value: 10, overridden: false, basic: true }],
    timing_overrides: {}, adjacent_rooms: [], reference_occupancy_entity: null, entities: {}, problems: [], diagnostics: [],
    automation: { lighting_automatic: false, lighting_paused: false, vacancy_action: "turn_off", curtains: [], curtains_automatic: [] },
    zones: [], occupancy: null, signals: [], explain: null, tags: [], confirm_at: null,
    counts: { sensors: 0, active: 0, unavailable: 0, excluded: 0, errors: 0, warnings: 0, info: 0, overrides: 0 },
    ...over,
  };
}

describe("clock and durations", () => {
  it("uses the integration's clock for countdowns", () => {
    const received = Date.parse("2026-11-17T12:00:00Z");
    const offset = clockOffset("2026-11-17T12:00:05Z", received); // browser 5 s behind
    expect(offset).toBe(5000);
    expect(secondsUntil("2026-11-17T12:01:05Z", offset, received)).toBe(60);
    expect(secondsUntil(null, offset, received)).toBeNull();
    expect(clockOffset("garbage", received)).toBe(0);
  });
  it("formats", () => {
    expect(clock(42)).toBe("0:42");
    expect(clock(245.2)).toBe("4:06");
    expect(clock(3723)).toBe("1:02:03");
    expect(clock(-3)).toBe("0:00");
    expect(clock(null)).toBe("—");
    expect(duration(45)).toBe("45 s");
    expect(duration(600)).toBe("10 min");
    expect(duration(90)).toBe("1 min 30 s");
    expect(duration(5400)).toBe("1 h 30 min");
    expect(duration(7200)).toBe("2 h");
    expect(duration(172800)).toBe("2 d");
    expect(ago(2)).toBe("just now");
    expect(ago(125)).toBe("2 min ago");
  });
  it("keeps exact units in the inputs", () => {
    expect(bestUnit(600)).toBe("min");
    expect(bestUnit(7200)).toBe("h");
    expect(bestUnit(90)).toBe("s");
    expect(bestUnit(0)).toBe("s");
    expect(toSeconds(1.5, "min")).toBe(90);
  });
  it("shows 24-hour times in Home Assistant's time zone", () => {
    const fmt = timeFormatter("Atlantic/Faroe");
    const text = fmt(new Date().toISOString().slice(0, 10) + "T21:05:09Z");
    expect(text).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(text.startsWith("21") || text.startsWith("22")).toBe(true); // UTC+0 / +1 (summer)
    expect(timeFormatter("Not/AZone")("2026-01-01T00:00:00Z")).not.toBe("—"); // falls back to the browser
    expect(fmt(null)).toBe("—");
  });
});

describe("timing inheritance", () => {
  const global = { inactivity_timeout: 900, correlation_window: 10, trigger_stuck_after: 1800 };
  it("room: inherits, overrides and restores the global value", () => {
    let views = timingViews(SCHEMA, "room", global, { correlation_window: 20 }, {});
    expect(views.map((v) => [v.key, v.effective, v.overridden, v.changed])).toEqual([
      ["inactivity_timeout", 900, false, false], ["correlation_window", 20, true, false], ["trigger_stuck_after", 1800, false, false]]);
    views = timingViews(SCHEMA, "room", global, { correlation_window: 20 }, { inactivity_timeout: 300, correlation_window: null });
    expect(views.map((v) => [v.key, v.effective, v.overridden, v.changed])).toEqual([
      ["inactivity_timeout", 300, true, true], ["correlation_window", 10, false, true], ["trigger_stuck_after", 1800, false, false]]);
    expect(timingChange(views)).toEqual({ inactivity_timeout: 300, correlation_window: null });
    // Setting the same value as stored is not a change.
    expect(timingChange(timingViews(SCHEMA, "room", global, { correlation_window: 20 }, { correlation_window: 20 }))).toEqual({});
    // Reset all: every override goes.
    views = timingViews(SCHEMA, "room", global, { correlation_window: 20 }, {}, true);
    expect(views[1].effective).toBe(10);
    // ... and typing the old value again keeps it (it is sent with the reset).
    views = timingViews(SCHEMA, "room", global, { correlation_window: 20 }, { correlation_window: 20 }, true);
    expect(timingChange(views)).toEqual({ correlation_window: 20 });
  });
  it("global: a default restores the engine's default", () => {
    const views = timingViews(SCHEMA, "global", global, {}, { inactivity_timeout: null });
    expect(views[0].effective).toBe(600);
    expect(views[0].changed).toBe(true);
    expect(views[1].overridden).toBe(false);
    expect(timingViews(SCHEMA, "global", global, {}, {})[0].overridden).toBe(true); // 900 differs from 600
  });
  it("validates like the integration", () => {
    expect(timingError(SCHEMA[0], 10)).toMatch(/From 30 s/);
    expect(timingError(SCHEMA[2], 30)).toMatch(/at least 1 min/);
    expect(timingError(SCHEMA[2], 0)).toBeNull();
    expect(timingError(SCHEMA[0], 12.5)).toMatch(/Whole/);
    expect(timingViews(SCHEMA, "global", global, {}, { trigger_stuck_after: 30 })[2].error).toBeTruthy();
  });
});

describe("dashboard", () => {
  it("filters and searches", () => {
    const r = room({ tags: ["occupied", "problems"], signals: [{ entity_id: "binary_sensor.radar_1", name: "Radar" } as PresenceRoom["signals"][0]] });
    expect(matches(r, "all", "")).toBe(true);
    expect(matches(r, "occupied", "")).toBe(true);
    expect(matches(r, "vacant", "")).toBe(false);
    expect(matches(r, "all", "kitch radar")).toBe(true);
    expect(matches(r, "all", "bedroom")).toBe(false);
  });
  it("counts down only from the engine's deadlines", () => {
    const now = Date.parse("2026-11-17T12:00:00Z");
    const occ = { state: "possible_vacancy", vacancy_at: "2026-11-17T12:05:00Z", required_quiet: 600 } as PresenceRoom["occupancy"];
    expect(countdown(room({ occupancy: occ }), 0, now)).toEqual({ label: "Vacant in", seconds: 300, total: 600 });
    const possible = { state: "possible_occupancy", vacancy_at: null } as unknown as PresenceRoom["occupancy"];
    expect(countdown(room({ occupancy: possible }), 0, now)).toBeNull(); // no deadline known: nothing invented
    expect(countdown(room({ occupancy: possible, confirm_at: "2026-11-17T12:00:06Z" }), 0, now)?.seconds).toBe(6);
    expect(countdown(room({ occupancy: { state: "confirmed_occupancy" } as PresenceRoom["occupancy"] }), 0, now)).toBeNull();
  });
  it("links diagnostics to the right screen", () => {
    const d = { code: "x", level: "warning", basis: "heuristic", title: "", detail: "", action: "", target: "timing", entity_id: null, setting: "exit_confirmation" } as const;
    expect(diagnosticLink(d, "kitchen")).toBe("#/presence/kitchen?tab=timing&setting=exit_confirmation");
    expect(diagnosticLink({ ...d, target: "sensors", entity_id: "binary_sensor.a" }, "kitchen")).toBe("#/presence/kitchen?sensor=binary_sensor.a");
    expect(diagnosticLink({ ...d, target: "rooms_editor" }, "kitchen")).toBe("#/rooms/kitchen/presence");
    expect(diagnosticLink({ ...d, target: "room" }, null)).toBe("#/presence/settings");
  });
});

const T = (s: number): string => new Date(Date.parse("2026-11-17T12:00:00Z") + s * 1000).toISOString();
function tr(seq: number, s: number, prev: string, state: string, evidence: string[] = [], extra: Partial<LogEntry> = {}): LogEntry {
  return { seq, time: T(s), kind: "transition", area_id: "kitchen", previous_state: prev as LogEntry["state"], state: state as LogEntry["state"], evidence: evidence.map((n) => ({ entity_id: `binary_sensor.${n.toLowerCase()}`, name: n, status: "active" })), ...extra };
}

describe("visit chain", () => {
  it("tells the engine's decisions of the current visit", () => {
    const log = [
      tr(1, 0, "possible_vacancy", "vacant", [], { reason: "Vacant: no activity for 10 min." }),
      tr(2, 10, "vacant", "possible_occupancy", ["PIR"]),
      tr(3, 14, "possible_occupancy", "confirmed_occupancy", ["PIR", "Radar"]),
      tr(4, 200, "confirmed_occupancy", "possible_vacancy", [], { required_quiet: 300 }),
    ];
    expect(visitChain(log, "kitchen").map((s) => s.text)).toEqual([
      "PIR detected", "Waiting for confirmation", "Radar confirmed presence", "Occupied", "Every signal cleared",
      "Vacancy confirmation started (waiting 5 min)",
    ]);
    expect(visitChain(log, "hall")).toEqual([]);
    expect(visitChain([...log, tr(5, 500, "possible_vacancy", "vacant", [], { reason: "Vacant: no activity for 5 min." })], "kitchen").at(-1)?.text)
      .toBe("Vacant: no activity for 5 min");
  });
});

describe("timeline", () => {
  const data: TimelineData = {
    area_id: "kitchen", window: 3600, windows: [900, 3600], start: T(0), end: T(3600), now: T(3600),
    entities: { state: "sensor.kitchen_state", reason: "sensor.kitchen_reason" },
    sensors: [{ entity_id: "binary_sensor.pir", kind: "pir", role: "trigger", name: "PIR" }, { entity_id: "binary_sensor.pc", kind: "pc_activity", role: "maintain", inverted: true, name: "PC" }],
    state_values: { active: ["on", "home"], inactive: ["off"] },
    history: {
      available: true, error: null, truncated: [], series: {
        "binary_sensor.pir": [{ t: T(0), s: "off" }, { t: T(100), s: "on" }, { t: T(160), s: "off" }, { t: T(170), s: "unavailable" }],
        "binary_sensor.pc": [{ t: T(0), s: "on" }, { t: T(300), s: "off" }],
        "sensor.kitchen_state": [{ t: T(0), s: "vacant" }, { t: T(101), s: "possible_occupancy" }, { t: T(2000), s: "vacant" }],
        "sensor.kitchen_reason": [{ t: T(101), s: "Possible occupancy: PIR" }],
      },
    },
    log: [tr(9, 1500, "possible_occupancy", "possible_vacancy"), tr(10, 2000, "possible_vacancy", "vacant"),
          { seq: 11, time: T(2500), kind: "config", area_id: "kitchen", changes: [{ type: "timing_override", key: "inactivity_timeout", old: null, new: 300 }], source: "app", applied: "live" }],
    log_seq: 11, time_zone: "UTC",
  };
  it("reads values like the engine, inverted included", () => {
    expect(interpret("on", false, data.state_values)).toBe("active");
    expect(interpret("home", true, data.state_values)).toBe("inactive");
    expect(interpret("weird", false, data.state_values)).toBe("unknown");
    expect(interpret("unavailable", true, data.state_values)).toBe("unavailable");
    expect(interpret(null, false, null)).toBe("unknown");
  });
  it("merges the Recorder, the engine's log and live events", () => {
    const live = [{ id: "live-1", time: T(3000), kind: "sensor" as const, entity_id: "binary_sensor.pir", title: "PIR activated", detail: null, state: "on", tone: "ok" as const, source: "live" as const },
                  { id: "live-dup", time: T(300.5), kind: "sensor" as const, entity_id: "binary_sensor.pc", title: "PC activated", detail: null, state: "off", tone: "ok" as const, source: "live" as const }];
    const events = buildEvents(data, live);
    expect(events.map((e) => [e.source, e.kind, e.title])).toEqual([
      ["recorder", "sensor", "PIR activated"],
      ["recorder", "occupancy", "Vacant → Possible occupancy"], // before the log begins: from the Recorder
      ["recorder", "sensor", "PIR cleared"],
      ["recorder", "availability", "PIR became unavailable"],
      ["recorder", "sensor", "PC activated"], // inverted: off = active
      ["engine", "occupancy", "Possible occupancy → Vacancy pending"],
      ["engine", "occupancy", "Vacancy pending → Vacant"], // the Recorder's copy is not repeated
      ["engine", "config", "Presence settings changed in the App"],
      ["live", "sensor", "PIR activated"],
    ]);
    expect(events[1].detail).toBe("Possible occupancy: PIR");
    expect(buildEvents(data, [], 3).length).toBe(3);
  });
  it("builds lanes over the window", () => {
    const start = Date.parse(T(0));
    const end = Date.parse(T(3600));
    const lane = segments(data.history.series["binary_sensor.pir"], start, end, (s) => interpret(s, false, data.state_values));
    expect(lane.map((x) => [x.value, (x.end - x.start) / 1000])).toEqual([["inactive", 100], ["active", 60], ["inactive", 10], ["unavailable", 3430]]);
    expect(segments([], start, end, (s) => s)).toEqual([]);
    // A point before the window starts the first segment at the window start.
    expect(segments([{ t: T(-50), s: "on" }], start, end, (s) => s)[0].start).toBe(start);
  });
});
