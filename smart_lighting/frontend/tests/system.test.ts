import { describe, expect, it } from "vitest";

import { companionText, nextStep, ownerText, stepTone } from "../src/system/api";
import { lightingText, occupancyText } from "../src/pages/dashboard";

describe("Phase A6 system helpers", () => {
  it("names every owner the companion can report", () => {
    expect(ownerText("app")).toMatchObject({ tone: "ok" });
    expect(ownerText("integration")).toMatchObject({ tone: "info" });
    expect(ownerText("none")).toMatchObject({ tone: "warning" });
    expect(ownerText("app_away").label.toLowerCase()).toContain("away");
    expect(ownerText("app_away").tone).toBe("warning");
  });

  it("describes the companion state without a session as absent, not as an error", () => {
    expect(companionText(null).tone).toBe("neutral");
    expect(companionText({ state: "absent", error: null, trusted: false } as never).tone).toBe("info");
    expect(companionText({ state: "present", error: null, trusted: false, paired: false } as never).tone).toBe("warning");
    expect(companionText({ state: "present", error: null, trusted: true, paired: true } as never).tone).toBe("ok");
    expect(companionText({ state: "incompatible", error: "x", trusted: false } as never).tone).toBe("error");
  });

  it("picks the first step that is not done as the current one", () => {
    const steps = [
      { key: "inspect", status: "done" }, { key: "backup", status: "done" }, { key: "validate", status: "blocked" },
      { key: "transfer_config", status: "pending" },
    ] as never;
    expect(nextStep(steps)).toBe("validate");
    expect(stepTone("done")).toBe("ok");
    expect(stepTone("blocked")).toBe("error");
    expect(stepTone("reviewed")).toBe("warning");
    expect(stepTone("pending")).toBe("pending");
  });
});

describe("dashboard texts", () => {
  it("marks stale occupancy explicitly instead of inferring vacancy", () => {
    expect(occupancyText("occupied", true)).toMatchObject({ tone: "ok", label: "Occupied" });
    expect(occupancyText("occupied", false).label).toContain("not fresh");
    expect(occupancyText(null, true).label).toBe("Unknown");
  });

  it("summarises a room's lighting mode", () => {
    expect(lightingText({ mode: "observe", active: false, overrides: 0, paused: false, mood: null }).label).toBe("Observing");
    expect(lightingText({ mode: "active", active: true, overrides: 2, paused: false, mood: null }).label).toContain("2 overrides");
    expect(lightingText({ mode: "paused", active: true, overrides: 0, paused: true, mood: null }).tone).toBe("warning");
  });
});
