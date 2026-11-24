import { describe, expect, it } from "vitest";

import { beforeAfter, rangeOf } from "./Audit";
import { price } from "./Settings";

describe("audit history", () => {
  it("sends no range by default, so the API's last 7 days apply", () => {
    expect(rangeOf(null)).toEqual({});
  });

  it("turns a relative range into from and to", () => {
    const now = new Date("2026-11-24T12:00:00Z");
    expect(rangeOf({ type: "relative", amount: 7, unit: "day" }, now)).toEqual({
      from: "2026-11-17T12:00:00.000Z",
      to: "2026-11-24T12:00:00.000Z",
    });
  });

  it("passes an absolute range through as ISO times", () => {
    const range = rangeOf({ type: "absolute", startDate: "2026-11-01T00:00:00Z", endDate: "2026-11-02T00:00:00Z" });
    expect(range).toEqual({ from: "2026-11-01T00:00:00.000Z", to: "2026-11-02T00:00:00.000Z" });
  });
});

describe("audit event panel", () => {
  it("leaves out Before and After when the event has neither (a sign-out)", () => {
    expect(beforeAfter({})).toBeNull();
    expect(beforeAfter(null)).toBeNull();
    expect(beforeAfter({ before: null, after: null })).toBeNull();
    expect(beforeAfter({ before: {}, after: {}, step: "tenant" })).toBeNull();
  });

  it("shows a dash, never null, for the one empty side", () => {
    expect(beforeAfter({ before: null, after: { status: "active" } })).toEqual({
      before: "-",
      after: JSON.stringify({ status: "active" }, null, 2),
    });
    expect(beforeAfter({ before: { role: "tenant-user" } })).toEqual({
      before: JSON.stringify({ role: "tenant-user" }, null, 2),
      after: "-",
    });
  });

  it("keeps values that are present but falsy", () => {
    expect(beforeAfter({ before: false, after: 0 })).toEqual({ before: "false", after: "0" });
  });
});

describe("settings", () => {
  it("shows each model's price per million tokens", () => {
    expect(price({ price_in: "1.000000", price_out: "5.000000" })).toBe(
      "$1.000000 in / $5.000000 out per million tokens",
    );
  });
});
