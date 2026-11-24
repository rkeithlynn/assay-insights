import { describe, expect, it } from "vitest";

import { canSee, landingFor, screensFor } from "./navigation";

describe("navigation", () => {
  it("shows each role only its screens", () => {
    expect(screensFor("tenant-admin").map((s) => s.id)).toEqual(["home", "users", "settings"]);
    expect(screensFor("tenant-user").map((s) => s.id)).toEqual(["home"]);
    expect(screensFor("master-admin").map((s) => s.id)).toEqual(["tenants", "audit"]);
    expect(screensFor("master-user").map((s) => s.id)).toEqual(["tenants", "audit"]);
    expect(screensFor("someone-else")).toEqual([]);
  });

  it("never shows tenant screens to master roles or the reverse", () => {
    expect(canSee("master-admin", "/users")).toBe(false);
    expect(canSee("tenant-user", "/users")).toBe(false);
    expect(canSee("tenant-admin", "/tenants")).toBe(false);
    expect(canSee("tenant-admin", "/audit")).toBe(false);
    expect(canSee("tenant-user", "/settings")).toBe(false);
    expect(canSee("master-user", "/settings")).toBe(false);
  });

  it("lands master roles on Tenants and tenant roles on Home", () => {
    expect(landingFor("master-user")).toBe("/tenants");
    expect(landingFor("tenant-admin")).toBe("/");
  });
});
