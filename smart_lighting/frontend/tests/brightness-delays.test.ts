// App 0.9.0: what the brightness matrix editor sends for cell delays.
import { describe, expect, it } from "vitest";

import { delaysPayload, delayText } from "../src/lighting/lighting-brightness";

describe("brightness cell delays", () => {
  it("sends every cell, null where there is no delay (a removed delay is removed)", () => {
    const out = delaysPayload({
      room: { none: 0, low: 30 },
      zones: { desk: { none: 65, low: 45 } },
      delays: { desk: { low: { on: 3, off: 15 } }, room: { low: { on: 0, off: 0 } } },
    });
    expect(out).toEqual({ room: { none: null, low: null }, desk: { none: null, low: { on: 3, off: 15 } } });
  });

  it("never sends a delay for an Off or Not configured cell", () => {
    const out = delaysPayload({ room: { low: 0, none: null }, zones: {}, delays: { room: { low: { on: 5, off: 5 }, none: { on: 2, off: 0 } } } });
    expect(out.room).toEqual({ low: null, none: null });
  });

  it("describes delays compactly", () => {
    expect(delayText({ on: 3, off: 15 })).toBe("Delay On 3 s · Delay Off 15 s");
    expect(delayText({ on: 0, off: 20 })).toBe("Delay Off 20 s");
    expect(delayText(null)).toBe("");
  });
});
