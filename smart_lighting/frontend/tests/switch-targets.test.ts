import { describe, expect, it } from "vitest";

import { mappingSummary, previewTargets, refLabel, targetRefs, withTargets, type SwitchesModel, type TargetLight } from "../src/lighting/switches-model";

const light = (entity_id: string, name: string, room: string, room_name: string, extra: Partial<TargetLight> = {}): TargetLight => ({
  entity_id, name, room, room_name, group: false, members: [], online: true, brightness: true, color_temp: true, on_off_only: false, ...extra,
});

const sw = {
  actions: [{ value: "toggle", label: "Toggle" }, { value: "off", label: "Turn off" }, { value: "dim_up", label: "Dim up (hold)" }],
  targets: [],
  target_options: {
    this_room: { area_id: "kamar", name: "Kamar Suður", ref: "room:kamar", managed: true, lights: [
      light("light.ceiling", "Ceiling Light", "kamar", "Kamar Suður"),
      light("light.spots", "Spots", "kamar", "Kamar Suður", { group: true, members: ["light.spot_1", "light.spot_2"] }),
      light("light.spot_1", "Spot 1", "kamar", "Kamar Suður")] },
    adjacent: [{ area_id: "trappa", name: "Trappa", ref: "room:trappa", managed: true, lights: [
      light("light.stair", "Stairway Lamp", "trappa", "Trappa", { brightness: false, color_temp: false, on_off_only: true })] }],
    other: [{ area_id: "inngongd", name: "Inngongd", ref: "room:inngongd", managed: true, lights: [
      light("light.entrance", "Entrance Lights", "inngongd", "Inngongd", { online: false })] }],
  },
} as unknown as SwitchesModel;

describe("cross-room switch targets", () => {
  it("keeps the old single-target form when that is enough", () => {
    expect(targetRefs({ action: "on" })).toEqual(["room"]);
    expect(targetRefs({ action: "on", target: "light.ceiling" })).toEqual(["light.ceiling"]);
    expect(withTargets(["room"], sw)).toEqual({ target: "room", targets: undefined });
    expect(withTargets(["light.ceiling"], sw)).toEqual({ target: "light.ceiling", targets: undefined });
    // A light in another room, or several, need the explicit list.
    expect(withTargets(["light.stair"], sw)).toEqual({ target: "room", targets: ["light.stair"] });
    expect(withTargets(["light.ceiling", "light.stair", "light.stair"], sw)).toEqual({ target: "room", targets: ["light.ceiling", "light.stair"] });
  });

  it("names targets with their room", () => {
    expect(refLabel("room", sw)).toBe("All room lights");
    expect(refLabel("room:trappa", sw)).toBe("Trappa: all lights");
    expect(refLabel("light.stair", sw)).toBe("Stairway Lamp (Trappa)");
    expect(refLabel("light.ceiling", sw)).toBe("Ceiling Light");
    expect(mappingSummary({ action: "toggle", targets: ["light.stair"] }, sw)).toBe("Toggle Stairway Lamp (Trappa)");
    expect(mappingSummary({ action: "off", targets: ["light.ceiling", "room:trappa"] }, sw)).toBe("Turn off Ceiling Light and Trappa: all lights");
  });

  it("previews the physical lights: groups once, capabilities and offline lights", () => {
    const p = previewTargets({ action: "dim_up", targets: ["light.spots", "light.spot_1", "light.stair", "room:inngongd", "room:nowhere"] }, sw);
    expect(p.rows.map((r) => r.entity_id)).toEqual(["light.spots", "light.stair", "light.entrance"]);
    expect(p.dropped).toEqual(["light.spot_1"]);
    expect(p.missing).toEqual(["room:nowhere"]);
    const skipped = Object.fromEntries(p.rows.map((r) => [r.entity_id, r.skipped]));
    expect(skipped["light.stair"]).toMatch(/on\/off only/);
    expect(skipped["light.entrance"]).toBe("offline");
    expect(skipped["light.spots"]).toBeNull();
  });
});
