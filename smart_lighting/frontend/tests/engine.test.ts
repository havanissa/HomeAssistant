import { describe, expect, it } from "vitest";

import { eventText, matchedPercent, modeLabel, ownerInfo, reasonText, recordTitle, secondsBetween, type EngineRecord } from "../src/presence/engine";

describe("engine migration helpers", () => {
  it("shows the acknowledged owner, never a guess", () => {
    expect(ownerInfo("integration")).toMatchObject({ app: false, tone: "info" });
    expect(ownerInfo("app")).toMatchObject({ app: true, tone: "ok" });
    expect(ownerInfo("resume_pending")).toMatchObject({ app: true, tone: "warning" });
    expect(ownerInfo(undefined).label).toContain("Unknown");
    expect(ownerInfo("something")).toMatchObject({ app: false, tone: "neutral" });
  });

  it("labels modes and reasons", () => {
    expect(modeLabel("shadow")).toContain("not published");
    expect(modeLabel("authoritative")).toBe("Publishing occupancy");
    expect(reasonText("lease_expired")).toContain("lease");
    expect(reasonText("custom")).toBe("custom");
  });

  it("computes durations and percentages", () => {
    expect(secondsBetween("2026-11-17T12:00:00+00:00", "2026-11-17T12:01:30+00:00")).toBe(90);
    expect(secondsBetween(null, "2026-11-17T12:00:00+00:00")).toBeNull();
    expect(secondsBetween("nonsense", "2026-11-17T12:00:00+00:00")).toBeNull();
    const totals = { transitions: 4, matched: 3, timing_differences: 0, disagreements: 1, deadline_differences: 0, timing_max_ms: 0, deadline_max_s: 0 };
    expect(matchedPercent(totals)).toBe(75);
    expect(matchedPercent({ ...totals, transitions: 0, matched: 0 })).toBeNull();
  });

  it("describes records and their events", () => {
    const base: EngineRecord = { kind: "disagreement", area_id: "r", opened_at: "2026-11-17T12:00:00+00:00", duration_s: 12.5, app: null, integration: null, events: [] };
    expect(recordTitle(base)).toBe("Disagreement for 12.5 s");
    expect(recordTitle({ ...base, kind: "deadline", delta_s: 4 })).toContain("4 s");
    expect(recordTitle({ ...base, kind: "timing", duration_s: 1.2 })).toContain("1.2 s");
    const name = (id: string) => (id === "binary_sensor.pir" ? "PIR" : id);
    expect(eventText({ at: "", kind: "state", entity_id: "binary_sensor.pir", state: "on" }, name)).toBe("PIR → on");
    expect(eventText({ at: "", kind: "state", entity_id: "binary_sensor.pir", state: null }, name)).toBe("PIR → removed");
    expect(eventText({ at: "", kind: "zone_hold", zones: [] }, name)).toBe("No zone holds the room");
    expect(eventText({ at: "", kind: "preliminary", signals: ["binary_sensor.pir"] }, name)).toContain("PIR");
  });
});
