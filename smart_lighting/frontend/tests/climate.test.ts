// Phase A7.0: Climate unit handling (everything is stored in °C) and zone ids.
import { describe, expect, it } from "vitest";

import { delta, fromDisplay, slugify, temp, toDisplay } from "../src/climate/api";

describe("climate units", () => {
  it("shows °C values in the display unit and converts input back to °C", () => {
    expect(temp(20, "°C")).toBe("20.0 °C");
    expect(temp(20, "°F")).toBe("68.0 °F");
    expect(temp(null, "°C")).toBe("—");
    expect(toDisplay(21.11, "°F")).toBeCloseTo(70, 1);
    expect(fromDisplay(70, "°F")).toBe(21.11);
    expect(fromDisplay(21.5, "°C")).toBe(21.5);
  });

  it("converts a difference without the 32° offset", () => {
    expect(delta(10, "°F")).toBe("+18.0 °F");
    expect(delta(-2.5, "°C")).toBe("-2.5 °C");
    expect(delta(null, "°C")).toBe("—");
  });

  it("makes unique, readable zone ids", () => {
    expect(slugify("Stova + Eldhús", new Set())).toBe("stova_eldhus");
    expect(slugify("Stova + Eldhús", new Set(["stova_eldhus"]))).toBe("stova_eldhus_2");
    expect(slugify("!!!", new Set())).toBe("zone");
  });
});
